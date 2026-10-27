"""Scoring test data (M3 Tasks 3 and 6): generator job facts, confirmed profiles and evaluation policies for
the fact-id, knockout, deterministic-criteria and trigger tests. Every value is invented: no real ad, company
or person.

- `job_facts(**overrides)` is a `JobFacts` that asserts nothing (no work mode, no language, no degree, no
  certification, no clearance, no work permit), with the given fields replaced, so a test names only the
  facts it is about;
- `required_language(code, *, level, mandatory=True, evidence=("Sprachkenntnisse",), other=None)` is one
  `required_languages` item (`other` is the `language_other` name of the code `other`);
- `profile(**overrides)` is a `ConfirmedProfile` of an invented senior developer with no language, education,
  certification or clearance and an unspecified work authorisation, with the given fields replaced;
  `language(code, cefr, *, other=None)` is one of its `languages`, `profile_with_language(code, cefr)` a
  profile speaking that language only, and `profile_without(code)` one that speaks English at C1 (German when
  `code` is English) and not `code`;
- `CENTRE` is a scope location (Berlin's centre, radius 20 km) and `km_east(*km)` a resolved location with one
  candidate that many kilometres east of it for each distance (`ambiguous` when there are several), so a
  test sets `x = d / r` directly: `km_east(28)` lies at 1.4 times the radius;
- `rating(level)` is a draft criterion rated `level` (`observed`, or `unknown` for `None`) with no evidence,
  as the aggregation reads it;
- `evaluation_policy(**overrides)` is the seeded evaluation policy (`settings_v1.json`'s `evaluation` block)
  with `t8_sample_pct = 0` unless overridden, so no test's triggers depend on its ids (Controller Ruling
  P48): a test that exercises T8 sets the rate itself.

M3 Task 9 adds the `score_target` subgraph's side:
- `activate_test_policy(conn, t8_sample_pct=0)` makes an evaluation policy with that rate (the seeded
  thresholds otherwise) the active one, so the claims below pin it (Controller Ruling P48);
- `insert_target(engine, ad=…)` is a `pending` target of a new job whose current revision is the synthetic
  ad (`tests.support.validation.synthetic_ad`: its material projection `llm_input` and its description),
  over the score tuple (`score_llm_tuple`: `tests.support.llm.score_profile` with the active rubric,
  prompt-schema version and model policy); `claimed_target(engine, ad=…)` claims it (`claim_again`);
- `reclaim_now(engine, target_id)` is the §9.4.3 reclaim (L3572) through `transition_where`: the target
  `in_progress → pending` with its claim cleared, its running attempt `claim_lost`; `claim_again(engine,
  target_id)` is the atomic claim (Task 2's statement, `tests.support.m3.CLAIM_WHERE`) of the pending target
  with a fresh attempt pinning the active execution config and evaluation policy;
- `attempt_of(engine, attempt_id)` reads an attempt's counters and outcome;
- `score_deps(engine, gateway)` are the subgraph's deps on the gateway's clock; `invoke_subgraph(deps,
  claimed)` runs one attempt with a `ScoreContext` of a no-op claim tracker (`NoClaims`), once
  `stub_terminals(monkeypatch)` replaced `publish` and `fail` (Task 10's) with nodes that end the attempt
  with `outcome` set to `publish(<mode>)` or `fail(<error_code>)`;
- `synthetic_c_draft(levels=…, facts=…)` is a candidate C generator answer whose quotes ground in the
  synthetic ad (every criterion level 3 except `experience_seniority` 4, the deterministic level of the ad's
  senior title for `score_profile`), and `synthetic_review(*reviews, verdict=…, injection=…)` with
  `review_item(target, assessment, *quotes, suggested_level=None)` a critic answer;
- `Walk`, `Trace` and `route(walk)` walk the routing of §9.4.2 (`scoring.routing`) with scripted step
  outcomes, from a fresh start or a resumed one (`Resume`), and count the logical calls and HTTP tries a real
  attempt would count (a step denied before its dispatch, by the budget or a lost claim, counts neither,
  L3567).

M3 Task 10 adds the terminals' side: `target_of` (a target's state, reason, claim columns, published result,
backoff and charges), `attempt_end` (an attempt's outcome, detail and end), `count(engine, table)` of the
result tables, `score_config(deps, claimed, claims)` (the invoke config `invoke_subgraph` uses, with a claim
tracker), `state_at_terminal(deps, claimed, monkeypatch)` (the state with which an attempt reaches `publish`
or `fail`, run with the stub terminals, so a test calls a real terminal node on it) and `ended_with(command)`
(the `outcome` a terminal node's `Command` sets).

M3 Task 11 adds the cycle's side:
- `activate_score_tuple(conn)` makes the score tuple the active one (a cycle starts only under an active
  tuple), and `insert_targets(engine, n, *, lane=None)` makes `n` pending targets of `n` new jobs
  (`insert_target`) under it, each with `lane` when given (`score_now`: what the API's Score now leaves);
- `cycle_runner(engine, gateway, *, sleep=asyncio.sleep, epoch=1)`: the process side of score cycles for a
  test, `CycleRunner`: the deps on `gateway` with the test evaluation policy active (`score_deps`, so
  `t8_sample_pct = 0` unless a test exercises T8, Ruling P48) and an in-memory checkpoint saver, the graphs
  compiled once (`graph.build_score_graphs`), `start(trigger)` (`cycles.start_cycle` as worker `epoch`, in
  fake LLM mode) and `run(cycle_id)` (`graph.invoke_cycle` with a fresh `ClaimHeartbeats` on the injected
  `sleep`, kept as `claims`), so a test drives the claim beats with a `ManualSleep`;
- `cycle_of(engine, cycle_id)`: a cycle's status, reason, trigger, admission, `ĉ`, bucket, spend and report.

M3 Task 12 adds the admission's side:
- `candidate_targets(engine, *specs)`: one pending target of a new job per `TargetSpec` (its similarity, the
  job's title, the completeness of the job and of its revision, the lane, the charged attempts, the backoff
  and the creation time) over the score tuple made active, in order; a spec's default title matches none of
  `score_profile`'s target titles, so a target is always-include only when a test makes it so;
- `lane_credits(engine, llm_tuple_id)`: the stored `admission_lane_credits` of the tuple, by lane;
- `list_on_watchlist(conn, entries)`: a new settings version, copied from the active one and made active,
  whose `watchlist[]` names each company id of `entries` with its `enabled` (Controller Ruling T12-b: a
  watched company is enabled in the table and in the settings).
"""

import asyncio
import dataclasses
import hashlib
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, cast

import pytest
from jobhunter.db.seed import canonical_json, load_settings_v1
from jobhunter.db.sql import q
from jobhunter.db.transitions import transition, transition_where
from jobhunter.graphs.score import graph as score_graph
from jobhunter.graphs.score import subgraph
from jobhunter.graphs.score.claims import ClaimHeartbeats
from jobhunter.graphs.score.context import ScoreContext
from jobhunter.graphs.score.deps import ScoreDeps
from jobhunter.graphs.score.graph import ScoreGraphs
from jobhunter.graphs.score.nodes import Node
from jobhunter.graphs.score.state import ClaimRef, CycleState, TargetState, initial_state
from jobhunter.llm.gateway import LLMGateway
from jobhunter.llm.schemas.generator import DraftCriterion, JobFacts, RequiredLanguage
from jobhunter.llm.schemas.profile import ConfirmedProfile, Language
from jobhunter.normalize.titles import normalize_title
from jobhunter.run_settings import ScopeLocation
from jobhunter.scoring import cycles, routing
from jobhunter.scoring.cycles import CycleTrigger
from jobhunter.scoring.evaluation_policy import EvaluationPolicy
from jobhunter.scoring.routing import DerivedOutcome, Route
from jobhunter.sources.parsing.locations import Candidate, Location
from jobhunter.types import JSONValue, get_list, get_object
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.llm import score_profile
from tests.support.m2 import insert_profile_chain
from tests.support.m3 import CLAIM_FROM, CLAIM_RETURNING, CLAIM_SET_SQL, CLAIM_WHERE, Job
from tests.support.validation import SyntheticAd, criterion_item, synthetic_ad, synthetic_wire_c

_NOTHING_ASSERTED: Final[dict[str, Any]] = {
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


def job_facts(**overrides: Any) -> JobFacts:
    """`JobFacts` asserting nothing, with the given fields (models or their dicts) replaced."""
    return JobFacts.model_validate({**_NOTHING_ASSERTED, **overrides})


def required_language(
    code: str,
    *,
    level: str,
    mandatory: bool = True,
    evidence: Sequence[str] = ("Sprachkenntnisse",),
    other: str | None = None,
) -> RequiredLanguage:
    return RequiredLanguage.model_validate(
        {
            "language": code,
            "language_other": other,
            "level": level,
            "mandatory": mandatory,
            "evidence": list(evidence),
        }
    )


_PROFILE: Final[dict[str, Any]] = {
    "summary": "Erfundener Backend-Entwickler",
    "target_titles": [{"title": "Backend Developer", "lang": "en"}],
    "seniority": "senior",
    "total_years_experience": 6.0,
    "roles": [],
    "skills": [],
    "languages": [],
    "education": [],
    "certifications": [],
    "domains": [],
}


def profile(**overrides: Any) -> ConfirmedProfile:
    """The invented profile with the given fields (models or their dicts) replaced."""
    return ConfirmedProfile.model_validate({**_PROFILE, **overrides})


def language(code: str, cefr: str, *, other: str | None = None) -> Language:
    return Language.model_validate({"code": code, "code_other": other, "cefr": cefr, "evidence": "Sprachen"})


def profile_with_language(code: str, cefr: str) -> ConfirmedProfile:
    return profile(languages=[language(code, cefr)])


def profile_without(code: str) -> ConfirmedProfile:
    return profile(languages=[language("de" if code == "en" else "en", "C1")])


CENTRE: Final = ScopeLocation("Berlin", "geonames:2950159", 52.52, 13.405, 20, "berlin")
_KM_PER_DEGREE: Final = 6371.0088 * math.pi / 180  # the gazetteer's haversine radius


def _east(km: float) -> Candidate:
    degrees = km / (_KM_PER_DEGREE * math.cos(math.radians(CENTRE.lat)))
    return Candidate(gazetteer_id=f"test:{km}", country_code="DE", lat=CENTRE.lat, lon=CENTRE.lon + degrees)


def km_east(*km: float) -> Location:
    """A resolved location with one candidate `km` east of `CENTRE` per distance given."""
    return Location(
        label="Erfundenes Dorf",
        resolution="unique" if len(km) == 1 else "ambiguous",
        candidates=[_east(distance) for distance in km],
        postal_code=None,
        source="test",
    )


def evaluation_policy(**overrides: JSONValue) -> EvaluationPolicy:
    """The seeded evaluation policy with `t8_sample_pct = 0` and the given payload fields replaced."""
    seeded = get_object(load_settings_v1(), "evaluation")
    return EvaluationPolicy.from_payload({**seeded, "t8_sample_pct": 0, **overrides})


def rating(level: int | None) -> DraftCriterion:
    """A criterion rated `level`, `observed` (or `unknown` for `None`), with no evidence."""
    basis = "unknown" if level is None else "observed"
    return DraftCriterion.model_validate(
        {"level": level, "basis": basis, "ad_evidence": [], "profile_evidence": [], "rationale": ""}
    )


# --- M3 Task 9: the score_target subgraph -------------------------------------------------------------------

_SCORE_CHAIN: Final = 90  # the chain number of the score tuple's profile version
_POLICY_ROW: Final = q(
    "INSERT INTO evaluation_policies (version_no, policy_hash, payload, projected_critic_rate) "
    "SELECT max(version_no) + 1, :hash, :payload, 0.3 FROM evaluation_policies "
    "ON CONFLICT (policy_hash) DO NOTHING",
    jsonb=["payload"],
)
_POLICY_ID: Final = q("SELECT id FROM evaluation_policies WHERE policy_hash = :hash")
_ACTIVATE_POLICY: Final = q("UPDATE active_config SET evaluation_policy_id = :p WHERE id = 1")


async def activate_test_policy(conn: AsyncConnection, **overrides: JSONValue) -> int:
    """An evaluation policy of the seeded thresholds with `t8_sample_pct = 0` and `overrides`, made active
    (the reset restores the seeded pointer); its id."""
    payload: dict[str, JSONValue] = {
        **get_object(load_settings_v1(), "evaluation"),
        "t8_sample_pct": 0,
        **overrides,
    }
    digest = hashlib.sha256(canonical_json(payload)).digest()
    await conn.execute(_POLICY_ROW, {"hash": digest, "payload": payload})
    policy_id = int((await conn.execute(_POLICY_ID, {"hash": digest})).scalar_one())
    await conn.execute(_ACTIVATE_POLICY, {"p": policy_id})
    return policy_id


_SCORE_TUPLE: Final = q(
    "SELECT t.id FROM llm_tuples t JOIN active_config a ON a.rubric_version_id = t.rubric_version_id "
    "AND a.prompt_schema_version_id = t.prompt_schema_version_id AND a.model_policy_id = t.model_policy_id "
    "ORDER BY t.id LIMIT 1"
)
_INSERT_TUPLE: Final = q(
    "INSERT INTO llm_tuples (profile_version_id, rubric_version_id, prompt_schema_version_id, "
    "model_policy_id) SELECT :pv, rubric_version_id, prompt_schema_version_id, model_policy_id "
    "FROM active_config WHERE id = 1 RETURNING id"
)


async def score_llm_tuple(conn: AsyncConnection) -> int:
    """The tuple of the score tests: `score_profile` with the active rubric, prompt-schema version and model
    policy (made once per test)."""
    found = (await conn.execute(_SCORE_TUPLE)).scalar_one_or_none()
    if found is not None:
        return int(found)
    chain = await insert_profile_chain(conn, _SCORE_CHAIN, profile=score_profile().model_dump(mode="json"))
    return int((await conn.execute(_INSERT_TUPLE, {"pv": chain.profile_version_id})).scalar_one())


_SCORE_JOB: Final = q(
    "INSERT INTO jobs (company_key, company_display, title, title_norm, seniority_tokens, location_kind, "
    "work_mode_possible, work_mode_source, employment_types, employment_source, ad_language, completeness, "
    "posted_precision, first_seen_at, last_seen_at, availability, availability_changed_at, source_ids, "
    "field_provenance, det_facts_hash) "
    "VALUES ('beispiel logistik', :company, :title, :title_norm, CAST(:tokens AS text[]), 'unknown', "
    "CAST(:modes AS text[]), :mode_source, CAST(:types AS text[]), :type_source, 'de', :completeness, "
    "'unknown', now(), now(), 'active', now(), ARRAY['jsearch'], '{}', '\\x00') RETURNING id"
)
_SCORE_REVISION: Final = q(
    "INSERT INTO job_revisions (job_id, revision_no, material_hash, llm_input, description_text, "
    "description_completeness, ad_language, embedding_status) "
    "VALUES (:job, 1, :hash, :llm_input, :description, :completeness, 'de', 'awaiting_embedding') "
    "RETURNING id",
    jsonb=["llm_input"],
)
_CURRENT: Final = q("UPDATE jobs SET current_revision_id = :r WHERE id = :j")


def _source(pairs: tuple[tuple[str, str], ...]) -> str:
    return pairs[0][1] if pairs else "none"


async def insert_score_job(conn: AsyncConnection, ad: SyntheticAd) -> Job:
    """A job whose current revision is `ad`: its material projection and description (the job's det facts
    agree with it, with no location)."""
    title = normalize_title(ad.title)
    modes = [mode for mode, _ in ad.work_modes] or ["remote", "hybrid", "onsite"]
    types = [kind for kind, _ in ad.employment_types]
    job = int(
        (
            await conn.execute(
                _SCORE_JOB,
                {
                    "company": ad.company or "",
                    "title": title.title,
                    "title_norm": title.title_norm,
                    "tokens": sorted(title.seniority_tokens),
                    "modes": modes,
                    "mode_source": _source(ad.work_modes),
                    "types": types,
                    "type_source": _source(ad.employment_types),
                    "completeness": ad.completeness,
                },
            )
        ).scalar_one()
    )
    llm_input: dict[str, JSONValue] = {
        "title": ad.title,
        "company_display": ad.company,
        "locations": list(ad.locations),
        "work_mode_possible": list(modes),
        "work_mode_source": _source(ad.work_modes),
        "employment_types": list(types),
        "employment_source": _source(ad.employment_types),
        "ad_language": "de",
        "description_completeness": ad.completeness,
        "description_text": ad.description_text,
    }
    params = {
        "job": job,
        "hash": hashlib.sha256(canonical_json(llm_input)).digest(),
        "llm_input": llm_input,
        "description": ad.description_text,
        "completeness": ad.completeness,
    }
    revision = int((await conn.execute(_SCORE_REVISION, params)).scalar_one())
    await conn.execute(_CURRENT, {"r": revision, "j": job})
    return Job(job, revision)


@dataclass(frozen=True)
class ClaimedTarget:
    """A claimed target and its running attempt, with its revision (the fake's answer key) and tuple."""

    target_id: int
    attempt_id: int
    claim_id: str
    attempt_no: int
    job_id: int
    job_revision_id: int
    llm_tuple_id: int

    @property
    def ref(self) -> ClaimRef:
        return {"target_id": self.target_id, "claim_id": self.claim_id, "attempt_no": self.attempt_no}


@dataclass(frozen=True)
class PendingTarget:
    target_id: int
    job_id: int
    job_revision_id: int
    llm_tuple_id: int


_PENDING: Final = q(
    "INSERT INTO score_targets (job_revision_id, llm_tuple_id, job_id, state, bulk_operation_id) "
    "VALUES (:r, :tuple, :j, 'pending', :bulk) RETURNING id"
)


async def insert_target(
    engine: AsyncEngine, *, ad: SyntheticAd | None = None, bulk_operation_id: int | None = None
) -> PendingTarget:
    """A `pending` target of a new job whose revision is `ad` (`synthetic_ad()` by default), over the score
    tuple; the test policy is made active first."""
    async with engine.begin() as conn:
        await activate_test_policy(conn)
        tuple_id = await score_llm_tuple(conn)
        job = await insert_score_job(conn, synthetic_ad() if ad is None else ad)
        params = {"r": job.revision_id, "tuple": tuple_id, "j": job.job_id, "bulk": bulk_operation_id}
        target = int((await conn.execute(_PENDING, params)).scalar_one())
    return PendingTarget(target, job.job_id, job.revision_id, tuple_id)


_TARGET_IDS: Final = q("SELECT job_id, job_revision_id, llm_tuple_id FROM score_targets WHERE id = :t")
# The claim node returns the claimed target's resume draft as well (Task 11), so the statement here is its.
_CLAIM_AGAIN_RETURNING: Final = (*CLAIM_RETURNING, "resume_draft_id")
_NEW_ATTEMPT: Final = q(
    "INSERT INTO score_attempts (target_id, attempt_no, mode, claim_id, claim_epoch, execution_config_id, "
    "evaluation_policy_id, lane, outcome, resumed_from_attempt_id) "
    "SELECT :t, :a, 'online', :c, 1, execution_config_id, evaluation_policy_id, 'similarity', 'running', "
    ":resumed FROM active_config WHERE id = 1 RETURNING id"
)


async def claim_again(
    engine: AsyncEngine, target_id: int, *, resumed_from_attempt_id: int | None = None
) -> ClaimedTarget:
    """The atomic claim of the `pending` target (module notes) and its fresh `running` attempt, which pins
    the active execution config and evaluation policy (and names `resumed_from_attempt_id`, the link Task
    11's claim writes for a resumed attempt)."""
    async with engine.begin() as conn:
        rows = await transition_where(
            conn,
            "score_targets",
            ["pending"],
            "in_progress",
            where=CLAIM_WHERE,
            from_sql=CLAIM_FROM,
            params={
                "ids": [target_id],
                "lanes": ["similarity"],
                "reasons": [None],
                "epoch": 1,
                "max_attempts": 4,
            },
            set_sql=CLAIM_SET_SQL,
            returning=_CLAIM_AGAIN_RETURNING,
        )
        (claim,) = rows
        params = {
            "t": target_id,
            "a": claim["attempt_no"],
            "c": claim["claim_id"],
            "resumed": resumed_from_attempt_id,
        }
        attempt = int((await conn.execute(_NEW_ATTEMPT, params)).scalar_one())
        ids = (await conn.execute(_TARGET_IDS, {"t": target_id})).one()
    return ClaimedTarget(
        target_id=target_id,
        attempt_id=attempt,
        claim_id=str(claim["claim_id"]),
        attempt_no=int(claim["attempt_no"]),
        job_id=int(ids.job_id),
        job_revision_id=int(ids.job_revision_id),
        llm_tuple_id=int(ids.llm_tuple_id),
    )


async def claimed_target(
    engine: AsyncEngine, *, ad: SyntheticAd | None = None, bulk_operation_id: int | None = None
) -> ClaimedTarget:
    """`insert_target`, claimed (`claim_again`): attempt 1 pins the test policy (`t8_sample_pct = 0`)."""
    pending = await insert_target(engine, ad=ad, bulk_operation_id=bulk_operation_id)
    return await claim_again(engine, pending.target_id)


_RUNNING_ATTEMPT: Final = q(
    "SELECT id FROM score_attempts WHERE target_id = :t AND outcome = 'running' ORDER BY id DESC LIMIT 1"
)


async def reclaim_now(engine: AsyncEngine, target_id: int) -> None:
    """The reclaim of L3572 (module notes): the target back to `pending` with its claim cleared, its running
    attempt `claim_lost`."""
    async with engine.begin() as conn:
        moved = await transition_where(
            conn,
            "score_targets",
            ["in_progress"],
            "pending",
            where="t.id IN (SELECT id FROM score_targets WHERE id = :t FOR UPDATE)",
            params={"t": target_id},
        )
        assert len(moved) == 1, "the target was not in_progress"
        attempt = (await conn.execute(_RUNNING_ATTEMPT, {"t": target_id})).scalar_one()
        await transition(
            conn, "score_attempts", int(attempt), ["running"], "claim_lost", reason="stale_heartbeat"
        )


_ATTEMPT_ROW: Final = q(
    "SELECT logical_calls, http_calls, charged, cost_usd, outcome FROM score_attempts WHERE id = :a"
)


async def attempt_of(engine: AsyncEngine, attempt_id: int) -> Row[Any]:
    """The attempt's `logical_calls`, `http_calls`, `charged`, `cost_usd` and `outcome`."""
    async with engine.connect() as conn:
        return (await conn.execute(_ATTEMPT_ROW, {"a": attempt_id})).one()


class NoClaims:
    """A claim tracker that tracks nothing (the subgraph reads none of it; `claims.ClaimHeartbeats` is the
    cycle's)."""

    def start(self, ref: ClaimRef) -> None:
        pass

    def attach(self, ref: ClaimRef, child: "asyncio.Task[object]") -> None:
        pass

    def lost(self, ref: ClaimRef) -> bool:
        return False

    async def stop(self, ref: ClaimRef) -> None:
        pass

    async def stop_all(self) -> None:
        pass


async def score_deps(engine: AsyncEngine, gateway: LLMGateway) -> ScoreDeps:
    """The subgraph's deps on `gateway` (its clock is every call's deadline clock), with the test policy made
    active (`activate_test_policy`)."""
    async with engine.begin() as conn:
        await activate_test_policy(conn)
    return ScoreDeps(
        engine=engine,
        gateway=gateway,
        clock=lambda: datetime.now(UTC),
        monotonic=gateway.monotonic,
        saver=None,
    )


def _terminal(name: str) -> Callable[[ScoreDeps], Node]:
    def make(deps: ScoreDeps) -> Node:
        async def node(state: TargetState, config: RunnableConfig) -> Command[Route]:
            if name == "fail":
                outcome = f"fail({state['error_code']})"
            elif state["outcome"] == "already_published":
                outcome = "publish(already_published)"
            else:
                outcome = f"publish({state['publish_mode']})"
            return Command(goto=routing.END, update={"outcome": outcome})

        return node

    return make


def stub_terminals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace Task 10's `publish` and `fail` with nodes that end the attempt with `outcome` set to
    `publish(<mode>)` or `fail(<error_code>)`."""
    monkeypatch.setattr(subgraph, "make_publish", _terminal("publish"))
    monkeypatch.setattr(subgraph, "make_fail", _terminal("fail"))


_ACTIVE_CYCLE_IDS: Final = q("SELECT evaluation_policy_id, scope_version_id FROM active_config WHERE id = 1")


async def invoke_subgraph(deps: ScoreDeps, claimed: ClaimedTarget) -> TargetState:
    """One attempt of `claimed` through the subgraph; its final state."""
    config = await score_config(deps, claimed)
    graph = subgraph.build_score_target(deps)
    return cast(TargetState, await graph.ainvoke(initial_state(claimed.ref, claimed.attempt_id), config))


_LEVELS: Final[Mapping[str, int | None]] = {
    "role_fit": 3,
    "skills": 3,
    "experience_seniority": 4,
    "domain": 3,
    "languages": 3,
    "education_certifications": 3,
}


def synthetic_c_draft(
    *, levels: Mapping[str, int | None] | None = None, facts: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """A candidate C answer for the synthetic ad (module notes): `levels` replace criterion levels (`None`:
    `unknown`, without evidence) and `facts` the facts."""
    items = []
    for key, level in {**_LEVELS, **(levels or {})}.items():
        item = criterion_item(key, level=level, basis="unknown" if level is None else "observed")
        if level is None:
            item |= {"ad_evidence": [], "profile_evidence": []}
        items.append(item)
    return synthetic_wire_c(facts=facts, criteria=items)


def review_item(
    target: str, assessment: str, *quotes: str, suggested_level: int | None = None
) -> dict[str, Any]:
    return {
        "target": target,
        "assessment": assessment,
        "suggested_level": suggested_level,
        "ad_evidence": list(quotes),
        "explanation": "Synthetic explanation.",
    }


def synthetic_review(
    *reviews: dict[str, Any], verdict: str = "accept", injection: str = "none"
) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "reviews": list(reviews),
        "injection": injection,
        "summary": "Synthetic review.",
    }


# ---- the routing walk ------------------------------------------------------------------------------------

# The outcomes of a step denied before its dispatch: neither a logical call nor an HTTP try (L3567).
BEFORE_DISPATCH: Final = frozenset(
    {"budget_exhausted", "claim_lost", "llm_not_configured", "input_unbounded"}
)
_REVIEWS: Final[Mapping[str, DerivedOutcome]] = {
    "accept": "accept",
    "revise": "revise",
    "incomplete": "incomplete",
}
_MAX_STEPS: Final = 50


@dataclass(frozen=True)
class Resume:
    """Where a resumed attempt starts (`load_context` restored it): the current draft's `round`, the chain's
    `critic_rounds`, and the derived outcome of the current draft's own review (`None`: none reviewed
    it)."""

    round: int
    critic_rounds: int
    review: DerivedOutcome | None = None


@dataclass(frozen=True)
class Walk:
    """The scripted outcomes of one attempt. `generate` is its calls' outcomes, a tuple or one string of
    them joined by `_then_` (`"invalid_then_ok"`); `critic` one per critic call (`accept`, `revise`,
    `incomplete`, `invalid` or an error code), `revise` one per reviser call (`ok`, `invalid` or an error
    code); `triggers` and `failures` are each validated draft's, in order (the last repeats); `tries` the
    HTTP tries of each dispatched logical call, in order (the last repeats); `resume` a resumed start (the
    restored draft is the first validated one)."""

    generate: str | tuple[str, ...] = "ok"
    critic: tuple[str, ...] = ()
    revise: tuple[str, ...] = ()
    triggers: tuple[tuple[str, ...], ...] = (("T5",), ())
    failures: tuple[tuple[str, ...], ...] = ((),)
    tries: tuple[int, ...] = (1,)
    resume: Resume | None = None


@dataclass(frozen=True)
class Trace:
    """The steps of a walk (a repair named `repair`, the end `publish` or `fail`), the logical calls and HTTP
    tries it made, how it ended (`publish(<mode>)`, `fail(<code>)` or `end`), its rounds and critic rounds,
    and the last review the routing went by (a critic round's or the restored one), with the failures of the
    draft it reviewed."""

    steps: tuple[str, ...]
    logical_calls: int
    http_calls: int
    ends_with: str
    round: int
    critic_rounds: int
    last_review: DerivedOutcome | None = None
    reviewed_failures: tuple[str, ...] = ()


def _nth[T](items: Sequence[T], n: int) -> T:
    return items[min(n, len(items) - 1)]


def route(walk: Walk) -> Trace:
    """Walk `walk` through `scoring.routing` from `load_context`, fresh or resumed (module notes)."""
    generate = walk.generate.split("_then_") if isinstance(walk.generate, str) else list(walk.generate)
    scripts = {"generate": list(generate), "critic": list(walk.critic), "revise": list(walk.revise)}
    steps: list[str] = []
    logical = http = drafts = round = critic_rounds = 0
    restored: DerivedOutcome | None = None
    if walk.resume is not None:  # the restored draft is the first one validated
        drafts, round, critic_rounds = 1, walk.resume.round, walk.resume.critic_rounds
        restored = walk.resume.review
    last_review: DerivedOutcome | None = None
    reviewed_failures: tuple[str, ...] = ()
    repair_used = False
    failures: tuple[str, ...] = ()
    step = routing.after_load_context(outcome=None, error_code=None, resumed=walk.resume is not None)
    while step.route not in ("publish", "fail", routing.END):
        if len(steps) > _MAX_STEPS:
            raise AssertionError(f"the routing does not end: {steps}")
        name = step.route
        steps.append("repair" if step.repair else name)
        repair_used = repair_used or step.repair
        if name == "validate":
            failures = _nth(walk.failures, drafts - 1)
            if restored is not None:
                last_review, reviewed_failures = restored, failures
            step = routing.after_validate(
                triggers=_nth(walk.triggers, drafts - 1),
                det_failures=failures,
                critic_rounds=critic_rounds,
                round=round,
                review=restored,
            )
            restored = None  # only the restored draft has its review already
            continue
        if not scripts[name]:
            raise AssertionError(f"the walk scripts no outcome for {name} call {len(steps)}")
        outcome = scripts[name].pop(0)
        if outcome not in BEFORE_DISPATCH:
            http += _nth(walk.tries, logical)
            logical += 1
        if name == "critic" and (outcome in _REVIEWS or outcome == "invalid"):
            review = _REVIEWS.get(outcome)
            critic_rounds += review is not None
            if review is not None:
                last_review, reviewed_failures = review, failures
            step = routing.after_critic(
                review=review,
                det_failures=failures,
                round=round,
                repair_used=repair_used,
            )
        elif name in ("generate", "revise") and outcome in ("ok", "invalid"):
            stored = outcome == "ok"
            drafts += stored
            round += stored and name == "revise"
            step = routing.after_generation(name, stored=stored, repair_used=repair_used)
        else:
            step = routing.fail(outcome)
    if step.route == "publish":
        steps.append("publish")
        ends_with = f"publish({step.publish_mode})"
    elif step.route == "fail":
        steps.append("fail")
        ends_with = f"fail({step.error_code})"
    else:
        ends_with = "end"
    return Trace(tuple(steps), logical, http, ends_with, round, critic_rounds, last_review, reviewed_failures)


# ---- M3 Task 10: the publish and fail terminals ----------------------------------------------------------

_TARGET_ROW: Final = q(
    "SELECT state, state_reason, attempt_no, claim_id, claim_epoch, claimed_at, claim_heartbeat_at, "
    "published_result_id, not_before, transient_failures, charged_attempts, refusal_category "
    "FROM score_targets WHERE id = :t"
)
_ATTEMPT_END: Final = q("SELECT outcome, outcome_detail, ended_at FROM score_attempts WHERE id = :a")
# The tables `count` counts (a fixed set: the name is never a caller string in the SQL).
COUNTED: Final = frozenset(
    {"llm_results", "result_evidence", "det_results", "score_drafts", "critic_notes", "alerts", "llm_calls"}
)


async def target_of(engine: AsyncEngine, target_id: int) -> Row[Any]:
    """The target's state, reason, claim columns, published result, backoff and charge counters."""
    async with engine.connect() as conn:
        return (await conn.execute(_TARGET_ROW, {"t": target_id})).one()


async def attempt_end(engine: AsyncEngine, attempt_id: int) -> Row[Any]:
    """The attempt's `outcome`, `outcome_detail` and `ended_at`."""
    async with engine.connect() as conn:
        return (await conn.execute(_ATTEMPT_END, {"a": attempt_id})).one()


async def count(engine: AsyncEngine, table: str) -> int:
    """The number of rows of `table`, one of `COUNTED`."""
    if table not in COUNTED:
        raise ValueError(f"count does not count {table}")
    async with engine.connect() as conn:
        return int((await conn.execute(q(f"SELECT count(*) FROM {table}"))).scalar_one())  # noqa: S608


async def score_config(deps: ScoreDeps, claimed: ClaimedTarget, claims: Any = None) -> RunnableConfig:
    """The invoke config of one attempt of `claimed`: a `ScoreContext` with `claims` (`NoClaims` by
    default)."""
    async with deps.engine.connect() as conn:
        policy = (await conn.execute(_ACTIVE_CYCLE_IDS)).one()
    cycle = ScoreContext(
        cycle_id=0,
        worker_epoch=1,
        llm_tuple_id=claimed.llm_tuple_id,
        scope_version_id=int(policy.scope_version_id),
        evaluation_policy_id=int(policy.evaluation_policy_id),
        deps=deps,
        claims=NoClaims() if claims is None else claims,
    )
    return {"configurable": {"cycle": cycle}, "recursion_limit": 50}


async def state_at_terminal(
    deps: ScoreDeps, claimed: ClaimedTarget, monkeypatch: pytest.MonkeyPatch
) -> TargetState:
    """The state with which the attempt of `claimed` reaches `publish` or `fail` (run with the stub
    terminals of Task 9, which only name where it went), its `outcome` back to what the routing left."""
    with monkeypatch.context() as patched:
        stub_terminals(patched)
        final = await invoke_subgraph(deps, claimed)
    reached = final["outcome"] or ""
    final["outcome"] = "already_published" if reached == "publish(already_published)" else None
    return final


def ended_with(command: Command[Any]) -> str:
    """The `outcome` a terminal node's `Command` sets."""
    update = command.update
    assert isinstance(update, dict), "a terminal node updates the state"
    return str(update["outcome"])


# ---- M3 Task 11: the score cycle -------------------------------------------------------------------------

_SET_LANE: Final = q("UPDATE score_targets SET lane = :lane WHERE id = :t")
_ACTIVATE_TUPLE: Final = q(
    "UPDATE active_config SET profile_version_id = u.profile_version_id, llm_tuple_id = u.id, "
    "updated_at = now() FROM llm_tuples u WHERE active_config.id = 1 AND u.id = :t"
)


async def activate_score_tuple(conn: AsyncConnection) -> int:
    """The score tuple (`score_llm_tuple`) made the active one, its profile version with it (the reset
    restores no tuple); its id."""
    tuple_id = await score_llm_tuple(conn)
    await conn.execute(_ACTIVATE_TUPLE, {"t": tuple_id})
    return tuple_id


async def insert_targets(engine: AsyncEngine, n: int, *, lane: str | None = None) -> list[PendingTarget]:
    """`n` pending targets of `n` new jobs (`insert_target`), each with `lane` when given, over the score
    tuple made active (`activate_score_tuple`)."""
    made = [await insert_target(engine) for _ in range(n)]
    async with engine.begin() as conn:
        await activate_score_tuple(conn)
        if lane is not None:
            for target in made:
                await conn.execute(_SET_LANE, {"lane": lane, "t": target.target_id})
    return made


@dataclass
class CycleRunner:
    """The process side of score cycles for a test (module notes)."""

    deps: ScoreDeps
    graphs: ScoreGraphs
    sleep: Callable[[float], Awaitable[None]]
    epoch: int = 1
    claims: ClaimHeartbeats | None = None

    async def start(self, trigger: CycleTrigger = "manual", *, fake_llm: bool = True) -> int:
        """A new cycle of `trigger` (`cycles.start_cycle` as this runner's epoch); one must start."""
        async with self.deps.engine.begin() as conn:
            cycle_id = await cycles.start_cycle(
                conn, trigger=trigger, now=self.deps.clock(), epoch=self.epoch, fake_llm=fake_llm
            )
        assert cycle_id is not None, f"no {trigger} cycle started"
        return cycle_id

    async def context(self, cycle_id: int) -> ScoreContext:
        """The cycle's `ScoreContext` (`graph.cycle_context`), with a fresh `ClaimHeartbeats` on the
        injected sleep (`claims`)."""
        self.claims = ClaimHeartbeats(self.deps.engine, monotonic=self.deps.monotonic, sleep=self.sleep)
        return await score_graph.cycle_context(self.deps, cycle_id, claims=self.claims)

    async def run(self, cycle_id: int) -> CycleState:
        """The cycle's graph invocation (`graph.invoke_cycle`); its final state."""
        return await score_graph.invoke_cycle(self.graphs.cycle, await self.context(cycle_id))


async def cycle_runner(
    engine: AsyncEngine,
    gateway: LLMGateway,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    epoch: int = 1,
) -> CycleRunner:
    """A `CycleRunner` on `gateway` (module notes)."""
    deps = dataclasses.replace(await score_deps(engine, gateway), saver=InMemorySaver())
    return CycleRunner(deps=deps, graphs=score_graph.build_score_graphs(deps), sleep=sleep, epoch=epoch)


_CYCLE_ROW: Final = q(
    "SELECT status, status_reason, trigger, worker_epoch, admission_size, admitted, c_hat_usd, "
    "bucket_usd_at_start, spent_usd, report, finished_at FROM score_cycles WHERE id = :c"
)


async def cycle_of(engine: AsyncEngine, cycle_id: int) -> Row[Any]:
    """The cycle's status, reason, trigger, epoch, admission (`S` and `admitted`), `ĉ`, bucket at its start,
    spend, report and end."""
    async with engine.connect() as conn:
        return (await conn.execute(_CYCLE_ROW, {"c": cycle_id})).one()


# ---- M3 Task 12: the admission ---------------------------------------------------------------------------

# A title that shares too few trigrams with `score_profile`'s target titles ("Backend Engineer",
# "Backend-Entwickler") to be a title match (`admission.TITLE_MATCH_MIN`).
UNMATCHED_TITLE: Final = "Lagerlogistik Koordinator (m/w/d)"


@dataclass(frozen=True)
class TargetSpec:
    """One candidate target of `candidate_targets` (module notes); `created_at` `None` is the database's
    `now()`."""

    similarity: float | None = None
    title: str = UNMATCHED_TITLE
    completeness: str = "full"
    lane: str | None = None
    charged_attempts: int = 0
    not_before: datetime | None = None
    created_at: datetime | None = None


_CANDIDATE_JOB: Final = q(
    "INSERT INTO jobs (company_key, company_display, title, title_norm, location_kind, work_mode_possible, "
    "work_mode_source, employment_source, ad_language, completeness, posted_precision, first_seen_at, "
    "last_seen_at, availability, availability_changed_at, source_ids, field_provenance, det_facts_hash) "
    "VALUES ('musterfirma', 'Musterfirma', :title, :title_norm, 'unknown', "
    "ARRAY['remote','hybrid','onsite'], 'none', 'none', 'de', :completeness, 'unknown', now(), now(), "
    "'active', now(), ARRAY['jsearch'], '{}', '\\x00') RETURNING id"
)
_CANDIDATE_REVISION: Final = q(
    "INSERT INTO job_revisions (job_id, revision_no, material_hash, llm_input, description_text, "
    "description_completeness, ad_language, embedding_status) VALUES (:job, 1, '\\x00', '{}', "
    "'Erfundene Testanzeige.', :completeness, 'de', 'awaiting_embedding') RETURNING id"
)
_CANDIDATE_TARGET: Final = q(
    "INSERT INTO score_targets (job_revision_id, llm_tuple_id, job_id, state, similarity, lane, "
    "charged_attempts, not_before, created_at) VALUES (:r, :tuple, :j, 'pending', :similarity, :lane, "
    ":charged, :not_before, coalesce(CAST(:created_at AS timestamptz), now())) RETURNING id"
)


async def candidate_targets(engine: AsyncEngine, *specs: TargetSpec) -> list[PendingTarget]:
    """One pending target per spec, in order (module notes)."""
    made: list[PendingTarget] = []
    async with engine.begin() as conn:
        await activate_test_policy(conn)
        tuple_id = await activate_score_tuple(conn)
        for spec in specs:
            title = normalize_title(spec.title)
            job_params = {
                "title": title.title,
                "title_norm": title.title_norm,
                "completeness": spec.completeness,
            }
            job = int((await conn.execute(_CANDIDATE_JOB, job_params)).scalar_one())
            revision_params = {"job": job, "completeness": spec.completeness}
            revision = int((await conn.execute(_CANDIDATE_REVISION, revision_params)).scalar_one())
            await conn.execute(_CURRENT, {"r": revision, "j": job})
            params = {
                "r": revision,
                "tuple": tuple_id,
                "j": job,
                "similarity": spec.similarity,
                "lane": spec.lane,
                "charged": spec.charged_attempts,
                "not_before": spec.not_before,
                "created_at": spec.created_at,
            }
            target = int((await conn.execute(_CANDIDATE_TARGET, params)).scalar_one())
            made.append(PendingTarget(target, job, revision, tuple_id))
    return made


_LANE_CREDITS: Final = q("SELECT lane, credit FROM admission_lane_credits WHERE llm_tuple_id = :t")


async def lane_credits(engine: AsyncEngine, llm_tuple_id: int) -> dict[str, Decimal]:
    """The stored lane credits of the tuple, by lane (none before its first admission)."""
    async with engine.connect() as conn:
        rows = await conn.execute(_LANE_CREDITS, {"t": llm_tuple_id})
        return {str(row.lane): Decimal(row.credit) for row in rows}


_ACTIVE_SETTINGS: Final = q(
    "SELECT v.payload FROM active_config a JOIN settings_versions v ON v.id = a.settings_version_id "
    "WHERE a.id = 1 FOR UPDATE OF a"
)
_SETTINGS_VERSION: Final = q(
    "INSERT INTO settings_versions (version_no, schema_version, payload, scope_version_id, model_policy_id, "
    "execution_config_id, evaluation_policy_id, effect_classes) "
    "SELECT (SELECT max(version_no) + 1 FROM settings_versions), v.schema_version, :payload, "
    "v.scope_version_id, v.model_policy_id, v.execution_config_id, v.evaluation_policy_id, '{collection}' "
    "FROM active_config a JOIN settings_versions v ON v.id = a.settings_version_id WHERE a.id = 1 "
    "RETURNING id",
    jsonb=["payload"],
)
_ACTIVATE_SETTINGS: Final = q("UPDATE active_config SET settings_version_id = :v WHERE id = 1")


async def list_on_watchlist(conn: AsyncConnection, entries: Mapping[int, bool]) -> None:
    """The active settings' `watchlist[]` with each company id of `entries` listed as given (module notes)."""
    payload = dict(get_object((await conn.execute(_ACTIVE_SETTINGS)).scalar_one(), ""))
    listed: dict[str, JSONValue] = {}
    for entry in get_list(payload["watchlist"], ""):
        listed[str(get_object(entry, "")["watchlist_company_id"])] = entry
    for company, enabled in entries.items():
        listed[str(company)] = {"watchlist_company_id": company, "enabled": enabled}
    payload["watchlist"] = list(listed.values())
    version = int((await conn.execute(_SETTINGS_VERSION, {"payload": payload})).scalar_one())
    await conn.execute(_ACTIVATE_SETTINGS, {"v": version})
