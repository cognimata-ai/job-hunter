"""Node 8, `hard_filters` (spec §9.2): verification and backfill of the hard-filter results.

Node 6 writes the job's `hard_filter_results` for the active and the pending scope in the transaction that
changes the job, so normally there is nothing to do here. For each job the run touched (`jobs.updated_at` at
or after the run's `started_at`) that lacks a result for the active or the pending scope, or whose result has
an `inputs_hash` other than `jobs.det_facts_hash` or another `det_algorithm_version`, the node recomputes it
under `active_config` `FOR SHARE` (`filters.recompute_stale`), `BATCH` jobs in a transaction, until a pass
finds none. Its update's `counters["hard_filter_backfilled"]` is how many jobs it recomputed.

The node is fenced as every owner write is (§3.3.3): in each batch transaction the job rows are locked first,
then `ledger.lock_run_owner` takes the run's owner row `FOR SHARE` with its predicate, so a superseded
attempt's pass raises `FencedOut` (which ends the attempt quietly) before it writes anything.
"""

from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.graphs.collect.deps import CollectDeps
from jobhunter.graphs.collect.nodes import Node
from jobhunter.graphs.collect.state import CollectState, CollectUpdate
from jobhunter.quota.ledger import lock_run_owner
from jobhunter.scoring import filters
from jobhunter.worker.runs import RunOwner

HARD_FILTERS: Final = "hard_filters"
HARD_FILTER_BACKFILLED: Final = "hard_filter_backfilled"
_STARTED_AT: Final = q("SELECT started_at FROM runs WHERE id = :run")


def make_hard_filters(deps: CollectDeps) -> Node:
    async def hard_filters(state: CollectState) -> CollectUpdate:
        owner = RunOwner(state["run_id"], state["attempt_no"])

        async def fenced(conn: AsyncConnection) -> None:
            await lock_run_owner(conn, owner)

        async with deps.engine.connect() as conn:
            started_at = (await conn.execute(_STARTED_AT, {"run": owner.run_id})).scalar_one()
        if started_at is None:
            raise LookupError(f"run {owner.run_id} has not started")
        backfilled = 0
        while True:
            async with deps.engine.begin() as conn:
                scopes = await filters.active_scopes(conn)
                count = await filters.recompute_stale(conn, scopes, guard=fenced, since=started_at)
            if count == 0:
                break
            backfilled += count
        return {"counters": {HARD_FILTER_BACKFILLED: backfilled}, "steps": [HARD_FILTERS]}

    return hard_filters
