"""`batch_poll` on the fake Batches API (spec §10.10 lifecycle steps 5-7, L3958-3961; plan Task 17, Codex r6
B1, r7 B1, Carries T8-e, T16-g): a submitted batch is retrieved until it ends, its results are processed
item by item and the submission is reconciled in the same pass; the missing items are decided after the
completion, each in its own transaction, never holding `ranking_state` while waiting for a job row; the
poll's and the adoption's reconcile each insert their follow-on once, in the reconciling transaction;
follow-ups request a `batch_followup` cycle; a result stream that stays unreadable is given up after
`RESULTS_UNREADABLE_PASSES` passes.

The targets are 250 synthetic ads of the score tuple in one bulk operation, each answered by its own keyed
body (`tests.support.batches.submit_batch`); every datum is invented.
"""

import asyncio
from collections import Counter
from datetime import timedelta
from typing import Any

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db import ranking
from jobhunter.llm.fake import BatchScript
from jobhunter.scoring import batch_operations as batch_ops
from jobhunter.scoring import batches as batches_mod
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.batches import (
    PUBLISHED_BODY,
    BulkTargets,
    SubmittedBatch,
    batch_client,
    bulk_row,
    bulk_with_targets,
    decisions,
    items_of,
    open_alert_codes,
    poll_once,
    reconcile_ambiguous_once,
    submission_rows,
    submission_state,
    submit_batch,
    targets_by_custom_id,
)
from tests.support.faults import SimulatedCrash, raising_at
from tests.support.fixtures import count_rows
from tests.support.llm import FakeLLM
from tests.support.pg import PgServer, lock_wait_details, relation_locks
from tests.support.procs import await_until
from tests.support.profile import OpRunner
from tests.support.scoring import operations_of

pytestmark = pytest.mark.integration
N = 250
_SCORE_CYCLE_JOBS = sa.text(
    "SELECT args ->> 'trigger' FROM procrastinate_jobs WHERE task_name = 'jh:score_cycle' AND status = 'todo'"
)


@pytest.fixture
async def submitted(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, queue_app: procrastinate.App
) -> SubmittedBatch:
    return await submit_batch(engine, op_runner, fake_llm, N)


async def test_a_submitted_batch_is_polled_until_it_ends_and_reconciled_in_the_same_pass(
    engine: AsyncEngine, fake_llm: FakeLLM, submitted: SubmittedBatch
) -> None:
    """Steps 5-7: retrieved `in_progress` for `polls_until_ended` passes (the submission stays `submitted`),
    then `ended`; its results processed and the submission `reconciled` by that pass, with
    `results_complete_at` set and every target published."""
    fake_llm.batches(BatchScript(polls_until_ended=2))
    first = await poll_once(engine, fake_llm)
    assert (first.polled, first.ended, first.decided) == (1, 0, 0)
    assert await submission_state(engine, submitted.submission_id) == "submitted"
    await poll_once(engine, fake_llm)
    assert await submission_state(engine, submitted.submission_id) == "submitted"
    third = await poll_once(engine, fake_llm)
    assert (third.ended, third.decided, third.reconciled) == (1, N, 1)
    (row,) = await submission_rows(engine)
    assert row.state == "reconciled" and row.ended_at is not None
    assert row.results_complete_at is not None and row.reconciled_at is not None
    assert Counter(await decisions(engine)) == {"published": N}
    assert await count_rows(engine, "llm_results") == N
    assert fake_llm.state.batches[submitted.provider_id].results_read == 1  # streamed once, never buffered
    assert await poll_once(engine, fake_llm) == batch_ops.PollReport()  # nothing left to poll


async def test_missing_items_are_decided_after_completion_each_in_its_own_transaction(
    engine: AsyncEngine, fake_llm: FakeLLM, submitted: SubmittedBatch, migrated: PgServer
) -> None:
    """Codex r6 B1: an ended batch whose stream omits three of our items, with a concurrent merge of the
    first missing item's job holding that job row and then bumping the ranking. The completion commits
    `results_complete_at` first; each missing item is processed in a transaction of its own, so the one that
    waits for the merge's job row holds no `ranking_state` lock, and the merge's bump goes through (no
    deadlock); the three targets are `pending('batch_missing')`, `batch.items_missing` is open, and the same
    pass reconciles the submission."""
    missing = [submitted.custom_ids[i] for i in (10, 20, 30)]
    fake_llm.batches(BatchScript(items=dict.fromkeys(missing, "missing"), polls_until_ended=0))
    merged_job = submitted.setup.targets[10].job_id
    async with engine.connect() as merge:  # the merge: the job row, then (later) the ranking
        await merge.begin()
        await merge.execute(sa.text("SELECT id FROM jobs WHERE id = :j FOR UPDATE"), {"j": merged_job})
        polling = asyncio.create_task(poll_once(engine, fake_llm))

        async def waiting() -> bool:
            return bool(lock_wait_details(migrated)) or polling.done()

        await await_until(waiting, 60, 0.05)
        assert not polling.done()
        (wait,) = lock_wait_details(migrated)
        assert (wait.relation, wait.key) == ("jobs", str(merged_job)), wait
        assert "ranking_state" not in relation_locks(migrated, wait.pid)  # one item per transaction
        (row,) = await submission_rows(engine)
        assert row.state == "ended" and row.results_complete_at is not None  # the completion committed first
        assert await count_rows(engine, "batch_items", result_type="awaiting") == 3
        await asyncio.wait_for(ranking.bump(merge), 10)  # no transaction holds the ranking for a job row
        await merge.commit()
    report = await asyncio.wait_for(polling, 60)
    assert (report.decided, report.reconciled) == (N, 1)
    targets = await targets_by_custom_id(engine)
    assert {(targets[c].state, targets[c].state_reason) for c in missing} == {("pending", "batch_missing")}
    assert await open_alert_codes(engine) == ["batch.items_missing"]
    assert await submission_state(engine, submitted.submission_id) == "reconciled"


def _follow_ons(ops: list[Any], submission_id: int) -> list[Any]:
    return [op for op in ops if op.payload.get("after_submission_id") == submission_id]


@pytest.mark.parametrize("path", ["poll", "adoption"])
async def test_each_entry_path_enqueues_the_follow_on_once(
    engine: AsyncEngine,
    op_runner: OpRunner,
    fake_llm: FakeLLM,
    queue_app: procrastinate.App,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    """Codex r7 B1: the poll's and the adoption's reconcile each insert exactly one `batch_submit` follow-on
    in the reconciling transaction: a crash right after that commit finds the operation row and
    `follow_on_at` committed together, and a second run of either path inserts none."""
    if path == "poll":
        s = (await submit_batch(engine, op_runner, fake_llm, N)).submission_id
    else:  # a create whose answer was lost: the batch exists, the submission is ambiguous (C20)
        setup = await bulk_with_targets_answered(engine, fake_llm)
        fake_llm.batches(BatchScript(create="ambiguous_created", polls_until_ended=0))
        assert setup.operation_id is not None
        await op_runner.run_one(setup.operation_id)
        (row,) = await submission_rows(engine)
        assert row.state == "submit_ambiguous"
        s = int(row.id)
    expired = [
        f"t{t}_a{a}" for t, a in (await items_of(engine, s))[:5]
    ]  # retried online: the bulk stays active
    fake_llm.batches(BatchScript(items=dict.fromkeys(expired, "expired"), polls_until_ended=0))
    run = poll_once if path == "poll" else reconcile_ambiguous_once
    monkeypatch.setattr(batch_ops, "afault_point", raising_at(batch_ops.AFTER_RECONCILE_COMMIT))
    with pytest.raises(SimulatedCrash):
        await run(engine, fake_llm)
    (row,) = await submission_rows(engine)
    assert row.state == "reconciled" and row.follow_on_at is not None
    (follow_on,) = _follow_ons(await operations_of(engine, "batch_submit"), s)
    assert follow_on.status == "pending"
    monkeypatch.undo()
    await run(engine, fake_llm)  # the second run of the same path
    async with engine.begin() as conn:  # and the completion orchestration itself again
        assert not await batches_mod.follow_on(conn, submission_id=s, defer=None)
    finalized = await batch_ops.finalize_results(engine, s, now=row.reconciled_at, defer=None)
    assert finalized == batch_ops.Finalized(missing=0, reconciled=True)
    assert len(_follow_ons(await operations_of(engine, "batch_submit"), s)) == 1


async def bulk_with_targets_answered(engine: AsyncEngine, fake_llm: FakeLLM) -> BulkTargets:
    """`N` targets of a bulk operation, each answered by `PUBLISHED_BODY`."""
    setup = await bulk_with_targets(engine, N)
    for target in setup.targets:
        fake_llm.answer("generate", target.job_revision_id, PUBLISHED_BODY)
    return setup


async def test_followup_decisions_request_a_batch_followup_cycle(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, queue_app: procrastinate.App
) -> None:
    """A `followup` decision hands its target to the online lane: the poll requests a `batch_followup`
    cycle (`cycles.request_cycle`), once while one waits; the reconcile then requests the follow-on
    submission of the still-active bulk operation."""
    submitted = await submit_batch(engine, op_runner, fake_llm, N, followups=5)
    fake_llm.batches(BatchScript(polls_until_ended=0))
    report = await poll_once(engine, fake_llm)
    assert (report.followups, report.reconciled) == (5, 1)
    async with engine.connect() as conn:
        assert list((await conn.execute(_SCORE_CYCLE_JOBS)).scalars()) == ["batch_followup"]
    (follow_on,) = _follow_ons(await operations_of(engine, "batch_submit"), submitted.submission_id)
    assert follow_on.payload == {
        "bulk_operation_id": submitted.setup.bulk_operation_id,
        "llm_tuple_id": submitted.setup.llm_tuple_id,
        "after_submission_id": submitted.submission_id,
    }
    assert (await bulk_row(engine, submitted.setup.bulk_operation_id)).state == "active"  # follow-ups pending


async def test_an_unreadable_result_stream_is_given_up_after_its_bound(
    engine: AsyncEngine,
    fake_llm: FakeLLM,
    submitted: SubmittedBatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Carry T16-g: a result line the pinned SDK cannot validate (a provider schema change) ends every read
    of the stream. Each such pass is counted on the submission (a cut stream is not: C23) and leaves it
    `ended` with no item `missing`; at `RESULTS_UNREADABLE_PASSES` the completion runs with the critical
    `batch.results_unreadable` and every item still unread is decided `missing`."""
    monkeypatch.setattr(batch_ops, "RESULTS_UNREADABLE_PASSES", 3)
    fake_llm.batches(BatchScript(polls_until_ended=0))
    async with fake_llm.client() as http:  # the batch observed as ended: its lines are fixed
        await batch_client(fake_llm, http).retrieve(submitted.provider_id)
    stored = fake_llm.state.batches[submitted.provider_id]
    bad = submitted.custom_ids[100]
    stored.lines[bad] = {"custom_id": bad, "result": {"type": "succeeded"}}  # no message: no valid result
    for passes in (1, 2):
        await poll_once(engine, fake_llm)
        (row,) = await submission_rows(engine)
        assert (row.state, row.results_complete_at) == ("ended", None)
        assert row.reconcile_report["unreadable_passes"] == passes
        assert row.reconcile_report["error_class"] == "ValidationError"
        assert await count_rows(engine, "batch_items", result_type="missing") == 0
    await poll_once(engine, fake_llm)
    (row,) = await submission_rows(engine)
    assert row.state == "reconciled" and row.reconcile_report["results_unreadable"] == "ValidationError"
    assert await open_alert_codes(engine) == ["batch.items_missing", "batch.results_unreadable"]
    unread = await count_rows(engine, "batch_items", result_type="missing")
    assert 1 <= unread < N and await count_rows(engine, "batch_items", result_type="awaiting") == 0
    assert (await targets_by_custom_id(engine))[bad].state_reason == "batch_missing"


async def test_a_batch_past_its_expiry_is_retrieved_like_any_other(
    engine: AsyncEngine, fake_llm: FakeLLM, submitted: SubmittedBatch
) -> None:
    """L3958: a `submitted` batch past `expires_at + 1 h` that never ended is retrieved like any other; the
    provider ends it with every unprocessed item `expired`, which settles `not_billed(batch_item_expired)` and
    is retried online."""
    fake_llm.batches(BatchScript(polls_until_ended=1000))
    later = utcnow() + timedelta(hours=25, minutes=1)
    fake_llm.state.clock = lambda: later
    report = await poll_once(engine, fake_llm, now=later)
    assert (report.ended, report.decided, report.reconciled) == (1, N, 1)
    targets = await targets_by_custom_id(engine)
    assert {(t.state, t.state_reason, t.outcome) for t in targets.values()} == {
        ("pending", "batch_expired", "transient_error")
    }
    assert await count_rows(engine, "batch_items", result_type="expired") == N


async def test_the_batch_poll_task_body_polls_with_the_workers_client(
    engine: AsyncEngine, fake_llm: FakeLLM, submitted: SubmittedBatch, current_epoch: int
) -> None:
    """`batch_poll`'s body (`poll_task`): the worker's client from its config and gateway, `poll` at now."""
    fake_llm.batches(BatchScript(polls_until_ended=0))
    report = await batch_ops.poll_task(engine, fake_llm.cfg, fake_llm.gateway(engine), epoch=current_epoch)
    assert (report.polled, report.reconciled) == (1, 1)
    assert await submission_state(engine, submitted.submission_id) == "reconciled"


async def test_a_stream_never_read_whole_is_given_up_after_the_results_retention(
    engine: AsyncEngine, fake_llm: FakeLLM, submitted: SubmittedBatch
) -> None:
    """Carry T16-g: a stream cut on every read (no line unreadable, so no pass is counted) completes nothing
    while the results can still be downloaded; once their 29-day retention after `submit_started_at` has
    passed, the poll gives it up as it gives up an unreadable one: the critical `batch.results_unreadable`
    (reason `results_retention_passed`), every unread item `missing`, the submission `reconciled`."""
    fake_llm.batches(BatchScript(polls_until_ended=0, stream_cut_after=60))
    (row,) = await submission_rows(engine)
    first = await poll_once(engine, fake_llm, now=row.submit_started_at + timedelta(days=28, hours=23))
    assert first.reconciled == 0 and await submission_state(engine, submitted.submission_id) == "ended"
    assert await count_rows(engine, "batch_items", result_type="missing") == 0
    late = await poll_once(engine, fake_llm, now=row.submit_started_at + timedelta(days=29, minutes=1))
    assert late.reconciled == 1
    (row,) = await submission_rows(engine)
    assert row.reconcile_report == {"results_unreadable": batch_ops.RETENTION_PASSED}
    assert await open_alert_codes(engine) == ["batch.items_missing", "batch.results_unreadable"]
    unread = await count_rows(engine, "batch_items", result_type="missing")
    assert 0 < unread < N and await count_rows(engine, "batch_items", result_type="awaiting") == 0
