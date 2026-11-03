"""Quota test helpers: the seeded settings (Task 7) and, for the ledger (Task 8), account reads, a seeded run
settings view, a window written as it is and (M4 Task 10) a ledger entry written as it is."""

import json
import os
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from importlib import resources
from typing import Any, Final, Literal

from jobhunter.db.sql import q
from jobhunter.quota.windows import (
    AccountObservation,
    Insert,
    Provider,
    Window,
    apply_changes,
    bootstrap_window,
)
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


LedgerState = Literal["reserved", "dispatched", "billed", "not_billed", "ambiguous"]  # the `state` CHECK
_SETTLED: Final = frozenset({"billed", "not_billed", "ambiguous"})
_LEDGER_REQUEST: Final = q(
    "INSERT INTO quota_ledger (provider, window_start, kind, request_class, request_key, attempt_seq, units, "
    "state, evidence, per_run_cap, dispatched_at, settled_at) VALUES (:p, :ws, 'request', 'normal', :key, 1, "
    ":units, :state, :evidence, 1, CASE WHEN :dispatched THEN now() END, CASE WHEN :settled THEN now() END)"
)
_LEDGER_ADJUSTMENT: Final = q(
    "INSERT INTO quota_ledger (provider, window_start, kind, units, evidence) "
    "VALUES (:p, :ws, 'adjustment', :units, 'provider_remaining')"
)


async def insert_ledger_entry(
    engine: AsyncEngine, provider: Provider, window_start: datetime, *, units: int, state: LedgerState | None
) -> None:
    """A ledger row of the window `(provider, window_start)`, written as it is (committed): a normal-class
    `request` of a fresh key in `state` with the columns that state needs (`not_billed` as never dispatched),
    or an `adjustment` when `state` is `None`."""
    params: dict[str, object] = {"p": provider, "ws": window_start, "units": units}
    async with engine.begin() as conn:
        if state is None:
            await conn.execute(_LEDGER_ADJUSTMENT, params)
            return
        await conn.execute(
            _LEDGER_REQUEST,
            {
                **params,
                "key": os.urandom(32),
                "state": state,
                "evidence": "never_dispatched" if state == "not_billed" else None,
                "dispatched": state not in {"reserved", "not_billed"},
                "settled": state in _SETTLED,
            },
        )
