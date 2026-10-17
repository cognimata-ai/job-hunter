"""M0 carry-forward 5: the teardown never closes the lock session under a watchdog that is still alive.

A watchdog whose `stop()` returned with its thread alive timed out joining it: the thread is inside a
heartbeat on the lock session itself. The teardown then leaves the session open, and the process exit drops
the socket and so the lock.
"""

from pathlib import Path
from typing import cast

import pytest
from jobhunter.config import load_config
from jobhunter.worker import main
from jobhunter.worker.lock import LockSession
from jobhunter.worker.watchdog import Watchdog

from tests.support.logs import logged


class FakeLockSession:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def close(self) -> None:
        self.events.append("lock_close")


class FakeWatchdog:
    """A watchdog whose thread is still alive after `stop()`, or not, as the test says."""

    def __init__(self, events: list[str], *, alive_after_stop: bool) -> None:
        self.events = events
        self.alive_after_stop = alive_after_stop

    def stop(self, timeout_s: float = 15.0) -> None:
        self.events.append("watchdog_stop")

    def is_alive(self) -> bool:
        return self.alive_after_stop


def worker_process(tmp_path: Path, events: list[str], *, alive_after_stop: bool) -> main._WorkerProcess:
    """A `_WorkerProcess` whose start built the lock session and the watchdog, and nothing else."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for name, value in (("db_app", "pw"), ("data_key", "a" * 43 + "=")):
        (secrets / name).write_text(value)
        (secrets / name).chmod(0o440)
    env = {"JOBHUNTER_ENV": "test", "JOBHUNTER_DB_NAME": "jobhunter", "JOBHUNTER_DB_HOST": "unused"}
    process = main._WorkerProcess(load_config("worker", env, secrets))
    process.lock_session = cast("LockSession", FakeLockSession(events))
    process.watchdog = cast("Watchdog", FakeWatchdog(events, alive_after_stop=alive_after_stop))
    return process


@pytest.mark.parametrize("alive_after_stop", [True, False])
async def test_lock_session_is_not_closed_while_the_watchdog_is_alive(
    tmp_path, caplog, alive_after_stop
) -> None:
    events: list[str] = []
    process = worker_process(tmp_path, events, alive_after_stop=alive_after_stop)
    assert await process.teardown() is True  # it returns, and leaving the session open is no failed step
    if alive_after_stop:
        assert events == ["watchdog_stop"]  # `close` was not called
        assert len(logged(caplog, "lock_session_left_open")) == 1
    else:
        assert events == ["watchdog_stop", "lock_close"]  # closed, and last
        assert logged(caplog, "lock_session_left_open") == []
