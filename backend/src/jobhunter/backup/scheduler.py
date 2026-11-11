"""The backup container's scheduler (spec §19.2, §19.5; plan M5 Task 24, Deviation 29, R15, Rulings T24-1 (a)
and T24-2 (h)): the backup service's command, `jobhunter backup scheduler`, in place of a cron daemon.

**The jobs** (`BackupJob`) are named `set`, `ship`, `prune` and `restore_test`, each with a five-field cron
expression in UTC from `cfg.backup` (`JOBHUNTER_BACKUP_CRON`, `JOBHUNTER_MANIFEST_SHIP_CRON`,
`JOBHUNTER_BACKUP_PRUNE_CRON`, `JOBHUNTER_RESTORE_TEST_CRON`; an empty value takes the default). `next_fire`
is a job's first tick strictly after a time (`backup.cron.next_tick`); the restore test's tick counts only on
a day of the month of at most 7, so its weekly default (`30 3 * * 0`) runs on the first Sunday of each month.
A job's cron is checked by the validator `env_valid` uses too (`backup.cron.cron_refusal`, Ruling T26-1 (a)
T24-4 M7), so a cron the configuration accepts is one the scheduler can schedule. `next_due` is the job that
fires first after `now`, the table's order breaking a tie.

**`run_scheduler`** runs one job at a time, through `run_job`, on the injected `clock` and `sleep`:

- each job holds the tick it is due at; the scheduler waits for the earliest (in waits of at most
  `MAX_WAIT_S`, so the wall clock is read again at least that often), then runs that job;
- a job that comes due while another runs waits and runs once the other has ended, once whatever number of
  its ticks passed meanwhile; its next tick is then the first after the time it started;
- except `ship`: a late tick of the ship is skipped when its next tick has already come (only the latest
  passed tick runs, so a long backup set is followed by one shipment, not a queue of them);
- each run is logged `backup_job` with the job, the outcome (`succeeded`, `failed` with the error's class and,
  for a backup job's own failure, its `<step>:<error class>`, or `skipped`) and the duration in seconds;
- a stop request (`stop`, set by SIGTERM) ends it between jobs: a running job finishes first, a wait ends at
  once.

**The start** (`fail_interrupted`): one backup container runs one job at a time, so a `backup_sets` or
`backup_runs` row still `running` when the scheduler starts was left by a container that died; nothing else
would ever end it (the health page would show it running for good). Each becomes `failed` with `error` =
`interrupted`, and the set's staging is cleared (restic's cache is kept). The backup service starts with the
stack, not after `migrate`, so `wait_for_database` tries that again every `DATABASE_RETRY_S` while the
database refuses it (not up, not migrated, not granted yet), for up to `DATABASE_WAIT_S`.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal, get_args

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.backup.backup_set import BackupJobFailed, clear_staging
from jobhunter.backup.cron import cron_refusal, next_tick
from jobhunter.config import AppConfig
from jobhunter.db.transitions import transition_where
from jobhunter.observability.logging import get_logger

JobName = Literal["set", "ship", "prune", "restore_test"]
JOB_NAMES: Final[tuple[JobName, ...]] = get_args(JobName)
MAX_WAIT_S: Final = 300.0
DATABASE_RETRY_S: Final = 5.0
DATABASE_WAIT_S: Final = 300.0
INTERRUPTED: Final = "interrupted"

log = get_logger("jobhunter.backup.scheduler")


@dataclass(frozen=True)
class BackupJob:
    name: JobName
    cron: str


@dataclass(frozen=True)
class Interrupted:
    """The rows a dead container left `running`, now `failed` (`interrupted`), by id."""

    sets: tuple[int, ...]
    runs: tuple[int, ...]


def backup_jobs(cfg: AppConfig, names: Sequence[JobName] = JOB_NAMES) -> tuple[BackupJob, ...]:
    """The jobs `names`, in that order, each with its cron expression from `cfg.backup`."""
    crons: dict[JobName, str] = {
        "set": cfg.backup.set_cron,
        "ship": cfg.backup.ship_cron,
        "prune": cfg.backup.prune_cron,
        "restore_test": cfg.backup.restore_test_cron,
    }
    return tuple(BackupJob(name, crons[name]) for name in names)


def next_fire(job: BackupJob, *, after: datetime) -> datetime:
    """`job`'s first tick strictly after `after` (UTC); for the restore test, the first on a day of the month
    of at most 7. `ValueError` for a cron the shared validator refuses (`backup.cron.cron_refusal`): not five
    fields croniter reads, or a restore-test cron that never fires in a month's first seven days."""
    first_week = job.name == "restore_test"
    if (refusal := cron_refusal(job.cron, first_week=first_week)) is not None:
        raise ValueError(f"the {job.name} cron {refusal}")
    return next_tick(job.cron, after=after, first_week=first_week)


def next_due(jobs: Sequence[BackupJob], *, now: datetime) -> tuple[BackupJob, datetime]:
    """The job of `jobs` that fires first after `now`, with that tick; on a tie, the first in `jobs`."""
    if not jobs:
        raise ValueError("no backup job to schedule")
    return min(((job, next_fire(job, after=now)) for job in jobs), key=lambda due: due[1])


def _failure_fields(exc: Exception) -> dict[str, str]:
    fields = {"error_class": type(exc).__name__}
    if isinstance(exc, BackupJobFailed):
        fields["error"] = exc.error
    return fields


async def _run_one(
    job: BackupJob, run_job: Callable[[BackupJob], Awaitable[None]], clock: Callable[[], datetime]
) -> None:
    """Run `job` once and log its outcome; a failure is the job's (logged), never the scheduler's."""
    started = clock()
    try:
        await run_job(job)
    except Exception as exc:  # noqa: BLE001 - a job's boundary: logged by class, the next job still runs
        duration = (clock() - started).total_seconds()
        log.error("backup_job", job=job.name, outcome="failed", duration_s=duration, **_failure_fields(exc))
        return
    log.info("backup_job", job=job.name, outcome="succeeded", duration_s=(clock() - started).total_seconds())


async def _ended(task: asyncio.Future[Any]) -> None:
    """`task` cancelled unless done, and awaited to its end without raising its cancellation (a cancellation
    of the caller still propagates)."""
    if not task.done():
        task.cancel()
        await asyncio.wait((task,))


async def _wait(
    sleep: Callable[[float], Awaitable[None]], stopped: asyncio.Future[Any], seconds: float
) -> None:
    """`sleep(seconds)`, cut short when `stopped` completes."""
    sleeper = asyncio.ensure_future(sleep(seconds))
    try:
        await asyncio.wait((sleeper, stopped), return_when=asyncio.FIRST_COMPLETED)
    finally:
        await _ended(sleeper)
    if not sleeper.cancelled():
        sleeper.result()  # a sleep that raised raises here


async def run_scheduler(
    cfg: AppConfig,
    *,
    clock: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]],
    run_job: Callable[[BackupJob], Awaitable[None]],
    stop: asyncio.Event,
    names: Sequence[JobName] = JOB_NAMES,
) -> None:
    """Run the jobs `names` on their crons, one at a time, until `stop` is set (module docstring). The
    `jobhunter backup scheduler` command names the jobs its table runs."""
    jobs = backup_jobs(cfg, names)
    if not jobs:
        raise ValueError("no backup job to schedule")
    started = clock()
    due = {job.name: next_fire(job, after=started) for job in jobs}
    log.info("backup_scheduler_started", jobs=list(names))
    stopped = asyncio.ensure_future(stop.wait())
    try:
        while not stop.is_set():
            # The earliest tick; on a tie, the table's order (min keeps the first).
            job = min(jobs, key=lambda candidate: due[candidate.name])
            at, now = due[job.name], clock()
            if at > now:
                await _wait(sleep, stopped, min((at - now).total_seconds(), MAX_WAIT_S))
                continue
            following = next_fire(job, after=at)
            if job.name == "ship" and following <= now:
                log.info("backup_job", job=job.name, outcome="skipped", duration_s=0.0)
                due[job.name] = following
                continue
            await _run_one(job, run_job, clock)
            due[job.name] = next_fire(job, after=max(at, now))
    finally:
        await _ended(stopped)
    log.info("backup_scheduler_stopped")


async def fail_interrupted(cfg: AppConfig, engine: AsyncEngine) -> Interrupted:
    """The start's cleanup (module docstring): every `running` set and run `failed` (`interrupted`), in one
    transaction as the backup role, then the set's staging cleared."""
    async with engine.begin() as conn:
        sets = await transition_where(
            conn, "backup_sets", ["running"], "failed", where="true", params={}, reason=INTERRUPTED
        )
        runs = await transition_where(
            conn, "backup_runs", ["running"], "failed", where="true", params={}, reason=INTERRUPTED
        )
    clear_staging(cfg)
    interrupted = Interrupted(
        sets=tuple(sorted(int(row["id"]) for row in sets)), runs=tuple(sorted(int(row["id"]) for row in runs))
    )
    if interrupted.sets or interrupted.runs:
        log.warning("backup_rows_interrupted", sets=list(interrupted.sets), runs=list(interrupted.runs))
    return interrupted


async def wait_for_database[T](
    attempt: Callable[[], Awaitable[T]],
    *,
    clock: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]],
    stop: asyncio.Event,
) -> T | None:
    """`attempt()`, tried again every `DATABASE_RETRY_S` while the database refuses it (a `DBAPIError`), for
    up to `DATABASE_WAIT_S`, after which its error is raised; `None` when `stop` is set meanwhile."""
    deadline = clock() + timedelta(seconds=DATABASE_WAIT_S)
    while True:
        try:
            return await attempt()
        except DBAPIError as exc:
            if clock() >= deadline:
                raise
            log.warning("backup_scheduler_waiting_for_database", error_class=type(exc).__name__)
        if stop.is_set():
            return None
        stopped = asyncio.ensure_future(stop.wait())
        try:
            await _wait(sleep, stopped, DATABASE_RETRY_S)
        finally:
            await _ended(stopped)
        if stop.is_set():
            return None
