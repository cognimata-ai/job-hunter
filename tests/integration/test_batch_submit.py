"""The `batch_submit` operation end to end on the fake Batches API (spec §10.10 lifecycle steps 1-4d, §7.12,
§6.9.3; plan Task 16, Deviation 13, Rulings T7-c, T7-d, P16, Carries T8-a, T8-f, Codex r1 #23, #24, r2 B12,
B15): prepare, submit-start and the create, a definitive 4xx and its follow-on, the always-rejected batch that
exhausts its targets, an ambiguous create, a crash on either side of `submit_started` and its recovery at the
takeover, a cancel requested during the send, and a tuple superseded before the send.

The operations run in process (`op_runner`) with the fake LLM; the targets are 250 synthetic ads of the score
tuple in one bulk operation (`tests.support.batches.bulk_with_targets`). A test that crashes or takes over
registers no online scenario (Carry T8-f); none polls (Task 17's).
"""

import asyncio
import uuid
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.llm import budget, bulk, spend_ledger
from jobhunter.llm.fake import BatchScript
from jobhunter.scoring import batches as batches_mod
from jobhunter.settings.model import ModelPolicy
from jobhunter.worker.epoch import WorkerIdentity, claim_epoch_and_sweep
from jobhunter.worker.lock import LockSession
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.batches import (
    BulkTargets,
    batch_ledger,
    bulk_row,
    bulk_with_targets,
    charged_attempts,
    items_of,
    make_due,
    submission_rows,
)
from tests.support.faults import PausePoint, no_fault, raising_at
from tests.support.fixtures import count_rows
from tests.support.llm import FakeLLM
from tests.support.profile import OpRunner
from tests.support.scoring import attempt_outcomes, operations_of, targets_by_state

pytestmark = pytest.mark.integration
N = 250
# The test evaluation policy's projected critic rate (0.3, `activate_test_policy`) times ĉ_followup of
# policy B ($0.066939, §21.4): each item's follow-up hold.
HOLD = (Decimal("0.3") * budget.c_hat_followup(ModelPolicy())).quantize(Decimal("0.000001"), ROUND_HALF_UP)
_LEDGER_SUM = sa.text(
    "SELECT coalesce(sum(l.reserved_usd), 0) FROM llm_spend_ledger l "
    "JOIN llm_calls c ON c.spend_ledger_id = l.id "
    "WHERE c.transport = 'batch'"
)
_ATTEMPT_DETAILS = sa.text("SELECT DISTINCT outcome, outcome_detail FROM score_attempts WHERE mode = 'batch'")


@pytest.fixture
async def setup(engine: AsyncEngine, current_epoch: int, queue_app: procrastinate.App) -> BulkTargets:
    return await bulk_with_targets(engine, N)


def claimant(lock: LockSession, pid: int) -> WorkerIdentity:
    assert lock.backend_pid is not None
    return WorkerIdentity(uuid.uuid4(), "h", pid, lock.backend_pid, "v")


async def ledger_sum(engine: AsyncEngine) -> Decimal:
    async with engine.connect() as conn:
        return Decimal((await conn.execute(_LEDGER_SUM)).scalar_one())


async def batch_attempt_details(engine: AsyncEngine) -> set[tuple[str, str | None]]:
    async with engine.connect() as conn:
        return {(str(r.outcome), r.outcome_detail) for r in await conn.execute(_ATTEMPT_DETAILS)}


def states(ledger: list[tuple[str, str | None]]) -> list[str]:
    return [state for state, _ in ledger]


async def test_prepare_submit_and_create_on_the_fake(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, setup: BulkTargets
) -> None:
    fake_llm.batches(BatchScript(create="ok"))
    assert await op_runner.run_pending() == [setup.operation_id]
    (sub,) = await submission_rows(engine)
    stored = fake_llm.state.batches[sub.provider_batch_id]
    assert (sub.state, sub.request_count) == ("submitted", N)
    assert sub.expires_at == stored.created_at + timedelta(hours=24)  # L3954
    assert sub.submit_started_at is not None and sub.submitted_at is not None
    assert await count_rows(engine, "score_targets", state="in_batch") == N
    assert states(await batch_ledger(engine)) == ["dispatched"] * N
    assert await charged_attempts(engine, setup.llm_tuple_id) == N  # charged at submit_started (R6)
    assert sorted(stored.custom_ids) == sorted(f"t{t}_a{a}" for t, a in await items_of(engine, sub.id))
    assert Decimal(sub.reserved_usd) == await ledger_sum(engine) and sub.followup_held_usd == N * HOLD
    account = await bulk_row(engine, setup.bulk_operation_id)
    assert (account.spent_usd, account.followup_held_usd) == (sub.reserved_usd, N * HOLD)
    assert await attempt_outcomes(engine, setup.llm_tuple_id) == {"running": N}
    (op,) = await operations_of(engine, "batch_submit")
    assert op.status == "succeeded"
    assert op.progress == {"submission_id": sub.id, "state": "submitted", "request_count": N}


async def test_c19_crash_before_submit_start_abandons(
    engine: AsyncEngine,
    op_runner: OpRunner,
    fake_llm: FakeLLM,
    setup: BulkTargets,
    worker_lock: LockSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(batches_mod, "afault_point", raising_at(batches_mod.BEFORE_SUBMIT_START))
    await op_runner.run_pending()  # the execution dies with the submission preparing
    (sub,) = await submission_rows(engine)
    assert sub.state == "preparing" and states(await batch_ledger(engine)) == ["reserved"] * N
    epoch, counts = await claim_epoch_and_sweep(engine, claimant(worker_lock, 2))
    assert (epoch, counts["batch_submissions"]) == (2, 1)  # the sweep abandons the preparing row
    (sub,) = await submission_rows(engine)
    assert sub.state == "abandoned"
    assert await batch_ledger(engine) == [("not_billed", "never_dispatched")] * N
    assert await count_rows(engine, "score_targets", state="pending") == N
    account = await bulk_row(engine, setup.bulk_operation_id)
    assert (account.spent_usd, account.followup_held_usd) == (Decimal(0), Decimal(0))
    assert await batch_attempt_details(engine) == {("cancelled", "batch_abandoned")}
    assert await charged_attempts(engine, setup.llm_tuple_id) == 0  # never dispatched, never charged
    assert fake_llm.state.batches == {}
    follow_on = [op for op in await operations_of(engine, "batch_submit") if op.id != setup.operation_id]
    assert [op.payload["after_submission_id"] for op in follow_on] == [sub.id]  # the follow-on of 4d


@pytest.mark.parametrize("point", [batches_mod.AFTER_SUBMIT_START, batches_mod.AFTER_CREATE])
async def test_a_lost_execution_after_submit_started_becomes_ambiguous_and_is_never_resent(
    engine: AsyncEngine,
    op_runner: OpRunner,
    fake_llm: FakeLLM,
    setup: BulkTargets,
    worker_lock: LockSession,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
) -> None:
    """Codex r1 #23 (both sides of the send), Ruling P16: the adoption half is Task 17's."""
    fake_llm.batches(BatchScript(create="ok", polls_until_ended=0))  # ended, a line per custom_id, if created
    monkeypatch.setattr(batches_mod, "afault_point", raising_at(point))
    await op_runner.run_pending()  # the execution dies with the row submit_started
    (sub,) = await submission_rows(engine)
    assert sub.state == "submit_started"
    await claim_epoch_and_sweep(engine, claimant(worker_lock, 2))  # or the reconciler after 3 min
    (sub,) = await submission_rows(engine)
    assert (sub.state, sub.reconcile_report) == ("submit_ambiguous", {"error_class": "execution_lost"})
    assert states(await batch_ledger(engine)) == ["dispatched"] * N
    monkeypatch.setattr(batches_mod, "afault_point", no_fault)
    op_runner.epoch = 2
    await make_due(engine)
    await op_runner.run_pending()  # the operation recovers, never resends
    (sub,) = await submission_rows(engine)
    assert sub.state == "submit_ambiguous" and states(await batch_ledger(engine)) == ["dispatched"] * N
    assert len(fake_llm.state.batches) == (1 if point == batches_mod.AFTER_CREATE else 0)
    assert await count_rows(engine, "score_targets", state="in_batch") == N  # quarantined, not released
    (op,) = await operations_of(engine, "batch_submit")
    assert (op.status, op.progress) == ("succeeded", {"skipped": "ambiguous"})


async def test_definitive_4xx_rejects_and_releases(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, setup: BulkTargets
) -> None:
    fake_llm.batches(BatchScript(create="reject_400"))
    assert setup.operation_id is not None
    await op_runner.run_one(setup.operation_id)
    (sub,) = await submission_rows(engine)
    assert sub.state == "submit_rejected"
    ledger = await batch_ledger(engine)
    assert set(ledger) == {("not_billed", "batch_submit_rejected")} and len(ledger) == N
    assert await attempt_outcomes(engine, setup.llm_tuple_id) == {"submit_rejected": N}
    # Ruling T7-d (Deviation 13): the rejected attempt stays charged; its entries settle at $0
    assert await charged_attempts(engine, setup.llm_tuple_id) == N
    assert await targets_by_state(engine, setup.llm_tuple_id) == {"pending": N}
    assert await count_rows(engine, "score_targets", state="in_batch") == 0
    account = await bulk_row(engine, setup.bulk_operation_id)
    assert (account.spent_usd, account.followup_held_usd) == (Decimal(0), Decimal(0))
    assert await count_rows(engine, "batch_items", hold_released_at=None) == 0  # every hold released once
    assert await count_rows(engine, "operations", kind="batch_submit") == 2  # the follow-on
    follow_on = (await operations_of(engine, "batch_submit"))[-1]
    assert (follow_on.status, follow_on.payload) == (
        "pending",
        {
            "bulk_operation_id": setup.bulk_operation_id,
            "llm_tuple_id": setup.llm_tuple_id,
            "after_submission_id": sub.id,
        },
    )
    assert sub.follow_on_at is not None
    async with engine.begin() as conn:  # once only: a second request finds the marker set
        assert not await batches_mod.follow_on(conn, submission_id=sub.id, defer=None)
    assert await count_rows(engine, "operations", kind="batch_submit") == 2


async def test_an_always_rejected_batch_exhausts_its_targets_after_four_attempts(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, setup: BulkTargets
) -> None:
    """Ruling T7-d: every rejection charges its attempt, so the follow-ons stop at `max_score_attempts`."""
    fake_llm.batches(BatchScript(create="reject_400"))
    executed = await op_runner.run_pending()
    assert len(executed) == 5  # four rejected submissions, then the exhaustion sweep leaves none
    assert [row.state for row in await submission_rows(engine)] == ["submit_rejected"] * 4
    assert await targets_by_state(engine, setup.llm_tuple_id) == {"failed_attempts_exhausted": N}
    assert await attempt_outcomes(engine, setup.llm_tuple_id) == {"submit_rejected": 4 * N}
    last = (await operations_of(engine, "batch_submit"))[-1]
    assert (last.status, last.progress) == ("succeeded", {"skipped": "below_minimum"})


async def test_an_ambiguous_create_quarantines_the_submission(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, setup: BulkTargets
) -> None:
    """§10.10 step 4c: a 5xx records `submit_ambiguous` and moves nothing else; no follow-on."""
    fake_llm.batches(BatchScript(create="error_500"))
    assert await op_runner.run_pending() == [setup.operation_id]
    (sub,) = await submission_rows(engine)
    assert (sub.state, sub.reconcile_report) == ("submit_ambiguous", {"error_class": "InternalServerError"})
    assert states(await batch_ledger(engine)) == ["dispatched"] * N
    assert await count_rows(engine, "score_targets", state="in_batch") == N
    assert await attempt_outcomes(engine, setup.llm_tuple_id) == {"running": N}
    assert (await bulk_row(engine, setup.bulk_operation_id)).followup_held_usd == N * HOLD
    assert await count_rows(engine, "operations", kind="batch_submit") == 1 and sub.follow_on_at is None


async def test_a_cancel_requested_during_the_send_reaches_the_provider(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, setup: BulkTargets
) -> None:
    """Codex r1 #24: a confirm during the send records the request; `record_created` carries it and the
    worker calls `messages.batches.cancel` right after the commit."""
    fake_llm.batches(BatchScript(create="ok", polls_until_ended=5))
    pause = PausePoint(batches_mod.AFTER_SUBMIT_START)
    assert setup.operation_id is not None
    running = asyncio.create_task(op_runner.run_one(setup.operation_id, pause=pause))
    await asyncio.wait_for(pause.reached.wait(), 60)
    async with engine.begin() as conn:
        requested = await batches_mod.request_cancel(conn, llm_tuple_id=setup.llm_tuple_id, now=utcnow())
    assert (requested.submissions, requested.targets) == (1, N)
    pause.resume()
    await asyncio.wait_for(running, 120)
    (sub,) = await submission_rows(engine)
    assert sub.state == "submitted" and sub.cancel_requested_at is not None
    assert fake_llm.state.batches[sub.provider_batch_id].cancel_requested


async def test_a_tuple_superseded_before_submit_start_abandons_and_sends_nothing(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, setup: BulkTargets
) -> None:
    """Codex r1 #24: `submit_start`'s re-check abandons a submission whose tuple was superseded or whose
    cancel was requested since the prepare; nothing obsolete is dispatched, and its bulk operation, cancelled,
    requests no follow-on."""
    fake_llm.batches(BatchScript(create="ok"))
    pause = PausePoint(batches_mod.AFTER_PREPARE_COMMIT)
    assert setup.operation_id is not None
    running = asyncio.create_task(op_runner.run_one(setup.operation_id, pause=pause))
    await asyncio.wait_for(pause.reached.wait(), 60)
    async with engine.begin() as conn:  # the activation's (f) and (e), as `targets.activate_tuple` runs them
        assert await bulk.cancel(conn, setup.bulk_operation_id, now=utcnow())
        await batches_mod.request_cancel(conn, llm_tuple_id=setup.llm_tuple_id, now=utcnow())
    pause.resume()
    await asyncio.wait_for(running, 120)
    (sub,) = await submission_rows(engine)
    assert sub.state == "abandoned" and fake_llm.state.batches == {}
    assert await batch_ledger(engine) == [("not_billed", "never_dispatched")] * N
    assert await targets_by_state(engine, setup.llm_tuple_id) == {"cancelled": N}
    assert await count_rows(engine, "operations", kind="batch_submit") == 1  # no follow-on
    (op,) = await operations_of(engine, "batch_submit")
    assert (op.status, op.progress) == ("succeeded", {"submission_id": sub.id, "state": "abandoned"})


async def test_a_global_purge_fence_before_submit_start_abandons_and_sends_nothing(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, setup: BulkTargets
) -> None:
    """L6365: the global purge fence refuses the submit before `submit_start`; the prepared submission is
    abandoned (its entries released, its targets pending) and its follow-on waits for the fence to go."""
    fake_llm.batches(BatchScript(create="ok"))
    pause = PausePoint(batches_mod.AFTER_PREPARE_COMMIT)
    assert setup.operation_id is not None
    running = asyncio.create_task(op_runner.run_one(setup.operation_id, pause=pause))
    await asyncio.wait_for(pause.reached.wait(), 60)
    async with engine.begin() as conn:
        await conn.execute(sa.text("INSERT INTO purge_fences (kind, manifest_id) VALUES ('global', 1)"))
    pause.resume()
    await asyncio.wait_for(running, 120)
    (sub,) = await submission_rows(engine)
    assert sub.state == "abandoned" and fake_llm.state.batches == {}
    assert await batch_ledger(engine) == [("not_billed", "never_dispatched")] * N
    assert await targets_by_state(engine, setup.llm_tuple_id) == {"pending": N}
    assert await charged_attempts(engine, setup.llm_tuple_id) == 0
    follow_on = (await operations_of(engine, "batch_submit"))[-1]
    assert follow_on.payload["after_submission_id"] == sub.id and follow_on.status == "pending"
    await op_runner.run_one(follow_on.id)  # paused by the fence (`pause_reason`): nothing prepared
    assert (await operations_of(engine, "batch_submit"))[-1].progress == {"skipped": "paused"}
    assert await count_rows(engine, "batch_submissions") == 1


async def test_a_hard_cap_below_the_batch_aborts_the_whole_preparation(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, setup: BulkTargets
) -> None:
    """L1159 ("including batches"): every item is admitted against the hard cap; a refusal of any item rolls
    the whole preparation back, and the operation ends `skipped: budget_exhausted` with nothing written. The
    headroom still admits a first generator call (`r_first`), so the lane is not paused."""
    month = budget.month_of(utcnow())
    async with engine.begin() as conn:
        p = await spend_ledger.current_params(conn)
        await spend_ledger.ensure_month(conn, month, p, utcnow())
        await spend_ledger.change_cap(
            conn, month=month, cap_usd=Decimal("2.21"), overrun_buffer_usd=Decimal("2.00"), now=utcnow()
        )  # A = $0.21: a scoring headroom of about $0.20
    fake_llm.batches(BatchScript(create="ok"))
    assert await op_runner.run_pending() == [setup.operation_id]
    (op,) = await operations_of(engine, "batch_submit")
    assert (op.status, op.progress) == ("succeeded", {"skipped": "budget_exhausted"})
    assert (
        await count_rows(engine, "batch_submissions") == 0
        and await count_rows(engine, "llm_spend_ledger") == 0
    )
    assert await targets_by_state(engine, setup.llm_tuple_id) == {"pending": N}
    assert (await bulk_row(engine, setup.bulk_operation_id)).spent_usd == 0
    assert fake_llm.state.batches == {}
