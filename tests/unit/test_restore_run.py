"""`jobhunter restore run` (spec §19.5 "Restore order"; plan M5 Task 26, R16, R23, Deviations 46 and 55):
`backup.restore.run_restore` in §19.5's order, after the loss of a whole site.

A site's volumes (`staging`, `cv`, `manifests`, `exports`) are directories of the test that the backup and the
restore container both mount at the same paths, as `/data/...` in the images. A backup set is taken of the
shared database by `run_backup_set` (and the stream shipped by `ship_manifests`) into `FakeRepository`, restic
in a directory of the test; then the site is lost: every volume emptied, the manifests volume gone, and the
restore runs into a database of its own on the test server, created empty from `jobhunter_template` as a new
`pgdata` holds it. restic is that fake; `pg_dump` and `pg_restore` are the server's own, run in its container
(`ContainerRunner`: `/opt/pgclient/bin` exists only in the backup image). Every datum is invented.
"""

import hashlib
import os
import shutil
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest
import sqlalchemy as sa
from jobhunter.backup.backup_set import run_backup_set
from jobhunter.backup.restore import RestoreFailed, run_restore
from jobhunter.backup.shipper import ship_manifests
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.db.alembic_runner import code_head
from jobhunter.db.session import make_null_engine
from jobhunter.privacy.manifests import (
    ManifestContent,
    canonical_bytes,
    manifest_file_name,
    write_manifest_file,
)
from jobhunter.profile.storage import CvStore
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from tests.support.backups import FakeRepository, Restored, shipment
from tests.support.pg import PgServer, config_for
from tests.support.restore import ContainerRunner, RestoredDatabase, restored_database

pytestmark = pytest.mark.usefixtures("clean_db")

_SET_ROW: Final = sa.text("SELECT status, restic_snapshot_id, error FROM backup_sets WHERE id = :id")
_MANIFEST: Final = sa.text("SELECT status FROM deletion_manifests WHERE manifest_uuid = :u")
_REVISION: Final = sa.text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
_HEAD: Final = sa.text("SELECT version_num FROM alembic_version")
_CV_FILE: Final = sa.text(
    "INSERT INTO cv_files (sha256, mime, size_bytes, storage_path) VALUES (:h, 'application/pdf', 1, :p)"
)


@dataclass(frozen=True)
class Site:
    """The volumes of a site (module docstring)."""

    staging: Path
    cv: Path
    manifests: Path
    exports: Path
    repository: Path

    @property
    def env(self) -> dict[str, str]:
        return {
            "JOBHUNTER_STAGING_DIR": str(self.staging),
            "JOBHUNTER_CV_STORE_DIR": str(self.cv),
            "JOBHUNTER_MANIFESTS_DIR": str(self.manifests),
            "JOBHUNTER_EXPORTS_DIR": str(self.exports),
            "RESTIC_REPOSITORY": str(self.repository),
        }

    def lose(self) -> None:
        """The site lost: every volume emptied, the manifests volume gone (the restore creates it)."""
        for volume in (self.staging, self.cv, self.exports):
            shutil.rmtree(volume)
            volume.mkdir(mode=0o700)
        shutil.rmtree(self.manifests)


@pytest.fixture
def site(tmp_path: Path) -> Site:
    volumes = {name: tmp_path / name for name in ("staging", "cv", "manifests", "exports")}
    for volume in volumes.values():
        volume.mkdir(mode=0o700)
    return Site(**volumes, repository=tmp_path / "repository")


@pytest.fixture
def backup_cfg(migrated: PgServer, site: Site) -> AppConfig:
    return config_for(migrated, "backup", **site.env)


@pytest.fixture
def repo(backup_cfg: AppConfig, site: Site) -> FakeRepository:
    return FakeRepository(backup_cfg, site.repository)


@pytest.fixture
def runner(migrated: PgServer) -> ContainerRunner:
    return ContainerRunner(migrated)


@pytest.fixture
async def backup_engine(backup_cfg: AppConfig) -> AsyncIterator[AsyncEngine]:
    engine = make_null_engine(backup_cfg.dsn("sqlalchemy"))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def target(migrated: PgServer, tmp_path: Path) -> AsyncIterator[RestoredDatabase]:
    """The new site's database: empty, from the template."""
    async with restored_database(migrated, tmp_path / "target") as db:
        yield db


def restore_cfg(server: PgServer, site: Site, db: RestoredDatabase) -> AppConfig:
    return config_for(server, "restore", JOBHUNTER_DB_NAME=db.name, **site.env)


def events(logs: Sequence[Mapping[str, Any]], name: str) -> list[Mapping[str, Any]]:
    return [entry for entry in logs if entry["event"] == name]


async def scalar(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(statement, params)).scalar_one()


async def one(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(statement, params)).one()


def a_manifest(max_alert: int) -> ManifestContent:
    """An invented golden-case deletion that deletes nothing: a replay inserts, runs and completes it."""
    stamp = utcnow().isoformat()
    built: dict[str, Any] = {
        "format": 1,
        "manifest_uuid": str(uuid.uuid4()),
        "kind": "golden_case_delete",
        "admitted_at": stamp,
        "fenced_at": stamp,
        "max_ids": {"alerts": max_alert},
        "operations": [],
    }
    return built  # type: ignore[return-value]  # the TypedDict's keys are built above


def pg_restored(runner: ContainerRunner) -> list[list[str]]:
    """The `pg_restore` loads the runner ran (not its `--list` reads)."""
    return [argv for argv in runner.calls if argv[0].endswith("pg_restore") and argv[1] != "--list"]


async def test_an_empty_stream_is_restored_as_empty(
    migrated: PgServer,
    site: Site,
    backup_cfg: AppConfig,
    repo: FakeRepository,
    runner: ContainerRunner,
    target: RestoredDatabase,
) -> None:
    """No `manifests` snapshot yet (no purge ever ran): nothing of a stream is restored,
    `manifest_stream_empty` is logged, the manifests directory is created empty before `migrate` (Ruling
    T25-2 (9)), and the restore completes with both verifications."""
    set_id = await run_backup_set(backup_cfg, repo, runner, clock=utcnow)
    site.lose()

    with capture_logs() as logs:
        await run_restore(restore_cfg(migrated, site, target), repo, runner, set_id=None, clock=utcnow)

    assert len(events(logs, "manifest_stream_empty")) == 1
    assert [restored.subfolder for restored in repo.restores] == [str(site.staging / "set"), str(site.cv)]
    assert site.manifests.is_dir() and list(site.manifests.iterdir()) == []
    assert [(e["phase"], e["ok"]) for e in events(logs, "restore_verified")] == [
        ("restored", True),
        ("replayed", True),
    ]
    row = await one(target.engine, _SET_ROW, id=set_id)
    assert (row.status, row.restic_snapshot_id, row.error) == ("succeeded", repo.infos[-1].id, None)
    assert await scalar(target.engine, _HEAD) == code_head()


async def test_the_restore_staging_is_removed_after_success_and_failure(
    migrated: PgServer,
    site: Site,
    backup_cfg: AppConfig,
    repo: FakeRepository,
    runner: ContainerRunner,
    target: RestoredDatabase,
    tmp_path: Path,
) -> None:
    """`<staging>/restore` holds a full copy of the database while the restore runs: the dump is loaded from
    its exact path there, and the directory is gone at the end, after a success and after a failure."""
    await run_backup_set(backup_cfg, repo, runner, clock=utcnow)
    staged = site.staging / "restore"
    site.lose()

    await run_restore(restore_cfg(migrated, site, target), repo, runner, set_id=None, clock=utcnow)

    [loaded] = pg_restored(runner)
    assert loaded[-1] == str(staged / "jobhunter.dump")
    assert loaded[loaded.index("-L") + 1] == str(staged / "restore.list")
    assert not staged.exists()

    site.lose()
    failing = ContainerRunner(migrated, fail="pg_restore")
    async with restored_database(migrated, tmp_path / "again") as again:
        with pytest.raises(RestoreFailed) as failed:
            await run_restore(restore_cfg(migrated, site, again), repo, failing, set_id=None, clock=utcnow)
        assert (failed.value.step, failed.value.error) == ("pg_restore", "pg_restore_failed")
        assert not await scalar(again.engine, _REVISION)
    assert repo.restores[-1].target == staged  # the set was staged before the failure
    assert not staged.exists()


async def test_a_restic_failure_stops_the_restore(
    migrated: PgServer,
    site: Site,
    backup_cfg: AppConfig,
    repo: FakeRepository,
    runner: ContainerRunner,
    target: RestoredDatabase,
) -> None:
    """restic answers no listing (an unreachable repository, a wrong password): the restore stops at its first
    step with `restic_failed`, having restored, loaded and created nothing."""
    await run_backup_set(backup_cfg, repo, runner, clock=utcnow)
    site.lose()
    repo.failing.add("snapshots")
    calls = len(runner.calls)

    with capture_logs() as logs, pytest.raises(RestoreFailed) as failed:
        await run_restore(restore_cfg(migrated, site, target), repo, runner, set_id=None, clock=utcnow)

    assert (failed.value.step, failed.value.error) == ("stream", "restic_failed")
    assert [(e["step"], e["error"]) for e in events(logs, "restore_failed")] == [("stream", "restic_failed")]
    assert repo.restores == [] and len(runner.calls) == calls
    assert not await scalar(target.engine, _REVISION)
    assert not site.manifests.exists() and list(site.cv.iterdir()) == []
    assert not (site.staging / "restore").exists()


async def test_a_shipped_row_without_a_stream_stops_with_manifest_stream_missing(
    migrated: PgServer,
    engine: AsyncEngine,
    site: Site,
    backup_cfg: AppConfig,
    repo: FakeRepository,
    runner: ContainerRunner,
    target: RestoredDatabase,
) -> None:
    """The set holds a `shipped` manifest, yet the repository has no `manifests` snapshot: a shipped manifest
    must be in the stream, so the restore stops at step 5 with `manifest_stream_missing`, the transaction that
    would have closed the set's row rolled back, and no CV file restored."""
    await shipment(engine, utcnow(), manifests=1)
    set_id = await run_backup_set(backup_cfg, repo, runner, clock=utcnow)
    site.lose()

    with pytest.raises(RestoreFailed) as failed:
        await run_restore(restore_cfg(migrated, site, target), repo, runner, set_id=None, clock=utcnow)

    assert (failed.value.step, failed.value.error) == ("verify_restored", "manifest_stream_missing")
    assert (await one(target.engine, _SET_ROW, id=set_id)).status == "running"
    assert [restored.subfolder for restored in repo.restores] == [str(site.staging / "set")]
    assert list(site.cv.iterdir()) == [] and not (site.staging / "restore").exists()


async def test_each_restore_names_its_subfolder_and_target(
    migrated: PgServer,
    engine: AsyncEngine,
    site: Site,
    backup_cfg: AppConfig,
    backup_engine: AsyncEngine,
    repo: FakeRepository,
    runner: ContainerRunner,
    target: RestoredDatabase,
) -> None:
    """Each restic restore names its snapshot's subfolder and its target, so every file lands at its exact
    path: the stream's `<uuid>.json` directly in the manifests volume, the dump and `backup_set.json` directly
    in `<staging>/restore`, the CV files directly in `cv_store`. The manifest is replayed, the CV file a row
    names kept, and the one no row names removed."""
    manifest = a_manifest(max_alert=50)
    write_manifest_file(site.manifests, manifest)
    await ship_manifests(backup_cfg, repo, backup_engine, now=utcnow())
    store = CvStore(site.cv)
    named = store.write(b"%PDF-1.7 an invented CV", [os.urandom(32)]).storage_path
    orphan = store.write(b"%PDF-1.7 a CV whose row is gone", [os.urandom(32)]).storage_path
    async with engine.begin() as conn:
        await conn.execute(_CV_FILE, {"h": hashlib.sha256(b"cv").digest(), "p": named})
    set_id = await run_backup_set(backup_cfg, repo, runner, clock=utcnow)
    stream_snapshot, set_snapshot = repo.infos[0].id, repo.infos[-1].id
    site.lose()

    await run_restore(restore_cfg(migrated, site, target), repo, runner, set_id=set_id, clock=utcnow)

    assert repo.restores == [
        Restored(stream_snapshot, str(site.manifests), site.manifests),
        Restored(set_snapshot, str(site.staging / "set"), site.staging / "restore"),
        Restored(set_snapshot, str(site.cv), site.cv),
    ]
    [loaded] = pg_restored(runner)
    assert loaded[-1] == str(site.staging / "restore" / "jobhunter.dump")
    file_name = manifest_file_name(manifest["manifest_uuid"])
    assert sorted(path.name for path in site.manifests.iterdir()) == [file_name]
    assert (site.manifests / file_name).read_bytes() == canonical_bytes(manifest)
    assert sorted(path.name for path in site.cv.iterdir()) == [named]  # the orphan removed at step 7
    assert await scalar(target.engine, _MANIFEST, u=uuid.UUID(manifest["manifest_uuid"])) == "completed"
    assert orphan != named
