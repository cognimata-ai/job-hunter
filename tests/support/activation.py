"""The shipped-version activation's test helpers (M3 Task 21; shared by the activation and its preview, M4
Task 14): the routes as a client calls them, the shipped versions' ids, and schema dry-run records.

- `RUBRICS_URL`, `PROMPTS_URL`: the two listings, whose `/{id}/activate` and `/{id}/activation-preview` a test
  calls; `ACK`: the body that acknowledges the re-score;
- `activate(client, kind, version_id, body, *, key=None)`: `POST …/{id}/activate` with an `Idempotency-Key`
  (a fresh one unless `key` is given);
- `prompt_versions(engine)`: the shipped prompt-schema versions 1 and 2 by their parser families;
  `rubric_versions(engine)`: the rubric ids by version; `point_prompt_schema_at(engine, version)`: the
  prompt-schema pointer moved (the reset restores version 2);
- `case(schema, model)`, `POLICY_B_CASES`: passed schema dry-run cases, those of every pair policy B needs;
  `dry_run_record(engine, version, cases, *, transport, status, started_minutes_ago)`: a `schema_dry_run`
  run of the version with those cases, its id.
"""

from datetime import timedelta
from typing import Any, Final

import httpx
import sqlalchemy as sa
from jobhunter.clock import utcnow
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.m3 import insert_id
from tests.support.profile import idem
from tests.support.settings import fresh_key

SONNET, OPUS, HAIKU = "claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"
RUBRICS_URL: Final = "/api/settings/rubric-versions"
PROMPTS_URL: Final = "/api/settings/prompt-schema-versions"
ACK: Final = {"expected_rescore_ack": True}


def case(schema: str, model: str) -> dict[str, Any]:
    return {"name": f"{schema}/{model}", "schema": schema, "model": model, "passed": True}


POLICY_B_CASES: Final = [
    case("generator", SONNET),
    case("critic", OPUS),
    case("profile", OPUS),
    case("queries", OPUS),
]


async def dry_run_record(
    engine: AsyncEngine,
    version: int,
    cases: list[dict[str, Any]],
    *,
    transport: str = "live",
    status: str = "completed",
    started_minutes_ago: int = 0,
) -> int:
    """A schema dry-run of prompt-schema version `version` with `cases`; its id."""
    started = utcnow() - timedelta(minutes=started_minutes_ago)
    async with engine.begin() as conn:
        return await insert_id(
            conn,
            "eval_runs",
            {
                "kind": "schema_dry_run",
                "route": "production",
                "prompt_schema_version_id": version,
                "status": status,
                "passed": None if status == "running" else True,
                "metrics": None if status == "running" else {"transport": transport, "cases": cases},
                "started_at": started,
                "finished_at": None if status == "running" else started + timedelta(seconds=30),
            },
        )


async def prompt_versions(engine: AsyncEngine) -> tuple[int, int]:
    """The shipped prompt-schema versions 1 and 2, by their parser families."""
    async with engine.connect() as conn:
        rows = await conn.execute(sa.text("SELECT id, parser_family FROM prompt_schema_versions ORDER BY id"))
        by_family = {str(r.parser_family): int(r.id) for r in rows}
    return by_family["generator_v1"], by_family["generator_v2_items"]


async def rubric_versions(engine: AsyncEngine) -> dict[str, int]:
    async with engine.connect() as conn:
        rows = await conn.execute(sa.text("SELECT id, version FROM rubric_versions ORDER BY id"))
        return {str(r.version): int(r.id) for r in rows}


async def point_prompt_schema_at(engine: AsyncEngine, version: int) -> None:
    """The prompt-schema pointer on `version` (the reset restores version 2)."""
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE active_config SET prompt_schema_version_id = :v WHERE id = 1"), {"v": version}
        )


async def activate(
    client: httpx.AsyncClient, kind: str, version_id: int, body: dict[str, Any], *, key: str | None = None
) -> httpx.Response:
    path = RUBRICS_URL if kind == "rubric" else PROMPTS_URL
    return await client.post(
        f"{path}/{version_id}/activate", json=body, headers=idem(key or fresh_key("act"))
    )
