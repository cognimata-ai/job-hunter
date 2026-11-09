"""The job detail of the drawer (spec §14.2 L4989, §15.4, §15.5, §11.10, §8.8, §8.10 L3384; plan M4 Task 6,
B12, B14, B22, B25, B26, B28).

`job_detail(conn, job_id, *, now)` reads a job's detail in the caller's read snapshot: its canonical fields
and their provenance, its description, postings and links, its score with every part of it, the contested
data, its target, group, merge events, user actions and feedback, and the first page of its history. `now`
is the request's time (the route's clock); no field depends on it.

**Gone** (B28): a job that does not exist, is a synthetic golden-case job (`synthetic_eval`), is fenced by a
purge (`is_fenced(job_id)`, §20.2) or has no current revision raises `JobGone(None)`; a merged one
`JobGone(survivor)`, the job at the end of its `merged_into_job_id` chain. `ensure_job(conn, job_id)` is that
check alone (the history route's). The route answers both 404 `not_found` with the member
`merged_into_job_id`.

**The job** (`JobOut`): the card's canonical fields (`jobs.cards`), and `employment_types`; `salary` as the
job stores it (§8.10) with its amounts as decimal strings and its `kind` `stated` or `estimate` (a
`predicted` salary: the UI's "Adzuna estimate", L3384); `field_provenance`, each field's entry
(`normalize.fields.Provenance`) with the `source_id` of its source instance; and `work_mode_source_id`, the
source of the work-mode provenance when the modes are known (`work_mode_source <> 'none'`, its rule not
`none`).

**The bucket** (`bucket`, Ruling T24-e) is the job's own card's scoring bucket: the card's CASE
(`cards.BUCKET_CASE`, over `_sql.BUCKET_SQL`) over the card's joins (`_sql.CARD_FROM`) for its current
revision's active-tuple target, so the drawer names the state the list does (a verified not-a-job-ad is
`not_a_job_ad`). It is `None` where no bucket matches: a published result with no current instance under
the active scope (the T4-c gap a recompute closes, which no list page shows), and a target in no bucket of
the list (`deferred_duplicate`, whose card is its representative's, or `cancelled`).

**The score.** A job whose current revision's active-tuple target is `deferred_duplicate` in an active group
with another representative is **scored via** that representative (B26): its score, contested data and
description are the representative's, and `score.scored_via_job_id` and `description.from_job_id` name it;
every other part of the detail (fields, postings, links, target, group, actions, feedback, history) is the
job's own. Otherwise the job is scored by its own result. The result is the one the scored job's
current-revision target of the active tuple published, its instance the current one under the active scope;
`score` is `None` without an instance with a score (as on the card), and then so is `contested`: neither,
nor the evidence and the chain behind them, is read. Beyond the card's `ScoreOut`:
- `criteria`: the six LLM criteria in §11.2's order, each with its rubric weight (`None` when the result's
  rubric names none), level, basis, rationale and its evidence, the `result_evidence` rows whose subject is
  the criterion, in order;
- `fact_evidence` (Ruling T6-a): the other `result_evidence` rows, in the same order, those whose subject is a
  fact's key (its fact id `code:subject`, or an id-less fact's pseudo key), with the same span rule;
- `det_criteria` (the instance's two deterministic criteria with their weights and inputs), `knockouts` (the
  card's, `cards.instance_knockouts`), `caps_applied`, `industry_match`;
- `versions`: the profile version's number, the rubric's version, the prompt-schema hash (hex), the model
  and effort the final draft's call served (`generator`; the requested model when the call recorded none) and
  those of the chain's last critic call (`critic`, `None` when the critic did not run);
- `corrected_fields` and `shortened_fields` (B22): `llm_results.truncated_fields` holds both. The corrected
  ones are the paths of the final draft's stored correction history (`score_drafts.validation`: the
  normaliser's repairs, then the first validation's corrections), in that order; the shortened ones are the
  rest of `truncated_fields`, the publication's cuts (§11.6 L4379, L4393).

**The chain** of a result (as `load_context` resumes it): its attempt, the attempt of its final draft, and
every attempt each was resumed from. **Contested** (`None` unless the result is contested, §11.10 item 7):
- `rounds`: the chain's critic notes on the chain's drafts, in order: the round, its triggers, the returned
  verdict, the derived outcome, and its `issues` as M3's validation of the round kept them
  (`critic_notes.validation`, Ruling T6-d; `scoring.steps.note_issues` without the injection issue, which
  `injection_suspected` shows). First each grounded dispute (`disputes`: its target, assessment and
  suggested level, `None` unless a plain integer), then each required fact (`required`) the round did not
  confirm (`fact_reviews`): `fact_rejected` when rejected, else `fact_unreviewed` when the critic did not
  review it (`unreviewed`), else `fact_uncertain`. The critic's raw output (`review_json`) is never read: a
  review it discarded, one of no valid target or a confirmed fact is no issue. No text is served;
- `rejected_drafts`: the chain's drafts other than the final one, by round, each with the criteria whose
  level differs from the final draft's;
- `nulled_criteria`.

**The description** (B14) is `job_revisions.description_text` as stored: one normalised line per paragraph
of the sanitised ad, joined by line breaks (`normalize.html.html_to_text`), so the evidence spans, offsets in
code points into that text (Python's `str` indexing), index it as served. `lang` is the revision's
`ad_language` when `de` or `en`.

**Postings** of the job by id, each with its own link (`jobs.links.POSTING_LINK`, http(s) only); `links` are
`jobs.links.outbound_links`'. Links are never logged. **Merge events** into or from the job, oldest first
(each side by its index: `merge_events_into`, `merge_events_from_job`); `reversible` while the event is a
merge not reversed yet. **Feedback** newest first. `group` is `jobs.groups.group_view` of the job's group.

Each stored document (a criterion, a deterministic criterion, a cap, a provenance entry, the salary, a
repair, a stored dispute or required fact of a round, a draft's level) is read on its own: one that cannot be
read is left out and a warning names the job whose document it is (for a deferred member's score, its
representative), the kind and how many were left out, never their content (`job_detail_records_skipped`).
The knockouts are read by the card's `cards.instance_knockouts`, so their skips are the card's warning,
`job_card_records_skipped`, which names the result. Every statement is static SQL with its tag,
`/* jobs_detail.<kind> */`.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.api.models import DecimalOut, UtcDatetime
from jobhunter.db.sql import q
from jobhunter.errors import DomainError
from jobhunter.jobs import _sql
from jobhunter.jobs.cards import (
    BUCKET_CASE,
    KnockoutOut,
    LinksOut,
    ScoreOut,
    TargetOut,
    instance_knockouts,
    target_of,
)
from jobhunter.jobs.groups import GroupOut, group_view, location_labels
from jobhunter.jobs.history import HistoryPageOut, Position, history_page
from jobhunter.jobs.links import HTTP_URL, POSTING_LINK, OutboundLinks, outbound_links
from jobhunter.jobs.listing import Active, read_active
from jobhunter.jobs.query import ScoringBucket
from jobhunter.observability.logging import get_logger
from jobhunter.scoring.rubric import LLM_CRITERIA
from jobhunter.scoring.steps import FACT_REJECTED, FACT_UNCERTAIN, FACT_UNREVIEWED
from jobhunter.types import JSONValue

log = get_logger("jobhunter.jobs.detail")

HISTORY_LIMIT: Final = 20  # the drawer's first history page (the history route's default limit)
DET_CRITERIA: Final = ("location_work_mode", "employment_salary")
_DEFERRED: Final = "deferred_duplicate"
_MODES: Final = ("remote", "hybrid", "onsite")
_LANGS: Final = frozenset({"de", "en"})
_SALARY_KINDS: Final[Mapping[str, Literal["stated", "estimate"]]] = {
    "stated": "stated",
    "predicted": "estimate",
}
_NONE_RULE: Final = "none"
_MERGE_DEPTH: Final = 64  # a merge chain longer than this is a cycle, never data


class JobGone(DomainError):
    """The job has no detail (module docstring, Gone): `merged_into_job_id` is its survivor when it was
    merged, else `None`. The message names the job only."""

    def __init__(self, job_id: int, merged_into_job_id: int | None = None) -> None:
        super().__init__(f"jobs {job_id}: no detail")
        self.job_id = job_id
        self.merged_into_job_id = merged_into_job_id


# --- the answer -------------------------------------------------------------------------------------------


class SalaryOut(BaseModel):
    min: DecimalOut | None
    max: DecimalOut | None
    currency: str
    period: str
    kind: Literal["stated", "estimate"]


class FieldProvenanceOut(BaseModel):
    source_id: str | None
    source_instance_id: int
    posting_id: int
    observation_id: int
    observed_at: UtcDatetime
    rule: str


class JobOut(BaseModel):
    job_id: int
    title: str
    company: str
    company_key: str
    locations: list[str]
    location_kind: str
    work_modes: list[str]
    work_mode_known: bool
    work_mode_source_id: str | None
    posted_at: UtcDatetime | None
    posted_precision: str
    first_seen_at: UtcDatetime
    last_seen_at: UtcDatetime
    source_ids: list[str]
    availability: str
    completeness: str
    ad_language: str
    employment_types: list[str]
    salary: SalaryOut | None
    field_provenance: dict[str, FieldProvenanceOut]


class DescriptionOut(BaseModel):
    text: str
    lang: Literal["de", "en"] | None
    completeness: str
    from_job_id: int


class PostingOut(BaseModel):
    id: int
    source_id: str
    native_id: str
    first_seen_at: UtcDatetime
    last_seen_at: UtcDatetime
    availability: str
    link: str | None


class EvidenceOut(BaseModel):
    subject: str
    source_kind: Literal["description", "structured_field", "profile"]
    field_name: str | None
    quote: str
    span_start: int | None
    span_end: int | None


class CriterionOut(BaseModel):
    criterion: str
    weight: int | None
    level: int | None
    basis: str
    rationale: str
    evidence: list[EvidenceOut]


class DetCriterionOut(BaseModel):
    criterion: str
    weight: int | None
    level: int | None
    basis: str
    inputs: dict[str, JSONValue]


class CapOut(BaseModel):
    code: str
    kind: str


class ModelOut(BaseModel):
    model: str
    effort: str


class VersionsOut(BaseModel):
    profile_version_no: int
    rubric_version: str
    prompt_schema_hash: str
    generator: ModelOut
    critic: ModelOut | None


# Its fields: the module docstring, The score.
class DetailScoreOut(ScoreOut):
    """The card's score and the rest of the result."""

    result_id: int
    det_result_id: int
    critic_outcome: Literal["not_run", "accepted", "contested"]
    injection_suspected: bool
    versions: VersionsOut
    summary_en: str
    summary_de: str
    criteria: list[CriterionOut]
    fact_evidence: list[EvidenceOut]
    det_criteria: list[DetCriterionOut]
    knockouts: list[KnockoutOut]
    caps_applied: list[CapOut]
    industry_match: str
    shortened_fields: list[str]
    corrected_fields: list[str]
    scored_via_job_id: int | None


class IssueOut(BaseModel):
    criterion: str
    issue: str
    suggested_level: int | None


class RoundOut(BaseModel):
    round: int
    triggers: list[str]
    verdict: str
    outcome: str
    issues: list[IssueOut]


class LevelDiffOut(BaseModel):
    criterion: str
    from_level: int | None
    to_level: int | None


class RejectedDraftOut(BaseModel):
    round: int
    level_diff: list[LevelDiffOut]


class ContestedOut(BaseModel):
    rounds: list[RoundOut]
    rejected_drafts: list[RejectedDraftOut]
    nulled_criteria: list[str]


class MergeEventOut(BaseModel):
    id: int
    kind: str
    from_job_id: int
    into_job_id: int
    created_at: UtcDatetime
    reversible: bool


class UserActionsOut(BaseModel):
    """The user's actions on a job, its note included (`user_job_actions`)."""

    job_id: int
    seen_at: UtcDatetime | None
    saved_at: UtcDatetime | None
    applied_at: UtcDatetime | None
    hidden_at: UtcDatetime | None
    note: str | None = Field(repr=False)  # the user's own text: never in a repr, so never in a traceback
    updated_at: UtcDatetime


class JobFeedbackOut(BaseModel):
    id: int
    kind: Literal["label", "wrong_score"]
    label: Literal["good", "ok", "poor"]
    comment: str | None
    created_at: UtcDatetime


# Its sections: the module docstring.
class JobDetailOut(BaseModel):
    """A job's detail."""

    job: JobOut
    description: DescriptionOut
    postings: list[PostingOut]
    links: LinksOut
    score: DetailScoreOut | None
    contested: ContestedOut | None
    target: TargetOut | None
    bucket: ScoringBucket | None
    group: GroupOut | None
    merge_events: list[MergeEventOut]
    user: UserActionsOut | None
    feedback: list[JobFeedbackOut]
    history: HistoryPageOut
    # The first history page's next position, unsigned: the route signs it into `history.next_cursor`. A
    # private attribute, so no member of the answer and not in the OpenAPI document.
    _history_next: Position | None = PrivateAttr(default=None)

    @property
    def history_next(self) -> Position | None:
        return self._history_next


# --- the statements ---------------------------------------------------------------------------------------

_GONE_COLUMNS: Final = (
    "j.id, j.merged_into_job_id, j.synthetic_eval, is_fenced(j.id) AS fenced, j.current_revision_id"
)
_LIVE: Final = q(f"/* jobs_detail.live */ SELECT {_GONE_COLUMNS} FROM jobs j WHERE j.id = :job")  # noqa: S608
_JOB: Final = q(
    f"/* jobs_detail.job */ SELECT {_GONE_COLUMNS}, j.title, j.company_display, j.company_key, "  # noqa: S608
    "j.locations, j.location_kind, j.work_mode_possible, j.work_mode_source, j.posted_at_effective, "
    "j.posted_precision, j.first_seen_at, j.last_seen_at, j.source_ids, j.availability, j.completeness, "
    "j.ad_language, j.employment_types, j.salary, j.field_provenance, j.duplicate_group_id, "
    "t.id AS target_id, t.state, t.state_reason, t.refusal_category, t.similarity, t.override, "
    "g.representative_job_id, u.job_id AS acted_job_id, u.seen_at, u.saved_at, u.applied_at, u.hidden_at, "
    "u.note, u.updated_at FROM jobs j "
    "LEFT JOIN score_targets t ON t.job_revision_id = j.current_revision_id "
    "AND t.llm_tuple_id = CAST(:tuple AS bigint) "
    "LEFT JOIN duplicate_groups g ON g.id = j.duplicate_group_id AND g.status = 'active' "
    "LEFT JOIN user_job_actions u ON u.job_id = j.id WHERE j.id = :job"
)
# The job's card bucket (module docstring, The bucket): the card's own CASE over the card's own joins
_BUCKET: Final = q(
    "/* jobs_detail.bucket */ SELECT " + BUCKET_CASE + " AS bucket" + _sql.CARD_FROM + " WHERE j.id = :job"
)
# The end of a merged job's chain: the first job of it that is not merged (`_MERGE_DEPTH` bounds a cycle)
_SURVIVOR: Final = q(
    "/* jobs_detail.survivor */ WITH RECURSIVE chain(id, next_id, depth) AS ("
    "SELECT id, merged_into_job_id, 0 FROM jobs WHERE id = :job "
    "UNION ALL SELECT j.id, j.merged_into_job_id, c.depth + 1 FROM jobs j JOIN chain c ON j.id = c.next_id "
    "WHERE c.depth < :depth) SELECT id FROM chain WHERE next_id IS NULL"
)
_SCORE: Final = q(
    "/* jobs_detail.score */ SELECT v.description_text, v.ad_language AS description_lang, "
    "v.description_completeness, r.id AS result_id, r.status AS result_status, r.critic_outcome, "
    "r.injection_suspected, r.summary_en, r.summary_de, r.criteria, r.job_facts, r.knockout_candidates, "
    "r.nulled_criteria, r.truncated_fields, r.attempt_id, r.final_draft_id, pv.version_no, "
    "pv.purged_at IS NOT NULL AS purged, rv.version AS rubric_version, rv.weights, ps.prompt_schema_hash, "
    "fd.validation AS final_validation, coalesce(gc.model_served, gc.model_requested) AS generator_model, "
    "gc.effort AS generator_effort, d.id AS instance_id, d.score_low, d.score_mid, d.score_high, d.coverage, "
    "d.confidence, d.det_criteria, d.knockouts, d.industry_match, d.caps_applied FROM jobs sj "
    "JOIN job_revisions v ON v.id = sj.current_revision_id "
    "LEFT JOIN score_targets st ON st.job_revision_id = sj.current_revision_id "
    "AND st.llm_tuple_id = CAST(:tuple AS bigint) "
    "LEFT JOIN llm_results r ON r.id = st.published_result_id "
    "LEFT JOIN det_results d ON d.llm_result_id = r.id AND d.scope_version_id = :scope AND d.is_current "
    "LEFT JOIN profile_versions pv ON pv.id = r.profile_version_id "
    "LEFT JOIN rubric_versions rv ON rv.id = r.rubric_version_id "
    "LEFT JOIN prompt_schema_versions ps ON ps.id = r.prompt_schema_version_id "
    "LEFT JOIN score_drafts fd ON fd.id = r.final_draft_id "
    "LEFT JOIN llm_calls gc ON gc.id = fd.llm_call_id WHERE sj.id = :scored"
)
_EVIDENCE: Final = q(
    "/* jobs_detail.evidence */ SELECT subject, source_kind, field_name, quote, span_start, span_end "
    "FROM result_evidence WHERE llm_result_id = :result ORDER BY id"
)
# The chain of a result (module docstring): its attempt, its final draft's, and those they resumed from
_CHAIN: Final = q(
    "/* jobs_detail.chain */ WITH RECURSIVE chain(id) AS (SELECT s.id FROM ("
    "SELECT CAST(:attempt AS bigint) AS id UNION SELECT d.attempt_id FROM score_drafts d WHERE d.id = :draft"
    ") s UNION SELECT a.resumed_from_attempt_id FROM score_attempts a JOIN chain c ON a.id = c.id "
    "WHERE a.resumed_from_attempt_id IS NOT NULL) SELECT id FROM chain ORDER BY id"
)
_CRITIC: Final = q(
    "/* jobs_detail.critic */ SELECT coalesce(c.model_served, c.model_requested) AS model, c.effort "
    "FROM llm_calls c WHERE c.attempt_id = ANY(CAST(:chain AS bigint[])) AND c.purpose = 'critic' "
    "ORDER BY c.id DESC LIMIT 1"
)
_NOTES: Final = q(
    "/* jobs_detail.notes */ SELECT n.round, n.triggers, n.verdict_returned, n.derived_outcome, "
    "n.validation FROM critic_notes n JOIN score_drafts d ON d.id = n.draft_id "
    "WHERE n.attempt_id = ANY(CAST(:chain AS bigint[])) AND d.attempt_id = ANY(CAST(:chain AS bigint[])) "
    "ORDER BY n.id"
)
_DRAFTS: Final = q(
    "/* jobs_detail.drafts */ SELECT d.id, d.round, d.output_json -> 'criteria' AS criteria "
    "FROM score_drafts d WHERE d.attempt_id = ANY(CAST(:chain AS bigint[])) ORDER BY d.round, d.id"
)
_POSTINGS: Final = q(
    "/* jobs_detail.postings */ SELECT p.id, i.source_id, p.native_id, p.first_seen_at, "  # noqa: S608
    f"p.last_seen_at, p.availability, CASE WHEN c.link ~* {HTTP_URL} THEN c.link END AS link FROM postings p "
    "JOIN source_instances i ON i.id = p.source_instance_id "
    "LEFT JOIN posting_observations o ON o.id = p.latest_observation_id "
    f"CROSS JOIN LATERAL (SELECT {POSTING_LINK} AS link) c WHERE p.job_id = :job ORDER BY p.id"
)
_SOURCES: Final = q(
    "/* jobs_detail.sources */ SELECT id, source_id FROM source_instances "
    "WHERE id = ANY(CAST(:ids AS bigint[]))"
)
# The merge events into the job (`merge_events_into`) and from it (`merge_events_from_job`), each side by its
# index; an event is from and into two different jobs (its CHECK), and the second side leaves out an event
# into the job all the same, so each is listed once.
_MERGE_COLUMNS: Final = (
    "e.id, e.kind, e.from_job_id, e.into_job_id, e.created_at, "
    "(e.kind <> 'unmerge' AND e.reversed_by_event_id IS NULL) AS reversible FROM merge_events e"
)
_MERGES: Final = q(
    f"/* jobs_detail.merges */ SELECT {_MERGE_COLUMNS} WHERE e.into_job_id = :job "
    f"UNION ALL SELECT {_MERGE_COLUMNS} WHERE e.from_job_id = :job AND e.into_job_id <> :job "
    "ORDER BY created_at, id"
)
_FEEDBACK: Final = q(
    "/* jobs_detail.feedback */ SELECT f.id, f.kind, f.label, f.comment, f.created_at FROM score_feedback f "
    "WHERE f.job_id = :job ORDER BY f.created_at DESC, f.id DESC"
)


# --- the stored documents ---------------------------------------------------------------------------------

Level = Annotated[int, Field(ge=0, le=4, strict=True)] | None


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _Criterion(_Lenient):
    level: Level
    basis: str
    rationale: str = ""


class _Leveled(_Lenient):
    level: Level


class _DetCriterion(_Lenient):
    level: Level
    basis: str
    inputs: dict[str, JSONValue] = Field(default_factory=dict)


class _Cap(_Lenient):
    code: str
    kind: str


class _Salary(_Lenient):
    min: float | None
    max: float | None
    currency: str
    period: str
    kind: Literal["stated", "predicted"]


class _Provenance(_Lenient):
    source_instance_id: int
    posting_id: int
    observation_id: int
    observed_at: datetime
    rule: str


class _Repair(_Lenient):
    path: str


class _Dispute(_Lenient):
    """A grounded dispute of a round's validation (`steps._note_validation`)."""

    target: str
    assessment: str
    suggested_level: JSONValue = None


class _Round(_Lenient):
    """What a round's issues read of its validation (`critic_notes.validation`, `steps._note_validation`)."""

    disputes: list[JSONValue] = Field(default_factory=list)
    required: list[JSONValue] = Field(default_factory=list)
    fact_reviews: dict[str, JSONValue] = Field(default_factory=dict)
    unreviewed: list[JSONValue] = Field(default_factory=list)


def _skipped(job_id: int, kind: str, count: int) -> None:
    """A content-free warning that `count` stored records could not be read (never their text)."""
    if count:
        log.warning("job_detail_records_skipped", job_id=job_id, kind=kind, skipped=count)


def _read[M: BaseModel](model: type[M], value: object) -> M | None:
    try:
        return model.model_validate(value)
    except ValidationError:
        return None


def _keyed[M: BaseModel](
    job_id: int, kind: str, model: type[M], document: object, keys: Sequence[str]
) -> dict[str, M]:
    """The entries of a stored object at `keys`, each read on its own; an empty document holds none, an
    absent or unreadable entry of another is skipped (and so is a document that is no object, once)."""
    if document in (None, {}):
        return {}
    if not isinstance(document, dict):
        _skipped(job_id, kind, 1)
        return {}
    found = {key: entry for key in keys if (entry := _read(model, document.get(key))) is not None}
    _skipped(job_id, kind, len(keys) - len(found))
    return found


def _listed[M: BaseModel](job_id: int, kind: str, model: type[M], document: object) -> list[M]:
    """The items of a stored list, each read on its own (a document that is no list counts once)."""
    if document in (None, {}, []):
        return []
    if not isinstance(document, list):
        _skipped(job_id, kind, 1)
        return []
    found = [entry for item in document if (entry := _read(model, item)) is not None]
    _skipped(job_id, kind, len(document) - len(found))
    return found


def _amount(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(repr(value)).normalize()


def _salary(job_id: int, stored: object) -> SalaryOut | None:
    if stored is None:
        return None
    salary = _read(_Salary, stored)
    if salary is None:
        _skipped(job_id, "salary", 1)
        return None
    return SalaryOut(
        min=_amount(salary.min),
        max=_amount(salary.max),
        currency=salary.currency,
        period=salary.period,
        kind=_SALARY_KINDS[salary.kind],
    )


def _provenance(job_id: int, stored: object) -> dict[str, _Provenance]:
    """Each field's provenance entry, each read on its own."""
    fields = [str(name) for name in stored] if isinstance(stored, dict) else []
    return _keyed(job_id, "field_provenance", _Provenance, stored, fields)


async def _sources(conn: AsyncConnection, entries: Mapping[str, _Provenance]) -> dict[int, str]:
    ids = sorted({entry.source_instance_id for entry in entries.values()})
    if not ids:
        return {}
    return {int(row.id): str(row.source_id) for row in await conn.execute(_SOURCES, {"ids": ids})}


def _weight(weights: object, key: str) -> int | None:
    value = weights.get(key) if isinstance(weights, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# --- the parts --------------------------------------------------------------------------------------------


async def _gone(conn: AsyncConnection, row: Any) -> JobGone | None:
    """Why a job row has no detail (module docstring, Gone), or `None`."""
    job_id = int(row.id)
    if row.synthetic_eval or row.fenced:
        return JobGone(job_id)
    if row.merged_into_job_id is not None:
        survivor = (
            await conn.execute(_SURVIVOR, {"job": job_id, "depth": _MERGE_DEPTH})
        ).scalar_one_or_none()
        return JobGone(job_id, None if survivor is None else int(survivor))
    if row.current_revision_id is None:
        return JobGone(job_id)
    return None


async def ensure_job(conn: AsyncConnection, job_id: int) -> None:
    """`JobGone` unless the job has a detail (module docstring, Gone)."""
    row = (await conn.execute(_LIVE, {"job": job_id})).one_or_none()
    if row is None:
        raise JobGone(job_id)
    if (gone := await _gone(conn, row)) is not None:
        raise gone


def _job(row: Any, provenance: Mapping[str, _Provenance], sources: Mapping[int, str]) -> JobOut:
    job_id = int(row.id)
    moded = provenance.get("work_mode_possible")
    known = row.work_mode_source != "none"
    return JobOut(
        job_id=job_id,
        title=row.title,
        company=row.company_display,
        company_key=row.company_key,
        locations=location_labels(row.locations),
        location_kind=row.location_kind,
        work_modes=[mode for mode in _MODES if mode in row.work_mode_possible],
        work_mode_known=known,
        work_mode_source_id=(
            sources.get(moded.source_instance_id)
            if known and moded is not None and moded.rule != _NONE_RULE
            else None
        ),
        posted_at=row.posted_at_effective,
        posted_precision=row.posted_precision,
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        source_ids=list(row.source_ids),
        availability=row.availability,
        completeness=row.completeness,
        ad_language=row.ad_language,
        employment_types=list(row.employment_types),
        salary=_salary(job_id, row.salary),
        field_provenance={
            name: FieldProvenanceOut(source_id=sources.get(entry.source_instance_id), **entry.model_dump())
            for name, entry in provenance.items()
        },
    )


def _user(row: Any) -> UserActionsOut | None:
    if row.acted_job_id is None:
        return None
    return UserActionsOut(
        job_id=row.acted_job_id,
        seen_at=row.seen_at,
        saved_at=row.saved_at,
        applied_at=row.applied_at,
        hidden_at=row.hidden_at,
        note=row.note,
        updated_at=row.updated_at,
    )


def _description(row: Any, from_job_id: int) -> DescriptionOut:
    lang = row.description_lang
    return DescriptionOut(
        text=row.description_text,
        lang=lang if lang in _LANGS else None,
        completeness=row.description_completeness,
        from_job_id=from_job_id,
    )


def _corrected(job_id: int, validation: object) -> list[str]:
    """The paths of the final draft's correction history (module docstring, B22), in order, once each."""
    if not isinstance(validation, dict):
        _skipped(job_id, "validation", 0 if validation is None else 1)
        return []
    normalisation = validation.get("normalisation")
    repairs = normalisation.get("repairs") if isinstance(normalisation, dict) else None
    found = [
        *_listed(job_id, "repairs", _Repair, repairs),
        *_listed(job_id, "repairs", _Repair, validation.get("corrections")),
    ]
    return list(dict.fromkeys(repair.path for repair in found))


def _criteria(job_id: int, row: Any, evidence: Sequence[EvidenceOut]) -> list[CriterionOut]:
    stored = _keyed(job_id, "criteria", _Criterion, row.criteria, LLM_CRITERIA)
    return [
        CriterionOut(
            criterion=key,
            weight=_weight(row.weights, key),
            level=criterion.level,
            basis=criterion.basis,
            rationale=criterion.rationale,
            evidence=[e for e in evidence if e.subject == key],
        )
        for key, criterion in stored.items()
    ]


def _det_criteria(job_id: int, row: Any) -> list[DetCriterionOut]:
    stored = _keyed(job_id, "det_criteria", _DetCriterion, row.det_criteria, DET_CRITERIA)
    return [
        DetCriterionOut(
            criterion=key,
            weight=_weight(row.weights, key),
            level=criterion.level,
            basis=criterion.basis,
            inputs=criterion.inputs,
        )
        for key, criterion in stored.items()
    ]


async def _chain(conn: AsyncConnection, row: Any) -> list[int]:
    params = {"attempt": row.attempt_id, "draft": row.final_draft_id}
    return [int(i) for i in (await conn.execute(_CHAIN, params)).scalars()]


async def _critic(conn: AsyncConnection, chain: Sequence[int]) -> ModelOut | None:
    found = (await conn.execute(_CRITIC, {"chain": list(chain)})).one_or_none()
    return None if found is None else ModelOut(model=found.model, effort=found.effort)


def _has_score(row: Any) -> bool:
    """Whether a score row has a result with a current instance that has a score (module docstring)."""
    values = (row.score_mid, row.score_low, row.score_high, row.coverage, row.confidence)
    return row.result_id is not None and row.instance_id is not None and all(v is not None for v in values)


def _score(
    job_id: int,
    row: Any,
    evidence: Sequence[EvidenceOut],
    critic: ModelOut | None,
    scored_via: int | None,
) -> DetailScoreOut:
    """The score of a row that has one (`_has_score`); `job_id` is the job whose result it is."""
    corrected = _corrected(job_id, row.final_validation)
    history = set(corrected)
    return DetailScoreOut(
        mid=row.score_mid,
        low=row.score_low,
        high=row.score_high,
        coverage=row.coverage,
        confidence=row.confidence,
        result_status=row.result_status,
        result_id=row.result_id,
        det_result_id=row.instance_id,
        critic_outcome=row.critic_outcome,
        injection_suspected=row.injection_suspected,
        versions=VersionsOut(
            profile_version_no=row.version_no,
            rubric_version=row.rubric_version,
            prompt_schema_hash=bytes(row.prompt_schema_hash).hex(),
            generator=ModelOut(model=row.generator_model, effort=row.generator_effort),
            critic=critic,
        ),
        summary_en=row.summary_en,
        summary_de=row.summary_de,
        criteria=_criteria(job_id, row, evidence),
        fact_evidence=[e for e in evidence if e.subject not in LLM_CRITERIA],
        det_criteria=_det_criteria(job_id, row),
        knockouts=instance_knockouts(row),
        caps_applied=[
            CapOut(code=c.code, kind=c.kind) for c in _listed(job_id, "caps_applied", _Cap, row.caps_applied)
        ],
        industry_match=row.industry_match,
        shortened_fields=[path for path in row.truncated_fields if path not in history],
        corrected_fields=corrected,
        scored_via_job_id=scored_via,
    )


def _plain_level(value: JSONValue) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _issues(job_id: int, validation: object) -> list[IssueOut]:
    """A round's issues from its validation (module docstring, Contested, Ruling T6-d): its grounded disputes,
    then the required facts it did not confirm. A record that cannot be read is skipped with the warning."""
    stored = _read(_Round, validation)
    if stored is None:
        _skipped(job_id, "critic_round", 1)
        return []
    issues = [
        IssueOut(criterion=d.target, issue=d.assessment, suggested_level=_plain_level(d.suggested_level))
        for d in _listed(job_id, "critic_disputes", _Dispute, stored.disputes)
    ]
    required = [name for name in stored.required if isinstance(name, str)]
    unreviewed = {name for name in stored.unreviewed if isinstance(name, str)}
    _skipped(job_id, "critic_facts", len(stored.required) - len(required))
    for name in required:
        status = stored.fact_reviews.get(name)
        if status == "critic_rejected":
            issue = FACT_REJECTED
        elif status == "critic_confirmed":
            continue
        else:
            issue = FACT_UNREVIEWED if name in unreviewed else FACT_UNCERTAIN
        issues.append(IssueOut(criterion=name, issue=issue, suggested_level=None))
    return issues


def _levels(job_id: int, criteria: object) -> dict[str, int | None]:
    return {
        key: c.level for key, c in _keyed(job_id, "draft_levels", _Leveled, criteria, LLM_CRITERIA).items()
    }


async def _contested(conn: AsyncConnection, job_id: int, row: Any, chain: Sequence[int]) -> ContestedOut:
    rounds = [
        RoundOut(
            round=note.round,
            triggers=list(note.triggers),
            verdict=note.verdict_returned,
            outcome=note.derived_outcome,
            issues=_issues(job_id, note.validation),
        )
        for note in await conn.execute(_NOTES, {"chain": list(chain)})
    ]
    drafts = list(await conn.execute(_DRAFTS, {"chain": list(chain)}))
    final = next((_levels(job_id, d.criteria) for d in drafts if d.id == row.final_draft_id), {})
    rejected: list[RejectedDraftOut] = []
    for draft in drafts:
        if draft.id == row.final_draft_id:
            continue
        levels = _levels(job_id, draft.criteria)
        diff = [
            LevelDiffOut(criterion=key, from_level=levels[key], to_level=final[key])
            for key in LLM_CRITERIA
            if key in levels and key in final and levels[key] != final[key]
        ]
        rejected.append(RejectedDraftOut(round=draft.round, level_diff=diff))
    return ContestedOut(rounds=rounds, rejected_drafts=rejected, nulled_criteria=list(row.nulled_criteria))


async def _score_row(conn: AsyncConnection, scored: int, active: Active) -> Any:
    params = {"scored": scored, "tuple": active.llm_tuple_id, "scope": active.scope_version_id}
    return (await conn.execute(_SCORE, params)).one_or_none()


# --- the detail -------------------------------------------------------------------------------------------


async def job_detail(conn: AsyncConnection, job_id: int, *, now: datetime) -> JobDetailOut:
    """The job's detail in the caller's snapshot (module docstring); `JobGone` for a job without one."""
    active = await read_active(conn)
    row = (await conn.execute(_JOB, {"job": job_id, "tuple": active.llm_tuple_id})).one_or_none()
    if row is None:
        raise JobGone(job_id)
    if (gone := await _gone(conn, row)) is not None:
        raise gone
    representative = row.representative_job_id
    deferred = row.state == _DEFERRED and representative is not None and representative != job_id
    scored = int(representative) if deferred else job_id
    scoring = await _score_row(conn, scored, active)
    if scoring is None:  # a representative without a current revision, which an active group never has
        scored, scoring = job_id, await _score_row(conn, job_id, active)
    if scoring is None:
        raise JobGone(job_id)
    score: DetailScoreOut | None = None
    contested: ContestedOut | None = None
    if _has_score(scoring):  # else neither score nor contested data, and none of their reads
        evidence = [
            EvidenceOut.model_validate(e._asdict())
            for e in await conn.execute(_EVIDENCE, {"result": scoring.result_id})
        ]
        critic: ModelOut | None = None
        if scoring.critic_outcome != "not_run":
            chain = await _chain(conn, scoring)
            critic = await _critic(conn, chain)
            if scoring.result_status == "contested":
                contested = await _contested(conn, scored, scoring, chain)
        score = _score(scored, scoring, evidence, critic, scored if scored != job_id else None)
    provenance = _provenance(job_id, row.field_provenance)
    sources = await _sources(conn, provenance)
    postings = [
        PostingOut.model_validate(p._asdict()) for p in await conn.execute(_POSTINGS, {"job": job_id})
    ]
    links = (await outbound_links(conn, [job_id])).get(job_id, OutboundLinks(None, None))
    merges = [MergeEventOut.model_validate(e._asdict()) for e in await conn.execute(_MERGES, {"job": job_id})]
    feedback = [
        JobFeedbackOut.model_validate(f._asdict()) for f in await conn.execute(_FEEDBACK, {"job": job_id})
    ]
    group = None if row.duplicate_group_id is None else await group_view(conn, int(row.duplicate_group_id))
    params = {"job": job_id, "tuple": active.llm_tuple_id, "scope": active.scope_version_id}
    bucket: ScoringBucket | None = (await conn.execute(_BUCKET, params)).scalar_one()
    items, history_next = await history_page(conn, job_id, after=None, limit=HISTORY_LIMIT)
    detail = JobDetailOut(
        job=_job(row, provenance, sources),
        description=_description(scoring, scored),
        postings=postings,
        links=LinksOut(open_ad=links.open_ad, adzuna=links.adzuna),
        score=score,
        contested=contested,
        target=target_of(row),
        bucket=bucket,
        group=group,
        merge_events=merges,
        user=_user(row),
        feedback=feedback,
        history=HistoryPageOut(items=items, next_cursor=None),
    )
    detail._history_next = history_next
    return detail
