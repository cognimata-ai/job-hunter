"""The monthly LLM budget, pure (spec §6.9.1, §6.9.2): the admission rules against the hard cap, the USD
bucket that paces scoring, and the counter table every reservation and settlement applies. `spend_ledger` runs
these functions on the month row it holds `FOR UPDATE`; the property tests drive them directly (§18.1).

**Admission** (§6.9.1). With `A = cap_usd - overrun_buffer_usd` (the buffer is never admitted) and
`reserve_frac = nonscoring_reserve_pct / 100`:
- a non-scoring reservation of `r` is admitted iff `counted_usd + r ≤ A`;
- a scoring one iff `counted_usd + r ≤ A - max(0, A x reserve_frac - nonscoring_counted_usd)`, so scoring
  never uses the unspent part of the non-scoring reserve;
- after `cap_lowered_at`, nothing is admitted while `counted_usd ≥ A`.

`A` always comes from the row (`row.cap_usd`, `row.overrun_buffer_usd`), which a save of class `budget`
changes under its lock; the reserve share, the bucket floor and the collection interval come from the active
settings (`BudgetParams`), read in the same transaction.

**The bucket** (§6.9.2). `rate = A x (1 - reserve_frac) / month_length_s` and
`B_max = max(2 x rate x collection_interval_s, min_bucket_capacity_usd)`; accrual is
`bucket := min(B_max, bucket + rate x (now - bucket_at))`, rounded down to $0.000001 so that the stored
`numeric(12,6)` equals the value computed here. A new month starts full.

**The counters** (§6.9.1 table). Each event changes `counted_usd`, its class's part and, for scoring outside
bulk operations, the bucket by one signed amount (`apply_count`): a reservation `+r`; a settlement
`settlement_delta(outcome, r, s)`, that is `s - r` billed, `-r` not billed, `0` ambiguous, from `dispatched`
and, late, from `ambiguous` alike.

**Cap edits.** Raising the cap takes effect at once. Lowering it below `counted_usd` records `cap_lowered_at`
and `counted_at_cap_change_usd`, so the cap invariant of §7.17 #6 is `counted_usd ≤ max(cap_usd,
counted_at_cap_change_usd)` (`cap_holds`).

**Scoring estimates** (M3). `r_first` is §6.9.4's worst-case reservation of a first generator call, the unit
of the headroom that returns `budget_deferred` targets; `c_hat_followup` is §6.9.3's `ĉ_followup`, the
expected online critic-plus-revise cost of a triggered draft, which a batch submission holds per item.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import TYPE_CHECKING, Final, Literal

from jobhunter.llm import bounds
from jobhunter.llm.pricing import worst_case_usd
from jobhunter.types import JSONValue, get_int, json_get

if TYPE_CHECKING:
    from jobhunter.settings.model import ExecutionConfig, ModelPolicy

Purpose = Literal["generate", "revise", "critic", "extract_profile", "propose_queries", "schema_dry_run"]
PurposeClass = Literal["scoring", "nonscoring"]
SettleOutcome = Literal["billed", "not_billed", "ambiguous"]

# The purposes that may be booked as scoring (the ledger CHECK); every other purpose is non-scoring.
SCORING_PURPOSES: Final[frozenset[Purpose]] = frozenset({"generate", "revise", "critic"})
USD_QUANTUM: Final = Decimal("0.000001")
# `llm.cap_80pct` (§17.6): counted spend at or above this share of the cap.
CAP_ALERT_SHARE: Final = Decimal("0.8")
# §21.4 L6624: the input bound of a score request from its assumed component sizes (rubric 9.0 KB, profile
# 9.5 KB, role 1.3 KB, ad 8.0 KB, output schema 8.0 KB) plus the 1,000-token overhead floor (plan Deviation
# 22: `r_first` re-derives it at the measured overhead).
SIZING_INPUT_BOUND_TOKENS: Final = 36_800
_ZERO: Final = Decimal(0)


def month_of(t: datetime) -> date:
    """The UTC calendar month of `t`, as its first day."""
    if t.tzinfo is None:
        raise ValueError("month_of needs an aware datetime")
    u = t.astimezone(UTC)
    return date(u.year, u.month, 1)


def _first(month: date) -> date:
    if month.day != 1:
        raise ValueError(f"a budget month is the first of its month, got {month.isoformat()}")
    return month


def next_month(month: date) -> date:
    """The first day of the month after `month`."""
    _first(month)
    return date(month.year + month.month // 12, month.month % 12 + 1, 1)


def month_start(month: date) -> datetime:
    """Midnight UTC of the first day of `month`."""
    _first(month)
    return datetime(month.year, month.month, 1, tzinfo=UTC)


def month_length_s(month: date) -> int:
    """The length of `month` in seconds (UTC has no DST)."""
    return int((month_start(next_month(month)) - month_start(month)).total_seconds())


def _usd(doc: JSONValue, path: str) -> Decimal:
    """A money setting: a decimal string (the payload shape of `settings_v1.json`) or an integer."""
    value = json_get(doc, path)
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise TypeError(f"{path}: expected a decimal string, got {type(value).__name__}")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{path}: not a decimal") from exc


@dataclass(frozen=True)
class BudgetParams:
    """The budget settings of one transaction (§13.1 `llm.*`, `schedule.interval_hours`).

    Inside the database, `cap_usd` and `overrun_buffer_usd` come from the locked month row only; these two
    serve the pure layer and the creation of a new month (`spend_ledger.ensure_month`).
    """

    cap_usd: Decimal
    overrun_buffer_usd: Decimal
    nonscoring_reserve_pct: int
    min_bucket_capacity_usd: Decimal
    collection_interval_s: int

    @classmethod
    def from_payload(cls, payload: Mapping[str, JSONValue]) -> "BudgetParams":
        """The params of a settings payload (the shape of `settings_v1.json`)."""
        doc: JSONValue = dict(payload)
        return cls(
            cap_usd=_usd(doc, "llm.cap_usd"),
            overrun_buffer_usd=_usd(doc, "llm.overrun_buffer_usd"),
            nonscoring_reserve_pct=get_int(doc, "llm.nonscoring_reserve_pct"),
            min_bucket_capacity_usd=_usd(doc, "llm.min_bucket_capacity_usd"),
            collection_interval_s=get_int(doc, "schedule.interval_hours") * 3600,
        )


def admissible(p: BudgetParams) -> Decimal:
    """`A = cap_usd - overrun_buffer_usd`, the budget that may be admitted."""
    return p.cap_usd - p.overrun_buffer_usd


def reserve_frac(p: BudgetParams) -> Decimal:
    """The non-scoring reserve as a fraction of `A`."""
    return Decimal(p.nonscoring_reserve_pct) / 100


def scoring_share(p: BudgetParams) -> Decimal:
    """`A x (1 - reserve_frac)`: the month's share of `A` that scoring may spend at an even pace, $93.10 with
    the defaults (the bucket's rate over the month, and the header status's pace line)."""
    return admissible(p) * (1 - reserve_frac(p))


def rate_per_s(p: BudgetParams, month: date) -> Decimal:
    """The bucket's accrual in USD per second: the scoring share of `A` spread over the month."""
    return scoring_share(p) / month_length_s(month)


def bucket_capacity(p: BudgetParams, month: date) -> Decimal:
    """`B_max = max(2 x rate x collection_interval_s, min_bucket_capacity_usd)`."""
    return max(2 * rate_per_s(p, month) * p.collection_interval_s, p.min_bucket_capacity_usd)


def _seconds(span: timedelta) -> Decimal:
    """`span` in seconds, exactly (microseconds included)."""
    return Decimal(span.days * 86_400 + span.seconds) + Decimal(span.microseconds) / 1_000_000


def accrue(bucket_usd: Decimal, bucket_at: datetime, now: datetime, p: BudgetParams, month: date) -> Decimal:
    """`min(B_max, bucket_usd + rate x (now - bucket_at))`, rounded down to $0.000001. A `now` before
    `bucket_at` accrues nothing; a bucket above `B_max` (a lowered cap) comes down to it."""
    elapsed = max(_ZERO, _seconds(now - bucket_at))
    accrued = min(bucket_capacity(p, month), bucket_usd + rate_per_s(p, month) * elapsed)
    return accrued.quantize(USD_QUANTUM, rounding=ROUND_FLOOR)


@dataclass
class BudgetRow:
    """One `llm_budget` row (§7.11), every column."""

    month: date
    cap_usd: Decimal
    overrun_buffer_usd: Decimal
    counted_usd: Decimal
    scoring_counted_usd: Decimal
    nonscoring_counted_usd: Decimal
    bucket_usd: Decimal
    bucket_at: datetime
    cap_lowered_at: datetime | None = None
    counted_at_cap_change_usd: Decimal | None = None
    cap_reached_alerted_at: datetime | None = None
    updated_at: datetime | None = None


def new_row(month: date, p: BudgetParams, now: datetime) -> BudgetRow:
    """A new month's row: the cap and buffer of `p`, nothing counted, the bucket full (§6.9.2)."""
    return BudgetRow(
        month=_first(month),
        cap_usd=p.cap_usd,
        overrun_buffer_usd=p.overrun_buffer_usd,
        counted_usd=_ZERO,
        scoring_counted_usd=_ZERO,
        nonscoring_counted_usd=_ZERO,
        bucket_usd=bucket_capacity(p, month).quantize(USD_QUANTUM, rounding=ROUND_FLOOR),
        bucket_at=now,
        updated_at=now,
    )


def row_params(row: BudgetRow, p: BudgetParams) -> BudgetParams:
    """`p` with the row's own cap and buffer: what admission and accrual on `row` use."""
    return replace(p, cap_usd=row.cap_usd, overrun_buffer_usd=row.overrun_buffer_usd)


def accrue_row(row: BudgetRow, p: BudgetParams, now: datetime) -> None:
    """Accrue `row`'s bucket to `now` in place (`bucket_at` never moves back)."""
    row.bucket_usd = accrue(row.bucket_usd, row.bucket_at, now, row_params(row, p), row.month)
    row.bucket_at = max(row.bucket_at, now)


def blocked(row: BudgetRow) -> bool:
    """A cap lowered below the counted spend admits nothing while `counted_usd ≥ A` (§6.9.1)."""
    return row.cap_lowered_at is not None and row.counted_usd >= row.cap_usd - row.overrun_buffer_usd


def admission_limit(row: BudgetRow, purpose_class: PurposeClass, p: BudgetParams) -> Decimal:
    """The most `counted_usd + r` may reach for a reservation of `purpose_class` on `row`."""
    a = row.cap_usd - row.overrun_buffer_usd
    if purpose_class == "nonscoring":
        return a
    return a - max(_ZERO, a * reserve_frac(p) - row.nonscoring_counted_usd)


def admits(row: BudgetRow, r: Decimal, purpose_class: PurposeClass, p: BudgetParams) -> bool:
    """Whether a reservation of `r` passes the §6.9.1 hard-cap check on `row`."""
    return not blocked(row) and row.counted_usd + r <= admission_limit(row, purpose_class, p)


def headroom(row: BudgetRow, purpose_class: PurposeClass, p: BudgetParams) -> Decimal:
    """The largest reservation of `purpose_class` that `row` still admits (0 when none); for scoring this is
    §6.9.4's scoring headroom, `A` minus `counted_usd` minus the unspent non-scoring reserve."""
    if blocked(row):
        return _ZERO
    return max(_ZERO, admission_limit(row, purpose_class, p) - row.counted_usd)


def apply_count(row: BudgetRow, delta: Decimal, purpose_class: PurposeClass, *, bucket: bool) -> None:
    """One row of the §6.9.1 counter table: `counted_usd` and the class's part change by `delta`, and the
    bucket (`bucket`: scoring outside bulk operations) by `-delta`."""
    row.counted_usd += delta
    if purpose_class == "scoring":
        row.scoring_counted_usd += delta
    else:
        row.nonscoring_counted_usd += delta
    if bucket:
        row.bucket_usd -= delta


def settlement_delta(outcome: SettleOutcome, reserved_usd: Decimal, settled_usd: Decimal | None) -> Decimal:
    """The counter change of settling an entry that counts `reserved_usd` (it is `dispatched`, or `ambiguous`
    and settled late): `settled - reserved` billed, `-reserved` not billed, `0` ambiguous."""
    if outcome == "billed":
        if settled_usd is None:
            raise ValueError("a billed settlement needs its settled cost")
        return settled_usd - reserved_usd
    if outcome == "not_billed":
        return -reserved_usd
    return _ZERO


def change_cap(row: BudgetRow, cap_usd: Decimal, overrun_buffer_usd: Decimal, now: datetime) -> None:
    """A cap edit on `row` in place (§6.9.1): a cap below `counted_usd` records the time and the counted spend
    of the change; any other edit leaves both as they were."""
    row.cap_usd, row.overrun_buffer_usd = cap_usd, overrun_buffer_usd
    if cap_usd < row.counted_usd:
        row.cap_lowered_at, row.counted_at_cap_change_usd = now, row.counted_usd


def cap_holds(row: BudgetRow) -> bool:
    """§7.17 #6: `counted_usd ≤ max(cap_usd, counted_at_cap_change_usd)`."""
    ceiling = row.cap_usd
    if row.counted_at_cap_change_usd is not None:
        ceiling = max(ceiling, row.counted_at_cap_change_usd)
    return row.counted_usd <= ceiling


def cap_share_reached(row: BudgetRow) -> bool:
    """`llm.cap_80pct` (§17.6): counted spend at or above 80% of a non-zero cap."""
    return row.cap_usd > 0 and row.counted_usd >= CAP_ALERT_SHARE * row.cap_usd


def r_first(model_policy: "ModelPolicy", execution: "ExecutionConfig") -> Decimal:
    """§6.9.4's `r_first`: the worst-case reservation of a first generator call, on the policy's generate
    model at the execution config's first stage, over the §21.4 input bound re-derived from the current
    overhead,
    `PROMPT_OVERHEAD_TOKENS[model] + 36,800 - 1,000` (plan Deviation 22): the component sizes of L6624 with
    the measured overhead in place of the floor they were sized with."""
    model = model_policy.nodes.generate.model
    bound = bounds.PROMPT_OVERHEAD_TOKENS[model] + SIZING_INPUT_BOUND_TOKENS - bounds.PROMPT_OVERHEAD_FLOOR
    return worst_case_usd(model, input_tokens_bound=bound, max_tokens=execution.generate.max_tokens)


def c_hat_followup(model_policy: "ModelPolicy") -> Decimal:
    """§6.9.3's `ĉ_followup`: the expected online critic-plus-revise cost of a triggered draft under
    `model_policy`, `(first_round + follow_ups x full_critic) + follow_ups x revision`, that is the critic and
    revise parts of `llm_projection.target_cost` divided by the funnel's `first_round_rate`, rounded half-up
    to $0.000001 ($0.066939 for policy B, the §21.4 figure)."""
    # Imported here: the projection imports this module (its bucket and admission).
    from jobhunter.settings import llm_projection

    cost = llm_projection.target_cost(model_policy)
    rate = llm_projection.funnel(model_policy).first_round_rate
    return ((cost.critic + cost.revise) / rate).quantize(USD_QUANTUM, rounding=ROUND_HALF_UP)
