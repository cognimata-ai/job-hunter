"""Hard filters (spec §11.1), their per-(job, scope) results (§7.11) and the completeness reconciler (§17.4).

**Positive evidence only.** `evaluate` fails a job under a filter only when the job's own data proves it;
whatever is unknown passes. It reads `DetFacts`, the job's deterministic facts (§8.8): the columns of `jobs`
that feed the filters and `det_results` (so that `det_facts_hash` is what `jobs.det_facts_hash` records), plus
`structured_seniority`, which `jobs` has no column for and `load_facts` reads from the postings' latest
observations. `too_old` is not a stored filter (§11.1: it is evaluated at admission as expiry).

- `outside_germany`: at least one location, all of them resolved with every candidate outside Germany
  (`positively_outside_germany`), and the job is not remote-capable (`remote` in `work_mode_possible`) under
  the "remote within Germany" toggle, unless its structured `remote_countries` is non-empty and excludes DE.
- `beyond_radius`: `work_mode_possible` is within {onsite, hybrid}, the scope has a location, the job has one,
  and every job location lies beyond twice the radius of *each* scope location (a location is *beyond* only
  when it is resolved and all its candidates are; candidates in different bands make the band unknown, §8.12).
- `work_mode_excluded`, `employment_type_excluded`, `excluded_industry`: structured values only; an empty
  structured set says nothing.
- `seniority_out_of_range`: only for `seniority_range.mode == "explicit"` (a `profile_relative` range is
  unknown in M1, plan Deviation 8). The title's tokens and the structured level map onto `SENIORITY_LADDER`;
  the job fails when it has such evidence and every level of it lies outside `[min, max]`. A bound that is
  `None`, or not a ladder level, is open. A structured level is the value a provider wrote
  (`STRUCTURED_LEVELS`: SmartRecruiters' `experienceLevel.id`, Personio's `seniority`) or a ladder level; it
  may stand for several levels (`mid_senior_level` stands for mid, senior and lead), and a value that is not
  known says nothing.
- `excluded_company`: the company key is one of the scope's.
- `excluded_keyword`: a whole-word match in the title (and in the description when the scope says so). Text
  and keywords go through the normalisation of `title_norm` (`normalize_title`: separators, `Sr.`, accents,
  case) before the match, so `Front-End` and a title normalised to `front end` meet.
- `ad_language_excluded`: the language is known (not `unknown`) and not accepted.

`write_result` upserts the row of a (job, scope) with `inputs_hash`, the `jobs.det_facts_hash` the result was
computed from, and `det_algorithm_version`. `refilter_job` computes and writes a job's rows for the scopes it
is given under the job row `FOR SHARE`: it waits for an identity transaction that holds the row `FOR UPDATE`
and reads the facts that transaction committed. Locks are taken in the order of §3.3.3: `active_config`
(`active_scopes`), then the jobs in ascending id.

**Completeness.** `recompute_stale` finds the unmerged jobs that lack a row for a scope of `active_scopes`, or
whose row has an `inputs_hash` other than `jobs.det_facts_hash` or another `det_algorithm_version`, and
refilters up to `BATCH` of them. The heavy reconciler `hard_filter_completeness` runs it over every job;
node 8 (`graphs.collect.nodes.hard_filters`) over the jobs the run touched. Nothing is fenced by the worker
epoch: the rows are an idempotent projection of the job, not an owner's write. The unpublished targets of a
job whose result changed are refiltered once `score_targets` exists (M3).
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from types import MappingProxyType
from typing import Final, Literal

from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.seed import canonical_json, content_hash
from jobhunter.db.sql import q
from jobhunter.geo.gazetteer import distance_km
from jobhunter.normalize.text import fold
from jobhunter.normalize.titles import SENIORITY_LADDER, normalize_title
from jobhunter.run_settings import ScopeLocation, ScopeSettings, scope_from_payload
from jobhunter.scoring.versioning import DET_ALGORITHM_VERSION
from jobhunter.sources.parsing.locations import GERMANY, Location, positively_outside_germany
from jobhunter.sources.parsing.salary import Salary
from jobhunter.types import JSONValue, get_object
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
# The levels a provider's own seniority value stands for, by `_level_key` of the value. The wider the set the
# fewer jobs it fails (a job fails only when every level lies outside the range): `entry_level` is the junior
# token of §8.2, `associate` sits between junior and mid, `experienced` and `mid_senior_level` span the
# middle of the ladder, `director` and `executive` are the head of §8.2, and `not_applicable` says nothing.
STRUCTURED_LEVELS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "internship": ("intern",),
        "student": ("working_student", "intern"),
        "entry_level": ("junior",),
        "associate": ("junior", "mid"),
        "experienced": ("mid", "senior", "lead", "principal"),
        "mid_senior_level": ("mid", "senior", "lead"),
        "director": ("head",),
        "executive": ("head",),
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


def _sorted(values: Iterable[str]) -> list[JSONValue]:
    return [*sorted(values)]


def det_facts_hash(facts: DetFacts) -> bytes:
    """The SHA-256 of the canonical JSON of `facts`: sets and the unordered lists (locations, remote
    countries) are sorted, so that only a change of content changes the hash."""
    locations: list[JSONValue] = sorted(
        (location.model_dump(mode="json") for location in facts.locations), key=canonical_json
    )
    document: dict[str, JSONValue] = {
        "company_key": facts.company_key,
        "title_norm": facts.title_norm,
        "seniority_tokens": _sorted(facts.seniority_tokens),
        "structured_seniority": facts.structured_seniority,
        "locations": locations,
        "location_kind": facts.location_kind,
        "work_mode_possible": _sorted(facts.work_mode_possible),
        "work_mode_source": facts.work_mode_source,
        "remote_countries": _sorted(facts.remote_countries),
        "employment_types": _sorted(facts.employment_types),
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


def _excluded_keyword(facts: DetFacts, description_text: str, scope: ScopeSettings) -> bool:
    pattern = _keyword_pattern(scope.excluded_keywords)
    if pattern is None:
        return False
    if pattern.search(facts.title_norm) is not None:
        return True
    return (
        scope.exclude_keywords_in_description
        and pattern.search(_comparison_form(description_text)) is not None
    )


def evaluate(facts: DetFacts, description_text: str, scope: ScopeSettings) -> HardFilterResult:
    """The hard filters of §11.1 over one job and one scope: the failed ones, in the order of the table."""
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
    if facts.company_key in scope.excluded_companies:
        reasons.append("excluded_company")
    if _excluded_keyword(facts, description_text, scope):
        reasons.append("excluded_keyword")
    if facts.ad_language != "unknown" and facts.ad_language not in scope.accepted_ad_languages:
        reasons.append("ad_language_excluded")
    if facts.industry_structured is not None and fold(facts.industry_structured) in scope.industries_exclude:
        reasons.append("excluded_industry")
    return HardFilterResult(not reasons, tuple(reasons))


# -- reading a job -------------------------------------------------------------------------------------------

_JOB: Final = (
    "SELECT j.company_key, j.title_norm, j.seniority_tokens, j.locations, j.location_kind, "
    "j.work_mode_possible, j.work_mode_source, j.remote_countries, j.employment_types, j.employment_source, "
    "j.salary, j.industry_structured, j.ad_language, j.posted_at_effective, j.det_facts_hash, "
    "coalesce(r.description_text, '') AS description_text "
    "FROM jobs j LEFT JOIN job_revisions r ON r.id = j.current_revision_id WHERE j.id = :job"
)
_READ_JOB: Final = q(_JOB)
_LOCK_JOB: Final = q(_JOB + " FOR SHARE OF j")
# The structured seniority of the job: the §8.8 precedence over its postings' latest observations (the best
# source rank, then the most recent `observed_at`, then the lowest posting id).
_STRUCTURED_SENIORITY: Final = q(
    "SELECT o.normalized #>> '{source_structured,seniority}' FROM postings p "
    "JOIN posting_observations o ON o.id = p.latest_observation_id "
    "JOIN source_instances i ON i.id = p.source_instance_id JOIN sources s ON s.id = i.source_id "
    "WHERE p.job_id = :job AND o.normalized #>> '{source_structured,seniority}' IS NOT NULL "
    "ORDER BY s.precedence_rank, o.observed_at DESC, p.id LIMIT 1"
)


@dataclass(frozen=True)
class _Loaded:
    facts: DetFacts
    description_text: str
    det_facts_hash: (
        bytes  # what `jobs.det_facts_hash` records: the `inputs_hash` of the results computed from it
    )


def _facts(row: Row[tuple[object, ...]], structured_seniority: str | None) -> DetFacts:
    return DetFacts(
        company_key=row.company_key,
        title_norm=row.title_norm,
        seniority_tokens=frozenset(row.seniority_tokens),
        structured_seniority=structured_seniority,
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


async def _load(conn: AsyncConnection, job_id: int, *, lock: bool) -> _Loaded:
    row = (await conn.execute(_LOCK_JOB if lock else _READ_JOB, {"job": job_id})).first()
    if row is None:
        raise LookupError(f"job {job_id} does not exist")
    structured = (await conn.execute(_STRUCTURED_SENIORITY, {"job": job_id})).scalar_one_or_none()
    return _Loaded(_facts(row, structured), row.description_text, bytes(row.det_facts_hash))


async def load_facts(conn: AsyncConnection, job_id: int) -> tuple[DetFacts, str]:
    """The job's `DetFacts` and the `description_text` of its current revision (empty when it has none).

    Raises `LookupError` for a job that does not exist.
    """
    loaded = await _load(conn, job_id, lock=False)
    return loaded.facts, loaded.description_text


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
# one, with `active_config` held `FOR SHARE` (§9.1 lock order: the first lock).
_ACTIVE_SCOPES: Final = q(
    "SELECT s.id, s.payload FROM active_config a "
    "JOIN scope_versions s ON s.id IN (a.scope_version_id, a.pending_scope_version_id) "
    "WHERE a.id = 1 ORDER BY s.id FOR SHARE OF a"
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
    rows = (await conn.execute(_ACTIVE_SCOPES)).all()
    if not rows:
        raise LookupError("there is no active configuration")
    return {int(row.id): scope_from_payload(get_object(row.payload, "")) for row in rows}


async def refilter_job(
    conn: AsyncConnection, job_id: int, scopes: Mapping[int, ScopeSettings]
) -> dict[int, HardFilterResult]:
    """Compute and write the results of the job for every scope of `scopes` (by scope version id).

    The job row is taken `FOR SHARE`, so the facts are the committed ones and `inputs_hash` is their hash.
    The caller holds `active_config` `FOR SHARE` (`active_scopes`). Raises `LookupError` for a job that does
    not exist.
    """
    loaded = await _load(conn, job_id, lock=True)
    results: dict[int, HardFilterResult] = {}
    for scope_version_id, scope in scopes.items():
        result = evaluate(loaded.facts, loaded.description_text, scope)
        await write_result(conn, job_id, scope_version_id, result, inputs_hash=loaded.det_facts_hash)
        results[scope_version_id] = result
    return results


# -- completeness --------------------------------------------------------------------------------------------

# The unmerged jobs (touched since `since`, when given) that lack a row for a scope, or whose row is stale.
_STALE: Final = q(
    "SELECT j.id FROM jobs j WHERE j.merged_into_job_id IS NULL "
    "AND (CAST(:since AS timestamptz) IS NULL OR j.updated_at >= :since) "
    "AND EXISTS (SELECT 1 FROM unnest(CAST(:scopes AS bigint[])) AS s(id) "
    "LEFT JOIN hard_filter_results h ON h.job_id = j.id AND h.scope_version_id = s.id "
    "WHERE h.job_id IS NULL OR h.inputs_hash <> j.det_facts_hash OR h.det_algorithm_version <> :version) "
    "ORDER BY j.id LIMIT :limit"
)


async def recompute_stale(
    conn: AsyncConnection, scopes: Mapping[int, ScopeSettings], *, since: datetime | None = None
) -> int:
    """Refilter up to `BATCH` unmerged jobs, in id order, that lack a result for a scope of `scopes`, or whose
    result has an `inputs_hash` other than the job's `det_facts_hash` or another algorithm version; only the
    jobs updated at or after `since` when it is given. Returns how many jobs it recomputed (a pass that
    returns `BATCH` may have left more)."""
    if not scopes:
        return 0
    found = await conn.execute(
        _STALE,
        {"since": since, "scopes": list(scopes), "version": DET_ALGORITHM_VERSION, "limit": BATCH},
    )
    job_ids = [int(job_id) for job_id in found.scalars()]
    for job_id in job_ids:
        await refilter_job(conn, job_id, scopes)
    return len(job_ids)


@reconciler("hard_filter_completeness", "heavy")
async def hard_filter_completeness(ctx: ReconcileContext) -> int:
    """§17.4 `hard_filter_completeness`: the unmerged jobs without a current result for the active (or
    pending) scope are recomputed, under `active_config` `FOR SHARE`, `BATCH` jobs per pass. Returns the
    count of jobs recomputed."""
    async with ctx.engine.begin() as conn:
        return await recompute_stale(conn, await active_scopes(conn))
