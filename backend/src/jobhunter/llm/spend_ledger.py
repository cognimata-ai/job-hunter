"""The LLM spend ledger (spec §6.9.1-6.9.4, §7.11, §7.16.7, §9.4.2, §10.7, §17.4): one `llm_spend_ledger` row
and one `llm_calls` row per HTTP try, reserved before the try under the serialised month row and counted
until positive evidence says it was not charged. Every function runs in the caller's transaction (`conn`).

**Lock order** (§3.3.3, §9.1): `active_config` (`current_params`, `FOR SHARE`) → the owner row (`FOR SHARE`
with its predicate; in recovery `FOR UPDATE`, or `FOR NO KEY UPDATE` for the M3 owners) → the `llm_budget`
month row (`FOR UPDATE`) → the `bulk_operations` row of a funded entry (`FOR NO KEY UPDATE`) → ledger rows →
other domain rows (`score_attempts`, and `score_targets` not as the owner). Every reservation, settlement,
release, cap or buffer edit and accrual takes the month row; the dispatch marker takes the owner row and then
moves the attempt's counters. So a caller that holds the owner row must not call `settle` or
`release_never_dispatched` afterwards in the same transaction unless it already holds `active_config`: both
read `active_config` first (every M3 caller holds it, its step 2).

**Spend owners** (§3.3.3): an operation's execution (`OperationOwner`), an eval run (`EvalRunOwner`, under
its operation's claim when an `eval_run` operation executes it), a score attempt (`TargetAttemptOwner`: its
target under `claim_id`, `attempt_no` and `state = 'in_progress'`, else `ClaimLost`), and a batch submission
(`BatchSubmissionOwner`: its row in the expected state). Each records itself in `llm_calls.input_refs`
(`durable()`), which `owner_holds` and the reconciler test again.

**Reservation** (`reserve`, in the §3.3.3 order): the params, the owner lock (and no active `global` purge
fence unless the owner is the purge operation itself, §20.2), the month row created from the current params if
it is new (`ensure_month`, the bucket full), the month row locked and accrued (`lock_month`), the worst case
(`pricing.worst_case_usd` at batch prices for a batch item, plus `bounds.native_pdf_extra_usd` of the locked
row's buffer for a native PDF), the §6.9.1 admission (`budget.admits`) or `BudgetExhausted`, the bulk row of a
funded reservation, the ledger row with `admitted_cap_usd`, `admitted_buffer_usd` and `counted_before_usd`,
the `llm_calls` row (`reserved`, with the durable owner and, for an online try, the deadline in
`input_refs`), the counters and the pace account, and the `llm.cap_80pct` check. The pace account (§6.9.2,
§6.9.3): a scoring reservation that names an `active` bulk operation and fits its allocation (`spent_usd + r
≤ budget_usd`) debits `spent_usd` and the entry keeps the id; any other scoring reservation (one that would
exceed the allocation, or names a finished operation) is stored without it and debits the bucket;
non-scoring touches neither.

**A batch** (`reserve_batch`, M3 Task 16) reserves every item of a submission at once, under one owner lock,
one month lock and its bulk row, after the allocation check of L1189 under those locks; each item is
admitted against the hard cap as any other reservation.

**The dispatch marker** (`mark_dispatched`) commits `reserved → dispatched` on both rows under the owner lock;
for a score attempt it also counts the try (`http_calls`, `logical_calls` on a logical call's first try) and
charges the attempt and, the first time, its target (§9.4.2, L2863). `mark_batch_dispatched` does the same for
every item of a submission at `submit_started` (R6, plan Deviation 13), its target rows locked before its
attempt rows (Ruling T16-c: the order of a cancel, `targets._cancel`). A lost owner is `FencedOut` and the
caller must release the try (`release_never_dispatched`) without sending.

**Settlement** (`settle`) applies the §6.9.1 counter table under the month lock of the reservation's own
month, also after the month has ended, to the bucket or to the allocation the entry debited. A cost above the
reservation is charged in full with `evidence='reservation_bound_violated'` and the critical
`llm.reservation_bound_violated`, even when it lifts `spent_usd` above `budget_usd`. An entry the reconciler
already moved to `ambiguous` settles late through §7.16.7's `ambiguous → billed | not_billed` (plan Deviation
24); one already `billed` or `not_billed` is left alone (`llm_settle_duplicate`). A score attempt's
`cost_usd` follows each try at what it counts (billed its cost, ambiguous its reservation, L3911). The
provider metadata it writes is made storable first (`storable_outcome`). `settle` updates no metric: it
returns the figures (`SettleFigures`), and the gateway records them after its transaction has committed
(`record_settlement_metrics`), so a rolled-back settlement is never counted.

**The cap reached** (§6.9.4): `raise_cap_reached` raises `llm.cap_reached` once per month (the month row's
`cap_reached_alerted_at`, never cleared); `resolve_cap_reached` and `resolve_stale_cap_alerts` resolve it.

**Read-only views** (plan Task 16: the settings dry-run, `GET /api/system/spend`, system health): `read_month`
accrues the month row in memory from a plain `SELECT`, `read_month_or_new` also gives a month without a row
as `ensure_month` would create it, and `spend_totals` and `spend_by_model_effort_role` sum the month's ledger
rows. None of them locks or writes.

**Receipts** (`receipts`, plan Task 13, Deviation 26): the newest `completed` call of each logical call of an
operation, which a resumed or reclaimed execution reuses instead of paying again (§9.1);
`last_call_of_attempt` is a score attempt's newest call of one purpose. Both carry the try's `max_tokens`.

**Reconcilers.** `llm_ledger_unsettled` (tick, plan Deviation 5) fails the abandoned `eval_runs` (no live
`eval_run` operation and, for a run that never had one, more than an hour old; M3 Task 19), releases
`reserved` entries older than 15 minutes whose recorded owner no longer holds, and marks online `dispatched`
entries `ambiguous` once older than `max(15 min, deadline_s + 5 min)` (plan Deviation 24), one transaction per
entry. A batch item's entry is never aged (L2979): its submission's processing settles
it. `budget_counters` (heavy) recomputes the counters from the ledger with the §7.11 formula and corrects any
drift under the month lock with the critical `llm.budget_counter_drift`, and each active bulk operation's
`spent_usd` from the entries it funded under the bulk row's lock (M3 Task 18). Both are fenced by the worker
epoch first and let the worker's own `FencedOut` propagate (M1 Ruling FR-w).

Nothing here logs or alerts with content: ids, states, amounts, model ids and codes only.
"""

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal
from uuid import UUID

from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.guards import global_fence_active
from jobhunter.db.sql import q
from jobhunter.db.transitions import CLEAR_REASON, transition, transition_where
from jobhunter.errors import FencedOut, TransitionConflict
from jobhunter.llm import budget
from jobhunter.llm.bounds import native_pdf_extra_usd
from jobhunter.llm.budget import BudgetParams, BudgetRow, Purpose, PurposeClass, SettleOutcome
from jobhunter.llm.errors import BudgetExhausted, BulkAllocationExceeded, ClaimLost
from jobhunter.llm.pricing import Effort, Model, Usage, settled_usd, worst_case_usd
from jobhunter.observability.alerts import open_alert, raise_alert, resolve_alert, resolve_alerts
from jobhunter.observability.logging import get_logger
from jobhunter.observability.metrics import (
    LLM_BUCKET_USD,
    LLM_HEADROOM_USD,
    LLM_RESERVATION_RATIO,
    LLM_SPEND_USD,
)
from jobhunter.settings.active import lock_active
from jobhunter.types import JSONValue, get_int, unstorable
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.operations import OperationClaim, fail_eval_run, lock_operation_owner
from jobhunter.worker.reconcilers import ReconcileContext, reconciler

CAP_80PCT_ALERT: Final = "llm.cap_80pct"
COUNTER_DRIFT_ALERT: Final = "llm.budget_counter_drift"
BOUND_VIOLATED_ALERT: Final = "llm.reservation_bound_violated"
CAP_REACHED_ALERT: Final = "llm.cap_reached"  # raised by M3's scoring denial (§6.9.4, `raise_cap_reached`)

# §17.4: a `reserved` entry is recovered after this long; it is also the floor for a `dispatched` one.
RECOVERY_AFTER: Final = timedelta(minutes=15)
# Plan Deviation 24: a `dispatched` online entry waits for its recorded deadline plus this grace.
DEADLINE_GRACE: Final = timedelta(minutes=5)
# A `running` eval run older than this is abandoned (a CLI that died between cases or inside a call).
EVAL_RUN_ABANDONED: Final = timedelta(hours=1)
EVAL_RUN_ABANDONED_REASON: Final = "abandoned"  # its `metrics.failed`
# The reconciler recovers at most this many entries per tick; the next tick takes the rest.
RECOVERY_BATCH: Final = 100

BOUND_VIOLATED: Final = "reservation_bound_violated"
NEVER_DISPATCHED: Final = "never_dispatched"
UNSETTLED_CODE: Final = "unsettled"
# Positive evidence of no charge a settlement may carry (§10.7; the ledger CHECK lists them). Online tries
# have their own; a batch item's come from its provider result (§6.9.1, M3).
ONLINE_REFUND_EVIDENCE: Final = frozenset({"error_response_no_content", "connect_failed_before_send"})
# `batch_submit_rejected` (L2335): the provider refused the whole submission, so none of its items ran; the
# call row ends `error` with that code and the rejection's `http_status`. `connect_failed_before_send`
# (Ruling T16-a): the create's last try failed to connect before any byte left (the evidence follows the last
# try; no earlier one was processed either), so no batch exists.
BATCH_REFUND_EVIDENCE: Final = frozenset(
    {
        "batch_submit_rejected",
        "connect_failed_before_send",
        "batch_item_errored",
        "batch_item_canceled",
        "batch_item_expired",
    }
)
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

# The states of a batch submission (§7.12, §7.16.6): the expected state is a `BatchSubmissionOwner`'s
# predicate (L303).
BatchState = Literal[
    "preparing",
    "submit_started",
    "submitted",
    "submit_rejected",
    "submit_ambiguous",
    "released",
    "ended",
    "reconciled",
    "abandoned",
]

EVAL_RUN_OWNER: Final = "eval_run"
SUBMISSION_OWNER: Final = "batch_submission"

_LOCK_EVAL_RUN = q("SELECT 1 FROM eval_runs WHERE id = :id AND status = 'running' FOR SHARE")
# §3.3.3 L302, §9.4.3 L3573: the score attempt's full predicate.
_LOCK_TARGET = q(
    "SELECT 1 FROM score_targets WHERE id = :t AND claim_id = :c AND attempt_no = :a "
    "AND state = 'in_progress' FOR SHARE"
)
_LOCK_SUBMISSION = q("SELECT 1 FROM batch_submissions WHERE id = :s AND state = :expected FOR SHARE")
_OPERATION_OWNER = q("SELECT status, claim_id FROM operations WHERE id = :id FOR UPDATE")
_EVAL_RUN_OWNER = q("SELECT status FROM eval_runs WHERE id = :id FOR UPDATE")
# The M3 owner rows are referenced by foreign keys (`llm_calls`, `batch_items`, `score_attempts`), so recovery
# locks them `FOR NO KEY UPDATE`: it conflicts with the owner's `FOR SHARE` and never with an FK check's
# `FOR KEY SHARE`.
_TARGET_OWNER = q("SELECT state, claim_id, attempt_no FROM score_targets WHERE id = :id FOR NO KEY UPDATE")
_SUBMISSION_STATE = q("SELECT state FROM batch_submissions WHERE id = :id FOR NO KEY UPDATE")


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
    """An eval run, owned while its `eval_runs` row is `running`. In M2 the schema dry-run of the CLI owns its
    run this way (no `claim`); a run that an `eval_run` operation executes (M3) is owned under that
    operation's claim as well (Codex r1 #29), so a reclaimed execution reserves nothing more."""

    eval_run_id: int
    claim: OperationClaim | None = None

    async def lock(self, conn: AsyncConnection) -> None:
        """With a claim, the `operations` row under `claim_id = :c AND status = 'running'` (§3.3.3) and then
        the `eval_runs` row `FOR SHARE` while `running`; `FencedOut("eval_run")` when either fails."""
        if self.claim is not None:
            try:
                await lock_operation_owner(conn, self.claim)
            except FencedOut as exc:
                raise FencedOut(EVAL_RUN_OWNER) from exc
        if (await conn.execute(_LOCK_EVAL_RUN, {"id": self.eval_run_id})).first() is None:
            raise FencedOut(EVAL_RUN_OWNER)

    def durable(self) -> dict[str, JSONValue]:
        ref: dict[str, JSONValue] = {"kind": "eval_run", "id": self.eval_run_id}
        if self.claim is not None:
            ref |= {"operation_id": self.claim.operation_id, "claim_id": str(self.claim.claim_id)}
        return ref


@dataclass(frozen=True)
class TargetAttemptOwner:
    """A score attempt (§3.3.3 L302: its target under `claim_id = :claim AND attempt_no = :attempt AND state =
    'in_progress'`), and `attempt_id`, the `score_attempts` row whose counters its dispatch markers move."""

    target_id: int
    claim_id: UUID
    attempt_no: int
    attempt_id: int

    async def lock(self, conn: AsyncConnection) -> None:
        """The target row `FOR SHARE` with the full predicate (L3573), else `ClaimLost` (§17.1
        `claim_lost`)."""
        params = {"t": self.target_id, "c": self.claim_id, "a": self.attempt_no}
        if (await conn.execute(_LOCK_TARGET, params)).first() is None:
            raise ClaimLost()

    def durable(self) -> dict[str, JSONValue]:
        return {
            "kind": "target",
            "id": self.target_id,
            "claim_id": str(self.claim_id),
            "attempt_no": self.attempt_no,
            "attempt_id": self.attempt_id,
        }


@dataclass(frozen=True)
class BatchSubmissionOwner:
    """A batch submission (§3.3.3 L303: `state = :expected`), the owner of its items' entries."""

    submission_id: int
    expected: BatchState

    async def lock(self, conn: AsyncConnection) -> None:
        """The `batch_submissions` row `FOR SHARE` while it is in `expected`, else
        `FencedOut("batch_submission")`."""
        params = {"s": self.submission_id, "expected": self.expected}
        if (await conn.execute(_LOCK_SUBMISSION, params)).first() is None:
            raise FencedOut(SUBMISSION_OWNER)

    def durable(self) -> dict[str, JSONValue]:
        return {"kind": "batch_submission", "id": self.submission_id, "state": self.expected}


SpendOwner = OperationOwner | EvalRunOwner | TargetAttemptOwner | BatchSubmissionOwner


async def _lock_owner(conn: AsyncConnection, owner: SpendOwner) -> None:
    """The owner lock, then no active `global` purge fence (§20.2) unless the owner is the purge itself."""
    await owner.lock(conn)
    exempt = isinstance(owner, OperationOwner) and owner.claim.kind == "purge"
    if not exempt and await global_fence_active(conn):
        raise FencedOut("purge_fence")


async def _operation_holds(conn: AsyncConnection, operation_id: int, claim_id: JSONValue) -> bool:
    row = (await conn.execute(_OPERATION_OWNER, {"id": operation_id})).first()
    return (
        row is not None
        and row.status == "running"
        and row.claim_id is not None
        and str(row.claim_id) == claim_id
    )


async def owner_holds(conn: AsyncConnection, ref: Mapping[str, JSONValue], *, now: datetime) -> bool:
    """Lock the recorded owner's row (`FOR UPDATE`; `FOR NO KEY UPDATE` for a target or a submission, rows
    other rows reference by foreign key) and test its predicate: an operation that is `running` under
    the recorded `claim_id`; a `running` eval run, and its operation under the recorded claim when it has one
    (the operation's row first, as `EvalRunOwner.lock` takes them); a target `in_progress` under the recorded
    `claim_id` and `attempt_no`; a batch submission in the recorded state. A missing row never holds; an
    unknown owner kind is a `ValueError` (a writer this function does not know). `now` is the caller's clock
    (no owner uses it)."""
    kind = ref.get("kind")
    doc = dict(ref)
    if kind == "operation":
        return await _operation_holds(conn, get_int(doc, "id"), ref.get("claim_id"))
    if kind == "eval_run":
        if ref.get("claim_id") is not None and not await _operation_holds(
            conn, get_int(doc, "operation_id"), ref.get("claim_id")
        ):
            return False
        row = (await conn.execute(_EVAL_RUN_OWNER, {"id": get_int(doc, "id")})).first()
        return row is not None and row.status == "running"
    if kind == "target":
        row = (await conn.execute(_TARGET_OWNER, {"id": get_int(doc, "id")})).first()
        return (
            row is not None
            and row.state == "in_progress"
            and row.claim_id is not None
            and str(row.claim_id) == ref.get("claim_id")
            and row.attempt_no == ref.get("attempt_no")
        )
    if kind == "batch_submission":
        row = (await conn.execute(_SUBMISSION_STATE, {"id": get_int(doc, "id")})).first()
        return row is not None and row.state == ref.get("state")
    raise ValueError(f"unknown spend owner kind {kind!r}")


# ---- the month row (§6.9.1) ------------------------------------------------------------------------------

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
    call it first in the transaction). It waits for a settings save in progress (`FOR UPDATE`) and then reads
    the version that save made active (`settings.active`: the lock and the document are two statements)."""
    payload = (await lock_active(conn, share=True)).payload
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


async def change_buffer(
    conn: AsyncConnection, *, month: date, overrun_buffer_usd: Decimal, now: datetime
) -> None:
    """A save of class `budget` that changes the overrun buffer and not the cap (Task 17): the buffer is
    written to `month`'s row under its lock (§6.9.1). The cap, and a lowering `change_cap` recorded with its
    time and counted spend, stay as they are; the 80% share reads the cap only. The row must exist
    (`ensure_month` first)."""
    row = await _lock_row(conn, month)
    row.overrun_buffer_usd = overrun_buffer_usd
    await _save(conn, row, now)
    log.info("llm_buffer_changed", month=month.isoformat())


# ---- the cap reached (§6.9.4 L1199, §17.6; Codex r5 B4) ----------------------------------------------------

_MARK_CAP_REACHED = q(
    "UPDATE llm_budget SET cap_reached_alerted_at = :now, updated_at = :now WHERE month = :month"
)


def _cap_reached_subject(month: date) -> str:
    """The month's ISO date: the subjects of two months order as the months do."""
    return month.isoformat()


async def raise_cap_reached(
    conn: AsyncConnection, month: date, *, quarantined_usd: Decimal, now: datetime
) -> bool:
    """Under `month`'s row lock: raise the critical `llm.cap_reached` (subject: the month; context: the amount
    held by quarantined batch submissions, L1199) and set the row's `cap_reached_alerted_at`, unless that
    once-only marker is set already. Returns whether it raised. The marker is never cleared: a denial after a
    recovery in the same month raises no second alert ("once per month"). The row must exist (the denied
    reservation created it)."""
    row = await _lock_row(conn, month)
    if row.cap_reached_alerted_at is not None:
        return False
    message = "a scoring reservation was denied by the monthly LLM cap; scoring pauses until headroom returns"
    if quarantined_usd > 0:
        message += f"; ${quarantined_usd} is held by quarantined batch submissions"
    await raise_alert(
        conn,
        code=CAP_REACHED_ALERT,
        severity="critical",
        subject=_cap_reached_subject(month),
        message=message,
        context={"quarantined_batch_usd": str(quarantined_usd)},
    )
    await conn.execute(_MARK_CAP_REACHED, {"month": month, "now": now})
    log.error("llm_cap_reached", month=month.isoformat(), quarantined_batch_usd=str(quarantined_usd))
    return True


async def resolve_cap_reached(conn: AsyncConnection, month: date) -> bool:
    """Resolve `month`'s open `llm.cap_reached` (headroom returned, §17.6), leaving its marker; `False` when
    none was open."""
    return await resolve_alert(conn, code=CAP_REACHED_ALERT, subject=_cap_reached_subject(month))


async def resolve_stale_cap_alerts(conn: AsyncConnection, *, current: date) -> int:
    """Resolve every open `llm.cap_reached` of a month before `current` (`budget_release`'s every tick, so an
    alert of one month never outlives the first tick of the next); the number resolved."""
    return await resolve_alerts(conn, code=CAP_REACHED_ALERT, subject_before=_cap_reached_subject(current))


# ---- reservation and dispatch (§6.9.1, §10.7, §7.16.7) ----------------------------------------------------

_INSERT_ENTRY = q(
    "INSERT INTO llm_spend_ledger (budget_month, kind, purpose, eval_run, purpose_class, model_requested, "
    "state, reserved_usd, input_tokens_bound, batch, bulk_operation_id, admitted_cap_usd, "
    "admitted_buffer_usd, counted_before_usd, created_at) VALUES (:month, 'request', :purpose, :eval_run, "
    ":purpose_class, :model, 'reserved', :reserved_usd, :input_tokens_bound, :batch, :bulk_operation_id, "
    ":admitted_cap_usd, :admitted_buffer_usd, :counted_before_usd, :now) RETURNING id"
)
_INSERT_CALL = q(
    "INSERT INTO llm_calls (purpose, attempt_id, target_id, round, logical_call_no, max_tokens_stage, "
    "transport_try, profile_draft_id, eval_run_id, golden_case_id, execution_config_id, model_requested, "
    "effort, max_tokens, transport, batch_item_id, prompt_template_version, rendered_prompt_sha256, "
    "prefix_sha256, input_refs, input_tokens_bound, status, spend_ledger_id, started_at) VALUES (:purpose, "
    ":attempt_id, :target_id, :round, :logical_call_no, :max_tokens_stage, :transport_try, "
    ":profile_draft_id, :eval_run_id, :golden_case_id, :execution_config_id, :model, :effort, :max_tokens, "
    ":transport, :batch_item_id, :prompt_template_version, :rendered_prompt_sha256, :prefix_sha256, "
    ":input_refs, :input_tokens_bound, 'reserved', :ledger_id, :now) RETURNING id",
    jsonb=["input_refs"],
)
_ENTRY = q(
    "SELECT l.id, l.budget_month, l.purpose, l.purpose_class, l.model_requested, l.state, l.reserved_usd, "
    "l.settled_usd, l.batch, l.bulk_operation_id, l.created_at, l.dispatched_at, c.id AS call_id, "
    "c.status AS call_status, c.attempt_id, c.input_refs FROM llm_spend_ledger l "
    "JOIN llm_calls c ON c.spend_ledger_id = l.id WHERE l.id = :id AND l.kind = 'request'"
)
# The bulk operation's allocation (§6.9.3), step 7 of the schedule: locked after the month row, before the
# ledger rows. `FOR NO KEY UPDATE`, the lock its `spent_usd` update takes: it serialises the allocation's
# writers and leaves the FK checks of the rows that reference the operation (targets, submissions, ledger
# entries; `FOR KEY SHARE`) unblocked.
_LOCK_BULK = q("SELECT state, budget_usd, spent_usd FROM bulk_operations WHERE id = :id FOR NO KEY UPDATE")
_BULK_SPENT = q("UPDATE bulk_operations SET spent_usd = spent_usd + :delta WHERE id = :id")
# §9.4.2 L3567, L2224, L2863: the dispatch marker counts the try on its attempt and charges the attempt and,
# the first time, its target. The rows are locked in id order (the schedule's step 10), `FOR NO KEY UPDATE`
# (the lock of the counter updates themselves: an FK check of a row that references them is not blocked).
_LOCK_ATTEMPTS = q(
    "SELECT id, target_id, charged FROM score_attempts WHERE id = ANY(CAST(:ids AS bigint[])) "
    "ORDER BY id FOR NO KEY UPDATE"
)
_COUNT_TRIES = q(
    "UPDATE score_attempts SET http_calls = http_calls + 1, logical_calls = logical_calls + :l, "
    "charged = true WHERE id = ANY(CAST(:ids AS bigint[]))"
)
# Ruling T16-c: a batch marker's targets before its attempts, the order of a cancel (`targets._cancel`).
_LOCK_TARGETS_OF_ATTEMPTS = q(
    "SELECT id FROM score_targets WHERE id IN (SELECT target_id FROM score_attempts "
    "WHERE id = ANY(CAST(:ids AS bigint[]))) ORDER BY id FOR NO KEY UPDATE"
)
_CHARGE_TARGETS = q(
    "UPDATE score_targets SET charged_attempts = charged_attempts + 1 WHERE id IN "
    "(SELECT id FROM score_targets WHERE id = ANY(CAST(:ids AS bigint[])) ORDER BY id FOR NO KEY UPDATE)"
)
# L3911: what an attempt's tries count at (billed their cost, ambiguous their reservation).
_ATTEMPT_COST = q("UPDATE score_attempts SET cost_usd = cost_usd + :delta WHERE id = :id")
# The entries of a batch dispatch marker, in id order.
_BATCH_ENTRIES = q(
    "SELECT l.id, l.batch, c.id AS call_id, c.attempt_id, c.input_refs -> 'owner' AS owner "
    "FROM llm_spend_ledger l JOIN llm_calls c ON c.spend_ledger_id = l.id "
    "WHERE l.id = ANY(CAST(:ids AS bigint[])) AND l.kind = 'request' ORDER BY l.id"
)
# Every entry a submission still has `reserved`: its owner is the submission, by kind and id.
_SUBMISSION_RESERVED = q(
    "SELECT l.id FROM llm_spend_ledger l JOIN llm_calls c ON c.spend_ledger_id = l.id "
    "WHERE l.state = 'reserved' AND l.kind = 'request' AND c.input_refs -> 'owner' @> "
    "jsonb_build_object('kind', 'batch_submission', 'id', CAST(:submission_id AS bigint)) ORDER BY l.id"
)
# The bulk operation a score target or a batch submission belongs to, read under its owner lock.
_TARGET_BULK = q("SELECT bulk_operation_id FROM score_targets WHERE id = :id")
_SUBMISSION_BULK = q("SELECT bulk_operation_id FROM batch_submissions WHERE id = :id")
# Codex r1 #27: a target's online attempt is a funded follow-up iff it has an unreleased `followup` item.
_FUNDED_FOLLOW_UP = q(
    "SELECT t.bulk_operation_id FROM score_targets t WHERE t.id = :t AND t.bulk_operation_id IS NOT NULL "
    "AND EXISTS (SELECT 1 FROM batch_items i WHERE i.target_id = t.id AND i.decision = 'followup' "
    "AND i.hold_released_at IS NULL)"
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
    attempt_id: int | None
    input_refs: dict[str, JSONValue]

    @property
    def bucketed(self) -> bool:
        """Scoring outside bulk operations debits and credits the bucket (§6.9.2)."""
        return self.purpose_class == "scoring" and self.bulk_operation_id is None

    @property
    def attempt_cost(self) -> Decimal:
        """What the entry counts at in its attempt's `cost_usd` (L3911): billed its cost, ambiguous its
        reservation, anything else (reserved, dispatched in flight, not billed) nothing."""
        if self.state == "ambiguous":
            return self.reserved_usd
        if self.state == "billed" and self.settled_usd is not None:
            return self.settled_usd
        return _ZERO

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
        attempt_id=None if row.attempt_id is None else int(row.attempt_id),
        input_refs=dict(row.input_refs),
    )


async def _fund(conn: AsyncConnection, bulk_operation_id: int, r: Decimal) -> int | None:
    """The bulk row `FOR NO KEY UPDATE` (§6.9.3); `bulk_operation_id` when the operation is `active` and `r`
    fits its allocation (`spent_usd + r ≤ budget_usd`, L1191), else `None`: a finished operation is refused
    as a full one is, and the reservation is charged to the bucket like any other online work."""
    row = (await conn.execute(_LOCK_BULK, {"id": bulk_operation_id})).first()
    if row is not None and row.state == "active" and row.spent_usd + r <= row.budget_usd:
        return bulk_operation_id
    log.info(
        "llm_bulk_allocation_refused",
        bulk_operation_id=bulk_operation_id,
        state=None if row is None else row.state,
        reserved_usd=str(r),
    )
    return None


async def _lock_bulk(conn: AsyncConnection, bulk_operation_id: int) -> None:
    """The funded entry's bulk row `FOR NO KEY UPDATE`, before its ledger rows (an entry keeps the id only of
    a row that existed when it was funded; `ON DELETE SET NULL` clears it with the row)."""
    await conn.execute(_LOCK_BULK, {"id": bulk_operation_id})


async def _bulk_spent(conn: AsyncConnection, bulk_operation_id: int, delta: Decimal) -> None:
    """`spent_usd` moves by `delta`, the entry's counter change (no CHECK of `spent_usd ≤ budget_usd`: a
    bound violation is charged in full, L1164)."""
    if delta != 0:
        await conn.execute(_BULK_SPENT, {"id": bulk_operation_id, "delta": delta})


async def _attempt_cost(conn: AsyncConnection, attempt_id: int | None, delta: Decimal) -> None:
    """The attempt's `cost_usd` moves by `delta` (L3911), for a score call's entry."""
    if attempt_id is not None and delta != 0:
        await conn.execute(_ATTEMPT_COST, {"id": attempt_id, "delta": delta})


async def _check_own_bulk(conn: AsyncConnection, owner: SpendOwner, bulk_operation_id: int) -> None:
    """A score attempt or a batch submission is funded by its own bulk operation only (its row's
    `bulk_operation_id`, read under the owner lock the caller holds); any other is a `ValueError`."""
    if isinstance(owner, TargetAttemptOwner):
        own = (await conn.execute(_TARGET_BULK, {"id": owner.target_id})).scalar_one_or_none()
    elif isinstance(owner, BatchSubmissionOwner):
        own = (await conn.execute(_SUBMISSION_BULK, {"id": owner.submission_id})).scalar_one_or_none()
    else:
        return
    if own != bulk_operation_id:
        raise ValueError(f"bulk operation {bulk_operation_id} is not the owner's own bulk operation")


async def funded_bulk_operation_id(conn: AsyncConnection, *, target_id: int) -> int | None:
    """The bulk operation a score attempt of `target_id` is funded by (Codex r1 #27): the target's
    `bulk_operation_id` iff the target has a `batch_items` row with `decision = 'followup'` and
    `hold_released_at IS NULL`, the durable funding link of its online follow-up; `None` for every other
    online attempt. A plain read: the reservation re-checks the operation under its lock."""
    found = (await conn.execute(_FUNDED_FOLLOW_UP, {"t": target_id})).scalar_one_or_none()
    return None if found is None else int(found)


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
    logical_call_no: int | None = None,
    deadline_s: int | None = None,
    attempt_id: int | None = None,
    target_id: int | None = None,
    round: int | None = None,  # the `llm_calls` column
    bulk_operation_id: int | None = None,
    batch_item_id: int | None = None,
    golden_case_id: int | None = None,
) -> Reservation:
    """Reserve one try in the caller's transaction (module docstring; the steps 0-10 below), or raise.

    `FencedOut` (the owner lost its predicate, `ClaimLost` for a score attempt, or a global purge fence is
    active) is raised before anything is written. `BudgetExhausted` (the §6.9.1 check refused the worst case)
    is raised before the ledger and call rows: only the month row's accrual is stored in the caller's
    transaction, which commits it or rolls it back.

    The score columns (M3): `attempt_id`, `target_id` and `round` of a score call (a `TargetAttemptOwner`
    names its own attempt and target, and a call of it carries them); `batch_item_id` of a batch item, whose
    try is `transport='batch'`, its single submission attempt (`transport_try` 1, `llm_calls.transport_try`
    is NOT NULL) with no `logical_call_no` and no `deadline_s` (an online try has both); `golden_case_id` of
    an eval case's call. An eval run's call (`eval_run_id`) is booked `eval_run`, which the ledger requires to
    be non-scoring. `bulk_operation_id` is the funding operation of a scoring reservation (step 6): a batched
    target's item or its funded follow-up (`funded_bulk_operation_id`); it is kept on the entry only while
    the operation is active and the reservation fits its allocation.

    `ValueError`, before anything is written, for an inconsistent call: a non-scoring one with a bulk
    operation; a score attempt's call for another attempt or target, or not online; a batch submission's
    reservation that is not a batch item; a bulk operation that is not the target's or the submission's own
    (its row's `bulk_operation_id`, read under the owner lock); an online try without its logical call and
    deadline; `input_refs` that set the owner or the deadline.
    """
    if purpose not in budget.SCORING_PURPOSES and purpose_class != "nonscoring":
        raise ValueError(f"purpose {purpose} is non-scoring")
    if bulk_operation_id is not None and purpose_class != "scoring":
        raise ValueError("a non-scoring call is never booked against a bulk operation")
    if isinstance(owner, EvalRunOwner) and eval_run_id != owner.eval_run_id:
        raise ValueError("an eval run's call carries its own eval_run_id")
    if isinstance(owner, TargetAttemptOwner) and (attempt_id, target_id) != (
        owner.attempt_id,
        owner.target_id,
    ):
        raise ValueError("a score attempt's call carries its own attempt_id and target_id")
    if isinstance(owner, TargetAttemptOwner) and transport != "stream":
        raise ValueError("a score attempt's own calls are online; a batch item is its submission's")
    if isinstance(owner, BatchSubmissionOwner) and transport != "batch":
        raise ValueError("a batch submission reserves batch items only (transport='batch')")
    if transport == "stream" and (logical_call_no is None or deadline_s is None):
        raise ValueError("an online try names its logical call and its deadline")
    if reserved_keys := _RESERVED_REFS & set(input_refs):
        raise ValueError(
            f"input_refs must not set {sorted(reserved_keys)}: the owner and deadline are reserve's"
        )
    p = await current_params(conn)  # 0. active_config FOR SHARE
    await _lock_owner(conn, owner)  # 1. the owner row FOR SHARE
    if bulk_operation_id is not None:  # under the owner lock: nothing is written before the refusal
        await _check_own_bulk(conn, owner, bulk_operation_id)
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
    # 6. the bulk row FOR NO KEY UPDATE: the allocation funds the reservation while it fits (L1191)
    funded = None if bulk_operation_id is None else await _fund(conn, bulk_operation_id, r)
    ledger_id = (  # 7.
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
                "bulk_operation_id": funded,
                "admitted_cap_usd": row.cap_usd,
                "admitted_buffer_usd": row.overrun_buffer_usd,
                "counted_before_usd": row.counted_usd,
                "now": now,
            },
        )
    ).scalar_one()
    refs: dict[str, JSONValue] = {**input_refs, "owner": owner.durable()}
    if deadline_s is not None:
        refs["deadline_s"] = deadline_s
    call_id = (  # 8.
        await conn.execute(
            _INSERT_CALL,
            {
                "purpose": purpose,
                "attempt_id": attempt_id,
                "target_id": target_id,
                "round": round,
                "logical_call_no": logical_call_no,
                "max_tokens_stage": max_tokens_stage,
                "transport_try": transport_try,
                "profile_draft_id": profile_draft_id,
                "eval_run_id": eval_run_id,
                "golden_case_id": golden_case_id,
                "execution_config_id": execution_config_id,
                "model": model,
                "effort": effort,
                "max_tokens": max_tokens,
                "transport": transport,
                "batch_item_id": batch_item_id,
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
    # 9. the counters, and the pace account of a scoring reservation: the allocation that funds it, else the
    # bucket (§6.9.2, §6.9.3)
    budget.apply_count(row, r, purpose_class, bucket=purpose_class == "scoring" and funded is None)
    await _save(conn, row, now)
    if funded is not None:
        await _bulk_spent(conn, funded, r)
    await _check_cap_share(conn, row)  # 10. opens at 80%; resolves only for a new month
    return Reservation(
        ledger_id=int(ledger_id),
        llm_call_id=int(call_id),
        month=month,
        reserved_usd=r,
        purpose_class=purpose_class,
        model=model,
        batch=batch,
    )


# A batch preparation's bulk row (§6.9.3 L1189): the accounts its allocation check reads, under the lock of
# step 7 every writer of them takes (`bulk.lock_accounts` reads them with it too).
LOCK_BULK_ACCOUNTS: Final = q(
    "SELECT state, budget_usd, spent_usd, followup_held_usd FROM bulk_operations WHERE id = :b "
    "FOR NO KEY UPDATE"
)


@dataclass(frozen=True)
class BatchItemReservation:
    """One item of a batch reservation (`reserve_batch`): its target, its batch attempt and `batch_items`
    row, the input bound of its rendered request, the request's hashes, and the ids of its inputs."""

    target_id: int
    attempt_id: int
    batch_item_id: int
    input_tokens_bound: int
    rendered_prompt_sha256: bytes
    prefix_sha256: bytes | None
    input_refs: Mapping[str, JSONValue]


async def reserve_batch(
    conn: AsyncConnection,
    *,
    owner: BatchSubmissionOwner,
    items: Sequence[BatchItemReservation],
    model: Model,
    effort: Effort,
    max_tokens: int,
    execution_config_id: int,
    prompt_template_version: str,
    bulk_operation_id: int,
    hold_usd: Decimal,
    now: datetime,
) -> list[Reservation]:
    """Reserve every item of a batch submission in the caller's transaction (plan Task 16, §10.10 lifecycle
    step 1, L1159, L1187-1191, Codex r1 #22, r2 B10); the reservations in `items`' order.

    In the §3.3.3 order, once for the whole batch: `active_config` (`current_params`), the owner (the
    submission `FOR SHARE` in its expected state, and no active global purge fence), the month row (created
    if new, locked and accrued) and the bulk row (`FOR NO KEY UPDATE`, step 7). Then, under those locks, the
    allocation check of L1189, `spent_usd + followup_held_usd + Σ worst cases + len(items) x hold_usd ≤
    budget_usd` on an `active` operation, else `BulkAllocationExceeded` before anything is written (the
    preparation sized the batch under the same locks, so this never fires after it). Then per item: its worst
    case at batch prices (`pricing.worst_case_usd(..., batch=True)`), the admission against the hard cap
    (L1159, "including batches"), else `BudgetExhausted` (the caller's transaction rolls back every item
    before it: nothing of the batch is written), the ledger row (`batch`, funded by `bulk_operation_id`,
    `logical_call_no` NULL) and the `llm_calls` row (`transport = 'batch'`, `transport_try` 1, round 0, the
    item's attempt, target and `batch_items` row, the owner in `input_refs`). The counters move by the sum
    (scoring, never the bucket: the allocation pays) and the bulk row's `spent_usd` with them; then the
    `llm.cap_80pct` check. `ValueError`, before anything is written, for an empty batch, a bulk operation
    that is not the submission's own, or `input_refs` that set the owner or the deadline."""
    if not items:
        raise ValueError("a batch reservation names at least one item")
    for item in items:
        if reserved_keys := _RESERVED_REFS & set(item.input_refs):
            raise ValueError(f"input_refs must not set {sorted(reserved_keys)}: the owner is reserve_batch's")
    p = await current_params(conn)  # 0. active_config FOR SHARE
    await _lock_owner(conn, owner)  # 1. the submission FOR SHARE in its expected state
    await _check_own_bulk(conn, owner, bulk_operation_id)
    month = budget.month_of(now)
    await ensure_month(conn, month, p, now)
    row = await lock_month(conn, month, p, now=now)  # 2. llm_budget FOR UPDATE, accrued
    bulk = (await conn.execute(LOCK_BULK_ACCOUNTS, {"b": bulk_operation_id})).one()  # 3. step 7
    worst = [
        worst_case_usd(model, input_tokens_bound=item.input_tokens_bound, max_tokens=max_tokens, batch=True)
        for item in items
    ]
    needed = bulk.spent_usd + bulk.followup_held_usd + sum(worst) + len(items) * hold_usd
    if bulk.state != "active" or needed > bulk.budget_usd:
        shortfall = max(_ZERO, needed - bulk.budget_usd)
        log.error(
            "llm_bulk_allocation_exceeded",
            bulk_operation_id=bulk_operation_id,
            state=bulk.state,
            items=len(items),
            shortfall_usd=str(shortfall),
        )
        raise BulkAllocationExceeded(bulk_operation_id, shortfall)
    reservations: list[Reservation] = []
    durable = owner.durable()
    for item, r in zip(items, worst, strict=True):
        if not budget.admits(row, r, "scoring", p):  # 4. the hard cap, every item (L1159)
            room = budget.headroom(row, "scoring", p)
            log.warning(
                "llm_budget_exhausted",
                month=month.isoformat(),
                purpose="generate",
                purpose_class="scoring",
                reserved_usd=str(r),
                headroom_usd=str(room),
                batch_items=len(items),
            )
            raise BudgetExhausted("scoring", room)
        ledger_id = (
            await conn.execute(
                _INSERT_ENTRY,
                {
                    "month": month,
                    "purpose": "generate",
                    "eval_run": False,
                    "purpose_class": "scoring",
                    "model": model,
                    "reserved_usd": r,
                    "input_tokens_bound": item.input_tokens_bound,
                    "batch": True,
                    "bulk_operation_id": bulk_operation_id,
                    "admitted_cap_usd": row.cap_usd,
                    "admitted_buffer_usd": row.overrun_buffer_usd,
                    "counted_before_usd": row.counted_usd,
                    "now": now,
                },
            )
        ).scalar_one()
        call_id = (
            await conn.execute(
                _INSERT_CALL,
                {
                    "purpose": "generate",
                    "attempt_id": item.attempt_id,
                    "target_id": item.target_id,
                    "round": 0,
                    "logical_call_no": None,
                    "max_tokens_stage": "first",
                    "transport_try": 1,
                    "profile_draft_id": None,
                    "eval_run_id": None,
                    "golden_case_id": None,
                    "execution_config_id": execution_config_id,
                    "model": model,
                    "effort": effort,
                    "max_tokens": max_tokens,
                    "transport": "batch",
                    "batch_item_id": item.batch_item_id,
                    "prompt_template_version": prompt_template_version,
                    "rendered_prompt_sha256": item.rendered_prompt_sha256,
                    "prefix_sha256": item.prefix_sha256,
                    "input_refs": {**item.input_refs, "owner": durable},
                    "input_tokens_bound": item.input_tokens_bound,
                    "ledger_id": ledger_id,
                    "now": now,
                },
            )
        ).scalar_one()
        budget.apply_count(row, r, "scoring", bucket=False)  # the allocation pays, never the bucket
        reservations.append(
            Reservation(
                ledger_id=int(ledger_id),
                llm_call_id=int(call_id),
                month=month,
                reserved_usd=r,
                purpose_class="scoring",
                model=model,
                batch=True,
            )
        )
    await _save(conn, row, now)
    await _bulk_spent(conn, bulk_operation_id, sum(worst, _ZERO))
    await _check_cap_share(conn, row)
    log.info(
        "llm_batch_reserved",
        submission_id=owner.submission_id,
        bulk_operation_id=bulk_operation_id,
        items=len(items),
        reserved_usd=str(sum(worst, _ZERO)),
    )
    return reservations


async def _count_tries(conn: AsyncConnection, attempt_ids: Sequence[int], *, logical: bool) -> None:
    """§9.4.2: one HTTP try more on each attempt (and one logical call more on a logical call's first try),
    the attempt `charged`, and its target's `charged_attempts` once, when the attempt was not yet charged
    (L2224, L2863). Rows in id order."""
    ids = sorted(set(attempt_ids))
    rows = (await conn.execute(_LOCK_ATTEMPTS, {"ids": ids})).all()
    if len(rows) != len(ids):
        raise LookupError(f"score attempts {sorted(set(ids) - {int(r.id) for r in rows})} do not exist")
    await conn.execute(_COUNT_TRIES, {"ids": ids, "l": 1 if logical else 0})
    first_charges = sorted({int(r.target_id) for r in rows if not r.charged})
    if first_charges:
        await conn.execute(_CHARGE_TARGETS, {"ids": first_charges})


async def mark_dispatched(
    conn: AsyncConnection,
    res: Reservation,
    owner: SpendOwner,
    *,
    now: datetime,
    first_try_of_logical_call: bool = False,
) -> None:
    """Commit the dispatch marker (§3.3.3) in the caller's transaction, immediately before the send: under the
    owner lock again, `reserved → dispatched` on the ledger row and on the call. Anything else (a lost
    predicate, an active global purge fence, another owner's try, a try released meanwhile) is `FencedOut`;
    the caller must not send and releases the try (`release_never_dispatched`).

    For a score attempt (`TargetAttemptOwner`), in the same transaction (§9.4.2 L3567): its `http_calls`
    one more, `logical_calls` one more when `first_try_of_logical_call`, `charged`, and its target's
    `charged_attempts` one more when the attempt was not charged before. `now` is the caller's clock;
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
    if isinstance(owner, TargetAttemptOwner):
        await _count_tries(conn, [owner.attempt_id], logical=first_try_of_logical_call)


def _of_submission(ref: JSONValue, owner: BatchSubmissionOwner) -> bool:
    """Whether an entry's recorded owner is `owner`'s submission. The items are reserved while it is
    `preparing` and dispatched at `submit_started`, so the recorded state is the reservation's, not the
    marker's: the marker's own predicate is its owner lock."""
    return (
        isinstance(ref, dict)
        and ref.get("kind") == "batch_submission"
        and ref.get("id") == owner.submission_id
    )


async def mark_batch_dispatched(
    conn: AsyncConnection, owner: BatchSubmissionOwner, entries: Sequence[Reservation], *, now: datetime
) -> None:
    """The batch dispatch marker (R6, plan Deviation 13: a batch attempt is charged at `submit_started`), in
    the caller's transaction: under the submission's owner lock, every item entry `reserved → dispatched`
    with its call, and each item's attempt and target counted and charged as `mark_dispatched` does (the
    item is its attempt's first logical call, one try). An entry of another submission, an online entry, or
    one no longer `reserved` is `FencedOut` (nothing of the marker may commit: the caller's transaction rolls
    back). The items' target rows are locked before their attempt rows (Ruling T16-c). `entries` must be
    every entry the submission still has `reserved`: one left out would stay `reserved` on a submitted
    batch, which no reconciler ages (L2979), so that is a `ValueError`. `now` is the caller's clock, as in
    `mark_dispatched`."""
    ledger_ids = sorted(res.ledger_id for res in entries)
    if len(set(ledger_ids)) != len(ledger_ids):
        raise ValueError("a batch dispatch marker names each entry once")
    await _lock_owner(conn, owner)
    calls = {res.ledger_id: res.llm_call_id for res in entries}
    rows = (await conn.execute(_BATCH_ENTRIES, {"ids": ledger_ids})).all()
    if len(rows) != len(ledger_ids) or any(
        row.call_id != calls[row.id] or not row.batch or not _of_submission(row.owner, owner) for row in rows
    ):
        raise FencedOut("llm_spend_ledger")
    params = {"submission_id": owner.submission_id}
    reserved = {int(i) for i in (await conn.execute(_SUBMISSION_RESERVED, params)).scalars()}
    if not reserved >= set(ledger_ids):  # one of them was dispatched or released meanwhile
        raise FencedOut("llm_spend_ledger")
    if omitted := sorted(reserved - set(ledger_ids)):
        raise ValueError(
            f"the batch dispatch marker names every reserved item of its submission: {omitted} are missing"
        )
    where = "t.id = ANY(CAST(:ids AS bigint[]))"
    moved = await transition_where(
        conn, "llm_spend_ledger", ["reserved"], "dispatched", where=where, params={"ids": ledger_ids}
    )
    call_ids = sorted(calls.values())
    moved_calls = await transition_where(
        conn, "llm_calls", ["reserved"], "dispatched", where=where, params={"ids": call_ids}
    )
    if len(moved) != len(ledger_ids) or len(moved_calls) != len(call_ids):
        raise FencedOut("llm_spend_ledger")
    attempt_ids = sorted({int(row.attempt_id) for row in rows if row.attempt_id is not None})
    # the target rows first (Ruling T16-c): `_count_tries` then locks the attempts and re-takes these
    await conn.execute(_LOCK_TARGETS_OF_ATTEMPTS, {"ids": attempt_ids})
    await _count_tries(conn, attempt_ids, logical=True)


async def _release(conn: AsyncConnection, entry: _Entry, row: BudgetRow, *, now: datetime) -> bool:
    """`reserved → not_billed('never_dispatched')` and the call `cancelled`, the counters `-r`, on the locked
    month `row`, and the allocation credited by `r` for a funded entry (Codex r2 B11: under its bulk row,
    locked before the ledger rows); `False` (nothing written) when the entry was dispatched or released
    meanwhile: the transition is the once-only guard of the credit."""
    if entry.bulk_operation_id is not None:
        await _lock_bulk(conn, entry.bulk_operation_id)
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
    if entry.bulk_operation_id is not None:
        await _bulk_spent(conn, entry.bulk_operation_id, -entry.reserved_usd)
    return True


async def release_never_dispatched(conn: AsyncConnection, res: Reservation, *, now: datetime) -> None:
    """Release a try that was never dispatched (its dispatch marker was fenced), under the month lock:
    `not_billed('never_dispatched')`, the call `cancelled` and the counters (and a funded entry's allocation)
    adjusted. A try that is no longer `reserved` is left as it is, with nothing written (logged
    `llm_release_skipped`)."""
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


# The provider metadata a settlement writes to the call row (M2 Carry-forward J1).
_PROVIDER_METADATA: Final = ("model_served", "stop_reason", "refusal_category", "provider_request_id")
_UNSTORABLE_TEXT: Final = "?"


def storable_text(text: str) -> str:
    """`text` with every NUL (U+0000) and every surrogate code point replaced by `?`: no `text` column holds a
    NUL, and no UTF-8 encodes a lone surrogate (`types.unstorable`)."""
    if unstorable(text) is None:
        return text
    return "".join(
        _UNSTORABLE_TEXT if char == "\x00" or "\ud800" <= char <= "\udfff" else char for char in text
    )


def storable_outcome(call: CallOutcome) -> CallOutcome:
    """`call` with its provider metadata (`model_served`, `stop_reason`, `refusal_category`,
    `provider_request_id`) made storable (`storable_text`), so the settlement that writes them never rolls
    back (M2 Deviation 44, Carry-forward J1); a replacement is logged `llm_provider_metadata_unstorable`
    with the field names only, never a value."""
    stored = dataclasses.replace(
        call,
        model_served=_storable(call.model_served),
        stop_reason=_storable(call.stop_reason),
        refusal_category=_storable(call.refusal_category),
        provider_request_id=_storable(call.provider_request_id),
    )
    changed = [name for name in _PROVIDER_METADATA if getattr(stored, name) != getattr(call, name)]
    if not changed:
        return call
    log.warning("llm_provider_metadata_unstorable", fields=changed)
    return stored


def _storable(text: str | None) -> str | None:
    return None if text is None else storable_text(text)


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

    The counter change goes to the bucket, or for a funded entry to its operation's `spent_usd` under the bulk
    row (locked after the month row, before the ledger rows), in full also above `budget_usd` (L1164). A
    score call's attempt `cost_usd` moves by what the try now counts at minus what it counted before (L3911):
    `+ s` billed, `+ r` ambiguous, `+ (s - r)` late billed, `- r` late not billed. The call's provider
    metadata is written storable (`storable_outcome`).
    """
    _check_settlement(res, outcome, evidence, usage, call)
    call = storable_outcome(call)
    p = await current_params(conn)
    row = await _lock_row(conn, res.month)  # the month lock; the accrual is stored with the counters below
    entry = await _reserved_entry(conn, res)
    budget.accrue_row(row, p, now)
    if entry.state in ("billed", "not_billed") or (entry.state == "ambiguous" and outcome == "ambiguous"):
        log.warning("llm_settle_duplicate", ledger_id=entry.id, state=entry.state, outcome=outcome)
        return await _figures(conn, entry, row, p, _counted_at(entry))  # nothing written
    if entry.state not in ("dispatched", "ambiguous"):
        raise ValueError(f"llm_spend_ledger {entry.id} is {entry.state}: only a dispatched try settles")
    if entry.bulk_operation_id is not None:
        await _lock_bulk(conn, entry.bulk_operation_id)  # the allocation, before the ledger rows
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
    delta = budget.settlement_delta(outcome, entry.reserved_usd, cost)
    budget.apply_count(row, delta, entry.purpose_class, bucket=entry.bucketed)
    await _save(conn, row, now)
    if entry.bulk_operation_id is not None:
        await _bulk_spent(conn, entry.bulk_operation_id, delta)
    counted_at = entry.reserved_usd if cost is None else cost
    await _attempt_cost(conn, entry.attempt_id, counted_at - entry.attempt_cost)
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
    "SELECT DISTINCT ON (logical_call_no) logical_call_no, id, output_json, error_code, max_tokens "
    "FROM llm_calls WHERE input_refs -> 'owner' @> "
    "jsonb_build_object('kind', 'operation', 'id', CAST(:operation_id AS bigint)) "
    "AND purpose = :purpose AND status = 'completed' AND logical_call_no IS NOT NULL "
    "ORDER BY logical_call_no, id DESC"
)
# A score attempt's newest call of one purpose, whatever its status (`llm_calls_attempt` serves it).
_LAST_CALL_OF_ATTEMPT = q(
    "SELECT id, output_json, error_code, max_tokens FROM llm_calls "
    "WHERE attempt_id = :attempt_id AND purpose = :purpose ORDER BY id DESC LIMIT 1"
)


@dataclass(frozen=True)
class Receipt:
    """The newest `completed` call of one logical call: its parsed output (`output_json`), or the code of why
    it has none (`error_code`: `invalid_output`, `refused`, `max_tokens_first_stage`, `max_tokens`,
    `unexpected_stop_reason`, `model_mismatch`), and the `max_tokens` its try was sent with (M2
    Carry-forward J2: a resume compares it with the stage it would send now)."""

    llm_call_id: int
    output_json: JSONValue | None
    error_code: str | None
    max_tokens: int


async def receipts(conn: AsyncConnection, *, operation_id: int, purpose: Purpose) -> dict[int, Receipt]:
    """The receipts of operation `operation_id`'s `purpose` calls, by `logical_call_no` (§9.1): the newest
    `completed` call of each. A node reads them under its owner lock, before it pays for a call again. Calls
    that did not complete (an error response, a lost stream, a released try) are no receipts."""
    rows = await conn.execute(_RECEIPTS, {"operation_id": operation_id, "purpose": purpose})
    return {int(row.logical_call_no): _receipt(row) for row in rows}


def _receipt(row: Row[tuple[object, ...]]) -> Receipt:
    return Receipt(
        llm_call_id=int(row.id),
        output_json=row.output_json,
        error_code=row.error_code,
        max_tokens=int(row.max_tokens),
    )


async def last_call_of_attempt(conn: AsyncConnection, *, attempt_id: int, purpose: Purpose) -> Receipt | None:
    """The newest `llm_calls` row of score attempt `attempt_id` for `purpose`, any status (plan Task 9's
    `load_context` decides the start stage from it: a first-stage `max_tokens` try whose `max_tokens` is below
    the pinned stage's value starts at `first` again, else `enlarged`); `None` without one."""
    row = (await conn.execute(_LAST_CALL_OF_ATTEMPT, {"attempt_id": attempt_id, "purpose": purpose})).first()
    return None if row is None else _receipt(row)


# ---- the counters recomputed from the ledger (§7.11, §6.9.1) ----------------------------------------------

# §7.11: counted(month) = Σ settled_usd (billed) + Σ reserved_usd (reserved, dispatched, ambiguous)
# + Σ adjustments. An adjustment row (no M2 code writes one) counts its `reserved_usd`, its one mandatory
# amount; a `not_billed` entry counts 0. `_COUNTED` is what one ledger row counts at, for the month's counters
# and for a bulk operation's `spent_usd` alike. `CAST(:month AS date) IS NULL` selects every month.
_COUNTED: Final = (
    "CASE WHEN kind = 'adjustment' THEN reserved_usd WHEN state = 'billed' THEN settled_usd "
    "WHEN state IN ('reserved', 'dispatched', 'ambiguous') THEN reserved_usd ELSE 0 END"
)
_COUNTERS = q(
    f"WITH amounts AS (SELECT budget_month, purpose_class, {_COUNTED} AS amount FROM llm_spend_ledger "  # noqa: S608
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
# §7.17 #7's first clause (A1 issue 29, M3 Task 18): an active bulk operation's `spent_usd` against what the
# ledger entries it funded count at; `_LEDGER_SPENT` again under the bulk row's lock.
_BULK_DRIFTS = q(
    f"SELECT b.id FROM bulk_operations b WHERE b.state = 'active' AND b.spent_usd <> (SELECT "  # noqa: S608
    f"COALESCE(SUM({_COUNTED}), 0) FROM llm_spend_ledger l WHERE l.bulk_operation_id = b.id) ORDER BY b.id"
)
_LEDGER_SPENT = q(
    f"SELECT COALESCE(SUM({_COUNTED}), 0) FROM llm_spend_ledger WHERE bulk_operation_id = :id"  # noqa: S608
)
_CORRECT_BULK = q("UPDATE bulk_operations SET spent_usd = :spent WHERE id = :id")


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
    month); then (M3 Task 18, A1 issue 29) every active bulk operation whose `spent_usd` differs from what
    its funded entries count at (`_COUNTED`: reserved, dispatched and ambiguous at their reservation, billed
    at its cost, not billed 0) is corrected likewise under the bulk row's lock (step 7; every writer of a
    funded entry holds it), with the same alert (subject `bulk:<id>`). A pass that corrects nothing resolves
    the alert. Returns the months and operations corrected. Fenced by the worker epoch first: the worker's own
    `FencedOut` propagates (Ruling FR-w), another owner's ends the pass with the count so far."""
    corrected = 0
    try:
        async with ctx.engine.begin() as conn:
            await fence_epoch(conn, ctx.epoch)
            months = await _drifting_months(conn)
            operations = [int(i) for i in (await conn.execute(_BULK_DRIFTS)).scalars()]
        for month in months:
            async with ctx.engine.begin() as conn:
                await fence_epoch(conn, ctx.epoch)
                corrected += await _correct(conn, month, now=ctx.now)
        for bulk_operation_id in operations:
            async with ctx.engine.begin() as conn:
                await fence_epoch(conn, ctx.epoch)
                corrected += await _correct_bulk(conn, bulk_operation_id)
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


async def _correct_bulk(conn: AsyncConnection, bulk_operation_id: int) -> int:
    """Correct an active bulk operation's `spent_usd` from its ledger entries under its row lock; 1 when it
    differed (and was corrected, with the alert), 0 when it agrees again or the operation has ended."""
    row = (await conn.execute(_LOCK_BULK, {"id": bulk_operation_id})).first()
    if row is None or row.state != "active":
        return 0
    ledger = Decimal((await conn.execute(_LEDGER_SPENT, {"id": bulk_operation_id})).scalar_one())
    if ledger == row.spent_usd:
        return 0
    await conn.execute(_CORRECT_BULK, {"spent": ledger, "id": bulk_operation_id})
    context: dict[str, JSONValue] = {
        "bulk_operation_id": bulk_operation_id,
        "stored_spent_usd": str(row.spent_usd),
        "ledger_spent_usd": str(ledger),
    }
    log.error("llm_bulk_spent_drift", **context)
    await raise_alert(
        conn,
        code=COUNTER_DRIFT_ALERT,
        severity="critical",
        subject=f"bulk:{bulk_operation_id}",
        message="a bulk operation's spent amount differed from the spend ledger and was corrected from it",
        context=context,
    )
    return 1


# ---- unsettled entries (§17.4 `ledger_unsettled`, plan Deviations 5 and 24) --------------------------------

# M3 Task 19 (Codex r1 #31): a `running` eval run is abandoned when no live `eval_run` operation executes it
# and, when none ever did (the CLI's schema dry-run), it started more than `EVAL_RUN_ABANDONED` ago. An
# operation-backed run's health is its operation's claim and heartbeat (the `operations` reconciler): a
# healthy run may take hours.
_EVAL_RUN_OPERATION = (
    "SELECT 1 FROM operations o WHERE o.kind = 'eval_run' "
    "AND CAST(o.payload ->> 'eval_run_id' AS bigint) = e.id"
)
_ABANDONED_EVAL_RUNS = q(
    "SELECT e.id FROM eval_runs e WHERE e.status = 'running' "  # noqa: S608 (a constant fragment)
    f"AND NOT EXISTS ({_EVAL_RUN_OPERATION} AND o.status IN ('pending', 'running')) "
    f"AND (EXISTS ({_EVAL_RUN_OPERATION}) OR e.started_at < :cutoff) ORDER BY e.id"
)
# The candidates (`llm_spend_unsettled` serves the scan), never a batch item's entry (L2979: its submission's
# processing settles it, and a `reserved` one is released by the submission's own end, plan Task 16):
# `reserved` entries older than 15 minutes whose recorded owner does not hold (checked again under its row
# lock), and online `dispatched` entries past `max(15 min, deadline_s + 5 min)`. An owner of an unknown kind
# is a candidate, so `owner_holds` fails loudly instead of leaving it unsettled.
_UNSETTLED = q(
    "SELECT l.id FROM llm_spend_ledger l JOIN llm_calls c ON c.spend_ledger_id = l.id "
    "WHERE l.state IN ('reserved', 'dispatched') AND NOT l.batch AND ("
    "(l.state = 'reserved' AND l.created_at < :reserved_cutoff AND NOT COALESCE("
    "(c.input_refs -> 'owner' ->> 'kind' = 'operation' AND EXISTS (SELECT 1 FROM operations o "
    "WHERE o.id = (c.input_refs -> 'owner' ->> 'id')::bigint AND o.status = 'running' "
    "AND o.claim_id::text = c.input_refs -> 'owner' ->> 'claim_id')) "
    "OR (c.input_refs -> 'owner' ->> 'kind' = 'eval_run' AND EXISTS (SELECT 1 FROM eval_runs e "
    "WHERE e.id = (c.input_refs -> 'owner' ->> 'id')::bigint AND e.status = 'running') "
    "AND (c.input_refs -> 'owner' ->> 'claim_id' IS NULL OR EXISTS (SELECT 1 FROM operations o "
    "WHERE o.id = (c.input_refs -> 'owner' ->> 'operation_id')::bigint AND o.status = 'running' "
    "AND o.claim_id::text = c.input_refs -> 'owner' ->> 'claim_id'))) "
    "OR (c.input_refs -> 'owner' ->> 'kind' = 'target' AND EXISTS (SELECT 1 FROM score_targets t "
    "WHERE t.id = (c.input_refs -> 'owner' ->> 'id')::bigint AND t.state = 'in_progress' "
    "AND t.claim_id::text = c.input_refs -> 'owner' ->> 'claim_id' "
    "AND t.attempt_no = (c.input_refs -> 'owner' ->> 'attempt_no')::integer)) "
    "OR (c.input_refs -> 'owner' ->> 'kind' = 'batch_submission' AND EXISTS (SELECT 1 "
    "FROM batch_submissions s WHERE s.id = (c.input_refs -> 'owner' ->> 'id')::bigint "
    "AND s.state = c.input_refs -> 'owner' ->> 'state')), false)) "
    "OR (l.state = 'dispatched' AND l.dispatched_at + GREATEST(:floor, "
    "make_interval(secs => (c.input_refs ->> 'deadline_s')::double precision) + :grace) < :now)"
    ") ORDER BY l.id LIMIT :limit"
)


async def _fail_abandoned_eval_runs(conn: AsyncConnection, *, now: datetime) -> int:
    """Every abandoned eval run (`_ABANDONED_EVAL_RUNS`: no live `eval_run` operation, and older than
    `EVAL_RUN_ABANDONED` when it never had one) → `failed` with `metrics.failed = "abandoned"` and the
    `cost_usd` its calls count (`operations.fail_eval_run`), so that its unsettled entries fail
    `owner_holds`."""
    ids = [
        int(i)
        for i in (await conn.execute(_ABANDONED_EVAL_RUNS, {"cutoff": now - EVAL_RUN_ABANDONED})).scalars()
    ]
    failed = 0
    for eval_run_id in ids:
        if not await fail_eval_run(conn, eval_run_id, EVAL_RUN_ABANDONED_REASON):
            continue  # it finished meanwhile
        log.warning("eval_run_abandoned", eval_run_id=eval_run_id)
        failed += 1
    return failed


async def _mark_ambiguous(conn: AsyncConnection, entry: _Entry, *, now: datetime) -> None:
    """`dispatched → ambiguous` on both rows; the entry keeps its reservation, so no budget counter changes,
    and a score call's attempt counts it at that reservation (L3911), as an ambiguous settlement does."""
    await transition(conn, "llm_spend_ledger", entry.id, ["dispatched"], "ambiguous")
    await transition(
        conn,
        "llm_calls",
        entry.call_id,
        ["dispatched"],
        "ambiguous",
        set_={"error_code": UNSETTLED_CODE, "ended_at": now, "cost_usd": entry.reserved_usd},
    )
    await _attempt_cost(conn, entry.attempt_id, entry.reserved_usd)


async def _recover(conn: AsyncConnection, ledger_id: int, *, now: datetime) -> int:
    """One candidate in the §9.1 order: `active_config`, the owner row `FOR UPDATE` (a `reserved` entry
    only), the month row, a funded entry's bulk row, the ledger rows, the attempt (an ambiguous score try's
    `cost_usd`). 1 when it was released or marked `ambiguous`."""
    p = await current_params(conn)
    first = await _entry(conn, ledger_id)
    if first is None or first.state not in ("reserved", "dispatched"):
        return 0
    if first.batch:  # never aged (L2979)
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
    elif entry.state == "dispatched" and entry.dispatched_at is not None:
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
    becomes `not_billed('never_dispatched')` (its call `cancelled`, the counters and a funded entry's
    allocation adjusted), and an online `dispatched` entry older than `max(15 min, deadline_s + 5 min)`
    becomes `ambiguous` with its call (plan Deviation 24). A batch item's entry is never aged (L2979). Returns
    the eval runs failed plus the entries recovered. Fenced by the worker epoch first:
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
