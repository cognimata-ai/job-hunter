"""Durable operations with claim tokens (spec §3.3.2 to §3.3.4, §7.3, §7.16.8, §17.4).

An `operations` row is a durable intent: a mutation that needs follow-up work inserts one in its own
transaction (`create_operation`), and the `run_operation(operation_id)` job only names it. Every execution
claims the row with a fresh `claim_id` (`pending → running`); the heartbeat, every lock of the owner row and
every completion write check `claim_id = :c AND status = 'running'`, and every way out of `running` clears the
claim, its epoch and its heartbeat in the same statement (`transition()`). So an old execution, once its row
was reclaimed, can never satisfy the predicate again, even within the same worker epoch (§18.2 Operation ABA).

The ways out of `running`:
- `finish_operation`: `succeeded`, or `failed` with an error code (the owner's write);
- `requeue_operation`: back to `pending` with `attempts + 1` and a `not_before` (the owner's write);
- `reclaim_stale_operations` (the `operations` reconciler, heartbeat older than 3 min) and the `operations`
  takeover sweep (an older worker epoch): back to `pending` with `attempts + 1`, no reason;
- `cancel_operation`: `cancelled` (delete-everything, a superseding purge or scope).

A requeue or reclaim that reaches `MAX_OPERATION_ATTEMPTS` ends the row `failed('attempts_exhausted')`
instead. Every permanent failure (a `failed` finish, or exhaustion) raises the `operation.failed` alert (warn,
subject `operation:<id>`) exactly once, in the transaction that ends the row (§7.3; ruling T10-g).

`execute_operation` is the task body's protocol; it runs the kind's handler through `executions.run_owned`,
the one ownership-loss protocol shared with runs. Every time comparison of the sweeps and reconcilers uses the
caller's clock (`ctx.now`, or `now`), never the database's `now()`.

This module is a task boundary (ruff `BLE001` is allowed here); it needs no broad catch, because `run_owned`
reports an unexpected handler error as an outcome, which `execute_operation` persists as a requeue (`bug`).
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.clock import utcnow
from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.errors import FencedOut, TransitionConflict
from jobhunter.observability.alerts import raise_alert
from jobhunter.observability.logging import get_logger
from jobhunter.types import JSONValue
from jobhunter.worker.epoch import register_takeover_sweep
from jobhunter.worker.executions import EXECUTIONS, AlreadyExecuting, ExecKey, run_owned
from jobhunter.worker.reconcilers import ReconcileContext, reconciler
from jobhunter.worker.tasks import Deferrer, job_task_name

OperationKind = Literal[
    "extract_profile_text",
    "extract_profile",
    "embed_profile",
    "propose_queries",
    "apply_scope",
    "create_targets_for_tuple",
    "batch_submit",
    "export",
    "purge",
    "eval_run",
    "watchlist_validate",
]

MAX_OPERATION_ATTEMPTS: Final = 10
OPERATION_STALE: Final = timedelta(minutes=3)
REDEFER_AFTER: Final = timedelta(minutes=2)
# At most this many lost jobs are re-deferred per tick; the next tick takes the rest.
REDEFER_BATCH: Final = 100
# How long a requeued operation waits before it can be claimed again: after an unexpected handler error
# (`bug`) or a refused local start (`already_executing`).
RETRY_DELAY: Final = timedelta(minutes=1)

FAILED_ALERT_CODE: Final = "operation.failed"
_TABLE: Final = "operations"
# How often `create_operation` retries when the live row it conflicted with ended before it could be read.
_CREATE_TRIES: Final = 3

log = get_logger("jobhunter.worker.operations")


@dataclass(frozen=True)
class OperationClaim:
    operation_id: int
    claim_id: UUID
    kind: OperationKind
    payload: Mapping[str, JSONValue]


@dataclass(frozen=True)
class OperationContext:
    claim: OperationClaim
    engine: AsyncEngine


OperationHandler = Callable[[OperationContext], Awaitable[None]]

# The handler of each kind; the modules that own a kind register it (none in M0).
HANDLERS: dict[OperationKind, OperationHandler] = {}


def register_operation_handler(kind: OperationKind) -> Callable[[OperationHandler], OperationHandler]:
    """Decorator: run the function for every operation of `kind`."""

    def register(handler: OperationHandler) -> OperationHandler:
        registered = HANDLERS.get(kind)
        if registered is not None and registered is not handler:
            raise ValueError(f"another handler is already registered for operation kind {kind!r}")
        HANDLERS[kind] = handler
        return handler

    return register


# The conflict target repeats the index `operations_live_dedup` of §7.3 exactly:
# `(kind, dedup_key) WHERE status IN ('pending','running')`.
_INSERT = q(
    "INSERT INTO operations (kind, payload, dedup_key, status) "
    "VALUES (:kind, :payload, :dedup_key, 'pending') "
    "ON CONFLICT (kind, dedup_key) WHERE status IN ('pending','running') DO NOTHING RETURNING id",
    jsonb=["payload"],
)
_LIVE_ID = q(
    "SELECT id FROM operations "
    "WHERE kind = :kind AND dedup_key = :dedup_key AND status IN ('pending','running')"
)
_CLAIMABLE = q(
    "SELECT kind, payload FROM operations "
    "WHERE id = :i AND status = 'pending' AND (not_before IS NULL OR not_before <= now()) FOR UPDATE"
)
_HEARTBEAT = q(
    "UPDATE operations SET heartbeat_at = now() "
    "WHERE id = :i AND claim_id = :c AND status = 'running' RETURNING id"
)
_LOCK_OWNER = q("SELECT id FROM operations WHERE id = :i AND claim_id = :c AND status = 'running' FOR SHARE")
_OWNED_ATTEMPTS = q(
    "SELECT attempts FROM operations WHERE id = :i AND claim_id = :c AND status = 'running' FOR UPDATE"
)
_STALE = q(
    "SELECT id, claim_id, attempts FROM operations WHERE status = 'running' AND heartbeat_at < :cutoff "
    "ORDER BY id FOR UPDATE SKIP LOCKED"
)
_OLDER_EPOCH = q(
    "SELECT id, claim_id, attempts FROM operations WHERE status = 'running' AND worker_epoch < :epoch "
    "ORDER BY id FOR UPDATE SKIP LOCKED"
)
# Due `pending` operations without a live job (the predicate of `tasks.live_job_exists`, inline so that
# only real candidates are locked and the batch is never filled by rows that still have their job).
# `operations_pending` serves the `status = 'pending'` scan.
_REDEFER_DUE = q(
    "SELECT o.id FROM operations o WHERE o.status = 'pending' "
    "AND greatest(o.created_at, coalesce(o.not_before, o.created_at)) <= :cutoff "
    "AND NOT EXISTS (SELECT 1 FROM procrastinate_jobs j WHERE j.task_name = :task "
    "AND j.status IN ('todo', 'doing') AND j.args @> jsonb_build_object('operation_id', o.id)) "
    "ORDER BY o.id LIMIT :limit FOR UPDATE OF o SKIP LOCKED"
)


async def create_operation(
    conn: AsyncConnection,
    *,
    kind: OperationKind,
    payload: Mapping[str, JSONValue],
    dedup_key: str,
    defer: Deferrer | None,
) -> tuple[int, bool]:
    """The live (`pending` or `running`) operation of `kind` and `dedup_key`, inserted when there is none.

    Returns `(id, created)`. Only a new row defers its job, `run_operation(operation_id=id)` under the
    queueing lock `op:<id>`, in the caller's transaction (`defer=None` defers nothing; the `operations`
    reconciler re-defers it after `REDEFER_AFTER`). `payload` holds ids only, never content.
    """
    params = {"kind": kind, "payload": dict(payload), "dedup_key": dedup_key}
    for _ in range(_CREATE_TRIES):
        inserted = (await conn.execute(_INSERT, params)).scalar_one_or_none()
        if inserted is not None:
            operation_id = int(inserted)
            if defer is not None:
                await defer(
                    conn, "run_operation", queueing_lock=f"op:{operation_id}", operation_id=operation_id
                )
            return operation_id, True
        # The insert met a live row; a statement later it may have ended, and the next insert then succeeds.
        live = (await conn.execute(_LIVE_ID, {"kind": kind, "dedup_key": dedup_key})).scalar_one_or_none()
        if live is not None:
            return int(live), False
    raise RuntimeError(f"operations: the live {kind} row of a dedup key kept ending during its creation")


async def claim_operation(conn: AsyncConnection, operation_id: int, *, epoch: int) -> OperationClaim | None:
    """Claim a `pending` operation whose `not_before` has passed, with a fresh `claim_id`; `None` otherwise.

    `pending → running` sets the claim, the worker `epoch` and the first heartbeat in one statement.
    """
    row = (await conn.execute(_CLAIMABLE, {"i": operation_id})).first()
    if row is None:
        return None
    claim_id = uuid4()
    try:
        await transition(
            conn,
            _TABLE,
            operation_id,
            ["pending"],
            "running",
            set_={"claim_id": claim_id, "worker_epoch": epoch, "heartbeat_at": sa.func.now()},
        )
    except TransitionConflict:
        return None
    return OperationClaim(operation_id=operation_id, claim_id=claim_id, kind=row.kind, payload=row.payload)


def _owner(claim: OperationClaim) -> dict[str, object]:
    return {"i": claim.operation_id, "c": claim.claim_id}


async def heartbeat_operation(conn: AsyncConnection, claim: OperationClaim) -> bool:
    """Record a heartbeat of `claim`; `False` when the claim no longer owns the row (stop the execution)."""
    return (await conn.execute(_HEARTBEAT, _owner(claim))).first() is not None


async def lock_operation_owner(conn: AsyncConnection, claim: OperationClaim) -> None:
    """Hold the owner row `FOR SHARE` for the transaction, or raise `FencedOut` when `claim` lost it.

    Every reservation, dispatch marker and domain write made on the operation's behalf runs after it.
    """
    if (await conn.execute(_LOCK_OWNER, _owner(claim))).first() is None:
        raise FencedOut("operation")


async def finish_operation(
    conn: AsyncConnection,
    claim: OperationClaim,
    *,
    status: Literal["succeeded", "failed"],
    error_code: str | None = None,
) -> None:
    """End the claimed operation `succeeded` or `failed(error_code)`; `FencedOut` when `claim` lost the row.

    A `failed` end needs an `error_code`, and raises the `operation.failed` alert in the same transaction
    (ruling T10-g), so no caller can forget it; a fenced finish raises neither.
    """
    if status == "failed" and error_code is None:
        raise ValueError("a failed operation needs an error code")
    try:
        await transition(
            conn,
            _TABLE,
            claim.operation_id,
            ["running"],
            status,
            reason=error_code,
            owner={"claim_id": claim.claim_id},
        )
    except TransitionConflict as exc:
        raise FencedOut("operation") from exc
    if status == "failed":
        await _raise_failed_alert(conn, claim.operation_id)


async def _raise_failed_alert(conn: AsyncConnection, operation_id: int) -> None:
    await raise_alert(
        conn,
        code=FAILED_ALERT_CODE,
        severity="warn",
        subject=f"operation:{operation_id}",
        message="operation failed permanently",
    )


async def _release(
    conn: AsyncConnection,
    operation_id: int,
    claim_id: UUID,
    attempts: int,
    *,
    reason: str | None,
    not_before: datetime | None,
) -> Literal["pending", "failed"]:
    """Take the row from the execution `claim_id`: back to `pending` with one more attempt, or `failed`.

    The one copy of the attempts rule, for the owner's requeue, the stale reclaim and the takeover sweep.
    `TransitionConflict` propagates when `claim_id` no longer owns the row.
    """
    owner = {"claim_id": claim_id}
    attempts_after = attempts + 1
    if attempts_after >= MAX_OPERATION_ATTEMPTS:
        await transition(
            conn,
            _TABLE,
            operation_id,
            ["running"],
            "failed",
            reason="attempts_exhausted",
            owner=owner,
            set_={"attempts": MAX_OPERATION_ATTEMPTS},
        )
        await _raise_failed_alert(conn, operation_id)
        log.warning("operation_attempts_exhausted", operation_id=operation_id)
        return "failed"
    await transition(
        conn,
        _TABLE,
        operation_id,
        ["running"],
        "pending",
        reason=reason,
        owner=owner,
        set_={"attempts": attempts_after, "not_before": not_before},
    )
    return "pending"


async def requeue_operation(
    conn: AsyncConnection, claim: OperationClaim, *, error_code: str, not_before: datetime
) -> Literal["pending", "failed"]:
    """Give the claimed operation back for a retry after `not_before`, recording `error_code`.

    Returns the new status: `failed` (and the `operation.failed` alert) when this was the last attempt.
    Raises `FencedOut`, having written nothing, when `claim` no longer owns the row.
    """
    row = (await conn.execute(_OWNED_ATTEMPTS, _owner(claim))).first()
    if row is None:
        raise FencedOut("operation")
    try:
        return await _release(
            conn,
            claim.operation_id,
            claim.claim_id,
            int(row.attempts),
            reason=error_code,
            not_before=not_before,
        )
    except TransitionConflict as exc:
        raise FencedOut("operation") from exc


async def cancel_operation(conn: AsyncConnection, operation_id: int, *, reason: str) -> bool:
    """End a `pending` or `running` operation `cancelled(reason)`; `False` when it had already ended.

    A running execution is fenced by it (its next heartbeat or owner write finds no row). To stop a local
    execution at once, the caller cancels `("operation", operation_id)` in `EXECUTIONS` after its commit.
    """
    try:
        await transition(conn, _TABLE, operation_id, ["pending", "running"], "cancelled", reason=reason)
    except TransitionConflict:
        return False
    return True


async def reclaim_stale_operations(conn: AsyncConnection, *, now: datetime) -> list[int]:
    """Take back every `running` operation whose heartbeat is older than `OPERATION_STALE` at `now`.

    Each goes back to `pending` (or ends `failed` on its last attempt); rows locked by an in-flight
    heartbeat or owner write are skipped this time. Returns the ids taken back, in id order.
    """
    return [operation_id for operation_id, _ in await _reclaim_stale(conn, now=now)]


async def _reclaim_stale(conn: AsyncConnection, *, now: datetime) -> list[tuple[int, UUID]]:
    """`reclaim_stale_operations`, returning each id with the claim it was taken from."""
    rows = (await conn.execute(_STALE, {"cutoff": now - OPERATION_STALE})).all()
    for row in rows:
        await _release(conn, row.id, row.claim_id, int(row.attempts), reason=None, not_before=None)
    return [(int(row.id), row.claim_id) for row in rows]


@register_takeover_sweep("operations")
async def sweep_older_epoch_operations(conn: AsyncConnection, epoch: int) -> int:
    """At takeover: take back every `running` operation of an older worker epoch; the count."""
    rows = (await conn.execute(_OLDER_EPOCH, {"epoch": epoch})).all()
    for row in rows:
        await _release(conn, row.id, row.claim_id, int(row.attempts), reason=None, not_before=None)
    return len(rows)


async def _redefer_lost_jobs(conn: AsyncConnection, *, now: datetime, defer: Deferrer) -> int:
    """Defer `run_operation` again for up to `REDEFER_BATCH` due `pending` operations without a live job.

    Only those candidates are locked (`FOR UPDATE SKIP LOCKED`). Returns how many jobs were deferred.
    """
    params = {"cutoff": now - REDEFER_AFTER, "task": job_task_name("run_operation"), "limit": REDEFER_BATCH}
    redeferred = 0
    for operation_id in (await conn.execute(_REDEFER_DUE, params)).scalars().all():
        job_id = await defer(
            conn, "run_operation", queueing_lock=f"op:{operation_id}", operation_id=operation_id
        )
        redeferred += job_id is not None
    return redeferred


@reconciler("operations", "tick")
async def reconcile_operations(ctx: ReconcileContext) -> int:
    """Re-defer lost jobs, then reclaim stale executions and stop their local tasks; rows repaired.

    A `pending` operation without a live job is re-deferred once `ctx.now` is `REDEFER_AFTER` past its
    creation and its `not_before` (at most `REDEFER_BATCH` per tick). A `running` one whose heartbeat is
    older than `OPERATION_STALE` is taken back; after that commit, its local execution is cancelled and
    awaited when, and only when, it runs for the claim that was taken back (its registry token).
    """
    async with ctx.engine.begin() as conn:
        redeferred = await _redefer_lost_jobs(conn, now=ctx.now, defer=ctx.defer)
    async with ctx.engine.begin() as conn:
        reclaimed = await _reclaim_stale(conn, now=ctx.now)
    for operation_id, claim_id in reclaimed:
        key: ExecKey = ("operation", operation_id)
        live = EXECUTIONS.get(key)
        # No await between the check and the cancel: the entry cannot change in between.
        if live is not None and live[0] == claim_id:
            await EXECUTIONS.cancel_and_await(key)
    return redeferred + len(reclaimed)


async def _requeue(engine: AsyncEngine, claim: OperationClaim, *, error_code: str) -> None:
    """Requeue `claim` in a transaction of its own; a fenced claim returns quietly (it wrote nothing)."""
    try:
        async with engine.begin() as conn:
            status = await requeue_operation(
                conn, claim, error_code=error_code, not_before=utcnow() + RETRY_DELAY
            )
    except FencedOut:
        log.info("operation_fenced", operation_id=claim.operation_id, write="requeue")
        return
    log.info("operation_requeued", operation_id=claim.operation_id, error_code=error_code, status=status)


async def _finish(
    engine: AsyncEngine,
    claim: OperationClaim,
    *,
    status: Literal["succeeded", "failed"],
    error_code: str | None = None,
) -> None:
    """Finish `claim` in a transaction of its own (a `failed` finish raises `operation.failed` in it)."""
    try:
        async with engine.begin() as conn:
            await finish_operation(conn, claim, status=status, error_code=error_code)
    except FencedOut:
        log.info("operation_fenced", operation_id=claim.operation_id, write="finish")
        return
    log.info("operation_finished", operation_id=claim.operation_id, status=status, error_code=error_code)


async def _invoke(handler: OperationHandler, ctx: OperationContext) -> None:
    """The handler call as a coroutine of its own: a refused start closes it before the handler is called."""
    await handler(ctx)


async def execute_operation(
    engine: AsyncEngine, operation_id: int, *, epoch: int, heartbeat_period_s: float = 30.0
) -> None:
    """Claim and run operation `operation_id` (the `run_operation` task body), one protocol for every end.

    1. Claim it; return when it is not claimable. A kind without a handler ends `failed('no_handler')` with
       the `operation.failed` alert (raised by `finish_operation`).
    2. Run the handler through `run_owned(("operation", id), claim_id, …)`: the registry's task for the
       operation, next to the heartbeat task `heartbeat:operation:<id>` whose loss cancels it.
    3. By outcome: `lost` (a refused or failed heartbeat, a reconciler's or the shutdown's cancel, even one
       the handler swallowed, or `FencedOut`) returns with no further owner write; `raised` requeues with
       `error_code='bug'` and `not_before = now + RETRY_DELAY`; `returned` finishes `succeeded`. A local
       execution that is still live for the operation (`AlreadyExecuting`) requeues the new claim as
       `already_executing`.

    Finish and requeue each run in a transaction of their own; their `FencedOut` is caught outside it, and the
    fenced transaction wrote nothing, because its `transition()` is its first write. The caller's own
    cancellation propagates (the work is cancelled first) and nothing is written: the row's next owner
    (reclaim or takeover) records the outcome.
    """
    async with engine.begin() as conn:
        claim = await claim_operation(conn, operation_id, epoch=epoch)
    if claim is None:
        log.info("operation_not_claimable", operation_id=operation_id)
        return
    handler = HANDLERS.get(claim.kind)
    if handler is None:
        log.error("operation_without_handler", operation_id=operation_id, kind=claim.kind)
        await _finish(engine, claim, status="failed", error_code="no_handler")
        return

    async def beat() -> bool:
        async with engine.begin() as conn:
            return await heartbeat_operation(conn, claim)

    try:
        outcome = await run_owned(
            ("operation", operation_id),
            claim.claim_id,
            _invoke(handler, OperationContext(claim=claim, engine=engine)),
            beat=beat,
            period_s=heartbeat_period_s,
        )
    except AlreadyExecuting:
        log.warning("operation_already_executing", operation_id=operation_id)
        await _requeue(engine, claim, error_code="already_executing")
        return
    match outcome.status:
        case "lost":
            return
        case "raised":
            # The class only: an exception message may carry content.
            log.error(
                "operation_handler_failed",
                operation_id=operation_id,
                kind=claim.kind,
                error_class=type(outcome.error).__name__,
            )
            await _requeue(engine, claim, error_code="bug")
        case "returned":
            await _finish(engine, claim, status="succeeded")
