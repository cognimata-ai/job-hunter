"""The fake Message Batches API as a test drives it (M3 Task 8).

`BatchScript`, `ItemOutcome` and `StoredBatch` are defined in `jobhunter.llm.fake`, the module that serves the
batch routes (backend code never imports `tests`), and re-exported here. The builders:
- `scripted_batches(script)` (or `scripted_batches(**fields)`, a `BatchScript` of those fields) registers the
  script in the test's fake state and returns the batches the fake stores, so a test asserts what was
  submitted (`requests`, `custom_ids`, `keys`), polled (`retrievals`, `results_read`) and cancelled
  (`cancel_requested`);
- `synthetic_batch_results_jsonl(items)` writes the results JSONL the fake serves for those item outcomes and
  one answer, in the given order, for a result reader fed without the fake.

M3 Task 16 adds the submission lifecycle's side:
- `bulk_with_targets(engine, n, *, budget_usd=None, operation=True)`: the score tuple made active with the
  test evaluation policy (`tests.support.scoring`), its bulk operation opened (`bulk.open`, the allocation of
  the active settings unless `budget_usd` replaces it), `n` pending targets of new synthetic-ad jobs in it
  (similarity descending with the insertion order), and, with `operation`, its `batch_submit` operation
  (`pending`, no job deferred), all in one transaction: `BulkTargets`;
- `prepared_submission(engine, gateway, setup, *, settings=None, now=None)`: the setup's `batch_submit`
  operation claimed (epoch 1) and `batches.prepare` run for it in a transaction of its own, `settings` the
  active ones unless given (a copy with other `scoring` fields, which a direct call may take below the
  settings model's floors): the result and the claim;
- `submission_rows(engine)`: every `batch_submissions` row, oldest first; `items_of(engine, submission_id)`:
  the submission's `(target_id, attempt_no)` pairs; `batch_ledger(engine)`: the batch entries' `(state,
  evidence)`, oldest first; `bulk_row(engine, bulk_operation_id)`: its accounts; `charged_attempts(engine,
  tuple_id)`: the tuple's targets' charged attempts summed; `make_due(engine)`: every `pending` operation due
  now (a requeue's `not_before` cleared).

M3 Task 17 adds the poll's and the adoption's side:
- `submit_batch(engine, op_runner, fake_llm, n, *, followups=0, bodies=None, polls_until_ended=1)`: `n`
  targets, each answered by `PUBLISHED_BODY` (the first `followups` by `FOLLOWUP_BODY`), and their batch
  submitted on the fake, not yet ended (a test registers the items' outcomes afterwards, with
  `polls_until_ended=0` for a single poll, Carry T8-e): `SubmittedBatch`;
- `poll_once(engine, fake_llm)` and `reconcile_ambiguous_once(engine, fake_llm)`: one pass of each task body
  on the fake, at the current epoch (1) unless given, with `defer_in_tx` (the test needs `queue_app`);
- `submission_state`, `decisions`, `item_rows`, `entries_by_custom_id`, `targets_by_custom_id`,
  `open_alert_codes` and `eligible_for_batching` read the outcome.

Every value is synthetic: ids, counts and the synthetic draft bodies of `tests.support.llm`.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

import httpx2
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.llm import bulk, fake
from jobhunter.llm.batches import BatchesClient
from jobhunter.llm.fake import ITEM_ERRORS, BatchScript, ItemOutcome, StoredBatch
from jobhunter.llm.gateway import LLMGateway
from jobhunter.scoring import batch_adoption, batch_results, batches
from jobhunter.settings.dryrun import active_settings
from jobhunter.settings.model import Settings
from jobhunter.worker.operations import OperationClaim, claim_operation, create_operation
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.llm import generator_wire_body, synthetic_message_json
from tests.support.scoring import (
    PendingTarget,
    activate_score_tuple,
    activate_test_policy,
    insert_score_job,
    synthetic_c_draft,
)
from tests.support.validation import synthetic_ad

__all__ = [
    "FOLLOWUP_BODY",
    "PUBLISHED_BODY",
    "BatchScript",
    "BulkTargets",
    "ItemOutcome",
    "StoredBatch",
    "SubmittedBatch",
    "batch_client",
    "batch_ledger",
    "bulk_row",
    "bulk_with_targets",
    "charged_attempts",
    "decisions",
    "eligible_for_batching",
    "entries_by_custom_id",
    "item_ids",
    "item_rows",
    "items_of",
    "make_due",
    "open_alert_codes",
    "poll_once",
    "prepared_submission",
    "reconcile_ambiguous_once",
    "scripted_batches",
    "submission_rows",
    "submission_state",
    "submit_batch",
    "synthetic_batch_results_jsonl",
    "targets_by_custom_id",
]


def scripted_batches(script: BatchScript | None = None, /, **fields: Any) -> dict[str, StoredBatch]:
    """Register `script` (else `BatchScript(**fields)`) as the test's batch script and return the batches
    the fake stores (the live mapping, by batch id, in creation order). A later call replaces the script for
    every later call of the API, stored batches included: a batch's item outcomes are decided, and then
    frozen, at its cancel or when it is first observed as ended (`jobhunter.llm.fake`)."""
    if script is not None and fields:
        raise ValueError("pass a BatchScript or its fields, not both")
    return fake.batches(BatchScript(**fields) if script is None else script).batches


def synthetic_batch_results_jsonl(
    items: Mapping[str, ItemOutcome],
    *,
    model: str = "claude-sonnet-5-5",
    body: Any = None,
    usage: dict[str, int] | None = None,
    max_tokens: int = 6000,
    message_id: str = "msg_0123456789synthetic",
) -> bytes:
    """The results JSONL of a batch whose items (by `custom_id`, in this order) end with these outcomes, each
    `succeeded*` one answering `body` (by default the C draft `generator_wire_body`) on `model` with `usage`,
    as the fake serves them: `succeeded:max_tokens` cut to half its text with `max_tokens` output tokens,
    `succeeded:refusal` without content, `succeeded:unexpected_stop` at `tool_use`, `succeeded:invalid` with
    text that is no JSON, the two `errored:*` with their provider error, and no line for a `missing` item."""
    answered = generator_wire_body() if body is None else body
    text = answered if isinstance(answered, str) else json.dumps(answered, ensure_ascii=False)
    lines: list[bytes] = []
    for custom_id, outcome in items.items():
        result: dict[str, Any]
        if outcome == "missing":
            continue
        if outcome in ("canceled", "expired"):
            result = {"type": outcome}
        elif outcome in ITEM_ERRORS:
            kind, message = ITEM_ERRORS[outcome]
            result = {
                "type": "errored",
                "error": {"type": "error", "error": {"type": kind, "message": message}},
            }
        else:
            result = {
                "type": "succeeded",
                "message": _message(outcome, text, model, usage, max_tokens, message_id),
            }
        line = {"custom_id": custom_id, "result": result}
        lines.append((json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
    return b"".join(lines)


def _message(
    outcome: ItemOutcome,
    text: str,
    model: str,
    usage: dict[str, int] | None,
    max_tokens: int,
    message_id: str,
) -> dict[str, Any]:
    common: dict[str, Any] = {"model": model, "usage": usage, "message_id": message_id}
    if outcome == "succeeded:max_tokens":
        cut = synthetic_message_json(text[: max(1, len(text) // 2)], stop_reason="max_tokens", **common)
        cut["usage"]["output_tokens"] = max_tokens
        return cut
    if outcome == "succeeded:refusal":
        details = {"type": "refusal", "category": fake.REFUSAL_CATEGORY, "explanation": None}
        return synthetic_message_json(None, stop_reason="refusal", stop_details=details, **common)
    if outcome == "succeeded:unexpected_stop":
        return synthetic_message_json(text, stop_reason=fake.UNEXPECTED_STOP_REASON, **common)
    if outcome == "succeeded:invalid":
        return synthetic_message_json(fake.PARSE_ERROR_TEXT, **common)
    return synthetic_message_json(text, **common)


# ---- M3 Task 16: the submission lifecycle ------------------------------------------------------------------


@dataclass(frozen=True)
class BulkTargets:
    """A bulk operation of the active score tuple with its pending targets and its `batch_submit` operation
    (`None` without one)."""

    llm_tuple_id: int
    bulk_operation_id: int
    targets: tuple[PendingTarget, ...]
    operation_id: int | None


_PENDING_IN_BULK: Final = sa.text(
    "INSERT INTO score_targets (job_revision_id, llm_tuple_id, job_id, state, bulk_operation_id, similarity) "
    "VALUES (:r, :tuple, :j, 'pending', :bulk, :similarity) RETURNING id"
)
_BUDGET: Final = sa.text("UPDATE bulk_operations SET budget_usd = :budget WHERE id = :b")


async def bulk_with_targets(
    engine: AsyncEngine, n: int, *, budget_usd: Decimal | None = None, operation: bool = True
) -> BulkTargets:
    """The bulk operation of the active score tuple with `n` pending targets (module notes)."""
    async with engine.begin() as conn:
        await activate_test_policy(conn)
        tuple_id = await activate_score_tuple(conn)
        settings = await active_settings(conn)
        bulk_id = await bulk.open(conn, llm_tuple_id=tuple_id, settings=settings, now=utcnow())
        if budget_usd is not None:
            await conn.execute(_BUDGET, {"budget": budget_usd, "b": bulk_id})
        made: list[PendingTarget] = []
        for i in range(n):
            job = await insert_score_job(conn, synthetic_ad())
            params = {
                "r": job.revision_id,
                "tuple": tuple_id,
                "j": job.job_id,
                "bulk": bulk_id,
                "similarity": round(0.9 - i / (10 * max(n, 1)), 6),
            }
            target = int((await conn.execute(_PENDING_IN_BULK, params)).scalar_one())
            made.append(PendingTarget(target, job.job_id, job.revision_id, tuple_id))
        operation_id = None
        if operation:
            operation_id, _ = await create_operation(
                conn,
                kind="batch_submit",
                payload={"bulk_operation_id": bulk_id, "llm_tuple_id": tuple_id},
                dedup_key=f"batch_submit:{bulk_id}:1",
                defer=None,
            )
    return BulkTargets(tuple_id, bulk_id, tuple(made), operation_id)


async def prepared_submission(
    engine: AsyncEngine,
    gateway: LLMGateway,
    setup: BulkTargets,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> tuple[batches.Prepared | batches.NotPrepared, OperationClaim]:
    """The setup's operation claimed and prepared (module notes)."""
    if setup.operation_id is None:
        raise ValueError("prepared_submission needs the setup's batch_submit operation")
    async with engine.begin() as conn:
        claim = await claim_operation(conn, setup.operation_id, epoch=1)
    assert claim is not None, "the batch_submit operation was not claimable"
    async with engine.begin() as conn:
        result = await batches.prepare(
            conn,
            bulk_operation_id=setup.bulk_operation_id,
            llm_tuple_id=setup.llm_tuple_id,
            operation=claim,
            settings=await active_settings(conn) if settings is None else settings,
            fake_llm=True,
            gateway=gateway,
            now=utcnow() if now is None else now,
        )
    return result, claim


_SUBMISSIONS: Final = sa.text("SELECT * FROM batch_submissions ORDER BY id")
_ITEMS: Final = sa.text("SELECT target_id, attempt_no FROM batch_items WHERE submission_id = :s ORDER BY id")
_BATCH_LEDGER: Final = sa.text(
    "SELECT l.state, l.evidence FROM llm_spend_ledger l JOIN llm_calls c ON c.spend_ledger_id = l.id "
    "WHERE c.transport = 'batch' ORDER BY l.id"
)
_BULK_ROW: Final = sa.text(
    "SELECT state, budget_usd, spent_usd, followup_held_usd FROM bulk_operations WHERE id = :b"
)
_CHARGED: Final = sa.text(
    "SELECT coalesce(sum(charged_attempts), 0) FROM score_targets WHERE llm_tuple_id = :t"
)
_MAKE_DUE: Final = sa.text("UPDATE operations SET not_before = NULL WHERE status = 'pending'")


async def submission_rows(engine: AsyncEngine) -> list[sa.Row[Any]]:
    """Every `batch_submissions` row, oldest first."""
    async with engine.connect() as conn:
        return list((await conn.execute(_SUBMISSIONS)).all())


async def items_of(engine: AsyncEngine, submission_id: int) -> list[tuple[int, int]]:
    """The submission's items as `(target_id, attempt_no)`, in id order."""
    async with engine.connect() as conn:
        rows = await conn.execute(_ITEMS, {"s": submission_id})
        return [(int(r.target_id), int(r.attempt_no)) for r in rows]


async def batch_ledger(engine: AsyncEngine) -> list[tuple[str, str | None]]:
    """The batch entries' `(state, evidence)`, oldest first."""
    async with engine.connect() as conn:
        return [(str(r.state), r.evidence) for r in await conn.execute(_BATCH_LEDGER)]


async def bulk_row(engine: AsyncEngine, bulk_operation_id: int) -> sa.Row[Any]:
    """The bulk operation's `state`, `budget_usd`, `spent_usd` and `followup_held_usd`."""
    async with engine.connect() as conn:
        return (await conn.execute(_BULK_ROW, {"b": bulk_operation_id})).one()


async def charged_attempts(engine: AsyncEngine, llm_tuple_id: int) -> int:
    """The tuple's targets' `charged_attempts`, summed."""
    async with engine.connect() as conn:
        return int((await conn.execute(_CHARGED, {"t": llm_tuple_id})).scalar_one())


async def make_due(engine: AsyncEngine) -> None:
    """Every `pending` operation due now: a requeue's `not_before` (one minute after a `bug`) cleared."""
    async with engine.begin() as conn:
        await conn.execute(_MAKE_DUE)


# ---- M3 Task 17: polling, the item decisions, the adoption and the release ---------------------------------

# A candidate C answer that publishes `accepted` for the synthetic ad, and one whose provisional score lands
# within T2's band (`M = 63.75`, `tests/integration/test_publish.py`): a follow-up.
PUBLISHED_BODY: Final = synthetic_c_draft()
FOLLOWUP_BODY: Final = synthetic_c_draft(levels={"role_fit": 2, "domain": 2, "education_certifications": 2})


@dataclass(frozen=True)
class SubmittedBatch:
    """A submission of `bulk_with_targets`'s targets created on the fake: its row, its provider batch and its
    items' `custom_id`s in the setup's target order."""

    setup: BulkTargets
    submission_id: int
    provider_id: str
    custom_ids: tuple[str, ...]

    def custom_id(self, target: PendingTarget) -> str:
        return self.custom_ids[self.setup.targets.index(target)]


async def submit_batch(
    engine: AsyncEngine,
    op_runner: Any,
    fake_llm: Any,
    n: int,
    *,
    followups: int = 0,
    bodies: Mapping[int, Any] | None = None,
    polls_until_ended: int = 1,
) -> SubmittedBatch:
    """`n` targets in a bulk operation, each answered (`fake_llm.answer`, its revision's key) by
    `PUBLISHED_BODY` (the first `followups` by `FOLLOWUP_BODY`; `bodies` by target index replace either), and
    the setup's `batch_submit` operation run once on the fake (`BatchScript(create="ok",
    polls_until_ended=…)`, not ended at the create by default, so a test registers the items' outcomes
    afterwards): the submitted batch."""
    setup = await bulk_with_targets(engine, n)
    for i, target in enumerate(setup.targets):
        body = FOLLOWUP_BODY if i < followups else PUBLISHED_BODY
        fake_llm.answer("generate", target.job_revision_id, (bodies or {}).get(i, body))
    fake.batches(BatchScript(create="ok", polls_until_ended=polls_until_ended))
    assert setup.operation_id is not None
    await op_runner.run_one(setup.operation_id)
    (row,) = await submission_rows(engine)
    assert row.state == "submitted", row.state
    attempts = dict(await items_of(engine, row.id))
    ids = tuple(f"t{t.target_id}_a{attempts[t.target_id]}" for t in setup.targets)
    return SubmittedBatch(setup, int(row.id), str(row.provider_batch_id), ids)


def batch_client(fake_llm: Any, http_client: httpx2.AsyncClient) -> BatchesClient:
    """A Batches client on the fake (`fake_llm.cfg`) over `http_client`."""
    return BatchesClient(fake_llm.cfg, http_client=http_client)


async def poll_once(
    engine: AsyncEngine, fake_llm: Any, *, epoch: int = 1, now: datetime | None = None
) -> batch_results.PollReport:
    """One `batch_poll` pass on the fake at `now`."""
    async with fake_llm.client() as http:
        return await batch_results.poll(
            engine, batch_client(fake_llm, http), now=now or utcnow(), epoch=epoch, defer=defer_in_tx
        )


async def reconcile_ambiguous_once(
    engine: AsyncEngine, fake_llm: Any, *, epoch: int = 1, now: datetime | None = None
) -> batch_adoption.ReconcileReport:
    """One `batch_reconcile_ambiguous` pass on the fake at `now`."""
    async with fake_llm.client() as http:
        return await batch_adoption.reconcile_ambiguous(
            engine, batch_client(fake_llm, http), now=now or utcnow(), epoch=epoch, defer=defer_in_tx
        )


_SUBMISSION_STATE: Final = sa.text("SELECT state FROM batch_submissions WHERE id = :s")
_DECISIONS: Final = sa.text("SELECT decision FROM batch_items WHERE decision IS NOT NULL ORDER BY id")
_ITEM_IDS: Final = sa.text("SELECT id, custom_id FROM batch_items")
_ITEM_ROWS: Final = sa.text(
    "SELECT custom_id, result_type, decision, stop_reason, processed_at, hold_released_at, hold_usd "
    "FROM batch_items ORDER BY id"
)
_BATCH_ENTRY_OF: Final = sa.text(
    "SELECT i.custom_id, l.state, l.evidence, l.reserved_usd, l.settled_usd, c.status, c.error_code, "
    "c.output_json IS NOT NULL AS has_output, c.http_status, a.cost_usd AS attempt_cost "
    "FROM batch_items i JOIN llm_calls c ON c.id = i.llm_call_id "
    "JOIN llm_spend_ledger l ON l.id = c.spend_ledger_id "
    "JOIN score_attempts a ON a.target_id = i.target_id AND a.attempt_no = i.attempt_no ORDER BY i.id"
)
_TARGET_BY_CUSTOM_ID: Final = sa.text(
    "SELECT i.custom_id, t.state, t.state_reason, t.lane, t.resume_draft_id, t.batch_submission_id, "
    "t.refusal_category, a.outcome, a.outcome_detail FROM batch_items i "
    "JOIN score_targets t ON t.id = i.target_id "
    "JOIN score_attempts a ON a.target_id = i.target_id AND a.attempt_no = i.attempt_no ORDER BY i.id"
)
_OPEN_ALERT_CODES: Final = sa.text("SELECT code FROM alerts WHERE resolved_at IS NULL ORDER BY code")
_ELIGIBLE_OF: Final = sa.text(
    f"SELECT t.id FROM score_targets t WHERE {batches.ELIGIBLE_SQL} "  # noqa: S608
    "AND t.id = ANY(CAST(:ids AS bigint[])) ORDER BY t.id"
)
_POLICY: Final = sa.text("SELECT evaluation_policy_id FROM active_config WHERE id = 1")


async def submission_state(engine: AsyncEngine, submission_id: int) -> str:
    async with engine.connect() as conn:
        return str((await conn.execute(_SUBMISSION_STATE, {"s": submission_id})).scalar_one())


async def decisions(engine: AsyncEngine) -> list[str]:
    """Every decided item's decision, in item order."""
    async with engine.connect() as conn:
        return [str(d) for d in (await conn.execute(_DECISIONS)).scalars()]


async def item_ids(engine: AsyncEngine) -> dict[str, int]:
    """Every item's id by `custom_id`."""
    async with engine.connect() as conn:
        return {str(r.custom_id): int(r.id) for r in await conn.execute(_ITEM_IDS)}


async def item_rows(engine: AsyncEngine) -> dict[str, sa.Row[Any]]:
    """Every item by `custom_id`: its result type, decision, stop reason, processing time and hold."""
    async with engine.connect() as conn:
        return {str(r.custom_id): r for r in await conn.execute(_ITEM_ROWS)}


async def entries_by_custom_id(engine: AsyncEngine) -> dict[str, sa.Row[Any]]:
    """Every item's ledger entry and call (state, evidence, amounts, the call's status and code, whether it
    stored an output) and its attempt's `cost_usd`, by `custom_id`."""
    async with engine.connect() as conn:
        return {str(r.custom_id): r for r in await conn.execute(_BATCH_ENTRY_OF)}


async def targets_by_custom_id(engine: AsyncEngine) -> dict[str, sa.Row[Any]]:
    """Every item's target (state, reason, lane, resume draft, batch pin, refusal category) and attempt
    (outcome, detail), by `custom_id`."""
    async with engine.connect() as conn:
        return {str(r.custom_id): r for r in await conn.execute(_TARGET_BY_CUSTOM_ID)}


async def open_alert_codes(engine: AsyncEngine) -> list[str]:
    """The codes of every open alert, sorted."""
    async with engine.connect() as conn:
        return [str(c) for c in (await conn.execute(_OPEN_ALERT_CODES)).scalars()]


async def eligible_for_batching(engine: AsyncEngine, setup: BulkTargets, target_ids: list[int]) -> list[int]:
    """The targets of `target_ids` that `ELIGIBLE_SQL` would batch for the setup's operation now."""
    async with engine.connect() as conn:
        policy = int((await conn.execute(_POLICY)).scalar_one())
        found = await conn.execute(
            _ELIGIBLE_OF, {"b": setup.bulk_operation_id, "policy": policy, "ids": target_ids}
        )
        return [int(t) for t in found.scalars()]
