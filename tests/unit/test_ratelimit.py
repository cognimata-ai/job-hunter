# tests/unit/test_ratelimit.py
import asyncio

import pytest
from jobhunter.sources.base import RateLimit
from jobhunter.sources.ratelimit import ATS_HOST_LIMIT, SMARTRECRUITERS_DETAIL, SOURCE_LIMITS, RateLimiter

from tests.support.clock import FakeClock


async def test_bucket_waits_and_concurrency_caps() -> None:
    clock, slept = FakeClock(0.0), []

    async def sleep(s: float) -> None:
        slept.append(s)
        clock.now += s

    rl = RateLimiter([RateLimit("jsearch", 1.0, 1)], clock=clock, sleep=sleep)
    async with rl.acquire("jsearch"):
        pass
    async with rl.acquire("jsearch"):
        pass
    assert slept == [1.0]
    entered, gate = 0, asyncio.Event()

    async def hold() -> None:
        nonlocal entered
        async with rl.acquire("jsearch"):
            entered += 1
            await gate.wait()

    t1 = asyncio.create_task(hold())
    await asyncio.sleep(0)
    t2 = asyncio.create_task(hold())
    await asyncio.sleep(0)
    assert entered == 1
    gate.set()
    await asyncio.gather(t1, t2)
    assert entered == 2


async def test_the_bucket_refills_continuously_with_the_clock() -> None:
    clock, slept = FakeClock(100.0), []

    async def sleep(s: float) -> None:
        slept.append(s)
        clock.now += s

    rl = RateLimiter([RateLimit("arbeitnow", 0.5, 1)], clock=clock, sleep=sleep)
    async with rl.acquire("arbeitnow"):
        pass
    clock.now += 1.5  # three quarters of a token have come back
    async with rl.acquire("arbeitnow"):
        pass
    clock.now += 10.0  # more than a full token: no wait, and no burst saved up beyond one request
    async with rl.acquire("arbeitnow"):
        pass
    async with rl.acquire("arbeitnow"):
        pass
    assert slept == [pytest.approx(0.5), pytest.approx(2.0)]


async def test_a_host_family_key_gets_its_own_bucket_and_semaphore() -> None:
    clock, slept = FakeClock(0.0), []

    async def sleep(s: float) -> None:
        slept.append(s)
        clock.now += s

    rl = RateLimiter([ATS_HOST_LIMIT], clock=clock, sleep=sleep)
    async with rl.acquire("ats-host:boards-api.greenhouse.io"), rl.acquire("ats-host:api.lever.co"):
        pass  # two hosts: neither waits for the other
    assert slept == []
    async with rl.acquire("ats-host:boards-api.greenhouse.io"):
        pass
    assert slept == [pytest.approx(0.2)]  # 5 requests/s on the one host

    entered, gate = 0, asyncio.Event()

    async def hold() -> None:
        nonlocal entered
        async with rl.acquire("ats-host:api.ashbyhq.com"):
            entered += 1
            await gate.wait()

    tasks = []
    for _ in range(3):
        tasks.append(asyncio.create_task(hold()))
        await asyncio.sleep(0)
    assert entered == 2  # 2 concurrent requests per host
    gate.set()
    await asyncio.gather(*tasks)
    assert entered == 3


async def test_an_unknown_key_is_refused() -> None:
    rl = RateLimiter(SOURCE_LIMITS.values(), clock=FakeClock(0.0), sleep=asyncio.sleep)
    with pytest.raises(KeyError, match="jsaerch"):
        async with rl.acquire("jsaerch"):
            pass
    with pytest.raises(ValueError, match="twice"):
        RateLimiter(
            [RateLimit("jsearch", 1.0, 1), RateLimit("jsearch", 2.0, 1)],
            clock=FakeClock(0.0),
            sleep=asyncio.sleep,
        )


def test_the_section_5_3_limits() -> None:
    assert dict(SOURCE_LIMITS) == {
        "jsearch": RateLimit("jsearch", 1.0, 1),
        "serpapi": RateLimit("serpapi", 1.0, 1),
        "adzuna": RateLimit("adzuna", 1.0, 1),
        "arbeitnow": RateLimit("arbeitnow", 0.5, 1),
        "ats-host": RateLimit("ats-host", 5.0, 2),
        "smartrecruiters-detail": RateLimit("smartrecruiters-detail", 5.0, 4),
    }
    assert (ATS_HOST_LIMIT, SMARTRECRUITERS_DETAIL) == (
        RateLimit("ats-host", 5.0, 2),
        RateLimit("smartrecruiters-detail", 5.0, 4),
    )
