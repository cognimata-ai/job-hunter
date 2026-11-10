"""The purge fences' checks of the writers and of the privacy requests (spec §20.2; plan M5 Task 8, Deviation
14, P2, D21).

**The writers of a job's rows** (§20.2's consumer table, M5's rows): each takes the job rows `FOR SHARE`
(step 4 of the lock schedule, §3.3.3) and then checks `is_fenced`, in the transaction that writes. This module
is the one home of that lock and that check: `scoring.targets.lock_jobs` and `dedup.merge.fenced_jobs`
delegate to it (M5 Task 8 fix round 1). A purge's
chunk takes the same rows `FOR UPDATE`, so it either deletes what a writer committed before it or meets a
writer that saw its fence. The check is a statement of its own after the lock: a writer that waited for a
chunk's lock reads the fence, which committed before the chunk began. A job found gone (a purge deleted it
after the writer read its work) counts as fenced: nothing is written for it, so a late writer never
re-creates a row of a deleted job.

- `lock_jobs_shared(conn, job_ids)`: the rows `FOR SHARE` in id order, in one statement; the ids held (a job
  that does not exist is absent);
- `lock_unfenced_jobs(conn, job_ids, *, llm_tuple_id=None)`: that lock, then the check; the ids of the jobs
  that exist and that no unreleased fence matches (`is_fenced(job, tuple)`: a `profile` fence matches the
  tuples of its version only);
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

**The fence writers** (M5 Task 9; §7.14, §19.5 step 3). A purge's A0 transaction inserts its manifest
(`manifests.insert_manifest`) and its fences, `insert_fence(conn, manifest_id=..., spec=FenceSpec(...))`: a
`global`, a `source` (its source; the stored affected jobs are added at A3 by `manifests.record_manifest`),
a `profile` (its version) or a `jobs` fence (the batch, at least one job), the job ids distinct and in order.
The completing transaction releases them, `release_fences(conn, manifest_id)`: `released_at := now()` on every
active fence of the manifest, the number released. Neither commits.

**The quiesce** (D1). A transaction that began before A0 committed cannot see the fence, so once A0 has
committed the purge reads its commit cutoff, `fence_committed_at(engine)` (`clock_timestamp()` in a statement
of its own, after the commit: the manifest's `fenced_at`), and waits, `wait_for_quiesce(engine, since=...,
clock=..., sleep=...)`, until no transaction of the application role that began before it is still open:
`pg_stat_activity` rows of `jobhunter_app` in this database (controller ruling T9-1: a transaction in another
database cannot touch this one's rows, and the test server hosts many) other than the polling backend with
`xact_start < since`. An idle session (procrastinate's `LISTEN`, a pooled connection) has no `xact_start`.
The application role sees its own sessions' `xact_start`. Each poll is a transaction of its own on a pooled
connection (`pg_stat_activity` is a snapshot per transaction), and nothing is held open between polls, every
`QUIESCE_POLL_S` by the caller's `sleep`; the caller holds no transaction of its own meanwhile. Past
`QUIESCE_TIMEOUT` by the caller's `clock` it returns `False` (the purge then waits and tries again), else
`True`.
"""

from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.db.fence_guard import delete_everything_in_progress
from jobhunter.db.guards import global_fence_active
from jobhunter.db.sql import q
from jobhunter.errors import Refusal
from jobhunter.privacy.manifests import FenceKind

PURGE_IN_PROGRESS: Final = "purge_in_progress"
NO_OPEN_FENCE_SQL: Final = "NOT EXISTS (SELECT 1 FROM purge_fences f WHERE f.released_at IS NULL)"
QUIESCE_POLL_S: Final = 1.0
QUIESCE_TIMEOUT: Final = timedelta(minutes=10)
# The application role's own transactions that began before `:since`, in this database (module docstring).
_OLDER_OPEN: Final = q(
    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE usename = 'jobhunter_app' "
    "AND datname = current_database() AND pid <> pg_backend_pid() AND xact_start < :since)"
)
_COMMITTED_AT: Final = q("SELECT clock_timestamp()")
_INSERT_FENCE: Final = q(
    "INSERT INTO purge_fences (kind, source_id, profile_version_id, job_ids, manifest_id) "
    "VALUES (:kind, :source_id, :profile_version_id, CAST(:job_ids AS bigint[]), :manifest_id) RETURNING id"
)
_RELEASE: Final = q(
    "UPDATE purge_fences SET released_at = now() WHERE manifest_id = :m AND released_at IS NULL RETURNING id"
)

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


async def lock_jobs_shared(conn: AsyncConnection, job_ids: Collection[int]) -> frozenset[int]:
    """The rows of `job_ids` `FOR SHARE` in id order, in one statement (step 4); the ids held."""
    if not job_ids:
        return frozenset()
    return frozenset(
        int(job_id) for job_id in (await conn.execute(_LOCK, {"ids": sorted(set(job_ids))})).scalars()
    )


async def lock_unfenced_jobs(
    conn: AsyncConnection, job_ids: Collection[int], *, llm_tuple_id: int | None = None
) -> frozenset[int]:
    """The rows of `job_ids` `FOR SHARE` in id order (step 4), then their fences: the jobs that exist and that
    no unreleased fence matches (module docstring)."""
    held = await lock_jobs_shared(conn, job_ids)
    return held - await fenced_jobs(conn, held, llm_tuple_id=llm_tuple_id)


async def refuse_under_global_fence(conn: AsyncConnection) -> None:
    """Refuse a privacy request while delete-everything's `global` fence stands (module docstring); the caller
    holds `active_config`."""
    if await global_fence_active(conn):
        raise delete_everything_in_progress()


@dataclass(frozen=True)
class FenceSpec:
    """A fence to raise: its kind and the key the kind needs (§7.14's CHECKs refuse any other combination)."""

    kind: FenceKind
    source_id: str | None = None
    profile_version_id: int | None = None
    job_ids: tuple[int, ...] = ()


async def insert_fence(conn: AsyncConnection, *, manifest_id: int, spec: FenceSpec) -> int:
    """Raise `spec`'s fence for the manifest `manifest_id`, its job ids distinct and in order; its id (module
    docstring). The caller commits it with the manifest (A0)."""
    params = {
        "kind": spec.kind,
        "source_id": spec.source_id,
        "profile_version_id": spec.profile_version_id,
        "job_ids": sorted(set(spec.job_ids)),
        "manifest_id": manifest_id,
    }
    return int((await conn.execute(_INSERT_FENCE, params)).scalar_one())


async def release_fences(conn: AsyncConnection, manifest_id: int) -> int:
    """Release every active fence of the manifest (`released_at := now()`); the number released."""
    return len((await conn.execute(_RELEASE, {"m": manifest_id})).all())


async def fence_committed_at(engine: AsyncEngine) -> datetime:
    """The database's `clock_timestamp()`, read in a transaction of its own after the fence's committed: the
    cutoff `wait_for_quiesce` waits from (module docstring)."""
    async with engine.connect() as conn:
        committed: datetime = (await conn.execute(_COMMITTED_AT)).scalar_one()
    return committed


async def _older_open(engine: AsyncEngine, since: datetime) -> bool:
    async with engine.connect() as conn:  # a transaction of its own, rolled back when the block ends
        return bool((await conn.execute(_OLDER_OPEN, {"since": since})).scalar_one())


async def wait_for_quiesce(
    engine: AsyncEngine,
    *,
    since: datetime,
    clock: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]],
) -> bool:
    """Wait until no application transaction that began before `since` is open (D1, module docstring): `True`
    once none is, `False` once `QUIESCE_TIMEOUT` has passed on `clock` with one still open."""
    deadline = clock() + QUIESCE_TIMEOUT
    while await _older_open(engine, since):
        if clock() >= deadline:
            return False
        await sleep(QUIESCE_POLL_S)
    return True
