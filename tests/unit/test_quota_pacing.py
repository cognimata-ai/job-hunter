from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from jobhunter.quota.pacing import (
    Admit,
    Deny,
    accrue,
    capacity,
    decide,
    effective_reserve,
    fair_share,
    per_run_cap,
)
from jobhunter.quota.windows import PROBE_RETRY, RESERVE_RELEASE, Window, bootstrap_window

from tests.support.quota import SETTINGS

T0 = datetime(2026, 10, 16, tzinfo=UTC)
US = timedelta(microseconds=1)


def boot(provider="jsearch", *, interval_s: int) -> Window:
    w = bootstrap_window(provider, T0, SETTINGS, interval_s=interval_s, account=None)
    assert w is not None
    return w


def admit(d: Admit | Deny) -> Admit:
    assert isinstance(d, Admit), d
    return d


def code(d: Admit | Deny) -> str:
    assert isinstance(d, Deny), d
    return d.code


def test_fair_share_capacity_and_accrual() -> None:
    w = boot(interval_s=3600)
    assert round(fair_share(w, 3600), 3) == 0.242 and w.capacity == Decimal(1)
    assert per_run_cap(SETTINGS, fair_share(w, 3600)) == 10
    assert per_run_cap(SETTINGS, 41.0) == 82
    assert per_run_cap(replace(SETTINGS, paid_max_requests_per_run=3), 41.0) == 3
    w.tokens = Decimal(0)
    w.tokens_at = T0
    accrue(w, T0 + timedelta(hours=6), interval_s=3600)
    assert w.tokens == Decimal(1) and w.tokens_at == T0 + timedelta(hours=6)
    assert effective_reserve(w, T0) == 20 and effective_reserve(w, w.window_end - timedelta(hours=47)) == 0
    # a 7-day schedule: fair share about 41 units per run, so the carry is about 82 (§6.4)
    assert capacity(w, 7 * 86400) == Decimal("81.3863") and round(fair_share(w, 7 * 86400)) == 41


def test_the_reserve_is_released_for_the_final_48_hours() -> None:
    w = boot(interval_s=21600)
    w.state = "validated"
    edge = w.window_end - RESERVE_RELEASE
    assert (effective_reserve(w, edge - US), effective_reserve(w, edge)) == (20, 0)

    def normal_at(now: datetime) -> Admit | Deny:
        return decide(
            w,
            provider="jsearch",
            request_class="normal",
            units=1,
            used=180,
            run_normal_count=0,
            hour_ok=True,
            adzuna_ok=True,
            now=now,
            per_run_cap=10,
        )

    assert code(normal_at(edge - US)) == "reserve_held"
    assert admit(normal_at(edge)).tokens_used == 1


def test_an_hourly_schedule_carries_fractions_and_does_not_starve() -> None:
    w = boot(interval_s=3600)  # fair share 0.24 per run: C = 1
    w.state, w.tokens = "validated", Decimal(0)
    outcomes = []
    for tick in range(1, 6):
        now = T0 + timedelta(hours=tick)
        accrue(w, now, interval_s=3600)
        d = decide(
            w,
            provider="jsearch",
            request_class="normal",
            units=1,
            used=0,
            run_normal_count=0,
            hour_ok=True,
            adzuna_ok=True,
            now=now,
            per_run_cap=10,
        )
        outcomes.append(d if isinstance(d, Deny) else "admit")
    assert outcomes == [Deny("paced")] * 4 + ["admit"]  # 4 * 0.2422 < 1 ≤ 5 * 0.2422, capped at C = 1
    assert w.tokens == Decimal(1)


def test_accrual_is_capped_and_never_runs_backwards() -> None:
    w = boot(interval_s=21600)
    w.tokens, w.tokens_at = Decimal("0.5"), T0 + timedelta(hours=1)
    accrue(w, T0, interval_s=21600)  # an earlier `now` (a transaction that started before the last save)
    assert (w.tokens, w.tokens_at) == (Decimal("0.5"), T0 + timedelta(hours=1))
    accrue(w, T0 + timedelta(days=3), interval_s=21600)
    assert w.tokens == w.capacity == Decimal("2.9067")
    accrue(w, T0 + timedelta(days=3), interval_s=3600)  # the schedule tightened: the carry shrinks to C = 1
    assert w.tokens == w.capacity == Decimal(1)


def test_decision_table() -> None:
    w = boot(interval_s=21600)

    def d(cls="normal", used=0, run=0, hour=True, now=T0) -> Admit | Deny:
        return decide(
            w,
            provider="jsearch",
            request_class=cls,
            units=1,
            used=used,
            run_normal_count=run,
            hour_ok=hour,
            adzuna_ok=True,
            now=now,
            per_run_cap=10,
        )

    assert admit(d()).starts_probe and admit(d()).request_class == "probe" and admit(d()).tokens_used == 0
    w.probe_used = True
    assert code(d()) == "awaiting_window_validation"
    w.state, w.probe_used = "validated", False
    assert code(d(used=200)) == "exhausted" and code(d(used=180)) == "reserve_held"
    assert admit(d(used=180, cls="retry")).request_class == "retry"
    assert code(d(hour=False)) == "window_cap" and code(d(run=10)) == "paced"
    assert admit(d()) == Admit(request_class="normal", tokens_used=1, starts_probe=False)
    w.tokens = Decimal("0.5")
    assert code(d()) == "paced" and admit(d(cls="probe")).request_class == "probe"
    w.state, w.probe_failed, w.next_probe_at = "exhausted_until_reset", False, T0 + timedelta(hours=12)
    assert code(d()) == "exhausted"
    assert admit(d(now=T0 + timedelta(hours=12))).starts_probe


def test_probe_retry_boundaries() -> None:
    w = boot(interval_s=21600)
    w.probe_used, w.probe_failed, w.next_probe_at = True, True, T0 + PROBE_RETRY  # a failed bootstrap probe

    def d(now: datetime, used: int = 0) -> Admit | Deny:
        return decide(
            w,
            provider="jsearch",
            request_class="normal",
            units=1,
            used=used,
            run_normal_count=0,
            hour_ok=True,
            adzuna_ok=True,
            now=now,
            per_run_cap=10,
        )

    assert code(d(T0 + PROBE_RETRY - US)) == "awaiting_window_validation"
    assert admit(d(T0 + PROBE_RETRY)) == Admit(request_class="probe", tokens_used=0, starts_probe=True)
    w.state = "exhausted_until_reset"
    assert code(d(T0 + PROBE_RETRY, used=200)) == "exhausted"  # the periodic probe still needs the limit


def test_adzuna_has_no_per_run_cap_but_its_bucket() -> None:
    w = boot("adzuna", interval_s=21600)  # validated, fair share 2025 * 6 h / 30 d ≈ 16.9
    ok = decide(
        w,
        provider="adzuna",
        request_class="normal",
        units=1,
        used=0,
        run_normal_count=500,
        hour_ok=True,
        adzuna_ok=True,
        now=T0,
        per_run_cap=10,
    )
    assert admit(ok).tokens_used == 1
    capped = decide(
        w,
        provider="adzuna",
        request_class="normal",
        units=1,
        used=0,
        run_normal_count=0,
        hour_ok=True,
        adzuna_ok=False,
        now=T0,
        per_run_cap=10,
    )
    assert code(capped) == "window_cap"


def test_capacity_is_bounded_by_the_window_limit() -> None:
    # Ruling T8-a: a window refined to 1.5 s by a reset header would hold 2N x interval / 1.5 s = 5,184,000
    # tokens, past the numeric(10,4) column; no bucket holds more than the window's limit, and never below 1.
    w = boot(interval_s=21600)
    short = replace(w, window_end=w.window_start + timedelta(milliseconds=1500))
    assert capacity(short, 21600) == Decimal(200)
    accrue(short, short.window_start + timedelta(seconds=1), interval_s=21600)
    assert short.capacity == 200 and short.tokens <= 200
    assert capacity(replace(w, limit_units=0, reserve_units=0), 21600) == Decimal(1)
    assert capacity(w, 21600) == Decimal("2.9067")  # an ordinary window is unchanged
