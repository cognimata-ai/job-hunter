"""The `eval_run` operation and the eval runner on PostgreSQL and the recorded-SSE fake (spec §11.13
L4610-4613, §20.2 L6362; plan Task 19, R2, Rulings P17, P26, P33, round-4 ruling (a), Codex r1 #29-#31, r2
B19-B22, r3 B4-B7, r4 B4-B7, r5 B2, B8): `scoring.eval_runner`, the `eval_run` failure hook
(`worker.operations.eval_run_failed`) and `llm_ledger_unsettled`'s abandoned-run rule.

The fake answers each case by its revision (`fake.answer_key` puts `job_revision_id` first; Ruling P26):
`tests.support.eval.fake_eval_answers`. A run is one execution, never resumed: an interrupted run is failed
by the next execution, which makes no call, and a new run scores every case afresh.

Every datum is invented: the packaged adversarial fixtures and the synthetic ad of
`tests.support.validation`.
"""

import asyncio
import json
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Final

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter import cli_golden
from jobhunter.clock import utcnow
from jobhunter.db.seed import load_settings_v1
from jobhunter.llm import gateway as gateway_module
from jobhunter.llm import spend_ledger
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.scoring import eval_runner, golden
from jobhunter.scoring.eval_fixtures import adversarial_dir
from jobhunter.scoring.triggers import golden_sample_key, sampled
from jobhunter.settings.save import SaveRequest, save_settings
from jobhunter.types import JSONValue
from jobhunter.worker import operations as ops
from jobhunter.worker.epoch import WorkerIdentity, claim_epoch_and_sweep
from jobhunter.worker.executions import ExecutionRegistry
from jobhunter.worker.lock import LockSession
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.batches import make_due
from tests.support.collect import ctx
from tests.support.eval import (
    case_rows,
    cases,
    counted,
    eval_deps,
    eval_generator_body,
    execute,
    fake_eval_answers,
    filtering_scope,
    insert_cases,
    operation_row,
    run_calls,
    run_row,
    seed_adversarial,
    start_eval,
)
from tests.support.faults import PausePoint, SimulatedCrash, no_fault, raising_at
from tests.support.fixtures import ConfigFactory
from tests.support.llm import FakeLLM, create_llm_call_operation
from tests.support.m2 import insert_llm_call
from tests.support.scoring import activate_test_policy, fence_jobs, fence_profile, set_job_facts
from tests.support.settings import generation, procrastinate_job_exists_for

pytestmark = pytest.mark.integration

METRIC_KEYS: Final = {"transport", "transport_requested", "cases", "repetitions"}
# Task 20 (Ruling P17): the calibration merged into the same `metrics` by the terminal transaction
CALIBRATION_KEYS: Final = {
    "schema_validity",
    "grounding_rate",
    "spearman_rho",
    "pairwise_agreement",
    "false_exclusions",
    "knockout_recall",
    "contested_rate",
    "test_retest_sd",
    "test_retest_sample",
    "per_profile",
    "critic_rate",
    "projected_critic_rate",
    "admission_recall",
    "similarity_cutoff",
    "mean_cost_usd",
    "central_cost_usd",
    "observations",
    "gate",
}
SCORED: Final = frozenset({"accepted", "contested"})


@pytest.fixture(autouse=True)
async def _policy(engine: AsyncEngine, clean_db: None) -> None:
    """The candidate's evaluation policy: the seeded thresholds with `t8_sample_pct = 0` (Ruling P48), so no
    case's critic depends on its id unless a test sets the rate itself."""
    async with engine.begin() as conn:
        await activate_test_policy(conn)


@pytest.fixture(autouse=True)
def _unfaulted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(eval_runner, "afault_point", no_fault)


def key_of(keys: dict[int, str], key: str) -> int:
    return next(case_id for case_id, k in keys.items() if k == key)


async def rows_of(engine: AsyncEngine, table: str) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(sa.text(f"SELECT count(*) FROM {table}"))).scalar_one())  # noqa: S608


async def ledger_cost(engine: AsyncEngine, eval_run_id: int) -> Decimal:
    total = sum((counted(call) for call in await run_calls(engine, eval_run_id)), Decimal(0))
    return total.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


async def seeded_run(
    engine: AsyncEngine, fake_llm: FakeLLM, *, route: eval_runner.Route = "production"
) -> tuple[dict[int, str], eval_runner.StartedRun]:
    await seed_adversarial(engine)
    keys = await fake_eval_answers(engine, fake_llm)
    return keys, await start_eval(engine, route=route)


# ---- a golden run -----------------------------------------------------------------------------------------


async def test_a_golden_run_scores_every_case_outside_the_target_table(
    engine: AsyncEngine,
    fake_llm: FakeLLM,
    current_epoch: int,
    log_events: Callable[[], list[dict[str, Any]]],
) -> None:
    await seed_adversarial(engine)
    keys = await fake_eval_answers(engine, fake_llm)
    snippet, fenced = key_of(keys, "adzuna_snippet"), key_of(keys, "underqualified")
    by_id = {c.id: c for c in await cases(engine)}
    async with engine.begin() as conn:
        # the snippet's snapshot holds a structured full-time employment: a part-time-only scope filters it,
        # though the job's current facts, changed since its label, would pass (L4610: never the job's facts)
        await filtering_scope(conn, snippet, employment_types=["part_time"])
        await set_job_facts(conn, by_id[snippet].job_id, employment_types=["part_time"])
        await fence_jobs(conn, by_id[fenced].job_id)
    before = await generation(engine)
    started = await start_eval(engine, route="forced_critic")
    await execute(engine, started.operation_id, eval_deps(engine, fake_llm), epoch=current_epoch)

    run = await run_row(engine, started.eval_run_id)
    assert (run.status, run.passed, run.kind, run.route, run.repetitions) == (
        "completed",
        None,
        "golden",
        "forced_critic",
        2,
    )
    # Task 20: the calibration beside Task 19's keys, and `passed` from the gate: ten adversarial cases are
    # below the gate's 30, so it is uncalibrated and `passed` stays NULL
    assert set(run.metrics) == METRIC_KEYS | CALIBRATION_KEYS
    assert (run.metrics["gate"], run.metrics["observations"]["primary"]) == ("uncalibrated", len(keys) - 1)
    assert (run.metrics["transport"], run.metrics["transport_requested"]) == ("fake", "fake")
    assert run.metrics["cases"]["fenced"] == [fenced]
    assert (await operation_row(engine, started.operation_id)).status == "succeeded"

    rows = await case_rows(engine, started.eval_run_id)
    first = {r.golden_case_id: r for r in rows if r.repetition == 1}
    second = {r.golden_case_id: r for r in rows if r.repetition == 2}
    assert set(first) == set(keys) - {fenced}  # one row per case; the fenced case is skipped, no row
    filtered = first[snippet]
    assert (filtered.outcome, filtered.critic_ran, filtered.llm_call_ids, filtered.score_mid) == (
        "filtered_out",
        False,
        [],
        None,
    )
    assert filtered.output == {"filters": ["employment_type_excluded"]}
    scored = {case_id: r for case_id, r in first.items() if case_id != snippet}
    assert {r.outcome for r in scored.values()} <= SCORED
    assert all(r.critic_ran for r in scored.values())  # forced_critic: the critic reviews every case
    assert all(r.score_mid is not None for r in scored.values())
    # the test-retest sample: every case whose first score is numeric, in case-id order (fewer than ten)
    assert sorted(second) == sorted(scored) and len(rows) == len(first) + len(second)
    assert run.metrics["repetitions"] == {"attempted": sorted(scored), "sample": sorted(scored)}
    # fix round 1, item 12 (the shapes Task 20 reads): the outcomes by repetition, and each published
    # case's final validation, its repairs and its grounding failures as paths and codes only
    by_repetition = {str(n): dict(Counter(r.outcome for r in rows if r.repetition == n)) for n in (1, 2)}
    assert run.metrics["cases"]["outcomes"] == by_repetition
    for row in (r for r in rows if r.outcome in SCORED):
        validation = row.output["validation"]
        assert set(validation) == {"repairs", "grounding_failures"}
        entries = [*validation["repairs"], *validation["grounding_failures"]]
        assert all(
            set(e) == {"path", "code"} and all(isinstance(v, str) for v in e.values()) for e in entries
        )

    calls = await run_calls(engine, started.eval_run_id)
    assert calls and all(c.eval_run_id == started.eval_run_id and c.golden_case_id in scored for c in calls)
    assert {(c.purpose_class, c.eval_run) for c in calls} == {("nonscoring", True)}  # the reserve (L2341)
    owners = {c.input_refs["owner"]["operation_id"] for c in calls}
    assert owners == {started.operation_id}  # the owner is the operation's claim
    for row in rows:
        own = [
            c.id
            for c in calls
            if c.golden_case_id == row.golden_case_id and c.input_refs["repetition"] == row.repetition
        ]
        assert row.llm_call_ids == own
    assert run.cost_usd == await ledger_cost(engine, started.eval_run_id) > 0
    # the injected instruction changes nothing deterministic, and the not-a-job-ad draft is never verified:
    # it publishes contested, a real ad with the badge (`not_a_job_ad:ad` kept unconfirmed, carry T10-e)
    talent_pool = first[key_of(keys, "not_a_job_ad")]
    publication = talent_pool.output["publication"]
    assert (talent_pool.outcome, publication["not_a_job_ad"], talent_pool.output["badge"]) == (
        "contested",
        False,
        True,
    )
    assert publication["fact_confirmations"]["not_a_job_ad:ad"] in ("uncertain", "unconfirmed")
    assert [e["derived_outcome"] for e in talent_pool.output["rounds"] if e["step"] == "critic"]
    # nothing of the target table, the results or the ranking is written (L4611, Codex r2 B18)
    for table in (
        "score_targets",
        "score_attempts",
        "score_drafts",
        "critic_notes",
        "llm_results",
        "det_results",
    ):
        assert await rows_of(engine, table) == 0, table
    assert await generation(engine) == before
    # the logs name ids, codes and counts: no ad, profile or output text reaches them
    logged = json.dumps(log_events())
    assert "Ignoriere alle bisherigen Anweisungen" not in logged and "Talentpool" not in logged
    assert "Synthetic rationale" not in logged and "Frontend-Entwicklerin" not in logged


# ---- the cases a run skips: a fenced profile version, a case deleted meanwhile ---------------------------


def on_arrival(
    point: str, arrival: int, action: Callable[[], Awaitable[None]]
) -> Callable[[str], Awaitable[None]]:
    """An `afault_point` that runs `action` at the `arrival`-th arrival at `point`."""
    seen = 0

    async def fault(name: str) -> None:
        nonlocal seen
        if name == point:
            seen += 1
            if seen == arrival:
                await action()

    return fault


@pytest.mark.parametrize("when", ["before-the-run", "after-its-load"])
async def test_a_case_whose_profile_version_is_fenced_is_skipped_and_reported(
    engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    """Ruling T19-b (§20.2 L6352, L6362): an unreleased `profile` purge fence on the case's profile version
    skips the case as a fenced job does, whether it is there when the case is loaded or comes before its
    write: no row, reported in `metrics.cases.fenced`, and the run goes on."""
    keys, started = await seeded_run(engine, fake_llm, route="forced_critic")
    fenced = min(keys)  # the first case: its load is the run's first, its write follows its second call
    version = next(c.profile_version_id for c in await cases(engine) if c.id == fenced)

    async def fence() -> None:
        async with engine.begin() as conn:
            await fence_profile(conn, version)

    if when == "before-the-run":
        await fence()
    else:
        monkeypatch.setattr(eval_runner, "afault_point", on_arrival(eval_runner.BEFORE_CASE, 1, fence))
    await execute(engine, started.operation_id, eval_deps(engine, fake_llm), epoch=current_epoch)

    run = await run_row(engine, started.eval_run_id)
    assert (run.status, run.metrics["cases"]["fenced"]) == ("completed", [fenced])
    rows = await case_rows(engine, started.eval_run_id)
    assert {r.golden_case_id for r in rows if r.repetition == 1} == set(keys) - {fenced}
    calls = {c.golden_case_id for c in await run_calls(engine, started.eval_run_id)}
    assert (fenced in calls) == (when == "after-its-load")  # loaded, it was scored, but nothing is written


# where the case is deleted: before its load (at the previous case's commit), after its load (before its first
# call, whose reservation then names a case that is gone) or after its last call (before its write)
DELETIONS: Final[dict[str, tuple[str, int, int]]] = {
    "before-its-load": (eval_runner.AFTER_CASE_COMMIT, 1, 1),
    "before-its-first-call": (eval_runner.BEFORE_CASE, 1, 0),
    "before-its-write": (eval_runner.AFTER_CALL, 2, 0),
}


@pytest.mark.parametrize("when", list(DELETIONS))
async def test_a_case_deleted_during_the_run_is_skipped_and_reported(
    engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    """Fix round 1, item 5: a case deleted while the run scores it (`golden.delete_case`) is skipped and
    reported in `metrics.cases.deleted`, never requeued as a bug; the run goes on and completes."""
    point, arrival, index = DELETIONS[when]
    keys, started = await seeded_run(engine, fake_llm, route="forced_critic")
    deleted = sorted(keys)[index]

    async def delete() -> None:
        async with engine.begin() as conn:
            assert await golden.delete_case(conn, deleted)

    monkeypatch.setattr(eval_runner, "afault_point", on_arrival(point, arrival, delete))
    await execute(engine, started.operation_id, eval_deps(engine, fake_llm), epoch=current_epoch)

    run = await run_row(engine, started.eval_run_id)
    assert (await operation_row(engine, started.operation_id)).status == "succeeded"
    assert (run.status, run.metrics["cases"]["deleted"], run.metrics["cases"]["fenced"]) == (
        "completed",
        [deleted],
        [],
    )
    rows = await case_rows(engine, started.eval_run_id)
    assert {r.golden_case_id for r in rows if r.repetition == 1} == set(keys) - {deleted}
    assert deleted not in {c.golden_case_id for c in await run_calls(engine, started.eval_run_id)}


UNGROUNDED: Final = "Dieser Satz steht in keiner Anzeige und in keinem Profil"


async def test_a_published_case_keeps_its_final_validations_repairs_and_grounding_failures(
    engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int
) -> None:
    """Fix round 1, item 12: a published case's `output.validation` holds its final draft's repairs and its
    grounding failures (the V3 quote issues), each as a path and a code, never a quote. Every draft of the
    case repeats a criterion (the normaliser's `duplicate_criterion`) and cites an ungrounded ad quote, so the
    route revises twice and publishes it contested with both."""
    keys, started = await seeded_run(engine, fake_llm, route="forced_critic")
    flawed = key_of(keys, "overqualified")
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.text(
                    "SELECT g.job_revision_id, r.description_text, v.profile_json FROM golden_cases g "
                    "JOIN job_revisions r ON r.id = g.job_revision_id "
                    "JOIN profile_versions v ON v.id = g.profile_version_id WHERE g.id = :g"
                ),
                {"g": flawed},
            )
        ).one()
    body = eval_generator_body(
        "overqualified", row.description_text, ConfirmedProfile.model_validate(row.profile_json)
    )
    first, second = body["criteria"][0], body["criteria"][1]
    first["ad_evidence"] = [*first["ad_evidence"], UNGROUNDED]
    body["criteria"].append(dict(second))  # the same criterion twice
    revision = int(row.job_revision_id)
    fake_llm.answer("generate", revision, body, consume=True)
    for round_ in (1, 2):
        fake_llm.answer("revise", revision, body, round=round_, consume=True)
    await execute(engine, started.operation_id, eval_deps(engine, fake_llm), epoch=current_epoch)

    rows = [r for r in await case_rows(engine, started.eval_run_id) if r.golden_case_id == flawed]
    (case,) = [r for r in rows if r.repetition == 1]  # its second repetition reads the standing answers
    assert case.outcome == "contested"
    validation = case.output["validation"]
    assert {"path": f"criteria.{second['criterion']}", "code": "duplicate_criterion"} in validation["repairs"]
    assert validation["grounding_failures"] == [
        {"path": f"criteria.{first['criterion']}.ad_evidence[1]", "code": "quote_ungrounded"}
    ]
    assert UNGROUNDED not in json.dumps(case.output)  # a path and a code: never the quote


async def test_the_production_route_runs_the_critic_on_its_triggers_only(
    engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int
) -> None:
    keys, started = await seeded_run(engine, fake_llm)
    await execute(engine, started.operation_id, eval_deps(engine, fake_llm), epoch=current_epoch)
    first = {r.golden_case_id: r for r in await case_rows(engine, started.eval_run_id) if r.repetition == 1}
    quiet, triggered = first[key_of(keys, "german_ad_english_only")], first[key_of(keys, "injection_de")]
    assert (quiet.outcome, quiet.critic_ran) == ("accepted", False)  # no trigger, no critic (L3556)
    assert quiet.output["rounds"] == [{"step": "validate", "round": 0, "triggers": [], "det_failures": 0}]
    assert triggered.critic_ran and "T6" in triggered.output["rounds"][0]["triggers"]  # injection suspected
    assert (await run_row(engine, started.eval_run_id)).status == "completed"


async def test_t8_samples_each_case_by_its_golden_key(
    engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int
) -> None:
    """L4611: the sample identity is `golden:<case>:<tuple_identity>` under the candidate's policy."""
    async with engine.begin() as conn:
        await activate_test_policy(conn, t8_sample_pct=50)
    keys, started = await seeded_run(engine, fake_llm)
    await execute(engine, started.operation_id, eval_deps(engine, fake_llm), epoch=current_epoch)
    run = await run_row(engine, started.eval_run_id)
    profiles = {c.id: c.profile_version_id for c in await cases(engine)}
    rows = [r for r in await case_rows(engine, started.eval_run_id) if r.repetition == 1]
    expected = {}
    for row in rows:
        identity = golden.tuple_identity(
            profile_version_id=profiles[row.golden_case_id],
            rubric_version_id=run.rubric_version_id,
            prompt_schema_version_id=run.prompt_schema_version_id,
            model_policy_id=run.model_policy_id,
        )
        expected[row.golden_case_id] = sampled(
            golden_sample_key(golden_case_id=row.golden_case_id, tuple_identity=identity), 50
        )
    found = {r.golden_case_id: "T8" in r.output["rounds"][0]["triggers"] for r in rows}
    assert found == expected and set(found.values()) == {True, False}  # both sides of the sample occur
    assert len(found) == len(keys)


# ---- ownership: the claim, one execution, no resume --------------------------------------------------------


@pytest.mark.parametrize(
    "arrival", [1, 2], ids=["after-generate-next-reservation", "after-critic-next-case-write"]
)
async def test_a_reclaimed_eval_execution_can_neither_pay_nor_write(
    engine: AsyncEngine,
    fake_llm: FakeLLM,
    current_epoch: int,
    worker_lock: LockSession,
    monkeypatch: pytest.MonkeyPatch,
    log_events: Callable[[], list[dict[str, Any]]],
    arrival: int,
) -> None:
    """Codex r1 #29: the owner is the operation's claim. The old execution pauses after its first case's
    generator call (its next step is the critic's reservation) or after the critic call (its next write is
    the case row), and a second epoch takes the operation over. Resumed while the run is still `running` (so
    only the claim can fence it), the old execution reserves nothing and writes nothing; the new execution
    then fails the run `interrupted` and ends its operation `succeeded`, and the ledger keeps every call the
    old one settled."""
    keys, started = await seeded_run(engine, fake_llm, route="forced_critic")
    pause = PausePoint(eval_runner.AFTER_CALL, arrival=arrival)
    monkeypatch.setattr(eval_runner, "afault_point", pause)
    old = asyncio.create_task(
        execute(engine, started.operation_id, eval_deps(engine, fake_llm), epoch=current_epoch)
    )
    await asyncio.wait_for(pause.reached.wait(), 30)
    settled = [c.id for c in await run_calls(engine, started.eval_run_id)]
    assert len(settled) == arrival and fake_llm.served == arrival
    assert worker_lock.backend_pid is not None
    epoch, swept = await claim_epoch_and_sweep(
        engine, WorkerIdentity(uuid.uuid4(), "h", 2, worker_lock.backend_pid, "v")
    )
    assert swept["operations"] == 1  # the old claim is gone; the run is untouched
    assert (await run_row(engine, started.eval_run_id)).status == "running"

    pause.resume()
    await asyncio.wait_for(old, 30)
    lost = [e for e in log_events() if e["event"] == "execution_lost"]
    assert [e["key"] for e in lost] == [f"operation:{started.operation_id}"]  # fenced out, not finished
    assert fake_llm.served == arrival  # it reserved and sent nothing more
    assert await case_rows(engine, started.eval_run_id) == []  # nor wrote its case
    assert (await run_row(engine, started.eval_run_id)).status == "running"  # nor ended the run

    await execute(engine, started.operation_id, eval_deps(engine, fake_llm), epoch=epoch)
    run = await run_row(engine, started.eval_run_id)
    assert (run.status, run.metrics["failed"]) == ("failed", "interrupted")
    assert (await operation_row(engine, started.operation_id)).status == "succeeded"
    assert fake_llm.served == arrival  # the new execution made no call
    calls = await run_calls(engine, started.eval_run_id)
    assert [c.id for c in calls if c.state == "billed"] == settled  # the ledger holds what it settled
    assert all(c.state != "reserved" for c in calls) and len(keys) == 10
    assert run.cost_usd == await ledger_cost(engine, started.eval_run_id)


def crash_on_arrival(point: str, arrival: int) -> Callable[[str], Awaitable[None]]:
    """An `afault_point` that raises `SimulatedCrash` at the `arrival`-th arrival at `point`."""
    seen = 0

    async def fault(name: str) -> None:
        nonlocal seen
        if name == point:
            seen += 1
            if seen == arrival:
                raise SimulatedCrash(point)

    return fault


# point, arrival, the module whose fault point it is, and the first generator try's one-shot answer: a text
# that is no JSON (the gateway settles it, then raises `InvalidOutput`) or a cut text stopped at `max_tokens`
# (the gateway settles the first stage and reserves the enlarged one: its second reservation)
CRASHES: Final[dict[str, tuple[str, int, str, str | None]]] = {
    "after-the-first-call": (eval_runner.AFTER_CALL, 1, "eval", None),
    "after-an-invalid-output": (eval_runner.AFTER_CALL, 1, "eval", "invalid_output"),
    "after-a-max-tokens-first-stage": ("gateway.after_reserve", 2, "gateway", "max_tokens"),
    "after-a-critic-call": (eval_runner.AFTER_CALL, 2, "eval", None),
    "after-a-case-commit": (eval_runner.AFTER_CASE_COMMIT, 1, "eval", None),
    "before-the-first-call": (eval_runner.BEFORE_CASE, 1, "eval", None),
}
FIRST_CALL: Final = {  # the first call's row once the execution died: (stop reason, error code, ledger state)
    "invalid_output": ("end_turn", "invalid_output", "billed"),
    "max_tokens": ("max_tokens", "max_tokens_first_stage", "billed"),
}


@pytest.mark.parametrize("crash", list(CRASHES))
async def test_an_interrupted_eval_run_is_failed_and_never_resumed(
    engine: AsyncEngine,
    fake_llm: FakeLLM,
    current_epoch: int,
    monkeypatch: pytest.MonkeyPatch,
    crash: str,
) -> None:
    """Round-4 ruling (a): the execution dies at `crash`; the next execution of the operation makes no call
    and fails the run `interrupted`, unless nothing was touched yet (before the first call), when it is the
    run's first execution and completes it. A crashed case has no row; a new run scores every case afresh."""
    point, arrival, module, first_answer = CRASHES[crash]
    keys, started = await seeded_run(engine, fake_llm, route="forced_critic")
    first_case = min(keys)
    revision = next(c.job_revision_id for c in await cases(engine) if c.id == first_case)
    if first_answer == "invalid_output":  # one-shot, before the case's standing answer
        fake_llm.answer("generate", revision, "this text is no JSON", consume=True)
    elif first_answer == "max_tokens":
        fake_llm.answer("generate", revision, '{"not_a_job_ad": fal', stop_reason="max_tokens", consume=True)
    target = gateway_module if module == "gateway" else eval_runner
    monkeypatch.setattr(target, "afault_point", crash_on_arrival(point, arrival))
    deps = eval_deps(engine, fake_llm)
    await execute(engine, started.operation_id, deps, epoch=current_epoch)  # dies: requeued as `bug`
    assert (await operation_row(engine, started.operation_id)).status == "pending"
    rows_before = await case_rows(engine, started.eval_run_id)
    assert [r.golden_case_id for r in rows_before] == ([first_case] if crash == "after-a-case-commit" else [])
    if first_answer is not None:  # the crash came after that settlement
        call = (await run_calls(engine, started.eval_run_id))[0]
        assert (call.stop_reason, call.error_code, call.state) == FIRST_CALL[first_answer]
    served = fake_llm.served

    monkeypatch.setattr(target, "afault_point", no_fault)
    await make_due(engine)
    await execute(engine, started.operation_id, deps, epoch=current_epoch)
    run = await run_row(engine, started.eval_run_id)
    op = await operation_row(engine, started.operation_id)
    if crash == "before-the-first-call":
        assert run.status == "completed"  # nothing touched: this execution is the run's first
        assert {r.golden_case_id for r in await case_rows(engine, started.eval_run_id)} == set(keys)
    else:
        assert (run.status, run.metrics["failed"], op.status) == ("failed", "interrupted", "succeeded")
        assert fake_llm.served == served  # no call: nothing is resumed
        assert await case_rows(engine, started.eval_run_id) == rows_before  # the crashed case has no row
        assert run.cost_usd == await ledger_cost(engine, started.eval_run_id)
        again = await start_eval(engine, route="forced_critic")  # `jobhunter eval run` again
        await execute(engine, again.operation_id, deps, epoch=current_epoch)
        assert (await run_row(engine, again.eval_run_id)).status == "completed"
        fresh = await case_rows(engine, again.eval_run_id)
        assert {r.golden_case_id for r in fresh if r.repetition == 1} == set(keys)  # every case afresh


async def test_a_crash_around_the_terminal_commit_ends_the_run_once(
    engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex r5 B2: the run's terminal state and its operation's end commit together."""
    deps = eval_deps(engine, fake_llm)
    keys, before = await seeded_run(engine, fake_llm)
    monkeypatch.setattr(eval_runner, "afault_point", raising_at(eval_runner.BEFORE_TERMINAL_COMMIT))
    await execute(engine, before.operation_id, deps, epoch=current_epoch)
    assert (await run_row(engine, before.eval_run_id)).status == "running"  # rolled back with the end
    monkeypatch.setattr(eval_runner, "afault_point", no_fault)
    await make_due(engine)
    await execute(engine, before.operation_id, deps, epoch=current_epoch)
    run = await run_row(engine, before.eval_run_id)
    assert (run.status, run.metrics["failed"]) == ("failed", "interrupted")
    assert (await operation_row(engine, before.operation_id)).status == "succeeded"

    after = await start_eval(engine)
    monkeypatch.setattr(eval_runner, "afault_point", raising_at(eval_runner.AFTER_TERMINAL_COMMIT))
    await execute(engine, after.operation_id, deps, epoch=current_epoch)
    monkeypatch.setattr(eval_runner, "afault_point", no_fault)
    completed = await run_row(engine, after.eval_run_id)
    assert completed.status == "completed"
    assert (await operation_row(engine, after.operation_id)).status == "succeeded"  # nothing is running
    served = fake_llm.served
    await make_due(engine)
    await execute(engine, after.operation_id, deps, epoch=current_epoch)  # a re-delivery claims nothing
    # the reconciler's re-delivery as a new operation of the same run: the handler leaves it completed
    async with engine.begin() as conn:
        again, created = await ops.create_operation(
            conn,
            kind="eval_run",
            payload={"eval_run_id": after.eval_run_id},
            dedup_key=f"eval_run:{after.eval_run_id}",
            defer=None,
        )
    assert created
    await execute(engine, again, deps, epoch=current_epoch)  # no TransitionConflict escapes
    assert (await operation_row(engine, again)).status == "succeeded"
    assert await run_row(engine, after.eval_run_id) == completed and fake_llm.served == served
    # a failure hook run against the completed run leaves it as it is
    async with engine.begin() as conn:
        await ops.eval_run_failed(conn, again, {"eval_run_id": after.eval_run_id}, None)
    assert await run_row(engine, after.eval_run_id) == completed and len(keys) == 10


@pytest.mark.parametrize(("requested", "fake_mode"), [("fake", False), ("live", True)])
async def test_transport_mismatch_fails_before_any_call(
    engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int, requested: str, fake_mode: bool
) -> None:
    """Codex r2 B20, r3 B6: a fake run on a live worker pays nothing, a live run on a fake worker records
    nothing."""
    await seed_adversarial(engine)
    await fake_eval_answers(engine, fake_llm)
    started = await start_eval(engine, transport=requested)  # type: ignore[arg-type]
    assert (await run_row(engine, started.eval_run_id)).metrics == {"transport_requested": requested}
    await execute(
        engine, started.operation_id, eval_deps(engine, fake_llm, fake_mode=fake_mode), epoch=current_epoch
    )
    run = await run_row(engine, started.eval_run_id)
    assert (run.status, run.metrics) == (
        "failed",
        {"transport_requested": requested, "failed": "transport_mismatch"},
    )
    assert (await operation_row(engine, started.operation_id)).status == "succeeded"
    assert fake_llm.served == 0 and await run_calls(engine, started.eval_run_id) == []


async def test_an_operation_without_its_run_fails_once(
    engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int
) -> None:
    async with engine.begin() as conn:
        op_id, _ = await ops.create_operation(
            conn, kind="eval_run", payload={"eval_run_id": 10**12}, dedup_key="eval_run:missing", defer=None
        )
    await execute(engine, op_id, eval_deps(engine, fake_llm), epoch=current_epoch)
    op = await operation_row(engine, op_id)
    assert (op.status, op.last_error_code, op.attempts) == ("failed", eval_runner.RUN_MISSING, 0)
    assert fake_llm.served == 0


# ---- a candidate that does not exist yet -------------------------------------------------------------------


def candidate_payload(**evaluation: JSONValue) -> dict[str, Any]:
    payload = load_settings_v1()
    payload["evaluation"] = {**payload["evaluation"], **evaluation}  # type: ignore[dict-item]
    return payload


_POLICY_ROW: Final = sa.text("SELECT id, version_no FROM evaluation_policies WHERE id = :i")
_ACTIVE_POLICY: Final = sa.text("SELECT evaluation_policy_id, settings_version_id FROM active_config")


async def test_candidate_payload_stages_rows_without_activation(
    engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int, queue_app: procrastinate.App
) -> None:
    """Codex r2 B21, r3 B7: the candidate's rows are inserted by hash under `active_config FOR UPDATE`, with
    no settings version and no activation; a later save of the payload finds the same rows, so the gate's
    run is the candidate's. Two stagings started together each get their own policy and `version_no`."""
    await seed_adversarial(engine)
    await fake_eval_answers(engine, fake_llm)
    async with engine.connect() as conn:
        active_before = (await conn.execute(_ACTIVE_POLICY)).one()
    payload = candidate_payload(t2_band=7)
    async with engine.begin() as conn:
        candidate = await eval_runner.stage_candidate(conn, payload, now=utcnow())
    async with engine.connect() as conn:
        assert (await conn.execute(_ACTIVE_POLICY)).one() == active_before  # nothing activated
    assert candidate.evaluation_policy_id != active_before.evaluation_policy_id
    started = await start_eval(engine, candidate=candidate)
    await execute(engine, started.operation_id, eval_deps(engine, fake_llm), epoch=current_epoch)
    run = await run_row(engine, started.eval_run_id)
    assert (run.status, run.evaluation_policy_id) == ("completed", candidate.evaluation_policy_id)

    async with engine.begin() as conn:
        base = (await conn.execute(_ACTIVE_POLICY)).one().settings_version_id
        await save_settings(
            conn,
            SaveRequest(payload=payload, base_version_id=int(base), acknowledged_warnings=frozenset()),
            now=utcnow(),
            defer=defer_in_tx,
            internal=True,
        )
    async with engine.connect() as conn:
        saved = (await conn.execute(_ACTIVE_POLICY)).one()
        assert saved.evaluation_policy_id == candidate.evaluation_policy_id  # the save reuses the staged row
        found = (
            await conn.execute(
                sa.text("SELECT id FROM eval_runs WHERE evaluation_policy_id = :p AND status = 'completed'"),
                {"p": saved.evaluation_policy_id},
            )
        ).scalars()
        assert list(found) == [started.eval_run_id]

    async def stage(t2_band: int) -> eval_runner.Candidate:
        async with engine.begin() as conn:
            return await eval_runner.stage_candidate(conn, candidate_payload(t2_band=t2_band), now=utcnow())

    one, two = await asyncio.gather(stage(11), stage(12))
    async with engine.connect() as conn:
        rows = [(await conn.execute(_POLICY_ROW, {"i": c.evaluation_policy_id})).one() for c in (one, two)]
    assert rows[0].id != rows[1].id and rows[0].version_no != rows[1].version_no


async def test_an_invalid_candidate_payload_is_refused_before_any_row(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        policies = (await conn.execute(sa.text("SELECT count(*) FROM evaluation_policies"))).scalar_one()
    with pytest.raises(eval_runner.CandidateInvalid) as refused:
        async with engine.begin() as conn:
            await eval_runner.stage_candidate(conn, candidate_payload(t2_band=99), now=utcnow())
    assert [issue.field for issue in refused.value.issues] == ["payload.evaluation.t2_band"]
    async with engine.connect() as conn:
        assert (
            await conn.execute(sa.text("SELECT count(*) FROM evaluation_policies"))
        ).scalar_one() == policies


# ---- repetitions -------------------------------------------------------------------------------------------


async def test_second_repetitions_follow_case_id_order(
    engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex r2 B22, r5 B8: twelve active cases, the two lowest-id ones refused in repetition 1: repetition
    2 is attempted for cases 3-12 and no other, which are the sample. An interruption after the sixth second
    repetition fails the run with no further call, and a new run starts afresh at repetition 1."""
    made = await insert_cases(engine, 12)
    await fake_eval_answers(engine, fake_llm)
    for case in made[:2]:  # every try of the two lowest-id cases refused (a standing answer)
        fake_llm.answer("generate", case.job_revision_id, "", stop_reason="refusal")
    deps = eval_deps(engine, fake_llm)
    started = await start_eval(engine)
    await execute(engine, started.operation_id, deps, epoch=current_epoch)
    rows = await case_rows(engine, started.eval_run_id)
    ids = [c.id for c in made]
    assert [(r.golden_case_id, r.outcome) for r in rows if r.repetition == 1 and r.outcome == "refused"] == [
        (ids[0], "refused"),
        (ids[1], "refused"),
    ]
    assert sorted(r.golden_case_id for r in rows if r.repetition == 2) == ids[2:]
    run = await run_row(engine, started.eval_run_id)
    assert run.metrics["repetitions"] == {"attempted": ids[2:], "sample": ids[2:]}

    interrupted = await start_eval(engine)
    # 12 first repetitions, then the sixth second one: its commit is the 18th
    monkeypatch.setattr(eval_runner, "afault_point", crash_on_arrival(eval_runner.AFTER_CASE_COMMIT, 18))
    await execute(engine, interrupted.operation_id, deps, epoch=current_epoch)
    monkeypatch.setattr(eval_runner, "afault_point", no_fault)
    seconds = [
        r.golden_case_id for r in await case_rows(engine, interrupted.eval_run_id) if r.repetition == 2
    ]
    assert seconds == ids[2:8]
    served = fake_llm.served
    await make_due(engine)
    await execute(engine, interrupted.operation_id, deps, epoch=current_epoch)
    failed = await run_row(engine, interrupted.eval_run_id)
    assert (failed.status, failed.metrics["failed"], fake_llm.served) == ("failed", "interrupted", served)
    afresh = await start_eval(engine)
    await execute(engine, afresh.operation_id, deps, epoch=current_epoch)
    fresh = await case_rows(engine, afresh.eval_run_id)
    assert sorted(r.golden_case_id for r in fresh if r.repetition == 1) == ids  # no resume: from the start
    assert sorted(r.golden_case_id for r in fresh if r.repetition == 2) == ids[2:]


async def test_repetitions_stop_at_ten_samples(
    engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int
) -> None:
    """§11.13 L4613 "runs 10 cases twice": of twelve numeric cases, the first ten in id order are retested."""
    made = await insert_cases(engine, 12)
    await fake_eval_answers(engine, fake_llm)
    started = await start_eval(engine)
    await execute(engine, started.operation_id, eval_deps(engine, fake_llm), epoch=current_epoch)
    rows = await case_rows(engine, started.eval_run_id)
    ids = [c.id for c in made]
    assert sorted(r.golden_case_id for r in rows if r.repetition == 2) == ids[:10]
    assert (await run_row(engine, started.eval_run_id)).metrics["repetitions"]["sample"] == ids[:10]


async def test_a_canary_run_scores_the_canaries_once(
    engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int
) -> None:
    made = await insert_cases(engine, 4)
    await fake_eval_answers(engine, fake_llm)
    async with engine.begin() as conn:
        for case in made[1:3]:
            await golden.set_flags(conn, case.id, active=None, is_canary=True, now=utcnow())
    started = await start_eval(engine, kind="canary")
    await execute(engine, started.operation_id, eval_deps(engine, fake_llm), epoch=current_epoch)
    rows = await case_rows(engine, started.eval_run_id)
    assert [(r.golden_case_id, r.repetition) for r in rows] == [(made[1].id, 1), (made[2].id, 1)]
    run = await run_row(engine, started.eval_run_id)
    assert (run.status, run.repetitions, run.metrics["repetitions"]) == (
        "completed",
        1,
        {"attempted": [], "sample": []},
    )


# ---- abandonment (Codex r1 #31) ----------------------------------------------------------------------------

_STARTED_AT: Final = sa.text("UPDATE eval_runs SET started_at = :at WHERE id = :r")
_PROBE: Final = sa.text(
    "INSERT INTO eval_runs (kind, route, status, started_at) VALUES ('schema_dry_run', 'production', "
    "'running', :at) RETURNING id"
)


async def test_a_healthy_run_over_an_hour_is_not_abandoned(
    engine: AsyncEngine, current_epoch: int, queue_app: procrastinate.App
) -> None:
    await insert_cases(engine, 1)  # a golden run starts only with an active case (Task 20, `eval_no_cases`)
    started_at = utcnow() - timedelta(hours=2)
    healthy = await start_eval(engine, now=started_at)
    queued = await start_eval(engine)  # young, its operation still pending: live
    async with engine.begin() as conn:
        claim = await ops.claim_operation(conn, healthy.operation_id, epoch=current_epoch)
        assert claim is not None  # running, its heartbeat now: a live operation at started_at + 2 h
        probe = int((await conn.execute(_PROBE, {"at": started_at})).scalar_one())  # a CLI probe, no op
        await insert_llm_call(conn, purpose="generate", eval_run_id=probe)  # reserved at $1, unsettled
        young = int((await conn.execute(_PROBE, {"at": utcnow() - timedelta(minutes=59)})).scalar_one())
    reconcile = spend_ledger.reconcile_llm_ledger_unsettled
    assert await reconcile(ctx(engine, queue_app, now=utcnow(), epoch=current_epoch)) == 1
    assert (await run_row(engine, healthy.eval_run_id)).status == "running"
    assert (await run_row(engine, queued.eval_run_id)).status == "running"
    abandoned = await run_row(engine, probe)  # abandoned after an hour: the reason and the cost recorded
    assert (abandoned.status, abandoned.metrics, abandoned.cost_usd) == (
        "failed",
        {"failed": "abandoned"},
        Decimal("1.0000"),
    )
    assert (await run_row(engine, young)).status == "running"

    async with engine.begin() as conn:  # the operation ends `failed`: its hook fails the run
        await ops.finish_operation(conn, claim, status="failed", error_code="bug")
    failed = await run_row(engine, healthy.eval_run_id)
    assert (failed.status, failed.metrics) == (
        "failed",
        {"transport_requested": "fake", "failed": "interrupted"},
    )
    assert failed.finished_at is not None
    # once its operation has ended without ending the run, the run is failed at once, however young
    async with engine.begin() as conn:
        await ops.cancel_operation(conn, queued.operation_id, reason="test")
    assert await reconcile(ctx(engine, queue_app, now=utcnow(), epoch=current_epoch)) == 1
    assert (await run_row(engine, queued.eval_run_id)).status == "failed"


async def test_the_c35_helper_runs_under_kind_export(
    engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int
) -> None:
    """B R8: `create_llm_call_operation` registers its handler under kind `export` (M5's), since `eval_run`
    is this task's handler."""
    assert ops.HANDLERS["eval_run"] is eval_runner.eval_run
    assert ops.FAILURE_HOOKS["eval_run"] is ops.eval_run_failed
    op_id = await create_llm_call_operation(engine)
    async with engine.connect() as conn:
        kind = (
            await conn.execute(sa.text("SELECT kind FROM operations WHERE id = :o"), {"o": op_id})
        ).scalar_one()
    assert kind == "export"
    await ops.execute_operation(engine, op_id, epoch=current_epoch, registry=ExecutionRegistry())
    assert fake_llm.served == 1 and (await operation_row(engine, op_id)).status == "succeeded"
    assert ops.HANDLERS["eval_run"] is eval_runner.eval_run  # untouched


# ---- the CLI's side: `jobhunter golden seed-adversarial` and `jobhunter eval run` --------------------------


async def test_the_cli_seeds_starts_a_run_and_waits_for_its_end(
    engine: AsyncEngine,
    fake_llm: FakeLLM,
    current_epoch: int,
    make_config: ConfigFactory,
    queue_app: procrastinate.App,
    tmp_path: Path,
) -> None:
    """The CLI's functions against the installation's database: the seed of the packaged fixtures, a run and
    its operation inserted in one transaction with the operation's job deferred, a wait that returns once
    the worker (here: an in-process execution) ended the run, and a payload staged before its run starts."""
    cfg = make_config("worker")
    assert await cli_golden.seed(cfg, adversarial_dir()) == golden.Seeded(cases=10, jobs=10, profiles=10)
    await fake_eval_answers(engine, fake_llm)
    started = await cli_golden.start(
        cfg, kind="golden", route="production", candidate=None, payload=None, transport="fake", now=utcnow()
    )
    assert await procrastinate_job_exists_for(engine, started.operation_id)  # deferred with the insert
    run = await run_row(engine, started.eval_run_id)
    assert (run.status, run.metrics, run.repetitions) == ("running", {"transport_requested": "fake"}, 2)
    waiting = asyncio.create_task(cli_golden.wait(cfg, started.eval_run_id, poll_s=0.05))
    await asyncio.sleep(0.2)
    assert not waiting.done()  # the run is still running
    await execute(engine, started.operation_id, eval_deps(engine, fake_llm), epoch=current_epoch)
    end = await asyncio.wait_for(waiting, 30)
    assert (end.status, end.cost_usd) == ("completed", (await run_row(engine, started.eval_run_id)).cost_usd)

    path = tmp_path / "settings.json"
    path.write_text(json.dumps(candidate_payload(t2_band=9)), encoding="utf-8")
    staged = await cli_golden.start(
        cfg,
        kind="golden",
        route="forced_critic",
        candidate=None,
        payload=cli_golden.read_payload(path),
        transport="live",
        now=utcnow(),
    )
    row = await run_row(engine, staged.eval_run_id)
    async with engine.connect() as conn:
        active = (await conn.execute(_ACTIVE_POLICY)).one()
        payload = (
            await conn.execute(
                sa.text("SELECT payload FROM evaluation_policies WHERE id = :i"),
                {"i": row.evaluation_policy_id},
            )
        ).scalar_one()
    assert row.evaluation_policy_id != active.evaluation_policy_id and payload["t2_band"] == 9
    assert (row.route, row.metrics) == ("forced_critic", {"transport_requested": "live"})
