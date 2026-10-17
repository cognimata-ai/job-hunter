import asyncio
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pyotp
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

pytestmark = pytest.mark.integration
PW = "Tannenbaum-Ölfass-7"


def login(c: httpx.AsyncClient, password: str = PW, **extra: str) -> Coroutine[Any, Any, httpx.Response]:
    return c.post("/api/auth/login", json={"username": "ada", "password": password, **extra})


async def test_login_sets_host_cookie_and_stores_only_hmac(api_client, make_user, engine) -> None:
    await make_user()
    r = await login(api_client)
    assert r.status_code == 204
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("__Host-jh_session=") and "Domain" not in cookie
    for attr in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=2592000"):
        assert attr in cookie
    token = cookie.split(";")[0].split("=", 1)[1]
    async with engine.connect() as c:
        stored = (await c.execute(sa.text("SELECT token_hmac FROM sessions"))).scalar_one()
    assert token.encode() not in stored and len(stored) == 32


async def test_failures_are_generic_and_constant_path(api_client, make_user, monkeypatch) -> None:
    await make_user()
    called: list[int] = []
    import jobhunter.api.routes.auth as auth_routes

    monkeypatch.setattr(auth_routes, "dummy_verify", lambda p: called.append(1))
    a = await login(api_client, password="wrong-password-123")
    b = await api_client.post(
        "/api/auth/login", json={"username": "nobody", "password": "wrong-password-123"}
    )
    assert a.status_code == b.status_code == 401 and a.json()["title"] == b.json()["title"] and called == [1]


async def test_login_checks_origin_only_and_rejection_has_edge_headers(api_client, make_user) -> None:
    await make_user()
    r = await api_client.post(
        "/api/auth/login",
        json={"username": "ada", "password": PW},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403 and r.json()["code"] == "csrf_failed"
    assert (
        r.headers["x-content-type-options"] == "nosniff"
        and r.json()["request_id"] == r.headers["x-request-id"]
    )
    assert uuid.UUID(r.headers["x-request-id"]).version == 7
    assert (await login(api_client)).status_code == 204


async def test_ip_lockout_after_five_failures(api_client, make_user, engine) -> None:
    await make_user()
    for _ in range(5):
        assert (await login(api_client, password="x" * 12)).status_code == 401
    async with engine.connect() as c:  # a separate connection sees committed rows only
        assert tuple(
            (
                await c.execute(
                    sa.text(
                        "SELECT (SELECT count(*) FROM login_attempts "
                        "WHERE host(ip) = '198.51.100.10' AND NOT success), "
                        "(SELECT count(*) FROM login_lockouts "
                        "WHERE scope = 'ip:198.51.100.10' AND locked_until > now())"
                    )
                )
            ).one()
        ) == (5, 1)
    r = await login(api_client)
    assert (
        r.status_code == 429 and r.json()["code"] == "throttled" and 880 <= r.json()["retry_after_s"] <= 900
    )
    async with engine.connect() as c:
        assert (
            await c.execute(
                sa.text("SELECT count(*) FROM login_attempts WHERE username_hmac = convert_to('ada','UTF8')")
            )
        ).scalar_one() == 0


async def test_global_lockout_after_twenty_failures(engine, make_config, make_user) -> None:
    from jobhunter.api.app import create_app

    await make_user()
    app = create_app(cfg=make_config("api"), engine=engine)

    def client(ip: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(ip, 1)),
            base_url="https://testserver",
            headers={"Origin": "https://testserver"},
        )

    for i in range(20):
        async with client(f"203.0.113.{i + 1}") as c:
            assert (await login(c, password="y" * 12)).status_code == 401
    async with engine.connect() as c:
        assert tuple(
            (
                await c.execute(
                    sa.text(
                        "SELECT (SELECT count(*) FROM login_attempts WHERE NOT success), "
                        "(SELECT count(*) FROM login_lockouts WHERE scope = 'global')"
                    )
                )
            ).one()
        ) == (20, 1)
    async with client("192.0.2.200") as c:
        assert (await login(c)).status_code == 429
    async with engine.connect() as c:
        assert (
            await c.execute(
                sa.text("SELECT severity FROM alerts WHERE code='auth.lockout' AND resolved_at IS NULL")
            )
        ).scalar_one() == "critical"


async def test_session_idle_absolute_and_slide(logged_in_client, engine) -> None:
    assert (await logged_in_client.get("/api/auth/session")).status_code == 200
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE sessions SET last_seen_at = now() - interval '30 seconds'"))
        before = (await c.execute(sa.text("SELECT idle_expires_at FROM sessions"))).scalar_one()
    await logged_in_client.get("/api/auth/session")
    async with engine.begin() as c:
        assert (
            await c.execute(sa.text("SELECT idle_expires_at FROM sessions"))
        ).scalar_one() == before  # < 1 min: no slide
        await c.execute(
            sa.text(
                "UPDATE sessions SET last_seen_at = now() - interval '2 minutes', "
                "idle_expires_at = now() + interval '1 hour'"
            )
        )
    await logged_in_client.get("/api/auth/session")
    async with engine.begin() as c:  # >= 1 min: the slide persisted now + 12 h
        assert (
            await c.execute(sa.text("SELECT idle_expires_at > now() + interval '11 hours' FROM sessions"))
        ).scalar_one() is True
        await c.execute(
            sa.text(
                "UPDATE sessions SET last_seen_at = now() - interval '2 minutes', "
                "idle_expires_at = now() + interval '30 minutes', "
                "absolute_expires_at = now() + interval '1 hour'"
            )
        )
    await logged_in_client.get("/api/auth/session")
    async with engine.begin() as c:  # absolute bound: the slide stops at absolute_expires_at
        # (CHECK idle <= absolute makes every absolute-expired session idle-expired too,
        # so the cap is the observable effect)
        assert (
            await c.execute(sa.text("SELECT idle_expires_at = absolute_expires_at FROM sessions"))
        ).scalar_one() is True
        await c.execute(sa.text("UPDATE sessions SET idle_expires_at = now() - interval '1 second'"))
    r = await logged_in_client.get("/api/auth/session")
    assert (
        r.status_code == 401
        and r.json()["code"] == "unauthenticated"
        and "Max-Age=0" in r.headers["set-cookie"]
    )


async def test_csrf_on_state_changing_routes(logged_in_client) -> None:
    r = await logged_in_client.post("/api/auth/logout", headers={"X-JH-Request": ""})
    assert (
        r.status_code == 403
        and r.json()["code"] == "csrf_failed"
        and r.json()["request_id"] == r.headers["x-request-id"]
    )
    assert (await logged_in_client.post("/api/auth/logout")).status_code == 204
    assert (await logged_in_client.get("/api/auth/session")).status_code == 401


async def test_totp_required_when_enabled(api_client, make_user, engine, make_config) -> None:
    from jobhunter.auth import totp, users

    await make_user()
    secret = totp.new_secret()
    async with engine.begin() as c:
        await users.enable_totp(c, secret, data_key=make_config("api").secret_b64("data_key"))
    assert (await login(api_client)).status_code == 401
    assert (await login(api_client, totp=pyotp.TOTP(secret).now())).status_code == 204


async def test_rate_limit_600_per_minute(logged_in_client) -> None:
    codes = [(await logged_in_client.get("/api/auth/session")).status_code for _ in range(601)]
    assert codes[:600].count(200) == 600 and codes[600] == 429


async def test_v20_dev_real_client_ip_through_trusted_proxy(engine, make_config, make_user) -> None:
    from jobhunter.api.app import create_app

    await make_user()
    # uvicorn types its ASGI callables more narrowly than Starlette does: both are the same protocol.
    app: Any = ProxyHeadersMiddleware(
        cast(Any, create_app(cfg=make_config("api"), engine=engine)), trusted_hosts=["172.30.0.2"]
    )
    for peer, xff in (("172.30.0.2", "203.0.113.7"), ("198.51.100.9", "203.0.113.8")):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(peer, 4000)),
            base_url="https://testserver",
            headers={"Origin": "https://testserver", "X-Forwarded-For": xff},
        ) as c:
            await login(c, password="z" * 12)
    async with engine.connect() as c:
        assert [
            r[0] for r in await c.execute(sa.text("SELECT host(ip) FROM login_attempts ORDER BY id"))
        ] == [
            "203.0.113.7",
            "198.51.100.9",
        ]


# --- beyond the plan: V20 for sessions.ip, replay protection (ruling T16-a), CSRF edges, logs, CLI ----------


async def test_v20_dev_session_ip_is_the_real_client_ip(engine, make_config, make_user) -> None:
    from jobhunter.api.app import create_app

    await make_user()
    # uvicorn types its ASGI callables more narrowly than Starlette does: both are the same protocol.
    app: Any = ProxyHeadersMiddleware(
        cast(Any, create_app(cfg=make_config("api"), engine=engine)), trusted_hosts=["172.30.0.2"]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("172.30.0.2", 4000)),
        base_url="https://testserver",
        headers={"Origin": "https://testserver", "X-Forwarded-For": "203.0.113.9"},
    ) as c:
        assert (await login(c)).status_code == 204
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT host(ip) FROM sessions"))).scalar_one() == "203.0.113.9"


def test_matched_step_names_the_step_of_the_code() -> None:
    from jobhunter.auth import totp

    secret, now = totp.new_secret(), datetime(2026, 10, 16, 10, 0, 7, tzinfo=UTC)
    step = int(now.timestamp()) // 30
    for offset in (-1, 0, 1):
        code = pyotp.TOTP(secret).at(now + timedelta(seconds=30 * offset))
        assert totp.matched_step(secret, code, at=now) == step + offset
    assert totp.matched_step(secret, pyotp.TOTP(secret).at(now + timedelta(seconds=60)), at=now) is None
    assert totp.matched_step(secret, "abcdef", at=now) is None
    with pytest.raises(ValueError, match="timezone"):
        totp.matched_step(secret, "123456", at=datetime(2026, 10, 16, 10))  # noqa: DTZ001 - the naive time is the point


async def test_a_totp_code_is_accepted_once(api_client, make_user, engine, make_config, monkeypatch) -> None:
    import jobhunter.api.routes.auth as auth_routes
    from jobhunter.auth import totp, users

    await make_user()
    secret = totp.new_secret()
    async with engine.begin() as c:
        await users.enable_totp(c, secret, data_key=make_config("api").secret_b64("data_key"))
    step = int(utcnow().timestamp()) // 30
    start = datetime.fromtimestamp(step * 30, UTC)  # the start of the current step
    clock = [start + timedelta(seconds=1)]
    monkeypatch.setattr(auth_routes, "utcnow", lambda: clock[0])  # the injected clock of the login route
    code = pyotp.TOTP(secret).at(start)
    assert (await login(api_client, totp=code)).status_code == 204
    assert (await login(api_client, totp=code)).status_code == 401  # the same code in its own step
    earlier = pyotp.TOTP(secret).at(start - timedelta(seconds=30))  # inside the window, before the used step
    assert (await login(api_client, totp=earlier)).status_code == 401
    clock[0] = start + timedelta(seconds=31)  # the next step: the old code is still inside the ±1 window
    assert (await login(api_client, totp=code)).status_code == 401
    assert (await login(api_client, totp=pyotp.TOTP(secret).at(clock[0]))).status_code == 204  # a later step
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT totp_last_step FROM users"))).scalar_one() == step + 1
        counts = await c.execute(
            sa.text("SELECT success, count(*) FROM login_attempts GROUP BY 1 ORDER BY 1")
        )
        assert [tuple(row) for row in counts] == [(False, 3), (True, 2)]
        assert (await c.execute(sa.text("SELECT count(*) FROM sessions"))).scalar_one() == 2


async def test_disabling_totp_forgets_the_last_step(engine, make_user) -> None:
    from jobhunter.auth import totp, users

    await make_user()
    async with engine.begin() as c:
        await users.enable_totp(c, totp.new_secret(), data_key=os.urandom(32))
        await c.execute(sa.text("UPDATE users SET totp_last_step = 42"))
        await users.disable_totp(c)
        assert (await c.execute(sa.text("SELECT totp_last_step FROM users"))).scalar_one() is None


async def test_a_login_with_a_password_changed_meanwhile_is_rejected(
    api_client, make_user, engine, monkeypatch
) -> None:
    import jobhunter.api.routes.auth as auth_routes
    from jobhunter.auth import passwords, users

    await make_user()
    verify = passwords.verify_password

    def verify_then_change(stored: str, p: str) -> tuple[bool, str | None]:
        result = verify(stored, p)  # the old password verifies; the operator changes it before the decision
        asyncio.run(_set_password(engine.url, "Kastanienallee-Ölfass-9"))
        return result

    monkeypatch.setattr(auth_routes, "verify_password", verify_then_change)
    assert (await login(api_client)).status_code == 401
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM sessions"))).scalar_one() == 0
        row = await users.get_user(c)
    assert row is not None and passwords.verify_password(row.password_hash, "Kastanienallee-Ölfass-9")[0]


async def _set_password(url: sa.URL, password: str) -> None:
    from jobhunter.auth import users
    from sqlalchemy.ext.asyncio import create_async_engine

    other = create_async_engine(url)
    try:
        async with other.begin() as c:
            await users.set_password(c, password, now=utcnow())
    finally:
        await other.dispose()


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_csrf_covers_every_state_changing_api_method(api_client, method) -> None:
    ok = {"Origin": "https://testserver", "X-JH-Request": "1"}
    for headers in (
        {"X-JH-Request": "1"},  # no Origin at all
        {**ok, "Origin": "http://testserver"},
        {**ok, "Origin": "https://testserver:8443"},
        {**ok, "Origin": "https://testserver.evil.example"},
        {**ok, "Origin": "null"},
        {"Origin": "https://testserver"},  # no X-JH-Request
        {**ok, "X-JH-Request": "true"},
    ):
        request = api_client.build_request(method, "/api/no-such-route", headers=headers)
        if "Origin" not in headers:
            del request.headers["Origin"]  # the client's default Origin
        r = await api_client.send(request)
        assert r.status_code == 403 and r.json()["code"] == "csrf_failed", headers
        assert r.headers["content-type"] == "application/problem+json"
    assert (await api_client.request(method, "/api/no-such-route", headers=ok)).status_code == 404
    assert (
        await api_client.request(method, "/healthz", headers={"Origin": "https://evil.example"})
    ).status_code == 405


async def test_safe_methods_skip_the_csrf_check(api_client) -> None:
    r = await api_client.get("/api/auth/session", headers={"Origin": "https://evil.example"})
    assert r.status_code == 401 and r.json()["code"] == "unauthenticated"


async def test_a_login_without_origin_is_refused(api_client, make_user) -> None:
    await make_user()
    request = api_client.build_request("POST", "/api/auth/login", json={"username": "ada", "password": PW})
    del request.headers["Origin"]
    r = await api_client.send(request)
    assert r.status_code == 403 and r.json()["code"] == "csrf_failed"


async def test_auth_logs_carry_the_outcome_and_the_address_only(
    api_client, make_user, make_config, log_events
) -> None:
    from jobhunter.auth import throttle

    await make_user("Ada Lovelace")
    body = {"username": "Ada Lovelace", "password": "wrong-password-123"}
    assert (await api_client.post("/api/auth/login", json=body)).status_code == 401
    r = await api_client.post("/api/auth/login", json={**body, "password": PW})
    assert r.status_code == 204
    token = r.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    events = log_events()
    logins = [e for e in events if e["event"] == "login"]
    assert [(e["outcome"], e["ip"]) for e in logins] == [
        ("rejected", "198.51.100.10"),
        ("accepted", "198.51.100.10"),
    ]
    dumped = json.dumps(events)
    username_hmac = throttle.username_hmac("Ada Lovelace", make_config("api").secret_b64("session_secret"))
    for value in ("Ada Lovelace", PW, "wrong-password-123", token, username_hmac.hex()):
        assert value not in dumped


def test_session_rate_limiter_slides_per_key() -> None:
    from jobhunter.api.app import SessionRateLimiter

    limiter = SessionRateLimiter(limit=3, window_s=60.0)
    assert [limiter.hit(b"a", t) for t in (0.0, 1.0, 2.0, 3.0)] == [True, True, True, False]
    assert limiter.hit(b"b", 3.0)  # another session has its own window
    assert not limiter.hit(b"a", 59.9)  # a refused request is not counted, but the window has not moved yet
    assert limiter.hit(b"a", 60.5) and not limiter.hit(b"a", 60.6)  # the hit at 0.0 slid out: one more


def _cli(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "jobhunter.cli", *args],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


async def test_cli_sessions_revoke_all(logged_in_client, migrated, proc_env, engine) -> None:
    assert (await logged_in_client.get("/api/auth/session")).status_code == 200
    done = await asyncio.to_thread(_cli, proc_env("api", migrated), "sessions", "revoke-all")
    assert done.returncode == 0 and "revoked 1 session" in done.stdout, done.stderr
    r = await logged_in_client.get("/api/auth/session")
    assert r.status_code == 401 and "Max-Age=0" in r.headers["set-cookie"]
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT revoked_at IS NOT NULL FROM sessions"))).scalar_one() is True


async def test_cli_sessions_unlock(api_client, make_user, migrated, proc_env, engine) -> None:
    from jobhunter.observability.alerts import raise_alert

    await make_user()
    async with engine.begin() as c:
        await c.execute(
            sa.text(
                "INSERT INTO login_lockouts VALUES ('global', now() + interval '1 hour', 'global_failures'), "
                "('ip:198.51.100.10', now() + interval '15 minutes', 'ip_failures')"
            )
        )
        await raise_alert(
            c, code="auth.lockout", severity="critical", subject="global", message="global login lockout"
        )
    assert (await login(api_client)).status_code == 429
    done = await asyncio.to_thread(_cli, proc_env("api", migrated), "sessions", "unlock")
    assert done.returncode == 0 and "released 2 lockout" in done.stdout, done.stderr
    assert (await login(api_client)).status_code == 204
    async with engine.connect() as c:
        assert (
            await c.execute(sa.text("SELECT resolved_at IS NOT NULL FROM alerts WHERE code = 'auth.lockout'"))
        ).scalar_one() is True


async def test_an_expired_global_lockout_resolves_its_alert(api_client, make_user, engine) -> None:
    from jobhunter.observability.alerts import raise_alert

    await make_user()
    async with engine.begin() as c:
        await c.execute(
            sa.text(
                "INSERT INTO login_lockouts VALUES ('global', now() - interval '1 second', 'global_failures')"
            )
        )
        await raise_alert(
            c, code="auth.lockout", severity="critical", subject="global", message="global login lockout"
        )
    assert (await login(api_client)).status_code == 204
    async with engine.connect() as c:
        assert (
            await c.execute(sa.text("SELECT resolved_at IS NOT NULL FROM alerts WHERE code = 'auth.lockout'"))
        ).scalar_one() is True
