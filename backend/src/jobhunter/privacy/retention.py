"""Daily retention (spec §20.1), of the M0 data and of the M1 collection data: the body of the
`retention_daily` task.

Each rule removes what has passed its window. Every window is measured from the caller's `now`, never from the
database's `now()` (ruling P14), with one exception: procrastinate's finished jobs are deleted through its own
API (`tasks.delete_finished_jobs`), which compares their latest event with the database clock and returns no
count, so the report holds `-1` for them.

Each rule runs in a transaction of its own (ruling T13-e) and in an error boundary of its own (ruling T13-g):
a rule that fails is reported as `-1`, logged `retention_rule_failed` and raised as the warn alert
`retention.failed` (subject: the rule's name; context: the rule and the error class, never the message), and
the rules after it still run. A rule that succeeds resolves its alert. Every rule is idempotent, so the next
day's run repeats whatever failed.

The rules run in dependency order (§20.1: a row goes only after what references it): the checkpoint threads
of a run's attempts go before the run, and a run's `run_attempts` go with it (`ON DELETE CASCADE`). A live
run (`queued` or `running`) is never deleted, however old.

**The M1 rules** (Task 24), after `operations`; none deletes what the live pipeline still uses:
- `raw_items`: 90 days by `created_at`; an observation keeps its provenance copy (`raw_item_id := NULL`). A
  `deferred('job_fenced')` item stays past the 90 days, as long as its request: a later run re-resolves it
  once its fence is released (§7.16.1).
- `raw_bodies`: `body_zstd := NULL, purged_at := now` once `expires_at` has passed. The window is 0, so the
  cutoff is `now`: `expires_at` already holds `fetched_at + 30 d` and the deduplication extension (§7.7: a row
  whose body a newer row references expires no earlier than that row), so a 30-day window compared with it
  would keep a body about 60 days.
- `ats_snapshots`: 30 days by `taken_at`, except each instance's last complete snapshot, its open mass
  event's first snapshot (`mass_event_snapshot_id`) and every unacknowledged `mass_disappearance` snapshot
  of an event that is still open (Task 19: the confirmation and the acknowledgement read them).
- `embeddings`: every row no `job_revisions.embedding_id` references (`profile_vectors` joins in M2). A row
  that an embedding writer holds `FOR KEY SHARE` (`embeddings.upsert_embedding`, about to reference it) is
  skipped (`SKIP LOCKED`) and goes in a later pass if it is still unreferenced then.
- `coverage_intervals`: 13 months by `coalesce(closed_at, created_at)`.
- `fetch_requests`: terminal ones 13 months by `created_at`, their responses and raw items with them
  (cascade); it depends on `raw_items`.

**Shared bodies.** A response whose body a newer, unpurged response still references (`body_ref_id`; an
unchanged board answers the same bytes every day) is kept with its request, and its run, by both
`fetch_requests` and `runs`: its deletion would set the newer row's `body_ref_id` to NULL while that row has
no body of its own and no `purged_at`, which its CHECK refuses. Once that body expires, `raw_bodies` purges
the newer row too, and both rules can delete in that same pass.

Isolation has one exception, the dependent rules (ruling T21-f): a rule names in `depends_on` the rules whose
work its own would make impossible to finish later. `runs` depends on `checkpoints`, because its cascade
removes the `run_attempts` through which the checkpoint threads are found; `fetch_requests` depends on
`raw_items`, because its cascade deletes the raw items of the requests' responses, which the `raw_items` rule
(with its exception for deferred items) must have handled first. A rule whose dependency failed (or
was skipped itself) earlier in the same pass is skipped: it is reported as `-1` and logged
`retention_rule_skipped` (with `blocked_by`), and it neither raises nor resolves an alert, so the failed
dependency's alert stays open until a pass in which both succeed. Independent rules still run.

This module is a task boundary (ruff `BLE001` is allowed here). The catch around a rule persists its outcome
as the alert, written in a transaction of its own. When that write fails too, `retention_alert_write_failed`
is logged: the database that would hold the outcome is the one that failed, so the log line is what is left
(as in `worker.reconcilers`). Neither log line carries an exception message.
"""

from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

import procrastinate
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql.elements import TextClause

from jobhunter.db.sql import q
from jobhunter.observability.alerts import raise_alert, resolve_alert
from jobhunter.observability.logging import get_logger
from jobhunter.worker.tasks import delete_finished_jobs


@dataclass(frozen=True)
class RetentionRule:
    """What `name` keeps: rows younger than `window` (measured from the caller's `now`).

    `depends_on`: the earlier rules that must have succeeded in the same pass for this one to run.
    """

    name: str
    window: timedelta
    depends_on: tuple[str, ...] = ()


# 13 calendar months last 393 to 397 days; the longest bound keeps every row for at least 13 months.
THIRTEEN_MONTHS: Final = timedelta(days=397)

# In the order they run, which is also the order of the report.
RULES: Final[tuple[RetentionRule, ...]] = (
    RetentionRule("api_idempotency", timedelta(0)),  # once expired (created + 7 days)
    RetentionRule("sessions", timedelta(days=1)),  # expiry + 1 day
    RetentionRule("login_attempts", timedelta(days=30)),
    RetentionRule("alerts", timedelta(days=90)),  # after resolution
    RetentionRule("operations", timedelta(days=30)),  # after they finished
    RetentionRule("raw_items", timedelta(days=90)),
    RetentionRule("raw_bodies", timedelta(0)),  # once `expires_at` (fetched + 30 days, extended) has passed
    RetentionRule("ats_snapshots", timedelta(days=30)),
    RetentionRule("embeddings", timedelta(0)),  # once nothing references them
    RetentionRule("coverage_intervals", THIRTEEN_MONTHS),
    # Its cascade deletes the raw items of the requests' responses, so never after a failed `raw_items`.
    RetentionRule("fetch_requests", THIRTEEN_MONTHS, depends_on=("raw_items",)),
    RetentionRule("checkpoints", timedelta(days=14)),  # threads of the runs that ended that long ago
    # Its cascade deletes the run_attempts that name the checkpoint threads, so never after a failed purge.
    RetentionRule("runs", THIRTEEN_MONTHS, depends_on=("checkpoints",)),
    RetentionRule("procrastinate_jobs", timedelta(days=7)),  # finished jobs
)

# One statement per row rule, each bound to its rule's `cutoff` (`now - window`): a DELETE, or for
# `raw_bodies` the UPDATE that nulls the bodies.
_DELETES: Final[dict[str, TextClause]] = {
    "api_idempotency": q("DELETE FROM api_idempotency WHERE expires_at < :cutoff"),
    "sessions": q("DELETE FROM sessions WHERE absolute_expires_at < :cutoff OR idle_expires_at < :cutoff"),
    "login_attempts": q("DELETE FROM login_attempts WHERE at < :cutoff"),
    "alerts": q("DELETE FROM alerts WHERE resolved_at < :cutoff"),
    "operations": q(
        "DELETE FROM operations "
        "WHERE status IN ('succeeded', 'failed', 'cancelled') AND finished_at < :cutoff"
    ),
    "raw_items": q("DELETE FROM raw_items WHERE created_at < :cutoff AND disposition <> 'deferred'"),
    "raw_bodies": q(
        "UPDATE raw_responses SET body_zstd = NULL, purged_at = :cutoff "
        "WHERE expires_at < :cutoff AND purged_at IS NULL"
    ),
    "ats_snapshots": q(
        "DELETE FROM ats_snapshots s WHERE s.taken_at < :cutoff "
        "AND NOT EXISTS (SELECT 1 FROM source_instances i "
        "WHERE i.last_complete_snapshot_id = s.id OR i.mass_event_snapshot_id = s.id) "
        "AND NOT (s.status = 'mass_disappearance' AND s.acknowledged_at IS NULL "
        "AND EXISTS (SELECT 1 FROM source_instances i "
        "WHERE i.mass_event_snapshot_id = coalesce(s.event_snapshot_id, s.id)))"
    ),
    "embeddings": q(
        "DELETE FROM embeddings WHERE id IN (SELECT e.id FROM embeddings e WHERE e.created_at < :cutoff "
        "AND NOT EXISTS (SELECT 1 FROM job_revisions r WHERE r.embedding_id = e.id) "
        "ORDER BY e.id FOR UPDATE SKIP LOCKED)"
    ),
    "coverage_intervals": q("DELETE FROM coverage_intervals WHERE coalesce(closed_at, created_at) < :cutoff"),
    # Terminal and created before the cutoff, unless a newer, unpurged response references the body of its
    # response or of a detail request's (with a list request goes its detail requests: cascade).
    "fetch_requests": q(
        "DELETE FROM fetch_requests p "
        "WHERE p.status IN ('succeeded', 'failed', 'ambiguous', 'skipped') AND p.created_at < :cutoff "
        "AND NOT EXISTS (SELECT 1 FROM fetch_requests f "
        "JOIN raw_responses b ON b.fetch_request_id = f.id JOIN raw_responses d ON d.body_ref_id = b.id "
        "WHERE (f.id = p.id OR f.parent_fetch_request_id = p.id) AND d.purged_at IS NULL)"
    ),
    # Terminal and finished, or never started (`missed`, `skipped_overlap`) and queued, before the cutoff;
    # not while a newer, unpurged response references a body of its requests (their cascade would orphan it).
    "runs": q(
        "DELETE FROM runs r "
        "WHERE ((r.status IN ('succeeded', 'partial', 'failed', 'failed_invariant', 'cancelled') "
        "AND r.finished_at < :cutoff) "
        "OR (r.status IN ('missed', 'skipped_overlap') AND r.queued_at < :cutoff)) "
        "AND NOT EXISTS (SELECT 1 FROM fetch_requests f "
        "JOIN raw_responses b ON b.fetch_request_id = f.id JOIN raw_responses d ON d.body_ref_id = b.id "
        "WHERE f.run_id = r.id AND d.purged_at IS NULL)"
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

FAILED_ALERT_CODE: Final = "retention.failed"

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


async def _apply(
    rule: RetentionRule,
    engine: AsyncEngine,
    queue: procrastinate.App,
    saver: AsyncPostgresSaver,
    now: datetime,
) -> int:
    cutoff = now - rule.window
    if rule.name == "checkpoints":
        return await _purge_threads(engine, saver, cutoff)
    if rule.name == "procrastinate_jobs":
        return await _delete_finished_jobs(queue, rule.window)
    return await _delete_rows(engine, _DELETES[rule.name], cutoff)


async def _raise_failure(engine: AsyncEngine, name: str, error_class: str) -> None:
    async with engine.begin() as conn:
        await raise_alert(
            conn,
            code=FAILED_ALERT_CODE,
            severity="warn",
            subject=name,
            message=f"retention rule {name} failed",
            context={"rule": name, "error_class": error_class},
        )


async def _resolve_failure(engine: AsyncEngine, name: str) -> None:
    async with engine.begin() as conn:
        await resolve_alert(conn, code=FAILED_ALERT_CODE, subject=name)


async def _record_alert_outcome(name: str, write: Awaitable[None]) -> None:
    """Await the alert `write` of rule `name`; a failure of it is logged and goes no further."""
    try:
        await write
    except Exception as exc:
        log.error("retention_alert_write_failed", rule=name, error_class=type(exc).__name__)


async def run_retention_daily(
    engine: AsyncEngine, queue: procrastinate.App, saver: AsyncPostgresSaver, *, now: datetime
) -> dict[str, int]:
    """Apply every rule of `RULES` at `now`, in order; the number deleted by rule name, `-1` when it failed
    or was skipped (a dependency of it failed in this pass).

    `checkpoints` counts threads; `procrastinate_jobs` is always `-1`. `queue` must be open.
    """
    report: dict[str, int] = {}
    not_done: set[str] = set()  # the rules that failed or were skipped in this pass
    for rule in RULES:
        blocked_by = [name for name in rule.depends_on if name in not_done]
        if blocked_by:
            report[rule.name] = -1
            not_done.add(rule.name)
            log.warning("retention_rule_skipped", rule=rule.name, blocked_by=blocked_by)
            continue
        try:
            report[rule.name] = await _apply(rule, engine, queue, saver, now)
        except Exception as exc:
            report[rule.name] = -1
            not_done.add(rule.name)
            error_class = type(exc).__name__
            # Not the exception's message and not its traceback: either may carry content.
            log.error("retention_rule_failed", rule=rule.name, error_class=error_class)
            await _record_alert_outcome(rule.name, _raise_failure(engine, rule.name, error_class))
        else:
            await _record_alert_outcome(rule.name, _resolve_failure(engine, rule.name))
    log.info("retention_done", **report)
    return report
