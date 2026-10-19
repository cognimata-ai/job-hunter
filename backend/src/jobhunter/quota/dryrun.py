"""The quota part of the settings dry-run (spec §6.8): projections and warnings for proposed settings.

`quota_projections` covers the `jsearch`, `adzuna`, `arbeitnow`, `serpapi_fallback` and `stale_ttl_short`
parts of §6.8's `dry_run`, on window snapshots the caller read (M2 composes `dry_run()` around it, Deviation
10). It is pure: windows are rolled forward (or bootstrapped) in memory to `now` and nothing is written.
Figures are rounded to 3 decimals in the projections and to 1 in the warning details.

- **JSearch.** `fair_share = N x interval / window length` units per run; `units_per_run = min(per-run cap,
  fair share)`; `refresh_cadence_days = cells x interval_days / units_per_run`. Warnings:
  `paid_run_cap_binding` (an explicit cap below the fair share), `paid_refresh_slow` (cadence over 7 days) and
  `coverage_gap_guaranteed` (cadence x 24 h + 24 h beyond JSearch's 720 h date window).
- **Adzuna.** Every cell runs every run (its first page, §5.5.3): `calls_per_day = cells x runs per day`. The
  allowance per day is the tightest of the daily cap, the weekly cap over 7 days and the bucket's `N` over its
  window (§6.5); cells rotate (`cadence_days`) when the calls exceed it. `adzuna_rotation` is warned when the
  calls exceed the daily cap (§6.8).
- **Arbeitnow.** `page_cap = min(10, max(3, ceil(interval_h / 30) + 1))`, `expected_pages = interval_h / 37 +
  1` (page 1 holds about 37 h of postings); `arbeitnow_coverage_risk` when more pages are expected than
  allowed.
- **SerpApi fallback.** `None` before the first account read (no window: no fallback); otherwise its units per
  run, the effective hourly cap and the window's state (an unvalidated window admits nothing, Ruling T7-a).
- **`stale_ttl_short`** when `stale_ttl_days` is below `max_ad_age_days` (§8.11).
"""

from collections.abc import Mapping
from datetime import datetime, timedelta
from math import ceil
from types import MappingProxyType
from typing import Final

from jobhunter.quota.pacing import fair_share, per_run_cap
from jobhunter.quota.windows import Provider, Window, roll_forward
from jobhunter.run_settings import RunSettings
from jobhunter.types import JSONValue

PAID_REFRESH_SLOW_DAYS: Final = 7
JSEARCH_DATE_WINDOW_H: Final = 720  # `date_posted=month`, the widest JSearch window (§5.5.3)
ARBEITNOW_HOURS_PER_PAGE: Final = 37
_DAY: Final = timedelta(days=1)


class Warning:
    """A dry-run warning, a code and its details (§6.8 `W(code, **detail)`): acknowledged on save."""

    __slots__ = ("code", "detail")

    def __init__(self, code: str, **detail: JSONValue) -> None:
        self.code = code
        self.detail: Mapping[str, JSONValue] = MappingProxyType(dict(detail))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Warning) and (self.code, dict(self.detail)) == (
            other.code,
            dict(other.detail),
        )

    def __hash__(self) -> int:
        return hash((self.code, tuple(sorted(self.detail))))

    def __repr__(self) -> str:
        details = ", ".join(f"{name}={value!r}" for name, value in self.detail.items())
        return f"Warning({self.code!r}{', ' if details else ''}{details})"


def _rolled(w: Window | None, provider: Provider, settings: RunSettings, now: datetime) -> Window | None:
    rolled, _ = roll_forward(w, provider, now, settings.quota, interval_s=settings.interval_s)
    return rolled


def _length_days(w: Window) -> float:
    return (w.window_end - w.window_start) / _DAY


def _jsearch(settings: RunSettings, w: Window, cells: int) -> tuple[list[Warning], dict[str, JSONValue]]:
    warns: list[Warning] = []
    fair = fair_share(w, settings.interval_s)
    cap = per_run_cap(settings.quota, fair)
    units_per_run = min(cap, fair)
    if cap < fair:
        warns.append(Warning("paid_run_cap_binding", cap=cap, fair_share=round(fair, 1)))
    interval_days = settings.interval_s / 86400
    cadence_days = cells * interval_days / max(units_per_run, 1e-9)
    if cadence_days > PAID_REFRESH_SLOW_DAYS:
        warns.append(Warning("paid_refresh_slow", cadence_days=round(cadence_days, 1)))
    if cadence_days * 24 + 24 > JSEARCH_DATE_WINDOW_H:
        warns.append(
            Warning(
                "coverage_gap_guaranteed", detail="date window would exceed 'month'; gaps will be recorded"
            )
        )
    projection: dict[str, JSONValue] = {
        "units_per_run": round(units_per_run, 3),
        "refresh_cadence_days": round(cadence_days, 3),
        "cells": cells,
    }
    return warns, projection


def _adzuna(settings: RunSettings, w: Window, cells: int) -> tuple[list[Warning], dict[str, JSONValue]]:
    q = settings.quota
    interval_days = settings.interval_s / 86400
    calls_per_day = cells / interval_days
    bucket_per_day = (w.limit_units - w.reserve_units) / _length_days(w)
    allowance_per_day = min(q.adzuna_cap_day, q.adzuna_cap_week / 7, bucket_per_day)
    cadence_days = max(interval_days, cells / allowance_per_day) if allowance_per_day > 0 else float("inf")
    warns = []
    if calls_per_day > q.adzuna_cap_day:
        warns.append(Warning("adzuna_rotation", cadence_days=round(cadence_days, 1)))
    projection: dict[str, JSONValue] = {
        "calls_per_day": round(calls_per_day, 3),
        "allowance_per_day": round(allowance_per_day, 3),
        "cadence_days": round(cadence_days, 3),
        "cells": cells,
    }
    return warns, projection


def _arbeitnow(settings: RunSettings) -> tuple[list[Warning], dict[str, JSONValue]]:
    interval_h = settings.interval_s / 3600
    page_cap = min(10, max(3, ceil(interval_h / 30) + 1))
    expected_pages = interval_h / ARBEITNOW_HOURS_PER_PAGE + 1
    warns = [Warning("arbeitnow_coverage_risk")] if expected_pages > page_cap else []
    return warns, {"page_cap": page_cap, "expected_pages": round(expected_pages, 3)}


def _serpapi(settings: RunSettings, w: Window | None) -> JSONValue:
    if w is None:
        return None  # no account read yet: SerpApi has no window, so no fallback is admitted
    fair = fair_share(w, settings.interval_s)
    hourly_cap = settings.quota.serpapi_hourly_cap
    if w.observed_hourly_cap is not None:
        hourly_cap = min(hourly_cap, w.observed_hourly_cap)
    return {
        "units_per_run": round(min(per_run_cap(settings.quota, fair), fair), 3),
        "hourly_cap": hourly_cap,
        "state": w.state,
    }


def quota_projections(
    settings: RunSettings,
    *,
    jsearch: Window | None,
    adzuna: Window | None,
    serpapi: Window | None,
    cells: int,
    now: datetime,
) -> tuple[list[Warning], dict[str, JSONValue]]:
    """The quota warnings and projections of the proposed `settings` (§6.8) for `cells` paid cells per source,
    on the current window snapshots (`None` when a provider has none)."""
    warns: list[Warning] = []
    proj: dict[str, JSONValue] = {}
    jw, aw = _rolled(jsearch, "jsearch", settings, now), _rolled(adzuna, "adzuna", settings, now)
    if jw is None or aw is None:
        raise ValueError("JSearch and Adzuna windows always bootstrap")
    for part, (found, projection) in (
        ("jsearch", _jsearch(settings, jw, cells)),
        ("adzuna", _adzuna(settings, aw, cells)),
        ("arbeitnow", _arbeitnow(settings)),
    ):
        warns += found
        proj[part] = projection
    max_age = settings.scope.max_ad_age_days
    if max_age is not None and settings.stale_ttl_days < max_age:
        warns.append(Warning("stale_ttl_short", label_after_days=settings.stale_ttl_days))
    proj["serpapi_fallback"] = _serpapi(settings, _rolled(serpapi, "serpapi", settings, now))
    return warns, proj
