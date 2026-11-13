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

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
import sqlalchemy as sa
from jobhunter import cli, cli_backup, crypto
from jobhunter.backup import shipper
from jobhunter.backup.backup_set import run_backup_set
from jobhunter.backup.prune import run_prune
from jobhunter.backup.restic import ResticError, SnapshotInfo
from jobhunter.backup.restore import (
    ManifestStreamConflict,
    RestoreFailed,
    RestoreResult,
    complete_restore,
    load_dump,
    restore_set,
    run_restore,
)
from jobhunter.backup.shipper import ShipResult, ship_manifests
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.db.alembic_runner import code_head
from jobhunter.db.session import make_null_engine
from jobhunter.observability.alerts import open_subjects
from jobhunter.privacy.manifests import (
    ManifestContent,
    ManifestRow,
    canonical_bytes,
    complete_manifest,
    insert_manifest,
    manifest_file_name,
    read_manifest,
    record_manifest,
    write_manifest_file,
)
from jobhunter.privacy.reconcilers import MANIFEST_UNSHIPPED_ALERT, UNSHIPPED_AFTER, manifest_shipping
from jobhunter.profile.storage import CvStore
from jobhunter.worker.reconcilers import ReconcileContext
from jobhunter.worker.tasks import defer_in_tx, task_app
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from tests.support.backups import FakeRepository, Restored, shipment
from tests.support.pg import PgServer, config_for, pg_one, pg_rows, server_env
from tests.support.restore import (
    ContainerRunner,
    RestoredDatabase,
    drop_database,
    empty_database,
    restored_database,
)

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


async def scalars(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(statement, params)).scalars())


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
    path: each `manifests` snapshot's `<uuid>.json` directly in `<staging>/restore/stream/snapshot`, merged
    from there into the manifests volume (final review C1: the union of every snapshot), the dump and
    `backup_set.json` directly in `<staging>/restore`, the CV files directly in `cv_store`. The manifest is
    replayed, the CV file a row names kept, and the one no row names removed."""
    manifest = a_manifest(max_alert=50)
    write_manifest_file(site.manifests, manifest)
    await ship_manifests(backup_cfg, repo, backup_engine, clock=utcnow)
    store = CvStore(site.cv)
    keys = [os.urandom(32) for _ in range(2)]
    named = store.write(b"%PDF-1.7 an invented CV", [keys[0]]).storage_path
    orphan = store.write(b"%PDF-1.7 a CV whose row is gone", [keys[1]]).storage_path
    async with engine.begin() as conn:
        await conn.execute(_CV_FILE, {"h": hashlib.sha256(b"cv").digest(), "p": named})
    set_id = await run_backup_set(backup_cfg, repo, runner, clock=utcnow)
    stream_snapshot, set_snapshot = repo.infos[0].id, repo.infos[-1].id
    site.lose()

    result = await run_restore(restore_cfg(migrated, site, target), repo, runner, set_id=set_id, clock=utcnow)

    # Review M3: the set restored and the hex ids of the data keys its ciphertexts name (both CV files').
    assert result == RestoreResult(set_id, set_snapshot, tuple(sorted(crypto.key_id(k).hex() for k in keys)))
    assert repo.restores == [
        Restored(stream_snapshot, str(site.manifests), site.staging / "restore" / "stream" / "snapshot"),
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


# ---- the stream: the union of every `manifests` snapshot (final review C1, M2) -----------------------------


def stream_file(site: Site, manifest: ManifestContent) -> Path:
    return site.manifests / manifest_file_name(manifest["manifest_uuid"])


async def older_list_shipped_late(repo: FakeRepository, site: Site, *manifests: ManifestContent) -> str:
    """A `manifests` snapshot of `manifests`' files alone, made after a complete one: what two shippers left
    before the shipment lock serialised them (final review C1: shipper A listed the older stream, B shipped
    the newer one with its receipt, and A's upload came last). No shipment row names it."""
    listing = site.staging / "older.list"
    listing.write_text("".join(f"{stream_file(site, m)}\n" for m in manifests), encoding="utf-8")
    try:
        return await repo.backup(files_from=listing, tags=["manifests"])
    finally:
        listing.unlink()


@pytest.mark.parametrize("newest", ["an_older_list_shipped_late", "an_incomplete_snapshot"])
async def test_a_manifest_only_an_older_snapshot_holds_is_restored_and_replayed(
    migrated: PgServer,
    site: Site,
    backup_cfg: AppConfig,
    backup_engine: AsyncEngine,
    repo: FakeRepository,
    runner: ContainerRunner,
    target: RestoredDatabase,
    newest: str,
) -> None:
    """The set predates M1 and M2, which one shipment confirms offsite (its snapshot holds both). A newer
    `manifests` snapshot then lacks M2: an older list shipped late, or restic's exit 3 (M2 unreadable, M3
    new), an incomplete snapshot no shipment names. The restore reads the union of every snapshot, whatever
    the newest holds: M2, newer than the dump and in the older snapshot only, is restored and replayed, and so
    is M3, which only the incomplete snapshot holds."""
    await run_backup_set(backup_cfg, repo, runner, clock=utcnow)
    m1, m2, m3 = a_manifest(max_alert=50), a_manifest(max_alert=51), a_manifest(max_alert=52)
    for manifest in (m1, m2):
        write_manifest_file(site.manifests, manifest)
    confirmed = await ship_manifests(backup_cfg, repo, backup_engine, clock=utcnow)
    if newest == "an_older_list_shipped_late":
        expected = [m1, m2]
        await older_list_shipped_late(repo, site, m1)
    else:
        expected = [m1, m2, m3]
        write_manifest_file(site.manifests, m3)
        repo.unreadable.add(stream_file(site, m2))
        with pytest.raises(ResticError):
            await ship_manifests(backup_cfg, repo, backup_engine, clock=utcnow)
    newest_snapshot = repo.infos[-1]
    assert newest_snapshot.id != confirmed.snapshot_id and "manifests" in newest_snapshot.tags
    assert str(stream_file(site, m2)) not in newest_snapshot.paths
    site.lose()

    with capture_logs() as logs:
        await run_restore(restore_cfg(migrated, site, target), repo, runner, set_id=None, clock=utcnow)

    names = sorted(manifest_file_name(m["manifest_uuid"]) for m in expected)
    assert sorted(path.name for path in site.manifests.iterdir()) == names
    for manifest in expected:
        assert stream_file(site, manifest).read_bytes() == canonical_bytes(manifest)
        assert await scalar(target.engine, _MANIFEST, u=uuid.UUID(manifest["manifest_uuid"])) == "completed"
    [restored] = events(logs, "manifest_stream_restored")
    assert (restored["snapshots"], restored["manifests"]) == (2, len(expected))
    assert [(e["phase"], e["ok"]) for e in events(logs, "restore_verified")] == [
        ("restored", True),
        ("replayed", True),
    ]


@pytest.mark.parametrize("beside", ["a_complete_snapshot", "nothing"])
async def test_a_manifests_snapshot_that_holds_no_file_contributes_nothing(
    migrated: PgServer,
    site: Site,
    backup_cfg: AppConfig,
    backup_engine: AsyncEngine,
    repo: FakeRepository,
    runner: ContainerRunner,
    target: RestoredDatabase,
    beside: str,
) -> None:
    """A `manifests` snapshot restic saved on exit 3 with no listed file readable holds no file: the stub
    saves it without the manifests folder, which a restore of that subfolder cannot find (final fix B8). It
    holds no manifest, so it cannot hide one: the restore skips it, logged by id (`manifest_snapshot_empty`),
    and restores the others, never `restic_failed`. Beside a complete snapshot, that one's manifest is
    restored and replayed; alone, the stream is empty, as with no `manifests` snapshot at all."""
    await run_backup_set(backup_cfg, repo, runner, clock=utcnow)
    m1, m2 = a_manifest(max_alert=50), a_manifest(max_alert=51)
    expected = []
    if beside == "a_complete_snapshot":
        write_manifest_file(site.manifests, m1)
        assert (await ship_manifests(backup_cfg, repo, backup_engine, clock=utcnow)).shipped
        expected = [m1]
    write_manifest_file(site.manifests, m2)
    repo.unreadable.update(stream_file(site, m) for m in (m1, m2))
    with pytest.raises(ResticError):
        await ship_manifests(backup_cfg, repo, backup_engine, clock=utcnow)
    empty = repo.infos[-1]
    assert "manifests" in empty.tags and empty.files == 0
    site.lose()

    with capture_logs() as logs:
        await run_restore(restore_cfg(migrated, site, target), repo, runner, set_id=None, clock=utcnow)

    assert [e["snapshot_id"] for e in events(logs, "manifest_snapshot_empty")] == [empty.id]
    assert empty.id not in [restored.snapshot for restored in repo.restores]
    names = sorted(manifest_file_name(m["manifest_uuid"]) for m in expected)
    assert sorted(path.name for path in site.manifests.iterdir()) == names
    for manifest in expected:
        assert await scalar(target.engine, _MANIFEST, u=uuid.UUID(manifest["manifest_uuid"])) == "completed"
    if expected:
        [restored] = events(logs, "manifest_stream_restored")
        assert (restored["snapshots"], restored["manifests"]) == (1, 1)
    else:
        assert len(events(logs, "manifest_stream_empty")) == 1
    assert [(e["phase"], e["ok"]) for e in events(logs, "restore_verified")] == [
        ("restored", True),
        ("replayed", True),
    ]


async def test_one_file_name_with_other_bytes_in_two_snapshots_stops_the_restore(
    migrated: PgServer,
    site: Site,
    backup_cfg: AppConfig,
    backup_engine: AsyncEngine,
    repo: FakeRepository,
    runner: ContainerRunner,
    target: RestoredDatabase,
) -> None:
    """A manifest file is written once, so two snapshots that hold one file name hold the same bytes. Ones
    that do not stop the restore at its stream step with `manifest_stream_conflict`, naming the file and both
    snapshots (logged, and on the error): it never picks one. Nothing of the stream is restored, and nothing
    is loaded."""
    manifest = a_manifest(max_alert=50)
    write_manifest_file(site.manifests, manifest)
    first = await ship_manifests(backup_cfg, repo, backup_engine, clock=utcnow)
    await run_backup_set(backup_cfg, repo, runner, clock=utcnow)
    other: dict[str, Any] = {**manifest, "max_ids": {"alerts": 51}}
    stream_file(site, manifest).write_bytes(canonical_bytes(other))  # type: ignore[arg-type]
    second = await ship_manifests(backup_cfg, repo, backup_engine, clock=utcnow)
    assert first.snapshot_id is not None and second.snapshot_id is not None
    site.lose()
    loads = len(pg_restored(runner))

    with capture_logs() as logs, pytest.raises(ManifestStreamConflict) as failed:
        await run_restore(restore_cfg(migrated, site, target), repo, runner, set_id=None, clock=utcnow)

    name = manifest_file_name(manifest["manifest_uuid"])
    assert (failed.value.step, failed.value.error) == ("stream", "manifest_stream_conflict")
    assert (failed.value.file_name, failed.value.snapshot_ids) == (
        name,
        (first.snapshot_id, second.snapshot_id),
    )
    [conflict] = events(logs, "manifest_stream_conflict")
    assert (conflict["file"], conflict["snapshot_ids"]) == (name, [first.snapshot_id, second.snapshot_id])
    assert [(e["step"], e["error"]) for e in events(logs, "restore_failed")] == [
        ("stream", "manifest_stream_conflict")
    ]
    assert list(site.manifests.iterdir()) == []
    assert len(pg_restored(runner)) == loads and not await scalar(target.engine, _REVISION)
    assert not (site.staging / "restore").exists()


async def test_a_restore_over_the_kept_manifests_snapshots_restores_every_manifest(
    migrated: PgServer,
    site: Site,
    backup_cfg: AppConfig,
    backup_engine: AsyncEngine,
    repo: FakeRepository,
    runner: ContainerRunner,
    target: RestoredDatabase,
) -> None:
    """The weekly prune forgets the `manifests` snapshots strictly older than the latest receipted one (final
    fix C1). Twelve shipments of one new manifest each make twelve receipted snapshots; after the prune only
    the newest is left, and the restore over it restores and replays all twelve manifests: a receipted
    snapshot is complete, and every run ships every file of the stream, which only grows."""
    await run_backup_set(backup_cfg, repo, runner, clock=utcnow)
    manifests = [a_manifest(max_alert=50 + n) for n in range(12)]
    for manifest in manifests:
        write_manifest_file(site.manifests, manifest)
        assert (await ship_manifests(backup_cfg, repo, backup_engine, clock=utcnow)).shipped
    shipped = [info.id for info in repo.infos if "manifests" in info.tags]
    assert len(shipped) == 12

    await run_prune(backup_cfg, repo, backup_engine, clock=utcnow)

    assert [info.id for info in repo.infos if "manifests" in info.tags] == shipped[-1:]
    assert repo.forgets[0] == ("ids", shipped[:-1])
    site.lose()
    with capture_logs() as logs:
        await run_restore(restore_cfg(migrated, site, target), repo, runner, set_id=None, clock=utcnow)

    names = sorted(manifest_file_name(m["manifest_uuid"]) for m in manifests)
    assert sorted(path.name for path in site.manifests.iterdir()) == names
    for manifest in manifests:
        assert await scalar(target.engine, _MANIFEST, u=uuid.UUID(manifest["manifest_uuid"])) == "completed"
    [restored] = events(logs, "manifest_stream_restored")
    assert (restored["snapshots"], restored["manifests"]) == (1, 12)


async def test_incomplete_snapshots_after_a_receipt_never_evict_the_receipted_one(
    migrated: PgServer,
    site: Site,
    backup_cfg: AppConfig,
    backup_engine: AsyncEngine,
    repo: FakeRepository,
    runner: ContainerRunner,
    target: RestoredDatabase,
) -> None:
    """The final re-review's sequence (R-C1). A set predates M2's deletion. `{M1}` ships with its receipt,
    then `{M1, M2}` with its receipt: M2 is confirmed offsite. Then M3 is written, and ten runs find M2
    unreadable at restic's read: ten incomplete `{M1, M3}` snapshots, no receipt. The prune forgets only what
    is strictly older than the latest receipted snapshot, `{M1}`'s; the receipted one and the ten newer are
    kept. The site is lost and the older set restored: M2 is restored and replayed, with M1 and M3."""
    await run_backup_set(backup_cfg, repo, runner, clock=utcnow)
    m1, m2, m3 = a_manifest(max_alert=50), a_manifest(max_alert=51), a_manifest(max_alert=52)
    write_manifest_file(site.manifests, m1)
    first = await ship_manifests(backup_cfg, repo, backup_engine, clock=utcnow)
    write_manifest_file(site.manifests, m2)
    confirmed = await ship_manifests(backup_cfg, repo, backup_engine, clock=utcnow)
    assert first.snapshot_id is not None and confirmed.snapshot_id is not None
    write_manifest_file(site.manifests, m3)
    repo.unreadable.add(stream_file(site, m2))
    for _ in range(10):
        with pytest.raises(ResticError):
            await ship_manifests(backup_cfg, repo, backup_engine, clock=utcnow)
    partial = [info.id for info in repo.infos if "manifests" in info.tags][2:]
    assert len(partial) == 10

    await run_prune(backup_cfg, repo, backup_engine, clock=utcnow)
    site.lose()
    with capture_logs() as logs:
        await run_restore(restore_cfg(migrated, site, target), repo, runner, set_id=None, clock=utcnow)

    names = {m["manifest_uuid"]: manifest_file_name(m["manifest_uuid"]) for m in (m1, m2, m3)}
    restored_names = {path.name for path in site.manifests.iterdir()}
    assert [name for name in names.values() if name not in restored_names] == []
    for manifest in (m1, m2, m3):
        assert stream_file(site, manifest).read_bytes() == canonical_bytes(manifest)
        assert await scalar(target.engine, _MANIFEST, u=uuid.UUID(manifest["manifest_uuid"])) == "completed"
    assert repo.forgets[0] == ("ids", [first.snapshot_id])
    assert [info.id for info in repo.infos if "manifests" in info.tags] == [confirmed.snapshot_id, *partial]
    [restored] = events(logs, "manifest_stream_restored")
    assert (restored["snapshots"], restored["manifests"]) == (11, 3)


class ShipWaits:
    """The shipper's logger, which also tells the test when a shipment waits for the shipment lock."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.waiting = asyncio.Event()

    def info(self, event: str, **fields: Any) -> None:
        if event == "manifest_ship_waiting":
            self.waiting.set()
        self.inner.info(event, **fields)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


async def test_a_shipment_during_a_prune_waits_for_it_and_is_never_forgotten(
    migrated: PgServer,
    site: Site,
    backup_cfg: AppConfig,
    backup_engine: AsyncEngine,
    repo: FakeRepository,
    runner: ContainerRunner,
    target: RestoredDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final re-review's round-2 sequence (R2-C1), with the real prune and shipper over the fakes. A set
    predates M2; `{M1}` ships with its receipt (A). The prune reads A's receipt, and just before it lists the
    `manifests` snapshots, M2 is written and a shipment starts whose snapshot (B) is timestamped before A's:
    the clock stepped back. The prune holds the shipment lock from its receipt read through its forget, so
    the shipment waits for it and ships `{M1, M2}` afterwards: B is never forgotten, and a restore of the
    older set replays M2."""
    await run_backup_set(backup_cfg, repo, runner, clock=utcnow)
    m1, m2 = a_manifest(max_alert=50), a_manifest(max_alert=51)
    write_manifest_file(site.manifests, m1)
    first = await ship_manifests(backup_cfg, repo, backup_engine, clock=utcnow)
    spy = ShipWaits(shipper.log)
    monkeypatch.setattr(shipper, "log", spy)
    list_snapshots = repo.snapshots
    shipping: list[asyncio.Task[ShipResult]] = []

    async def listing(*, tags: Sequence[str]) -> list[SnapshotInfo]:
        if not shipping:  # the prune, after its receipt read: M2 is written and a shipment starts
            write_manifest_file(site.manifests, m2)
            repo.shift = -timedelta(hours=1)
            ship = ship_manifests(backup_cfg, repo, backup_engine, clock=utcnow)
            shipping.append(asyncio.create_task(ship))
            waits = asyncio.create_task(spy.waiting.wait())
            # The shipment either waits on the shipment lock or, unserialised, ships at once.
            await asyncio.wait((shipping[0], waits), timeout=10, return_when=asyncio.FIRST_COMPLETED)
            waits.cancel()
        return await list_snapshots(tags=tags)

    monkeypatch.setattr(repo, "snapshots", listing)
    await run_prune(backup_cfg, repo, backup_engine, clock=utcnow)
    second = await asyncio.wait_for(shipping[0], 30)

    assert first.snapshot_id is not None and second.snapshot_id is not None and second.files == 2
    kept = [info for info in repo.infos if "manifests" in info.tags]
    assert [info.id for info in kept] == [first.snapshot_id, second.snapshot_id]
    assert kept[1].time < kept[0].time  # B is the earlier in time, and still kept
    assert spy.waiting.is_set()  # the shipment waited for the prune
    site.lose()
    await run_restore(restore_cfg(migrated, site, target), repo, runner, set_id=None, clock=utcnow)

    for manifest in (m1, m2):
        assert stream_file(site, manifest).read_bytes() == canonical_bytes(manifest)
        assert await scalar(target.engine, _MANIFEST, u=uuid.UUID(manifest["manifest_uuid"])) == "completed"


_END_SHIP_SESSION: Final = (
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = 'jobhunter-backup-ship'"
)


async def test_a_shipment_whose_lock_session_ends_during_its_upload_never_receipts_what_a_prune_forgot(
    migrated: PgServer,
    site: Site,
    backup_cfg: AppConfig,
    backup_engine: AsyncEngine,
    repo: FakeRepository,
    runner: ContainerRunner,
    target: RestoredDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final re-review's round-3 sequence (R3-C1), with the real shipper, prune and restore over the
    fakes. A set predates M2; `{M1}` ships with its receipt (A). M2 is written, and a shipment lists `{M1,
    M2}`, confirms its lock and uploads snapshot B, timestamped before A (the clock stepped back). Its lock
    session ends during the upload; a prune then takes the free lock, anchors on A's receipt and forgets B as
    older. The shipment writes its receipt on its lock's session (final fix E1), so it writes none:
    `ShipLockLost`. The next ship therefore finds the stream unlike A's items and uploads it again, and a
    restore of the older set replays M2."""
    await run_backup_set(backup_cfg, repo, runner, clock=utcnow)
    m1, m2 = a_manifest(max_alert=50), a_manifest(max_alert=51)
    write_manifest_file(site.manifests, m1)
    first = await ship_manifests(backup_cfg, repo, backup_engine, clock=utcnow)
    write_manifest_file(site.manifests, m2)
    upload = repo.backup
    uploaded: list[str] = []

    async def the_session_ends_during_the_upload(**kwargs: Any) -> str:
        repo.shift = -timedelta(hours=1)
        uploaded.append(await upload(**kwargs))  # B, visible in the repository
        repo.shift = timedelta(0)
        assert pg_rows(migrated, "superuser", _END_SHIP_SESSION) == [(True,)]
        await run_prune(backup_cfg, repo, backup_engine, clock=utcnow)
        return uploaded[0]

    monkeypatch.setattr(repo, "backup", the_session_ends_during_the_upload)
    with pytest.raises(shipper.ShipLockLost):
        await ship_manifests(backup_cfg, repo, backup_engine, clock=utcnow)
    monkeypatch.setattr(repo, "backup", upload)

    assert repo.forgets[0] == ("ids", uploaded)  # B, older than A by its time
    assert [info.id for info in repo.infos if "manifests" in info.tags] == [first.snapshot_id]
    assert await scalars(backup_engine, _RECEIPT_SNAPSHOTS) == [first.snapshot_id]  # none for B
    again = await ship_manifests(backup_cfg, repo, backup_engine, clock=utcnow)
    assert (again.shipped, again.files) == (True, 2)
    site.lose()
    await run_restore(restore_cfg(migrated, site, target), repo, runner, set_id=None, clock=utcnow)

    for manifest in (m1, m2):
        assert stream_file(site, manifest).read_bytes() == canonical_bytes(manifest)
        assert await scalar(target.engine, _MANIFEST, u=uuid.UUID(manifest["manifest_uuid"])) == "completed"


_RECEIPT_SNAPSHOTS: Final = sa.text("SELECT restic_snapshot_id FROM manifest_shipments ORDER BY id")


async def completed_manifest(engine: AsyncEngine, site: Site, *, max_alert: int) -> ManifestRow:
    """A golden-case deletion that deletes nothing, as the worker leaves it: its row recorded with its file's
    hash and `completed`, its file in the site's manifests volume."""
    async with engine.begin() as conn:
        manifest = await insert_manifest(conn, kind="golden_case_delete", request={}, now=utcnow())
    content = a_manifest(max_alert)
    content["manifest_uuid"] = str(manifest.manifest_uuid)
    sha = write_manifest_file(site.manifests, content)
    async with engine.begin() as conn:
        await record_manifest(conn, manifest, content, sha)
        await complete_manifest(conn, manifest.id)
        row = await read_manifest(conn, manifest.id)
    assert row is not None
    return row


async def unshipped_alerts(engine: AsyncEngine) -> list[str]:
    """The subjects of the open `privacy.manifest_unshipped` alerts."""
    async with engine.connect() as conn:
        return sorted(await open_subjects(conn, code=MANIFEST_UNSHIPPED_ALERT))


async def ship_marks(engine: AsyncEngine, *, now: datetime) -> None:
    """The worker's `manifest_shipping` reconciler, once, at `now`."""
    await manifest_shipping(
        ReconcileContext(engine=engine, queue=task_app(), defer=defer_in_tx, now=now, epoch=1)
    )


_SHIPPED: Final = sa.text(
    "SELECT status, completed_at, shipped_at, shipment_id FROM deletion_manifests WHERE id = :id"
)
_RECEIPTS: Final = sa.text("SELECT id FROM manifest_shipments ORDER BY id")
_ITEMS_OF: Final = sa.text("SELECT count(*) FROM manifest_shipment_items WHERE shipment_id = :id")
_PRUNE_REPORT: Final = sa.text("SELECT status, report FROM backup_runs WHERE id = :id")


async def test_a_receipt_whose_snapshot_restic_lost_is_removed_and_its_manifest_shipped_again(
    migrated: PgServer,
    site: Site,
    backup_cfg: AppConfig,
    backup_engine: AsyncEngine,
    engine: AsyncEngine,
    repo: FakeRepository,
    runner: ContainerRunner,
    target: RestoredDatabase,
    current_epoch: int,
    queue_app: Any,
) -> None:
    """A receipt that names a snapshot restic no longer lists is false (final fix D1 (b)). M1 completes and
    ships (receipt R1); a set is taken; M2 completes and ships (R2), and the reconciler marks both shipped.
    Then R2's snapshot is gone from the repository. The prune, under the shipment lock, removes R2 and its
    items and forgets nothing: M2 is `completed` again, its completion time kept, while R1's snapshot is
    listed, so R1 and M1 stay. The reconciler then raises `privacy.manifest_unshipped` for M2, the next ship
    uploads the whole stream, and a restore of the set replays M1 and M2."""
    m1 = await completed_manifest(engine, site, max_alert=50)
    await ship_manifests(backup_cfg, repo, backup_engine, clock=utcnow)
    await ship_marks(engine, now=utcnow())
    await run_backup_set(backup_cfg, repo, runner, clock=utcnow)
    m2 = await completed_manifest(engine, site, max_alert=51)
    lost = await ship_manifests(backup_cfg, repo, backup_engine, clock=utcnow)
    await ship_marks(engine, now=utcnow())
    r1, r2 = await scalars(engine, _RECEIPTS)
    assert (await one(engine, _SHIPPED, id=m2.id)).shipment_id == r2
    assert lost.snapshot_id is not None
    await repo.forget_snapshots([lost.snapshot_id])
    forgets = len(repo.forgets)

    run_id = await run_prune(backup_cfg, repo, backup_engine, clock=utcnow)

    assert [kind for kind, _ in repo.forgets[forgets:]] == ["set"]  # no `manifests` snapshot forgotten
    assert await scalars(engine, _RECEIPTS) == [r1]
    assert await scalar(engine, _ITEMS_OF, id=r2) == 0
    unshipped = await one(engine, _SHIPPED, id=m2.id)
    assert (unshipped.status, unshipped.shipped_at, unshipped.shipment_id) == ("completed", None, None)
    assert unshipped.completed_at == m2.completed_at
    kept = await one(engine, _SHIPPED, id=m1.id)
    assert (kept.status, kept.shipment_id) == ("shipped", r1)
    run = await one(engine, _PRUNE_REPORT, id=run_id)
    assert (run.status, run.report["receipts_removed"], run.report["manifests_forgotten"]) == (
        "succeeded",
        [r2],
        0,
    )

    await ship_marks(engine, now=utcnow() + UNSHIPPED_AFTER + timedelta(minutes=1))
    assert await unshipped_alerts(engine) == [f"manifest:{m2.manifest_uuid}"]
    again = await ship_manifests(backup_cfg, repo, backup_engine, clock=utcnow)
    assert (again.shipped, again.files) == (True, 2)
    await ship_marks(engine, now=utcnow() + UNSHIPPED_AFTER + timedelta(minutes=1))
    assert (await one(engine, _SHIPPED, id=m2.id)).status == "shipped"
    assert await unshipped_alerts(engine) == []
    site.lose()
    await run_restore(restore_cfg(migrated, site, target), repo, runner, set_id=None, clock=utcnow)

    for manifest in (m1, m2):
        assert await scalar(target.engine, _MANIFEST, u=manifest.manifest_uuid) in {"completed", "shipped"}
    assert await scalar(target.engine, _MANIFEST, u=m2.manifest_uuid) == "completed"  # replayed


# ---- the refusals (review M1) ------------------------------------------------------------------------------


async def test_a_database_that_is_not_empty_is_refused(
    migrated: PgServer,
    engine: AsyncEngine,
    site: Site,
    backup_cfg: AppConfig,
    repo: FakeRepository,
    runner: ContainerRunner,
) -> None:
    """R16: a restore into the migrated application database (the shared one, which has `alembic_version`)
    stops at its first step with `database_not_empty`: nothing restored, nothing loaded, nothing created."""
    set_id = await run_backup_set(backup_cfg, repo, runner, clock=utcnow)
    site.lose()
    calls = len(runner.calls)

    with pytest.raises(RestoreFailed) as failed:
        await run_restore(
            config_for(migrated, "restore", **site.env), repo, runner, set_id=None, clock=utcnow
        )

    assert (failed.value.step, failed.value.error) == ("refuse", "database_not_empty")
    assert repo.restores == [] and len(runner.calls) == calls
    assert not site.manifests.exists() and not (site.staging / "restore").exists()
    assert (await one(engine, _SET_ROW, id=set_id)).status == "succeeded"  # the live row, untouched


async def test_a_cv_store_that_is_not_empty_is_refused(
    migrated: PgServer,
    site: Site,
    backup_cfg: AppConfig,
    repo: FakeRepository,
    runner: ContainerRunner,
    target: RestoredDatabase,
) -> None:
    """R16: one file left in `cv_store` stops the restore with `cv_store_not_empty`, before restic is asked
    anything; the file stays and the database stays empty."""
    await run_backup_set(backup_cfg, repo, runner, clock=utcnow)
    site.lose()
    left = site.cv / "left-behind.bin"
    left.write_bytes(b"a file of the lost site")

    with pytest.raises(RestoreFailed) as failed:
        await run_restore(restore_cfg(migrated, site, target), repo, runner, set_id=None, clock=utcnow)

    assert (failed.value.step, failed.value.error) == ("refuse", "cv_store_not_empty")
    assert repo.restores == [] and left.read_bytes() == b"a file of the lost site"
    assert not await scalar(target.engine, _REVISION)


@pytest.mark.parametrize(
    ("deeper", "step", "error"),
    [
        ("manifests", "stream", "manifest_stream_not_restored"),
        ("set", "set", "backup_set_not_restored"),
        ("cv", "cv", "cv_not_restored"),
    ],
)
async def test_files_restored_off_their_exact_paths_stop_the_restore(
    migrated: PgServer,
    site: Site,
    backup_cfg: AppConfig,
    backup_engine: AsyncEngine,
    repo: FakeRepository,
    runner: ContainerRunner,
    target: RestoredDatabase,
    deeper: str,
    step: str,
    error: str,
) -> None:
    """A restore whose files land one level deeper than the step expects (as a restore naming no subfolder
    would) is checked before the restore goes on, and stops it at that step with its code."""
    write_manifest_file(site.manifests, a_manifest(max_alert=50))
    await ship_manifests(backup_cfg, repo, backup_engine, clock=utcnow)
    await run_backup_set(backup_cfg, repo, runner, clock=utcnow)
    site.lose()
    subfolder = {"manifests": site.manifests, "set": site.staging / "set", "cv": site.cv}[deeper]
    repo.whole_paths.add(str(subfolder))

    with pytest.raises(RestoreFailed) as failed:
        await run_restore(restore_cfg(migrated, site, target), repo, runner, set_id=None, clock=utcnow)

    assert (failed.value.step, failed.value.error) == (step, error)
    assert repo.restores[-1].subfolder == str(subfolder)
    assert not (site.staging / "restore").exists()


def _dump_altered(path: Path) -> None:
    path.write_bytes(path.read_bytes() + b"\0")


def _json_unparsable(path: Path) -> None:
    path.write_text("{not json", encoding="utf-8")


def _json_of_another_set(path: Path) -> None:
    inventory = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**inventory, "set_id": inventory["set_id"] + 1}), encoding="utf-8")


@pytest.mark.parametrize(
    ("name", "alter", "error"),
    [
        ("jobhunter.dump", _dump_altered, "dump_sha256_mismatch"),
        ("backup_set.json", _json_unparsable, "backup_set_json_invalid"),
        ("backup_set.json", _json_of_another_set, "backup_set_id_mismatch"),
    ],
)
async def test_a_set_whose_files_do_not_match_stops_the_restore(
    migrated: PgServer,
    site: Site,
    backup_cfg: AppConfig,
    repo: FakeRepository,
    runner: ContainerRunner,
    target: RestoredDatabase,
    name: str,
    alter: Callable[[Path], None],
    error: str,
) -> None:
    """The staged set is checked before anything is loaded: a dump whose sha256 is not `backup_set.json`'s, a
    json that does not parse, and a json of another set than the snapshot's tag each stop the restore at step
    2, the database still empty and the staging removed."""
    await run_backup_set(backup_cfg, repo, runner, clock=utcnow)
    alter(repo.stored(repo.infos[-1].id, site.staging / "set" / name))
    site.lose()
    loads = len(pg_restored(runner))

    with pytest.raises(RestoreFailed) as failed:
        await run_restore(restore_cfg(migrated, site, target), repo, runner, set_id=None, clock=utcnow)

    assert (failed.value.step, failed.value.error) == ("set", error)
    assert len(pg_restored(runner)) == loads and not await scalar(target.engine, _REVISION)
    assert not (site.staging / "restore").exists()


# ---- the CLI: `restore run` prints the set's data key ids, `restore verify` (review M3, M1, M4) ------------


@pytest.fixture
def cli_db(migrated: PgServer) -> Iterator[str]:
    """An empty database for a CLI test, made and dropped synchronously (the CLI runs its own event loop)."""
    name = f"jh_restore_cli_{secrets.token_hex(4)}"
    empty_database(migrated, name)
    try:
        yield name
    finally:
        drop_database(migrated, name)


def as_restore_role(
    monkeypatch: pytest.MonkeyPatch,
    server: PgServer,
    site: Site,
    db_name: str,
    repo: FakeRepository,
    runner: ContainerRunner,
) -> None:
    """The process environment of the `restore` container on `db_name` and the site's volumes; its restic is
    `repo` and its PostgreSQL client `runner`."""
    for name, value in server_env(server, "restore", JOBHUNTER_DB_NAME=db_name, **site.env).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(cli_backup, "Restic", lambda cfg, client: repo)
    monkeypatch.setattr(cli_backup, "SubprocessRunner", lambda: runner)


def ran(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, Any]:
    """`jobhunter <argv>`'s exit status and the one JSON line it printed; never a traceback."""
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    assert "Traceback" not in out + err
    [line] = out.splitlines()
    return code, json.loads(line)


def test_restore_run_prints_the_sets_data_key_ids(
    migrated: PgServer,
    site: Site,
    backup_cfg: AppConfig,
    repo: FakeRepository,
    runner: ContainerRunner,
    cli_db: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Review M3: `restore run` prints the set it restored and the data key ids its ciphertexts name, in hex
    only, on its one stdout line: the operator runs one `crypto rotate` pass per key id (Task 28)."""
    key = os.urandom(32)
    stored = CvStore(site.cv).write(b"%PDF-1.7 an invented CV", [key]).storage_path
    pg_rows(
        migrated,
        "migrator",
        "INSERT INTO cv_files (sha256, mime, size_bytes, storage_path) "
        "VALUES ('\\x01', 'application/pdf', 1, %s)",
        stored,
    )
    set_id = asyncio.run(run_backup_set(backup_cfg, repo, runner, clock=utcnow))
    site.lose()
    as_restore_role(monkeypatch, migrated, site, cli_db, repo, runner)

    code, printed = ran(capsys, "restore", "run")

    assert (code, printed) == (
        0,
        {"restored": True, "set_id": set_id, "data_key_ids": [crypto.key_id(key).hex()]},
    )
    assert key.hex() not in json.dumps(printed)


def test_restore_verify_prints_each_phase(
    migrated: PgServer,
    site: Site,
    backup_cfg: AppConfig,
    repo: FakeRepository,
    runner: ContainerRunner,
    cli_db: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`restore verify` as the `restore` role, one JSON line each time: without a staged set,
    `backup_set_not_staged` (exit 1, no traceback); `--phase restored` on a loaded set holds (exit 0, the
    set's row closed) and fails on an inventory that is not the database's (exit 1, nothing written);
    `--phase replayed` holds after the rest of the restore and fails on a manifest of the stream that the
    database never replayed (exit 1)."""
    set_id = asyncio.run(run_backup_set(backup_cfg, repo, runner, clock=utcnow))
    site.lose()
    as_restore_role(monkeypatch, migrated, site, cli_db, repo, runner)
    restored = ("restore", "verify", "--set", str(set_id), "--phase", "restored")
    replayed = ("restore", "verify", "--set", str(set_id), "--phase", "replayed")

    assert ran(capsys, *restored) == (1, {"error": "backup_set_not_staged"})

    cfg = config_for(migrated, "restore", JOBHUNTER_DB_NAME=cli_db, **site.env)
    staged = asyncio.run(
        restore_set(repo, set_id=set_id, subfolder=site.staging / "set", target=site.staging / "restore")
    )
    asyncio.run(load_dump(cfg, runner, staged, db_role="migrator"))
    inventory_file = site.staging / "restore" / "backup_set.json"
    inventory = json.loads(inventory_file.read_text(encoding="utf-8"))
    count = inventory["manifest_count"]
    inventory_file.write_text(json.dumps({**inventory, "manifest_count": count + 1}), encoding="utf-8")
    row = f"SELECT status FROM backup_sets WHERE id = {set_id}"  # noqa: S608 - an int
    assert ran(capsys, *restored) == (
        1,
        {"phase": "restored", "ok": False, "failures": [f"manifest_count:{count}:{count + 1}"]},
    )
    assert pg_one(migrated, "migrator", row, db=cli_db) == ("running",)
    inventory_file.write_text(json.dumps(inventory), encoding="utf-8")
    assert ran(capsys, *restored) == (0, {"phase": "restored", "ok": True, "failures": []})
    assert pg_one(migrated, "migrator", row, db=cli_db) == ("succeeded",)

    site.manifests.mkdir(mode=0o700)  # restore run's step 1 makes it (an empty stream)
    asyncio.run(complete_restore(cfg, now=utcnow()))
    assert ran(capsys, *replayed) == (0, {"phase": "replayed", "ok": True, "failures": []})
    stray = a_manifest(max_alert=50)
    write_manifest_file(site.manifests, stray)
    code, printed = ran(capsys, *replayed)
    assert (code, printed["phase"], printed["ok"]) == (1, "replayed", False)
    assert f"manifest:{stray['manifest_uuid']}:replayed" in printed["failures"]
