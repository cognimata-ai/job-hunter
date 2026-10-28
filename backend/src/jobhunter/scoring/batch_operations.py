"""The worker side of the score lane's batch submissions (spec §10.10, §3.3.1, §17.4, §18.3 C20-C23, C33; plan
Tasks 16 and 17, Rulings P16, T16-c, T16-e, Carries T8-g, T16-g, Codex r1 #23, #24, r2 B12, B14, B15, r3 B2,
r5 B5, r6 B1, B2, r7 B1, B3, A2 B12, B15, B16, chair decision C3, Deviation 27): the `batch_submit`
operation, the takeover sweep and the tick reconciler that recover a lost execution, the poll, the completion
of a submission's results and the ambiguous-submit reconciliation. The submission state machine they drive is
`scoring.batches`, the decision of one item `scoring.batch_items` (Ruling T16-e).

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

**The provider cancel** (`cancel_requested`, L6443): `messages.batches.cancel` of a recorded request
(`cancel_requested_at`) up to `CANCEL_TRIES` (3) times, then logged; the handler sends it right after
`record_created`'s commit, and `batch_poll` again on every pass while the submission is `submitted`.

**The poll** (`poll`, the body of `batch_poll`, every 5 min on `aux`, L3958), for every `submitted` and
`ended` submission, one at a time, each transaction fenced by the worker epoch: a `submitted` one has its
recorded cancel sent first, then it is retrieved, and once `processing_status = 'ended'` it moves `submitted
→ ended` (`active_config FOR SHARE`, the owner `FOR UPDATE` under `state = 'submitted'`); an `ended` one has
its results streamed line by line (`BatchesClient.results`, never buffered whole), each line's item decided in
**a transaction of its own** (`batch_items.process_item`; never two items in one: an item takes its job row
before its owner and ends on `ranking_state`, Codex r6 B1), the fault point `batch.after_item` after each
commit. A `followup` decision requests a `batch_followup` cycle (`cycles.request_cycle`). When the stream was
read to its end and every line named an item of the submission, `finalize_results` runs; a line naming no
item of ours raises the critical `batch.custom_id_anomaly` and completes nothing; a cut stream completes
nothing and decides no item `missing` (L3961): the next poll reads it again, and every item already decided
replays its stored decision. **An unreadable stream** (Carry T16-g: a line the pinned SDK cannot validate,
`BatchApiError.unreadable`, a provider schema change) is bounded: each such pass is counted in the
submission's `reconcile_report` (`unreadable_passes`), and at `RESULTS_UNREADABLE_PASSES` (12, an hour at the
poll's cadence) the completion runs with the critical `batch.results_unreadable`, so every item still unread
is decided `missing` instead of leaving the submission `ended` for good; a stream not read whole for any
other reason (cut, or not opened) is given up the same way once the results' retention has passed
(`RESULTS_RETENTION`, 29 days after `submit_started_at`, reason `results_retention_passed`).

**The completion orchestration** (`finalize_results`, shared by the poll and the adoption, Codex r5 B5, r6
B1, r7 B1), three kinds of transactions, never one: (1) the completion: `active_config FOR SHARE`, the owner
`FOR UPDATE` under `state = 'ended' AND results_complete_at IS NULL`, `results_complete_at = now` (the
durable record that the stream was read whole; the `reconciled` CHECK demands it), the items still
`awaiting` counted and the critical `batch.items_missing` when there are any (the fault point
`batch.before_completion_commit` last; a marker set already writes nothing; then
`batch.after_completion_commit`);
(2) each item still `awaiting`, in id order, in a transaction of its own, decided through the same
`process_item` from a synthetic `ItemResult(custom_id, "missing")` (`batch.after_missing_item` after each);
(3) the reconcile: `active_config FOR SHARE`, the owner `FOR UPDATE` under `state = 'ended' AND
results_complete_at IS NOT NULL`, no item unprocessed, `ended → reconciled`, `bulk.complete_if_done` and
`batches.follow_on` in that same transaction, so its once-only marker and the follow-on operation commit with
the reconcile (`batch.after_reconcile_commit` after it). The rows are the recovery: every step is idempotent,
a crash after (1) leaves the next poll to skip the stream and run (2) and (3), a crash after the last missing
item leaves it to run (3).

**The ambiguous-submit reconciliation** (`reconcile_ambiguous`, the body of `batch_reconcile_ambiguous`,
every 15 min on `aux`, L3984-3999), for every `submit_ambiguous` submission and every `released` one with
`submit_started_at > now - 29 days` (A2 B12; an older one is listed no more): first, at 25 h after its
submit-start, each `submit_ambiguous` one opens the critical `batch.submit_ambiguous_unresolved` with the
amount it holds (never a transition, chair decision C3; also while the listing fails, C22); then one listing
of the whole workspace (`list_all`; a failure leaves every row untouched, logged `batch_listing_failed`; the
workspace is this installation's alone, A2 B15), less the batches some submission adopted already. For each
submission, the candidates are the batches created within `[submit_started_at - 1 min, submit_started_at + 10
min]` whose request count totals ours; a candidate is confirmed only once it has ended and its results,
downloaded whole (ids only), name exactly our `custom_id` set: the adoption transaction (`active_config FOR
SHARE`, the owner `FOR UPDATE` under `state IN ('submit_ambiguous', 'released')` without a provider id) adopts
its id and moves the submission to `ended` (a released one keeps `released_at`, outside the singleton; a
recorded cancel stays a record: an ended batch has nothing to cancel, Codex r7 B3), then its results are
processed and completed by the poll's own path in the same pass (every item of a released submission is
`ignored_fenced`; the follow-on fires for a `submit_ambiguous` origin only). A candidate whose ids intersect
ours partially raises `batch.custom_id_anomaly` and adopts nothing (rule 4); a pass without a match records
`last_reconcile_at` and `reconcile_report = {…, "candidates": n, "listed": m}`.

**The task bodies** (`poll_task`, `reconcile_ambiguous_task`, Deviation 27: periodic tasks of `worker.tasks`,
each under the queueing lock of its name) build the client from the worker's config and gateway and do
nothing without a configured one.

Ids, counts, codes and amounts only reach a log line, a payload or a report: never a param or prompt text.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Final

import sqlalchemy as sa
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.db import ranking
from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.errors import FencedOut
from jobhunter.faults import afault_point
from jobhunter.graphs.sanitize import DB_RETRY, db_retry_interval, is_retryable_db_error
from jobhunter.llm import bulk
from jobhunter.llm.batches import (
    Ambiguous,
    BatchApiError,
    BatchesClient,
    BatchSummary,
    Created,
    CreateOutcome,
    ItemResult,
    Rejected,
)
from jobhunter.llm.gateway import LLMGateway
from jobhunter.observability.alerts import open_alert, raise_alert
from jobhunter.observability.logging import get_logger
from jobhunter.scoring import batch_items, batches, cycles, targets
from jobhunter.settings.dryrun import active_settings
from jobhunter.types import JSONValue, get_int
from jobhunter.worker.epoch import fence_epoch, register_takeover_sweep
from jobhunter.worker.operations import OperationContext, register_operation_handler, set_progress
from jobhunter.worker.reconcilers import ReconcileContext, reconciler
from jobhunter.worker.tasks import Deferrer, defer_in_tx

# The fault points of the handler (module docstring); the state machine's are `scoring.batches`'.
AFTER_PREPARE_COMMIT: Final = "batch.after_prepare_commit"
AFTER_SUBMIT_START: Final = "batch.after_submit_start"
AFTER_CREATE: Final = "batch.after_create"

# Task 17's fault points: after each item's commit, inside and after the completion, after each missing item,
# after the reconcile's commit.
AFTER_ITEM: Final = "batch.after_item"
BEFORE_COMPLETION_COMMIT: Final = "batch.before_completion_commit"
AFTER_COMPLETION_COMMIT: Final = "batch.after_completion_commit"
AFTER_MISSING_ITEM: Final = "batch.after_missing_item"
AFTER_RECONCILE_COMMIT: Final = "batch.after_reconcile_commit"
CANCEL_TRIES: Final = 3  # L6443: `messages.batches.cancel` after the commit, 3 tries, then logged
# Carry T16-g: the passes whose result stream stays unreadable before its unread items are decided missing
# (an hour at `batch_poll`'s five-minute cadence).
RESULTS_UNREADABLE_PASSES: Final = 12
# ... and a stream not read whole, for any reason, by the end of the results' retention (L3961: 29 days after
# the creation, its `submit_started_at` at the latest) is given up the same way, with this as its reason.
RESULTS_RETENTION: Final = timedelta(days=29)
RETENTION_PASSED: Final = "results_retention_passed"
# The ambiguous-submit recovery (L3984-3999): the creation window around `submit_started_at`, the unresolved
# alert's age, and how long a released submission is listed for (the results' retention).
WINDOW_BEFORE: Final = timedelta(minutes=1)
WINDOW_AFTER: Final = timedelta(minutes=10)
UNRESOLVED_AFTER: Final = timedelta(hours=25)
RELEASED_LISTED_FOR: Final = timedelta(days=29)
ITEMS_MISSING_ALERT: Final = "batch.items_missing"
CUSTOM_ID_ANOMALY_ALERT: Final = "batch.custom_id_anomaly"
RESULTS_UNREADABLE_ALERT: Final = "batch.results_unreadable"
_SUBMISSIONS: Final = "batch_submissions"
_UNREADABLE_PASSES: Final = "unreadable_passes"

_OWN_EPOCH: Final = q(
    "SELECT worker_epoch FROM operations WHERE id = :i AND claim_id = :c AND status = 'running'"
)
# The poll's submissions, read without a lock, and its owners.
_POLLED: Final = q(
    "SELECT id, state, provider_batch_id, cancel_requested_at FROM batch_submissions "
    "WHERE state IN ('submitted', 'ended') ORDER BY id"
)
_LOCK_SUBMITTED: Final = q(
    "SELECT id FROM batch_submissions WHERE id = :s AND state = 'submitted' FOR UPDATE"
)
_ENDED: Final = q(
    "SELECT id, bulk_operation_id, provider_batch_id, submit_started_at, results_complete_at, "
    "reconcile_report FROM batch_submissions WHERE id = :s AND state = 'ended'"
)
_STATE_OF: Final = q("SELECT state FROM batch_submissions WHERE id = :s")
_ITEMS_OF: Final = q("SELECT id, target_id, attempt_no, custom_id FROM batch_items WHERE submission_id = :s")
_AWAITING: Final = q(
    "SELECT id, target_id, attempt_no, custom_id FROM batch_items "
    "WHERE submission_id = :s AND processed_at IS NULL ORDER BY id"
)
_COUNT_AWAITING: Final = q(
    "SELECT count(*) FROM batch_items WHERE submission_id = :s AND processed_at IS NULL"
)
_ANY_AWAITING: Final = q(
    "SELECT 1 FROM batch_items WHERE submission_id = :s AND processed_at IS NULL LIMIT 1"
)
# The completion's and the reconcile's owners (module docstring).
_LOCK_TO_COMPLETE: Final = q(
    "SELECT id FROM batch_submissions WHERE id = :s AND state = 'ended' AND results_complete_at IS NULL "
    "FOR UPDATE"
)
_MARK_COMPLETE: Final = q(
    "UPDATE batch_submissions SET results_complete_at = :now, "
    "reconcile_report = coalesce(reconcile_report, '{}') || :report WHERE id = :s",
    jsonb=["report"],
)
_COUNT_UNREADABLE: Final = q(
    "UPDATE batch_submissions SET reconcile_report = coalesce(reconcile_report, '{}') || :report "
    "WHERE id = :s AND state = 'ended' AND results_complete_at IS NULL",
    jsonb=["report"],
)
_LOCK_TO_RECONCILE: Final = q(
    "SELECT bulk_operation_id, released_at FROM batch_submissions WHERE id = :s AND state = 'ended' "
    "AND results_complete_at IS NOT NULL FOR UPDATE"
)
# The ambiguous reconciliation's submissions (L3984: every `submit_ambiguous` one, the `released` ones within
# the results' retention), the ids adopted already, its owner and its record of a pass without a match.
_AMBIGUOUS: Final = q(
    "SELECT id, state, submit_started_at, request_count, reserved_usd FROM batch_submissions "
    "WHERE state = 'submit_ambiguous' OR (state = 'released' AND submit_started_at > :since) ORDER BY id"
)
_ADOPTED_IDS: Final = q("SELECT provider_batch_id FROM batch_submissions WHERE provider_batch_id IS NOT NULL")
_LOCK_ADOPTABLE: Final = q(
    "SELECT state, cancel_requested_at FROM batch_submissions WHERE id = :s "
    "AND state IN ('submit_ambiguous', 'released') AND provider_batch_id IS NULL FOR UPDATE"
)
_RECORD_PASS: Final = q(
    "UPDATE batch_submissions SET last_reconcile_at = :now, "
    "reconcile_report = coalesce(reconcile_report, '{}') || :report "
    "WHERE id = :s AND state IN ('submit_ambiguous', 'released')",
    jsonb=["report"],
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
    if cancel_due and isinstance(outcome, Created):  # `batch_poll` sends it again while it has not ended
        await cancel_requested(client, outcome.provider_batch_id, submission_id=s)
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


# ---- Task 17: the provider cancel of a recorded request ----------------------------------------------------


async def cancel_requested(client: BatchesClient, provider_batch_id: str, *, submission_id: int) -> bool:
    """The provider cancel of a request recorded on submission `submission_id` (`cancel_requested_at`,
    L6443): `messages.batches.cancel` up to `CANCEL_TRIES` times, then logged; whether one was accepted. The
    caller has committed the request; `batch_poll` sends it again while the batch has not ended."""
    for tries in range(1, CANCEL_TRIES + 1):
        try:
            status = await client.cancel(provider_batch_id)
        except BatchApiError as failure:
            log.warning(
                "batch_cancel_failed",
                submission_id=submission_id,
                provider_batch_id=provider_batch_id,
                tries=tries,
                error_class=failure.error_class,
                status=failure.status,
            )
            continue
        log.info(
            "batch_cancel_sent",
            submission_id=submission_id,
            provider_batch_id=provider_batch_id,
            processing_status=status.processing_status,
            tries=tries,
        )
        return True
    log.error("batch_cancel_gave_up", submission_id=submission_id, provider_batch_id=provider_batch_id)
    return False


# ---- Task 17: the items of an ended submission -------------------------------------------------------------


@dataclass(frozen=True)
class _Ended:
    """An `ended` submission as the result processing reads it, without a lock."""

    submission: batch_items.Submission
    provider_batch_id: str
    submit_started_at: datetime
    results_complete: bool
    unreadable_passes: int


async def _ended(conn: AsyncConnection, submission_id: int) -> _Ended | None:
    row = (await conn.execute(_ENDED, {"s": submission_id})).first()
    if row is None:
        return None
    report = row.reconcile_report if isinstance(row.reconcile_report, dict) else {}
    passes = report.get(_UNREADABLE_PASSES)
    return _Ended(
        submission=batch_items.Submission(int(row.id), "ended", int(row.bulk_operation_id)),
        provider_batch_id=str(row.provider_batch_id),
        submit_started_at=row.submit_started_at,
        results_complete=row.results_complete_at is not None,
        unreadable_passes=passes if isinstance(passes, int) and not isinstance(passes, bool) else 0,
    )


async def _items_of(conn: AsyncConnection, submission_id: int) -> dict[str, batch_items.Item]:
    """The submission's items by `custom_id` (ids only)."""
    return {
        str(r.custom_id): batch_items.Item(int(r.id), int(r.target_id), int(r.attempt_no))
        for r in await conn.execute(_ITEMS_OF, {"s": submission_id})
    }


async def _process(
    engine: AsyncEngine,
    submission: batch_items.Submission,
    item: batch_items.Item,
    result: ItemResult,
    *,
    now: datetime,
    epoch: int | None,
) -> batch_items.Decision:
    """One item in a transaction of its own (never two: Codex r6 B1), fenced by the worker epoch."""
    async with engine.begin() as conn:
        if epoch is not None:
            await fence_epoch(conn, epoch)
        decision = await batch_items.process_item(
            conn, submission=submission, item=item, result=result, now=now
        )
    await afault_point(AFTER_ITEM)
    return decision


@dataclass(frozen=True)
class _Streamed:
    """What one read of a submission's results did: the items it decided now, its follow-ups, the lines that
    named no item of ours, whether the stream was read to its end, and the class of an unreadable line."""

    decided: int = 0
    followups: int = 0
    foreign: int = 0
    complete: bool = False
    unreadable: str | None = None


async def _stream(
    engine: AsyncEngine,
    client: BatchesClient,
    ended: _Ended,
    *,
    now: datetime,
    epoch: int | None,
) -> _Streamed:
    """The submission's result lines read one by one, each item processed in its own transaction (never
    buffered whole)."""
    async with engine.connect() as conn:
        items = await _items_of(conn, ended.submission.id)
    decided = followups = foreign = 0
    try:
        stream = await client.results(ended.provider_batch_id)
        async for line in stream:
            item = items.get(line.custom_id)
            if item is None:
                foreign += 1
                continue
            decision = await _process(engine, ended.submission, item, line, now=now, epoch=epoch)
            if not decision.replayed:
                decided += 1
                followups += decision.decision == "followup"
    except BatchApiError as failure:
        log.warning(
            "batch_results_incomplete",
            submission_id=ended.submission.id,
            provider_batch_id=ended.provider_batch_id,
            decided=decided,
            error_class=failure.error_class,
            unreadable=failure.unreadable,
        )
        unreadable = failure.error_class if failure.unreadable else None
        return _Streamed(decided, followups, foreign, complete=False, unreadable=unreadable)
    return _Streamed(decided, followups, foreign, complete=stream.complete)


@dataclass(frozen=True)
class Finalized:
    """The completion orchestration's outcome: the items it decided `missing` now, and whether the submission
    is `reconciled`."""

    missing: int
    reconciled: bool


async def finalize_results(
    engine: AsyncEngine,
    submission_id: int,
    *,
    now: datetime,
    defer: Deferrer | None,
    epoch: int | None = None,
) -> Finalized:
    """The one completion orchestration of an `ended` submission whose results were read whole (module
    docstring), shared by the poll and the adoption, in its three kinds of transactions: the completion, each
    missing item, the reconcile. Every step is idempotent; `epoch` fences each transaction."""
    await _complete(engine, submission_id, now=now, epoch=epoch)
    await afault_point(AFTER_COMPLETION_COMMIT)
    async with engine.connect() as conn:
        ended = await _ended(conn, submission_id)
        awaiting = (await conn.execute(_AWAITING, {"s": submission_id})).all()
    if ended is None or not ended.results_complete:
        return Finalized(0, await _is_reconciled(engine, submission_id))
    missing = 0
    for row in awaiting:  # id order, as any item
        item = batch_items.Item(int(row.id), int(row.target_id), int(row.attempt_no))
        decision = await _process(
            engine, ended.submission, item, ItemResult(str(row.custom_id), "missing"), now=now, epoch=epoch
        )
        missing += not decision.replayed
        await afault_point(AFTER_MISSING_ITEM)
    reconciled = await _reconcile(engine, submission_id, now=now, defer=defer, epoch=epoch)
    if reconciled:
        await afault_point(AFTER_RECONCILE_COMMIT)
    return Finalized(missing, reconciled or await _is_reconciled(engine, submission_id))


async def _complete(
    engine: AsyncEngine,
    submission_id: int,
    *,
    now: datetime,
    epoch: int | None,
    unreadable: str | None = None,
) -> bool:
    """The completion transaction (module docstring): `results_complete_at = now` on the `ended` owner whose
    marker is unset, the items still `awaiting` counted, `batch.items_missing` when there are any, and for a
    stream given up as unreadable (`unreadable`, its line's error class, Carry T16-g) the critical
    `batch.results_unreadable`; whether it wrote the marker (a marker set already writes nothing)."""
    async with engine.begin() as conn:
        if epoch is not None:
            await fence_epoch(conn, epoch)
        await cycles.lock_active_scoring(conn)
        if (await conn.execute(_LOCK_TO_COMPLETE, {"s": submission_id})).first() is None:
            return False
        report: dict[str, JSONValue] = {} if unreadable is None else {"results_unreadable": unreadable}
        await conn.execute(_MARK_COMPLETE, {"s": submission_id, "now": now, "report": report})
        missing = int((await conn.execute(_COUNT_AWAITING, {"s": submission_id})).scalar_one())
        subject = str(submission_id)
        if missing:
            await raise_alert(
                conn,
                code=ITEMS_MISSING_ALERT,
                severity="critical",
                subject=subject,
                message="a batch's results were read whole without a line for some of its items",
                context={"submission_id": submission_id, "missing": missing},
            )
        if unreadable is not None:
            await raise_alert(
                conn,
                code=RESULTS_UNREADABLE_ALERT,
                severity="critical",
                subject=subject,
                message="a batch's results could not be read whole; its unread items are decided missing",
                context={"submission_id": submission_id, "error_class": unreadable, "missing": missing},
            )
        await afault_point(BEFORE_COMPLETION_COMMIT)
    log.info("batch_results_complete", submission_id=submission_id, missing=missing, unreadable=unreadable)
    return True


async def _reconcile(
    engine: AsyncEngine, submission_id: int, *, now: datetime, defer: Deferrer | None, epoch: int | None
) -> bool:
    """The reconcile transaction (module docstring): `ended → reconciled` once every item is processed, the
    bulk operation completed when it is done, and the follow-on in the same transaction; whether it
    reconciled now."""
    async with engine.begin() as conn:
        if epoch is not None:
            await fence_epoch(conn, epoch)
        await cycles.lock_active_scoring(conn)
        owner = (await conn.execute(_LOCK_TO_RECONCILE, {"s": submission_id})).first()
        if owner is None or (await conn.execute(_ANY_AWAITING, {"s": submission_id})).first() is not None:
            return False
        await transition(conn, _SUBMISSIONS, submission_id, ["ended"], "reconciled")
        completed = await bulk.complete_if_done(conn, int(owner.bulk_operation_id), now=now)
        requested = await batches.follow_on(conn, submission_id=submission_id, defer=defer)
    log.info(
        "batch_reconciled",
        submission_id=submission_id,
        released=owner.released_at is not None,
        bulk_operation_completed=completed,
        follow_on=requested,
    )
    return True


async def _is_reconciled(engine: AsyncEngine, submission_id: int) -> bool:
    async with engine.connect() as conn:
        return (await conn.execute(_STATE_OF, {"s": submission_id})).scalar_one_or_none() == "reconciled"


async def _results(
    engine: AsyncEngine,
    client: BatchesClient,
    submission_id: int,
    *,
    now: datetime,
    epoch: int,
    defer: Deferrer,
) -> tuple[_Streamed, Finalized | None]:
    """An `ended` submission's results: streamed (unless its marker says it was read whole already), then
    the completion orchestration once the stream was read to its end with every line ours (a cut stream
    decides nothing `missing`, L3961), or once it is given up (Carry T16-g: unreadable for
    `RESULTS_UNREADABLE_PASSES` passes, or not read whole by the end of the results' retention); the
    follow-ups' cycle requested."""
    async with engine.connect() as conn:
        ended = await _ended(conn, submission_id)
    if ended is None:
        return _Streamed(), None
    streamed = _Streamed(complete=True)
    if not ended.results_complete:
        streamed = await _stream(engine, client, ended, now=now, epoch=epoch)
    if streamed.followups:
        async with engine.begin() as conn:
            await fence_epoch(conn, epoch)
            await cycles.request_cycle(conn, trigger="batch_followup", defer=defer)
    if streamed.foreign:
        await _anomaly(engine, submission_id, epoch=epoch, foreign=streamed.foreign)
        return streamed, None
    if not streamed.complete:
        unreadable = streamed.unreadable
        given_up = unreadable is not None and await _unreadable_pass(
            engine, ended, unreadable, now=now, epoch=epoch
        )
        if not given_up and now >= ended.submit_started_at + RESULTS_RETENTION:
            # nothing can be read any more: the read this far is all there is (L3961's 29 days)
            await _complete(engine, submission_id, now=now, epoch=epoch, unreadable=RETENTION_PASSED)
            given_up = True
        if not given_up:
            return streamed, None
    return streamed, await finalize_results(engine, submission_id, now=now, defer=defer, epoch=epoch)


async def _unreadable_pass(
    engine: AsyncEngine, ended: _Ended, error_class: str, *, now: datetime, epoch: int
) -> bool:
    """One more pass whose stream stayed unreadable, counted on the submission (`reconcile_report`); at the
    `RESULTS_UNREADABLE_PASSES`th, the completion with `batch.results_unreadable` (Carry T16-g): whether the
    stream is given up."""
    passes = ended.unreadable_passes + 1
    if passes < RESULTS_UNREADABLE_PASSES:
        async with engine.begin() as conn:
            await fence_epoch(conn, epoch)
            await conn.execute(
                _COUNT_UNREADABLE,
                {
                    "s": ended.submission.id,
                    "report": {_UNREADABLE_PASSES: passes, "error_class": error_class},
                },
            )
        log.warning("batch_results_unreadable", submission_id=ended.submission.id, passes=passes)
        return False
    await _complete(engine, ended.submission.id, now=now, epoch=epoch, unreadable=error_class)
    return True


async def _anomaly(engine: AsyncEngine, submission_id: int, *, epoch: int, foreign: int) -> None:
    async with engine.begin() as conn:
        await fence_epoch(conn, epoch)
        await raise_alert(
            conn,
            code=CUSTOM_ID_ANOMALY_ALERT,
            severity="critical",
            subject=str(submission_id),
            message="a batch's results name items of no submission of ours; it is not completed",
            context={"submission_id": submission_id, "foreign_lines": foreign},
        )
    log.error("batch_custom_id_anomaly", submission_id=submission_id, foreign_lines=foreign)


# ---- Task 17: the poll (`batch_poll`) ---------------------------------------------------------------------


@dataclass(frozen=True)
class PollReport:
    """What one poll did: the submissions it looked at, the cancels it sent, the batches it found ended, the
    items it decided, their follow-ups, and the submissions it reconciled."""

    polled: int = 0
    cancels: int = 0
    ended: int = 0
    decided: int = 0
    followups: int = 0
    reconciled: int = 0


async def _observe(
    engine: AsyncEngine, client: BatchesClient, row: Row[tuple[object, ...]], *, epoch: int
) -> tuple[bool, bool]:
    """A `submitted` submission: its recorded cancel sent (`cancel_requested`), then the batch retrieved and,
    once `ended`, the move `submitted → ended`; whether a cancel was sent and whether it ended."""
    submission_id, provider_batch_id = int(row.id), str(row.provider_batch_id)
    cancelled = False
    if row.cancel_requested_at is not None:
        cancelled = await cancel_requested(client, provider_batch_id, submission_id=submission_id)
    try:
        status = await client.retrieve(provider_batch_id)
    except BatchApiError as failure:
        log.warning(
            "batch_retrieve_failed",
            submission_id=submission_id,
            error_class=failure.error_class,
            status=failure.status,
        )
        return cancelled, False
    if status.processing_status != "ended":
        return cancelled, False
    async with engine.begin() as conn:
        await fence_epoch(conn, epoch)
        await cycles.lock_active_scoring(conn)
        if (await conn.execute(_LOCK_SUBMITTED, {"s": submission_id})).first() is None:
            return cancelled, False  # moved meanwhile
        await transition(conn, _SUBMISSIONS, submission_id, ["submitted"], "ended")
    log.info("batch_ended", submission_id=submission_id, provider_batch_id=provider_batch_id)
    return cancelled, True


async def poll(
    engine: AsyncEngine, client: BatchesClient, *, now: datetime, epoch: int, defer: Deferrer
) -> PollReport:
    """The body of `batch_poll` (module docstring): every `submitted` submission observed (its recorded
    cancel first), and every `ended` one's results processed and completed; one submission's failure leaves
    the others to this pass and itself to the next."""
    async with engine.connect() as conn:
        rows = (await conn.execute(_POLLED)).all()
    report = PollReport(polled=len(rows))
    for row in rows:
        if row.state == "submitted":
            cancelled, ended = await _observe(engine, client, row, epoch=epoch)
            report = replace(report, cancels=report.cancels + cancelled, ended=report.ended + ended)
            if not ended:
                continue
        streamed, finalized = await _results(engine, client, int(row.id), now=now, epoch=epoch, defer=defer)
        report = replace(
            report,
            decided=report.decided + streamed.decided + (0 if finalized is None else finalized.missing),
            followups=report.followups + streamed.followups,
            reconciled=report.reconciled + (finalized is not None and finalized.reconciled),
        )
    if rows:
        log.info("batch_polled", **asdict(report))
    return report


# ---- Task 17: the ambiguous-submit reconciliation (`batch_reconcile_ambiguous`) ----------------------------


@dataclass(frozen=True)
class ReconcileReport:
    """What one reconciliation did: the submissions it looked at, the batches the listing held (`None`: the
    listing failed), the submissions it adopted and reconciled, the anomalies and the unresolved alerts it
    opened."""

    submissions: int = 0
    listed: int | None = None
    adopted: int = 0
    reconciled: int = 0
    anomalies: int = 0
    unresolved: int = 0


async def _unresolved(
    engine: AsyncEngine, rows: Sequence[Row[tuple[object, ...]]], *, now: datetime, epoch: int
) -> int:
    """`batch.submit_ambiguous_unresolved` for each `submit_ambiguous` submission 25 h after its submit-start
    (never a transition, chair decision C3); how many it opened now."""
    due = [
        r
        for r in rows
        if r.state == "submit_ambiguous"
        and isinstance(r.submit_started_at, datetime)
        and r.submit_started_at < now - UNRESOLVED_AFTER
    ]
    if not due:
        return 0
    opened = 0
    async with engine.begin() as conn:
        await fence_epoch(conn, epoch)
        for r in due:
            found = await open_alert(
                conn,
                code=batches.UNRESOLVED_ALERT,
                severity="critical",
                subject=str(r.id),
                message="a batch submission is still ambiguous 25 h after its send; release it to re-score",
                context={"submission_id": int(r.id), "reserved_usd": str(r.reserved_usd)},
            )
            opened += found is not None
    return opened


async def _custom_ids(client: BatchesClient, provider_batch_id: str) -> frozenset[str] | None:
    """The `custom_id`s of a candidate's results, read whole (ids only); `None` when the stream did not
    end."""
    seen: set[str] = set()
    try:
        stream = await client.results(provider_batch_id)
        async for line in stream:
            seen.add(line.custom_id)
    except BatchApiError as failure:
        log.warning(
            "batch_candidate_unread",
            provider_batch_id=provider_batch_id,
            error_class=failure.error_class,
            unreadable=failure.unreadable,
        )
        return None
    return frozenset(seen) if stream.complete else None


async def _adopt(
    engine: AsyncEngine, submission_id: int, candidate: BatchSummary, *, now: datetime, epoch: int
) -> bool:
    """The adoption transaction (module docstring): the candidate's id adopted and the submission `ended`
    (a released one keeps `released_at`, outside the singleton); whether it adopted now."""
    async with engine.begin() as conn:
        await fence_epoch(conn, epoch)
        await cycles.lock_active_scoring(conn)
        owner = (await conn.execute(_LOCK_ADOPTABLE, {"s": submission_id})).first()
        if owner is None:
            return False
        await transition(
            conn,
            _SUBMISSIONS,
            submission_id,
            [str(owner.state)],
            "ended",
            set_={
                "provider_batch_id": candidate.id,
                "expires_at": candidate.created_at + batches.BATCH_TTL,
                "last_reconcile_at": now,
            },
        )
    log.warning(
        "batch_adopted",
        submission_id=submission_id,
        provider_batch_id=candidate.id,
        origin=str(owner.state),
        cancel_requested=owner.cancel_requested_at is not None,
    )
    return True


async def _record_pass(
    engine: AsyncEngine, submission_id: int, *, now: datetime, epoch: int, candidates: int, listed: int
) -> None:
    async with engine.begin() as conn:
        await fence_epoch(conn, epoch)
        await conn.execute(
            _RECORD_PASS,
            {"s": submission_id, "now": now, "report": {"candidates": candidates, "listed": listed}},
        )


async def reconcile_ambiguous(
    engine: AsyncEngine, client: BatchesClient, *, now: datetime, epoch: int, defer: Deferrer
) -> ReconcileReport:
    """The body of `batch_reconcile_ambiguous` (module docstring)."""
    async with engine.connect() as conn:
        rows = (await conn.execute(_AMBIGUOUS, {"since": now - RELEASED_LISTED_FOR})).all()
    if not rows:
        return ReconcileReport()
    unresolved = await _unresolved(engine, rows, now=now, epoch=epoch)
    try:
        listing = [batch async for batch in client.list_all()]
    except BatchApiError as failure:
        log.warning(
            "batch_listing_failed",
            submissions=len(rows),
            error_class=failure.error_class,
            status=failure.status,
        )
        return ReconcileReport(submissions=len(rows), unresolved=unresolved)
    async with engine.connect() as conn:
        adopted_ids = {str(i) for i in (await conn.execute(_ADOPTED_IDS)).scalars()}
    report = ReconcileReport(submissions=len(rows), listed=len(listing), unresolved=unresolved)
    for row in rows:
        report = await _reconcile_one(
            engine,
            client,
            row,
            listing=[b for b in listing if b.id not in adopted_ids],
            report=report,
            now=now,
            epoch=epoch,
            defer=defer,
        )
    log.info("batch_ambiguous_reconciled", **asdict(report))
    return report


async def _reconcile_one(
    engine: AsyncEngine,
    client: BatchesClient,
    row: Row[tuple[object, ...]],
    *,
    listing: Sequence[BatchSummary],
    report: ReconcileReport,
    now: datetime,
    epoch: int,
    defer: Deferrer,
) -> ReconcileReport:
    """One submission's reconciliation (module docstring, rules 1-4)."""
    submission_id = int(row.id)
    started = row.submit_started_at
    if not isinstance(started, datetime):
        return report
    window = [
        b
        for b in listing
        if started - WINDOW_BEFORE <= b.created_at <= started + WINDOW_AFTER
        and b.request_counts.total == row.request_count
    ]
    async with engine.connect() as conn:
        ours = frozenset((await _items_of(conn, submission_id)).keys())
    for candidate in window:
        if candidate.processing_status != "ended":
            continue  # confirmed only once it has ended
        ids = await _custom_ids(client, candidate.id)
        if ids is None or ids.isdisjoint(ours):
            continue
        if ids != ours:  # rule 4: a partial intersection, never adopted
            await _anomaly(engine, submission_id, epoch=epoch, foreign=len(ids - ours))
            return replace(report, anomalies=report.anomalies + 1)
        if not await _adopt(engine, submission_id, candidate, now=now, epoch=epoch):
            return report
        _, finalized = await _results(engine, client, submission_id, now=now, epoch=epoch, defer=defer)
        reconciled = finalized is not None and finalized.reconciled
        return replace(report, adopted=report.adopted + 1, reconciled=report.reconciled + reconciled)
    await _record_pass(
        engine, submission_id, now=now, epoch=epoch, candidates=len(window), listed=len(listing)
    )
    return report


# ---- Task 17: the task bodies (`worker.tasks`) -------------------------------------------------------------


def batches_client(cfg: AppConfig, gateway: LLMGateway) -> BatchesClient:
    """The worker's Batches client on the gateway's `httpx2` client, clocks and sleep (Task 27 moves it into
    the runtime)."""
    return BatchesClient(
        cfg, http_client=gateway.http_client, clock=utcnow, monotonic=gateway.monotonic, sleep=gateway.sleep
    )


async def poll_task(engine: AsyncEngine, cfg: AppConfig, gateway: LLMGateway, *, epoch: int) -> PollReport:
    """`batch_poll`'s body: `poll` at now, nothing without a configured client."""
    client = batches_client(cfg, gateway)
    if not client.configured:
        return PollReport()
    return await poll(engine, client, now=utcnow(), epoch=epoch, defer=defer_in_tx)


async def reconcile_ambiguous_task(
    engine: AsyncEngine, cfg: AppConfig, gateway: LLMGateway, *, epoch: int
) -> ReconcileReport:
    """`batch_reconcile_ambiguous`'s body: `reconcile_ambiguous` at now, nothing without a configured
    client."""
    client = batches_client(cfg, gateway)
    if not client.configured:
        return ReconcileReport()
    return await reconcile_ambiguous(engine, client, now=utcnow(), epoch=epoch, defer=defer_in_tx)
