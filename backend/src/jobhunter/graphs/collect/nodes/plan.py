"""Node 1, `plan` (spec §9.2): the run's settings and its primary fetch requests.

The node reads the run row, its settings version (`load_run_settings`) and the source instances, makes the
free SerpApi account read when the run includes paid sources and SerpApi is enabled (`serpapi_account`: for
the projection only, outside any transaction; a read that fails is no read), and calls `planner.plan_run` in
one transaction. It returns the run's settings, scope and tuple ids, `plan_done` and the primary fetch request
ids to fan out.
"""

from collections.abc import Sequence

from jobhunter.db.sql import q
from jobhunter.graphs.collect.deps import CollectDeps
from jobhunter.graphs.collect.nodes import Node
from jobhunter.graphs.collect.state import CollectState, CollectUpdate
from jobhunter.observability.logging import get_logger
from jobhunter.quota.planner import plan_run
from jobhunter.quota.windows import AccountObservation
from jobhunter.run_settings import RunSettings, load_run_settings
from jobhunter.sources.acquire import AccountAdapter
from jobhunter.sources.base import SourceError
from jobhunter.sources.instances import InstanceRow, list_instances
from jobhunter.worker.runs import RunOwner

_RUN = q("SELECT settings_version_id, scope_version_id, llm_tuple_id, include_paid FROM runs WHERE id = :i")

log = get_logger("jobhunter.graphs.collect.plan")


async def serpapi_account(
    deps: CollectDeps, settings: RunSettings, instances: Sequence[InstanceRow]
) -> AccountObservation | None:
    """The free SerpApi account read for a projection (§5.3.2), or `None`: SerpApi disabled (by settings or
    health), an adapter without the read, or a read that failed."""
    instance = next((i for i in instances if i.source_id == "serpapi"), None)
    if not settings.sources.serpapi or instance is None or instance.health == "disabled":
        return None
    adapter = deps.adapters(instance)
    if not isinstance(adapter, AccountAdapter):
        return None
    try:
        return await adapter.account()
    except SourceError as err:
        log.warning("account_read_failed", error_kind=err.kind, error_code=err.code)
        return None


def make_plan(deps: CollectDeps) -> Node:
    async def plan(state: CollectState) -> CollectUpdate:
        owner = RunOwner(state["run_id"], state["attempt_no"])
        async with deps.engine.connect() as conn:
            run = (await conn.execute(_RUN, {"i": owner.run_id})).one()
            settings = await load_run_settings(conn, int(run.settings_version_id))
            instances = await list_instances(conn)
        include_paid = bool(run.include_paid)
        account = await serpapi_account(deps, settings, instances) if include_paid else None
        async with deps.engine.begin() as conn:
            report = await plan_run(
                conn,
                owner=owner,
                include_paid=include_paid,
                settings=settings,
                instances=instances,
                now=deps.clock(),
                account=account,
            )
        return {
            "settings_version_id": settings.settings_version_id,
            "scope_version_id": int(run.scope_version_id),
            "llm_tuple_id": None if run.llm_tuple_id is None else int(run.llm_tuple_id),
            "plan_done": True,
            "primary_fetch_request_ids": report.primary_fetch_request_ids,
            "steps": ["plan"],
        }

    return plan
