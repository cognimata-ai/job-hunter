"""Node 8, `hard_filters` (spec §9.2): verification and backfill of the hard-filter results.

Node 6 writes the job's `hard_filter_results` for the active and the pending scope in the transaction that
changes the job, so normally there is nothing to do here. For each job the run touched (`jobs.updated_at` at
or after the run's `started_at`) that lacks a result for the active or the pending scope, or whose result has
an `inputs_hash` other than `jobs.det_facts_hash` or another `det_algorithm_version`, the node recomputes it
under `active_config` `FOR SHARE` (`filters.lock_active_scopes`, then `filters.recompute_stale_active`),
`BATCH` jobs in a transaction, until a pass finds none. A batch that changed a job's `passed` under the active
scope (a first result counts, an equal rewrite does not) bumps the ranking as its transaction's last
statement: the Jobs list filters on it (§7.1 L1255, Ruling T3-a). Its update's
`counters["hard_filter_backfilled"]` is how many jobs it recomputed.

The node is fenced as every owner write is (§3.3.3): in each batch transaction the job rows are locked first,
then `ledger.lock_run_owner` takes the run's owner row `FOR SHARE` with its predicate, so a superseded
attempt's pass raises `FencedOut` (which ends the attempt quietly) before it writes anything, the ranking
bump included.
"""

from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db import ranking
from jobhunter.graphs.collect.deps import CollectDeps
from jobhunter.graphs.collect.nodes import Node, run_started_at
from jobhunter.graphs.collect.state import CollectState, CollectUpdate
from jobhunter.quota.ledger import lock_run_owner
from jobhunter.scoring import filters
from jobhunter.worker.runs import RunOwner

HARD_FILTERS: Final = "hard_filters"
HARD_FILTER_BACKFILLED: Final = "hard_filter_backfilled"


def make_hard_filters(deps: CollectDeps) -> Node:
    async def hard_filters(state: CollectState) -> CollectUpdate:
        owner = RunOwner(state["run_id"], state["attempt_no"])

        async def fenced(conn: AsyncConnection) -> None:
            await lock_run_owner(conn, owner)

        async with deps.engine.connect() as conn:
            started_at = await run_started_at(conn, owner.run_id)
        backfilled = 0
        while True:
            async with deps.engine.begin() as conn:
                active = await filters.lock_active_scopes(conn)
                recomputed = await filters.recompute_stale_active(
                    conn, active, guard=fenced, since=started_at
                )
                if recomputed.passed_changed:
                    await ranking.bump(conn)  # `ranking_state`, the last lock of this batch's transaction
            count = len(recomputed.job_ids)
            if count == 0:
                break
            backfilled += count
        return {"counters": {HARD_FILTER_BACKFILLED: backfilled}, "steps": [HARD_FILTERS]}

    return hard_filters
