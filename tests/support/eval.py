"""Golden-set and eval test support (M3 Task 19). Every datum is invented: the packaged adversarial fixtures,
the synthetic ad of `tests.support.validation` and the profile `score_profile`.

- `seed_adversarial(engine, *, fixtures_dir=None)`: `golden.seed_adversarial` of the packaged fixtures (or
  of `fixtures_dir`) in a transaction of its own, `now` the test clock; the `Seeded` counts;
- `scored_job(conn)`: a job of the synthetic ad with a published result of the score tuple made active and
  its current score instance under the active scope (`Scored`), two of its det-fact fields supplied by two
  source instances (`jobs.field_provenance`), so its snapshot's sources are those two sources;
- `cases(engine)`: the golden cases (`CaseRef`: id, revision, job, profile, source, label), by id;
- `insert_cases(engine, n)`: `n` `week1` cases of new synthetic-ad jobs over the score tuple's profile.

The eval run's side:
- `fake_eval_answers(engine, fake_llm)`: the fake's standing answers of every case, keyed by its revision
  (Ruling P26): `eval_generator_body` for the generator and both revisions (every criterion rated with a quote
  of the ad and of the profile; the `not_a_job_ad` fixture's says it is no job ad) and `eval_critic_body` for
  the three critic rounds (accepting; for `not_a_job_ad`, `not_a_job_ad:ad` left uncertain, carry T10-e);
- `eval_deps(engine, fake_llm, *, fake_mode=True)`: the `OperationDeps` an execution reads, on the fake;
- `start_eval(engine, …)`: a run and its operation (`eval_runner.start_run`), its job not deferred;
  `execute(engine, op_id, deps, *, epoch)`: one in-process execution on a registry of its own;
- `run_row`, `operation_row`, `case_rows`, `run_calls` and `counted` read what a run wrote;
  `filtering_scope(conn, case_id, **scope)` gives a case a scope of its own.
"""

import dataclasses
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.seed import ensure_hashed
from jobhunter.db.sql import q
from jobhunter.graphs.score.deps import ScoreDeps
from jobhunter.llm.schemas.critic import CriticOut
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.run_settings import scope_from_payload
from jobhunter.scoring import eval_runner, golden
from jobhunter.scoring.det_results import JobRow, ScopeRow, compute_and_write, det_inputs, job_sources
from jobhunter.scoring.eval_fixtures import adversarial_dir
from jobhunter.scoring.facts import NOT_A_JOB_AD
from jobhunter.scoring.filters import read_det_facts
from jobhunter.types import JSONValue, get_object
from jobhunter.worker.executions import ExecutionRegistry
from jobhunter.worker.operations import OperationDeps, execute_operation
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.llm import FakeLLM
from tests.support.m3 import NOW, Job, insert_golden_case
from tests.support.scoring import (
    activate_score_tuple,
    insert_score_job,
    profile_of_tuple,
    published_result,
    review_item,
    score_llm_tuple,
    synthetic_review,
)
from tests.support.validation import synthetic_ad

NOT_A_JOB_AD_FACT: Final = NOT_A_JOB_AD.fact_id


async def seed_adversarial(
    engine: AsyncEngine, *, fixtures_dir: Path | None = None, now: datetime = NOW
) -> golden.Seeded:
    """The adversarial seed in a transaction of its own (module docstring)."""
    async with engine.begin() as conn:
        return await golden.seed_adversarial(
            conn, fixtures_dir=adversarial_dir() if fixtures_dir is None else fixtures_dir, now=now
        )


@dataclass(frozen=True)
class Scored:
    """A job with a published result of the active tuple and its current score instance."""

    job: Job
    llm_tuple_id: int
    target_id: int
    result_id: int
    det_result_id: int
    scope_version_id: int
    sources: tuple[str, str]


_INSTANCES: Final = q(
    "SELECT DISTINCT ON (source_id) id AS instance_id, source_id FROM source_instances "
    "ORDER BY source_id, id LIMIT 2"
)
_PROVENANCE: Final = q(
    "UPDATE jobs SET field_provenance = jsonb_build_object("
    "'salary', jsonb_build_object('source_instance_id', CAST(:a AS bigint), 'rule', 'first_complete'), "
    "'employment_types', jsonb_build_object('source_instance_id', CAST(:b AS bigint), 'rule', 'first')) "
    "WHERE id = :j"
)
_ACTIVE_SCOPE: Final = q(
    "SELECT s.id, s.payload FROM active_config a JOIN scope_versions s ON s.id = a.scope_version_id"
)


async def scored_job(conn: AsyncConnection) -> Scored:
    """A scored job (module docstring)."""
    llm_tuple_id = await activate_score_tuple(conn)
    job = await insert_score_job(conn, synthetic_ad())
    first, second = (await conn.execute(_INSTANCES)).all()
    await conn.execute(_PROVENANCE, {"a": first.instance_id, "b": second.instance_id, "j": job.job_id})
    published = await published_result(conn, job, llm_tuple_id)
    scope = (await conn.execute(_ACTIVE_SCOPE)).one()
    instance = await compute_and_write(
        conn,
        llm_result_id=published.result_id,
        scope=ScopeRow(int(scope.id), scope_from_payload(get_object(scope.payload, ""))),
        det=await read_det_facts(conn, job.job_id),
        sources=await job_sources(conn, job.job_id),
        job=JobRow(job.job_id),
    )
    assert instance is not None
    return Scored(
        job=job,
        llm_tuple_id=llm_tuple_id,
        target_id=published.target_id,
        result_id=published.result_id,
        det_result_id=instance,
        scope_version_id=int(scope.id),
        sources=(str(first.source_id), str(second.source_id)),
    )


@dataclass(frozen=True)
class CaseRef:
    id: int
    job_revision_id: int
    job_id: int
    profile_version_id: int
    source: str
    label: str


_CASES: Final = sa.text(
    "SELECT g.id, g.job_revision_id, r.job_id, g.profile_version_id, g.source, g.label "
    "FROM golden_cases g JOIN job_revisions r ON r.id = g.job_revision_id ORDER BY g.id"
)


async def cases(engine: AsyncEngine) -> list[CaseRef]:
    """The golden cases, by id (module docstring)."""
    async with engine.connect() as conn:
        return [
            CaseRef(
                int(r.id), int(r.job_revision_id), int(r.job_id), int(r.profile_version_id), r.source, r.label
            )
            for r in await conn.execute(_CASES)
        ]


# ---- the fake's answers of an eval run (keyed by the case's revision, Ruling P26) -------------------------

_CRITERIA: Final = (
    "role_fit",
    "skills",
    "experience_seniority",
    "domain",
    "languages",
    "education_certifications",
)
_NOT_A_JOB_AD: Final = "not_a_job_ad"  # the fixture whose generator answer says it is no job ad


def ad_quote(description: str) -> str:
    """A quote of the description: its first eight words, verbatim."""
    words = description.split(" ")
    return " ".join(words[:8])


def profile_quote(profile: ConfirmedProfile) -> str:
    """A quote of the profile's `skills` section: its first skill name of at least four characters."""
    return next(skill.name for skill in profile.skills if len(skill.name) >= 4)


def eval_generator_body(
    key: str, description: str, profile: ConfirmedProfile, *, level: int = 2
) -> dict[str, Any]:
    """A candidate C generator answer for an eval case: every criterion `level` with a quote of the ad and of
    the profile, no fact asserted; the `not_a_job_ad` fixture's says it is no job ad (T7: the critic must
    review `not_a_job_ad:ad`)."""
    criterion = {
        "level": level,
        "basis": "observed",
        "ad_evidence": [ad_quote(description)],
        "profile_evidence": [{"section": "skills", "quote": profile_quote(profile)}],
        "rationale": "Synthetic rationale.",
    }
    return {
        "not_a_job_ad": key == _NOT_A_JOB_AD,
        "injection_observed": False,
        "facts": [],
        "criteria": [{"criterion": name, **criterion} for name in _CRITERIA],
        "summary_en": "A synthetic summary.",
        "summary_de": "Eine erfundene Zusammenfassung.",
    }


def eval_critic_body(key: str) -> dict[str, Any]:
    """The critic's answer for an eval case: an accepting review with nothing disputed; the `not_a_job_ad`
    fixture's leaves `not_a_job_ad:ad` uncertain, so the flag is never verified (the contested badge path,
    carry T10-e)."""
    if key == _NOT_A_JOB_AD:
        return synthetic_review(review_item(NOT_A_JOB_AD_FACT, "fact_uncertain"), verdict="revise")
    return synthetic_review()


_ANSWER_CASES: Final = sa.text(
    "SELECT g.id, g.job_revision_id, r.description_text, j.company_key, v.profile_json "
    "FROM golden_cases g JOIN job_revisions r ON r.id = g.job_revision_id JOIN jobs j ON j.id = r.job_id "
    "JOIN profile_versions v ON v.id = g.profile_version_id ORDER BY g.id"
)


def fixture_key(company_key: str) -> str:
    """The adversarial fixture of a synthetic job's company key (`eval-<key>`), else the key itself."""
    return company_key.removeprefix(golden.EVAL_COMPANY_PREFIX)


async def fake_eval_answers(engine: AsyncEngine, fake_llm: FakeLLM) -> dict[int, str]:
    """Register the fake's standing answers of every golden case, keyed by its revision (Ruling P26: the id
    the gateway's fake key puts first): the generator's and both revisions' (`eval_generator_body`) and the
    three critic rounds' (`eval_critic_body`). Returns each case's fixture key by case id."""
    keys: dict[int, str] = {}
    async with engine.connect() as conn:
        rows = (await conn.execute(_ANSWER_CASES)).all()
    for row in rows:
        key = fixture_key(row.company_key)
        keys[int(row.id)] = key
        profile = ConfirmedProfile.model_validate(row.profile_json)
        body = eval_generator_body(key, row.description_text, profile)
        revision = int(row.job_revision_id)
        fake_llm.answer("generate", revision, body)
        for round_ in (1, 2):
            fake_llm.answer("revise", revision, body, round=round_)
        for round_ in (1, 2, 3):
            fake_llm.answer("critic", revision, eval_critic_body(key), round=round_, schema=CriticOut)
    return keys


def eval_deps(engine: AsyncEngine, fake_llm: FakeLLM, *, fake_mode: bool = True) -> OperationDeps:
    """The worker's `OperationDeps` an `eval_run` execution reads (`cfg` and `score_steps`, its gateway and
    clock; Ruling P33, M3 Task 27): a gateway on the fake, the config in fake LLM mode or, with
    `fake_mode=False`, the same config as a live-configured worker's (the transport check reads
    `cfg.fake_llm`; the gateway still sends to the fake, so a test sees any try that would have been paid).
    The profile collaborators and the Batches client are not used."""
    cfg = fake_llm.cfg if fake_mode else dataclasses.replace(fake_llm.cfg, fake_llm=False)
    gateway = fake_llm.gateway(engine)
    unused: Any = None
    return OperationDeps(
        cfg=cfg,
        gateway=gateway,
        parse_client=unused,
        cv_store=unused,
        profile_graphs=unused,
        embedder=None,
        clock=utcnow,
        score_steps=ScoreDeps(
            engine=engine, gateway=gateway, clock=utcnow, monotonic=gateway.monotonic, saver=None
        ),
        batches_client=unused,
    )


async def start_eval(
    engine: AsyncEngine,
    *,
    route: eval_runner.Route = "production",
    kind: eval_runner.RunKind = "golden",
    transport: eval_runner.Transport = "fake",
    candidate: eval_runner.Candidate | None = None,
    now: datetime | None = None,
) -> eval_runner.StartedRun:
    """A new eval run and its operation (`eval_runner.start_run`, as the CLI inserts them) over the active
    candidate unless one is given, its job not deferred (a test executes it)."""
    async with engine.begin() as conn:
        chosen = await eval_runner.active_candidate(conn) if candidate is None else candidate
        return await eval_runner.start_run(
            conn,
            kind=kind,
            route=route,
            candidate=chosen,
            transport_requested=transport,
            now=utcnow() if now is None else now,
            defer=None,
        )


async def execute(engine: AsyncEngine, op_id: int, deps: OperationDeps, *, epoch: int) -> None:
    """One execution of operation `op_id` in process with `deps`, on an execution registry of its own."""
    await execute_operation(engine, op_id, epoch=epoch, deps=deps, registry=ExecutionRegistry())


_RUN: Final = sa.text("SELECT * FROM eval_runs WHERE id = :r")
_OPERATION: Final = sa.text("SELECT status, last_error_code, attempts FROM operations WHERE id = :o")
_CASE_ROWS: Final = sa.text(
    "SELECT * FROM eval_case_results WHERE eval_run_id = :r ORDER BY golden_case_id, repetition"
)
_RUN_CALLS: Final = sa.text(
    "SELECT c.id, c.purpose, c.round, c.logical_call_no, c.eval_run_id, c.golden_case_id, c.input_refs, "
    "c.status, c.stop_reason, c.error_code, l.purpose_class, l.eval_run, l.state, l.settled_usd, "
    "l.reserved_usd "
    "FROM llm_calls c JOIN llm_spend_ledger l ON l.id = c.spend_ledger_id "
    "WHERE c.eval_run_id = :r ORDER BY c.id"
)


async def run_row(engine: AsyncEngine, eval_run_id: int) -> sa.Row[Any]:
    async with engine.connect() as conn:
        return (await conn.execute(_RUN, {"r": eval_run_id})).one()


async def operation_row(engine: AsyncEngine, operation_id: int) -> sa.Row[Any]:
    async with engine.connect() as conn:
        return (await conn.execute(_OPERATION, {"o": operation_id})).one()


async def case_rows(engine: AsyncEngine, eval_run_id: int) -> list[sa.Row[Any]]:
    """The run's `eval_case_results`, by case and repetition."""
    async with engine.connect() as conn:
        return list((await conn.execute(_CASE_ROWS, {"r": eval_run_id})).all())


async def run_calls(engine: AsyncEngine, eval_run_id: int) -> list[sa.Row[Any]]:
    """The run's calls with their ledger rows, oldest first."""
    async with engine.connect() as conn:
        return list((await conn.execute(_RUN_CALLS, {"r": eval_run_id})).all())


def counted(call: sa.Row[Any]) -> Decimal:
    """What the ledger counts for one call's try (§6.9.1): billed at its cost, not billed at 0, the rest at
    its reservation."""
    if call.state == "billed":
        return Decimal(call.settled_usd)
    return Decimal(0) if call.state == "not_billed" else Decimal(call.reserved_usd)


_CASE_SCOPE: Final = q("UPDATE golden_cases SET scope_version_id = :s WHERE id = :g")


async def filtering_scope(conn: AsyncConnection, case_id: int, **scope: JSONValue) -> int:
    """Case `case_id`'s scope replaced by its own scope with `scope`'s fields (a new `scope_versions` row by
    hash); its id."""
    payload = (
        await conn.execute(
            sa.text(
                "SELECT s.payload FROM golden_cases g JOIN scope_versions s ON s.id = g.scope_version_id "
                "WHERE g.id = :g"
            ),
            {"g": case_id},
        )
    ).scalar_one()
    scope_id = await ensure_hashed(conn, "scope_versions", {**get_object(payload, ""), **scope})
    await conn.execute(_CASE_SCOPE, {"s": scope_id, "g": case_id})
    return scope_id


async def insert_cases(engine: AsyncEngine, n: int) -> list[CaseRef]:
    """`n` active `week1` golden cases, one per new job of the synthetic ad, of the score tuple's profile
    (`score_profile`) under the active scope, each with its job's det-facts frozen as its snapshot (no
    source); by id."""
    async with engine.begin() as conn:
        profile_version_id = await profile_of_tuple(conn, await score_llm_tuple(conn))
        scope = (await conn.execute(_ACTIVE_SCOPE)).one()
        settings = scope_from_payload(get_object(scope.payload, ""))
        for _ in range(n):
            job = await insert_score_job(conn, synthetic_ad())
            snapshot, _ = det_inputs(await read_det_facts(conn, job.job_id), {}, settings)
            await insert_golden_case(
                conn, job, profile_version_id, det_inputs_snapshot=snapshot, det_input_sources=[]
            )
    return await cases(engine)
