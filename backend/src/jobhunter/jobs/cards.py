"""The cards of the Jobs list (spec §15.3 L5192-5212, §15.5, §7.18, §8.7 L3339-3344; plan M4 Task 5, B2, B8,
B17, B21-B24, B27).

`build_cards(conn, rows, *, now)` renders the rows of a list page (`listing.SelectedRow`, in their order) as
`JobCardOut`s in the caller's read snapshot, with **one statement per kind of data for the whole page**, never
one per card: the active configuration (`listing.read_active`), the cards, the members of the page's group
cards, the knockout texts of its results, the previous scores of its cards being re-scored, the descriptions
its keyword chips are found in, and its outbound links (`jobs.links`). A kind that no card of the page needs
is not read, so a page runs at most seven statements, however many cards it holds. `now` is the request's
time (the route's clock); no card field depends on it.

A card is a row of the list: an unmerged job with a current revision (a group card is its representative's
row, `listing`). Its score row is the listing's (`jobs._sql.CARD_FROM`): `t` the active-tuple target of its
current revision, `r` the result `t` published, `d` the current instance of `r` under the active scope, `h`
its hard-filter result under that scope, `u` the user's actions.

- **Job fields**: `title`, `company` (`company_display`), `company_key`, `locations` (their labels),
  `location_kind`, `work_modes` (`work_mode_possible` in the order remote, hybrid, onsite) and
  `work_mode_known` (`work_mode_source <> 'none'`: B5's unknown mode is all three, never remote),
  `posted_at` (`posted_at_effective`) and `posted_precision`, `first_seen_at`, `last_seen_at`,
  `source_ids`, `availability`, `completeness`, `ad_language`. A group card's `availability` is `active`
  when its representative or one of its deferred members is (§8.7 L3342), else the representative's.
- **`user`**: the timestamps of `u` and whether it holds a note (`has_note`; the note itself never leaves
  the drawer); `None` before the user's first action.
- **`score`**: `d`'s interval, coverage and confidence, and `r`'s status; `None` without a scored instance.
  **`target`**: `t`'s id, state, reason, refusal category, similarity (the preliminary bar of an unscored
  card) and override; `None` without one. **`bucket`**: the card's scoring state, the listing's exclusive
  buckets (`jobs._sql.BUCKET_SQL`, B8).
- **`badges`**, in the order of `Badge`: `contested` (`r` contested), `partial` (a partial description),
  `shortened` (`r.truncated_fields` not empty, B22), `possibly_not_a_job_ad` (the critic reviewed the
  draft's not-a-job-ad flag, `r.fact_confirmations ? 'not_a_job_ad:ad'`, and the result is not one, B23),
  `injection_suspected`, `stale` and `closed` (the card's availability), `adzuna` (an Adzuna source: the
  attribution badge, whose link is `links.adzuna`).
- **`knockouts`** (B21): each record of `d.knockouts` (`scoring.aggregate`: code, fact id, comparison,
  severity, binding) as `KnockoutOut`: `status` is its comparison (`met`, `unmet`, `uncomparable`; an
  `unmet` one that does not bind is unconfirmed), `requirement` the ad's requirement, read from `r.job_facts`
  at the fact id's place (`scoring.facts.binding_facts`), and `profile_value` the profile's side, read from
  the candidate's `inputs` in `r.knockout_candidates` (`scoring.knockouts`); `profile_value` is `None` once
  the result's profile version is purged. Each stored record (a knockout record, a candidate, a field of
  the job facts or an item of one of its lists) is read on its own: one that cannot be read is left out,
  and a warning names the result and how many were, never their content (`job_card_records_skipped`).
  `requirement` is, by code: `mandatory_language` the language (its ISO code, or the ad's name for `other`)
  and its level (none when `unspecified`), as in `de C1`; `degree` the level and, in parentheses, the field;
  `certification` the name; `security_clearance` the level; `contract_type` the job's structured employment
  types; `None` for the rest. `profile_value` is the profile's level for `mandatory_language` and `degree`,
  its `work_authorization_de` for `work_permit`, else `None`.
- **`filter_reasons`**: `h.reasons` (§11.1 codes; empty without a row). **`filter_keyword`** (B24): with
  `excluded_keyword` among them, the first of the active scope's excluded keywords found in the title, else
  in the current revision's description when the scope matches descriptions
  (`scoring.filters.first_excluded_keyword`).
- **`previous`** (B17): while the card has no published active-tuple target (re-scoring), the newest result
  (by `published_at`, then id) of another tuple for any revision of the job that has a scored current
  instance, with that instance's midpoint, the instance under the active scope first, else the one computed
  last; and the number of the result's profile version ("previous: 72 (profile v3)").
- **Groups** (§15.5, §7.18 `display_scores`, B2, B27). The representative of an active group is a card of
  kind `group`: `group` holds the group's size (its unmerged members, the representative included), the
  representative's reference and its **deferred** members (the jobs whose active-tuple target is
  `deferred_duplicate`), by job id. A member of an active group with its own current instance (split out of
  the deferral: scored individually, or with conflicting facts) is its own card with `possible_duplicate_of`,
  the representative. A member of an active group without a representative carries `group_badge`: the
  group and its `unscorable_reason`.
- **`links`**: `jobs.links.outbound_links`, never logged.

Every statement is static SQL and starts with its tag, `/* jobs_cards.<kind> */`.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.api.models import DecimalOut, UtcDatetime
from jobhunter.db.sql import q
from jobhunter.jobs import _sql
from jobhunter.jobs.links import OutboundLinks, outbound_links
from jobhunter.jobs.listing import Active, SelectedRow, read_active
from jobhunter.jobs.query import ScoringBucket
from jobhunter.llm.schemas.generator import JobFacts
from jobhunter.observability.logging import get_logger
from jobhunter.run_settings import ScopeSettings, scope_from_payload
from jobhunter.scoring.facts import binding_facts, fact_id
from jobhunter.scoring.filters import first_excluded_keyword
from jobhunter.types import JSONValue

log = get_logger("jobhunter.jobs.cards")

Badge = Literal[
    "contested",
    "partial",
    "shortened",
    "possibly_not_a_job_ad",
    "injection_suspected",
    "stale",
    "closed",
    "adzuna",
]
BADGES: Final[tuple[Badge, ...]] = get_args(Badge)
_MODES: Final = ("remote", "hybrid", "onsite")
_PUBLISHED: Final = frozenset({"published_accepted", "published_contested"})
_DEFERRED: Final = "deferred_duplicate"
_KEYWORD_REASON: Final = "excluded_keyword"


# --- the card ---------------------------------------------------------------------------------------------


class CardUserOut(BaseModel):
    seen_at: UtcDatetime | None
    saved_at: UtcDatetime | None
    applied_at: UtcDatetime | None
    hidden_at: UtcDatetime | None
    has_note: bool


class ScoreOut(BaseModel):
    mid: DecimalOut
    low: DecimalOut
    high: DecimalOut
    coverage: DecimalOut
    confidence: Literal["low", "medium", "high"]
    result_status: Literal["accepted", "contested"]


class TargetOut(BaseModel):
    id: int
    state: str
    state_reason: str | None
    refusal_category: str | None
    similarity: float | None
    override: str


class KnockoutOut(BaseModel):
    code: str
    binding: bool
    severity: str
    status: str
    requirement: str | None
    profile_value: str | None


class PreviousOut(BaseModel):
    mid: DecimalOut
    profile_version_no: int


class RefOut(BaseModel):
    job_id: int
    title: str
    source_ids: list[str]


class MemberBriefOut(BaseModel):
    job_id: int
    title: str
    source_ids: list[str]
    availability: str
    completeness: str
    target_state: str | None


class GroupCardOut(BaseModel):
    group_id: int
    size: int
    representative: RefOut
    members: list[MemberBriefOut]


class GroupBadgeOut(BaseModel):
    group_id: int
    unscorable_reason: str | None


class LinksOut(BaseModel):
    open_ad: str | None
    adzuna: str | None


class JobCardOut(BaseModel):
    """One card of the Jobs list (module docstring)."""

    job_id: int
    kind: Literal["job", "group"]
    title: str
    company: str
    company_key: str
    locations: list[str]
    location_kind: str
    work_modes: list[str]
    work_mode_known: bool
    posted_at: UtcDatetime | None
    posted_precision: str
    first_seen_at: UtcDatetime
    last_seen_at: UtcDatetime
    source_ids: list[str]
    availability: str
    completeness: str
    ad_language: str
    user: CardUserOut | None
    score: ScoreOut | None
    target: TargetOut | None
    bucket: ScoringBucket
    badges: list[Badge]
    knockouts: list[KnockoutOut]
    filter_reasons: list[str]
    filter_keyword: str | None
    previous: PreviousOut | None
    possible_duplicate_of: RefOut | None
    group: GroupCardOut | None
    group_badge: GroupBadgeOut | None
    links: LinksOut


# --- the statements ---------------------------------------------------------------------------------------

# The scoring bucket of a card's score row: the listing's exclusive buckets (B8), each its own WHEN
_BUCKET: Final = (
    "CASE " + " ".join(f"WHEN {_sql.BUCKET_SQL[b]} THEN '{b}'" for b in _sql.BUCKET_ORDER) + " END"
)
_CARDS: Final = q(
    "/* jobs_cards.cards */ SELECT j.id AS job_id, j.title, j.title_norm, j.company_display, j.company_key, "
    "j.locations, j.location_kind, j.work_mode_possible, j.work_mode_source, j.posted_at_effective, "
    "j.posted_precision, j.first_seen_at, j.last_seen_at, j.source_ids, j.availability, j.completeness, "
    "j.ad_language, t.id AS target_id, t.state, t.state_reason, t.refusal_category, t.similarity, "
    "t.override, r.id AS result_id, r.status AS result_status, r.not_a_job_ad, r.injection_suspected, "
    "cardinality(r.truncated_fields) > 0 AS shortened, "
    "(r.fact_confirmations -> 'not_a_job_ad:ad') IS NOT NULL AS not_an_ad_reviewed, "
    "d.id AS instance_id, d.score_low, d.score_mid, d.score_high, d.coverage, d.confidence, d.knockouts, "
    "h.reasons, u.job_id IS NOT NULL AS acted, u.seen_at, u.saved_at, u.applied_at, u.hidden_at, "
    "coalesce(u.note, '') <> '' AS has_note, g.id AS group_id, g.representative_job_id, "
    "g.unscorable_reason, rep.title AS rep_title, rep.source_ids AS rep_source_ids, "
    + _BUCKET
    + " AS bucket"
    + _sql.CARD_FROM
    + " LEFT JOIN duplicate_groups g ON g.id = j.duplicate_group_id AND g.status = 'active'"
    " LEFT JOIN jobs rep ON rep.id = g.representative_job_id"
    " WHERE j.id = ANY(CAST(:ids AS bigint[]))"
)
_MEMBERS: Final = q(
    "/* jobs_cards.members */ SELECT m.duplicate_group_id AS group_id, m.id AS job_id, m.title, "
    "m.source_ids, m.availability, m.completeness, tm.state AS target_state FROM jobs m "
    "LEFT JOIN score_targets tm ON tm.job_revision_id = m.current_revision_id "
    "AND tm.llm_tuple_id = CAST(:tuple AS bigint) "
    "WHERE m.duplicate_group_id = ANY(CAST(:groups AS bigint[])) AND m.merged_into_job_id IS NULL "
    "ORDER BY m.duplicate_group_id, m.id"
)
_KNOCKOUT_TEXTS: Final = q(
    "/* jobs_cards.knockouts */ SELECT r.id AS result_id, r.job_facts, r.knockout_candidates, "
    "pv.purged_at IS NOT NULL AS purged FROM llm_results r "
    "JOIN profile_versions pv ON pv.id = r.profile_version_id WHERE r.id = ANY(CAST(:ids AS bigint[]))"
)
_PREVIOUS: Final = q(
    "/* jobs_cards.previous */ SELECT DISTINCT ON (jr.job_id) jr.job_id, i.score_mid, pv.version_no "
    "FROM job_revisions jr JOIN llm_results r ON r.job_revision_id = jr.id "
    "JOIN score_targets pt ON pt.id = r.target_id "
    "JOIN profile_versions pv ON pv.id = r.profile_version_id "
    "JOIN LATERAL (SELECT d.score_mid FROM det_results d WHERE d.llm_result_id = r.id AND d.is_current "
    "AND d.score_mid IS NOT NULL ORDER BY d.scope_version_id = :scope DESC, d.computed_at DESC, d.id DESC "
    "LIMIT 1) i ON true "
    "WHERE jr.job_id = ANY(CAST(:ids AS bigint[])) "
    "AND pt.llm_tuple_id IS DISTINCT FROM CAST(:tuple AS bigint) "
    "ORDER BY jr.job_id, r.published_at DESC, r.id DESC"
)
_DESCRIPTIONS: Final = q(
    "/* jobs_cards.descriptions */ SELECT j.id AS job_id, v.description_text FROM jobs j "
    "JOIN job_revisions v ON v.id = j.current_revision_id WHERE j.id = ANY(CAST(:ids AS bigint[]))"
)


# --- the knockouts ----------------------------------------------------------------------------------------


class _Record(BaseModel):
    """A knockout record of `det_results.knockouts` (`scoring.aggregate`)."""

    model_config = ConfigDict(extra="ignore")

    code: str
    fact_id: str
    comparison: str
    severity: str | None
    binding: bool


class _Candidate(BaseModel):
    """What a card reads of a candidate of `llm_results.knockout_candidates` (`scoring.knockouts`)."""

    model_config = ConfigDict(extra="ignore")

    fact_id: str
    inputs: dict[str, JSONValue]


def _skipped(result_id: int, kind: str, count: int) -> None:
    """A content-free warning that `count` stored records of a result could not be read (never their text)."""
    if count:
        log.warning("job_card_records_skipped", result_id=result_id, kind=kind, skipped=count)


def _records(value: object) -> tuple[list[_Record], int]:
    """The knockout records of an instance, each read on its own, and how many could not be (a document that
    is no list counts once; an empty one, as some writers leave it, is no record)."""
    if value in (None, {}, []):
        return [], 0
    if not isinstance(value, list):
        return [], 1
    records: list[_Record] = []
    for item in value:
        try:
            records.append(_Record.model_validate(item))
        except ValidationError:
            continue
    return records, len(value) - len(records)


@dataclass(frozen=True)
class _Texts:
    """What a result says about its knockouts: each fact's requirement, each candidate's inputs, and whether
    its profile version is purged."""

    requirements: Mapping[str, str | None]
    inputs: Mapping[str, Mapping[str, JSONValue]]
    purged: bool


def _text(*parts: object) -> str | None:
    words = [str(part) for part in parts if isinstance(part, str) and part]
    return " ".join(words) or None


# A `JobFacts` that asserts nothing: what a field of a stored document that cannot be read stands for
_NOTHING_ASSERTED: Final[dict[str, JSONValue]] = {
    "work_mode": {"value": "unspecified", "evidence": []},
    "onsite_only": {"asserted": False, "evidence": []},
    "remote_region": {"value": "unspecified", "countries": [], "evidence": []},
    "required_languages": [],
    "must_haves": [],
    "nice_to_haves": [],
    "seniority": {"value": "unspecified", "evidence": []},
    "min_years_experience": {"value": None, "evidence": []},
    "industry": {"value": None, "evidence": []},
    "employment_types": {"values": [], "evidence": []},
    "degree": {
        "level": "unspecified",
        "mandatory": False,
        "or_equivalent": False,
        "field": None,
        "evidence": [],
    },
    "certifications": [],
    "security_clearance": {"required": False, "level": None, "evidence": []},
    "work_permit": {"required": False, "evidence": []},
}
_NEUTRAL_FACTS: Final = JobFacts.model_validate(_NOTHING_ASSERTED)  # fails at import if the model changed
_LIST_FACTS: Final = frozenset({"required_languages", "must_haves", "nice_to_haves", "certifications"})


def _valid_facts(fields: Mapping[str, object]) -> bool:
    try:
        JobFacts.model_validate({**_NOTHING_ASSERTED, **fields})
    except ValidationError:
        return False
    return True


def _job_facts(document: object) -> tuple[JobFacts, int]:
    """A result's `job_facts` as a `JobFacts`, each field (and each item of a list field) read on its own: a
    field that is absent or cannot be read asserts nothing, an item that cannot be read is left out; and how
    many present ones could not be read (a key that is no field among them)."""
    if not isinstance(document, dict):
        return _NEUTRAL_FACTS, 1
    if _valid_facts(document):
        return JobFacts.model_validate({**_NOTHING_ASSERTED, **document}), 0
    kept: dict[str, object] = {}
    skipped = 0
    for name, value in document.items():
        if name not in JobFacts.model_fields:
            skipped += 1
        elif name in _LIST_FACTS and isinstance(value, list):
            items = [item for item in value if _valid_facts({name: [item]})]
            skipped += len(value) - len(items)
            kept[name] = items
        elif _valid_facts({name: value}):
            kept[name] = value
        else:
            skipped += 1
    return JobFacts.model_validate({**_NOTHING_ASSERTED, **kept}), skipped


def _requirements(facts: JobFacts) -> dict[str, str | None]:
    """The requirement text of each fact id of a result's job facts (module docstring)."""
    found: dict[str, str | None] = {}
    for fact in binding_facts(facts):
        key = fact_id(fact.code, fact.subject)
        if key in found:  # the first fact of an id is kept (§11.4)
            continue
        match fact.path:
            case ("required_languages", int(i)):
                language = facts.required_languages[i]
                name = language.language_other if language.language == "other" else language.language
                level = None if language.level == "unspecified" else language.level
                found[key] = _text(name, level)
            case ("degree",):
                degree = facts.degree
                stated = None if degree.level == "unspecified" else degree.level
                found[key] = _text(stated, None if degree.field is None else f"({degree.field})")
            case ("certifications", int(i)):
                found[key] = _text(facts.certifications[i].name)
            case ("security_clearance",):
                found[key] = _text(facts.security_clearance.level)
            case _:
                found[key] = None
    return found


def _texts(row: Any) -> _Texts:
    """The knockout texts of a result's row, each stored record read on its own (the unreadable ones are
    left out with a content-free warning)."""
    result_id = int(row.result_id)
    readable, skipped = _candidates(row.knockout_candidates)
    _skipped(result_id, "knockout_candidates", skipped)
    candidates: dict[str, Mapping[str, JSONValue]] = {}
    for candidate in readable:
        candidates.setdefault(candidate.fact_id, candidate.inputs)
    facts, skipped = _job_facts(row.job_facts)
    _skipped(result_id, "job_facts", skipped)
    return _Texts(_requirements(facts), candidates, bool(row.purged))


def _candidates(value: object) -> tuple[list[_Candidate], int]:
    """A result's knockout candidates, each read on its own, and how many could not be (as `_records`: a
    document that is no list counts once, an empty one is no candidate; Ruling T5-g)."""
    if value in (None, {}, []):
        return [], 0
    if not isinstance(value, list):
        return [], 1
    readable: list[_Candidate] = []
    for item in value:
        try:
            readable.append(_Candidate.model_validate(item))
        except ValidationError:
            continue
    return readable, len(value) - len(readable)


def _input_text(inputs: Mapping[str, JSONValue], name: str) -> str | None:
    value = inputs.get(name)
    return value if isinstance(value, str) else None


def _knockout(record: _Record, texts: _Texts | None) -> KnockoutOut:
    inputs = {} if texts is None else texts.inputs.get(record.fact_id, {})
    requirement = None if texts is None else texts.requirements.get(record.fact_id)
    if record.code == "contract_type":  # a structured requirement: no fact, the job's types are the inputs
        types = inputs.get("employment_types")
        listed = [x for x in types if isinstance(x, str)] if isinstance(types, list) else []
        requirement = ", ".join(listed) or None
    match record.code:
        case "mandatory_language" | "degree":
            profile_value = _input_text(inputs, "profile_level")
        case "work_permit":
            profile_value = _input_text(inputs, "work_authorization_de")
        case _:
            profile_value = None
    return KnockoutOut(
        code=record.code,
        binding=record.binding,
        severity=record.severity or "",
        status=record.comparison,
        requirement=requirement,
        profile_value=None if texts is None or texts.purged else profile_value,
    )


def instance_knockouts(row: Any) -> list[KnockoutOut]:
    """The knockouts of one score instance as a card shows them (module docstring, `knockouts`), for the
    drawer (M4 Task 6): `row` holds the instance's `knockouts` and its result's `result_id`, `job_facts`,
    `knockout_candidates` and `purged` (whether its profile version is purged). Each stored record is read on
    its own; the unreadable ones are left out with the content-free warning."""
    records, skipped = _records(row.knockouts)
    _skipped(int(row.result_id), "knockouts", skipped)
    texts = _texts(row) if records else None
    return [_knockout(record, texts) for record in records]


# --- the page ---------------------------------------------------------------------------------------------


def _ids(values: Sequence[int]) -> list[int]:
    return sorted(set(values))


def _score(row: Any) -> ScoreOut | None:
    values = (row.score_mid, row.score_low, row.score_high, row.coverage, row.confidence)
    if any(value is None for value in values):
        return None
    return ScoreOut(
        mid=row.score_mid,
        low=row.score_low,
        high=row.score_high,
        coverage=row.coverage,
        confidence=row.confidence,
        result_status=row.result_status,
    )


def _target(row: Any) -> TargetOut | None:
    if row.target_id is None:
        return None
    similarity = None if row.similarity is None else float(row.similarity)
    return TargetOut(
        id=row.target_id,
        state=row.state,
        state_reason=row.state_reason,
        refusal_category=row.refusal_category,
        similarity=similarity,
        override=row.override,
    )


def _user(row: Any) -> CardUserOut | None:
    if not row.acted:
        return None
    return CardUserOut(
        seen_at=row.seen_at,
        saved_at=row.saved_at,
        applied_at=row.applied_at,
        hidden_at=row.hidden_at,
        has_note=bool(row.has_note),
    )


def _labels(locations: object) -> list[str]:
    if not isinstance(locations, list):
        return []
    return [str(x["label"]) for x in locations if isinstance(x, dict) and isinstance(x.get("label"), str)]


def _badges(row: Any, availability: str) -> list[Badge]:
    on: dict[Badge, bool] = {
        "contested": row.result_status == "contested",
        "partial": row.completeness == "partial",
        "shortened": bool(row.shortened),
        "possibly_not_a_job_ad": bool(row.not_an_ad_reviewed) and row.not_a_job_ad is False,
        "injection_suspected": bool(row.injection_suspected),
        "stale": availability == "stale",
        "closed": availability == "closed",
        "adzuna": "adzuna" in row.source_ids,
    }
    return [badge for badge in BADGES if on[badge]]


@dataclass(frozen=True)
class _Member:
    brief: MemberBriefOut
    deferred: bool


async def _members(conn: AsyncConnection, groups: Sequence[int], active: Active) -> dict[int, list[_Member]]:
    found: dict[int, list[_Member]] = {}
    if not groups:
        return found
    rows = await conn.execute(_MEMBERS, {"groups": _ids(groups), "tuple": active.llm_tuple_id})
    for row in rows:
        brief = MemberBriefOut(
            job_id=row.job_id,
            title=row.title,
            source_ids=list(row.source_ids),
            availability=row.availability,
            completeness=row.completeness,
            target_state=row.target_state,
        )
        found.setdefault(int(row.group_id), []).append(_Member(brief, row.target_state == _DEFERRED))
    return found


async def _knockout_texts(conn: AsyncConnection, results: Sequence[int]) -> dict[int, _Texts]:
    if not results:
        return {}
    rows = await conn.execute(_KNOCKOUT_TEXTS, {"ids": _ids(results)})
    return {int(row.result_id): _texts(row) for row in rows}


async def _previous(conn: AsyncConnection, jobs: Sequence[int], active: Active) -> dict[int, PreviousOut]:
    if not jobs:
        return {}
    params = {"ids": _ids(jobs), "tuple": active.llm_tuple_id, "scope": active.scope_version_id}
    rows = await conn.execute(_PREVIOUS, params)
    return {
        int(row.job_id): PreviousOut(mid=row.score_mid, profile_version_no=row.version_no) for row in rows
    }


async def _keywords(conn: AsyncConnection, cards: Sequence[Any], active: Active) -> dict[int, str]:
    """B24: the excluded keyword of each card filtered by one (module docstring)."""
    filtered = [row for row in cards if _KEYWORD_REASON in (row.reasons or ())]
    if not filtered:
        return {}
    scope: ScopeSettings = scope_from_payload(active.scope)
    found: dict[int, str] = {}
    for row in filtered:
        if (keyword := first_excluded_keyword(scope.excluded_keywords, row.title_norm)) is not None:
            found[int(row.job_id)] = keyword
    rest = {int(row.job_id): str(row.title_norm) for row in filtered if int(row.job_id) not in found}
    if rest and scope.exclude_keywords_in_description:
        for row in await conn.execute(_DESCRIPTIONS, {"ids": _ids(list(rest))}):
            job_id = int(row.job_id)
            keyword = first_excluded_keyword(scope.excluded_keywords, rest[job_id], row.description_text)
            if keyword is not None:
                found[job_id] = keyword
    return found


async def build_cards(
    conn: AsyncConnection, rows: Sequence[SelectedRow], *, now: datetime
) -> list[JobCardOut]:
    """The cards of `rows`, in their order (module docstring). A row that names no job raises `LookupError`
    (the rows come from a page selected in the same snapshot, so it never does)."""
    if not rows:
        return []
    active = await read_active(conn)
    params = {"ids": _ids([row.job_id for row in rows]), "tuple": active.llm_tuple_id}
    cards = {
        int(row.job_id): row
        for row in await conn.execute(_CARDS, {**params, "scope": active.scope_version_id})
    }
    if missing := [row.job_id for row in rows if row.job_id not in cards]:
        raise LookupError(f"no card for the jobs {missing}")
    listed = list(cards.values())
    records: dict[int, list[_Record]] = {}
    for card in listed:
        records[int(card.job_id)], skipped = _records(card.knockouts)
        if card.result_id is not None:
            _skipped(int(card.result_id), "knockouts", skipped)
    represented = [
        int(c.group_id) for c in listed if c.group_id is not None and c.representative_job_id == c.job_id
    ]
    members = await _members(conn, represented, active)
    with_knockouts = [int(c.result_id) for c in listed if records[int(c.job_id)]]
    texts = await _knockout_texts(conn, with_knockouts)
    rescored = [int(c.job_id) for c in listed if c.state not in _PUBLISHED]
    previous = await _previous(conn, rescored, active)
    keywords = await _keywords(conn, listed, active)
    links = await outbound_links(conn, list(cards))
    return [
        _card(
            cards[row.job_id], records[row.job_id], members, texts, previous, keywords, links.get(row.job_id)
        )
        for row in rows
    ]


def _card(
    row: Any,
    records: Sequence[_Record],
    members: Mapping[int, Sequence[_Member]],
    texts: Mapping[int, _Texts],
    previous: Mapping[int, PreviousOut],
    keywords: Mapping[int, str],
    links: OutboundLinks | None,
) -> JobCardOut:
    job_id = int(row.job_id)
    group_id = None if row.group_id is None else int(row.group_id)
    representative = None if row.representative_job_id is None else int(row.representative_job_id)
    group = possible_duplicate_of = group_badge = None
    availability = str(row.availability)
    if group_id is not None and representative == job_id:
        of_group = members.get(group_id, [])
        deferred = [m.brief for m in of_group if m.deferred and m.brief.job_id != job_id]
        if any(m.availability == "active" for m in deferred):
            availability = "active"  # §8.7 L3342: a group is active when a member is
        group = GroupCardOut(
            group_id=group_id,
            size=len(of_group),
            representative=RefOut(job_id=job_id, title=row.title, source_ids=list(row.source_ids)),
            members=deferred,
        )
    elif group_id is not None and representative is None:
        group_badge = GroupBadgeOut(group_id=group_id, unscorable_reason=row.unscorable_reason)
    elif group_id is not None and representative is not None and row.instance_id is not None:
        possible_duplicate_of = RefOut(
            job_id=representative, title=row.rep_title, source_ids=list(row.rep_source_ids)
        )
    found = texts.get(int(row.result_id)) if row.result_id is not None else None
    outbound = links or OutboundLinks(None, None)
    return JobCardOut(
        job_id=job_id,
        kind="group" if group is not None else "job",
        title=row.title,
        company=row.company_display,
        company_key=row.company_key,
        locations=_labels(row.locations),
        location_kind=row.location_kind,
        work_modes=[mode for mode in _MODES if mode in row.work_mode_possible],
        work_mode_known=row.work_mode_source != "none",
        posted_at=row.posted_at_effective,
        posted_precision=row.posted_precision,
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        source_ids=list(row.source_ids),
        availability=availability,
        completeness=row.completeness,
        ad_language=row.ad_language,
        user=_user(row),
        score=_score(row),
        target=_target(row),
        bucket=_bucket(row),
        badges=_badges(row, availability),
        knockouts=[_knockout(record, found) for record in records],
        filter_reasons=list(row.reasons or ()),
        filter_keyword=keywords.get(job_id),
        previous=previous.get(job_id),
        possible_duplicate_of=possible_duplicate_of,
        group=group,
        group_badge=group_badge,
        links=LinksOut(open_ad=outbound.open_ad, adzuna=outbound.adzuna),
    )


def _bucket(row: Any) -> ScoringBucket:
    if row.bucket is None:  # a published card without a current instance, which no page lists (T4-c)
        raise ValueError(f"job {row.job_id} is in no scoring bucket")
    bucket: ScoringBucket = row.bucket
    return bucket
