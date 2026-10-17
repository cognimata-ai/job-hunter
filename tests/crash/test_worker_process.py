import signal
import sys

import psycopg
import pytest

from tests.support.pg import pg_one
from tests.support.procs import wait_until

pytestmark = [pytest.mark.crash, pytest.mark.slow]
WORKER = [sys.executable, "-m", "jobhunter.cli", "worker"]


def one(server, statement: str):
    return pg_one(server, "superuser", statement)  # shared helper (Task 4)


def due_slot_now(server) -> None:
    one(
        server,
        "UPDATE schedule_state SET anchor_utc = date_trunc('minute', now()), interval_s = 3600, "
        "last_evaluated_slot = date_trunc('minute', now()) - interval '1 hour'",
    )


def test_idle_sigterm_exits_zero_and_releases_lock(migrated, proc_env, procs) -> None:
    w = procs.spawn(WORKER, proc_env("worker", migrated))
    w.wait_event("worker_ready", 60)
    w.signal(signal.SIGTERM)
    assert w.exit_within(30) == 0
    w.wait_event("worker_shutdown_complete", 1)
    assert one(migrated, "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND objid=7261001") == (0,)


def test_busy_sigterm_drains_inflight_run(migrated, proc_env, procs) -> None:
    w = procs.spawn(WORKER, proc_env("worker", migrated, JOBHUNTER_FAULT="collect.before_finalize:sleep=5"))
    w.wait_event("worker_ready", 60)
    due_slot_now(migrated)
    w.wait_event("fault_point_reached", 120, point="collect.before_finalize")
    w.signal(signal.SIGTERM)
    assert w.exit_within(60) == 0
    assert one(
        migrated, "SELECT status FROM runs WHERE scheduled_slot IS NOT NULL ORDER BY id DESC LIMIT 1"
    ) == ("succeeded",)


def test_c17_zombie_worker_writes_nothing(migrated, proc_env, procs) -> None:
    a = procs.spawn(WORKER, proc_env("worker", migrated, JOBHUNTER_FAULT="collect.before_finalize:pause"))
    a.wait_event("worker_ready", 60)
    due_slot_now(migrated)
    a.wait_event("fault_point_reached", 120, point="collect.before_finalize")
    wait_until(a.is_stopped, 10)
    one(migrated, "UPDATE worker_heartbeat SET heartbeat_at = now() - interval '2 minutes'")
    one(migrated, "UPDATE procrastinate_workers SET last_heartbeat = now() - interval '5 minutes'")
    b = procs.spawn(WORKER, proc_env("worker", migrated))
    b.wait_event("worker_ready", 60)
    wait_until(
        lambda: (
            one(migrated, "SELECT status, attempt_no FROM runs WHERE scheduled_slot IS NOT NULL")
            == ("succeeded", 2)
        ),
        120,
    )
    a.signal(signal.SIGCONT)
    assert a.exit_within(20) == 70
    assert one(
        migrated,
        "SELECT h.pid, h.lock_backend_pid = l.pid FROM worker_heartbeat h JOIN pg_locks l "
        "ON l.locktype = 'advisory' AND l.objid = 7261001 AND l.granted",
    ) == (b.pid, True)  # Review Focus 1: the replacement holds the lock
    assert one(migrated, "SELECT end_reason FROM run_attempts WHERE attempt_no=1") == ("worker_takeover",)
    assert one(migrated, "SELECT status, attempt_no FROM runs WHERE scheduled_slot IS NOT NULL") == (
        "succeeded",
        2,
    )


def test_c27_killed_worker_job_released_and_run_resumes(migrated, proc_env, procs) -> None:
    a = procs.spawn(WORKER, proc_env("worker", migrated, JOBHUNTER_FAULT="collect.before_finalize:pause"))
    a.wait_event("worker_ready", 60)
    due_slot_now(migrated)
    a.wait_event("fault_point_reached", 120, point="collect.before_finalize")
    wait_until(a.is_stopped, 10)
    a.signal(signal.SIGKILL)
    a.exit_within(10)
    one(migrated, "UPDATE procrastinate_workers SET last_heartbeat = now() - interval '5 minutes'")
    b = procs.spawn(WORKER, proc_env("worker", migrated))
    b.wait_event("worker_ready", 60)
    wait_until(
        lambda: (
            one(migrated, "SELECT status, attempt_no FROM runs WHERE scheduled_slot IS NOT NULL")
            == ("succeeded", 2)
        ),
        120,
    )
    # Eventually, not at once: procrastinate marks the replacement's job done just after the run's commit, and
    # the worker's minute tick can be `doing` at any instant.
    wait_until(
        lambda: one(migrated, "SELECT count(*) FROM procrastinate_jobs WHERE status='doing'") == (0,), 10
    )


# --- Ruling T13-b: a takeover blocked behind a frozen holder's row lock ends, it never waits forever --------


def test_takeover_blocked_by_a_row_lock_exits_one_and_releases_the_lock(migrated, proc_env, procs) -> None:
    one(
        migrated,
        "INSERT INTO worker_heartbeat (id, worker_instance_id, worker_epoch, hostname, pid, "
        "lock_backend_pid, app_version, started_at, heartbeat_at) "
        "VALUES (1, gen_random_uuid(), 7, 'h', 1, 1, 'v', now(), now())",
    )
    # Like a frozen holder's pooled connection: the heartbeat row stays locked in an open transaction.
    with psycopg.connect(migrated.dsn("superuser")) as holder:
        holder.execute("SELECT 1 FROM worker_heartbeat WHERE id = 1 FOR UPDATE")
        w = procs.spawn(WORKER, proc_env("worker", migrated))
        w.wait_event("takeover_blocked", 60, error_class="OperationalError")
        assert w.exit_within(30) == 1
        assert one(migrated, "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND objid=7261001") == (
            0,
        )
        assert one(migrated, "SELECT worker_epoch FROM worker_heartbeat") == (7,)  # the claim was rolled back
    output = w.output()
    assert "worker_ready" not in output
    assert "lock timeout" not in output  # the error class only, never the server's message
