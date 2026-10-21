"""The schema dry-run (spec §10.11; plan Task 7) on the recorded-SSE fake, with the V4 recordings.

The fake replays V4's live answers (`docs/spikes/V4.md`; controller rulings R7-1 to R7-3), committed under
`tests/contract/fixtures/anthropic/`:
- `schema_dry_run/claude-sonnet-5-5/<generator schema12>`: the API's 400 `invalid_request_error` for the §11.5
  generator schema ("The compiled grammar is too large …"), its status and error body only;
- `schema_dry_run/claude-opus-5-5/<schema12>`: the recorded streams of the critic, profile and queries cases;
- `count_tokens/claude-sonnet-5-5/<probe schema12>.json`: the counting endpoint's 400 for the informational
  `minimum` probe, which is never a paid message.
The real gateway runs above the fake, so each case reserves, dispatches and settles as a live run does. On
these answers the default run is the correct M2 state of prompt-schema version 1: `completed`, not passed,
with the generator case rejected.
"""

import asyncio
import copy
import dataclasses
import json
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Final

import anthropic
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.seed import load_settings_v1
from jobhunter.llm import fake, schema_dry_run
from jobhunter.llm.cache_monitor import CacheMonitor
from jobhunter.llm.gateway import LLMGateway
from jobhunter.llm.schema_dry_run import DryRunCase
from jobhunter.llm.schemas import SCHEMAS, transformed
from jobhunter.llm.schemas.generator import GeneratorOut
from jobhunter.types import JSONValue, get_object
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.llm import FAKE_LLM_DIR, FakeAnthropic, fake_llm_config, synthetic_sse
from tests.support.pg import PgServer

SONNET: Final = "claude-sonnet-5-5"
OPUS: Final = "claude-opus-5-5"
HAIKU: Final = "claude-haiku-5-5"
SEEDED_POLICY = get_object(load_settings_v1(), "scoring.model_policy")
GRAMMAR_TOO_LARGE: Final = (
    "The compiled grammar is too large, which would cause performance issues. Simplify your tool schemas or "
    "reduce the number of strict tools."
)
MINIMUM_NOT_SUPPORTED: Final = (
    "output_config.format.schema: For 'integer' type, property 'minimum' is not supported"
)
GATING: Final = ("generator", "critic", "profile", "queries")

_ACTIVE = sa.text(
    "SELECT prompt_schema_version_id, model_policy_id, execution_config_id FROM active_config WHERE id = 1"
)
_EVAL_RUN = sa.text(
    "SELECT kind, route, status, passed, metrics, cost_usd, prompt_schema_version_id, model_policy_id, "
    "execution_config_id, finished_at FROM eval_runs WHERE id = :id"
)
_CALLS = sa.text(
    "SELECT c.purpose, c.eval_run_id, c.model_requested, c.effort, c.max_tokens, c.status, c.http_status, "
    "c.stop_reason, c.error_code, c.input_refs, l.purpose_class, l.eval_run, l.state, l.settled_usd, "
    "l.reserved_usd FROM llm_calls c JOIN llm_spend_ledger l ON l.id = c.spend_ledger_id ORDER BY c.id"
)


async def no_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


@dataclasses.dataclass
class DryRun:
    """Runs the dry-run of the active configuration on a gateway over `fake_llm`."""

    engine: AsyncEngine
    fake_llm: FakeAnthropic
    ids: dict[str, int]

    async def __call__(self, **ids: int) -> schema_dry_run.DryRunReport:
        async with self.fake_llm.client() as client:
            gateway = LLMGateway(
                self.engine, self.fake_llm.cfg, http_client=client, sleep=no_sleep, cache=CacheMonitor()
            )
            return await schema_dry_run.run(self.engine, gateway, **{**self.ids, **ids}, now=utcnow())


async def active_ids(engine: AsyncEngine) -> dict[str, int]:
    async with engine.connect() as conn:
        row = (await conn.execute(_ACTIVE)).one()
    return {
        "prompt_schema_version_id": int(row.prompt_schema_version_id),
        "model_policy_id": int(row.model_policy_id),
        "execution_config_id": int(row.execution_config_id),
    }


@pytest.fixture
async def dry_run(engine: AsyncEngine, fake_anthropic: FakeAnthropic) -> AsyncIterator[DryRun]:
    yield DryRun(engine, fake_anthropic, await active_ids(engine))


def copied_recordings(tmp_path: Path) -> Path:
    """A copy of the committed V4 answers, for a test that adds or replaces one."""
    root = tmp_path / "anthropic"
    for directory in ("schema_dry_run", fake.COUNT_DIR):
        shutil.copytree(FAKE_LLM_DIR / directory, root / directory)
    return root


async def eval_run(engine: AsyncEngine, eval_run_id: int) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(_EVAL_RUN, {"id": eval_run_id})).one()


async def calls(engine: AsyncEngine) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(_CALLS)).all())


def outcomes(report: schema_dry_run.DryRunReport) -> dict[str, str]:
    """The gating cases' outcomes by schema."""
    return {r.case.schema_name: r.outcome for r in report.results if not r.case.probe}


def canonical(schema: Any) -> bytes:
    return fake.canonical_schema_json(schema).encode("utf-8")


# ---- the cases --------------------------------------------------------------------------------------------


def test_the_dry_run_constants() -> None:
    assert schema_dry_run.DRY_RUN_EFFORT == "low"
    assert schema_dry_run.DRY_RUN_MAX_TOKENS == 2000
    assert schema_dry_run.DRY_RUN_DEADLINE_S == 300


def test_the_default_policy_yields_the_four_gating_schemas_and_the_probe() -> None:
    assert schema_dry_run.cases(SEEDED_POLICY) == [
        DryRunCase("generator", SONNET),
        DryRunCase("critic", OPUS),
        DryRunCase("profile", OPUS),
        DryRunCase("queries", OPUS),
        DryRunCase("generator", SONNET, probe=True),
    ]


def _policy(**models: str) -> dict[str, JSONValue]:
    policy = copy.deepcopy(dict(SEEDED_POLICY))
    nodes = get_object(policy, "nodes")
    for node, model in models.items():
        nodes[node] = {**get_object(nodes, node), "model": model}
    return policy


def test_each_distinct_model_of_a_schema_is_one_case() -> None:
    # the generator schema serves `generate` and `revise`: two models are two cases; the probe stays on the
    # `generate` model
    policy = _policy(revise=OPUS, critic=HAIKU)
    assert schema_dry_run.cases(policy) == [
        DryRunCase("generator", SONNET),
        DryRunCase("generator", OPUS),
        DryRunCase("critic", HAIKU),
        DryRunCase("profile", OPUS),
        DryRunCase("queries", OPUS),
        DryRunCase("generator", SONNET, probe=True),
    ]
    with pytest.raises(ValueError, match="model"):
        schema_dry_run.cases(_policy(critic="claude-unknown"))


def test_the_probe_schema_is_the_generator_schema_with_one_added_minimum() -> None:
    probe = schema_dry_run.probe_schema()
    generator = transformed(SCHEMAS["generator"])
    assert probe != generator
    assert probe["$defs"]["nullable_int"]["anyOf"][0] == {"type": "integer", "minimum": 0}
    probe["$defs"]["nullable_int"]["anyOf"][0].pop("minimum")
    assert probe == generator  # nothing else differs
    assert schema_dry_run.probe_schema() != probe  # each call gives its own copy


# ---- the default run on V4's answers ----------------------------------------------------------------------


async def test_the_default_run_completes_unpassed_with_the_generator_rejected(
    dry_run: DryRun, engine: AsyncEngine
) -> None:
    report = await dry_run()
    assert report.status == "completed" and report.passed is False
    assert outcomes(report) == {
        "generator": "http_400",
        "critic": "passed",
        "profile": "passed",
        "queries": "passed",
    }
    row = await eval_run(engine, report.eval_run_id)
    assert (row.kind, row.route, row.status, row.passed) == (
        "schema_dry_run",
        "production",
        "completed",
        False,
    )
    assert row.finished_at is not None
    assert (row.prompt_schema_version_id, row.model_policy_id, row.execution_config_id) == tuple(
        dry_run.ids.values()
    )
    rows = await calls(engine)
    assert len(rows) == 4 and fake.state().served == 4  # the probe is no message
    for call in rows:
        assert (call.purpose, call.eval_run_id, call.purpose_class, call.eval_run) == (
            "schema_dry_run",
            report.eval_run_id,
            "nonscoring",
            True,
        )
        assert call.input_refs["owner"] == {"kind": "eval_run", "id": report.eval_run_id}
    generator, *others = rows
    # the 400 is an error response without content: not billed, not retried
    assert (generator.status, generator.http_status, generator.error_code, generator.state) == (
        "error",
        400,
        "http_400",
        "not_billed",
    )
    for call in others:
        assert (call.status, call.http_status, call.stop_reason, call.state) == (
            "completed",
            200,
            "end_turn",
            "billed",
        )
    # the cost is what the ledger counts for the run's calls, at the column's 4 decimals
    counted = sum((c.settled_usd for c in others), Decimal(0))
    assert report.cost_usd == counted > 0
    assert row.cost_usd == counted.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    # the metrics name the cases, their outcomes and checks, never content
    cases = row.metrics["cases"]
    assert [(c["schema"], c["model"], c["outcome"]) for c in cases] == [
        ("generator", SONNET, "http_400"),
        ("critic", OPUS, "passed"),
        ("profile", OPUS, "passed"),
        ("queries", OPUS, "passed"),
    ]
    passing = {"http_200": True, "end_turn": True, "structural": True, "schema_bytes_equal": True}
    for case in cases[1:]:
        assert case["checks"] == {**passing, "enums_allowed": True}
    # the generator schema was sent as stored: the API refused it, nothing departed from the version
    assert cases[0]["checks"] == {
        "http_200": False,
        "end_turn": False,
        "structural": False,
        "schema_bytes_equal": True,
        "enums_allowed": False,
    }
    assert (row.metrics["cases_passed"], row.metrics["cases_total"]) == (3, 4)
    assert "Beispiel" not in json.dumps(row.metrics)


async def test_the_probe_is_answered_by_the_counting_endpoint_and_never_gates(
    dry_run: DryRun, engine: AsyncEngine
) -> None:
    report = await dry_run()
    probe = report.probe
    assert probe is not None and probe.case == DryRunCase("generator", SONNET, probe=True)
    assert (probe.outcome, probe.http_status, probe.passed, probe.llm_call_id) == (
        "rejected",
        400,
        None,
        None,
    )
    assert fake.state().count_tokens_requests == 1
    row = await eval_run(engine, report.eval_run_id)
    assert row.metrics["probe"] == {
        "schema": "generator",
        "model": SONNET,
        "keyword": "minimum",
        "endpoint": "messages.count_tokens",
        "outcome": "rejected",
        "http_status": 400,
        "keyword_sent": True,
    }
    assert len(await calls(engine)) == 4  # nothing reserved for the probe
    # the committed count answer is the API's own text
    answer_path = fake.count_recording_path(FAKE_LLM_DIR, SONNET, schema_dry_run.probe_schema())
    answer = json.loads(answer_path.read_text("utf-8"))
    assert answer == {
        "status": 400,
        "body": {
            "type": "error",
            "error": {"type": "invalid_request_error", "message": MINIMUM_NOT_SUPPORTED},
        },
    }


async def test_an_accepting_or_failing_count_is_recorded_as_the_probe_outcome(dry_run: DryRun) -> None:
    fake.count_tokens(4_321)  # an answer set for the test wins over the recorded one
    assert (await dry_run()).probe.outcome == "accepted"  # type: ignore[union-attr]
    fake.count_tokens("error")
    report = await dry_run()
    assert report.probe is not None and report.probe.outcome == "error:http_500"
    assert outcomes(report)["critic"] == "passed"


async def test_every_message_call_sends_max_tokens_2000_effort_low_and_its_stored_schema(
    dry_run: DryRun, engine: AsyncEngine
) -> None:
    await dry_run()
    requests = fake.state().requests
    assert len(requests) == 4
    for served in requests:
        assert served.purpose == "schema_dry_run"
        assert served.max_tokens == 2000 == served.body["max_tokens"]
        assert served.body["output_config"]["effort"] == "low"
        assert served.body["stream"] is True
    assert [r.body["output_config"]["format"]["schema"] for r in requests] == [
        transformed(SCHEMAS[name]) for name in GATING
    ]
    for call in await calls(engine):
        assert (call.max_tokens, call.effort) == (2000, "low")


def _generator_body() -> dict[str, Any]:
    """A synthetic answer that validates as the generator mirror."""
    quoted: dict[str, Any] = {"evidence": []}
    criterion = {"level": 3, "basis": "observed", "ad_evidence": [], "profile_evidence": [], "rationale": "x"}
    body = {
        "not_a_job_ad": False,
        "injection_observed": False,
        "job_facts": {
            "work_mode": {"value": "hybrid", **quoted},
            "onsite_only": {"asserted": False, **quoted},
            "remote_region": {"value": "unspecified", "countries": [], **quoted},
            "required_languages": [],
            "must_haves": [],
            "nice_to_haves": [],
            "seniority": {"value": "senior", **quoted},
            "min_years_experience": {"value": 4, **quoted},
            "industry": {"value": None, **quoted},
            "employment_types": {"values": ["full_time"], **quoted},
            "degree": {
                "level": "unspecified",
                "mandatory": False,
                "or_equivalent": False,
                "field": None,
                **quoted,
            },
            "certifications": [],
            "security_clearance": {"required": False, "level": None, **quoted},
            "work_permit": {"required": False, **quoted},
        },
        "criteria": {
            name: criterion
            for name in (
                "role_fit",
                "skills",
                "experience_seniority",
                "domain",
                "languages",
                "education_certifications",
            )
        },
        "summary_en": "Synthetic.",
        "summary_de": "Synthetisch.",
    }
    GeneratorOut.model_validate(body)
    return body


async def test_a_run_whose_gating_cases_all_pass_completes_passed_whatever_the_probe(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    # A generator answer that the API would give for a compilable schema (synthetic, in a copy): every gating
    # case passes, and the probe's rejection does not change that.
    root = copied_recordings(tmp_path)
    generator = fake.recording_path(root, "schema_dry_run", SONNET, transformed(SCHEMAS["generator"]))
    generator.write_bytes(synthetic_sse(_generator_body(), model=SONNET))
    generator.with_suffix(".headers.json").unlink()
    report = await DryRun(
        engine, FakeAnthropic(fake_llm_config(root, tmp_path / "s")), await active_ids(engine)
    )()
    assert outcomes(report) == dict.fromkeys(GATING, "passed")
    assert report.probe is not None and report.probe.outcome == "rejected"
    assert (report.status, report.passed) == ("completed", True)
    row = await eval_run(engine, report.eval_run_id)
    assert (row.status, row.passed, row.metrics["cases_passed"]) == ("completed", True, 4)


async def test_a_stripped_enum_in_a_recorded_response_fails_its_case(
    dry_run: DryRun, engine: AsyncEngine
) -> None:
    fake.scenario("ok", "enum_stripped", "ok")  # the second try (the critic case) holds an out-of-enum value
    report = await dry_run()
    assert outcomes(report) == {
        "generator": "http_400",
        "critic": "invalid_output",
        "profile": "passed",
        "queries": "passed",
    }
    assert report.passed is False
    row = await eval_run(engine, report.eval_run_id)
    assert (row.status, row.passed, row.metrics["cases_passed"]) == ("completed", False, 2)


def _one_byte_off(schema: dict[str, Any]) -> dict[str, Any]:
    """The critic schema with one byte changed: the enum value `none` of `injection` becomes `nonE`."""
    changed = copy.deepcopy(schema)
    enum = changed["properties"]["injection"]["enum"]
    enum[enum.index("none")] = "nonE"
    return changed


async def test_a_sent_schema_one_byte_off_fails_its_case(
    engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    critic = transformed(SCHEMAS["critic"])
    changed = _one_byte_off(critic)
    assert len(canonical(changed)) == len(canonical(critic))
    assert sum(a != b for a, b in zip(canonical(changed), canonical(critic), strict=True)) == 1
    # The SDK sends `transform_schema(model)`: from here on it sends the critic schema one byte off. The fake
    # answers it with the recorded critic stream, so only the sent schema differs.
    root = copied_recordings(tmp_path)
    recorded = fake.recording_path(root, "schema_dry_run", OPUS, critic)
    shutil.copy(recorded, fake.recording_path(root, "schema_dry_run", OPUS, changed))
    original: Callable[..., dict[str, Any]] = anthropic.transform_schema

    def transform(schema: Any) -> dict[str, Any]:
        out = original(schema)
        return _one_byte_off(out) if out == critic else out

    monkeypatch.setattr(anthropic, "transform_schema", transform)
    fake_llm = FakeAnthropic(fake_llm_config(root, tmp_path / "no-secrets"))
    report = await DryRun(engine, fake_llm, await active_ids(engine))()
    results = {r.case.schema_name: r for r in report.results if not r.case.probe}
    assert results["critic"].outcome == "schema_mismatch"
    assert results["critic"].checks["schema_bytes_equal"] is False
    assert results["critic"].checks["structural"] is True
    assert [results[n].outcome for n in ("profile", "queries")] == ["passed", "passed"]
    assert report.passed is False
    assert (await eval_run(engine, report.eval_run_id)).metrics["cases_passed"] == 2


async def test_a_prompt_schema_version_one_byte_off_fails_its_case(
    dry_run: DryRun, engine: AsyncEngine
) -> None:
    async with engine.begin() as conn:
        templates, schemas = (
            await conn.execute(
                sa.text("SELECT templates, schemas FROM prompt_schema_versions WHERE id = :id"),
                {"id": dry_run.ids["prompt_schema_version_id"]},
            )
        ).one()
        schemas["critic"] = _one_byte_off(schemas["critic"])
        version = (
            await conn.execute(
                sa.text(
                    "INSERT INTO prompt_schema_versions (prompt_schema_hash, templates, schemas) "
                    "VALUES (:hash, CAST(:t AS jsonb), CAST(:s AS jsonb)) RETURNING id"
                ),
                {"hash": b"\x07" * 32, "t": json.dumps(templates), "s": json.dumps(schemas)},
            )
        ).scalar_one()
    report = await dry_run(prompt_schema_version_id=int(version))
    assert outcomes(report) == {
        "generator": "http_400",
        "critic": "schema_mismatch",
        "profile": "passed",
        "queries": "passed",
    }
    assert report.passed is False


async def test_a_run_that_aborts_ends_failed_with_the_cases_it_ran(
    dry_run: DryRun, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls_made = 0
    original = LLMGateway.call

    async def call(self: LLMGateway, *args: Any, **kwargs: Any) -> Any:
        nonlocal calls_made
        calls_made += 1
        if calls_made == 3:
            raise RuntimeError("the database went away")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(LLMGateway, "call", call)
    with pytest.raises(RuntimeError):
        await dry_run()
    async with engine.connect() as conn:
        row = (await conn.execute(sa.text("SELECT id FROM eval_runs"))).one()
    stored = await eval_run(engine, int(row.id))
    assert (stored.status, stored.passed) == ("failed", False)
    assert [c["outcome"] for c in stored.metrics["cases"]] == ["http_400", "passed"]


# ---- the CLI ----------------------------------------------------------------------------------------------


def _cli(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "jobhunter.cli", "schema-dry-run", *args],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


async def test_the_cli_runs_on_the_fake_prints_only_the_case_table_and_fails_on_the_generator(
    migrated: PgServer, proc_env: Callable[..., dict[str, str]], engine: AsyncEngine
) -> None:
    env = proc_env("worker", migrated, JOBHUNTER_FAKE_LLM_DIR=str(FAKE_LLM_DIR))
    done = await asyncio.to_thread(_cli, env)
    assert done.returncode == 1, done.stderr  # a gating case failed
    lines = done.stdout.splitlines()
    assert lines[0].split() == ["schema", "model", "result"]
    assert [line.split()[:3] for line in lines[1:5]] == [
        ["generator", SONNET, "http_400"],
        ["critic", OPUS, "passed"],
        ["profile", OPUS, "passed"],
        ["queries", OPUS, "passed"],
    ]
    assert lines[5].split()[:3] == ["probe:minimum", SONNET, "rejected"]
    assert "count_tokens" in lines[5] and "informational" in lines[5]
    assert "status=completed" in lines[6] and "passed=false" in lines[6] and "transport=fake" in lines[6]
    assert len(lines) == 7
    # never content: none of the synthetic inputs or outputs reaches the terminal
    for text in (
        schema_dry_run.SYSTEM_TEXT[:40],
        "Beispiel Logistik",
        '"verdict"',
        "summary",
        GRAMMAR_TOO_LARGE,
    ):
        assert text not in done.stdout + done.stderr
    async with engine.connect() as conn:
        assert (await conn.execute(sa.text("SELECT status, passed FROM eval_runs"))).one() == (
            "completed",
            False,
        )


async def test_live_needs_the_anthropic_key_and_sends_nothing_without_it(
    migrated: PgServer, proc_env: Callable[..., dict[str, str]], engine: AsyncEngine
) -> None:
    done = await asyncio.to_thread(_cli, proc_env("worker", migrated), "--live")
    assert done.returncode == 2
    assert "anthropic_key" in done.stderr
    async with engine.connect() as conn:
        assert (await conn.execute(sa.text("SELECT count(*) FROM eval_runs"))).scalar_one() == 0


async def test_live_refuses_the_fake_llm_mode(
    migrated: PgServer, proc_env: Callable[..., dict[str, str]]
) -> None:
    done = await asyncio.to_thread(_cli, proc_env("worker", migrated, JOBHUNTER_FAKE_LLM="1"), "--live")
    assert done.returncode == 2
    assert "JOBHUNTER_FAKE_LLM" in done.stderr
