"""The purge fences' checks of the writers and of the privacy requests (spec §20.2; plan M5 Task 8, Deviation
14, P2, D21).

**The writers of a job's rows** (§20.2's consumer table, M5's rows): each takes the job rows `FOR SHARE`
(step 4 of the lock schedule, §3.3.3) and then checks `is_fenced`, in the transaction that writes. A purge's
chunk takes the same rows `FOR UPDATE`, so it either deletes what a writer committed before it or meets a
writer that saw its fence. The check is a statement of its own after the lock: a writer that waited for a
chunk's lock reads the fence, which committed before the chunk began. A job found gone (a purge deleted it
after the writer read its work) counts as fenced: nothing is written for it, so a late writer never
re-creates a row of a deleted job.

- `lock_unfenced_jobs(conn, job_ids, *, llm_tuple_id=None)`: the rows `FOR SHARE` in id order, in one
  statement, then the check; the ids of the jobs that exist and that no unreleased fence matches
  (`is_fenced(job, tuple)`: a `profile` fence matches the tuples of its version only);
- `fenced_jobs(conn, job_ids, *, llm_tuple_id=None)`: the check alone, for a caller that holds the rows; the
  ids an unreleased fence matches;
- `purge_in_progress()`: the refusal of a user's write that meets a fenced job, 409 `conflict_state` with
  `detail = "purge_in_progress"` (M4 Ruling P3's mapping, the problem `api.errors.domain_problem` answers a
  `FencedOut` with);
- `NO_OPEN_FENCE_SQL`: "no fence is open", the uncorrelated `NOT EXISTS` a selection ORs before `is_fenced`
  (D21): an InitPlan, run once, that spares every row the function while no fence stands.

**The privacy requests** (`request_golden_case_delete`, `request_source_purge`, `request_export`: Tasks 12,
15 and 17), which the edge and the write guard exempt (`api.edge.fence_exempt`), call
`refuse_under_global_fence(conn)` right after they lock `active_config` (delete-everything commits its fence
under `active_config FOR UPDATE`): while the `global` fence stands they are refused with 409
`delete_everything_in_progress` (`db.fence_guard.delete_everything_in_progress`).
"""

from collections.abc import Collection
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.fence_guard import delete_everything_in_progress
from jobhunter.db.guards import global_fence_active
from jobhunter.db.sql import q
from jobhunter.errors import Refusal

PURGE_IN_PROGRESS: Final = "purge_in_progress"
NO_OPEN_FENCE_SQL: Final = "NOT EXISTS (SELECT 1 FROM purge_fences f WHERE f.released_at IS NULL)"

_LOCK: Final = q("SELECT id FROM jobs WHERE id = ANY(CAST(:ids AS bigint[])) ORDER BY id FOR SHARE")
_FENCED: Final = q(
    "SELECT j.id FROM unnest(CAST(:ids AS bigint[])) AS j(id) "  # noqa: S608 (the module's constant)
    f"WHERE NOT {NO_OPEN_FENCE_SQL} AND is_fenced(j.id, CAST(:tuple AS bigint)) ORDER BY j.id"
)


def purge_in_progress() -> Refusal:
    """The refusal of a user's write that meets a fenced job (module docstring)."""
    return Refusal(409, "conflict_state", "A purge is in progress", detail=PURGE_IN_PROGRESS)


async def fenced_jobs(
    conn: AsyncConnection, job_ids: Collection[int], *, llm_tuple_id: int | None = None
) -> frozenset[int]:
    """The jobs of `job_ids` that an unreleased purge fence matches (for `llm_tuple_id`'s profile too, when
    given); the caller holds their rows (module docstring)."""
    if not job_ids:
        return frozenset()
    params = {"ids": sorted(set(job_ids)), "tuple": llm_tuple_id}
    return frozenset(int(job_id) for job_id in (await conn.execute(_FENCED, params)).scalars())


async def lock_unfenced_jobs(
    conn: AsyncConnection, job_ids: Collection[int], *, llm_tuple_id: int | None = None
) -> frozenset[int]:
    """The rows of `job_ids` `FOR SHARE` in id order (step 4), then their fences: the jobs that exist and that
    no unreleased fence matches (module docstring)."""
    if not job_ids:
        return frozenset()
    held = [int(job_id) for job_id in (await conn.execute(_LOCK, {"ids": sorted(set(job_ids))})).scalars()]
    return frozenset(held) - await fenced_jobs(conn, held, llm_tuple_id=llm_tuple_id)


async def refuse_under_global_fence(conn: AsyncConnection) -> None:
    """Refuse a privacy request while delete-everything's `global` fence stands (module docstring); the caller
    holds `active_config`."""
    if await global_fence_active(conn):
        raise delete_everything_in_progress()
