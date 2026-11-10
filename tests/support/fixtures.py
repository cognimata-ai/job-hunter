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
import psycopg.types.json
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
from jobhunter.llm.prompts.render import prompt_schema_hash, prompt_schema_v2
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
# The tables `reset_database` empties with TRUNCATE (the migrator owns them, so only the migrator may
# truncate). The seeded configuration chain, `active_config`, `ranking_state` and `sources` are kept
# (`reset_database` restores them to their seeded state), `schedule_state` and `deadman_state` are
# reset (below), and so are `checkpoint_migrations`, which the checkpointer reads to know its schema is set
# up, and `schema_compat`, `migrate`'s record of the migration chain (M5). `source_instances` is
# named on purpose: CASCADE empties it anyway with `watchlist_companies` and `ats_snapshots`, so
# `reset_database` inserts the default instances again.
#
# `TRUNCATE ... CASCADE` picks the tables it empties from the foreign-key graph, whatever their rows hold: it
# also empties every table that references a named one, and so on. So no table is named here from which a
# referencing path reaches the configuration (`test_reset_truncates_no_configuration_path` walks the graph):
# `jobs`, `job_revisions` and `duplicate_groups` (M3's `score_targets` -> `llm_calls` -> `profile_drafts` ->
# `profile_versions` -> `active_config`), `operations` (`batch_submissions.operation_id`; an ON DELETE action
# changes nothing about what TRUNCATE empties), and `embeddings` and `job_tombstones` (referenced by
# `job_revisions` and `jobs`) are emptied with DELETE (`_M3_DELETE`, `_M2_DELETE`).
TRUNCATED_TABLES: Final = (
    "runs",
    "run_attempts",
    "sessions",
    "login_attempts",
    "login_lockouts",
    "api_idempotency",
    "alerts",
    "purge_fences",
    "worker_heartbeat",
    "users",
    "procrastinate_jobs",
    "procrastinate_periodic_defers",
    "procrastinate_workers",
    "checkpoints",
    "checkpoint_writes",
    "checkpoint_blobs",
    "watchlist_companies",
    "source_instances",
    "queries",
    "coverage_cells",
    "coverage_intervals",
    "fetch_requests",
    "raw_responses",
    "raw_items",
    "ats_snapshots",
    "ats_snapshot_members",
    "ats_snapshot_absences",
    "quota_windows",
    "quota_ledger",
    "postings",
    "posting_observations",
    "job_revision_occurrences",
    "job_revision_sources",
    "ad_keys",
    "ad_key_sources",
    "merge_events",
    "identity_conflicts",
    "duplicate_group_members",
    "dedup_decisions",
    "tombstone_keys",
    "hard_filter_results",
    # M5: the manifests, their shipments, the backups, the exports and the purged batch items. Only
    # `purge_fences` (named above) references a manifest besides `purged_batch_items`; `batch_submissions`,
    # which `purged_batch_items` references, is not emptied by them (CASCADE follows the referencing side).
    "deletion_manifests",
    "manifest_shipments",
    "manifest_shipment_items",
    "backup_sets",
    "backup_runs",
    "exports",
    "purged_batch_items",
)
_TRUNCATE: Final = f"TRUNCATE {', '.join(TRUNCATED_TABLES)} CASCADE"


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
# The dead-man state as `seed_defaults` wrote it (M5): the row deleted and inserted again as the seed inserts
# it, so every other column takes its default (NULL today), whatever columns a later task adds. No foreign key
# references the table.
_DEADMAN_RESET: Final = ("DELETE FROM deadman_state", "INSERT INTO deadman_state (id) VALUES (1)")
# A source purge's record on the kept `sources` rows (M5 Task 15), which `migrate` seeds NULL.
_SOURCES_RESET: Final = "UPDATE sources SET purge_fence_at = NULL WHERE purge_fence_at IS NOT NULL"
# Every column of the row as `seed_defaults` wrote it: the four pointers settings version 1 holds, the shipped
# rubric v2 (M3 Task 3) and the shipped prompt-schema version 2 (M3 Task 4; `_SEEDED_ROWS` fails the reset
# when one is missing), and the pointers a profile or a scope apply sets left NULL (the pending scope, the
# profile and the tuple).
_ACTIVE_CONFIG_RESET: Final = """\
UPDATE active_config SET settings_version_id = v.id, scope_version_id = v.scope_version_id,
  model_policy_id = v.model_policy_id, execution_config_id = v.execution_config_id,
  evaluation_policy_id = v.evaluation_policy_id, pending_scope_version_id = NULL, profile_version_id = NULL,
  rubric_version_id = (SELECT r.id FROM rubric_versions r WHERE r.version = 'v2'),
  prompt_schema_version_id = (SELECT p.id FROM prompt_schema_versions p WHERE p.prompt_schema_hash = %(v2)s),
  llm_tuple_id = NULL, updated_at = now()
  FROM settings_versions v WHERE v.version_no = 1"""
# The two shipped prompt-schema versions the seed inserts, found by their hashes. Version 1's is a pinned
# literal, the hash the M2 seed wrote (`prompt_schema_v1()` must keep giving it:
# `test_v1_hash_is_the_m2_literal_and_v2_keeps_the_m2_templates`); version 2's is this code's.
_SEEDED_PROMPT_SCHEMA_HASH_V1: Final = bytes.fromhex(
    "cf66004fe81e954d08848d6b6e7e3de0a8c7d1dab3ca6a9c7f4efbaa527e801c"
)
_SEEDED_PROMPT_SCHEMA_HASH_V2: Final = prompt_schema_hash(*prompt_schema_v2())
_SEEDED_HASHES: Final = {"v1": _SEEDED_PROMPT_SCHEMA_HASH_V1, "v2": _SEEDED_PROMPT_SCHEMA_HASH_V2}
# The rows `_ACTIVE_CONFIG_RESET` needs, checked before it runs: without the `active_config` row it would
# change nothing, and without a seeded rubric or prompt-schema row it would fail on the NOT NULL of that
# pointer (migration 0008) with an error that does not name the cause.
_SEEDED_ROWS: Final = (
    "SELECT EXISTS (SELECT 1 FROM active_config WHERE id = 1) "
    "AND EXISTS (SELECT 1 FROM rubric_versions WHERE version = 'v2') "
    "AND EXISTS (SELECT 1 FROM prompt_schema_versions WHERE prompt_schema_hash = %(v1)s) "
    "AND EXISTS (SELECT 1 FROM prompt_schema_versions WHERE prompt_schema_hash = %(v2)s)"
)
# The M3 tables, and the M1 tables they reference that `_TRUNCATE` cannot name (above), emptied with DELETE.
# Run after `_TRUNCATE` (it emptied every other table that references `jobs`) and before
# `_ACTIVE_CONFIG_RESET`, in this order: every child before its parent under the immediate foreign keys. The
# deferrable ones (targets <-> results and drafts, batch items <-> calls, jobs <-> revisions) are deferred.
_M3_DELETE: Final = (
    "SET CONSTRAINTS ALL DEFERRED",
    "DELETE FROM eval_case_results",
    "DELETE FROM score_feedback",
    "DELETE FROM det_results",
    "DELETE FROM result_evidence",
    "DELETE FROM golden_cases",  # cascades into the eval calls that name a golden case
    "DELETE FROM critic_notes",
    "DELETE FROM llm_results",
    "DELETE FROM score_drafts",
    "DELETE FROM batch_items",
    # the scoring calls; the ledger rows go with `_M2_DELETE`, as do the other calls
    "DELETE FROM llm_calls WHERE attempt_id IS NOT NULL OR target_id IS NOT NULL "
    "OR batch_item_id IS NOT NULL OR golden_case_id IS NOT NULL",
    "DELETE FROM score_attempts",
    "DELETE FROM score_targets",
    "DELETE FROM batch_submissions",
    "DELETE FROM admission_lane_credits",
    "DELETE FROM bulk_operations",  # the ledger's bulk_operation_id becomes NULL
    "DELETE FROM score_cycles",
    "DELETE FROM user_job_actions",
    # `duplicate_groups.representative_job_id` is immediate (NO ACTION) and the delete nulls
    # `jobs.duplicate_group_id`, so the groups go first; `jobs` <-> `job_revisions` is deferrable
    "DELETE FROM duplicate_groups",
    "DELETE FROM job_revisions",
    "DELETE FROM jobs",
    "DELETE FROM job_tombstones",
    "DELETE FROM operations",
)
# The M2 tables are emptied with DELETE, never by `_TRUNCATE`: `TRUNCATE ... CASCADE` of a table that
# `active_config` reaches through its foreign keys (`cv_files` -> `profile_drafts` -> `profile_versions`,
# `llm_calls`, `eval_runs`, `llm_tuples`) would truncate `active_config` itself. Run after
# `_ACTIVE_CONFIG_RESET` (nothing points at them any more) and after `_TRUNCATE` and `_M3_DELETE` (they
# emptied `runs`, which references `llm_tuples`, and every M3 row), in this order: every child before its
# parent under the immediate foreign keys. Only the pair `profile_drafts` <-> `profile_versions` is
# deferrable, so those two go in any order. `embeddings` goes after `profile_vectors`, its last referencing
# rows (`_M3_DELETE` deleted the job revisions).
_M2_DELETE: Final = (
    "SET CONSTRAINTS ALL DEFERRED",
    "UPDATE profile_drafts SET llm_call_id = NULL",
    "UPDATE query_proposals SET llm_call_id = NULL",
    "DELETE FROM profile_vectors",
    "DELETE FROM embeddings",
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
# EXISTS, the form that stays right when a pointer is NULL (`NOT IN` over a NULL deletes no row at all). The
# seed inserts rubric v1 and v2 and points at v2 only, so the rubric delete also keeps both shipped versions
# by name: a reset leaves what `migrate` wrote (M3 Task 3). It inserts prompt-schema versions 1 and 2 and
# points at version 2, so the prompt-schema delete keeps both shipped rows by their hashes (M3 Task 4).
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
    "WHERE a.rubric_version_id = r.id) AND r.version NOT IN ('v1', 'v2')",
    "DELETE FROM prompt_schema_versions p WHERE NOT EXISTS (SELECT 1 FROM active_config a "
    "WHERE a.prompt_schema_version_id = p.id) AND p.prompt_schema_hash NOT IN (%(v1)s, %(v2)s)",
    "UPDATE ranking_state SET generation = 0, updated_at = now()",
)


# Settings version 1 as `migrate` seeded it, per server (its container), read by the first reset that finds
# it: delete-everything (M5 Task 16) deletes every settings version within its bound, version 1 among them,
# and keeps the rows it names (its derived rows are the default document's, which the purge's reset reuses),
# so the reset inserts it again with its id and its `created_at`, the day the schedule's anchor is computed
# from.
_SETTINGS_V1_COLUMNS: Final = (
    "id, version_no, schema_version, payload, scope_version_id, model_policy_id, execution_config_id, "
    "evaluation_policy_id, base_version_id, effect_classes, acknowledged_warnings, eval_override_ack, "
    "created_at"
)
_SETTINGS_V1_READ: Final = f"SELECT {_SETTINGS_V1_COLUMNS} FROM settings_versions WHERE version_no = 1"  # noqa: S608
_SETTINGS_V1_INSERT: Final = (
    f"INSERT INTO settings_versions ({_SETTINGS_V1_COLUMNS}) OVERRIDING SYSTEM VALUE "  # noqa: S608
    f"VALUES ({', '.join(['%s'] * len(_SETTINGS_V1_COLUMNS.split(',')))})"
)
_SEEDED_SETTINGS_V1: dict[str, tuple[Any, ...]] = {}


def _restore_settings_v1(conn: psycopg.Connection[tuple[Any, ...]], server: PgServer) -> None:
    """Settings version 1 inserted again as `migrate` seeded it when a delete-everything deleted it (above);
    remembered on the first reset that finds it."""
    found = conn.execute(_SETTINGS_V1_READ).fetchone()
    if found is not None:
        _SEEDED_SETTINGS_V1.setdefault(server.container, tuple(found))
        return
    seeded = _SEEDED_SETTINGS_V1.get(server.container)
    if seeded is None:
        raise LookupError("settings version 1 is missing and no reset has seen it: `migrated` seeds it")
    values = list(seeded)
    values[3] = psycopg.types.json.Jsonb(values[3])  # the payload
    conn.execute(_SETTINGS_V1_INSERT, values)


def reset_database(server: PgServer, *, lock_timeout: str = LOCK_TIMEOUT) -> None:
    """Empty the tables of `_TRUNCATE`, `_M3_DELETE` and `_M2_DELETE`, restore the seeded configuration, in
    one transaction.

    The TRUNCATE waits for the locks of any transaction a test leaked; after `lock_timeout` it fails with
    SQLSTATE 55P03 instead of hanging the suite.

    In this order: the TRUNCATE; the `default` source instances inserted again with the seed's own statement
    (their ids change with every reset, so tests look them up by `(source_id, instance_key)`); the M3 tables
    and the M1 tables they reference deleted (`_M3_DELETE`); settings version 1 inserted again when a
    delete-everything deleted it (`_restore_settings_v1`, M5 Task 16); `active_config` as the seed wrote it
    (`_ACTIVE_CONFIG_RESET`: the pointers of settings version 1, the shipped rubric v2 and the shipped
    prompt-schema version 2, and the profile, tuple and pending-scope pointers NULL); the M2 tables deleted
    (`_M2_DELETE`); the schedule and the dead-man state reset (`_DEADMAN_RESET`: its row inserted again as
    the seed inserts it); the sources' `purge_fence_at` NULL again, as `migrate` seeds it (`_SOURCES_RESET`: a
    source purge's record, M5 Task 15); then the settings versions after 1, the scope versions no settings
    version uses, the model policies, execution configs and evaluation policies that no settings version or
    `active_config` names, the rubric rows other than the shipped v1 and v2 and the prompt-schema rows other
    than the two shipped versions that `active_config` does not name deleted, and the ranking generation set
    to 0.

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
        for statement in _M3_DELETE:
            conn.execute(statement)
        _restore_settings_v1(conn, server)
        seeded = _SEEDED_HASHES
        if conn.execute(_SEEDED_ROWS, seeded).fetchone() != (True,):
            raise LookupError(
                "active_config or a seeded rubric or prompt-schema row is missing: `migrated` seeds them"
            )
        conn.execute(_ACTIVE_CONFIG_RESET, seeded)
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
        for statement in _DEADMAN_RESET:
            conn.execute(statement)
        conn.execute(_SOURCES_RESET)
        for statement in _CONFIGURATION_RESET:
            conn.execute(statement, seeded if "%(" in statement else None)


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
