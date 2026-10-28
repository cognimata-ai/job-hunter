"""LLM spend properties (spec §18.1): the monthly budget row and the USD bucket over the pure layer.

`SpendMachine` drives the counter arithmetic exactly as `spend_ledger` applies it on the locked month row
(`budget.new_row` for `ensure_month`, `budget.accrue_row` for `lock_month`, `budget.admits` and
`budget.apply_count` for a reservation, `budget.settlement_delta` for a settlement, `budget.change_cap` for a
cap edit) against an in-memory ledger: reservations, the three settlements (late ones from `ambiguous`
included), releases, cap edits in both directions, time and the month boundary. M3 Task 17 adds the batch
and bulk events (L5695): a bulk operation's batch submission reserves its items at once, each admitted against
the hard cap and all of them against the operation's allocation (`spent + held + Σ r + n x hold ≤ budget`,
L1189), funded by the allocation instead of the bucket, with its follow-up holds; its items are dispatched
at submit-start, settle `billed` or `not_billed` from their results, or `ambiguous` (missing, or the user's
release) and later `billed | not_billed` once the batch is found (§7.16.7), and each item's hold is
released once. Its invariants are §7.17 #6: the cap invariant of every month, the admission audit of every
ledger row (and of every batch against its allocation), the counters equal to the ledger sums of the §7.11
formula, the operation's `spent_usd` equal to the counted amounts of the entries it funds, and its
`followup_held_usd` equal to its items' unreleased holds.

`BucketMachine` paces an unbounded scoring backlog with the §6.9.2 cycle rules (trigger
`bucket ≥ min(min_cycle_targets x ĉ, B_max)`, `S = min(max_targets_per_cycle, floor(bucket / ĉ))`) under any
tick pattern, with actual costs anywhere up to the reservation: the scoring spend at time `t` of the month
never exceeds `t/month x scoring share + B_max + one cycle's reservations`. The Fable B1 scenario (a 1,180
target backlog) follows as a fixed simulation.
"""

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule
from jobhunter.llm import budget
from jobhunter.llm.budget import BudgetParams, BudgetRow, PurposeClass

START = datetime(2026, 10, 1, tzinfo=UTC)
P = BudgetParams(
    cap_usd=Decimal("100.00"),
    overrun_buffer_usd=Decimal("2.00"),
    nonscoring_reserve_pct=5,
    min_bucket_capacity_usd=Decimal("1.00"),
    collection_interval_s=6 * 3600,
)
UNSETTLED = ("reserved", "dispatched", "ambiguous")
MAX_TARGETS_PER_CYCLE = 60  # §13.1 defaults
MIN_CYCLE_TARGETS = 10

cents = st.integers(0, 20_000).map(lambda n: Decimal(n) / 100)  # $0.00 to $200.00, the numeric(10,2) of a cap
micro_usd = st.integers(1, 5_000_000).map(lambda n: Decimal(n) / 1_000_000)  # a reservation, $0.000001 to $5
fractions = st.integers(0, 1_000).map(lambda n: Decimal(n) / 1_000)


@dataclass
class Entry:
    """A ledger row as the machine sees it, with the buffer in force when it was admitted."""

    month: date
    purpose_class: PurposeClass
    reserved: Decimal
    admitted_cap: Decimal
    admitted_buffer: Decimal
    counted_before: Decimal
    state: str = "reserved"
    settled: Decimal | None = None
    funded: bool = False  # a batch item of the bulk operation: its allocation, not the bucket (§6.9.3)

    @property
    def counted(self) -> Decimal:
        """What the entry counts at (§7.11): billed its cost, unsettled its reservation, not billed 0."""
        if self.state == "billed":
            return self.settled or Decimal(0)
        return self.reserved if self.state in UNSETTLED else Decimal(0)


class SpendMachine(RuleBasedStateMachine):
    """The counters, the cap invariant and the admission audit (§6.9.1, §7.17 #6), over months and edits."""

    @initialize(cap=cents, buffer=st.integers(0, 500).map(lambda n: Decimal(n) / 100), pct=st.integers(0, 20))
    def start(self, cap: Decimal, buffer: Decimal, pct: int) -> None:
        self.p = replace(P, cap_usd=cap, overrun_buffer_usd=buffer, nonscoring_reserve_pct=pct)
        self.now = START
        self.rows: dict[date, BudgetRow] = {}
        self.entries: list[Entry] = []
        # the bulk operation (§6.9.3): its fixed budget, its spent and held accounts, its items' holds
        self.bulk_budget = Decimal("40.00")
        self.bulk_spent = Decimal(0)
        self.bulk_held = Decimal(0)
        self.holds: list[tuple[Decimal, bool]] = []  # (hold, released)
        self.batch_admissions: list[
            tuple[Decimal, Decimal, Decimal, Decimal]
        ] = []  # spent, held, Σ r, n x hold

    def _month_row(self) -> BudgetRow:
        """`ensure_month` for the month of `now`, from the current params (a new month starts full)."""
        month = budget.month_of(self.now)
        if month not in self.rows:
            self.rows[month] = budget.new_row(month, self.p, self.now)
        return self.rows[month]

    def _locked(self, month: date) -> BudgetRow:
        """`lock_month`: the row accrued to `now` with the row's own cap and buffer."""
        row = self.rows[month]
        budget.accrue_row(row, self.p, self.now)
        return row

    def _in(self, *states: str) -> list[int]:
        return [i for i, e in enumerate(self.entries) if e.state in states]

    def _settle(self, i: int, outcome: budget.SettleOutcome, settled: Decimal | None) -> None:
        e = self.entries[i]
        row = self._locked(e.month)  # the reservation's month, even after the rollover
        delta = budget.settlement_delta(outcome, e.reserved, settled)
        budget.apply_count(row, delta, e.purpose_class, bucket=e.purpose_class == "scoring" and not e.funded)
        if e.funded:
            self.bulk_spent += delta  # the allocation it debited (L1191), in full
        e.state = outcome
        e.settled = settled if outcome == "billed" else Decimal(0) if outcome == "not_billed" else None

    @rule(purpose_class=st.sampled_from(["scoring", "nonscoring"]), r=micro_usd)
    def reserve(self, purpose_class: PurposeClass, r: Decimal) -> None:
        row = self._month_row()
        budget.accrue_row(row, self.p, self.now)
        if not budget.admits(row, r, purpose_class, self.p):
            return
        self.entries.append(
            Entry(row.month, purpose_class, r, row.cap_usd, row.overrun_buffer_usd, row.counted_usd)
        )
        budget.apply_count(row, r, purpose_class, bucket=purpose_class == "scoring")

    @precondition(lambda self: bool(self._in("reserved")))
    @rule(data=st.data())
    def dispatch(self, data: st.DataObject) -> None:
        self.entries[data.draw(st.sampled_from(self._in("reserved")))].state = "dispatched"

    @precondition(lambda self: bool(self._in("reserved")))
    @rule(data=st.data())
    def release_never_dispatched(self, data: st.DataObject) -> None:
        i = data.draw(st.sampled_from(self._in("reserved")))
        row = self._locked(self.entries[i].month)
        e = self.entries[i]
        budget.apply_count(
            row, -e.reserved, e.purpose_class, bucket=e.purpose_class == "scoring" and not e.funded
        )
        if e.funded:  # an abandoned batch's item credits the allocation it debited
            self.bulk_spent -= e.reserved
        e.state, e.settled = "not_billed", Decimal(0)

    @precondition(lambda self: bool(self._in("dispatched", "ambiguous")))
    @rule(data=st.data(), fraction=fractions)
    def settle_billed(self, data: st.DataObject, fraction: Decimal) -> None:
        """At most the reservation: the bound holds (§1.5 S3); a violation is a defect with its own alert."""
        i = data.draw(st.sampled_from(self._in("dispatched", "ambiguous")))
        settled = (self.entries[i].reserved * fraction).quantize(budget.USD_QUANTUM, rounding=ROUND_FLOOR)
        self._settle(i, "billed", settled)

    @precondition(lambda self: bool(self._in("dispatched", "ambiguous")))
    @rule(data=st.data())
    def settle_not_billed(self, data: st.DataObject) -> None:
        self._settle(data.draw(st.sampled_from(self._in("dispatched", "ambiguous"))), "not_billed", None)

    @precondition(lambda self: bool(self._in("dispatched")))
    @rule(data=st.data())
    def settle_ambiguous(self, data: st.DataObject) -> None:
        self._settle(data.draw(st.sampled_from(self._in("dispatched"))), "ambiguous", None)

    @rule(
        n=st.integers(1, 8), r=micro_usd, hold=st.integers(0, 100_000).map(lambda n: Decimal(n) / 1_000_000)
    )
    def reserve_batch(self, n: int, r: Decimal, hold: Decimal) -> None:
        """A batch submission (`spend_ledger.reserve_batch`): under the month row and the bulk row, the
        allocation check of L1189 first, then each item admitted against the hard cap; a refusal of either
        writes nothing (the preparation's savepoint)."""
        row = self._month_row()
        budget.accrue_row(row, self.p, self.now)
        if self.bulk_spent + self.bulk_held + n * r + n * hold > self.bulk_budget:
            return
        trial = replace(row)
        for _ in range(n):
            if not budget.admits(trial, r, "scoring", self.p):
                return
            budget.apply_count(trial, r, "scoring", bucket=False)
        self.batch_admissions.append((self.bulk_spent, self.bulk_held, n * r, n * hold))
        for _ in range(n):
            self.entries.append(
                Entry(
                    row.month, "scoring", r, row.cap_usd, row.overrun_buffer_usd, row.counted_usd, funded=True
                )
            )
            budget.apply_count(row, r, "scoring", bucket=False)  # the allocation, never the bucket
        self.bulk_spent += n * r
        self.bulk_held += n * hold
        self.holds.extend([(hold, False)] * n)

    @precondition(lambda self: any(not released for _, released in self.holds))
    @rule(data=st.data())
    def release_item_hold(self, data: st.DataObject) -> None:
        """An item decided (anything but a follow-up), or its follow-up ended: its hold given back once,
        never below 0 (`bulk.release_item_hold`)."""
        i = data.draw(st.sampled_from([i for i, (_, released) in enumerate(self.holds) if not released]))
        hold, _ = self.holds[i]
        self.holds[i] = (hold, True)
        self.bulk_held = max(Decimal(0), self.bulk_held - hold)

    @precondition(lambda self: any(e.funded and e.state == "dispatched" for e in self.entries))
    @rule()
    def release_submission(self) -> None:
        """The user's release of a quarantined submission (`batches.release`): every dispatched item
        `ambiguous`, counted at its reservation (§7.16.7)."""
        for i, e in enumerate(self.entries):
            if e.funded and e.state == "dispatched":
                self._settle(i, "ambiguous", None)

    @rule(cap=cents, buffer=st.integers(0, 500).map(lambda n: Decimal(n) / 100))
    def edit_cap(self, cap: Decimal, buffer: Decimal) -> None:
        """A save of class `budget`: the settings change, and the current month's row is changed under its
        lock (`ensure_month` from the new values, then `change_cap`)."""
        self.p = replace(self.p, cap_usd=cap, overrun_buffer_usd=buffer)
        budget.change_cap(self._month_row(), cap, buffer, self.now)

    @rule(minutes=st.integers(1, 7 * 24 * 60))
    def advance(self, minutes: int) -> None:
        self.now += timedelta(minutes=minutes)

    @rule(seconds=st.integers(0, 120))
    def month_boundary(self, seconds: int) -> None:
        nxt = budget.next_month(budget.month_of(self.now))
        self.now = datetime(nxt.year, nxt.month, 1, tzinfo=UTC) + timedelta(seconds=seconds)

    @invariant()
    def counted_never_exceeds_the_cap(self) -> None:
        for row in self.rows.values():
            assert budget.cap_holds(row), row  # counted ≤ max(cap, counted_at_cap_change)

    @invariant()
    def every_admission_fits_its_cap(self) -> None:
        for e in self.entries:
            assert e.counted_before + e.reserved <= e.admitted_cap - e.admitted_buffer, e

    @invariant()
    def counters_equal_the_ledger(self) -> None:
        for month, row in self.rows.items():
            sums: dict[PurposeClass, Decimal] = {"scoring": Decimal(0), "nonscoring": Decimal(0)}
            for e in self.entries:
                if e.month == month:
                    amount = e.settled if e.state == "billed" else e.reserved if e.state in UNSETTLED else 0
                    assert amount is not None
                    sums[e.purpose_class] += amount
            assert (row.scoring_counted_usd, row.nonscoring_counted_usd) == (
                sums["scoring"],
                sums["nonscoring"],
            )
            assert row.counted_usd == sums["scoring"] + sums["nonscoring"]

    @invariant()
    def the_allocation_equals_its_funded_entries(self) -> None:
        """The bulk operation's `spent_usd` is the counted amount of the entries it funds (every settlement
        moves both by the same delta), and its `followup_held_usd` its items' unreleased holds."""
        assert self.bulk_spent == sum((e.counted for e in self.entries if e.funded), Decimal(0))
        assert self.bulk_held == sum((hold for hold, released in self.holds if not released), Decimal(0))
        assert self.bulk_held >= 0

    @invariant()
    def every_batch_fit_its_allocation(self) -> None:
        for spent, held, worst, holds in self.batch_admissions:
            assert spent + held + worst + holds <= self.bulk_budget


TestSpendMachine = SpendMachine.TestCase
TestSpendMachine.settings = settings(max_examples=300, stateful_step_count=60, deadline=None)


def _cycle_due(row: BudgetRow, c_hat: Decimal, p: BudgetParams) -> bool:
    """§3.4 step 6 / §6.9.2: a periodic cycle starts when `bucket ≥ min(min_cycle_targets x ĉ, B_max)`."""
    return row.bucket_usd >= min(MIN_CYCLE_TARGETS * c_hat, budget.bucket_capacity(p, row.month))


def _cycle_size(row: BudgetRow, c_hat: Decimal) -> int:
    """§6.9.2: `S = min(max_targets_per_cycle, floor(bucket / ĉ))`."""
    return min(MAX_TARGETS_PER_CYCLE, int(row.bucket_usd // c_hat))


def _start_cycle(row: BudgetRow, p: BudgetParams, c_hat: Decimal, r: Decimal, backlog: int) -> list[Decimal]:
    """Admit up to `S` targets of `backlog`, each reserving `r` (scoring outside bulk operations)."""
    admitted: list[Decimal] = []
    for _ in range(min(_cycle_size(row, c_hat), backlog)):
        if not budget.admits(row, r, "scoring", p):
            break
        budget.apply_count(row, r, "scoring", bucket=True)
        admitted.append(r)
    return admitted


class BucketMachine(RuleBasedStateMachine):
    """§6.9.2 / §18.1: an unbounded scoring backlog is paced by the bucket under any tick pattern."""

    @initialize(
        interval_h=st.sampled_from([1, 2, 6, 12, 24, 168]),
        c_hat=st.integers(5_000, 200_000).map(lambda n: Decimal(n) / 1_000_000),
        worst_factor=st.integers(10, 40).map(lambda n: Decimal(n) / 10),
    )
    def start(self, interval_h: int, c_hat: Decimal, worst_factor: Decimal) -> None:
        self.p = replace(P, collection_interval_s=interval_h * 3600)
        self.now = START
        self.end = START + timedelta(seconds=budget.month_length_s(budget.month_of(START)))
        self.row = budget.new_row(budget.month_of(START), self.p, START)
        self.c_hat = c_hat
        self.r = (c_hat * worst_factor).quantize(budget.USD_QUANTUM)  # the worst case reserved per target
        self.in_flight: list[Decimal] = []
        self.last_cycle_reserved = Decimal(0)

    @precondition(lambda self: self.now < self.end)
    @rule(minutes=st.integers(1, 12 * 60))
    def tick(self, minutes: int) -> None:
        """A tick: accrual under the month lock, then the cycle trigger when no cycle is running."""
        self.now = min(self.end, self.now + timedelta(minutes=minutes))
        budget.accrue_row(self.row, self.p, self.now)
        if not self.in_flight and _cycle_due(self.row, self.c_hat, self.p):
            self.in_flight = _start_cycle(self.row, self.p, self.c_hat, self.r, backlog=10**9)
            self.last_cycle_reserved = sum(self.in_flight, Decimal(0))

    @precondition(lambda self: bool(self.in_flight))
    @rule(fraction=fractions, spread=st.booleans())
    def cycle_settles(self, fraction: Decimal, spread: bool) -> None:
        """The running cycle's targets settle anywhere between free and their whole reservation: all at
        `fraction` of it, or (`spread`) alternately at `fraction` and at the whole reservation."""
        budget.accrue_row(self.row, self.p, self.now)
        for n, r in enumerate(self.in_flight):
            share = Decimal(1) if spread and n % 2 else fraction
            s = (r * share).quantize(budget.USD_QUANTUM, rounding=ROUND_FLOOR)
            budget.apply_count(self.row, budget.settlement_delta("billed", r, s), "scoring", bucket=True)
        self.in_flight = []

    @invariant()
    def scoring_spend_follows_the_month(self) -> None:
        elapsed = Decimal((self.now - START).total_seconds())
        bound = (
            budget.rate_per_s(self.p, self.row.month) * elapsed
            + budget.bucket_capacity(self.p, self.row.month)
            + self.last_cycle_reserved
        )
        assert self.row.scoring_counted_usd <= bound


TestBucketMachine = BucketMachine.TestCase
TestBucketMachine.settings = settings(max_examples=200, stateful_step_count=100, deadline=None)


def test_a_1180_target_backlog_does_not_exhaust_the_month_in_its_first_days() -> None:
    """Fable B1 (spec review): a profile confirmed with 1,180 retained ads, every one online-pending, with
    ticks every minute and each cycle settling at the next tick. The per-cycle allowance of the reviewed
    draft spent the $100 cap on day 1; the bucket keeps three days at the month's pace."""
    c_hat, r = Decimal("0.0576"), Decimal("0.0825")  # §21.2 config B per target; a generator worst case
    row = budget.new_row(budget.month_of(START), P, START)
    backlog, in_flight, cycles, now = 1180, list[Decimal](), 0, START
    while now < START + timedelta(days=3):
        now += timedelta(minutes=1)
        budget.accrue_row(row, P, now)
        for reserved in in_flight:  # last tick's cycle settles at ĉ
            budget.apply_count(
                row, budget.settlement_delta("billed", reserved, c_hat), "scoring", bucket=True
            )
        in_flight = []
        if _cycle_due(row, c_hat, P):
            in_flight = _start_cycle(row, P, c_hat, r, backlog)
            backlog -= len(in_flight)
            cycles += 1
    month_s = Decimal(budget.month_length_s(row.month))
    scoring_share = budget.admissible(P) * Decimal("0.95")  # $93.10
    pace = Decimal(3 * 86400) / month_s * scoring_share  # ≈ $9.01 in three days
    assert row.scoring_counted_usd <= pace + budget.bucket_capacity(P, row.month) + MAX_TARGETS_PER_CYCLE * r
    assert row.scoring_counted_usd < Decimal("0.15") * scoring_share  # far from the month's share
    assert backlog > 900  # the backlog drains at the accrual rate, not in a day
    assert 10 <= cycles <= 30  # a cycle about every 4-7 h at the 6 h interval, not every few minutes
