"""Synthetic Jobs list cards (plan M4 Task 4): the per-card seeder of the list engine's tests and the
set-based seed of the plan tests here and the p95 tests of Tasks 5 and 6. Invented titles, companies and
texts only, no ad content.

`seed_cards(engine, specs)` commits one job per `Card` in spec order, its current revision holding the card's
title and text, and returns their job ids. A card's `state` is the target of its current revision under the
active tuple (`None`: no target): a published state comes with its attempt, call, draft and result
(`tests/support/m3.py`) and, when the card has a `score` or is `not_a_job_ad`, the result's current score
instance under the active scope. `deferred_to` makes the card a deferred member of the active duplicate group
of the card at that index (its target `deferred_duplicate`), which is no card of the list. `skip_ids` leaves
that many job ids unused before the card's own, so a test can lay its cards out on chosen ids.

`bulk_seed_cards(engine, n, *, scored_share, groups, langs)` commits `n` cards in one set-based
`INSERT … SELECT` per table (every row from one JSONB array, `jsonb_to_recordset`) over the active tuple,
which it activates when there is none: `scored_share` of the cards published with their current score
instance; of the others every tenth (the first, the eleventh, …) without a target of the tuple and the rest
pending, some targets without a similarity; `groups` active duplicate
groups (each a card and one deferred member job, which is no card), and revisions whose description text is
in each language of `langs` in turn. It analyses nothing: a caller that needs planner statistics runs
`ANALYZE` as the owner on a server of its own.

`activate_tuple` makes a new confirmed profile's tuple the active one; the `active_tuple` fixture does it for
a test on the shared database (whose reset restores no tuple).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

import pytest
from jobhunter.clock import utcnow
from jobhunter.db.sql import q
from jobhunter.normalize.titles import normalize_title
from jobhunter.types import JSONValue
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.elements import TextClause

from tests.support.m2 import insert_profile_chain
from tests.support.m3 import (
    Job,
    insert_attempt,
    insert_draft,
    insert_group,
    insert_id,
    insert_job,
    insert_row,
    insert_scoring_call,
    insert_target,
    result_values,
)

SEEN: Final = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)  # a card's first sighting unless it names its own
BERLIN: Final = (52.52437, 13.41053)
MUNICH: Final = (48.13743, 11.57549)
HAMBURG: Final = (53.55073, 9.99302)
# 20 km north of Berlin on the haversine sphere (a degree of latitude is 111.195 km)
NORTH_OF_BERLIN: Final = (BERLIN[0] + 20 / 111.19508, BERLIN[1])
PUBLISHED: Final = frozenset({"published_accepted", "published_contested"})

# The columns a target in each seedable state needs beyond `state` (the `score_targets` CHECKs); the scope's
# id fills `filter_scope_version_id`.
_STATE_COLUMNS: Final[Mapping[str, Mapping[str, object]]] = {
    "pending": {},
    "budget_deferred": {"state_reason": "test"},
    "filtered_out": {"state_reason": "test"},
    "refused": {"refusal_category": "test"},
    "failed_max_tokens": {"state_reason": "test"},
    "failed_invalid_output": {"state_reason": "test"},
    "failed_request": {"state_reason": "test"},
    "failed_transient": {"state_reason": "test"},
    "failed_attempts_exhausted": {"state_reason": "test"},
    "expired_unscored": {"state_reason": "test"},
    "cancelled": {"state_reason": "test"},
}


def place(*points: tuple[float, float], resolution: str = "unique") -> dict[str, JSONValue]:
    """A stored location (`sources.parsing.locations.Location`) with one candidate per point."""
    return {
        "label": "Testort",
        "resolution": resolution,
        "candidates": [
            {"gazetteer_id": f"geonames:{i}", "country_code": "DE", "lat": lat, "lon": lon}
            for i, (lat, lon) in enumerate(points, start=1)
        ],
        "postal_code": None,
        "source": "jsearch",
    }


@dataclass(frozen=True)
class Card:
    """One job of the list and its state under the active tuple and scope (module docstring)."""

    title: str = "Testrolle"
    company_key: str = "musterfirma"
    description: str = "Erfundene Testanzeige."
    ad_language: str = "de"
    posted_at: datetime | None = None  # None: the posting date is unknown
    first_seen_at: datetime = SEEN
    locations: tuple[dict[str, JSONValue], ...] = ()
    location_kind: str = "unknown"
    work_mode_possible: tuple[str, ...] = ("remote", "hybrid", "onsite")
    work_mode_source: str = "none"
    employment_types: tuple[str, ...] = ()
    seniority_tokens: tuple[str, ...] | None = None  # None: those of the title
    structured_seniority: str | None = None
    source_ids: tuple[str, ...] = ("jsearch",)
    availability: str = "active"
    completeness: str = "full"
    state: str | None = None  # the active-tuple target of the current revision; None: no target
    similarity: float | None = None
    score: tuple[Decimal, Decimal] | None = None  # (low, high) of the current instance of a published card
    not_a_job_ad: bool = False  # a published result whose current instance has no score
    coverage: Decimal = Decimal("0.9")
    confidence: str = "high"
    passed: bool | None = None  # the hard-filter result under the active scope; None: no row
    seen: bool = False
    saved: bool = False
    applied: bool = False
    hidden: bool = False
    deferred_to: int | None = None  # the index in the specs of the representative of this deferred member
    skip_ids: int = 0


_ACTIVE: Final = q("SELECT llm_tuple_id, scope_version_id FROM active_config WHERE id = 1")
_ACTIVATE: Final = q(
    "UPDATE active_config SET profile_version_id = :p, llm_tuple_id = :t, updated_at = now()"
)
_SKIP_JOB_IDS: Final = q("SELECT nextval(pg_get_serial_sequence('jobs', 'id')) FROM generate_series(1, :k)")
_NEXT_RESULT_ID: Final = q("SELECT nextval(pg_get_serial_sequence('llm_results', 'id'))")
_CARD_COLUMNS: Final = q(
    "UPDATE jobs SET title = :title, title_norm = :title_norm, company_key = :company_key, "
    "company_display = :company_key, seniority_tokens = CAST(:seniority_tokens AS text[]), "
    "structured_seniority = :structured_seniority, locations = :locations, location_kind = :location_kind, "
    "work_mode_possible = CAST(:work_mode_possible AS text[]), work_mode_source = :work_mode_source, "
    "employment_types = CAST(:employment_types AS text[]), ad_language = :ad_language, "
    "completeness = :completeness, posted_at_effective = :posted_at, posted_precision = :precision, "
    "first_seen_at = :first_seen_at, last_seen_at = :first_seen_at, availability = :availability, "
    "source_ids = CAST(:source_ids AS text[]) WHERE id = :id",
    jsonb=["locations"],
)
_REVISION_TEXT: Final = q(
    "UPDATE job_revisions SET llm_input = :llm_input, description_text = :description, "
    "ad_language = :ad_language WHERE id = :id",
    jsonb=["llm_input"],
)


async def activate_tuple(engine: AsyncEngine, n: int = 1) -> int:
    """A new confirmed profile (chain number `n`) whose tuple `active_config` names, committed; its id."""
    async with engine.begin() as c:
        chain = await insert_profile_chain(c, n)
        await c.execute(_ACTIVATE, {"p": chain.profile_version_id, "t": chain.llm_tuple_id})
    return chain.llm_tuple_id


@pytest.fixture
async def active_tuple(engine: AsyncEngine) -> int:
    """The id of the tuple `activate_tuple` made the active one on the shared database."""
    return await activate_tuple(engine)


async def seed_cards(engine: AsyncEngine, specs: Sequence[Card]) -> list[int]:
    """Commit one job per spec (module docstring); their job ids in spec order."""
    async with engine.begin() as c:
        active = (await c.execute(_ACTIVE)).one()
        llm_tuple, scope = active.llm_tuple_id, int(active.scope_version_id)
        jobs: list[Job] = []
        for spec in specs:
            if spec.skip_ids:
                await c.execute(_SKIP_JOB_IDS, {"k": spec.skip_ids})
            jobs.append(await _insert_card(c, spec))
        groups = {
            rep: await insert_group(
                c, jobs[rep], [jobs[i] for i, s in enumerate(specs) if s.deferred_to == rep]
            )
            for rep in sorted({s.deferred_to for s in specs if s.deferred_to is not None})
        }
        for spec, job in zip(specs, jobs, strict=True):
            if spec.state is not None or spec.deferred_to is not None:
                if llm_tuple is None:
                    raise LookupError("a card with a target needs an active tuple (activate_tuple)")
                await _insert_state(c, spec, job, int(llm_tuple), scope, groups)
            await _insert_marks(c, spec, job, scope)
    return [job.job_id for job in jobs]


async def _insert_card(c: AsyncConnection, spec: Card) -> Job:
    job = await insert_job(c)
    title = normalize_title(spec.title)
    tokens = sorted(title.seniority_tokens) if spec.seniority_tokens is None else list(spec.seniority_tokens)
    await c.execute(
        _CARD_COLUMNS,
        {
            "id": job.job_id,
            "title": title.title,
            "title_norm": title.title_norm,
            "company_key": spec.company_key,
            "seniority_tokens": tokens,
            "structured_seniority": spec.structured_seniority,
            "locations": list(spec.locations),
            "location_kind": spec.location_kind,
            "work_mode_possible": list(spec.work_mode_possible),
            "work_mode_source": spec.work_mode_source,
            "employment_types": list(spec.employment_types),
            "ad_language": spec.ad_language,
            "completeness": spec.completeness,
            "posted_at": spec.posted_at,
            "precision": "unknown" if spec.posted_at is None else "exact",
            "first_seen_at": spec.first_seen_at,
            "availability": spec.availability,
            "source_ids": list(spec.source_ids),
        },
    )
    await c.execute(
        _REVISION_TEXT,
        {
            "id": job.revision_id,
            "llm_input": {"title": title.title},
            "description": spec.description,
            "ad_language": spec.ad_language,
        },
    )
    return job


async def _insert_state(
    c: AsyncConnection, spec: Card, job: Job, llm_tuple: int, scope: int, groups: Mapping[int, int]
) -> None:
    if spec.deferred_to is not None:
        await insert_target(
            c,
            job,
            llm_tuple,
            state="deferred_duplicate",
            deferred_group_id=groups[spec.deferred_to],
            similarity=spec.similarity,
        )
        return
    if spec.state in PUBLISHED:
        result = await _publish(c, spec, job, llm_tuple)
        if spec.score is not None or spec.not_a_job_ad:
            await _insert_instance(c, spec, result, job.job_id, scope)
        return
    state = spec.state
    if state is None or state not in _STATE_COLUMNS:
        raise ValueError(f"no seed for the target state {state}")
    columns = dict(_STATE_COLUMNS[state])
    if state == "filtered_out":
        columns["filter_scope_version_id"] = scope
    await insert_target(c, job, llm_tuple, state=state, similarity=spec.similarity, **columns)


async def _publish(c: AsyncConnection, spec: Card, job: Job, llm_tuple: int) -> int:
    """`insert_published` in the card's published state; the result's id."""
    result = int((await c.execute(_NEXT_RESULT_ID)).scalar_one())
    state = str(spec.state)
    target = await insert_target(
        c, job, llm_tuple, state=state, published_result_id=result, similarity=spec.similarity
    )
    attempt = await insert_attempt(c, target, outcome=state, lane="similarity")
    _, call = await insert_scoring_call(c, attempt_id=attempt, target_id=target)
    draft = await insert_draft(c, attempt, call)
    values = await result_values(
        c, target_id=target, attempt_id=attempt, draft_id=draft, job=job, llm_tuple_id=llm_tuple
    )
    values["not_a_job_ad"] = spec.not_a_job_ad
    if state == "published_contested":
        values |= {"status": "contested", "critic_outcome": "contested", "critic_injection": "none"}
    await insert_row(c, "llm_results", {"id": result, **values}, overriding=True)
    return result


async def _insert_instance(c: AsyncConnection, spec: Card, result: int, job_id: int, scope: int) -> None:
    low, high = spec.score if spec.score is not None else (None, None)
    mid = None if low is None or high is None else ((low + high) / 2).quantize(Decimal("0.01"), ROUND_HALF_UP)
    await insert_id(
        c,
        "det_results",
        {
            "llm_result_id": result,
            "scope_version_id": scope,
            "job_id": job_id,
            "det_algorithm_version": "det-test",
            "det_criteria": {},
            "knockouts": {},
            "industry_match": "neutral",
            "caps_applied": {},
            "score_low": low,
            "score_mid": mid,
            "score_high": high,
            "coverage": None if mid is None else spec.coverage,
            "confidence": None if mid is None else spec.confidence,
            "det_inputs": {},
            "det_inputs_hash": b"\x00",
        },
    )


async def _insert_marks(c: AsyncConnection, spec: Card, job: Job, scope: int) -> None:
    """The card's hard-filter result and user actions, when it has them."""
    if spec.passed is not None:
        values: dict[str, object] = {
            "job_id": job.job_id,
            "scope_version_id": scope,
            "passed": spec.passed,
            "inputs_hash": b"\x00",
            "det_algorithm_version": "det-test",
        }
        if not spec.passed:
            values["reasons"] = ["excluded_keyword"]
        await insert_row(c, "hard_filter_results", values)
    marks = {
        "seen_at": spec.seen,
        "saved_at": spec.saved,
        "applied_at": spec.applied,
        "hidden_at": spec.hidden,
    }
    if any(marks.values()):
        await insert_row(
            c,
            "user_job_actions",
            {"job_id": job.job_id, **{k: SEEN if v else None for k, v in marks.items()}},
        )


# --- the set-based seed ---------------------------------------------------------------------------------

# Invented titles, German and English, so that a keyword selects a few cards of the seed (two of the 25 hold
# `entwickler`, the keyword tests' word), and descriptions that name no role: the full text matches a role
# by the title alone.
BULK_TITLES: Final = (
    "Senior Softwareentwickler Java Backend (m/w/d)",
    "Frontend Entwickler React",
    "Data Engineer",
    "Platform Engineer",
    "Marketing Manager",
    "Projektleiter Bau (m/w/d)",
    "Werkstudent Vertrieb",
    "Business Analyst",
    "Buchhalter Finanzen",
    "Elektriker Gebäudetechnik",
    "Pflegefachkraft Intensivstation",
    "Lagerist Logistik",
    "Mechatroniker Instandhaltung",
    "Product Owner Payments",
    "UX Designer",
    "Account Manager DACH",
    "Steuerfachangestellte (m/w/d)",
    "Industriemechaniker",
    "Kundenberater Energie",
    "Teamleiter Kundenservice",
    "Controller Konzern",
    "Einkäufer Rohstoffe",
    "Qualitätsprüfer Automotive",
    "Personalreferent",
    "Systemadministrator Linux",
)
BULK_TEXTS: Final[Mapping[str, str]] = {
    "de": "Eine erfundene Testanzeige für ein erfundenes Team in einer erfundenen Stadt.",
    "en": "An invented test advertisement for an invented team in an invented city.",
}
_OTHER_TEXT: Final = "Un texte inventé pour les tests."
_CITIES: Final = (BERLIN, MUNICH, HAMBURG, (50.93333, 6.95))
_BULK_CHAIN: Final = 4_711  # the profile chain number of the seed's tuple; the per-card tests use 1 and 2
_SOURCES: Final = (("jsearch",), ("adzuna",), ("greenhouse",), ("jsearch", "greenhouse"))


@dataclass(frozen=True)
class BulkSeed:
    """What `bulk_seed_cards` made: the card ids (ascending), the scored, the untargeted and the grouped ones
    among them, and the tuple and scope it scored them under."""

    card_ids: tuple[int, ...]
    scored_ids: tuple[int, ...]
    untargeted_ids: tuple[int, ...]
    representative_ids: tuple[int, ...]
    llm_tuple_id: int
    scope_version_id: int
    now: datetime


@dataclass
class _Rows:
    jobs: list[dict[str, JSONValue]] = field(default_factory=list)
    revisions: list[dict[str, JSONValue]] = field(default_factory=list)
    targets: list[dict[str, JSONValue]] = field(default_factory=list)
    published: list[dict[str, JSONValue]] = field(default_factory=list)
    filters: list[dict[str, JSONValue]] = field(default_factory=list)
    actions: list[dict[str, JSONValue]] = field(default_factory=list)


def _set_insert(table: str, columns: Mapping[str, str], constants: Mapping[str, str]) -> TextClause:
    """`INSERT INTO table` of every element of the JSONB array `:rows` (`columns`: name to SQL type) and the
    SQL `constants`, with the ids the rows name (OVERRIDING SYSTEM VALUE)."""
    record = ", ".join(f"{name} {type_}" for name, type_ in columns.items())
    names = ", ".join([*columns, *constants])
    values = ", ".join([*(f"c.{name}" for name in columns), *constants.values()])
    statement = (
        f"INSERT INTO {table} ({names}) OVERRIDING SYSTEM VALUE "  # noqa: S608 (this module's constants)
        f"SELECT {values} FROM jsonb_to_recordset(:rows) AS c({record})"
    )
    return q(statement, jsonb=["rows"])


_IDS: Final = q("SELECT nextval(pg_get_serial_sequence(:table, 'id')) FROM generate_series(1, :k) ORDER BY 1")
_BUDGET: Final = q(
    "INSERT INTO llm_budget (month, cap_usd, overrun_buffer_usd, bucket_usd, bucket_at) "
    "VALUES (:m, 100, 2, 1, now()) ON CONFLICT DO NOTHING"
)
_CONFIG: Final = q(
    "SELECT a.scope_version_id, a.execution_config_id, a.evaluation_policy_id, u.profile_version_id, "
    "u.rubric_version_id, u.prompt_schema_version_id, u.model_policy_id "
    "FROM active_config a JOIN llm_tuples u ON u.id = a.llm_tuple_id WHERE a.id = 1"
)
_BULK_JOBS: Final = _set_insert(
    "jobs",
    {
        "id": "bigint",
        "current_revision_id": "bigint",
        "company_key": "text",
        "company_display": "text",
        "title": "text",
        "title_norm": "text",
        "seniority_tokens": "text[]",
        "locations": "jsonb",
        "location_kind": "text",
        "work_mode_possible": "text[]",
        "work_mode_source": "text",
        "employment_types": "text[]",
        "ad_language": "text",
        "completeness": "text",
        "posted_at_effective": "timestamptz",
        "posted_precision": "text",
        "first_seen_at": "timestamptz",
        "availability": "text",
        "source_ids": "text[]",
    },
    {
        "employment_source": "'none'",
        "last_seen_at": "c.first_seen_at",
        "availability_changed_at": "c.first_seen_at",
        "field_provenance": "'{}'",
        "det_facts_hash": "'\\x00'",
    },
)
_BULK_REVISIONS: Final = _set_insert(
    "job_revisions",
    {
        "id": "bigint",
        "job_id": "bigint",
        "llm_input": "jsonb",
        "description_text": "text",
        "ad_language": "text",
    },
    {
        "revision_no": "1",
        "material_hash": "'\\x00'",
        "description_completeness": "'full'",
        "embedding_status": "'awaiting_embedding'",
    },
)
_BULK_GROUPS: Final = _set_insert(
    "duplicate_groups",
    {"id": "bigint", "representative_job_id": "bigint"},
    {"status": "'active'", "election_reason": "'initial'"},
)
_BULK_GROUP_MEMBERS: Final = q(
    "UPDATE jobs SET duplicate_group_id = c.group_id FROM jsonb_to_recordset(:rows) AS c(job_id bigint, "
    "group_id bigint) WHERE jobs.id = c.job_id",
    jsonb=["rows"],
)
_BULK_TARGETS: Final = _set_insert(
    "score_targets",
    {
        "id": "bigint",
        "job_revision_id": "bigint",
        "job_id": "bigint",
        "state": "text",
        "similarity": "real",
        "deferred_group_id": "bigint",
        "published_result_id": "bigint",
    },
    {"llm_tuple_id": ":tuple"},
)
_BULK_ATTEMPTS: Final = _set_insert(
    "score_attempts",
    {"id": "bigint", "target_id": "bigint"},
    {
        "attempt_no": "1",
        "mode": "'online'",
        "execution_config_id": ":execution_config",
        "evaluation_policy_id": ":evaluation_policy",
        "lane": "'similarity'",
        "outcome": "'published_accepted'",
        "ended_at": "now()",
    },
)
_BULK_LEDGER: Final = _set_insert(
    "llm_spend_ledger",
    {"id": "bigint"},
    {
        "budget_month": ":month",
        "kind": "'request'",
        "purpose": "'generate'",
        "purpose_class": "'scoring'",
        "model_requested": "'claude-sonnet-5-5'",
        "state": "'reserved'",
        "reserved_usd": "0.1",
        "admitted_cap_usd": "100",
        "counted_before_usd": "0",
    },
)
_BULK_CALLS: Final = _set_insert(
    "llm_calls",
    {"id": "bigint", "attempt_id": "bigint", "target_id": "bigint", "spend_ledger_id": "bigint"},
    {
        "purpose": "'generate'",
        "round": "0",
        "logical_call_no": "1",
        "execution_config_id": ":execution_config",
        "model_requested": "'claude-sonnet-5-5'",
        "effort": "'medium'",
        "max_tokens": "6000",
        "transport": "'stream'",
        "prompt_template_version": "'v2'",
        "rendered_prompt_sha256": "'\\x00'",
        "input_refs": "'{}'",
        "input_tokens_bound": "1000",
        "status": "'reserved'",
    },
)
_BULK_DRAFTS: Final = _set_insert(
    "score_drafts",
    {"id": "bigint", "attempt_id": "bigint", "llm_call_id": "bigint"},
    {"round": "0", "output_json": "'{}'", "validation": "'{}'", "provisional": "'{}'"},
)
_BULK_RESULTS: Final = _set_insert(
    "llm_results",
    {
        "id": "bigint",
        "target_id": "bigint",
        "attempt_id": "bigint",
        "final_draft_id": "bigint",
        "job_revision_id": "bigint",
    },
    {
        "profile_version_id": ":profile_version",
        "rubric_version_id": ":rubric_version",
        "prompt_schema_version_id": ":prompt_schema_version",
        "model_policy_id": ":model_policy",
        "evaluation_policy_id": ":evaluation_policy",
        "status": "'accepted'",
        "criteria": "'{}'",
        "job_facts": "'{}'",
        "knockout_candidates": "'{}'",
        "fact_confirmations": "'{}'",
        "not_a_job_ad": "false",
        "injection_suspected": "false",
        "critic_outcome": "'not_run'",
        "revisions_used": "0",
        "summary_en": "'Invented summary.'",
        "summary_de": "'Erfundene Zusammenfassung.'",
    },
)
_BULK_INSTANCES: Final = _set_insert(
    "det_results",
    {
        "llm_result_id": "bigint",
        "job_id": "bigint",
        "score_low": "numeric",
        "score_mid": "numeric",
        "score_high": "numeric",
        "coverage": "numeric",
        "confidence": "text",
    },
    {
        "scope_version_id": ":scope",
        "det_algorithm_version": "'det-test'",
        "det_criteria": "'{}'",
        "knockouts": "'{}'",
        "industry_match": "'neutral'",
        "caps_applied": "'{}'",
        "det_inputs": "'{}'",
        "det_inputs_hash": "'\\x00'",
    },
)
_BULK_FILTERS: Final = q(
    "INSERT INTO hard_filter_results (job_id, scope_version_id, passed, reasons, inputs_hash, "
    "det_algorithm_version) SELECT c.job_id, :scope, c.passed, c.reasons, '\\x00', 'det-test' "
    "FROM jsonb_to_recordset(:rows) AS c(job_id bigint, passed boolean, reasons text[])",
    jsonb=["rows"],
)
_BULK_ACTIONS: Final = q(
    "INSERT INTO user_job_actions (job_id, seen_at, saved_at, hidden_at) "
    "SELECT c.job_id, c.seen_at, c.saved_at, c.hidden_at FROM jsonb_to_recordset(:rows) "
    "AS c(job_id bigint, seen_at timestamptz, saved_at timestamptz, hidden_at timestamptz)",
    jsonb=["rows"],
)


async def _ids(c: AsyncConnection, table: str, k: int) -> list[int]:
    if k == 0:
        return []
    return [int(i) for i in (await c.execute(_IDS, {"table": table, "k": k})).scalars()]


def _job_row(
    i: int, job_id: int, revision_id: int, now: datetime, langs: Sequence[str]
) -> dict[str, JSONValue]:
    """Card number `i`'s job: its title, company, dates, place, work mode and marks vary with `i`."""
    title = normalize_title(BULK_TITLES[i % len(BULK_TITLES)])
    first_seen = now - timedelta(minutes=(43 * i) % 86_400)  # spread over 60 days
    posted = None if i % 10 == 9 else first_seen - timedelta(hours=i % 72)
    remote = i % 9 == 4
    modes = (("remote", "hybrid"), ("onsite",), ("remote", "hybrid", "onsite"))[i % 3]
    employment = (("full_time",), (), ("part_time", "working_student"), ("contract",))[i % 4]
    return {
        "id": job_id,
        "current_revision_id": revision_id,
        "company_key": f"firma-{i % 40:02d}",
        "company_display": f"Firma {i % 40}",
        "title": title.title,
        "title_norm": title.title_norm,
        "seniority_tokens": _texts(sorted(title.seniority_tokens)),
        "locations": [] if remote else [place(_CITIES[i % len(_CITIES)])],
        "location_kind": "remote" if remote else "city",
        "work_mode_possible": _texts(modes),
        "work_mode_source": ("structured", "text", "none")[i % 3],
        "employment_types": _texts(employment),
        "ad_language": langs[i % len(langs)],
        "completeness": "partial" if i % 11 == 0 else "full",
        "posted_at_effective": None if posted is None else posted.isoformat(),
        "posted_precision": "unknown" if posted is None else "day",
        "first_seen_at": first_seen.isoformat(),
        "availability": "stale" if i % 13 == 0 else "unknown" if i % 17 == 0 else "active",
        "source_ids": _texts(_SOURCES[i % len(_SOURCES)]),
    }


def _scores(i: int) -> dict[str, JSONValue]:
    low = Decimal(10 + (7 * i) % 60)
    high = low + 5 + i % 20
    mid = ((low + high) / 2).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return {
        "score_low": str(low),
        "score_mid": str(mid),
        "score_high": str(high),
        "coverage": str(Decimal(50 + i % 50) / 100),
        "confidence": ("high", "medium", "low")[i % 3],
    }


async def bulk_seed_cards(
    engine: AsyncEngine, n: int, *, scored_share: float, groups: int, langs: Sequence[str]
) -> BulkSeed:
    """Commit `n` cards set-based (module docstring)."""
    if not (n >= 1 and 0 <= scored_share <= 1 and 0 <= groups <= n and langs):
        raise ValueError("n >= 1, 0 <= scored_share <= 1, 0 <= groups <= n and at least one language")
    if (await _active_tuple(engine)) is None:
        await activate_tuple(engine, n=_BULK_CHAIN)
    now = utcnow()
    scored = [int((i + 1) * scored_share) > int(i * scored_share) for i in range(n)]
    untargeted = frozenset([i for i in range(n) if not scored[i]][::10])  # every tenth unscored card
    representatives = sorted({(g * n) // groups for g in range(groups)}) if groups else []
    async with engine.begin() as c:
        config = (await c.execute(_CONFIG)).one()
        llm_tuple = int((await c.execute(_ACTIVE)).one().llm_tuple_id)
        scope = int(config.scope_version_id)
        jobs = await _ids(c, "jobs", n + len(representatives))
        revisions = await _ids(c, "job_revisions", n + len(representatives))
        group_ids = await _ids(c, "duplicate_groups", len(representatives))
        k_scored = sum(scored)
        published = {
            table: await _ids(c, table, k_scored)
            for table in ("llm_spend_ledger", "llm_calls", "score_attempts", "score_drafts", "llm_results")
        }
        target_ids = iter(await _ids(c, "score_targets", n - len(untargeted) + len(representatives)))
        rows = _Rows()
        rank = 0
        for i in range(n):
            rows.jobs.append(_job_row(i, jobs[i], revisions[i], now, langs))
            lang = langs[i % len(langs)]
            rows.revisions.append(
                {
                    "id": revisions[i],
                    "job_id": jobs[i],
                    "llm_input": {"title": rows.jobs[-1]["title"]},
                    "description_text": BULK_TEXTS.get(lang, _OTHER_TEXT),
                    "ad_language": lang,
                }
            )
            similarity = None if i % 4 == 0 else ((13 * i) % 200 - 100) / 100
            if scored[i]:
                result = published["llm_results"][rank]
                target = next(target_ids)
                rows.targets.append(_target(target, revisions[i], jobs[i], "published_accepted", similarity))
                rows.targets[-1]["published_result_id"] = result
                rows.published.append(
                    {
                        "target": target,
                        "revision": revisions[i],
                        "job": jobs[i],
                        **{table: ids[rank] for table, ids in published.items()},
                        **_scores(i),
                    }
                )
                rows.filters.append({"job_id": jobs[i], "passed": i % 29 != 0, "reasons": _reasons(i)})
                rank += 1
            elif i not in untargeted:
                rows.targets.append(_target(next(target_ids), revisions[i], jobs[i], "pending", similarity))
            if i % 6 == 0:
                mark = (now - timedelta(days=1)).isoformat()
                rows.actions.append(
                    {
                        "job_id": jobs[i],
                        "seen_at": mark,
                        "saved_at": mark if i % 12 == 0 else None,
                        "hidden_at": mark if i % 31 == 0 else None,
                    }
                )
        members = []
        for g, rep in enumerate(representatives):
            member = n + g
            row = _job_row(rep, jobs[member], revisions[member], now, langs)
            row["locations"] = [place(_CITIES[(rep + 1) % len(_CITIES)])]
            rows.jobs.append(row)
            rows.revisions.append({**rows.revisions[rep], "id": revisions[member], "job_id": jobs[member]})
            deferred = _target(next(target_ids), revisions[member], jobs[member], "deferred_duplicate", 0.5)
            rows.targets.append({**deferred, "deferred_group_id": group_ids[g]})
            members += [
                {"job_id": jobs[rep], "group_id": group_ids[g]},
                {"job_id": jobs[member], "group_id": group_ids[g]},
            ]
        binds = {
            "tuple": llm_tuple,
            "scope": scope,
            "execution_config": int(config.execution_config_id),
            "evaluation_policy": int(config.evaluation_policy_id),
            "profile_version": int(config.profile_version_id),
            "rubric_version": int(config.rubric_version_id),
            "prompt_schema_version": int(config.prompt_schema_version_id),
            "model_policy": int(config.model_policy_id),
            "month": now.date().replace(day=1),
        }
        await c.execute(_BUDGET, {"m": binds["month"]})
        await c.execute(_BULK_JOBS, {"rows": rows.jobs})
        await c.execute(_BULK_REVISIONS, {"rows": rows.revisions})
        if representatives:
            groups_rows = [
                {"id": g, "representative_job_id": jobs[rep]}
                for g, rep in zip(group_ids, representatives, strict=True)
            ]
            await c.execute(_BULK_GROUPS, {"rows": groups_rows})
            await c.execute(_BULK_GROUP_MEMBERS, {"rows": members})
        await c.execute(_BULK_TARGETS, {"rows": rows.targets, **binds})
        if rows.published:
            await _bulk_publish(c, rows.published, binds)
            await c.execute(_BULK_FILTERS, {"rows": rows.filters, **binds})
        if rows.actions:
            await c.execute(_BULK_ACTIONS, {"rows": rows.actions})
    return BulkSeed(
        card_ids=tuple(jobs[:n]),
        scored_ids=tuple(jobs[i] for i in range(n) if scored[i]),
        untargeted_ids=tuple(jobs[i] for i in sorted(untargeted)),
        representative_ids=tuple(jobs[rep] for rep in representatives),
        llm_tuple_id=llm_tuple,
        scope_version_id=scope,
        now=now,
    )


def _target(
    target: int, revision: int, job: int, state: str, similarity: float | None
) -> dict[str, JSONValue]:
    return {
        "id": target,
        "job_revision_id": revision,
        "job_id": job,
        "state": state,
        "similarity": similarity,
        "deferred_group_id": None,
        "published_result_id": None,
    }


def _texts(values: Sequence[str]) -> list[JSONValue]:
    return list(values)


def _reasons(i: int) -> list[JSONValue]:
    return [] if i % 29 != 0 else ["excluded_keyword"]


async def _bulk_publish(
    c: AsyncConnection, published: list[dict[str, JSONValue]], binds: Mapping[str, object]
) -> None:
    """The attempt, ledger row, call, draft, result and current instance of every published card."""
    p = published
    await c.execute(
        _BULK_ATTEMPTS, {"rows": [{"id": r["score_attempts"], "target_id": r["target"]} for r in p], **binds}
    )
    await c.execute(_BULK_LEDGER, {"rows": [{"id": r["llm_spend_ledger"]} for r in p], **binds})
    calls = [
        {
            "id": r["llm_calls"],
            "attempt_id": r["score_attempts"],
            "target_id": r["target"],
            "spend_ledger_id": r["llm_spend_ledger"],
        }
        for r in p
    ]
    await c.execute(_BULK_CALLS, {"rows": calls, **binds})
    drafts = [
        {"id": r["score_drafts"], "attempt_id": r["score_attempts"], "llm_call_id": r["llm_calls"]} for r in p
    ]
    await c.execute(_BULK_DRAFTS, {"rows": drafts, **binds})
    results = [
        {
            "id": r["llm_results"],
            "target_id": r["target"],
            "attempt_id": r["score_attempts"],
            "final_draft_id": r["score_drafts"],
            "job_revision_id": r["revision"],
        }
        for r in p
    ]
    await c.execute(_BULK_RESULTS, {"rows": results, **binds})
    instances = [
        {
            "llm_result_id": r["llm_results"],
            "job_id": r["job"],
            **{k: r[k] for k in ("score_low", "score_mid", "score_high", "coverage", "confidence")},
        }
        for r in p
    ]
    await c.execute(_BULK_INSTANCES, {"rows": instances, **binds})


async def _active_tuple(engine: AsyncEngine) -> int | None:
    async with engine.connect() as c:
        value = (await c.execute(_ACTIVE)).one().llm_tuple_id
    return None if value is None else int(value)
