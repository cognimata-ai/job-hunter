"""`DELETE /api/feedback/{feedback_id}` over HTTP (spec §14.2 L4997; plan M4 Task 7, Deviation 24).

The deletion removes the `score_feedback` row and nothing else: the golden case the feedback upserted stays
(nothing references `score_feedback`, and the golden set is edited through `/api/golden-cases`), and the
Jobs list does not change, so the ranking generation stays. An unknown feedback is 404, a request without
the session 401, without the CSRF header 403. Every datum is invented.
"""

from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.eval import Scored, scored_job
from tests.support.fixtures import count_rows
from tests.support.settings import generation

pytestmark = pytest.mark.integration

# The foreign keys that reference `score_feedback`: none, so a deletion cascades to nothing.
_REFERENCING: Final = sa.text(
    "SELECT count(*) FROM pg_constraint "
    "WHERE contype = 'f' AND confrelid = CAST('score_feedback' AS regclass)"
)


@pytest.fixture
async def scored(engine: AsyncEngine) -> Scored:
    async with engine.begin() as conn:
        return await scored_job(conn)


def url(feedback_id: int) -> str:
    return f"/api/feedback/{feedback_id}"


async def record(client: httpx.AsyncClient, job_id: int, body: dict[str, Any]) -> dict[str, Any]:
    r = await client.post(f"/api/jobs/{job_id}/feedback", json=body)
    assert r.status_code == 201, r.text
    recorded: dict[str, Any] = r.json()
    return recorded


async def test_delete_removes_the_feedback_and_keeps_the_golden_case(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, scored: Scored
) -> None:
    label = await record(
        logged_in_client, scored.job.job_id, {"kind": "label", "label": "good", "comment": "passt"}
    )
    wrong = await record(logged_in_client, scored.job.job_id, {"kind": "wrong_score", "label": "poor"})
    assert wrong["golden_case_id"] == label["golden_case_id"]
    before = await generation(engine)
    r = await logged_in_client.delete(url(label["feedback_id"]))
    assert (r.status_code, r.content) == (204, b"")
    assert await count_rows(engine, "score_feedback", id=label["feedback_id"]) == 0
    assert await count_rows(engine, "score_feedback", id=wrong["feedback_id"]) == 1  # the other one stays
    assert await count_rows(engine, "golden_cases", id=label["golden_case_id"]) == 1
    async with engine.connect() as conn:
        assert (await conn.execute(_REFERENCING)).scalar_one() == 0
    assert await generation(engine) == before  # the Jobs list did not change
    again = await logged_in_client.delete(url(label["feedback_id"]))
    assert (again.status_code, again.json()["code"]) == (404, "not_found")


async def test_delete_unknown_feedback_is_404(
    logged_in_client: httpx.AsyncClient, api_client: httpx.AsyncClient, engine: AsyncEngine, scored: Scored
) -> None:
    recorded = await record(logged_in_client, scored.job.job_id, {"kind": "label", "label": "ok"})
    r = await logged_in_client.delete(url(10**12))
    assert (r.status_code, r.json()["code"]) == (404, "not_found")
    r = await logged_in_client.delete(url(0))
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed")
    r = await api_client.delete(url(recorded["feedback_id"]), headers={"X-JH-Request": "1"})
    assert (r.status_code, r.json()["code"]) == (401, "unauthenticated")
    r = await logged_in_client.delete(url(recorded["feedback_id"]), headers={"X-JH-Request": "0"})
    assert (r.status_code, r.json()["code"]) == (403, "csrf_failed")
    assert await count_rows(engine, "score_feedback") == 1
