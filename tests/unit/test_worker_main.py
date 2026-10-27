import asyncio
import functools
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, NoReturn

import pytest
from jobhunter.worker.main import exit_unless_lock_held

from tests.support.clock import FakeClock
from tests.support.logs import logged


class Exited(Exception):
    pass


def recording_exit(codes: list[int]):
    def exit(code: int) -> NoReturn:
        codes.append(code)
        raise Exited

    return exit


class Session:
    def __init__(self, outcome: bool | Exception) -> None:
        self.outcome = outcome
        self.calls: list[tuple[uuid.UUID, int]] = []

    def heartbeat(self, instance_id: uuid.UUID, epoch: int) -> bool:
        self.calls.append((instance_id, epoch))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.mark.parametrize(
    ("outcome", "error_class"), [(False, None), (OSError("detail that must not be logged"), "OSError")]
)
def test_a_process_that_cannot_prove_it_holds_the_lock_exits_70(caplog, outcome, error_class) -> None:
    me, codes = uuid.uuid4(), list[int]()
    session = Session(outcome)
    with pytest.raises(Exited):
        exit_unless_lock_held(session, me, 4, exit=recording_exit(codes))
    assert codes == [70] and session.calls == [(me, 4)]
    (event,) = logged(caplog, "worker_lock_lost")
    assert (event["epoch"], event["error_class"], event["exit_code"]) == (4, error_class, 70)
    assert "must not be logged" not in caplog.text


def test_the_lock_holder_goes_on_to_its_teardown(caplog) -> None:
    codes: list[int] = []
    exit_unless_lock_held(Session(True), uuid.uuid4(), 4, exit=recording_exit(codes))
    assert codes == [] and logged(caplog, "worker_lock_lost") == []


# --- Fix round 1, item 4: a lock timeout is recognised however it reaches the claim ------------------------


def test_a_lock_timeout_is_recognised_through_sqlalchemy_and_through_procrastinate() -> None:
    import psycopg
    import sqlalchemy as sa
    from jobhunter.worker.main import is_lock_timeout
    from procrastinate.exceptions import ConnectorException

    class LockNotAvailable(psycopg.Error):
        sqlstate = "55P03"

    class Unique(psycopg.Error):
        sqlstate = "23505"

    def connector_error(cause: BaseException | None) -> ConnectorException:
        error = ConnectorException()
        error.__cause__ = cause  # how procrastinate wraps the driver's error (`raise ... from exc`)
        return error

    assert is_lock_timeout(sa.exc.OperationalError("SELECT 1", {}, LockNotAvailable()))
    assert is_lock_timeout(connector_error(LockNotAvailable()))  # a sweep's defer through procrastinate
    assert not is_lock_timeout(sa.exc.OperationalError("SELECT 1", {}, psycopg.Error()))
    assert not is_lock_timeout(sa.exc.IntegrityError("SELECT 1", {}, Unique()))
    assert not is_lock_timeout(connector_error(Unique()))
    assert not is_lock_timeout(connector_error(None))
    assert not is_lock_timeout(RuntimeError("55P03"))


# --- Fix round 1, item 6: the start and the teardown of `worker_main`, with fake collaborators -------------


class World:
    """Fakes for every collaborator of `worker_main` that does I/O; `events` records what they did."""

    def __init__(self, monkeypatch, tmp_path, *, fake_llm: bool = True) -> None:
        from contextlib import asynccontextmanager

        from jobhunter.config import load_config
        from jobhunter.worker import main

        events: list[str] = []
        self.events = events
        self.open_fails = False
        self.dispose_fails = False
        # For the stop budget: what each bounded teardown wait was given, and hooks that spend fake time.
        self.timeouts: dict[str, float] = {}
        self.on_loop_cancelled: Callable[[], None] = lambda: None
        self.on_executions_cancelled: Callable[[], None] = lambda: None
        self.on_watchdog_stopped: Callable[[], None] = lambda: None
        self.saver_close_hangs = False
        # What the start handed on (Task 22 fix round 1, M-2): the runtime, the reconcile contexts, the deps.
        self.runtimes: list[object] = []
        self.contexts: list[object] = []
        self.deps: list[object] = []
        self.profile_deps: list[object] = []  # Task 11: what the profile graphs were built with
        self.profile_graphs = object()
        self.score_deps: list[object] = []  # M3 Task 11: what the score graphs were built with
        self.score_graphs = object()
        world = self

        class FakeLockSession:
            def __init__(self, dsn: str) -> None:
                pass

            def connect(self) -> int:
                events.append("lock_connect")
                return 4242

            def heartbeat(self, instance_id: uuid.UUID, epoch: int) -> bool:
                return True

            def close(self) -> None:
                events.append("lock_close")

        class FakeWatchdog:
            def __init__(self, session: object, instance_id: uuid.UUID, epoch: int) -> None:
                pass

            def start(self) -> None:
                events.append("watchdog_start")

            def stop(self, timeout_s: float = 15.0) -> None:
                events.append("watchdog_stop")
                world.timeouts["watchdog"] = timeout_s
                world.on_watchdog_stopped()

            def is_alive(self) -> bool:
                return False  # its thread ended within the stop (`test_teardown.py` covers the other case)

        class FakeExecutions:
            async def cancel_all(self, *, timeout_s: float = 30.0) -> int:
                world.timeouts["executions"] = timeout_s
                world.on_executions_cancelled()
                return 0

        class FakeEngine:
            @asynccontextmanager
            async def begin(self):
                yield object()  # the connection of step 8's `sync_instances`, which is faked as well

            async def dispose(self) -> None:
                events.append("engine_dispose")
                if world.dispose_fails:
                    raise OSError("detail that must not be logged")

        class FakeQueue:
            async def open_async(self) -> None:
                events.append("queue_open")
                if world.open_fails:
                    raise RuntimeError("detail that must not be logged")

            async def close_async(self) -> None:
                events.append("queue_close")

            async def run_worker_async(self, **options: object) -> None:
                world.runtimes.append(options["additional_context"]["runtime"])  # type: ignore[index]
                if options["name"] == "jh-tick":  # this loop asks for the shutdown, as a SIGTERM would
                    options["additional_context"]["runtime"].shutdown.requested.set()  # type: ignore[index]
                try:
                    await asyncio.get_running_loop().create_future()  # runs until it is cancelled
                except asyncio.CancelledError:
                    world.on_loop_cancelled()
                    raise

        @asynccontextmanager
        async def fake_open_saver(dsn: str):
            events.append("saver_open")
            try:
                yield object()
            finally:
                events.append("saver_close")
                if world.saver_close_hangs:
                    await asyncio.get_running_loop().create_future()  # a close that never ends

        async def fake_claim(engine: object, ident: object) -> tuple[int, dict[str, int]]:
            events.append("claim")
            return 3, {}

        def fake_build_collect_graph(saver: object, deps: object) -> object:
            world.deps.append(deps)
            return object()

        def fake_build_profile_graphs(saver: object, deps: object) -> object:
            world.profile_deps.append(deps)
            return world.profile_graphs

        def fake_build_score_graphs(deps: object) -> object:
            world.score_deps.append(deps)
            return world.score_graphs

        async def fake_reconcilers(ctx: Any, cadence: str) -> dict[str, int]:
            events.append(f"reconcile_{cadence}")
            world.contexts.append(ctx)
            if world.start_fenced and cadence == "start":  # as `run_reconcilers` does when a fence refuses
                ctx.on_superseded()
            return {}

        # Task 24: step 7's HTTP layer and clients (real, on no network), step 8's instance sync (faked).
        from jobhunter.sources import http, registry

        real_configure = http.configure
        self.clients: dict[str, object] = {}

        def recording_configure(cfg: object) -> None:
            events.append("http_configure")
            real_configure(cfg)  # type: ignore[arg-type]

        def recording_make_clients(cfg: object) -> object:
            events.append("make_clients")
            made = registry.make_clients(cfg)  # type: ignore[arg-type]
            world.clients.update(made)
            return made

        async def fake_sync_instances(
            conn: object, cfg: object, *, guard: Callable[[object], Awaitable[None]]
        ) -> dict[str, str]:
            events.append("sync_instances")
            await guard(conn)  # the real sync's first statement (final review C2)
            world.synced.append(cfg)
            return {}

        async def fake_fence_epoch(conn: object, epoch: int) -> None:
            from jobhunter.errors import FencedOut

            world.fenced.append(epoch)
            if world.superseded:
                raise FencedOut(owner=world.fence_owner)

        self.synced: list[object] = []
        self.fenced: list[int] = []  # the epochs step 8's guard fenced
        self.superseded = False  # a newer worker took over before step 8
        self.fence_owner = "worker_epoch"  # the owner of the fence step 8's guard raises when `superseded`
        self.start_fenced = False  # a newer worker took over after step 8's sync, before its reconcilers
        monkeypatch.setattr(http, "_configuration", http._configuration)  # restored on teardown
        monkeypatch.setattr(http, "configure", recording_configure)

        for name, fake in {
            "LockSession": FakeLockSession,
            "acquire_lock": lambda session: events.append("lock_acquire"),
            "make_engine": lambda cfg, pool_size: FakeEngine(),
            "make_queue_app": lambda dsn, max_size: FakeQueue(),
            "claim_epoch_and_sweep": fake_claim,
            "Watchdog": FakeWatchdog,
            "open_saver": fake_open_saver,
            "build_collect_graph": fake_build_collect_graph,
            "build_profile_graphs": fake_build_profile_graphs,
            "build_score_graphs": fake_build_score_graphs,
            "run_reconcilers": fake_reconcilers,
            "EXECUTIONS": FakeExecutions(),
            "make_clients": recording_make_clients,
            "sync_instances": fake_sync_instances,
            "fence_epoch": fake_fence_epoch,
        }.items():
            monkeypatch.setattr(main, name, fake)
        secrets = tmp_path / "secrets"
        secrets.mkdir()
        for name, value in (("db_app", "pw"), ("data_key", "a" * 43 + "=")):
            (secrets / name).write_text(value)
            (secrets / name).chmod(0o440)
        # Task 11: the gateway is on the fake LLM transport (this worker has no `anthropic_key`), and the
        # parse root and the CV store are the test's own. Task 23: without `fake_llm`, a worker with neither
        # the key nor the fake (its gateway is not configured).
        self.parse_root = tmp_path / "parse"
        self.parse_root.mkdir()
        fake_env = {"JOBHUNTER_FAKE_LLM": "1", "JOBHUNTER_FAKE_LLM_DIR": str(tmp_path / "anthropic")}
        env = {
            "JOBHUNTER_ENV": "test",
            "JOBHUNTER_DB_NAME": "jobhunter",
            "JOBHUNTER_DB_HOST": "unused",
            **(fake_env if fake_llm else {}),
            "JOBHUNTER_PARSE_JOBS_DIR": str(self.parse_root),
            "JOBHUNTER_CV_STORE_DIR": str(tmp_path / "cv"),
        }
        self.cfg = load_config("worker", env, secrets)


TEARDOWN = ["watchdog_stop", "saver_close", "queue_close", "engine_dispose", "lock_close"]


async def test_a_requested_shutdown_tears_down_in_order_and_exits_0(monkeypatch, tmp_path, caplog) -> None:
    from jobhunter.worker.main import worker_main

    world = World(monkeypatch, tmp_path)
    assert await worker_main(world.cfg) == 0
    assert world.events == [
        "lock_connect",
        "lock_acquire",
        "claim",
        "watchdog_start",
        "queue_open",
        "saver_open",
        "http_configure",
        "make_clients",
        "sync_instances",
        "reconcile_start",
        *TEARDOWN,
    ]
    assert [e["exit_code"] for e in logged(caplog, "worker_shutdown_complete")] == [0]


async def test_step_7_configures_http_before_the_clients_and_step_8_syncs_the_instances_first(
    monkeypatch, tmp_path
) -> None:
    """Task 24, carries 1 to 3: `http.configure(cfg)` comes before `make_clients(cfg)` (a client made earlier
    in fake mode would be on the live transport); the graph's adapters are the registry's, on those clients;
    `sync_instances(conn, cfg)` runs before the start reconcilers; the teardown closes every client."""
    from jobhunter.graphs.collect.deps import CollectDeps
    from jobhunter.sources.arbeitnow import ArbeitnowAdapter
    from jobhunter.sources.instances import InstanceRow
    from jobhunter.worker.main import worker_main
    from jobhunter.worker.runtime import WorkerRuntime

    world = World(monkeypatch, tmp_path)
    assert await worker_main(world.cfg) == 0
    events = world.events
    assert events.index("http_configure") < events.index("make_clients") < events.index("sync_instances")
    assert events.index("sync_instances") < events.index("reconcile_start")
    assert world.synced == [world.cfg]
    [deps], [runtime] = world.deps, {id(r): r for r in world.runtimes}.values()
    assert isinstance(deps, CollectDeps) and isinstance(runtime, WorkerRuntime)
    arbeitnow = InstanceRow(3, "arbeitnow", "default", None, "ok", None, None, None, None)
    adapter = deps.adapters(arbeitnow)
    assert isinstance(adapter, ArbeitnowAdapter)
    # The shutdown stops the admission of new requests (§3.3.5): the deps read the runtime's state.
    runtime.shutdown.admission_stopped = False
    assert deps.admission_stopped() is False
    runtime.shutdown.admission_stopped = True
    assert deps.admission_stopped() is True
    assert world.clients and all(client.is_closed for client in world.clients.values())  # type: ignore[attr-defined]


async def test_step_8_syncs_the_instances_under_the_fence_of_the_workers_own_epoch(
    monkeypatch, tmp_path
) -> None:
    """Final review C2: the startup sync's guard is the epoch fence of the epoch this worker claimed."""
    from jobhunter.worker.main import worker_main

    world = World(monkeypatch, tmp_path)
    assert await worker_main(world.cfg) == 0
    assert world.fenced == [3] and world.synced == [world.cfg]


async def test_a_worker_superseded_before_its_startup_sync_starts_no_loop_and_exits_70(
    monkeypatch, tmp_path, caplog
) -> None:
    """Final review C2, Ruling FR-i: a newer worker took over while this one was starting. Its fenced sync
    writes nothing, and the start ends there: no start reconciler, no worker loop; the teardown runs and the
    exit is the watchdog's superseded code, also when a teardown step fails."""
    from jobhunter.worker.lock import WATCHDOG_EXIT_CODE
    from jobhunter.worker.main import worker_main

    world = World(monkeypatch, tmp_path)
    world.superseded = True
    assert WATCHDOG_EXIT_CODE == 70
    assert await worker_main(world.cfg) == WATCHDOG_EXIT_CODE
    assert world.events == [
        "lock_connect",
        "lock_acquire",
        "claim",
        "watchdog_start",
        "queue_open",
        "saver_open",
        "http_configure",
        "make_clients",
        "sync_instances",
        *TEARDOWN,
    ]
    assert world.synced == [] and world.runtimes == []
    [event] = logged(caplog, "worker_superseded_at_start")
    assert (event["epoch"], event["exit_code"], event["level"]) == (3, 70, "critical")
    assert logged(caplog, "worker_failed") == []
    # a failing teardown step leaves the superseded code as it is
    (tmp_path / "again").mkdir()
    failing = World(monkeypatch, tmp_path / "again")
    failing.superseded, failing.dispose_fails = True, True
    assert await worker_main(failing.cfg) == WATCHDOG_EXIT_CODE


async def test_a_fence_of_another_owner_at_the_startup_sync_fails_the_start_and_exits_1(
    monkeypatch, tmp_path, caplog
) -> None:
    """Fix wave F6 (F5 re-review, out-of-scope observation): only the worker's own fence proves a takeover.
    Should the sync ever raise another owner's fence, the start failed (`worker_failed`, exit 1, the restart
    tries again), never `worker_superseded_at_start` (70). No start reconciler and no loop runs."""
    from jobhunter.worker.main import worker_main

    world = World(monkeypatch, tmp_path)
    world.superseded, world.fence_owner = True, "run"
    assert await worker_main(world.cfg) == 1
    assert world.events[world.events.index("sync_instances") :] == ["sync_instances", *TEARDOWN]
    assert world.synced == [] and world.runtimes == []
    assert [e["error_class"] for e in logged(caplog, "worker_failed")] == ["FencedOut"]
    assert logged(caplog, "worker_superseded_at_start") == []


async def test_a_worker_superseded_during_its_start_cadence_stops_itself_and_exits_0(
    monkeypatch, tmp_path, caplog
) -> None:
    """Final wave F5 (F2 re-review, observation 2): a takeover that commits between the startup sync and the
    start reconcilers fences their cadence. The cadence requests the graceful stop of a superseded worker
    (Ruling T24-a), so no worker loop starts; the teardown runs and the exit is that path's 0."""
    from jobhunter.worker.main import worker_main

    world = World(monkeypatch, tmp_path)
    world.start_fenced = True
    assert await worker_main(world.cfg) == 0
    assert world.events[world.events.index("sync_instances") :] == [
        "sync_instances",
        "reconcile_start",
        *TEARDOWN,
    ]
    assert world.runtimes == []  # no loop ran
    [event] = logged(caplog, "shutdown_requested")
    assert (event["level"], event["reason"]) == ("warning", "epoch_superseded")
    assert [e["exit_code"] for e in logged(caplog, "worker_shutdown_complete")] == [0]


def test_a_signal_requests_the_stop_with_the_one_log_shape(log_events) -> None:
    """Ledger L390: a SIGTERM goes through `runtime.request_shutdown`, logged `shutdown_requested` with the
    signal's name as its `reason` (at `info`), once however often a signal arrives."""
    import signal

    from jobhunter.worker.main import _on_signal
    from jobhunter.worker.runtime import ShutdownState

    state = ShutdownState()
    _on_signal(state, signal.SIGTERM)
    _on_signal(state, signal.SIGINT)
    assert state.requested.is_set()
    [event] = [e for e in log_events() if e["event"] == "shutdown_requested"]
    assert (event["level"], event["reason"]) == ("info", "SIGTERM") and "signal" not in event


async def test_the_worker_shares_one_embedder_client_on_its_configured_url(monkeypatch, tmp_path) -> None:
    """Task 22 fix round 1, M-2: the graph's deps, the runtime (whose task bodies hand it to every reconcile
    context) and the start's reconcile context carry one client on `cfg.embedder_url`, closed at teardown."""
    from jobhunter.embeddings import EmbedderClient
    from jobhunter.graphs.collect.deps import CollectDeps
    from jobhunter.worker.main import worker_main
    from jobhunter.worker.reconcilers import ReconcileContext
    from jobhunter.worker.runtime import WorkerRuntime

    world = World(monkeypatch, tmp_path)
    assert await worker_main(world.cfg) == 0
    runtime = world.runtimes[0]
    assert isinstance(runtime, WorkerRuntime) and all(r is runtime for r in world.runtimes)
    embedder = runtime.embedder
    assert isinstance(embedder, EmbedderClient) and embedder.base_url == world.cfg.embedder_url
    [start] = world.contexts
    assert isinstance(start, ReconcileContext) and start.embedder is embedder
    [deps] = world.deps
    assert isinstance(deps, CollectDeps) and deps.embedder is embedder
    assert embedder.http.is_closed  # the teardown's `embedder` step


async def test_the_runtime_carries_the_operation_dependencies(monkeypatch, tmp_path, caplog) -> None:
    """Task 11: after the saver, the gateway (on the fake LLM here), the parse client on `cfg.parse_jobs_dir`
    (its leftovers removed at the start), the CV store on `cfg.cv_store_dir` and the profile graphs on
    their process-wide deps; `operation_deps` holds the same objects and the shared embedder, and the
    gateway's HTTP client is closed at teardown. M3 Task 11: the score graphs on `ScoreDeps` of the same
    engine, gateway and saver."""
    from jobhunter.clock import utcnow
    from jobhunter.graphs.profile.deps import ProfileDeps
    from jobhunter.graphs.score.deps import ScoreDeps
    from jobhunter.llm.gateway import LLMGateway
    from jobhunter.profile.parse_client import ParseClient
    from jobhunter.profile.storage import CvStore
    from jobhunter.worker.main import worker_main
    from jobhunter.worker.operations import OperationDeps
    from jobhunter.worker.runtime import WorkerRuntime

    world = World(monkeypatch, tmp_path)
    leftover = world.parse_root / "a-crashed-job"
    leftover.mkdir()
    (leftover / "input.pdf").write_bytes(b"%PDF-")
    assert await worker_main(world.cfg) == 0
    assert not leftover.exists()
    assert [e["count"] for e in logged(caplog, "parse_leftovers_removed")] == [1]
    runtime = world.runtimes[0]
    assert isinstance(runtime, WorkerRuntime)
    assert isinstance(runtime.gateway, LLMGateway)
    assert (
        isinstance(runtime.parse_client, ParseClient)
        and runtime.parse_client.root == world.cfg.parse_jobs_dir
    )
    assert isinstance(runtime.cv_store, CvStore) and runtime.cv_store.root == world.cfg.cv_store_dir
    assert runtime.profile_graphs is world.profile_graphs
    [profile_deps] = world.profile_deps
    assert isinstance(profile_deps, ProfileDeps) and profile_deps.engine is runtime.engine
    deps = runtime.operation_deps
    assert isinstance(deps, OperationDeps)
    assert (deps.cfg, deps.gateway, deps.parse_client, deps.cv_store, deps.profile_graphs, deps.embedder) == (
        world.cfg,
        runtime.gateway,
        runtime.parse_client,
        runtime.cv_store,
        runtime.profile_graphs,
        runtime.embedder,
    )
    assert runtime.gateway.http_client.is_closed  # the teardown's `llm` step
    assert runtime.gateway.configured and logged(caplog, "llm_not_configured") == []
    # M3 Task 11: the score graphs on the process's engine, gateway, clock and saver, compiled once
    [score_deps] = world.score_deps
    assert runtime.score_graphs is world.score_graphs and runtime.score_deps is score_deps
    assert isinstance(score_deps, ScoreDeps)
    assert (score_deps.engine, score_deps.gateway, score_deps.saver) == (
        runtime.engine,
        runtime.gateway,
        runtime.saver,
    )
    assert score_deps.monotonic is runtime.gateway.monotonic and score_deps.clock is utcnow


async def test_a_worker_without_a_key_and_not_in_fake_mode_starts_and_logs_llm_not_configured(
    monkeypatch, tmp_path, caplog
) -> None:
    """Task 23: a worker with neither the `anthropic_key` secret nor the fake LLM starts as any other (its
    loops run, the teardown closes the gateway's client); its gateway is not configured, so every LLM call
    fails `llm_not_configured`, and it says so once at start, at `warning`."""
    from jobhunter.worker.main import worker_main
    from jobhunter.worker.runtime import WorkerRuntime

    world = World(monkeypatch, tmp_path, fake_llm=False)
    assert not world.cfg.fake_llm and "anthropic_key" not in world.cfg.secrets
    assert await worker_main(world.cfg) == 0
    assert world.events == [
        "lock_connect",
        "lock_acquire",
        "claim",
        "watchdog_start",
        "queue_open",
        "saver_open",
        "http_configure",
        "make_clients",
        "sync_instances",
        "reconcile_start",
        *TEARDOWN,
    ]
    runtime = world.runtimes[0]
    assert isinstance(runtime, WorkerRuntime) and runtime.gateway.configured is False
    assert runtime.operation_deps.gateway is runtime.gateway
    [event] = logged(caplog, "llm_not_configured")
    assert event["level"] == "warning"
    assert runtime.gateway.http_client.is_closed


async def test_a_start_that_fails_after_the_claim_stops_the_watchdog_and_closes_the_lock(
    monkeypatch, tmp_path, caplog
) -> None:
    from jobhunter.worker.main import worker_main

    world = World(monkeypatch, tmp_path)
    world.open_fails = True
    assert await worker_main(world.cfg) == 1
    # Nothing after the failed step was built; what was built is closed, the lock session last.
    assert world.events[world.events.index("queue_open") + 1 :] == [
        "watchdog_stop",
        "queue_close",
        "engine_dispose",
        "lock_close",
    ]
    assert [e["error_class"] for e in logged(caplog, "worker_failed")] == ["RuntimeError"]
    assert [e["exit_code"] for e in logged(caplog, "worker_shutdown_complete")] == [1]
    assert "must not be logged" not in caplog.text


async def test_a_failing_teardown_step_does_not_stop_the_others_and_exits_1(
    monkeypatch, tmp_path, caplog
) -> None:
    from jobhunter.worker.main import worker_main

    world = World(monkeypatch, tmp_path)
    world.dispose_fails = True
    assert await worker_main(world.cfg) == 1
    assert world.events[-len(TEARDOWN) :] == TEARDOWN  # the lock session is still closed after the failure
    (event,) = logged(caplog, "worker_teardown_failed")
    assert (event["step"], event["error_class"]) == ("engine", "OSError")
    assert [e["exit_code"] for e in logged(caplog, "worker_shutdown_complete")] == [1]
    assert "must not be logged" not in caplog.text


# --- Final fix wave, item 6: every wait of the stop takes what is left of one deadline ---------------------


async def test_the_teardown_shares_what_is_left_of_the_one_stop_deadline(
    monkeypatch, tmp_path, caplog
) -> None:
    from jobhunter.worker import main, shutdown

    world = World(monkeypatch, tmp_path)
    clock = FakeClock(1000.0)
    monkeypatch.setattr(main, "coordinate", functools.partial(shutdown.coordinate, clock=clock))

    def spend(seconds: float) -> Callable[[], None]:
        return lambda: setattr(clock, "now", clock.now + seconds)

    # The running jobs drain for the whole grace, cancelling the executions takes 4 s and stopping the
    # watchdog the 6 s left; the saver's close would then never end.
    world.on_loop_cancelled = lambda: setattr(clock, "now", max(clock.now, 1000.0 + shutdown.GRACE_S))
    world.on_executions_cancelled = spend(4.0)
    world.on_watchdog_stopped = spend(6.0)
    world.saver_close_hangs = True
    # Bounded: a teardown that waits past its budget fails the test, never hangs it.
    assert await asyncio.wait_for(main.worker_main(world.cfg), 5) == 1  # a teardown step was cut off
    assert world.timeouts == {"executions": shutdown.TEARDOWN_S, "watchdog": shutdown.TEARDOWN_S - 4.0}
    (event,) = logged(caplog, "worker_teardown_failed")
    assert (event["step"], event["error_class"]) == ("saver", "TimeoutError")
    assert world.events[-len(TEARDOWN) :] == TEARDOWN  # every step still ran, the lock session last
    assert clock.now == 1000.0 + shutdown.STOP_BUDGET_S  # the whole stop took exactly its budget


# --- Task 24: step 4's modules register every reconciler, sweep and handler the worker runs -----------------

# Every reconciler of M1 with its cadences (spec §17.4), as `worker_main` has them before its takeover.
M1_RECONCILERS = {
    "stalled_jobs": ["start", "tick"],
    "metrics_refresh": ["heavy"],
    "operations": ["tick"],
    "stale_runs": ["tick"],
    "queued_runs": ["tick"],
    "stale_data": ["tick"],
    "ledger_unsettled": ["tick"],
    "hard_filter_completeness": ["heavy"],
    "stale_sweep": ["tick"],
    "stale_sweep_daily": ["daily"],
    "ats_mass_events": ["tick"],
    "group_representatives": ["heavy"],
    "awaiting_embedding": ["heavy"],
}
# The reconcilers M2 adds, by task: the spend ledger's two (Task 3), the gateway's refusal rate (Task 5), the
# scope apply's permanent-failure recovery (Task 18) and the profile vectors' (Task 19, registered through
# `profile.operations` -> `profile.vectors`).
M2_RECONCILERS = {
    "llm_ledger_unsettled": ["tick"],
    "budget_counters": ["heavy"],
    "llm_refusal_rate": ["heavy"],
    "scope_apply_failed": ["tick"],
    "profile_vectors": ["heavy"],
}
# The M3 reconcilers so far: `orphaned_cycles` (Task 11, `scoring.cycles`, with the takeover sweeps
# `score_targets` and `score_cycles`; Ruling P44), and `expiry_sweep` and `budget_release` (Task 13,
# `scoring.targets`).
M3_RECONCILERS = {"orphaned_cycles": ["tick"], "expiry_sweep": ["heavy"], "budget_release": ["tick"]}
# Run with the module names as arguments: it imports those modules and nothing else, then reads the registries
# from `sys.modules` (each registry module is imported by the modules that register into it).
_REGISTRY_SCRIPT = """\
import importlib, json, sys
for module in sys.argv[1:]:
    importlib.import_module(module)
print(json.dumps({
    "reconcilers": {
        name: sorted(cadences)
        for name, (cadences, _) in sys.modules["jobhunter.worker.reconcilers"].REGISTRY.items()
    },
    "sweeps": sorted(sys.modules["jobhunter.worker.epoch"]._SWEEPS),
    "handlers": sorted(sys.modules["jobhunter.worker.operations"].HANDLERS),
    "failure_hooks": sorted(sys.modules["jobhunter.worker.operations"].FAILURE_HOOKS),
    "main_imported": "jobhunter.worker.main" in sys.modules,
}))
"""


def registered_by(modules: tuple[str, ...]) -> dict[str, object]:
    """What a fresh interpreter has registered after importing only `modules`."""
    import json
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-c", _REGISTRY_SCRIPT, *modules],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    registered: dict[str, object] = json.loads(done.stdout.strip().splitlines()[-1])
    return registered


def test_the_registering_modules_alone_register_every_reconciler_sweep_and_handler() -> None:
    """Fix round 1 (Minor 5): in a fresh interpreter that imports ONLY `worker.main._REGISTERING_MODULES`
    (step 4, before the takeover; never `worker.main` itself, whose imports would register most of it
    anyway), the registries hold exactly the M1, M2 and M3 (so far) reconcilers with their cadences, the four
    takeover sweeps (`operations`, `runs`, and M3 Task 11's `score_targets` and `score_cycles`), the
    `extract_profile_text` (Task 11), `extract_profile` (Task 13), `embed_profile` (Task 19),
    `propose_queries` (Task 20), `apply_scope` (Task 18) and `watchlist_validate` handlers, and the failure
    hooks of the two profile phases and of the query proposal (`embed_profile` has none: it drives no domain
    row). `embed_profile`, `propose_queries` and the `profile_vectors` reconciler come with
    `profile.operations`, which imports `profile.vectors`; `worker.main` lists neither."""
    from jobhunter.worker.main import _REGISTERING_MODULES

    assert registered_by(_REGISTERING_MODULES) == {
        "reconcilers": {**M1_RECONCILERS, **M2_RECONCILERS, **M3_RECONCILERS},
        "sweeps": ["operations", "runs", "score_cycles", "score_targets"],
        "handlers": [
            "apply_scope",
            "embed_profile",
            "extract_profile",
            "extract_profile_text",
            "propose_queries",
            "watchlist_validate",
        ],
        "failure_hooks": ["extract_profile", "extract_profile_text", "propose_queries"],
        "main_imported": False,
    }
