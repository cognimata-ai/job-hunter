import asyncio
import contextlib
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.auth import throttle
from jobhunter.clock import utcnow

from tests.support.procs import await_until

pytestmark = pytest.mark.integration
IP = "198.51.100.77"


async def failures(engine, n: int, ip: str = IP) -> None:
    async with engine.begin() as c:
        for _ in range(n):
            await c.execute(
                sa.text(
                    "INSERT INTO login_attempts (ip, username_hmac, success) "
                    "VALUES (CAST(:ip AS inet), '\\x01', false)"
                ),
                {"ip": ip},
            )


async def blocked_on_advisory(engine) -> bool:
    async with engine.connect() as c:
        waiting: int = (
            await c.execute(
                sa.text(
                    "SELECT count(*) FROM pg_locks "
                    "WHERE locktype='advisory' AND objid=7261002 AND NOT granted"
                )
            )
        ).scalar_one()
        return waiting == 1


@pytest.mark.parametrize(
    ("pre", "ip_of", "scope"), [(3, lambda i: IP, f"ip:{IP}"), (18, lambda i: f"203.0.113.{i}", "global")]
)
async def test_concurrent_failures_reach_the_threshold(engine, pre, ip_of, scope) -> None:
    for i in range(pre):
        await failures(engine, 1, ip_of(100 + i))
    a, b = await engine.connect(), await engine.connect()
    ta, tb = await a.begin(), await b.begin()
    await throttle.serialize(a)
    await throttle.record_attempt(
        a, ip=ip_of(1), username_hmac=b"\x01", success=False, now=utcnow()
    )  # count = pre+1, below threshold

    async def second() -> None:
        await throttle.serialize(b)
        await throttle.record_attempt(b, ip=ip_of(2), username_hmac=b"\x01", success=False, now=utcnow())
        await tb.commit()

    task = asyncio.create_task(second())
    await await_until(lambda: blocked_on_advisory(engine), 5)  # b is provably waiting behind a
    await ta.commit()
    await asyncio.wait_for(task, 5)
    await a.close()
    await b.close()
    async with engine.connect() as c:
        assert (
            await c.execute(
                sa.text("SELECT count(*) FROM login_lockouts WHERE scope = :s AND locked_until > now()"),
                {"s": scope},
            )
        ).scalar_one() == 1


async def decide(conn, **overrides: Any) -> Any:
    """`decide_login` for a failed attempt from `IP`, unless `overrides` say otherwise."""
    from jobhunter.api.routes.auth import decide_login

    arguments: dict[str, Any] = {
        "ip": IP,
        "username_hmac": b"\x01",
        "credentials_ok": False,
        "new_hash": None,
        "pepper": b"p" * 32,
        "user_agent": None,
        "now": utcnow(),
        "verified_hash": None,
        "totp_step": None,
        "verified_totp_secret_enc": None,
        "previous_token": None,
    }
    return await decide_login(conn, **{**arguments, **overrides})


async def test_decide_login_takes_the_throttle_lock_first(engine) -> None:  # ruling T16-c
    from jobhunter.api.routes.auth import Rejected

    a, b = await engine.connect(), await engine.connect()
    task: asyncio.Task[Any] | None = None
    try:
        ta = await a.begin()
        assert isinstance(await decide(a), Rejected)  # a holds the throttle lock until it commits
        tb = await b.begin()
        b_pid = (await b.execute(sa.text("SELECT pg_backend_pid()"))).scalar_one()
        task = asyncio.create_task(decide(b))
        await await_until(lambda: blocked_on_advisory(engine), 5)  # b waits on advisory key 7261002 ...
        async with engine.connect() as c:  # ... before it has touched a table (no relation lock yet)
            held = (
                await c.execute(
                    sa.text("SELECT count(*) FROM pg_locks WHERE pid = :pid AND locktype = 'relation'"),
                    {"pid": b_pid},
                )
            ).scalar_one()
        assert held == 0
        await ta.commit()
        assert isinstance(await asyncio.wait_for(task, 5), Rejected)
        await tb.commit()
    finally:
        await a.close()  # releases the throttle lock, should the test have failed while b waited
        if task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, 5)
        await b.close()
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM login_attempts"))).scalar_one() == 2


async def test_decide_login_rechecks_the_lockout_under_the_lock(engine) -> None:
    from jobhunter.api.routes.auth import Locked

    async with engine.begin() as c:
        await c.execute(
            sa.text(
                "INSERT INTO login_lockouts VALUES (:scope, now() + interval '15 minutes', 'ip_failures')"
            ),
            {"scope": f"ip:{IP}"},
        )
    async with engine.begin() as c:  # valid credentials make no difference: the lockout is authoritative
        outcome = await decide(c, credentials_ok=True, verified_hash="$argon2id$verified")
    assert isinstance(outcome, Locked) and 0 < outcome.retry_after.total_seconds() <= 900
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM login_attempts"))).scalar_one() == 0
