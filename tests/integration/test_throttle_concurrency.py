import asyncio

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
