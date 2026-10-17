"""Daily retention of the M0 data (spec §20.1): the body of the `retention_daily` task.

Each rule removes what has passed its window. Every window is measured from the caller's `now`, never from the
database's `now()` (ruling P14), with one exception: procrastinate's finished jobs are deleted through its own
API (`tasks.delete_finished_jobs`), which compares their latest event with the database clock and returns no
count, so the report holds `-1` for them.

Each rule runs in a transaction of its own (ruling T13-e), so a rule that fails leaves the rules before it
committed. The rules run in dependency order (§20.1: a row goes only after what references it): the checkpoint
threads of a run's attempts go before the run, and a run's `run_attempts` go with it (`ON DELETE CASCADE`).
A live run (`queued` or `running`) is never deleted, however old.

No exception is caught here: a failing rule propagates to the task boundary (procrastinate records the job as
failed and logs it), the rules after it wait for the next day, and every rule is idempotent.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

import procrastinate
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql.elements import TextClause

from jobhunter.db.sql import q
from jobhunter.observability.logging import get_logger
from jobhunter.worker.tasks import delete_finished_jobs


@dataclass(frozen=True)
class RetentionRule:
    """What `name` keeps: rows younger than `window` (measured from the caller's `now`)."""

    name: str
    window: timedelta


# 13 calendar months last 393 to 397 days; the longest bound keeps every row for at least 13 months.
THIRTEEN_MONTHS: Final = timedelta(days=397)

# In the order they run, which is also the order of the report.
RULES: Final[tuple[RetentionRule, ...]] = (
    RetentionRule("api_idempotency", timedelta(0)),  # once expired (created + 7 days)
    RetentionRule("sessions", timedelta(days=1)),  # expiry + 1 day
    RetentionRule("login_attempts", timedelta(days=30)),
    RetentionRule("alerts", timedelta(days=90)),  # after resolution
    RetentionRule("operations", timedelta(days=30)),  # after they finished
    RetentionRule("checkpoints", timedelta(days=14)),  # threads of the runs that ended that long ago
    RetentionRule("runs", THIRTEEN_MONTHS),
    RetentionRule("procrastinate_jobs", timedelta(days=7)),  # finished jobs
)

# One DELETE per row rule, each bound to its rule's `cutoff` (`now - window`).
_DELETES: Final[dict[str, TextClause]] = {
    "api_idempotency": q("DELETE FROM api_idempotency WHERE expires_at < :cutoff"),
    "sessions": q("DELETE FROM sessions WHERE absolute_expires_at < :cutoff OR idle_expires_at < :cutoff"),
    "login_attempts": q("DELETE FROM login_attempts WHERE at < :cutoff"),
    "alerts": q("DELETE FROM alerts WHERE resolved_at < :cutoff"),
    "operations": q(
        "DELETE FROM operations "
        "WHERE status IN ('succeeded', 'failed', 'cancelled') AND finished_at < :cutoff"
    ),
    # Terminal and finished, or never started (`missed`, `skipped_overlap`) and queued, before the cutoff.
    "runs": q(
        "DELETE FROM runs "
        "WHERE (status IN ('succeeded', 'partial', 'failed', 'failed_invariant', 'cancelled') "
        "AND finished_at < :cutoff) OR (status IN ('missed', 'skipped_overlap') AND queued_at < :cutoff)"
    ),
}

# The checkpoint threads of the attempts of runs that ended before the cutoff and still have rows in any of
# the saver's tables (so a thread is purged, and counted, once; a partly deleted one is finished next time).
_ENDED_THREADS: Final = q(
    "SELECT a.thread_id FROM run_attempts a JOIN runs r ON r.id = a.run_id "
    "WHERE r.status IN ('succeeded', 'partial', 'failed', 'failed_invariant', 'cancelled') "
    "AND r.finished_at < :cutoff "
    "AND (EXISTS (SELECT 1 FROM checkpoints c WHERE c.thread_id = a.thread_id) "
    "OR EXISTS (SELECT 1 FROM checkpoint_blobs b WHERE b.thread_id = a.thread_id) "
    "OR EXISTS (SELECT 1 FROM checkpoint_writes w WHERE w.thread_id = a.thread_id)) "
    "ORDER BY a.thread_id"
)

log = get_logger("jobhunter.privacy.retention")


async def _delete_rows(engine: AsyncEngine, statement: TextClause, cutoff: datetime) -> int:
    async with engine.begin() as conn:
        return (await conn.execute(statement, {"cutoff": cutoff})).rowcount


async def _purge_threads(engine: AsyncEngine, saver: AsyncPostgresSaver, cutoff: datetime) -> int:
    """Delete the checkpoint threads of the runs that ended before `cutoff`; the number of threads."""
    async with engine.connect() as conn:
        threads = [
            str(thread) for thread in (await conn.execute(_ENDED_THREADS, {"cutoff": cutoff})).scalars()
        ]
    for thread in threads:
        # The saver's own deletion: checkpoints, blobs and writes of the thread, on its autocommit pool.
        await saver.adelete_thread(thread)
    return len(threads)


async def _delete_finished_jobs(queue: procrastinate.App, window: timedelta) -> int:
    await delete_finished_jobs(queue, older_than_hours=int(window / timedelta(hours=1)))
    return -1  # procrastinate returns no count


async def run_retention_daily(
    engine: AsyncEngine, queue: procrastinate.App, saver: AsyncPostgresSaver, *, now: datetime
) -> dict[str, int]:
    """Apply every rule of `RULES` at `now`, in order; the number deleted by rule name.

    `checkpoints` counts threads; `procrastinate_jobs` is always `-1`. `queue` must be open.
    """
    report: dict[str, int] = {}
    for rule in RULES:
        cutoff = now - rule.window
        if rule.name == "checkpoints":
            report[rule.name] = await _purge_threads(engine, saver, cutoff)
        elif rule.name == "procrastinate_jobs":
            report[rule.name] = await _delete_finished_jobs(queue, rule.window)
        else:
            report[rule.name] = await _delete_rows(engine, _DELETES[rule.name], cutoff)
    log.info("retention_done", **report)
    return report
