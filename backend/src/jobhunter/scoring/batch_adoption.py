"""The ambiguous-submit reconciliation of the score lane's batch submissions (spec §10.10 L3984-3999, §18.3
C20-C22; plan Task 17, Ruling T17-b): a `submit_ambiguous` or `released` submission matched against the
workspace's listing, its batch adopted and its results completed by the poll's own path
(`scoring.batch_results`).

**The ambiguous-submit reconciliation** (`reconcile_ambiguous`, the body of `batch_reconcile_ambiguous`, every
15 min on `aux`, L3984-3999), for every `submit_ambiguous` submission and every `released` one with
`submit_started_at > now - 29 days` (A2 B12; an older one is listed no more): first, at 25 h after its
submit-start, each `submit_ambiguous` one opens the critical `batch.submit_ambiguous_unresolved` with the
amount it holds and the unread candidates of the last pass (`reconcile_report.unread_candidates`, 0 without;
never a transition, chair decision C3; also while the listing fails, C22); then one listing
of the whole workspace (`list_all`; a failure leaves every row untouched, logged `batch_listing_failed`; the
workspace is this installation's alone, A2 B15), less the batches some submission adopted already. For each
submission, the candidates are the batches created within `[submit_started_at - 1 min, submit_started_at + 10
min]` whose request count totals ours; the results of every ended candidate are downloaded whole (ids only)
before anything is decided, so the listing's order decides nothing, and a candidate is confirmed only when it
alone names exactly our `custom_id` set and no other intersects it partially: the adoption transaction
(`active_config FOR SHARE`, the owner `FOR UPDATE` under `state IN ('submit_ambiguous', 'released')` without a
provider id) adopts its id and moves the submission to `ended` (a released one keeps `released_at`, outside
the singleton; a recorded cancel stays a record: an ended batch has nothing to cancel, Codex r7 B3), then its
results are processed and completed by the poll's own path in the same pass (every item of a released
submission is `ignored_fenced`; the follow-on fires for a `submit_ambiguous` origin only). A candidate whose
ids intersect ours partially, or more than one equal to ours, raises `batch.custom_id_anomaly` (with how many
lines are foreign and how many of ours are lacking) and adopts nothing (rule 4); a pass without a match
records `last_reconcile_at` and `reconcile_report = {…, "candidates": n, "listed": m}`. A pass in which an
ended candidate of the window cannot be read whole decides nothing for the submission (no adoption, no
anomaly, no transition; logged `batch_adoption_deferred`), since that candidate may intersect ours; the next
pass tries again. It leaves a durable trace (Carry T17-d): `last_reconcile_at` and `reconcile_report = {…,
"candidates": n, "listed": m, "unread_candidates": k}`, the count of candidates it could not read, which the
25 h alert's context then carries; a later pass that reads every candidate (with no match, or adopting ours)
removes the count. One submission's unexpected error is logged with its class and leaves the others to the
pass (Ruling T17-a).

**The task body** (`reconcile_ambiguous_task`, Deviation 27: the periodic task `batch_reconcile_ambiguous` of
`worker.tasks`, under the queueing lock of its name) builds the client from the worker's config and gateway
(`batch_results.batches_client`) and does nothing without a configured one.

Ids, counts, codes and amounts only reach a log line, a payload or a report: never a param or prompt text.
"""

from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.errors import FencedOut
from jobhunter.llm.batches import (
    BatchApiError,
    BatchesClient,
    BatchSummary,
)
from jobhunter.llm.gateway import LLMGateway
from jobhunter.observability.alerts import open_alert
from jobhunter.observability.logging import get_logger
from jobhunter.scoring import batches, cycles
from jobhunter.scoring.batch_results import batches_client, custom_id_anomaly, items_of, process_results
from jobhunter.types import JSONValue
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.tasks import Deferrer, defer_in_tx

# The ambiguous-submit recovery (L3984-3999): the creation window around `submit_started_at`, the unresolved
# alert's age, and how long a released submission is listed for (the results' retention).
WINDOW_BEFORE: Final = timedelta(minutes=1)
WINDOW_AFTER: Final = timedelta(minutes=10)
UNRESOLVED_AFTER: Final = timedelta(hours=25)
RELEASED_LISTED_FOR: Final = timedelta(days=29)
_SUBMISSIONS: Final = "batch_submissions"
# The ambiguous reconciliation's submissions (L3984: every `submit_ambiguous` one, the `released` ones within
# the results' retention), the ids adopted already, its owner and its record of a pass without a match.
_AMBIGUOUS: Final = q(
    "SELECT id, state, submit_started_at, request_count, reserved_usd, "
    "coalesce(CAST(reconcile_report ->> 'unread_candidates' AS integer), 0) AS unread_candidates "
    "FROM batch_submissions "
    "WHERE state = 'submit_ambiguous' OR (state = 'released' AND submit_started_at > :since) ORDER BY id"
)
_ADOPTED_IDS: Final = q("SELECT provider_batch_id FROM batch_submissions WHERE provider_batch_id IS NOT NULL")
_LOCK_ADOPTABLE: Final = q(
    "SELECT state, cancel_requested_at FROM batch_submissions WHERE id = :s "
    "AND state IN ('submit_ambiguous', 'released') AND provider_batch_id IS NULL FOR UPDATE"
)
# A pass's record: the report's keys merged, `unread_candidates` dropped first (a pass that read every
# candidate leaves none; a deferred one writes its own count, Carry T17-d).
_RECORD_PASS: Final = q(
    "UPDATE batch_submissions SET last_reconcile_at = :now, "
    "reconcile_report = (coalesce(reconcile_report, '{}') - 'unread_candidates') || :report "
    "WHERE id = :s AND state IN ('submit_ambiguous', 'released')",
    jsonb=["report"],
)
_FORGET_UNREAD: Final = q(
    "UPDATE batch_submissions SET reconcile_report = reconcile_report - 'unread_candidates' "
    "WHERE id = :s AND reconcile_report ? 'unread_candidates'"
)

log = get_logger("jobhunter.scoring.batch_adoption")


# ---- Task 17: the ambiguous-submit reconciliation (`batch_reconcile_ambiguous`) ----------------------------


@dataclass(frozen=True)
class ReconcileReport:
    """What one reconciliation did: the submissions it looked at, the batches the listing held (`None`: the
    listing failed), the submissions it adopted and reconciled, the anomalies and the unresolved alerts it
    opened, the submissions whose reconciliation raised (each logged and left to the next pass), and those
    left undecided because an ended candidate could not be read whole."""

    submissions: int = 0
    listed: int | None = None
    adopted: int = 0
    reconciled: int = 0
    anomalies: int = 0
    unresolved: int = 0
    failed: int = 0  # submissions whose reconciliation raised this pass (Ruling T17-a)
    unread: int = 0  # submissions left undecided: an ended candidate of theirs could not be read whole


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
                context={
                    "submission_id": int(r.id),
                    "reserved_usd": str(r.reserved_usd),
                    "unread_candidates": int(r.unread_candidates),
                },
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
        await conn.execute(_FORGET_UNREAD, {"s": submission_id})  # this pass read every candidate
    log.warning(
        "batch_adopted",
        submission_id=submission_id,
        provider_batch_id=candidate.id,
        origin=str(owner.state),
        cancel_requested=owner.cancel_requested_at is not None,
    )
    return True


async def _record_pass(
    engine: AsyncEngine,
    submission_id: int,
    *,
    now: datetime,
    epoch: int,
    candidates: int,
    listed: int,
    unread: int = 0,
) -> None:
    """A pass that adopted nothing, recorded (module docstring): with `unread` candidates, their count."""
    report: dict[str, JSONValue] = {"candidates": candidates, "listed": listed}
    if unread:
        report["unread_candidates"] = unread
    async with engine.begin() as conn:
        await fence_epoch(conn, epoch)
        await conn.execute(_RECORD_PASS, {"s": submission_id, "now": now, "report": report})


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
    unadopted = [b for b in listing if b.id not in adopted_ids]
    for row in rows:
        try:
            report = await _reconcile_one(
                engine, client, row, listing=unadopted, report=report, now=now, epoch=epoch, defer=defer
            )
        except FencedOut:
            raise
        except Exception as exc:  # noqa: BLE001 - Ruling T17-a: one submission's error leaves the others to the pass
            log.error("batch_reconcile_failed", submission_id=int(row.id), error_class=type(exc).__name__)
            report = replace(report, failed=report.failed + 1)
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
        ours = frozenset((await items_of(conn, submission_id)).keys())
    # every ended candidate downloaded first (ids only), so the decision is the listing order's in no way
    equal: list[BatchSummary] = []
    partial: list[frozenset[str]] = []
    unread = 0
    for candidate in window:
        if candidate.processing_status != "ended":
            continue  # confirmed only once it has ended
        ids = await _custom_ids(client, candidate.id)
        if ids is None:
            unread += 1
            continue
        if ids.isdisjoint(ours):
            continue
        if ids == ours:
            equal.append(candidate)
        else:
            partial.append(ids)
    if unread:  # a candidate not read whole may intersect ours: nothing is decided until every one is
        log.warning("batch_adoption_deferred", submission_id=submission_id, unread_candidates=unread)
        await _record_pass(
            engine,
            submission_id,
            now=now,
            epoch=epoch,
            candidates=len(window),
            listed=len(listing),
            unread=unread,
        )
        return replace(report, unread=report.unread + 1)
    if partial or len(equal) > 1:  # rule 4 (or no one match): never adopted
        first = partial[0] if partial else ours
        await custom_id_anomaly(
            engine,
            submission_id,
            epoch=epoch,
            foreign=len(first - ours),
            lacking=len(ours - first),
            equal=len(equal),
        )
        return replace(report, anomalies=report.anomalies + 1)
    if equal:  # rule 3: the one candidate whose ids equal ours
        if not await _adopt(engine, submission_id, equal[0], now=now, epoch=epoch):
            return report
        _, finalized = await process_results(engine, client, submission_id, now=now, epoch=epoch, defer=defer)
        reconciled = finalized is not None and finalized.reconciled
        return replace(report, adopted=report.adopted + 1, reconciled=report.reconciled + reconciled)
    await _record_pass(
        engine, submission_id, now=now, epoch=epoch, candidates=len(window), listed=len(listing)
    )
    return report


# ---- Task 17: the task body (`worker.tasks`) ---------------------------------------------------------------


async def reconcile_ambiguous_task(
    engine: AsyncEngine, cfg: AppConfig, gateway: LLMGateway, *, epoch: int
) -> ReconcileReport:
    """`batch_reconcile_ambiguous`'s body: `reconcile_ambiguous` at now, nothing without a configured
    client."""
    client = batches_client(cfg, gateway)
    if not client.configured:
        return ReconcileReport()
    return await reconcile_ambiguous(engine, client, now=utcnow(), epoch=epoch, defer=defer_in_tx)
