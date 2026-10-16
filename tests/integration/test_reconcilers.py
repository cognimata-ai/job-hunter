import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.worker import reconcilers as r
from jobhunter.worker.tasks import defer_in_tx

pytestmark = pytest.mark.integration


async def test_failure_is_persisted_isolated_and_resolved(engine, queue_app, monkeypatch, caplog) -> None:
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
