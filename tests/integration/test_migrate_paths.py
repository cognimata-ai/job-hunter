"""The `migrate` paths (spec §18.2 "Migrate paths", §19.3, §19.5; plan M5 Task 23, R25, Deviation 38).

`migrate` completes on a first installation, with and without manifests on the volume; on an upgrade from the
preceding release's schema with data; and on a restored database whose manifest stream is newer than the dump,
both as a disaster restore runs it (every step, on the application database) and as the restore test runs its
subset (`--database`, `--db-role restore`, `--steps sequences,alembic,replay`, on a scratch database). In
every case the backup role's grants exist afterwards, and no identity value the run allocates is at or below
any manifest's bound. On a first installation the queue's sequences exist only from step 5, which ends with an
advancement of its own (Codex plan round 11).

Each test has a server of its own; the bounds that cover every table take the identity tables of the code head
from the shared migrated server. Step 7 replays the stream (M5 Task 25): every manifest here is of a kind
whose procedure deletes only the rows its operations name (a golden-case deletion, a tombstone), so a
manifest the database has no row for is inserted, replayed and completed without touching the rows of the
test, and the next `migrate` finds it `satisfied`. A tombstone's `max_ids` holds the one key its kind reads,
`score_targets` (final review M3: a replay refuses a manifest without it). Every manifest and row is
invented.
"""

import asyncio
import hashlib
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import procrastinate
import pytest
from alembic import command
from alembic.script import ScriptDirectory
from jobhunter.db.alembic_runner import alembic_config, code_head
from jobhunter.db.migrate import BACKUP_GRANT_TABLES, MIGRATE_ORDER, StepGroup, run_migrate_async
from jobhunter.db.session import make_null_engine
from jobhunter.ids import uuid7
from jobhunter.privacy.manifests import (
    FileRoots,
    ManifestContent,
    ManifestKind,
    Operation,
    canonical_bytes,
    complete_manifest,
    identity_columns,
    insert_manifest,
    manifest_file_name,
    record_manifest,
    write_manifest_file,
)
from jobhunter.privacy.replay import Classified, classify_stream
from procrastinate import PsycopgConnector
from structlog.testing import capture_logs

from tests.support.fixtures import ConfigFactory
from tests.support.pg import PgRole, PgServer, pg_one, pg_rows, table_counts

pytestmark = pytest.mark.integration

NOW: Final = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
APP_DB: Final = "jobhunter"
SCRATCH: Final = "jobhunter_restore_test"
# The steps that end with an identity advancement, in their order.
ADVANCING_STEPS: Final = ("reconstruct_and_advance", "advance_again", "queue_and_checkpointer")
Column = tuple[str, str]


def content(
    *,
    kind: ManifestKind,
    max_ids: Mapping[str, int],
    operations: Iterable[Operation] = (),
    manifest_uuid: str | None = None,
) -> ManifestContent:
    stamp = NOW.isoformat()
    built: dict[str, Any] = {
        "format": 1,
        "manifest_uuid": manifest_uuid or str(uuid7()),
        "kind": kind,
        "admitted_at": stamp,
        "fenced_at": stamp,
        "max_ids": dict(max_ids),
        "operations": list(operations),
    }
    return built  # type: ignore[return-value]  # the TypedDict's keys are built above


def listed_jobs(job_ids: Sequence[int]) -> list[Operation]:
    """The deletion of the queue jobs a manifest lists by id, their events first (under the jobs' bound: the
    events' `job_id`), as delete-everything lists them."""
    return [
        {
            "op": "row_delete",
            "table": "procrastinate_events",
            "ids": list(job_ids),
            "bound": {"by": "parent", "table": "procrastinate_jobs", "column": "job_id"},
        },
        {"op": "row_delete", "table": "procrastinate_jobs", "ids": list(job_ids), "bound": {"by": "id"}},
    ]


async def columns_of(server: PgServer, role: PgRole = "migrator", db: str = APP_DB) -> list[Column]:
    """The identity and serial columns of `public` in `db`, as the advancement finds them."""
    engine = make_null_engine(server.dsn(role, db, driver="sqlalchemy"))
    try:
        async with engine.connect() as conn:
            return await identity_columns(conn)
    finally:
        await engine.dispose()


def next_value(server: PgServer, table: str, column: str, role: PgRole, db: str) -> int:
    found = pg_one(server, role, "SELECT pg_get_serial_sequence(%s, %s)", f"public.{table}", column, db=db)
    assert found is not None
    state = pg_one(server, role, f"SELECT last_value, is_called FROM {found[0]}", db=db)  # noqa: S608
    assert state is not None
    return int(str(state[0])) + 1 if state[1] else int(str(state[0]))


def maxima(
    server: PgServer, columns: Iterable[Column], role: PgRole = "migrator", db: str = APP_DB
) -> dict[str, int]:
    found: dict[str, int] = {}
    for table, column in columns:
        row = pg_one(server, role, f'SELECT coalesce(max("{column}"), 0) FROM public."{table}"', db=db)  # noqa: S608
        assert row is not None
        found[table] = int(str(row[0]))
    return found


def allocated_at_or_below(
    server: PgServer,
    columns: Iterable[Column],
    bounds: Mapping[str, int],
    before: Mapping[str, int],
    role: PgRole = "migrator",
    db: str = APP_DB,
) -> dict[str, str]:
    """Per bounded table, what breaks "no identity value the run allocated is at or below the bound": a row
    above the table's maximum `before` the run and at or below the bound, or a next value at or below it."""
    wrong: dict[str, str] = {}
    for table, column in columns:
        bound = bounds.get(table)
        if bound is None:
            continue
        row = pg_one(
            server,
            role,
            f'SELECT count(*) FROM public."{table}" WHERE "{column}" > %s AND "{column}" <= %s',  # noqa: S608
            before.get(table, 0),
            bound,
            db=db,
        )
        if row != (0,):
            wrong[table] = f"{row} rows allocated at or below {bound}"
        if (following := next_value(server, table, column, role, db)) <= bound:
            wrong[table] = f"next value {following} at or below {bound}"
    return wrong


def backup_grants(server: PgServer, role: PgRole = "migrator", db: str = APP_DB) -> bool:
    row = pg_one(
        server,
        role,
        "SELECT bool_and(has_table_privilege('jobhunter_backup', 'public.' || t, 'INSERT') "
        "AND has_table_privilege('jobhunter_backup', 'public.' || t, 'UPDATE')) "
        "FROM unnest(CAST(%s AS text[])) AS t",
        list(BACKUP_GRANT_TABLES),
        db=db,
    )
    return row == (True,)


def files_in(directory: Path) -> dict[str, bytes]:
    """Each file of `directory` with its bytes."""
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir())}


def advanced_by_step(logs: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    return {e["step"]: e["advanced"] for e in logs if e["event"] == "migrate_identities_advanced"}


def insert_alerts(server: PgServer, n: int, role: PgRole = "migrator", db: str = APP_DB) -> list[int]:
    ids: list[int] = []
    for _ in range(n):
        row = pg_one(
            server,
            role,
            "INSERT INTO alerts (code, severity, dedup_key, message) "
            "VALUES ('test.migrate_paths', 'info', %s, 'an invented alert') RETURNING id",
            f"paths:{uuid7()}",
            db=db,
        )
        assert row is not None
        ids.append(int(str(row[0])))
    return ids


async def enqueue(server: PgServer, n: int) -> list[int]:
    """`n` queue jobs deferred as the worker defers them (the application role), by id."""
    app = procrastinate.App(connector=PsycopgConnector(conninfo=server.dsn("app"), min_size=1, max_size=1))
    async with app.open_async():
        return [await app.configure_task("jobhunter.tests.queue_probe").defer_async() for _ in range(n)]


@pytest.mark.parametrize("with_manifests", [False, True], ids=["empty_volume", "manifests_on_the_volume"])
async def test_first_installation_with_and_without_manifests_on_the_volume(
    fresh_pg_server: PgServer, migrated: PgServer, make_config: ConfigFactory, with_manifests: bool
) -> None:
    server = fresh_pg_server
    cfg = make_config("migrate", server)
    head = await columns_of(migrated)
    bounds: dict[str, int] = {}
    if with_manifests:
        # An earlier installation's manifest with a bound on every table, and a later one that lists a queue
        # job above them.
        bounds = {table: 1000 + 10 * i for i, (table, _) in enumerate(head)}
        write_manifest_file(cfg.manifests_dir, content(kind="golden_case_delete", max_ids=bounds))
        write_manifest_file(
            cfg.manifests_dir,
            content(kind="job_tombstone", max_ids={"score_targets": 0}, operations=listed_jobs([5000])),
        )
        bounds["procrastinate_jobs"] = 5000
    volume = files_in(cfg.manifests_dir)

    with capture_logs() as logs:
        assert await run_migrate_async(cfg, now=NOW) == list(MIGRATE_ORDER)

    assert pg_one(server, "migrator", "SELECT version_num FROM alembic_version") == (code_head(),)
    assert backup_grants(server)
    assert await columns_of(server) == head
    assert allocated_at_or_below(server, head, bounds, before={}) == {}
    assert files_in(cfg.manifests_dir) == volume  # read, never written
    advanced = advanced_by_step(logs)
    assert list(advanced) == list(ADVANCING_STEPS)
    if not with_manifests:
        assert advanced == {step: {} for step in ADVANCING_STEPS}  # no bound: no sequence set
        return
    queue = {table for table in bounds if table.startswith("procrastinate_")}
    assert advanced["reconstruct_and_advance"] == {}  # no table exists yet
    assert set(advanced["advance_again"]) == set(bounds) - queue  # every table Alembic created
    assert advanced["queue_and_checkpointer"] == {
        table: bounds[table] for table in queue
    }  # created in step 5


async def test_a_first_installation_advances_the_queue_past_listed_job_ids(
    fresh_pg_server: PgServer, make_config: ConfigFactory
) -> None:
    server = fresh_pg_server
    cfg = make_config("migrate", server)
    listed = [17, 23, 42]
    # Its `max_ids` bound on the queue lies below the ids it lists: the listed ids themselves must count.
    listing = content(
        kind="job_tombstone",
        max_ids={"procrastinate_jobs": 10, "score_targets": 0},
        operations=listed_jobs(listed),
    )
    write_manifest_file(cfg.manifests_dir, listing)
    with capture_logs() as logs:
        await run_migrate_async(cfg, now=NOW)
    # The database had no row of it: step 7 inserted, replayed and completed it.
    assert [(e["replayed"], e["pending_completed"]) for e in logs if e["event"] == "migrate_replayed"] == [
        (1, 0)
    ]
    # The queue's sequence exists only from step 5, whose own advancement moves it past the listed ids.
    advanced = advanced_by_step(logs)
    assert "procrastinate_jobs" not in advanced["reconstruct_and_advance"] | advanced["advance_again"]
    assert advanced["queue_and_checkpointer"] == {"procrastinate_jobs": 42}
    jobs = await enqueue(server, 3)
    assert min(jobs) > max(listed)

    columns = await columns_of(server)
    counts = table_counts(server)
    queue = pg_rows(
        server, "migrator", "SELECT id, CAST(status AS text), task_name FROM procrastinate_jobs ORDER BY id"
    )
    before = maxima(server, columns)
    roots = FileRoots(cv_store=cfg.cv_store_dir, exports=cfg.exports_dir)
    engine = make_null_engine(server.dsn("migrator", driver="sqlalchemy"))
    try:
        async with engine.connect() as conn:
            classified = await classify_stream(conn, cfg.manifests_dir, files=roots)
    finally:
        await engine.dispose()
    assert classified == [Classified(uuid.UUID(listing["manifest_uuid"]), "satisfied")]
    with capture_logs() as again:
        await run_migrate_async(cfg, now=NOW)
    # The second migrate changes no row and allocates no id at or below the largest listed one.
    assert table_counts(server) == counts
    assert (
        pg_rows(
            server,
            "migrator",
            "SELECT id, CAST(status AS text), task_name FROM procrastinate_jobs ORDER BY id",
        )
        == queue
    )
    assert advanced_by_step(again) == {step: {} for step in ADVANCING_STEPS}
    # Step 7 replays nothing: the manifest is satisfied.
    assert [e["outcomes"] for e in again if e["event"] == "replay_classified"] == [{"satisfied": 1}]
    assert not [e for e in again if e["event"].startswith("replay_phase")]
    assert allocated_at_or_below(server, columns, {"procrastinate_jobs": max(listed)}, before) == {}
    assert await enqueue(server, 1) == [max(jobs) + 1]


async def test_upgrade_from_0009_with_data(
    fresh_pg_server: PgServer, migrated: PgServer, make_config: ConfigFactory
) -> None:
    server = fresh_pg_server
    cfg = make_config("migrate", server)
    await asyncio.to_thread(
        command.upgrade, alembic_config(server.dsn("migrator", driver="sqlalchemy")), "0009_m4_ui"
    )
    # The preceding release's data, and the identity columns of its schema.
    assert insert_alerts(server, 3) == [1, 2, 3]
    pg_rows(
        server,
        "migrator",
        "INSERT INTO login_attempts (ip, username_hmac, success) "
        "SELECT '192.0.2.1', '\\x01', false FROM generate_series(1, 2)",
    )
    before = maxima(server, await columns_of(server))
    assert "deletion_manifests" not in before  # 0010 creates it
    head = await columns_of(migrated)
    bounds = {table: 400 for table, _ in head}
    write_manifest_file(cfg.manifests_dir, content(kind="golden_case_delete", max_ids=bounds))

    assert await run_migrate_async(cfg, now=NOW) == list(MIGRATE_ORDER)

    # At the code head, which is 0010 or a later revision on the chain through it.
    assert pg_one(server, "migrator", "SELECT version_num FROM alembic_version") == (code_head(),)
    scripts = ScriptDirectory.from_config(alembic_config(server.dsn("migrator", driver="sqlalchemy")))
    chain = [script.revision for script in scripts.walk_revisions(base="0009_m4_ui", head=code_head())]
    assert "0010_m5_operations" in chain and chain[-1] == "0009_m4_ui"
    assert backup_grants(server)
    # The data is kept; every id the run allocated (the seed's rows above all) lies above the bound.
    assert pg_rows(server, "migrator", "SELECT id FROM alerts ORDER BY id") == [(1,), (2,), (3,)]
    assert pg_one(server, "migrator", "SELECT count(*) FROM login_attempts") == (2,)
    assert allocated_at_or_below(server, head, bounds, before) == {}
    assert pg_one(server, "migrator", "SELECT min(id) FROM settings_versions") == (401,)


@pytest.mark.parametrize("path", ["disaster_restore", "restore_test"])
async def test_a_restored_database_with_a_newer_stream_advances_above_it(
    fresh_pg_server: PgServer, make_config: ConfigFactory, tmp_path: Path, path: str
) -> None:
    server = fresh_pg_server
    flags: dict[str, Any] = {}
    if path == "disaster_restore":
        cfg = make_config("migrate", server)
        role: PgRole = "migrator"
        db = APP_DB
        stream = cfg.manifests_dir
    else:
        # The restore test's scratch database, created by the restore role from the template (§19.5), and its
        # scratch directories; it runs as the backup container does, with the backup role's secrets.
        pg_rows(server, "restore", f"CREATE DATABASE {SCRATCH} TEMPLATE jobhunter_template", db="postgres")
        cfg = make_config("backup", server)
        role, db = "restore", SCRATCH
        scratch = {name: tmp_path / name for name in ("manifests", "cv", "exports")}
        for directory in scratch.values():
            directory.mkdir(mode=0o700)
        stream = scratch["manifests"]
        flags = {
            "database": SCRATCH,
            "db_role": "restore",
            "cv_dir": scratch["cv"],
            "exports_dir": scratch["exports"],
        }

    # The database the dump captured: migrated, three alerts, and a completed manifest whose file was never
    # shipped (the restored stream lacks it).
    await run_migrate_async(cfg, now=NOW, manifest_dir=stream, **flags)
    assert insert_alerts(server, 3, role, db) == [1, 2, 3]
    engine = make_null_engine(server.dsn(role, db, driver="sqlalchemy"))
    try:
        async with engine.begin() as conn:
            unshipped = await insert_manifest(conn, kind="golden_case_delete", request={}, now=NOW)
            body = content(
                kind="golden_case_delete", max_ids={"alerts": 3}, manifest_uuid=str(unshipped.manifest_uuid)
            )
            await record_manifest(conn, unshipped, body, hashlib.sha256(canonical_bytes(body)).digest())
            await complete_manifest(conn, unshipped.id)
    finally:
        await engine.dispose()
    if path == "disaster_restore":  # restored with `--no-acl`: the backup role's grants are gone
        pg_rows(
            server,
            "migrator",
            f"REVOKE INSERT, UPDATE ON {', '.join(BACKUP_GRANT_TABLES)} FROM jobhunter_backup",
        )
        assert not backup_grants(server)

    # The latest manifest stream, newer than the dump: what its manifests deleted had ids the dump never saw.
    bounds = {"alerts": 70, "login_attempts": 50, "jobs": 90, "procrastinate_jobs": 60}
    write_manifest_file(
        stream,
        content(
            kind="golden_case_delete", max_ids={t: bounds[t] for t in ("alerts", "login_attempts", "jobs")}
        ),
    )
    write_manifest_file(
        stream,
        content(
            kind="job_tombstone", max_ids={"alerts": 65, "score_targets": 0}, operations=listed_jobs([60])
        ),
    )
    columns = await columns_of(server, role, db)
    before = maxima(server, columns, role, db)

    if path == "disaster_restore":
        assert await run_migrate_async(cfg, now=NOW) == list(MIGRATE_ORDER)
        assert backup_grants(server)
    else:
        steps: frozenset[StepGroup] = frozenset({"sequences", "alembic", "replay"})
        ran = await run_migrate_async(cfg, now=NOW, steps=steps, manifest_dir=stream, **flags)
        assert ran == ["reconstruct_and_advance", "alembic", "advance_again", "replay"]
        # Only the scratch database and directories were touched: the application database was never migrated.
        assert pg_one(server, "migrator", "SELECT to_regclass('public.alembic_version') IS NULL") == (True,)
        assert files_in(cfg.manifests_dir) == {}

    assert pg_one(server, role, "SELECT version_num FROM alembic_version", db=db) == (code_head(),)
    assert (stream / manifest_file_name(unshipped.manifest_uuid)).read_bytes() == canonical_bytes(body)
    assert allocated_at_or_below(server, columns, bounds, before, role, db) == {}
    assert insert_alerts(server, 1, role, db) == [71]
