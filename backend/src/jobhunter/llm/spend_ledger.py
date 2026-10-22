"""The LLM spend ledger (spec §6.9.1-6.9.2, §7.11, §7.16.7, §10.7, §17.4): one `llm_spend_ledger` row and one
`llm_calls` row per HTTP try, reserved before the try under the serialised month row and counted until
positive evidence says it was not charged. Every function runs in the caller's transaction (`conn`).

**Lock order** (§3.3.3, §9.1): `active_config` (`current_params`, `FOR SHARE`) → the owner row (`FOR SHARE`
with its predicate; `FOR UPDATE` in recovery) → the `llm_budget` month row (`FOR UPDATE`) → ledger rows.
Every reservation, settlement, release, cap edit and accrual takes the month row; the dispatch marker takes
the owner row only (it changes no counter). So a caller that holds the owner row must not call `settle` or
`release_never_dispatched` afterwards in the same transaction: both read `active_config` first.

**Reservation** (`reserve`, in the §3.3.3 order): the params, the owner lock (and no active `global` purge
fence unless the owner is the purge operation itself, §20.2), the month row created from the current params if
it is new (`ensure_month`, the bucket full), the month row locked and accrued (`lock_month`), the worst case
(`pricing.worst_case_usd`, plus `bounds.native_pdf_extra_usd` of the locked row's buffer for a native PDF),
the §6.9.1 admission (`budget.admits`) or `BudgetExhausted`, the ledger row with `admitted_cap_usd` and
`counted_before_usd`, the `llm_calls` row (`reserved`, with the durable owner and the deadline in
`input_refs`), the counters and the bucket (scoring outside bulk operations only), and the `llm.cap_80pct`
check.

**The dispatch marker** (`mark_dispatched`) commits `reserved → dispatched` on both rows under the owner lock;
a lost owner is `FencedOut` and the caller must release the try (`release_never_dispatched`) without sending.

**Settlement** (`settle`) applies the §6.9.1 counter table under the month lock of the reservation's own
month, also after the month has ended. A cost above the reservation is charged in full with
`evidence='reservation_bound_violated'` and the critical `llm.reservation_bound_violated`. An entry the
reconciler already moved to `ambiguous` settles late through §7.16.7's `ambiguous → billed | not_billed` (plan
Deviation 24); one already `billed` or `not_billed` is left alone (`llm_settle_duplicate`). `settle` updates
no metric: it returns the figures (`SettleFigures`), and the gateway records them after its transaction has
committed (`record_settlement_metrics`), so a rolled-back settlement is never counted.

**Read-only views** (plan Task 16: the settings dry-run, `GET /api/system/spend`, system health): `read_month`
accrues the month row in memory from a plain `SELECT`, `read_month_or_new` also gives a month without a row
as `ensure_month` would create it, and `spend_totals` and `spend_by_model_effort_role` sum the month's ledger
rows. None of them locks or writes.

**Receipts** (`receipts`, plan Task 13, Deviation 26): the newest `completed` call of each logical call of an
operation, which a resumed or reclaimed execution reuses instead of paying again (§9.1).

**Reconcilers.** `llm_ledger_unsettled` (tick, plan Deviation 5) fails the `eval_runs` abandoned for more than
an hour, releases `reserved` entries older than 15 minutes whose recorded owner no longer holds, and marks
online `dispatched` entries `ambiguous` once older than `max(15 min, deadline_s + 5 min)` (plan Deviation 24),
one transaction per entry. `budget_counters` (heavy) recomputes the counters from the ledger with the §7.11
formula and corrects any drift under the month lock with the critical `llm.budget_counter_drift`. Both are
fenced by the worker epoch first and let the worker's own `FencedOut` propagate (M1 Ruling FR-w).

Nothing here logs or alerts with content: ids, states, amounts, model ids and codes only.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal

from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.db.transitions import CLEAR_REASON, transition
from jobhunter.errors import FencedOut, TransitionConflict
from jobhunter.llm import budget
from jobhunter.llm.bounds import native_pdf_extra_usd
from jobhunter.llm.budget import BudgetParams, BudgetRow, Purpose, PurposeClass, SettleOutcome
from jobhunter.llm.errors import BudgetExhausted
from jobhunter.llm.pricing import Effort, Model, Usage, settled_usd, worst_case_usd
from jobhunter.observability.alerts import open_alert, raise_alert, resolve_alert, resolve_alerts
from jobhunter.observability.logging import get_logger
from jobhunter.observability.metrics import (
    LLM_BUCKET_USD,
    LLM_HEADROOM_USD,
    LLM_RESERVATION_RATIO,
    LLM_SPEND_USD,
)
from jobhunter.types import JSONValue, get_int
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.operations import OperationClaim, lock_operation_owner
from jobhunter.worker.reconcilers import ReconcileContext, reconciler

CAP_80PCT_ALERT: Final = "llm.cap_80pct"
COUNTER_DRIFT_ALERT: Final = "llm.budget_counter_drift"
BOUND_VIOLATED_ALERT: Final = "llm.reservation_bound_violated"
CAP_REACHED_ALERT: Final = "llm.cap_reached"  # raised by M3's scoring denial (§6.9.4)

# §17.4: a `reserved` entry is recovered after this long; it is also the floor for a `dispatched` one.
RECOVERY_AFTER: Final = timedelta(minutes=15)
# Plan Deviation 24: a `dispatched` online entry waits for its recorded deadline plus this grace.
DEADLINE_GRACE: Final = timedelta(minutes=5)
# A `running` eval run older than this is abandoned (a CLI that died between cases or inside a call).
EVAL_RUN_ABANDONED: Final = timedelta(hours=1)
# The reconciler recovers at most this many entries per tick; the next tick takes the rest.
RECOVERY_BATCH: Final = 100

BOUND_VIOLATED: Final = "reservation_bound_violated"
NEVER_DISPATCHED: Final = "never_dispatched"
UNSETTLED_CODE: Final = "unsettled"
# Positive evidence of no charge a settlement may carry (§10.7; the ledger CHECK lists them). Online tries
# have their own; a batch item's come from its provider result (§6.9.1, M3).
ONLINE_REFUND_EVIDENCE: Final = frozenset({"error_response_no_content", "connect_failed_before_send"})
BATCH_REFUND_EVIDENCE: Final = frozenset({"batch_item_errored", "batch_item_canceled", "batch_item_expired"})
# The `input_refs` keys `reserve` writes itself.
_RESERVED_REFS: Final = frozenset({"owner", "deadline_s"})
_CAP_SUBJECT: Final = "budget"
_ZERO: Final = Decimal(0)

CallStatus = Literal["completed", "error", "ambiguous"]
_CALL_STATUS: Final[Mapping[SettleOutcome, frozenset[str]]] = {
    "billed": frozenset({"completed", "error"}),
    "not_billed": frozenset({"error"}),
    "ambiguous": frozenset({"ambiguous"}),
}

log = get_logger("jobhunter.llm.spend_ledger")


# ---- spend owners (§3.3.3) -------------------------------------------------------------------------------

_LOCK_EVAL_RUN = q("SELECT 1 FROM eval_runs WHERE id = :id AND status = 'running' FOR SHARE")
_GLOBAL_FENCE = q("SELECT EXISTS (SELECT 1 FROM purge_fences WHERE kind = 'global' AND released_at IS NULL)")
_OPERATION_OWNER = q("SELECT status, claim_id FROM operations WHERE id = :id FOR UPDATE")
_EVAL_RUN_OWNER = q("SELECT status FROM eval_runs WHERE id = :id FOR UPDATE")


@dataclass(frozen=True)
class OperationOwner:
    """A durable operation's execution (§3.3.3: `claim_id = :claim AND status = 'running'`)."""

    claim: OperationClaim

    async def lock(self, conn: AsyncConnection) -> None:
        """The `operations` row `FOR SHARE` with its predicate, else `FencedOut("operation")`."""
        await lock_operation_owner(conn, self.claim)

    def durable(self) -> dict[str, JSONValue]:
        """The owner as `llm_calls.input_refs["owner"]` records it, for `owner_holds`."""
        return {"kind": "operation", "id": self.claim.operation_id, "claim_id": str(self.claim.claim_id)}


@dataclass(frozen=True)
class EvalRunOwner:
    """An eval run (in M2 the schema dry-run, §10.11), owned while its `eval_runs` row is `running`."""

    eval_run_id: int

    async def lock(self, conn: AsyncConnection) -> None:
        """The `eval_runs` row `FOR SHARE` while `running`, else `FencedOut("eval_run")`."""
        if (await conn.execute(_LOCK_EVAL_RUN, {"id": self.eval_run_id})).first() is None:
            raise FencedOut("eval_run")

    def durable(self) -> dict[str, JSONValue]:
        return {"kind": "eval_run", "id": self.eval_run_id}


SpendOwner = OperationOwner | EvalRunOwner


async def _lock_owner(conn: AsyncConnection, owner: SpendOwner) -> None:
    """The owner lock, then no active `global` purge fence (§20.2) unless the owner is the purge itself."""
    await owner.lock(conn)
    exempt = isinstance(owner, OperationOwner) and owner.claim.kind == "purge"
    if not exempt and (await conn.execute(_GLOBAL_FENCE)).scalar_one():
        raise FencedOut("purge_fence")


async def owner_holds(conn: AsyncConnection, ref: Mapping[str, JSONValue], *, now: datetime) -> bool:
    """Lock the recorded owner's row `FOR UPDATE` and test its predicate: an operation that is `running` under
    the recorded `claim_id`, or a `running` eval run. A missing row never holds; an unknown owner kind is a
    `ValueError` (a writer this function does not know). `now` is the caller's clock (no M2 owner uses it)."""
    kind = ref.get("kind")
    if kind == "operation":
        row = (await conn.execute(_OPERATION_OWNER, {"id": get_int(dict(ref), "id")})).first()
        return (
            row is not None
            and row.status == "running"
            and row.claim_id is not None
            and str(row.claim_id) == ref.get("claim_id")
        )
    if kind == "eval_run":
        row = (await conn.execute(_EVAL_RUN_OWNER, {"id": get_int(dict(ref), "id")})).first()
        return row is not None and row.status == "running"
    raise ValueError(f"unknown spend owner kind {kind!r}")


# ---- the month row (§6.9.1) ------------------------------------------------------------------------------

_PARAMS = q(
    "SELECT v.payload FROM active_config a JOIN settings_versions v ON v.id = a.settings_version_id "
    "WHERE a.id = 1 FOR SHARE OF a"
)
_ENSURE_MONTH = q(
    "INSERT INTO llm_budget (month, cap_usd, overrun_buffer_usd, bucket_usd, bucket_at, updated_at) "
    "VALUES (:month, :cap_usd, :overrun_buffer_usd, :bucket_usd, :bucket_at, :updated_at) "
    "ON CONFLICT (month) DO NOTHING"
)
_LOCK_MONTH = q(
    "SELECT month, cap_usd, overrun_buffer_usd, counted_usd, scoring_counted_usd, nonscoring_counted_usd, "
    "bucket_usd, bucket_at, cap_lowered_at, counted_at_cap_change_usd, cap_reached_alerted_at, updated_at "
    "FROM llm_budget WHERE month = :month FOR UPDATE"
)
_READ_MONTH = q(
    "SELECT month, cap_usd, overrun_buffer_usd, counted_usd, scoring_counted_usd, nonscoring_counted_usd, "
    "bucket_usd, bucket_at, cap_lowered_at, counted_at_cap_change_usd, cap_reached_alerted_at, updated_at "
    "FROM llm_budget WHERE month = :month"
)
# Every column the ledger changes (`cap_reached_alerted_at` is M3's).
_SAVE_MONTH = q(
    "UPDATE llm_budget SET cap_usd = :cap_usd, overrun_buffer_usd = :overrun_buffer_usd, "
    "counted_usd = :counted_usd, scoring_counted_usd = :scoring_counted_usd, "
    "nonscoring_counted_usd = :nonscoring_counted_usd, bucket_usd = :bucket_usd, bucket_at = :bucket_at, "
    "cap_lowered_at = :cap_lowered_at, counted_at_cap_change_usd = :counted_at_cap_change_usd, "
    "updated_at = :updated_at WHERE month = :month"
)


def _budget_row(row: Row[tuple[object, ...]]) -> BudgetRow:
    return BudgetRow(**row._mapping)


async def current_params(conn: AsyncConnection) -> BudgetParams:
    """The budget params of the active settings, holding `active_config FOR SHARE` (§9.1: the first lock, so
    call it first in the transaction). It waits for a settings save in progress (`FOR UPDATE`)."""
    payload = (await conn.execute(_PARAMS)).scalar_one_or_none()
    if not isinstance(payload, dict):
        raise LookupError("active_config has no settings version")
    return BudgetParams.from_payload(payload)


async def ensure_month(conn: AsyncConnection, month: date, p: BudgetParams, now: datetime) -> None:
    """Create `month`'s row from `p` (cap, buffer, the bucket full at `now`) unless it exists."""
    row = budget.new_row(month, p, now)
    await conn.execute(_ENSURE_MONTH, _row_params(row))


def _row_params(row: BudgetRow) -> dict[str, object]:
    return {
        "month": row.month,
        "cap_usd": row.cap_usd,
        "overrun_buffer_usd": row.overrun_buffer_usd,
        "counted_usd": row.counted_usd,
        "scoring_counted_usd": row.scoring_counted_usd,
        "nonscoring_counted_usd": row.nonscoring_counted_usd,
        "bucket_usd": row.bucket_usd,
        "bucket_at": row.bucket_at,
        "cap_lowered_at": row.cap_lowered_at,
        "counted_at_cap_change_usd": row.counted_at_cap_change_usd,
        "updated_at": row.updated_at,
    }


async def _lock_row(conn: AsyncConnection, month: date) -> BudgetRow:
    """`month`'s row `FOR UPDATE`, as stored; `LookupError` without one."""
    row = (await conn.execute(_LOCK_MONTH, {"month": month})).first()
    if row is None:
        raise LookupError(f"no llm_budget row for {month.isoformat()}")
    return _budget_row(row)


async def _save(conn: AsyncConnection, row: BudgetRow, now: datetime) -> None:
    row.updated_at = now
    await conn.execute(_SAVE_MONTH, _row_params(row))


async def lock_month(conn: AsyncConnection, month: date, p: BudgetParams, *, now: datetime) -> BudgetRow:
    """`month`'s row `FOR UPDATE`, its bucket accrued to `now` and stored (§6.9.2). The accrual uses `p`'s
    reserve share, floor and interval with the row's own cap and buffer."""
    row = await _lock_row(conn, month)
    budget.accrue_row(row, p, now)
    await _save(conn, row, now)
    return row


async def read_month(
    conn: AsyncConnection, month: date, p: BudgetParams, *, now: datetime
) -> BudgetRow | None:
    """`lock_month` read-only: a plain `SELECT` (no row lock), the accrual done in memory and nothing stored;
    `None` when `month` has no row."""
    found = (await conn.execute(_READ_MONTH, {"month": month})).first()
    if found is None:
        return None
    row = _budget_row(found)
    budget.accrue_row(row, p, now)
    return row


async def read_month_or_new(
    conn: AsyncConnection, month: date, p: BudgetParams, *, now: datetime
) -> BudgetRow:
    """`read_month`, or, for a month without a row, the row `ensure_month` would create from `p` (its cap and
    buffer, nothing counted, the bucket full), in memory: nothing is locked or stored (the dry-run, the spend
    summary and system health)."""
    row = await read_month(conn, month, p, now=now)
    return budget.new_row(month, p, now) if row is None else row


# The month an open `llm.cap_80pct` was raised for (its context); the alert has one subject, so one open row.
_OPEN_CAP_ALERT_MONTH = q(
    "SELECT context ->> 'month' FROM alerts WHERE code = :code AND resolved_at IS NULL "
    "ORDER BY id DESC LIMIT 1"
)


async def _check_cap_share(conn: AsyncConnection, row: BudgetRow, *, raised: bool = False) -> None:
    """`llm.cap_80pct` (warn, §17.6) for `row`'s month: opened once its counted spend reaches 80% of the cap,
    and resolved only by what §17.6 names, a new month (the open alert was raised for an earlier month) or a
    raised cap (`raised`, from `change_cap`) that brings the share below 80%. A share that falls below 80%
    otherwise (in-flight worst cases settling, refunds) leaves it open: `counted_usd` moves around the
    threshold with every reservation and settlement, and resolving there would re-open it again and again."""
    open_for = (await conn.execute(_OPEN_CAP_ALERT_MONTH, {"code": CAP_80PCT_ALERT})).scalar_one_or_none()
    reached = budget.cap_share_reached(row)
    month = row.month.isoformat()
    if open_for is not None and (open_for < month or (raised and not reached and open_for == month)):
        await resolve_alert(conn, code=CAP_80PCT_ALERT, subject=_CAP_SUBJECT)
        open_for = None
    if reached and open_for is None:
        await open_alert(
            conn,
            code=CAP_80PCT_ALERT,
            severity="warn",
            subject=_CAP_SUBJECT,
            message="LLM spend reached 80% of the monthly cap",
            context={"month": month, "counted_usd": str(row.counted_usd), "cap_usd": str(row.cap_usd)},
        )


async def change_cap(
    conn: AsyncConnection, *, month: date, cap_usd: Decimal, overrun_buffer_usd: Decimal, now: datetime
) -> None:
    """A save of class `budget` (Task 17) changes `month`'s row under its lock (§6.9.1): raising the cap takes
    effect at once, lowering it below `counted_usd` records `cap_lowered_at` and `counted_at_cap_change_usd`.
    `llm.cap_80pct` opens when the new share reaches 80%, and a raised cap below 80% resolves it. The row
    must exist (`ensure_month` first)."""
    row = await _lock_row(conn, month)
    raised = cap_usd > row.cap_usd
    budget.change_cap(row, cap_usd, overrun_buffer_usd, now)
    await _save(conn, row, now)
    await _check_cap_share(conn, row, raised=raised)
    log.info(
        "llm_cap_changed",
        month=month.isoformat(),
        cap_usd=str(cap_usd),
        overrun_buffer_usd=str(overrun_buffer_usd),
        cap_lowered=row.cap_lowered_at == now,
    )


# ---- reservation and dispatch (§6.9.1, §10.7, §7.16.7) ----------------------------------------------------

_INSERT_ENTRY = q(
    "INSERT INTO llm_spend_ledger (budget_month, kind, purpose, eval_run, purpose_class, model_requested, "
    "state, reserved_usd, input_tokens_bound, batch, admitted_cap_usd, counted_before_usd, created_at) "
    "VALUES (:month, 'request', :purpose, :eval_run, :purpose_class, :model, 'reserved', :reserved_usd, "
    ":input_tokens_bound, :batch, :admitted_cap_usd, :counted_before_usd, :now) RETURNING id"
)
_INSERT_CALL = q(
    "INSERT INTO llm_calls (purpose, attempt_id, target_id, logical_call_no, max_tokens_stage, "
    "transport_try, profile_draft_id, eval_run_id, execution_config_id, model_requested, effort, max_tokens, "
    "transport, prompt_template_version, rendered_prompt_sha256, prefix_sha256, input_refs, "
    "input_tokens_bound, status, spend_ledger_id, started_at) VALUES (:purpose, :attempt_id, :target_id, "
    ":logical_call_no, :max_tokens_stage, :transport_try, :profile_draft_id, :eval_run_id, "
    ":execution_config_id, :model, :effort, :max_tokens, :transport, :prompt_template_version, "
    ":rendered_prompt_sha256, :prefix_sha256, :input_refs, :input_tokens_bound, 'reserved', :ledger_id, "
    ":now) RETURNING id",
    jsonb=["input_refs"],
)
_ENTRY = q(
    "SELECT l.id, l.budget_month, l.purpose, l.purpose_class, l.model_requested, l.state, l.reserved_usd, "
    "l.settled_usd, l.batch, l.bulk_operation_id, l.created_at, l.dispatched_at, c.id AS call_id, "
    "c.status AS call_status, c.input_refs FROM llm_spend_ledger l "
    "JOIN llm_calls c ON c.spend_ledger_id = l.id WHERE l.id = :id AND l.kind = 'request'"
)


@dataclass(frozen=True)
class Reservation:
    """One reserved try: its ledger row, its call row, the month it counts in and its worst case."""

    ledger_id: int
    llm_call_id: int
    month: date
    reserved_usd: Decimal
    purpose_class: PurposeClass
    model: Model
    batch: bool


@dataclass(frozen=True)
class _Entry:
    id: int
    month: date
    purpose: Purpose
    purpose_class: PurposeClass
    model: str
    state: str
    reserved_usd: Decimal
    settled_usd: Decimal | None
    batch: bool
    bulk_operation_id: int | None
    created_at: datetime
    dispatched_at: datetime | None
    call_id: int
    call_status: str
    input_refs: dict[str, JSONValue]

    @property
    def bucketed(self) -> bool:
        """Scoring outside bulk operations debits and credits the bucket (§6.9.2)."""
        return self.purpose_class == "scoring" and self.bulk_operation_id is None

    @property
    def unsettled_after(self) -> timedelta:
        """How long a `dispatched` online entry waits before it is `ambiguous` (plan Deviation 24)."""
        deadline_s = self.input_refs.get("deadline_s")
        if isinstance(deadline_s, bool) or not isinstance(deadline_s, int):
            return RECOVERY_AFTER
        return max(RECOVERY_AFTER, timedelta(seconds=deadline_s) + DEADLINE_GRACE)


async def _reserved_entry(conn: AsyncConnection, res: Reservation) -> _Entry:
    """The entry of `res`; a `LookupError` without one, a `ValueError` for a reservation that does not
    describe it (another call or month): nothing is changed on another month's counters."""
    entry = await _entry(conn, res.ledger_id)
    if entry is None:
        raise LookupError(f"no llm_spend_ledger request {res.ledger_id}")
    if (entry.call_id, entry.month) != (res.llm_call_id, res.month):
        raise ValueError(f"reservation {res.ledger_id} does not match its ledger row")
    return entry


async def _entry(conn: AsyncConnection, ledger_id: int) -> _Entry | None:
    row = (await conn.execute(_ENTRY, {"id": ledger_id})).first()
    if row is None:
        return None
    return _Entry(
        id=int(row.id),
        month=row.budget_month,
        purpose=row.purpose,
        purpose_class=row.purpose_class,
        model=row.model_requested,
        state=row.state,
        reserved_usd=row.reserved_usd,
        settled_usd=row.settled_usd,
        batch=bool(row.batch),
        bulk_operation_id=row.bulk_operation_id,
        created_at=row.created_at,
        dispatched_at=row.dispatched_at,
        call_id=int(row.call_id),
        call_status=row.call_status,
        input_refs=dict(row.input_refs),
    )


async def reserve(
    conn: AsyncConnection,
    *,
    owner: SpendOwner,
    purpose: Purpose,
    purpose_class: PurposeClass,
    model: Model,
    effort: Effort,
    max_tokens: int,
    max_tokens_stage: Literal["first", "enlarged"],
    transport_try: int,
    logical_call_no: int,
    deadline_s: int,
    input_tokens_bound: int,
    native_pdf_extra: bool,
    execution_config_id: int,
    prompt_template_version: str,
    rendered_prompt_sha256: bytes,
    prefix_sha256: bytes | None,
    input_refs: Mapping[str, JSONValue],
    profile_draft_id: int | None,
    eval_run_id: int | None,
    transport: Literal["stream", "batch"],
    now: datetime,
    attempt_id: int | None = None,
    target_id: int | None = None,
) -> Reservation:
    """Reserve one try in the caller's transaction (module docstring, steps 0-9), or raise.

    `FencedOut` (the owner lost its predicate, or a global purge fence is active) is raised before anything is
    written. `BudgetExhausted` (the §6.9.1 check refused the worst case) is raised before the ledger and call
    rows: only the month row's accrual is stored in the caller's transaction, which commits it or rolls it
    back. `attempt_id` and `target_id` are M3's score-call columns (their
    FKs are M3's, plan Deviation 1); no M2 caller passes them. An eval run's call (`eval_run_id`) is booked
    `eval_run`, which the ledger requires to be non-scoring.
    """
    if purpose not in budget.SCORING_PURPOSES and purpose_class != "nonscoring":
        raise ValueError(f"purpose {purpose} is non-scoring")
    if isinstance(owner, EvalRunOwner) and eval_run_id != owner.eval_run_id:
        raise ValueError("an eval run's call carries its own eval_run_id")
    if reserved_keys := _RESERVED_REFS & set(input_refs):
        raise ValueError(
            f"input_refs must not set {sorted(reserved_keys)}: the owner and deadline are reserve's"
        )
    p = await current_params(conn)  # 0. active_config FOR SHARE
    await _lock_owner(conn, owner)  # 1. the owner row FOR SHARE
    month = budget.month_of(now)
    await ensure_month(conn, month, p, now)  # 2. a new month from the current settings
    row = await lock_month(conn, month, p, now=now)  # 3. llm_budget FOR UPDATE, accrued
    batch = transport == "batch"
    # 4. the worst case; a native PDF's share of the buffer is the locked row's, never a carried value
    extra = native_pdf_extra_usd(row.overrun_buffer_usd) if native_pdf_extra else _ZERO
    r = worst_case_usd(
        model, input_tokens_bound=input_tokens_bound, max_tokens=max_tokens, batch=batch, extra_usd=extra
    )
    if not budget.admits(row, r, purpose_class, p):  # 5.
        room = budget.headroom(row, purpose_class, p)
        log.warning(
            "llm_budget_exhausted",
            month=month.isoformat(),
            purpose=purpose,
            purpose_class=purpose_class,
            reserved_usd=str(r),
            headroom_usd=str(room),
        )
        raise BudgetExhausted(purpose_class, room)
    ledger_id = (  # 6.
        await conn.execute(
            _INSERT_ENTRY,
            {
                "month": month,
                "purpose": purpose,
                "eval_run": eval_run_id is not None,
                "purpose_class": purpose_class,
                "model": model,
                "reserved_usd": r,
                "input_tokens_bound": input_tokens_bound,
                "batch": batch,
                "admitted_cap_usd": row.cap_usd,
                "counted_before_usd": row.counted_usd,
                "now": now,
            },
        )
    ).scalar_one()
    refs: dict[str, JSONValue] = {**input_refs, "owner": owner.durable(), "deadline_s": deadline_s}
    call_id = (  # 7.
        await conn.execute(
            _INSERT_CALL,
            {
                "purpose": purpose,
                "attempt_id": attempt_id,
                "target_id": target_id,
                "logical_call_no": logical_call_no,
                "max_tokens_stage": max_tokens_stage,
                "transport_try": transport_try,
                "profile_draft_id": profile_draft_id,
                "eval_run_id": eval_run_id,
                "execution_config_id": execution_config_id,
                "model": model,
                "effort": effort,
                "max_tokens": max_tokens,
                "transport": transport,
                "prompt_template_version": prompt_template_version,
                "rendered_prompt_sha256": rendered_prompt_sha256,
                "prefix_sha256": prefix_sha256,
                "input_refs": refs,
                "input_tokens_bound": input_tokens_bound,
                "ledger_id": ledger_id,
                "now": now,
            },
        )
    ).scalar_one()
    # 8. the counters, and the bucket for scoring outside bulk operations (M2 has no bulk operation)
    budget.apply_count(row, r, purpose_class, bucket=purpose_class == "scoring")
    await _save(conn, row, now)
    await _check_cap_share(conn, row)  # 9. opens at 80%; resolves only for a new month
    return Reservation(
        ledger_id=int(ledger_id),
        llm_call_id=int(call_id),
        month=month,
        reserved_usd=r,
        purpose_class=purpose_class,
        model=model,
        batch=batch,
    )


async def mark_dispatched(
    conn: AsyncConnection, res: Reservation, owner: SpendOwner, *, now: datetime
) -> None:
    """Commit the dispatch marker (§3.3.3) in the caller's transaction, immediately before the send: under the
    owner lock again, `reserved → dispatched` on the ledger row and on the call. Anything else (a lost
    predicate, an active global purge fence, another owner's try, a try released meanwhile) is `FencedOut`;
    the caller must not send and releases the try (`release_never_dispatched`). `now` is the caller's clock;
    `dispatched_at` is the database's, as for every changed-at column of `transition()`."""
    await _lock_owner(conn, owner)
    entry = await _entry(conn, res.ledger_id)
    if entry is None or entry.call_id != res.llm_call_id or entry.input_refs.get("owner") != owner.durable():
        raise FencedOut("llm_spend_ledger")
    try:
        await transition(conn, "llm_spend_ledger", res.ledger_id, ["reserved"], "dispatched")
        await transition(conn, "llm_calls", res.llm_call_id, ["reserved"], "dispatched")
    except TransitionConflict as exc:
        raise FencedOut("llm_spend_ledger") from exc


async def _release(conn: AsyncConnection, entry: _Entry, row: BudgetRow, *, now: datetime) -> bool:
    """`reserved → not_billed('never_dispatched')` and the call `cancelled`, the counters `-r`, on the locked
    month `row`; `False` (nothing written) when the entry was dispatched meanwhile."""
    try:
        await transition(
            conn,
            "llm_spend_ledger",
            entry.id,
            ["reserved"],
            "not_billed",
            reason=NEVER_DISPATCHED,
            set_={"settled_usd": _ZERO},
        )
    except TransitionConflict:
        return False
    await transition(
        conn,
        "llm_calls",
        entry.call_id,
        ["reserved"],
        "cancelled",
        set_={"error_code": NEVER_DISPATCHED, "ended_at": now, "cost_usd": _ZERO},
    )
    budget.apply_count(row, -entry.reserved_usd, entry.purpose_class, bucket=entry.bucketed)
    await _save(conn, row, now)
    return True


async def release_never_dispatched(conn: AsyncConnection, res: Reservation, *, now: datetime) -> None:
    """Release a try that was never dispatched (its dispatch marker was fenced), under the month lock:
    `not_billed('never_dispatched')`, the call `cancelled` and the counters adjusted. A try that is no longer
    `reserved` is left as it is, with nothing written (logged `llm_release_skipped`)."""
    p = await current_params(conn)
    row = await _lock_row(conn, res.month)
    entry = await _reserved_entry(conn, res)
    budget.accrue_row(row, p, now)  # stored with the counters by `_release`
    if entry.state != "reserved" or not await _release(conn, entry, row, now=now):
        log.info("llm_release_skipped", ledger_id=res.ledger_id, state=entry.state)
        return
    log.info("llm_try_released", ledger_id=res.ledger_id, reserved_usd=str(entry.reserved_usd))


# ---- settlement (§6.9.1 counter table, §10.7) -------------------------------------------------------------

_SPEND_BY_STATE = q(
    "SELECT COALESCE(SUM(settled_usd) FILTER (WHERE state = 'billed'), 0) AS billed, "
    "COALESCE(SUM(reserved_usd) FILTER (WHERE state = 'ambiguous'), 0) AS ambiguous, "
    "COALESCE(SUM(reserved_usd) FILTER (WHERE state IN ('reserved', 'dispatched')), 0) AS reserved "
    "FROM llm_spend_ledger WHERE budget_month = :month AND kind = 'request'"
)
# The month's tries per (model, effort, purpose), each at what it counts (§7.11): billed its cost, reserved,
# dispatched and ambiguous their reservation, not billed nothing. A try whose `llm_calls` row a purge deleted
# (§20.2) keeps its ledger row and is grouped with no effort.
_SPEND_BY_MODEL_EFFORT_ROLE = q(
    "SELECT l.model_requested AS model, c.effort, l.purpose AS role, count(*) AS calls, "
    "COALESCE(SUM(CASE WHEN l.state = 'billed' THEN l.settled_usd "
    "WHEN l.state IN ('reserved', 'dispatched', 'ambiguous') THEN l.reserved_usd ELSE 0 END), 0) AS counted "
    "FROM llm_spend_ledger l LEFT JOIN llm_calls c ON c.spend_ledger_id = l.id "
    "WHERE l.budget_month = :month AND l.kind = 'request' "
    "GROUP BY l.model_requested, c.effort, l.purpose ORDER BY l.model_requested, c.effort, l.purpose"
)


@dataclass(frozen=True)
class SpendTotals:
    """A month's tries by state (§17.5 `jobhunter_llm_spend_usd{state}`): `billed` at their cost, `ambiguous`
    and `reserved` (in flight: reserved or dispatched) at their reservation."""

    billed: Decimal
    ambiguous: Decimal
    reserved: Decimal


@dataclass(frozen=True)
class SpendGroup:
    """The tries of one (model, effort, role) in a month and what they count; `effort` is `None` for a try
    whose call row was purged."""

    model: str
    effort: str | None
    role: Purpose
    calls: int
    counted_usd: Decimal


async def spend_totals(conn: AsyncConnection, month: date) -> SpendTotals:
    """`month`'s ledger totals by state, one plain `SELECT` (no lock)."""
    row = (await conn.execute(_SPEND_BY_STATE, {"month": month})).one()
    return SpendTotals(billed=row.billed, ambiguous=row.ambiguous, reserved=row.reserved)


async def spend_by_model_effort_role(conn: AsyncConnection, month: date) -> list[SpendGroup]:
    """`month`'s tries per (model, effort, role), ordered by them, one plain `SELECT` (no lock)."""
    rows = await conn.execute(_SPEND_BY_MODEL_EFFORT_ROLE, {"month": month})
    return [
        SpendGroup(model=r.model, effort=r.effort, role=r.role, calls=int(r.calls), counted_usd=r.counted)
        for r in rows
    ]


@dataclass(frozen=True)
class CallOutcome:
    """The response fields of one try, written to its `llm_calls` row at settlement, and the call's status
    (`completed` or `error` for a billed try, `error` for a refunded one, `ambiguous` for a lost one)."""

    status: CallStatus
    model_served: str | None = None
    http_status: int | None = None
    error_code: str | None = None
    stop_reason: str | None = None
    refusal_category: str | None = None
    provider_request_id: str | None = None
    output_json: Mapping[str, JSONValue] | None = None
    first_event_at: datetime | None = None


@dataclass(frozen=True)
class SettleFigures:
    """The inputs of the §17.5 spend metrics after a settlement, of the reservation's month.

    `settled_usd` is what the try counts at now (billed: its cost; not billed: 0; ambiguous: its reservation);
    `spend_by_state` holds `counted`, `billed`, `ambiguous` and `reserved` (in flight); `headroom_usd` is the
    scoring headroom (§6.9.4)."""

    settled_usd: Decimal
    reserved_usd: Decimal
    purpose: Purpose
    spend_by_state: Mapping[str, Decimal]
    bucket_usd: Decimal
    headroom_usd: Decimal


def record_settlement_metrics(figures: SettleFigures) -> None:
    """Set the spend gauges and observe the reservation ratio; called once the settlement has committed."""
    for state, usd in figures.spend_by_state.items():
        LLM_SPEND_USD.labels(state=state).set(float(usd))
    LLM_BUCKET_USD.set(float(figures.bucket_usd))
    LLM_HEADROOM_USD.set(float(figures.headroom_usd))
    if figures.reserved_usd > 0:
        LLM_RESERVATION_RATIO.labels(purpose=figures.purpose).observe(
            float(figures.settled_usd / figures.reserved_usd)
        )


def _check_settlement(
    res: Reservation, outcome: SettleOutcome, evidence: str | None, usage: Usage | None, call: CallOutcome
) -> None:
    """Refuse an inconsistent settlement before anything is written."""
    if call.status not in _CALL_STATUS[outcome]:
        raise ValueError(f"a {outcome} try's call cannot be {call.status}")
    if outcome == "billed":
        if usage is None:
            raise ValueError("a billed settlement needs the usage")
        if evidence is not None:
            raise ValueError(
                "a billed settlement carries no evidence (a bound violation is recorded by settle)"
            )
    elif outcome == "not_billed":
        allowed = BATCH_REFUND_EVIDENCE if res.batch else ONLINE_REFUND_EVIDENCE
        if evidence not in allowed:
            raise ValueError(
                f"not_billed needs positive evidence of no charge ({', '.join(sorted(allowed))})"
            )
        if usage is not None:
            raise ValueError("a not_billed try has no usage")


async def _figures(
    conn: AsyncConnection, entry: _Entry, row: BudgetRow, p: BudgetParams, counted_at: Decimal
) -> SettleFigures:
    spend = await spend_totals(conn, entry.month)
    return SettleFigures(
        settled_usd=counted_at,
        reserved_usd=entry.reserved_usd,
        purpose=entry.purpose,
        spend_by_state={
            "counted": row.counted_usd,
            "billed": spend.billed,
            "ambiguous": spend.ambiguous,
            "reserved": spend.reserved,
        },
        bucket_usd=row.bucket_usd,
        headroom_usd=budget.headroom(row, "scoring", p),
    )


def _counted_at(entry: _Entry) -> Decimal:
    """What a settled entry counts at: billed its cost, not billed 0, ambiguous its reservation."""
    if entry.state == "ambiguous":
        return entry.reserved_usd
    return entry.settled_usd if entry.settled_usd is not None else _ZERO


async def settle(
    conn: AsyncConnection,
    res: Reservation,
    *,
    outcome: SettleOutcome,
    evidence: str | None,
    usage: Usage | None,
    call: CallOutcome,
    now: datetime,
) -> SettleFigures:
    """Settle a dispatched try (§10.7) in the caller's transaction, under the lock of its own month's row.

    - `billed` with its `usage`: `settled_usd` at the settlement price; a cost above the reservation is
      charged in full with `evidence='reservation_bound_violated'` and the critical
      `llm.reservation_bound_violated` (subject: the model);
    - `not_billed` with positive evidence of no charge (`ONLINE_REFUND_EVIDENCE`, or a batch item's);
    - `ambiguous`: it keeps its reservation (its call `ambiguous`, `cost_usd` the reservation).

    An entry the reconciler moved to `ambiguous` meanwhile settles late (`ambiguous → billed | not_billed`,
    its call `ambiguous → completed | error`, plan Deviation 24) with the same counter change. An entry
    already `billed` or `not_billed` (or `ambiguous` again) is left as it is and logged
    `llm_settle_duplicate`; a `reserved` one is a `ValueError` (release it). Returns the figures for the
    metrics (no metric here).
    """
    _check_settlement(res, outcome, evidence, usage, call)
    p = await current_params(conn)
    row = await _lock_row(conn, res.month)  # the month lock; the accrual is stored with the counters below
    entry = await _reserved_entry(conn, res)
    budget.accrue_row(row, p, now)
    if entry.state in ("billed", "not_billed") or (entry.state == "ambiguous" and outcome == "ambiguous"):
        log.warning("llm_settle_duplicate", ledger_id=entry.id, state=entry.state, outcome=outcome)
        return await _figures(conn, entry, row, p, _counted_at(entry))  # nothing written
    if entry.state not in ("dispatched", "ambiguous"):
        raise ValueError(f"llm_spend_ledger {entry.id} is {entry.state}: only a dispatched try settles")
    cost: Decimal | None = None
    violated = False
    if outcome == "billed":
        if usage is None:  # refused by `_check_settlement` already
            raise ValueError("a billed settlement needs the usage")
        cost = settled_usd(res.model, usage, batch=entry.batch)
        violated = cost > entry.reserved_usd
    elif outcome == "not_billed":
        cost = _ZERO
    entry_set: dict[str, object] = {} if cost is None else {"settled_usd": cost}
    # A billed row carries no evidence but a bound violation (a late settlement may find an ambiguous try's
    # code); the others carry the caller's.
    reason = (BOUND_VIOLATED if violated else CLEAR_REASON) if outcome == "billed" else evidence
    await transition(
        conn, "llm_spend_ledger", entry.id, [entry.state], outcome, reason=reason, set_=entry_set
    )
    await transition(
        conn,
        "llm_calls",
        entry.call_id,
        [entry.call_status],
        call.status,
        set_=_call_set(call, usage, cost, entry, now),
    )
    budget.apply_count(
        row,
        budget.settlement_delta(outcome, entry.reserved_usd, cost),
        entry.purpose_class,
        bucket=entry.bucketed,
    )
    await _save(conn, row, now)
    counted_at = entry.reserved_usd if cost is None else cost
    log.info(
        "llm_try_settled",
        ledger_id=entry.id,
        outcome=outcome,
        late=entry.state == "ambiguous",
        reserved_usd=str(entry.reserved_usd),
        settled_usd=str(counted_at),
    )
    if violated:
        await _raise_bound_violated(conn, entry, counted_at)
    return await _figures(conn, entry, row, p, counted_at)


def _call_set(
    call: CallOutcome, usage: Usage | None, cost: Decimal | None, entry: _Entry, now: datetime
) -> dict[str, object]:
    values: dict[str, object] = {
        "model_served": call.model_served,
        "http_status": call.http_status,
        "error_code": call.error_code,
        "stop_reason": call.stop_reason,
        "refusal_category": call.refusal_category,
        "provider_request_id": call.provider_request_id,
        "output_json": None if call.output_json is None else dict(call.output_json),
        "first_event_at": call.first_event_at,
        "ended_at": now,
        "cost_usd": entry.reserved_usd if cost is None else cost,
    }
    if usage is not None:
        values.update(
            input_tokens=usage.input_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens,
            cache_read_input_tokens=usage.cache_read_input_tokens,
            output_tokens=usage.output_tokens,
        )
    return values


async def _raise_bound_violated(conn: AsyncConnection, entry: _Entry, cost: Decimal) -> None:
    log.error(
        "llm_reservation_bound_violated",
        ledger_id=entry.id,
        purpose=entry.purpose,
        model=entry.model,
        reserved_usd=str(entry.reserved_usd),
        settled_usd=str(cost),
    )
    await raise_alert(
        conn,
        code=BOUND_VIOLATED_ALERT,
        severity="critical",
        subject=f"model:{entry.model}",
        message="a settled LLM cost exceeded its reservation; the score lane stays paused until acknowledged",
        context={
            "ledger_id": entry.id,
            "purpose": entry.purpose,
            "model": entry.model,
            "reserved_usd": str(entry.reserved_usd),
            "settled_usd": str(cost),
        },
    )


# ---- receipts of an operation's calls (§9.1; plan Task 13, Deviation 26) ----------------------------------

# The newest `completed` call per `logical_call_no` of one operation and purpose. The operation is matched by
# the id `OperationOwner.durable()` writes into `input_refs["owner"]`, never by a second key: by containment,
# the kind and the id as a JSON number, so no cast ever runs on another owner kind's id.
_RECEIPTS = q(
    "SELECT DISTINCT ON (logical_call_no) logical_call_no, id, output_json, error_code FROM llm_calls "
    "WHERE input_refs -> 'owner' @> "
    "jsonb_build_object('kind', 'operation', 'id', CAST(:operation_id AS bigint)) "
    "AND purpose = :purpose AND status = 'completed' AND logical_call_no IS NOT NULL "
    "ORDER BY logical_call_no, id DESC"
)


@dataclass(frozen=True)
class Receipt:
    """The newest `completed` call of one logical call: its parsed output (`output_json`), or the code of why
    it has none (`error_code`: `invalid_output`, `refused`, `max_tokens_first_stage`, `max_tokens`,
    `unexpected_stop_reason`, `model_mismatch`)."""

    llm_call_id: int
    output_json: JSONValue | None
    error_code: str | None


async def receipts(conn: AsyncConnection, *, operation_id: int, purpose: Purpose) -> dict[int, Receipt]:
    """The receipts of operation `operation_id`'s `purpose` calls, by `logical_call_no` (§9.1): the newest
    `completed` call of each. A node reads them under its owner lock, before it pays for a call again. Calls
    that did not complete (an error response, a lost stream, a released try) are no receipts."""
    rows = await conn.execute(_RECEIPTS, {"operation_id": operation_id, "purpose": purpose})
    return {
        int(row.logical_call_no): Receipt(
            llm_call_id=int(row.id), output_json=row.output_json, error_code=row.error_code
        )
        for row in rows
    }


# ---- the counters recomputed from the ledger (§7.11, §6.9.1) ----------------------------------------------

# §7.11: counted(month) = Σ settled_usd (billed) + Σ reserved_usd (reserved, dispatched, ambiguous)
# + Σ adjustments. An adjustment row (no M2 code writes one) counts its `reserved_usd`, its one mandatory
# amount. `CAST(:month AS date) IS NULL` selects every month.
_COUNTERS = q(
    "WITH amounts AS (SELECT budget_month, purpose_class, CASE WHEN kind = 'adjustment' THEN reserved_usd "
    "WHEN state = 'billed' THEN settled_usd WHEN state IN ('reserved', 'dispatched', 'ambiguous') "
    "THEN reserved_usd ELSE 0 END AS amount FROM llm_spend_ledger "
    "WHERE CAST(:month AS date) IS NULL OR budget_month = CAST(:month AS date)), "
    "sums AS (SELECT budget_month, SUM(amount) AS counted, "
    "SUM(amount) FILTER (WHERE purpose_class = 'scoring') AS scoring, "
    "SUM(amount) FILTER (WHERE purpose_class = 'nonscoring') AS nonscoring FROM amounts "
    "GROUP BY budget_month) "
    "SELECT b.month, b.counted_usd, b.scoring_counted_usd, b.nonscoring_counted_usd, "
    "COALESCE(s.counted, 0) AS ledger_counted, COALESCE(s.scoring, 0) AS ledger_scoring, "
    "COALESCE(s.nonscoring, 0) AS ledger_nonscoring FROM llm_budget b "
    "LEFT JOIN sums s ON s.budget_month = b.month "
    "WHERE CAST(:month AS date) IS NULL OR b.month = CAST(:month AS date) ORDER BY b.month"
)
_CORRECT = q(
    "UPDATE llm_budget SET counted_usd = :counted, scoring_counted_usd = :scoring, "
    "nonscoring_counted_usd = :nonscoring, updated_at = :now WHERE month = :month"
)


@dataclass(frozen=True)
class Counters:
    counted_usd: Decimal
    scoring_counted_usd: Decimal
    nonscoring_counted_usd: Decimal


@dataclass(frozen=True)
class Drift:
    """A month whose stored counters differ from the ledger's."""

    month: date
    stored: Counters
    ledger: Counters


def _drift(row: Row[tuple[object, ...]]) -> Drift | None:
    stored = Counters(row.counted_usd, row.scoring_counted_usd, row.nonscoring_counted_usd)
    ledger = Counters(row.ledger_counted, row.ledger_scoring, row.ledger_nonscoring)
    return None if stored == ledger else Drift(row.month, stored, ledger)


async def recompute(conn: AsyncConnection, month: date) -> Drift | None:
    """`month`'s counters recomputed from the ledger (the §7.11 formula), one statement: the `Drift` when they
    differ from the stored ones, `None` when they agree or the month has no row. It locks nothing; under the
    month lock (the heavy reconciler) its answer is final."""
    row = (await conn.execute(_COUNTERS, {"month": month})).first()
    return None if row is None else _drift(row)


async def _drifting_months(conn: AsyncConnection) -> list[date]:
    return [
        d.month for row in await conn.execute(_COUNTERS, {"month": None}) if (d := _drift(row)) is not None
    ]


@reconciler("budget_counters", "heavy")
async def reconcile_budget_counters(ctx: ReconcileContext) -> int:
    """§17.4 `budget_counters`: every month whose counters differ from the ledger is corrected from it under
    its row lock, in a transaction of its own, with the critical `llm.budget_counter_drift` (subject: the
    month); a pass that corrects nothing resolves the alert. Returns the months corrected. Fenced by the
    worker epoch first: the worker's own `FencedOut` propagates (Ruling FR-w), another owner's ends the pass
    with the count so far."""
    corrected = 0
    try:
        async with ctx.engine.begin() as conn:
            await fence_epoch(conn, ctx.epoch)
            months = await _drifting_months(conn)
        for month in months:
            async with ctx.engine.begin() as conn:
                await fence_epoch(conn, ctx.epoch)
                corrected += await _correct(conn, month, now=ctx.now)
        if corrected == 0:
            async with ctx.engine.begin() as conn:
                await fence_epoch(conn, ctx.epoch)
                await resolve_alerts(conn, code=COUNTER_DRIFT_ALERT)
    except FencedOut as fenced_out:
        if fenced_out.worker_superseded:
            raise  # `fence_epoch` logged it
        return corrected
    return corrected


async def _correct(conn: AsyncConnection, month: date, *, now: datetime) -> int:
    await _lock_row(conn, month)
    found = await recompute(conn, month)  # again, under the month lock
    if found is None:
        return 0
    ledger = found.ledger
    params = {
        "month": month,
        "counted": ledger.counted_usd,
        "scoring": ledger.scoring_counted_usd,
        "nonscoring": ledger.nonscoring_counted_usd,
        "now": now,
    }
    await conn.execute(_CORRECT, params)
    context: dict[str, JSONValue] = {
        "month": month.isoformat(),
        "stored_counted_usd": str(found.stored.counted_usd),
        "ledger_counted_usd": str(ledger.counted_usd),
    }
    log.error("llm_budget_counter_drift", **context)
    await raise_alert(
        conn,
        code=COUNTER_DRIFT_ALERT,
        severity="critical",
        subject=f"month:{month.isoformat()}",
        message="the LLM budget counters differed from the spend ledger and were corrected from it",
        context=context,
    )
    return 1


# ---- unsettled entries (§17.4 `ledger_unsettled`, plan Deviations 5 and 24) --------------------------------

_ABANDONED_EVAL_RUNS = q(
    "SELECT id FROM eval_runs WHERE status = 'running' AND started_at < :cutoff ORDER BY id"
)
# The candidates (`llm_spend_unsettled` serves the scan): `reserved` entries older than 15 minutes whose
# recorded owner does not hold (checked again under its row lock), and online `dispatched` entries past
# `max(15 min, deadline_s + 5 min)`. An owner of an unknown kind is a candidate, so `owner_holds` fails
# loudly instead of leaving it unsettled.
_UNSETTLED = q(
    "SELECT l.id FROM llm_spend_ledger l JOIN llm_calls c ON c.spend_ledger_id = l.id "
    "WHERE l.state IN ('reserved', 'dispatched') AND ("
    "(l.state = 'reserved' AND l.created_at < :reserved_cutoff AND NOT COALESCE("
    "(c.input_refs -> 'owner' ->> 'kind' = 'operation' AND EXISTS (SELECT 1 FROM operations o "
    "WHERE o.id = (c.input_refs -> 'owner' ->> 'id')::bigint AND o.status = 'running' "
    "AND o.claim_id::text = c.input_refs -> 'owner' ->> 'claim_id')) "
    "OR (c.input_refs -> 'owner' ->> 'kind' = 'eval_run' AND EXISTS (SELECT 1 FROM eval_runs e "
    "WHERE e.id = (c.input_refs -> 'owner' ->> 'id')::bigint AND e.status = 'running')), false)) "
    "OR (l.state = 'dispatched' AND NOT l.batch AND l.dispatched_at + GREATEST(:floor, "
    "make_interval(secs => (c.input_refs ->> 'deadline_s')::double precision) + :grace) < :now)"
    ") ORDER BY l.id LIMIT :limit"
)


async def _fail_abandoned_eval_runs(conn: AsyncConnection, *, now: datetime) -> int:
    """Every eval run `running` for longer than `EVAL_RUN_ABANDONED` → `failed`, so that its unsettled
    entries fail `owner_holds`."""
    ids = [
        int(i)
        for i in (await conn.execute(_ABANDONED_EVAL_RUNS, {"cutoff": now - EVAL_RUN_ABANDONED})).scalars()
    ]
    failed = 0
    for eval_run_id in ids:
        try:
            await transition(conn, "eval_runs", eval_run_id, ["running"], "failed")
        except TransitionConflict:
            continue  # it finished meanwhile
        log.warning("eval_run_abandoned", eval_run_id=eval_run_id)
        failed += 1
    return failed


async def _mark_ambiguous(conn: AsyncConnection, entry: _Entry, *, now: datetime) -> None:
    """`dispatched → ambiguous` on both rows; the entry keeps its reservation, so no counter changes."""
    await transition(conn, "llm_spend_ledger", entry.id, ["dispatched"], "ambiguous")
    await transition(
        conn,
        "llm_calls",
        entry.call_id,
        ["dispatched"],
        "ambiguous",
        set_={"error_code": UNSETTLED_CODE, "ended_at": now, "cost_usd": entry.reserved_usd},
    )


async def _recover(conn: AsyncConnection, ledger_id: int, *, now: datetime) -> int:
    """One candidate in the §9.1 order: `active_config`, the owner row `FOR UPDATE` (a `reserved` entry
    only), the month row, the ledger row. 1 when it was released or marked `ambiguous`."""
    p = await current_params(conn)
    first = await _entry(conn, ledger_id)
    if first is None or first.state not in ("reserved", "dispatched"):
        return 0
    holds = first.state == "reserved" and await owner_holds(conn, _owner_ref(first), now=now)
    row = await lock_month(conn, first.month, p, now=now)
    entry = await _entry(conn, ledger_id)  # again, under the owner's and the month's locks
    if entry is None:
        return 0
    if entry.state == "reserved" and first.state == "reserved" and not holds:
        if entry.created_at < now - RECOVERY_AFTER and await _release(conn, entry, row, now=now):
            log.info("llm_entry_released", ledger_id=ledger_id, reserved_usd=str(entry.reserved_usd))
            return 1
    elif entry.state == "dispatched" and not entry.batch and entry.dispatched_at is not None:
        if entry.dispatched_at + entry.unsettled_after < now:
            await _mark_ambiguous(conn, entry, now=now)
            log.warning("llm_entry_ambiguous", ledger_id=ledger_id, reserved_usd=str(entry.reserved_usd))
            return 1
    return 0


def _owner_ref(entry: _Entry) -> Mapping[str, JSONValue]:
    ref = entry.input_refs.get("owner")
    if not isinstance(ref, dict):
        raise ValueError(f"llm_calls {entry.call_id} records no owner")
    return ref


@reconciler("llm_ledger_unsettled", "tick")
async def reconcile_llm_ledger_unsettled(ctx: ReconcileContext) -> int:
    """§17.4 `ledger_unsettled`, the LLM half (plan Deviation 5): first every abandoned eval run fails; then,
    one transaction per entry, a `reserved` entry older than 15 minutes whose recorded owner no longer holds
    becomes `not_billed('never_dispatched')` (its call `cancelled`, the counters adjusted), and an online
    `dispatched` entry older than `max(15 min, deadline_s + 5 min)` becomes `ambiguous` with its call (plan
    Deviation 24). Returns the eval runs failed plus the entries recovered. Fenced by the worker epoch first:
    the worker's own `FencedOut` propagates (Ruling FR-w), another owner's ends the pass with the count so
    far.
    """
    repaired = 0
    try:
        async with ctx.engine.begin() as conn:
            await fence_epoch(conn, ctx.epoch)
            repaired += await _fail_abandoned_eval_runs(conn, now=ctx.now)
        async with ctx.engine.connect() as conn:
            params = {
                "reserved_cutoff": ctx.now - RECOVERY_AFTER,
                "floor": RECOVERY_AFTER,
                "grace": DEADLINE_GRACE,
                "now": ctx.now,
                "limit": RECOVERY_BATCH,
            }
            ids = [int(i) for i in (await conn.execute(_UNSETTLED, params)).scalars()]
        for ledger_id in ids:
            async with ctx.engine.begin() as conn:
                await fence_epoch(conn, ctx.epoch)
                repaired += await _recover(conn, ledger_id, now=ctx.now)
    except FencedOut as fenced_out:
        if fenced_out.worker_superseded:
            raise  # `fence_epoch` logged it
        return repaired
    return repaired
