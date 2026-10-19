"""Node 6, `resolve_identity` (spec §8.6, §7.16.1, §9.2): each pending raw item of the run becomes a sighting.

It passes `collect.before_resolve_identity` first (crash case C5b). Then `identity.resolve_run_items`
resolves the run's `pending` items, oldest first, one transaction each (crash case C6 dies inside one),
`identity.resolve_deferred_items` the `deferred` items of earlier runs whose jobs are no longer fenced (at
most `identity.DEFERRED_LIMIT`), and `snapshots.apply_run_snapshots` applies the run's ATS snapshots that are
not applied yet (§8.11), one transaction each under the run's owner row, after the items, so a member's
sighting is counted by its item alone. Then the grouping pass (§8.7, crash case C6b dies before it): it passes
`collect.identity.before_grouping` and `groups.regroup_run_jobs` regroups every unmerged job with a
`job_revision_occurrences` row since the run's `started_at` (the durable trace of "created or changed in this
run", never an in-memory result), one transaction each, under the company lock, then the job rows, then the
run's owner row.

A resumed attempt continues every `pending` item from its stored `normalized` intermediate; the terminal
dispositions are never touched again. The update's `counters` are what this call did:

- the run's own items, by resolution: `raw_items_linked`, `raw_items_out_of_scope`, `raw_items_rejected`
  and `raw_items_deferred` (the reducer adds node 5's `raw_items_out_of_scope` and `raw_items_rejected` to
  the same names: in the state they are the run's totals of those dispositions);
- the deferred items of earlier runs, by resolution: `deferred_relinked`, `deferred_out_of_scope`,
  `deferred_rejected`, and `deferred_still_fenced` for an item fenced again before its transaction;
- of both passes: `jobs_created`, `merges`, `conflicts` and `revisions_new`;
- of the snapshot pass (`snapshots.SNAPSHOT_COUNTERS`): `availability_closed`, `availability_unknown`,
  `availability_revived` and `mass_events_opened`;
- of the grouping pass (`groups.GROUPING_COUNTERS`): `groups_formed`, `groups_changed` and `elections`.

The run's own totals are its rows (I1, §17.2), whichever attempt wrote them. A resumed attempt runs the
grouping pass again: a job already grouped is confirmed and counts nothing.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from jobhunter.availability import snapshots
from jobhunter.dedup import groups, identity
from jobhunter.faults import afault_point
from jobhunter.graphs.collect.deps import CollectDeps
from jobhunter.graphs.collect.nodes import Node
from jobhunter.graphs.collect.state import CollectState, CollectUpdate
from jobhunter.worker.runs import RunOwner

RESOLVE_IDENTITY: Final = "resolve_identity"
BEFORE_GROUPING: Final = "collect.identity.before_grouping"
# The counter of each resolution of the deferred pass (items of earlier runs).
DEFERRED_COUNTERS: Final[Mapping[identity.Resolution, str]] = MappingProxyType(
    {
        "linked": "deferred_relinked",
        "out_of_scope": "deferred_out_of_scope",
        "rejected": "deferred_rejected",
        "deferred": "deferred_still_fenced",
    }
)


def make_resolve_identity(deps: CollectDeps) -> Node:
    async def resolve_identity(state: CollectState) -> CollectUpdate:
        await afault_point("collect.before_resolve_identity")
        owner = RunOwner(state["run_id"], state["attempt_no"])
        tally = identity.IdentityTally()
        run = await identity.resolve_run_items(deps.engine, owner, clock=deps.clock, tally=tally)
        deferred = await identity.resolve_deferred_items(deps.engine, owner, tally=tally)
        counters = {f"raw_items_{resolution}": run.get(resolution, 0) for resolution in identity.RESOLUTIONS}
        counters |= {name: deferred.get(resolution, 0) for resolution, name in DEFERRED_COUNTERS.items()}
        counters |= {
            "jobs_created": tally.jobs_created,
            "merges": tally.merges,
            "conflicts": tally.conflicts,
            "revisions_new": tally.revisions_new,
        }
        counters |= await snapshots.apply_run_snapshots(deps.engine, owner, clock=deps.clock)
        await afault_point(BEFORE_GROUPING)
        counters |= await groups.regroup_run_jobs(deps.engine, owner)
        return {"counters": counters, "steps": [RESOLVE_IDENTITY]}

    return resolve_identity
