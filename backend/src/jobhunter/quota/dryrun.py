"""The quota part of the settings dry-run (spec §6.8): projections and warnings for proposed settings, the
live windows and the run-now estimate of the header strip (§15.1 L5123; plan M4 Task 11, D16, Ruling T11-c).

`quota_projections` covers the `jsearch`, `adzuna`, `arbeitnow`, `serpapi_fallback` and `stale_ttl_short`
parts of §6.8's `dry_run`, on window snapshots the caller read (M2 composes `dry_run()` around it, Deviation
10). It is pure: windows are rolled forward (or bootstrapped) in memory to `now` and nothing is written.
Figures are rounded to 3 decimals in the projections and to 1 in the warning details.

`per_run(settings, w)` is the one computation of a paid provider's units per run, `min(per-run cap, fair
share)`, which the JSearch and SerpApi projections and `run_now_units` share.

`live_windows(conn, settings, now)` is each quota provider's window as a run would find it at `now`: the
stored current one (`windows.current_snapshot`, a plain read) rolled forward in memory exactly as here, so a
stored window that has ended is replaced by its successor, which starts unspent; a provider without a stored
window gets the window its first reservation would bootstrap (JSearch and Adzuna always; SerpApi only from an
account read, so `None`). Nothing is written. The header status computes it once and judges every quota
figure on it (Ruling T11-c).

`run_now_units(settings, windows)` is `per_run` of JSearch and the SerpApi fallback on those windows, rounded
up to whole units: an estimate (the even pace), not a bound, since a run spends what the bucket holds, up to
twice the fair share after an idle stretch and next to nothing right after a run. A JSearch without a stored
window counts the units of the window its first run would bootstrap. A provider the settings disable counts
0, and so does SerpApi without a window or with one not yet validated (`extrapolated_unvalidated`), which
admits no search (Ruling T7-a).

- **JSearch.** `fair_share = N x interval / window length` units per run; `units_per_run = min(per-run cap,
  fair share)`; `refresh_cadence_days = cells x interval_days / units_per_run` (`refresh_cadence_days`, which
  the query lists show too). Warnings:
  `paid_run_cap_binding` (an explicit cap below the fair share), `paid_refresh_slow` (cadence over 7 days) and
  `coverage_gap_guaranteed` (cadence x 24 h + 24 h beyond JSearch's 720 h date window).
- **Adzuna.** Every cell runs every run (its first page, §5.5.3): `calls_per_day = cells x runs per day`. The
  allowance per day is the tightest of the daily cap, the weekly cap over 7 days and the bucket's `N` over its
  window (§6.5); cells rotate (`cadence_days`) when the calls exceed it. `adzuna_rotation` is warned when the
  calls exceed the daily cap (§6.8). With no allowance at all (a monthly cap of 1 leaves a bucket of 0 units)
  a cell is never refreshed: `cadence_days` is then `None` ("never"), in the projection and in the warning,
  never an infinite float, which JSON cannot carry.
- **Arbeitnow.** `page_cap = min(10, max(3, ceil(interval_h / 30) + 1))`, `expected_pages = interval_h / 17.5
  + 1` (page 1 holds about 17.5 h of postings, V8's measurement); `arbeitnow_coverage_risk` when more pages
  are expected than allowed, which is the case above 35 h within §13.1's 1 to 168 h.
- **SerpApi fallback.** `None` before the first account read (no window: no fallback); otherwise its units per
  run, the effective hourly cap and the window's state (an unvalidated window admits nothing, Ruling T7-a).
- **`stale_ttl_short`** when `stale_ttl_days` is below `max_ad_age_days` (§8.11).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil
from types import MappingProxyType
from typing import Final, NamedTuple, get_args

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.quota.pacing import fair_share, per_run_cap
from jobhunter.quota.windows import Provider, Window, current_snapshot, roll_forward
from jobhunter.run_settings import RunSettings
from jobhunter.types import JSONValue

PAID_REFRESH_SLOW_DAYS: Final = 7
JSEARCH_DATE_WINDOW_H: Final = 720  # `date_posted=month`, the widest JSearch window (§5.5.3)
# V8: page 1 holds 300 items spanning about 17.5 h (the only page whose span the cap formula bounds)
ARBEITNOW_HOURS_PER_PAGE: Final = 17.5
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


class PerRun(NamedTuple):
    """A paid provider's units per run on a window: `units = min(cap, fair_share)`."""

    units: float
    fair_share: float
    cap: int


def per_run(settings: RunSettings, w: Window) -> PerRun:
    """The fair share of a run on `w` at the schedule's interval, the per-run cap of `settings` and the units
    per run they allow (module docstring)."""
    fair = fair_share(w, settings.interval_s)
    cap = per_run_cap(settings.quota, fair)
    return PerRun(units=min(cap, fair), fair_share=fair, cap=cap)


def _cadence_days(settings: RunSettings, units_per_run: float, cells: int) -> float:
    return cells * (settings.interval_s / 86400) / max(units_per_run, 1e-9)


def refresh_cadence_days(settings: RunSettings, w: Window, cells: int) -> float:
    """The JSearch projection's `refresh_cadence_days` on `w` for `cells` paid cells, `cells x interval_days /
    units_per_run`, rounded as the projection shows it (module docstring)."""
    return round(_cadence_days(settings, per_run(settings, w).units, cells), 3)


def _jsearch(settings: RunSettings, w: Window, cells: int) -> tuple[list[Warning], dict[str, JSONValue]]:
    warns: list[Warning] = []
    units_per_run, fair, cap = per_run(settings, w)
    if cap < fair:
        warns.append(Warning("paid_run_cap_binding", cap=cap, fair_share=round(fair, 1)))
    cadence_days = _cadence_days(settings, units_per_run, cells)  # the warnings judge it unrounded
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
        "refresh_cadence_days": refresh_cadence_days(settings, w, cells),
        "cells": cells,
    }
    return warns, projection


def _adzuna(settings: RunSettings, w: Window, cells: int) -> tuple[list[Warning], dict[str, JSONValue]]:
    q = settings.quota
    interval_days = settings.interval_s / 86400
    calls_per_day = cells / interval_days
    bucket_per_day = (w.limit_units - w.reserve_units) / _length_days(w)
    allowance_per_day = min(q.adzuna_cap_day, q.adzuna_cap_week / 7, bucket_per_day)
    # None: never (no allowance)
    cadence_days = max(interval_days, cells / allowance_per_day) if allowance_per_day > 0 else None
    warns = []
    if calls_per_day > q.adzuna_cap_day:
        rotation = None if cadence_days is None else round(cadence_days, 1)
        warns.append(Warning("adzuna_rotation", cadence_days=rotation))
    projection: dict[str, JSONValue] = {
        "calls_per_day": round(calls_per_day, 3),
        "allowance_per_day": round(allowance_per_day, 3),
        "cadence_days": None if cadence_days is None else round(cadence_days, 3),
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
    hourly_cap = settings.quota.serpapi_hourly_cap
    if w.observed_hourly_cap is not None:
        hourly_cap = min(hourly_cap, w.observed_hourly_cap)
    return {
        "units_per_run": round(per_run(settings, w).units, 3),
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


@dataclass(frozen=True)
class RunNowUnits:
    """The run-now estimate: whole units of JSearch and of the SerpApi fallback (module docstring)."""

    jsearch_units: int
    serpapi_fallback_units: int


async def live_windows(
    conn: AsyncConnection, settings: RunSettings, now: datetime
) -> dict[Provider, Window | None]:
    """Each quota provider's window at `now`, the stored one rolled forward in memory, or the one its first
    reservation would bootstrap (module docstring). Plain reads, nothing written."""
    return {
        provider: _rolled(await current_snapshot(conn, provider), provider, settings, now)
        for provider in get_args(Provider)
    }


def _admits(provider: Provider, enabled: bool, w: Window | None) -> Window | None:
    if not enabled or w is None or (provider == "serpapi" and w.state == "extrapolated_unvalidated"):
        return None
    return w


def run_now_units(settings: RunSettings, windows: Mapping[Provider, Window | None]) -> RunNowUnits:
    """`per_run(settings, w).units`, rounded up, of JSearch and SerpApi on `windows` (`live_windows`); 0 for a
    provider `settings` disable, and for SerpApi without a validated window (module docstring)."""
    jsearch = _admits("jsearch", settings.sources.jsearch, windows.get("jsearch"))
    serpapi = _admits("serpapi", settings.sources.serpapi, windows.get("serpapi"))
    return RunNowUnits(
        jsearch_units=0 if jsearch is None else ceil(per_run(settings, jsearch).units),
        serpapi_fallback_units=0 if serpapi is None else ceil(per_run(settings, serpapi).units),
    )
