"""The alert acknowledgement over HTTP (spec §14.2 L5030, §17.6; plan Task 21, R10, Deviation 19, Ruling P13).

`POST /api/alerts/{id}/ack` → 200 `{id, acknowledged_at}` (`alerts.acknowledge_alert`): the open alert is
acknowledged once (a repeat answers the first time) and stays open; an acknowledged
`llm.reservation_bound_violated` or `llm.auth_failed` no longer pauses the score lane, so a cycle starts
afterwards. The first is resolved by its acknowledgement (§17.6 "manual acknowledgement"; plan M5 Task 2), so
a repeat finds it resolved. A resolved or unknown alert is 404. Every datum is invented.
"""

from datetime import datetime

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.llm.gateway import AUTH_FAILED_ALERT
from jobhunter.llm.spend_ledger import BOUND_VIOLATED_ALERT
from jobhunter.observability.alerts import raise_alert, resolve_alert
from jobhunter.scoring import cycles
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.scoring import insert_targets

pytestmark = pytest.mark.integration


async def alert(engine: AsyncEngine, code: str, subject: str = "target:1") -> int:
    async with engine.begin() as conn:
        return await raise_alert(
            conn, code=code, severity="critical", subject=subject, message="test", context={}
        )


async def start_cycle(engine: AsyncEngine, epoch: int) -> int | None:
    async with engine.begin() as conn:
        return await cycles.start_cycle(conn, trigger="manual", now=utcnow(), epoch=epoch, fake_llm=True)


async def alert_row(engine: AsyncEngine, alert_id: int) -> sa.Row[tuple[datetime | None, datetime | None]]:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                sa.text("SELECT acknowledged_at, resolved_at FROM alerts WHERE id = :a"), {"a": alert_id}
            )
        ).one()


@pytest.mark.parametrize(("code", "resolved"), [(BOUND_VIOLATED_ALERT, True), (AUTH_FAILED_ALERT, False)])
async def test_the_acknowledgement_lifts_the_score_lane_s_pause(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, current_epoch: int, code: str, resolved: bool
) -> None:
    await insert_targets(engine, 1)
    alert_id = await alert(engine, code)
    async with engine.connect() as conn:
        assert await cycles.pause_reason(conn, fake_llm=True, now=utcnow()) is not None
    assert await start_cycle(engine, current_epoch) is None  # paused
    r = await logged_in_client.post(f"/api/alerts/{alert_id}/ack")
    assert r.status_code == 200, r.text
    body = r.json()
    row = await alert_row(engine, alert_id)
    assert row.acknowledged_at is not None
    # a bound violation is resolved by its acknowledgement; an auth failure stays open
    assert row.resolved_at == (row.acknowledged_at if resolved else None)
    assert set(body) == {"id", "acknowledged_at"} and body["id"] == alert_id
    assert datetime.fromisoformat(body["acknowledged_at"]) == row.acknowledged_at
    async with engine.connect() as conn:
        assert await cycles.pause_reason(conn, fake_llm=True, now=utcnow()) is None
    assert await start_cycle(engine, current_epoch) is not None  # a cycle starts afterwards
    again = await logged_in_client.post(f"/api/alerts/{alert_id}/ack")
    if resolved:  # nothing to acknowledge
        assert (again.status_code, again.json()["code"]) == (404, "not_found")
    else:
        assert (again.status_code, again.json()) == (200, body)  # the first acknowledgement is kept


async def test_a_resolved_or_unknown_alert_is_not_found(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    alert_id = await alert(engine, BOUND_VIOLATED_ALERT)
    async with engine.begin() as conn:
        await resolve_alert(conn, code=BOUND_VIOLATED_ALERT, subject="target:1")
    for target in (alert_id, 10**12):
        r = await logged_in_client.post(f"/api/alerts/{target}/ack")
        assert (r.status_code, r.json()["code"]) == (404, "not_found"), r.text
    assert (await alert_row(engine, alert_id)).acknowledged_at is None
    r = await logged_in_client.post("/api/alerts/0/ack")
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed")
