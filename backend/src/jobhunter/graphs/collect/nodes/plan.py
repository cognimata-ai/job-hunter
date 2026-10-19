"""Node 1, `plan` (spec §9.2): the run's settings and its primary fetch requests.

The node reads the run row, its settings version (`load_run_settings`) and the source instances, and calls
`planner.plan_run` in one transaction. It makes no SerpApi account read (Ruling T13-c): SerpApi gets no
primary spec, so a read would change no decision; `plan_fallback` makes the one the fallback projection needs.
It returns the run's settings, scope and tuple ids, `plan_done` and the primary fetch request ids to fan out.
"""

from jobhunter.db.sql import q
from jobhunter.graphs.collect.deps import CollectDeps
from jobhunter.graphs.collect.nodes import Node
from jobhunter.graphs.collect.state import CollectState, CollectUpdate
from jobhunter.quota.planner import plan_run
from jobhunter.run_settings import load_run_settings
from jobhunter.sources.instances import list_instances
from jobhunter.worker.runs import RunOwner

_RUN = q("SELECT settings_version_id, scope_version_id, llm_tuple_id, include_paid FROM runs WHERE id = :i")


def make_plan(deps: CollectDeps) -> Node:
    async def plan(state: CollectState) -> CollectUpdate:
        owner = RunOwner(state["run_id"], state["attempt_no"])
        async with deps.engine.connect() as conn:
            run = (await conn.execute(_RUN, {"i": owner.run_id})).one()
            settings = await load_run_settings(conn, int(run.settings_version_id))
            instances = await list_instances(conn)
        async with deps.engine.begin() as conn:
            report = await plan_run(
                conn,
                owner=owner,
                include_paid=bool(run.include_paid),
                settings=settings,
                instances=instances,
                now=deps.clock(),
                account=None,
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
