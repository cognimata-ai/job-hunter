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
`det_results.job_id`).
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, get_args

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.seed import canonical_json, content_hash
from jobhunter.db.sql import q
from jobhunter.llm.schemas.generator import JobFacts, RemoteRegion
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.run_settings import ScopeSettings
from jobhunter.scoring.aggregate import Aggregation, aggregate, industry_match
from jobhunter.scoring.det_criteria import employment_salary, location_work_mode
from jobhunter.scoring.filters import DetFacts
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
    algorithm_version: str = DET_ALGORITHM_VERSION,
) -> int | None:
    """Write the instance of `aggregation` (module docstring, Writing): the new id, or `None` when the current
    instance has the same hash and algorithm version. `LookupError` for a result that does not exist.

    The caller holds the job row of `job_id` `FOR SHARE` (lock-schedule step 4) before this call takes the
    result's row (step 10): the insert's foreign key to `jobs` key-shares that row."""
    if (await conn.execute(_LOCK_RESULT, {"id": llm_result_id})).first() is None:
        raise LookupError(f"llm result {llm_result_id} does not exist")
    current = (await conn.execute(_CURRENT, {"result": llm_result_id, "scope": scope_version_id})).first()
    if current is not None:
        if (
            bytes(current.det_inputs_hash) == det_inputs_hash
            and current.det_algorithm_version == algorithm_version
        ):
            return None
        await conn.execute(_SUPERSEDE, {"id": current.id})
    params = {
        "result": llm_result_id,
        "scope": scope_version_id,
        "job": job_id,
        "version": algorithm_version,
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
