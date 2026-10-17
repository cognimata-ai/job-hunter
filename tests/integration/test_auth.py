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


async def test_session_idle_absolute_and_slide(logged_in_client, engine, monkeypatch) -> None:
    # One injected clock for `require_session` and every timestamp the test writes (no elapsed real time).
    t = utcnow()
    monkeypatch.setattr("jobhunter.api.deps.utcnow", lambda: t)
    at = {"t": t}
    assert (await logged_in_client.get("/api/auth/session")).status_code == 200
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE sessions SET last_seen_at = :t - interval '30 seconds'"), at)
        before = (await c.execute(sa.text("SELECT idle_expires_at FROM sessions"))).scalar_one()
    await logged_in_client.get("/api/auth/session")
    async with engine.begin() as c:
        assert (
            await c.execute(sa.text("SELECT idle_expires_at FROM sessions"))
        ).scalar_one() == before  # < 1 min: no slide
        await c.execute(
            sa.text(
                "UPDATE sessions SET last_seen_at = :t - interval '2 minutes', "
                "idle_expires_at = :t + interval '1 hour'"
            ),
            at,
        )
    await logged_in_client.get("/api/auth/session")
    async with engine.begin() as c:  # >= 1 min: the slide persisted now + 12 h
        assert (
            await c.execute(sa.text("SELECT idle_expires_at > :t + interval '11 hours' FROM sessions"), at)
        ).scalar_one() is True
        await c.execute(
            sa.text(
                "UPDATE sessions SET last_seen_at = :t - interval '2 minutes', "
                "idle_expires_at = :t + interval '30 minutes', "
                "absolute_expires_at = :t + interval '1 hour'"
            ),
            at,
        )
    await logged_in_client.get("/api/auth/session")
    async with engine.begin() as c:  # absolute bound: the slide stops at absolute_expires_at
        # (CHECK idle <= absolute makes every absolute-expired session idle-expired too,
        # so the cap is the observable effect)
        assert (
            await c.execute(sa.text("SELECT idle_expires_at = absolute_expires_at FROM sessions"))
        ).scalar_one() is True
        await c.execute(sa.text("UPDATE sessions SET idle_expires_at = :t - interval '1 second'"), at)
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


async def test_rate_limit_600_per_minute(logged_in_client, monkeypatch) -> None:
    clock = [1000.0]  # the injected monotonic clock of `require_session`: every request at the same instant
    monkeypatch.setattr("jobhunter.api.deps.monotonic", lambda: clock[0])
    codes = [(await logged_in_client.get("/api/auth/session")).status_code for _ in range(601)]
    assert codes[:600].count(200) == 600 and codes[600] == 429
    clock[0] += 60.0  # the window slid past every counted request
    assert (await logged_in_client.get("/api/auth/session")).status_code == 200


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
        # the second success came with the first one's cookie, so it replaced that session (rotation)
        assert (await c.execute(sa.text("SELECT count(*) FROM sessions"))).scalar_one() == 1


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
    check = passwords.check_password

    def check_then_change(stored: str, p: str) -> tuple[bool, bool]:
        result = check(stored, p)  # the old password verifies; the operator changes it before the decision
        asyncio.run(_set_password(engine.url, "Kastanienallee-Ölfass-9"))
        return result

    monkeypatch.setattr(auth_routes, "check_password", check_then_change)
    assert (await login(api_client)).status_code == 401
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM sessions"))).scalar_one() == 0
        row = await users.get_user(c)
    assert row is not None and passwords.verify_password(row.password_hash, "Kastanienallee-Ölfass-9")[0]


async def test_a_login_verified_with_a_totp_secret_replaced_meanwhile_is_rejected(
    api_client, make_user, engine, make_config, monkeypatch
) -> None:
    import jobhunter.api.routes.auth as auth_routes
    from jobhunter.auth import totp, users

    await make_user()
    data_key = make_config("api").secret_b64("data_key")
    secret_a, secret_b = totp.new_secret(), totp.new_secret()
    async with engine.begin() as c:
        await users.enable_totp(c, secret_a, data_key=data_key)
    decide = auth_routes.decide_login

    async def replace_secret_then_decide(conn: Any, **arguments: Any) -> Any:
        # The login has verified the password and secret A's code. Before its decision (no statement of it
        # has run yet) the operator turns TOTP off and on again with secret B; the password is unchanged.
        async with engine.begin() as operator:
            await users.disable_totp(operator)
            await users.enable_totp(operator, secret_b, data_key=data_key)
        return await decide(conn, **arguments)

    monkeypatch.setattr(auth_routes, "decide_login", replace_secret_then_decide)
    assert (await login(api_client, totp=pyotp.TOTP(secret_a).now())).status_code == 401
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM sessions"))).scalar_one() == 0
        attempts = await c.execute(sa.text("SELECT success FROM login_attempts"))
        assert attempts.scalars().all() == [False]  # it counts as a failure
        row = await users.get_user(c)
    assert row is not None and users.totp_secret(row, keys=[data_key]) == secret_b


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


def test_session_rate_limiter_forgets_idle_sessions() -> None:
    from jobhunter.api.app import SessionRateLimiter

    limiter = SessionRateLimiter(limit=3, window_s=60.0)
    assert limiter.hit(b"a", 0.0) and limiter.hit(b"b", 0.0)
    assert limiter.hit(b"c", 30.0) and len(limiter) == 3  # no sweep before a window has passed
    assert limiter.hit(b"c", 61.0) and len(limiter) == 1  # a and b had no request in the last window


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


# --- fix round 1: constant-cost phase 1, lockout pre-check, unreadable TOTP secret, rotation, branches ------


class CountingHasher:
    """Stands in for `passwords.ARGON2` and counts its expensive operations (`verify` and `hash`)."""

    def __init__(self, real: Any) -> None:
        self.real = real
        self.ops: list[str] = []

    def verify(self, stored: str, p: str) -> bool:
        self.ops.append("verify")
        return bool(self.real.verify(stored, p))

    def hash(self, p: str) -> str:
        self.ops.append("hash")
        return str(self.real.hash(p))

    def check_needs_rehash(self, stored: str) -> bool:
        return bool(self.real.check_needs_rehash(stored))


@pytest.fixture
def argon2_ops(monkeypatch) -> list[str]:
    """The Argon2 operations of the test so far, in order (`dummy_verify` included)."""
    from jobhunter.auth import passwords

    spy = CountingHasher(passwords.ARGON2)
    monkeypatch.setattr(passwords, "ARGON2", spy)
    return spy.ops


def wrong_code(secret: str) -> str:
    """A six-digit code that no step around now accepts."""
    now = utcnow()
    valid = {pyotp.TOTP(secret).at(now + timedelta(seconds=30 * offset)) for offset in range(-2, 3)}
    return next(
        code for code in ("000000", "111111", "222222", "333333", "444444", "555555") if code not in valid
    )


async def test_with_totp_on_a_right_password_costs_what_a_wrong_one_does(
    api_client, make_user, engine, make_config, argon2_ops
) -> None:
    from argon2 import PasswordHasher
    from jobhunter.auth import passwords, totp, users

    await make_user()
    secret = totp.new_secret()
    weaker = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(
        passwords.normalize_password(PW)
    )
    async with engine.begin() as c:  # a stored hash with other parameters: a successful login rehashes it
        await c.execute(sa.text("UPDATE users SET password_hash = :h"), {"h": weaker})
        await users.enable_totp(c, secret, data_key=make_config("api").secret_b64("data_key"))
    argon2_ops.clear()
    assert (await login(api_client, totp=wrong_code(secret))).status_code == 401
    right_password_ops = list(argon2_ops)
    argon2_ops.clear()
    assert (
        await login(api_client, password="wrong-password-123", totp=wrong_code(secret))
    ).status_code == 401
    assert right_password_ops == argon2_ops == ["verify"]  # no rehash before the code matched
    argon2_ops.clear()
    assert (await login(api_client, totp=pyotp.TOTP(secret).now())).status_code == 204
    assert argon2_ops == ["verify", "hash"]  # the rehash, once every factor matched
    async with engine.connect() as c:
        stored = (await c.execute(sa.text("SELECT password_hash FROM users"))).scalar_one()
    assert stored != weaker and not passwords.ARGON2.check_needs_rehash(stored)


async def test_a_locked_out_client_costs_no_argon2_work(api_client, make_user, engine, argon2_ops) -> None:
    await make_user()
    argon2_ops.clear()
    async with engine.begin() as c:
        await c.execute(
            sa.text(
                "INSERT INTO login_lockouts "
                "VALUES ('ip:198.51.100.10', now() + interval '15 minutes', 'ip_failures')"
            )
        )
    r = await login(api_client)
    assert r.status_code == 429 and r.json()["code"] == "throttled" and 0 < r.json()["retry_after_s"] <= 900
    assert argon2_ops == []
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM login_attempts"))).scalar_one() == 0


async def test_an_unreadable_totp_secret_fails_generically_and_raises_an_alert(
    api_client, make_user, engine, log_events
) -> None:
    from jobhunter.auth import totp, users

    await make_user()
    secret = totp.new_secret()
    async with engine.begin() as c:  # sealed with a key the api does not have
        await users.enable_totp(c, secret, data_key=os.urandom(32))
    right = await login(api_client, totp=pyotp.TOTP(secret).now())
    wrong = await login(api_client, password="wrong-password-123", totp=pyotp.TOTP(secret).now())
    # The right password is not confirmed: same answer, and the alert comes from both attempts alike.
    assert right.status_code == wrong.status_code == 401 and right.json() == {
        **wrong.json(),
        "request_id": right.json()["request_id"],
    }
    async with engine.connect() as c:
        assert (
            await c.execute(sa.text("SELECT count(*) FROM login_attempts WHERE NOT success"))
        ).scalar_one() == 2
        alert = (
            await c.execute(
                sa.text(
                    "SELECT severity, occurrences FROM alerts "
                    "WHERE dedup_key = 'auth.totp_key_mismatch:totp' AND resolved_at IS NULL"
                )
            )
        ).one()
    assert tuple(alert) == ("critical", 2)
    events = log_events()
    unreadable = [e for e in events if e["event"] == "totp_secret_unreadable"]
    assert [e["error_class"] for e in unreadable] == ["DecryptError", "DecryptError"]
    for value in (secret, PW, "wrong-password-123"):
        assert value not in json.dumps(events)


async def test_confirm_unchanged_and_claim_totp_step(engine, make_user) -> None:
    from jobhunter.auth import totp, users

    await make_user()
    async with engine.begin() as c:
        row = await users.get_user(c)
        assert row is not None
        stored = row.password_hash

        async def unchanged(expected_hash: str, totp_enabled: bool, sealed: bytes | None) -> bool:
            return await users.confirm_unchanged(
                c, expected_hash=expected_hash, totp_enabled=totp_enabled, totp_secret_enc=sealed
            )

        assert await unchanged(stored, False, None)
        assert not await unchanged("$argon2id$other", False, None)
        assert not await unchanged(stored, True, None)
        assert not await users.claim_totp_step(c, 7)  # TOTP is off: there is no step to claim
        secret, key = totp.new_secret(), os.urandom(32)
        await users.enable_totp(c, secret, data_key=key)
        enabled = await users.get_user(c)
        assert enabled is not None and enabled.totp_secret_enc is not None
        sealed = enabled.totp_secret_enc
        assert await unchanged(stored, True, sealed)
        assert not await unchanged(stored, False, sealed)
        assert not await unchanged(stored, True, None)
        await users.disable_totp(c)
        await users.enable_totp(c, secret, data_key=key)  # the same secret, sealed again: another value
        assert not await unchanged(stored, True, sealed)
        assert await users.claim_totp_step(c, 7)
        assert not await users.claim_totp_step(c, 7) and not await users.claim_totp_step(c, 6)
        assert await users.claim_totp_step(c, 8)


def test_client_ip_is_canonical_or_the_shared_unknown_bucket() -> None:
    from jobhunter.api.routes.auth import UNKNOWN_CLIENT_IP, client_ip
    from starlette.requests import Request

    def ip_of(client: tuple[str, int] | None) -> str:
        return client_ip(Request({"type": "http", "client": client, "headers": []}))

    assert UNKNOWN_CLIENT_IP == "0.0.0.0"  # noqa: S104 - an address recorded, not one bound to
    assert ip_of(None) == ip_of(("testclient", 50000)) == ip_of(("", 0)) == UNKNOWN_CLIENT_IP
    assert ip_of(("::ffff:198.51.100.5", 1)) == "198.51.100.5"  # IPv4-mapped IPv6: the IPv4 address
    assert ip_of(("2001:db8::1", 1)) == "2001:db8::1" and ip_of(("198.51.100.10", 1)) == "198.51.100.10"


async def test_a_client_without_an_address_is_counted_in_the_shared_bucket(
    engine, make_config, make_user
) -> None:
    from jobhunter.api.app import create_app
    from jobhunter.api.routes.auth import UNKNOWN_CLIENT_IP

    await make_user()
    app = create_app(cfg=make_config("api"), engine=engine)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("testclient", 50000)),
        base_url="https://testserver",
        headers={"Origin": "https://testserver"},
    ) as c:
        assert (await login(c, password="w" * 12)).status_code == 401
    async with engine.connect() as c:
        assert (
            await c.execute(sa.text("SELECT host(ip) FROM login_attempts"))
        ).scalar_one() == UNKNOWN_CLIENT_IP


async def test_a_login_rotates_the_session_it_was_sent_with(logged_in_client, api_client, engine) -> None:
    old = logged_in_client.cookies["__Host-jh_session"]
    async with engine.connect() as c:
        old_hmac = (await c.execute(sa.text("SELECT token_hmac FROM sessions"))).scalar_one()
    failed = await logged_in_client.post("/api/auth/login", json={"username": "ada", "password": "w" * 12})
    assert failed.status_code == 401  # a failed login leaves the session alone
    assert (await logged_in_client.get("/api/auth/session")).status_code == 200
    r = await logged_in_client.post("/api/auth/login", json={"username": "ada", "password": PW})
    assert r.status_code == 204
    new = r.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    async with engine.connect() as c:
        hmacs = [row[0] for row in await c.execute(sa.text("SELECT token_hmac FROM sessions"))]
    assert new != old and len(hmacs) == 1 and hmacs[0] != old_hmac  # the old session is gone, not revoked
    stale = await api_client.get("/api/auth/session", headers={"Cookie": f"__Host-jh_session={old}"})
    assert stale.status_code == 401
    assert (await logged_in_client.get("/api/auth/session")).status_code == 200
