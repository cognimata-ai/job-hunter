"""Crash cases of the batch result processing (spec §18.3 C23, §10.10 step 7, L3961; plan Task 17, Codex r6
B1): every step is idempotent and the rows are the recovery. A cut stream keeps the submission `ended` and
decides nothing `missing`; a crash between two items, after the completion's commit or after a missing item
leaves a state the next poll finishes from, without streaming again once the stream was read whole and
without deciding an item twice; a crash before the completion's commit leaves its marker unset.

The targets are 250 synthetic ads of the score tuple in one bulk operation; every datum is invented.
"""

from collections import Counter
from collections.abc import Awaitable, Callable

import procrastinate
import pytest
from jobhunter.llm.fake import BatchScript
from jobhunter.scoring import batch_operations as batch_ops
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.batches import (
    SubmittedBatch,
    decisions,
    poll_once,
    submission_rows,
    submit_batch,
    targets_by_custom_id,
)
from tests.support.faults import SimulatedCrash
from tests.support.fixtures import count_rows
from tests.support.llm import FakeLLM
from tests.support.profile import OpRunner

pytestmark = pytest.mark.integration
N = 250


def raising_at_arrival(point: str, arrival: int) -> Callable[[str], Awaitable[None]]:
    """An `afault_point` replacement that raises `SimulatedCrash` at the `arrival`-th arrival at `point`."""
    arrived = 0

    async def fault(name: str) -> None:
        nonlocal arrived
        if name == point:
            arrived += 1
            if arrived == arrival:
                raise SimulatedCrash(point)

    return fault


@pytest.fixture
async def submitted(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, queue_app: procrastinate.App
) -> SubmittedBatch:
    return await submit_batch(engine, op_runner, fake_llm, N)


async def assert_finished_once(engine: AsyncEngine, missing: int = 0) -> None:
    """Every item decided once: the submission `reconciled`, `N - missing` results published (no double
    publish), each target in one end state."""
    (row,) = await submission_rows(engine)
    assert row.state == "reconciled"
    assert Counter(await decisions(engine)) == +Counter({"published": N - missing, "retry_online": missing})
    assert await count_rows(engine, "llm_results") == N - missing
    assert await count_rows(engine, "score_targets", state="published_accepted") == N - missing


async def test_c23_a_cut_stream_keeps_ended_and_the_next_poll_finishes_once(
    engine: AsyncEngine, fake_llm: FakeLLM, submitted: SubmittedBatch
) -> None:
    """C23: the download drops after 100 lines; the items read are decided, the submission stays `ended`
    with its marker unset and no item `missing` (L3961), and the next poll's whole read finishes it, each item
    decided once."""
    fake_llm.batches(BatchScript(polls_until_ended=0, stream_cut_after=100))
    report = await poll_once(engine, fake_llm)
    assert report.reconciled == 0 and 0 < report.decided <= 100
    (row,) = await submission_rows(engine)
    assert (row.state, row.results_complete_at, row.reconcile_report) == ("ended", None, None)
    assert await count_rows(engine, "batch_items", result_type="missing") == 0
    assert await count_rows(engine, "batch_items", result_type="awaiting") == N - report.decided
    fake_llm.batches(BatchScript(polls_until_ended=0))
    again = await poll_once(engine, fake_llm)
    assert (again.decided, again.reconciled) == (N - report.decided, 1)
    await assert_finished_once(engine)


async def test_c23_a_crash_between_two_items_is_finished_by_the_next_poll(
    engine: AsyncEngine, fake_llm: FakeLLM, submitted: SubmittedBatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C23: the worker dies after the 120th item's commit; the next poll reads the stream again, replays the
    120 stored decisions and decides the rest, no item twice."""
    fake_llm.batches(BatchScript(polls_until_ended=0))
    monkeypatch.setattr(batch_ops, "afault_point", raising_at_arrival(batch_ops.AFTER_ITEM, 120))
    with pytest.raises(SimulatedCrash):
        await poll_once(engine, fake_llm)
    assert await count_rows(engine, "batch_items", result_type="succeeded") == 120
    monkeypatch.undo()
    again = await poll_once(engine, fake_llm)
    assert (again.decided, again.reconciled) == (N - 120, 1)
    await assert_finished_once(engine)
    assert fake_llm.state.batches[submitted.provider_id].results_read == 2


@pytest.mark.parametrize(
    ("point", "arrival", "decided_missing"),
    [
        (batch_ops.AFTER_COMPLETION_COMMIT, 1, 0),
        (batch_ops.AFTER_MISSING_ITEM, 2, 2),
    ],
)
async def test_finalize_results_crash_points(
    engine: AsyncEngine,
    fake_llm: FakeLLM,
    submitted: SubmittedBatch,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
    arrival: int,
    decided_missing: int,
) -> None:
    """Codex r6 B1: a crash after the completion's commit, or after the second missing item's, leaves the
    submission `ended` with `results_complete_at` set; the next poll skips the stream (read whole already)
    and finishes the missing items and the reconcile, deciding none twice."""
    missing = [submitted.custom_ids[i] for i in (3, 4, 5)]
    fake_llm.batches(BatchScript(items=dict.fromkeys(missing, "missing"), polls_until_ended=0))
    monkeypatch.setattr(batch_ops, "afault_point", raising_at_arrival(point, arrival))
    with pytest.raises(SimulatedCrash):
        await poll_once(engine, fake_llm)
    (row,) = await submission_rows(engine)
    assert row.state == "ended" and row.results_complete_at is not None
    assert await count_rows(engine, "batch_items", result_type="missing") == decided_missing
    assert await count_rows(engine, "batch_items", result_type="awaiting") == 3 - decided_missing
    monkeypatch.undo()
    read = fake_llm.state.batches[submitted.provider_id].results_read
    again = await poll_once(engine, fake_llm)
    assert fake_llm.state.batches[submitted.provider_id].results_read == read  # not streamed again
    assert (again.decided, again.reconciled) == (3 - decided_missing, 1)
    await assert_finished_once(engine, missing=3)
    targets = await targets_by_custom_id(engine)
    assert {(targets[c].state, targets[c].state_reason) for c in missing} == {("pending", "batch_missing")}


async def test_a_crash_before_the_completion_commit_leaves_its_marker_unset(
    engine: AsyncEngine, fake_llm: FakeLLM, submitted: SubmittedBatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex r6 B1: the completion rolls back whole: no marker, no item `missing`, no alert; the next poll
    reads the stream again and completes."""
    missing = [submitted.custom_ids[i] for i in (3, 4, 5)]
    fake_llm.batches(BatchScript(items=dict.fromkeys(missing, "missing"), polls_until_ended=0))
    monkeypatch.setattr(batch_ops, "afault_point", raising_at_arrival(batch_ops.BEFORE_COMPLETION_COMMIT, 1))
    with pytest.raises(SimulatedCrash):
        await poll_once(engine, fake_llm)
    (row,) = await submission_rows(engine)
    assert (row.state, row.results_complete_at) == ("ended", None)
    assert await count_rows(engine, "batch_items", result_type="missing") == 0
    assert await count_rows(engine, "alerts", code="batch.items_missing") == 0
    monkeypatch.undo()
    await poll_once(engine, fake_llm)
    await assert_finished_once(engine, missing=3)
