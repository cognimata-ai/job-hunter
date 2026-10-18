"""In-process rate limits of the sources (spec §5.2, §5.3): token buckets and concurrency semaphores.

`RateLimiter.acquire(key)` takes a slot of the key's semaphore first and then a token of its bucket, so a
request that waited for a slot cannot start right after another one: the bucket spaces the *starts*. A
bucket holds at most one token and is refilled continuously at `per_second`, so consecutive starts of one
key are at least `1 / per_second` apart and no burst is saved up while a key is idle.

A key `<family>:<suffix>` (for example `ats-host:boards-api.greenhouse.io`) whose family is a configured
limit gets a bucket and a semaphore of its own with the family's values, created on first use: the §5.3
"every ATS host" limit. Any other unknown key raises `KeyError`, so a misspelt key never runs unlimited.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from types import MappingProxyType
from typing import Final

from jobhunter.sources.base import RateLimit

ATS_HOST_LIMIT: Final = RateLimit("ats-host", 5.0, 2)
SMARTRECRUITERS_DETAIL: Final = RateLimit("smartrecruiters-detail", 5.0, 4)
# Every in-process limit of §5.3, by key. SerpApi's hourly cap and Adzuna's rolling windows are quota rules
# (§6), enforced by admission, not here.
SOURCE_LIMITS: Final[Mapping[str, RateLimit]] = MappingProxyType(
    {
        limit.key: limit
        for limit in (
            RateLimit("jsearch", 1.0, 1),
            RateLimit("serpapi", 1.0, 1),
            RateLimit("adzuna", 1.0, 1),
            RateLimit("arbeitnow", 0.5, 1),  # 1 request per 2 s
            ATS_HOST_LIMIT,
            SMARTRECRUITERS_DETAIL,
        )
    }
)
_FULL: Final = 1.0
# A wake-up a hair early (float rounding) must not count as a missing token and cause a second wait.
_EPSILON: Final = 1e-9


class _Bucket:
    def __init__(self, limit: RateLimit, now: float) -> None:
        self.rate = limit.per_second
        self.tokens = _FULL
        self.stamp = now
        self.semaphore = asyncio.Semaphore(limit.concurrency)
        self.lock = asyncio.Lock()  # waiters take tokens one after the other, in arrival order

    def refill(self, now: float) -> None:
        self.tokens = min(_FULL, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now


class RateLimiter:
    def __init__(
        self,
        limits: Iterable[RateLimit],
        *,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self._limits: dict[str, RateLimit] = {}
        for limit in limits:
            if limit.key in self._limits:
                raise ValueError(f"rate limit {limit.key!r} is given twice")
            if limit.per_second <= 0 or limit.concurrency < 1:
                raise ValueError(f"rate limit {limit.key!r} needs a positive rate and concurrency")
            self._limits[limit.key] = limit
        self._clock = clock
        self._sleep = sleep
        self._buckets: dict[str, _Bucket] = {}

    def _bucket(self, key: str) -> _Bucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            limit = self._limits.get(key)
            if limit is None and ":" in key:
                limit = self._limits.get(key.partition(":")[0])
            if limit is None:
                raise KeyError(f"no rate limit for {key!r}")
            bucket = self._buckets[key] = _Bucket(limit, self._clock())
        return bucket

    def acquire(self, key: str) -> AbstractAsyncContextManager[None]:
        """`async with limiter.acquire(key):` holds a slot of the key's semaphore and has taken a token."""
        return self._acquire(self._bucket(key))

    @asynccontextmanager
    async def _acquire(self, bucket: _Bucket) -> AsyncIterator[None]:
        async with bucket.semaphore:
            async with bucket.lock:
                bucket.refill(self._clock())
                while bucket.tokens < _FULL - _EPSILON:
                    await self._sleep((_FULL - bucket.tokens) / bucket.rate)
                    bucket.refill(self._clock())
                bucket.tokens = max(0.0, bucket.tokens - _FULL)
            yield
