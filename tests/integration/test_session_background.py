"""A background request (`X-JH-Background: 1`, the SPA's polls) is validated without sliding the session.

Its session is checked like any other (both expiries, the rate limit), but its last use and its idle expiry
stay as they were, so polls alone never keep an unattended session alive (spec §16.4).
"""

from dataclasses import replace
from datetime import datetime
from typing import Any, Final

import pytest
import sqlalchemy as sa
from jobhunter.auth import sessions
from jobhunter.clock import utcnow

pytestmark = pytest.mark.integration
BACKGROUND: Final = {"X-JH-Background": "1"}


async def _age_the_session(engine, t: datetime) -> tuple[datetime, datetime]:
    """Last used 2 min before `t`, idle expiry 1 h after it: due to slide at `t`. Returns both as stored."""
    async with engine.begin() as c:
        await c.execute(
            sa.text(
                "UPDATE sessions SET last_seen_at = :t - interval '2 minutes', "
                "idle_expires_at = :t + interval '1 hour'"
            ),
            {"t": t},
        )
    return await _stored(engine)


async def _stored(engine) -> tuple[datetime, datetime]:
    async with engine.connect() as c:
        row = (await c.execute(sa.text("SELECT last_seen_at, idle_expires_at FROM sessions"))).one()
    return row.last_seen_at, row.idle_expires_at


async def test_a_background_request_does_not_slide_the_session(logged_in_client, engine, monkeypatch) -> None:
    t = utcnow()  # the injected clock of `require_session` (no elapsed real time)
    monkeypatch.setattr("jobhunter.api.deps.utcnow", lambda: t)
    before = await _age_the_session(engine, t)
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        statements.append(statement)

    sa.event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        background = await logged_in_client.get("/api/auth/session", headers=BACKGROUND)
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", record)
    assert background.status_code == 200
    assert datetime.fromisoformat(background.json()["idle_expires_at"]) == before[1]
    assert await _stored(engine) == before  # neither the last use nor the idle expiry moved
    assert any("FROM sessions" in s for s in statements)  # the session was looked up ...
    assert [s for s in statements if s.lstrip().upper().startswith("UPDATE")] == []  # ... and not written
    normal = await logged_in_client.get("/api/auth/session")
    assert normal.status_code == 200
    assert await _stored(engine) == (t, t + sessions.IDLE)  # a normal request at the same instant slides it


@pytest.mark.parametrize("value", ["0", "true", ""])
async def test_only_the_value_1_marks_a_request_as_background(
    logged_in_client, engine, monkeypatch, value: str
) -> None:
    t = utcnow()
    monkeypatch.setattr("jobhunter.api.deps.utcnow", lambda: t)
    await _age_the_session(engine, t)
    r = await logged_in_client.get("/api/auth/session", headers={"X-JH-Background": value})
    assert r.status_code == 200
    assert await _stored(engine) == (t, t + sessions.IDLE)


async def test_a_background_request_after_the_idle_expiry_is_401(
    logged_in_client, engine, monkeypatch
) -> None:
    t = utcnow()
    monkeypatch.setattr("jobhunter.api.deps.utcnow", lambda: t)
    async with engine.begin() as c:  # just before the idle expiry, and not due for a slide
        await c.execute(
            sa.text("UPDATE sessions SET last_seen_at = :t, idle_expires_at = :t + interval '1 second'"),
            {"t": t},
        )
    assert (await logged_in_client.get("/api/auth/session", headers=BACKGROUND)).status_code == 200
    async with engine.begin() as c:
        await c.execute(
            sa.text(
                "UPDATE sessions SET last_seen_at = :t - interval '12 hours 1 second', "
                "idle_expires_at = :t - interval '1 second'"
            ),
            {"t": t},
        )
    r = await logged_in_client.get("/api/auth/session", headers=BACKGROUND)
    assert r.status_code == 401 and r.json()["code"] == "unauthenticated"
    clears_the_cookie = "Max-Age=0" in r.headers["set-cookie"]
    assert clears_the_cookie


async def test_a_background_request_counts_toward_the_rate_limit(logged_in_client, monkeypatch) -> None:
    clock = [1000.0]  # the injected monotonic clock of `require_session`: every request at the same instant
    monkeypatch.setattr("jobhunter.api.deps.monotonic", lambda: clock[0])
    codes = [
        (await logged_in_client.get("/api/auth/session", headers=BACKGROUND if n % 2 else {})).status_code
        for n in range(600)
    ]
    assert codes.count(200) == 600
    refused = await logged_in_client.get("/api/auth/session", headers=BACKGROUND)
    assert refused.status_code == 429 and refused.json()["code"] == "throttled"


async def test_a_validation_without_the_slide_writes_nothing_and_returns_the_same_session(
    logged_in_client, engine, make_config
) -> None:
    token = logged_in_client.cookies[sessions.SESSION_COOKIE]
    pepper = sessions.session_pepper(make_config("api").secret_b64("session_secret"))
    t = utcnow()
    before = await _age_the_session(engine, t)
    async with engine.connect() as c:  # a read-only transaction: any write would fail it
        await c.execute(sa.text("SET TRANSACTION READ ONLY"))
        still = await sessions.validate_session(c, token, pepper=pepper, now=t, slide=False)
    async with engine.begin() as c:
        slid = await sessions.validate_session(c, token, pepper=pepper, now=t)
    assert still is not None and slid is not None
    assert still.idle_expires_at == before[1] and slid.idle_expires_at == t + sessions.IDLE
    assert replace(still, idle_expires_at=slid.idle_expires_at) == slid
