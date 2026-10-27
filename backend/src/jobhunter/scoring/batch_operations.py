"""The worker side of the score lane's batch submissions (spec §10.10, §3.3.1, §17.4; plan Tasks 16 and 17,
Rulings P16, T16-c, T16-e, Codex r1 #23, r2 B12, r3 B2): the `batch_submit` operation, the takeover sweep and
the tick reconciler that recover a lost execution. The submission state machine they drive is
`scoring.batches` (Ruling T16-e: the state machine and its worker side, split).

**The `batch_submit` handler**, payload `{"bulk_operation_id", "llm_tuple_id"}`: (1) `batches.recover_lost` of
its own operation, (2) the four sweeps of the tuple (expiry, refilter, regroup and exhaustion, L3939,
Deviation 7) in a transaction of their own, (3) `batches.prepare` (a `NotPrepared` ends the operation
`succeeded` with `progress = {"skipped": reason}`), the fault point `batch.after_prepare_commit`, (4)
`batches.submit_start` (an abandoned submission ends the operation), the fault point
`batch.after_submit_start`, (5) the owner and the `submit_started` row checked again
(`batches.check_sendable`: a reclaimed execution sends nothing), (6) `BatchesClient.create` outside any
transaction, the fault point `batch.after_create`, (7) `batches.record_created | record_rejected |
record_ambiguous` in a transaction of its own, retried on a retryable database error
(`graphs.sanitize.is_retryable_db_error`: a deadlock, a serialization failure, a lost connection) up to
`DB_RETRY.max_attempts` tries, its interval waited with the gateway's injected `sleep` (the owner predicate
makes each try the same recording, and a row whose tries ran out stays `submit_started`, the recovery's),
then the carried cancel, and (8) `finish_in(succeeded)` with
`progress = {"submission_id", "state", "request_count"}`. The client is built from today's `OperationDeps`
fields (`cfg`, `gateway.http_client`, `clock`, and the gateway's `monotonic` and `sleep` for the create's own
retries; Task 27 moves it into `OperationDeps.batches_client`).

**The recovery of a lost execution** (`batches.recover_lost`) runs in the takeover sweep `batch_submissions`
(today in the takeover's transaction; Task 27 gives each sweep its own), after `fence_epoch` and
`active_config FOR SHARE`, and in the tick reconciler `batch_submissions`, which takes no lock on a pass whose
lock-free read finds no candidate (`batches.any_lost`).

Ids, counts, codes and amounts only reach a log line, a payload or a report: never a param or prompt text.
"""

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.clock import utcnow
from jobhunter.db import ranking
from jobhunter.db.sql import q
from jobhunter.errors import FencedOut
from jobhunter.faults import afault_point
from jobhunter.graphs.sanitize import DB_RETRY, db_retry_interval, is_retryable_db_error
from jobhunter.llm.batches import Ambiguous, BatchApiError, BatchesClient, Created, CreateOutcome, Rejected
from jobhunter.observability.logging import get_logger
from jobhunter.scoring import batches, cycles, targets
from jobhunter.settings.dryrun import active_settings
from jobhunter.types import JSONValue, get_int
from jobhunter.worker.epoch import fence_epoch, register_takeover_sweep
from jobhunter.worker.operations import OperationContext, register_operation_handler, set_progress
from jobhunter.worker.reconcilers import ReconcileContext, reconciler
from jobhunter.worker.tasks import defer_in_tx

# The fault points of the handler (module docstring); the state machine's are `scoring.batches`'.
AFTER_PREPARE_COMMIT: Final = "batch.after_prepare_commit"
AFTER_SUBMIT_START: Final = "batch.after_submit_start"
AFTER_CREATE: Final = "batch.after_create"

_OWN_EPOCH: Final = q(
    "SELECT worker_epoch FROM operations WHERE id = :i AND claim_id = :c AND status = 'running'"
)

log = get_logger("jobhunter.scoring.batch_operations")


# ---- the recovery of a lost execution ----------------------------------------------------------------------


@register_takeover_sweep("batch_submissions")
async def sweep_lost_submissions(conn: AsyncConnection, epoch: int) -> int:
    """At takeover: the submissions of every lost execution recovered (module docstring), after
    `fence_epoch` and `active_config FOR SHARE`; how many."""
    await fence_epoch(conn, epoch)
    await cycles.lock_active_scoring(conn)
    recovered = await batches.recover_lost(conn, epoch=epoch, now=utcnow(), defer=defer_in_tx)
    return recovered.abandoned + recovered.ambiguous


@reconciler("batch_submissions", "tick")
async def reconcile_batch_submissions(ctx: ReconcileContext) -> int:
    """The tick's recovery of lost executions (module docstring): a lock-free read first, then, only when it
    finds a candidate, `fence_epoch`, `active_config FOR SHARE` and `recover_lost`; how many it moved. The
    worker's own `FencedOut` propagates (Ruling FR-w), another owner's ends the pass with 0."""
    try:
        async with ctx.engine.connect() as conn:
            if not await batches.any_lost(conn, epoch=ctx.epoch):
                return 0
        async with ctx.engine.begin() as conn:
            await fence_epoch(conn, ctx.epoch)
            await cycles.lock_active_scoring(conn)
            recovered = await batches.recover_lost(conn, epoch=ctx.epoch, now=ctx.now, defer=ctx.defer)
    except FencedOut as fenced:
        if fenced.worker_superseded:
            raise
        return 0
    return recovered.abandoned + recovered.ambiguous


# ---- the batch_submit handler ------------------------------------------------------------------------------


async def _sweep(conn: AsyncConnection, *, llm_tuple_id: int, now: datetime) -> None:
    """The four sweeps of the tuple, as online admission runs them (`admission.admit`'s pre-lock first)."""
    active = await cycles.lock_active_scoring(conn)
    if active.llm_tuple_id != llm_tuple_id:
        return  # the preparation ends tuple_superseded
    max_age = await targets.active_max_ad_age_days(conn)
    held = await targets.lock_hold_candidates(
        conn, llm_tuple_id=llm_tuple_id, max_ad_age_days=max_age, now=now
    )
    moved = (
        await targets.expiry_sweep(
            conn, llm_tuple_id=llm_tuple_id, max_ad_age_days=max_age, now=now, held=held
        )
    ).moved
    moved += (
        await targets.refilter_sweep(
            conn, llm_tuple_id=llm_tuple_id, scope_version_id=active.scope_version_id
        )
    ).moved
    moved += (await targets.regroup_sweep(conn, llm_tuple_id=llm_tuple_id)).moved
    moved += await targets.exhaustion_sweep(conn, llm_tuple_id=llm_tuple_id, held=held)
    if moved:
        await ranking.bump(conn)  # step 11, last


async def _end(ctx: OperationContext, conn: AsyncConnection, progress: Mapping[str, JSONValue]) -> None:
    await set_progress(conn, ctx.claim, progress)
    await ctx.finish_in(conn, status="succeeded")


@register_operation_handler(batches.KIND)
async def batch_submit(ctx: OperationContext) -> None:
    """The `batch_submit` operation (module docstring)."""
    deps = ctx.deps
    if deps is None:
        raise RuntimeError("batch_submit needs the worker's operation dependencies")
    payload = dict(ctx.claim.payload)
    bulk_operation_id = get_int(payload, "bulk_operation_id")
    llm_tuple_id = get_int(payload, "llm_tuple_id")
    clock = deps.clock
    client = BatchesClient(
        deps.cfg,
        http_client=deps.gateway.http_client,
        clock=clock,
        monotonic=deps.gateway.monotonic,
        sleep=deps.gateway.sleep,
    )
    async with ctx.engine.begin() as conn:  # 1. this operation's lost executions
        epoch = (
            await conn.execute(_OWN_EPOCH, {"i": ctx.claim.operation_id, "c": ctx.claim.claim_id})
        ).scalar_one_or_none()
        if epoch is None:
            raise FencedOut("operation")
        await fence_epoch(conn, int(epoch))
        await cycles.lock_active_scoring(conn)
        await batches.recover_lost(
            conn, epoch=int(epoch), operation_id=ctx.claim.operation_id, now=clock(), defer=defer_in_tx
        )
    if not client.configured:
        async with ctx.engine.begin() as conn:
            await ctx.finish_in(conn, status="failed", error_code="llm_not_configured")
        return
    async with ctx.engine.begin() as conn:  # 2. the sweeps
        await _sweep(conn, llm_tuple_id=llm_tuple_id, now=clock())
    async with ctx.engine.begin() as conn:  # 3. the preparation
        await cycles.lock_active_scoring(conn)
        prepared = await batches.prepare(
            conn,
            bulk_operation_id=bulk_operation_id,
            llm_tuple_id=llm_tuple_id,
            operation=ctx.claim,
            settings=await active_settings(conn),
            fake_llm=deps.cfg.fake_llm,
            gateway=deps.gateway,
            now=clock(),
        )
        if isinstance(prepared, batches.NotPrepared):
            await _end(ctx, conn, {"skipped": prepared.reason})
            return
    s = prepared.submission_id
    await afault_point(AFTER_PREPARE_COMMIT)
    async with ctx.engine.begin() as conn:  # 4. submit-start
        started = await batches.submit_start(conn, s, operation=ctx.claim, now=clock(), defer=defer_in_tx)
    if started == "abandoned":
        async with ctx.engine.begin() as conn:
            await _end(ctx, conn, {"submission_id": s, "state": "abandoned"})
        return
    await afault_point(AFTER_SUBMIT_START)
    async with ctx.engine.begin() as conn:  # 5. still this execution's to send
        await batches.check_sendable(conn, s, operation=ctx.claim)
    outcome = await client.create(prepared.requests)  # 6. outside any transaction
    await afault_point(AFTER_CREATE)
    state, cancel_due = await _record(ctx, s, outcome, now=clock(), sleep=deps.gateway.sleep)  # 7.
    if cancel_due and isinstance(outcome, Created):
        try:
            await client.cancel(outcome.provider_batch_id)
        except BatchApiError as failure:  # batch_poll sends it again (Task 17)
            log.warning(
                "batch_cancel_failed", submission_id=s, error_class=failure.error_class, status=failure.status
            )
    async with ctx.engine.begin() as conn:  # 8.
        await _end(ctx, conn, {"submission_id": s, "state": state, "request_count": prepared.request_count})


async def _record(
    ctx: OperationContext,
    submission_id: int,
    outcome: CreateOutcome,
    *,
    now: datetime,
    sleep: Callable[[float], Awaitable[object]],
) -> tuple[str, bool]:
    """The recording transaction of the create's outcome, retried on a retryable database error (module
    docstring), `sleep` waiting out `DB_RETRY`'s interval; the submission's state and whether a cancel is
    due."""
    tries = 0
    while True:
        tries += 1
        try:
            return await _record_once(ctx, submission_id, outcome, now=now)
        except sa.exc.DBAPIError as exc:
            if tries >= DB_RETRY.max_attempts or not is_retryable_db_error(exc):
                raise
            log.warning(
                "batch_record_retried",
                submission_id=submission_id,
                tries=tries,
                error_class=type(exc).__name__,
            )
        await sleep(db_retry_interval(tries))


async def _record_once(
    ctx: OperationContext, submission_id: int, outcome: CreateOutcome, *, now: datetime
) -> tuple[str, bool]:
    async with ctx.engine.begin() as conn:
        match outcome:
            case Created():
                due = await batches.record_created(conn, submission_id, outcome, operation=ctx.claim, now=now)
                return "submitted", due
            case Rejected():
                await batches.record_rejected(
                    conn, submission_id, outcome, operation=ctx.claim, now=now, defer=defer_in_tx
                )
                return "submit_rejected", False
            case Ambiguous():
                await batches.record_ambiguous(conn, submission_id, outcome, operation=ctx.claim, now=now)
                return "submit_ambiguous", False
