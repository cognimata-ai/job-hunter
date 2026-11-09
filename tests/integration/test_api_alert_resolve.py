"""Resolving a manual alert over HTTP, and the delivery state of every alert (spec §14.2, §17.6; plan M5
Task 2, O-ops O6, O22d, M4 Carry-forward 4).

`POST /api/alerts/{id}/resolve` → 200 `AlertOut` (`lists.resolve_manual`): an alert whose code does not
resolve itself (§17.6 "manual", or a code the catalogue lacks) is resolved at the request's time; one that
resolves itself is 409 `conflict_state` with `detail = "not_manual"` and stays open, and so is one resolved
already (the code decides, whatever the status); an unknown id is 404; a manual alert resolved already is
answered as it is. An acknowledged manual alert is resolved with its acknowledgement kept. `alert_resolved`
is logged once, by the call that resolved the alert, never by a repeat. `GET /api/alerts` shows each alert's
`last_notified_at`, `notify_failures` and `manual`. Every datum is invented.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.api.routes import alerts as alerts_route
from jobhunter.observability.alerts import acknowledge_alert, raise_alert, resolve_alert
from jobhunter.observability.catalogue import Severity
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.logs import logged

pytestmark = pytest.mark.integration

NOW: Final = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alerts_route, "utcnow", lambda: NOW)


async def alert(engine: AsyncEngine, code: str, severity: Severity = "critical", subject: str = "s") -> int:
    async with engine.begin() as conn:
        return await raise_alert(
            conn, code=code, severity=severity, subject=subject, message="test", context={"n": 1}
        )


async def resolved_at(engine: AsyncEngine, alert_id: int) -> datetime | None:
    async with engine.connect() as conn:
        value: datetime | None = (
            await conn.execute(sa.text("SELECT resolved_at FROM alerts WHERE id = :a"), {"a": alert_id})
        ).scalar_one()
        return value


def at(value: Any) -> datetime:
    assert isinstance(value, str)
    return datetime.fromisoformat(value)


@pytest.mark.parametrize("code", ["quota.limit_breach", "x.uncatalogued"])
async def test_resolve_a_manual_alert_200(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, code: str
) -> None:
    alert_id = await alert(engine, code)
    r = await logged_in_client.post(f"/api/alerts/{alert_id}/resolve")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["id"], body["code"], body["status"], body["manual"]) == (alert_id, code, "resolved", True)
    assert at(body["resolved_at"]) == NOW == await resolved_at(engine, alert_id)
    # the condition again: a new alert
    assert await alert(engine, code) != alert_id


async def test_resolve_an_auto_resolving_alert_409_not_manual(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    alert_id = await alert(engine, "run.failed", subject="run:7")
    r = await logged_in_client.post(f"/api/alerts/{alert_id}/resolve")
    assert r.status_code == 409, r.text
    assert (r.json()["code"], r.json()["detail"]) == ("conflict_state", "not_manual")
    assert await resolved_at(engine, alert_id) is None


async def test_an_auto_resolving_alert_resolved_already_is_409_not_manual(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    alert_id = await alert(engine, "run.failed", subject="run:9")
    async with engine.begin() as conn:
        assert await resolve_alert(conn, code="run.failed", subject="run:9")
    resolved = await resolved_at(engine, alert_id)
    r = await logged_in_client.post(f"/api/alerts/{alert_id}/resolve")
    assert r.status_code == 409, r.text
    assert (r.json()["code"], r.json()["detail"]) == ("conflict_state", "not_manual")
    assert await resolved_at(engine, alert_id) == resolved  # untouched


async def test_resolve_an_acknowledged_manual_alert_keeps_its_acknowledgement(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    alert_id = await alert(engine, "eval.gate_failed", severity="warn")
    acked = NOW - timedelta(hours=1)
    async with engine.begin() as conn:
        assert await acknowledge_alert(conn, alert_id, now=acked) == acked
    r = await logged_in_client.post(f"/api/alerts/{alert_id}/resolve")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "resolved"
    assert (at(body["acknowledged_at"]), at(body["resolved_at"])) == (acked, NOW)
    assert await resolved_at(engine, alert_id) == NOW


async def test_resolve_unknown_404(logged_in_client: httpx.AsyncClient) -> None:
    r = await logged_in_client.post(f"/api/alerts/{10**12}/resolve")
    assert (r.status_code, r.json()["code"]) == (404, "not_found"), r.text
    r = await logged_in_client.post("/api/alerts/0/resolve")
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed"), r.text


async def test_resolve_is_idempotent(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    alert_id = await alert(engine, "operation.failed", severity="warn")
    first = await logged_in_client.post(f"/api/alerts/{alert_id}/resolve")
    assert first.status_code == 200, first.text
    monkeypatch.setattr(alerts_route, "utcnow", lambda: NOW + timedelta(minutes=5))
    again = await logged_in_client.post(f"/api/alerts/{alert_id}/resolve")
    assert (again.status_code, again.json()) == (200, first.json())  # the first resolution is kept
    assert await resolved_at(engine, alert_id) == NOW
    # one resolution, so one line: the repeat resolved nothing
    assert [event["alert_id"] for event in logged(caplog, "alert_resolved")] == [alert_id]


async def test_alert_out_has_delivery_fields(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    pushed = await alert(engine, "quota.limit_breach")
    failing = await alert(engine, "run.failed", subject="run:8")
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE alerts SET last_notified_at = :t WHERE id = :a"), {"t": NOW, "a": pushed}
        )
        await conn.execute(sa.text("UPDATE alerts SET notify_failures = 2 WHERE id = :a"), {"a": failing})
    r = await logged_in_client.get("/api/alerts")
    assert r.status_code == 200, r.text
    items = {item["id"]: item for item in r.json()["items"]}
    assert at(items[pushed]["last_notified_at"]) == NOW
    assert (items[pushed]["notify_failures"], items[pushed]["manual"]) == (0, True)
    assert (items[failing]["last_notified_at"], items[failing]["notify_failures"]) == (None, 2)
    assert items[failing]["manual"] is False
