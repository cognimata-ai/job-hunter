"""Quota test helpers: the seeded settings (Task 7) and, for the ledger (Task 8), account reads, a seeded run
settings view and a window written as it is."""

import json
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from importlib import resources
from typing import Any, Final

from jobhunter.quota.windows import AccountObservation, Insert, Window, apply_changes, bootstrap_window
from jobhunter.run_settings import QuotaSettings, RunSettings, SourcesEnabled, scope_from_payload
from sqlalchemy.ext.asyncio import AsyncEngine

# The seeded `quota` settings (`settings_v1.json`, Task 5): JSearch 200, SerpApi 250, reserve 10 %, SerpApi
# hourly cap 45, Adzuna 20 / 225 / 900 / 2,250, `paid_max_requests_per_run` auto.
SETTINGS: Final = QuotaSettings(200, 250, 10, 45, 20, 225, 900, 2250, "auto")
SIX_H: Final = 21600


def account(
    observed_at: datetime,
    renewal: datetime,
    *,
    left: int = 250,
    this_hour: int = 0,
    per_month: int = 250,
    rate_cap: int = 250,
) -> AccountObservation:
    """A SerpApi `account.json` read at `observed_at` for the plan renewing at `renewal`."""
    return AccountObservation(
        observed_at=observed_at,
        plan_renewal_date=renewal,
        searches_per_month=per_month,
        plan_searches_left=left,
        this_hour_searches=this_hour,
        account_rate_limit_per_hour=rate_cap,
    )


def seeded_payload() -> dict[str, Any]:
    """The seeded settings version 1 (`settings_v1.json`) as a document."""
    text = resources.files("jobhunter.db").joinpath("seed_data/settings_v1.json").read_text(encoding="utf-8")
    payload: dict[str, Any] = json.loads(text)
    return payload


def seeded_run_settings(*, interval_s: int = SIX_H, stale_ttl_days: int | None = None) -> RunSettings:
    """The run settings view of the seeded version (no database): its scope, sources and quota, the schedule's
    `interval_s`, and `stale_ttl_days` (`scope.max_ad_age_days` when not given, as for a `null` setting)."""
    payload = seeded_payload()
    scope = scope_from_payload(payload["scope"])
    enabled = payload["sources"]["enabled"]
    assert scope.max_ad_age_days is not None
    return RunSettings(
        settings_version_id=1,
        scope_version_id=1,
        scope=scope,
        sources=SourcesEnabled(**enabled),
        quota=SETTINGS,
        queries={},
        watchlist={},
        stale_ttl_days=scope.max_ad_age_days if stale_ttl_days is None else stale_ttl_days,
        interval_s=interval_s,
        schedule_include_paid=bool(payload["schedule"]["include_paid"]),
    )


async def insert_window(engine: AsyncEngine, w: Window) -> None:
    """Write `w` as the provider's current window (committed), exactly as given."""
    async with engine.begin() as conn:
        await apply_changes(conn, [Insert(replace(w))])


def jsearch_window(now: datetime, *, limit: int, reserve: int, tokens: int, interval_s: int) -> Window:
    """A validated JSearch window starting at `now` with the given limit, reserve and bucket."""
    boot = bootstrap_window("jsearch", now, SETTINGS, interval_s=interval_s, account=None)
    assert boot is not None
    return replace(
        boot,
        state="validated",
        limit_units=limit,
        reserve_units=reserve,
        tokens=Decimal(tokens),
        capacity=max(Decimal(tokens), Decimal(1)),
        tokens_at=now,
    )
