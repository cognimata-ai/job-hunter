"""The procrastinate app, the task blueprint and deferring inside the caller's transaction (spec §3.3.2).

`blueprint` declares the nine tasks. `make_queue_app` builds the process's *task app* (the blueprint under
the namespace `jh`, so a task is named `jh:<name>`) on a psycopg pool; `defer_in_tx` inserts a job through the
caller's SQLAlchemy connection, in a savepoint, so the job commits or rolls back with the caller's own writes
(spike V1). Correctness never rests on it: every mutation that needs follow-up work also leaves a durable
`operations` or `runs` row that the reconcilers re-defer from (§3.3.2).

The bodies: `run_operation` (Task 10), `tick` (Task 11), `run_collect` (Task 12), `reconcile_heavy` and
`retention_daily` (Task 13; it runs the `daily` reconcilers after the retention pass, Task 19, unless the
worker epoch fenced that pass out, final review C2; a pass that raises does not keep them from running, ledger
L312), M3 Task 17's periodic `batch_poll` (every 5 min) and `batch_reconcile_ambiguous` (every 15 min) on
`aux`, each with the queueing lock of its name (`scoring.batch_results`, `scoring.batch_adoption`: Task 27
registers and pins them), M3 Task 20's periodic `canary_daily` (`41 6 * * *`) on the `tick` queue with the
queueing lock of its name (`eval_calibration.start_canary_run`; Task 27 registers and pins it, Ruling P33),
and M3 Task 11's `score_cycle(trigger)` on the `score` queue
(`graphs.score.graph.execute_cycle`: one cycle, created by its first transaction, never resumed). The tick
runs the score lane's trigger (`tick.tick_score`, §3.4 step 6) after its scheduling step, in a transaction of
its own. Every body is `pass_context` and reads its inputs from the `WorkerRuntime` in
`context.additional_context["runtime"]`. The tick's own scheduling step (`run_tick`) is fenced by the worker
epoch too (Ruling FR-h): a superseded worker's tick writes nothing. Whatever the worker's own fence refuses
(`FencedOut.worker_superseded`), a delivery, the tick's step, a cadence or the retention pass, the body then
requests this worker's graceful stop (`_stop_when_superseded`, Ruling T24-a): the refusal proves it
superseded. A fence of any other owner is a failure of that body, never a reason to stop.

`tick`, `reconcile_heavy` and `retention_daily` share the `tick` queue, served with concurrency 1
(`QUEUES`): a long heavy cadence or daily pass delays the minute's tick until it ends (ledger L312). A slot
that comes due meanwhile is still recorded once and queued late (`run_tick` evaluates every slot since its
cursor; its queueing lock keeps one tick waiting), as a `catch_up` run after 5 min; only a delay longer than
the schedule's interval would make a slot `missed`. The daily pass is batched (Task 19's `stale_sweep_daily`).

`release_stalled_jobs` is called from the `stalled_jobs` reconciler only (spike V2); no task body calls it.

A process has one task app, and the first call of `make_queue_app` wins: it fixes the app's DSN and its pool's
`max_size`, and a later call with the same DSN returns that app whatever `max_size` it passes. (Adding the
blueprint to an app namespaces its tasks, so it can be added once.) `_reset_task_app` undoes that for tests.
"""

from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

import procrastinate
import psycopg
from procrastinate.exceptions import AlreadyEnqueued, ConnectorException, NoResult, UniqueViolation
from procrastinate.job_context import JobContext
from procrastinate.jobs import Job, Status
from procrastinate.manager import QUEUEING_LOCK_CONSTRAINT, JobManager
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.clock import utcnow
from jobhunter.db.sql import q
from jobhunter.errors import FencedOut
from jobhunter.observability.logging import get_logger
from jobhunter.types import JSONValue
from jobhunter.worker.epoch import fence_epoch

if TYPE_CHECKING:
    # Only for annotations: the runtime pulls in langgraph, which a process that only defers (the API) skips;
    # `reconcilers` imports this module.
    from jobhunter.worker.reconcilers import ReconcileContext
    from jobhunter.worker.runtime import WorkerRuntime

TASK_NAMESPACE: Final = "jh"
TaskName = Literal[
    "run_collect",
    "run_operation",
    "tick",
    "reconcile_heavy",
    "retention_daily",
    "score_cycle",
    "batch_poll",
    "batch_reconcile_ambiguous",
    "canary_daily",
]
# The tasks whose stalled job is failed outright, never retried (`_release_one`, Codex r1 #17; M2
# Carry-forward I4): a retried `score_cycle` would find its cycle still `running` and start nothing until the
# takeover sweep or `orphaned_cycles` failed it; failing the job leaves that to them, and the next trigger
# starts a new cycle.
NO_STALL_RETRY: Final[frozenset[TaskName]] = frozenset({"score_cycle"})

# Queue name to the concurrency of the worker that serves it (spec §3.3.2).
QUEUES: Final[Mapping[str, int]] = {"collect": 1, "score": 1, "aux": 2, "tick": 1}

log = get_logger("jobhunter.worker.tasks")

blueprint = procrastinate.Blueprint()


def job_task_name(name: TaskName) -> str:
    """The name under which the task app registers `name` (and `procrastinate_jobs.task_name` stores it)."""
    return f"{TASK_NAMESPACE}:{name}"


def _stop_when_superseded(rt: "WorkerRuntime") -> Callable[[], None]:
    """What a body does once the epoch fence refused it (ruling T24-a): this worker is superseded, so it
    requests its own graceful stop (`runtime.request_shutdown`, reason `epoch_superseded`)."""
    # Imported here: the runtime pulls in langgraph, which a process that only defers (the API) skips.
    from jobhunter.worker.runtime import EPOCH_SUPERSEDED, request_shutdown

    return lambda: request_shutdown(rt.shutdown, reason=EPOCH_SUPERSEDED)


def _reconcile_context(rt: "WorkerRuntime") -> "ReconcileContext":
    """The context of a cadence a task body runs: the runtime's engine, task app, epoch and embedder, this
    module's deferrer, `now`, and the stop of a superseded worker (`_stop_when_superseded`)."""
    # Imported here: `reconcilers` imports this module (`Deferrer`, `release_stalled_jobs`).
    from jobhunter.worker.reconcilers import ReconcileContext

    return ReconcileContext(
        engine=rt.engine,
        queue=rt.queue,
        defer=defer_in_tx,
        now=utcnow(),
        epoch=rt.epoch,
        embedder=rt.embedder,
        on_superseded=_stop_when_superseded(rt),
    )


@blueprint.task(name="run_collect", queue="collect", lock="collect", pass_context=True)
async def run_collect(context: JobContext, run_id: int, attempt_no: int) -> None:
    # Imported here: `runs` imports this module (`Deferrer`, `defer_in_tx`, `job_task_name`). The worker
    # imports `runs` itself before takeover, so its sweep and reconcilers are registered (Task 13).
    from jobhunter.worker.runs import execute_run

    rt: WorkerRuntime = context.additional_context["runtime"]
    await execute_run(
        rt.engine,
        rt.collect_graph,
        run_id,
        attempt_no,
        epoch=rt.epoch,
        on_superseded=_stop_when_superseded(rt),
    )


@blueprint.task(name="run_operation", queue="aux", pass_context=True)
async def run_operation(context: JobContext, operation_id: int) -> None:
    # Imported here: `operations` imports this module (`Deferrer`, `live_job_exists`). The worker imports
    # `operations` itself before takeover, so its sweep, reconciler and handlers are registered (Task 13).
    from jobhunter.worker.operations import execute_operation

    rt: WorkerRuntime = context.additional_context["runtime"]
    await execute_operation(
        rt.engine,
        operation_id,
        epoch=rt.epoch,
        deps=rt.operation_deps,
        on_superseded=_stop_when_superseded(rt),
    )


@blueprint.periodic(cron="* * * * *")
@blueprint.task(name="tick", queue="tick", queueing_lock="tick", pass_context=True)
async def tick(context: JobContext, timestamp: int) -> None:
    # Imported here: `reconcilers` and `tick` import this module (`Deferrer`, `release_stalled_jobs`).
    from jobhunter.worker.reconcilers import run_reconcilers
    from jobhunter.worker.tick import observe_tick, run_tick, tick_score

    rt: WorkerRuntime = context.additional_context["runtime"]
    # The `stalled_jobs` reconciler runs in this cadence, so the tick does not release stalled jobs itself.
    await run_reconcilers(_reconcile_context(rt), "tick")
    try:
        async with rt.engine.begin() as conn:
            # The `worker_heartbeat` row first, before `active_config` (Ruling FR-h; no job row is locked).
            await fence_epoch(conn, rt.epoch)
            report = await run_tick(conn, now=utcnow(), defer=defer_in_tx)
    except FencedOut as fenced:
        if not fenced.worker_superseded:
            raise  # not this worker's own fence: a failure of the tick, never a reason to stop (F5 Minor 1)
        # A newer worker owns the schedule (`fence_epoch` logged it): this tick wrote nothing, and this
        # superseded worker stops (Ruling T24-a).
        _stop_when_superseded(rt)()
        return
    observe_tick(report)  # after the commit: a rolled-back tick inserted nothing
    try:
        # §3.4 step 6, in a transaction of its own: the score lane's periodic trigger (fenced by the epoch).
        await tick_score(rt.engine, now=utcnow(), epoch=rt.epoch, defer=defer_in_tx, fake_llm=rt.cfg.fake_llm)
    except FencedOut as fenced:
        if not fenced.worker_superseded:
            raise
        _stop_when_superseded(rt)()


@blueprint.task(name="score_cycle", queue="score", lock="score", pass_context=True)
async def score_cycle(context: JobContext, trigger: str) -> None:
    """One score cycle of `trigger` (§9.3): `execute_cycle` creates it in its first transaction and runs its
    graph, owned and beaten; a delivery never resumes a cycle (a retried one creates a fresh cycle or starts
    nothing). The worker's own fence refusing it stops this superseded worker (Ruling T24-a)."""
    # Imported here: the graph pulls in langgraph, which a process that only defers (the API) skips; and
    # `cycles` imports this module (`Deferrer`).
    from jobhunter.graphs.score.graph import execute_cycle
    from jobhunter.scoring.cycles import cycle_trigger

    rt: WorkerRuntime = context.additional_context["runtime"]
    shutdown = rt.shutdown
    try:
        await execute_cycle(
            rt.score_graphs,
            rt.score_deps,
            trigger=cycle_trigger(trigger),
            epoch=rt.epoch,
            fake_llm=rt.cfg.fake_llm,
            admission_stopped=lambda: shutdown.admission_stopped,
        )
    except FencedOut as fenced:
        if not fenced.worker_superseded:
            raise  # not this worker's own fence: a failure of the job
        _stop_when_superseded(rt)()


# M3 Task 17 (A2 B16, Deviation 27): the batch branch's periodic tasks on `aux`, one waiting job each.
@blueprint.periodic(cron="*/5 * * * *")
@blueprint.task(name="batch_poll", queue="aux", queueing_lock="batch_poll", pass_context=True)
async def batch_poll(context: JobContext, timestamp: int) -> None:
    """The submitted and ended batch submissions polled, their results processed (`batch_results.poll`)."""
    # Imported here: the batch modules import this module (`Deferrer`, `defer_in_tx`).
    from jobhunter.scoring.batch_results import poll_task

    rt: WorkerRuntime = context.additional_context["runtime"]
    try:
        await poll_task(rt.engine, rt.cfg, rt.gateway, epoch=rt.epoch)
    except FencedOut as fenced:
        if not fenced.worker_superseded:
            raise  # not this worker's own fence: a failure of the job
        _stop_when_superseded(rt)()


@blueprint.periodic(cron="*/15 * * * *")
@blueprint.task(
    name="batch_reconcile_ambiguous",
    queue="aux",
    queueing_lock="batch_reconcile_ambiguous",
    pass_context=True,
)
async def batch_reconcile_ambiguous(context: JobContext, timestamp: int) -> None:
    """The ambiguous and the released submissions matched against the listing
    (`batch_adoption.reconcile_ambiguous`)."""
    from jobhunter.scoring.batch_adoption import reconcile_ambiguous_task

    rt: WorkerRuntime = context.additional_context["runtime"]
    try:
        await reconcile_ambiguous_task(rt.engine, rt.cfg, rt.gateway, epoch=rt.epoch)
    except FencedOut as fenced:
        if not fenced.worker_superseded:
            raise  # not this worker's own fence: a failure of the job
        _stop_when_superseded(rt)()


# On the `tick` queue, as the tick and `retention_daily` (module docstring): it holds the tick back while it
# runs.
@blueprint.periodic(cron="*/10 * * * *")
@blueprint.task(name="reconcile_heavy", queue="tick", pass_context=True)
async def reconcile_heavy(context: JobContext, timestamp: int) -> None:
    # Imported here: `reconcilers` imports this module (`Deferrer`, `release_stalled_jobs`).
    from jobhunter.worker.reconcilers import run_reconcilers

    rt: WorkerRuntime = context.additional_context["runtime"]
    await run_reconcilers(_reconcile_context(rt), "heavy")


# On the `tick` queue too (module docstring): the daily pass holds the tick back while it runs.
@blueprint.periodic(cron="17 3 * * *")
@blueprint.task(name="retention_daily", queue="tick", pass_context=True)
async def retention_daily(context: JobContext, timestamp: int) -> None:
    # Imported here: `privacy.retention` and `reconcilers` import this module (`delete_finished_jobs`,
    # `Deferrer`, `release_stalled_jobs`).
    from jobhunter.privacy.retention import run_retention_daily
    from jobhunter.worker.reconcilers import run_reconcilers

    rt: WorkerRuntime = context.additional_context["runtime"]
    failure: Exception | None = None
    try:
        await run_retention_daily(
            rt.engine, rt.queue, rt.saver, cv_store=rt.cv_store, now=utcnow(), epoch=rt.epoch
        )
    except FencedOut as fenced:
        if not fenced.worker_superseded:  # not this worker's own fence: a failure of the pass (F5 Minor 1)
            log.error("retention_daily_failed", error_class=type(fenced).__name__, owner=fenced.owner)
            failure = fenced
        else:
            # A newer worker owns the work (final review C2; `retention_fenced` logged it): the fenced pass
            # wrote nothing, no daily reconciler runs for this superseded worker either, and it stops (Ruling
            # T24-a).
            _stop_when_superseded(rt)()
            return
    except Exception as exc:
        # The pass isolates its rules itself; whatever still escapes it must not cost the daily reconcilers
        # their run (ledger L312). The class only, never the message; the job still fails after them.
        log.error("retention_daily_failed", error_class=type(exc).__name__)
        failure = exc
    # The daily maintenance (§17.4, e.g. `stale_sweep_daily`, §8.11), after the retention pass.
    await run_reconcilers(_reconcile_context(rt), "daily")
    if failure is not None:
        raise failure


# M3 Task 20 (L285, L4632; Ruling P33): the daily canary run on the `tick` queue, one waiting job.
@blueprint.periodic(cron="41 6 * * *")
@blueprint.task(name="canary_daily", queue="tick", queueing_lock="canary_daily", pass_context=True)
async def canary_daily(context: JobContext, timestamp: int) -> None:
    """A canary run of the active configuration and its `eval_run` operation, in one transaction
    (`eval_calibration.start_canary_run`: nothing without a tuple or an active canary), on the worker's
    transport; the `eval_run` handler on `aux` scores it."""
    # Imported here: the eval modules import this module (`Deferrer`) and pull in the score steps.
    from jobhunter.scoring.eval_calibration import start_canary_run

    rt: WorkerRuntime = context.additional_context["runtime"]
    async with rt.engine.begin() as conn:
        await start_canary_run(
            conn, transport="fake" if rt.cfg.fake_llm else "live", now=utcnow(), defer=defer_in_tx
        )


# The blueprint can be added to one app only (adding it namespaces its tasks), so a process has one task app.
_task_app: procrastinate.App | None = None
_task_dsn: str | None = None


def _autocommit(conn: psycopg.Connection[Any] | psycopg.AsyncConnection[Any]) -> Awaitable[None] | None:
    """The `configure` of the task app's pools: autocommit, for an async and for a sync connection alike.

    The async pool and procrastinate's listener await what it returns (the coroutine of
    `AsyncConnection.set_autocommit`). The sync connector that procrastinate derives from the same arguments,
    for an app used synchronously before it is opened, calls it and ignores the result: a sync connection
    sets autocommit at once and returns `None`.
    """
    return conn.set_autocommit(True)


def _new_app(dsn: str, max_size: int) -> procrastinate.App:
    # Every query procrastinate runs on its pool is one statement (one SQL function call), so its pool
    # connections are autocommit: the server commits each statement as it ends. Otherwise the COMMIT is a
    # round trip of its own, and a process frozen in between (SIGSTOP, a paused VM) would keep the job or
    # worker row locked until the server's idle-in-transaction timeout (60 s); the replacement worker's
    # stalled-job release and worker pruning would wait for it (C17). `configure` also runs on procrastinate's
    # own listener connection, which is autocommit already. (`autocommit=True` as a pool argument would clash
    # with that connection's own.) `_autocommit` works for procrastinate's derived sync connector too.
    return procrastinate.App(
        connector=procrastinate.PsycopgConnector(
            conninfo=dsn, min_size=1, max_size=max_size, configure=_autocommit
        )
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


async def defer_run_attempt(
    conn: AsyncConnection, defer: Deferrer, run_id: int, attempt_no: int
) -> int | None:
    """Defer `run_collect(run_id, attempt_no)` with `defer` on `conn`, in the caller's transaction.

    Under the `collect` lock and the queueing lock `run:<run_id>:<attempt_no>`, so each attempt has at most
    one waiting job. Returns the job id, or `None` when that attempt's job is already waiting. The one
    deferral of a run attempt (ruling P16): the tick, the manual run, new attempts and the `queued_runs`
    reconciler all use it.
    """
    return await defer(
        conn,
        "run_collect",
        lock="collect",
        queueing_lock=f"run:{run_id}:{attempt_no}",
        run_id=run_id,
        attempt_no=attempt_no,
    )


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

type _Outcome = Literal["released", "superseded", "not_retryable", "failed"]

_OUTCOME_EVENTS: Final[Mapping[_Outcome, str]] = {
    "released": "stalled_job_released",
    "superseded": "stalled_job_superseded",
    "not_retryable": "stalled_job_not_retryable",
    "failed": "stalled_job_failed",
}
_NO_RETRY_NAMES: Final = frozenset(job_task_name(name) for name in NO_STALL_RETRY)


async def _has_left(manager: JobManager, job_id: int, statuses: tuple[Status, ...]) -> bool:
    """Whether the job is gone or is in none of `statuses`, i.e. a failed state change was a lost race."""
    try:
        return await manager.get_job_status_async(job_id) not in statuses
    except NoResult:
        return True


async def _fail_job(manager: JobManager, job: Job, job_id: int, outcome: _Outcome) -> _Outcome:
    """Mark `job` failed (kept); `not_retryable` when it ended meanwhile, any other error propagates."""
    try:
        await manager.finish_job(job, Status.FAILED, delete_job=False)
    except ConnectorException:
        if not await _has_left(manager, job_id, _FINISHABLE):
            raise
        return "not_retryable"
    return outcome


async def _release_one(manager: JobManager, job: Job) -> _Outcome:
    """Retry `job`, or fail it when its queueing lock is taken or its task is one of `NO_STALL_RETRY`, or
    leave it when it is no longer stalled.

    Only these two failures of the retry are expected; any other error propagates. A retry raises
    `UniqueViolation` (on the queueing-lock index) when a waiting job holds the job's queueing lock, and
    `ConnectorException` (an SQL `RAISE`) when the job is not in `doing` or `failed` any more.
    """
    if job.id is None:
        raise TypeError("a stalled job from the manager has no id")
    if job.task_name in _NO_RETRY_NAMES:  # Codex r1 #17: failed outright, its owner row is the sweeps'
        return await _fail_job(manager, job, job.id, "failed")
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
    return await _fail_job(manager, job, job.id, "superseded")


async def release_stalled_jobs(app: procrastinate.App, *, seconds_since_heartbeat: float = 60.0) -> int:
    """Retry every job in `doing` whose worker has had no heartbeat for `seconds_since_heartbeat`; the count.

    Every M0 task is idempotent, so a stalled job is retried, not failed (spike V2). Each job is handled on
    its own, so one job cannot keep the others from being released:
    - a job whose queueing lock a waiting job holds is superseded by it, so it is marked failed (logged
      `stalled_job_superseded`) and not counted;
    - a job of a task in `NO_STALL_RETRY` (`score_cycle`) is marked failed (logged `stalled_job_failed`) and
      not counted: its cycle is failed `orphaned` by the takeover sweep or `orphaned_cycles` (M3 Task 11);
    - a job that is no longer retryable, because it ended after it was listed, is skipped (logged
      `stalled_job_not_retryable`) and not counted;
    - any other error propagates, and the `stalled_jobs` reconciler records it as `reconciler.failed`.

    Which tasks can be superseded (ruling T9-d): only one whose queueing lock a new job can take while the
    stalled job is still `doing`. In M0 that was `tick` alone: the periodic deferrer queues the next minute's
    `tick` (queueing lock `tick`) while a dead worker's `tick` is stalled, and the new one does the same work.
    Since M1 (Task 24, M0 carry-forward 6) `run_collect` and `run_operation` can be too: a delivery whose
    epoch the fence refused re-defers its own attempt (`run:<id>:<n>`) or operation (`op:<id>`) while its
    job is still `doing`, and when that superseded worker dies before the job ends (its watchdog's exit 70),
    the stalled job is superseded here by the waiting one. That is safe for the same reason as `tick`'s: the
    waiting job is the same attempt or operation, so it does the same work, and the stalled one did nothing
    (a refused delivery writes nothing but the re-defer). The reconcilers still re-defer only work without a
    live job (`queued_runs`, `operations`). Neither `reconcile_heavy` nor `retention_daily` can be (no
    queueing lock). A task that a later milestone gives a queueing lock inherits this: if anything can defer
    a job with that lock while the stalled one is `doing`, the stalled job ends `failed` here and never runs
    again, so either its successor must do the same work (as these do) or its re-deferral must skip work
    that has a live job.

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


# procrastinate's own `delete_old_jobs` query (procrastinate 3.10 `sql/queries.sql`: a job's age is that of
# its latest event, on the database clock), bounded to `limit` jobs so that retention deletes in batches (M1
# Carry-forward 17); the ids it deleted.
_DELETE_FINISHED: Final = """\
DELETE FROM procrastinate_jobs
WHERE id IN (
    SELECT job.id FROM (
        SELECT DISTINCT ON (job.id) job.id, job.status, event.at AS latest_at
            FROM procrastinate_jobs job
            JOIN procrastinate_events event ON job.id = event.job_id
            ORDER BY job.id, event.at DESC
    ) AS job
    WHERE job.status IN ('succeeded', 'failed', 'cancelled', 'aborted')
      AND latest_at < NOW() - (%(nb_hours)s || 'HOUR')::INTERVAL
    ORDER BY job.id
    LIMIT %(limit)s
)
RETURNING id"""


async def delete_finished_jobs(app: procrastinate.App, *, older_than_hours: int = 168, limit: int) -> int:
    """Delete at most `limit` jobs that ended (succeeded, failed, cancelled, aborted) more than
    `older_than_hours` ago, on procrastinate's own pool; how many it deleted."""
    deleted = await app.connector.execute_query_all_async(
        query=_DELETE_FINISHED, nb_hours=older_than_hours, limit=limit
    )
    return len(deleted)
