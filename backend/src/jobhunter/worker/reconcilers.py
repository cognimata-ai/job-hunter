"""Reconcilers (spec §17.4): idempotent repairs that run on a cadence and make durable intents come true.

A reconciler is an async function of a `ReconcileContext` that returns how many rows it repaired. It opens its
own transactions on `ctx.engine`. `run_reconcilers` runs those registered for a cadence, in registration
order, and isolates them from each other: a reconciler that raises is recorded as `-1` and as the open alert
`reconciler.failed` (subject: its name), written in a transaction of its own, and the others still run. The
alert and the log line carry the reconciler's name and the exception's class only, never its message. When
the reconciler succeeds again, the alert is resolved.

**The epoch fence** (final review F2, Ruling FR-h). Reconcilers are worker-owned maintenance: each fences its
own writes with `ctx.epoch` (`epoch.fence_epoch`), and so do the alert writes here (the first statement of
their transaction; no job row is locked). A `FencedOut` of that fence (`FencedOut.worker_superseded`), from a
reconciler or from an alert write, is no failure: this worker is superseded, so the cadence stops there
(logged `reconcilers_fenced` with the cadence and the reconciler), a superseded worker never raises or
resolves `reconciler.failed`, and it requests its own graceful stop (`ReconcileContext.on_superseded`, Ruling
T24-a), as a fenced task body does. A `FencedOut` of another owner (a run, a purge fence) that a reconciler
lets escape is a failure of that reconciler, recorded like any other; it never stops the worker (F5 review,
Minor 1).

This module is a task boundary (ruff `BLE001` is allowed here), and the catch persists an outcome (§17.1).

`stalled_jobs` (fenced like the others) and the heavy `metrics_refresh` are registered here; the tasks that
own the other repairs register theirs. A module that owns gauges read from the database registers their
refresh with `gauge_refresh` (M3 Task 18: `scoring.reconcilers`' score lane gauges), and `metrics_refresh`
runs each after its own, on its read-only connection, at the cadence's `now`: this module imports none of
them.

**The system alerts** (M5 Task 3, O-ops Q11, Q18, O12, M2 Carry-forward 3). `metrics_refresh` also judges what
nothing else watches, each alert raised while its condition holds (`raise_alert`, which escalates) and
resolved once it clears:

- `system.disk_pressure` (subject `disk`): the filesystem of the data volumes, measured where the CV store is
  (`cfg.cv_store_dir`), used of used plus available as `df` reads it (the blocks reserved for root count for
  neither): warn from `DISK_WARN_RATIO`, critical from `DISK_CRITICAL_RATIO` (the open warn alert escalates in
  place), resolved below `DISK_WARN_RATIO`. A filesystem it cannot read (`OSError`) is logged
  `disk_usage_unreadable` with the error's class and judged neither way.
- `system.table_growth` (info, subject `table:<name>`): `procrastinate_jobs` with a row estimate
  (`pg_class.reltuples`, maintained by autovacuum) above `QUEUE_ROWS_LIMIT`, or any table whose total size
  (`metrics.refresh_table_bytes`) is above `TABLE_BYTES_LIMIT` (§17.6's "10 GB"); every other open one is
  resolved.
- `embedder.checksum_mismatch` (critical, subject `embedder`): the embedder's `/health` (the process's client,
  `ctx.embedder`) answering `checksum_ok: false`, resolved by `true`; an embedder that gives no usable answer
  judges neither way. Best effort (O-ops O12): the real embedder exits 78 on a mismatch (ruling T21-b), which
  shows as readiness `embedder`, `embedder.unavailable` and the dead-man alarm.
- `docparse.leftover` (warn, subject `docparse`): docparse's marker `DOCPARSE_LEFTOVER_MARKER` in the parse
  root (`cfg.parse_jobs_dir`), which docparse writes before it exits after a sweep that could not remove a
  leftover and removes after a clean start sweep (`docparse/worker.py`); resolved once it is gone.

The filesystem and the embedder are read first, outside any transaction; then one transaction, fenced by
`ctx.epoch` like every reconciler write, writes the alerts and refreshes the open-alerts gauge last, so the
gauge counts them. Without a configuration (`ctx.cfg`) the disk and the marker are not judged, without an
embedder client the checksum is not.
"""

import os
import shutil
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

import procrastinate
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.config import AppConfig
from jobhunter.db.sql import q
from jobhunter.errors import FencedOut
from jobhunter.observability.alerts import raise_alert, refresh_alert_gauge, resolve_alert
from jobhunter.observability.logging import get_logger
from jobhunter.observability.metrics import refresh_table_bytes
from jobhunter.profile import parse_client
from jobhunter.types import JSONValue
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.tasks import Deferrer, release_stalled_jobs

if TYPE_CHECKING:
    # Only for the annotation: `embeddings` imports this module to register its reconciler.
    from jobhunter.embeddings import EmbedderClient

Cadence = Literal["start", "tick", "heavy", "daily"]

FAILED_ALERT_CODE = "reconciler.failed"

# `metrics_refresh`'s system alerts (module docstring, §17.6).
DISK_WARN_RATIO: Final = 0.80
DISK_CRITICAL_RATIO: Final = 0.90
QUEUE_ROWS_LIMIT: Final = 1_000_000
TABLE_BYTES_LIMIT: Final = 10_000_000_000  # §17.6's "10 GB"
DOCPARSE_LEFTOVER_MARKER: Final = parse_client.LEFTOVER_MARKER  # the worker's half of the file protocol
DISK_PRESSURE_ALERT: Final = "system.disk_pressure"
TABLE_GROWTH_ALERT: Final = "system.table_growth"
CHECKSUM_MISMATCH_ALERT: Final = "embedder.checksum_mismatch"
DOCPARSE_LEFTOVER_ALERT: Final = "docparse.leftover"
QUEUE_TABLE: Final = "procrastinate_jobs"
# The planner's row estimate of the queue table (-1 before its first vacuum or analyze counts as 0).
_QUEUE_ROWS: Final = q(
    "SELECT CAST(greatest(reltuples, 0) AS bigint) FROM pg_class "
    "WHERE oid = to_regclass('procrastinate_jobs')"
)
_OPEN_SUBJECTS: Final = q(
    "SELECT substr(dedup_key, length(code) + 2) FROM alerts WHERE code = :code AND resolved_at IS NULL"
)

log = get_logger("jobhunter.worker.reconcilers")


@dataclass(frozen=True)
class ReconcileContext:
    """What a reconciler gets: the engine, the task app and its deferrer, the cadence's `now`, the worker
    epoch, the process's embedder client (`awaiting_embedding`, `profile_vectors` and `metrics_refresh` need
    it; `None` where there is none) and the process's configuration (`metrics_refresh` reads its volumes'
    paths; `None` where there is none).

    `on_superseded` is what `run_reconcilers` calls once a fence refused `epoch`: the worker requests its own
    graceful stop there (Ruling T24-a; the task bodies and the start pass it, `None` does nothing)."""

    engine: AsyncEngine
    queue: procrastinate.App
    defer: Deferrer
    now: datetime
    epoch: int
    embedder: "EmbedderClient | None" = None
    on_superseded: Callable[[], None] | None = None
    cfg: AppConfig | None = None


ReconcilerFn = Callable[[ReconcileContext], Awaitable[int]]

# Insertion ordered: reconcilers run in registration order.
REGISTRY: dict[str, tuple[frozenset[Cadence], ReconcilerFn]] = {}


GaugeRefresh = Callable[[AsyncConnection, datetime], Awaitable[None]]

# Insertion ordered: `metrics_refresh` runs them in registration order.
GAUGE_REFRESHES: dict[str, GaugeRefresh] = {}


def gauge_refresh(name: str) -> Callable[[GaugeRefresh], GaugeRefresh]:
    """Decorator: `metrics_refresh` runs the function under `name` with its connection and `now` (module
    docstring)."""

    def register(fn: GaugeRefresh) -> GaugeRefresh:
        registered = GAUGE_REFRESHES.get(name)
        if registered is not None and registered is not fn:
            raise ValueError(f"another gauge refresh is already registered as {name!r}")
        GAUGE_REFRESHES[name] = fn
        return fn

    return register


def reconciler(name: str, *cadences: Cadence) -> Callable[[ReconcilerFn], ReconcilerFn]:
    """Decorator: register the function under `name` to run at each of `cadences`."""

    def register(fn: ReconcilerFn) -> ReconcilerFn:
        registered = REGISTRY.get(name)
        if registered is not None and registered[1] is not fn:
            raise ValueError(f"another reconciler is already registered as {name!r}")
        REGISTRY[name] = (frozenset(cadences), fn)
        return fn

    return register


async def _persist_failure(ctx: ReconcileContext, name: str, error_class: str) -> None:
    """Raise (or count one more occurrence of) the alert for `name`, in a transaction of its own, fenced by
    `ctx.epoch`."""
    async with ctx.engine.begin() as conn:
        await fence_epoch(conn, ctx.epoch)
        await raise_alert(
            conn,
            code=FAILED_ALERT_CODE,
            severity="warn",
            subject=name,
            message=f"reconciler {name} failed",
            context={"reconciler": name, "error_class": error_class},
        )


async def _resolve_failure(ctx: ReconcileContext, name: str) -> None:
    async with ctx.engine.begin() as conn:
        await fence_epoch(conn, ctx.epoch)
        await resolve_alert(conn, code=FAILED_ALERT_CODE, subject=name)


async def _record_alert_outcome(name: str, write: Awaitable[None]) -> None:
    """Await the alert `write` of reconciler `name`; a failure of it is logged and does not propagate, except
    a `FencedOut` (a newer epoch is current), which stops the cadence."""
    try:
        await write
    except FencedOut:
        raise
    except Exception as exc:
        log.error("reconciler_alert_write_failed", reconciler=name, error_class=type(exc).__name__)


async def run_reconcilers(ctx: ReconcileContext, cadence: Cadence) -> dict[str, int]:
    """Run the reconcilers of `cadence`; the repaired count of each by name, `-1` for one that failed.

    A reconciler that raises is logged as `reconciler_failed` first, then recorded as the alert. When the
    alert write (or the resolution of an earlier alert) fails in turn, that is logged as
    `reconciler_alert_write_failed`: the database that would hold the outcome is the one that failed, so the
    log line is the outcome left. It does not stop the reconcilers that follow, and the `-1` is still
    recorded. Neither log line carries an exception message.

    A `FencedOut` of the worker's own fence (`FencedOut.worker_superseded`), from a reconciler or its alert
    write, stops the cadence (module docstring): the counts so far are returned, logged `reconcilers_fenced`,
    and no alert is written for it; `ctx.on_superseded()` then asks this superseded worker for its graceful
    stop. A `FencedOut` of another owner that a reconciler lets escape is a failure like any other.
    """
    counts: dict[str, int] = {}
    for name, (cadences, fn) in list(REGISTRY.items()):
        if cadence not in cadences:
            continue
        try:
            try:
                counts[name] = await fn(ctx)
            except Exception as exc:
                if isinstance(exc, FencedOut) and exc.worker_superseded:
                    raise
                # Any other `FencedOut` (a run's, a purge fence's) is one piece of work lost: a failure of
                # this reconciler, never a reason to stop the worker (F5 review, Minor 1).
                counts[name] = -1
                error_class = type(exc).__name__
                # Not the exception's message and not its traceback: either may carry content.
                log.error("reconciler_failed", reconciler=name, error_class=error_class)
                await _record_alert_outcome(name, _persist_failure(ctx, name, error_class))
            else:
                await _record_alert_outcome(name, _resolve_failure(ctx, name))
        except FencedOut as exc:
            # `fence_epoch` logged both epochs; the fenced transaction wrote nothing. Only the worker's own
            # fence gets here (the alert writes are fenced by the epoch alone).
            log.warning("reconcilers_fenced", cadence=cadence, reconciler=name)
            if exc.worker_superseded and ctx.on_superseded is not None:
                ctx.on_superseded()
            return counts
    return counts


@reconciler("stalled_jobs", "start", "tick")
async def stalled_jobs(ctx: ReconcileContext) -> int:
    """Release the jobs of dead workers: the one stalled-job path (spike V2), at start and on every tick.

    Fenced by `ctx.epoch` like `queued_runs`, `operations` and `stale_data` (Ruling FR-h; fix wave F7): the
    fence is held in an open transaction while procrastinate lists and retries the jobs on its own pool, so
    no takeover commits in between, and a superseded worker raises `FencedOut` having released nothing,
    which stops its cadence. No job row is locked before it. The fence is held for one listing and a retry
    per stalled job (a dead worker's few jobs), far below the takeover's lock timeout.
    """
    async with ctx.engine.begin() as fenced:
        await fence_epoch(fenced, ctx.epoch)
        return await release_stalled_jobs(ctx.queue)


def _disk_used(path: Path) -> float | None:
    """The used share of the filesystem of `path` as `df` reads it (module docstring); `None`, logged, when it
    cannot be read."""
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        log.warning("disk_usage_unreadable", error_class=type(exc).__name__)
        return None
    usable = usage.used + usage.free
    return usage.used / usable if usable > 0 else None


def _marker_present(root: Path) -> bool:
    """Whether docparse's leftover marker is in the parse root: the entry itself, never a symlink's target.
    A root that is gone holds none; any other error propagates."""
    try:
        os.lstat(root / DOCPARSE_LEFTOVER_MARKER)
    except FileNotFoundError:
        return False
    return True


async def _table_growth(conn: AsyncConnection, sizes: Mapping[str, int], queue_rows: int) -> None:
    """`system.table_growth` for each table over its limit; every other open one resolved."""
    over: dict[str, dict[str, JSONValue]] = {
        table: {"table": table, "bytes": size} for table, size in sizes.items() if size > TABLE_BYTES_LIMIT
    }
    if queue_rows > QUEUE_ROWS_LIMIT:
        over.setdefault(QUEUE_TABLE, {"table": QUEUE_TABLE})["rows"] = queue_rows
    for table, context in sorted(over.items()):
        await raise_alert(
            conn,
            code=TABLE_GROWTH_ALERT,
            severity="info",
            subject=f"table:{table}",
            message=f"table {table} is above its growth limit",
            context=context,
        )
    subjects = {f"table:{table}" for table in over}
    for subject in (await conn.execute(_OPEN_SUBJECTS, {"code": TABLE_GROWTH_ALERT})).scalars().all():
        if subject not in subjects:
            await resolve_alert(conn, code=TABLE_GROWTH_ALERT, subject=str(subject))


async def _disk_pressure(conn: AsyncConnection, used: float) -> None:
    """`system.disk_pressure` at the used share `used`: warn, critical (escalated), or resolved."""
    if used < DISK_WARN_RATIO:
        await resolve_alert(conn, code=DISK_PRESSURE_ALERT, subject="disk")
        return
    pct = round(used * 100, 1)
    await raise_alert(
        conn,
        code=DISK_PRESSURE_ALERT,
        severity="critical" if used >= DISK_CRITICAL_RATIO else "warn",
        subject="disk",
        message=f"the data volumes' disk is {pct} % full",
        context={"used_pct": pct},
    )


async def _checksum(conn: AsyncConnection, checksum_ok: bool) -> None:
    """`embedder.checksum_mismatch` raised on a mismatch, resolved on a match."""
    if checksum_ok:
        await resolve_alert(conn, code=CHECKSUM_MISMATCH_ALERT, subject="embedder")
        return
    await raise_alert(
        conn,
        code=CHECKSUM_MISMATCH_ALERT,
        severity="critical",
        subject="embedder",
        message="the embedder's model checksum does not match its lock",
    )


async def _docparse_leftover(conn: AsyncConnection, present: bool) -> None:
    """`docparse.leftover` raised while docparse's marker is there, resolved once it is gone."""
    if not present:
        await resolve_alert(conn, code=DOCPARSE_LEFTOVER_ALERT, subject="docparse")
        return
    await raise_alert(
        conn,
        code=DOCPARSE_LEFTOVER_ALERT,
        severity="warn",
        subject="docparse",
        message="docparse could not remove a parse's leftover and exited; it restarts",
    )


@reconciler("metrics_refresh", "heavy")
async def metrics_refresh(ctx: ReconcileContext) -> int:
    """Refresh the gauges read from the database: the size of every table, then each registered gauge refresh
    (`gauge_refresh`) at `ctx.now`; then the system alerts (module docstring), and the open alerts by severity
    last, in the alerts' transaction.

    It repairs nothing, so it returns 0. Raises `FencedOut` when `ctx.epoch` is no longer the current worker
    epoch (`fence_epoch` logged it; its transaction wrote nothing), as every reconciler write does.
    """
    async with ctx.engine.connect() as conn:
        sizes = await refresh_table_bytes(conn)
        queue_rows = int((await conn.execute(_QUEUE_ROWS)).scalar_one_or_none() or 0)
        for refresh in list(GAUGE_REFRESHES.values()):
            await refresh(conn, ctx.now)
    health = None if ctx.embedder is None else await ctx.embedder.health()
    used = None if ctx.cfg is None else _disk_used(ctx.cfg.cv_store_dir)
    leftover = None if ctx.cfg is None else _marker_present(ctx.cfg.parse_jobs_dir)
    async with ctx.engine.begin() as conn:
        await fence_epoch(conn, ctx.epoch)
        await _table_growth(conn, sizes, queue_rows)
        if used is not None:
            await _disk_pressure(conn, used)
        if health is not None and health.checksum_ok is not None:
            await _checksum(conn, health.checksum_ok)
        if leftover is not None:
            await _docparse_leftover(conn, leftover)
        await refresh_alert_gauge(conn)
    return 0
