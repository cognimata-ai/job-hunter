"""`schema-dry-run` start refusals (spec §10.11; plan Task 7, review fix 7): which configurations the command
refuses before anything runs, prod without `--live` among them (the fake transport is never used in prod).
And the manual workflow's cost note (review fix 5), recomputed from pricing."""

import argparse
import dataclasses
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from jobhunter import cli_schema_dry_run
from jobhunter.cli_schema_dry_run import NOT_STARTED, start_refusal
from jobhunter.config import AppConfig, load_config
from jobhunter.db.seed import load_settings_v1
from jobhunter.llm import pricing, schema_dry_run
from jobhunter.llm.bounds import PROMPT_OVERHEAD_TOKENS, text_input_bound
from jobhunter.types import get_object

PROD_WITHOUT_LIVE = "without --live the dry-run runs on the fake transport, which prod refuses"
WORKFLOW = Path(__file__).parents[2] / ".github/workflows/schema-dry-run-manual.yml"
TRIES_PER_CASE = 6  # 3 transport tries in each of 2 max_tokens stages (gateway)


def config(tmp_path: Path, env: str, *, fake_llm: bool = False, key: bool = False) -> AppConfig:
    environ = {"JOBHUNTER_ENV": env, "JOBHUNTER_DB_NAME": "jobhunter"}
    cfg = load_config("worker", environ, tmp_path / "no-secrets")
    secrets = {"anthropic_key": b"unit-test-key-never-sent"} if key else {}
    return dataclasses.replace(cfg, fake_llm=fake_llm, secrets=MappingProxyType(secrets))


@pytest.mark.parametrize(
    ("env", "fake_llm", "key", "live", "why"),
    [
        pytest.param("prod", False, True, False, PROD_WITHOUT_LIVE, id="prod without --live"),
        pytest.param("prod", False, False, False, PROD_WITHOUT_LIVE, id="prod without --live or key"),
        pytest.param("prod", False, True, True, None, id="prod --live with the key"),
        pytest.param(
            "prod",
            False,
            False,
            True,
            "--live needs the worker's anthropic_key secret",
            id="prod --live without the key",
        ),
        pytest.param(
            "dev",
            True,
            True,
            True,
            "--live sends to the Anthropic API: unset JOBHUNTER_FAKE_LLM",
            id="--live in the fake-LLM mode",
        ),
        pytest.param("dev", False, False, False, None, id="dev on the fake"),
        pytest.param("test", True, False, False, None, id="test on the fake, fake-LLM mode set"),
        pytest.param("dev", False, True, True, None, id="dev --live with the key"),
    ],
)
def test_start_refusal(
    tmp_path: Path, env: str, fake_llm: bool, key: bool, live: bool, why: str | None
) -> None:
    assert start_refusal(config(tmp_path, env, fake_llm=fake_llm, key=key), live=live) == why


def test_prod_without_live_exits_2_before_anything_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prod = config(tmp_path, "prod", key=True)
    monkeypatch.setattr(cli_schema_dry_run, "startup_or_exit", lambda role: prod)
    monkeypatch.setattr(cli_schema_dry_run, "configure_logging", lambda *a, **k: None)

    def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the refused dry-run must not start")

    monkeypatch.setattr(cli_schema_dry_run, "_dry_run", never)
    assert cli_schema_dry_run.run_schema_dry_run(argparse.Namespace(live=False)) == NOT_STARTED
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == f"schema-dry-run: {PROD_WITHOUT_LIVE}; nothing sent\n"


def test_the_workflow_states_the_worst_case_from_pricing() -> None:
    # Each case's try reserves the gateway's own worst case, and every try of a case reserves the same, since
    # both max_tokens stages are DRY_RUN_MAX_TOKENS: the run's worst case is 6 x the sum over its cases.
    note = " ".join(line.lstrip("# ") for line in WORKFLOW.read_text("utf-8").splitlines()[:20])
    policy = get_object(load_settings_v1(), "scoring.model_policy")
    per_try: list[Decimal] = []
    for case in schema_dry_run.cases(policy):
        if case.probe:
            continue  # the free counting endpoint
        node = schema_dry_run.node_for(case)
        assert node.max_tokens_retry == node.max_tokens == schema_dry_run.DRY_RUN_MAX_TOKENS
        bound = text_input_bound(case.model, schema_dry_run.prompt_for(case).as_request(node.schema))
        per_try.append(
            pricing.worst_case_usd(case.model, input_tokens_bound=bound, max_tokens=node.max_tokens)
        )
    assert len(per_try) == 4
    assert {PROMPT_OVERHEAD_TOKENS[c.model] for c in schema_dry_run.cases(policy)} == {2_640}
    assert "PROMPT_OVERHEAD_TOKENS = 2,640" in note
    for usd in per_try:
        assert f"${usd}" in note
    assert f"${sum(per_try)} for one try" in note
    assert sum(per_try) * TRIES_PER_CASE == Decimal("1.522200")
    assert "The worst case is $1.522200" in note
    assert "at most 6 paid tries" in note
