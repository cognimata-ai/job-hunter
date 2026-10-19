"""Node 3, `plan_fallback` (spec §6.6, §9.2): the SerpApi specs of the JSearch cells the run did not serve.

It runs once every primary spec is terminal. In a run that includes paid sources it makes the free SerpApi
account read for the projection (`plan.serpapi_account`; the acquisition reads the account again before every
search) and calls `planner.plan_fallback` in one transaction. `route` sends the graph to `fetch_fallback` when
it planned specs, else straight to `parse_normalize`.
"""

from typing import Literal

from jobhunter.db.sql import q
from jobhunter.graphs.collect.deps import CollectDeps
from jobhunter.graphs.collect.nodes import Node
from jobhunter.graphs.collect.nodes.plan import serpapi_account
from jobhunter.graphs.collect.state import CollectState, CollectUpdate
from jobhunter.quota import planner
from jobhunter.run_settings import load_run_settings
from jobhunter.sources.instances import list_instances
from jobhunter.worker.runs import RunOwner

_INCLUDE_PAID = q("SELECT include_paid FROM runs WHERE id = :i")


def make_plan_fallback(deps: CollectDeps) -> Node:
    async def plan_fallback(state: CollectState) -> CollectUpdate:
        owner = RunOwner(state["run_id"], state["attempt_no"])
        async with deps.engine.connect() as conn:
            include_paid = bool((await conn.execute(_INCLUDE_PAID, {"i": owner.run_id})).scalar_one())
            settings = await load_run_settings(conn, state["settings_version_id"])
            instances = await list_instances(conn)
        account = await serpapi_account(deps, settings, instances) if include_paid else None
        async with deps.engine.begin() as conn:
            ids = await planner.plan_fallback(
                conn, owner=owner, settings=settings, instances=instances, now=deps.clock(), account=account
            )
        return {"fallback_fetch_request_ids": ids, "steps": ["plan_fallback"]}

    return plan_fallback


def route(state: CollectState) -> Literal["fetch_fallback", "parse_normalize"]:
    """`fetch_fallback` when the fallback planned specs, else `parse_normalize` (§9.2 edges)."""
    return "fetch_fallback" if state["fallback_fetch_request_ids"] else "parse_normalize"
