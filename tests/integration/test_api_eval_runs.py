"""The eval-run list (spec §11.13, §14.2, §14.3; plan M4 Task 14, D9): `GET /api/eval-runs?kind&cursor&limit`.

The runs of one kind, newest first by id, keyset-paged with a signed cursor whose filters hold the kind; each
with its gate's figures from `metrics`, and beside them the gate of the active configuration with the number
of cases it counts. Runs start from the CLI only. Every datum is invented.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.eval import insert_cases
from tests.support.m3 import insert_id

pytestmark = pytest.mark.integration

URL: Final = "/api/eval-runs"
RUN_KEYS: Final = {
    "id",
    "kind",
    "route",
    "status",
    "passed",
    "gate",
    "spearman_rho",
    "pairwise",
    "contested_rate",
    "failed_gates",
    "cost_usd",
    "started_at",
    "finished_at",
    "rubric_version_id",
    "prompt_schema_version_id",
}
_ACTIVE: Final = sa.text(
    "SELECT rubric_version_id, prompt_schema_version_id, model_policy_id, evaluation_policy_id, "
    "execution_config_id FROM active_config WHERE id = 1"
)


async def insert_run(engine: AsyncEngine, kind: str, metrics: dict[str, Any] | None, **columns: Any) -> int:
    """A run of `kind` on the active configuration, completed a minute after it started unless `columns` say
    otherwise; its id."""
    started = utcnow() - timedelta(hours=1)
    async with engine.begin() as conn:
        active = (await conn.execute(_ACTIVE)).one()
        values: dict[str, Any] = {
            "kind": kind,
            "route": "production",
            "rubric_version_id": None if kind == "schema_dry_run" else active.rubric_version_id,
            "prompt_schema_version_id": active.prompt_schema_version_id,
            "model_policy_id": active.model_policy_id,
            "evaluation_policy_id": None if kind == "schema_dry_run" else active.evaluation_policy_id,
            "execution_config_id": active.execution_config_id,
            "status": "completed",
            "passed": None,
            "metrics": metrics,
            "cost_usd": Decimal("0.3150"),
            "started_at": started,
            "finished_at": started + timedelta(minutes=1),
            **columns,
        }
        return await insert_id(conn, "eval_runs", values)


def calibrated(gate: str, rho: str | None, **more: Any) -> dict[str, Any]:
    """A golden run's `metrics` as its calibration merges them (the transport beside them)."""
    return {
        "transport": "fake",
        "gate": gate,
        "spearman_rho": rho,
        "pairwise_agreement": "0.800000",
        "contested_rate": "0.050000",
        **more,
    }


async def page(client: httpx.AsyncClient, **params: Any) -> dict[str, Any]:
    r = await client.get(URL, params=params)
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    assert set(body) == {"items", "next_cursor", "active_gate"}
    assert all(set(item) == RUN_KEYS for item in body["items"])
    return body


async def test_golden_runs_page_newest_first_with_rho_and_the_active_gate(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, fake_llm_process: None
) -> None:
    first = await insert_run(engine, "golden", calibrated("uncalibrated", "0.550000"))
    failed = await insert_run(
        engine,
        "golden",
        calibrated("failed", "0.420000", failed_gates=["spearman_rho", "test_retest_missing"]),
        passed=False,
    )
    await insert_run(engine, "schema_dry_run", {"transport": "fake", "cases": []}, passed=True)
    await insert_run(engine, "canary", {"transport": "fake"})
    crashed = await insert_run(engine, "golden", None, status="failed", cost_usd=Decimal(0))
    running = await insert_run(engine, "golden", None, status="running", finished_at=None)
    passed = await insert_run(engine, "golden", calibrated("passed", "0.710000"), passed=True)

    body = await page(logged_in_client, kind="golden", limit=2)
    assert [item["id"] for item in body["items"]] == [passed, running]
    newest = body["items"][0]
    async with engine.connect() as conn:
        ids = (await conn.execute(_ACTIVE)).one()
    assert {k: newest[k] for k in RUN_KEYS - {"started_at", "finished_at"}} == {
        "id": passed,
        "kind": "golden",
        "route": "production",
        "status": "completed",
        "passed": True,
        "gate": "passed",
        "spearman_rho": "0.710000",
        "pairwise": "0.800000",
        "contested_rate": "0.050000",
        "failed_gates": [],
        "cost_usd": "0.3150",
        "rubric_version_id": ids.rubric_version_id,
        "prompt_schema_version_id": ids.prompt_schema_version_id,
    }
    assert newest["started_at"].endswith("Z") and newest["finished_at"].endswith("Z")
    assert body["items"][1]["finished_at"] is None and body["items"][1]["gate"] is None
    # below 30 of the user's active cases the active configuration's gate is uncalibrated
    assert body["active_gate"] == {"state": "uncalibrated", "cases": 0}

    body = await page(logged_in_client, kind="golden", limit=2, cursor=body["next_cursor"])
    assert [item["id"] for item in body["items"]] == [crashed, failed]
    crashed_item, failed_item = body["items"]
    assert (crashed_item["status"], crashed_item["gate"], crashed_item["spearman_rho"]) == (
        "failed",
        None,
        None,
    )
    assert crashed_item["failed_gates"] == [] and crashed_item["cost_usd"] == "0.0000"
    assert (failed_item["passed"], failed_item["gate"], failed_item["spearman_rho"]) == (
        False,
        "failed",
        "0.420000",
    )
    assert failed_item["failed_gates"] == ["spearman_rho", "test_retest_missing"]
    last = await page(logged_in_client, kind="golden", limit=2, cursor=body["next_cursor"])
    assert ([item["id"] for item in last["items"]], last["next_cursor"]) == ([first], None)

    # the other kind has its own list, and a cursor of one kind is no cursor of the other
    dry = await page(logged_in_client, kind="schema_dry_run")
    assert [(item["kind"], item["rubric_version_id"]) for item in dry["items"]] == [("schema_dry_run", None)]
    assert dry["next_cursor"] is None
    r = await logged_in_client.get(URL, params={"kind": "schema_dry_run", "cursor": body["next_cursor"]})
    assert (r.status_code, r.json()["code"]) == (400, "invalid_cursor"), r.text

    # 30 cases calibrate the gate; the newest completed golden run of the active candidate decides it
    await insert_cases(engine, 30)
    assert (await page(logged_in_client, kind="golden"))["active_gate"] == {"state": "passed", "cases": 30}
    await insert_run(engine, "golden", calibrated("failed", "0.300000"), passed=False)
    assert (await page(logged_in_client, kind="golden"))["active_gate"] == {"state": "failed", "cases": 30}


async def test_the_kind_is_required_and_the_limit_bounded(logged_in_client: httpx.AsyncClient) -> None:
    cases: list[dict[str, Any]] = [
        {},
        {"kind": "canary"},
        {"kind": "golden", "limit": 0},
        {"kind": "golden", "limit": 101},
    ]
    for params in cases:
        r = await logged_in_client.get(URL, params=params)
        assert (r.status_code, r.json()["code"]) == (422, "validation_failed"), params
    r = await logged_in_client.get(URL, params={"kind": "golden", "cursor": "not-a-cursor"})
    assert (r.status_code, r.json()["code"]) == (400, "invalid_cursor")


async def test_the_list_needs_a_session(api_client: httpx.AsyncClient) -> None:
    r = await api_client.get(URL, params={"kind": "golden"})
    assert r.status_code == 401
