"""What M5 adds to `GET /api/system/health` (spec §17.5 L5625; plan M5 Task 6, O16, Q17, Ruling T6-3).

`langsmith` is the tracing state the last worker published at its start (`publish_tracing_state`, on
`worker_heartbeat.tracing`, in a transaction that starts with its epoch fence): `null` until a worker
published one, and not cleared when the heartbeat goes stale. The api has no LangSmith configuration of its
own, so this is all it can report.
"""

import pytest
import sqlalchemy as sa
from jobhunter.observability.langsmith import TracingState, publish_tracing_state
from jobhunter.worker.epoch import fence_epoch

pytestmark = pytest.mark.integration


async def published(engine, epoch: int, state: TracingState) -> None:
    async with engine.begin() as conn:
        await fence_epoch(conn, epoch)
        await publish_tracing_state(conn, state)


async def test_health_reports_the_published_tracing_state(logged_in_client, engine, current_epoch) -> None:
    await published(engine, current_epoch, TracingState(tracing=True, region="eu"))
    async with engine.connect() as conn:
        stored = (
            await conn.execute(sa.text("SELECT tracing FROM worker_heartbeat WHERE id = 1"))
        ).scalar_one()
    assert stored == {"tracing": True, "region": "eu"}
    body = (await logged_in_client.get("/api/system/health")).json()
    assert body["langsmith"] == {"tracing": True, "region": "eu"}
    # the next worker's start publishes its own state over it
    await published(engine, current_epoch, TracingState(tracing=False, region=None))
    body = (await logged_in_client.get("/api/system/health")).json()
    assert body["langsmith"] == {"tracing": False, "region": None}
    # a stale heartbeat fails readiness but keeps the last published state
    async with engine.begin() as conn:
        await conn.execute(sa.text("UPDATE worker_heartbeat SET heartbeat_at = now() - interval '1 hour'"))
    body = (await logged_in_client.get("/api/system/health")).json()
    assert "worker_heartbeat" in body["ready"]["failing"]
    assert body["langsmith"] == {"tracing": False, "region": None}


@pytest.mark.parametrize("heartbeat", [False, True], ids=["no_heartbeat_row", "row_not_published"])
async def test_health_langsmith_is_null_before_the_worker_published(
    logged_in_client, request: pytest.FixtureRequest, heartbeat: bool
) -> None:
    if heartbeat:  # the row a worker's takeover writes, before its start published anything
        request.getfixturevalue("current_epoch")
    body = (await logged_in_client.get("/api/system/health")).json()
    assert "langsmith" in body and body["langsmith"] is None
    assert ("worker_heartbeat" in body["ready"]["failing"]) is not heartbeat
