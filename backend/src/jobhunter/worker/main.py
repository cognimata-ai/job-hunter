"""The worker process, `jobhunter worker` (spec §3.3.1, §3.3.5).

`worker_main` starts the process in this order:
1. logging at `cfg.log_level`;
2. the lock session and the singleton lock (`acquire_lock`: standby while a healthy worker holds it), in a
   thread, because both block;
3. the engine (a pool of 10);
4. the task app (`make_queue_app`, a pool of 4) and the modules whose import registers the takeover sweeps,
   the reconcilers and the operation handlers and failure hooks (`operations`, `runs`, `profile.operations`,
   …: `_REGISTERING_MODULES`): all of them exist before the takeover;
5. the epoch claim and the takeover sweep (`claim_epoch_and_sweep`; its sweeps defer through the sweep
   transaction's own connection), then `startup_check_passed` with `check="advisory_lock_acquired"`;
6. the watchdog;
7. the task app opened, the checkpointer, the M2 operation dependencies (Task 11), the collection graph on
   its `CollectDeps` and the `WorkerRuntime` with a fresh `ShutdownState`. The operation dependencies, after
   the checkpointer: the LLM gateway on its `httpx2` client (`llm.transport.make_http_client`: in fake LLM
   mode the recorded transport, else the `anthropic_key` secret; with neither, a gateway that is not
   configured, whose every call fails `llm_not_configured`, logged once here: the worker still starts, plan
   Task 23), the docparse client on
   `cfg.parse_jobs_dir` (every leftover a crashed worker left there removed first, §12.2), the CV store on
   `cfg.cv_store_dir`, the profile graphs on their process-wide `ProfileDeps`, and `OperationDeps` over
   them, the embedder and the clock, which `run_operation` hands to every execution. The collection deps:
   the HTTP layer configured first (`http.configure(cfg)`: in fake
   mode the fixture transport serves every source), then one client per source (`registry.make_clients`,
   which in fake mode refuses to run before that configuration: a client made earlier would be on the live
   transport); the adapters of `registry.build_adapter` on those clients (the provider secrets of `cfg`, the
   keys `b"fake"` in fake mode); the process's one `RateLimiter`; admission stopped with the shutdown; the
   embedder client on `cfg.embedder_url`, which the `embed` node, the `awaiting_embedding` and
   `profile_vectors` reconcilers and the `embed_profile` handler (through `OperationDeps`) share;
8. the source instances synced with the provider secrets (`sources.instances.sync_instances`, in a
   transaction of its own, fenced by this worker's epoch: a source without its secret is
   `disabled('missing_secret')` and never planned, plan Deviation 6), then the start reconcilers
   (`stalled_jobs` releases the jobs of dead workers) and, outside the test env, the metrics server. When the
   fence refuses the epoch, a newer worker took over during this start (final review C2): the sync wrote
   nothing, so this worker's stale view of the secrets cannot undo the newer worker's sync, and the start
   ends there (`worker_superseded_at_start`, exit 70 after the teardown, Ruling FR-i) before any loop runs.
   When the start reconcilers' cadence is fenced instead (a takeover that committed after the sync), the
   cadence requests the graceful stop of a superseded worker (`runtime.request_shutdown`, reason
   `epoch_superseded`, Ruling T24-a), and the start ends there too, before any loop runs, with that path's
   exit 0;
9. SIGTERM and SIGINT handlers that request the shutdown (`runtime.request_shutdown`, the signal's name as
   the reason);
10. one procrastinate worker loop per queue of `QUEUES`, then `worker_ready`;
11. `shutdown.coordinate` until a shutdown is requested or a loop ends by itself;
12. the teardown: the live executions cancelled, the watchdog stopped, the saver, the task app, the source
    clients, the embedder's and the gateway's HTTP clients and the engine closed, and the lock session closed
    last, which releases the lock; then `worker_shutdown_complete`. Each of its waits takes at most what is
    left of the stop's one deadline (`shutdown.StopDeadline`), so the whole stop fits in
    `shutdown.STOP_BUDGET_S`.

Until step 9 a SIGTERM ends the process at once (the default action): nothing runs yet that could drain. The
teardown runs on every other path, also when a step of the start fails, and closes what the start had built.
Each of its steps runs even when an earlier one failed (logged `worker_teardown_failed`; the exit code is then
1, unless it is the superseded 70). The watchdog is always stopped before the lock session closes, so closing
it never reads as a lost lock. When the watchdog thread is still alive after that stop (its join timed out
inside a heartbeat on the lock session), the session is not closed under it: `lock_session_left_open` is
logged and the process exit drops the socket, and with it the lock.

Exit codes: 0 after a requested shutdown: a signal's, or the one a superseded worker requests of itself once a
fence refused its epoch (a task body, the tick, a reconciler cadence, the start's cadence; Ruling T24-a); 1
when a worker loop ended by itself, when the takeover was blocked (`takeover_blocked`: a row lock outlived the
claim's lock timeout, ruling T13-b) and when the start failed (`worker_failed`), so the container restart
tries again. 70 (`lock.WATCHDOG_EXIT_CODE`) means superseded: the watchdog's exit, and a worker whose startup
sync a newer one fenced out (`worker_superseded_at_start`, Ruling FR-i): the newer worker's claim needed the
singleton lock, so this one had lost it, the watchdog's condition, and one code holds whichever of the two
sees it first; the restart stands by while the newer worker is healthy. The startup assertions exit 78.

A loop that ends by itself can be the first sign that this process lost the singleton lock: a zombie (frozen
while a replacement took over, §18.2 C17) finds its procrastinate workers pruned by the replacement, so its
next fetch fails, possibly before its watchdog's next heartbeat. Before its teardown, such a process therefore
runs the heartbeat once more, and when that proves it no longer holds the lock it exits 70 at once, as the
watchdog would (`worker_lock_lost`). A zombie whose first sign is a refused epoch fence instead requests its
own graceful stop (`epoch_superseded`) and exits 0, unless its watchdog's 70 comes first: one of the two
codes, whichever sees the takeover first.

This module is a task boundary (ruff `BLE001` is allowed here). Each broad catch logs the error class only,
never a message, and its outcome is the process exit (§17.1). Neither a shutdown nor a failure rewrites any
state: the next worker's takeover sweep resumes the work (§3.3.5).
"""

import asyncio
import importlib
import os
import signal
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Final, NoReturn, Protocol

import httpx
import httpx2
import procrastinate
import sqlalchemy as sa
from procrastinate.exceptions import ConnectorException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter import __version__
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.db.session import make_engine
from jobhunter.db.sql import LOCK_NOT_AVAILABLE, pg_error
from jobhunter.embeddings import EmbedderClient, make_http_client
from jobhunter.errors import FencedOut
from jobhunter.graphs.checkpointer import open_saver
from jobhunter.graphs.collect.deps import CollectDeps, FetchContext
from jobhunter.graphs.collect.graph import build_collect_graph
from jobhunter.graphs.profile.deps import ProfileDeps
from jobhunter.graphs.profile.graph import build_profile_graphs
from jobhunter.llm import transport as llm_transport
from jobhunter.llm.cache_monitor import CacheMonitor
from jobhunter.llm.gateway import LLMGateway
from jobhunter.observability.logging import configure_logging, get_logger
from jobhunter.observability.metrics import start_metrics_server
from jobhunter.profile.parse_client import ParseClient
from jobhunter.profile.storage import CvStore
from jobhunter.sources import http
from jobhunter.sources.base import SourceAdapter
from jobhunter.sources.instances import InstanceRow, sync_instances
from jobhunter.sources.ratelimit import SOURCE_LIMITS, RateLimiter
from jobhunter.sources.registry import build_adapter, make_clients
from jobhunter.worker.epoch import WorkerIdentity, claim_epoch_and_sweep, fence_epoch
from jobhunter.worker.executions import EXECUTIONS
from jobhunter.worker.lock import WATCHDOG_EXIT_CODE, LockSession, acquire_lock
from jobhunter.worker.operations import OperationDeps
from jobhunter.worker.reconcilers import ReconcileContext, run_reconcilers
from jobhunter.worker.runtime import EPOCH_SUPERSEDED, ShutdownState, WorkerRuntime, request_shutdown
from jobhunter.worker.shutdown import GRACE_S, StopDeadline, coordinate
from jobhunter.worker.tasks import QUEUES, defer_in_tx, make_queue_app
from jobhunter.worker.watchdog import Watchdog

METRICS_PORT: Final = 9100
ENGINE_POOL_SIZE: Final = 10
QUEUE_POOL_MAX_SIZE: Final = 4
# The bounds of two teardown waits; each also takes at most what is left of the stop's deadline.
EXECUTIONS_CANCEL_S: Final = 30.0
WATCHDOG_STOP_S: Final = 15.0
# Imported before the takeover (step 4): importing them registers their sweeps, reconcilers and handlers.
_REGISTERING_MODULES: Final = (
    "jobhunter.worker.operations",
    "jobhunter.worker.runs",
    "jobhunter.quota.ledger",
    "jobhunter.availability.rules",
    "jobhunter.availability.snapshots",
    "jobhunter.dedup.groups",
    "jobhunter.scoring.filters",
    "jobhunter.graphs.collect.nodes.embed",  # through `jobhunter.embeddings`: `awaiting_embedding`
    "jobhunter.sources.watchlist_validate",
    "jobhunter.llm.spend_ledger",  # `llm_ledger_unsettled` and `budget_counters` (M2)
    "jobhunter.llm.gateway",  # `llm_refusal_rate` (M2)
    # the profile handlers and hooks (M2 Tasks 11 and 13), `embed_profile` and, through `profile.vectors`, the
    # `profile_vectors` reconciler (Task 19)
    "jobhunter.profile.operations",
    "jobhunter.settings.scope_apply",  # the `apply_scope` handler and `scope_apply_failed` (M2 Task 18)
)
_SHUTDOWN_SIGNALS: Final = (signal.SIGTERM, signal.SIGINT)

log = get_logger("jobhunter.worker.main")


def is_lock_timeout(exc: BaseException) -> bool:
    """Whether `exc` is a lock wait that ran past `lock_timeout` (SQLSTATE 55P03), however it was raised.

    A statement of the claim raises it as SQLAlchemy's `DBAPIError` (`db.sql.pg_error`). A takeover sweep's
    defer goes through procrastinate's connector on the sweep's own connection, which raises its
    `ConnectorException` from the driver's error: there the SQLSTATE is the cause's `sqlstate`.
    """
    if isinstance(exc, sa.exc.DBAPIError):
        return pg_error(exc).sqlstate == LOCK_NOT_AVAILABLE
    if isinstance(exc, ConnectorException):
        return getattr(exc.__cause__, "sqlstate", None) == LOCK_NOT_AVAILABLE
    return False


class _Heartbeat(Protocol):
    def heartbeat(self, instance_id: uuid.UUID, epoch: int) -> bool: ...


def exit_unless_lock_held(
    session: _Heartbeat, instance_id: uuid.UUID, epoch: int, *, exit: Callable[[int], NoReturn] = os._exit
) -> None:
    """Exit 70 at once, like the watchdog, unless the §3.3.1 heartbeat proves `session` holds the lock.

    For a process whose worker loop ended by itself. No cleanup runs after the exit: nothing more may act as
    the old owner (the replacement's takeover sweep records the outcome). Not the exception's message: it may
    carry content.
    """
    error_class: str | None = None
    try:
        held = session.heartbeat(instance_id, epoch)
    except Exception as exc:
        # The outcome this catch persists is the exit below (§17.1).
        held, error_class = False, type(exc).__name__
    if not held:
        log.critical("worker_lock_lost", epoch=epoch, error_class=error_class, exit_code=WATCHDOG_EXIT_CODE)
        exit(WATCHDOG_EXIT_CODE)


def _on_signal(state: ShutdownState, sig: signal.Signals) -> None:
    """A SIGTERM or SIGINT asks for the graceful stop, as every request does (`runtime.request_shutdown`)."""
    request_shutdown(state, reason=sig.name)


class _WorkerProcess:
    """The start (steps 2 to 11) and the teardown (step 12); it records what the start built so far."""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.lock_session = LockSession(cfg.dsn("psycopg"))
        self.engine: AsyncEngine | None = None
        self.queue: procrastinate.App | None = None  # its `close_async` is a no-op until it is open
        self.watchdog: Watchdog | None = None
        self.saver_scope = AsyncExitStack()
        self.clients: dict[str, httpx.AsyncClient] = {}  # the source clients (step 7), closed at teardown
        self.embedder_http: httpx.AsyncClient | None = None
        self.llm_http: httpx2.AsyncClient | None = None  # the gateway's client (step 7), closed at teardown
        self.shutdown = ShutdownState()  # its `stop_deadline` is set once the stop began (`coordinate`)

    async def run(self) -> int:
        cfg = self.cfg
        dsn = cfg.dsn("psycopg")
        # 2. The lock session and the singleton lock.
        lock_pid = await asyncio.to_thread(self.lock_session.connect)
        await asyncio.to_thread(acquire_lock, self.lock_session)
        # 3. The engine.
        engine = self.engine = make_engine(cfg, pool_size=ENGINE_POOL_SIZE)
        # 4. The task app, then the modules that register what the takeover and the tasks need.
        queue = self.queue = make_queue_app(dsn, max_size=QUEUE_POOL_MAX_SIZE)
        for module in _REGISTERING_MODULES:
            importlib.import_module(module)
        # 5. The epoch claim and the takeover sweep.
        ident = WorkerIdentity(
            instance_id=uuid.uuid4(),
            hostname=socket.gethostname(),
            pid=os.getpid(),
            lock_backend_pid=lock_pid,
            app_version=__version__,
        )
        try:
            epoch, _ = await claim_epoch_and_sweep(engine, ident)
        except (sa.exc.DBAPIError, ConnectorException) as exc:
            if not is_lock_timeout(exc):
                raise
            # Nothing was committed. The teardown closes the lock session, which releases the lock, and the
            # exit lets the restart try again once the holder of the row lock is gone.
            log.error("takeover_blocked", error_class=type(exc).__name__)
            return 1
        log.info("startup_check_passed", check="advisory_lock_acquired", epoch=epoch)
        # 6. The watchdog.
        self.watchdog = Watchdog(self.lock_session, ident.instance_id, epoch)
        self.watchdog.start()
        # 7. The task app opened, the checkpointer, the operation dependencies, the collection graph and the
        # runtime.
        await queue.open_async()
        saver = await self.saver_scope.enter_async_context(open_saver(dsn))
        llm_http = self.llm_http = llm_transport.make_http_client(cfg)
        gateway = LLMGateway(engine, cfg, http_client=llm_http, cache=CacheMonitor())
        if not gateway.configured:
            log.warning("llm_not_configured")  # neither the key nor the fake: every LLM call fails with it
        parse_client = ParseClient(cfg.parse_jobs_dir)
        parse_client.cleanup_leftovers()  # before any job: what a crashed worker left (§12.2)
        cv_store = CvStore(cfg.cv_store_dir)
        profile_graphs = build_profile_graphs(saver, ProfileDeps(engine=engine, clock=utcnow))
        http.configure(cfg)  # before the clients: in fake mode they must be on the fixture transport
        clients = self.clients = make_clients(cfg)
        shutdown = self.shutdown
        self.embedder_http = make_http_client()
        embedder = EmbedderClient(cfg.embedder_url, self.embedder_http)

        def adapter_of(instance: InstanceRow) -> SourceAdapter:
            return build_adapter(instance, cfg, clients)

        deps = CollectDeps(
            engine=engine,
            adapters=adapter_of,
            embedder=embedder,
            clock=utcnow,
            sleep=asyncio.sleep,
            admission_stopped=lambda: shutdown.admission_stopped,
            limiter=RateLimiter(SOURCE_LIMITS.values(), clock=time.monotonic, sleep=asyncio.sleep),
            fetch_ctx=FetchContext(),
        )
        operation_deps = OperationDeps(
            cfg=cfg,
            gateway=gateway,
            parse_client=parse_client,
            cv_store=cv_store,
            profile_graphs=profile_graphs,
            embedder=embedder,
            clock=utcnow,
        )
        runtime = WorkerRuntime(
            cfg=cfg,
            engine=engine,
            queue=queue,
            saver=saver,
            collect_graph=build_collect_graph(saver, deps),
            epoch=epoch,
            shutdown=shutdown,
            embedder=embedder,
            gateway=gateway,
            parse_client=parse_client,
            cv_store=cv_store,
            profile_graphs=profile_graphs,
            operation_deps=operation_deps,
        )

        # 8. The source instances synced with the secrets under this worker's epoch fence, the start
        # reconcilers, the metrics server.
        async def fenced(conn: AsyncConnection) -> None:
            await fence_epoch(conn, epoch)

        try:
            async with engine.begin() as conn:
                await sync_instances(conn, cfg, guard=fenced)
        except FencedOut as refused:
            if not refused.worker_superseded:
                raise  # not this worker's own fence: the start failed (`worker_failed`, exit 1), F5 Minor 1
            # A newer worker owns the work (`fence_epoch` logged both epochs) and the sync wrote nothing. No
            # loop has started; the teardown follows (it closes what the start built), then the watchdog's
            # superseded exit (Ruling FR-i). The restart stands by while that worker is healthy.
            log.critical("worker_superseded_at_start", epoch=epoch, exit_code=WATCHDOG_EXIT_CODE)
            return WATCHDOG_EXIT_CODE
        start = ReconcileContext(
            engine=engine,
            queue=queue,
            defer=defer_in_tx,
            now=utcnow(),
            epoch=epoch,
            embedder=embedder,
            on_superseded=lambda: request_shutdown(shutdown, reason=EPOCH_SUPERSEDED),
        )
        await run_reconcilers(start, "start")
        if shutdown.requested.is_set():
            # The start cadence was fenced (a newer worker took over after the sync): this worker stopped
            # itself, as a fenced task body does (Ruling T24-a). No loop starts; the teardown follows, exit 0.
            return 0
        if cfg.env != "test":
            start_metrics_server(METRICS_PORT)
        # 9. The signal handlers.
        loop = asyncio.get_running_loop()
        for sig in _SHUTDOWN_SIGNALS:
            loop.add_signal_handler(sig, _on_signal, runtime.shutdown, sig)
        # 10. One worker loop per queue.
        loops = [
            asyncio.create_task(
                queue.run_worker_async(
                    queues=[name],
                    concurrency=concurrency,
                    install_signal_handlers=False,
                    shutdown_graceful_timeout=GRACE_S,
                    additional_context={"runtime": runtime},
                    name=f"jh-{name}",
                ),
                name=f"jh-{name}",
            )
            for name, concurrency in QUEUES.items()
        ]
        log.info("worker_ready", instance_id=str(ident.instance_id), epoch=epoch)
        # 11. Until a shutdown is requested or a loop ends by itself.
        code = await coordinate(loops, runtime.shutdown)
        if code != 0:
            await asyncio.to_thread(exit_unless_lock_held, self.lock_session, ident.instance_id, epoch)
        return code

    async def _stop_watchdog(self, timeout_s: float) -> None:
        if self.watchdog is not None:
            # `stop()` joins the thread, which can be in a heartbeat (5 s statement timeout): off the loop.
            await asyncio.to_thread(self.watchdog.stop, timeout_s)

    async def _close_queue(self) -> None:
        if self.queue is not None:
            await self.queue.close_async()

    async def _close_clients(self) -> None:
        clients, self.clients = list(self.clients.values()), {}
        for client in clients:
            await client.aclose()

    async def _close_embedder(self) -> None:
        if self.embedder_http is not None:
            await self.embedder_http.aclose()

    async def _close_llm(self) -> None:
        if self.llm_http is not None:
            await self.llm_http.aclose()

    async def _dispose_engine(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()

    async def _close_lock_session(self) -> None:
        if self.watchdog is not None and self.watchdog.is_alive():
            # Its stop timed out joining the thread, which is still in a heartbeat on this very session:
            # closing the connection under that statement is not safe (M0 carry-forward 5). The process exit
            # drops the socket, and the server then ends the session and releases the lock.
            log.warning("lock_session_left_open")
            return
        self.lock_session.close()

    async def teardown(self) -> bool:
        """Step 12, in order, for what the start built; `False` when a step failed (the rest still ran).

        Every wait takes at most what is left of the stop's one deadline: the coordinator's, or one that
        starts now when the start failed before it. A step that would wait past it is cut off (`TimeoutError`,
        logged like any failed step); one that needs no wait still runs. The lock session is the last step,
        always: it is closed unless the watchdog thread is still alive (`lock_session_left_open`, which is not
        a failed step).
        """
        deadline = self.shutdown.stop_deadline or StopDeadline.start()
        steps: tuple[tuple[str, Callable[[], Awaitable[object]]], ...] = (
            ("executions", lambda: EXECUTIONS.cancel_all(timeout_s=deadline.remaining(EXECUTIONS_CANCEL_S))),
            ("watchdog", lambda: self._stop_watchdog(deadline.remaining(WATCHDOG_STOP_S))),
            ("saver", lambda: _within(deadline, self.saver_scope.aclose())),
            ("queue", lambda: _within(deadline, self._close_queue())),
            ("clients", lambda: _within(deadline, self._close_clients())),
            ("embedder", lambda: _within(deadline, self._close_embedder())),
            ("llm", lambda: _within(deadline, self._close_llm())),
            ("engine", lambda: _within(deadline, self._dispose_engine())),
            ("lock_session", self._close_lock_session),  # last: closing it releases the lock
        )
        clean = True
        for name, step in steps:
            try:
                await step()
            except Exception as exc:
                # The outcome is the exit code (1, `worker_main`): what is left open ends with the process.
                clean = False
                log.error("worker_teardown_failed", step=name, error_class=type(exc).__name__)
        return clean


async def _within(deadline: StopDeadline, step: Awaitable[object]) -> None:
    """Await `step` until the stop's `deadline`, then cut it off with `TimeoutError`.

    `asyncio.timeout` fires only once `step` waits, so a step that needs no wait completes even when nothing
    is left of the budget.
    """
    async with asyncio.timeout(deadline.remaining()):
        await step


async def worker_main(cfg: AppConfig) -> int:
    """Run the worker process until it is shut down; its exit code (0, or 1 when it failed)."""
    # 1. Logging at the configured level (a startup assertion checked it).
    configure_logging(cfg.log_level)
    process = _WorkerProcess(cfg)
    try:
        code = await process.run()
    except Exception as exc:
        # The outcome is the exit (1) after the teardown below; the restart tries again. Not the message.
        log.critical("worker_failed", error_class=type(exc).__name__)
        code = 1
    finally:
        clean = await process.teardown()
    if not clean and code != WATCHDOG_EXIT_CODE:  # superseded stays 70 (Ruling FR-i)
        code = 1
    log.info("worker_shutdown_complete", exit_code=code)
    return code
