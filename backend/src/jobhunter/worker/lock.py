"""The worker singleton (spec §3.3.1): a dedicated lock session that holds advisory lock 7261001.

`LockSession` is one autocommit psycopg connection, the *lock session*. The worker holds the singleton lock
on it for its whole life and writes its heartbeat on it, so a heartbeat that succeeds proves that this very
session still holds the lock. Closing the session ends it on the server, which releases the lock.

`acquire_lock` waits for the lock without ever killing a healthy holder: it terminates the holder's lock
session only when the recorded heartbeat is stale and the recorded backend pid is the one that holds the lock;
otherwise it logs `standby` and retries after `STANDBY_SLEEP_S`.

No statement or log line here contains the DSN.
"""

import time
from collections.abc import Callable
from typing import Final
from uuid import UUID

import psycopg
from psycopg.rows import TupleRow

from jobhunter.observability.logging import get_logger

ADVISORY_LOCK_KEY: Final = 7261001
HEARTBEAT_PERIOD_S: Final = 10
HEARTBEAT_STALE_S: Final = 60
STANDBY_SLEEP_S: Final = 10
WATCHDOG_EXIT_CODE: Final = 70

LOCK_APPLICATION_NAME: Final = "jobhunter-worker-lock"
# How long `terminate_stale_holder` waits for the terminated backend to exit (and so to release the lock).
# Below the 5 s statement timeout, so the wait ends with `false` rather than with a cancelled statement.
TERMINATE_WAIT_MS: Final = 3000
HEALTH_CONNECT_TIMEOUT_S: Final = 5

_SET_STATEMENT_TIMEOUT: Final = "SET statement_timeout = '5s'"
_TRY_LOCK: Final = "SELECT pg_try_advisory_lock(7261001)"
# The holder is terminated only when its heartbeat is stale AND the backend pid it recorded is the one that
# holds the lock in this database: neither a fresh holder nor an unrelated backend that reused the pid is hit.
_TERMINATE_STALE_HOLDER: Final = """\
SELECT pg_terminate_backend(h.lock_backend_pid, %(wait_ms)s)
  FROM worker_heartbeat h
  JOIN pg_locks l
    ON l.locktype = 'advisory' AND l.objid = 7261001 AND l.granted AND l.pid = h.lock_backend_pid
   AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
 WHERE h.id = 1 AND h.heartbeat_at < now() - make_interval(secs => %(stale_s)s)"""
# §3.3.1 step 4, verbatim apart from psycopg's parameter markers.
_HEARTBEAT: Final = """\
UPDATE worker_heartbeat SET heartbeat_at = now()
 WHERE id = 1 AND worker_instance_id = %(me)s AND worker_epoch = %(my_epoch)s
RETURNING EXISTS (SELECT 1 FROM pg_locks
                   WHERE locktype='advisory' AND objid=7261001
                     AND pid=pg_backend_pid() AND granted);"""
# Measured by the database's clock, like every other heartbeat comparison.
_HEARTBEAT_FRESH: Final = (
    "SELECT now() - heartbeat_at <= make_interval(secs => %(stale_s)s) FROM worker_heartbeat WHERE id = 1"
)

log = get_logger("jobhunter.worker.lock")


class LockSession:
    """The worker's lock session. Create it with the DSN, then `connect()`; `close()` releases the lock."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: psycopg.Connection[TupleRow] | None = None
        self._backend_pid: int | None = None

    def __repr__(self) -> str:
        # Never the DSN: it carries the password.
        return f"<LockSession backend_pid={self._backend_pid}>"

    @property
    def backend_pid(self) -> int | None:
        """The server pid of the lock session while it is connected, else `None`."""
        return self._backend_pid

    def connect(self) -> int:
        """Open the lock session (keepalives on, 5 s statement timeout) and return its backend pid."""
        if self._conn is not None:
            raise RuntimeError("the lock session is already connected")
        conn = psycopg.connect(
            self._dsn,
            autocommit=True,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
            application_name=LOCK_APPLICATION_NAME,
        )
        try:
            conn.execute(_SET_STATEMENT_TIMEOUT)
            pid = _scalar(conn, "SELECT pg_backend_pid()")
            if not isinstance(pid, int):
                raise TypeError("pg_backend_pid() did not return an integer")
        except BaseException:
            conn.close()
            raise
        self._conn, self._backend_pid = conn, pid
        return pid

    def _connection(self) -> psycopg.Connection[TupleRow]:
        if self._conn is None:
            raise RuntimeError("the lock session is not connected")
        return self._conn

    def try_lock(self) -> bool:
        """`pg_try_advisory_lock(7261001)` on the lock session: `True` when this session holds the lock."""
        return _scalar(self._connection(), _TRY_LOCK) is True

    def terminate_stale_holder(self) -> bool:
        """Terminate the holder's lock session if its heartbeat is stale and its recorded pid holds the lock.

        `True` when the holder's backend was terminated and has exited (its lock is free again); `False`
        when there is no row, the heartbeat is fresh, the recorded pid does not hold the lock, or the backend
        did not exit within `TERMINATE_WAIT_MS`.
        """
        terminated = _scalar(
            self._connection(),
            _TERMINATE_STALE_HOLDER,
            {"wait_ms": TERMINATE_WAIT_MS, "stale_s": HEARTBEAT_STALE_S},
        )
        return terminated is True

    def heartbeat(self, instance_id: UUID, epoch: int) -> bool:
        """The §3.3.1 heartbeat: `True` only when the row is still ours and this session holds the lock.

        `False` when the statement updates no row (another instance or epoch owns it) or returns `false` (the
        lock is not held by this session). A failure or a statement timeout raises.
        """
        return _scalar(self._connection(), _HEARTBEAT, {"me": instance_id, "my_epoch": epoch}) is True

    def close(self) -> None:
        """Close the lock session; the server then ends it, which releases the advisory lock. Idempotent."""
        conn, self._conn, self._backend_pid = self._conn, None, None
        if conn is not None:
            conn.close()


def _scalar(
    conn: psycopg.Connection[TupleRow], statement: str, params: dict[str, object] | None = None
) -> object:
    row = conn.execute(statement, params).fetchone()
    return None if row is None else row[0]


def acquire_lock(session: LockSession, *, sleep: Callable[[float], None] = time.sleep) -> None:
    """Block until `session` holds the singleton lock (§3.3.1 steps 1 and 2).

    A stale holder (heartbeat older than `HEARTBEAT_STALE_S`, recorded pid holding the lock) is terminated and
    the lock is tried again at once. A fresh holder is never terminated: each wait logs `standby` with its
    `attempt` number and sleeps `STANDBY_SLEEP_S`.
    """
    attempt = 0
    while True:
        if session.try_lock():
            log.info("lock_acquired", backend_pid=session.backend_pid, standby_attempts=attempt)
            return
        if session.terminate_stale_holder():
            log.warning("stale_lock_holder_terminated", stale_after_s=HEARTBEAT_STALE_S)
            continue
        attempt += 1
        log.info("standby", attempt=attempt, retry_in_s=STANDBY_SLEEP_S)
        sleep(STANDBY_SLEEP_S)


def heartbeat_is_fresh(dsn: str) -> bool:
    """`True` when `worker_heartbeat.heartbeat_at` is at most `HEARTBEAT_STALE_S` old, by the database clock.

    The read of `jobhunter worker-health`: one statement on a short-lived autocommit connection. No row means
    not fresh.
    """
    with psycopg.connect(
        dsn,
        autocommit=True,
        connect_timeout=HEALTH_CONNECT_TIMEOUT_S,
        application_name="jobhunter-worker-health",
        options="-c statement_timeout=5s",
    ) as conn:
        return _scalar(conn, _HEARTBEAT_FRESH, {"stale_s": HEARTBEAT_STALE_S}) is True
