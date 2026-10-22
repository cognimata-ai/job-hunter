"""The planning-time adapter factory (spec §5.5, §9.2 nodes 1 and 3; M1 Carry-forward 13: moved out of
`quota.planner`, so that `quota/` builds no adapter itself).

`_spec_adapters(now)` gives the adapter of an instance for building request specs: the registry's
`make_adapter` (the one factory) on a transport that refuses every request (`_refuse`), so `plan` and
`next_page` build specs and nothing is ever sent from inside the planning transaction; without credentials
(`_no_secret`: a spec carries none, the fetch node's adapters hold the keys) and with the planning time as its
clock. One adapter per instance for the length of the block.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime

import httpx

from jobhunter.sources.base import SourceAdapter
from jobhunter.sources.instances import InstanceRow
from jobhunter.sources.registry import make_adapter


def _refuse(request: httpx.Request) -> httpx.Response:
    raise RuntimeError(f"the planner's adapters build specs and never send (a request to {request.url.host})")


def _no_secret(name: str) -> bytes:
    return b""


@asynccontextmanager
async def _spec_adapters(now: datetime) -> AsyncIterator[Callable[[InstanceRow], SourceAdapter]]:
    """The adapter of an instance for building specs (module docstring)."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(_refuse)) as client:
        built: dict[int, SourceAdapter] = {}

        def clock() -> datetime:
            return now

        def adapter_of(instance: InstanceRow) -> SourceAdapter:
            if (adapter := built.get(instance.id)) is None:
                adapter = built[instance.id] = make_adapter(instance, client, secret=_no_secret, clock=clock)
            return adapter

        yield adapter_of
