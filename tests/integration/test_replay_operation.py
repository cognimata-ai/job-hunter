"""The `replay_raw_items` operation (spec §7.16.1, §14.2; plan M4 Task 12, M1 Carry-forward 10).

Step 1 replays the payload's `rejected` items in one transaction under the operation's claim, after the
global purge fence (an active one requeues the operation `purge_in_progress`, writing nothing); step 2
resolves every payload item that is `pending` then, one transaction each, under the same claim
(`identity.resolve_item(owner=claim)`), so the items of a run that is over become jobs. A crashed execution
resumes on the items still `pending`. The progress is `{"asked", "moved", "resolved"}`.

"Rejected by a parser bug fixed since" is played by items of the recorded Arbeitnow page that today's parser
reads, moved `pending -> rejected('item_contract')` as an older parser would have left them; "still
unparsable" by the crafted JSearch page's fifth job, which has no `job_uid`. Every datum is a scrubbed fixture
or invented.
"""

import hashlib
from collections.abc import Sequence
from datetime import timedelta
from typing import Any, Final

import httpx
import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.snapshot import read_snapshot
from jobhunter.db.transitions import transition
from jobhunter.dedup import identity
from jobhunter.errors import FencedOut, InvariantViolation
from jobhunter.graphs.collect.nodes.finalize import _i1
from jobhunter.observability.attention import needs_attention
from jobhunter.sources import replay
from jobhunter.sources.arbeitnow import ArbeitnowAdapter
from jobhunter.sources.items import parse_response
from jobhunter.sources.jsearch import JSearchAdapter
from jobhunter.sources.receipts import RAW_BODY_TTL
from jobhunter.worker import operations
from jobhunter.worker.executions import ExecutionRegistry
from jobhunter.worker.operations import execute_operation
from jobhunter.worker.runs import finalize_run
from jobhunter.worker.tasks import defer_in_tx, job_task_name
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.batches import make_due
from tests.support.faults import SimulatedCrash, no_fault, raising_at
from tests.support.settings import generation
from tests.support.sources import StoredPage

pytestmark = pytest.mark.integration

OPERATION: Final = sa.text(
    "SELECT kind, status, attempts, last_error_code, progress, payload, dedup_key FROM operations "
    "WHERE id = :o"
)
ITEMS: Final = sa.text(
    "SELECT id, disposition, reason_code, observation_id FROM raw_items "
    "WHERE id = ANY(CAST(:ids AS bigint[])) ORDER BY id"
)
JOBS_OF: Final = sa.text(
    "SELECT count(DISTINCT p.job_id) FROM raw_items i JOIN posting_observations o ON o.id = i.observation_id "
    "JOIN postings p ON p.id = o.posting_id WHERE i.id = ANY(CAST(:ids AS bigint[]))"
)
PENDING_OF: Final = sa.text(
    "SELECT id FROM raw_items WHERE raw_response_id = :r AND disposition = 'pending' ORDER BY id"
)
GLOBAL_FENCE: Final = sa.text(
    "INSERT INTO purge_fences (kind, manifest_id) VALUES ('global', 1) RETURNING id"
)
RELEASE_FENCE: Final = sa.text("UPDATE purge_fences SET released_at = now() WHERE id = :f")


async def operation(engine: AsyncEngine, operation_id: int) -> Any:
    async with engine.connect() as c:
        return (await c.execute(OPERATION, {"o": operation_id})).one()


async def item_states(engine: AsyncEngine, ids: Sequence[int]) -> list[tuple[str, str | None]]:
    async with engine.connect() as c:
        return [(r.disposition, r.reason_code) for r in await c.execute(ITEMS, {"ids": list(ids)})]


async def linked_jobs(engine: AsyncEngine, ids: Sequence[int]) -> int:
    async with engine.connect() as c:
        return int((await c.execute(JOBS_OF, {"ids": list(ids)})).scalar_one())


async def parsed_page(engine: AsyncEngine, adapter: ArbeitnowAdapter, page: StoredPage) -> list[int]:
    """The stored Arbeitnow page parsed for its run, the run then ended: its `pending` items."""
    owner, rr = page
    async with engine.begin() as c:
        await parse_response(c, rr, adapter=adapter, owner=owner)
        await finalize_run(c, owner, status="succeeded", reason=None, report={})
    async with engine.connect() as c:
        return [int(i) for i in (await c.execute(PENDING_OF, {"r": rr})).scalars()]


async def rejected_by_an_old_parser(engine: AsyncEngine, ids: Sequence[int]) -> None:
    """`pending -> rejected('item_contract')`, as a parser with a bug fixed since would have left them."""
    async with engine.begin() as c:
        for raw_item_id in ids:
            await transition(
                c,
                "raw_items",
                raw_item_id,
                ["pending"],
                "rejected",
                reason="item_contract",
                set_={"normalized": None},
            )


async def requested(engine: AsyncEngine, ids: Sequence[int]) -> int:
    """The replay of `ids` requested, its `run_operation` job deferred (a test executes it itself)."""
    async with engine.begin() as c:
        return await replay.request_replay(c, raw_item_ids=ids, defer=defer_in_tx)


async def execute(engine: AsyncEngine, operation_id: int, epoch: int) -> None:
    await execute_operation(engine, operation_id, epoch=epoch, registry=ExecutionRegistry())


async def test_replay_moves_rejected_items_and_resolves_them_under_the_operation(
    engine: AsyncEngine,
    current_epoch: int,
    queue_app: procrastinate.App,
    arbeitnow_adapter: ArbeitnowAdapter,
    stored_arbeitnow_page: StoredPage,
) -> None:
    owner, _ = stored_arbeitnow_page
    pending = await parsed_page(engine, arbeitnow_adapter, stored_arbeitnow_page)
    ids = pending[:3]
    await rejected_by_an_old_parser(engine, ids)
    # the run is over: its owner resolves nothing any more (one of its items still pending, for the proof)
    with pytest.raises(FencedOut):
        async with engine.begin() as c:
            await identity.resolve_item(c, owner=owner, raw_item_id=pending[3])
    before = await generation(engine)

    operation_id = await requested(engine, list(reversed(ids)))
    op = await operation(engine, operation_id)
    assert (op.kind, op.status, op.payload) == ("replay_raw_items", "pending", {"raw_item_ids": ids})
    assert op.dedup_key == replay.dedup_key(ids) and op.dedup_key.startswith("replay_raw_items:")
    assert await requested(engine, [*ids, ids[0]]) == operation_id  # the same set: the live operation
    async with engine.connect() as c:  # its job, deferred once, under the replays' execution lock
        jobs = (
            await c.execute(
                sa.text(
                    "SELECT lock, queueing_lock FROM procrastinate_jobs WHERE task_name = :t "
                    "AND args @> jsonb_build_object('operation_id', CAST(:o AS bigint))"
                ),
                {"t": job_task_name("run_operation"), "o": operation_id},
            )
        ).all()
    assert [tuple(job) for job in jobs] == [("replay", f"op:{operation_id}")]
    assert operations.OPERATION_LOCKS["replay_raw_items"] == "replay"

    await execute(engine, operation_id, current_epoch)
    op = await operation(engine, operation_id)
    assert (op.status, op.attempts, op.progress) == ("succeeded", 0, {"asked": 3, "moved": 3, "resolved": 3})
    assert await item_states(engine, ids) == [("linked", None)] * 3
    assert await linked_jobs(engine, ids) == 3  # three new jobs, each with its posting and observation
    # one bump per resolution that inserted a job (`identity._link`, its last statement); step 1 bumps nothing
    assert await generation(engine) == before + 3


async def test_an_active_global_fence_requeues_with_purge_in_progress(
    engine: AsyncEngine,
    current_epoch: int,
    queue_app: procrastinate.App,
    arbeitnow_adapter: ArbeitnowAdapter,
    stored_arbeitnow_page: StoredPage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = (await parsed_page(engine, arbeitnow_adapter, stored_arbeitnow_page))[:2]
    await rejected_by_an_old_parser(engine, ids)
    async with engine.begin() as c:
        fence = int((await c.execute(GLOBAL_FENCE)).scalar_one())
    operation_id = await requested(engine, ids)
    started = utcnow()

    # step 1 under the fence: requeued, nothing written (no item moved, no progress)
    await execute(engine, operation_id, current_epoch)
    op = await operation(engine, operation_id)
    assert (op.status, op.attempts, op.last_error_code, op.progress) == (
        "pending",
        1,
        "purge_in_progress",
        {},
    )
    async with engine.connect() as c:
        not_before = (
            await c.execute(sa.text("SELECT not_before FROM operations WHERE id = :o"), {"o": operation_id})
        ).scalar_one()
    assert not_before >= started + replay.PURGE_RETRY_AFTER
    assert await item_states(engine, ids) == [("rejected", "item_contract")] * 2

    # the fence released; a new one raised between step 1 and step 2: the resolution is fenced (the owner
    # lock of an operation claim checks the global fence) and requeues, keeping what step 1 did
    async with engine.begin() as c:
        await c.execute(RELEASE_FENCE, {"f": fence})
    fences: list[int] = []

    async def fence_after_the_replay(point: str) -> None:
        if point == replay.AFTER_REPLAY_COMMIT:
            async with engine.begin() as c:
                fences.append(int((await c.execute(GLOBAL_FENCE)).scalar_one()))

    monkeypatch.setattr(replay, "afault_point", fence_after_the_replay)
    await make_due(engine)
    await execute(engine, operation_id, current_epoch)
    op = await operation(engine, operation_id)
    assert (op.status, op.attempts, op.last_error_code) == ("pending", 2, "purge_in_progress")
    assert op.progress == {"asked": 2, "moved": 2, "resolved": 0}
    assert await item_states(engine, ids) == [("pending", None)] * 2

    # released again: the next execution moves nothing more and resolves the pending items
    monkeypatch.setattr(replay, "afault_point", no_fault)
    async with engine.begin() as c:
        await c.execute(RELEASE_FENCE, {"f": fences[0]})
    await make_due(engine)
    await execute(engine, operation_id, current_epoch)
    op = await operation(engine, operation_id)
    assert (op.status, op.progress) == ("succeeded", {"asked": 2, "moved": 2, "resolved": 2})
    assert await item_states(engine, ids) == [("linked", None)] * 2


async def test_a_crash_after_the_replay_transaction_resumes_on_the_pending_ids(
    engine: AsyncEngine,
    current_epoch: int,
    queue_app: procrastinate.App,
    arbeitnow_adapter: ArbeitnowAdapter,
    stored_arbeitnow_page: StoredPage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = (await parsed_page(engine, arbeitnow_adapter, stored_arbeitnow_page))[:3]
    await rejected_by_an_old_parser(engine, ids)
    operation_id = await requested(engine, ids)

    # the process dies between step 1's commit and step 2: the items moved, none resolved
    monkeypatch.setattr(replay, "afault_point", raising_at(replay.AFTER_REPLAY_COMMIT))
    await execute(engine, operation_id, current_epoch)
    op = await operation(engine, operation_id)
    assert (op.status, op.attempts, op.last_error_code) == ("pending", 1, "bug")
    assert op.progress == {"asked": 3, "moved": 3, "resolved": 0}
    assert await item_states(engine, ids) == [("pending", None)] * 3

    # the next execution: step 1 finds nothing left to move, step 2 resumes on the pending ids
    monkeypatch.setattr(replay, "afault_point", no_fault)
    await make_due(engine)
    await execute(engine, operation_id, current_epoch)
    op = await operation(engine, operation_id)
    assert (op.status, op.progress) == ("succeeded", {"asked": 3, "moved": 3, "resolved": 3})
    assert await item_states(engine, ids) == [("linked", None)] * 3
    assert await linked_jobs(engine, ids) == 3


async def test_a_crash_between_an_item_and_its_progress_loses_no_count(
    engine: AsyncEngine,
    current_epoch: int,
    queue_app: procrastinate.App,
    arbeitnow_adapter: ArbeitnowAdapter,
    stored_arbeitnow_page: StoredPage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = (await parsed_page(engine, arbeitnow_adapter, stored_arbeitnow_page))[:3]
    await rejected_by_an_old_parser(engine, ids)
    operation_id = await requested(engine, ids)
    written = operations.set_progress
    calls: list[int] = []

    async def dies_after_the_first_item(conn: AsyncConnection, claim: Any, progress: Any) -> None:
        calls.append(1)
        if len(calls) == 2:  # step 1's progress is the first; the first item's the second
            raise SimulatedCrash("after the first item")
        await written(conn, claim, progress)

    monkeypatch.setattr(replay, "set_progress", dies_after_the_first_item)
    await execute(engine, operation_id, current_epoch)
    op = await operation(engine, operation_id)
    assert (op.status, op.progress) == ("pending", {"asked": 3, "moved": 3, "resolved": 0})
    assert [state for state, _ in await item_states(engine, ids)] == ["linked", "pending", "pending"]

    monkeypatch.setattr(replay, "set_progress", written)
    await make_due(engine)
    await execute(engine, operation_id, current_epoch)
    op = await operation(engine, operation_id)
    assert (op.status, op.progress) == ("succeeded", {"asked": 3, "moved": 3, "resolved": 3})


async def test_an_item_of_a_running_run_stays_rejected_and_is_not_moved(
    engine: AsyncEngine,
    current_epoch: int,
    queue_app: procrastinate.App,
    arbeitnow_adapter: ArbeitnowAdapter,
    stored_arbeitnow_page: StoredPage,
) -> None:
    owner, rr = stored_arbeitnow_page
    async with engine.begin() as c:  # parsed by its run, which is still running
        await parse_response(c, rr, adapter=arbeitnow_adapter, owner=owner)
    async with engine.connect() as c:
        pending = [int(i) for i in (await c.execute(PENDING_OF, {"r": rr})).scalars()]
    ids = pending[:2]
    await rejected_by_an_old_parser(engine, ids)
    async with engine.begin() as c:  # the run's resolve node settled the rest: I1 holds
        for raw_item_id in pending[2:]:
            await transition(c, "raw_items", raw_item_id, ["pending"], "linked", set_={"normalized": None})
        assert (await _i1(c, owner.run_id)).ok

    # an operation recorded for them (the route refuses them; another path, or a run that resumed): step 1
    # moves nothing while the run runs, so its I1 still holds
    operation_id = await requested(engine, ids)
    await execute(engine, operation_id, current_epoch)
    op = await operation(engine, operation_id)
    assert (op.status, op.progress) == ("succeeded", {"asked": 2, "moved": 0, "resolved": 0})
    assert await item_states(engine, ids) == [("rejected", "item_contract")] * 2
    async with engine.connect() as c:
        assert (await _i1(c, owner.run_id)).ok

    # the run over, a new replay of them moves and resolves them
    async with engine.begin() as c:
        await finalize_run(c, owner, status="succeeded", reason=None, report={})
    operation_id = await requested(engine, ids)
    await execute(engine, operation_id, current_epoch)
    op = await operation(engine, operation_id)
    assert (op.status, op.progress) == ("succeeded", {"asked": 2, "moved": 2, "resolved": 2})
    assert await item_states(engine, ids) == [("linked", None)] * 2


async def test_a_re_execution_moves_nothing_again(
    engine: AsyncEngine,
    current_epoch: int,
    queue_app: procrastinate.App,
    arbeitnow_adapter: ArbeitnowAdapter,
    stored_arbeitnow_page: StoredPage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = (await parsed_page(engine, arbeitnow_adapter, stored_arbeitnow_page))[:3]
    await rejected_by_an_old_parser(engine, ids)
    operation_id = await requested(engine, ids)
    first = ids[0]
    link = identity._link
    written = operations.set_progress
    calls: list[int] = []

    async def identity_error_on_the_first(conn: AsyncConnection, owner: Any, item: Any, active: Any) -> Any:
        if item.raw_item_id == first:
            raise InvariantViolation(f"raw item {first}: a test's identity error")
        return await link(conn, owner, item, active)

    async def dies_after_the_first_item(conn: AsyncConnection, claim: Any, progress: Any) -> None:
        calls.append(1)
        if len(calls) == 2:
            raise SimulatedCrash("after the first item")
        await written(conn, claim, progress)

    # execution 1: step 1 moves the three, step 2 rejects the first (`identity_error`), then dies
    monkeypatch.setattr(identity, "_link", identity_error_on_the_first)
    monkeypatch.setattr(replay, "set_progress", dies_after_the_first_item)
    await execute(engine, operation_id, current_epoch)
    op = await operation(engine, operation_id)
    assert (op.status, op.progress) == ("pending", {"asked": 3, "moved": 3, "resolved": 0})
    assert await item_states(engine, ids) == [
        ("rejected", "identity_error"),
        ("pending", None),
        ("pending", None),
    ]

    # execution 2: step 1 committed before (its progress has `moved`), so nothing is replayed again; the
    # first item stays rejected, and `moved` stays within `asked`
    monkeypatch.setattr(identity, "_link", link)
    monkeypatch.setattr(replay, "set_progress", written)
    await make_due(engine)
    await execute(engine, operation_id, current_epoch)
    op = await operation(engine, operation_id)
    assert (op.status, op.progress) == ("succeeded", {"asked": 3, "moved": 3, "resolved": 3})
    assert await item_states(engine, ids) == [
        ("rejected", "identity_error"),
        ("linked", None),
        ("linked", None),
    ]


async def test_an_expired_body_is_not_replayable(
    engine: AsyncEngine,
    current_epoch: int,
    queue_app: procrastinate.App,
    arbeitnow_adapter: ArbeitnowAdapter,
    stored_arbeitnow_page: StoredPage,
    logged_in_client: httpx.AsyncClient,
) -> None:
    _, rr = stored_arbeitnow_page
    ids = (await parsed_page(engine, arbeitnow_adapter, stored_arbeitnow_page))[:1]
    await rejected_by_an_old_parser(engine, ids)
    async with engine.begin() as c:  # fetched 30 days and an hour ago: its body is past `RAW_BODY_TTL`
        await c.execute(
            sa.text("UPDATE raw_responses SET fetched_at = :at WHERE id = :r"),
            {"at": utcnow() - RAW_BODY_TTL - timedelta(hours=1), "r": rr},
        )
    async with engine.connect() as c:
        assert await replay.replayable_items(c, ids, now=utcnow()) == []
    # the route queues nothing: 409, and no operation
    r = await logged_in_client.post("/api/system/raw-items/replay", json={"raw_item_ids": ids})
    assert (r.status_code, r.json()["code"]) == (409, "conflict_state")
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM operations"))).scalar_one() == 0
    async with read_snapshot(engine) as c:
        section = (await needs_attention(c, now=utcnow())).raw_items
    assert [(i.raw_response_id, i.replayable) for i in section.items] == [(rr, False)]

    # an operation recorded before it expired moves nothing: the item stays rejected
    operation_id = await requested(engine, ids)
    await execute(engine, operation_id, current_epoch)
    op = await operation(engine, operation_id)
    assert (op.status, op.progress) == ("succeeded", {"asked": 1, "moved": 0, "resolved": 0})
    assert await item_states(engine, ids) == [("rejected", "item_contract")]


async def test_a_still_unparsable_item_stays_rejected_and_reports_none_moved(
    engine: AsyncEngine, current_epoch: int, queue_app: procrastinate.App, crafted_page: StoredPage
) -> None:
    owner, rr = crafted_page  # its fifth job has no `job_uid`: today's JSearch parser rejects it
    async with httpx.AsyncClient() as client, engine.begin() as c:
        instance = (
            await c.execute(sa.text("SELECT source_instance_id FROM raw_responses WHERE id = :r"), {"r": rr})
        ).scalar_one()
        adapter = JSearchAdapter(client, api_key=b"fake", instance_id=instance, clock=utcnow)
        await parse_response(c, rr, adapter=adapter, owner=owner)
    async with engine.connect() as c:
        (rejected,) = (
            await c.execute(
                sa.text("SELECT id FROM raw_items WHERE raw_response_id = :r AND disposition = 'rejected'"),
                {"r": rr},
            )
        ).scalars()

    async def raw_items_count() -> int:
        async with read_snapshot(engine) as c:
            return (await needs_attention(c, now=utcnow())).raw_items.count

    assert await raw_items_count() == 1
    operation_id = await requested(engine, [rejected])
    await execute(engine, operation_id, current_epoch)
    op = await operation(engine, operation_id)
    assert (op.status, op.progress) == ("succeeded", {"asked": 1, "moved": 0, "resolved": 0})
    assert await item_states(engine, [rejected]) == [("rejected", "item_contract")]  # never "recovered"
    assert await raw_items_count() == 1


def test_a_replay_names_one_to_500_items_and_its_key_is_the_sorted_set() -> None:
    assert replay.dedup_key([3, 1, 2, 1]) == "replay_raw_items:" + hashlib.sha256(b"1,2,3").hexdigest()
    assert replay.dedup_key([1, 2, 3]) != replay.dedup_key([1, 2])
    for wrong in ([], list(range(1, 502))):
        with pytest.raises(ValueError, match="1 to 500"):
            replay.dedup_key(wrong)
