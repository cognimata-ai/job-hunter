"""Node 8, `hard_filters` (spec §9.2): verification and backfill of the hard-filter results.

Node 6 writes the job's `hard_filter_results` for the active and the pending scope in the transaction that
changes the job, so normally there is nothing to do here. For each job the run touched (`jobs.updated_at` at
or after the run's `started_at`) that lacks a result for the active or the pending scope, or whose result has
an `inputs_hash` other than `jobs.det_facts_hash` or another `det_algorithm_version`, the node recomputes it
under `active_config` `FOR SHARE` (`filters.recompute_stale`), `BATCH` jobs in a transaction, until none is
left. Its update's `counters["hard_filter_backfilled"]` is how many jobs it recomputed.

The node writes an idempotent projection of the jobs, not an owner's state, so it is not fenced by the run's
attempt: a superseded attempt's pass writes what the next attempt's would.
"""

from typing import Final

from jobhunter.db.sql import q
from jobhunter.graphs.collect.deps import CollectDeps
from jobhunter.graphs.collect.nodes import Node
from jobhunter.graphs.collect.state import CollectState, CollectUpdate
from jobhunter.scoring import filters

HARD_FILTERS: Final = "hard_filters"
HARD_FILTER_BACKFILLED: Final = "hard_filter_backfilled"
_STARTED_AT: Final = q("SELECT started_at FROM runs WHERE id = :run")


def make_hard_filters(deps: CollectDeps) -> Node:
    async def hard_filters(state: CollectState) -> CollectUpdate:
        async with deps.engine.connect() as conn:
            started_at = (await conn.execute(_STARTED_AT, {"run": state["run_id"]})).scalar_one()
        if started_at is None:
            raise LookupError(f"run {state['run_id']} has not started")
        backfilled = 0
        while True:
            async with deps.engine.begin() as conn:
                count = await filters.recompute_stale(
                    conn, await filters.active_scopes(conn), since=started_at
                )
            backfilled += count
            if count < filters.BATCH:
                break
        return {"counters": {HARD_FILTER_BACKFILLED: backfilled}, "steps": [HARD_FILTERS]}

    return hard_filters
