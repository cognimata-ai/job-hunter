"""The api's watch of the worker heartbeat (spec §17.6 L5642, §16.5 L5442; plan M5 Task 3, O-ops O11, Q9).

`worker.heartbeat_stale` (critical, subject `worker`) is raised by a task of the api's lifespan, never by a
GET handler: a heartbeat older than 60 s on the database clock, or none, once the api has been up for the
start grace; a fresh one resolves it. A pass that fails is logged with its error class and the watch goes on;
the lifespan's end stops it.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
import sqlalchemy as sa
from jobhunter.api import app as app_module
from jobhunter.heartbeats import HEARTBEAT_STALE, heartbeat_fresh
from jobhunter.observability import heartbeat_watch as hw
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.logs import logged

pytestmark = pytest.mark.integration

STALE: Final = "worker.heartbeat_stale"
T0: Final = datetime(2026, 11, 9, 12, tzinfo=UTC)
GRACE_END: Final = T0 + timedelta(seconds=hw.START_GRACE_S)  # the first moment it may raise
_ALERTS: Final = sa.text(
    "SELECT id, severity, dedup_key, occurrences, context, resolved_at FROM alerts WHERE code = :c "
    "ORDER BY id"
)
_BEAT: Final = sa.text("UPDATE worker_heartbeat SET heartbeat_at = now() - :age WHERE id = 1")


async def alerts(engine: AsyncEngine) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(_ALERTS, {"c": STALE})).all())


async def beat(engine: AsyncEngine, age: timedelta) -> None:
    """The worker's last heartbeat `age` ago, on the database clock."""
    async with engine.begin() as conn:
        await conn.execute(_BEAT, {"age": age})


def test_the_watch_runs_every_30_s_after_a_120_s_grace_on_the_60_s_rule() -> None:
    assert (hw.HEARTBEAT_WATCH_INTERVAL_S, hw.START_GRACE_S) == (30.0, 120.0)  # the brief's values
    assert HEARTBEAT_STALE == timedelta(seconds=60)  # readiness's rule, shared (`heartbeats`)
    assert heartbeat_fresh(timedelta(seconds=60)) and not heartbeat_fresh(
        timedelta(seconds=60, microseconds=1)
    )
    assert not heartbeat_fresh(None)  # no heartbeat at all


async def test_a_stale_heartbeat_raises_after_the_start_grace(
    engine: AsyncEngine, current_epoch: int
) -> None:
    await beat(engine, timedelta(seconds=61))
    assert await hw.heartbeat_watch_pass(engine, now=GRACE_END, started_at=T0) == "stale"
    [alert] = await alerts(engine)
    assert (alert.severity, alert.dedup_key, alert.occurrences, alert.resolved_at) == (
        "critical",
        f"{STALE}:worker",
        1,
        None,
    )
    assert alert.context["age_s"] >= 61  # its age on the database clock, a number only
    # still stale: the open alert counts one more occurrence
    assert (
        await hw.heartbeat_watch_pass(engine, now=GRACE_END + timedelta(seconds=30), started_at=T0) == "stale"
    )
    # no heartbeat at all is stale too
    async with engine.begin() as conn:
        await conn.execute(sa.text("DELETE FROM worker_heartbeat"))
    assert (
        await hw.heartbeat_watch_pass(engine, now=GRACE_END + timedelta(seconds=60), started_at=T0) == "stale"
    )
    assert [(a.occurrences, a.resolved_at) for a in await alerts(engine)] == [(3, None)]


async def test_within_the_start_grace_nothing_is_raised(engine: AsyncEngine, current_epoch: int) -> None:
    await beat(engine, timedelta(minutes=10))
    just_before = GRACE_END - timedelta(milliseconds=1)
    assert await hw.heartbeat_watch_pass(engine, now=T0, started_at=T0) == "grace"
    assert await hw.heartbeat_watch_pass(engine, now=just_before, started_at=T0) == "grace"
    async with engine.begin() as conn:
        await conn.execute(sa.text("DELETE FROM worker_heartbeat"))
    assert await hw.heartbeat_watch_pass(engine, now=just_before, started_at=T0) == "grace"
    assert await alerts(engine) == []


async def test_a_fresh_heartbeat_resolves(engine: AsyncEngine, current_epoch: int) -> None:
    await beat(engine, timedelta(seconds=90))
    assert await hw.heartbeat_watch_pass(engine, now=GRACE_END, started_at=T0) == "stale"
    await beat(engine, timedelta(seconds=10))
    assert await hw.heartbeat_watch_pass(engine, now=GRACE_END, started_at=T0) == "fresh"
    [alert] = await alerts(engine)
    assert alert.resolved_at is not None
    # a later stale heartbeat opens a new alert; an api that starts while it is open resolves it at its
    # first pass, within its own grace, once the heartbeat is fresh
    await beat(engine, timedelta(seconds=90))
    assert await hw.heartbeat_watch_pass(engine, now=GRACE_END, started_at=T0) == "stale"
    await beat(engine, timedelta(seconds=59))
    assert await hw.heartbeat_watch_pass(engine, now=T0, started_at=T0) == "fresh"
    assert [a.resolved_at is not None for a in await alerts(engine)] == [True, True]


async def test_the_watch_survives_a_failing_pass(
    engine: AsyncEngine, current_epoch: int, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    await beat(engine, timedelta(minutes=5))
    stop = asyncio.Event()
    nows: list[datetime] = []
    real = hw.heartbeat_watch_pass

    async def failing_once(engine: AsyncEngine, *, now: datetime, started_at: datetime) -> str:
        nows.append(now)
        if len(nows) == 1:
            raise RuntimeError("must not be logged")
        verdict = await real(engine, now=now, started_at=started_at)
        stop.set()
        return verdict

    times = iter([T0, GRACE_END, GRACE_END + timedelta(seconds=30)])  # its start, then each pass
    monkeypatch.setattr(hw, "heartbeat_watch_pass", failing_once)
    monkeypatch.setattr(hw, "HEARTBEAT_WATCH_INTERVAL_S", 0.01)
    async with asyncio.timeout(10):
        await hw.watch_heartbeat(engine, clock=lambda: next(times), stop=stop)
    assert nows == [GRACE_END, GRACE_END + timedelta(seconds=30)]  # the failed pass, then the next one
    [event] = logged(caplog, "heartbeat_watch_failed")
    assert event["error_class"] == "RuntimeError"
    assert "must not be logged" not in caplog.text
    [alert] = await alerts(engine)  # the pass after the failure raised it
    assert (alert.severity, alert.resolved_at) == ("critical", None)


async def test_the_lifespan_stops_the_watch(
    engine: AsyncEngine, current_epoch: int, make_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await beat(engine, timedelta(seconds=90))
    assert await hw.heartbeat_watch_pass(engine, now=GRACE_END, started_at=T0) == "stale"  # an earlier api's
    await beat(engine, timedelta(0))
    passed = asyncio.Event()
    real = hw.heartbeat_watch_pass

    async def observed(engine: AsyncEngine, *, now: datetime, started_at: datetime) -> str:
        verdict = await real(engine, now=now, started_at=started_at)
        passed.set()
        return verdict

    monkeypatch.setattr(hw, "heartbeat_watch_pass", observed)
    cfg = make_config("api")
    monkeypatch.setattr(app_module, "startup_or_exit", lambda role: cfg)
    api = app_module.create_app().api  # nothing injected: the lifespan runs
    async with asyncio.timeout(10):
        async with api.router.lifespan_context(api):
            watch = api.state.heartbeat_watch
            await passed.wait()  # its first pass, at the start: the heartbeat is fresh
            [alert] = await alerts(engine)
            assert alert.resolved_at is not None
            assert not watch.done()
    assert watch.done() and not watch.cancelled() and watch.exception() is None
