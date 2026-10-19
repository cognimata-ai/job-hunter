"""Node 3, `plan_fallback` (spec §6.6, §9.2): the SerpApi specs of the JSearch cells the run did not serve.

It runs once every primary spec is terminal. It reads the run's settings version and `include_paid` from the
run row and, in a run that includes paid sources, makes the free SerpApi account read for the projection
(`serpapi_account`, outside any transaction and within `ACCOUNT_READ_TIMEOUT_S`; the acquisition reads the
account again before every search). A read that expires or fails is no read: the projection then fails
closed by its own rules (`window_cap` without an account window). Then `planner.plan_fallback` in one
transaction. `route` sends the graph to `fetch_fallback` when it planned specs, else to `parse_normalize`.
"""

import asyncio
from collections.abc import Callable, Sequence
from typing import Final, Literal

import httpx

from jobhunter.db.sql import q
from jobhunter.graphs.collect.deps import CollectDeps
from jobhunter.graphs.collect.nodes import Node
from jobhunter.graphs.collect.state import CollectState, CollectUpdate
from jobhunter.observability.logging import get_logger
from jobhunter.quota import planner
from jobhunter.quota.windows import AccountObservation
from jobhunter.run_settings import RunSettings, load_run_settings
from jobhunter.sources.acquire import AccountAdapter
from jobhunter.sources.base import SourceAdapter, SourceError
from jobhunter.sources.instances import InstanceRow, list_instances
from jobhunter.worker.runs import RunOwner

# How long the projection's account read may take (the node's own timeout is 30 s, §9.2).
ACCOUNT_READ_TIMEOUT_S: Final = 10.0

_RUN = q("SELECT settings_version_id, include_paid FROM runs WHERE id = :i")

log = get_logger("jobhunter.graphs.collect.plan_fallback")


async def serpapi_account(
    adapters: Callable[[InstanceRow], SourceAdapter],
    settings: RunSettings,
    instances: Sequence[InstanceRow],
    *,
    timeout_s: float = ACCOUNT_READ_TIMEOUT_S,
) -> AccountObservation | None:
    """The free SerpApi account read for the fallback projection (§5.3.2), or `None`: SerpApi disabled (by
    settings or health), an adapter without the read, or a read that failed (`SourceError`, a transport error)
    or did not end within `timeout_s`. Anything else (a bug, a fenced owner, a database error) propagates."""
    instance = next((i for i in instances if i.source_id == "serpapi"), None)
    if not settings.sources.serpapi or instance is None or instance.health == "disabled":
        return None
    adapter = adapters(instance)
    if not isinstance(adapter, AccountAdapter):
        return None
    try:
        async with asyncio.timeout(timeout_s):
            return await adapter.account()
    except TimeoutError:
        log.warning("account_read_failed", error_kind="timeout", error_code="account_read_timeout")
    except SourceError as err:
        log.warning("account_read_failed", error_kind=err.kind, error_code=err.code)
    except httpx.TransportError as err:  # the class only: a message may carry the URL
        log.warning("account_read_failed", error_kind="transport", error_code=type(err).__name__)
    return None


def make_plan_fallback(deps: CollectDeps) -> Node:
    async def plan_fallback(state: CollectState) -> CollectUpdate:
        owner = RunOwner(state["run_id"], state["attempt_no"])
        async with deps.engine.connect() as conn:
            run = (await conn.execute(_RUN, {"i": owner.run_id})).one()
            settings = await load_run_settings(conn, int(run.settings_version_id))
            instances = await list_instances(conn)
        account = await serpapi_account(deps.adapters, settings, instances) if run.include_paid else None
        async with deps.engine.begin() as conn:
            ids = await planner.plan_fallback(
                conn, owner=owner, settings=settings, instances=instances, now=deps.clock(), account=account
            )
        return {"fallback_fetch_request_ids": ids, "steps": ["plan_fallback"]}

    return plan_fallback


def route(state: CollectState) -> Literal["fetch_fallback", "parse_normalize"]:
    """`fetch_fallback` when the fallback planned specs, else `parse_normalize` (§9.2 edges)."""
    return "fetch_fallback" if state["fallback_fetch_request_ids"] else "parse_normalize"
