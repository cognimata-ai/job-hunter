import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.worker import reconcilers as r
from jobhunter.worker.tasks import defer_in_tx

pytestmark = pytest.mark.integration


async def test_failure_is_persisted_isolated_and_resolved(
    engine, current_epoch, queue_app, monkeypatch, caplog
) -> None:
    state = {"fail": True}

    async def flaky(ctx: r.ReconcileContext) -> int:
        if state["fail"]:
            raise RuntimeError("detail that must not be stored")
        return 0

    async def ok(ctx: r.ReconcileContext) -> int:
        return 3

    monkeypatch.setattr(r, "REGISTRY", {})
    r.reconciler("flaky", "tick")(flaky)
    r.reconciler("ok", "tick")(ok)
    r.reconciler("daily_only", "daily")(ok)
    ctx = r.ReconcileContext(engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow(), epoch=1)
    assert await r.run_reconcilers(ctx, "tick") == {"flaky": -1, "ok": 3}
    assert "reconciler_failed" in caplog.text  # the failure is logged, without the exception message
    assert "must not be stored" not in caplog.text
    await r.run_reconcilers(ctx, "tick")
    async with engine.connect() as c:
        row = (
            await c.execute(
                sa.text(
                    "SELECT severity, occurrences, context, message FROM alerts "
                    "WHERE code='reconciler.failed' AND resolved_at IS NULL"
                )
            )
        ).one()
    assert tuple(row[:3]) == ("warn", 2, {"error_class": "RuntimeError", "reconciler": "flaky"})
    assert "must not be stored" not in row[3]
    state["fail"] = False
    await r.run_reconcilers(ctx, "tick")
    async with engine.connect() as c:
        assert (
            await c.execute(
                sa.text("SELECT count(*) FROM alerts WHERE code='reconciler.failed' AND resolved_at IS NULL")
            )
        ).scalar_one() == 0


async def test_alert_write_failure_is_logged_and_does_not_stop_the_other_reconcilers(
    engine, current_epoch, queue_app, monkeypatch, caplog
) -> None:
    async def flaky(ctx: r.ReconcileContext) -> int:
        raise RuntimeError("detail that must not be stored")

    async def ok(ctx: r.ReconcileContext) -> int:
        return 3

    async def broken_raise_alert(*args, **kwargs):
        raise ConnectionError("alert write detail that must not be logged")

    monkeypatch.setattr(r, "REGISTRY", {})
    r.reconciler("flaky", "tick")(flaky)
    r.reconciler("ok", "tick")(ok)
    monkeypatch.setattr(r, "raise_alert", broken_raise_alert)
    ctx = r.ReconcileContext(engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow(), epoch=1)
    assert await r.run_reconcilers(ctx, "tick") == {"flaky": -1, "ok": 3}
    events = [record.msg["event"] for record in caplog.records if isinstance(record.msg, dict)]
    # The failure itself is logged first, then the failed attempt to persist it.
    assert events.index("reconciler_failed") < events.index("reconciler_alert_write_failed")
    logged = next(
        rec.msg for rec in caplog.records if rec.msg.get("event") == "reconciler_alert_write_failed"
    )
    assert (logged["reconciler"], logged["error_class"]) == ("flaky", "ConnectionError")
    assert "must not be" not in caplog.text
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM alerts"))).scalar_one() == 0


async def test_resolve_failure_is_logged_and_does_not_stop_the_other_reconcilers(
    engine, current_epoch, queue_app, monkeypatch, caplog
) -> None:
    async def first(ctx: r.ReconcileContext) -> int:
        return 1

    async def second(ctx: r.ReconcileContext) -> int:
        return 2

    async def broken_resolve_alert(*args, **kwargs):
        raise ConnectionError("resolve detail that must not be logged")

    monkeypatch.setattr(r, "REGISTRY", {})
    r.reconciler("first", "tick")(first)
    r.reconciler("second", "tick")(second)
    monkeypatch.setattr(r, "resolve_alert", broken_resolve_alert)
    ctx = r.ReconcileContext(engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow(), epoch=1)
    assert await r.run_reconcilers(ctx, "tick") == {"first": 1, "second": 2}  # both succeeded
    failed = [rec.msg for rec in caplog.records if rec.msg.get("event") == "reconciler_alert_write_failed"]
    assert [(m["reconciler"], m["error_class"]) for m in failed] == [
        ("first", "ConnectionError"),
        ("second", "ConnectionError"),
    ]
    assert "must not be" not in caplog.text


async def test_heavy_metrics_refresh_sets_table_sizes_and_the_open_alert_gauge(
    engine, current_epoch, queue_app
) -> None:
    from jobhunter.observability.alerts import raise_alert
    from jobhunter.observability.metrics import ALERTS_OPEN, REGISTRY, TABLE_BYTES, refresh_table_bytes

    tables = ("runs", "procrastinate_jobs", "checkpoints", "alerts")

    def table_bytes() -> dict[str, float | None]:
        return {
            table: REGISTRY.get_sample_value("jobhunter_table_bytes", {"table": table}) for table in tables
        }

    async with engine.begin() as c:
        await raise_alert(c, code="x.test", severity="critical", subject="s", message="m")
    TABLE_BYTES.clear()  # whatever an earlier test set: only the reconciler below may set them
    ALERTS_OPEN.clear()
    ctx = r.ReconcileContext(engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow(), epoch=1)
    assert (await r.run_reconcilers(ctx, "heavy"))["metrics_refresh"] == 0  # a refresh repairs nothing
    set_by_reconciler = table_bytes()
    assert REGISTRY.get_sample_value("jobhunter_alerts_open", {"severity": "critical"}) == 1
    # Every public table has at least its primary key's index.
    assert all(size is not None and size > 0 for size in set_by_reconciler.values()), set_by_reconciler
    async with engine.connect() as c:
        sizes = await refresh_table_bytes(c)  # the one collector, which system health reuses (ruling T13-c)
    assert {table: sizes[table] for table in tables} == set_by_reconciler  # nothing was written in between
    assert "alembic_version" in sizes and not any(name.startswith("pg_") for name in sizes)


# --- Ruling FR-h: the alert writes are fenced, and a fenced cadence stops ----------------------------------

_TAKEOVER = sa.text("UPDATE worker_heartbeat SET worker_epoch = worker_epoch + 1")
_FAILED_ALERT = sa.text(
    "SELECT occurrences, resolved_at IS NULL AS open FROM alerts WHERE code = 'reconciler.failed'"
)


async def test_a_superseded_worker_stops_the_cadence_and_never_writes_reconciler_failed(
    engine, current_epoch, queue_app, monkeypatch, caplog
) -> None:
    """A newer worker took over: a reconciler of the superseded one that fails raises no
    `reconciler.failed`, one that succeeds resolves none, and one fenced out itself is no failure. Each stops
    the cadence there (`reconcilers_fenced`): the reconcilers after it do not run."""
    from jobhunter.errors import FencedOut

    from tests.support.logs import logged

    state = {"fail": True, "fenced": False}
    ran: list[str] = []

    async def flaky(ctx: r.ReconcileContext) -> int:
        ran.append("flaky")
        if state["fenced"]:
            raise FencedOut(owner="worker_epoch")
        if state["fail"]:
            raise RuntimeError("detail")
        return 0

    async def after(ctx: r.ReconcileContext) -> int:
        ran.append("after")
        return 1

    monkeypatch.setattr(r, "REGISTRY", {})
    r.reconciler("flaky", "tick")(flaky)
    r.reconciler("after", "tick")(after)
    ctx = r.ReconcileContext(engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow(), epoch=1)
    assert await r.run_reconcilers(ctx, "tick") == {"flaky": -1, "after": 1}  # the current epoch: recorded
    async with engine.begin() as c:
        assert tuple((await c.execute(_FAILED_ALERT)).one()) == (1, True)
        await c.execute(_TAKEOVER)
    ran.clear()
    assert await r.run_reconcilers(ctx, "tick") == {"flaky": -1}  # its alert write was fenced: stop
    state["fail"] = False
    assert await r.run_reconcilers(ctx, "tick") == {"flaky": 0}  # its resolution was fenced: stop
    state["fenced"] = True
    assert await r.run_reconcilers(ctx, "tick") == {}  # fenced out itself: no failure, stop
    assert ran == ["flaky", "flaky", "flaky"]
    async with engine.connect() as c:
        assert tuple((await c.execute(_FAILED_ALERT)).one()) == (1, True)  # never raised again or resolved
    assert [(e["cadence"], e["reconciler"]) for e in logged(caplog, "reconcilers_fenced")] == [
        ("tick", "flaky")
    ] * 3
    assert len(logged(caplog, "reconciler_failed")) == 2  # the two real failures; the fenced pass is none
    assert logged(caplog, "reconciler_alert_write_failed") == []
