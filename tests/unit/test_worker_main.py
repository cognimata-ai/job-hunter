import asyncio
import functools
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, NoReturn, cast

import pytest
from jobhunter.worker.main import exit_unless_lock_held

from tests.support.clock import FakeClock
from tests.support.logs import logged
from tests.support.reconcile import registered_by


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

    def __init__(
        self,
        monkeypatch,
        tmp_path,
        *,
        fake_llm: bool = True,
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | None = None,
    ) -> None:
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
                events.append("executions_cancel")
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
        # Task 28 fix round 1: the teardown's `llm_tries` step, on the real gateway the start builds.
        from jobhunter.llm.gateway import LLMGateway

        async def recording_settle_in_flight(gateway: object, timeout_s: float) -> int:
            events.append("llm_tries")
            world.timeouts["llm_tries"] = timeout_s
            return 0

        monkeypatch.setattr(LLMGateway, "settle_in_flight", recording_settle_in_flight)
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
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        for name, value in (("db_app", "pw"), ("data_key", "a" * 43 + "="), *(secrets or {}).items()):
            (secrets_dir / name).write_text(value)
            (secrets_dir / name).chmod(0o440)
        # Task 11: the gateway is on the fake LLM transport (this worker has no `anthropic_key`), and the
        # parse root and the CV store are the test's own. Task 23: without `fake_llm`, a worker with neither
        # the key nor the fake (its gateway is not configured).
        self.parse_root = tmp_path / "parse"
        self.parse_root.mkdir()
        fake_env = {"JOBHUNTER_FAKE_LLM": "1", "JOBHUNTER_FAKE_LLM_DIR": str(tmp_path / "anthropic")}
        worker_env = {
            "JOBHUNTER_ENV": "test",
            "JOBHUNTER_DB_NAME": "jobhunter",
            "JOBHUNTER_DB_HOST": "unused",
            **(fake_env if fake_llm else {}),
            "JOBHUNTER_PARSE_JOBS_DIR": str(self.parse_root),
            "JOBHUNTER_CV_STORE_DIR": str(tmp_path / "cv"),
            **(env or {}),
        }
        self.cfg = load_config("worker", worker_env, secrets_dir)


# The executions are cancelled first, then the gateway's tries still in flight settle (`llm_tries`), before
# the watchdog stops and well before the engine and the lock session close (Task 28 fix round 1).
TEARDOWN = [
    "executions_cancel",
    "llm_tries",
    "watchdog_stop",
    "saver_close",
    "queue_close",
    "engine_dispose",
    "lock_close",
]


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


@pytest.mark.parametrize(("owner", "code"), [("worker_epoch", 70), ("worker_lock", 1)])
async def test_a_takeover_fenced_by_a_newer_epoch_exits_70_and_the_claim_s_own_fence_exits_1(
    monkeypatch, tmp_path, caplog, owner, code
) -> None:
    """M3 Task 27: the claim commits before the sweeps, so a newer worker can claim in between and a sweep's
    fence then refuses this worker's epoch: it is superseded, as at its startup sync (exit 70,
    `worker_superseded_at_start`), and nothing after the takeover is built. The claim's own lock check
    (`worker_lock`) stays a failed start (exit 1)."""
    from jobhunter.errors import FencedOut
    from jobhunter.worker import main

    world = World(monkeypatch, tmp_path)

    async def fenced_claim(engine: object, ident: object) -> tuple[int, dict[str, int]]:
        world.events.append("claim")
        raise FencedOut(owner=owner)

    monkeypatch.setattr(main, "claim_epoch_and_sweep", fenced_claim)
    assert await main.worker_main(world.cfg) == code
    assert world.events[world.events.index("claim") :] == [
        "claim",
        "executions_cancel",  # no gateway was built, so no `llm_tries`
        "queue_close",
        "engine_dispose",
        "lock_close",
    ]
    assert len(logged(caplog, "worker_superseded_at_start")) == (code == 70)
    assert [e["exit_code"] for e in logged(caplog, "worker_shutdown_complete")] == [code]


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
    from jobhunter.llm.batches import BatchesClient
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
    # M3 Task 27: one Batches client on the gateway's client, monotonic clock and sleep; the operation
    # dependencies carry the score steps' deps and that client, the handlers build neither themselves
    client = runtime.batches_client
    assert isinstance(client, BatchesClient) and client.configured
    assert (client._http, client._monotonic, client._sleep, client._clock) == (
        runtime.gateway.http_client,
        runtime.gateway.monotonic,
        runtime.gateway.sleep,
        utcnow,
    )
    assert deps.score_steps is runtime.score_deps and deps.batches_client is client


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


# --- M5 Task 4: the alert dispatcher runs beside the queue loops when ntfy is configured -------------------

NTFY_TOPIC = "https://ntfy.test/jh-alerts-4f2a9c"
NTFY_ENV = {"NTFY_TOPIC_URL": NTFY_TOPIC, "JOBHUNTER_PUBLIC_ORIGIN": "https://jobs.example.test"}
NTFY_TOKEN = "tk-test-token"  # a low-entropy stand-in: no secret scanner reads it as a key


def fake_dispatchers(
    monkeypatch, world: World, *, ends_by_itself: bool = False, stops: bool = True
) -> list[Any]:
    """`AlertDispatcher` as `worker_main` builds and runs it; each one made is returned in the list. Its
    `run` records its start and its stop; with `ends_by_itself` it returns at once, as a loop whose fence
    refused the worker's epoch does; without `stops` it ignores its stop event until it is cancelled."""
    from jobhunter.worker import main

    made: list[Any] = []

    class FakeDispatcher:
        def __init__(
            self, engine: object, client: object, *, epoch: int, clock: object, sleep: object
        ) -> None:
            self.engine, self.client, self.epoch, self.clock, self.sleep = engine, client, epoch, clock, sleep
            made.append(self)

        async def run(self, stop: asyncio.Event) -> None:
            world.events.append("dispatcher_run")
            if ends_by_itself:
                return
            try:
                await (stop.wait() if stops else asyncio.get_running_loop().create_future())
            except asyncio.CancelledError:
                world.events.append("dispatcher_cancelled")
                raise
            world.events.append("dispatcher_stop")

    monkeypatch.setattr(main, "AlertDispatcher", FakeDispatcher)
    return made


START = [
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
]


async def test_without_a_topic_url_no_dispatcher_runs_and_ntfy_disabled_is_logged_once(
    monkeypatch, tmp_path, caplog
) -> None:
    from jobhunter.worker.main import worker_main
    from jobhunter.worker.runtime import WorkerRuntime

    world = World(monkeypatch, tmp_path)
    made = fake_dispatchers(monkeypatch, world)
    assert world.cfg.ntfy_topic_url is None
    assert await worker_main(world.cfg) == 0
    assert made == [] and world.events == [*START, *TEARDOWN]
    [runtime, *_] = world.runtimes
    assert isinstance(runtime, WorkerRuntime) and runtime.dispatcher is None
    [event] = logged(caplog, "ntfy_disabled")
    assert event["level"] == "info"


async def test_with_a_topic_url_the_dispatcher_runs_beside_the_loops_and_stops_first(
    monkeypatch, tmp_path, caplog
) -> None:
    from jobhunter.clock import utcnow
    from jobhunter.observability.ntfy import NtfyClient
    from jobhunter.worker.main import worker_main
    from jobhunter.worker.runtime import WorkerRuntime

    world = World(monkeypatch, tmp_path, env=NTFY_ENV, secrets={"ntfy_token": f"{NTFY_TOKEN}\n"})
    made = fake_dispatchers(monkeypatch, world)
    assert await worker_main(world.cfg) == 0
    # step 10 starts it with the loops; the teardown stops it before anything else
    assert world.events == [*START, "dispatcher_run", "dispatcher_stop", *TEARDOWN]
    [dispatcher] = made
    runtime = world.runtimes[0]
    assert isinstance(runtime, WorkerRuntime) and runtime.dispatcher is made[0]
    assert (dispatcher.engine, dispatcher.epoch, dispatcher.clock, dispatcher.sleep) == (
        runtime.engine,
        3,  # the claimed epoch
        utcnow,
        asyncio.sleep,
    )
    client = dispatcher.client
    assert isinstance(client, NtfyClient)
    assert (client._topic_url, client._token, client._public_origin) == (
        NTFY_TOPIC,
        NTFY_TOKEN,
        "https://jobs.example.test",
    )
    assert client._http.headers["User-Agent"] == world.cfg.http_user_agent
    assert client._http.is_closed  # closed by the teardown
    assert logged(caplog, "ntfy_disabled") == []


async def test_without_the_token_the_dispatcher_sends_no_authorization(monkeypatch, tmp_path) -> None:
    from jobhunter.worker.main import worker_main

    world = World(monkeypatch, tmp_path, env=NTFY_ENV, secrets={"ntfy_token": ""})  # empty: absent
    made = fake_dispatchers(monkeypatch, world)
    assert await worker_main(world.cfg) == 0
    [dispatcher] = made
    assert dispatcher.client._token is None


async def test_a_dispatcher_that_ends_by_itself_requests_the_stop_of_a_superseded_worker(
    monkeypatch, tmp_path, caplog
) -> None:
    """Its loop ends only when a fence refused the worker's epoch (Ruling T24-a): the worker then stops
    itself, as a fenced task body does. (It starts before the queue loops, whose fake asks for the stop
    without a log line.)"""
    from jobhunter.worker.main import worker_main

    world = World(monkeypatch, tmp_path, env=NTFY_ENV)
    fake_dispatchers(monkeypatch, world, ends_by_itself=True)
    assert await worker_main(world.cfg) == 0
    assert world.events == [*START, "dispatcher_run", *TEARDOWN]
    [event] = logged(caplog, "shutdown_requested")
    assert (event["level"], event["reason"]) == ("warning", "epoch_superseded")


async def test_a_dispatcher_that_does_not_stop_is_cancelled_within_its_bound(
    monkeypatch, tmp_path, caplog
) -> None:
    """Mid-pass (a backoff sleep or a push) the loop does not see its stop event: the teardown waits at most
    `DISPATCHER_STOP_S`, 1 s of the stop's deadline (Ruling T4-4), then cancels it, and the rest of the
    teardown runs. The wait the step is given is recorded; the test then waits 10 ms of it, not the second."""
    from jobhunter.worker import main
    from jobhunter.worker.shutdown import TEARDOWN_S

    assert main.DISPATCHER_STOP_S == 1.0 and main.DISPATCHER_STOP_S < TEARDOWN_S
    given: list[float] = []
    real = main._WorkerProcess._stop_dispatcher

    async def recorded(process: Any, timeout_s: float, deadline: Any) -> None:
        given.append(timeout_s)
        await real(process, min(timeout_s, 0.01), deadline)

    monkeypatch.setattr(main._WorkerProcess, "_stop_dispatcher", recorded)
    world = World(monkeypatch, tmp_path, env=NTFY_ENV)
    fake_dispatchers(monkeypatch, world, stops=False)
    assert await asyncio.wait_for(main.worker_main(world.cfg), 5) == 0
    assert given == [main.DISPATCHER_STOP_S]  # the full stop budget is left: the bound is the 1 s
    assert world.events == [*START, "dispatcher_run", "dispatcher_cancelled", *TEARDOWN]
    assert logged(caplog, "worker_teardown_failed") == []


# --- M5 Task 5: one dead-man pinger for the run ends, the tick and the sync, when both secrets are set ------

HC_PING_URL = "https://hc-ping.test/00000000-0000-4000-8000-000000000001"  # low-entropy stand-ins
HC_SECRETS = {"healthchecks_ping_url": f"{HC_PING_URL}\n", "healthchecks_api_key": "hc-test-key\n"}


@pytest.mark.parametrize(
    ("secrets", "reason"),
    [
        ({}, "not_configured"),
        ({"healthchecks_ping_url": HC_SECRETS["healthchecks_ping_url"]}, "not_configured"),
        ({"healthchecks_api_key": HC_SECRETS["healthchecks_api_key"]}, "not_configured"),
        (
            {**HC_SECRETS, "healthchecks_ping_url": "https://hc-ping.test/pingkeyxyz/collection"},
            "ping_url_form",
        ),
    ],
)
async def test_without_both_healthchecks_secrets_no_pinger_is_built_and_deadman_disabled_is_logged_once(
    monkeypatch, tmp_path, caplog, secrets: dict[str, str], reason: str
) -> None:
    """O-ops Q8: dev and test have no dead-man, and say so once at start (a slug ping URL, which a prod
    worker's startup refuses, is no dead-man either outside prod)."""
    from jobhunter.graphs.collect.deps import CollectDeps
    from jobhunter.worker.main import worker_main
    from jobhunter.worker.runtime import WorkerRuntime

    world = World(monkeypatch, tmp_path, secrets=secrets)
    assert await worker_main(world.cfg) == 0
    assert world.events == [*START, *TEARDOWN]
    [runtime, *_] = world.runtimes
    [deps] = world.deps
    assert isinstance(runtime, WorkerRuntime) and isinstance(deps, CollectDeps)
    assert runtime.deadman is None and deps.deadman is None
    assert [cast(Any, ctx).deadman for ctx in world.contexts] == [None]  # the start cadence's
    [event] = logged(caplog, "deadman_disabled")
    assert (event["level"], event["reason"]) == ("info", reason)
    assert "hc-ping" not in caplog.text and "hc-test-key" not in caplog.text


async def test_with_both_secrets_one_pinger_serves_the_run_ends_the_tick_and_the_sync(
    monkeypatch, tmp_path, caplog
) -> None:
    from jobhunter.clock import utcnow
    from jobhunter.graphs.collect.deps import CollectDeps
    from jobhunter.observability.deadman import DeadmanPinger, HealthchecksClient
    from jobhunter.worker.main import worker_main
    from jobhunter.worker.runtime import WorkerRuntime

    env = {"HEALTHCHECKS_API_URL": "https://healthchecks.test/api/v3"}
    world = World(monkeypatch, tmp_path, env=env, secrets=HC_SECRETS)
    assert await worker_main(world.cfg) == 0
    assert world.events == [*START, *TEARDOWN]
    [runtime, *_] = world.runtimes
    [deps] = world.deps
    assert isinstance(runtime, WorkerRuntime) and isinstance(runtime.deadman, DeadmanPinger)
    assert isinstance(deps, CollectDeps)
    pinger = runtime.deadman
    # the graph's `finalize` node, `run_collect`'s `_finalize`, the reconcilers and `deadman_sync` share it
    assert deps.deadman is pinger and [cast(Any, ctx).deadman for ctx in world.contexts] == [pinger]
    assert (pinger._engine, pinger._epoch, pinger._clock) == (runtime.engine, 3, utcnow)
    client = pinger._client
    assert isinstance(client, HealthchecksClient)
    assert (client._ping_url, client._api_url, client._api_key) == (
        HC_PING_URL,
        "https://healthchecks.test/api/v3",
        "hc-test-key",
    )
    assert client._http.headers["User-Agent"] == world.cfg.http_user_agent
    assert client._http.is_closed  # closed by the teardown
    assert logged(caplog, "deadman_disabled") == []


async def test_a_start_that_fails_after_the_claim_stops_the_watchdog_and_closes_the_lock(
    monkeypatch, tmp_path, caplog
) -> None:
    from jobhunter.worker.main import worker_main

    world = World(monkeypatch, tmp_path)
    world.open_fails = True
    assert await worker_main(world.cfg) == 1
    # Nothing after the failed step was built; what was built is closed, the lock session last.
    assert world.events[world.events.index("queue_open") + 1 :] == [
        "executions_cancel",  # no gateway was built, so no `llm_tries`
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
    assert world.timeouts == {
        "executions": shutdown.TEARDOWN_S,
        "llm_tries": min(main.TRIES_SETTLE_S, shutdown.TEARDOWN_S - 4.0),  # the tries share what is left
        "watchdog": shutdown.TEARDOWN_S - 4.0,
    }
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
    # M1's, registered by `worker.embedding_reconcile` since M3 Task 15 (M1 Carry-forward 14, Ruling P44)
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
# The M3 reconcilers, final (Task 27): `orphaned_cycles` (Task 11, `scoring.cycles`, with the takeover sweeps
# `score_targets` and `score_cycles`; Ruling P44), `expiry_sweep` and `budget_release` (Task 13,
# `scoring.targets`), `bulk_operations` (Task 14, `llm.bulk`, through `scoring.cycles`) and
# `missing_targets` (Task 14, `scoring.targets`), `batch_submissions` (Task 16, `scoring.batch_operations`
# since Ruling T16-e), and Task 18's four in `scoring.reconcilers`: `stuck_targets`, `det_completeness`,
# `admission_alerts` and `scoring_drift`. (`awaiting_embedding` is M1's, registered by
# `worker.embedding_reconcile` since Task 15.)
M3_RECONCILERS = {
    "orphaned_cycles": ["tick"],
    "stuck_targets": ["tick"],
    "budget_release": ["tick"],
    "bulk_operations": ["heavy"],
    "missing_targets": ["heavy"],
    "det_completeness": ["heavy"],
    "expiry_sweep": ["heavy"],
    "admission_alerts": ["heavy"],
    "scoring_drift": ["heavy"],
    "batch_submissions": ["tick"],
}
# M5 Task 5: the dead-man switch's durable path, registered by `observability.deadman` (O-ops Q6, O10).
M5_RECONCILERS = {"deadman_ping": ["tick"]}
# The modules step 4 imports for M3 (Task 27): each registers what the pin below names, or is listed so that
# the start imports it before the takeover (`scoring.golden`, `llm.batches`: nothing of their own, checked by
# import in `test_the_registering_modules_list_every_m3_module`).
M3_MODULES = (
    "jobhunter.scoring.targets",
    "jobhunter.scoring.cycles",
    "jobhunter.scoring.batches",
    "jobhunter.scoring.batch_operations",
    "jobhunter.scoring.reconcilers",
    "jobhunter.scoring.golden",
    "jobhunter.scoring.eval_runner",
    "jobhunter.llm.batches",
    "jobhunter.worker.embedding_reconcile",
)


def test_the_registering_modules_alone_register_every_reconciler_sweep_and_handler() -> None:
    """The final pin of M3 (Task 27; M2 fix round 1, Minor 5, made it a fresh interpreter): one that imports
    ONLY `worker.main._REGISTERING_MODULES` (step 4, before the takeover; never `worker.main` itself, whose
    imports would register most of it anyway) holds exactly:
    - the reconcilers of M1, M2 and M3 with their cadences (`M3_RECONCILERS` is the milestone's final set),
      and M5's `deadman_ping` (Task 5, `observability.deadman`);
    - the five takeover sweeps (`operations`, `runs`, M3 Task 11's `score_targets` and `score_cycles`, Task
      16's `batch_submissions`), each run in a fenced transaction of its own since Task 27;
    - the handlers `extract_profile_text` (M2 Task 11), `extract_profile` (Task 13), `embed_profile` (Task
      19), `propose_queries` (Task 20), `apply_scope` (Task 18), `watchlist_validate`, and M3's
      `create_targets_for_tuple` (Task 14, `scoring.targets`), `batch_submit` (Task 16,
      `scoring.batch_operations`, Ruling T16-e) and `eval_run` (Task 19, `scoring.eval_runner`), and M4's
      `replay_raw_items` (M4 Task 12, `sources.replay`);
    - the failure hooks of the two profile phases, of the query proposal and of `eval_run` only (written in
      `worker.operations`, Ruling P39; `embed_profile` drives no domain row; `batch_submit`'s cleanup needs
      `active_config`, which its reconciler takes, Codex r3 B2; `create_targets_for_tuple` is re-requested
      by `missing_targets`);
    - the `bulk_operations` reconciler's one active step, Task 16's `rerequest` (Ruling T16-d);
    - the one gauge refresh `metrics_refresh` runs, Task 18's `score_lane`;
    - the execution lock of each kind that has one (`OPERATION_LOCKS`: `batch_submit` under `batch`, Task 14;
      M4's `replay_raw_items` under `replay`, M4 Task 12).
    `embed_profile`, `propose_queries` and the `profile_vectors` reconciler come with `profile.operations`,
    which imports `profile.vectors`; `worker.main` lists neither."""
    from jobhunter.worker.main import _REGISTERING_MODULES

    assert registered_by(_REGISTERING_MODULES) == {
        "reconcilers": {**M1_RECONCILERS, **M2_RECONCILERS, **M3_RECONCILERS, **M5_RECONCILERS},
        "sweeps": ["batch_submissions", "operations", "runs", "score_cycles", "score_targets"],
        "handlers": [
            "apply_scope",
            "batch_submit",
            "create_targets_for_tuple",
            "embed_profile",
            "eval_run",
            "extract_profile",
            "extract_profile_text",
            "propose_queries",
            "replay_raw_items",
            "watchlist_validate",
        ],
        "failure_hooks": ["eval_run", "extract_profile", "extract_profile_text", "propose_queries"],
        "active_steps": ["jobhunter.scoring.batches.rerequest"],
        "gauge_refreshes": ["score_lane"],
        # M3 Task 14 (Codex r3 B20): `defer_operation_job` reads it at every enqueue
        "operation_locks": {"batch_submit": "batch", "replay_raw_items": "replay"},
        "main_imported": False,
    }


def test_the_registering_modules_list_every_m3_module() -> None:
    """M3 Task 27: step 4 imports every M3 module of the plan's list. `scoring.golden` and `llm.batches`
    register nothing of their own (checked by import: what each registers is what its imports bring along),
    so listing them changes nothing of the pin above."""
    from jobhunter.worker.main import _REGISTERING_MODULES

    assert set(M3_MODULES) <= set(_REGISTERING_MODULES)
    for module in ("jobhunter.scoring.golden", "jobhunter.llm.batches"):
        registered = registered_by((module,))
        assert registered["handlers"] == [] and registered["failure_hooks"] == [], module
        assert registered["active_steps"] == [] and registered["gauge_refreshes"] == [], module
        assert registered["sweeps"] in ([], ["operations"]), module  # `worker.operations`', through an import


def test_awaiting_embedding_is_registered_by_the_worker_s_module_alone() -> None:
    """M3 Task 15 (M1 Carry-forward 14, Rulings P15 and P44): the `awaiting_embedding` reconciler lives in
    `worker.embedding_reconcile`, which step 4 lists, so the registered set keeps it; `jobhunter.embeddings`,
    which the api imports, registers it no longer."""
    from jobhunter.worker.main import _REGISTERING_MODULES

    assert "jobhunter.worker.embedding_reconcile" in _REGISTERING_MODULES
    moved = registered_by(("jobhunter.worker.embedding_reconcile",))["reconcilers"]
    assert isinstance(moved, dict) and moved["awaiting_embedding"] == ["heavy"]
    left = registered_by(("jobhunter.embeddings",))["reconcilers"]
    assert isinstance(left, dict) and "awaiting_embedding" not in left
    # Ruling P15, Task 27: the api still registers other reconcilers (only the worker runs them), not this one
    api = registered_by(("jobhunter.api.app",))["reconcilers"]
    assert isinstance(api, dict) and api and "awaiting_embedding" not in api


def test_the_m3_tasks_queues_and_pools_keep_their_pinned_shape() -> None:
    """M3 Task 27: the four M3 tasks on their queues (spec §3.3.2): `score_cycle` under the lock `score`, its
    stalled job failed, never retried (`NO_STALL_RETRY`, Codex r1 #17); the three periodic ones at their
    crons with the queueing lock of their name; the queues' concurrency; and the pools at the spec's sizes
    (§9.1 L3375: the checkpointer 1 to 4, the engine 10, the queue pool 4), which the perf test measures."""
    from jobhunter.graphs import checkpointer
    from jobhunter.worker.main import ENGINE_POOL_SIZE, QUEUE_POOL_MAX_SIZE
    from jobhunter.worker.tasks import NO_STALL_RETRY, QUEUES, _reset_task_app, job_task_name, make_queue_app

    assert dict(QUEUES) == {"collect": 1, "score": 1, "aux": 2, "tick": 1}
    assert NO_STALL_RETRY == frozenset({"score_cycle"})
    assert (ENGINE_POOL_SIZE, QUEUE_POOL_MAX_SIZE) == (10, 4)
    assert (checkpointer._POOL_MIN_SIZE, checkpointer._POOL_MAX_SIZE) == (1, 4)
    _reset_task_app()
    try:
        app = make_queue_app("dbname=unused")
        cycle = app.tasks[job_task_name("score_cycle")]
        assert (cycle.queue, cycle.lock, cycle.queueing_lock) == (
            "score",
            "score",
            None,
        )  # per trigger, at defer
        periodic = {
            name: (task.cron, task.task.queue, task.task.queueing_lock)
            for (name, _), task in app.periodic_registry.periodic_tasks.items()
        }
        assert {
            name: periodic[job_task_name(name)] for name in ("batch_poll", "batch_reconcile_ambiguous")
        } == {
            "batch_poll": ("*/5 * * * *", "aux", "batch_poll"),
            "batch_reconcile_ambiguous": ("*/15 * * * *", "aux", "batch_reconcile_ambiguous"),
        }
        assert periodic[job_task_name("canary_daily")] == ("41 6 * * *", "tick", "canary_daily")
    finally:
        _reset_task_app()
