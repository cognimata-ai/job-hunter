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

Every value is synthetic: ids, counts and the synthetic draft bodies of `tests.support.llm`.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.llm import bulk, fake
from jobhunter.llm.fake import ITEM_ERRORS, BatchScript, ItemOutcome, StoredBatch
from jobhunter.llm.gateway import LLMGateway
from jobhunter.scoring import batches
from jobhunter.settings.dryrun import active_settings
from jobhunter.settings.model import Settings
from jobhunter.worker.operations import OperationClaim, claim_operation, create_operation
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.llm import generator_wire_body, synthetic_message_json
from tests.support.scoring import PendingTarget, activate_score_tuple, activate_test_policy, insert_score_job
from tests.support.validation import synthetic_ad

__all__ = [
    "BatchScript",
    "BulkTargets",
    "ItemOutcome",
    "StoredBatch",
    "batch_ledger",
    "bulk_row",
    "bulk_with_targets",
    "charged_attempts",
    "items_of",
    "make_due",
    "prepared_submission",
    "scripted_batches",
    "submission_rows",
    "synthetic_batch_results_jsonl",
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
