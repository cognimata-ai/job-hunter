"""The per-item decision of a batch result (spec §10.10 step 6 and its decision table L3962-3976, §7.16.7;
plan Task 17, Rulings T16-e, T7-b, Codex r1 #18, #26, #27, r2 B14, r5 B6, A1 issue 22, A2 B1, B3, B4, R6,
Carries T8-b, T10-b, T10-f, Deviation 27): `process_item`, one item in the caller's transaction, idempotent on
`batch_items.processed_at`.

**The transaction**, in the lock schedule's order (§3.3.3, Deviation 38):
1. `active_config FOR SHARE` and the item's job row `FOR SHARE` (`publish.lock_config_and_job`, the caller's
   steps 1 and 1a of the publish transaction, before the owner row: Carry T10-b);
2. the owner, the submission `FOR SHARE` in the state the caller expects (`BatchSubmissionOwner`, else
   `FencedOut`);
3. **the spend first, with the gateway's own classification** (Codex r1 #26): a `succeeded` item's message is
   classified as an online try of its first stage is (`gateway.classify_message`: another served model is
   `model_mismatch`, `max_tokens` is `max_tokens_first_stage`, `refusal`, `end_turn` with the schema's mirror,
   the parse and `storable`, else `invalid_output`, any other stop reason `unexpected_stop_reason`) and
   settled `billed` from its usage at batch prices, its call `completed` with the code and the wire JSON only
   when the output is usable, so the usage is always billed and a partial, refused, malformed or unstorable
   output is never stored; an `errored`, `canceled` or `expired` item settles `not_billed(batch_item_errored |
   batch_item_canceled | batch_item_expired)` with its call `error` (`call_error_code`: the code its decision
   reason names, an errored item's followed by `:` and the provider's nested `error.error.type`, made storable
   and bounded to `PROVIDER_TYPE_MAX` characters, never the provider's message, Carry T8-b); a
   `missing` item (synthesised after a complete stream) settles `ambiguous` (A1 issue 22). An entry already
   `ambiguous` (a released submission's) takes the late edge `ambiguous → billed | not_billed` (L2974-2975,
   Codex N9), and the attempt's `cost_usd` follows (`settle`: ambiguous its reservation, late billed its cost;
   Carry T7-b). `settle` takes the month row and the funded entry's bulk row before the ledger rows; the
   alert of the classification (`gateway.raise_classified_alert`: the critical `llm.model_mismatch` of G11,
   the warn `llm.unexpected_stop_reason`) is raised in the same transaction;
4. the submission's bulk row `FOR NO KEY UPDATE` (step 7, held already for a funded entry: a hold release
   later re-takes it), then the item row `FOR UPDATE` (step 10): an item processed meanwhile returns its
   stored decision and this transaction has written nothing (its settlement was the duplicate `settle`
   logs);
5. **the fence**: the target `FOR UPDATE` under `state = 'in_batch' AND batch_submission_id = :s AND
   attempt_no = :a` (L3959). No row is `ignored_fenced`: the target was released, cancelled (a tuple
   supersession cancels the targets of its submissions, so their `canceled` items land here, A2 B1) or moved
   on, and the attempt, if still `running`, ends `cancelled('ignored_fenced')` (a non-owner ends an attempt
   only so, Carry T10-f);
6. with the fence passed, the decision table (below), the item recorded (`awaiting → <result type>`,
   `decision`, `stop_reason`), the item's hold released once for every decision but `followup`
   (`bulk.release_item_hold`), and `ranking.bump` last (the publish transaction bumps it itself).

**The decision table** (L3962-3976), keyed by the result type and, for a `succeeded` item, by the code:
- `model_mismatch`: `failed_request` → the target `failed_request('model_mismatch')`, the attempt
  `failed_request`; nothing is published under the wrong identity;
- `invalid_output`, or a usable output the stage-1 parse refuses (`parse_generator` with the tuple version's
  parser family, as `load_context` loads it: Codex r5 B6): `retry_online` → `pending('batch_invalid_output')`,
  no draft, the attempt `failed_invalid_output('batch_invalid_output')`;
- a usable output: its draft stored (`score_drafts`, round 0, `validation = {"normalisation": …}`, under the
  batch attempt), `steps.validate_step` under the active scope and the batch attempt's evaluation policy (V1-
  V6, knockouts, the provisional aggregation, T1-T8; L3967), then the routing of `routing.after_validate`: no
  trigger and no failure publishes `accepted` (`publish.publish_result` under `BatchPredicate`: `published`,
  the attempt `published_accepted`; a purge-fenced job is `ignored_fenced`, a publication `Bug` is
  `failed_invalid_output`), anything else is `followup`: the target `in_batch → pending` with `lane =
  'batch_followup'` and `resume_draft_id`, its batch pin cleared, the attempt `batch_draft_ready`, and the
  hold kept until the follow-up's target reaches a terminal state (Task 10's releasers);
- `refused`: the target `refused(category)` with `refusal_category`, the attempt `refused`;
- `max_tokens_first_stage`: `retry_online` → `pending('batch_max_tokens')`, the attempt `failed_max_tokens`
  (`load_context` starts the next attempt at the enlarged stage);
- `unexpected_stop_reason`: the target and the attempt `failed_invalid_output('unexpected_stop_reason')`;
- `errored` of type `invalid_request_error` (the SDK's nested `error.error.type`, Carry T8-b):
  `failed_request('batch_invalid_request')`, the attempt `failed_request`;
- `errored` of any other type, `canceled` (a cancel requested for a superseded tuple fences instead, step 5),
  `expired` and `missing`: `retry_online` → `pending('batch_errored' | 'batch_canceled' | 'batch_expired' |
  'batch_missing')`, the attempt `transient_error`.
Every `retry_online` target is never batched again (`batches.ELIGIBLE_SQL`: its reason, and its item's
result). **An `in_batch` target never has an unreleased `followup` item** (Carry T10-b): a follow-up's item
has a result, so `ELIGIBLE_SQL` never batches its target again, and the publish transaction's
`release_target_hold`, which takes the bulk row after the target row, finds nothing to release here.

Nothing here logs content: ids, result types, decisions and codes only.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db import ranking
from jobhunter.db.sql import q
from jobhunter.db.transitions import CLEAR_REASON, transition, transition_where
from jobhunter.errors import InvariantViolation
from jobhunter.llm import bulk, spend_ledger
from jobhunter.llm.batches import ItemResult
from jobhunter.llm.gateway import (
    INVALID_OUTPUT,
    MAX_TOKENS,
    MAX_TOKENS_FIRST_STAGE,
    MODEL_MISMATCH,
    REFUSED,
    Classified,
    classify_message,
    raise_classified_alert,
)
from jobhunter.llm.prompts.render import PromptVersion, load_prompt_version, score_schema
from jobhunter.llm.spend_ledger import (
    BatchState,
    BatchSubmissionOwner,
    CallOutcome,
    Reservation,
    storable_text,
)
from jobhunter.observability.logging import get_logger
from jobhunter.scoring import publish as publishing
from jobhunter.scoring import routing, steps
from jobhunter.scoring.attempt_inputs import load_attempt_inputs
from jobhunter.scoring.drafts import (
    INSERT_DRAFT,
    READ_DRAFT,
    SAVE_VALIDATION,
    draft_params,
    validation_params,
)
from jobhunter.scoring.filters import active_scope
from jobhunter.scoring.steps import NoteRecord, StoredDraft, StoredNote, ValidationRecord
from jobhunter.scoring.validator import ParsedGenerator, StructuralFailure, parse_generator

Decided = Literal[
    "published",
    "followup",
    "refused",
    "retry_online",
    "failed_request",
    "failed_invalid_output",
    "ignored_fenced",
]

IGNORED_FENCED: Final = "ignored_fenced"
# The provider's errored type of a request it refused (`error.error.type`, Carry T8-b).
INVALID_REQUEST_ERROR: Final = "invalid_request_error"
PROVIDER_TYPE_MAX: Final = 64  # the characters of that type an errored item's call code keeps
# The decisions' reasons (§7.16.3: the targets' `state_reason`, the attempts' `outcome_detail`).
BATCH_INVALID_OUTPUT: Final = "batch_invalid_output"
BATCH_MAX_TOKENS: Final = "batch_max_tokens"
BATCH_INVALID_REQUEST: Final = "batch_invalid_request"
UNEXPECTED_STOP_REASON: Final = "unexpected_stop_reason"
FOLLOWUP_LANE: Final = "batch_followup"
_RETRY_REASON: Final[Mapping[str, str]] = {
    "errored": "batch_errored",
    "canceled": "batch_canceled",
    "expired": "batch_expired",
    "missing": "batch_missing",
}
# The positive evidence of no charge of an item the provider did not run (§6.9.1).
_EVIDENCE: Final[Mapping[str, str]] = {
    "errored": "batch_item_errored",
    "canceled": "batch_item_canceled",
    "expired": "batch_item_expired",
}

# What an item's processing reads first, without a lock: its target, tuple, attempt and reserved entry.
_ITEM: Final = q(
    "SELECT i.id, i.target_id, i.attempt_no, i.result_type, i.decision, t.job_id, t.job_revision_id, "
    "t.llm_tuple_id, u.prompt_schema_version_id, a.id AS attempt_id, c.id AS call_id, l.id AS ledger_id, "
    "l.budget_month, l.reserved_usd, l.purpose_class, l.model_requested FROM batch_items i "
    "JOIN score_targets t ON t.id = i.target_id JOIN llm_tuples u ON u.id = t.llm_tuple_id "
    "JOIN score_attempts a ON a.target_id = i.target_id AND a.attempt_no = i.attempt_no "
    "JOIN llm_calls c ON c.id = i.llm_call_id JOIN llm_spend_ledger l ON l.id = c.spend_ledger_id "
    "WHERE i.id = :i AND i.submission_id = :s"
)
_LOCK_ITEM: Final = q("SELECT result_type, decision FROM batch_items WHERE id = :i FOR UPDATE")
_LOCK_BATCHED: Final = q(
    "SELECT id FROM score_targets WHERE id = :t AND state = 'in_batch' AND batch_submission_id = :s "
    "AND attempt_no = :a FOR UPDATE"
)
_ATTEMPT: Final = "t.id = :attempt"

log = get_logger("jobhunter.scoring.batch_items")


@dataclass(frozen=True)
class Submission:
    """The submission an item belongs to, as its processing needs it: its id, the state its owner predicate
    expects (`ended` while its results are processed) and its bulk operation."""

    id: int
    state: BatchState
    bulk_operation_id: int


@dataclass(frozen=True)
class Item:
    """One item of a submission (ids only): its row, its target and its attempt number."""

    id: int
    target_id: int
    attempt_no: int


@dataclass(frozen=True)
class Decision:
    """How an item was decided: the decision (`batch_items.decision`), the result type it recorded, the state
    its target moved to (`None` when it was fenced and left as it was), and whether it was decided before
    (`replayed`: the stored decision, nothing written now)."""

    decision: Decided
    result_type: str
    target_state: str | None = None
    replayed: bool = False


@dataclass(frozen=True)
class _ItemRow:
    item_id: int
    target_id: int
    attempt_no: int
    attempt_id: int
    job_id: int
    job_revision_id: int
    llm_tuple_id: int
    prompt_schema_version_id: int
    model: str
    reservation: Reservation

    @classmethod
    def of(cls, row: Row[tuple[object, ...]]) -> "_ItemRow":
        return cls(
            item_id=int(row.id),
            target_id=int(row.target_id),
            attempt_no=int(row.attempt_no),
            attempt_id=int(row.attempt_id),
            job_id=int(row.job_id),
            job_revision_id=int(row.job_revision_id),
            llm_tuple_id=int(row.llm_tuple_id),
            prompt_schema_version_id=int(row.prompt_schema_version_id),
            model=str(row.model_requested),
            reservation=Reservation(
                ledger_id=int(row.ledger_id),
                llm_call_id=int(row.call_id),
                month=row.budget_month,
                reserved_usd=row.reserved_usd,
                purpose_class=row.purpose_class,
                model=row.model_requested,
                batch=True,
            ),
        )


@dataclass(frozen=True)
class _Decided:
    """What the fence and the table decided: the decision, the target's new state (`None`: left as it was)
    and whether the publish transaction bumped the ranking already."""

    decision: Decided
    target_state: str | None = None
    bumped: bool = False


@dataclass(frozen=True)
class _Move:
    """A decision with the fence passed: the target's new state and reason, the attempt's outcome and detail,
    and the target's other columns."""

    decision: Decided
    target_state: str
    target_reason: str | None
    attempt_outcome: str
    attempt_detail: str | None
    target_set: Mapping[str, object] | None = None


def _stored(row: Row[tuple[object, ...]]) -> Decision:
    decided: Decided = row.decision
    return Decision(decided, str(row.result_type), replayed=True)


class _ItemStore:
    """The `steps.DraftStore` of a batch item, in the item's transaction, which holds its owner and its
    target: the generated draft and its validation only (a batch runs no critic)."""

    def __init__(self, conn: AsyncConnection, item: _ItemRow) -> None:
        self._conn = conn
        self._item = item

    async def add_draft(self, *, round: int, llm_call_id: int, parsed: ParsedGenerator) -> int:
        params = draft_params(
            attempt_id=self._item.attempt_id, round=round, llm_call_id=llm_call_id, parsed=parsed
        )
        return int((await self._conn.execute(INSERT_DRAFT, params)).scalar_one())

    async def draft(self, draft_id: int) -> StoredDraft:
        row = (await self._conn.execute(READ_DRAFT, {"d": draft_id})).first()
        if row is None:
            raise LookupError(f"score draft {draft_id} does not exist")
        return StoredDraft.from_row(
            id=int(row.id), round=int(row.round), output_json=row.output_json, validation=row.validation
        )

    async def save_validation(self, draft_id: int, record: ValidationRecord) -> None:
        await self._conn.execute(SAVE_VALIDATION, validation_params(draft_id, record))

    async def add_note(self, note: NoteRecord) -> int:
        raise InvariantViolation("a batch item stores no critic note")

    async def note(self, note_id: int) -> StoredNote:
        raise InvariantViolation("a batch item reads no critic note")


async def process_item(
    conn: AsyncConnection, *, submission: Submission, item: Item, result: ItemResult, now: datetime
) -> Decision:
    """Decide `item` from `result` in the caller's transaction (module docstring); its `Decision`. A second
    call returns the stored decision and writes nothing. `FencedOut` when the submission is not in the state
    `submission.state` names."""
    found = (await conn.execute(_ITEM, {"i": item.id, "s": submission.id})).first()
    if found is None:
        raise LookupError(f"batch item {item.id} of submission {submission.id} does not exist")
    if found.decision is not None:
        return _stored(found)
    row = _ItemRow.of(found)
    await publishing.lock_config_and_job(conn, row.job_id)  # steps 1 and 1a
    await BatchSubmissionOwner(submission.id, submission.state).lock(conn)  # step 5
    version = await load_prompt_version(conn, row.prompt_schema_version_id)
    classified = await _settle(conn, row, result, version, now=now)  # steps 6, 7 and 9
    await bulk.lock_accounts(conn, submission.bulk_operation_id)  # step 7 (re-taken when funded)
    locked = (await conn.execute(_LOCK_ITEM, {"i": item.id})).one()  # step 10
    if locked.decision is not None:  # decided meanwhile: its settlement was the duplicate
        return _stored(locked)
    fence = {"t": row.target_id, "s": submission.id, "a": row.attempt_no}
    if (await conn.execute(_LOCK_BATCHED, fence)).first() is None:
        decided = await _fenced(conn, row)
    else:
        decided = await _decide(conn, submission, row, result, classified, version, now=now)
    decision = Decision(decided.decision, result.result_type, decided.target_state)
    stop_reason = None if classified is None else storable_text(classified.stop_reason)
    await transition(
        conn,
        "batch_items",
        item.id,
        ["awaiting"],
        result.result_type,
        reason=decision.decision,
        set_={"stop_reason": stop_reason},
    )
    released = None
    if decision.decision != "followup":
        released = await bulk.release_item_hold(conn, item_id=item.id)
    if decision.target_state is not None and not decided.bumped:
        await ranking.bump(conn)  # step 11, last (a publish transaction bumped it itself)
    log.info(
        "batch_item_processed",
        submission_id=submission.id,
        item_id=item.id,
        target_id=row.target_id,
        result_type=result.result_type,
        error_code=None if classified is None else classified.code,
        decision=decision.decision,
        target_state=decision.target_state,
        hold_released_usd=None if released is None else str(released),
    )
    return decision


async def _settle(
    conn: AsyncConnection, row: _ItemRow, result: ItemResult, version: PromptVersion, *, now: datetime
) -> Classified | None:
    """Step 3 (module docstring): the item's entry settled from its result; the classification of a
    `succeeded` one."""
    res = row.reservation
    if result.result_type == "succeeded":
        if result.message is None:
            raise InvariantViolation(f"batch item {row.item_id}: a succeeded result without its message")
        classified = classify_message(
            node_model=res.model, schema=score_schema("generate", version), message=result.message
        )
        output = classified.parsed  # exactly when the code is `None`: the wire JSON is stored then only
        call = CallOutcome(
            status="completed",
            model_served=classified.model_served,
            error_code=classified.code,
            stop_reason=classified.stop_reason,
            refusal_category=classified.refusal_category,
            provider_request_id=result.message.id,
            output_json=None if output is None else output.model_dump(mode="json"),
        )
        await spend_ledger.settle(
            conn, res, outcome="billed", evidence=None, usage=classified.usage, call=call, now=now
        )
        await raise_classified_alert(conn, classified, purpose="generate", requested=res.model)
        return classified
    if result.result_type == "missing":
        call = CallOutcome(status="ambiguous", error_code=_RETRY_REASON["missing"])
        await spend_ledger.settle(
            conn, res, outcome="ambiguous", evidence=None, usage=None, call=call, now=now
        )
        return None
    call = CallOutcome(status="error", error_code=call_error_code(result))
    await spend_ledger.settle(
        conn,
        res,
        outcome="not_billed",
        evidence=_EVIDENCE[result.result_type],
        usage=None,
        call=call,
        now=now,
    )
    return None


def call_error_code(result: ItemResult) -> str:
    """The call's code of an item the provider did not run (module docstring): its reason, and an errored
    item's provider type after a `:` (storable, at most `PROVIDER_TYPE_MAX` characters; never the message)."""
    reason = _reason_of(result)
    if result.result_type != "errored" or not result.error_type:
        return reason
    return f"{reason}:{storable_text(result.error_type)[:PROVIDER_TYPE_MAX]}"


def _reason_of(result: ItemResult) -> str:
    """The reason of an item the provider did not run (the call's code and the target's reason)."""
    if result.result_type == "errored" and result.error_type == INVALID_REQUEST_ERROR:
        return BATCH_INVALID_REQUEST
    return _RETRY_REASON[result.result_type]


async def _fenced(conn: AsyncConnection, row: _ItemRow) -> _Decided:
    """Step 5's `ignored_fenced`: the attempt, if still `running`, ends `cancelled('ignored_fenced')`."""
    await transition_where(
        conn,
        "score_attempts",
        ["running"],
        "cancelled",
        reason=IGNORED_FENCED,
        where=_ATTEMPT,
        params={"attempt": row.attempt_id},
    )
    return _Decided("ignored_fenced")


async def _decide(
    conn: AsyncConnection,
    submission: Submission,
    row: _ItemRow,
    result: ItemResult,
    classified: Classified | None,
    version: PromptVersion,
    *,
    now: datetime,
) -> _Decided:
    """Step 6's table (module docstring), the fence passed."""
    if classified is None:  # the provider ran nothing: errored, canceled, expired or missing
        reason = _reason_of(result)
        if reason == BATCH_INVALID_REQUEST:
            move = _Move("failed_request", "failed_request", reason, "failed_request", reason)
        else:
            move = _Move("retry_online", "pending", reason, "transient_error", reason)
        return await _move(conn, submission, row, move)
    code = classified.code
    if code == MODEL_MISMATCH:
        move = _Move("failed_request", "failed_request", MODEL_MISMATCH, "failed_request", MODEL_MISMATCH)
    elif code == REFUSED:
        category = storable_text(classified.category)
        move = _Move("refused", "refused", category, "refused", category, {"refusal_category": category})
    elif code in (MAX_TOKENS_FIRST_STAGE, MAX_TOKENS):
        move = _Move("retry_online", "pending", BATCH_MAX_TOKENS, "failed_max_tokens", BATCH_MAX_TOKENS)
    elif code is None and classified.parsed is not None:
        return await _usable(conn, submission, row, classified.parsed, version, now=now)
    elif code == INVALID_OUTPUT:
        move = _invalid_output()
    else:
        move = _Move(
            "failed_invalid_output",
            "failed_invalid_output",
            UNEXPECTED_STOP_REASON,
            "failed_invalid_output",
            UNEXPECTED_STOP_REASON,
        )
    return await _move(conn, submission, row, move)


def _invalid_output() -> _Move:
    return _Move(
        "retry_online", "pending", BATCH_INVALID_OUTPUT, "failed_invalid_output", BATCH_INVALID_OUTPUT
    )


async def _move(conn: AsyncConnection, submission: Submission, row: _ItemRow, move: _Move) -> _Decided:
    """The target `in_batch → <state>` under the batch pin (its pin cleared on exit), then its attempt."""
    await transition(
        conn,
        "score_targets",
        row.target_id,
        ["in_batch"],
        move.target_state,
        reason=CLEAR_REASON if move.target_reason is None else move.target_reason,
        owner={"batch_submission_id": submission.id, "attempt_no": row.attempt_no},
        set_=move.target_set,
    )
    await transition(
        conn, "score_attempts", row.attempt_id, ["running"], move.attempt_outcome, reason=move.attempt_detail
    )
    return _Decided(move.decision, move.target_state)


async def _usable(
    conn: AsyncConnection,
    submission: Submission,
    row: _ItemRow,
    output: BaseModel,
    version: PromptVersion,
    *,
    now: datetime,
) -> _Decided:
    """A usable output (module docstring): parsed with the stored family, stored, validated, then published
    or handed to an online follow-up."""
    parsed = parse_generator(output.model_dump(mode="json"), family=version.parser_family)
    if isinstance(parsed, StructuralFailure):
        log.info(
            "batch_item_unparsed",
            item_id=row.item_id,
            error_class=parsed.error_class,
            path=parsed.path,
        )
        return await _move(conn, submission, row, _invalid_output())
    _, scope = await active_scope(conn)  # `active_config` held FOR SHARE since step 1
    inputs, _ = await load_attempt_inputs(
        conn,
        target_id=row.target_id,
        attempt_id=row.attempt_id,
        prompt_schema_version_id=row.prompt_schema_version_id,
    )
    store = _ItemStore(conn, row)
    draft_id = await store.add_draft(round=0, llm_call_id=row.reservation.llm_call_id, parsed=parsed)
    validated = await steps.validate_step(
        inputs, store, draft_id=draft_id, scope=scope, note=None, critic_rounds=0
    )
    nxt = routing.after_validate(
        triggers=validated.triggers,
        det_failures=validated.det_failures,
        critic_rounds=0,
        round=0,
        review=None,
    )
    if nxt.route != "publish":
        followup = _Move(
            "followup",
            "pending",
            None,
            "batch_draft_ready",
            None,
            {"lane": FOLLOWUP_LANE, "resume_draft_id": draft_id},
        )
        log.info("batch_item_followup", item_id=row.item_id, draft_id=draft_id, triggers=validated.triggers)
        return await _move(conn, submission, row, followup)
    outcome = await publishing.publish_result(
        conn,
        target=publishing.TargetRow(row.target_id, row.job_id, row.job_revision_id, row.llm_tuple_id),
        attempt_id=row.attempt_id,
        final_draft_id=draft_id,
        mode="accepted",
        predicate=publishing.BatchPredicate(submission.id, row.attempt_no),
        inputs=inputs,
        last_critic_note_id=None,
        now=now,
    )
    match outcome.reason:
        case "published":
            return _Decided("published", "published_accepted", bumped=True)
        case "fenced":  # a purge fence holds the job: the attempt ended `cancelled('purge_fenced')`
            return _Decided("ignored_fenced")
        case "publication_invalid":  # a `Bug`: the target and the attempt failed, the ranking bumped
            return _Decided("failed_invalid_output", "failed_invalid_output", bumped=True)
        case _:  # the target is held under the batch predicate since step 5
            raise InvariantViolation(f"score target {row.target_id}: the batch publish lost its own fence")
