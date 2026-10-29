from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from jobhunter.quota.dryrun import Warning, quota_projections
from jobhunter.quota.windows import AccountObservation, Provider, Window, bootstrap_window
from jobhunter.settings.model import Schedule

from tests.support.quota import SETTINGS, account, seeded_run_settings

NOW = datetime(2026, 10, 19, 6, tzinfo=UTC)
SIX_H = 21600


def boot(provider: Provider, *, account: AccountObservation | None = None) -> Window:
    w = bootstrap_window(provider, NOW, SETTINGS, interval_s=SIX_H, account=account)
    assert w is not None
    return w


def codes(warnings: list[Warning]) -> set[str]:
    return {w.code for w in warnings}


def test_projections_with_defaults_and_six_hour_interval() -> None:
    settings = seeded_run_settings()
    warns, proj = quota_projections(
        settings, jsearch=boot("jsearch"), adzuna=boot("adzuna"), serpapi=None, cells=4, now=NOW
    )
    assert proj["jsearch"] == {"units_per_run": 1.453, "refresh_cadence_days": 0.688, "cells": 4}
    assert proj["arbeitnow"] == {"page_cap": 3, "expected_pages": 1.343}
    assert proj["adzuna"] == {
        "calls_per_day": 16.0,
        "allowance_per_day": 67.5,
        "cadence_days": 0.25,
        "cells": 4,
    }
    assert proj["serpapi_fallback"] is None  # no account read yet: no fallback capacity
    assert warns == []
    # the same with 40 cells and stale_ttl_days 7: 40 x 0.25 d / 1.4533 = 6.881 days, not more than 7 (§6.8)
    short = replace(settings, stale_ttl_days=7)
    warns, proj = quota_projections(
        short, jsearch=boot("jsearch"), adzuna=boot("adzuna"), serpapi=None, cells=40, now=NOW
    )
    assert proj["jsearch"] == {"units_per_run": 1.453, "refresh_cadence_days": 6.881, "cells": 40}
    assert codes(warns) == {"stale_ttl_short"}
    assert [w.detail for w in warns] == [{"label_after_days": 7}]
    # 42 cells take 7.225 days to refresh: the codes are exactly {"paid_refresh_slow", "stale_ttl_short"}
    warns, _ = quota_projections(
        short, jsearch=boot("jsearch"), adzuna=boot("adzuna"), serpapi=None, cells=42, now=NOW
    )
    assert codes(warns) == {"paid_refresh_slow", "stale_ttl_short"}
    assert next(w for w in warns if w.code == "paid_refresh_slow").detail == {"cadence_days": 7.2}


def test_absent_windows_are_simulated_and_nothing_else_is_needed() -> None:
    settings = seeded_run_settings()
    absent = quota_projections(settings, jsearch=None, adzuna=None, serpapi=None, cells=4, now=NOW)
    assert absent == quota_projections(
        settings, jsearch=boot("jsearch"), adzuna=boot("adzuna"), serpapi=None, cells=4, now=NOW
    )


def test_a_binding_run_cap_a_guaranteed_gap_and_adzuna_rotation() -> None:
    weekly = seeded_run_settings(interval_s=7 * 86400)
    capped = replace(weekly, quota=replace(SETTINGS, paid_max_requests_per_run=10))
    warns, proj = quota_projections(capped, jsearch=None, adzuna=None, serpapi=None, cells=4, now=NOW)
    # 180 units over 30.96 days, 7 days per run: a fair share of 40.7 against an explicit cap of 10
    assert proj["jsearch"] == {"units_per_run": 10, "refresh_cadence_days": 2.8, "cells": 4}
    # (and the Arbeitnow risk: 7 days expect 10.6 pages against a cap of 7, V8's 17.5 h per page)
    assert [(w.code, w.detail) for w in warns] == [
        ("paid_run_cap_binding", {"cap": 10, "fair_share": 40.7}),
        ("arbeitnow_coverage_risk", {}),
    ]
    warns, proj = quota_projections(
        seeded_run_settings(), jsearch=None, adzuna=None, serpapi=None, cells=200, now=NOW
    )
    # 200 cells at 1.45 units per run: 34.4 days per refresh, past JSearch's 720 h window; Adzuna 800 a day
    assert codes(warns) == {"paid_refresh_slow", "coverage_gap_guaranteed", "adzuna_rotation"}
    hourly = seeded_run_settings(interval_s=3600)
    warns, proj = quota_projections(hourly, jsearch=None, adzuna=None, serpapi=None, cells=10, now=NOW)
    # 10 cells every hour are 240 calls a day, above the 225 of the daily cap; 67.5 a day are paced
    assert proj["adzuna"] == {
        "calls_per_day": 240.0,
        "allowance_per_day": 67.5,
        "cadence_days": 0.148,
        "cells": 10,
    }
    assert [(w.code, w.detail) for w in warns] == [("adzuna_rotation", {"cadence_days": 0.1})]


def test_arbeitnow_risk_beyond_the_page_cap() -> None:
    # a 400 h interval (beyond the 7-day maximum of the settings) expects 23.9 pages against a cap of 10
    long = seeded_run_settings(interval_s=400 * 3600)
    warns, proj = quota_projections(long, jsearch=None, adzuna=None, serpapi=None, cells=1, now=NOW)
    assert proj["arbeitnow"] == {"page_cap": 10, "expected_pages": 23.857}
    assert "arbeitnow_coverage_risk" in codes(warns)


def _interval_hours_range() -> range:
    """§13.1's `schedule.interval_hours` range, read from the settings model (1 to 168)."""
    metadata = Schedule.model_fields["interval_hours"].metadata
    (low,) = [m.ge for m in metadata if hasattr(m, "ge")]
    (high,) = [m.le for m in metadata if hasattr(m, "le")]
    return range(low, high + 1)


@pytest.mark.parametrize(
    ("hours", "page_cap", "expected_pages", "warns"),
    [
        # `page_cap = min(10, max(3, ceil(h / 30) + 1))`, `expected_pages = h / 17.5 + 1` (V8: page 1 holds
        # about 17.5 h of postings); the warning is `expected_pages > page_cap`, first true at 36 h
        (24, 3, 2.371, False),
        (35, 3, 3.0, False),  # 3.0 pages against a cap of 3: not more pages than allowed
        (36, 3, 3.057, True),
        (48, 3, 3.743, True),
        (72, 4, 5.114, True),
        (120, 5, 7.857, True),
        (168, 7, 10.6, True),
    ],
)
def test_arbeitnow_warning_fires_above_35_hours(
    hours: int, page_cap: int, expected_pages: float, warns: bool
) -> None:
    """Spec 3.5 (U3, V8): page 1 holds about 17.5 h, so the warning fires above 35 h of the settings range."""
    settings = seeded_run_settings(interval_s=hours * 3600)
    found, proj = quota_projections(settings, jsearch=None, adzuna=None, serpapi=None, cells=1, now=NOW)
    assert proj["arbeitnow"] == {"page_cap": page_cap, "expected_pages": expected_pages}
    assert [w for w in found if w.code == "arbeitnow_coverage_risk"] == (
        [Warning("arbeitnow_coverage_risk")] if warns else []
    )


@pytest.mark.parametrize("hours", _interval_hours_range())
def test_arbeitnow_warning_threshold_holds_for_every_interval_hour(hours: int) -> None:
    """The warning is raised for every interval §13.1 allows above 35 h and for none up to it (the cap steps
    up by one at 61, 91, 121 and 151 h, and the expected pages stay ahead of it from 36 h on)."""
    settings = seeded_run_settings(interval_s=hours * 3600)
    warns, proj = quota_projections(settings, jsearch=None, adzuna=None, serpapi=None, cells=1, now=NOW)
    assert ("arbeitnow_coverage_risk" in codes(warns)) == (hours > 35)
    arbeitnow = proj["arbeitnow"]
    assert isinstance(arbeitnow, dict)
    pages, cap = arbeitnow["expected_pages"], arbeitnow["page_cap"]
    assert isinstance(pages, float | int) and isinstance(cap, int)
    assert (pages > cap) == (hours > 35)  # the projection agrees with the warning


def test_the_sweep_covers_the_whole_settings_range() -> None:
    assert _interval_hours_range() == range(1, 169)


def test_serpapi_fallback_capacity_from_its_window() -> None:
    renewal = datetime(2026, 11, 8, tzinfo=UTC)  # the month [Oct 8, Nov 8) has 31 days
    serpapi = boot("serpapi", account=account(NOW, renewal))
    _, proj = quota_projections(
        seeded_run_settings(), jsearch=None, adzuna=None, serpapi=serpapi, cells=4, now=NOW
    )
    assert proj["serpapi_fallback"] == {"units_per_run": 1.815, "hourly_cap": 45, "state": "validated"}
    # after the local rollover and before a read of the new renewal, the fallback is closed (Ruling T7-a)
    _, proj = quota_projections(
        seeded_run_settings(),
        jsearch=None,
        adzuna=None,
        serpapi=serpapi,
        cells=4,
        now=renewal + timedelta(hours=1),
    )
    fallback = proj["serpapi_fallback"]
    assert isinstance(fallback, dict) and fallback["state"] == "extrapolated_unvalidated"


def test_no_adzuna_allowance_is_a_cadence_of_never() -> None:
    # a monthly cap of 1 reserves its one unit (N = 0.9 x 1, rounded down): no allowance, so no cell is ever
    # refreshed; `cadence_days` is None (JSON null), never an infinite float, in the projection and warning
    capped = replace(seeded_run_settings(), quota=replace(SETTINGS, adzuna_cap_month=1, adzuna_cap_day=10))
    warns, proj = quota_projections(capped, jsearch=None, adzuna=None, serpapi=None, cells=4, now=NOW)
    assert proj["adzuna"] == {
        "calls_per_day": 16.0,
        "allowance_per_day": 0.0,
        "cadence_days": None,
        "cells": 4,
    }
    assert [(w.code, w.detail) for w in warns] == [("adzuna_rotation", {"cadence_days": None})]
