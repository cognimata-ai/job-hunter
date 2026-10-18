"""The typed view of a settings version that a collection run reads (spec §13.1).

M1 reads the stored payload field by field (a malformed one raises `KeyError`, `TypeError` or `ValueError`);
M2's Pydantic settings model replaces `load_run_settings`. A scope location's `key` is the slug of its label
(`slug`): the `coverage_cells.location_key` of its cells. `scope_from_payload` reads the `scope` object of a
settings payload, and also a `scope_versions.payload`, which M0 stores without `max_ad_age_days`
(`seed.scope_payload`): its `max_ad_age_days` is then `None`.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, cast

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.normalize.text import fold
from jobhunter.types import JSONValue, get_bool, get_int, get_list, get_object, get_str, json_get

_NOT_ALPHANUMERIC: Final = re.compile(r"[\W_]+")


def slug(label: str) -> str:
    """`fold(label)` with every run of non-alphanumerics replaced by one `-`, without a `-` at either end."""
    key = _NOT_ALPHANUMERIC.sub("-", fold(label)).strip("-")
    if not key:
        raise ValueError(f"no slug for the location label {label!r}")
    return key


@dataclass(frozen=True)
class ScopeLocation:
    label: str
    gazetteer_id: str
    lat: float
    lon: float
    radius_km: int
    key: str  # slug(label)


@dataclass(frozen=True)
class SeniorityRange:
    mode: Literal["profile_relative", "explicit"]
    below: int
    above: int
    min: str | None
    max: str | None


@dataclass(frozen=True)
class ScopeSettings:
    locations: tuple[ScopeLocation, ...]
    remote_within_germany: bool
    work_modes: frozenset[str]
    employment_types: frozenset[str]
    seniority_range: SeniorityRange
    industries_exclude: frozenset[str]  # case-folded
    accepted_ad_languages: frozenset[str]
    excluded_companies: frozenset[str]  # company keys
    excluded_keywords: tuple[str, ...]
    exclude_keywords_in_description: bool
    max_ad_age_days: int | None  # None for a scope built from a `scope_versions.payload`


@dataclass(frozen=True)
class SourcesEnabled:
    jsearch: bool
    serpapi: bool
    adzuna: bool
    arbeitnow: bool
    ats: bool


@dataclass(frozen=True)
class QuotaSettings:
    jsearch_monthly_limit: int
    serpapi_monthly_limit: int
    reserve_pct: int
    serpapi_hourly_cap: int
    adzuna_cap_minute: int
    adzuna_cap_day: int
    adzuna_cap_week: int
    adzuna_cap_month: int
    paid_max_requests_per_run: int | Literal["auto"]


@dataclass(frozen=True)
class RunSettings:
    settings_version_id: int
    scope_version_id: int
    scope: ScopeSettings
    sources: SourcesEnabled
    quota: QuotaSettings
    queries: Mapping[int, bool]  # query id -> enabled (§13.1 `queries[]`)
    watchlist: Mapping[int, bool]  # watchlist company id -> enabled
    stale_ttl_days: int
    interval_s: int
    schedule_include_paid: bool


def _strings(doc: JSONValue, path: str) -> list[str]:
    return [get_str(item, "") for item in get_list(doc, path)]


def _optional_str(doc: JSONValue, path: str) -> str | None:
    return None if json_get(doc, path) is None else get_str(doc, path)


def _number(doc: JSONValue, path: str) -> float:
    value = json_get(doc, path)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{path}: expected number, got {type(value).__name__}")
    return float(value)


def _location(doc: JSONValue) -> ScopeLocation:
    label = get_str(doc, "label")
    return ScopeLocation(
        label=label,
        gazetteer_id=get_str(doc, "gazetteer_id"),
        lat=_number(doc, "lat"),
        lon=_number(doc, "lon"),
        radius_km=get_int(doc, "radius_km"),
        key=slug(label),
    )


def _seniority(doc: JSONValue) -> SeniorityRange:
    mode = get_str(doc, "mode")
    if mode not in ("profile_relative", "explicit"):
        raise ValueError(f"seniority_range.mode: unknown mode {mode!r}")
    return SeniorityRange(
        mode=cast(Literal["profile_relative", "explicit"], mode),
        below=get_int(doc, "below"),
        above=get_int(doc, "above"),
        min=_optional_str(doc, "min"),
        max=_optional_str(doc, "max"),
    )


def scope_from_payload(payload: Mapping[str, JSONValue]) -> ScopeSettings:
    """The scope of a settings payload's `scope` object, or of a `scope_versions.payload`."""
    doc: JSONValue = dict(payload)
    return ScopeSettings(
        locations=tuple(_location(item) for item in get_list(doc, "locations")),
        remote_within_germany=get_bool(doc, "remote_within_germany"),
        work_modes=frozenset(_strings(doc, "work_modes")),
        employment_types=frozenset(_strings(doc, "employment_types")),
        seniority_range=_seniority(get_object(doc, "seniority_range")),
        industries_exclude=frozenset(fold(industry) for industry in _strings(doc, "industries_exclude")),
        accepted_ad_languages=frozenset(_strings(doc, "accepted_ad_languages")),
        excluded_companies=frozenset(_strings(doc, "excluded_companies")),
        excluded_keywords=tuple(_strings(doc, "excluded_keywords")),
        exclude_keywords_in_description=get_bool(doc, "exclude_keywords_in_description"),
        max_ad_age_days=None if payload.get("max_ad_age_days") is None else get_int(doc, "max_ad_age_days"),
    )


def _enabled(doc: JSONValue, path: str, id_key: str) -> dict[int, bool]:
    return {get_int(entry, id_key): get_bool(entry, "enabled") for entry in get_list(doc, path)}


def _paid_max(doc: JSONValue) -> int | Literal["auto"]:
    if json_get(doc, "quota.paid_max_requests_per_run") == "auto":
        return "auto"
    return get_int(doc, "quota.paid_max_requests_per_run")


_VERSION = q(
    "SELECT v.payload, v.scope_version_id, s.interval_s FROM settings_versions v CROSS JOIN schedule_state s "
    "WHERE v.id = :id"
)


async def load_run_settings(conn: AsyncConnection, settings_version_id: int) -> RunSettings:
    """The run's view of settings version `settings_version_id`; `interval_s` is the schedule's (§3.4).

    `stale_ttl_days` is `collection.stale_ttl_days`, or `scope.max_ad_age_days` when that is `null` (§13.1).
    """
    row = (await conn.execute(_VERSION, {"id": settings_version_id})).first()
    if row is None:
        raise LookupError(f"settings version {settings_version_id} (or the schedule) does not exist")
    doc: JSONValue = row.payload
    scope = scope_from_payload(get_object(doc, "scope"))
    if scope.max_ad_age_days is None:
        raise KeyError("scope.max_ad_age_days")
    stale_ttl = json_get(doc, "collection.stale_ttl_days")
    return RunSettings(
        settings_version_id=settings_version_id,
        scope_version_id=int(row.scope_version_id),
        scope=scope,
        sources=SourcesEnabled(
            jsearch=get_bool(doc, "sources.enabled.jsearch"),
            serpapi=get_bool(doc, "sources.enabled.serpapi"),
            adzuna=get_bool(doc, "sources.enabled.adzuna"),
            arbeitnow=get_bool(doc, "sources.enabled.arbeitnow"),
            ats=get_bool(doc, "sources.enabled.ats"),
        ),
        quota=QuotaSettings(
            jsearch_monthly_limit=get_int(doc, "quota.jsearch_monthly_limit"),
            serpapi_monthly_limit=get_int(doc, "quota.serpapi_monthly_limit"),
            reserve_pct=get_int(doc, "quota.reserve_pct"),
            serpapi_hourly_cap=get_int(doc, "quota.serpapi_hourly_cap"),
            adzuna_cap_minute=get_int(doc, "quota.adzuna_cap_minute"),
            adzuna_cap_day=get_int(doc, "quota.adzuna_cap_day"),
            adzuna_cap_week=get_int(doc, "quota.adzuna_cap_week"),
            adzuna_cap_month=get_int(doc, "quota.adzuna_cap_month"),
            paid_max_requests_per_run=_paid_max(doc),
        ),
        queries=_enabled(doc, "queries", "query_id"),
        watchlist=_enabled(doc, "watchlist", "watchlist_company_id"),
        stale_ttl_days=scope.max_ad_age_days
        if stale_ttl is None
        else get_int(doc, "collection.stale_ttl_days"),
        interval_s=int(row.interval_s),
        schedule_include_paid=get_bool(doc, "schedule.include_paid"),
    )
