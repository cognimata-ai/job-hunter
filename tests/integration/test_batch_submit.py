"""The `batch_submit` operation end to end on the fake Batches API (spec §10.10 lifecycle steps 1-4d, §7.12,
§6.9.3; plan Task 16, Deviation 13, Rulings T7-c, T7-d, T16-a, T16-c, T16-d, P16, Carries T8-a, T8-f, Codex
r1 #23, #24, r2 B12, B15): prepare, submit-start and the create, a definitive 4xx and its follow-on, the
always-rejected batch that exhausts its targets, an ambiguous create, a crash on either side of
`submit_started` and its recovery at the takeover, a cancel requested during the send, a tuple superseded or a
global fence before the send, the hard cap's cut, a refused key and its alert, a create never sent, a
deadlocked recording, and the `bulk_operations` reconciler's request of a batch a paused prepare skipped.

The operations run in process (`op_runner`) with the fake LLM; the targets are 250 synthetic ads of the score
tuple in one bulk operation (`tests.support.batches.bulk_with_targets`). A test that crashes or takes over
registers no online scenario (Carry T8-f); none polls (Task 17's).
"""

import asyncio
import uuid
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.sql import DEADLOCK_DETECTED
from jobhunter.graphs.sanitize import DB_RETRY
from jobhunter.llm import budget, bulk, spend_ledger
from jobhunter.llm.batches import BatchesClient, Rejected
from jobhunter.llm.fake import BatchScript
from jobhunter.observability.alerts import resolve_alert
from jobhunter.scoring import batch_operations as batch_ops
from jobhunter.scoring import batches as batches_mod
from jobhunter.settings.model import ModelPolicy
from jobhunter.worker.epoch import WorkerIdentity, claim_epoch_and_sweep
from jobhunter.worker.lock import LockSession
from jobhunter.worker.operations import cancel_operation, create_operation
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

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
from tests.support.fences import insert_test_fence
from tests.support.fixtures import count_rows
from tests.support.llm import FakeLLM, open_alerts
from tests.support.profile import OpRunner
from tests.support.reconcile import run_reconciler
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
_CALL_ERRORS = sa.text("SELECT DISTINCT http_status, error_code FROM llm_calls WHERE transport = 'batch'")
_RAISE = "DO $$ BEGIN RAISE EXCEPTION 'injected' USING ERRCODE = '{}'; END $$"
_CHARGE_THREE = sa.text("UPDATE score_targets SET charged_attempts = 3 WHERE bulk_operation_id = :b")


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


async def call_errors(engine: AsyncEngine) -> set[tuple[int | None, str | None]]:
    async with engine.connect() as conn:
        return {(r.http_status, r.error_code) for r in await conn.execute(_CALL_ERRORS)}


async def reconcile(engine: AsyncEngine, epoch: int) -> int:
    """One pass of the `bulk_operations` reconciler (its completion and its batch request)."""
    return await run_reconciler("bulk_operations", engine, now=utcnow(), epoch=epoch)


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


@pytest.mark.parametrize("point", [batch_ops.AFTER_SUBMIT_START, batch_ops.AFTER_CREATE])
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
    monkeypatch.setattr(batch_ops, "afault_point", raising_at(point))
    await op_runner.run_pending()  # the execution dies with the row submit_started
    (sub,) = await submission_rows(engine)
    assert sub.state == "submit_started"
    await claim_epoch_and_sweep(engine, claimant(worker_lock, 2))  # or the reconciler after 3 min
    (sub,) = await submission_rows(engine)
    assert (sub.state, sub.reconcile_report) == ("submit_ambiguous", {"error_class": "execution_lost"})
    assert states(await batch_ledger(engine)) == ["dispatched"] * N
    monkeypatch.setattr(batch_ops, "afault_point", no_fault)
    op_runner.epoch = 2
    await make_due(engine)
    await op_runner.run_pending()  # the operation recovers, never resends
    (sub,) = await submission_rows(engine)
    assert sub.state == "submit_ambiguous" and states(await batch_ledger(engine)) == ["dispatched"] * N
    assert len(fake_llm.state.batches) == (1 if point == batch_ops.AFTER_CREATE else 0)
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
    pause = PausePoint(batch_ops.AFTER_SUBMIT_START)
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
    pause = PausePoint(batch_ops.AFTER_PREPARE_COMMIT)
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
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, setup: BulkTargets, current_epoch: int
) -> None:
    """L6365: the global purge fence refuses the submit before `submit_start`; the prepared submission is
    abandoned (its entries released, its targets pending). Its follow-on is kept back at its claim while the
    fence stands (M5 Task 8: `pending`, no attempt spent, nothing prepared), and the `bulk_operations`
    reconciler requests nothing while it is pending (Ruling T16-d); once the fence is released, the follow-on
    runs and its batch is submitted."""
    fake_llm.batches(BatchScript(create="ok"))
    pause = PausePoint(batch_ops.AFTER_PREPARE_COMMIT)
    assert setup.operation_id is not None
    running = asyncio.create_task(op_runner.run_one(setup.operation_id, pause=pause))
    await asyncio.wait_for(pause.reached.wait(), 60)
    async with engine.begin() as conn:
        await insert_test_fence(conn, kind="global")
    pause.resume()
    await asyncio.wait_for(running, 120)
    (sub,) = await submission_rows(engine)
    assert sub.state == "abandoned" and fake_llm.state.batches == {}
    assert await batch_ledger(engine) == [("not_billed", "never_dispatched")] * N
    assert await targets_by_state(engine, setup.llm_tuple_id) == {"pending": N}
    assert await charged_attempts(engine, setup.llm_tuple_id) == 0
    follow_on = (await operations_of(engine, "batch_submit"))[-1]
    assert follow_on.payload["after_submission_id"] == sub.id and follow_on.status == "pending"
    await op_runner.run_one(follow_on.id)  # not claimed under the fence (M5 Task 8): nothing prepared
    kept = (await operations_of(engine, "batch_submit"))[-1]
    assert (kept.id, kept.status, kept.progress, kept.last_error_code) == (follow_on.id, "pending", {}, None)
    assert await count_rows(engine, "operations", id=follow_on.id, attempts=0) == 1
    assert await count_rows(engine, "batch_submissions") == 1
    assert await reconcile(engine, current_epoch) == 0  # the fence stands: no request
    async with engine.begin() as conn:
        await conn.execute(sa.text("UPDATE purge_fences SET released_at = now() WHERE kind = 'global'"))
    assert await reconcile(engine, current_epoch) == 0  # the follow-on is pending: no request
    await make_due(engine)
    assert await op_runner.run_pending() == [follow_on.id]
    assert [row.state for row in await submission_rows(engine)] == ["abandoned", "submitted"]
    assert await reconcile(engine, current_epoch) == 0  # a submission in flight: no request


async def test_a_hard_cap_below_the_minimum_batch_prepares_nothing(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, setup: BulkTargets
) -> None:
    """L1159 ("including batches"), Ruling T16-d: every item is admitted against the hard cap, so the cut
    stops at the month's scoring headroom; two items fit it here, below `batch_min_targets`, so the operation
    ends `skipped: below_minimum` with nothing written. The headroom still admits a first generator call
    (`r_first`), so the lane is not paused."""
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
    assert (op.status, op.progress) == ("succeeded", {"skipped": "below_minimum"})
    assert (
        await count_rows(engine, "batch_submissions") == 0
        and await count_rows(engine, "llm_spend_ledger") == 0
    )
    assert await targets_by_state(engine, setup.llm_tuple_id) == {"pending": N}
    assert (await bulk_row(engine, setup.bulk_operation_id)).spent_usd == 0
    assert fake_llm.state.batches == {}


async def refused(status: int | None, error_type: str, **evidence: str) -> Rejected:
    return Rejected(status, error_type, "test: refused", **evidence)


@pytest.mark.parametrize("status", [401, 403])
async def test_a_refused_key_rejects_alerts_and_pauses_the_follow_on_until_the_alert_clears(
    engine: AsyncEngine,
    op_runner: OpRunner,
    fake_llm: FakeLLM,
    setup: BulkTargets,
    current_epoch: int,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    """Ruling T16-a: a 401 or 403 is a definitive 4xx (4b) that also raises the critical `llm.auth_failed`,
    as the gateway raises it, in the recording's transaction; the follow-on prepare is paused by it
    (`pause_reason`), and once it is resolved the `bulk_operations` reconciler requests the batch again
    (Ruling T16-d)."""
    fake_llm.batches(BatchScript(create="ok"))
    rejected = await refused(status, "authentication_error")
    sends = BatchesClient.create

    async def create(self: BatchesClient, requests: object) -> Rejected:
        return rejected

    monkeypatch.setattr(BatchesClient, "create", create)
    assert setup.operation_id is not None
    await op_runner.run_one(setup.operation_id)
    (sub,) = await submission_rows(engine)
    assert sub.state == "submit_rejected"
    assert set(await batch_ledger(engine)) == {("not_billed", "batch_submit_rejected")}
    assert await open_alerts(engine, "llm.auth_failed") == [("llm.auth_failed:anthropic", "critical", 1)]
    follow_on = (await operations_of(engine, "batch_submit"))[-1]
    await op_runner.run_one(follow_on.id)
    assert (await operations_of(engine, "batch_submit"))[-1].progress == {"skipped": "paused"}
    assert await reconcile(engine, current_epoch) == 0  # paused still: no request
    async with engine.begin() as conn:
        assert await resolve_alert(conn, code="llm.auth_failed", subject="anthropic")
    assert await reconcile(engine, current_epoch) == 1
    monkeypatch.setattr(BatchesClient, "create", sends)  # the key works again
    executed = await op_runner.run_pending()
    assert len(executed) == 1
    assert [row.state for row in await submission_rows(engine)] == ["submit_rejected", "submitted"]


async def test_a_create_never_sent_refunds_with_the_connect_evidence(
    engine: AsyncEngine,
    op_runner: OpRunner,
    fake_llm: FakeLLM,
    setup: BulkTargets,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruling T16-a: a create whose connection failed before any byte left, on every try, made no batch; it is
    rejected (4b) with the items refunded on `connect_failed_before_send` (their calls `connect_failed`, no
    status), and the rejected attempts stay charged (T7-d)."""
    fake_llm.batches(BatchScript(create="ok"))
    rejected = await refused(None, "connect_failed", evidence="connect_failed_before_send")

    async def create(self: BatchesClient, requests: object) -> Rejected:
        return rejected

    monkeypatch.setattr(BatchesClient, "create", create)
    assert setup.operation_id is not None
    await op_runner.run_one(setup.operation_id)
    (sub,) = await submission_rows(engine)
    assert sub.state == "submit_rejected" and fake_llm.state.batches == {}
    assert set(await batch_ledger(engine)) == {("not_billed", "connect_failed_before_send")}
    assert await call_errors(engine) == {(None, "connect_failed")}
    assert await attempt_outcomes(engine, setup.llm_tuple_id) == {"submit_rejected": N}
    assert await charged_attempts(engine, setup.llm_tuple_id) == N
    assert await targets_by_state(engine, setup.llm_tuple_id) == {"pending": N}
    assert await open_alerts(engine, "llm.auth_failed") == []


@pytest.mark.parametrize(
    "rejected",
    [
        Rejected(429, "rate_limit_error", "test: refused"),
        Rejected(409, "conflict_error", "test: refused"),
        Rejected(None, "connect_failed", "", evidence="connect_failed_before_send"),
    ],
    ids=["429", "409", "connect"],
)
async def test_a_not_sent_rejection_leaves_the_follow_on_to_the_reconciler(
    engine: AsyncEngine,
    op_runner: OpRunner,
    fake_llm: FakeLLM,
    setup: BulkTargets,
    current_epoch: int,
    monkeypatch: pytest.MonkeyPatch,
    rejected: Rejected,
) -> None:
    """Ruling T16-f: a create that ran out of tries without being sent (a 409 or a 429 on its last try, or a
    connect failure before any byte left) is rejected (4b, T7-d unchanged), and its follow-on is marked
    handled but requests no operation: the provider asked for time, so the next `bulk_operations` heavy pass
    requests the batch (`rerequest`, T16-d). A definitive 4xx keeps its immediate follow-on
    (`test_definitive_4xx_rejects_and_releases`)."""
    fake_llm.batches(BatchScript(create="ok"))

    async def create(self: BatchesClient, requests: object) -> Rejected:
        return rejected

    monkeypatch.setattr(BatchesClient, "create", create)
    assert setup.operation_id is not None
    await op_runner.run_one(setup.operation_id)
    (sub,) = await submission_rows(engine)
    assert sub.state == "submit_rejected" and sub.follow_on_at is not None
    assert await charged_attempts(engine, setup.llm_tuple_id) == N
    (op,) = await operations_of(engine, "batch_submit")  # no follow-on operation
    assert op.status == "succeeded"
    async with engine.begin() as conn:  # handled once: a second request finds the marker set
        assert not await batches_mod.follow_on(conn, submission_id=sub.id, defer=None)
    assert await count_rows(engine, "operations", kind="batch_submit") == 1
    assert await reconcile(engine, current_epoch) == 1
    resumed = (await operations_of(engine, "batch_submit"))[-1]
    assert (resumed.status, resumed.payload) == (
        "pending",
        {"bulk_operation_id": setup.bulk_operation_id, "llm_tuple_id": setup.llm_tuple_id},
    )


async def test_no_batch_is_requested_while_the_target_creation_is_live(
    engine: AsyncEngine, current_epoch: int, queue_app: procrastinate.App
) -> None:
    """Fix round 2 (item 2): the first batch of a bulk operation is Task 14's, requested once every chunk of
    its `create_targets_for_tuple` is done; while that creation is `pending` or `running`, the reconciler's
    heavy pass requests nothing. Once it has ended, the pass requests the batch."""
    setup = await bulk_with_targets(engine, N, operation=False)
    async with engine.begin() as conn:
        creation, _ = await create_operation(
            conn,
            kind="create_targets_for_tuple",
            payload={"llm_tuple_id": setup.llm_tuple_id, "bulk_operation_id": setup.bulk_operation_id},
            dedup_key=f"create_targets_for_tuple:{setup.llm_tuple_id}",
            defer=None,
        )
    assert await reconcile(engine, current_epoch) == 0
    assert await count_rows(engine, "operations", kind="batch_submit") == 0
    async with engine.begin() as conn:
        assert await cancel_operation(conn, creation, reason="test")
    assert await reconcile(engine, current_epoch) == 1
    assert await count_rows(engine, "operations", kind="batch_submit") == 1


@pytest.mark.parametrize("deadlocks", [1, DB_RETRY.max_attempts])
async def test_a_deadlocked_recording_is_retried_within_its_bound(
    engine: AsyncEngine,
    op_runner: OpRunner,
    fake_llm: FakeLLM,
    setup: BulkTargets,
    monkeypatch: pytest.MonkeyPatch,
    deadlocks: int,
) -> None:
    """Ruling T16-c: the recording transaction is retried on a retryable database error (PostgreSQL aborts
    one side of a lock cycle with 40P01), up to `DB_RETRY.max_attempts` tries; the owner predicate makes each
    try the same recording. A deadlock on every try leaves the row `submit_started`, the recovery's."""
    fake_llm.batches(BatchScript(create="ok"))
    real = batches_mod.record_created
    tries: list[int] = []

    async def deadlocking(conn: AsyncConnection, *args: Any, **kwargs: Any) -> bool:
        tries.append(1)
        cancel_due = await real(conn, *args, **kwargs)
        if len(tries) <= deadlocks:  # its writes, then the abort: the whole transaction rolls back
            await conn.execute(sa.text(_RAISE.format(DEADLOCK_DETECTED)))
        return cancel_due

    monkeypatch.setattr(batches_mod, "record_created", deadlocking)
    assert setup.operation_id is not None
    await op_runner.run_one(setup.operation_id)
    (sub,) = await submission_rows(engine)
    (op,) = await operations_of(engine, "batch_submit")
    if deadlocks < DB_RETRY.max_attempts:
        assert len(tries) == deadlocks + 1
        assert sub.state == "submitted" and op.status == "succeeded"
    else:
        assert len(tries) == DB_RETRY.max_attempts
        assert sub.state == "submit_started" and op.status != "succeeded"
    assert len(fake_llm.state.batches) == 1  # sent once, whatever the recording's tries
    # DB_RETRY's interval between the tries (0.5 s, then 1 s, each plus up to 1 s of jitter), waited with the
    # gateway's injected sleep (here recorded, not waited)
    waits = op_runner.sleeps.waits
    assert len(waits) == len(tries) - 1
    assert all(2 ** (n - 1) * 0.5 <= w <= 2 ** (n - 1) * 0.5 + 1 for n, w in enumerate(waits, 1))


async def test_a_not_sent_rejection_exhausting_the_last_targets_completes_the_operation_in_one_pass(
    engine: AsyncEngine,
    op_runner: OpRunner,
    fake_llm: FakeLLM,
    setup: BulkTargets,
    current_epoch: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Carry T16-h: a not-sent rejection requests no follow-on (Ruling T16-f), whose handler would have run
    the exhaustion sweep; when its charge used up the operation's last pending targets, the next
    `bulk_operations` heavy pass sweeps them (`rerequest`, under its locks) and completes the operation in
    that same pass, requesting no batch."""
    async with engine.begin() as conn:  # every target in its fourth and last attempt
        await conn.execute(_CHARGE_THREE, {"b": setup.bulk_operation_id})
    fake_llm.batches(BatchScript(create="ok"))

    async def create(self: BatchesClient, requests: object) -> Rejected:
        return Rejected(429, "rate_limit_error", "")

    monkeypatch.setattr(BatchesClient, "create", create)
    assert setup.operation_id is not None
    await op_runner.run_one(setup.operation_id)
    (sub,) = await submission_rows(engine)
    assert sub.state == "submit_rejected" and sub.follow_on_at is not None
    assert await targets_by_state(engine, setup.llm_tuple_id) == {"pending": N}  # charged out, not swept
    assert await charged_attempts(engine, setup.llm_tuple_id) == 4 * N
    assert (await bulk_row(engine, setup.bulk_operation_id)).state == "active"
    assert await reconcile(engine, current_epoch) == 1
    assert await targets_by_state(engine, setup.llm_tuple_id) == {"failed_attempts_exhausted": N}
    assert (await bulk_row(engine, setup.bulk_operation_id)).state == "completed"
    assert await count_rows(engine, "operations", kind="batch_submit") == 1  # no batch requested
    assert await reconcile(engine, current_epoch) == 0  # nothing left
