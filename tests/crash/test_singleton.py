import signal
import subprocess
import sys

import pytest

from tests.support.pg import pg_one
from tests.support.procs import wait_until

pytestmark = [pytest.mark.crash, pytest.mark.slow]
HOLDER = [sys.executable, "-m", "tests.crash.lock_holder"]


def sql1(server, statement: str, *args: object):
    return pg_one(server, "superuser", statement, *args)  # shared helper (Task 4)


def test_second_holder_stays_standby_while_fresh(migrated, proc_env, procs) -> None:
    a = procs.spawn(HOLDER, proc_env("worker", migrated))
    ready = a.wait_event("holder_ready", 20)
    b = procs.spawn(HOLDER, proc_env("worker", migrated))
    b.wait_event("standby", 15)
    b.wait_event("standby", 15, attempt=2)  # two full standby cycles
    assert sql1(migrated, "SELECT worker_instance_id::text FROM worker_heartbeat") == (ready["instance_id"],)
    assert a.exit_within(0.1) is None  # a is still alive


def test_takeover_terminates_stale_holder(migrated, proc_env, procs) -> None:
    a = procs.spawn(HOLDER, proc_env("worker", migrated))
    ra = a.wait_event("holder_ready", 20)
    a.signal(signal.SIGSTOP)
    wait_until(a.is_stopped, 5)
    sql1(migrated, "UPDATE worker_heartbeat SET heartbeat_at = now() - interval '2 minutes'")
    b = procs.spawn(HOLDER, proc_env("worker", migrated))
    rb = b.wait_event("holder_ready", 30)
    assert rb["epoch"] == ra["epoch"] + 1
    assert sql1(
        migrated, "SELECT pid FROM pg_locks WHERE locktype='advisory' AND objid=7261001 AND granted"
    ) == (rb["lock_pid"],)
    a.signal(signal.SIGCONT)
    assert a.exit_within(15) == 70


def test_c18_watchdog_exits_70_when_lock_session_dies(migrated, proc_env, procs) -> None:
    a = procs.spawn(HOLDER, proc_env("worker", migrated))
    ready = a.wait_event("holder_ready", 20)
    sql1(migrated, "SELECT pg_terminate_backend(%s)", ready["lock_pid"])
    assert a.exit_within(12) == 70


def test_watchdog_exits_on_db_restart(
    fresh_pg_server, make_config, proc_env, procs
) -> None:  # Review Focus 1
    from jobhunter.db.migrate import run_migrate

    from tests.support.pg import restart_pg_server

    run_migrate(make_config("migrate", fresh_pg_server))
    a = procs.spawn(HOLDER, proc_env("worker", fresh_pg_server))
    a.wait_event("holder_ready", 20)
    restart_pg_server(fresh_pg_server)
    assert a.exit_within(15) == 70


def test_no_termination_when_pid_mismatch(migrated, proc_env, procs) -> None:
    a = procs.spawn(HOLDER, proc_env("worker", migrated))
    ra = a.wait_event("holder_ready", 20)
    a.signal(signal.SIGSTOP)
    wait_until(a.is_stopped, 5)
    sql1(
        migrated,
        "UPDATE worker_heartbeat SET heartbeat_at = now() - interval '2 minutes', lock_backend_pid = 1",
    )
    b = procs.spawn(HOLDER, proc_env("worker", migrated))
    b.wait_event("standby", 15)
    b.wait_event("standby", 15, attempt=2)
    assert sql1(
        migrated, "SELECT pid FROM pg_locks WHERE locktype='advisory' AND objid=7261001 AND granted"
    ) == (ra["lock_pid"],)


def test_worker_health_cli(migrated, proc_env, procs) -> None:
    env = proc_env("worker", migrated)
    a = procs.spawn(HOLDER, env)
    a.wait_event("holder_ready", 20)
    assert (
        subprocess.run([sys.executable, "-m", "jobhunter.cli", "worker-health"], env={**env}).returncode == 0
    )
    a.signal(signal.SIGSTOP)
    wait_until(a.is_stopped, 5)
    sql1(migrated, "UPDATE worker_heartbeat SET heartbeat_at = now() - interval '61 seconds'")
    assert (
        subprocess.run([sys.executable, "-m", "jobhunter.cli", "worker-health"], env={**env}).returncode == 1
    )


def test_watchdog_stop_never_exits(migrated, make_config) -> None:
    import threading
    import uuid

    from jobhunter.worker.lock import LockSession, acquire_lock
    from jobhunter.worker.watchdog import Watchdog

    class GatedSession(LockSession):  # holds the first heartbeat open until the test has called stop()
        entered, release = threading.Event(), threading.Event()

        def heartbeat(self, instance_id, epoch) -> bool:
            self.entered.set()
            self.release.wait(10)
            # random instance id: False, which would mean exit(70)
            return super().heartbeat(instance_id, epoch)

    s = GatedSession(make_config("worker").dsn("psycopg"))
    s.connect()
    acquire_lock(s)
    exits: list[int] = []
    w = Watchdog(s, uuid.uuid4(), 1, period_s=0.1, exit=lambda code: exits.append(code))  # type: ignore[arg-type,return-value]
    w.start()
    assert s.entered.wait(5)  # readiness: a heartbeat is in flight
    w.stop(timeout_s=0)  # stop() lands before the failing heartbeat returns
    s.release.set()
    w.join(5)
    s.close()
    assert not w.is_alive() and exits == [] and w.daemon
