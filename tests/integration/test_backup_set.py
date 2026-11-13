"""Backup sets, the prune and the backup records' retention (spec §19.5 steps 1-7 and Retention, §20.1; plan
M5 Task 24, Deviations 29 and 31, R13, Rulings T24-1 (b) and (c)).

A set is one exported snapshot: the backup role's transaction (REPEATABLE READ, READ ONLY) exports it, counts
every table and reads the data key ids of the database's ciphertexts in it, and stays open while `pg_dump
--snapshot` dumps from it; then `pg_restore --list` checks the dump, `backup_set.json` is staged beside it,
and one restic snapshot tagged `set` and `set:<id>` takes the staged set, the CV files and the manifests. Any
failing step fails the set (`<step>:<error class>`) and clears the staging; the restic cache stays.

`FakeCommands` stands in for `pg_dump`, `pg_restore` and restic, whose binaries are the backup image's
(`tests/stack/test_prod_backup.py` runs the real ones): its `pg_dump` reads the exported snapshot it is given
on a connection of its own (`SET TRANSACTION SNAPSHOT`), as the real one would, and writes a stand-in dump;
its restic `backup` reads the staged `backup_set.json` and answers with a stand-in snapshot id. The restic
wrapper (`Restic`) is the real one on top of it, and the database is the shared server, written as
`jobhunter_backup`. No key is a literal: every data key is drawn at run time.
"""

import hashlib
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path, PurePath
from typing import Any, Final

import procrastinate
import psycopg
import pytest
import sqlalchemy as sa
from jobhunter import __version__, crypto
from jobhunter.backup.backup_set import PG_DUMP, PG_RESTORE, BackupJobBusy, BackupSetFailed, run_backup_set
from jobhunter.backup.prune import PruneFailed, run_prune
from jobhunter.backup.restic import RESTIC_BIN, Restic
from jobhunter.backup.restore_test import run_restore_test
from jobhunter.backup.runner import CommandResult
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.db.migrate import BACKUP_DELETE_TABLES, BACKUP_GRANT_TABLES
from jobhunter.db.session import make_engine
from jobhunter.db.sql import pg_error
from jobhunter.graphs.checkpointer import open_saver
from jobhunter.privacy import retention
from jobhunter.privacy.retention import THIRTEEN_MONTHS, run_retention_daily
from jobhunter.profile.storage import CvStore
from psycopg import sql
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from tests.support.backups import backup_run, backup_set, manifest
from tests.support.pg import PgServer, config_for, pg_rows

pytestmark = pytest.mark.integration

SNAPSHOT_ID: Final = "5e" * 32
DUMP: Final = b"PGDMP stand-in for a custom-format dump"
# What a failing command writes to its stderr: never part of the set's error.
FAILURE_DETAIL: Final = "detail of the failure, never recorded"
_TABLES: Final = (
    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY c.relname"
)
# Every table of the application schema with the backup role's write privileges on it.
_PRIVILEGES: Final = (
    "SELECT c.relname, has_table_privilege('jobhunter_backup', c.oid, 'INSERT'), "
    "has_table_privilege('jobhunter_backup', c.oid, 'UPDATE'), "
    "has_table_privilege('jobhunter_backup', c.oid, 'DELETE'), "
    "has_table_privilege('jobhunter_backup', c.oid, 'TRUNCATE') "
    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') ORDER BY c.relname"
)
# The columns of `deletion_manifests` the backup role may update.
_MANIFEST_COLUMNS_UPDATED: Final = (
    "SELECT attname FROM pg_attribute WHERE attrelid = 'public.deletion_manifests'::regclass AND attnum > 0 "
    "AND NOT attisdropped AND has_column_privilege('jobhunter_backup', attrelid, attnum, 'UPDATE') "
    "ORDER BY attname"
)
# The functions of the application schema that run as their owner and that a role may execute.
_DEFINER_FUNCTIONS: Final = (
    "SELECT proname FROM pg_proc WHERE pronamespace = CAST('public' AS regnamespace) AND prosecdef "
    "AND has_function_privilege(%s, oid, 'EXECUTE') ORDER BY 1"
)
_IDLE_IN_TRANSACTION: Final = (
    "SELECT usename FROM pg_stat_activity WHERE state = 'idle in transaction' AND pid <> pg_backend_pid()"
)
_SET: Final = sa.text("SELECT * FROM backup_sets WHERE id = :id")
_RUN: Final = sa.text("SELECT * FROM backup_runs WHERE id = :id")


@dataclass(frozen=True)
class Call:
    argv: list[str]
    env: dict[str, str]


async def seen_in_snapshot(server: PgServer, snapshot_id: str) -> dict[str, int]:
    """Every table's row count as a transaction importing the exported snapshot sees it: the dump's view."""
    async with await psycopg.AsyncConnection.connect(server.dsn("backup")) as conn:
        await conn.set_isolation_level(psycopg.IsolationLevel.REPEATABLE_READ)
        await conn.set_read_only(True)
        async with conn.transaction():
            await conn.execute(sql.SQL("SET TRANSACTION SNAPSHOT {}").format(sql.Literal(snapshot_id)))
            tables = [row[0] for row in await (await conn.execute(_TABLES)).fetchall()]
            counts = {}
            for table in tables:
                query = sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier("public", table))
                counted = await (await conn.execute(query)).fetchone()
                assert counted is not None
                counts[table] = int(counted[0])
    return counts


async def snapshot_imports(server: PgServer, snapshot_id: str) -> bool:
    """Whether the exported snapshot can still be imported: only while its exporting transaction is open."""
    try:
        await seen_in_snapshot(server, snapshot_id)
    except psycopg.Error:
        return False
    return True


def read_staged(staged: Path) -> tuple[list[str], dict[str, Any]]:
    """What the set staged for its upload: the file names, and `backup_set.json`."""
    inventory = json.loads((staged / "backup_set.json").read_text(encoding="utf-8"))
    return sorted(path.name for path in staged.iterdir()), inventory


def write_dump(path: Path) -> None:
    path.write_bytes(DUMP)


class FakeCommands:
    """`CommandRunner` for `pg_dump`, `pg_restore` and restic (module docstring). `fail` names the program
    that fails: `pg_dump` and `pg_restore` exit 1, restic exits 3 (a backup) or 11 (a forget). `during_dump`
    runs while the dump runs, before it reads the snapshot; `during_restic` runs at each restic command, with
    its argv. restic's `snapshots` answers `listing`."""

    def __init__(
        self,
        server: PgServer,
        *,
        fail: str | None = None,
        during_dump: Callable[[], Awaitable[None]] | None = None,
        during_restic: Callable[[list[str]], Awaitable[None]] | None = None,
        listing: bytes = b"[]",
    ) -> None:
        self.server = server
        self.fail = fail
        self.during_dump = during_dump
        self.during_restic = during_restic
        self.listing = listing
        self.calls: list[Call] = []
        self.snapshot_id: str | None = None
        self.dump_saw: dict[str, int] = {}
        self.inventory: dict[str, Any] | None = None
        self.staged: list[str] = []

    async def run(
        self, argv: Sequence[str], *, env: Mapping[str, str], timeout_s: float, stdin: bytes | None = None
    ) -> CommandResult:
        self.calls.append(Call(list(argv), dict(env)))
        program = PurePath(argv[0]).name
        if program == "pg_dump":
            return await self._dump(list(argv))
        if program == "pg_restore":
            assert argv[1] == "--list"
            return CommandResult(1 if self.fail == "pg_restore" else 0, b";\n; Archive\n", FAILURE_DETAIL)
        assert program == "restic", argv
        if self.during_restic is not None:
            await self.during_restic(list(argv))
        if argv[1] == "forget":
            return CommandResult(11 if self.fail == "restic" else 0, b"", FAILURE_DETAIL)
        if argv[1] == "snapshots":
            return CommandResult(0, self.listing, "")
        assert argv[1] == "backup", argv
        self.staged, self.inventory = read_staged(Path(argv[argv.index("--") + 1]))
        if self.fail == "restic":
            return CommandResult(3, b"", FAILURE_DETAIL)
        summary = {"message_type": "summary", "snapshot_id": SNAPSHOT_ID}
        return CommandResult(0, json.dumps(summary).encode() + b"\n", "")

    async def _dump(self, argv: list[str]) -> CommandResult:
        [snapshot] = [arg.removeprefix("--snapshot=") for arg in argv if arg.startswith("--snapshot=")]
        self.snapshot_id = snapshot
        if self.during_dump is not None:
            await self.during_dump()
        self.dump_saw = await seen_in_snapshot(self.server, snapshot)
        if self.fail == "pg_dump":
            return CommandResult(1, b"", FAILURE_DETAIL)
        write_dump(Path(argv[argv.index("-f") + 1]))
        return CommandResult(0, b"", "")


@pytest.fixture
def backup_cfg(migrated: PgServer, tmp_path: Path) -> AppConfig:
    """The backup role against the shared server, with a staging, a CV store and a manifests directory of the
    test's own (0700, as the images make the volumes' mount points), and a local repository (never used: the
    commands are `FakeCommands`)."""
    for name in ("staging", "cv", "manifests"):
        (tmp_path / name).mkdir(mode=0o700)
    return config_for(
        migrated,
        "backup",
        JOBHUNTER_STAGING_DIR=str(tmp_path / "staging"),
        JOBHUNTER_CV_STORE_DIR=str(tmp_path / "cv"),
        JOBHUNTER_MANIFESTS_DIR=str(tmp_path / "manifests"),
        RESTIC_REPOSITORY=str(tmp_path / "repo"),
    )


@pytest.fixture
async def backup_engine(backup_cfg: AppConfig) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(backup_cfg, pool_size=2)
    try:
        yield engine
    finally:
        await engine.dispose()


def restic_cache(cfg: AppConfig) -> Path:
    """restic's cache in the staging directory, with a file in it: a set never removes it."""
    cache = cfg.staging_dir / ".restic-cache"
    cache.mkdir(mode=0o700)
    (cache / "config").write_bytes(b"restic's cache")
    return cache


async def one(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(statement, params)).one()


async def scalar(engine: AsyncEngine, statement: str) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(sa.text(statement))).scalar_one()


async def rows(engine: AsyncEngine, statement: sa.TextClause) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(statement)).all())


async def run_set(cfg: AppConfig, commands: FakeCommands, started: Any = None) -> int:
    at = utcnow() if started is None else started
    return await run_backup_set(cfg, Restic(cfg, commands), commands, clock=lambda: at)


async def test_a_set_records_its_inventory_from_one_snapshot(
    migrated: PgServer, backup_cfg: AppConfig, engine: AsyncEngine
) -> None:
    for _ in range(2):
        await manifest(engine, "executing")
    data_key = os.urandom(32)
    CvStore(backup_cfg.cv_store_dir).write(b"%PDF-1.7 a CV", [data_key])
    cache = restic_cache(backup_cfg)
    staged = backup_cfg.staging_dir / "set"
    staged.mkdir(mode=0o700)
    (staged / "jobhunter.dump").write_bytes(b"a dead run's half dump")  # a set starts from a fresh staging
    in_transaction: list[list[tuple[object, ...]]] = []

    async def a_purge_commits_during_the_dump() -> None:
        in_transaction.append(pg_rows(migrated, "superuser", _IDLE_IN_TRANSACTION))
        await manifest(engine, "executing")

    commands = FakeCommands(migrated, during_dump=a_purge_commits_during_the_dump)
    started = utcnow()
    set_id = await run_set(backup_cfg, commands, started)

    row = await one(engine, _SET, id=set_id)
    assert (row.status, row.error, row.restic_snapshot_id) == ("succeeded", None, SNAPSHOT_ID)
    assert (bytes(row.dump_sha256), row.dump_bytes) == (hashlib.sha256(DUMP).digest(), len(DUMP))
    assert row.started_at == started and row.finished_at is not None
    assert abs(row.db_snapshot_at - started) < timedelta(minutes=1)
    # One snapshot: the counts are every table as the dump saw it, so the manifest that a purge committed
    # during the dump is in neither; the set's own row was there, `running`.
    assert row.row_counts == commands.dump_saw
    assert row.manifest_count == row.row_counts["deletion_manifests"] == 2
    assert await scalar(engine, "SELECT count(*) FROM deletion_manifests") == 3
    assert row.row_counts["backup_sets"] == 1
    assert row.cv_file_count == 1
    # The export's transaction stayed open through the dump, as the backup role, and ended with the set.
    assert in_transaction == [[("jobhunter_backup",)]]
    assert commands.snapshot_id is not None and not await snapshot_imports(migrated, commands.snapshot_id)

    revision = await scalar(engine, "SELECT version_num FROM alembic_version")
    assert commands.inventory == {
        "set_id": set_id,
        "dump_sha256": hashlib.sha256(DUMP).hexdigest(),
        "alembic_revision": revision,
        "db_snapshot_at": row.db_snapshot_at.isoformat(),
        "manifest_count": 2,
        "row_counts": row.row_counts,
        "cv_file_count": 1,
        "app_version": __version__,
        "data_key_ids": [crypto.key_id(data_key).hex()],
    }
    assert commands.staged == ["backup_set.json", "jobhunter.dump"]

    dump, listing, backup = commands.calls
    db = backup_cfg.db
    assert dump.argv == [
        PG_DUMP,
        "-Fc",
        f"--snapshot={commands.snapshot_id}",
        "-f",
        str(staged / "jobhunter.dump"),
        "--no-password",
        "--host",
        db.host,
        "--port",
        str(db.port),
        "--username",
        "jobhunter_backup",
        "--dbname",
        db.name,
    ]
    # T24-1 (b): the password reaches pg_dump in its environment only, and no other command's.
    password = backup_cfg.secret("db_backup").decode("utf-8")
    assert dump.env == {"PGPASSWORD": password}
    assert not any(password in arg for call in commands.calls for arg in call.argv)
    assert not any("PGPASSWORD" in call.env or password in call.env.values() for call in (listing, backup))
    assert listing.argv == [PG_RESTORE, "--list", str(staged / "jobhunter.dump")]
    assert backup.argv[:2] == [RESTIC_BIN, "backup"]
    assert [backup.argv[i + 1] for i, arg in enumerate(backup.argv) if arg == "--tag"] == [
        "set",
        f"set:{set_id}",
    ]
    # The subdirectory `set/`: the restic cache in the staging directory stays out of the snapshot.
    assert backup.argv[backup.argv.index("--") + 1 :] == [
        str(staged),
        str(backup_cfg.cv_store_dir),
        str(backup_cfg.manifests_dir),
    ]
    assert sorted(path.name for path in backup_cfg.staging_dir.iterdir()) == [".restic-cache"]
    assert (cache / "config").read_bytes() == b"restic's cache"


async def test_the_set_names_the_data_key_ids_of_its_ciphertexts(
    migrated: PgServer, backup_cfg: AppConfig, engine: AsyncEngine
) -> None:
    """Deviation 31: the keys a restore of the set needs, from the snapshot's sealed CV texts and TOTP secret
    and from the first bytes of every CV file."""
    current, previous, older, unused = (os.urandom(32) for _ in range(4))
    store = CvStore(backup_cfg.cv_store_dir)
    store.write(b"a CV under the current key", [current])
    store.write(b"a CV under the previous key", [previous, current])
    # Not a file of the store: its header names no key a restore of the CV files needs.
    (backup_cfg.cv_store_dir / "notes.bin").write_bytes(crypto.encrypt(unused, b"not a CV", aad=b"x"))
    async with engine.begin() as conn:
        cv_file_id = (
            await conn.execute(
                sa.text(
                    "INSERT INTO cv_files (sha256, mime, size_bytes) VALUES ('\\x01', 'text/plain', 1) "
                    "RETURNING id"
                )
            )
        ).scalar_one()
        sealed = store.encrypt_text("a pasted CV", [current], cv_file_id=cv_file_id)
        await conn.execute(
            sa.text("UPDATE cv_files SET raw_text_enc = :t WHERE id = :id"), {"t": sealed, "id": cv_file_id}
        )
        totp = crypto.encrypt(older, b"a TOTP secret", aad=b"totp")
        await conn.execute(
            sa.text(
                "INSERT INTO users (id, username, password_hash, totp_secret_enc, password_changed_at) "
                "VALUES (1, 'owner', 'x', :t, now())"
            ),
            {"t": totp},
        )
    commands = FakeCommands(migrated)

    await run_set(backup_cfg, commands)

    assert commands.inventory is not None
    assert commands.inventory["data_key_ids"] == sorted(
        crypto.key_id(k).hex() for k in (current, previous, older)
    )
    assert commands.inventory["cv_file_count"] == 2


@pytest.mark.parametrize(
    ("fail", "error"),
    [
        ("pg_dump", "dump:CommandFailed"),
        ("pg_restore", "verify:CommandFailed"),
        ("cv_store", "inventory:FileNotFoundError"),
        ("restic", "upload:ResticError"),
    ],
)
async def test_any_failing_step_fails_the_set_and_clears_staging(
    migrated: PgServer, backup_cfg: AppConfig, engine: AsyncEngine, tmp_path: Path, fail: str, error: str
) -> None:
    cfg = backup_cfg
    if fail == "cv_store":  # the CV volume is not there: the inventory cannot list its files
        cfg = config_for(
            migrated,
            "backup",
            JOBHUNTER_STAGING_DIR=str(backup_cfg.staging_dir),
            JOBHUNTER_CV_STORE_DIR=str(tmp_path / "absent"),
            JOBHUNTER_MANIFESTS_DIR=str(backup_cfg.manifests_dir),
            RESTIC_REPOSITORY=str(tmp_path / "repo"),
        )
    cache = restic_cache(cfg)
    commands = FakeCommands(migrated, fail=fail)

    with pytest.raises(BackupSetFailed) as failed:
        await run_set(cfg, commands)

    assert failed.value.error == error  # `<step>:<error class>`, never the command's output (T24-1 (b))
    row = await one(engine, _SET, id=failed.value.set_id)
    assert (row.status, row.error, row.restic_snapshot_id, row.dump_sha256) == ("failed", error, None, None)
    assert row.finished_at is not None
    assert sorted(path.name for path in cfg.staging_dir.iterdir()) == [".restic-cache"]
    assert (cache / "config").read_bytes() == b"restic's cache"
    # T24-1 (c): the export's transaction ended on the failure path too.
    assert commands.snapshot_id is not None and not await snapshot_imports(migrated, commands.snapshot_id)
    assert pg_rows(migrated, "superuser", _IDLE_IN_TRANSACTION) == []
    # The steps after the failing one never ran.
    ran = [PurePath(call.argv[0]).name for call in commands.calls]
    assert (
        ran
        == {
            "pg_dump": ["pg_dump"],
            "pg_restore": ["pg_dump", "pg_restore"],
            "cv_store": ["pg_dump", "pg_restore"],
            "restic": ["pg_dump", "pg_restore", "restic"],
        }[fail]
    )


async def test_the_backup_role_writes_only_the_backup_tables(
    migrated: PgServer, backup_cfg: AppConfig, backup_engine: AsyncEngine, engine: AsyncEngine
) -> None:
    written = {
        table: tuple(privileges)
        for table, *privileges in pg_rows(migrated, "superuser", _PRIVILEGES)
        if any(privileges)
    }
    # INSERT and UPDATE on its own four tables (`migrate`'s explicit grants), DELETE only on the two receipt
    # tables (the prune's removal of a false receipt, final fix D1), and nothing else: never a TRUNCATE,
    # never an application table.
    assert written == {
        **dict.fromkeys(BACKUP_GRANT_TABLES, (True, True, False, False)),
        **dict.fromkeys(BACKUP_DELETE_TABLES, (True, True, True, False)),
    }
    with pytest.raises(sa.exc.ProgrammingError) as denied:
        async with backup_engine.begin() as conn:
            await conn.execute(sa.text("DELETE FROM backup_sets"))
    assert pg_error(denied.value).sqlstate == "42501"  # insufficient privilege
    # No UPDATE of any column of `deletion_manifests`: the backup role returns a shipped manifest to
    # `completed` only through `unship_manifest` (final fix E2), the one function it may execute that runs as
    # its owner; the application role may execute none.
    assert pg_rows(migrated, "superuser", _MANIFEST_COLUMNS_UPDATED) == []
    assert pg_rows(migrated, "superuser", _DEFINER_FUNCTIONS, "jobhunter_backup") == [("unship_manifest",)]
    assert pg_rows(migrated, "superuser", _DEFINER_FUNCTIONS, "jobhunter_app") == []
    # Ruling T26-1 (a) T24-4 M3: no INSERT and no UPDATE on a table of the application either, nor of a
    # manifest's status, times or content.
    for statement in (
        "INSERT INTO alerts (code, severity, dedup_key, message) VALUES ('t', 'info', 'backup-role', 'x')",
        "UPDATE settings_versions SET version_no = version_no",
        "UPDATE deletion_manifests SET status = status",
        "UPDATE deletion_manifests SET completed_at = completed_at",
        "UPDATE deletion_manifests SET scope = scope",
        "DELETE FROM deletion_manifests",
    ):
        with pytest.raises(sa.exc.ProgrammingError) as refused:
            async with backup_engine.begin() as conn:
                await conn.execute(sa.text(statement))
        assert pg_error(refused.value).sqlstate == "42501", statement  # insufficient privilege
    # A whole set, as that role, writes its row and moves it to `succeeded`.
    set_id = await run_set(backup_cfg, FakeCommands(migrated))
    assert (await one(engine, _SET, id=set_id)).status == "succeeded"


async def test_two_backup_jobs_never_run_at_once(
    migrated: PgServer, backup_cfg: AppConfig, backup_engine: AsyncEngine, engine: AsyncEngine
) -> None:
    """Ruling T26-1 (a) T24-4 M1: a set holds the backup jobs' lock (7261006) for its whole run. A second set,
    a prune and a restore test started while it dumps find the lock held: each writes no row, touches no
    staging, logs `backup_job_busy` and raises `BackupJobBusy`. Once the first has ended, a set runs again."""
    found: dict[str, Any] = {}

    async def the_others_start_meanwhile() -> None:
        marker = backup_cfg.staging_dir / "set" / "the first set's"
        marker.write_bytes(b"")
        with capture_logs() as logs:
            for job in ("set", "prune", "restore_test"):
                with pytest.raises(BackupJobBusy) as busy:
                    if job == "set":
                        await run_set(backup_cfg, FakeCommands(migrated))
                    elif job == "prune":
                        await run_prune(
                            backup_cfg,
                            Restic(backup_cfg, FakeCommands(migrated)),
                            backup_engine,
                            clock=utcnow,
                        )
                    else:
                        await run_restore_test(
                            backup_cfg,
                            Restic(backup_cfg, FakeCommands(migrated)),
                            FakeCommands(migrated),
                            backup_engine,
                            clock=utcnow,
                        )
                assert (busy.value.job, busy.value.error) == (job, "backup_job_busy")
        found["busy"] = [(e["job"], e["lock_key"]) for e in logs if e["event"] == "backup_job_busy"]
        found["marker"] = marker.exists()
        found["rows"] = (
            await scalar(engine, "SELECT count(*) FROM backup_sets"),
            await scalar(engine, "SELECT count(*) FROM backup_runs"),
        )
        marker.unlink()

    first = await run_set(backup_cfg, FakeCommands(migrated, during_dump=the_others_start_meanwhile))

    assert found == {
        "busy": [("set", 7261006), ("prune", 7261006), ("restore_test", 7261006)],
        "marker": True,
        "rows": (1, 0),
    }
    assert (await one(engine, _SET, id=first)).status == "succeeded"
    assert (
        await one(engine, _SET, id=await run_set(backup_cfg, FakeCommands(migrated)))
    ).status == "succeeded"


# Restic's listing of four `manifests` snapshots, oldest first: two older than the latest receipt's (one an
# earlier receipt's, one incomplete), the latest receipt's, and an incomplete one newer than it.
OLDER_RECEIPTED, OLDER_PARTIAL, RECEIPTED, NEWER_PARTIAL = ("a1" * 32, "b2" * 32, "c3" * 32, "d4" * 32)
_RECEIPT: Final = sa.text(
    "INSERT INTO manifest_shipments (restic_snapshot_id, shipped_at) VALUES (:snapshot, now())"
)


def manifests_listing() -> bytes:
    snapshots = (OLDER_RECEIPTED, OLDER_PARTIAL, RECEIPTED, NEWER_PARTIAL)
    return json.dumps(
        [
            {"id": snapshot, "time": f"2026-11-1{n}T03:00:00Z", "tags": ["manifests"], "paths": ["/data/m"]}
            for n, snapshot in enumerate(snapshots)
        ]
    ).encode()


SET_FORGET: Final = [
    RESTIC_BIN,
    "forget",
    "--retry-lock",
    "30m",
    "--host",
    "jobhunter-backup",
    "--tag",
    "set",
    "--keep-daily",
    "7",
    "--keep-weekly",
    "4",
    "--keep-monthly",
    "3",
    "--prune",
]


async def test_prune_forgets_the_manifests_snapshots_older_than_the_latest_receipt(
    migrated: PgServer,
    clean_db: None,
    backup_cfg: AppConfig,
    backup_engine: AsyncEngine,
    engine: AsyncEngine,
) -> None:
    """The prune reads the latest receipt's snapshot and forgets, by id, only the `manifests` snapshots
    strictly older than it (final fix C1): the receipted one is complete and covers every manifest shipped up
    to it, and a newer one, incomplete or not, can only add to the restore's union. Then it forgets the sets
    beyond the counts kept, pruning what both forgets left."""
    async with backup_engine.begin() as conn:
        for snapshot in (OLDER_RECEIPTED, RECEIPTED):  # the latest receipt is the newest shipment row
            await conn.execute(_RECEIPT, {"snapshot": snapshot})
    commands = FakeCommands(migrated, listing=manifests_listing())
    started = utcnow()

    run_id = await run_prune(backup_cfg, Restic(backup_cfg, commands), backup_engine, clock=lambda: started)

    listing, manifests, forget = commands.calls
    assert listing.argv[1:] == [
        "snapshots",
        "--retry-lock",
        "30m",
        "--host",
        "jobhunter-backup",
        "--json",
        "--tag",
        "manifests",
    ]
    assert manifests.argv == [
        RESTIC_BIN,
        "forget",
        "--retry-lock",
        "30m",
        "--host",
        "jobhunter-backup",
        "--",
        OLDER_RECEIPTED,
        OLDER_PARTIAL,
    ]
    # R13: the filter is the `set` tag, so this forget never considers a `manifests` snapshot.
    assert forget.argv == SET_FORGET
    row = await one(engine, _RUN, id=run_id)
    assert (row.kind, row.status, row.error, row.backup_set_id) == ("prune", "succeeded", None, None)
    assert row.report == {
        "keep_daily": 7,
        "keep_weekly": 4,
        "keep_monthly": 3,
        "manifests_forgotten": 2,
        "manifests_skipped": None,  # nothing skipped (final fix D3)
        "receipts_removed": [],
    }
    assert row.started_at == started and row.finished_at is not None


# The report's keys on the `manifests` snapshots: how many were forgotten, why none were (`None` when not
# skipped), and the false receipts removed.
_MANIFESTS_REPORT: Final = ("manifests_forgotten", "manifests_skipped", "receipts_removed")


async def test_prune_without_a_receipt_forgets_no_manifests_snapshot(
    migrated: PgServer,
    clean_db: None,
    backup_cfg: AppConfig,
    backup_engine: AsyncEngine,
    engine: AsyncEngine,
) -> None:
    """With no receipt, nothing proves a `manifests` snapshot complete, so the prune lists none and forgets
    none (final fix C1), and its report says why (`manifests_skipped`, final fix D3). The sets' forget runs
    as always."""
    commands = FakeCommands(migrated, listing=manifests_listing())

    run_id = await run_prune(backup_cfg, Restic(backup_cfg, commands), backup_engine, clock=utcnow)

    assert [call.argv for call in commands.calls] == [SET_FORGET]
    row = await one(engine, _RUN, id=run_id)
    assert row.status == "succeeded"
    assert {key: row.report[key] for key in _MANIFESTS_REPORT} == {
        "manifests_forgotten": 0,
        "manifests_skipped": "no_receipt",
        "receipts_removed": [],
    }


_ITEM: Final = sa.text(
    "INSERT INTO manifest_shipment_items (shipment_id, manifest_uuid, file_sha256) "
    "VALUES (:shipment, gen_random_uuid(), :sha)"
)
_RECEIPTS: Final = sa.text("SELECT id, restic_snapshot_id FROM manifest_shipments ORDER BY id")
_ITEMS: Final = sa.text("SELECT shipment_id, count(*) FROM manifest_shipment_items GROUP BY shipment_id")


@pytest.mark.parametrize("listed", [True, False], ids=["down_to_a_listed_receipt", "until_none_remains"])
async def test_prune_removes_each_receipt_whose_snapshot_restic_does_not_list(
    migrated: PgServer,
    clean_db: None,
    backup_cfg: AppConfig,
    backup_engine: AsyncEngine,
    engine: AsyncEngine,
    listed: bool,
) -> None:
    """A receipt whose snapshot restic does not list is false (final fix D1 (b)): in the same step, under
    the shipment lock, the prune removes it with its items, logging the shipment id, the snapshot id and the
    item count, then the receipt below it, and so on until restic lists one or none remains. It forgets no
    `manifests` snapshot in that run and reports the receipts it removed. The sets' forget runs as always."""
    oldest = RECEIPTED if listed else "e4" * 32
    snapshots = (oldest, "e5" * 32, "e6" * 32)  # restic lists RECEIPTED, never an `e…` one
    async with backup_engine.begin() as conn:
        for count, snapshot in enumerate(snapshots, start=1):
            await conn.execute(_RECEIPT, {"snapshot": snapshot})
            shipment = (await conn.execute(sa.text("SELECT max(id) FROM manifest_shipments"))).scalar_one()
            for _ in range(count):
                await conn.execute(_ITEM, {"shipment": shipment, "sha": os.urandom(32)})
    receipts = [row.id for row in await rows(engine, _RECEIPTS)]
    commands = FakeCommands(migrated, listing=manifests_listing())

    with capture_logs() as logs:
        run_id = await run_prune(backup_cfg, Restic(backup_cfg, commands), backup_engine, clock=utcnow)

    assert [call.argv[1] for call in commands.calls] == ["snapshots", "forget"]
    assert commands.calls[-1].argv == SET_FORGET
    removed = receipts[:0:-1] if listed else receipts[::-1]
    assert [row.id for row in await rows(engine, _RECEIPTS)] == (receipts[:1] if listed else [])
    assert {row.shipment_id: row.count for row in await rows(engine, _ITEMS)} == (
        {receipts[0]: 1} if listed else {}
    )
    by_id = dict(zip(receipts, zip(snapshots, range(1, 4), strict=True), strict=True))
    assert [
        (entry["shipment_id"], entry["snapshot_id"], entry["items"])
        for entry in logs
        if entry["event"] == "manifest_receipt_removed"
    ] == [(shipment, *by_id[shipment]) for shipment in removed]
    row = await one(engine, _RUN, id=run_id)
    assert row.status == "succeeded"
    assert {key: row.report[key] for key in _MANIFESTS_REPORT} == {
        "manifests_forgotten": 0,
        "manifests_skipped": "receipt_not_listed",  # final fix D3
        "receipts_removed": removed,
    }


_ADVISORY_HELD: Final = (
    "SELECT objid::bigint FROM pg_locks WHERE locktype = 'advisory' AND objsubid = 1 AND granted "
    "AND objid IN (7261006, 7261007) ORDER BY objid"
)


async def test_the_prune_holds_the_shipment_lock_inside_its_job_lock_through_its_forget(
    migrated: PgServer,
    clean_db: None,
    backup_cfg: AppConfig,
    backup_engine: AsyncEngine,
) -> None:
    """The prune takes the shipment lock (7261007) after its job lock (7261006) and holds it from its receipt
    read through the listing and the forget of the `manifests` snapshots (final fix D1 (a)), so no shipment
    lands between them; it releases it before the sets' forget, whose prune can take long."""
    async with backup_engine.begin() as conn:
        for snapshot in (OLDER_RECEIPTED, RECEIPTED):
            await conn.execute(_RECEIPT, {"snapshot": snapshot})
    held: list[tuple[str, list[object]]] = []

    async def locks_held(argv: list[str]) -> None:
        held.append((argv[1], [row[0] for row in pg_rows(migrated, "superuser", _ADVISORY_HELD)]))

    commands = FakeCommands(migrated, listing=manifests_listing(), during_restic=locks_held)

    await run_prune(backup_cfg, Restic(backup_cfg, commands), backup_engine, clock=utcnow)

    assert held == [
        ("snapshots", [7261006, 7261007]),
        ("forget", [7261006, 7261007]),  # the `manifests` snapshots, by id
        ("forget", [7261006]),  # the sets
    ]


async def test_a_prune_whose_shipment_lock_session_ended_forgets_no_manifests_snapshot(
    migrated: PgServer,
    clean_db: None,
    backup_cfg: AppConfig,
    backup_engine: AsyncEngine,
    engine: AsyncEngine,
) -> None:
    """The prune confirms it still holds the shipment lock just before it forgets or removes anything (final
    fix D2, as the shipper does before restic starts): a lock session that ended after the listing took the
    lock with it, and a shipment may have landed since. The run fails `prune:ShipLockLost`, having forgotten
    no snapshot."""
    async with backup_engine.begin() as conn:
        for snapshot in (OLDER_RECEIPTED, RECEIPTED):
            await conn.execute(_RECEIPT, {"snapshot": snapshot})

    async def the_session_ends(argv: list[str]) -> None:
        if argv[1] == "snapshots":
            ended = pg_rows(
                migrated,
                "superuser",
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE application_name = 'jobhunter-backup-prune'",
            )
            assert ended == [(True,)]

    commands = FakeCommands(migrated, listing=manifests_listing(), during_restic=the_session_ends)

    with pytest.raises(PruneFailed) as failed:
        await run_prune(backup_cfg, Restic(backup_cfg, commands), backup_engine, clock=utcnow)

    assert [call.argv[1] for call in commands.calls] == ["snapshots"]
    row = await one(engine, _RUN, id=failed.value.run_id)
    assert (row.status, row.error) == ("failed", "prune:ShipLockLost")


async def test_prune_takes_its_counts_from_the_settings_and_records_a_failure(
    migrated: PgServer,
    clean_db: None,
    backup_cfg: AppConfig,
    backup_engine: AsyncEngine,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """The counts are the settings'; a failure fails the run with its step and class only."""
    kept = config_for(
        migrated,
        "backup",
        JOBHUNTER_STAGING_DIR=str(backup_cfg.staging_dir),
        RESTIC_REPOSITORY=str(tmp_path / "repo"),
        JOBHUNTER_BACKUP_KEEP_DAILY="14",
        JOBHUNTER_BACKUP_KEEP_WEEKLY="8",
        JOBHUNTER_BACKUP_KEEP_MONTHLY="6",
    )
    counted = FakeCommands(migrated)
    await run_prune(kept, Restic(kept, counted), backup_engine, clock=utcnow)
    [forget] = counted.calls  # no receipt: no `manifests` snapshot is considered
    assert forget.argv[-7:] == ["--keep-daily", "14", "--keep-weekly", "8", "--keep-monthly", "6", "--prune"]
    failing = FakeCommands(migrated, fail="restic")
    with pytest.raises(PruneFailed) as failed:
        await run_prune(kept, Restic(kept, failing), backup_engine, clock=utcnow)
    row = await one(engine, _RUN, id=failed.value.run_id)
    assert (row.kind, row.status, row.error) == ("prune", "failed", "prune:ResticError")
    assert failed.value.error == "prune:ResticError" and row.finished_at is not None


async def test_retention_keeps_the_newest_succeeded_set(
    migrated: PgServer,
    engine: AsyncEngine,
    current_epoch: int,
    queue_app: procrastinate.App,
    cv_store_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`backup_records`: the finished runs, then the finished sets, 13 months after they finished; never the
    newest succeeded set (here as old as the others: backups that stopped working), a running row, or a set
    that a kept run names."""
    now = utcnow()
    old = now - THIRTEEN_MONTHS - timedelta(days=1)
    oldest_ok = await backup_set(engine, "succeeded", old - timedelta(days=20))
    old_failed = await backup_set(engine, "failed", old - timedelta(days=15))
    named = await backup_set(engine, "succeeded", old - timedelta(days=12))
    newest_ok = await backup_set(engine, "succeeded", old - timedelta(days=10))
    young_failed = await backup_set(engine, "failed", now - timedelta(days=1))
    async with engine.begin() as conn:
        still_running = (
            await conn.execute(
                sa.text("INSERT INTO backup_sets (status, started_at) VALUES ('running', :at) RETURNING id"),
                {"at": old},
            )
        ).scalar_one()
        running_test = (
            await conn.execute(
                sa.text(
                    "INSERT INTO backup_runs (kind, backup_set_id, status, started_at) "
                    "VALUES ('restore_test', :set, 'running', :at) RETURNING id"
                ),
                {"set": named, "at": old},
            )
        ).scalar_one()
    old_prune = await backup_run(engine, "succeeded", old - timedelta(days=1), kind="prune")
    old_test = await backup_run(engine, "failed", old - timedelta(days=2), set_id=oldest_ok)
    young_prune = await backup_run(engine, "succeeded", now - timedelta(days=1), kind="prune")
    rule = [rule for rule in retention.RULES if rule.name == "backup_records"]
    assert [r.window for r in rule] == [THIRTEEN_MONTHS]
    monkeypatch.setattr(retention, "RULES", tuple(rule))

    async with open_saver(migrated.dsn("app")) as saver:
        report = await run_retention_daily(
            engine, queue_app, saver, cv_store=CvStore(cv_store_dir), now=now, epoch=current_epoch
        )

    assert report == {"backup_records": 4}
    async with engine.connect() as conn:
        sets = set((await conn.execute(sa.text("SELECT id FROM backup_sets"))).scalars())
        runs = set((await conn.execute(sa.text("SELECT id FROM backup_runs"))).scalars())
    assert sets == {named, newest_ok, young_failed, still_running}
    assert runs == {running_test, young_prune}
    assert {oldest_ok, old_failed}.isdisjoint(sets) and {old_prune, old_test}.isdisjoint(runs)


async def test_retention_keeps_the_newest_succeeded_restore_test(
    migrated: PgServer,
    engine: AsyncEngine,
    current_epoch: int,
    queue_app: procrastinate.App,
    cv_store_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruling T26-1 (a) T24-4 M5: `backup_records` keeps the newest succeeded restore test, however old (the
    restore tests' age reference: restore tests that stopped working), as it keeps the newest succeeded set,
    and so the set it names; an older succeeded one and a failed one go."""
    now = utcnow()
    old = now - THIRTEEN_MONTHS - timedelta(days=1)
    newest_set = await backup_set(engine, "succeeded", old - timedelta(days=1))
    tested = await backup_set(engine, "succeeded", old - timedelta(days=30))
    older_test = await backup_run(engine, "succeeded", old - timedelta(days=40), set_id=tested)
    newest_test = await backup_run(engine, "succeeded", old - timedelta(days=20), set_id=tested)
    failed_test = await backup_run(engine, "failed", old - timedelta(days=10), set_id=newest_set)
    rule = [rule for rule in retention.RULES if rule.name == "backup_records"]
    monkeypatch.setattr(retention, "RULES", tuple(rule))

    async with open_saver(migrated.dsn("app")) as saver:
        report = await run_retention_daily(
            engine, queue_app, saver, cv_store=CvStore(cv_store_dir), now=now, epoch=current_epoch
        )

    async with engine.connect() as conn:
        sets = set((await conn.execute(sa.text("SELECT id FROM backup_sets"))).scalars())
        runs = set((await conn.execute(sa.text("SELECT id FROM backup_runs"))).scalars())
    assert runs == {newest_test}
    assert sets == {newest_set, tested}  # the newest succeeded set, and the set the kept test names
    assert {older_test, failed_test}.isdisjoint(runs) and report == {"backup_records": 2}
