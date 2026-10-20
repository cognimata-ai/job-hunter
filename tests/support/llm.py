"""LLM test helpers (Task 3; Tasks 4 and 5 extend it): spend owners, reservation arguments, a completed call's
response fields, the stored month rows, and the settings save of class `budget` that Task 17 will make.

Every row these helpers write holds ids, hashes and amounts only, never prompt or CV text.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.sql import q
from jobhunter.llm.budget import BudgetParams, BudgetRow, month_of
from jobhunter.llm.pricing import Usage
from jobhunter.llm.spend_ledger import (
    CallOutcome,
    EvalRunOwner,
    OperationOwner,
    SpendOwner,
    change_cap,
    ensure_month,
)
from jobhunter.types import JSONValue, get_object
from jobhunter.worker import operations as ops
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.m2 import insert_eval_run
from tests.support.pg import PgServer, pg_one

_ACTIVE_EXECUTION_CONFIG: Final = "SELECT execution_config_id FROM active_config WHERE id = 1"
_MONTH: Final = q(
    "SELECT month, cap_usd, overrun_buffer_usd, counted_usd, scoring_counted_usd, nonscoring_counted_usd, "
    "bucket_usd, bucket_at, cap_lowered_at, counted_at_cap_change_usd, cap_reached_alerted_at, updated_at "
    "FROM llm_budget WHERE month = :month"
)
_ACTIVE_PAYLOAD: Final = q(
    "SELECT v.payload FROM active_config a JOIN settings_versions v ON v.id = a.settings_version_id "
    "WHERE a.id = 1 FOR UPDATE OF a"
)
_NEXT_VERSION: Final = q(
    "INSERT INTO settings_versions (version_no, schema_version, payload, scope_version_id, model_policy_id, "
    "execution_config_id, evaluation_policy_id, effect_classes) "
    "SELECT (SELECT max(version_no) + 1 FROM settings_versions), v.schema_version, :payload, "
    "v.scope_version_id, v.model_policy_id, v.execution_config_id, v.evaluation_policy_id, '{budget}' "
    "FROM active_config a JOIN settings_versions v ON v.id = a.settings_version_id WHERE a.id = 1 "
    "RETURNING id",
    jsonb=["payload"],
)
_ACTIVATE: Final = q("UPDATE active_config SET settings_version_id = :v, updated_at = now() WHERE id = 1")

# The seeded execution config never changes within a session (`reset_database` keeps the seeded rows), so
# `reserve_kw` can name it without a database round trip; the owner fixtures read it.
_execution_config_id: int | None = None


def _seeded_execution_config(server: PgServer) -> int:
    global _execution_config_id
    if _execution_config_id is None:
        row = pg_one(server, "app", _ACTIVE_EXECUTION_CONFIG)
        assert row is not None
        _execution_config_id = int(str(row[0]))
    return _execution_config_id


async def new_operation_owner(
    engine: AsyncEngine, *, epoch: int = 1, key: str | None = None
) -> OperationOwner:
    """A claimed (`running`) `extract_profile` operation as a spend owner."""
    async with engine.begin() as conn:
        op_id, _ = await ops.create_operation(
            conn,
            kind="extract_profile",
            payload={"draft_id": 1},
            dedup_key=key or f"llm-test:{uuid.uuid4()}",
            defer=None,
        )
    return await claim_owner(engine, op_id, epoch=epoch)


async def claim_owner(engine: AsyncEngine, operation_id: int, *, epoch: int = 1) -> OperationOwner:
    """A new execution's claim of the `pending` operation `operation_id`, as a spend owner."""
    async with engine.begin() as conn:
        claim = await ops.claim_operation(conn, operation_id, epoch=epoch)
    assert claim is not None, f"operation {operation_id} is not claimable"
    return OperationOwner(claim)


async def reclaim(engine: AsyncEngine, owner: OperationOwner) -> None:
    """The `operations` reconciler's stale reclaim of `owner`'s operation: `claim_id := NULL`, back to
    `pending` (§3.3.3), so the old claim never holds again."""
    async with engine.begin() as conn:
        reclaimed = await ops.reclaim_stale_operations(
            conn, now=utcnow() + ops.OPERATION_STALE + timedelta(seconds=1)
        )
    assert owner.claim.operation_id in reclaimed


async def new_eval_owner(engine: AsyncEngine) -> EvalRunOwner:
    """A `running` schema dry-run `eval_runs` row as a spend owner."""
    async with engine.begin() as conn:
        return EvalRunOwner(await insert_eval_run(conn))


@pytest.fixture
async def op_owner(
    engine: AsyncEngine, current_epoch: int, migrated: PgServer
) -> AsyncIterator[OperationOwner]:
    """The spend owner of an operation claimed by the current worker epoch (§3.3.3 `operations` predicate)."""
    _seeded_execution_config(migrated)
    yield await new_operation_owner(engine, epoch=current_epoch)


@pytest.fixture
async def eval_owner(engine: AsyncEngine, clean_db: None, migrated: PgServer) -> AsyncIterator[EvalRunOwner]:
    """The spend owner of a `running` schema dry-run."""
    _seeded_execution_config(migrated)
    yield await new_eval_owner(engine)


def reserve_kw(
    owner: SpendOwner, *, now: datetime, bound: int, max_tokens: int, **overrides: Any
) -> dict[str, Any]:
    """The keyword arguments of `spend_ledger.reserve` for one try of `owner`: an `extract_profile` call
    (900 s) of an operation, a `schema_dry_run` call (300 s) of an eval run, on `claude-opus-5-5`, with the
    input bound `bound`; `overrides` replace any of them."""
    assert _execution_config_id is not None, "an owner fixture reads the seeded execution config first"
    if isinstance(owner, EvalRunOwner):
        purpose, deadline_s, eval_run_id = "schema_dry_run", 300, owner.eval_run_id
    else:
        purpose, deadline_s, eval_run_id = "extract_profile", 900, None
    kw: dict[str, Any] = {
        "owner": owner,
        "purpose": purpose,
        "purpose_class": "nonscoring",
        "model": "claude-opus-5-5",
        "effort": "medium",
        "max_tokens": max_tokens,
        "max_tokens_stage": "first",
        "transport_try": 1,
        "logical_call_no": 1,
        "deadline_s": deadline_s,
        "input_tokens_bound": bound,
        "native_pdf_extra": False,
        "execution_config_id": _execution_config_id,
        "prompt_template_version": "test-v1",
        "rendered_prompt_sha256": b"\x01" * 32,
        "prefix_sha256": None,
        "input_refs": {"profile_draft_id": None},
        "profile_draft_id": None,
        "eval_run_id": eval_run_id,
        "transport": "stream",
        "now": now,
    }
    kw.update(overrides)
    return kw


def scoring_kw(
    owner: SpendOwner, *, now: datetime, bound: int, max_tokens: int, **overrides: Any
) -> dict[str, Any]:
    """`reserve_kw` for a scoring `generate` call. M2 has no score attempts, so the attempt and target ids
    are made up (their FKs are M3's, plan Deviation 1): only the arithmetic of the scoring class is tested."""
    values: dict[str, Any] = {
        "purpose": "generate",
        "purpose_class": "scoring",
        "deadline_s": 600,
        "attempt_id": 1,
        "target_id": 1,
        **overrides,
    }
    return reserve_kw(owner, now=now, bound=bound, max_tokens=max_tokens, **values)


def completed_call(**overrides: Any) -> CallOutcome:
    """A completed streamed response of `claude-opus-5-5` with a synthetic structured output."""
    values: dict[str, Any] = {
        "status": "completed",
        "model_served": "claude-opus-5-5",
        "http_status": 200,
        "stop_reason": "end_turn",
        "provider_request_id": "req_test_0001",
        "output_json": {"synthetic": True},
        **overrides,
    }
    return CallOutcome(**values)


def error_call(http_status: int | None = 429, **overrides: Any) -> CallOutcome:
    """An error response without content (or, with `http_status=None`, a connect failure)."""
    return CallOutcome(status="error", http_status=http_status, error_code="test_error", **overrides)


USAGE: Final = Usage(1000, 200, 0, 0)  # 1000 x $4 + 200 x $20 per MTok = $0.008 on claude-opus-5-5


async def budget_rows(engine: AsyncEngine, *months: date) -> tuple[Any, ...]:
    """The stored `llm_budget` rows of `months` (no accrual) as `BudgetRow`s, `None` for a month without one.

    Typed `Any` so that a test reads the row of a month it knows exists without narrowing it first (the pinned
    Review Focus 4 tests)."""
    rows: list[BudgetRow | None] = []
    async with engine.connect() as conn:
        for month in months:
            row = (await conn.execute(_MONTH, {"month": month})).first()
            rows.append(None if row is None else BudgetRow(**row._mapping))
    return tuple(rows)


async def save_budget(engine: AsyncEngine, *, now: datetime, **llm: JSONValue) -> None:
    """A settings save of class `budget` as Task 17 makes it, committed: `active_config FOR UPDATE`, a new
    settings version with `llm.<key> = value` for each of `llm` (for example `cap_usd="10.00"`), activated,
    then the month of `now` ensured from the new values and its row changed under its lock."""
    async with engine.begin() as conn:
        payload = dict(get_object((await conn.execute(_ACTIVE_PAYLOAD)).scalar_one(), ""))
        payload["llm"] = {**get_object(payload, "llm"), **llm}
        version = int((await conn.execute(_NEXT_VERSION, {"payload": payload})).scalar_one())
        await conn.execute(_ACTIVATE, {"v": version})
        p = BudgetParams.from_payload(payload)
        await ensure_month(conn, month_of(now), p, now)
        await change_cap(
            conn, month=month_of(now), cap_usd=p.cap_usd, overrun_buffer_usd=p.overrun_buffer_usd, now=now
        )


@dataclass(frozen=True)
class LedgerRow:
    state: str
    reserved_usd: Decimal
    settled_usd: Decimal | None
    evidence: str | None
    budget_month: date
    admitted_cap_usd: Decimal
    counted_before_usd: Decimal
    call_status: str
    cost_usd: Decimal | None
    error_code: str | None
    input_refs: dict[str, JSONValue]


async def ledger_row(engine: AsyncEngine, ledger_id: int) -> LedgerRow:
    """A ledger row with its call's status, cost, error code and `input_refs`."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.text(
                    "SELECT l.state, l.reserved_usd, l.settled_usd, l.evidence, l.budget_month, "
                    "l.admitted_cap_usd, l.counted_before_usd, c.status AS call_status, c.cost_usd, "
                    "c.error_code, c.input_refs FROM llm_spend_ledger l "
                    "JOIN llm_calls c ON c.spend_ledger_id = l.id WHERE l.id = :id"
                ),
                {"id": ledger_id},
            )
        ).one()
    return LedgerRow(**row._mapping)


_COUNTS: Final = {
    "llm_spend_ledger": sa.text("SELECT count(*) FROM llm_spend_ledger"),
    "llm_calls": sa.text("SELECT count(*) FROM llm_calls"),
    "llm_budget": sa.text("SELECT count(*) FROM llm_budget"),
}


async def count_rows(engine: AsyncEngine, table: str) -> int:
    """The number of rows in `table` (`llm_spend_ledger`, `llm_calls` or `llm_budget`)."""
    async with engine.connect() as conn:
        return int((await conn.execute(_COUNTS[table])).scalar_one())


async def open_alerts(engine: AsyncEngine, code: str) -> list[tuple[str, str, int]]:
    """`(dedup_key, severity, occurrences)` of the open alerts with `code`."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            sa.text(
                "SELECT dedup_key, severity, occurrences FROM alerts "
                "WHERE code = :code AND resolved_at IS NULL ORDER BY id"
            ),
            {"code": code},
        )
        return [(r.dedup_key, r.severity, int(r.occurrences)) for r in rows]
