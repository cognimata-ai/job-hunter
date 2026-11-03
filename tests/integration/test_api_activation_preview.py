"""The activation previews (spec §13.3, §14.2, §11.13, §10.11; plan M4 Task 14, D8):
`GET /api/settings/{rubric,prompt-schema}-versions/{id}/activation-preview`.

A preview reads what the activation reads before its pointer flip (the version, whether it is active, the
live schema dry-runs a prompt-schema version needs, the eval gate of the candidate and the re-score estimate)
with the activation's own computation, in one read-only snapshot: it writes nothing and waits for no lock.
The tests' process is a live one, so a fake run never verifies a schema. Every datum is invented.
"""

import asyncio
from decimal import Decimal
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.test_api_activation import (
    ACK,
    OPUS,
    POLICY_B_CASES,
    SONNET,
    activate,
    case,
    dry_run_record,
    point_prompt_schema_at,
    prompt_versions,
    rubric_versions,
)
from tests.integration.test_settings_dry_run import table_counts
from tests.support.eval import insert_cases
from tests.support.m3 import insert_id, insert_job
from tests.support.pg import PgServer
from tests.support.settings import activate_profile

pytestmark = pytest.mark.integration

RUBRICS_URL: Final = "/api/settings/rubric-versions"
PROMPTS_URL: Final = "/api/settings/prompt-schema-versions"
PREVIEW_KEYS: Final = {
    "version_id",
    "active",
    "gate_state",
    "latest_golden_run",
    "schema_verified",
    "unverified_models",
    "targets",
    "est_usd_online",
    "est_usd_batch",
    "bulk_budget_usd",
}
_LOCK_ACTIVE_CONFIG: Final = sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE")
_ACTIVE: Final = sa.text(
    "SELECT prompt_schema_version_id, model_policy_id, evaluation_policy_id, execution_config_id "
    "FROM active_config WHERE id = 1"
)


@pytest.fixture(autouse=True)
def _live_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts as a live process, whatever the environment the suite runs in."""
    monkeypatch.delenv("JOBHUNTER_FAKE_LLM", raising=False)


async def preview(client: httpx.AsyncClient, kind: str, version_id: int) -> dict[str, Any]:
    path = RUBRICS_URL if kind == "rubric" else PROMPTS_URL
    r = await client.get(f"{path}/{version_id}/activation-preview")
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    assert set(body) == PREVIEW_KEYS
    return body


async def golden_run(engine: AsyncEngine, rubric_version_id: int, *, rho: str, passed: bool) -> int:
    """A completed live golden run on the production route of the active configuration with
    `rubric_version_id`: the run the candidate's gate reads."""
    async with engine.begin() as conn:
        active = (await conn.execute(_ACTIVE)).one()
        return await insert_id(
            conn,
            "eval_runs",
            {
                "kind": "golden",
                "route": "production",
                "rubric_version_id": rubric_version_id,
                "prompt_schema_version_id": active.prompt_schema_version_id,
                "model_policy_id": active.model_policy_id,
                "evaluation_policy_id": active.evaluation_policy_id,
                "execution_config_id": active.execution_config_id,
                "status": "completed",
                "passed": passed,
                "metrics": {
                    "transport": "live",
                    "gate": "passed" if passed else "failed",
                    "spearman_rho": rho,
                },
                "cost_usd": Decimal("0.4200"),
                "finished_at": utcnow(),
            },
        )


async def test_preview_of_an_inactive_rubric_has_gate_cost_and_writes_nothing(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, migrated: PgServer
) -> None:
    rubrics = await rubric_versions(engine)
    _, v2 = await prompt_versions(engine)
    await activate_profile(engine)
    async with engine.begin() as conn:
        for _ in range(2):
            await insert_job(conn)  # two retained revisions
    run = await golden_run(engine, rubrics["v1"], rho="0.710000", passed=True)

    body = await preview(logged_in_client, "rubric", rubrics["v1"])
    # below 30 active cases the gate is uncalibrated; the run it reads once calibrated is shown anyway
    assert (body["version_id"], body["active"], body["gate_state"]) == (rubrics["v1"], False, "uncalibrated")
    golden = body["latest_golden_run"]
    assert (golden["eval_run_id"], golden["passed"], golden["spearman_rho"]) == (run, True, "0.710000")
    assert golden["finished_at"].endswith("Z")
    # a rubric version needs no schema dry-run of its own
    assert (body["schema_verified"], body["unverified_models"]) == (None, [])
    # 2 x ĉ $0.052646 online, 2 x $0.046671 in Batches (§21.4); 0.4 x $93.10 of scoring share (§6.9.3)
    assert (body["targets"], body["est_usd_online"], body["est_usd_batch"], body["bulk_budget_usd"]) == (
        2,
        "0.105292",
        "0.093342",
        "37.240000",
    )

    await insert_cases(engine, 30)  # 30 more retained revisions, and a calibrated gate
    body = await preview(logged_in_client, "rubric", rubrics["v1"])
    assert (body["gate_state"], body["targets"]) == ("passed", 32)
    failed = await golden_run(engine, rubrics["v1"], rho="0.420000", passed=False)
    body = await preview(logged_in_client, "rubric", rubrics["v1"])
    assert body["gate_state"] == "failed"
    assert (body["latest_golden_run"]["eval_run_id"], body["latest_golden_run"]["passed"]) == (failed, False)
    active = await preview(logged_in_client, "rubric", rubrics["v2"])
    assert (active["active"], active["latest_golden_run"]) == (True, None)

    # one read-only snapshot: no row of any table written, and no lock waited for
    before = table_counts(migrated)
    for kind, version in (("rubric", rubrics["v1"]), ("rubric", rubrics["v2"]), ("prompt_schema", v2)):
        await preview(logged_in_client, kind, version)
    async with engine.connect() as blocker:
        await blocker.begin()
        await blocker.execute(_LOCK_ACTIVE_CONFIG)
        held = await asyncio.wait_for(preview(logged_in_client, "rubric", rubrics["v1"]), 10)
        await blocker.rollback()
    assert held == body
    assert table_counts(migrated) == before

    # the activation computes the same figures: it re-scores what the preview counted at its budget
    await dry_run_record(engine, v2, POLICY_B_CASES)  # the save re-checks the active pair
    r = await activate(logged_in_client, "rubric", rubrics["v1"], {**ACK, "eval_override_ack": True})
    assert r.status_code == 201, r.text
    assert (r.json()["targets_to_create"], r.json()["bulk_budget_usd"]) == (
        body["targets"],
        body["bulk_budget_usd"],
    )


async def test_prompt_schema_preview_names_the_unverified_models(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    v1, v2 = await prompt_versions(engine)
    await point_prompt_schema_at(engine, v1)
    body = await preview(logged_in_client, "prompt_schema", v2)
    # policy B needs the generator schema on Sonnet and the critic, profile and queries schemas on Opus
    assert (body["active"], body["schema_verified"], body["unverified_models"]) == (
        False,
        False,
        [OPUS, SONNET],
    )
    await dry_run_record(engine, v2, POLICY_B_CASES, transport="fake")  # a fake run never verifies
    assert (await preview(logged_in_client, "prompt_schema", v2))["unverified_models"] == [OPUS, SONNET]
    await dry_run_record(engine, v1, POLICY_B_CASES)  # nor a run of another version
    assert (await preview(logged_in_client, "prompt_schema", v2))["unverified_models"] == [OPUS, SONNET]
    await dry_run_record(engine, v2, [case("generator", SONNET), case("critic", OPUS)])
    body = await preview(logged_in_client, "prompt_schema", v2)
    assert (body["schema_verified"], body["unverified_models"]) == (False, [OPUS])  # profile and queries
    await dry_run_record(engine, v2, [case("profile", OPUS), case("queries", OPUS)])
    body = await preview(logged_in_client, "prompt_schema", v2)
    assert (body["schema_verified"], body["unverified_models"]) == (True, [])
    # without a profile there is no tuple and nothing to re-score
    assert (body["targets"], body["est_usd_online"], body["est_usd_batch"], body["bulk_budget_usd"]) == (
        0,
        "0.000000",
        "0.000000",
        "0.000000",
    )
    # a process on the fake LLM does not check the schema at all, as its activation does not
    monkeypatch.setenv("JOBHUNTER_FAKE_LLM", "1")
    await point_prompt_schema_at(engine, v2)
    body = await preview(logged_in_client, "prompt_schema", v1)
    assert (body["schema_verified"], body["unverified_models"]) == (None, [])
    assert (await preview(logged_in_client, "prompt_schema", v2))["active"] is True


async def test_preview_of_an_unknown_version_is_404(logged_in_client: httpx.AsyncClient) -> None:
    for path in (RUBRICS_URL, PROMPTS_URL):
        r = await logged_in_client.get(f"{path}/{10**12}/activation-preview")
        assert (r.status_code, r.json()["code"]) == (404, "not_found"), r.text
        r = await logged_in_client.get(f"{path}/0/activation-preview")
        assert (r.status_code, r.json()["code"]) == (422, "validation_failed"), r.text


async def test_the_previews_need_a_session(api_client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    rubrics = await rubric_versions(engine)
    r = await api_client.get(f"{RUBRICS_URL}/{rubrics['v1']}/activation-preview")
    assert r.status_code == 401
