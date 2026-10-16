"""Shared pytest fixtures; later tasks add their fixtures here."""

import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Final, Protocol

import procrastinate
import psycopg
import pytest
import structlog
from jobhunter.config import AppConfig, Role
from jobhunter.db.migrate import run_migrate
from jobhunter.db.seed import anchor_utc_for, latest_slot_at_or_before
from jobhunter.db.session import make_engine
from jobhunter.observability import redact
from jobhunter.types import get_int, get_str
from jobhunter.worker.lock import ADVISORY_LOCK_KEY, LockSession, acquire_lock
from jobhunter.worker.tasks import make_queue_app
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.pg import PgServer, config_for, pg_one, process_env, remove_pg_server, start_pg_server
from tests.support.procs import ProcessGroup, wait_until

_QUIET_LOGGERS = ("httpx", "httpcore")
# Every table a test can write to (the migrator owns them, so only the migrator may truncate). The seeded
# configuration chain, `active_config` and `ranking_state` are kept, `schedule_state` is reset (below), and so
# is `checkpoint_migrations`: the checkpointer reads it to know its schema is set up.
_TRUNCATE: Final = (
    "TRUNCATE runs, run_attempts, operations, sessions, login_attempts, login_lockouts, api_idempotency, "
    "alerts, purge_fences, worker_heartbeat, users, procrastinate_jobs, procrastinate_periodic_defers, "
    "procrastinate_workers, checkpoints, checkpoint_writes, checkpoint_blobs CASCADE"
)


@pytest.fixture(autouse=True)
def _restore_logging_state() -> Iterator[None]:
    """`configure_logging` changes process-wide state; give every test the state it found."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    quiet_levels = {name: logging.getLogger(name).level for name in _QUIET_LOGGERS}
    structlog_config = structlog.get_config()
    bound_context = structlog.contextvars.get_contextvars()
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, quiet_level in quiet_levels.items():
        logging.getLogger(name).setLevel(quiet_level)
    structlog.configure(**structlog_config)
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(**bound_context)
    redact._reset_secret_values()


class ConfigFactory(Protocol):
    def __call__(self, role: Role, server: PgServer | None = None, **env: str) -> AppConfig: ...


class ProcessEnvFactory(Protocol):
    def __call__(self, role: Role, server: PgServer | None = None, **extra: str) -> dict[str, str]: ...


@pytest.fixture(scope="session")
def pg_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[PgServer]:
    """One initialised PostgreSQL container for the whole session; removed at the end even if tests failed."""
    server = start_pg_server(tmp_path_factory.mktemp("pg"))
    try:
        yield server
    finally:
        remove_pg_server(server)


@pytest.fixture(scope="session")
def migrated(pg_server: PgServer) -> PgServer:
    """`pg_server` after `jobhunter migrate`: schema at the code head, queue and checkpoint tables, seeds."""
    run_migrate(config_for(pg_server, "migrate"))
    return pg_server


@pytest.fixture
def fresh_pg_server(tmp_path: Path) -> Iterator[PgServer]:
    """A PostgreSQL container of its own, with a clean database, for one test."""
    server = start_pg_server(tmp_path / "pg")
    try:
        yield server
    finally:
        remove_pg_server(server)


@pytest.fixture
def make_config(request: pytest.FixtureRequest) -> ConfigFactory:
    """`make_config(role, server=None, **env)`: the `AppConfig` of `role`, by default against `pg_server`."""

    def make(role: Role, server: PgServer | None = None, **env: str) -> AppConfig:
        # Resolved only when no server is given, so a test with its own server does not start the shared one.
        return config_for(server or request.getfixturevalue("pg_server"), role, **env)

    return make


@pytest.fixture
def proc_env(request: pytest.FixtureRequest) -> ProcessEnvFactory:
    """`proc_env(role, server=None, **extra)`: the environment of a child process of `role`."""

    def make(role: Role, server: PgServer | None = None, **extra: str) -> dict[str, str]:
        return process_env(server or request.getfixturevalue("pg_server"), role, **extra)

    return make


# How long `reset_database` waits for a lock before it fails. A healthy reset never waits.
LOCK_TIMEOUT: Final = "10s"
_SCHEDULE_RESET: Final = """\
INSERT INTO schedule_state (id, anchor_utc, interval_s, enabled, last_evaluated_slot, settings_version_id)
SELECT 1, %(anchor)s, %(interval_s)s, true, %(slot)s, id FROM settings_versions WHERE version_no = 1
ON CONFLICT (id) DO UPDATE SET
  anchor_utc = EXCLUDED.anchor_utc, interval_s = EXCLUDED.interval_s, enabled = true,
  last_evaluated_slot = EXCLUDED.last_evaluated_slot, settings_version_id = EXCLUDED.settings_version_id,
  updated_at = now()"""


def reset_database(server: PgServer, *, lock_timeout: str = LOCK_TIMEOUT) -> None:
    """Return the tables of `_TRUNCATE` to empty and `schedule_state` to its seeded row, in one transaction.

    The TRUNCATE waits for the locks of any transaction a test leaked; after `lock_timeout` it fails with
    SQLSTATE 55P03 instead of hanging the suite.

    The schedule keeps the interval and the anchor that `migrate` seeded: both come from the stored settings
    version 1 (the anchor on the day that version was created, as the seed computes it), not from constants
    here. Its `last_evaluated_slot` is the latest slot at or before the database's `now()`, and it is enabled.
    """
    # The block commits when it ends and closes the connection.
    with psycopg.connect(server.dsn("migrator")) as conn:
        conn.execute("SELECT set_config('lock_timeout', %s, true)", (lock_timeout,))
        conn.execute(_TRUNCATE)
        stored = conn.execute(
            "SELECT payload, created_at FROM settings_versions WHERE version_no = 1"
        ).fetchone()
        db_now = conn.execute("SELECT now()").fetchone()
        if stored is None or db_now is None:
            raise LookupError("settings version 1 is missing: `migrated` seeds it")
        payload, created_at = stored
        interval_s = get_int(payload, "schedule.interval_hours") * 3600
        anchor = anchor_utc_for(
            created_at, get_str(payload, "schedule.anchor_local"), get_str(payload, "display.timezone")
        )
        slot = latest_slot_at_or_before(anchor, interval_s, db_now[0])
        conn.execute(_SCHEDULE_RESET, {"anchor": anchor, "interval_s": interval_s, "slot": slot})


@pytest.fixture
def clean_db(migrated: PgServer) -> Iterator[None]:
    """The shared database with no test data in it, before the test and again after it.

    Sync, on a psycopg connection, so async tests and sync ones (the hypothesis property) share it. In
    `tests/integration` and `tests/crash` it applies by itself to every test that uses `migrated`.
    """
    reset_database(migrated)
    yield
    reset_database(migrated)


@pytest.fixture
async def engine(migrated: PgServer, make_config: ConfigFactory) -> AsyncIterator[AsyncEngine]:
    """A pooled engine as the application database role against the shared server; disposed after the test."""
    app_engine = make_engine(make_config("api", migrated))
    try:
        yield app_engine
    finally:
        await app_engine.dispose()


@pytest.fixture
def procs() -> Iterator[ProcessGroup]:
    """Spawn tracked children with `procs.spawn(args, env)`.

    After the test, also a failed one, every child still alive gets `SIGCONT` and then `SIGKILL` and is waited
    for, and every pipe is closed: no child, stopped or not, outlives its test.
    """
    group = ProcessGroup()
    try:
        yield group
    finally:
        group.close()


_SINGLETON_LOCK_HELD: Final = (
    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND objid = 7261001 AND granted)"
)
# How long `worker_lock` waits for the singleton lock to be free first. The server ends a closed session
# asynchronously, so a lock session that an earlier test closed can hold the lock for a few milliseconds more.
LOCK_RELEASE_WAIT_S: Final = 5.0


def _refuse_standby(seconds: float) -> None:
    raise RuntimeError(
        f"worker_lock: advisory lock {ADVISORY_LOCK_KEY} is held by another session (the fixture never "
        "stands by)"
    )


@pytest.fixture
def worker_lock(migrated: PgServer, make_config: ConfigFactory) -> Iterator[LockSession]:
    """A connected worker `LockSession` that holds the singleton lock (advisory key 7261001) during the test.

    A lock-proving epoch claim (ruling T8-h) uses its pid: `WorkerIdentity(uuid4(), "h", n,
    worker_lock.backend_pid, "v")`. It waits up to `LOCK_RELEASE_WAIT_S` for the lock to be free, then
    acquires it with a `sleep` that raises: a lock that another session still holds fails the test at once
    instead of standing by. Closed on teardown, also after a failure; closing it earlier is allowed.
    """
    session = LockSession(make_config("worker", migrated).dsn("psycopg"))
    try:
        try:
            wait_until(
                lambda: pg_one(migrated, "app", _SINGLETON_LOCK_HELD) == (False,), LOCK_RELEASE_WAIT_S, 0.02
            )
        except TimeoutError:
            pytest.fail(
                f"advisory lock {ADVISORY_LOCK_KEY} still held after {LOCK_RELEASE_WAIT_S} s: an earlier "
                "test leaked a lock holder",
                pytrace=False,
            )
        session.connect()
        acquire_lock(session, sleep=_refuse_standby)
        yield session
    finally:
        session.close()


@pytest.fixture
async def queue_app(migrated: PgServer, make_config: ConfigFactory) -> AsyncIterator[procrastinate.App]:
    """The task app of this process (`make_queue_app`) against the shared server, open for the test.

    `defer_in_tx` and the stalled-job queries work on it; its pool is closed after the test.
    """
    app = make_queue_app(make_config("worker", migrated).dsn("psycopg"))
    async with app.open_async():
        yield app
