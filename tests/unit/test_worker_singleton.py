"""The decision logic of `acquire_lock`, the `Watchdog` and `worker-health`, without a database."""

import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from jobhunter.config import ENV_PREFIXES
from jobhunter.worker import epoch, lock
from jobhunter.worker.lock import STANDBY_SLEEP_S, WATCHDOG_EXIT_CODE, LockSession, acquire_lock
from jobhunter.worker.watchdog import Watchdog
from structlog.testing import capture_logs


class ScriptedLock(LockSession):
    """`try_lock` and `terminate_stale_holder` answer from scripts; the call order is recorded."""

    def __init__(self, locks: list[bool], terminations: list[bool]) -> None:
        super().__init__("postgresql://never-connected")
        self.locks, self.terminations, self.calls = locks, terminations, list[str]()

    def try_lock(self) -> bool:
        self.calls.append("try")
        return self.locks.pop(0)

    def terminate_stale_holder(self) -> bool:
        self.calls.append("terminate")
        return self.terminations.pop(0)


class ScriptedHeartbeat(LockSession):
    def __init__(self, results: list[bool | Exception]) -> None:
        super().__init__("postgresql://never-connected")
        self.results = results

    def heartbeat(self, instance_id: uuid.UUID, epoch: int) -> bool:
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_the_lock_statements_use_the_lock_key() -> None:
    # The statements keep the spec's literal key (§3.3.1 is quoted verbatim); this ties them to the constant.
    for statement in (lock._TRY_LOCK, lock._TERMINATE_STALE_HOLDER, lock._HEARTBEAT, epoch._HOLDS_LOCK):
        assert re.findall(r"\b\d{7}\b", statement) == [str(lock.ADVISORY_LOCK_KEY)]


def test_the_lock_session_connects_with_keepalives_and_a_tcp_user_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class Connected(Exception):
        pass

    def connect(dsn: str, **kwargs: object) -> None:
        seen.update(kwargs)
        raise Connected  # the kwargs are all this test needs

    monkeypatch.setattr(psycopg, "connect", connect)
    with pytest.raises(Connected):
        LockSession("postgresql://never-connected").connect()
    assert seen == {
        "autocommit": True,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
        "tcp_user_timeout": 20000,
        "application_name": "jobhunter-worker-lock",
    }


def test_acquire_lock_retries_at_once_after_a_termination_and_sleeps_per_standby() -> None:
    session = ScriptedLock(locks=[False, False, False, True], terminations=[False, True, False])
    sleeps: list[float] = []
    with capture_logs() as logs:
        acquire_lock(session, sleep=sleeps.append)
    assert session.calls == ["try", "terminate", "try", "terminate", "try", "terminate", "try"]
    assert sleeps == [STANDBY_SLEEP_S, STANDBY_SLEEP_S]  # none after the termination
    assert [e["attempt"] for e in logs if e["event"] == "standby"] == [1, 2]


def test_watchdog_exits_70_when_the_heartbeat_reports_the_lock_lost() -> None:
    exits: list[int] = []
    session = ScriptedHeartbeat([True, True, False])
    w = Watchdog(session, uuid.uuid4(), 3, period_s=0.001, exit=exits.append)  # type: ignore[arg-type]
    with capture_logs() as logs:
        w.start()
        w.join(5)
    assert not w.is_alive() and exits == [WATCHDOG_EXIT_CODE] and session.results == []
    assert [e["event"] for e in logs] == ["watchdog_lock_lost"] and logs[0]["epoch"] == 3


def test_watchdog_exits_70_on_a_heartbeat_error_and_logs_only_its_class() -> None:
    exits: list[int] = []
    session = ScriptedHeartbeat([True, psycopg.OperationalError("server at 10.0.0.9 said no")])
    w = Watchdog(session, uuid.uuid4(), 1, period_s=0.001, exit=exits.append)  # type: ignore[arg-type]
    with capture_logs() as logs:
        w.start()
        w.join(5)
    assert not w.is_alive() and exits == [WATCHDOG_EXIT_CODE]
    assert [(e["event"], e["error_class"]) for e in logs] == [
        ("watchdog_heartbeat_failed", "OperationalError")
    ]
    assert "10.0.0.9" not in repr(logs)


def test_watchdog_stop_before_start_is_harmless() -> None:
    exits: list[int] = []
    w = Watchdog(ScriptedHeartbeat([]), uuid.uuid4(), 1, period_s=0.001, exit=exits.append)  # type: ignore[arg-type]
    w.stop(timeout_s=0)  # the worker's teardown may stop a watchdog it never started
    w.start()
    w.join(5)  # it sees the stop event at once and never heartbeats (an empty script would raise: exit 70)
    assert not w.is_alive() and exits == []


@pytest.mark.parametrize(
    ("env", "line"),
    [
        ({"JOBHUNTER_DB_HOST": "127.0.0.1", "JOBHUNTER_DB_PORT": "1"}, "worker-health: OperationalError\n"),
        ({"JOBHUNTER_ENV": "staging"}, "worker-health: StartupCheckFailed\n"),
    ],
)
def test_worker_health_failure_prints_one_line_naming_the_error_class(
    tmp_path: Path, env: dict[str, str], line: str
) -> None:
    (tmp_path / "db_app").write_text("the-db-app-password\n")
    inherited = {k: v for k, v in os.environ.items() if not k.startswith(ENV_PREFIXES)}
    base = {"JOBHUNTER_ENV": "test", "JOBHUNTER_DB_NAME": "jobhunter", "JOBHUNTER_SECRETS_DIR": str(tmp_path)}
    done = subprocess.run(
        [sys.executable, "-m", "jobhunter.cli", "worker-health"],
        env={**inherited, **base, **env},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert (done.returncode, done.stdout, done.stderr) == (1, "", line)
