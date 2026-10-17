import asyncio
import uuid
from typing import NoReturn

import pytest
from jobhunter.worker.main import exit_unless_lock_held

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

    def __init__(self, monkeypatch, tmp_path) -> None:
        from contextlib import asynccontextmanager

        from jobhunter.config import load_config
        from jobhunter.worker import main

        events: list[str] = []
        self.events = events
        self.open_fails = False
        self.dispose_fails = False
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

        class FakeEngine:
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
                if options["name"] == "jh-tick":  # this loop asks for the shutdown, as a SIGTERM would
                    options["additional_context"]["runtime"].shutdown.requested.set()  # type: ignore[index]
                await asyncio.get_running_loop().create_future()  # runs until it is cancelled

        @asynccontextmanager
        async def fake_open_saver(dsn: str):
            events.append("saver_open")
            try:
                yield object()
            finally:
                events.append("saver_close")

        async def fake_claim(engine: object, ident: object) -> tuple[int, dict[str, int]]:
            events.append("claim")
            return 3, {}

        async def fake_reconcilers(ctx: object, cadence: str) -> dict[str, int]:
            events.append(f"reconcile_{cadence}")
            return {}

        for name, fake in {
            "LockSession": FakeLockSession,
            "acquire_lock": lambda session: events.append("lock_acquire"),
            "make_engine": lambda cfg, pool_size: FakeEngine(),
            "make_queue_app": lambda dsn, max_size: FakeQueue(),
            "claim_epoch_and_sweep": fake_claim,
            "Watchdog": FakeWatchdog,
            "open_saver": fake_open_saver,
            "build_noop_collect_graph": lambda saver: object(),
            "run_reconcilers": fake_reconcilers,
        }.items():
            monkeypatch.setattr(main, name, fake)
        secrets = tmp_path / "secrets"
        secrets.mkdir()
        for name, value in (("db_app", "pw"), ("data_key", "a" * 43 + "=")):
            (secrets / name).write_text(value)
            (secrets / name).chmod(0o440)
        env = {"JOBHUNTER_ENV": "test", "JOBHUNTER_DB_NAME": "jobhunter", "JOBHUNTER_DB_HOST": "unused"}
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
        "reconcile_start",
        *TEARDOWN,
    ]
    assert [e["exit_code"] for e in logged(caplog, "worker_shutdown_complete")] == [0]


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
