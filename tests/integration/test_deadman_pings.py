"""The dead-man pings (spec §17.7; plan M5 Task 5, O-ops Q6, O10, O18, M5-R2).

A run that ends `succeeded` or `partial` pings the check, one that ends `failed` or `failed_invariant` pings
its `/fail`; only a scheduled or catch-up run does, or a manual run whose window holds the slot it made
`skipped_overlap` (O18). The fast path pings right after the run-ending commit, from the graph's `finalize`
node (`finalize_from_graph`) and from `runs._finalize`; the `deadman_ping` reconciler (the tick) pings every
run of `runs_to_ping` above the watermark `deadman_state.last_pinged_run_id`, so the ends no finalize writes
(`resume_exhausted`, `orphaned_queue`) and a ping lost to a crash after the commit are sent, once. The
watermark only moves forward, and only past a run it pinged (or passed over).

respx serves healthchecks (`tests.support.healthchecks`; no network) and the pings' retry sleep waits for
nothing. Every run is invented.
"""

import dataclasses
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Final

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.graphs.collect.deps import CollectDeps
from jobhunter.graphs.collect.nodes.finalize import finalize_from_graph
from jobhunter.graphs.collect.state import CollectState, CollectUpdate, initial_state
from jobhunter.observability import deadman
from jobhunter.observability.deadman import DeadmanPinger, HealthchecksClient, RunToPing, runs_to_ping
from jobhunter.worker import runs
from jobhunter.worker.reconcilers import ReconcileContext
from jobhunter.worker.runs import RunOwner
from jobhunter.worker.tasks import defer_in_tx
from jobhunter.worker.tick import run_tick
from langgraph.graph import END, START, StateGraph
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import insert_revisions_awaiting, make_deps, planned_request, queued_run
from tests.support.deadman import admitted, deadman_state, manual_queued_ago, scheduled_run, tick
from tests.support.healthchecks import API_KEY, API_URL, PING_URL, FakeCheck, fake_healthchecks

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
