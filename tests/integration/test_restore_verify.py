"""`restore verify`, both phases (spec §19.5 "Restore order" steps 5 and 8; plan M5 Task 26, R23, Deviation
46): `privacy.restore_verify.verify_restored` and `verify_replayed`.

A backup set is the shared database as `run_backup_set` takes it (its `pg_dump` the server's own, run in its
container by `ContainerRunner`; restic `FakeRepository`), staged and loaded into a database of its own on the
test server by `restore run`'s steps 2 and 4 (`restore_set`, `load_dump` as the migrator). `verify_restored`
then compares it with the set's `backup_set.json`; `verify_replayed` reads it after `migrate` with a stream of
the test's own. A check that fails is shown on a copy of the inventory, or inside a transaction that is rolled
back afterwards. Every datum is invented.
"""

import copy
import hashlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest
import sqlalchemy as sa
from jobhunter.backup.backup_set import run_backup_set
from jobhunter.backup.restore import StagedSet, load_dump, restore_set
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.db.migrate import run_migrate_async
from jobhunter.ids import uuid7
from jobhunter.privacy.manifests import ManifestContent, Operation, manifest_file_name, write_manifest_file
from jobhunter.privacy.restore_verify import RESTORED_MID_BACKUP, verify_replayed, verify_restored
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.backups import FakeRepository, backup_run, backup_set
from tests.support.collect import pending_item, queued_run
from tests.support.fixtures import ConfigFactory
from tests.support.pg import PgServer, config_for
from tests.support.purges import request_source_purge, seeded
from tests.support.restore import ContainerRunner, RestoredDatabase, restored_database

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("current_epoch")]

_SET: Final = sa.text("SELECT * FROM backup_sets WHERE id = :id")
_STATUSES: Final = sa.text(
    "SELECT 'set' AS kind, id, status, error FROM backup_sets "
    "UNION ALL SELECT 'run', id, status, error FROM backup_runs ORDER BY 1, 2"
)
_A_BODY: Final = sa.text("SELECT id FROM raw_responses WHERE body_zstd IS NOT NULL ORDER BY id LIMIT 1")
_BREAK_BODY: Final = sa.text("UPDATE raw_responses SET body_sha256 = '\\x00' WHERE id = :id")
_ALERT_WITH_ID: Final = sa.text(
    "INSERT INTO alerts (id, code, severity, dedup_key, message) OVERRIDING SYSTEM VALUE "
    "VALUES (:id, 'test.restore', 'info', 'restore-verify', 'an invented alert')"
)
_ACTIVE_FENCE: Final = sa.text(
    "INSERT INTO purge_fences (manifest_id, kind) "
    "SELECT id, 'global' FROM deletion_manifests WHERE manifest_uuid = :u RETURNING id"
)
_LOWER_ALERTS: Final = sa.text("SELECT setval(pg_get_serial_sequence('public.alerts', 'id'), 1, false)")
_MANIFEST: Final = sa.text("SELECT status, file_sha256 FROM deletion_manifests WHERE manifest_uuid = :u")
_EXECUTING: Final = sa.text(
    "INSERT INTO deletion_manifests (manifest_uuid, kind, status) VALUES (:u, 'job_tombstone', 'executing')"
)
NAMED_ALERT: Final = 1000  # above every alert of these tests


@dataclass(frozen=True)
class Sets:
    """The backup role's configuration and its fake repository, the restore role's, and the runner."""

    backup: AppConfig
    restore: AppConfig
    repo: FakeRepository
    runner: ContainerRunner
    staging: Path


@pytest.fixture
def sets(migrated: PgServer, tmp_path: Path) -> Sets:
    volumes = {name: tmp_path / name for name in ("staging", "cv", "manifests", "restore-staging")}
    for volume in volumes.values():
        volume.mkdir(mode=0o700)
    env = {
        "JOBHUNTER_STAGING_DIR": str(volumes["staging"]),
        "JOBHUNTER_CV_STORE_DIR": str(volumes["cv"]),
        "JOBHUNTER_MANIFESTS_DIR": str(volumes["manifests"]),
        "RESTIC_REPOSITORY": str(tmp_path / "repository"),
    }
    backup = config_for(migrated, "backup", **env)
    restore = config_for(migrated, "restore", **env)
    return Sets(
        backup,
        restore,
        FakeRepository(backup, tmp_path / "repository"),
        ContainerRunner(migrated),
        volumes["restore-staging"] / "restore",
    )


@pytest.fixture
async def db(migrated: PgServer, tmp_path: Path) -> AsyncIterator[RestoredDatabase]:
    async with restored_database(migrated, tmp_path / "restored") as restored:
        yield restored


async def loaded(sets: Sets, db: RestoredDatabase) -> StagedSet:
    """A backup set of the shared database, staged and loaded into `db` (module docstring)."""
    await run_backup_set(sets.backup, sets.repo, sets.runner, clock=utcnow)
    staged = await restore_set(
        sets.repo, set_id=None, subfolder=sets.backup.staging_dir / "set", target=sets.staging
    )
    await load_dump(sets.restore, sets.runner, staged, db_role="migrator", database=db.name)
    return staged


@asynccontextmanager
async def rolled_back(db: RestoredDatabase) -> AsyncIterator[AsyncConnection]:
    """A connection on `db` whose transaction is rolled back at the end."""
    async with db.engine.connect() as conn:
        transaction = await conn.begin()
        try:
            yield conn
        finally:
            await transaction.rollback()


async def one(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(statement, params)).one()


async def statuses(engine: AsyncEngine) -> list[tuple[str, int, str, str | None]]:
    async with engine.connect() as conn:
        return [(r.kind, int(r.id), r.status, r.error) for r in await conn.execute(_STATUSES)]


def alert_manifest(alert_id: int) -> ManifestContent:
    """An invented golden-case deletion whose one other operation names an alert that does not exist."""
    stamp = utcnow().isoformat()
    operations: list[Operation] = [
        {"op": "row_delete", "table": "alerts", "ids": [alert_id], "bound": {"by": "id"}}
    ]
    built: dict[str, Any] = {
        "format": 1,
        "manifest_uuid": str(uuid7()),
        "kind": "golden_case_delete",
        "admitted_at": stamp,
        "fenced_at": stamp,
        "max_ids": {"alerts": alert_id},
        "operations": operations,
    }
    return built  # type: ignore[return-value]  # the TypedDict's keys are built above


async def migrate(make_config: ConfigFactory, db: RestoredDatabase, stream: Path) -> None:
    """`restore run`'s step 7: every step of `migrate` on `db` with `stream` and `db`'s volumes."""
    await run_migrate_async(
        make_config("migrate"),
        now=utcnow(),
        database=db.name,
        manifest_dir=stream,
        cv_dir=db.files.cv_store,
        exports_dir=db.files.exports,
    )


async def test_the_restored_phase_checks_revision_counts_manifests_and_bodies(
    engine: AsyncEngine, sets: Sets, db: RestoredDatabase
) -> None:
    owner = await queued_run(engine)
    for _ in range(2):
        await pending_item(engine, owner, "arbeitnow", {})  # a raw response with a real zstd body each
    async with engine.begin() as conn:
        await conn.execute(_EXECUTING, {"u": uuid.uuid4()})
    staged = await loaded(sets, db)
    inventory = dict(staged.inventory)
    counts: dict[str, int] = dict(inventory["row_counts"])  # type: ignore[arg-type]
    assert counts["raw_responses"] == 2 and inventory["manifest_count"] == counts["deletion_manifests"] == 1

    def changed(**fields: Any) -> dict[str, Any]:
        edited = copy.deepcopy(inventory)
        edited.update(fields)
        return edited

    wrong = {
        "alembic_revision": (changed(alembic_revision="0000_not_the_head"), ["alembic_revision"]),
        "row_counts": (
            changed(row_counts={**counts, "alerts": counts["alerts"] + 1}),
            [f"row_count:alerts:{counts['alerts']}:{counts['alerts'] + 1}"],
        ),
        "manifest_count": (changed(manifest_count=2), ["manifest_count:1:2"]),
        "shape": (changed(row_counts="many"), ["backup_set_json"]),
    }
    for name, (edited, failures) in wrong.items():
        async with rolled_back(db) as conn:
            report = await verify_restored(conn, edited, snapshot_id=staged.snapshot.id)
        assert (report.phase, report.ok, report.failures) == ("restored", False, failures), name
    async with rolled_back(db) as conn:
        body = (await conn.execute(_A_BODY)).scalar_one()
        await conn.execute(_BREAK_BODY, {"id": body})
        report = await verify_restored(conn, inventory, snapshot_id=staged.snapshot.id)
    assert report.failures == [f"raw_response:{body}"]
    # A failing report wrote nothing: the set's row is still as the dump holds it.
    assert (await one(db.engine, _SET, id=staged.set_id)).status == "running"

    async with db.engine.begin() as conn:
        report = await verify_restored(conn, inventory, snapshot_id=staged.snapshot.id)
    assert (report.ok, report.failures) == (True, [])
    row = await one(db.engine, _SET, id=staged.set_id)
    assert (row.status, row.error, row.restic_snapshot_id) == ("succeeded", None, staged.snapshot.id)
    assert bytes(row.dump_sha256).hex() == inventory["dump_sha256"]
    assert (row.row_counts, row.manifest_count, row.cv_file_count) == (counts, 1, inventory["cv_file_count"])
    assert row.db_snapshot_at.isoformat() == inventory["db_snapshot_at"] and row.finished_at is not None


async def test_the_restored_phase_closes_mid_run_backup_rows(
    engine: AsyncEngine, sets: Sets, db: RestoredDatabase
) -> None:
    """The dump holds its own set `running`, a set a dead container left `running`, and a restore test and a
    prune `running`: the set's own row becomes `succeeded`, the others `failed` (`restored_mid_backup`); an
    earlier succeeded set stays. A second verification of the restore holds and changes nothing; one with
    another snapshot id fails on the set's row."""
    earlier = await backup_set(engine, "succeeded", utcnow())
    dead = await backup_set(engine, "running")
    test_run = await backup_run(engine, "running", set_id=earlier)
    prune_run = await backup_run(engine, "running", kind="prune")
    staged = await loaded(sets, db)
    own = staged.set_id

    async with db.engine.begin() as conn:
        report = await verify_restored(conn, staged.inventory, snapshot_id=staged.snapshot.id)
    assert report.ok
    closed = await statuses(db.engine)
    assert closed == [
        ("run", test_run, "failed", RESTORED_MID_BACKUP),
        ("run", prune_run, "failed", RESTORED_MID_BACKUP),
        ("set", earlier, "succeeded", None),
        ("set", dead, "failed", RESTORED_MID_BACKUP),
        ("set", own, "succeeded", None),
    ]

    async with db.engine.begin() as conn:
        again = await verify_restored(conn, staged.inventory, snapshot_id=staged.snapshot.id)
    assert (again.ok, await statuses(db.engine)) == (True, closed)
    async with rolled_back(db) as conn:
        other = await verify_restored(conn, staged.inventory, snapshot_id="9" * 64)
    assert other.failures == ["backup_set_row:succeeded"]


async def test_the_replayed_phase_requires_completed_manifests_postconditions_and_no_fence(
    sets: Sets, db: RestoredDatabase, make_config: ConfigFactory, tmp_path: Path
) -> None:
    """Before `migrate`, a manifest of the stream that the dump lacks is to be replayed and the alerts'
    identity lies below its bound; after it, the phase holds. Then each condition fails it on its own: a row
    the manifest deleted back (its postcondition), an active purge fence, and an identity lowered below the
    bound."""
    stream = tmp_path / "stream"
    stream.mkdir(mode=0o700)
    manifest = alert_manifest(NAMED_ALERT)
    write_manifest_file(stream, manifest)
    await loaded(sets, db)
    manifest_uuid = uuid.UUID(manifest["manifest_uuid"])

    async with db.engine.connect() as conn:
        before = await verify_replayed(conn, stream, files=db.files)
    assert not before.ok
    assert f"manifest:{manifest_uuid}:replayed" in before.failures
    assert [f for f in before.failures if f.startswith("identity:alerts:")]

    await migrate(make_config, db, stream)
    async with db.engine.connect() as conn:
        after = await verify_replayed(conn, stream, files=db.files)
    assert (after.phase, after.ok, after.failures) == ("replayed", True, [])
    assert (await one(db.engine, _MANIFEST, u=manifest_uuid)).status == "completed"

    async with rolled_back(db) as conn:
        await conn.execute(_ALERT_WITH_ID, {"id": NAMED_ALERT})
        deleted_back = await verify_replayed(conn, stream, files=db.files)
    assert deleted_back.failures == [f"manifest:{manifest_uuid}:checked"]
    async with rolled_back(db) as conn:
        fence = (await conn.execute(_ACTIVE_FENCE, {"u": manifest_uuid})).scalar_one()
        fenced = await verify_replayed(conn, stream, files=db.files)
    assert fenced.failures == [f"manifest:{manifest_uuid}:checked", f"purge_fences_active:{[fence]}"]
    async with rolled_back(db) as conn:
        await conn.execute(_LOWER_ALERTS)
        lowered = await verify_replayed(conn, stream, files=db.files)
    assert lowered.failures == [f"identity:alerts:1:{NAMED_ALERT}"]


async def test_the_replayed_phase_refuses_a_pending_retry_until_completed(
    engine: AsyncEngine, sets: Sets, db: RestoredDatabase, make_config: ConfigFactory, tmp_path: Path
) -> None:
    """The set captures a source purge after its A0 alone: an `executing` row without a file, which the empty
    stream lacks. It is a pending retry, and the phase refuses it until `migrate` has completed it and written
    its file."""
    await seeded(engine)
    opened = await request_source_purge(engine)
    await loaded(sets, db)
    stream = tmp_path / "stream"
    stream.mkdir(mode=0o700)

    async with db.engine.connect() as conn:
        before = await verify_replayed(conn, stream, files=db.files)
    assert f"manifest:{opened.manifest_uuid}:pending_retry" in before.failures

    await migrate(make_config, db, stream)
    async with db.engine.connect() as conn:
        after = await verify_replayed(conn, stream, files=db.files)
    assert (after.ok, after.failures) == (True, [])
    row = await one(db.engine, _MANIFEST, u=opened.manifest_uuid)
    assert row.status == "completed" and row.file_sha256 is not None
    written = stream / manifest_file_name(opened.manifest_uuid)
    assert hashlib.sha256(written.read_bytes()).digest() == bytes(row.file_sha256)

    # Review M2 (§19.5 step 8): the phase holds only while the completed manifest's file is in the stream, as
    # the row records it.
    content = written.read_bytes()
    written.unlink()
    async with db.engine.connect() as conn:
        missing = await verify_replayed(conn, stream, files=db.files)
    assert not missing.ok and f"manifest_file_missing:{opened.manifest_uuid}" in missing.failures
    written.write_bytes(b"{}")
    async with db.engine.connect() as conn:
        differs = await verify_replayed(conn, stream, files=db.files)
    assert not differs.ok and f"manifest_file_differs:{opened.manifest_uuid}" in differs.failures
    written.write_bytes(content)
    async with db.engine.connect() as conn:
        assert (await verify_replayed(conn, stream, files=db.files)).ok
