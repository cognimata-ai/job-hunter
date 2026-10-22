"""Hard filters (spec §11.1), their per-(job, scope) results (§7.11) and the completeness reconciler (§17.4).

**Positive evidence only.** `evaluate` fails a job under a filter only when the job's own data proves it;
whatever is unknown passes. It reads `DetFacts`, the job's deterministic facts (§8.8): the columns of `jobs`
that feed the filters and `det_results`, so `det_facts_hash` is what `jobs.det_facts_hash` records;
`structured_seniority` is the column `jobs.structured_seniority` (Task 16 ruling T16-a; written by the
canonical selection of Task 17). `too_old` is not a stored filter (§11.1: it is evaluated at admission as
expiry).

- `outside_germany`: at least one location, all of them resolved with every candidate outside Germany
  (`positively_outside_germany`), and the job is not remote-capable (`remote` in `work_mode_possible`) under
  the "remote within Germany" toggle, unless its structured `remote_countries` is non-empty and excludes DE.
- `beyond_radius`: `work_mode_possible` is within {onsite, hybrid}, the scope has a location, the job has one,
  and every job location lies beyond twice the radius of *each* scope location (a location is *beyond* only
  when it is resolved and all its candidates are; candidates in different bands make the band unknown, §8.12).
- `work_mode_excluded`, `employment_type_excluded`, `excluded_industry`: structured values only; an empty
  structured set says nothing. The structured industry is compared as the fixed point of `fold`, the form the
  scope stores its industries in (FR-9).
- `seniority_out_of_range`: only for `seniority_range.mode == "explicit"` (a `profile_relative` range is
  unknown in M1, plan Deviation 8). The title's tokens and the structured level map onto `SENIORITY_LADDER`;
  the job fails when it has such evidence and every level of it lies outside `[min, max]`. A bound that is
  `None`, or not a ladder level, is open, and so is a range whose `min` lies above its `max`. A structured
  level is the value a provider wrote (`STRUCTURED_LEVELS`: SmartRecruiters' `experienceLevel.id`, Personio's
  `seniority`) or a ladder level; it stands for every level it may cover (`mid_senior_level` for mid to
  principal), and a value that is not known says nothing.
- `excluded_company`: the company key is one of the scope's. The scope stores an exclusion as the fixed point
  of `company_key` (`settings.model`), while identity keys a job in one pass (`jobs.company_key`, which stays
  as it is), so the job's key is compared as its fixed point (`normalize.text.fixed_point`; final review
  FR-9); a key with no stable form matches nothing.
- `excluded_keyword`: a whole-word match in the title (and in the description when the scope says so). Text
  and keywords go through the normalisation of `title_norm` (`normalize_title`: separators, `Sr.`, accents,
  case) before the match, so `Front-End` and a title normalised to `front end` meet.
- `ad_language_excluded`: the language is known (not `unknown`) and not accepted.

`write_result` upserts the row of a (job, scope) with `inputs_hash`, the `jobs.det_facts_hash` the result was
computed from, and `det_algorithm_version`. `refilter_job` computes and writes a job's rows for the scopes it
is given under the job row `FOR SHARE`: it waits for an identity transaction that holds the row `FOR UPDATE`
and reads the facts that transaction committed.

**Completeness.** `recompute_stale` finds the unmerged jobs that lack a row for a scope of `active_scopes`, or
whose row has an `inputs_hash` other than `jobs.det_facts_hash` or another `det_algorithm_version`, and
refilters up to `BATCH` of them. The heavy reconciler `hard_filter_completeness` runs it over every job
(fenced by the worker epoch); node 8 (`graphs.collect.nodes.hard_filters`) over the jobs the run touched
(fenced by the run's owner row). Each batch is one transaction in the lock order of §3.3.3: `active_config`
(`active_scopes`), then the batch's job rows `FOR SHARE` in ascending id and in one statement (a job merged or
deleted meanwhile is skipped), then the caller's `guard` (the owner or epoch fence), then the writes. The
unpublished targets of a job whose result changed are refiltered once `score_targets` exists (M3).
`STALE_PREDICATE` is that stale-result condition as one SQL fragment, which the scope apply's final
completeness check (`settings.scope_apply.incomplete_jobs`, M2 Task 18) counts with; the apply's precompute
calls `recompute_stale` for its pending scope alone and outside any `active_config` lock (§13.4 step 2).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cached_property, lru_cache
from types import MappingProxyType
from typing import Any, Final, Literal

from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.guards import Guard
from jobhunter.db.seed import canonical_json, content_hash
from jobhunter.db.sql import q
from jobhunter.errors import FencedOut
from jobhunter.geo.gazetteer import distance_km
from jobhunter.normalize.companies import company_key
from jobhunter.normalize.text import fixed_point, fold
from jobhunter.normalize.titles import SENIORITY_LADDER, normalize_title
from jobhunter.run_settings import ScopeLocation, ScopeSettings, scope_from_payload
from jobhunter.scoring.versioning import DET_ALGORITHM_VERSION
from jobhunter.sources.parsing.locations import GERMANY, Location, positively_outside_germany
from jobhunter.sources.parsing.salary import Salary
from jobhunter.types import get_object
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.reconcilers import ReconcileContext, reconciler

FilterCode = Literal[
    "outside_germany",
    "beyond_radius",
    "work_mode_excluded",
    "employment_type_excluded",
    "seniority_out_of_range",
    "excluded_company",
    "excluded_keyword",
    "ad_language_excluded",
    "excluded_industry",
]

BATCH: Final = 500  # jobs recomputed per pass of the reconciler (and per transaction of node 8)

_ON_SITE: Final = frozenset({"onsite", "hybrid"})
_LEVEL: Final = {name: level for level, name in enumerate(SENIORITY_LADDER)}
_TOP_LEVEL: Final = len(SENIORITY_LADDER) - 1
# The levels a provider's own seniority value stands for, by `_level_key` of the value. A job fails only when
# every level of its evidence lies outside the range, so a broad provider level maps broad: it must never
# filter out an ad that the level may describe (Task 16 ruling T16-b). `entry_level` covers the entry-level
# rungs (the junior token of §8.2 and the trainee ones), `associate` and `experienced` span the ladder from
# junior (SmartRecruiters and Personio tag junior ads so) upwards, `mid_senior_level` covers mid to principal,
# `director` and `executive` the management end, `internship` and `student` the intern rungs, and
# `not_applicable` says nothing.
STRUCTURED_LEVELS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "internship": ("intern", "working_student"),
        "student": ("intern", "working_student", "apprentice"),
        "entry_level": ("intern", "working_student", "apprentice", "junior"),
        "associate": ("junior", "mid", "senior"),
        "experienced": ("junior", "mid", "senior", "lead", "principal"),
        "mid_senior_level": ("mid", "senior", "lead", "principal"),
        "director": ("lead", "principal", "head"),
        "executive": ("principal", "head"),
    }
)
_LEVEL_SEPARATORS: Final = re.compile(r"[\s\-]+")


@dataclass(frozen=True)
class DetFacts:
    """The job's deterministic facts (§8.8) that the hard filters and the det results read."""

    company_key: str
    title_norm: str
    seniority_tokens: frozenset[str]
    structured_seniority: str | None
    locations: tuple[Location, ...]
    location_kind: str
    work_mode_possible: frozenset[str]
    work_mode_source: str
    remote_countries: tuple[str, ...]
    employment_types: frozenset[str]
    employment_source: str
    salary: Salary | None
    industry_structured: str | None
    ad_language: str
    posted_at_effective: datetime | None


@dataclass(frozen=True)
class HardFilterResult:
    passed: bool
    reasons: tuple[FilterCode, ...]


def det_facts_hash(facts: DetFacts) -> bytes:
    """The SHA-256 of the canonical JSON of `facts`: sets and the unordered lists (locations, remote
    countries) are sorted, so that only a change of content changes the hash."""
    locations = sorted((location.model_dump(mode="json") for location in facts.locations), key=canonical_json)
    document: dict[str, Any] = {
        "company_key": facts.company_key,
        "title_norm": facts.title_norm,
        "seniority_tokens": sorted(facts.seniority_tokens),
        "structured_seniority": facts.structured_seniority,
        "locations": locations,
        "location_kind": facts.location_kind,
        "work_mode_possible": sorted(facts.work_mode_possible),
        "work_mode_source": facts.work_mode_source,
        "remote_countries": sorted(facts.remote_countries),
        "employment_types": sorted(facts.employment_types),
        "employment_source": facts.employment_source,
        "salary": None if facts.salary is None else facts.salary.model_dump(mode="json"),
        "industry_structured": facts.industry_structured,
        "ad_language": facts.ad_language,
        "posted_at_effective": None
        if facts.posted_at_effective is None
        else facts.posted_at_effective.astimezone(UTC).isoformat(),
    }
    return content_hash(document)


# -- the filters ---------------------------------------------------------------------------------------------


def _outside_germany(facts: DetFacts, scope: ScopeSettings) -> bool:
    if not positively_outside_germany(facts.locations):
        return False
    countries = {country.upper() for country in facts.remote_countries}
    excluded_by_countries = bool(countries) and GERMANY not in countries
    remote_exempt = scope.remote_within_germany and "remote" in facts.work_mode_possible
    return not (remote_exempt and not excluded_by_countries)


Band = Literal["within", "between", "beyond"]


def _band(distance: float, radius_km: int) -> Band:
    """Within 1x the radius, between 1x and 2x (the job is scored), or beyond 2x."""
    if distance <= radius_km:
        return "within"
    return "between" if distance <= 2 * radius_km else "beyond"


def _location_band(location: Location, where: ScopeLocation) -> Band | None:
    """The band of `location` around the scope location `where`; `None` (unknown) when the location is
    unresolved or its candidates fall into different bands (§8.12)."""
    if location.resolution == "unresolved" or not location.candidates:
        return None
    bands = {
        _band(distance_km(where.lat, where.lon, c.lat, c.lon), where.radius_km) for c in location.candidates
    }
    return next(iter(bands)) if len(bands) == 1 else None


def _beyond_radius(facts: DetFacts, scope: ScopeSettings) -> bool:
    modes = facts.work_mode_possible
    if not modes or not modes <= _ON_SITE or not scope.locations or not facts.locations:
        return False
    return all(
        _location_band(location, where) == "beyond"
        for location in facts.locations
        for where in scope.locations
    )


def _work_mode_excluded(facts: DetFacts, scope: ScopeSettings) -> bool:
    modes = facts.work_mode_possible
    return facts.work_mode_source == "structured" and bool(modes) and not modes & scope.work_modes


def _employment_type_excluded(facts: DetFacts, scope: ScopeSettings) -> bool:
    types = facts.employment_types
    return facts.employment_source == "structured" and bool(types) and not types & scope.employment_types


def _level_key(value: str) -> str:
    return _LEVEL_SEPARATORS.sub("_", fold(value)).strip("_")


def _structured_levels(value: str) -> set[int]:
    key = _level_key(value)
    return {_LEVEL[name] for name in STRUCTURED_LEVELS.get(key, (key,)) if name in _LEVEL}


def _bound(name: str | None, default: int) -> int:
    """The ladder level of a range bound; an absent bound, or one that is no ladder level, is open."""
    return default if name is None else _LEVEL.get(name, default)


def _seniority_out_of_range(facts: DetFacts, scope: ScopeSettings) -> bool:
    wanted = scope.seniority_range
    if wanted.mode != "explicit":
        return False
    levels = {_LEVEL[token] for token in facts.seniority_tokens if token in _LEVEL}
    if facts.structured_seniority is not None:
        levels |= _structured_levels(facts.structured_seniority)
    low, high = _bound(wanted.min, 0), _bound(wanted.max, _TOP_LEVEL)
    if low > high:  # an inverted range says nothing: it is open
        return False
    return bool(levels) and not any(low <= level <= high for level in levels)


def _comparison_form(text: str) -> str:
    """`text` as `title_norm` is made: the match of an excluded keyword compares like with like."""
    return normalize_title(text).title_norm


@lru_cache(maxsize=128)
def _keyword_pattern(keywords: tuple[str, ...]) -> re.Pattern[str] | None:
    """One whole-word pattern over the normalised keywords; `None` when none is left (an empty one matches
    nothing)."""
    forms = tuple(dict.fromkeys(form for keyword in keywords if (form := _comparison_form(keyword))))
    if not forms:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, forms)) + r")(?!\w)")


class _Description:
    """A job's description for the keyword match: normalised on first use, once for every scope."""

    def __init__(self, text: str) -> None:
        self._text = text

    @cached_property
    def comparison_form(self) -> str:
        return _comparison_form(self._text)


def _excluded_keyword(facts: DetFacts, description: _Description, scope: ScopeSettings) -> bool:
    pattern = _keyword_pattern(scope.excluded_keywords)
    if pattern is None:
        return False
    if pattern.search(facts.title_norm) is not None:
        return True
    return scope.exclude_keywords_in_description and pattern.search(description.comparison_form) is not None


def evaluate(facts: DetFacts, description_text: str, scope: ScopeSettings) -> HardFilterResult:
    """The hard filters of §11.1 over one job and one scope: the failed ones, in the order of the table."""
    return _evaluate(facts, _Description(description_text), scope)


def _evaluate(facts: DetFacts, description: _Description, scope: ScopeSettings) -> HardFilterResult:
    reasons: list[FilterCode] = []
    if _outside_germany(facts, scope):
        reasons.append("outside_germany")
    if _beyond_radius(facts, scope):
        reasons.append("beyond_radius")
    if _work_mode_excluded(facts, scope):
        reasons.append("work_mode_excluded")
    if _employment_type_excluded(facts, scope):
        reasons.append("employment_type_excluded")
    if _seniority_out_of_range(facts, scope):
        reasons.append("seniority_out_of_range")
    if fixed_point(company_key, facts.company_key) in scope.excluded_companies:
        reasons.append("excluded_company")
    if _excluded_keyword(facts, description, scope):
        reasons.append("excluded_keyword")
    if facts.ad_language != "unknown" and facts.ad_language not in scope.accepted_ad_languages:
        reasons.append("ad_language_excluded")
    industry = facts.industry_structured
    if industry is not None and fixed_point(fold, industry) in scope.industries_exclude:
        reasons.append("excluded_industry")
    return HardFilterResult(not reasons, tuple(reasons))


# -- reading a job -------------------------------------------------------------------------------------------

_JOB: Final = (
    "SELECT j.company_key, j.title_norm, j.seniority_tokens, j.locations, j.location_kind, "
    "j.work_mode_possible, j.work_mode_source, j.remote_countries, j.employment_types, j.employment_source, "
    "j.salary, j.industry_structured, j.structured_seniority, j.ad_language, j.posted_at_effective, "
    "j.det_facts_hash, coalesce(r.description_text, '') AS description_text "
    "FROM jobs j LEFT JOIN job_revisions r ON r.id = j.current_revision_id WHERE j.id = :job"
)
_LOCK_JOB: Final = q(_JOB + " FOR SHARE OF j")


@dataclass(frozen=True)
class _Loaded:
    facts: DetFacts
    description_text: str
    # The job's stored `det_facts_hash`: the `inputs_hash` of every result written from these facts.
    det_facts_hash: bytes


def _facts(row: Row[tuple[object, ...]]) -> DetFacts:
    return DetFacts(
        company_key=row.company_key,
        title_norm=row.title_norm,
        seniority_tokens=frozenset(row.seniority_tokens),
        structured_seniority=row.structured_seniority,
        locations=tuple(Location.model_validate(item) for item in row.locations),
        location_kind=row.location_kind,
        work_mode_possible=frozenset(row.work_mode_possible),
        work_mode_source=row.work_mode_source,
        remote_countries=tuple(row.remote_countries),
        employment_types=frozenset(row.employment_types),
        employment_source=row.employment_source,
        salary=None if row.salary is None else Salary.model_validate(row.salary),
        industry_structured=row.industry_structured,
        ad_language=row.ad_language,
        posted_at_effective=row.posted_at_effective,
    )


async def _load(conn: AsyncConnection, job_id: int) -> _Loaded:
    """The job's facts, description and `det_facts_hash`, its row `FOR SHARE`."""
    row = (await conn.execute(_LOCK_JOB, {"job": job_id})).first()
    if row is None:
        raise LookupError(f"job {job_id} does not exist")
    return _Loaded(_facts(row), row.description_text, bytes(row.det_facts_hash))


# -- writing results -----------------------------------------------------------------------------------------

_WRITE: Final = q(
    "INSERT INTO hard_filter_results (job_id, scope_version_id, passed, reasons, inputs_hash, "
    "det_algorithm_version, computed_at) "
    "VALUES (:job, :scope, :passed, CAST(:reasons AS text[]), :hash, :version, now()) "
    "ON CONFLICT (job_id, scope_version_id) DO UPDATE SET passed = EXCLUDED.passed, "
    "reasons = EXCLUDED.reasons, inputs_hash = EXCLUDED.inputs_hash, "
    "det_algorithm_version = EXCLUDED.det_algorithm_version, computed_at = EXCLUDED.computed_at"
)
# The scopes whose results are required: the active one and, while a flip is being prepared, the pending
# one, with `active_config` held `FOR SHARE` (§9.1 lock order: the first lock). The scope rows are read by a
# statement of their own: joined to the lock, a statement that waited behind a settings save would look for
# the save's new pending scope in the snapshot of its own start, which lacks it (`settings/active.py`).
_LOCK_SCOPE_POINTERS: Final = q(
    "SELECT scope_version_id, pending_scope_version_id FROM active_config WHERE id = 1 FOR SHARE"
)
_SCOPES: Final = q(
    "SELECT id, payload FROM scope_versions WHERE id = ANY(CAST(:ids AS bigint[])) ORDER BY id"
)


async def write_result(
    conn: AsyncConnection,
    job_id: int,
    scope_version_id: int,
    result: HardFilterResult,
    *,
    inputs_hash: bytes,
) -> None:
    """Upsert the result of `(job_id, scope_version_id)`: `inputs_hash` is the `jobs.det_facts_hash` it was
    computed from; the algorithm version is `DET_ALGORITHM_VERSION` and `computed_at` is now."""
    await conn.execute(
        _WRITE,
        {
            "job": job_id,
            "scope": scope_version_id,
            "passed": result.passed,
            "reasons": list(result.reasons),
            "hash": inputs_hash,
            "version": DET_ALGORITHM_VERSION,
        },
    )


async def active_scopes(conn: AsyncConnection) -> dict[int, ScopeSettings]:
    """The active scope and the pending one when set, by scope version id, from `scope_versions.payload`.

    Takes `active_config` `FOR SHARE`: the rows of these scopes are written under that lock until the
    transaction ends, so no scope flip commits between the facts read and the results written.
    """
    pointers = (await conn.execute(_LOCK_SCOPE_POINTERS)).first()
    if pointers is None:
        raise LookupError("there is no active configuration")
    ids = [pointers.scope_version_id] + (
        [] if pointers.pending_scope_version_id is None else [pointers.pending_scope_version_id]
    )
    rows = (await conn.execute(_SCOPES, {"ids": ids})).all()
    return {int(row.id): scope_from_payload(get_object(row.payload, "")) for row in rows}


async def refilter_job(
    conn: AsyncConnection, job_id: int, scopes: Mapping[int, ScopeSettings]
) -> dict[int, HardFilterResult]:
    """Compute and write the results of the job for every scope of `scopes` (by scope version id).

    The job row is taken `FOR SHARE`, so the facts are the committed ones and `inputs_hash` is their hash.
    The caller holds `active_config` `FOR SHARE` (`active_scopes`). Raises `LookupError` for a job that does
    not exist.
    """
    loaded = await _load(conn, job_id)
    description = _Description(loaded.description_text)  # normalised at most once, whatever the scopes
    results: dict[int, HardFilterResult] = {}
    for scope_version_id, scope in scopes.items():
        result = _evaluate(loaded.facts, description, scope)
        await write_result(conn, job_id, scope_version_id, result, inputs_hash=loaded.det_facts_hash)
        results[scope_version_id] = result
    return results


# -- completeness --------------------------------------------------------------------------------------------

# The stale-result condition of the job `j` (M2 Task 18: written once, shared with
# `scope_apply.incomplete_jobs`): for a scope of `:scopes` (bigint[] of scope version ids) the job has no
# `hard_filter_results` row, or its row has an `inputs_hash` other than `jobs.det_facts_hash` or an algorithm
# version other than `:version` (`DET_ALGORITHM_VERSION`).
STALE_PREDICATE: Final = (
    "EXISTS (SELECT 1 FROM unnest(CAST(:scopes AS bigint[])) AS s(id) "
    "LEFT JOIN hard_filter_results h ON h.job_id = j.id AND h.scope_version_id = s.id "
    "WHERE h.job_id IS NULL OR h.inputs_hash <> j.det_facts_hash OR h.det_algorithm_version <> :version)"
)
# The unmerged jobs (touched since `since`, when given) that lack a row for a scope, or whose row is stale,
# in id order, locked `FOR SHARE` as they are returned: a job merged or deleted since the scan is skipped.
_STALE: Final = q(
    "SELECT j.id FROM jobs j WHERE j.merged_into_job_id IS NULL "  # noqa: S608 (constants only)
    "AND (CAST(:since AS timestamptz) IS NULL OR j.updated_at >= :since) "
    f"AND {STALE_PREDICATE} "
    "ORDER BY j.id LIMIT :limit FOR SHARE OF j"
)


async def recompute_stale(
    conn: AsyncConnection,
    scopes: Mapping[int, ScopeSettings],
    *,
    guard: Guard,
    since: datetime | None = None,
) -> int:
    """Refilter up to `BATCH` unmerged jobs, in id order, that lack a result for a scope of `scopes`, or whose
    result has an `inputs_hash` other than the job's `det_facts_hash` or another algorithm version; only the
    jobs updated at or after `since` when it is given. Returns how many jobs it recomputed (a pass that
    returns `BATCH` may have left more).

    In the lock order of §3.3.3 (the caller took `active_config` `FOR SHARE`, `active_scopes`, except the
    scope apply's precompute, which runs outside any `active_config` lock, §13.4 step 2): the batch's job
    rows `FOR SHARE` in one statement, then `await guard(conn)`, which proves the caller still owns the work
    (the owner row of the run or operation, or the worker epoch) and raises `FencedOut`, having written
    nothing, when it does not; then the results are written.
    """
    if not scopes:
        return 0
    found = await conn.execute(
        _STALE,
        {"since": since, "scopes": list(scopes), "version": DET_ALGORITHM_VERSION, "limit": BATCH},
    )
    job_ids = [int(job_id) for job_id in found.scalars()]
    await guard(conn)
    for job_id in job_ids:
        await refilter_job(conn, job_id, scopes)
    return len(job_ids)


@reconciler("hard_filter_completeness", "heavy")
async def hard_filter_completeness(ctx: ReconcileContext) -> int:
    """§17.4 `hard_filter_completeness`: the unmerged jobs without a current result for the active (or
    pending) scope are recomputed, under `active_config` `FOR SHARE`, `BATCH` jobs per pass. Returns the
    count of jobs recomputed. When `ctx.epoch` is no longer the current worker epoch it raises `FencedOut`
    (`fence_epoch`), having written nothing, so that `run_reconcilers` stops the cadence at this reconciler
    and the superseded worker requests its stop (Ruling FR-r; fix wave F7, Ruling FR-w); a `FencedOut` of
    another owner returns 0."""

    async def fenced(conn: AsyncConnection) -> None:
        await fence_epoch(conn, ctx.epoch)

    try:
        async with ctx.engine.begin() as conn:
            return await recompute_stale(conn, await active_scopes(conn), guard=fenced)
    except FencedOut as fenced_out:
        if fenced_out.worker_superseded:
            raise  # the fenced transaction wrote nothing (`fence_epoch` logged it)
        return 0
