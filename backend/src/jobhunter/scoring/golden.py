"""The golden set (spec §11.13 L4610-4632, §14.2 L4977-4979; plan Task 19, R2, Deviations 21 and 29): feedback
and its score snapshot, the golden cases and their flags, the adversarial seed, and a case's frozen facts.

**The adversarial fixtures** (`Fixture`, `load_fixture`; L4610 "each with its own profile and scope", §18.6,
Ruling P39): one JSON file per case under the packaged `eval_fixtures/adversarial/`, synthetic ads and
profiles of invented people: `key`, `label`, `expected_knockouts` (fact ids), `profile` (a
`ConfirmedProfile`), `scope` (a `scope_versions` payload), `ad` (`title`, `company`, `locations`,
`work_modes`, `employment_types`, `completeness`, `description`) and `det_facts` (the eight fields a
`det_inputs` document holds, `det_results.JOB_INPUT_FIELDS`). A fixture's job is `Fixture.det()`: those facts
with the title's normal form and seniority tokens, the company key `eval-<key>` and the ad's detected
language; its revision's material projection is `Fixture.material()` (the `llm_input` shape of
`normalize.fields`), and its frozen snapshot `Fixture.snapshot()` is `det_results.det_inputs` of its facts
under its scope, with no source (a synthetic job has none).

**A case's facts** (`case_det_facts`): an eval reads a case's det-facts from its frozen snapshot
(`det_results.det_facts_from_inputs`), never from the job's current row (L4610), and adds the facts its
labelled revision fixes: the title's normal form and seniority tokens (`normalize_title` of the revision's
title), the company key of its company and its ad language. A title, company or description change is a new
material revision, so these never drift from the case; a fact neither holds (the provider's structured
seniority) is unknown, which every hard filter passes (positive evidence only).

**Refusals** are `jobhunter.errors.Refusal`s (M4 Carry-forward 3): domain code never raises the api's
`Problem`. A user's write that meets a job under a purge fence (`record_feedback`, and `set_flags` activating
a case) is refused with 409 `conflict_state`, `detail = "purge_in_progress"` (`privacy.fences`, §20.2, M5 Task
8), after the job's row is taken `FOR SHARE`; a job gone since counts as fenced. `set_flags` also refuses to
activate a case whose profile version is purged or fenced (M5 Task 14), read under `active_config FOR SHARE`,
its first lock. The deletion of a case a running eval run includes is refused with 409 `conflict_state`,
`detail = "eval_run_running"` (`eval_run_running`, M5 Task 12): `lock_case`, `in_running_run` and `case_job`
are shared with the golden-case purge (`privacy.kinds.golden_case`).

**A pending deletion** (plan M5 Task 33, Ruling T33-15): a case whose `golden_case_delete` manifest is open
(`executing`: its purge pending, waiting or failed; `pending_deletion`) is not deleted again (the golden-case
purge's request refuses it) and is neither activated nor made a canary (`set_flags`), each with 409
`conflict_state`, `detail = "purge_in_progress"`, as a second source purge or delete-everything is refused;
deactivating it stays allowed. The listed case names that manifest (`CaseRow.deletion_manifest_id`). The
kind has no fence that names its case (Deviation 53), so this read stands in for one.

Nothing here logs content: ids, labels, sources and counts only.
"""

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.seed import content_hash, ensure_hashed
from jobhunter.db.sql import q
from jobhunter.errors import DomainError, Refusal
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.normalize.companies import company_key
from jobhunter.normalize.language import detect_language
from jobhunter.normalize.titles import normalize_title
from jobhunter.observability.logging import get_logger
from jobhunter.privacy.fences import fenced_jobs, lock_unfenced_jobs, purge_in_progress, version_fenced
from jobhunter.profile.contacts import strip_contacts
from jobhunter.run_settings import ScopeSettings
from jobhunter.scoring.det_results import det_facts_from_inputs, det_inputs
from jobhunter.scoring.filters import DetFacts, det_facts_hash
from jobhunter.settings.model import EmploymentType, ScopeVersionPayload, WorkMode
from jobhunter.sources.parsing.locations import Location, LocationKind
from jobhunter.sources.parsing.salary import Salary
from jobhunter.types import JSONValue, get_list, get_object, get_str, unstorable

Label = Literal["good", "ok", "poor"]
FactSource = Literal["structured", "text", "none"]

EVAL_COMPANY_PREFIX: Final = "eval-"  # the company key of an adversarial case's job: `eval-<key>`

log = get_logger("jobhunter.scoring.golden")


# ---- the adversarial fixtures ------------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FixtureAd(_Strict):
    """The ad of an adversarial case: its `<job_ad>` fields and its description."""

    title: str = Field(min_length=1)
    company: str = Field(min_length=1)
    locations: list[str]
    work_modes: list[WorkMode] = Field(min_length=1)
    employment_types: list[EmploymentType]
    completeness: Literal["full", "partial"]
    description: str = Field(min_length=1)


class FixtureFacts(_Strict):
    """The det-facts of an adversarial case's job: the fields a `det_inputs` snapshot holds."""

    locations: list[Location]
    work_mode_possible: list[WorkMode] = Field(min_length=1)
    work_mode_source: FactSource
    remote_countries: list[str]
    employment_types: list[EmploymentType]
    employment_source: FactSource
    salary: Salary | None
    industry_structured: str | None


class Fixture(_Strict):
    """One adversarial golden case (module docstring)."""

    key: str = Field(pattern=r"^[a-z0-9_]{1,40}$")
    label: Label
    expected_knockouts: list[str]
    profile: ConfirmedProfile
    scope: ScopeVersionPayload
    ad: FixtureAd
    det_facts: FixtureFacts

    def scope_settings(self) -> ScopeSettings:
        return ScopeSettings.from_model(self.scope)

    def scope_payload(self) -> dict[str, JSONValue]:
        """The scope as its `scope_versions` row stores it (the validated payload)."""
        return self.scope.model_dump(mode="json")

    def ad_language(self) -> str:
        return detect_language(self.ad.description)

    def det(self) -> DetFacts:
        """The job's det-facts (module docstring)."""
        facts = self.det_facts
        title = normalize_title(self.ad.title)
        return DetFacts(
            company_key=f"{EVAL_COMPANY_PREFIX}{self.key}",
            title_norm=title.title_norm,
            seniority_tokens=title.seniority_tokens,
            structured_seniority=None,
            locations=tuple(facts.locations),
            location_kind=location_kind(facts.locations),
            work_mode_possible=frozenset(facts.work_mode_possible),
            work_mode_source=facts.work_mode_source,
            remote_countries=tuple(facts.remote_countries),
            employment_types=frozenset(facts.employment_types),
            employment_source=facts.employment_source,
            salary=facts.salary,
            industry_structured=facts.industry_structured,
            ad_language=self.ad_language(),
            posted_at_effective=None,
        )

    def material(self) -> dict[str, JSONValue]:
        """The revision's material projection (`job_revisions.llm_input`, `normalize.fields`' shape)."""
        facts = self.det_facts
        return {
            "title": normalize_title(self.ad.title).title,
            "company_display": self.ad.company,
            "locations": [label for label in self.ad.locations],
            "work_mode_possible": [mode for mode in sorted(facts.work_mode_possible)],
            "work_mode_source": facts.work_mode_source,
            "employment_types": [kind for kind in self.ad.employment_types],
            "employment_source": facts.employment_source,
            "ad_language": self.ad_language(),
            "description_completeness": self.ad.completeness,
            "description_text": self.ad.description,
        }

    def snapshot(self) -> dict[str, JSONValue]:
        """The case's frozen `det_inputs_snapshot`: its job's facts under its scope, with no source."""
        document, _ = det_inputs(self.det(), {}, self.scope_settings())
        return document

    def case_facts(self) -> DetFacts:
        """The det-facts an eval reads for this case (`case_det_facts` of its snapshot and revision)."""
        return case_det_facts(self.snapshot(), material=self.material(), ad_language=self.ad_language())


def load_fixture(path: Path) -> Fixture:
    """The fixture of `path`; a pydantic `ValidationError` for a file that is not one."""
    return Fixture.model_validate_json(path.read_text(encoding="utf-8"))


def load_fixtures(fixtures_dir: Path) -> list[Fixture]:
    """The fixtures of a directory's `*.json` files, in file name order."""
    return [load_fixture(path) for path in sorted(fixtures_dir.glob("*.json"))]


def location_kind(locations: list[Location]) -> LocationKind:
    """`jobs.location_kind` of a fixture's locations: `city` for one, `multi` for several, else `unknown`."""
    if not locations:
        return "unknown"
    return "city" if len(locations) == 1 else "multi"


def case_det_facts(snapshot: JSONValue, *, material: JSONValue, ad_language: str) -> DetFacts:
    """A golden case's det-facts (module docstring): its frozen snapshot, with its revision's title, company
    and ad language."""
    title = normalize_title(get_str(material, "title"))
    company = get_object(material, "").get("company_display")
    return replace(
        det_facts_from_inputs(snapshot),
        company_key=company_key(company) if isinstance(company, str) else "",
        title_norm=title.title_norm,
        seniority_tokens=title.seniority_tokens,
        ad_language=ad_language,
    )


# ---- feedback (§14.2 L4977) --------------------------------------------------------------------------------

FeedbackKind = Literal["label", "wrong_score"]
CaseSource = Literal["week1", "feedback", "adversarial"]
MAX_CANARIES: Final = 2  # L4632: two canary ads are re-scored daily
COMMENT_MAX_CHARS: Final = 2000  # `score_feedback.comment`, `golden_cases.comment` (CHECK)
_SOURCE_OF: Final[dict[FeedbackKind, CaseSource]] = {"label": "week1", "wrong_score": "feedback"}
# The canary flag's serialisation (`set_flags`): the two-key advisory form, a key space of its own.
CANARY_LOCK_NAMESPACE: Final = 7261019

_LOCK_CONFIG: Final = q("SELECT scope_version_id FROM active_config WHERE id = 1 FOR SHARE")
_LOCK_JOB: Final = q("SELECT id FROM jobs WHERE id = :j FOR SHARE")
# The job's current published result of the active tuple and its instance under the active scope (the view
# joins both), with the ids of the result's tuple and the instance's inputs.
_CURRENT: Final = q(
    "SELECT cs.target_id, cs.llm_result_id, cs.det_result_id, cs.score_low, cs.score_mid, cs.score_high, "
    "cs.coverage, cs.confidence, t.llm_tuple_id, r.job_revision_id, r.profile_version_id, "
    "r.rubric_version_id, r.prompt_schema_version_id, r.model_policy_id, d.scope_version_id, d.det_inputs "
    "FROM current_scores cs JOIN score_targets t ON t.id = cs.target_id "
    "JOIN llm_results r ON r.id = cs.llm_result_id JOIN det_results d ON d.id = cs.det_result_id "
    "WHERE cs.job_id = :j"
)
_INSERT_FEEDBACK: Final = q(
    "INSERT INTO score_feedback (job_id, job_revision_id, profile_version_id, llm_result_id, det_result_id, "
    "score_snapshot, kind, label, comment, created_at) VALUES (:job, :revision, :profile, :result, :det, "
    ":snapshot, :kind, :label, :comment, :now) RETURNING id",
    jsonb=["snapshot"],
)
_UPSERT_CASE: Final = q(
    "INSERT INTO golden_cases (job_revision_id, profile_version_id, scope_version_id, label, comment, "
    "source, labelled_from_result_id, det_inputs_snapshot, det_input_sources, created_at, updated_at) "
    "VALUES (:revision, :profile, :scope, :label, :comment, :source, :result, :snapshot, "
    "CAST(:sources AS text[]), :now, :now) "
    "ON CONFLICT (job_revision_id, profile_version_id) DO UPDATE SET label = EXCLUDED.label, "
    "comment = EXCLUDED.comment, scope_version_id = EXCLUDED.scope_version_id, "
    "labelled_from_result_id = EXCLUDED.labelled_from_result_id, "
    "det_inputs_snapshot = EXCLUDED.det_inputs_snapshot, det_input_sources = EXCLUDED.det_input_sources, "
    "updated_at = EXCLUDED.updated_at RETURNING id",
    jsonb=["snapshot"],
)


@dataclass(frozen=True)
class Feedback:
    """A recorded label or "wrong score": its `score_feedback` row and the golden case it upserted."""

    feedback_id: int
    golden_case_id: int


def _decimal(value: object) -> JSONValue:
    return None if value is None else str(value)


def snapshot_sources(snapshot: JSONValue) -> list[str]:
    """Every source id a `det_inputs` document's job facts draw on, sorted and distinct (§20.5: a source purge
    finds the case by them)."""
    found: set[str] = set()
    for item in get_object(snapshot, "job").values():
        found.update(get_str(source, "") for source in get_list(item, "sources"))
    return sorted(found)


def _comment_refusal(comment: str | None) -> Refusal | None:
    if comment is None:
        return None
    if len(comment) > COMMENT_MAX_CHARS:
        return Refusal(
            422, "validation_failed", "Invalid comment", detail="A comment has at most 2,000 characters."
        )
    if (why := unstorable(comment)) is not None:
        return Refusal(422, "validation_failed", "Invalid comment", detail=why)
    return None


async def record_feedback(
    conn: AsyncConnection,
    *,
    job_id: int,
    kind: FeedbackKind,
    label: Label,
    comment: str | None,
    now: datetime,
) -> Feedback:
    """Record a label or a "wrong score" of job `job_id` in the caller's transaction (§14.2 L4977, §11.13
    L4610; Deviation 21).

    Lock order: `active_config FOR SHARE`, then the job `FOR SHARE`, whose purge fences are checked next: a
    fenced job is refused with 409 `conflict_state`, `detail = "purge_in_progress"` (module docstring). The
    job's current published result of the active tuple and its instance under the active scope
    (`current_scores`) are snapshotted into `score_feedback` (`{L, M, U, coverage, confidence}`, the tuple's
    ids, the scope and the instance), and the golden case of (revision, the tuple's profile) is upserted with
    the active scope, the instance's `det_inputs` as its frozen snapshot and their source ids,
    `source = 'week1'` for a label and `'feedback'` for a "wrong score" (an update keeps the case's source).
    No published result is a 409 `conflict_state` (so is every job with no profile active: `active_config`
    holds a profile exactly when it holds a tuple, and `current_scores` reads the active tuple's targets); a
    comment longer than 2,000 characters or holding a NUL or a lone surrogate a 422, both before any write."""
    if (refusal := _comment_refusal(comment)) is not None:
        raise refusal
    pointers = (await conn.execute(_LOCK_CONFIG)).one()
    if (await conn.execute(_LOCK_JOB, {"j": job_id})).first() is None:
        raise Refusal(404, "not_found", "Job not found")
    if await fenced_jobs(conn, [job_id]):
        log.info("golden_feedback_refused", job_id=job_id, reason="purge_in_progress")
        raise purge_in_progress()
    row = (await conn.execute(_CURRENT, {"j": job_id})).first()
    if row is None or row.llm_result_id is None or row.det_result_id is None:
        raise Refusal(
            409, "conflict_state", "No score to label", detail="The job has no published score to label yet."
        )
    snapshot: dict[str, JSONValue] = {
        "L": _decimal(row.score_low),
        "M": _decimal(row.score_mid),
        "U": _decimal(row.score_high),
        "coverage": _decimal(row.coverage),
        "confidence": row.confidence,
        "llm_tuple_id": int(row.llm_tuple_id),
        "rubric_version_id": int(row.rubric_version_id),
        "prompt_schema_version_id": int(row.prompt_schema_version_id),
        "model_policy_id": int(row.model_policy_id),
        "scope_version_id": int(row.scope_version_id),
        "det_result_id": int(row.det_result_id),
    }
    common = {"revision": int(row.job_revision_id), "profile": int(row.profile_version_id), "now": now}
    feedback_id = int(
        (
            await conn.execute(
                _INSERT_FEEDBACK,
                {
                    **common,
                    "job": job_id,
                    "result": int(row.llm_result_id),
                    "det": int(row.det_result_id),
                    "snapshot": snapshot,
                    "kind": kind,
                    "label": label,
                    "comment": comment,
                },
            )
        ).scalar_one()
    )
    case_id = int(
        (
            await conn.execute(
                _UPSERT_CASE,
                {
                    **common,
                    "scope": int(pointers.scope_version_id),
                    "label": label,
                    "comment": comment,
                    "source": _SOURCE_OF[kind],
                    "result": int(row.llm_result_id),
                    "snapshot": row.det_inputs,
                    "sources": snapshot_sources(row.det_inputs),
                },
            )
        ).scalar_one()
    )
    log.info(
        "golden_feedback_recorded",
        job_id=job_id,
        kind=kind,
        label=label,
        feedback_id=feedback_id,
        golden_case_id=case_id,
    )
    return Feedback(feedback_id, case_id)


_DELETE_FEEDBACK: Final = q("DELETE FROM score_feedback WHERE id = :i RETURNING id")


class FeedbackMissing(LookupError):
    """No `score_feedback` row has the id (its message names the id only)."""

    def __init__(self, feedback_id: int) -> None:
        super().__init__(f"score_feedback {feedback_id}: no such feedback")
        self.feedback_id = feedback_id


async def delete_feedback(conn: AsyncConnection, feedback_id: int) -> None:
    """Delete the label or "wrong score" `feedback_id` (`DELETE /api/feedback/{id}`, §14.2) in the caller's
    transaction: its `score_feedback` row alone. The golden case it upserted stays: the golden set is edited
    through `/api/golden-cases` (plan M4 Deviation 24), and no foreign key references `score_feedback`, so
    nothing else changes; the Jobs list does not read it, so the ranking is not bumped. `FeedbackMissing` (a
    `LookupError`) when there is no such row."""
    if (await conn.execute(_DELETE_FEEDBACK, {"i": feedback_id})).first() is None:
        raise FeedbackMissing(feedback_id)
    log.info("golden_feedback_deleted", feedback_id=feedback_id)


# ---- the cases (§14.2 L4978-4979) --------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseRow:
    """A golden case as the API lists it: its job (the revision's) with the title the case was labelled on
    (the revision's own; the job's title for a revision without one) and the job's company, its label,
    comment, profile version number, scope version id, source and flags."""

    id: int
    job_id: int
    title: str
    company: str
    label: Label
    comment: str | None
    profile_version: int
    scope_version: int
    source: CaseSource
    active: bool
    is_canary: bool
    deletion_manifest_id: int | None  # its open `golden_case_delete` manifest (`pending_deletion`)


# The request key of a golden-case deletion's manifest (`privacy.kinds.golden_case.REQUEST_KEY`).
DELETION_REQUEST_KEY: Final = "golden_case_id"
# The open deletion manifest of case `g`: `executing` (`deletion_manifests_open` serves the status), the
# oldest when, against the rule, there were two.
_OPEN_DELETION: Final = (
    "(SELECT m.id FROM deletion_manifests m "  # noqa: S608 (the module's constants)
    "WHERE m.kind = 'golden_case_delete' AND m.status = 'executing' "
    f"AND m.request -> '{DELETION_REQUEST_KEY}' = to_jsonb(g.id) ORDER BY m.id LIMIT 1)"
)
# A case's revision is retention-exempt and its job is never deleted while the revision exists, so both are
# always there; the revision's material names the title the case was labelled on (`llm_input ->> 'title'`).
_CASE_COLUMNS: Final = (
    "g.id, r.job_id, coalesce(r.llm_input ->> 'title', j.title) AS title, j.company_display AS company, "
    "g.label, g.comment, v.version_no AS profile_version, "
    "g.scope_version_id AS scope_version, g.source, g.active, g.is_canary, "
    f"{_OPEN_DELETION} AS deletion_manifest_id "
    "FROM golden_cases g JOIN job_revisions r ON r.id = g.job_revision_id JOIN jobs j ON j.id = r.job_id "
    "JOIN profile_versions v ON v.id = g.profile_version_id"
)
_LIST: Final = q(
    f"SELECT {_CASE_COLUMNS} WHERE (CAST(:after AS bigint) IS NULL OR g.id > CAST(:after AS bigint)) "
    "AND (CAST(:active AS boolean) IS NULL OR g.active = CAST(:active AS boolean)) ORDER BY g.id LIMIT :limit"
)
_ONE: Final = q(f"SELECT {_CASE_COLUMNS} WHERE g.id = :i")
_LOCK_CASE: Final = q("SELECT active, is_canary FROM golden_cases WHERE id = :i FOR UPDATE")
_PENDING_DELETION: Final = q(f"SELECT {_OPEN_DELETION} FROM golden_cases g WHERE g.id = :i")  # noqa: S608
# The job of a case's revision (a plain read: the golden-case purge then takes the job's row `FOR UPDATE`).
_CASE_JOB: Final = q(
    "SELECT r.job_id FROM golden_cases g JOIN job_revisions r ON r.id = g.job_revision_id WHERE g.id = :i"
)
# The case's job and its profile version, whether purged (plain reads after `set_flags`' `active_config`
# lock: then its job's row `FOR SHARE`).
_CASE_VERSION: Final = q(
    "SELECT r.job_id, g.profile_version_id, v.purged_at IS NOT NULL AS purged FROM golden_cases g "
    "JOIN job_revisions r ON r.id = g.job_revision_id JOIN profile_versions v ON v.id = g.profile_version_id "
    "WHERE g.id = :i"
)
# A running eval run includes the case (`in_running_run`): it has a call naming the case, or its
# kind selects it (a golden run every active case, a canary run every active canary; `eval_runner`'s list).
_INCLUDES_CASE: Final = (
    "r.status = 'running' AND ("
    "EXISTS (SELECT 1 FROM llm_calls c WHERE c.eval_run_id = r.id AND c.golden_case_id = :i) "
    "OR (CAST(:active AS boolean) AND (r.kind = 'golden' "
    "OR (r.kind = 'canary' AND CAST(:canary AS boolean)))))"
)
_IN_RUNNING_RUN: Final = q(f"SELECT EXISTS (SELECT 1 FROM eval_runs r WHERE {_INCLUDES_CASE})")  # noqa: S608
_RUNNING_RUNS_INCLUDING: Final = q(
    f"SELECT r.id FROM eval_runs r WHERE {_INCLUDES_CASE} ORDER BY r.id"  # noqa: S608 (the module's constant)
)
_DELETE: Final = q("DELETE FROM golden_cases WHERE id = :i RETURNING id")
_CANARY_LOCK: Final = q("SELECT pg_advisory_xact_lock(CAST(:namespace AS integer), 0)")
_OTHER_CANARIES: Final = q("SELECT count(*) FROM golden_cases WHERE is_canary AND id <> :i")
_SET_FLAGS: Final = q(
    "UPDATE golden_cases SET active = coalesce(CAST(:active AS boolean), active), "
    "is_canary = coalesce(CAST(:canary AS boolean), is_canary), updated_at = :now WHERE id = :i RETURNING id"
)


EVAL_RUN_RUNNING: Final = "eval_run_running"  # the refusal's detail, and a waiting purge's error code


class CaseMissing(LookupError):
    """No `golden_cases` row has the id (its message names the id only)."""

    def __init__(self, case_id: int) -> None:
        super().__init__(f"golden_cases {case_id}: no such case")
        self.case_id = case_id


@dataclass(frozen=True)
class CaseFlags:
    """The flags of a locked case that decide whether a running eval run includes it."""

    active: bool
    is_canary: bool


def eval_run_running() -> Refusal:
    """The refusal of a deletion of a case a running eval run includes (409 `conflict_state`, detail
    `eval_run_running`; Carry T19-d)."""
    return Refusal(
        409, "conflict_state", "The golden case is part of a running eval run", detail=EVAL_RUN_RUNNING
    )


async def lock_case(conn: AsyncConnection, case_id: int) -> CaseFlags | None:
    """The case's row `FOR UPDATE` (a call an eval run reserves for it meanwhile, whose foreign key takes
    `FOR KEY SHARE`, waits for the caller's transaction); its flags, `None` when there is no such case."""
    case = (await conn.execute(_LOCK_CASE, {"i": case_id})).first()
    return None if case is None else CaseFlags(active=bool(case.active), is_canary=bool(case.is_canary))


async def in_running_run(conn: AsyncConnection, case_id: int, flags: CaseFlags) -> bool:
    """Whether a `running` eval run includes the case: one of its calls names it, or its kind selects it (a
    golden run every active case, a canary run every active canary; `eval_runner`'s list)."""
    params = {"i": case_id, "active": flags.active, "canary": flags.is_canary}
    return bool((await conn.execute(_IN_RUNNING_RUN, params)).scalar_one())


async def running_runs_including(conn: AsyncConnection, case_id: int, flags: CaseFlags) -> list[int]:
    """The `running` eval runs that include the case (`in_running_run`'s predicate), by id."""
    params = {"i": case_id, "active": flags.active, "canary": flags.is_canary}
    return [int(i) for i in (await conn.execute(_RUNNING_RUNS_INCLUDING, params)).scalars()]


async def pending_deletion(conn: AsyncConnection, case_id: int) -> int | None:
    """The id of the case's open `golden_case_delete` manifest (module docstring), `None` when it has none or
    there is no such case. A plain read: the golden-case purge's request reads it under the case's row lock
    and `active_config FOR UPDATE`, `set_flags` after its `active_config FOR SHARE`, so a request committing
    meanwhile is seen."""
    manifest_id = (await conn.execute(_PENDING_DELETION, {"i": case_id})).scalar_one_or_none()
    return None if manifest_id is None else int(manifest_id)


async def case_job(conn: AsyncConnection, case_id: int) -> int | None:
    """The job of the case's revision (a plain read), `None` when there is no such case."""
    job_id = (await conn.execute(_CASE_JOB, {"i": case_id})).scalar_one_or_none()
    return None if job_id is None else int(job_id)


def _case(row: Row[Any]) -> CaseRow:
    return CaseRow(
        id=int(row.id),
        job_id=int(row.job_id),
        title=str(row.title),
        company=str(row.company),
        label=row.label,
        comment=row.comment,
        profile_version=int(row.profile_version),
        scope_version=int(row.scope_version),
        source=row.source,
        active=bool(row.active),
        is_canary=bool(row.is_canary),
        deletion_manifest_id=None if row.deletion_manifest_id is None else int(row.deletion_manifest_id),
    )


async def list_cases(
    conn: AsyncConnection, *, after_id: int | None, limit: int, active: bool | None
) -> list[CaseRow]:
    """The golden cases after `after_id` (keyset by id), at most `limit`, only those whose `active` flag is
    `active` when it is given."""
    params = {"after": after_id, "active": active, "limit": limit}
    return [_case(row) for row in await conn.execute(_LIST, params)]


async def delete_case(conn: AsyncConnection, case_id: int) -> bool:
    """Delete golden case `case_id` and, by cascade, its `eval_case_results` and eval calls; `False` when it
    did not exist. The job's inputs lose their retention exemption at once (§20.1). The user's deletion is the
    golden-case purge (`privacy.kinds.golden_case`, M5 Tasks 12 and 18), whose phase B calls this once no
    running eval run includes the case. A running eval run skips a case deleted meanwhile (`eval_runner`)."""
    deleted = (await conn.execute(_DELETE, {"i": case_id})).first() is not None
    if deleted:
        log.info("golden_case_deleted", golden_case_id=case_id)
    return deleted


async def set_flags(
    conn: AsyncConnection, case_id: int, *, active: bool | None, is_canary: bool | None, now: datetime
) -> CaseRow:
    """Set the case's `active` and `is_canary` flags (`None` keeps one) in the caller's transaction, its
    `updated_at` to `now` (the caller's clock); the case.

    Its first statement takes `active_config FOR SHARE` (step 2 of the lock schedule, before the canary lock
    and the job row; M5 Task 14, Deviation 25): a profile purge's admission holds it `FOR UPDATE` while it
    reads its version's eligibility, which an active case on the version spoils, so an activation either
    commits before that read or waits for the admission and then meets its fence. At most `MAX_CANARIES`
    cases are canaries (L4632): making a third one a canary is a 422 `validation_failed`; the check and the
    write are serialised by an advisory transaction lock, so two concurrent requests cannot both pass it. An
    unknown case is a 404. Activating a case (`active=True`) refuses, with 409 `conflict_state`, `detail =
    "purge_in_progress"` (module docstring): a case whose profile version is purged or under an unreleased
    `profile` or `global` fence (`privacy.fences.version_fenced`, read after the `active_config` lock); then
    it takes its job's row `FOR SHARE` (step 4) and refuses a job under a purge fence, or one gone. A case
    deactivated or made a canary is not activated, so neither is refused by those. Activating a case or
    making it a canary is refused the same way while its deletion is pending (`pending_deletion`, read after
    the `active_config` lock; module docstring); deactivating it, or making it no canary, is not."""
    await conn.execute(_LOCK_CONFIG)  # step 2, the first lock
    if (active or is_canary) and (manifest_id := await pending_deletion(conn, case_id)) is not None:
        log.info(
            "golden_case_flags_refused",
            golden_case_id=case_id,
            reason="deletion_pending",
            manifest_id=manifest_id,
        )
        raise purge_in_progress()
    if active:
        case = (await conn.execute(_CASE_VERSION, {"i": case_id})).first()
        if case is None:
            raise Refusal(404, "not_found", "Golden case not found")
        version = int(case.profile_version_id)
        if case.purged or await version_fenced(conn, version):
            log.info(
                "golden_case_activation_refused",
                golden_case_id=case_id,
                reason="profile_purge",
                version_id=version,
            )
            raise purge_in_progress()
        if not await lock_unfenced_jobs(conn, [int(case.job_id)]):
            log.info("golden_case_activation_refused", golden_case_id=case_id, reason="purge_in_progress")
            raise purge_in_progress()
    if is_canary:
        await conn.execute(_CANARY_LOCK, {"namespace": CANARY_LOCK_NAMESPACE})
        others = int((await conn.execute(_OTHER_CANARIES, {"i": case_id})).scalar_one())
        if others >= MAX_CANARIES:
            raise Refusal(
                422,
                "validation_failed",
                "Too many canaries",
                detail=f"At most {MAX_CANARIES} golden cases can be canaries.",
            )
    params = {"i": case_id, "active": active, "canary": is_canary, "now": now}
    updated = await conn.execute(_SET_FLAGS, params)
    if updated.first() is None:
        raise Refusal(404, "not_found", "Golden case not found")
    log.info("golden_case_flags_set", golden_case_id=case_id, active=active, is_canary=is_canary)
    return _case((await conn.execute(_ONE, {"i": case_id})).one())


def tuple_identity(
    *, profile_version_id: int, rubric_version_id: int, prompt_schema_version_id: int, model_policy_id: int
) -> str:
    """The hex SHA-256 of the canonical JSON of a case tuple's four ids (A3 G15, Deviation 29): the
    `tuple_identity` of a case's T8 sample key, `golden:<case>:<tuple_identity>` (L4611)."""
    ids: dict[str, JSONValue] = {
        "profile_version_id": profile_version_id,
        "rubric_version_id": rubric_version_id,
        "prompt_schema_version_id": prompt_schema_version_id,
        "model_policy_id": model_policy_id,
    }
    return content_hash(ids).hex()


# ---- the adversarial seed (L4610) --------------------------------------------------------------------------


class FixtureChanged(DomainError):
    """A shipped fixture no longer matches what an earlier seed stored for its key (`what`). Shipped fixtures
    are immutable: a changed case is a new fixture with a new key."""

    def __init__(self, key: str, what: str) -> None:
        super().__init__(
            f"adversarial fixture {key!r} changed since it was seeded ({what}): shipped fixtures are "
            "immutable, a changed case takes a new key"
        )
        self.key = key
        self.what = what


@dataclass(frozen=True)
class Seeded:
    """What a seed inserted: cases, jobs and profile versions (an existing one is reused, never counted)."""

    cases: int
    jobs: int
    profiles: int


# Ruling T19-c: a synthetic version's `version_no` is `SYNTHETIC_VERSION_BASE + n`, apart from the user's
# (`profile.activation` counts the user's only).
SYNTHETIC_VERSION_BASE: Final = 1_000_000

_LOCK_ACTIVE: Final = q("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE")
_SEEDED_JOB: Final = q(
    "SELECT j.id, j.current_revision_id, r.material_hash FROM jobs j "
    "JOIN job_revisions r ON r.id = j.current_revision_id WHERE j.company_key = :k AND j.synthetic_eval "
    "FOR SHARE OF j"
)
_SEEDED_CASES: Final = q(
    "SELECT profile_version_id, scope_version_id, label, expected_knockouts, det_inputs_snapshot "
    "FROM golden_cases WHERE job_revision_id = :r AND source = 'adversarial' ORDER BY id"
)
_LIVE_PROFILE: Final = q(
    "SELECT id FROM profile_versions WHERE profile_hash = :h AND purged_at IS NULL AND synthetic_eval "
    "FOR KEY SHARE"
)
_INSERT_CV: Final = q(
    "INSERT INTO cv_files (sha256, mime, size_bytes, stripped_text, strip_report, extraction_method, "
    "uploaded_at) VALUES (:sha, 'text/plain', :size, :text, :report, 'pasted', :now) RETURNING id",
    jsonb=["report"],
)
_NEXT_VERSION_ID: Final = q("SELECT nextval(pg_get_serial_sequence('profile_versions', 'id'))")
_INSERT_DRAFT: Final = q(
    "INSERT INTO profile_drafts (cv_file_id, status, reviewed_text, reviewed_text_sha256, draft_json, "
    "confirmed_profile_version_id, confirm_response, created_at, updated_at) VALUES (:cv, 'confirmed', "
    ":text, :sha, :profile, :version, :response, :now, :now) RETURNING id",
    jsonb=["profile", "response"],
)
_INSERT_VERSION: Final = q(
    "INSERT INTO profile_versions (id, version_no, draft_id, cv_file_id, profile_json, profile_hash, "
    "confirmed_at, synthetic_eval) OVERRIDING SYSTEM VALUE "
    "SELECT :id, coalesce(max(version_no), CAST(:base AS integer)) + 1, :draft, :cv, "
    ":profile, :hash, :now, true FROM profile_versions WHERE synthetic_eval",
    jsonb=["profile"],
)
_INSERT_JOB: Final = q(
    "INSERT INTO jobs (company_key, company_display, title, title_norm, seniority_tokens, locations, "
    "location_kind, work_mode_possible, work_mode_source, remote_countries, employment_types, "
    "employment_source, salary, industry_structured, ad_language, completeness, posted_precision, "
    "first_seen_at, last_seen_at, availability, availability_changed_at, source_ids, field_provenance, "
    "det_facts_hash, synthetic_eval) VALUES (:company_key, :company, :title, :title_norm, "
    "CAST(:tokens AS text[]), :locations, :location_kind, CAST(:modes AS text[]), :mode_source, "
    "CAST(:countries AS text[]), CAST(:types AS text[]), :type_source, :salary, :industry, :language, "
    ":completeness, 'unknown', :now, :now, 'active', :now, '{}', '{}', :hash, true) RETURNING id",
    jsonb=["locations", "salary"],
)
_INSERT_REVISION: Final = q(
    "INSERT INTO job_revisions (job_id, revision_no, material_hash, llm_input, description_text, "
    "description_completeness, ad_language, embedding_status, created_at) VALUES (:job, 1, :hash, :material, "
    ":description, :completeness, :language, 'awaiting_embedding', :now) RETURNING id",
    jsonb=["material"],
)
_SET_CURRENT: Final = q("UPDATE jobs SET current_revision_id = :r WHERE id = :j")
_INSERT_CASE: Final = q(
    "INSERT INTO golden_cases (job_revision_id, profile_version_id, scope_version_id, label, "
    "expected_knockouts, source, det_inputs_snapshot, det_input_sources, active, created_at, updated_at) "
    "VALUES (:revision, :profile, :scope, :label, CAST(:knockouts AS text[]), 'adversarial', :snapshot, "
    "'{}', true, :now, :now) ON CONFLICT (job_revision_id, profile_version_id) DO NOTHING RETURNING id",
    jsonb=["snapshot"],
)


def profile_text(profile: ConfirmedProfile) -> str:
    """A fixture profile as the reviewed text of its synthetic CV: its summary, roles and skills."""
    lines = [profile.summary]
    for role in profile.roles:
        lines.append(" ".join(filter(None, [role.title, role.employer, *role.highlights])))
    lines.append(", ".join(skill.name for skill in profile.skills))
    return "\n".join(lines)


def synthetic_profile_hash(profile_json: JSONValue) -> bytes:
    """A synthetic version's `profile_hash`: the content hash of `{"synthetic_eval": profile}`, apart from
    `content_hash(profile)` that a version of the user's with the same content holds, so the two never meet
    in the partial unique index `profile_versions_live_hash` and neither is ever reused as the other."""
    return content_hash({"synthetic_eval": profile_json})


async def _seed_profile(
    conn: AsyncConnection, profile: ConfirmedProfile, *, now: datetime
) -> tuple[int, bool]:
    """The fixture profile's live synthetic version, inserted as a synthetic chain when absent (module
    docstring of `seed_adversarial`); `(id, inserted)`."""
    profile_json = profile.model_dump(mode="json")
    digest = synthetic_profile_hash(profile_json)
    found = (await conn.execute(_LIVE_PROFILE, {"h": digest})).scalar_one_or_none()
    if found is not None:
        return int(found), False
    stripped = strip_contacts(profile_text(profile))
    text = stripped.stripped_text
    sha = hashlib.sha256(text.encode("utf-8")).digest()
    report: dict[str, JSONValue] = {category: count for category, count in stripped.report.items()}
    cv = int(
        (
            await conn.execute(
                _INSERT_CV,
                {"sha": sha, "size": len(text.encode("utf-8")), "text": text, "report": report, "now": now},
            )
        ).scalar_one()
    )
    version = int((await conn.execute(_NEXT_VERSION_ID)).scalar_one())
    draft = int(
        (
            await conn.execute(
                _INSERT_DRAFT,
                {
                    "cv": cv,
                    "text": text,
                    "sha": sha,
                    "profile": profile_json,
                    "version": version,
                    "response": {"profile_version_id": version},
                    "now": now,
                },
            )
        ).scalar_one()
    )
    await conn.execute(
        _INSERT_VERSION,
        {
            "id": version,
            "base": SYNTHETIC_VERSION_BASE,
            "draft": draft,
            "cv": cv,
            "profile": profile_json,
            "hash": digest,
            "now": now,
        },
    )
    return version, True


async def _seed_job(conn: AsyncConnection, fixture: Fixture, *, now: datetime) -> tuple[int, bool]:
    """The fixture's synthetic job's current revision, inserted with the job when absent; `(id, inserted)`.
    A job seeded before from a fixture whose material differs is `FixtureChanged`."""
    det = fixture.det()
    material = fixture.material()
    found = (await conn.execute(_SEEDED_JOB, {"k": det.company_key})).first()
    if found is not None:
        if bytes(found.material_hash) != content_hash(material):
            raise FixtureChanged(fixture.key, "ad")
        return int(found.current_revision_id), False
    job = int(
        (
            await conn.execute(
                _INSERT_JOB,
                {
                    "company_key": det.company_key,
                    "company": fixture.ad.company,
                    "title": material["title"],
                    "title_norm": det.title_norm,
                    "tokens": sorted(det.seniority_tokens),
                    "locations": [location.model_dump(mode="json") for location in det.locations],
                    "location_kind": det.location_kind,
                    "modes": sorted(det.work_mode_possible),
                    "mode_source": det.work_mode_source,
                    "countries": list(det.remote_countries),
                    "types": sorted(det.employment_types),
                    "type_source": det.employment_source,
                    "salary": None if det.salary is None else det.salary.model_dump(mode="json"),
                    "industry": det.industry_structured,
                    "language": det.ad_language,
                    "completeness": fixture.ad.completeness,
                    "now": now,
                    "hash": det_facts_hash(det),
                },
            )
        ).scalar_one()
    )
    revision = int(
        (
            await conn.execute(
                _INSERT_REVISION,
                {
                    "job": job,
                    "hash": content_hash(material),
                    "material": material,
                    "description": fixture.ad.description,
                    "completeness": fixture.ad.completeness,
                    "language": det.ad_language,
                    "now": now,
                },
            )
        ).scalar_one()
    )
    await conn.execute(_SET_CURRENT, {"r": revision, "j": job})
    return revision, True


async def _check_seeded(
    conn: AsyncConnection, fixture: Fixture, revision_id: int, expected: dict[str, Any]
) -> None:
    """`FixtureChanged` unless every adversarial case an earlier seed stored on the fixture's revision is the
    one the fixture makes now: its profile version, scope, label, knockouts and snapshot."""
    for row in await conn.execute(_SEEDED_CASES, {"r": revision_id}):
        stored = {
            "profile": int(row.profile_version_id),
            "scope": int(row.scope_version_id),
            "label": row.label,
            "knockouts": sorted(row.expected_knockouts),
            "snapshot": content_hash(row.det_inputs_snapshot),
        }
        now = {
            **{k: expected[k] for k in ("profile", "scope", "label")},
            "knockouts": sorted(expected["knockouts"]),
            "snapshot": content_hash(expected["snapshot"]),
        }
        changed = next((name for name in stored if stored[name] != now[name]), None)
        if changed is not None:
            raise FixtureChanged(fixture.key, changed)


async def seed_adversarial(conn: AsyncConnection, *, fixtures_dir: Path, now: datetime) -> Seeded:
    """Seed the adversarial cases of `fixtures_dir` (each `*.json` a `Fixture`) in the caller's transaction,
    idempotent by each fixture's `key` (L4610; Codex r1 #32).

    `active_config FOR UPDATE` first: it serialises the synthetic `version_no = max + 1` with another seed
    (and the user's with a profile confirm). For each fixture, in file name order: a synthetic CV
    (`cv_files`: the profile's text, contact stripped, as `stripped_text` with its `strip_report`), a
    `confirmed` `profile_drafts` row and a `profile_versions` row with `synthetic_eval = true`, the fixture's
    profile and its `synthetic_profile_hash`, numbered in the synthetic range (`SYNTHETIC_VERSION_BASE + n`,
    Ruling T19-c; a live synthetic version with the same hash is reused, a version of the user's never is);
    the fixture's scope (`ensure_hashed`); a `jobs` row with `synthetic_eval = true` (`availability =
    'active'`, `company_key = 'eval-<key>'`, the fixture's structured fields and det-facts, no source, no
    posting) and its one `job_revisions` row (the fixture's description, `awaiting_embedding`) as its current
    revision, found again by its company key; and the golden case (`source = 'adversarial'`, the fixture's
    label and expected knockouts, its frozen snapshot, `active`), inserted unless the (revision, profile) case
    exists. A synthetic job gets no target, no embedding and no row in the current-score views (their `NOT
    synthetic_eval`). The counts are what was inserted.

    **Shipped fixtures are immutable**: a changed case is a new fixture with a new key. A fixture found again
    by its key whose ad material differs from its stored revision's, or whose case on that revision differs
    from the stored one (its profile version, scope, label, knockouts or snapshot), is `FixtureChanged`
    naming the fixture and what changed; the caller's transaction then rolls back with whatever the seed
    wrote."""
    await conn.execute(_LOCK_ACTIVE)
    cases = jobs = profiles = 0
    for fixture in load_fixtures(fixtures_dir):
        profile_version_id, new_profile = await _seed_profile(conn, fixture.profile, now=now)
        scope_version_id = await ensure_hashed(conn, "scope_versions", fixture.scope_payload())
        revision_id, new_job = await _seed_job(conn, fixture, now=now)
        case: dict[str, Any] = {
            "revision": revision_id,
            "profile": profile_version_id,
            "scope": scope_version_id,
            "label": fixture.label,
            "knockouts": list(fixture.expected_knockouts),
            "snapshot": fixture.snapshot(),
        }
        if not new_job:
            await _check_seeded(conn, fixture, revision_id, case)
        inserted = await conn.execute(_INSERT_CASE, {**case, "now": now})
        cases += inserted.first() is not None
        jobs += new_job
        profiles += new_profile
    log.info("golden_adversarial_seeded", cases=cases, jobs=jobs, profiles=profiles)
    return Seeded(cases=cases, jobs=jobs, profiles=profiles)
