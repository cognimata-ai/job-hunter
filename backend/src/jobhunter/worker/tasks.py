"""The procrastinate app, the task blueprint and deferring inside the caller's transaction (spec §3.3.2).

`blueprint` declares the five tasks. `make_queue_app` builds the process's *task app* (the blueprint under the
namespace `jh`, so a task is named `jh:<name>`) on a psycopg pool; `defer_in_tx` inserts a job through the
caller's SQLAlchemy connection, in a savepoint, so the job commits or rolls back with the caller's own writes
(spike V1). Correctness never rests on it: every mutation that needs follow-up work also leaves a durable
`operations` or `runs` row that the reconcilers re-defer from (§3.3.2).

Task bodies are stubs here (they log `task_stub`); Tasks 10 to 13 fill them in. Every body is `pass_context`
and reads its inputs from the `WorkerRuntime` in `context.additional_context["runtime"]`.

`release_stalled_jobs` is called from the `stalled_jobs` reconciler only (spike V2); no task body calls it.
"""

from collections.abc import Mapping
from typing import Final, Literal, Protocol

import procrastinate
from procrastinate.exceptions import AlreadyEnqueued
from procrastinate.job_context import JobContext
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


async def release_stalled_jobs(app: procrastinate.App, *, seconds_since_heartbeat: float = 60.0) -> int:
    """Retry every job in `doing` whose worker has had no heartbeat for `seconds_since_heartbeat`; the count.

    Every M0 task is idempotent, so each stalled job is retried and none is failed (spike V2). `app` must
    be open.
    """
    manager = app.job_manager
    stalled = list(await manager.get_stalled_jobs(seconds_since_heartbeat=seconds_since_heartbeat))
    for job in stalled:
        await manager.retry_job(job)
        log.warning("stalled_job_released", job_id=job.id, task=job.task_name)
    return len(stalled)


async def delete_finished_jobs(app: procrastinate.App, *, older_than_hours: int = 168) -> None:
    """Delete the jobs that ended (succeeded, failed, cancelled, aborted) more than `older_than_hours` ago."""
    await app.job_manager.delete_old_jobs(
        nb_hours=older_than_hours, include_failed=True, include_cancelled=True, include_aborted=True
    )
