"""Score instances (spec §7.11 L2479-2512, §11.11 L4573-4581): the immutable `det_results` rows of a published
result, one current per (result, scope version), written with no LLM call whenever the scope, the job's
det-facts or `DET_ALGORITHM_VERSION` changes. Publish (Task 10), the scope apply and the det-facts change hook
(Task 15) and `det_completeness` (Task 18) all go through `compute_and_write`.

**Inputs.** `det_inputs(det, sources, scope)` is the instance's `det_inputs` document and its
`det_inputs_hash` (`seed.content_hash`, the SHA-256 of its `seed.canonical_json`): `{"job": …, "scope": …}`,
the format `golden_cases.det_inputs_snapshot` reuses (L2614-2615).
- `job` holds each `DetFacts` field an instance reads (`JOB_INPUT_FIELDS`: the deterministic criteria, the
  knockouts and the industry match) as `{"value": …, "sources": […]}`, its source ids from `sources` (by
  field name, as `jobs.field_provenance` names them; a field without an entry has none), sorted and
  distinct. A set is a sorted list, the locations are sorted by their canonical JSON (as `det_facts_hash`
  sorts them), a salary is its stored form.
- `scope` holds the scope fields an instance reads (`SCOPE_INPUT_FIELDS`), serialised explicitly and never
  through `dataclasses.asdict`, which a `ScopeSettings` refuses (its mappings are `MappingProxyType`s;
  Controller carry T3-c): each location as its `gazetteer_id`, `lat`, `lon` and `radius_km` (what the bands
  and the knockouts read; the label is display), sorted; the sets sorted; `language_policy` and
  `knockout_severity` as plain objects. The canonical JSON sorts every object's keys, so no order of a set,
  list or mapping changes the hash, and a field no instance reads (the hard filters' keywords, companies,
  ad languages and seniority range, `max_ad_age_days`) changes neither the document nor the hash.

**Writing** (`write_instance`, L2512): in the caller's transaction, the `llm_results` row `FOR UPDATE`
(serialising every replacement of the result's instances; step 10 of the lock schedule, with its
`det_results`); a current instance of the (result, scope) with the same hash and algorithm version makes it a
no-op (`None`); otherwise the current instance is superseded **first** (`is_current = false, superseded_at =
now()`) and the new one inserted **second**, so the immediate partial unique index `det_results_current` is
never violated. A failure rolls both back with the caller's transaction. An instance's scores are never
updated. **Precondition:** the caller already holds the job row `FOR SHARE` (step 4 of the lock
schedule), because the insert's immediate foreign key `det_results.job_id -> jobs(id)` takes `FOR KEY SHARE`
on that row, which would otherwise be a step-4 lock taken after the step-10 result lock (the callers of
Tasks 10, 15 and 18 list it in their lock steps).

**Recomputing** (`compute_and_write`): under the same lock, the aggregation is rebuilt from the stored result
alone: its `criteria` (levels after any null-out), `job_facts`, `fact_confirmations`, `status`,
`injection_suspected`, `critic_injection` (so the confidence of a scope-only recomputation is the
published one; Codex r1 #11) and `not_a_job_ad` (true only when verified, Task 10), the result's rubric
weights, its profile version (for the knockouts) and its revision's description completeness; the given
job must be the revision's job. The knockouts are derived under `scope`; the remote-region and industry facts
count only with evidence, which in a stored result is grounded (publication dropped every failing quote,
§11.6). The description's quality is not stored past the revision, and a `garbage` description is always
`partial` (§8.9), so the completeness carries it.

`ScopeRow` is a scope version (its id and its scope) and `JobRow` the job the instance belongs to (its id,
`det_results.job_id`). `job_sources(conn, job_id)` reads a job's `sources` from its `field_provenance` (Task
10: each field's source instance's `source_id`; a field no observation supplied has none), `jobs_sources`
those of several jobs in one statement.

**The published results of a tuple** (Task 15: the scope apply and the det-facts change hook; Task 18's
`det_completeness` reuses them): the `llm_results` of the tuple's published targets (`r.target_id`, whose
`llm_tuple_id` is the tuple), each with the job of its revision (`job_revision_id → job_id`, the instance's
`job_id`). `count_published(conn, llm_tuple_id=…)` counts them; `published_page(conn, …, after, limit)`
lists them by id after `after` with their current instance for a scope (its hash and algorithm version,
`None` without one); `stale_of(conn, page, scope)` keeps those of a page that lack a current instance whose
`det_inputs_hash` is that of the job's current det-facts and sources under the scope (`det_inputs_hash`, the
scope's part computed once per call or passed in; the facts and sources read in one statement each while the
caller holds the job rows, and returned with the results, `Stale`, so a recompute writes with them) and whose
algorithm version is current: what a recompute would change. `recompute_instance` is `compute_and_write`
whose content-carrying validation error becomes `StoredResultInvalid`, which names the result and pydantic's
error types only (carry T6-g), raised outside the handler; `recompute_job_instances(conn, job_id=…,
llm_tuple_id=…, scopes=…)` recomputes every published result of the tuple of the job's revisions under each
scope, the live hook's half (the caller holds the job row): only the (result, scope) pairs `stale_of` finds
stale reach `compute_and_write`, in result id order, so a re-sighting with unchanged det-facts takes no
`llm_results` lock (Review Minor 5). `recompute_stale_instances(conn, page, scope, *, scope_part=None)` is
a page's half (the caller holds its job rows): the results `stale_of` finds stale, recomputed in id order by
`recompute_instance` (a deleted job's skipped), what each wrote; the scope apply's `det_results` chunks and
the `det_completeness` reconciler share it (Task 18).
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, get_args

from pydantic import ValidationError
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.seed import canonical_json, content_hash
from jobhunter.db.sql import q
from jobhunter.errors import InvariantViolation
from jobhunter.llm.schemas.generator import JobFacts, RemoteRegion
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.run_settings import ScopeSettings
from jobhunter.scoring.aggregate import Aggregation, aggregate, industry_match
from jobhunter.scoring.det_criteria import employment_salary, location_work_mode
from jobhunter.scoring.filters import DetFacts, read_det_facts_of
from jobhunter.scoring.knockouts import Confirmation, derive_knockouts
from jobhunter.scoring.rubric import LLM_CRITERIA
from jobhunter.scoring.versioning import DET_ALGORITHM_VERSION
from jobhunter.types import JSONValue, get_object

JOB_INPUT_FIELDS: Final = (
    "locations",
    "work_mode_possible",
    "work_mode_source",
    "remote_countries",
    "employment_types",
    "employment_source",
    "salary",
    "industry_structured",
)
SCOPE_INPUT_FIELDS: Final = (
    "locations",
    "remote_within_germany",
    "work_modes",
    "employment_types",
    "min_salary_eur_year",
    "industries_include",
    "industries_exclude",
    "language_policy_default",
    "language_policy",
    "knockout_severity",
)
_CONFIRMATIONS: Final[Mapping[str, Confirmation]] = {c: c for c in get_args(Confirmation)}


# ---- the inputs --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DetInputs:
    """An instance's inputs: the job det-facts it reads, each with its source ids, and the scope fields."""

    job: dict[str, JSONValue]
    scope: dict[str, JSONValue]

    def to_json(self) -> dict[str, JSONValue]:
        return {"job": self.job, "scope": self.scope}


def _sorted(values: Iterable[str]) -> list[JSONValue]:
    return [value for value in sorted(set(values))]


def job_inputs(det: DetFacts, sources: Mapping[str, Sequence[str]]) -> dict[str, JSONValue]:
    """The `job` part of the inputs (module docstring)."""
    locations: list[JSONValue] = sorted(
        (location.model_dump(mode="json") for location in det.locations), key=canonical_json
    )
    values: dict[str, JSONValue] = {
        "locations": locations,
        "work_mode_possible": _sorted(det.work_mode_possible),
        "work_mode_source": det.work_mode_source,
        "remote_countries": _sorted(det.remote_countries),
        "employment_types": _sorted(det.employment_types),
        "employment_source": det.employment_source,
        "salary": None if det.salary is None else det.salary.model_dump(mode="json"),
        "industry_structured": det.industry_structured,
    }
    return {
        name: {"value": values[name], "sources": _sorted(sources.get(name, ()))} for name in JOB_INPUT_FIELDS
    }


def scope_inputs(scope: ScopeSettings) -> dict[str, JSONValue]:
    """The `scope` part of the inputs (module docstring): the fields an instance reads, explicitly."""
    locations: list[JSONValue] = sorted(
        (
            {"gazetteer_id": x.gazetteer_id, "lat": x.lat, "lon": x.lon, "radius_km": x.radius_km}
            for x in scope.locations
        ),
        key=canonical_json,
    )
    language_policy: dict[str, JSONValue] = {
        code: severity for code, severity in scope.language_policy.items()
    }
    knockout_severity: dict[str, JSONValue] = {
        code: severity for code, severity in scope.knockout_severity.items()
    }
    return {
        "locations": locations,
        "remote_within_germany": scope.remote_within_germany,
        "work_modes": _sorted(scope.work_modes),
        "employment_types": _sorted(scope.employment_types),
        "min_salary_eur_year": scope.min_salary_eur_year,
        "industries_include": _sorted(scope.industries_include),
        "industries_exclude": _sorted(scope.industries_exclude),
        "language_policy_default": scope.language_policy_default,
        "language_policy": language_policy,
        "knockout_severity": knockout_severity,
    }


def det_inputs(
    det: DetFacts, sources: Mapping[str, Sequence[str]], scope: ScopeSettings
) -> tuple[dict[str, JSONValue], bytes]:
    """The `det_inputs` document of an instance and its `det_inputs_hash` (module docstring)."""
    document = DetInputs(job_inputs(det, sources), scope_inputs(scope)).to_json()
    return document, content_hash(document)


def det_inputs_hash(
    det: DetFacts, sources: Mapping[str, Sequence[str]], scope_part: dict[str, JSONValue]
) -> bytes:
    """`det_inputs`' hash with the scope's part (`scope_inputs`) computed once by the caller: the staleness
    check of many results under one scope (Review Minor 4)."""
    return content_hash(DetInputs(job_inputs(det, sources), scope_part).to_json())


# The source of each field an instance reads: the `source_id` of the source instance its provenance names (a
# field no observation supplied, rule `none`, has none), for the jobs of `:jobs`.
_SOURCES: Final = q(
    "SELECT j.id, e.key, s.source_id FROM jobs j CROSS JOIN LATERAL jsonb_each(j.field_provenance) e "
    "JOIN source_instances s ON s.id = CAST(e.value ->> 'source_instance_id' AS bigint) "
    "WHERE j.id = ANY(CAST(:jobs AS bigint[])) AND e.key = ANY(CAST(:fields AS text[])) "
    "AND e.value ->> 'rule' <> 'none'"
)


async def jobs_sources(conn: AsyncConnection, job_ids: Sequence[int]) -> dict[int, dict[str, list[str]]]:
    """`job_sources` of each job of `job_ids`, by job id, in one statement (a job whose fields no observation
    supplied has an empty mapping)."""
    found: dict[int, dict[str, list[str]]] = {job_id: {} for job_id in job_ids}
    if not job_ids:
        return found
    params = {"jobs": sorted(set(job_ids)), "fields": list(JOB_INPUT_FIELDS)}
    for row in await conn.execute(_SOURCES, params):
        found.setdefault(int(row.id), {}).setdefault(str(row.key), []).append(str(row.source_id))
    return found


async def job_sources(conn: AsyncConnection, job_id: int) -> dict[str, list[str]]:
    """The `sources` of a job's instances (module docstring, Inputs): for each field of `JOB_INPUT_FIELDS`,
    the source id of the observation `jobs.field_provenance` names for it, read without a lock (the callers
    of `compute_and_write` hold the job row)."""
    return (await jobs_sources(conn, [job_id]))[job_id]


# ---- writing an instance -----------------------------------------------------------------------------------

_LOCK_RESULT: Final = q("SELECT id FROM llm_results WHERE id = :id FOR UPDATE")
_CURRENT: Final = q(
    "SELECT id, det_inputs_hash, det_algorithm_version FROM det_results "
    "WHERE llm_result_id = :result AND scope_version_id = :scope AND is_current"
)
_SUPERSEDE: Final = q("UPDATE det_results SET is_current = false, superseded_at = now() WHERE id = :id")
_INSERT: Final = q(
    "INSERT INTO det_results (llm_result_id, scope_version_id, job_id, det_algorithm_version, det_criteria, "
    "knockouts, industry_match, caps_applied, score_low, score_mid, score_high, coverage, confidence, "
    "det_inputs, det_inputs_hash) "
    "VALUES (:result, :scope, :job, :version, :det_criteria, :knockouts, :industry_match, :caps_applied, "
    ":low, :mid, :high, :coverage, :confidence, :det_inputs, :hash) RETURNING id",
    jsonb=["det_criteria", "knockouts", "caps_applied", "det_inputs"],
)


async def write_instance(
    conn: AsyncConnection,
    *,
    llm_result_id: int,
    scope_version_id: int,
    job_id: int,
    aggregation: Aggregation,
    det_inputs: dict[str, JSONValue],
    det_inputs_hash: bytes,
    algorithm_version: str | None = None,
) -> int | None:
    """Write the instance of `aggregation` (module docstring, Writing): the new id, or `None` when the current
    instance has the same hash and algorithm version. `LookupError` for a result that does not exist.
    `algorithm_version` defaults to the module's `DET_ALGORITHM_VERSION`, read at the call, as `stale_of`
    reads it (Task 18: a changed version makes both see every instance as stale).

    The caller holds the job row of `job_id` `FOR SHARE` (lock-schedule step 4) before this call takes the
    result's row (step 10): the insert's foreign key to `jobs` key-shares that row."""
    version = DET_ALGORITHM_VERSION if algorithm_version is None else algorithm_version
    if (await conn.execute(_LOCK_RESULT, {"id": llm_result_id})).first() is None:
        raise LookupError(f"llm result {llm_result_id} does not exist")
    current = (await conn.execute(_CURRENT, {"result": llm_result_id, "scope": scope_version_id})).first()
    if current is not None:
        if bytes(current.det_inputs_hash) == det_inputs_hash and current.det_algorithm_version == version:
            return None
        await conn.execute(_SUPERSEDE, {"id": current.id})
    params = {
        "result": llm_result_id,
        "scope": scope_version_id,
        "job": job_id,
        "version": version,
        "det_criteria": aggregation.det_criteria,
        "knockouts": aggregation.knockouts,
        "industry_match": aggregation.industry_match,
        "caps_applied": aggregation.caps_applied,
        "low": aggregation.low,
        "mid": aggregation.mid,
        "high": aggregation.high,
        "coverage": aggregation.coverage,
        "confidence": aggregation.confidence,
        "det_inputs": det_inputs,
        "hash": det_inputs_hash,
    }
    return int((await conn.execute(_INSERT, params)).scalar_one())


# ---- recomputing an instance -------------------------------------------------------------------------------


@dataclass(frozen=True)
class ScopeRow:
    """A scope version: its `scope_versions.id` and its scope."""

    id: int
    settings: ScopeSettings


@dataclass(frozen=True)
class JobRow:
    """The job an instance belongs to: its `jobs.id` (`det_results.job_id`)."""

    id: int


@dataclass(frozen=True)
class _Rating:
    level: int | None


_STORED: Final = q(
    "SELECT r.criteria, r.job_facts, r.fact_confirmations, r.status, r.injection_suspected, "
    "r.critic_injection, r.not_a_job_ad, v.profile_json, w.weights, jr.description_completeness, jr.job_id "
    "FROM llm_results r JOIN profile_versions v ON v.id = r.profile_version_id "
    "JOIN rubric_versions w ON w.id = r.rubric_version_id "
    "JOIN job_revisions jr ON jr.id = r.job_revision_id "
    "WHERE r.id = :id FOR UPDATE OF r"
)


def _ratings(criteria: JSONValue) -> dict[str, _Rating]:
    """The stored criteria's levels (`{key: {"level": …, …}}`); `ValueError` for another shape."""
    ratings: dict[str, _Rating] = {}
    for key in LLM_CRITERIA:
        level = get_object(criteria, key).get("level")
        if level is not None and (isinstance(level, bool) or not isinstance(level, int)):
            raise ValueError(f"the stored level of {key} is no integer")
        ratings[key] = _Rating(level)
    return ratings


def _weights(weights: JSONValue) -> dict[str, int]:
    stored = get_object(weights, "")
    if not all(isinstance(w, int) and not isinstance(w, bool) for w in stored.values()):
        raise ValueError("the stored rubric weights are no integers")
    return {key: w for key, w in stored.items() if isinstance(w, int)}


def _confirmations(confirmations: JSONValue) -> dict[str, Confirmation]:
    found: dict[str, Confirmation] = {}
    for name, value in get_object(confirmations, "").items():
        confirmation = _CONFIRMATIONS.get(value) if isinstance(value, str) else None
        if confirmation is None:
            raise ValueError("a stored fact confirmation is not one of the confirmations")
        found[name] = confirmation
    return found


def _grounded_region(facts: JobFacts) -> RemoteRegion | None:
    return facts.remote_region if facts.remote_region.evidence else None


def _grounded_industry(facts: JobFacts) -> str | None:
    return facts.industry.value if facts.industry.evidence else None


async def compute_and_write(
    conn: AsyncConnection,
    *,
    llm_result_id: int,
    scope: ScopeRow,
    det: DetFacts,
    sources: Mapping[str, Sequence[str]],
    job: JobRow,
) -> int | None:
    """Recompute and write the result's instance under `scope` (module docstring, Recomputing) with the job's
    current `det` facts and their `sources`: `write_instance`'s result. `LookupError` for a result that does
    not exist, or whose profile version is purged (its knockouts cannot be derived); `ValueError` when `job`
    is not the job of the result's revision.

    The caller holds `job`'s row `FOR SHARE` (lock-schedule step 4) before this call takes the result's row
    (step 10), as `write_instance` requires."""
    row = (await conn.execute(_STORED, {"id": llm_result_id})).first()
    if row is None:
        raise LookupError(f"llm result {llm_result_id} does not exist")
    if int(row.job_id) != job.id:
        raise ValueError(f"llm result {llm_result_id} scores job {row.job_id}, not job {job.id}")
    if row.profile_json is None:
        raise LookupError(f"the profile version of llm result {llm_result_id} is purged")
    facts = JobFacts.model_validate(row.job_facts)
    settings = scope.settings
    aggregation = aggregate(
        criteria=_ratings(row.criteria),
        det_location=location_work_mode(det, settings, _grounded_region(facts)),
        det_employment=employment_salary(det, settings),
        weights=_weights(row.weights),
        knockouts=derive_knockouts(facts, det, ConfirmedProfile.model_validate(row.profile_json), settings),
        confirmations=_confirmations(row.fact_confirmations),
        industry=industry_match(_grounded_industry(facts), det, settings),
        completeness=row.description_completeness,
        quality=None,
        contested=row.status == "contested",
        injection_suspected=row.injection_suspected,
        critic_injection=row.critic_injection,
        not_a_job_ad_verified=row.not_a_job_ad,
    )
    document, digest = det_inputs(det, sources, settings)
    return await write_instance(
        conn,
        llm_result_id=llm_result_id,
        scope_version_id=scope.id,
        job_id=job.id,
        aggregation=aggregation,
        det_inputs=document,
        det_inputs_hash=digest,
    )


# ---- the published results of a tuple (Task 15) -----------------------------------------------------------

# The published results of tuple `:tuple` (module docstring), with the job of their revision.
_PUBLISHED_FROM: Final = (
    "llm_results r JOIN score_targets t ON t.id = r.target_id "
    "JOIN job_revisions jr ON jr.id = r.job_revision_id"
)
_PUBLISHED_WHERE: Final = "t.llm_tuple_id = :tuple"
_COUNT_PUBLISHED: Final = q(
    f"SELECT count(*) FROM {_PUBLISHED_FROM} WHERE {_PUBLISHED_WHERE}"  # noqa: S608 (the module's constants)
)
# ... each with its current instance for scope `:scope` (NULL columns without one).
_WITH_INSTANCE: Final = (
    "SELECT r.id, jr.job_id, d.det_inputs_hash, d.det_algorithm_version "  # noqa: S608
    f"FROM {_PUBLISHED_FROM} LEFT JOIN det_results d ON d.llm_result_id = r.id "
    f"AND d.scope_version_id = :scope AND d.is_current WHERE {_PUBLISHED_WHERE}"
)
_PUBLISHED_PAGE: Final = q(f"{_WITH_INSTANCE} AND r.id > :after ORDER BY r.id LIMIT :limit")
_OF_JOB: Final = q(f"{_WITH_INSTANCE} AND jr.job_id = :job ORDER BY r.id")


@dataclass(frozen=True)
class PublishedInstance:
    """A published result of a tuple, the job of its revision, and its current instance for a scope: the
    instance's `det_inputs_hash` and `det_algorithm_version`, `None` without one."""

    llm_result_id: int
    job_id: int
    det_inputs_hash: bytes | None
    det_algorithm_version: str | None


class StoredResultInvalid(InvariantViolation):
    """A stored result (or its profile version) failed its validation while its instance was recomputed: a
    bug. The message and the attributes are the result's id and pydantic's error types, never a value (the
    `ValidationError` holds the stored content: carry T6-g)."""

    def __init__(self, llm_result_id: int, error_types: Sequence[str]) -> None:
        self.llm_result_id = llm_result_id
        self.error_types = tuple(error_types)
        super().__init__(f"llm result {llm_result_id} failed its validation: {', '.join(self.error_types)}")


async def count_published(conn: AsyncConnection, *, llm_tuple_id: int) -> int:
    """How many published results the tuple has (module docstring), read without a lock."""
    return int((await conn.execute(_COUNT_PUBLISHED, {"tuple": llm_tuple_id})).scalar_one())


def _instances(rows: Iterable[Row[tuple[object, ...]]]) -> list[PublishedInstance]:
    return [
        PublishedInstance(
            llm_result_id=int(row.id),
            job_id=int(row.job_id),
            det_inputs_hash=None if row.det_inputs_hash is None else bytes(row.det_inputs_hash),
            det_algorithm_version=row.det_algorithm_version,
        )
        for row in rows
    ]


async def published_page(
    conn: AsyncConnection, *, llm_tuple_id: int, scope_version_id: int, after: int, limit: int
) -> list[PublishedInstance]:
    """Up to `limit` published results of the tuple with an id above `after`, in id order, each with its
    current instance for the scope (module docstring), read without a lock."""
    params = {"tuple": llm_tuple_id, "scope": scope_version_id, "after": after, "limit": limit}
    return _instances(await conn.execute(_PUBLISHED_PAGE, params))


@dataclass(frozen=True)
class Stale:
    """What `stale_of` found in a page: the results whose recompute would write an instance, and the committed
    det-facts and sources of the page's jobs it read to decide (a recompute writes with these)."""

    results: list[PublishedInstance]
    facts: dict[int, DetFacts]
    sources: dict[int, dict[str, list[str]]]


async def stale_of(
    conn: AsyncConnection,
    page: Sequence[PublishedInstance],
    scope: ScopeRow,
    *,
    scope_part: dict[str, JSONValue] | None = None,
) -> Stale:
    """The results of `page` (read for `scope`) whose recompute would write an instance (module docstring):
    no current instance, another algorithm version, or a `det_inputs_hash` other than the job's current
    one, with the facts and sources of the page's jobs (one statement each). `scope_part` is
    `scope_inputs(scope.settings)` when the caller computed it once for several pages. The caller holds the
    jobs' rows, so the facts and sources read here are the committed ones; a job deleted meanwhile has
    none."""
    jobs = sorted({item.job_id for item in page})
    facts = await read_det_facts_of(conn, jobs)
    sources = await jobs_sources(conn, jobs)
    part = scope_inputs(scope.settings) if scope_part is None else scope_part
    current: dict[int, bytes] = {
        job_id: det_inputs_hash(det, sources[job_id], part) for job_id, det in facts.items()
    }
    results = [
        item
        for item in page
        if item.det_inputs_hash is None
        or item.det_algorithm_version != DET_ALGORITHM_VERSION
        or current.get(item.job_id) != item.det_inputs_hash
    ]
    return Stale(results, facts, sources)


async def recompute_instance(
    conn: AsyncConnection,
    *,
    llm_result_id: int,
    scope: ScopeRow,
    det: DetFacts,
    sources: Mapping[str, Sequence[str]],
    job: JobRow,
) -> int | None:
    """`compute_and_write` (its locks and its precondition: the caller holds the job row), whose validation
    error of the stored result becomes `StoredResultInvalid` (module docstring)."""
    invalid: StoredResultInvalid | None
    try:
        return await compute_and_write(
            conn, llm_result_id=llm_result_id, scope=scope, det=det, sources=sources, job=job
        )
    except ValidationError as error:
        types = [str(item["type"]) for item in error.errors(include_url=False, include_input=False)]
        invalid = StoredResultInvalid(llm_result_id, list(dict.fromkeys(types)))
    raise invalid  # outside the handler: neither its cause nor its context holds the stored content


async def recompute_stale_instances(
    conn: AsyncConnection,
    page: Sequence[PublishedInstance],
    scope: ScopeRow,
    *,
    scope_part: dict[str, JSONValue] | None = None,
) -> list[int | None]:
    """The results of `page` (read for `scope`) that `stale_of` finds stale, recomputed in id order with the
    facts and sources it read (`recompute_instance`): what each recompute wrote, `None` where the current
    instance was up to date meanwhile. A result whose job was deleted since the page was read is skipped (its
    results went with it). The caller holds the page's job rows (step 4) and its own owner row, if any (step
    5); this takes the result rows (step 10): the scope apply's `det_results` chunks and the
    `det_completeness` reconciler (Task 18)."""
    stale = await stale_of(conn, page, scope, scope_part=scope_part)
    written: list[int | None] = []
    for item in stale.results:
        if item.job_id not in stale.facts:
            continue
        written.append(
            await recompute_instance(
                conn,
                llm_result_id=item.llm_result_id,
                scope=scope,
                det=stale.facts[item.job_id],
                sources=stale.sources[item.job_id],
                job=JobRow(item.job_id),
            )
        )
    return written


async def recompute_job_instances(
    conn: AsyncConnection, *, job_id: int, llm_tuple_id: int, scopes: Sequence[ScopeRow]
) -> list[int]:
    """A current instance under each of `scopes` of every published result of the tuple of the job's
    revisions (module docstring), with the job's committed det-facts and sources; the ids of the instances
    written. Only the (result, scope) pairs `stale_of` finds stale reach `compute_and_write` and its
    `llm_results FOR UPDATE`, in result id order (Review Minor 5): an unchanged hash takes no result lock.
    The caller holds the job row (lock-schedule step 4)."""
    pending: list[tuple[int, ScopeRow]] = []
    facts: dict[int, DetFacts] = {}
    sources: dict[int, dict[str, list[str]]] = {}
    for scope in scopes:
        params = {"tuple": llm_tuple_id, "job": job_id, "scope": scope.id}
        page = _instances(await conn.execute(_OF_JOB, params))
        if not page:
            return []  # the job has no published result of the tuple
        stale = await stale_of(conn, page, scope)
        facts, sources = stale.facts, stale.sources
        pending += [(item.llm_result_id, scope) for item in stale.results]
    if job_id not in facts:
        return []
    written: list[int] = []
    for result_id, scope in sorted(pending, key=lambda pair: (pair[0], pair[1].id)):
        instance = await recompute_instance(
            conn,
            llm_result_id=result_id,
            scope=scope,
            det=facts[job_id],
            sources=sources[job_id],
            job=JobRow(job_id),
        )
        if instance is not None:
            written.append(instance)
    return written
