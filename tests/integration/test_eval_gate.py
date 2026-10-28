"""The eval gate on PostgreSQL and the recorded-SSE fake (spec §11.13 L4614-4631, §17.6 L5618; plan Task
20, B R14, Deviation 29, Codex r1 #25, #33, #34, r2 B20, B21, B24, r3 B7, Rulings P11, P17, T19-e): the
calibration in the golden run's terminal transaction, `validate.eval_gate_state` with a candidate, the
settings dry-run and save, `eval.gate_failed`, `validate.schema_verified` at the save, the `eval run` CLI's
refusals before any call, and the schema dry-run CLI's `--candidate-payload` and `--model-policy`.

A graded golden set: 30 cases of the synthetic ad, ten each `poor`, `ok` and `good`, the fake answering each
case's generator with every criterion at level 1, 2 or 3 by its label (Ruling P26: keyed by the revision), so
the scores follow the labels. The tests' process is a live one unless a test sets `JOBHUNTER_FAKE_LLM`
(`fake_llm_process`; a fake run counts for the gate only on the fake, Codex r2 B20). Every datum is
invented.
"""

import json
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter import cli_golden, cli_schema_dry_run
from jobhunter.clock import utcnow
from jobhunter.db.seed import content_hash, load_settings_v1
from jobhunter.llm.schemas.critic import CriticOut
from jobhunter.scoring import calibration, eval_runner
from jobhunter.settings import validate
from jobhunter.settings.derive import candidate_of, derived
from jobhunter.settings.model import Settings
from jobhunter.types import JSONValue
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.eval import (
    CaseRef,
    case_rows,
    eval_critic_body,
    eval_deps,
    eval_generator_body,
    execute,
    fake_eval_answers,
    insert_cases,
    operation_row,
    run_row,
    seed_adversarial,
    start_eval,
)
from tests.support.faults import no_fault, raising_at
from tests.support.fixtures import ConfigFactory
from tests.support.llm import FAKE_LLM_DIR, FakeLLM, score_profile
from tests.support.m3 import insert_id
from tests.support.profile import idem
from tests.support.scoring import activate_test_policy
from tests.support.settings import SETTINGS_URL, fresh_key, settings_body, warning_codes
from tests.support.validation import synthetic_ad

pytestmark = pytest.mark.integration

DRY_RUN_URL: Final = "/api/settings/dry-run"
METRIC_KEYS: Final = {"transport", "transport_requested", "cases", "repetitions"}  # Task 19's
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
LEVELS: Final = {"poor": 1, "ok": 2, "good": 3}
SONNET, OPUS, HAIKU = "claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"


@pytest.fixture(autouse=True)
async def _policy(engine: AsyncEngine, clean_db: None) -> None:
    """The seeded thresholds with `t8_sample_pct = 0` (Ruling P48): no case's critic depends on its id."""
    async with engine.begin() as conn:
        await activate_test_policy(conn)


@pytest.fixture(autouse=True)
def _unfaulted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(eval_runner, "afault_point", no_fault)


@pytest.fixture(autouse=True)
def _live_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts as a live process, whatever the environment the suite runs in."""
    monkeypatch.delenv("JOBHUNTER_FAKE_LLM", raising=False)


def label_of(i: int, n: int) -> str:
    return ("poor", "ok", "good")[i * 3 // n]


async def graded_cases(engine: AsyncEngine, fake_llm: FakeLLM, n: int = 30) -> list[CaseRef]:
    """`n` cases labelled `poor`, `ok`, `good` in thirds by id, the fake's generator answering each at the
    level of its label and the critic accepting (module docstring)."""
    made = (await insert_cases(engine, n))[-n:]  # the new ones: every case is listed, by id
    ad, profile = synthetic_ad(), score_profile()
    async with engine.begin() as conn:
        for i, case in enumerate(made):
            await conn.execute(
                sa.text("UPDATE golden_cases SET label = :l WHERE id = :g"),
                {"l": label_of(i, n), "g": case.id},
            )
    for i, case in enumerate(made):
        body = eval_generator_body("graded", ad.description_text, profile, level=LEVELS[label_of(i, n)])
        fake_llm.answer("generate", case.job_revision_id, body)
        for round_ in (1, 2):
            fake_llm.answer("revise", case.job_revision_id, body, round=round_)
        for round_ in (1, 2, 3):
            fake_llm.answer(
                "critic", case.job_revision_id, eval_critic_body("graded"), round=round_, schema=CriticOut
            )
    return made


async def golden_run(
    engine: AsyncEngine,
    fake_llm: FakeLLM,
    epoch: int,
    *,
    candidate: eval_runner.Candidate | None = None,
    route: eval_runner.Route = "production",
    repetitions: int | None = None,
) -> tuple[eval_runner.StartedRun, Any]:
    started = await start_eval(engine, candidate=candidate, route=route)
    if repetitions is not None:  # a run whose repeated sample is never attempted
        async with engine.begin() as conn:
            await conn.execute(
                sa.text("UPDATE eval_runs SET repetitions = :n WHERE id = :r"),
                {"n": repetitions, "r": started.eval_run_id},
            )
    await execute(engine, started.operation_id, eval_deps(engine, fake_llm), epoch=epoch)
    return started, await run_row(engine, started.eval_run_id)


async def alerts(engine: AsyncEngine, code: str) -> list[Any]:
    async with engine.connect() as conn:
        rows = await conn.execute(sa.text("SELECT * FROM alerts WHERE code = :c ORDER BY id"), {"c": code})
        return list(rows.all())


# ---- the calibration in the run's terminal transaction (Ruling P17) ---------------------------------------


async def test_a_golden_run_ends_with_its_calibration_and_its_gate(
    engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int
) -> None:
    made = await graded_cases(engine, fake_llm)
    started, run = await golden_run(engine, fake_llm, current_epoch)
    assert (run.status, run.passed) == ("completed", True)
    assert set(run.metrics) == METRIC_KEYS | CALIBRATION_KEYS  # merged beside Task 19's keys
    m = run.metrics
    assert (m["gate"], m["transport"]) == ("passed", "fake") and "failed_gates" not in m and "failed" not in m
    assert m["observations"] == {"primary": 30, "paired": 10, "cost": 40}
    assert m["test_retest_sample"] == [c.id for c in made[:10]] == m["repetitions"]["sample"]
    assert (m["schema_validity"], m["grounding_rate"], m["contested_rate"]) == (
        "1.000000",
        "1.000000",
        "0.000000",
    )
    # one score per label: the ranks of M are the labels' ranks, so rho and the pairs are perfect
    assert (m["spearman_rho"], m["pairwise_agreement"], m["test_retest_sd"]) == (
        "1.000000",
        "1.000000",
        "0.000000",
    )
    assert (m["false_exclusions"], m["knockout_recall"]) == (0, "1.000000")
    async with engine.connect() as conn:
        stored = (
            await conn.execute(
                sa.text("SELECT projected_critic_rate FROM evaluation_policies WHERE id = :p"),
                {"p": run.evaluation_policy_id},
            )
        ).scalar_one()
    assert m["projected_critic_rate"] == format(stored, "f") and m["admission_recall"] is None
    profile = str(made[0].profile_version_id)
    assert m["per_profile"][profile]["cases"] == 30 and m["per_profile"][profile]["below_pooled"] is False
    firsts = [r for r in await case_rows(engine, started.eval_run_id) if r.repetition == 1]
    by_label = {label_of(i, 30): {r.score_mid for r in firsts[i : i + 10]} for i in (0, 10, 20)}
    [poor], [ok], [good] = by_label["poor"], by_label["ok"], by_label["good"]  # one score per label
    assert poor < ok < good
    assert await alerts(engine, "eval.gate_failed") == []
    assert (await operation_row(engine, started.operation_id)).status == "succeeded"


async def test_a_run_without_its_repeated_sample_fails_the_gate_and_alerts(
    engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int
) -> None:
    """L4616-4625, Codex r1 #33: the test-retest gate is measured, never waived."""
    await graded_cases(engine, fake_llm)
    started, run = await golden_run(engine, fake_llm, current_epoch, repetitions=1)
    assert (run.status, run.passed, run.metrics["gate"]) == ("completed", False, "failed")
    assert run.metrics["failed_gates"] == [calibration.TEST_RETEST_MISSING] and "failed" not in run.metrics
    assert run.metrics["test_retest_sd"] is None and run.metrics["observations"]["paired"] == 0
    [alert] = await alerts(engine, "eval.gate_failed")
    assert (alert.severity, alert.resolved_at, alert.occurrences) == ("warn", None, 1)
    assert alert.context == {
        "eval_run_id": started.eval_run_id,
        "failed_gates": [calibration.TEST_RETEST_MISSING],
    }
    assert str(started.eval_run_id) in alert.message
    # a forced-critic run is a diagnostic: reported, never gating, never alerting (L4612)
    _, diagnostic = await golden_run(engine, fake_llm, current_epoch, route="forced_critic", repetitions=1)
    assert (diagnostic.passed, diagnostic.metrics["gate"]) == (False, "failed")
    assert [a.occurrences for a in await alerts(engine, "eval.gate_failed")] == [1]


async def test_a_runs_gate_counts_the_users_cases_only(
    engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int
) -> None:
    """Ruling T20-b (L4608, L4630): the ten adversarial cases are scored and pooled in the metrics, but the
    30 of the gate are the user's: 20 of them leave the run uncalibrated."""
    await seed_adversarial(engine)
    await fake_eval_answers(engine, fake_llm)
    await graded_cases(engine, fake_llm, 20)
    _, run = await golden_run(engine, fake_llm, current_epoch)
    assert (run.status, run.passed, run.metrics["gate"]) == ("completed", None, "uncalibrated")
    assert run.metrics["cases"]["active"] == 30 and run.metrics["observations"]["primary"] == 30


async def test_the_calibration_commits_with_the_operations_end(
    engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The metrics, `passed` and the alert are written in the transaction of the operation's end: a crash
    before its commit leaves none of them."""
    await graded_cases(engine, fake_llm)
    started = await start_eval(engine)
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE eval_runs SET repetitions = 1 WHERE id = :r"), {"r": started.eval_run_id}
        )
    monkeypatch.setattr(eval_runner, "afault_point", raising_at(eval_runner.BEFORE_TERMINAL_COMMIT))
    await execute(engine, started.operation_id, eval_deps(engine, fake_llm), epoch=current_epoch)
    run = await run_row(engine, started.eval_run_id)
    assert (run.status, run.passed, run.metrics) == ("running", None, {"transport_requested": "fake"})
    assert await alerts(engine, "eval.gate_failed") == []
    assert (await operation_row(engine, started.operation_id)).status != "succeeded"


# ---- eval_gate_state (B R14, Deviation 29) -----------------------------------------------------------------


_ACTIVE: Final = sa.text(
    "SELECT rubric_version_id, prompt_schema_version_id, model_policy_id, evaluation_policy_id, "
    "execution_config_id FROM active_config"
)


async def finished_run(
    engine: AsyncEngine,
    ids: Any,
    *,
    passed: bool | None,
    transport: str = "live",
    kind: str = "golden",
    route: str = "production",
    status: str = "completed",
    **overrides: int,
) -> int:
    """An ended eval run of the candidate `ids` (an `active_config` row's five ids, `overrides` replacing
    some), as the runner leaves it."""
    async with engine.begin() as conn:
        return await insert_id(
            conn,
            "eval_runs",
            {
                "kind": kind,
                "route": route,
                "rubric_version_id": ids.rubric_version_id,
                "prompt_schema_version_id": ids.prompt_schema_version_id,
                "model_policy_id": ids.model_policy_id,
                "evaluation_policy_id": ids.evaluation_policy_id,
                "execution_config_id": ids.execution_config_id,
                "repetitions": 2,
                "status": status,
                "passed": passed,
                "metrics": {"transport": transport, "transport_requested": transport},
                "finished_at": None if status == "running" else utcnow(),
                **overrides,
            },
        )


async def gate_state(engine: AsyncEngine, candidate: validate.Candidate | None = None) -> str:
    async with engine.connect() as conn:
        return await validate.eval_gate_state(conn, candidate=candidate)


async def test_the_gate_reads_the_candidates_newest_live_run(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with engine.connect() as conn:
        ids = (await conn.execute(_ACTIVE)).one()
    await finished_run(engine, ids, passed=True)
    await seed_adversarial(engine)  # ten active adversarial cases: pooled in the metrics, never counted here
    made = (await insert_cases(engine, 20))[-20:]  # the user's: `insert_cases` lists every case
    assert await gate_state(engine) == "uncalibrated"  # 20 of the user's cases and 10 adversarial (T20-b)
    await insert_cases(engine, 9)
    assert await gate_state(engine) == "uncalibrated"  # fewer than 30 of the user's cases (L4631)
    await insert_cases(engine, 1)
    assert await gate_state(engine) == "passed"  # the active configuration's passed live run
    async with engine.begin() as conn:
        await conn.execute(sa.text("UPDATE golden_cases SET active = false WHERE id = :g"), {"g": made[0].id})
    assert await gate_state(engine) == "uncalibrated"  # inactive cases do not count
    async with engine.begin() as conn:
        await conn.execute(sa.text("UPDATE golden_cases SET active = true"))
    await finished_run(engine, ids, passed=False)
    assert await gate_state(engine) == "failed"  # the newest run decides
    # runs that decide nothing: a fake one in a live process, a diagnostic, a canary, a running one, one
    # never gated (uncalibrated at its end), and one of another evaluation policy
    other_policy = await activate_other_policy(engine)
    await finished_run(engine, ids, passed=True, transport="fake")
    await finished_run(engine, ids, passed=True, route="forced_critic")
    await finished_run(engine, ids, passed=True, kind="canary")
    await finished_run(engine, ids, passed=True, status="running")
    await finished_run(engine, ids, passed=True, evaluation_policy_id=other_policy)
    assert await gate_state(engine, await active_gate_candidate(engine, ids)) == "failed"
    monkeypatch.setenv("JOBHUNTER_FAKE_LLM", "1")  # on the fake, a fake run is evidence (Codex r2 B20)
    assert await gate_state(engine, await active_gate_candidate(engine, ids)) == "passed"
    monkeypatch.delenv("JOBHUNTER_FAKE_LLM")
    await finished_run(engine, ids, passed=None)  # a run that was uncalibrated at its end passes nothing
    assert await gate_state(engine, await active_gate_candidate(engine, ids)) == "failed"


async def activate_other_policy(engine: AsyncEngine) -> int:
    """Another evaluation policy row (not activated): the one of `t2_band = 3`."""
    async with engine.begin() as conn:
        policy = await activate_test_policy(conn, t2_band=3)
        await activate_test_policy(conn)  # the test's own again
        return policy


async def active_gate_candidate(engine: AsyncEngine, ids: Any) -> validate.Candidate:
    """The active configuration's candidate as `eval_gate_state` takes it: its policies by hash."""
    async with engine.connect() as conn:
        hashes = (
            await conn.execute(
                sa.text(
                    "SELECT m.policy_hash AS mp, e.policy_hash AS ep FROM model_policies m, "
                    "evaluation_policies e WHERE m.id = :m AND e.id = :e"
                ),
                {"m": ids.model_policy_id, "e": ids.evaluation_policy_id},
            )
        ).one()
    return validate.Candidate(
        int(ids.rubric_version_id), int(ids.prompt_schema_version_id), bytes(hashes.mp), bytes(hashes.ep)
    )


async def test_a_payloads_candidate_is_its_policies_by_hash(engine: AsyncEngine) -> None:
    """The dry-run's and the save's candidate: the active rubric and prompt-schema pointers and the content
    hashes of the payload's model policy and evaluation blocks, whether or not their rows exist yet."""
    doc = load_settings_v1()
    doc["evaluation"] = {**doc["evaluation"], "t2_band": 7}  # type: ignore[dict-item]
    settings = Settings.model_validate(doc)
    async with engine.connect() as conn:
        ids = (await conn.execute(_ACTIVE)).one()
        made = await candidate_of(conn, settings)
    d = derived(settings)
    assert made == validate.Candidate(
        int(ids.rubric_version_id),
        int(ids.prompt_schema_version_id),
        content_hash(d.model_policy),
        content_hash(d.evaluation),
    )


# ---- the settings save and dry-run (§11.13 "What is gated") ------------------------------------------------


def gated_payload(base: dict[str, Any], **evaluation: JSONValue) -> dict[str, Any]:
    """`base` with an evaluation change (class `evaluation_policy`, gated); T8 off as the test policy's."""
    payload: dict[str, Any] = json.loads(json.dumps(base))
    payload["evaluation"] = {**payload["evaluation"], "t8_sample_pct": 0, **evaluation}
    return payload


async def post_save(client: httpx.AsyncClient, payload: dict[str, Any], **extra: Any) -> httpx.Response:
    active = await settings_body(client)
    body = {
        "payload": payload,
        "base_version_id": active["version"]["id"],
        "acknowledged_warnings": await warning_codes(client, payload),
        **extra,
    }
    return await client.post(SETTINGS_URL, json=body, headers=idem(fresh_key()))


async def dry_run_body(client: httpx.AsyncClient, payload: dict[str, Any]) -> dict[str, Any]:
    r = await client.post(DRY_RUN_URL, json={"payload": payload})
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


async def test_a_gated_save_needs_a_passed_run_of_its_candidate(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    fake_llm: FakeLLM,
    current_epoch: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User decision "Eval gate = fail until a run": with 30 active cases and no run of the candidate, a gated
    save is 409 `eval_gate_failed` unless `eval_override_ack`; a passed run of the candidate (staged from the
    payload, Codex r2 B21) lets it save."""
    monkeypatch.setenv("JOBHUNTER_FAKE_LLM", "1")  # the run below is a fake one
    await graded_cases(engine, fake_llm)
    payload = gated_payload((await settings_body(logged_in_client))["payload"], t2_band=7)
    r = await post_save(logged_in_client, payload)
    assert (r.status_code, r.json()["code"]) == (409, "eval_gate_failed")
    assert (await dry_run_body(logged_in_client, payload))["projections"]["eval_gate"] == "failed"

    async with engine.begin() as conn:
        candidate = await eval_runner.stage_candidate(conn, payload, now=utcnow())
    _, run = await golden_run(engine, fake_llm, current_epoch, candidate=candidate)
    assert (run.passed, run.evaluation_policy_id) == (True, candidate.evaluation_policy_id)
    assert (await dry_run_body(logged_in_client, payload))["projections"]["eval_gate"] == "passed"
    monkeypatch.delenv("JOBHUNTER_FAKE_LLM")  # a live process: the fake run is no evidence
    r = await post_save(logged_in_client, payload)
    assert (r.status_code, r.json()["code"]) == (409, "eval_gate_failed")
    monkeypatch.setenv("JOBHUNTER_FAKE_LLM", "1")
    r = await post_save(logged_in_client, payload)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    assert version["effect_classes"] == ["evaluation_policy"]
    async with engine.connect() as conn:
        saved = (
            await conn.execute(
                sa.text(
                    "SELECT v.eval_override_ack, a.evaluation_policy_id FROM settings_versions v, "
                    "active_config a WHERE v.id = :v"
                ),
                {"v": version["id"]},
            )
        ).one()
    assert (saved.eval_override_ack, saved.evaluation_policy_id) == (False, candidate.evaluation_policy_id)

    other = gated_payload(payload, t2_band=8)  # no run of this candidate: only the override saves it
    assert (await post_save(logged_in_client, other)).status_code == 409
    r = await post_save(logged_in_client, other, eval_override_ack=True)
    assert r.status_code == 201, r.text


async def test_the_generator_effort_low_rule_reads_the_payloads_gate(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, fake_llm: FakeLLM
) -> None:
    """§11.13, L3827, Ruling T20-d: below 30 cases the gate is uncalibrated and M2's warning applies; from
    30 cases with no run of the candidate it is failed, and selecting generator effort `low` is refused, while
    a save that carries an already-active `low` unchanged is allowed with the warning `eval_failed`."""
    payload = json.loads(json.dumps((await settings_body(logged_in_client))["payload"]))
    payload["scoring"]["model_policy"]["nodes"]["generate"]["effort"] = "low"
    made = await insert_cases(engine, 29)
    body = await dry_run_body(logged_in_client, payload)
    assert body["errors"] == [] and body["projections"]["eval_gate"] == "uncalibrated"
    assert {"code": "eval_uncalibrated", "detail": {"node": "generate"}} in body["warnings"]
    await insert_cases(engine, 1)
    body = await dry_run_body(logged_in_client, payload)
    assert [(e["field"], e["code"]) for e in body["errors"]] == [
        ("payload.scoring.model_policy.nodes.generate.effort", "conflict")
    ]
    # `low` selected while the gate was uncalibrated (29 active cases, the policy's models verified)...
    await dry_run_record(engine, await active_version(engine), POLICY_B_CASES)
    async with engine.begin() as conn:
        await conn.execute(sa.text("UPDATE golden_cases SET active = false WHERE id = :g"), {"g": made[0].id})
    assert (await post_save(logged_in_client, payload)).status_code == 201
    async with engine.begin() as conn:
        await conn.execute(sa.text("UPDATE golden_cases SET active = true"))
    # ...then kept unchanged once the gate has failed: a schedule save carries it, with the warning
    kept = json.loads(json.dumps((await settings_body(logged_in_client))["payload"]))
    kept["schedule"]["interval_hours"] = 12
    body = await dry_run_body(logged_in_client, kept)
    assert (
        body["errors"] == [] and {"code": "eval_failed", "detail": {"node": "generate"}} in body["warnings"]
    )
    r = await post_save(logged_in_client, kept)
    assert r.status_code == 201, r.text
    assert r.json()["version"]["effect_classes"] == ["schedule"]


async def active_version(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(_ACTIVE)).one().prompt_schema_version_id)


# ---- the schema verification at the save (Ruling P11, Codex r1 #25, r2 B24) --------------------------------


def case(schema: str, model: str) -> dict[str, Any]:
    return {"name": f"{schema}/{model}", "schema": schema, "model": model, "passed": True}


POLICY_B_CASES: Final = [
    case("generator", SONNET),
    case("critic", OPUS),
    case("profile", OPUS),
    case("queries", OPUS),
]


async def dry_run_record(engine: AsyncEngine, version: int, cases: list[dict[str, Any]]) -> None:
    """A completed live schema dry-run of prompt-schema version `version` with `cases`."""
    async with engine.begin() as conn:
        await insert_id(
            conn,
            "eval_runs",
            {
                "kind": "schema_dry_run",
                "route": "production",
                "prompt_schema_version_id": version,
                "status": "completed",
                "passed": True,
                "metrics": {"transport": "live", "cases": cases},
                "finished_at": utcnow(),
            },
        )


async def model_policies(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(sa.text("SELECT count(*) FROM model_policies"))).scalar_one())


async def test_a_model_policy_save_needs_a_verified_policy(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Ruling T20-a: the save finds or inserts the proposed policy's row by hash, then asks Task 11's
    `schema_verified`. A new policy that reuses verified models saves with no new run; one that moves
    `revise` to a model with no passed live case of the active prompt-schema version is 409 `conflict_state`
    (`schema_dry_run_required`, its title naming the pair), whatever the override, also with a pass of another
    version only, and its row goes with the refused save; a live pass of the version unblocks it."""
    base = json.loads(json.dumps((await settings_body(logged_in_client))["payload"]))
    async with engine.connect() as conn:
        ids = (await conn.execute(_ACTIVE)).one()
        other_version = int(
            (
                await conn.execute(
                    sa.text("SELECT id FROM prompt_schema_versions WHERE id <> :v ORDER BY id LIMIT 1"),
                    {"v": ids.prompt_schema_version_id},
                )
            ).scalar_one()
        )
    version = int(ids.prompt_schema_version_id)
    await dry_run_record(engine, version, POLICY_B_CASES)
    effort_only = json.loads(json.dumps(base))
    effort_only["scoring"]["model_policy"]["nodes"]["critic"]["effort"] = "high"
    r = await post_save(logged_in_client, effort_only)  # a new row, its models verified: no new run needed
    assert r.status_code == 201, r.text
    assert r.json()["version"]["effect_classes"] == ["llm_tuple"]

    payload = json.loads(json.dumps(base))
    payload["scoring"]["model_policy"]["preset"] = "custom"
    payload["scoring"]["model_policy"]["nodes"]["revise"]["model"] = HAIKU

    async def refused() -> None:
        before = await model_policies(engine)
        r = await post_save(logged_in_client, payload, eval_override_ack=True)
        body = r.json()
        assert (r.status_code, body["code"], body["detail"]) == (
            409,
            "conflict_state",
            "schema_dry_run_required",
        ), r.text
        assert body["title"] == f"The model policy has no passed live schema dry-run for generator on {HAIKU}"
        assert await model_policies(engine) == before  # the row inserted for the check is rolled back

    await refused()
    await dry_run_record(engine, other_version, [case("generator", HAIKU)])
    await refused()  # a pass of another version (Codex r1 #25)
    async with engine.connect() as conn:  # nothing was written by the refusals
        assert (await conn.execute(sa.text("SELECT count(*) FROM settings_versions"))).scalar_one() == 2
    await dry_run_record(engine, version, [case("generator", HAIKU)])
    r = await post_save(logged_in_client, payload)
    assert r.status_code == 201, r.text
    assert r.json()["version"]["effect_classes"] == ["llm_tuple"]


async def test_a_model_policy_save_needs_a_passed_eval_of_its_candidate(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """The `llm_tuple` class is gated like the `evaluation_policy` class (§11.13): with 30 of the user's
    cases, a verified policy with no passed run of its candidate is 409 `eval_gate_failed` (its row rolled
    back) unless the override is acknowledged."""
    await insert_cases(engine, 30)
    await dry_run_record(engine, await active_version(engine), POLICY_B_CASES)
    payload = json.loads(json.dumps((await settings_body(logged_in_client))["payload"]))
    payload["scoring"]["model_policy"]["nodes"]["generate"]["effort"] = "high"
    before = await model_policies(engine)
    r = await post_save(logged_in_client, payload)
    assert (r.status_code, r.json()["code"]) == (409, "eval_gate_failed"), r.text
    assert await model_policies(engine) == before
    r = await post_save(logged_in_client, payload, eval_override_ack=True)
    assert r.status_code == 201, r.text
    assert r.json()["version"]["effect_classes"] == ["llm_tuple"]


async def test_a_fake_process_saves_a_model_policy_without_a_live_dry_run(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, fake_llm_process: None
) -> None:
    """On the fake no live dry-run ever exists (the dev stack, the tests): the verification is skipped there,
    as the score lane's guard skips it (`cycles.pause_reason`)."""
    payload = json.loads(json.dumps((await settings_body(logged_in_client))["payload"]))
    payload["scoring"]["model_policy"]["nodes"]["generate"]["effort"] = "high"
    r = await post_save(logged_in_client, payload)
    assert r.status_code == 201, r.text


# ---- the eval run CLI's refusals (Codex r1 #34, carry T19-e) -----------------------------------------------


async def counts(engine: AsyncEngine) -> tuple[int, int]:
    async with engine.connect() as conn:
        runs = (await conn.execute(sa.text("SELECT count(*) FROM eval_runs"))).scalar_one()
        operations = (await conn.execute(sa.text("SELECT count(*) FROM operations"))).scalar_one()
    return int(runs), int(operations)


async def cli_start(cfg: Any, candidate: eval_runner.Candidate | None) -> eval_runner.StartedRun:
    """`jobhunter eval run --kind golden --fake`'s start (`cli_golden.start`; its exit statuses are
    `tests/unit/test_cli_golden.py`'s)."""
    return await cli_golden.start(
        cfg,
        kind="golden",
        route="production",
        candidate=candidate,
        payload=None,
        transport="fake",
        now=utcnow(),
    )


async def test_eval_run_refuses_a_candidate_id_that_does_not_resolve_before_any_call(
    engine: AsyncEngine, fake_llm: FakeLLM, make_config: ConfigFactory
) -> None:
    await insert_cases(engine, 2)
    async with engine.connect() as conn:
        ids = (await conn.execute(_ACTIVE)).one()
    unknown = eval_runner.Candidate(
        int(ids.rubric_version_id),
        int(ids.prompt_schema_version_id),
        int(ids.model_policy_id),
        int(ids.evaluation_policy_id),
        987654321,
    )
    with pytest.raises(eval_runner.NoCandidate, match=r"^execution config 987654321 does not exist$"):
        await cli_start(make_config("worker"), unknown)
    assert await counts(engine) == (0, 0) and fake_llm.served == 0


async def test_eval_run_refuses_a_golden_run_without_an_active_case_before_any_call(
    engine: AsyncEngine, fake_llm: FakeLLM, make_config: ConfigFactory
) -> None:
    made = await insert_cases(engine, 1)
    async with engine.begin() as conn:
        await conn.execute(sa.text("UPDATE golden_cases SET active = false WHERE id = :g"), {"g": made[0].id})
    with pytest.raises(eval_runner.NoActiveCases) as refused:
        await cli_start(make_config("worker"), None)
    assert refused.value.code == "eval_no_cases"
    assert await counts(engine) == (0, 0) and fake_llm.served == 0


# ---- the schema dry-run of a candidate policy (Codex r2 B21, r3 B7) ----------------------------------------

_DRY_RUNS: Final = sa.text("SELECT model_policy_id FROM eval_runs WHERE kind = 'schema_dry_run' ORDER BY id")


async def scalars(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(statement, params)).scalars())


async def test_the_schema_dry_run_runs_a_candidate_payloads_policy(
    engine: AsyncEngine, make_config: ConfigFactory
) -> None:
    """`--candidate-payload`: the document validated as the settings dry-run validates it, its policy row
    inserted by hash under `active_config FOR UPDATE` with no settings version and no activation, and its
    cases run; `--model-policy`: an existing policy's cases; an unknown id or an invalid document starts
    nothing (the exit statuses are `tests/unit/test_cli_schema_dry_run.py`'s)."""
    cfg = make_config("worker", JOBHUNTER_FAKE_LLM="1", JOBHUNTER_FAKE_LLM_DIR=str(FAKE_LLM_DIR))
    doc: dict[str, Any] = load_settings_v1()
    doc["scoring"]["model_policy"]["nodes"]["critic"]["effort"] = "high"
    async with engine.connect() as conn:
        before = (await conn.execute(_ACTIVE)).one()
    await cli_schema_dry_run._dry_run(cfg, transport="fake", payload=doc)
    digest = content_hash(derived(Settings.model_validate(doc)).model_policy)
    [staged] = await scalars(
        engine, sa.text("SELECT id FROM model_policies WHERE policy_hash = :h"), h=digest
    )
    async with engine.connect() as conn:
        assert (await conn.execute(_ACTIVE)).one() == before  # nothing activated
        assert (await conn.execute(sa.text("SELECT count(*) FROM settings_versions"))).scalar_one() == 1
    await cli_schema_dry_run._dry_run(cfg, transport="fake", model_policy_id=int(before.model_policy_id))
    assert await scalars(engine, _DRY_RUNS) == [staged, before.model_policy_id]

    with pytest.raises(eval_runner.NoCandidate, match=r"^model policy 987654321 does not exist$"):
        await cli_schema_dry_run._dry_run(cfg, transport="fake", model_policy_id=987654321)
    bad = gated_payload(doc, t2_band=99)
    with pytest.raises(eval_runner.CandidateInvalid):
        await cli_schema_dry_run._dry_run(cfg, transport="fake", payload=bad)
    assert len(await scalars(engine, _DRY_RUNS)) == 2  # neither started a run
