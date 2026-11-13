"""The dead-man pings (spec §17.7; plan M5 Task 5, O-ops Q6, O10, O18, M5-R2).

A run that ends `succeeded` or `partial` pings the check, one that ends `failed` or `failed_invariant` pings
its `/fail`; only a scheduled or catch-up run does, or a manual run whose window holds the slot it made
`skipped_overlap` (O18). The fast path pings right after the run-ending commit, from the graph's `finalize`
node (`finalize_from_graph`) and from `runs._finalize`; the `deadman_ping` reconciler (the tick) pings every
run of `runs_to_ping` above the watermark `deadman_state.last_pinged_run_id`, so the ends no finalize writes
(`resume_exhausted`, `orphaned_queue`) and a ping lost to a crash after the commit are sent, once. The
watermark only moves forward, and only past a run it pinged (or passed over). A worker's start arms a
watermark never set (Ruling T5-2): past the runs that ended before it, with no ping, so a database with
history never replays it.

respx serves healthchecks (`tests.support.healthchecks`; no network) and the pings' retry sleep waits for
nothing. Every run is invented.
"""

import asyncio
import dataclasses
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.errors import FencedOut
from jobhunter.graphs.collect.deps import CollectDeps
from jobhunter.graphs.collect.nodes.finalize import finalize_from_graph
from jobhunter.graphs.collect.state import CollectState, CollectUpdate, initial_state
from jobhunter.observability import deadman
from jobhunter.observability.deadman import (
    DEADMAN_LOCK_KEY,
    DeadmanPinger,
    HealthchecksClient,
    RunToPing,
    deadman_lock,
    runs_to_ping,
)
from jobhunter.worker import runs
from jobhunter.worker.reconcilers import ReconcileContext
from jobhunter.worker.runs import RunOwner
from jobhunter.worker.tasks import defer_in_tx
from jobhunter.worker.tick import run_tick
from langgraph.graph import END, START, StateGraph
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from tests.support.collect import insert_revisions_awaiting, make_deps, planned_request, queued_run
from tests.support.deadman import admitted, deadman_state, manual_queued_ago, scheduled_run, tick
from tests.support.healthchecks import API_KEY, API_URL, PING_URL, FakeCheck, fake_healthchecks
from tests.support.pg import PgServer, pg_one

pytestmark = pytest.mark.integration

SLOT: Final = datetime(2026, 11, 9, 0, tzinfo=UTC)
SIX_HOURS: Final = timedelta(hours=6)
_SCHEDULE: Final = sa.text(
    "UPDATE schedule_state SET anchor_utc = :anchor, interval_s = 3600, last_evaluated_slot = :cursor, "
    "enabled = true"
)
_RUN: Final = sa.text("SELECT status, status_reason FROM runs WHERE id = :id")


async def no_wait(seconds: float) -> None:
    return None


@pytest.fixture
def hc() -> Iterator[FakeCheck]:
    with fake_healthchecks() as (check, _):
        yield check


@pytest.fixture
async def pinger(engine: AsyncEngine, current_epoch: int, hc: FakeCheck) -> AsyncIterator[DeadmanPinger]:
    """The worker's pinger of epoch 1 on the respx healthchecks."""
    async with deadman.make_http_client(user_agent="JobHunter/test") as http:
        client = HealthchecksClient(http, ping_url=PING_URL, api_url=API_URL, api_key=API_KEY, sleep=no_wait)
        yield DeadmanPinger(engine, client, epoch=current_epoch)


def deps_of(engine: AsyncEngine, pinger: DeadmanPinger | None) -> CollectDeps:
    """The collection deps of a run that fetches nothing, with `pinger` as their dead-man."""
    return dataclasses.replace(make_deps(engine, lambda instance: pytest.fail("no adapter")), deadman=pinger)


async def finalized(engine: AsyncEngine, owner: RunOwner, pinger: DeadmanPinger | None) -> str:
    """`owner`'s run ended by the graph's `finalize` node, whose fast path pings with `pinger`."""
    state = initial_state(owner.run_id, owner.attempt_no)
    return await finalize_from_graph(deps_of(engine, pinger), owner, state)


def raising_graph() -> runs.RunGraph:
    """A one-node graph whose node raises: `execute_run` ends its run `failed('bug')` (`runs._finalize`)."""

    async def boom(state: CollectState) -> CollectUpdate:
        raise RuntimeError("the node broke")

    graph = StateGraph(CollectState)
    graph.add_node("boom", boom)
    graph.add_edge(START, "boom")
    graph.add_edge("boom", END)
    return graph.compile()


async def run_end(engine: AsyncEngine, run_id: int) -> tuple[str, str | None]:
    async with engine.connect() as conn:
        status, reason = (await conn.execute(_RUN, {"id": run_id})).one()
    return status, reason


async def watermark(engine: AsyncEngine) -> int | None:
    mark: int | None = (await deadman_state(engine)).last_pinged_run_id
    return mark


async def test_scheduled_success_and_partial_ping_success(
    engine: AsyncEngine, pinger: DeadmanPinger, hc: FakeCheck
) -> None:
    first = await admitted(engine, await scheduled_run(engine, slot=SLOT))
    assert await finalized(engine, first, pinger) == "succeeded"
    assert hc.pings == ["success"] and await watermark(engine) == first.run_id

    second = await admitted(engine, await scheduled_run(engine, slot=SLOT + SIX_HOURS, trigger="catch_up"))
    await insert_revisions_awaiting(engine, 1)  # the run's revision awaits its embedding: `partial`
    assert await finalized(engine, second, pinger) == "partial"
    # collection is alive: `partial` pings the check, not its `/fail` (§17.7)
    assert hc.pings == ["success", "success"] and await watermark(engine) == second.run_id
    assert hc.status == "up" and hc.api_keys_right == []  # a ping is no API call


async def test_failed_and_failed_invariant_ping_fail(
    engine: AsyncEngine, pinger: DeadmanPinger, hc: FakeCheck
) -> None:
    # `runs._finalize`'s fast path: the graph raised, `execute_run` ends the run failed('bug')
    broken = await scheduled_run(engine, slot=SLOT)
    graph = raising_graph()
    assert await runs.execute_run(engine, graph, broken, 1, epoch=1, deadman=pinger) == "started"
    assert await run_end(engine, broken) == ("failed", "bug")
    assert hc.pings == ["fail"] and await watermark(engine) == broken

    # the `finalize` node's fast path: a request of the run never ended, so I2 fails
    owner = await admitted(engine, await scheduled_run(engine, slot=SLOT + SIX_HOURS))
    await planned_request(engine, owner, provider="arbeitnow", key=b"deadman-i2", attempt_seq=1)
    assert await finalized(engine, owner, pinger) == "failed_invariant"
    assert await run_end(engine, owner.run_id) == ("failed_invariant", "I2")
    assert hc.pings == ["fail", "fail"] and hc.status == "down"
    assert await watermark(engine) == owner.run_id


async def test_a_manual_run_never_pings(
    engine: AsyncEngine, queue_app: procrastinate.App, pinger: DeadmanPinger, hc: FakeCheck
) -> None:
    owner = await queued_run(engine)  # a manual run, admitted
    assert await finalized(engine, owner, pinger) == "succeeded"
    assert hc.pings == [] and await watermark(engine) is None
    await tick(engine, queue_app, pinger, now=utcnow())  # nor does the tick send one later
    assert hc.pings == [] and await watermark(engine) is None
    async with engine.connect() as conn:
        assert await runs_to_ping(conn, after_run_id=0) == []


async def test_a_manual_run_covering_a_skipped_slot_pings(
    engine: AsyncEngine, queue_app: procrastinate.App, pinger: DeadmanPinger, hc: FakeCheck
) -> None:
    """O18, M5-R2: a slot that comes due while a manual run is active is recorded `skipped_overlap` (§3.4),
    so that run pings with its own status. A slot skipped outside a manual run's window does not."""

    async def skip_slot(at: datetime) -> None:
        # the tick finds the slot `at` due while the manual run is active: `skipped_overlap`
        async with engine.begin() as conn:
            await conn.execute(_SCHEDULE, {"anchor": at, "cursor": at - timedelta(hours=1)})
            report = await run_tick(conn, now=at + timedelta(minutes=1), defer=defer_in_tx)
        assert report.skipped_overlap and report.queued_run_id is None

    covering_id, covering_queued = await manual_queued_ago(engine, "10 minutes")
    covering = await admitted(engine, covering_id)
    await skip_slot(covering_queued + timedelta(minutes=5))  # within its window
    assert await finalized(engine, covering, pinger) == "succeeded"
    assert hc.pings == ["success"] and await watermark(engine) == covering_id

    later_id, later_queued = await manual_queued_ago(engine, "0 seconds")  # queued after the first ended
    later = await admitted(engine, later_id)
    await skip_slot(later_queued - timedelta(hours=1))  # a late tick: the slot is before this run's window
    assert await finalized(engine, later, pinger) == "succeeded"
    assert hc.pings == ["success"] and await watermark(engine) == covering_id
    async with engine.connect() as conn:  # the later run's window holds no skipped slot
        assert await runs_to_ping(conn, after_run_id=0) == [RunToPing(covering_id, "success")]


async def test_resume_exhausted_and_orphaned_queue_ping_fail_through_the_tick(
    engine: AsyncEngine, queue_app: procrastinate.App, pinger: DeadmanPinger, hc: FakeCheck
) -> None:
    """O10: neither end goes through a finalize, so no fast path pings it; the next tick does."""
    exhausted = await scheduled_run(engine, slot=SLOT)
    await admitted(engine, exhausted)
    for _ in range(runs.MAX_RESUMES + 1):  # the stale-run take-backs, until the last one ends the run
        async with engine.begin() as conn:
            await runs.begin_new_attempt(
                conn, exhausted, epoch=1, end_reason="heartbeat_stale", defer=defer_in_tx
            )
    assert await run_end(engine, exhausted) == ("failed", "resume_exhausted")
    assert hc.pings == []
    await tick(engine, queue_app, pinger, now=utcnow())
    assert hc.pings == ["fail"] and await watermark(engine) == exhausted

    orphaned = await scheduled_run(engine, slot=SLOT + SIX_HOURS)  # queued; its job is lost twice
    for minutes in (2.1, 4.2):
        async with engine.begin() as conn:
            await conn.execute(sa.text("DELETE FROM procrastinate_jobs WHERE task_name = 'jh:run_collect'"))
        ctx = ReconcileContext(
            engine=engine,
            queue=queue_app,
            defer=defer_in_tx,
            now=utcnow() + timedelta(minutes=minutes),
            epoch=1,
        )
        await runs.reconcile_queued_runs(ctx)
    assert await run_end(engine, orphaned) == ("failed", "orphaned_queue")
    assert hc.pings == ["fail"]
    await tick(engine, queue_app, pinger, now=utcnow())
    assert hc.pings == ["fail", "fail"] and await watermark(engine) == orphaned


async def test_a_crash_between_commit_and_ping_pings_once_from_the_tick(
    engine: AsyncEngine, queue_app: procrastinate.App, pinger: DeadmanPinger, hc: FakeCheck
) -> None:
    owner = await admitted(engine, await scheduled_run(engine, slot=SLOT))
    # the run's end committed, and the worker died before its fast path: no pinger ran
    assert await finalized(engine, owner, None) == "succeeded"
    assert hc.pings == [] and await watermark(engine) is None
    assert await tick(engine, queue_app, pinger, now=utcnow()) >= 1
    assert hc.pings == ["success"] and await watermark(engine) == owner.run_id
    await tick(engine, queue_app, pinger, now=utcnow())  # a second tick sends nothing
    await pinger.after_run(owner.run_id)  # nor does a late fast path
    assert hc.pings == ["success"] and await watermark(engine) == owner.run_id


async def test_the_watermark_never_moves_back(
    engine: AsyncEngine, queue_app: procrastinate.App, pinger: DeadmanPinger, hc: FakeCheck
) -> None:
    failed = await scheduled_run(engine, slot=SLOT)
    assert await runs.execute_run(engine, raising_graph(), failed, 1, epoch=1) == "started"
    succeeded = await admitted(engine, await scheduled_run(engine, slot=SLOT + SIX_HOURS))
    assert await finalized(engine, succeeded, None) == "succeeded"
    # the tick pings every run above the watermark, in id order
    await tick(engine, queue_app, pinger, now=utcnow())
    assert hc.pings == ["fail", "success"] and await watermark(engine) == succeeded.run_id

    await pinger.after_run(failed)  # an older run's late fast path: below the watermark, nothing
    assert hc.pings == ["fail", "success"] and await watermark(engine) == succeeded.run_id
    manual = await queued_run(engine)  # a newer run that pings nothing leaves the watermark where it is
    assert await finalized(engine, manual, pinger) == "succeeded"
    assert hc.pings == ["fail", "success"] and await watermark(engine) == succeeded.run_id
    async with engine.connect() as conn:
        assert await runs_to_ping(conn, after_run_id=0) == [
            RunToPing(failed, "fail"),
            RunToPing(succeeded.run_id, "success"),
        ]
        assert await runs_to_ping(conn, after_run_id=succeeded.run_id) == []


# --- Pre-review fix (Ruling T5-2): the watermark armed at the worker's start --------------------------------


async def ended_history(engine: AsyncEngine) -> list[int]:
    """Three pingable ends from before the dead-man was armed, oldest first: a failed one, two successes."""
    failed = await scheduled_run(engine, slot=SLOT)
    assert await runs.execute_run(engine, raising_graph(), failed, 1, epoch=1) == "started"
    ended = [failed]
    for hours in (6, 12):
        owner = await admitted(engine, await scheduled_run(engine, slot=SLOT + timedelta(hours=hours)))
        assert await finalized(engine, owner, None) == "succeeded"
        ended.append(owner.run_id)
    return ended


def armed_events(log_events: Callable[[], list[dict[str, Any]]]) -> list[int]:
    return [event["run_id"] for event in log_events() if event["event"] == "deadman_armed"]


async def test_arming_skips_history_before_the_first_start(
    engine: AsyncEngine,
    queue_app: procrastinate.App,
    pinger: DeadmanPinger,
    hc: FakeCheck,
    log_events: Callable[[], list[dict[str, Any]]],
) -> None:
    history = await ended_history(engine)
    manual = await queued_run(engine)  # a manual end too: the newest run, whatever it pings
    assert await finalized(engine, manual, None) == "succeeded"
    assert await watermark(engine) is None
    async with engine.connect() as conn:
        assert [run.run_id for run in await runs_to_ping(conn, after_run_id=0)] == history

    assert await pinger.arm() == manual.run_id  # the newest collection run
    assert await watermark(engine) == manual.run_id and armed_events(log_events) == [manual.run_id]
    assert await pinger.ping_pending() == 0
    await tick(engine, queue_app, pinger, now=utcnow())
    assert hc.pings == []  # the history replays no ping
    assert await pinger.arm() is None  # once only: the watermark is set
    assert armed_events(log_events) == [manual.run_id]

    after = await admitted(engine, await scheduled_run(engine, slot=SLOT + timedelta(hours=18)))
    assert await finalized(engine, after, pinger) == "succeeded"  # a run that ends after the arming pings
    assert hc.pings == ["success"] and await watermark(engine) == after.run_id


async def test_arming_keeps_a_set_watermark(
    engine: AsyncEngine,
    queue_app: procrastinate.App,
    pinger: DeadmanPinger,
    hc: FakeCheck,
    log_events: Callable[[], list[dict[str, Any]]],
) -> None:
    first = await admitted(engine, await scheduled_run(engine, slot=SLOT))
    assert await finalized(engine, first, pinger) == "succeeded"  # pinged: the watermark is set
    assert hc.pings == ["success"] and await watermark(engine) == first.run_id
    missed = await admitted(engine, await scheduled_run(engine, slot=SLOT + SIX_HOURS))
    assert await finalized(engine, missed, None) == "succeeded"  # its ping lost to a crash

    assert await pinger.arm() is None  # a restart arms nothing
    assert await watermark(engine) == first.run_id and armed_events(log_events) == []
    await tick(engine, queue_app, pinger, now=utcnow())  # the lost ping is still sent
    assert hc.pings == ["success", "success"] and await watermark(engine) == missed.run_id


async def test_arming_with_no_runs_leaves_null(
    engine: AsyncEngine, pinger: DeadmanPinger, hc: FakeCheck, log_events: Callable[[], list[dict[str, Any]]]
) -> None:
    assert await pinger.arm() is None
    assert await watermark(engine) is None and armed_events(log_events) == [] and hc.pings == []
    # the first run of a fresh database pings: the NULL reads as 0
    first = await admitted(engine, await scheduled_run(engine, slot=SLOT))
    assert await finalized(engine, first, pinger) == "succeeded"
    assert hc.pings == ["success"] and await watermark(engine) == first.run_id


async def test_arming_leaves_a_run_in_flight_to_ping(
    engine: AsyncEngine, pinger: DeadmanPinger, hc: FakeCheck
) -> None:
    """A run queued or running at the start (taken over by this worker) ends after it: the watermark stops
    just below it, though a slot it skipped meanwhile has a newer id."""
    history = await ended_history(engine)
    in_flight = await admitted(engine, await scheduled_run(engine, slot=SLOT + timedelta(hours=18)))
    slot = SLOT + timedelta(hours=24)
    async with engine.begin() as conn:  # the next slot comes due while it runs: `skipped_overlap`, a newer id
        await conn.execute(_SCHEDULE, {"anchor": slot, "cursor": slot - timedelta(hours=1)})
        assert (await run_tick(conn, now=slot + timedelta(minutes=1), defer=defer_in_tx)).skipped_overlap

    assert await pinger.arm() == in_flight.run_id - 1
    assert await pinger.ping_pending() == 0 and hc.pings == []  # the history replays nothing
    assert await finalized(engine, in_flight, pinger) == "succeeded"
    assert hc.pings == ["success"] and await watermark(engine) == in_flight.run_id
    assert history[-1] < in_flight.run_id


async def test_a_superseded_worker_arms_nothing(
    engine: AsyncEngine, current_epoch: int, hc: FakeCheck
) -> None:
    await ended_history(engine)
    async with deadman.make_http_client(user_agent="JobHunter/test") as http:
        client = HealthchecksClient(http, ping_url=PING_URL, api_url=API_URL, api_key=API_KEY, sleep=no_wait)
        stale = DeadmanPinger(engine, client, epoch=current_epoch + 1)  # not the current epoch
        with pytest.raises(FencedOut) as fenced:
            await stale.arm()
    assert fenced.value.worker_superseded
    assert await watermark(engine) is None and hc.pings == []


# --- Fix round 1 (review M1, M7): the fast path's lower runs, the paths together, a cancelled lock ----------

_LOCK_HELD = (  # 7261005, `DEADMAN_LOCK_KEY` (asserted in the test)
    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND objid = 7261005 AND granted)"
)


async def test_the_fast_path_pings_a_lower_pending_run_first(
    engine: AsyncEngine, pinger: DeadmanPinger, hc: FakeCheck
) -> None:
    """M1: R5 ended with no fast path (here: none ran; in the worker, an end no finalize writes), and R6's end
    comes before the next tick: R6's fast path pings R5, then R6, in that order."""
    r5 = await scheduled_run(engine, slot=SLOT)
    assert await runs.execute_run(engine, raising_graph(), r5, 1, epoch=1) == "started"  # failed: no pinger
    assert hc.pings == [] and await watermark(engine) is None
    r6 = await admitted(engine, await scheduled_run(engine, slot=SLOT + SIX_HOURS, trigger="catch_up"))
    assert await finalized(engine, r6, pinger) == "succeeded"
    assert hc.pings == ["fail", "success"] and hc.status == "up"
    assert await watermark(engine) == r6.run_id
    # a fast path pings nothing above its own run: R7, pending, waits for the tick
    r7 = await admitted(engine, await scheduled_run(engine, slot=SLOT + 2 * SIX_HOURS))
    assert await finalized(engine, r7, None) == "succeeded"
    await pinger.after_run(r6.run_id)
    assert hc.pings == ["fail", "success"] and await watermark(engine) == r6.run_id


async def test_the_fast_path_and_the_tick_together_ping_each_run_once(
    engine: AsyncEngine, pinger: DeadmanPinger, hc: FakeCheck
) -> None:
    """M7 (a): `after_run` and `ping_pending` at once take turns on the lock, run by run (final review M1:
    each send holds it alone, reading the watermark again): each run is pinged once, in order, by whichever
    pass reached it first, and the watermark ends at the newest."""
    failed = await scheduled_run(engine, slot=SLOT)
    assert await runs.execute_run(engine, raising_graph(), failed, 1, epoch=1) == "started"
    succeeded = await admitted(engine, await scheduled_run(engine, slot=SLOT + SIX_HOURS))
    assert await finalized(engine, succeeded, None) == "succeeded"

    _, by_the_tick = await asyncio.gather(pinger.after_run(succeeded.run_id), pinger.ping_pending())
    assert hc.pings == ["fail", "success"]
    assert by_the_tick in (0, 1, 2)  # the fast path's runs are the rest: none is pinged twice
    assert await watermark(engine) == succeeded.run_id


async def test_a_lock_held_by_a_cancelled_task_is_taken_again_at_once(
    engine: AsyncEngine, pinger: DeadmanPinger, migrated: PgServer
) -> None:
    """M7 (b): a task cancelled while it holds the dead-man lock gives it back: its dedicated connection
    unlocks (or closes), so a fresh `_locked()` takes it within a second."""
    assert DEADMAN_LOCK_KEY == 7261005
    lock_engine = create_async_engine(engine.url, poolclass=NullPool)
    holding = asyncio.Event()

    async def holder() -> None:
        async with deadman_lock(lock_engine):
            holding.set()
            await asyncio.Event().wait()  # until cancelled

    try:
        task = asyncio.create_task(holder())
        async with asyncio.timeout(10):
            await holding.wait()
        assert pg_one(migrated, "app", _LOCK_HELD) == (True,)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with asyncio.timeout(1):  # one poll of `LOCK_POLL_S` at most, had it been held
            async with pinger._locked():
                assert pg_one(migrated, "app", _LOCK_HELD) == (True,)  # this time, the pinger's
        assert pg_one(migrated, "app", _LOCK_HELD) == (False,)
    finally:
        await lock_engine.dispose()
