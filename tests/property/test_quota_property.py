"""Quota properties (spec §18.1): the ledger arithmetic of §6 over the pure functions, with an in-memory
ledger.

`QuotaMachine` drives one provider (JSearch or Adzuna) through the authoritative steps of `ledger.reserve` and
`ledger.settle` as pure functions (`roll_forward`, `ledger.admission` = accrue + decide + the admission's
bookkeeping, `ledger.settle_window` = `apply_observation_pure` + the probe outcome, `ledger.adzuna_admits`)
and the recovery writes (`note_probe_outcome`), against a provider that knows its own truth (its reset time,
its limit and the units it charged). Every observation is generated from that truth.

`HourMachine` is Q-SH1 (downgraded by chair decision C2): `ledger.serpapi_hour_admits` and the foreign-usage
detection of §6.5 against an in-memory SerpApi account whose `this_hour` sees the app's own searches up to 120
s late, and sees another user's searches (when injected) at once.
"""

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule
from jobhunter.quota import ledger
from jobhunter.quota.pacing import Admit, QuotaClass, effective_reserve, fair_share, per_run_cap
from jobhunter.quota.windows import (
    NOMINAL_PERIOD,
    PROBE_RETRY,
    Close,
    Insert,
    ObservationEffects,
    Provider,
    Window,
    note_probe_outcome,
    roll_forward,
)
from jobhunter.run_settings import QuotaSettings
from jobhunter.sources.base import Observation

from tests.support.quota import SETTINGS

START = datetime(2026, 1, 1, tzinfo=UTC)
RECOVERY = ledger.RECOVERY_AFTER
SPANS = ledger.ADZUNA_SPANS
Settlement = Literal[
    "ok", "ok_bare", "gateway_404", "connect", "lost", "rate_limited", "rate_limited_bare", "server_error"
]
# Drawn outcomes of a sent request; a served 2xx with headers is the most common one.
OUTCOMES: list[Settlement] = [
    "ok",
    "ok",
    "ok",
    "ok_bare",
    "gateway_404",
    "connect",
    "lost",
    "rate_limited",
    "rate_limited_bare",
    "server_error",
]
# The outcomes that decide a probe's fate: validated, failed (with or without headers), lost, never sent.
PROBE_OUTCOMES: list[Settlement] = ["ok", "ok_bare", "rate_limited", "rate_limited_bare", "lost", "connect"]
Settled = Literal["billed", "not_billed", "ambiguous"]


@dataclass
class Entry:
    window_start: datetime
    request_class: QuotaClass
    state: str
    created_at: datetime
    run: int
    dispatched_at: datetime | None = None
    units: int = 1


class QuotaMachine(RuleBasedStateMachine):
    """Counted usage never exceeds the limit, observations never loosen the ledger, Adzuna's rolling caps
    hold, and the bucket never starves a normal request for longer than one token's accrual.

    Limits and caps are drawn small, so that a run of a few dozen steps reaches them.
    """

    @initialize(
        provider=st.sampled_from(["jsearch", "adzuna"]),
        interval_s=st.sampled_from([3600, 6 * 3600, 86400, 7 * 86400]) | st.integers(3600, 7 * 86400),
        limit=st.integers(3, 12),
        reserve_pct=st.integers(0, 30),
        caps=st.tuples(st.integers(1, 4), st.integers(4, 12), st.integers(12, 30), st.integers(30, 60)),
        reset_offset=st.integers(3600, 2_675_242),
    )
    def setup(
        self,
        provider: Provider,
        interval_s: int,
        limit: int,
        reserve_pct: int,
        caps: tuple[int, int, int, int],
        reset_offset: int,
    ) -> None:
        minute, day, week, month = caps
        self.settings = QuotaSettings(limit, 250, reserve_pct, 45, minute, day, week, month, "auto")
        self.caps = caps
        self.provider, self.interval_s, self.now = provider, interval_s, START
        self.windows: dict[datetime, Window] = {}
        self.current: Window | None = None
        self.entries: list[Entry] = []
        self.adjustments: defaultdict[datetime, int] = defaultdict(int)
        self.floor: dict[datetime, int] = {}  # counted usage when a window's limit was last lowered
        self.run = 1
        self.last_take: datetime | None = None  # the bucket's last token taken (None: full or reshaped)
        # The provider's truth (JSearch): its own period end, its limit and the units it charged since.
        self.p_reset = START + timedelta(seconds=reset_offset)
        self.p_limit = limit
        self.p_used = 0
        self.last_observed: datetime | None = None
        # When a window's next probe is due, per window: a probe that did not validate it is retried.
        self.reprobe_due: dict[datetime, datetime] = {}

    # -- helpers -------------------------------------------------------------------------------------------
    def counted(self, ws: datetime) -> int:
        requests = sum(e.units for e in self.entries if e.window_start == ws and e.state in ledger.COUNTED)
        return requests + self.adjustments[ws]

    def _advance(self, by: timedelta) -> None:
        self.now += by
        while self.now >= self.p_reset:  # the provider resets on its own schedule, observed early or late
            self.p_reset += NOMINAL_PERIOD["jsearch"]
            self.p_used = 0

    def _roll(self) -> Window:
        w, changes = roll_forward(
            self.current, self.provider, self.now, self.settings, interval_s=self.interval_s
        )
        assert w is not None
        self._apply(changes)
        if changes:
            self.last_take = None  # a new window starts with a full bucket
        self.current = self.windows[w.window_start] = w
        return w

    def _apply(self, changes: list[Close | Insert]) -> None:
        for change in changes:
            self.windows[change.window.window_start] = change.window

    def _rolling(self) -> list[int]:
        return [
            sum(e.units for e in self.entries if e.state in ledger.COUNTED and e.created_at > self.now - span)
            for span in SPANS
        ]

    def _observation(self, observed_at: datetime, status: int) -> Observation:
        return Observation(
            observed_at=observed_at,
            status=status,
            reset_at=self.p_reset,
            limit=self.p_limit,
            remaining=max(0, self.p_limit - self.p_used),
        )

    def _reserve(self, request_class: QuotaClass) -> Entry | None:
        w = self._roll()
        used = self.counted(w.window_start)
        run_normal = sum(
            e.units
            for e in self.entries
            if e.run == self.run and e.request_class == "normal" and e.state in ledger.COUNTED
        )
        d = ledger.admission(
            w,
            provider=self.provider,
            request_class=request_class,
            units=1,
            used=used,
            run_normal_count=run_normal,
            hour_ok=True,
            adzuna_ok=self.provider != "adzuna" or ledger.adzuna_admits(self._rolling(), 1, self.settings),
            now=self.now,
            settings=self.settings,
            interval_s=self.interval_s,
        )
        if isinstance(d, Admit):
            assert used + 1 <= w.limit_units
            if d.request_class == "normal":  # only retries and probes may use the reserve
                assert used + 1 <= w.limit_units - effective_reserve(w, self.now)
            entry = Entry(w.window_start, d.request_class, "reserved", self.now, self.run)
            self.entries.append(entry)
            if d.request_class == "probe":
                self.reprobe_due.pop(w.window_start, None)
            if d.tokens_used:
                self.last_take = self.now
            return entry
        in_flight = any(
            e.window_start == w.window_start
            and e.request_class == "probe"
            and e.state in ("reserved", "dispatched")
            for e in self.entries
        )
        if d.code == "awaiting_window_validation":  # never a lockout: a probe is in flight, or due later
            assert in_flight or (
                w.probe_failed and w.next_probe_at is not None and self.now < w.next_probe_at
            )
        due = self.reprobe_due.get(w.window_start)
        if d.code in ("exhausted", "awaiting_window_validation") and due is not None and self.now >= due:
            # a probe that failed (or was never sent) is retried when due, not only when the window ends
            assert in_flight or used + 1 > w.limit_units, "the window's next probe is not admitted"
        cap = per_run_cap(self.settings, fair_share(w, self.interval_s))
        bucket = d.code == "paced" and (self.provider == "adzuna" or run_normal + 1 <= cap)
        if bucket and self.last_take is not None:
            # one token accrues in length / N seconds; 5 % covers the rounding down of every accrual
            one_token = (w.window_end - w.window_start) / (w.limit_units - w.reserve_units)
            assert self.now - self.last_take < one_token * 1.05, "a normal request starved past one token"
        return None

    def _pick(self, data: st.DataObject, state: str) -> Entry | None:
        candidates = [e for e in self.entries if e.state == state]
        return data.draw(st.sampled_from(candidates)) if candidates else None

    def _settle(self, e: Entry, outcome: Settlement, *, late: bool, charged: bool) -> None:
        state, status, obs = self._classify(e, outcome, late, charged)
        e.state = state
        cur = self._roll()
        used = self.counted(cur.window_start)  # after the entry's own state change
        entry_units = e.units if state in ledger.COUNTED else 0
        before = {ws: self.counted(ws) for ws in self.windows}
        probe = e.request_class == "probe" and e.window_start == cur.window_start
        w, changes, fx = ledger.settle_window(
            cur,
            obs,
            self.settings,
            provider=self.provider,
            state=state,
            http_status=status,
            probe=probe,
            used_units=used,
            entry_units=entry_units,
            interval_s=self.interval_s,
            now=self.now,
        )
        self._apply(changes)
        if changes or (w.limit_units, w.reserve_units, w.window_end) != (
            cur.limit_units,
            cur.reserve_units,
            cur.window_end,
        ):
            self.last_take = None  # the bucket's shape changed: the starvation clock restarts
        self.current = self.windows[w.window_start] = w
        if w.state == "validated":
            self.reprobe_due.pop(w.window_start, None)
        elif probe:
            # §6.3: a probe that ended without validating is failed (next one 12 h later), or never sent
            never_sent = state == "not_billed" and status is None
            self.reprobe_due[w.window_start] = self.now if never_sent else self.now + PROBE_RETRY
        source = e.window_start
        if fx.reattribute:
            e.window_start = w.window_start
        elif w.limit_units < cur.limit_units:
            self.floor[w.window_start] = max(self.floor.get(w.window_start, 0), used)
        self.adjustments[w.window_start] += fx.adjustment_units
        for ws, n in before.items():  # no observation ever lowers counted usage (an entry may only move)
            moved = entry_units if fx.reattribute and ws == source else 0
            assert self.counted(ws) >= n - moved
        if obs is None:
            return
        fresh = self.last_observed is None or obs.observed_at > self.last_observed
        if fresh:
            self.last_observed = obs.observed_at
        else:  # a late response is ignored entirely (§6.3 step 0)
            assert (changes, fx) == ([], ObservationEffects(0, (), False))
            assert (w.window_start, w.window_end, w.limit_units, w.state) == (
                cur.window_start,
                cur.window_end,
                cur.limit_units,
                cur.state,
            )
        describes = fresh and not (obs.reset_at is not None and obs.reset_at <= cur.window_start)
        if describes and obs.remaining is not None:  # tighten-only: never looser than the provider's report
            assert self.counted(w.window_start) >= w.limit_units - obs.remaining
        if describes and obs.limit is not None:  # a lower reported limit is applied at once
            assert w.limit_units <= obs.limit

    def _classify(
        self, e: Entry, outcome: Settlement, late: bool, charged: bool
    ) -> tuple[Settled, int | None, Observation | None]:
        at = self.last_observed if late and self.last_observed is not None else self.now
        if self.provider == "adzuna":
            # every dispatched request is a hit, except a connect error before sending
            return ("not_billed", None, None) if outcome == "connect" else ("billed", 200, None)
        if outcome == "ok":
            self.p_used += 1
            return "billed", 200, self._observation(at, 200)
        if outcome == "ok_bare":  # a 2xx without the quota headers: it validates nothing
            self.p_used += 1
            return "billed", 200, Observation(observed_at=at, status=200)
        if outcome == "gateway_404":
            return "not_billed", 404, None
        if outcome == "connect":
            return "not_billed", None, None
        if outcome == "lost":
            self.p_used += charged
            return "ambiguous", None, None
        if outcome == "rate_limited_bare":
            # a 429 without the quota headers: `remaining` absent (§6.3 step 4)
            return "ambiguous", 429, Observation(observed_at=at, status=429)
        if outcome == "rate_limited":
            return "ambiguous", 429, self._observation(at, 429)
        self.p_used += charged
        return "ambiguous", 503, self._observation(at, 503) if charged else None

    # -- rules ---------------------------------------------------------------------------------------------
    @rule(
        manual=st.booleans(), complete=st.booleans(), outcome=st.sampled_from(OUTCOMES), charged=st.booleans()
    )
    def tick(self, manual: bool, complete: bool, outcome: Settlement, charged: bool) -> None:
        """A scheduled run (one interval later) or a manual run (now) plans one normal request; `complete`
        sends it and settles its outcome at once, otherwise it is left reserved."""
        if not manual:
            self._advance(timedelta(seconds=self.interval_s))
        self.run += 1
        entry = self._reserve("normal")
        if entry is not None and complete:
            entry.state, entry.dispatched_at = "dispatched", self.now
            self._settle(entry, outcome, late=False, charged=charged)

    @rule(outcome=st.sampled_from(PROBE_OUTCOMES), charged=st.booleans())
    def when_the_probe_is_due(self, outcome: Settlement, charged: bool) -> None:
        """A run waits until the window's next probe is due (an exhausted or failed-probe window): by §6.3, or
        by the window's `next_probe_at`, whichever comes first. It plans one normal request (the re-probe) and
        sends it."""
        w = self._roll()
        due = [t for t in (w.next_probe_at, self.reprobe_due.get(w.window_start)) if t is not None]
        if due and min(due) > self.now:
            self._advance(min(due) - self.now)
        self.tick(manual=True, complete=True, outcome=outcome, charged=charged)

    @rule(
        minutes=st.integers(0, 120),
        request_class=st.sampled_from(["normal", "retry"]),
        count=st.integers(1, 4),
    )
    def reserve(self, minutes: int, request_class: QuotaClass, count: int) -> None:
        """A burst of `count` reservations of one class at the same instant."""
        self._advance(timedelta(minutes=minutes))
        for _ in range(count):
            self._reserve(request_class)

    @rule(
        data=st.data(),
        outcome=st.sampled_from(OUTCOMES),
        charged=st.booleans(),
    )
    def send(self, data: st.DataObject, outcome: Settlement, charged: bool) -> None:
        """The owner dispatches one of its reservations and settles its outcome at once."""
        e = self._pick(data, "reserved")
        if e is not None and e.run == self.run:
            e.state, e.dispatched_at = "dispatched", self.now
            self._settle(e, outcome, late=False, charged=charged)

    @rule(data=st.data())
    def dispatch(self, data: st.DataObject) -> None:
        e = self._pick(data, "reserved")
        if e is not None and e.run == self.run:  # only the owner dispatches, and only while it owns the run
            e.state, e.dispatched_at = "dispatched", self.now

    @rule(
        data=st.data(),
        outcome=st.sampled_from(OUTCOMES),
        late=st.booleans(),
        charged=st.booleans(),
    )
    def settle(self, data: st.DataObject, outcome: Settlement, late: bool, charged: bool) -> None:
        """A response (or its loss) is classified. `late` makes its observation arrive out of order: received
        no later than the last one applied (step 0 must ignore it entirely)."""
        e = self._pick(data, "dispatched")
        if e is not None:
            self._settle(e, outcome, late=late, charged=charged)

    @rule(units=st.integers(1, 3))
    def foreign_charge(self, units: int) -> None:
        """Someone else uses the key: the provider charges units the ledger never saw (unaccounted usage)."""
        self.p_used += units

    @rule(lower=st.integers(1, 12))
    def limit_decrease(self, lower: int) -> None:
        """The provider lowers the plan's limit; the next response reports it."""
        self.p_limit = min(self.p_limit, lower)

    @rule()
    def provider_reset(self) -> None:
        """The provider resets early (a plan change): its new period starts now."""
        self.p_used, self.p_reset = 0, self.now + NOMINAL_PERIOD["jsearch"]

    @rule()
    def crash(self) -> None:
        """The run attempt dies; 16 minutes later recovery classifies what it left (§6.2 rule 5)."""
        self.run += 1  # the old owner is superseded
        self._advance(RECOVERY + timedelta(minutes=1))
        for e in self.entries:
            w = self.windows[e.window_start]
            # `ledger.release_never_dispatched` and `mark_ambiguous`: a validated window awaits no probe, so
            # neither records a probe outcome on it (fix wave F4, ledger L109)
            if e.state == "reserved" and self.now - e.created_at > RECOVERY:
                e.state = "not_billed"
                if e.request_class == "probe" and w.state != "validated":
                    note_probe_outcome(w, "never_sent", now=self.now)
                    self.reprobe_due[e.window_start] = self.now
            elif (
                e.state == "dispatched"
                and e.dispatched_at is not None
                and self.now - e.dispatched_at > RECOVERY
            ):
                e.state = "ambiguous"
                if e.request_class == "probe" and w.state != "validated":
                    note_probe_outcome(w, "failed", now=self.now)
                    self.reprobe_due[e.window_start] = self.now + PROBE_RETRY

    # -- invariants ----------------------------------------------------------------------------------------
    @invariant()
    def counted_never_exceeds_the_limit(self) -> None:
        for ws, w in self.windows.items():
            assert self.counted(ws) <= max(w.limit_units, self.floor.get(ws, 0)), (ws, w.limit_units)

    @invariant()
    def adzuna_rolling_caps_hold(self) -> None:
        if self.provider == "adzuna":
            assert all(n <= cap for n, cap in zip(self._rolling(), self.caps, strict=True))

    @invariant()
    def one_current_window(self) -> None:
        assert sum(w.is_current for w in self.windows.values()) == (0 if self.current is None else 1)


TestQuotaMachine = QuotaMachine.TestCase
TestQuotaMachine.settings = settings(max_examples=300, stateful_step_count=80, deadline=None)


# -- Q-SH1 ---------------------------------------------------------------------------------------------------
HOUR = timedelta(hours=1)
LAG = ledger.SERPAPI_USAGE_LAG


class HourMachine(RuleBasedStateMachine):
    """SerpApi's hourly rule. Under exclusive use the account never goes above `H`, and no foreign usage is
    ever detected. With another user injected, foreign usage beyond the app's own lagging searches is
    detected, the alert is raised, conservative mode starts, and the app's own admissions stop at `H - 5`
    above the latest observation. Whether the account total stays below `H` then is not asserted: no local
    rule can bound it."""

    @initialize(cap=st.integers(6, 49), foreign_user=st.booleans())
    def setup(self, cap: int, foreign_user: bool) -> None:
        self.settings = replace(SETTINGS, serpapi_hourly_cap=cap)
        self.foreign_user, self.now = foreign_user, START
        self.app: list[tuple[datetime, timedelta]] = []  # the app's searches, each with its reporting lag
        self.foreign: list[datetime] = []
        self.alerts = 0
        self.last_detection: datetime | None = None
        renewal = START + timedelta(days=20)
        self.w = Window(
            provider="serpapi",
            window_start=renewal - timedelta(days=30),
            window_end=renewal,
            is_current=True,
            closed_reason=None,
            limit_units=250,
            reserve_units=25,
            state="validated",
            tokens=Decimal(1),
            tokens_at=START,
            capacity=Decimal(1),
        )

    def _in_hour(self, times: list[datetime], at: datetime) -> int:
        return sum(1 for t in times if at - HOUR < t <= at)

    def _account_this_hour(self, at: datetime) -> int:
        """What `account.json` reports at `at`: the app's searches once their lag has passed, foreign ones at
        once."""
        visible = [t for t, lag in self.app if t + lag <= at]
        return self._in_hour(visible, at) + self._in_hour(self.foreign, at)

    @rule(seconds=st.integers(0, 1800))
    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)

    @precondition(lambda self: self.foreign_user)
    @rule()
    def another_user_searches(self) -> None:
        self.foreign.append(self.now)

    @rule(lag=st.integers(0, int(LAG.total_seconds())))
    def search(self, lag: int) -> None:
        """`account.json` is read immediately before every search (§5.3.2), then the hour rule decides."""
        this_hour = self._account_this_hour(self.now)
        app_times = [t for t, _ in self.app]
        local_before = self._in_hour(app_times, self.now)
        foreign = ledger.note_foreign_usage(
            self.w, this_hour=this_hour, local_before=local_before, now=self.now
        )
        # own searches the account may not show yet
        recent_own = sum(1 for t in app_times if t > self.now - LAG)
        true_foreign = self._in_hour(self.foreign, self.now)
        if foreign > ledger.FOREIGN_TOLERANCE:
            self.alerts += 1
            self.last_detection = self.now
            assert self.w.conservative_until == self.now + ledger.CONSERVATIVE_FOR
        if true_foreign - recent_own > ledger.FOREIGN_TOLERANCE:
            assert foreign > ledger.FOREIGN_TOLERANCE, (
                "foreign usage beyond the app's lagging searches is detected"
            )
        if not self.foreign_user:
            assert foreign <= 0, "exclusive use is never taken for foreign usage"
        self.w.observed_this_hour, self.w.hour_observed_at = this_hour, self.now
        conservative = self.w.conservative_until is not None and self.now < self.w.conservative_until
        if self.last_detection is not None and self.now < self.last_detection + ledger.CONSERVATIVE_FOR:
            assert conservative  # conservative mode lasts 24 h after the last detection
        local_last_hour = self._in_hour(app_times, self.now)
        local_since = sum(1 for t in app_times if t > self.now - LAG)
        cap = self.settings.serpapi_hourly_cap
        if not ledger.serpapi_hour_admits(
            local_last_hour=local_last_hour,
            this_hour=this_hour,
            local_since_observation=local_since,
            hourly_cap=cap,
            conservative=conservative,
        ):
            return
        self.app.append((self.now, timedelta(seconds=lag)))
        if conservative:  # the app's own admissions stop at H - 5 above the latest observation
            assert this_hour + local_since + 1 <= cap - ledger.CONSERVATIVE_MARGIN
            assert local_last_hour + 1 <= cap - ledger.CONSERVATIVE_MARGIN

    @invariant()
    def exclusive_use_never_exceeds_the_hourly_cap(self) -> None:
        if not self.foreign_user:
            for t, _ in self.app:  # the rolling hour ending at every search
                assert self._in_hour([s for s, _ in self.app], t) <= self.settings.serpapi_hourly_cap


TestHourMachine = HourMachine.TestCase
TestHourMachine.settings = settings(max_examples=150, stateful_step_count=80, deadline=None)
