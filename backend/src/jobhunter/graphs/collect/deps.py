"""What the collection graph's nodes are built with (spec §9.2): the process's engine, adapters and limits.

`CollectDeps` is made once per worker process (`worker.main`, Task 24) and shared by every run: one
`RateLimiter` for the process (§5.2), the adapter of each source instance (`adapters`), the injected clock and
sleep, and `admission_stopped`, which the shutdown sets so that no new request starts (§3.3.5). `embedder` is
`None` until Task 22 gives it its client type.

`FetchContext` holds the §9.2 per-source semaphores of the fetch nodes: JSearch 1, SerpApi 1, Adzuna 1,
Arbeitnow 1 (Task 14 adds the ATS host semaphores). An `asyncio.Semaphore` binds to no event loop until it is
first awaited, so a context may be made before the loop runs.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Final

from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.sources.base import SourceAdapter
from jobhunter.sources.instances import InstanceRow
from jobhunter.sources.ratelimit import RateLimiter

# The fetch concurrency of each source (§9.2 node 2): its semaphore's slots.
SOURCE_CONCURRENCY: Final[Mapping[str, int]] = MappingProxyType(
    {"jsearch": 1, "serpapi": 1, "adzuna": 1, "arbeitnow": 1}
)


def _source_semaphores() -> dict[str, asyncio.Semaphore]:
    return {source: asyncio.Semaphore(slots) for source, slots in SOURCE_CONCURRENCY.items()}


@dataclass
class FetchContext:
    """The per-source semaphores the fetch nodes hold around a traversal, by source id."""

    sources: Mapping[str, asyncio.Semaphore] = field(default_factory=_source_semaphores)


@dataclass
class CollectDeps:
    engine: AsyncEngine
    adapters: Callable[[InstanceRow], SourceAdapter]
    embedder: object | None
    clock: Callable[[], datetime]
    sleep: Callable[[float], Awaitable[None]]
    admission_stopped: Callable[[], bool]
    limiter: RateLimiter
    fetch_ctx: FetchContext
