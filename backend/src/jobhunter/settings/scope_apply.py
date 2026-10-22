"""Scope apply: precompute, then flip (spec §13.4; plan Tasks 17 and 18, Deviations 12 and 40).

A save of class `scope` points `active_config.pending_scope_version_id` at the desired scope and records an
`apply_scope` operation in the same transaction; the operation precomputes the scope's rows and flips the
active scope once they are complete.

**The writers of the pending pointer** (Task 17):
- `KIND` and `dedup_key(scope_version_id)` (`"apply_scope:<id>"`): at most one live operation per scope
  version, which the partial unique index `operations_live_dedup` enforces;
- `request_apply(conn, scope_version_id, *, defer)`: the `create_operation` wrapper, payload
  `{"scope_version_id": id}`. It inserts a `pending` operation and defers its job, or, when the scope has a
  live (`pending` or `running`) operation, returns that one's id (`created` false) through the dedup key;
- `set_desired_scope(conn, desired_id, *, now, defer)`: the one place that moves the pending pointer towards
  a desired scope (the settings save, Task 21's profile confirm, retry-apply and the `scope_apply_failed`
  reconciler). The caller holds `active_config FOR UPDATE` (§9.1: the first lock; it is taken again here, a
  no-op, before the pointers are read). It returns the `apply_scope` operation's id, or `None` when no apply
  is needed:
  - the desired scope is the active one (a revert): `pending := NULL`, as the CHECK forbids pending = active;
  - it is the pending one: no change, and the live operation's id (`request_apply`; when that apply has just
    failed for good and the reconciler has not cleared the pointer yet, this requests a new one, as
    retry-apply would);
  - otherwise: `pending := desired` and `request_apply`.
  The pointer's `updated_at` is the caller's `now`. A pending apply of another scope is left as it is: it ends
  obsolete at its own final check.

**The handler** (`run_apply_scope`, Task 18). It first reads, without a lock, the scope's payload, `total`
(the unmerged jobs) and the jobs already complete for the scope (`total - incomplete_jobs`, so the apply of a
scope that kept its rows, a revert, starts near the end); `progress` is `{"done", "total"}`, `done` capped at
`total`. Then, in rounds:

1. **The precompute** (§13.4 step 2), outside any `active_config` lock, in chunks of one transaction each:
   - a plain read of the pending pointer. When it no longer names this scope, the precompute stops and the
     final transaction decides: the read is only a hint (Ruling R18-1), never an end;
   - `filters.recompute_stale(conn, {sid: scope}, guard=<owner lock>)`: up to `BATCH` stale jobs `FOR SHARE`,
     then the owner row `FOR SHARE` (`lock_operation_owner`: a stale execution is fenced, having written
     nothing), then the results;
   - `set_progress`, the **last statement**. A save that holds `active_config` and reuses this live apply
     (`INSERT … ON CONFLICT` on `operations_live_dedup`) waits for an uncommitted update of the live row, so
     it waits at most for the tail of a chunk, never for a whole one.
   After the commit, the fault point `scope_apply.after_chunk` (`AFTER_CHUNK_POINT`). The chunks go on until
   one recomputes fewer than `BATCH` jobs.
2. **The final transaction** (§13.4 step 3), in the §9.1 order: `active_config FOR UPDATE`, then the owner
   row (`lock_operation_owner`), then a fresh read of the pending pointer:
   - a pointer that no longer names this scope (a newer save replaced it, or a revert cleared it): progress
     `{"obsolete": true}` and the operation `succeeded` (`finish_in`). This is the only place an apply ends
     obsolete; its rows are keyed by scope version and harmless. Every writer of the pointer holds
     `active_config FOR UPDATE`, so an apply that a writer reused for the pointer it set reads that pointer
     here and never ends obsolete under it;
   - `incomplete_jobs(conn, sid) > 0`: the transaction rolls back and the precompute runs again, at most
     `FINAL_CHECK_ROUNDS` times; a check that fails after the last re-run requeues the operation in a
     transaction of its own (`requeue_in`, `scope_incomplete`, `not_before = now + INCOMPLETE_RETRY_AFTER`);
   - otherwise the flip (`scope_version_id := pending`, `pending := NULL`, `updated_at := now`), then the
     operation `succeeded` (`finish_in`, the owner row), every open `scope.apply_failed` resolved (other
     domain rows), and `ranking.bump` last (`ranking_state`, the last lock of §9.1 and the bump's own
     contract).
   The `det_results` half of the check and the `filtered_out ⇄ pending` target moves are M3's (Deviation 12).
   A crash anywhere is retried as the operation's next attempt: the chunks are idempotent upserts, and the
   pointer flips only in a transaction whose check passed (§13.4 step 4). `now` is the worker's injected
   clock (`OperationDeps.clock`; `utcnow` for an execution without deps).

**The reconciler** `scope_apply_failed` (tick; §13.4 step 5, Deviation 12, Ruling R18-2), one transaction:
`fence_epoch`, then `active_config FOR UPDATE`. With `pending = P` and no live apply of `P`:
- the newest apply of `P` `failed`: `pending := NULL` (`updated_at` the reconciler's `now`), so the view and
  the filters stay on the old scope, and the critical alert `scope.apply_failed` (`APPLY_FAILED_ALERT`,
  subject `scope:<P>`, context ids only). A permanent failure happens inside a reclaim or the takeover sweep,
  which hold the operation row, while `active_config` comes first in §9.1, so no failure hook can clear it:
  the reconciler does, within a minute;
- otherwise (it ended another way, `cancelled` or obsolete, or there was none): `set_desired_scope(conn, P)`
  requests it again under the same lock, logged as a warning with ids. Defence in depth: the obsolete end
  above cannot leave a pointer without its apply.

**Retry-apply** (`POST /api/settings/scope/retry-apply`, in `api.routes.settings`) refuses when its scope is
already the active one or while `live_apply(conn)` finds an apply of any scope, then calls
`set_desired_scope`.

Lock order (§9.1): `active_config`, then job rows, then the operation row, then the other domain rows, then
`ranking_state`. `request_apply` and `set_desired_scope` do not log (their caller logs the ids after its
commit); the handler and the reconciler log ids and codes after their commits.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.clock import utcnow
from jobhunter.db import ranking
from jobhunter.db.sql import q
from jobhunter.errors import FencedOut
from jobhunter.faults import afault_point
from jobhunter.observability.alerts import raise_alert, resolve_alerts
from jobhunter.observability.logging import get_logger
from jobhunter.run_settings import ScopeSettings, scope_from_payload
from jobhunter.scoring.filters import BATCH, STALE_PREDICATE, recompute_stale
from jobhunter.scoring.versioning import DET_ALGORITHM_VERSION
from jobhunter.types import JSONValue, get_int, get_object
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.operations import (
    OperationContext,
    create_operation,
    lock_operation_owner,
    register_operation_handler,
    set_progress,
)
from jobhunter.worker.reconcilers import ReconcileContext, reconciler
from jobhunter.worker.tasks import Deferrer

KIND: Final = "apply_scope"
APPLY_FAILED_ALERT: Final = "scope.apply_failed"
# How often a failed final check sends the handler back to the precompute before it requeues the operation.
FINAL_CHECK_ROUNDS: Final = 5
AFTER_CHUNK_POINT: Final = "scope_apply.after_chunk"
INCOMPLETE_CODE: Final = "scope_incomplete"
INCOMPLETE_RETRY_AFTER: Final = timedelta(minutes=1)
_LIVE: Final = ("pending", "running")

_POINTERS: Final = q(
    "SELECT scope_version_id, pending_scope_version_id FROM active_config WHERE id = 1 FOR UPDATE"
)
_SET_PENDING: Final = q(
    "UPDATE active_config SET pending_scope_version_id = :pending, updated_at = :now WHERE id = 1"
)
_PENDING_HINT: Final = q("SELECT pending_scope_version_id FROM active_config WHERE id = 1")
_FLIP: Final = q(
    "UPDATE active_config SET scope_version_id = pending_scope_version_id, pending_scope_version_id = NULL, "
    "updated_at = :now WHERE id = 1"
)
_SCOPE: Final = q("SELECT payload FROM scope_versions WHERE id = :id")
_RETAINED: Final = q("SELECT count(*) FROM jobs WHERE merged_into_job_id IS NULL")
_INCOMPLETE: Final = q(
    "SELECT count(*) FROM jobs j WHERE j.merged_into_job_id IS NULL "  # noqa: S608 (constants only)
    f"AND {STALE_PREDICATE}"
)
_ANY_LIVE: Final = q(
    "SELECT id FROM operations WHERE kind = :kind AND status IN ('pending', 'running') ORDER BY id LIMIT 1"
)
# The newest operation of a dedup key, live or ended (`operations_live_dedup` covers the live ones only).
_NEWEST: Final = q(
    "SELECT id, status FROM operations WHERE kind = :kind AND dedup_key = :key ORDER BY id DESC LIMIT 1"
)

log = get_logger("jobhunter.settings.scope_apply")


def dedup_key(scope_version_id: int) -> str:
    """The dedup key of the apply of `scope_version_id`: `apply_scope:<id>` (§13.4 step 1)."""
    return f"{KIND}:{scope_version_id}"


async def request_apply(conn: AsyncConnection, scope_version_id: int, *, defer: Deferrer) -> tuple[int, bool]:
    """The live `apply_scope` operation of `scope_version_id`, created (and its job deferred) when there is
    none: `(id, created)` (module docstring)."""
    return await create_operation(
        conn,
        kind=KIND,
        payload={"scope_version_id": scope_version_id},
        dedup_key=dedup_key(scope_version_id),
        defer=defer,
    )


async def set_desired_scope(
    conn: AsyncConnection, desired_id: int, *, now: datetime, defer: Deferrer
) -> int | None:
    """Move the pending scope pointer towards `desired_id`; the `apply_scope` operation's id, or `None` when
    no apply is needed (module docstring)."""
    pointers = (await conn.execute(_POINTERS)).one()
    if desired_id == pointers.scope_version_id:
        if pointers.pending_scope_version_id is not None:
            await conn.execute(_SET_PENDING, {"pending": None, "now": now})
        return None
    if desired_id != pointers.pending_scope_version_id:
        await conn.execute(_SET_PENDING, {"pending": desired_id, "now": now})
    operation_id, _ = await request_apply(conn, desired_id, defer=defer)
    return operation_id


async def incomplete_jobs(conn: AsyncConnection, scope_version_id: int) -> int:
    """How many unmerged jobs have no current `hard_filter_results` row for the scope
    (`filters.STALE_PREDICATE`): no limit, no lock."""
    params = {"scopes": [scope_version_id], "version": DET_ALGORITHM_VERSION}
    return int((await conn.execute(_INCOMPLETE, params)).scalar_one())


async def live_apply(conn: AsyncConnection) -> int | None:
    """The id of a live (`pending` or `running`) `apply_scope` operation of any scope, or `None`."""
    found = (await conn.execute(_ANY_LIVE, {"kind": KIND})).scalar_one_or_none()
    return None if found is None else int(found)


# --- the handler --------------------------------------------------------------------------------------------


@dataclass
class _Progress:
    """The apply's `{done, total}`: `done` counts the jobs complete for the scope, capped at `total`."""

    done: int
    total: int

    def add(self, recomputed: int) -> dict[str, JSONValue]:
        self.done = min(self.total, self.done + recomputed)
        return {"done": self.done, "total": self.total}


class _Incomplete(Exception):
    """The final check found stale rows: raised inside the final transaction, so that it rolls back."""


FinalEnd = Literal["flipped", "obsolete", "incomplete"]


async def _precompute(ctx: OperationContext, sid: int, scope: ScopeSettings, progress: _Progress) -> None:
    """Step 1 of the module docstring: chunks until one recomputes fewer than `BATCH`, or the pointer's hint
    says the scope is no longer pending."""
    claim = ctx.claim

    async def owner(conn: AsyncConnection) -> None:
        await lock_operation_owner(conn, claim)

    while True:
        async with ctx.engine.begin() as conn:
            if (await conn.execute(_PENDING_HINT)).scalar_one_or_none() != sid:
                return  # a hint only: the final transaction reads the pointer again under its lock
            recomputed = await recompute_stale(conn, {sid: scope}, guard=owner)
            await set_progress(conn, claim, progress.add(recomputed))  # the last statement (module docstring)
        await afault_point(AFTER_CHUNK_POINT)
        if recomputed < BATCH:
            return


async def _final(ctx: OperationContext, sid: int, *, now: datetime) -> tuple[FinalEnd, int | None]:
    """Step 2 of the module docstring: the end, and the pending pointer the transaction read."""
    try:
        async with ctx.engine.begin() as conn:
            pending = (await conn.execute(_POINTERS)).one().pending_scope_version_id
            await lock_operation_owner(conn, ctx.claim)
            if pending != sid:
                await set_progress(conn, ctx.claim, {"obsolete": True})
                await ctx.finish_in(conn, status="succeeded")
                return "obsolete", pending
            if await incomplete_jobs(conn, sid) > 0:
                raise _Incomplete  # rolls the transaction back: nothing was written
            await conn.execute(_FLIP, {"now": now})
            await ctx.finish_in(conn, status="succeeded")
            await resolve_alerts(conn, code=APPLY_FAILED_ALERT)
            await ranking.bump(conn)  # `ranking_state`, the last lock (§9.1)
    except _Incomplete:
        return "incomplete", sid
    return "flipped", sid


@register_operation_handler(KIND)
async def run_apply_scope(ctx: OperationContext) -> None:
    """Precompute the operation's scope, then flip to it in a checked final transaction (module docstring)."""
    claim = ctx.claim
    sid = get_int(dict(claim.payload), "scope_version_id")
    clock = utcnow if ctx.deps is None else ctx.deps.clock
    async with ctx.engine.connect() as conn:  # plain reads, rolled back when the block ends
        scope = scope_from_payload(get_object((await conn.execute(_SCOPE, {"id": sid})).scalar_one(), ""))
        total = int((await conn.execute(_RETAINED)).scalar_one())
        progress = _Progress(done=max(0, total - await incomplete_jobs(conn, sid)), total=total)
    for checks in range(1, FINAL_CHECK_ROUNDS + 2):  # the first round and at most FINAL_CHECK_ROUNDS re-runs
        await _precompute(ctx, sid, scope, progress)
        end, pending = await _final(ctx, sid, now=clock())
        if end == "flipped":
            log.info(
                "scope_apply_flipped", operation_id=claim.operation_id, scope_version_id=sid, checks=checks
            )
            return
        if end == "obsolete":
            log.info(
                "scope_apply_obsolete",
                operation_id=claim.operation_id,
                scope_version_id=sid,
                pending_scope_version_id=pending,
            )
            return
    async with ctx.engine.begin() as conn:
        await ctx.requeue_in(conn, error_code=INCOMPLETE_CODE, not_before=clock() + INCOMPLETE_RETRY_AFTER)
    log.warning(
        "scope_apply_incomplete",
        operation_id=claim.operation_id,
        scope_version_id=sid,
        checks=FINAL_CHECK_ROUNDS + 1,
    )


# --- the reconciler -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Repair:
    kind: Literal["cleared", "rerequested"]
    scope_version_id: int
    operation_id: int | None  # the apply that failed, or the one requested
    previous_operation_id: int | None = None  # what the re-requested apply follows (none ever existed: None)


async def _repair(ctx: ReconcileContext, conn: AsyncConnection) -> _Repair | None:
    pending = (await conn.execute(_POINTERS)).one().pending_scope_version_id
    if pending is None:
        return None
    newest = (await conn.execute(_NEWEST, {"kind": KIND, "key": dedup_key(pending)})).first()
    if newest is not None and newest.status in _LIVE:
        return None  # its apply is under way
    if newest is not None and newest.status == "failed":
        await conn.execute(_SET_PENDING, {"pending": None, "now": ctx.now})
        await raise_alert(
            conn,
            code=APPLY_FAILED_ALERT,
            severity="critical",
            subject=f"scope:{pending}",
            message="the scope apply failed permanently; the old scope stays active",
            context={"scope_version_id": pending, "operation_id": int(newest.id)},
        )
        return _Repair("cleared", pending, int(newest.id))
    requested = await set_desired_scope(conn, pending, now=ctx.now, defer=ctx.defer)
    return _Repair("rerequested", pending, requested, None if newest is None else int(newest.id))


@reconciler("scope_apply_failed", "tick")
async def reconcile_scope_apply_failed(ctx: ReconcileContext) -> int:
    """§13.4 step 5 and its self-heal (module docstring): 1 when it cleared or re-requested the pending
    scope's apply, else 0. Fenced by the worker epoch first: the worker's own `FencedOut` propagates (Ruling
    FR-w), another owner's ends the pass with 0, having written nothing."""
    try:
        async with ctx.engine.begin() as conn:
            await fence_epoch(conn, ctx.epoch)
            repair = await _repair(ctx, conn)
    except FencedOut as fenced_out:
        if fenced_out.worker_superseded:
            raise  # `fence_epoch` logged it
        return 0
    if repair is None:
        return 0
    if repair.kind == "cleared":
        log.error(
            "scope_apply_failed_cleared",
            scope_version_id=repair.scope_version_id,
            operation_id=repair.operation_id,
        )
    else:
        log.warning(
            "scope_apply_rerequested",
            scope_version_id=repair.scope_version_id,
            operation_id=repair.operation_id,
            previous_operation_id=repair.previous_operation_id,
        )
    return 1
