"""The typed view of a settings version that a collection run reads (spec §13.1).

The frozen dataclasses are the collection pipeline's view, unchanged since M1. They are built from the
Pydantic settings model (`settings.model.Settings`, M1 Carry-forward 1): `load_run_settings` validates the
stored payload with it (a malformed one raises `pydantic.ValidationError`, a `ValueError`), and
`RunSettings.from_settings` takes the view. The model checks each field alone; the rules across fields are the
save's (`settings.validate.validate_fields`), so any stored version loads.

`ScopeSettings` is also the scoring's one view of a scope (M3): beside the fields of the hard filters it holds
the scoring policies of the payload, `industries_include` (case-folded, like `industries_exclude`),
`language_policy_default`, `language_policy`, `min_salary_eur_year` and `knockout_severity` (the
`KnockoutSeverity` fields by knockout code), so the hard filters, the knockouts and the deterministic criteria
read the same values.

A scope location's `key` is the slug of its label (`slug`): the `coverage_cells.location_key` of its cells.
`scope_from_payload` reads the `scope` object of a settings payload, and also a `scope_versions.payload`,
which M0 stores without `max_ad_age_days` (`seed.scope_payload`): its `max_ad_age_days` is then `None`.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, Self

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.normalize.text import fold
from jobhunter.settings import model
from jobhunter.types import JSONValue

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
    industries_include: frozenset[str]  # case-folded
    industries_exclude: frozenset[str]  # case-folded
    accepted_ad_languages: frozenset[str]
    language_policy_default: model.Severity
    language_policy: Mapping[str, model.Severity]  # by language code
    excluded_companies: frozenset[str]  # company keys
    excluded_keywords: tuple[str, ...]
    exclude_keywords_in_description: bool
    min_salary_eur_year: int | None
    knockout_severity: Mapping[str, model.Severity]  # by knockout code (§11.4)
    max_ad_age_days: int | None  # None for a scope built from a `scope_versions.payload`

    @classmethod
    def from_model(cls, scope: model.ScopeVersionPayload) -> Self:
        """The view of a validated scope; `max_ad_age_days` is `None` unless `scope` is a `model.Scope`."""
        seniority = scope.seniority_range
        language_policy: dict[str, model.Severity] = {
            code: severity for code, severity in scope.language_policy.items()
        }
        return cls(
            locations=tuple(
                ScopeLocation(
                    label=location.label,
                    gazetteer_id=location.gazetteer_id,
                    lat=location.lat,
                    lon=location.lon,
                    radius_km=location.radius_km,
                    key=slug(location.label),
                )
                for location in scope.locations
            ),
            remote_within_germany=scope.remote_within_germany,
            work_modes=frozenset(scope.work_modes),
            employment_types=frozenset(scope.employment_types),
            seniority_range=SeniorityRange(
                mode=seniority.mode,
                below=seniority.below,
                above=seniority.above,
                min=seniority.min,
                max=seniority.max,
            ),
            industries_include=frozenset(fold(industry) for industry in scope.industries_include),
            industries_exclude=frozenset(fold(industry) for industry in scope.industries_exclude),
            accepted_ad_languages=frozenset(scope.accepted_ad_languages),
            language_policy_default=scope.language_policy_default,
            language_policy=MappingProxyType(language_policy),
            excluded_companies=frozenset(scope.excluded_companies),
            excluded_keywords=tuple(scope.excluded_keywords),
            exclude_keywords_in_description=scope.exclude_keywords_in_description,
            min_salary_eur_year=scope.min_salary_eur_year,
            knockout_severity=MappingProxyType(dict(scope.knockout_severity)),
            max_ad_age_days=scope.max_ad_age_days if isinstance(scope, model.Scope) else None,
        )


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

    @classmethod
    def from_settings(
        cls, settings: model.Settings, *, settings_version_id: int, scope_version_id: int, interval_s: int
    ) -> Self:
        """The run's view of validated `settings`; `interval_s` is the schedule's (§3.4), and `stale_ttl_days`
        is `collection.stale_ttl_days`, or `scope.max_ad_age_days` when that is `null` (§13.1)."""
        enabled = settings.sources.enabled
        quota = settings.quota
        stale_ttl = settings.collection.stale_ttl_days
        return cls(
            settings_version_id=settings_version_id,
            scope_version_id=scope_version_id,
            scope=ScopeSettings.from_model(settings.scope),
            sources=SourcesEnabled(
                jsearch=enabled.jsearch,
                serpapi=enabled.serpapi,
                adzuna=enabled.adzuna,
                arbeitnow=enabled.arbeitnow,
                ats=enabled.ats,
            ),
            quota=QuotaSettings(
                jsearch_monthly_limit=quota.jsearch_monthly_limit,
                serpapi_monthly_limit=quota.serpapi_monthly_limit,
                reserve_pct=quota.reserve_pct,
                serpapi_hourly_cap=quota.serpapi_hourly_cap,
                adzuna_cap_minute=quota.adzuna_cap_minute,
                adzuna_cap_day=quota.adzuna_cap_day,
                adzuna_cap_week=quota.adzuna_cap_week,
                adzuna_cap_month=quota.adzuna_cap_month,
                paid_max_requests_per_run=quota.paid_max_requests_per_run,
            ),
            queries={entry.query_id: entry.enabled for entry in settings.queries},
            watchlist={entry.watchlist_company_id: entry.enabled for entry in settings.watchlist},
            stale_ttl_days=settings.scope.max_ad_age_days if stale_ttl is None else stale_ttl,
            interval_s=interval_s,
            schedule_include_paid=settings.schedule.include_paid,
        )


def scope_from_payload(payload: Mapping[str, JSONValue]) -> ScopeSettings:
    """The scope of a settings payload's `scope` object, or of a `scope_versions.payload` (which has no
    `max_ad_age_days`)."""
    doc = dict(payload)
    if "max_ad_age_days" in doc:
        return ScopeSettings.from_model(model.Scope.model_validate(doc))
    return ScopeSettings.from_model(model.ScopeVersionPayload.model_validate(doc))


_VERSION = q(
    "SELECT v.payload, v.scope_version_id, s.interval_s FROM settings_versions v CROSS JOIN schedule_state s "
    "WHERE v.id = :id"
)


async def load_run_settings(conn: AsyncConnection, settings_version_id: int) -> RunSettings:
    """The run's view of settings version `settings_version_id` (`RunSettings.from_settings`); `interval_s` is
    the schedule's (§3.4)."""
    row = (await conn.execute(_VERSION, {"id": settings_version_id})).first()
    if row is None:
        raise LookupError(f"settings version {settings_version_id} (or the schedule) does not exist")
    return RunSettings.from_settings(
        model.Settings.model_validate(row.payload),
        settings_version_id=settings_version_id,
        scope_version_id=int(row.scope_version_id),
        interval_s=int(row.interval_s),
    )
