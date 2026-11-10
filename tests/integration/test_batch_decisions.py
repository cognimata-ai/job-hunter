"""The per-item decisions of a batch (spec §10.10 step 6 and its decision table L3962-3976, §7.16.7; plan Task
17, Review Focus 2 and 4, Codex r1 #26, #27, A2 B1, Carries T8-b, T10-b): every item settles its own spend
from its own result and decides its target.

- Review Focus 4: a partly expired batch with missing items settles each item (`billed`, `not_billed`,
  `ambiguous`), publishes or hands to an online follow-up every succeeded one, retries the others online and
  never batches them again, alerts on the missing ones, keeps the follow-ups' holds, and leaves the ledger's
  counters equal to its rows.
- Review Focus 2: a profile confirmed during a batch cancels its `in_batch` targets; the poll sends the
  provider cancel, and every late item is `ignored_fenced` with its spend settled from usage and no output
  stored (P2, M5 Task 11).
- Codex r1 #26: a succeeded item whose output holds a NUL or a lone surrogate, is cut short, stopped at
  `max_tokens` or came from another model settles `billed` with no output stored and the gateway's code.
- The other rows of the table: a refusal, an unexpected stop, an unreadable output, both errored types, a
  provider-side cancel.
- Carry T10-b: a follow-up target is never batched again (no `in_batch` target has an unreleased follow-up
  item, which the batch publish's hold release relies on).
- Carry T16-h: every unreleased follow-up item is the active bulk operation's, a supersession included
  (which `batches.rerequest`'s exhaustion sweep relies on for its bulk rows' id order).

Every datum is invented.
"""

import json
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.llm import budget, spend_ledger
from jobhunter.llm.fake import BatchScript, ItemOutcome
from jobhunter.llm.gateway import MODEL_MISMATCH_ALERT
from jobhunter.settings.model import ModelPolicy
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.batches import (
    PUBLISHED_BODY,
    batch_ledger,
    bulk_row,
    decisions,
    eligible_for_batching,
    entries_by_custom_id,
    item_rows,
    open_alert_codes,
    poll_once,
    submission_state,
    submit_batch,
    targets_by_custom_id,
)
from tests.support.fixtures import count_rows
from tests.support.llm import FakeLLM
from tests.support.profile import OpRunner
from tests.support.scoring import operations_of
from tests.support.settings import confirm_new_profile

pytestmark = pytest.mark.integration
N = 250
OPUS = "claude-opus-5-5"
# The test evaluation policy's projected critic rate (0.3) times ĉ_followup of policy B: an item's hold.
HOLD = (Decimal("0.3") * budget.c_hat_followup(ModelPolicy())).quantize(Decimal("0.000001"), ROUND_HALF_UP)
_TARGETS_OF_TUPLE = sa.text("SELECT count(*) FROM score_targets WHERE llm_tuple_id = :t AND state = :s")
_UNRELEASED_OUTSIDE_ACTIVE = sa.text(
    "SELECT count(*) FROM batch_items i JOIN batch_submissions s ON s.id = i.submission_id "
    "JOIN bulk_operations b ON b.id = s.bulk_operation_id "
    "WHERE i.decision = 'followup' AND i.hold_released_at IS NULL AND b.state <> 'active'"
)
_CLEAR_LANE = sa.text("UPDATE score_targets SET lane = NULL WHERE id = ANY(CAST(:ids AS bigint[]))")


@pytest.fixture
def queued(queue_app: procrastinate.App) -> None:
    """The task app the poll's follow-on and cycle requests defer on."""


def ledger_states(ledger: list[tuple[str, str | None]]) -> Counter[str]:
    return Counter(state for state, _ in ledger)


async def test_partly_expired_batch_settles_each_item_and_retries_online(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, queued: None
) -> None:
    """Review Focus 4 (L3962-3976, L3961): 200 succeeded (`followups` of them T2 follow-ups), 40 expired, 10
    without a line after a complete stream."""
    followups = 10
    submitted = await submit_batch(engine, op_runner, fake_llm, N, followups=followups)
    ids = submitted.custom_ids
    fake_llm.batches(
        BatchScript(
            items={
                **dict.fromkeys(ids[:200], "succeeded"),
                **dict.fromkeys(ids[200:240], "expired"),
                **dict.fromkeys(ids[240:], "missing"),
            },
            polls_until_ended=0,  # one poll (Carry T8-e)
        )
    )
    await poll_once(engine, fake_llm)
    assert await submission_state(engine, submitted.submission_id) == "reconciled"
    assert Counter(await decisions(engine)) == {
        "published": 200 - followups,
        "followup": followups,
        "retry_online": 50,
    }
    assert ledger_states(await batch_ledger(engine)) == {"billed": 200, "not_billed": 40, "ambiguous": 10}
    entries = await entries_by_custom_id(engine)
    assert {entries[c].evidence for c in ids[200:240]} == {"batch_item_expired"}
    targets = await targets_by_custom_id(engine)
    expired, missing = ids[200:240], ids[240:]
    assert {(targets[c].state, targets[c].state_reason) for c in expired} == {("pending", "batch_expired")}
    assert {(targets[c].state, targets[c].state_reason) for c in missing} == {("pending", "batch_missing")}
    assert {targets[c].outcome for c in ids[200:]} == {"transient_error"}
    assert {(targets[c].state, targets[c].lane) for c in ids[:followups]} == {("pending", "batch_followup")}
    assert all(targets[c].resume_draft_id is not None for c in ids[:followups])
    assert {targets[c].outcome for c in ids[:followups]} == {"batch_draft_ready"}
    assert {targets[c].state for c in ids[followups:200]} == {"published_accepted"}
    assert await open_alert_codes(engine) == ["batch.items_missing"]
    assert (await bulk_row(engine, submitted.setup.bulk_operation_id)).followup_held_usd == followups * HOLD
    items = await item_rows(engine)
    assert {c for c, item in items.items() if item.hold_released_at is None} == set(ids[:followups])
    retried = [t.target_id for t in submitted.setup.targets[200:]]
    assert await eligible_for_batching(engine, submitted.setup, retried) == []  # never batched again
    async with engine.connect() as conn:
        assert await spend_ledger.recompute(conn, budget.month_of(utcnow())) is None


async def test_reconfirm_during_a_batch_cancels_in_batch_targets_and_fences_late_items(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, queued: None
) -> None:
    """Review Focus 2 (A2 B1, L6443: the poll retrieves the batch, in progress, then sends the cancel)."""
    submitted = await submit_batch(engine, op_runner, fake_llm, N)
    _, tuple_b = await confirm_new_profile(engine)  # Task 14: the cancel recorded, in_batch → cancelled
    assert Counter(t.state for t in (await targets_by_custom_id(engine)).values()) == {"cancelled": N}
    fake_llm.batches(BatchScript(items=dict.fromkeys(submitted.custom_ids, "succeeded"), polls_until_ended=5))
    await poll_once(engine, fake_llm)  # the cancel sent, then the batch, ended at it, and its results
    assert fake_llm.state.batches[submitted.provider_id].cancel_requested is True
    assert set(await decisions(engine)) == {"ignored_fenced"} and await count_rows(engine, "llm_results") == 0
    assert set(ledger_states(await batch_ledger(engine))) <= {"billed", "not_billed"}
    # P2 (M5 Task 11): a fenced item settles from its usage and stores no output of the cancelled target
    entries = await entries_by_custom_id(engine)
    assert {(e.state, e.status) for e in entries.values()} == {("billed", "completed")}
    assert not any(e.has_output for e in entries.values())
    targets = await targets_by_custom_id(engine)
    assert {(t.state, t.state_reason) for t in targets.values()} == {("cancelled", "tuple_superseded")}
    assert await submission_state(engine, submitted.submission_id) == "reconciled"
    creation = [
        op for op in await operations_of(engine, "create_targets_for_tuple") if op.status == "pending"
    ]
    assert len(creation) == 1
    await op_runner.run_one(creation[0].id)  # B's create_targets_for_tuple operation
    async with engine.connect() as conn:
        pending_b = (await conn.execute(_TARGETS_OF_TUPLE, {"t": tuple_b, "s": "pending"})).scalar_one()
    assert pending_b > 0  # B's create_targets_for_tuple ran


def _with_summary(text: str) -> dict[str, Any]:
    return {**PUBLISHED_BODY, "summary_en": text}


async def test_an_unusable_succeeded_output_is_billed_and_never_stored(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, queued: None
) -> None:
    """Codex r1 #26: the gateway's classification of a succeeded item, spend first: a NUL in the output, a
    lone surrogate, an output cut short, a stop at `max_tokens` and another served model each settle `billed`
    from their usage with no output stored and the gateway's code; the targets are retried online (the first
    three as invalid outputs) or fail (the model mismatch, with the critical alert of G11); each hold is
    released once."""
    surrogate = json.dumps(_with_summary("\ud800"), ensure_ascii=True)  # the escape reaches the decoder
    cut = json.dumps(PUBLISHED_BODY, ensure_ascii=False)[:200]
    bodies = {0: _with_summary("A good fit.\x00"), 1: surrogate, 2: cut}
    submitted = await submit_batch(engine, op_runner, fake_llm, N, bodies=bodies)
    nul, lone, short, stopped, other = submitted.custom_ids[:5]
    fake_llm.answer("generate", submitted.setup.targets[4].job_revision_id, PUBLISHED_BODY, model=OPUS)
    fake_llm.batches(BatchScript(items={stopped: "succeeded:max_tokens"}, polls_until_ended=0))
    await poll_once(engine, fake_llm)
    entries = await entries_by_custom_id(engine)
    codes = {c: (entries[c].state, entries[c].error_code, entries[c].has_output) for c in (nul, lone, short)}
    assert codes == dict.fromkeys((nul, lone, short), ("billed", "invalid_output", False))
    assert (entries[stopped].state, entries[stopped].error_code) == ("billed", "max_tokens_first_stage")
    assert (entries[other].state, entries[other].error_code) == ("billed", "model_mismatch")
    assert not entries[stopped].has_output and not entries[other].has_output
    assert all(entries[c].settled_usd > 0 for c in (nul, lone, short, stopped, other))  # the usage is billed
    targets = await targets_by_custom_id(engine)
    assert {(targets[c].state, targets[c].state_reason) for c in (nul, lone, short)} == {
        ("pending", "batch_invalid_output")
    }
    assert {(targets[c].outcome, targets[c].outcome_detail) for c in (nul, lone, short)} == {
        ("failed_invalid_output", "batch_invalid_output")
    }
    assert (targets[stopped].state, targets[stopped].state_reason) == ("pending", "batch_max_tokens")
    assert targets[stopped].outcome == "failed_max_tokens"
    assert (targets[other].state, targets[other].state_reason) == ("failed_request", "model_mismatch")
    assert targets[other].outcome == "failed_request"
    assert MODEL_MISMATCH_ALERT in await open_alert_codes(engine)
    items = await item_rows(engine)
    assert all(items[c].hold_released_at is not None for c in (nul, lone, short, stopped, other))
    assert await count_rows(engine, "score_drafts") == N - 5  # none for an unusable output
    assert Counter(await decisions(engine)) == {"published": N - 5, "retry_online": 4, "failed_request": 1}


async def test_the_rest_of_the_decision_table(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, queued: None
) -> None:
    """L3962-3976: a refusal, an unexpected stop, an output that is no JSON, an `invalid_request` error (the
    SDK's nested type, Carry T8-b), another error type and a cancel the provider made on its own."""
    submitted = await submit_batch(engine, op_runner, fake_llm, N)
    refused, unexpected, invalid, bad_request, errored, canceled = submitted.custom_ids[:6]
    script: dict[str, ItemOutcome] = {
        refused: "succeeded:refusal",
        unexpected: "succeeded:unexpected_stop",
        invalid: "succeeded:invalid",
        bad_request: "errored:invalid_request",
        errored: "errored:other",
        canceled: "canceled",
    }
    fake_llm.batches(BatchScript(items=script, polls_until_ended=0))
    await poll_once(engine, fake_llm)
    items, entries = await item_rows(engine), await entries_by_custom_id(engine)
    targets = await targets_by_custom_id(engine)

    def row(c: str) -> tuple[Any, ...]:
        e, t = entries[c], targets[c]
        return (items[c].decision, e.state, e.evidence, e.error_code, t.state, t.state_reason, t.outcome)

    assert row(refused)[:5] == ("refused", "billed", None, "refused", "refused")
    assert (targets[refused].refusal_category, targets[refused].outcome) == (
        targets[refused].state_reason,
        "refused",
    )
    assert items[refused].stop_reason == "refusal"
    assert row(unexpected) == (
        "failed_invalid_output",
        "billed",
        None,
        "unexpected_stop_reason",
        "failed_invalid_output",
        "unexpected_stop_reason",
        "failed_invalid_output",
    )
    assert row(invalid) == (
        "retry_online",
        "billed",
        None,
        "invalid_output",
        "pending",
        "batch_invalid_output",
        "failed_invalid_output",
    )
    assert row(bad_request) == (
        "failed_request",
        "not_billed",
        "batch_item_errored",
        "batch_invalid_request:invalid_request_error",  # the provider's nested type on the call (T8-b)
        "failed_request",
        "batch_invalid_request",
        "failed_request",
    )
    assert row(errored) == (
        "retry_online",
        "not_billed",
        "batch_item_errored",
        "batch_errored:api_error",
        "pending",
        "batch_errored",
        "transient_error",
    )
    assert row(canceled) == (
        "retry_online",
        "not_billed",
        "batch_item_canceled",
        "batch_canceled",
        "pending",
        "batch_canceled",
        "transient_error",
    )
    assert "llm.unexpected_stop_reason" in await open_alert_codes(engine)
    assert {items[c].result_type for c in (bad_request, errored)} == {"errored"}
    retried = [t.target_id for t in submitted.setup.targets[:6]]
    assert await eligible_for_batching(engine, submitted.setup, retried) == []


async def test_a_follow_up_target_is_never_batched_again(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, queued: None
) -> None:
    """Carry T10-b: the batch publish takes the bulk row after the target row (its follow-up hold release),
    which is safe only because no `in_batch` target has an unreleased `followup` item: a follow-up's item has
    a result, so `ELIGIBLE_SQL` never batches its target again, whatever its lane becomes."""
    submitted = await submit_batch(engine, op_runner, fake_llm, N, followups=3)
    fake_llm.batches(BatchScript(polls_until_ended=0))
    await poll_once(engine, fake_llm)
    followed = [t.target_id for t in submitted.setup.targets[:3]]
    items = await item_rows(engine)
    assert {items[c].decision for c in submitted.custom_ids[:3]} == {"followup"}
    assert all(items[c].hold_released_at is None for c in submitted.custom_ids[:3])  # its hold kept
    assert await eligible_for_batching(engine, submitted.setup, followed) == []
    async with engine.begin() as conn:  # the lane cleared, as a later online attempt may leave it
        await conn.execute(_CLEAR_LANE, {"ids": followed})
    assert await eligible_for_batching(engine, submitted.setup, followed) == []


async def test_every_unreleased_follow_up_item_is_the_active_bulk_operations(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, queued: None
) -> None:
    """Carry T16-h (`batches.rerequest`): the `bulk_operations` pass holds the active operation's bulk row
    before the exhaustion sweep locks the bulk rows of its candidates' unreleased follow-up items, which is in
    id order only while every such item is the active operation's. A profile confirmed while follow-ups hold
    supersedes the operation and cancels their targets, releasing every hold in that transaction: no
    unreleased follow-up item outlives its operation's `active` state."""
    submitted = await submit_batch(engine, op_runner, fake_llm, N, followups=3)
    fake_llm.batches(BatchScript(polls_until_ended=0))
    await poll_once(engine, fake_llm)
    items = await item_rows(engine)
    assert all(items[c].hold_released_at is None for c in submitted.custom_ids[:3])
    assert await _unreleased_outside_the_active_operation(engine) == 0
    await confirm_new_profile(engine)
    assert (await bulk_row(engine, submitted.setup.bulk_operation_id)).state == "cancelled"
    assert await _unreleased_outside_the_active_operation(engine) == 0
    items = await item_rows(engine)
    assert all(items[c].hold_released_at is not None for c in submitted.custom_ids[:3])


async def _unreleased_outside_the_active_operation(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(_UNRELEASED_OUTSIDE_ACTIVE)).scalar_one())
