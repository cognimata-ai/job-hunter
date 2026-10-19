"""Node 6, `resolve_identity` (spec §8.6, §7.16.1, §9.2): each pending raw item of the run becomes a sighting.

It passes `collect.before_resolve_identity` first (crash case C5b). Then `identity.resolve_run_items`
resolves the run's `pending` items, oldest first, one transaction each (crash case C6 dies inside one), and
`identity.resolve_deferred_items` the `deferred` items of earlier runs whose jobs are no longer fenced (at
most `identity.DEFERRED_LIMIT`). Task 19 adds the pass over the run's ATS snapshots and Task 20 the grouping.

A resumed attempt continues every `pending` item from its stored `normalized` intermediate; the terminal
dispositions are never touched again. The update's `counters` are what this call did: the resolutions of
both passes (`raw_items_linked`, `raw_items_out_of_scope`, `raw_items_rejected`, `raw_items_deferred`) and
`jobs_created`, `merges`, `conflicts` and `revisions_new`. The run's own totals are its rows (I1, §17.2),
whichever attempt wrote them.
"""

from typing import Final

from jobhunter.dedup import identity
from jobhunter.faults import afault_point
from jobhunter.graphs.collect.deps import CollectDeps
from jobhunter.graphs.collect.nodes import Node
from jobhunter.graphs.collect.state import CollectState, CollectUpdate
from jobhunter.worker.runs import RunOwner

RESOLVE_IDENTITY: Final = "resolve_identity"


def make_resolve_identity(deps: CollectDeps) -> Node:
    async def resolve_identity(state: CollectState) -> CollectUpdate:
        await afault_point("collect.before_resolve_identity")
        owner = RunOwner(state["run_id"], state["attempt_no"])
        tally = identity.IdentityTally()
        now = deps.clock()
        run = await identity.resolve_run_items(deps.engine, owner, now=now, clock=deps.clock, tally=tally)
        deferred = await identity.resolve_deferred_items(deps.engine, owner, now=deps.clock(), tally=tally)
        counters = {
            f"raw_items_{resolution}": run.get(resolution, 0) + deferred.get(resolution, 0)
            for resolution in identity.RESOLUTIONS
        }
        counters |= {
            "jobs_created": tally.jobs_created,
            "merges": tally.merges,
            "conflicts": tally.conflicts,
            "revisions_new": tally.revisions_new,
        }
        return {"counters": counters, "steps": [RESOLVE_IDENTITY]}

    return resolve_identity
