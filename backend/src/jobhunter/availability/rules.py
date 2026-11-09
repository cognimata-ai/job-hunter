"""Availability rules (spec §7.16.2, §8.11): a posting's sighting, its membership in an ATS snapshot, a job's
availability from its postings, and the stale sweep of aggregator postings.

**`apply_sighting`** is the observation accounting of an existing posting, called by identity resolution only
(§8.6 step 4; Task 18's statements, moved here unchanged): `seen_count + 1`; `last_seen_at := greatest`;
`latest_observation_id` only when `observed_at ≥` the latest's `observed_at`; when `observed_at >
availability_changed_at`, the absence reset (also only after `first_absent_at`) and the revival of a `stale`,
`unknown` or `closed` posting through `transition()` (a `closed` one logs `posting_reopened`), with
`availability_changed_at := observed_at` (Ruling T18-d: the sighting's own time, so later evidence compares in
one time domain).

**`apply_membership`** is what a snapshot member gets (§8.11: "a member of any snapshot"), called by
`snapshots.apply_snapshot` only: the same revival rule and absence reset at the snapshot's `taken_at`, and
never `seen_count`, `last_seen_at` or `latest_observation_id`, which belong to the observation an item
produced (or to none, for an item that failed normalisation).

**`recompute_job_availability`** reuses `merge.derive_from_postings`, the one place a job's availability is
derived: the best state of its postings by `JOB_ORDER`, `jobs.availability_changed_at := now()` when it
changes. It returns whether a list column changed; the caller bumps the ranking once, as its last statement.
A job whose availability changes to `closed` (every posting closed: an ATS snapshot's absence) has its
`dedup.ambiguous_grouping` alert resolved: the choice between its groups no longer matters (M5 Task 3,
O-ops O17, M1 Carry-forward 11; a tombstoned job's is the purge's).

**Locks.** `lock_posting_jobs` takes the jobs of a set of postings, with their duplicate-group co-members, in
ONE ordered `FOR UPDATE` statement (`merge.lock_jobs`, Ruling T18-c), in a savepoint per round: the postings'
jobs are read, locked, and read again; a posting that moved to a job the round does not hold (a merge or an
unmerge between the read and the lock) rolls the round back, which releases its locks, and a fresh round
locks the new set in id order. No posting's availability changes without its job row held, so the caller's
re-check of its postings under these locks is stable.

**`stale_sweep`** (§8.11): one bounded batch, in the lock order of §3.3.3 (Ruling T19-a): the batch is chosen
by a plain read (`active` postings of non-ATS instances with `now - last_seen_at > stale_ttl_days`, in id
order, at most `limit`); their jobs are locked (`lock_posting_jobs`); then the caller's `guard` (the
reconciler's `fence_epoch`); then the batch is re-checked under the posting rows' `FOR UPDATE` and each
posting moves `active → stale` (no reason; `availability_changed_at` is the database's `now()`, carry 3);
then the jobs are recomputed and, last, `ranking_state` bumped once when a job changed. `now` is the caller's
clock for the TTL only. §17.4's `stale_sweep` is two reconcilers, each batch a transaction of its own fenced
by the worker epoch, with `stale_ttl_days` of the active settings (§13.1: `collection.stale_ttl_days`, or
`max_ad_age_days` when `null`): `stale_sweep` (cadence `tick`) runs one batch of `STALE_BATCH`;
`stale_sweep_daily` (cadence `daily`, the `retention_daily` task) runs batches until one is short (M-2).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db import ranking
from jobhunter.db.guards import Guard
from jobhunter.db.sql import q
from jobhunter.db.transitions import CLEAR_REASON, transition
from jobhunter.dedup import merge
from jobhunter.dedup.representative import AMBIGUOUS_GROUPING
from jobhunter.errors import CoMemberChanged, FencedOut, InvariantViolation
from jobhunter.observability.alerts import resolve_alert
from jobhunter.observability.logging import get_logger
from jobhunter.run_settings import load_run_settings
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.reconcilers import ReconcileContext, reconciler

# The order of a job's availability (§7.16.2): the first state its postings hold (`derive_from_postings`).
JOB_ORDER: Final = merge.AVAILABILITY_ORDER
STALE_BATCH: Final = 1000  # postings per stale-sweep batch (§8.11, §17.4: "tick (≤ 1,000 rows)")
_LOCK_ROUNDS: Final = 3  # read-then-lock rounds before a posting set that keeps moving is an invariant breach

# A revival's change time is the sighting's or the snapshot's (Ruling T18-d); `transition()` wrote `now()`.
_REVIVED_AT: Final = q(
    "UPDATE postings SET availability_changed_at = :t WHERE id = :posting AND availability = 'active' "
    "RETURNING id"
)
# The sighting of an existing posting (§7.16.2). `:t` is the sighting time; the conditions are constants of
# this module, never input.
_NOT_LATE: Final = (
    "CAST(:t AS timestamptz) > p.availability_changed_at "
    "AND (p.first_absent_at IS NULL OR CAST(:t AS timestamptz) > p.first_absent_at)"
)
_SIGHTING: Final = q(
    "UPDATE postings AS p SET seen_count = p.seen_count + 1, "  # noqa: S608
    "last_seen_at = greatest(p.last_seen_at, CAST(:t AS timestamptz)), "
    "latest_observation_id = CASE WHEN p.latest_observation_id IS NULL OR CAST(:t AS timestamptz) >= "
    "(SELECT o.observed_at FROM posting_observations o WHERE o.id = p.latest_observation_id) "
    "THEN CAST(:observation AS bigint) ELSE p.latest_observation_id END, "
    f"absent_snapshots = CASE WHEN {_NOT_LATE} THEN 0 ELSE p.absent_snapshots END, "
    f"first_absent_at = CASE WHEN {_NOT_LATE} THEN NULL ELSE p.first_absent_at END "
    "WHERE p.id = :posting RETURNING p.availability, p.availability_changed_at, p.latest_observation_id"
)
# A snapshot member (§8.11): the absence reset alone; no sighting is counted.
_MEMBERSHIP: Final = q(
    "UPDATE postings AS p SET "  # noqa: S608
    f"absent_snapshots = CASE WHEN {_NOT_LATE} THEN 0 ELSE p.absent_snapshots END, "
    f"first_absent_at = CASE WHEN {_NOT_LATE} THEN NULL ELSE p.first_absent_at END "
    "WHERE p.id = :posting RETURNING p.availability, p.availability_changed_at"
)
_JOBS_OF: Final = q("SELECT id, job_id FROM postings WHERE id = ANY(CAST(:ids AS bigint[]))")
# The batch of the stale sweep: `active` postings of non-ATS instances last seen before the cutoff.
_STALE_BATCH: Final = q(
    "SELECT p.id FROM postings p JOIN source_instances i ON i.id = p.source_instance_id "
    "JOIN sources s ON s.id = i.source_id WHERE p.availability = 'active' AND s.kind <> 'ats' "
    "AND p.last_seen_at < :cutoff ORDER BY p.id LIMIT :limit"
)
# The batch re-checked under the job locks (a posting sighted meanwhile drops out), its rows locked.
_LOCK_STALE: Final = q(
    "SELECT id, job_id FROM postings WHERE id = ANY(CAST(:ids AS bigint[])) "
    "AND job_id = ANY(CAST(:jobs AS bigint[])) AND availability = 'active' AND last_seen_at < :cutoff "
    "ORDER BY id FOR UPDATE"
)
_ACTIVE_SETTINGS: Final = q("SELECT settings_version_id FROM active_config WHERE id = 1")
_JOB_AVAILABILITY: Final = q("SELECT availability FROM jobs WHERE id = :job")

log = get_logger("jobhunter.availability.rules")


@dataclass(frozen=True)
class SightingEffect:
    """What a sighting did: the posting revived, and the observation became its latest."""

    revived: bool
    latest_moved: bool


async def _revive(
    conn: AsyncConnection, posting_id: int, row: Row[tuple[object, ...]], t: datetime, **context: object
) -> bool:
    """`stale | unknown | closed → active` when `t > availability_changed_at`, changed at `t`."""
    state = str(row.availability)
    if state == "active" or not t > row.availability_changed_at:
        return False
    await transition(conn, "postings", posting_id, [state], "active", reason=CLEAR_REASON)
    if (await conn.execute(_REVIVED_AT, {"posting": posting_id, "t": t})).first() is None:
        raise InvariantViolation(f"posting {posting_id} is not active after its revival")
    if state == "closed":
        log.info("posting_reopened", posting_id=posting_id, **context)
    return True


async def apply_sighting(
    conn: AsyncConnection, posting_id: int, *, observed_at: datetime, observation_id: int
) -> SightingEffect:
    """The sighting of an existing posting at `observed_at` by the new observation `observation_id`
    (§7.16.2; module docstring), in the caller's transaction, which holds the posting's job row and the
    posting row. Called by identity resolution only. The caller recomputes the job."""
    row = (
        await conn.execute(
            _SIGHTING, {"posting": posting_id, "t": observed_at, "observation": observation_id}
        )
    ).one()
    revived = await _revive(conn, posting_id, row, observed_at, observation_id=observation_id)
    return SightingEffect(revived=revived, latest_moved=row.latest_observation_id == observation_id)


async def apply_membership(conn: AsyncConnection, posting_id: int, *, taken_at: datetime) -> bool:
    """The posting is a member of a snapshot taken at `taken_at` (§8.11; module docstring), in the caller's
    transaction, which holds the posting's job row and the posting row: the absence reset and the revival
    rule, no sighting counted. Called by `snapshots.apply_snapshot` only. Whether it was revived; the caller
    recomputes the job."""
    row = (await conn.execute(_MEMBERSHIP, {"posting": posting_id, "t": taken_at})).one()
    return await _revive(conn, posting_id, row, taken_at, via="snapshot_membership")


async def recompute_job_availability(conn: AsyncConnection, job_id: int) -> bool:
    """The job's availability from its postings (`merge.derive_from_postings`: the best by `JOB_ORDER`,
    `availability_changed_at` set when it changes), in the caller's transaction, which holds the job row; a
    job that closed has its `dedup.ambiguous_grouping` alert resolved (module docstring). `True` when it (or
    the job's first sighting) changed: the caller then bumps the ranking, last."""
    derived = await merge.derive_from_postings(conn, job_id)
    if derived.availability_changed:
        availability = (await conn.execute(_JOB_AVAILABILITY, {"job": job_id})).scalar_one()
        if availability == "closed":
            await resolve_alert(conn, code=AMBIGUOUS_GROUPING, subject=f"job:{job_id}")
    return derived.changed


async def _read_and_lock(conn: AsyncConnection, posting_ids: list[int]) -> frozenset[int] | None:
    read = {int(row.id): int(row.job_id) for row in await conn.execute(_JOBS_OF, {"ids": posting_ids})}
    jobs = sorted(set(read.values()))
    locked = await merge.lock_jobs(conn, jobs, with_groups_of=jobs)
    again = {int(row.id): int(row.job_id) for row in await conn.execute(_JOBS_OF, {"ids": posting_ids})}
    if any(job not in locked for job in again.values()):
        return None
    if not await merge.co_members(conn, again.values()) <= locked.keys():
        return None
    return frozenset(locked)


async def lock_posting_jobs(conn: AsyncConnection, posting_ids: Iterable[int]) -> frozenset[int]:
    """The job rows of the postings `FOR UPDATE`, with their duplicate-group co-members, in one ordered
    statement per round (module docstring); the ids of every job row held. Every posting that still exists
    is on one of them. Raises `CoMemberChanged` (an `InvariantViolation`) when the set keeps moving for
    `_LOCK_ROUNDS` rounds, having written nothing."""
    ids = sorted(set(posting_ids))
    if not ids:
        return frozenset()
    for _ in range(_LOCK_ROUNDS):
        attempt = await conn.begin_nested()
        try:
            held = await _read_and_lock(conn, ids)
        except BaseException:
            await attempt.rollback()
            raise
        if held is not None:
            await attempt.commit()  # RELEASE SAVEPOINT: the locks stay with the transaction
            return held
        await attempt.rollback()
        log.info("posting_jobs_changed_under_the_lock", postings=len(ids))
    raise CoMemberChanged(f"the jobs of {len(ids)} postings kept changing under the lock")


async def stale_sweep(
    conn: AsyncConnection,
    *,
    now: datetime,
    stale_ttl_days: int,
    limit: int = STALE_BATCH,
    guard: Guard | None = None,
) -> int:
    """One batch of the stale sweep (module docstring) in the caller's transaction: up to `limit` `active`
    postings of non-ATS instances with `now - last_seen_at > stale_ttl_days` move to `stale`, in id order.
    Returns how many did (a batch of `limit` may have left more). `guard`, when given, runs after the job
    rows are locked and before anything is written; it raises `FencedOut` to stop."""
    cutoff = now - timedelta(days=stale_ttl_days)
    batch = [
        int(posting_id)
        for posting_id in (await conn.execute(_STALE_BATCH, {"cutoff": cutoff, "limit": limit})).scalars()
    ]
    if not batch:
        return 0
    held = await lock_posting_jobs(conn, batch)
    if guard is not None:
        await guard(conn)
    rows = (await conn.execute(_LOCK_STALE, {"ids": batch, "jobs": sorted(held), "cutoff": cutoff})).all()
    for row in rows:
        await transition(conn, "postings", int(row.id), ["active"], "stale", reason=CLEAR_REASON)
    changed = False
    for job_id in sorted({int(row.job_id) for row in rows}):
        changed |= await recompute_job_availability(conn, job_id)
    if changed:
        await ranking.bump(conn)
    if rows:
        log.info("postings_stale", postings=len(rows), stale_ttl_days=stale_ttl_days)
    return len(rows)


async def _active_stale_ttl_days(ctx: ReconcileContext) -> int:
    async with ctx.engine.connect() as conn:
        version = int((await conn.execute(_ACTIVE_SETTINGS)).scalar_one())
        return (await load_run_settings(conn, version)).stale_ttl_days


async def _sweep(ctx: ReconcileContext, *, until_short: bool) -> int:
    """Batches of `STALE_BATCH` (read when called), one fenced transaction each: one batch, or batches until
    one is short. Returns how many postings went stale. A fenced batch wrote nothing: the worker's own fence
    propagates, so that `run_reconcilers` stops the cadence at the reconciler and the superseded worker
    requests its stop (Ruling FR-r; fix wave F7, Ruling FR-w); a `FencedOut` of another owner ends the sweep
    with the count so far."""
    stale_ttl_days, limit, swept = await _active_stale_ttl_days(ctx), STALE_BATCH, 0

    async def fenced(conn: AsyncConnection) -> None:
        await fence_epoch(conn, ctx.epoch)

    while True:
        try:
            async with ctx.engine.begin() as conn:
                count = await stale_sweep(
                    conn, now=ctx.now, stale_ttl_days=stale_ttl_days, limit=limit, guard=fenced
                )
        except FencedOut as fenced_out:
            if fenced_out.worker_superseded:
                raise  # `fence_epoch` logged it
            return swept
        swept += count
        if not until_short or count < limit:
            return swept


@reconciler("stale_sweep", "tick")
async def stale_sweep_reconciler(ctx: ReconcileContext) -> int:
    """§17.4 `stale_sweep`, in the tick (≤ 1,000 rows): one batch of `STALE_BATCH` postings. Returns how many
    went stale; a superseded worker writes nothing and raises its fence (`_sweep`)."""
    return await _sweep(ctx, until_short=False)


@reconciler("stale_sweep_daily", "daily")
async def stale_sweep_daily_reconciler(ctx: ReconcileContext) -> int:
    """§17.4 `stale_sweep`, daily: batches of `STALE_BATCH` until a short one (M-2). Returns how many postings
    went stale; a superseded worker raises its fence at its first batch, which wrote nothing (`_sweep`)."""
    return await _sweep(ctx, until_short=True)
