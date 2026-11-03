"""The header status's pure parts (spec §15.1; plan M4 Task 11): the pace of the month's scoring spend, the
paid-search pause, the next schedule slots and the banners with their order.

Every datum is invented.
"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Final, get_args

import pytest
from jobhunter.observability.health import QuotaOut, RunAgeState
from jobhunter.observability.status import (
    BannerFacts,
    PaidSearchOut,
    RescoreOut,
    ScopeProgressOut,
    ScopeStatusOut,
    compute_banners,
    next_slots,
    pace,
    paid_search,
)
from jobhunter.quota.windows import WindowState
from jobhunter.scoring.cycles import PauseReason

# November 2026 has 30 days: its 16th at midnight is 15 days, half of the month, into it.
HALF_NOVEMBER: Final = datetime(2026, 11, 16, tzinfo=UTC)
SCORING_SHARE: Final = Decimal("93.10")  # A x (1 - reserve_frac) with the defaults: $98 x 0.95
JSEARCH_RESET: Final = datetime(2026, 11, 20, tzinfo=UTC)
SERPAPI_RESET: Final = datetime(2026, 11, 18, tzinfo=UTC)
NOT_PAUSED: Final = PaidSearchOut(paused=False, until=None, reason=None)
APPLIED: Final = ScopeStatusOut(pending_version_id=None, progress=None, applied=True)


def window(state: WindowState = "validated", *, used: int = 0, limit: int = 200, end: datetime) -> QuotaOut:
    return QuotaOut(
        window_start=end - timedelta(days=30),
        window_end=end,
        state=state,
        limit_units=limit,
        used_units=used,
        remaining_units=max(0, limit - used),
        tokens=0.0,
    )


def facts(
    *,
    run_age_state: RunAgeState = "ok",
    pause_reason: PauseReason | None = None,
    critical_unacked: int = 0,
    paid: PaidSearchOut = NOT_PAUSED,
    scope: ScopeStatusOut = APPLIED,
    rescore: RescoreOut | None = None,
) -> BannerFacts:
    return BannerFacts(
        run_age_state=run_age_state,
        pause_reason=pause_reason,
        critical_unacked=critical_unacked,
        paid_search=paid,
        scope=scope,
        rescore=rescore,
    )


def banners(f: BannerFacts) -> list[tuple[str, str, Mapping[str, object]]]:
    return [(b.code, b.severity, b.params) for b in compute_banners(f)]


def test_pace_ok_at_the_line_and_ahead_above() -> None:
    def at(counted: str, now: datetime = HALF_NOVEMBER) -> str:
        return pace(scoring_counted_usd=Decimal(counted), scoring_share_usd=SCORING_SHARE, now=now)

    assert at("46.55") == "ok"  # 93.10 x 15/30, exactly at the line
    assert at("46.56") == "ahead"
    assert at("0") == "ok"
    # at the month's first instant nothing may have been spent yet; a microsecond before its end, all but a
    # sliver of the share
    assert at("0", datetime(2026, 11, 1, tzinfo=UTC)) == "ok"
    assert at("0.000001", datetime(2026, 11, 1, tzinfo=UTC)) == "ahead"
    last = datetime(2026, 12, 1, tzinfo=UTC) - timedelta(microseconds=1)
    assert at("93.09", last) == "ok" and at("93.10", last) == "ahead"
    # the month is the UTC one: an instant given in another zone is the same instant
    assert at("46.55", datetime(2026, 11, 16, 1, tzinfo=timezone(timedelta(hours=1)))) == "ok"


# The five SerpApi cases that leave an exhausted JSearch without a fallback: SerpApi's window, whether the
# settings enable it and whether its instance is healthy, then the reason and the reset the pause names.
SERPAPI_UNAVAILABLE: Final = [
    pytest.param(window(end=SERPAPI_RESET), False, True, "serpapi_disabled", JSEARCH_RESET, id="disabled"),
    pytest.param(None, True, True, "serpapi_unavailable", JSEARCH_RESET, id="no-window"),
    pytest.param(
        window("extrapolated_unvalidated", end=SERPAPI_RESET),
        True,
        True,
        "serpapi_unavailable",
        JSEARCH_RESET,
        id="unvalidated",
    ),
    pytest.param(
        window(end=SERPAPI_RESET), True, False, "serpapi_unavailable", JSEARCH_RESET, id="unhealthy"
    ),
    pytest.param(
        window(used=250, limit=250, end=SERPAPI_RESET),
        True,
        True,
        "serpapi_exhausted",
        SERPAPI_RESET,  # the earliest reset: SerpApi's comes first
        id="exhausted",
    ),
]


@pytest.mark.parametrize(("serpapi", "enabled", "healthy", "reason", "until"), SERPAPI_UNAVAILABLE)
def test_paid_search_paused_needs_jsearch_exhausted_and_serpapi_unavailable(
    serpapi: QuotaOut | None, enabled: bool, healthy: bool, reason: str, until: datetime
) -> None:
    def paused_by(jsearch: QuotaOut | None) -> PaidSearchOut:
        return paid_search(jsearch, serpapi, serpapi_enabled=enabled, serpapi_healthy=healthy)

    exhausted = (
        window("exhausted_until_reset", used=12, end=JSEARCH_RESET),  # the provider said so
        window(used=200, end=JSEARCH_RESET),  # every unit counted
    )
    for jsearch in exhausted:
        assert paused_by(jsearch) == PaidSearchOut(paused=True, until=until, reason=reason)
    # JSearch with units left, or without a window yet, pauses nothing, whatever SerpApi's state
    assert paused_by(window(used=199, end=JSEARCH_RESET)) == NOT_PAUSED
    assert paused_by(None) == NOT_PAUSED


def test_paid_search_is_not_paused_while_serpapi_can_fall_back() -> None:
    jsearch = window("exhausted_until_reset", end=JSEARCH_RESET)
    serpapi = window(used=249, limit=250, end=SERPAPI_RESET)
    assert paid_search(jsearch, serpapi, serpapi_enabled=True, serpapi_healthy=True) == NOT_PAUSED
    # an exhausted SerpApi that resets after JSearch: JSearch's reset is the earliest
    late = window("exhausted_until_reset", end=JSEARCH_RESET + timedelta(days=3))
    assert paid_search(jsearch, late, serpapi_enabled=True, serpapi_healthy=True) == PaidSearchOut(
        paused=True, until=JSEARCH_RESET, reason="serpapi_exhausted"
    )


def test_next_slots_three_after_now_and_none_while_disabled() -> None:
    anchor = datetime(2026, 10, 1, 4, tzinfo=UTC)
    six_h = 6 * 3600
    now = datetime(2026, 11, 3, 13, 30, tzinfo=UTC)
    expected = [datetime(2026, 11, 3, h, tzinfo=UTC) for h in (16, 22)] + [
        datetime(2026, 11, 4, 4, tzinfo=UTC)
    ]
    assert next_slots(anchor, six_h, True, now) == expected
    # a slot at `now` itself is not a next one
    assert next_slots(anchor, six_h, True, datetime(2026, 11, 3, 10, tzinfo=UTC)) == expected
    assert next_slots(anchor, six_h, True, now, n=1) == expected[:1]
    # an anchor after `now` still gives the slots after `now` on its grid
    assert next_slots(datetime(2026, 12, 1, 4, tzinfo=UTC), six_h, True, now) == expected
    assert next_slots(anchor, six_h, False, now) == []


def test_banners_sort_by_severity_then_table_order() -> None:
    every = facts(
        run_age_state="critical",
        pause_reason="global_fence",  # the warn pause
        critical_unacked=2,
        paid=PaidSearchOut(paused=True, until=JSEARCH_RESET, reason="serpapi_disabled"),
        scope=ScopeStatusOut(
            pending_version_id=7,
            progress=ScopeProgressOut(done=1, total=4, det_done=0, det_total=2),
            applied=False,
        ),
        rescore=RescoreOut(bulk_operation_id=3, done=10, total=40, creating=False),
    )
    assert banners(every) == [
        ("stale_data", "critical", {}),
        ("critical_alerts", "critical", {"count": 2}),
        ("scoring_paused", "warn", {"reason": "global_fence"}),
        ("paid_search_paused", "warn", {"until": "2026-11-20T00:00:00Z", "reason": "serpapi_disabled"}),
        ("applying_settings", "info", {"done": 1, "total": 6}),
        ("rescoring", "info", {"done": 10, "total": 40}),
    ]
    # a critical pause comes before the critical alerts; the warn rows keep the table's order too
    other = facts(
        pause_reason="schema_unverified",
        critical_unacked=1,
        paid=PaidSearchOut(paused=True, until=None, reason="serpapi_unavailable"),
        scope=ScopeStatusOut(pending_version_id=None, progress=None, applied=False),
    )
    assert banners(other) == [
        ("scoring_paused", "critical", {"reason": "schema_unverified"}),
        ("critical_alerts", "critical", {"count": 1}),
        ("paid_search_paused", "warn", {"until": None, "reason": "serpapi_unavailable"}),
        ("scope_not_applied", "warn", {}),
    ]
    assert banners(facts()) == []
    quiet: tuple[RunAgeState, ...] = ("ok", "warn", "grace", "none")
    for state in quiet:
        assert banners(facts(run_age_state=state)) == []


EXPECTED_PAUSE_BANNERS: Final[dict[PauseReason, tuple[str, str, dict[str, object]]]] = {
    "reservation_bound_violated": ("reservation_bound_violated", "critical", {}),
    "auth_failed": ("scoring_paused", "critical", {"reason": "auth_failed"}),
    "cap_reached": ("cap_reached", "critical", {}),
    "global_fence": ("scoring_paused", "warn", {"reason": "global_fence"}),
    "schema_unverified": ("scoring_paused", "critical", {"reason": "schema_unverified"}),
}


def test_each_pause_reason_has_exactly_one_banner() -> None:
    assert set(EXPECTED_PAUSE_BANNERS) == set(get_args(PauseReason))
    for reason, expected in EXPECTED_PAUSE_BANNERS.items():
        assert banners(facts(pause_reason=reason)) == [expected], reason
    assert banners(facts(pause_reason=None)) == []


def test_applying_settings_counts_both_phases() -> None:
    progress = ScopeProgressOut(done=300, total=1200, det_done=40, det_total=500)
    live = ScopeStatusOut(pending_version_id=9, progress=progress, applied=False)
    assert banners(facts(scope=live)) == [("applying_settings", "info", {"done": 340, "total": 1700})]
    # before its first chunk the live apply reports nothing done
    start = ScopeStatusOut(
        pending_version_id=9,
        progress=ScopeProgressOut(done=0, total=0, det_done=0, det_total=0),
        applied=False,
    )
    assert banners(facts(scope=start)) == [("applying_settings", "info", {"done": 0, "total": 0})]
    # a pending scope without a live apply (it failed); "not applied" waits for the pointer to be cleared
    failed = ScopeStatusOut(pending_version_id=9, progress=None, applied=False)
    assert banners(facts(scope=failed)) == [("applying_settings", "info", {"failed": True})]
    cleared = ScopeStatusOut(pending_version_id=None, progress=None, applied=False)
    assert banners(facts(scope=cleared)) == [("scope_not_applied", "warn", {})]


def test_rescoring_while_targets_remain_or_creation_runs() -> None:
    def rescoring(done: int, total: int, creating: bool) -> list[tuple[str, str, Mapping[str, object]]]:
        return banners(
            facts(rescore=RescoreOut(bulk_operation_id=1, done=done, total=total, creating=creating))
        )

    assert rescoring(3, 5, False) == [("rescoring", "info", {"done": 3, "total": 5})]
    assert rescoring(5, 5, True) == [("rescoring", "info", {"done": 5, "total": 5})]  # targets still coming
    assert rescoring(0, 0, True) == [("rescoring", "info", {"done": 0, "total": 0})]
    assert rescoring(5, 5, False) == []
