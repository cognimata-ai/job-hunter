import pytest
import sqlalchemy as sa
from jobhunter.errors import FencedOut
from jobhunter.observability.alerts import open_alert, raise_alert, refresh_alert_gauge, resolve_alert
from jobhunter.observability.metrics import REGISTRY
from jobhunter.worker.epoch import fence_epoch

pytestmark = pytest.mark.integration


async def test_dedup_resolve_and_gauge(engine) -> None:
    async with engine.begin() as c:
        a = await raise_alert(
            c, code="run.orphaned_queue", severity="warn", subject="run:5", message="m", context={"run_id": 5}
        )
        assert (
            await raise_alert(c, code="run.orphaned_queue", severity="warn", subject="run:5", message="m")
            == a
        )
        assert (
            await c.execute(sa.text("SELECT occurrences, context FROM alerts WHERE id=:i"), {"i": a})
        ).one() == (
            2,
            {"run_id": 5},
        )
        assert await refresh_alert_gauge(c) == {"info": 0, "warn": 1, "critical": 0}
        assert await resolve_alert(c, code="run.orphaned_queue", subject="run:5") is True
        assert (
            await raise_alert(c, code="run.orphaned_queue", severity="warn", subject="run:5", message="m")
            != a
        )


async def test_gauge_is_set_for_every_severity_and_resolving_nothing_is_false(engine) -> None:
    async with engine.begin() as c:
        await raise_alert(c, code="a.one", severity="critical", subject="x", message="m")
        await raise_alert(c, code="a.two", severity="critical", subject="x", message="m")
        await raise_alert(c, code="a.three", severity="info", subject="x", message="m")
        # The same code with another subject is another alert; the first raise's severity stays on a repeat.
        await raise_alert(c, code="a.one", severity="info", subject="y", message="m")
        await raise_alert(c, code="a.one", severity="info", subject="x", message="other")
        assert await refresh_alert_gauge(c) == {"info": 2, "warn": 0, "critical": 2}
        assert {
            severity: REGISTRY.get_sample_value("jobhunter_alerts_open", {"severity": severity})
            for severity in ("info", "warn", "critical")
        } == {"info": 2, "warn": 0, "critical": 2}
        assert await resolve_alert(c, code="a.one", subject="x") is True
        assert await resolve_alert(c, code="a.one", subject="x") is False
        assert await resolve_alert(c, code="a.unknown", subject="x") is False
        assert await refresh_alert_gauge(c) == {"info": 2, "warn": 0, "critical": 1}
        assert REGISTRY.get_sample_value("jobhunter_alerts_open", {"severity": "critical"}) == 1


# --- `open_alert` (F6 review, Minor 2; fix wave F7) ---------------------------------------------------------

_ROWS = sa.text(
    "SELECT id, severity, message, context, occurrences, first_at, last_at, resolved_at IS NULL AS open "
    "FROM alerts WHERE dedup_key = :k ORDER BY id"
)


async def rows(engine, key: str) -> list[tuple[object, ...]]:
    async with engine.connect() as c:
        return [tuple(row) for row in await c.execute(_ROWS, {"k": key})]


async def test_open_alert_opens_one_only_when_none_is_open_and_counts_no_occurrence(engine) -> None:
    async with engine.begin() as c:
        first = await open_alert(
            c, code="source.auth_failed", severity="critical", subject="i:1", message="m", context={"n": 1}
        )
    assert isinstance(first, int)
    [(row_id, severity, message, context, occurrences, first_at, last_at, is_open)] = await rows(
        engine, "source.auth_failed:i:1"
    )
    assert (row_id, severity, message, context, occurrences, is_open) == (
        first,
        "critical",
        "m",
        {"n": 1},
        1,
        True,
    )
    # Open already (acknowledged or not): left as it is, whatever the second call says; no occurrence counted.
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE alerts SET acknowledged_at = now() WHERE id = :i"), {"i": first})
    for _ in range(2):
        async with engine.begin() as c:  # a transaction of its own: `now()` moved on, `last_at` must not
            again = await open_alert(
                c,
                code="source.auth_failed",
                severity="info",
                subject="i:1",
                message="other",
                context={"n": 2},
            )
        assert again is None
    assert await rows(engine, "source.auth_failed:i:1") == [
        (first, "critical", "m", {"n": 1}, 1, first_at, last_at, True)
    ]
    # The same code with another subject is another alert.
    async with engine.begin() as c:
        other = await open_alert(
            c, code="source.auth_failed", severity="critical", subject="i:2", message="m"
        )
    assert isinstance(other, int) and other != first
    # Once resolved, the next call opens a new row; the resolved one stays as it was.
    async with engine.begin() as c:
        assert await resolve_alert(c, code="source.auth_failed", subject="i:1") is True
        reopened = await open_alert(c, code="source.auth_failed", severity="warn", subject="i:1", message="n")
    assert isinstance(reopened, int) and reopened > first
    after = await rows(engine, "source.auth_failed:i:1")
    assert [(r[0], r[1], r[4], r[7]) for r in after] == [
        (first, "critical", 1, False),
        (reopened, "warn", 1, True),
    ]
    # A raise of the open one still counts its occurrence: `open_alert` left what `raise_alert` relies on.
    async with engine.begin() as c:
        assert (
            await raise_alert(c, code="source.auth_failed", severity="warn", subject="i:1", message="n")
            == reopened
        )
    assert (await rows(engine, "source.auth_failed:i:1"))[-1][4] == 2


async def test_open_alert_respects_the_fence_of_its_transaction(engine, current_epoch) -> None:
    """`open_alert` runs on the caller's transaction (the startup sync's, behind the worker's epoch fence): it
    opens under the current epoch, and a superseded worker's fenced transaction opens nothing; an alert opened
    in a transaction that rolls back is not opened either."""
    key = "source.auth_failed:i:3"
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE worker_heartbeat SET worker_epoch = worker_epoch + 1"))  # a takeover
    with pytest.raises(FencedOut) as fenced:
        async with engine.begin() as c:
            await fence_epoch(c, current_epoch)
            await open_alert(c, code="source.auth_failed", severity="critical", subject="i:3", message="m")
    assert fenced.value.worker_superseded and await rows(engine, key) == []
    with pytest.raises(FencedOut):
        async with engine.begin() as c:  # opened, then a fence of the same transaction refuses: rolled back
            assert await open_alert(
                c, code="source.auth_failed", severity="critical", subject="i:3", message="m"
            )
            await fence_epoch(c, current_epoch)
    assert await rows(engine, key) == []
    async with engine.begin() as c:
        await fence_epoch(c, current_epoch + 1)
        opened = await open_alert(
            c, code="source.auth_failed", severity="critical", subject="i:3", message="m"
        )
    assert [r[0] for r in await rows(engine, key)] == [opened]
