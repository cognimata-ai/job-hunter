"""The nightly backup set (spec §19.5 steps 1-7; plan M5 Task 24, Deviations 29 and 31, Rulings T24-1 and
T24-2 (j)): `jobhunter backup set`, and the scheduler's `set` job (`JOBHUNTER_BACKUP_CRON`).

`run_backup_set` is the spec's `backup_set.sh` as typed Python, in its order:

1. **start**: a `backup_sets` row `running`, as `jobhunter_backup` (its engine is the backup role's).
2. **snapshot**: a psycopg connection as `jobhunter_backup` opens `REPEATABLE READ, READ ONLY`, sets
   `idle_in_transaction_session_timeout = 0` for that transaction only (`SET LOCAL`; the server's 60 s would
   end it during a long dump) and exports its snapshot (`pg_export_snapshot()`). In it, it reads `now()`
   (`db_snapshot_at`, the transaction's start), the Alembic revision, `count(*)` of `deletion_manifests`
   (`manifest_count`) and of every table of the `public` schema (`row_counts`), and the data key ids of the
   sealed CV texts (`cv_files.raw_text_enc`) and of the sealed TOTP secret (`users.totp_secret_enc`). The
   transaction stays open until the dump has finished, then commits; it ends on every path (a failure or a
   cancellation rolls it back as the connection closes).
3. **dump**: `pg_dump -Fc --snapshot=<id> -f <staging>/set/jobhunter.dump` as `jobhunter_backup`, the password
   in the child's environment only (`PGPASSWORD`; never its argv, never a log line), so the dump holds exactly
   the rows counted.
4. **verify**: `pg_restore --list` must succeed; the dump's sha256 and size are recorded.
5. **inventory**: `<staging>/set/backup_set.json` (`BackupSetInventory`): the set id, the dump's sha256, the
   Alembic revision, `db_snapshot_at`, `manifest_count`, `row_counts`, the CV file count, the app version and
   `data_key_ids` (Deviation 31): the hex key ids (`crypto.key_id`, the 8 bytes after the version byte of
   every ciphertext) found in the snapshot's ciphertexts and in the first bytes of every CV file of the store,
   the keys a restore of this set needs (a file of another name in the CV directory is not the store's).
6. **upload**: one `restic backup <staging>/set <cv_store_dir> <manifests_dir> --tag set --tag set:<id>`:
   the subdirectory `set/`, so restic's cache (`<staging>/.restic-cache`) stays out of the snapshot. The CV
   files are taken after the dump, so every file the dump names is there; a later one is an orphan a restore
   removes.
7. **record**: `succeeded` with the snapshot id and the inventory.

**One backup job at a time** (Ruling T26-1 (a) T24-4 M1): a set, a prune (`backup.prune`) and a restore test
(`backup.restore_test`) each hold the one-key session advisory lock `BACKUP_JOB_LOCK_KEY` (7261006, in
`db.migrate`'s key list) for its whole run, on a dedicated autocommit connection as `jobhunter_backup`
(`backup_job_lock`): `pg_try_advisory_lock`, so a job that finds it held writes no row, touches no
staging, logs `backup_job_busy` and raises `BackupJobBusy` (the CLI exits 1); the scheduler's next tick
tries again.

The staging subdirectory `set/` is made fresh at the start (a dead run's leftovers removed) and removed at the
end, after a failure too; restic's cache is kept. Any failing step makes the set `failed` with `error` =
`<step>:<error class>` (no message, no output, no path) and raises `BackupSetFailed`; a set whose process is
stopped meanwhile (a cancellation) stays `running`, and the scheduler's next start ends it (`interrupted`).
No alert is raised here: the worker's `backup_health` reconciler derives `backup.failed` from the row (O1).
Log lines carry the set id, the step, the error class and counts.

Every path is `cfg.staging_dir`, `cfg.cv_store_dir` or `cfg.manifests_dir` (`/data/staging`, `/data/cv`,
`/data/manifests` in the backup image); `/opt/pgclient/bin` is the image's PostgreSQL 17 client.
"""

import asyncio
import hashlib
import json
import shutil
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, TypedDict

import psycopg
from psycopg import sql
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from jobhunter import __version__, crypto
from jobhunter.backup.restic import Restic
from jobhunter.backup.runner import CommandRunner, command_name
from jobhunter.config import AppConfig
from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.observability.logging import get_logger
from jobhunter.profile.storage import CvStore

PG_DUMP: Final = "/opt/pgclient/bin/pg_dump"
PG_RESTORE: Final = "/opt/pgclient/bin/pg_restore"
# The staging subdirectory of a set, its two files, and the restic tags of its snapshot.
SET_STAGING: Final = "set"
DUMP_NAME: Final = "jobhunter.dump"
INVENTORY_NAME: Final = "backup_set.json"
SET_TAG: Final = "set"
DUMP_TIMEOUT_S: Final = 2 * 3600.0
LIST_TIMEOUT_S: Final = 1800.0
_HASH_CHUNK: Final = 1024 * 1024
# The backup jobs' one-key session advisory lock (module docstring; `db.migrate`'s key list).
BACKUP_JOB_LOCK_KEY: Final = 7261006
BACKUP_JOB_BUSY: Final = "backup_job_busy"

Step = Literal["snapshot", "dump", "verify", "inventory", "upload", "record"]

_START: Final = q("INSERT INTO backup_sets (status, started_at) VALUES ('running', :started_at) RETURNING id")
# The tables whose rows the set counts: every ordinary table of the application schema (the queue's and the
# checkpointer's live there too), as `pg_dump` dumps them.
APPLICATION_TABLES: Final = (
    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY c.relname"
)
# The key ids of the database's ciphertexts (`crypto.encrypt`'s header: the version byte, then the key id), in
# every sealed column (`crypto.SEALED_VALUES_SQL`, the one inventory the data-key rotation counts too).
_DB_KEY_IDS: Final = (
    "SELECT DISTINCT substring(s.sealed FROM 2 FOR %(id_bytes)s) FROM ("  # noqa: S608 - constant SQL only
    + crypto.SEALED_VALUES_SQL
    + ") AS s WHERE length(s.sealed) >= %(header_bytes)s AND get_byte(s.sealed, 0) = %(version)s"
)

log = get_logger("jobhunter.backup.backup_set")


class BackupJobFailed(Exception):
    """A backup job failed and recorded it; `error` is its `<step>:<error class>` (no content)."""

    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


class BackupJobBusy(Exception):
    """Another backup job holds `BACKUP_JOB_LOCK_KEY`: the job `job` wrote nothing and did not run."""

    def __init__(self, job: str) -> None:
        super().__init__(f"{job}: {BACKUP_JOB_BUSY}")
        self.job = job
        self.error = BACKUP_JOB_BUSY


class BackupSetFailed(BackupJobFailed):
    """The set `set_id` is `failed` with `error`."""

    def __init__(self, set_id: int, error: str) -> None:
        super().__init__(error)
        self.set_id = set_id


class CommandFailed(Exception):
    """A command of the set exited non-zero; its output is never kept."""

    def __init__(self, command: str, returncode: int) -> None:
        super().__init__(f"{command} exited {returncode}")
        self.command = command
        self.returncode = returncode


class BackupSetInventory(TypedDict):
    """`backup_set.json`, which a restore verifies against (§19.5 restore steps 2 and 5)."""

    set_id: int
    dump_sha256: str
    alembic_revision: str
    db_snapshot_at: str
    manifest_count: int
    row_counts: dict[str, int]
    cv_file_count: int
    app_version: str
    data_key_ids: list[str]


@dataclass(frozen=True)
class _Snapshot:
    """What step 2 read in the exported snapshot."""

    snapshot_id: str
    db_snapshot_at: datetime
    alembic_revision: str
    row_counts: dict[str, int]
    key_ids: frozenset[bytes]


@dataclass(frozen=True)
class _Dump:
    sha256: bytes
    size: int


class _StepFailed(Exception):
    """Step `step` failed with `exc`: the set's `error` is `<step>:<error class>`."""

    def __init__(self, step: Step, exc: Exception) -> None:
        self.error = f"{step}:{type(exc).__name__}"
        super().__init__(self.error)


@contextmanager
def _step(step: Step) -> Iterator[None]:
    """A failure inside is the set's failure at `step`, unless an inner step's (which passes unchanged)."""
    try:
        yield
    except _StepFailed:
        raise
    except Exception as exc:  # recorded as `<step>:<error class>`, as `set -euo pipefail` would stop
        raise _StepFailed(step, exc) from exc


async def _first(conn: psycopg.AsyncConnection[Any], query: sql.Composed | str) -> Any:
    row = await (await conn.execute(query)).fetchone()
    if row is None:
        raise LookupError("the query returned no row")
    return row[0]


async def _read_snapshot(conn: psycopg.AsyncConnection[Any]) -> _Snapshot:
    """Step 2 in the open transaction: the export, then what the set records of that snapshot."""
    await conn.execute("SET LOCAL idle_in_transaction_session_timeout = 0")
    snapshot_id = str(await _first(conn, "SELECT pg_export_snapshot()"))
    db_snapshot_at: datetime = await _first(conn, "SELECT now()")
    revision = str(await _first(conn, "SELECT version_num FROM alembic_version"))
    tables = [str(row[0]) for row in await (await conn.execute(APPLICATION_TABLES)).fetchall()]
    row_counts = {}
    for table in tables:
        query = sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier("public", table))
        row_counts[table] = int(await _first(conn, query))
    params = {
        "id_bytes": crypto.KEY_ID_BYTES,
        "header_bytes": crypto.HEADER_BYTES,
        "version": crypto.FORMAT_VERSION,
    }
    key_ids = frozenset(bytes(row[0]) for row in await (await conn.execute(_DB_KEY_IDS, params)).fetchall())
    return _Snapshot(snapshot_id, db_snapshot_at.astimezone(UTC), revision, row_counts, key_ids)


@asynccontextmanager
async def _exported_snapshot(cfg: AppConfig) -> AsyncIterator[_Snapshot]:
    """Step 2's transaction, open while the caller dumps from its snapshot; committed when the block ends, and
    rolled back, with the connection closed, when it raises or is cancelled."""
    async with await psycopg.AsyncConnection.connect(cfg.dsn("psycopg", db_role="backup")) as conn:
        await conn.set_isolation_level(psycopg.IsolationLevel.REPEATABLE_READ)
        await conn.set_read_only(True)
        async with conn.transaction():
            yield await _read_snapshot(conn)


async def _run_command(runner: CommandRunner, argv: list[str], env: dict[str, str], timeout_s: float) -> None:
    result = await runner.run(argv, env=env, timeout_s=timeout_s)
    if result.returncode != 0:
        raise CommandFailed(command_name(argv), result.returncode)


async def _dump(cfg: AppConfig, runner: CommandRunner, snapshot_id: str, dump: Path) -> None:
    """Step 3: `pg_dump` of the exported snapshot as the backup role, the password in its environment only."""
    argv = [
        PG_DUMP,
        "-Fc",
        f"--snapshot={snapshot_id}",
        "-f",
        str(dump),
        "--no-password",
        "--host",
        cfg.db.host,
        "--port",
        str(cfg.db.port),
        "--username",
        "jobhunter_backup",
        "--dbname",
        cfg.db.name,
    ]
    password = cfg.secret("db_backup").decode("utf-8")
    await _run_command(runner, argv, {"PGPASSWORD": password}, DUMP_TIMEOUT_S)


def _hash_file(path: Path) -> _Dump:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as dump:
        while chunk := dump.read(_HASH_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return _Dump(digest.digest(), size)


async def _verify(runner: CommandRunner, dump: Path) -> _Dump:
    """Step 4: the dump reads as an archive (`pg_restore --list`, no database); its sha256 and size."""
    await _run_command(runner, [PG_RESTORE, "--list", str(dump)], {}, LIST_TIMEOUT_S)
    return await asyncio.to_thread(_hash_file, dump)


def _cv_key_ids(cv_store_dir: Path) -> tuple[int, frozenset[bytes]]:
    """The CV store's files (those `CvStore.write` made) and the key ids of their headers
    (`CvStore.header`); a file removed meanwhile (retention) is not counted, and a file too short for a
    header names no key."""
    store = CvStore(cv_store_dir)
    count, key_ids = 0, set()
    for stored in store.stored():
        header = store.header(stored.storage_path)
        if header is None:
            continue
        count += 1
        if (key := crypto.blob_key_id(header)) is not None:
            key_ids.add(key)
    return count, frozenset(key_ids)


def _write_inventory(
    cfg: AppConfig, staged: Path, set_id: int, snapshot: _Snapshot, dump: _Dump
) -> BackupSetInventory:
    """Step 5: `backup_set.json` beside the dump."""
    cv_file_count, cv_key_ids = _cv_key_ids(cfg.cv_store_dir)
    inventory: BackupSetInventory = {
        "set_id": set_id,
        "dump_sha256": dump.sha256.hex(),
        "alembic_revision": snapshot.alembic_revision,
        "db_snapshot_at": snapshot.db_snapshot_at.isoformat(),
        "manifest_count": snapshot.row_counts["deletion_manifests"],
        "row_counts": snapshot.row_counts,
        "cv_file_count": cv_file_count,
        "app_version": __version__,
        "data_key_ids": sorted(key.hex() for key in snapshot.key_ids | cv_key_ids),
    }
    (staged / INVENTORY_NAME).write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", "utf-8")
    return inventory


async def _try_job_lock(conn: psycopg.AsyncConnection[Any]) -> bool:
    row = await (
        await conn.execute("SELECT pg_try_advisory_lock(%s::bigint)", (BACKUP_JOB_LOCK_KEY,))
    ).fetchone()
    return row is not None and bool(row[0])


@asynccontextmanager
async def backup_job_lock(cfg: AppConfig, job: str) -> AsyncIterator[None]:
    """`BACKUP_JOB_LOCK_KEY` held for the block, on a dedicated autocommit connection as `jobhunter_backup`;
    `BackupJobBusy` (logged `backup_job_busy`) when another session holds it. Released and closed on every
    path; closing the connection would drop it too."""
    conn = await psycopg.AsyncConnection.connect(
        cfg.dsn("psycopg", db_role="backup"), autocommit=True, application_name="jobhunter-backup-job"
    )
    try:
        if not await _try_job_lock(conn):
            log.warning(BACKUP_JOB_BUSY, job=job, lock_key=BACKUP_JOB_LOCK_KEY)
            raise BackupJobBusy(job)
        try:
            yield
        finally:
            # A lost connection took the lock with it; do not let that hide the error that ended the job.
            with suppress(psycopg.OperationalError):
                await conn.execute("SELECT pg_advisory_unlock(%s::bigint)", (BACKUP_JOB_LOCK_KEY,))
    finally:
        await conn.close()


def fresh_staging(cfg: AppConfig) -> Path:
    """The set's staging subdirectory, empty (whatever a dead run left there is removed), mode 0700."""
    staged = cfg.staging_dir / SET_STAGING
    shutil.rmtree(staged, ignore_errors=True)
    staged.mkdir(mode=0o700)
    return staged


def clear_staging(cfg: AppConfig) -> None:
    """Remove the set's staging subdirectory (the restic cache beside it stays); one already gone is fine."""
    try:
        shutil.rmtree(cfg.staging_dir / SET_STAGING)
    except FileNotFoundError:
        pass
    except OSError as exc:  # the next set's start removes it; a staging leftover fails no set
        log.warning("backup_set_staging_not_cleared", error_class=type(exc).__name__)


async def _start(engine: AsyncEngine, started_at: datetime) -> int:
    async with engine.begin() as conn:
        return int((await conn.execute(_START, {"started_at": started_at})).scalar_one())


async def _succeed(
    engine: AsyncEngine, set_id: int, restic_snapshot_id: str, snapshot: _Snapshot, dump: _Dump, files: int
) -> None:
    async with engine.begin() as conn:
        await transition(
            conn,
            "backup_sets",
            set_id,
            ["running"],
            "succeeded",
            set_={
                "restic_snapshot_id": restic_snapshot_id,
                "dump_sha256": dump.sha256,
                "dump_bytes": dump.size,
                "db_snapshot_at": snapshot.db_snapshot_at,
                "manifest_count": snapshot.row_counts["deletion_manifests"],
                "row_counts": snapshot.row_counts,
                "cv_file_count": files,
            },
        )


async def _fail(engine: AsyncEngine, set_id: int, error: str) -> None:
    """The set `failed` with `error`; a database that refuses that too leaves it `running`, which the
    scheduler's next start ends (`interrupted`)."""
    try:
        async with engine.begin() as conn:
            await transition(conn, "backup_sets", set_id, ["running"], "failed", reason=error)
    except Exception as exc:  # noqa: BLE001 - the set's own failure is what the caller reports
        log.error("backup_set_failure_unrecorded", set_id=set_id, error_class=type(exc).__name__)


async def _run_steps(
    cfg: AppConfig, restic: Restic, runner: CommandRunner, engine: AsyncEngine, set_id: int
) -> None:
    """Steps 2 to 7 of the set `set_id` (module docstring); a failure raises `_StepFailed`."""
    with _step("dump"):
        staged = fresh_staging(cfg)
    dump_path = staged / DUMP_NAME
    # The export's transaction is open around the dump only; it commits once the dump has finished.
    with _step("snapshot"):
        async with _exported_snapshot(cfg) as snapshot:
            with _step("dump"):
                await _dump(cfg, runner, snapshot.snapshot_id, dump_path)
    with _step("verify"):
        dump = await _verify(runner, dump_path)
    with _step("inventory"):
        inventory = await asyncio.to_thread(_write_inventory, cfg, staged, set_id, snapshot, dump)
    with _step("upload"):
        paths = [staged, cfg.cv_store_dir, cfg.manifests_dir]
        restic_snapshot_id = await restic.backup(paths=paths, tags=[SET_TAG, f"{SET_TAG}:{set_id}"])
    with _step("record"):
        await _succeed(engine, set_id, restic_snapshot_id, snapshot, dump, inventory["cv_file_count"])
    log.info(
        "backup_set_succeeded",
        set_id=set_id,
        dump_bytes=dump.size,
        manifests=inventory["manifest_count"],
        files=inventory["cv_file_count"],
        data_keys=len(inventory["data_key_ids"]),
    )


async def run_backup_set(
    cfg: AppConfig, restic: Restic, runner: CommandRunner, *, clock: Callable[[], datetime]
) -> int:
    """Take one backup set (module docstring, steps 1-7); its id. `BackupSetFailed` once it is `failed`,
    `BackupJobBusy` (nothing written) while another backup job runs."""
    async with backup_job_lock(cfg, "set"):
        engine = create_async_engine(
            cfg.dsn("sqlalchemy", db_role="backup"), poolclass=NullPool, hide_parameters=True
        )
        try:
            set_id = await _start(engine, clock())
            log.info("backup_set_started", set_id=set_id)
            try:
                await _run_steps(cfg, restic, runner, engine, set_id)
            except _StepFailed as failed:
                await _fail(engine, set_id, failed.error)
                log.error("backup_set_failed", set_id=set_id, error=failed.error)
                raise BackupSetFailed(set_id, failed.error) from failed.__cause__
            finally:
                clear_staging(cfg)
            return set_id
        finally:
            await engine.dispose()
