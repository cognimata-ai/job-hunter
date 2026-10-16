"""The procrastinate app, the task blueprint and deferring inside the caller's transaction (spec §3.3.2).

`blueprint` declares the five tasks. `make_queue_app` builds the process's *task app* (the blueprint under the
namespace `jh`, so a task is named `jh:<name>`) on a psycopg pool; `defer_in_tx` inserts a job through the
caller's SQLAlchemy connection, in a savepoint, so the job commits or rolls back with the caller's own writes
(spike V1). Correctness never rests on it: every mutation that needs follow-up work also leaves a durable
`operations` or `runs` row that the reconcilers re-defer from (§3.3.2).

Task bodies are stubs here (they log `task_stub`); Tasks 10 to 13 fill them in. Every body is `pass_context`
and reads its inputs from the `WorkerRuntime` in `context.additional_context["runtime"]`.

`release_stalled_jobs` is called from the `stalled_jobs` reconciler only (spike V2); no task body calls it.

A process has one task app, and the first call of `make_queue_app` wins: it fixes the app's DSN and its pool's
`max_size`, and a later call with the same DSN returns that app whatever `max_size` it passes. (Adding the
blueprint to an app namespaces its tasks, so it can be added once.) `_reset_task_app` undoes that for tests.
"""

from collections.abc import Mapping
from typing import Final, Literal, Protocol

import procrastinate
from procrastinate.exceptions import AlreadyEnqueued, ConnectorException, NoResult, UniqueViolation
from procrastinate.job_context import JobContext
from procrastinate.jobs import Job, Status
from procrastinate.manager import QUEUEING_LOCK_CONSTRAINT, JobManager
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.observability.logging import get_logger
from jobhunter.types import JSONValue

TASK_NAMESPACE: Final = "jh"
TaskName = Literal["run_collect", "run_operation", "tick", "reconcile_heavy", "retention_daily"]

# Queue name to the concurrency of the worker that serves it (spec §3.3.2).
QUEUES: Final[Mapping[str, int]] = {"collect": 1, "score": 1, "aux": 2, "tick": 1}

log = get_logger("jobhunter.worker.tasks")

blueprint = procrastinate.Blueprint()


def job_task_name(name: TaskName) -> str:
    """The name under which the task app registers `name` (and `procrastinate_jobs.task_name` stores it)."""
    return f"{TASK_NAMESPACE}:{name}"


def _stub(name: TaskName, **fields: JSONValue) -> None:
    log.info("task_stub", task=name, **fields)


@blueprint.task(name="run_collect", queue="collect", lock="collect", pass_context=True)
async def run_collect(context: JobContext, run_id: int, attempt_no: int) -> None:
    _stub("run_collect", run_id=run_id, attempt_no=attempt_no)


@blueprint.task(name="run_operation", queue="aux", pass_context=True)
async def run_operation(context: JobContext, operation_id: int) -> None:
    _stub("run_operation", operation_id=operation_id)


@blueprint.periodic(cron="* * * * *")
@blueprint.task(name="tick", queue="tick", queueing_lock="tick", pass_context=True)
async def tick(context: JobContext, timestamp: int) -> None:
    _stub("tick", timestamp=timestamp)


@blueprint.periodic(cron="*/10 * * * *")
@blueprint.task(name="reconcile_heavy", queue="tick", pass_context=True)
async def reconcile_heavy(context: JobContext, timestamp: int) -> None:
    _stub("reconcile_heavy", timestamp=timestamp)


@blueprint.periodic(cron="17 3 * * *")
@blueprint.task(name="retention_daily", queue="tick", pass_context=True)
async def retention_daily(context: JobContext, timestamp: int) -> None:
    _stub("retention_daily", timestamp=timestamp)


# The blueprint can be added to one app only (adding it namespaces its tasks), so a process has one task app.
_task_app: procrastinate.App | None = None
_task_dsn: str | None = None


def _new_app(dsn: str, max_size: int) -> procrastinate.App:
    return procrastinate.App(
        connector=procrastinate.PsycopgConnector(conninfo=dsn, min_size=1, max_size=max_size)
    )


def make_queue_app(dsn: str, *, max_size: int = 4) -> procrastinate.App:
    """The task app of this process for `dsn`; the first call creates it, later calls with `dsn` return it.

    Another `dsn` gives a separate app without tasks, which serves job-manager queries (stalled jobs,
    deleting old jobs) against that database. The app is not open: the caller opens it with `open_async()`
    (`defer_in_tx` needs no open app, as it uses the caller's connection).
    """
    global _task_app, _task_dsn
    if _task_app is None:
        app = _new_app(dsn, max_size)
        app.add_tasks_from(blueprint, namespace=TASK_NAMESPACE)
        _task_app, _task_dsn = app, dsn
        return app
    if dsn == _task_dsn:
        return _task_app
    return _new_app(dsn, max_size)


def _reset_task_app() -> None:
    """Forget the task app and give the blueprint back its plain task names. Test-only: nothing uses it yet.

    For an in-process test that needs a task app of its own (another DSN or pool size) after one was made.
    The app that was returned earlier keeps working on its own connector, but its tasks are no longer
    registered with the blueprint.
    """
    global _task_app, _task_dsn
    prefix = f"{TASK_NAMESPACE}:"
    for task in set(blueprint.tasks.values()):
        task.name = task.name.removeprefix(prefix)
        task.blueprint = blueprint
    blueprint.tasks = {name.removeprefix(prefix): task for name, task in blueprint.tasks.items()}
    _task_app = _task_dsn = None


def task_app() -> procrastinate.App:
    """The task app that `make_queue_app` created in this process."""
    if _task_app is None:
        raise RuntimeError("make_queue_app() has not been called in this process")
    return _task_app


class Deferrer(Protocol):
    """Defers a task inside the caller's transaction; `None` means a job with that queueing lock waits."""

    async def __call__(
        self,
        conn: AsyncConnection,
        task: TaskName,
        *,
        lock: str | None = None,
        queueing_lock: str | None = None,
        **kwargs: JSONValue,
    ) -> int | None: ...


async def defer_in_tx(
    conn: AsyncConnection,
    task: TaskName,
    *,
    lock: str | None = None,
    queueing_lock: str | None = None,
    **kwargs: JSONValue,
) -> int | None:
    """Insert the job of `task` on `conn`, so that it commits or rolls back with the caller's transaction.

    Returns the job id, or `None` when a job with `queueing_lock` is already waiting (`AlreadyEnqueued`).
    The insert runs in a savepoint: when the queueing lock is taken, only the savepoint is rolled back and the
    caller's transaction stays usable. Any other error leaves the savepoint and propagates.

    `AlreadyEnqueued` is procrastinate's own exception, raised by its connector on the driver connection, so
    it is no SQLAlchemy-wrapped driver error and is caught here, outside the savepoint block.
    """
    raw = (await conn.get_raw_connection()).driver_connection
    deferrer = (
        task_app()
        .tasks[job_task_name(task)]
        .configure(connection=raw, lock=lock, queueing_lock=queueing_lock)
    )
    try:
        async with conn.begin_nested():
            return await deferrer.defer_async(**kwargs)
    except AlreadyEnqueued:
        return None


_LIVE_JOB = q(
    "SELECT EXISTS (SELECT 1 FROM procrastinate_jobs "
    "WHERE task_name = :t AND status IN ('todo', 'doing') AND args @> :m)",
    jsonb=["m"],
)


async def live_job_exists(
    conn: AsyncConnection, task: TaskName, *, args_match: Mapping[str, JSONValue]
) -> bool:
    """Whether a job of `task` is waiting or running whose arguments contain `args_match`."""
    result = await conn.execute(_LIVE_JOB, {"t": job_task_name(task), "m": dict(args_match)})
    return bool(result.scalar_one())


# The statuses that `procrastinate_retry_job_v2` accepts (it raises for any other) and that
# `procrastinate_finish_job_v1` accepts.
_RETRYABLE: Final = (Status.DOING, Status.FAILED)
_FINISHABLE: Final = (Status.TODO, Status.DOING)

type _Outcome = Literal["released", "superseded", "not_retryable"]

_OUTCOME_EVENTS: Final[Mapping[_Outcome, str]] = {
    "released": "stalled_job_released",
    "superseded": "stalled_job_superseded",
    "not_retryable": "stalled_job_not_retryable",
}


async def _has_left(manager: JobManager, job_id: int, statuses: tuple[Status, ...]) -> bool:
    """Whether the job is gone or is in none of `statuses`, i.e. a failed state change was a lost race."""
    try:
        return await manager.get_job_status_async(job_id) not in statuses
    except NoResult:
        return True


async def _release_one(manager: JobManager, job: Job) -> _Outcome:
    """Retry `job`, or fail it when its queueing lock is taken, or leave it when it is no longer stalled.

    Only these two failures of the retry are expected; any other error propagates. A retry raises
    `UniqueViolation` (on the queueing-lock index) when a waiting job holds the job's queueing lock, and
    `ConnectorException` (an SQL `RAISE`) when the job is not in `doing` or `failed` any more.
    """
    if job.id is None:
        raise TypeError("a stalled job from the manager has no id")
    try:
        await manager.retry_job(job)
    except UniqueViolation as exc:
        if exc.constraint_name != QUEUEING_LOCK_CONSTRAINT:
            raise
    except ConnectorException:
        if not await _has_left(manager, job.id, _RETRYABLE):
            raise
        return "not_retryable"
    else:
        return "released"
    # The waiting job with the same queueing lock supersedes this one: it is what the lock exists to allow.
    try:
        await manager.finish_job(job, Status.FAILED, delete_job=False)
    except ConnectorException:
        if not await _has_left(manager, job.id, _FINISHABLE):
            raise
        return "not_retryable"
    return "superseded"


async def release_stalled_jobs(app: procrastinate.App, *, seconds_since_heartbeat: float = 60.0) -> int:
    """Retry every job in `doing` whose worker has had no heartbeat for `seconds_since_heartbeat`; the count.

    Every M0 task is idempotent, so a stalled job is retried, not failed (spike V2). Each job is handled on
    its own, so one job cannot keep the others from being released:
    - a job whose queueing lock a waiting job holds is superseded by it, so it is marked failed (logged
      `stalled_job_superseded`) and not counted;
    - a job that is no longer retryable, because it ended after it was listed, is skipped (logged
      `stalled_job_not_retryable`) and not counted;
    - any other error propagates, and the `stalled_jobs` reconciler records it as `reconciler.failed`.

    `app` must be open.
    """
    manager = app.job_manager
    stalled = list(await manager.get_stalled_jobs(seconds_since_heartbeat=seconds_since_heartbeat))
    released = 0
    for job in stalled:
        outcome = await _release_one(manager, job)
        log.warning(_OUTCOME_EVENTS[outcome], job_id=job.id, task_name=job.task_name)
        released += outcome == "released"
    return released


async def delete_finished_jobs(app: procrastinate.App, *, older_than_hours: int = 168) -> None:
    """Delete the jobs that ended (succeeded, failed, cancelled, aborted) more than `older_than_hours` ago."""
    await app.job_manager.delete_old_jobs(
        nb_hours=older_than_hours, include_failed=True, include_cancelled=True, include_aborted=True
    )
