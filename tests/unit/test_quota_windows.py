import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from jobhunter.quota.pacing import Admit, Deny, QuotaClass, decide
from jobhunter.quota.windows import (
    NOMINAL_PERIOD,
    AccountObservation,
    Close,
    Insert,
    ObservationEffects,
    Window,
    WindowChange,
    apply_observation_pure,
    bootstrap_window,
    limit_for,
    reserve_for,
    roll_forward,
    serpapi_period,
)
from jobhunter.run_settings import QuotaSettings
from jobhunter.sources.base import Observation

from tests.support.quota import SETTINGS  # the seeded QuotaSettings (Task 5 values)
from tests.support.sources import load_fixture

T0 = datetime(2026, 10, 16, 9, 47, tzinfo=UTC)
SIX_H = 21600
US = timedelta(microseconds=1)


def boot(provider, now=T0, *, account=None, settings: QuotaSettings = SETTINGS) -> Window:
    w = bootstrap_window(provider, now, settings, interval_s=SIX_H, account=account)
    assert w is not None
    return w


def closes(changes: list[WindowChange]) -> list[Close]:
    return [c for c in changes if isinstance(c, Close)]


def inserts(changes: list[WindowChange]) -> list[Insert]:
    return [c for c in changes if isinstance(c, Insert)]


def recorded_account(observed_at: datetime, **override: int) -> AccountObservation:
    """The account of the scrubbed `account_before` recording (Task 6): renews 2026-11-08, 248 of 250 left."""
    _, _, body = load_fixture("serpapi", "account_before")
    doc = json.loads(body)
    fields = {
        "searches_per_month": doc["searches_per_month"],
        "plan_searches_left": doc["plan_searches_left"],
        "this_hour_searches": doc["this_hour_searches"],
        "account_rate_limit_per_hour": doc["account_rate_limit_per_hour"],
        **override,
    }
    renewal = datetime.fromisoformat(doc["plan_renewal_date"]).replace(tzinfo=UTC)
    return AccountObservation(observed_at=observed_at, plan_renewal_date=renewal, **fields)


def test_bootstrap_per_provider() -> None:
    js = boot("jsearch")
    assert (js.window_start, js.window_end, js.state, js.limit_units, js.reserve_units) == (
        T0,
        T0 + NOMINAL_PERIOD["jsearch"],
        "extrapolated_unvalidated",
        200,
        20,
    )
    # 2 * 180 * 21600 / 2675242, quantised to the column scale
    assert js.tokens == js.capacity == Decimal("2.9067") and js.probe_used is False
    assert (js.is_current, js.closed_reason, js.tokens_at, js.next_probe_at) == (True, None, T0, None)
    assert bootstrap_window("serpapi", T0, SETTINGS, interval_s=SIX_H, account=None) is None
    az = boot("adzuna")
    assert (az.state, az.limit_units, az.reserve_units, az.window_end) == (
        "validated",
        2250,
        225,
        T0 + timedelta(days=30),
    )


def test_serpapi_bootstraps_from_the_account_read() -> None:
    w = boot("serpapi", account=recorded_account(T0))
    # the calendar month ending at `plan_renewal_date`, validated by the free read
    assert (w.window_start, w.window_end, w.state, w.limit_units, w.reserve_units) == (
        datetime(2026, 10, 8, tzinfo=UTC),
        datetime(2026, 11, 8, tzinfo=UTC),
        "validated",
        250,
        25,
    )
    assert (w.observed_limit, w.observed_remaining, w.observed_this_hour, w.observed_hourly_cap) == (
        250,
        248,
        0,
        250,
    )
    assert w.hour_observed_at == T0 and w.tokens == w.capacity
    # the reconciliation of this same read (step 0 onwards) is still to come, so it is not marked as seen
    assert w.last_observed_at is None
    smaller = boot("serpapi", account=recorded_account(T0, searches_per_month=100))
    assert (smaller.limit_units, smaller.reserve_units) == (100, 10)  # tightened by the account


def test_serpapi_period_is_the_calendar_month_ending_at_the_renewal() -> None:
    assert serpapi_period(datetime(2026, 11, 8, tzinfo=UTC)) == (
        datetime(2026, 10, 8, tzinfo=UTC),
        datetime(2026, 11, 8, tzinfo=UTC),
    )
    assert serpapi_period(datetime(2026, 1, 8, 6, tzinfo=UTC))[0] == datetime(2025, 12, 8, 6, tzinfo=UTC)
    assert serpapi_period(datetime(2026, 3, 31, tzinfo=UTC))[0] == datetime(2026, 2, 28, tzinfo=UTC)


def test_limits_and_reserves() -> None:
    assert [limit_for(p, SETTINGS) for p in ("jsearch", "serpapi", "adzuna")] == [200, 250, 2250]
    assert [reserve_for(p, limit_for(p, SETTINGS), SETTINGS) for p in ("jsearch", "serpapi", "adzuna")] == [
        20,
        25,
        225,
    ]
    assert reserve_for("jsearch", 201, SETTINGS) == 21  # ceil(0.1 * 201)
    assert (
        reserve_for("jsearch", 100, replace(SETTINGS, reserve_pct=7)) == 7
    )  # exact: float 0.07 * 100 is 7.000…1
    # Adzuna's N is 0.9 * cap_month whatever reserve_pct says: 2,255 - int(2,029.5)
    assert reserve_for("adzuna", 2255, replace(SETTINGS, reserve_pct=50)) == 226


def test_roll_forward_across_long_downtime_keeps_history() -> None:
    w = boot("adzuna")
    later = T0 + timedelta(days=95)
    cur, changes = roll_forward(w, "adzuna", later, SETTINGS, interval_s=SIX_H)
    assert cur is not None
    assert [type(c).__name__ for c in changes] == ["Close", "Insert"] * 3
    assert cur.window_start == T0 + timedelta(days=90) and cur.tokens == cur.capacity
    assert all(c.reason == "rolled_over" for c in closes(changes))
    # each closed window is the one the previous change inserted; the inserted rows are current
    assert [c.window.window_start for c in closes(changes)] == [T0 + timedelta(days=d) for d in (0, 30, 60)]
    assert [i.window.window_start for i in inserts(changes)] == [T0 + timedelta(days=d) for d in (30, 60, 90)]
    assert all(i.window.is_current and i.window.closed_reason is None for i in inserts(changes))
    assert all(i.window.state == "validated" for i in inserts(changes))
    assert w.is_current and w.window_start == T0  # pure: the input window is not touched


def test_roll_forward_at_the_window_end_and_the_extrapolated_successor() -> None:
    w = replace(
        boot("jsearch"),
        state="validated",
        limit_units=100,
        reserve_units=10,
        tokens=Decimal(0),
        probe_used=True,
        last_observed_at=T0 + timedelta(days=3),
        observed_remaining=40,
    )
    same, none = roll_forward(w, "jsearch", w.window_end - US, SETTINGS, interval_s=SIX_H)
    assert (same, none) == (w, [])
    nxt, changes = roll_forward(w, "jsearch", w.window_end, SETTINGS, interval_s=SIX_H)  # now == end rolls
    assert nxt is not None and [type(c).__name__ for c in changes] == ["Close", "Insert"]
    assert (nxt.window_start, nxt.window_end, nxt.state) == (
        w.window_end,
        w.window_end + NOMINAL_PERIOD["jsearch"],
        "extrapolated_unvalidated",
    )
    # initial credit; the limit comes from settings again (its probe's headers tighten it); probes reset;
    # the observation clock stays monotonic, so a late response of the old window is still ignored
    assert (nxt.tokens, nxt.capacity, nxt.limit_units, nxt.reserve_units) == (
        Decimal("2.9067"),
        Decimal("2.9067"),
        200,
        20,
    )
    assert (nxt.probe_used, nxt.observed_remaining, nxt.last_observed_at) == (False, None, w.last_observed_at)


def test_roll_forward_bootstraps_and_extrapolates_serpapi_by_calendar_month() -> None:
    assert roll_forward(None, "serpapi", T0, SETTINGS, interval_s=SIX_H) == (None, [])
    js, boot_changes = roll_forward(None, "jsearch", T0, SETTINGS, interval_s=SIX_H)
    assert js is not None and [type(c).__name__ for c in boot_changes] == ["Insert"]
    stale = recorded_account(T0)  # renewal 2026-11-08; read once, then the app is down until mid-December
    december = datetime(2026, 12, 15, tzinfo=UTC)
    serp, changes = roll_forward(None, "serpapi", december, SETTINGS, interval_s=SIX_H, account=stale)
    assert [type(c).__name__ for c in changes] == ["Insert", "Close", "Insert", "Close", "Insert"]
    assert serp is not None and (serp.window_start, serp.window_end, serp.state) == (
        datetime(2026, 12, 8, tzinfo=UTC),
        datetime(2027, 1, 8, tzinfo=UTC),
        "extrapolated_unvalidated",
    )


def test_observation_steps_in_order() -> None:
    w = boot("jsearch")
    obs = Observation(
        observed_at=T0 + timedelta(minutes=1),
        status=200,
        reset_at=T0 + timedelta(days=30),
        limit=100,
        remaining=99,
    )
    w2, changes, fx = apply_observation_pure(w, obs, SETTINGS, provider="jsearch", used_units=1)  # Codex #20
    assert (
        w2.state,
        w2.window_end,
        w2.limit_units,
        w2.reserve_units,
        fx.adjustment_units,
        fx.alerts,
        changes,
    ) == (
        "validated",
        obs.reset_at,
        100,
        10,
        0,
        (),
        [],
    )
    assert (w2.observed_limit, w2.observed_remaining, w2.last_observed_at) == (100, 99, obs.observed_at)
    assert w.state == "extrapolated_unvalidated" and w.last_observed_at is None  # a copy is returned
    w3, _, fx3 = apply_observation_pure(
        w2,
        Observation(observed_at=T0 + timedelta(minutes=2), status=200, remaining=90),
        SETTINGS,
        provider="jsearch",
        used_units=1,
    )
    assert fx3.adjustment_units == 9 and fx3.alerts == ("quota.unaccounted_usage",)
    stale, stale_changes, stale_fx = apply_observation_pure(
        w3, Observation(observed_at=T0, status=200, remaining=0), SETTINGS, provider="jsearch", used_units=1
    )
    assert stale == w3 and stale_changes == [] and stale_fx.adjustment_units == 0  # step 0: a late response
    reset = Observation(
        observed_at=T0 + timedelta(days=31),
        status=200,
        reset_at=T0 + timedelta(days=61),
        limit=200,
        remaining=200,
    )
    w4, changes4, fx4 = apply_observation_pure(w3, reset, SETTINGS, provider="jsearch", used_units=10)
    assert [type(c).__name__ for c in changes4] == ["Close", "Insert"]
    assert closes(changes4)[0].reason == "provider_reset_observed" and fx4.reattribute
    assert w4.window_start == reset.observed_at and w4.window_end == reset.reset_at
    assert fx4.alerts == ("quota.limit_increased",)  # step 2 compares with the new window's (carried) limit
    assert (w4.limit_units, w4.state, inserts(changes4)[0].window) == (100, "validated", w4)
    ex, _, _ = apply_observation_pure(
        w4,
        Observation(observed_at=T0 + timedelta(days=32), status=429, remaining=0),
        SETTINGS,
        provider="jsearch",
        used_units=10,
    )
    assert ex.state == "exhausted_until_reset" and ex.window_end == w4.window_end  # a 429 never validates


def test_a_429_never_validates_and_only_exhausts_jsearch_without_remaining() -> None:
    w = boot("jsearch")
    later = T0 + timedelta(hours=1)
    reset_at = T0 + timedelta(days=20)
    limited, _, fx = apply_observation_pure(
        w,
        Observation(later, 429, reset_at=reset_at, remaining=150),
        SETTINGS,
        provider="jsearch",
        used_units=5,
    )
    # a rate limit (remaining > 0): no validation, no exhaustion, but the remaining still tightens
    assert (limited.state, limited.window_end, fx.adjustment_units) == (w.state, w.window_end, 45)
    absent, _, _ = apply_observation_pure(
        w, Observation(later, 429), SETTINGS, provider="jsearch", used_units=0
    )
    assert absent.state == "exhausted_until_reset"
    serp = boot("serpapi", account=recorded_account(T0))
    other, _, _ = apply_observation_pure(
        serp, Observation(later, 429), SETTINGS, provider="serpapi", used_units=0
    )
    assert other.state == "validated"


def test_a_reset_observed_early_counts_the_reattributed_entry_in_the_new_window() -> None:
    w = replace(boot("jsearch"), state="validated", last_observed_at=T0)
    obs = Observation(
        T0 + timedelta(days=20), 200, reset_at=T0 + timedelta(days=50), limit=200, remaining=199
    )
    new, changes, fx = apply_observation_pure(
        w, obs, SETTINGS, provider="jsearch", used_units=150, entry_units=1
    )
    # the entry that carried the response is the new window's only usage: 200 - 199 = 1, nothing unaccounted
    assert (fx.reattribute, fx.adjustment_units, fx.alerts, new.window_start) == (
        True,
        0,
        (),
        obs.observed_at,
    )
    # the new window starts full and unprobed; the closed one keeps its own history
    assert (new.tokens, new.probe_used, new.next_probe_at) == (new.capacity, False, None)
    assert closes(changes)[0].window.window_start == T0
    # without an entry (an account read), the provider's one used unit is unaccounted in the new window
    no_entry, _, no_entry_fx = apply_observation_pure(w, obs, SETTINGS, provider="jsearch", used_units=150)
    assert no_entry_fx.adjustment_units == 1 and no_entry.window_start == obs.observed_at


def test_a_reset_time_that_is_not_ahead_identifies_no_window() -> None:
    w = boot("jsearch")
    now = T0 + timedelta(days=2)
    same, changes, _ = apply_observation_pure(
        w, Observation(now, 200, reset_at=now), SETTINGS, provider="jsearch", used_units=0
    )
    assert (same.state, same.window_end, changes) == ("extrapolated_unvalidated", w.window_end, [])


def test_hourly_fields_are_recorded() -> None:
    serp = boot("serpapi", account=recorded_account(T0))
    later = T0 + timedelta(minutes=5)
    w, _, _ = apply_observation_pure(
        serp,
        Observation(later, 200, this_hour=12, hourly_cap=250),
        SETTINGS,
        provider="serpapi",
        used_units=0,
    )
    assert (w.observed_this_hour, w.observed_hourly_cap, w.hour_observed_at) == (12, 250, later)


# The renewal gap (Ruling T7-a). SerpApi's `plan_renewal_date` is a date (the recording's is "2026-11-08"), so
# the local window rolls over at 00:00 UTC while SerpApi renews the plan hours later: until then every account
# read still reports the old renewal date and the old month's `plan_searches_left`.
RENEWAL = datetime(2026, 11, 8, tzinfo=UTC)
NEXT_RENEWAL = datetime(2026, 12, 8, tzinfo=UTC)
IN_THE_GAP = RENEWAL + timedelta(hours=3)
OCTOBER = datetime(2026, 10, 20, tzinfo=UTC)


def account_read(account: AccountObservation) -> Observation:
    """An account read (§5.3.2) as the observation it is reconciled as (§6.3)."""
    return Observation(
        observed_at=account.observed_at,
        status=200,
        reset_at=account.plan_renewal_date,
        limit=account.searches_per_month,
        remaining=account.plan_searches_left,
        this_hour=account.this_hour_searches,
        hourly_cap=account.account_rate_limit_per_hour,
    )


def lagging(at: datetime, **override: int) -> AccountObservation:
    """A read before SerpApi has renewed: the old renewal date, October's 100 searches still counted."""
    return recorded_account(at, **{"plan_searches_left": 150, **override})


def renewed(at: datetime) -> AccountObservation:
    """A read after SerpApi has renewed: the next renewal date and the new month's full allowance."""
    return replace(recorded_account(at, plan_searches_left=250), plan_renewal_date=NEXT_RENEWAL)


def serpapi_decision(w: Window, now: datetime, *, used: int, cls: QuotaClass = "normal") -> Admit | Deny:
    return decide(
        w,
        provider="serpapi",
        request_class=cls,
        units=1,
        used=used,
        run_normal_count=0,
        hour_ok=True,
        adzuna_ok=True,
        now=now,
        per_run_cap=10,
    )


def rolled_into_the_gap() -> Window:
    """SerpApi bootstrapped in October from a read with 150 of 250 left, then rolled over locally on Nov 8."""
    october = boot("serpapi", OCTOBER, account=lagging(OCTOBER))
    cur, _ = roll_forward(october, "serpapi", IN_THE_GAP, SETTINGS, interval_s=SIX_H)
    assert cur is not None
    assert (cur.window_start, cur.window_end, cur.state) == (
        RENEWAL,
        NEXT_RENEWAL,
        "extrapolated_unvalidated",
    )
    return cur


def test_a_lagging_account_read_in_the_renewal_gap_describes_the_previous_window() -> None:
    cur = rolled_into_the_gap()
    w, changes, fx = apply_observation_pure(
        cur,
        account_read(lagging(IN_THE_GAP, this_hour_searches=2)),
        SETTINGS,
        provider="serpapi",
        used_units=0,
    )
    # its renewal is not after the window's start: nothing is charged to the new month and nothing alerts
    assert (fx, changes) == (ObservationEffects(0, (), False), [])
    assert (w.state, w.window_end, w.limit_units, w.observed_remaining, w.observed_limit) == (
        "extrapolated_unvalidated",
        NEXT_RENEWAL,
        250,
        None,
        None,
    )
    # only the observation clock and the hour figures are recorded
    assert (w.last_observed_at, w.observed_this_hour, w.observed_hourly_cap, w.hour_observed_at) == (
        IN_THE_GAP,
        2,
        250,
        IN_THE_GAP,
    )
    # SerpApi fails closed in the gap: no probe that would bypass the bucket and the reserve, no retry either
    assert serpapi_decision(w, IN_THE_GAP, used=0) == Deny("window_cap")
    assert serpapi_decision(w, IN_THE_GAP, used=0, cls="retry") == Deny("window_cap")
    later = IN_THE_GAP + timedelta(hours=1)
    w2, _, fx2 = apply_observation_pure(
        w, account_read(lagging(later)), SETTINGS, provider="serpapi", used_units=0
    )
    assert fx2 == ObservationEffects(0, (), False)
    assert serpapi_decision(w2, later, used=0) == Deny("window_cap")
    # until a read shows the new renewal date
    at = later + timedelta(hours=3)
    w3, _, _ = apply_observation_pure(
        w2, account_read(renewed(at)), SETTINGS, provider="serpapi", used_units=0
    )
    assert serpapi_decision(w3, at, used=0) == Admit("normal", tokens_used=1, starts_probe=False)


def test_after_the_real_renewal_the_window_is_validated_with_the_new_months_remaining() -> None:
    w = rolled_into_the_gap()
    # the new window's counted usage, as the ledger would hold it: adjustments plus admitted requests
    used = 0
    w, _, fx = apply_observation_pure(
        w, account_read(lagging(IN_THE_GAP)), SETTINGS, provider="serpapi", used_units=used
    )
    used += fx.adjustment_units
    if isinstance(serpapi_decision(w, IN_THE_GAP, used=used), Admit):
        used += 1
    at = IN_THE_GAP + timedelta(hours=4)
    w, changes, fx = apply_observation_pure(
        w, account_read(renewed(at)), SETTINGS, provider="serpapi", used_units=used
    )
    assert (w.state, w.window_start, w.window_end, w.limit_units, w.observed_remaining) == (
        "validated",
        RENEWAL,
        NEXT_RENEWAL,
        250,
        250,
    )
    assert (fx, changes) == (ObservationEffects(0, (), False), [])
    # the new month keeps its whole allowance: nothing of October's usage was carried into it
    assert (used, w.limit_units - used) == (0, 250)


def test_a_bootstrap_from_a_lagging_account_read_validates_only_the_month_it_describes() -> None:
    read = lagging(IN_THE_GAP)
    cur, changes = roll_forward(None, "serpapi", IN_THE_GAP, SETTINGS, interval_s=SIX_H, account=read)
    assert [type(c).__name__ for c in changes] == ["Insert", "Close", "Insert"]
    previous = closes(changes)[0].window
    # the read identifies the month that has just ended locally, and that window keeps its figures
    assert (previous.window_start, previous.window_end, previous.state, previous.observed_remaining) == (
        datetime(2026, 10, 8, tzinfo=UTC),
        RENEWAL,
        "validated",
        150,
    )
    # the current window records only the observation clock and the hour figures
    assert cur is not None and (cur.window_start, cur.state, cur.limit_units, cur.observed_remaining) == (
        RENEWAL,
        "extrapolated_unvalidated",
        250,
        None,
    )
    assert (cur.last_observed_at, cur.observed_this_hour, cur.hour_observed_at) == (IN_THE_GAP, 0, IN_THE_GAP)
    # the caller's reconciliation of the same read charges nothing to the new month
    w, rc_changes, fx = apply_observation_pure(
        cur, account_read(read), SETTINGS, provider="serpapi", used_units=0
    )
    assert (fx, rc_changes) == (ObservationEffects(0, (), False), [])
    assert serpapi_decision(w, IN_THE_GAP, used=0) == Deny("window_cap")
