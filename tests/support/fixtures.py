"""Shared pytest fixtures; later tasks add their fixtures here."""

import io
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any, Final, Protocol

import httpx
import procrastinate
import psycopg
import pytest
import sqlalchemy as sa
import structlog
from jobhunter.api.app import JobHunterApp, create_app
from jobhunter.auth import users
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig, Role
from jobhunter.db.migrate import run_migrate
from jobhunter.db.seed import DEFAULT_INSTANCES_SQL, anchor_utc_for, latest_slot_at_or_before
from jobhunter.db.session import make_engine
from jobhunter.llm.prompts.render import prompt_schema_hash, prompt_schema_v1
from jobhunter.observability import redact
from jobhunter.observability.logging import configure_logging
from jobhunter.types import get_int, get_str
from jobhunter.worker.lock import ADVISORY_LOCK_KEY, LockSession, acquire_lock
from jobhunter.worker.tasks import make_queue_app
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.embedder import FakeEmbedder
from tests.support.pg import PgServer, config_for, pg_one, process_env, remove_pg_server, start_pg_server
from tests.support.procs import ProcessGroup, wait_until

_QUIET_LOGGERS = ("httpx", "httpcore")
# `configure_logging` also holds these (`observability/logging._CONTENT_LOGGERS`, `_BODY_QUOTING_LOGGERS`,
# `_ERROR_QUOTING_LOGGERS`).
_LIBRARY_LOGGERS = (
    *_QUIET_LOGGERS,
    "anthropic",
    "httpx2",
    "httpcore2",
    "python_multipart",
    "langgraph.pregel._retry",
)
# Every table a test can write to (the migrator owns them, so only the migrator may truncate). The seeded
# configuration chain, `active_config`, `ranking_state` and `sources` are kept (`reset_database` restores the
# first three to their seeded state), `schedule_state` is reset (below), and so is `checkpoint_migrations`:
# the checkpointer reads it to know its schema is set up. `source_instances` is named on purpose: CASCADE
# empties it anyway with `watchlist_companies` and `ats_snapshots`, so `reset_database` inserts the default
# instances again.
_TRUNCATE: Final = (
    "TRUNCATE runs, run_attempts, operations, sessions, login_attempts, login_lockouts, api_idempotency, "
    "alerts, purge_fences, worker_heartbeat, users, procrastinate_jobs, procrastinate_periodic_defers, "
    "procrastinate_workers, checkpoints, checkpoint_writes, checkpoint_blobs, "
    "watchlist_companies, source_instances, queries, coverage_cells, coverage_intervals, fetch_requests, "
    "raw_responses, raw_items, ats_snapshots, ats_snapshot_members, ats_snapshot_absences, quota_windows, "
    "quota_ledger, postings, posting_observations, jobs, job_revisions, job_revision_occurrences, "
    "job_revision_sources, ad_keys, ad_key_sources, merge_events, identity_conflicts, duplicate_groups, "
    "duplicate_group_members, dedup_decisions, job_tombstones, tombstone_keys, embeddings, "
    "hard_filter_results CASCADE"
)


@pytest.fixture(autouse=True)
def _restore_logging_state() -> Iterator[None]:
    """`configure_logging` changes process-wide state; give every test the state it found."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    libraries = {name: logging.getLogger(name) for name in _LIBRARY_LOGGERS}
    library_state = {name: (lg.level, list(lg.handlers), lg.propagate) for name, lg in libraries.items()}
    structlog_config = structlog.get_config()
    bound_context = structlog.contextvars.get_contextvars()
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, (library_level, library_handlers, propagate) in library_state.items():
        libraries[name].setLevel(library_level)
        libraries[name].handlers[:] = library_handlers
        libraries[name].propagate = propagate
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


def _fake_embedder_url(request: pytest.FixtureRequest) -> str:
    """The base URL of the session's fake embedder (started on first use)."""
    server: FakeEmbedder = request.getfixturevalue("fake_embedder_server")
    return server.base_url


@pytest.fixture
def make_config(request: pytest.FixtureRequest) -> ConfigFactory:
    """`make_config(role, server=None, **env)`: the `AppConfig` of `role`, by default against `pg_server`.

    Its embedder is the session's fake embedder (`JOBHUNTER_EMBEDDER_URL`, Task 22), unless `env` names one.
    """

    def make(role: Role, server: PgServer | None = None, **env: str) -> AppConfig:
        # Resolved only when no server is given, so a test with its own server does not start the shared one.
        pg = server or request.getfixturevalue("pg_server")
        return config_for(pg, role, embedder_url=_fake_embedder_url(request), **env)

    return make


@pytest.fixture
def proc_env(request: pytest.FixtureRequest) -> ProcessEnvFactory:
    """`proc_env(role, server=None, **extra)`: the environment of a child process of `role`, whose embedder
    is the session's fake embedder (Task 22)."""

    def make(role: Role, server: PgServer | None = None, **extra: str) -> dict[str, str]:
        pg = server or request.getfixturevalue("pg_server")
        return process_env(pg, role, embedder_url=_fake_embedder_url(request), **extra)

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
# Every column of the row as `seed_defaults` wrote it: the four pointers settings version 1 holds, the seeded
# rubric v1 and prompt-schema version (M2 Task 6; `_SEEDED_POINTERS` fails the reset when one is missing),
# and the pointers a profile or a scope apply sets left NULL (the pending scope, the profile and the tuple).
_ACTIVE_CONFIG_RESET: Final = """\
UPDATE active_config SET settings_version_id = v.id, scope_version_id = v.scope_version_id,
  model_policy_id = v.model_policy_id, execution_config_id = v.execution_config_id,
  evaluation_policy_id = v.evaluation_policy_id, pending_scope_version_id = NULL, profile_version_id = NULL,
  rubric_version_id = (SELECT r.id FROM rubric_versions r WHERE r.version = 'v1'),
  prompt_schema_version_id = (SELECT p.id FROM prompt_schema_versions p
    WHERE p.prompt_schema_hash = %(prompt_schema_hash)s),
  llm_tuple_id = NULL, updated_at = now()
  FROM settings_versions v WHERE v.version_no = 1"""
# The hash of the prompt-schema version `seed_defaults` points at: this code's templates and schemas.
_SEEDED_PROMPT_SCHEMA_HASH: Final = prompt_schema_hash(*prompt_schema_v1())
# The columns are nullable in M2 (NOT NULL is M3's, plan Deviation 32), so a seeded row that is missing would
# leave a NULL pointer behind silently: the reset checks for it.
_SEEDED_POINTERS: Final = (
    "SELECT rubric_version_id IS NOT NULL AND prompt_schema_version_id IS NOT NULL "
    "FROM active_config WHERE id = 1"
)
# The M2 tables are emptied with DELETE, never by `_TRUNCATE`: `TRUNCATE ... CASCADE` of a table that
# `active_config` reaches through its foreign keys (`cv_files` -> `profile_drafts` -> `profile_versions`,
# `llm_calls`, `eval_runs`, `llm_tuples`) would truncate `active_config` itself. Run after
# `_ACTIVE_CONFIG_RESET` (nothing points at them any more) and after the M1 `_TRUNCATE` (it emptied `runs`,
# which references `llm_tuples`), in this order: every child before its parent under the immediate foreign
# keys. Only the pair `profile_drafts` <-> `profile_versions` is deferrable, so those two go in any order.
_M2_DELETE: Final = (
    "SET CONSTRAINTS ALL DEFERRED",
    "UPDATE profile_drafts SET llm_call_id = NULL",
    "UPDATE query_proposals SET llm_call_id = NULL",
    "DELETE FROM profile_vectors",
    "DELETE FROM llm_calls",  # it references llm_spend_ledger, profile_drafts and eval_runs
    "DELETE FROM llm_spend_ledger",  # it references llm_budget
    "DELETE FROM query_proposals",
    "DELETE FROM eval_runs",
    "DELETE FROM llm_tuples",
    "DELETE FROM profile_drafts",
    "DELETE FROM profile_versions",
    "DELETE FROM cv_files",
    "DELETE FROM llm_budget",
)
# Run after every row that references a newer version points at version 1 again (`schedule_state`'s FK is
# immediate) and after `_M2_DELETE` (tuples and eval runs reference the configuration rows), in this order.
# The last five statements delete the rows nothing names any more: the seeded ones stay, because version 1
# and `active_config` name them, and the others were made by a settings save or by a test. They use NOT
# EXISTS, the form that stays right when a pointer is NULL (`NOT IN` over a NULL deletes no row at all).
_CONFIGURATION_RESET: Final = (
    "DELETE FROM settings_versions WHERE version_no > 1",
    "DELETE FROM scope_versions WHERE id NOT IN (SELECT scope_version_id FROM settings_versions)",
    "DELETE FROM model_policies m WHERE NOT EXISTS (SELECT 1 FROM settings_versions s "
    "WHERE s.model_policy_id = m.id) AND NOT EXISTS (SELECT 1 FROM active_config a "
    "WHERE a.model_policy_id = m.id)",
    "DELETE FROM execution_configs e WHERE NOT EXISTS (SELECT 1 FROM settings_versions s "
    "WHERE s.execution_config_id = e.id) AND NOT EXISTS (SELECT 1 FROM active_config a "
    "WHERE a.execution_config_id = e.id)",
    "DELETE FROM evaluation_policies p WHERE NOT EXISTS (SELECT 1 FROM settings_versions s "
    "WHERE s.evaluation_policy_id = p.id) AND NOT EXISTS (SELECT 1 FROM active_config a "
    "WHERE a.evaluation_policy_id = p.id)",
    "DELETE FROM rubric_versions r WHERE NOT EXISTS (SELECT 1 FROM active_config a "
    "WHERE a.rubric_version_id = r.id)",
    "DELETE FROM prompt_schema_versions p WHERE NOT EXISTS (SELECT 1 FROM active_config a "
    "WHERE a.prompt_schema_version_id = p.id)",
    "UPDATE ranking_state SET generation = 0, updated_at = now()",
)


def reset_database(server: PgServer, *, lock_timeout: str = LOCK_TIMEOUT) -> None:
    """Empty the tables of `_TRUNCATE` and `_M2_DELETE`, restore the seeded configuration, in one transaction.

    The TRUNCATE waits for the locks of any transaction a test leaked; after `lock_timeout` it fails with
    SQLSTATE 55P03 instead of hanging the suite.

    In this order: the TRUNCATE; the `default` source instances inserted again with the seed's own statement
    (their ids change with every reset, so tests look them up by `(source_id, instance_key)`); `active_config`
    as the seed wrote it (`_ACTIVE_CONFIG_RESET`: the pointers of settings version 1, the seeded rubric and
    prompt-schema version, and the profile, tuple and pending-scope pointers NULL);
    the M2 tables deleted (`_M2_DELETE`); the schedule reset; then the settings versions after 1, the scope
    versions no settings version uses, the model policies, execution configs and evaluation policies that no
    settings version or `active_config` names, and the rubric and prompt-schema rows `active_config` does not
    name deleted, and the ranking generation set to 0.

    The schedule keeps the interval and the anchor that `migrate` seeded: both come from the stored settings
    version 1 (the anchor on the day that version was created, as the seed computes it), not from constants
    here. Its `last_evaluated_slot` is the latest slot at or before the database's `now()`, it is enabled, and
    it points at version 1.
    """
    # The block commits when it ends and closes the connection.
    with psycopg.connect(server.dsn("migrator")) as conn:
        conn.execute("SELECT set_config('lock_timeout', %s, true)", (lock_timeout,))
        conn.execute(_TRUNCATE)
        conn.execute(DEFAULT_INSTANCES_SQL)
        conn.execute(_ACTIVE_CONFIG_RESET, {"prompt_schema_hash": _SEEDED_PROMPT_SCHEMA_HASH})
        if conn.execute(_SEEDED_POINTERS).fetchone() != (True,):
            raise LookupError(
                "active_config or a seeded rubric or prompt-schema row is missing: `migrated` seeds them"
            )
        for statement in _M2_DELETE:
            conn.execute(statement)
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
        for statement in _CONFIGURATION_RESET:
            conn.execute(statement)


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


_EPOCH_1_IS_CURRENT: Final = (
    "INSERT INTO worker_heartbeat (id, worker_instance_id, worker_epoch, hostname, pid, lock_backend_pid, "
    "app_version, started_at, heartbeat_at) "
    "VALUES (1, gen_random_uuid(), 1, 'test', 1, 1, 'test', now(), now())"
)


@pytest.fixture
def current_epoch(clean_db: None, migrated: PgServer) -> int:
    """Worker epoch 1 recorded as the current one (a committed `worker_heartbeat` row); returns 1.

    For tests that admit runs or claim operations as `epoch=1` without a takeover: every such write is fenced
    by the current epoch (`epoch.fence_epoch`). A later epoch claim in the test increments it (to 2, 3, ...).
    """
    pg_one(migrated, "app", _EPOCH_1_IS_CURRENT)
    return 1


@pytest.fixture
def worker_lock_heartbeat(current_epoch: int) -> None:
    """A fresh `worker_heartbeat` row (the one `current_epoch` records), so readiness's `worker_heartbeat`
    probe passes for the next 60 s (Task 22)."""


@pytest.fixture
async def queue_app(migrated: PgServer, make_config: ConfigFactory) -> AsyncIterator[procrastinate.App]:
    """The task app of this process (`make_queue_app`) against the shared server, open for the test.

    `defer_in_tx` and the stalled-job queries work on it; its pool is closed after the test.
    """
    app = make_queue_app(make_config("worker", migrated).dsn("psycopg"))
    async with app.open_async():
        yield app


@pytest.fixture
def cv_store_dir(tmp_path: Path) -> Path:
    """The `cv_store` of the test's api (`JOBHUNTER_CV_STORE_DIR`): an empty directory of mode 0700."""
    root = tmp_path / "cv_store"
    root.mkdir(mode=0o700)
    return root


@pytest.fixture
def api_app(engine: AsyncEngine, make_config: ConfigFactory, cv_store_dir: Path) -> JobHunterApp:
    """The whole api (edge included) on the shared database; `api_client` and `logged_in_client` share it.

    The app gets its config and engine injected, so no lifespan runs. Its CV files go to `cv_store_dir`.
    """
    # Its `make_queue_app` may be the process's first (first call wins: which DSN registers depends on test
    # order), but deferring never uses the task app's DSN, only its registry.
    cfg = make_config("api", JOBHUNTER_CV_STORE_DIR=str(cv_store_dir))
    return create_app(cfg=cfg, engine=engine)


async def count_rows(engine: AsyncEngine, table: str, **filters: object) -> int:
    """How many rows of `table` match every filter: `column = value`, or `column IN value` for a tuple."""
    query = sa.select(sa.func.count()).select_from(sa.table(table))
    for name, value in filters.items():
        column: sa.ColumnClause[Any] = sa.column(name)
        query = query.where(column.in_(value) if isinstance(value, tuple) else column == value)
    async with engine.connect() as conn:
        return int((await conn.execute(query)).scalar_one())


def _app_client(app: JobHunterApp, client: tuple[str, int], headers: dict[str, str]) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False, client=client)
    return httpx.AsyncClient(transport=transport, base_url="https://testserver", headers=headers)


@pytest.fixture
async def api_client(api_app: JobHunterApp) -> AsyncIterator[httpx.AsyncClient]:
    """A client of `api_app` as client `198.51.100.10:50000`, sending `Origin: https://testserver`.

    An exception the app lets through comes back as a 500 instead of being raised in the test
    (`raise_app_exceptions=False`).
    """
    async with _app_client(api_app, ("198.51.100.10", 50000), {"Origin": "https://testserver"}) as client:
        yield client


TEST_USERNAME: Final = "ada"
TEST_PASSWORD: Final = "Tannenbaum-Ölfass-7"


class UserFactory(Protocol):
    def __call__(self, username: str = TEST_USERNAME, password: str = TEST_PASSWORD) -> Awaitable[None]: ...


@pytest.fixture
def make_user(engine: AsyncEngine) -> UserFactory:
    """`await make_user(username="ada", password=TEST_PASSWORD)`: create the single user (committed)."""

    async def make(username: str = TEST_USERNAME, password: str = TEST_PASSWORD) -> None:
        async with engine.begin() as conn:
            await users.create_user(conn, username, password, now=utcnow())

    return make


@pytest.fixture
async def logged_in_client(api_app: JobHunterApp, make_user: UserFactory) -> AsyncIterator[httpx.AsyncClient]:
    """A second client of `api_app` (client `198.51.100.20:50000`), logged in as the user `make_user` made.

    It sends `Origin: https://testserver` and `X-JH-Request: 1` with every request, and the session cookie
    it got from the login.
    """
    await make_user()
    headers = {"Origin": "https://testserver", "X-JH-Request": "1"}
    async with _app_client(api_app, ("198.51.100.20", 50000), headers) as client:
        r = await client.post("/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD})
        if r.status_code != 204:
            raise RuntimeError(f"logged_in_client: the login answered {r.status_code}")
        yield client


@pytest.fixture
def log_events() -> Iterator[Callable[[], list[dict[str, Any]]]]:
    """`log_events()`: the JSON events logged so far in this test, as rendered (redaction included), in order.

    The logging of the process goes to an in-memory stream at level INFO during the test; afterwards the
    default stream is back.
    """
    buffer = io.StringIO()
    configure_logging("INFO", stream=buffer)

    def events() -> list[dict[str, Any]]:
        return [json.loads(line) for line in buffer.getvalue().splitlines() if line]

    try:
        yield events
    finally:
        configure_logging("INFO")
