"""The manifest shipper (spec §19.5 step 5; plan M5 Task 10, R14, Deviation 30): the `*.json` files of the
manifests directory (never a `*.tmp` being written), each hashed; one `restic backup --tag manifests` of
exactly those files and, in one transaction as `jobhunter_backup`, a `manifest_shipments` row with one item
per file the snapshot contains. An unchanged stream (the latest shipment's items) ships nothing; a restic
failure writes no row. The `manifest_shipping` reconciler (Task 9) then marks the manifest `shipped`.

Restic is a fake here (`FakeRestic`: its `backup` reads the file list it is given and returns a stand-in
snapshot id); the database is the shared server, written as the backup role. The image test
(`tests/stack/test_backup_image.py`) ships with the real binary.
"""

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

import pytest
import sqlalchemy as sa
from jobhunter.backup import shipper
from jobhunter.backup.restic import ResticError
from jobhunter.backup.shipper import ShipResult, ship_manifests
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.db.session import make_engine
from jobhunter.privacy.manifests import (
    ManifestContent,
    complete_manifest,
    insert_manifest,
    record_manifest,
    write_manifest_file,
)
from jobhunter.privacy.reconcilers import manifest_shipping
from jobhunter.worker.reconcilers import ReconcileContext
from jobhunter.worker.tasks import defer_in_tx, task_app
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.backups import FakeRestic
from tests.support.pg import PgServer, config_for

pytestmark = pytest.mark.integration

_SHIPMENTS: Final = sa.text("SELECT id, restic_snapshot_id, shipped_at FROM manifest_shipments ORDER BY id")
_ITEMS: Final = sa.text(
    "SELECT manifest_uuid, file_sha256 FROM manifest_shipment_items WHERE shipment_id = :s "
    "ORDER BY manifest_uuid"
)
_ROLE: Final = sa.text("SELECT current_user")


@pytest.fixture
def backup_cfg(migrated: PgServer, tmp_path: Path) -> AppConfig:
    """The backup role against the shared server, with a manifests and a staging directory of this test's own
    (0700, as the images make the volumes' mount points): the server's shared staging directory is every
    backup test's, so a test here asserts on its own."""
    manifests, staging = tmp_path / "manifests", tmp_path / "staging"
    manifests.mkdir(mode=0o700)
    staging.mkdir(mode=0o700)
    return config_for(
        migrated,
        "backup",
        JOBHUNTER_MANIFESTS_DIR=str(manifests),
        JOBHUNTER_STAGING_DIR=str(staging),
        RESTIC_REPOSITORY=str(tmp_path / "repo"),
    )


@pytest.fixture
async def backup_engine(backup_cfg: AppConfig) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(backup_cfg, pool_size=2)
    try:
        yield engine
    finally:
        await engine.dispose()


def content(manifest_uuid: str) -> ManifestContent:
    stamp = "2026-11-10T08:00:00+00:00"
    return {
        "format": 1,
        "manifest_uuid": manifest_uuid,
        "kind": "job_tombstone",
        "admitted_at": stamp,
        "fenced_at": stamp,
        "max_ids": {"jobs": 1},
        "operations": [{"op": "row_delete", "table": "jobs", "ids": [1], "bound": {"by": "id"}}],
    }


def manifest_file(directory: Path) -> tuple[uuid.UUID, bytes]:
    """A manifest's file written as the worker writes it; its uuid and sha256."""
    manifest_uuid = uuid.uuid4()
    return manifest_uuid, write_manifest_file(directory, content(str(manifest_uuid)))


async def shipments(engine: AsyncEngine) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(_SHIPMENTS)).all())


async def items(engine: AsyncEngine, shipment_id: int) -> set[tuple[uuid.UUID, bytes]]:
    async with engine.connect() as conn:
        rows = (await conn.execute(_ITEMS, {"s": shipment_id})).all()
    return {(row.manifest_uuid, bytes(row.file_sha256)) for row in rows}


async def test_ship_lists_json_files_only_never_tmp(
    backup_cfg: AppConfig, backup_engine: AsyncEngine
) -> None:
    directory = backup_cfg.manifests_dir
    a, b = manifest_file(directory), manifest_file(directory)
    (directory / f"{uuid.uuid4()}.json.tmp").write_bytes(b"{")  # a file being written
    (directory / "notes.txt").write_text("not a manifest")
    (directory / "not-a-uuid.json").write_text("{}")  # no manifest's name: its uuid would be no uuid
    (directory / f"{str(uuid.uuid4()).upper()}.json").write_text("{}")  # not the canonical form either
    (directory / f"{uuid.uuid4()}.json").mkdir()  # not a regular file
    restic = FakeRestic(backup_cfg)

    result = await ship_manifests(backup_cfg, restic, backup_engine, now=utcnow())

    assert backup_cfg.staging_dir == directory.parent / "staging"  # this test's own
    listed, tags = restic.backups[0]
    assert sorted(listed) == sorted(str(directory / f"{u}.json") for u, _ in (a, b))
    assert tags == ("manifests",)
    assert result == ShipResult(shipped=True, snapshot_id=f"{1:064x}", files=2)
    assert not list(backup_cfg.staging_dir.iterdir())  # the list file is gone


async def test_one_item_per_file_the_snapshot_contains(
    backup_cfg: AppConfig, backup_engine: AsyncEngine
) -> None:
    files = {manifest_file(backup_cfg.manifests_dir) for _ in range(3)}
    for manifest_uuid, sha in files:  # the hash is the file's, as written
        assert (
            hashlib.sha256((backup_cfg.manifests_dir / f"{manifest_uuid}.json").read_bytes()).digest() == sha
        )
    now = utcnow()

    result = await ship_manifests(backup_cfg, FakeRestic(backup_cfg), backup_engine, now=now)

    [shipment] = await shipments(backup_engine)
    assert (shipment.restic_snapshot_id, shipment.shipped_at) == (result.snapshot_id, now)
    assert await items(backup_engine, shipment.id) == files
    async with backup_engine.connect() as conn:
        assert (await conn.execute(_ROLE)).scalar_one() == "jobhunter_backup"


async def test_an_unchanged_stream_ships_nothing(backup_cfg: AppConfig, backup_engine: AsyncEngine) -> None:
    restic = FakeRestic(backup_cfg)
    now = utcnow()
    # An empty stream (no purge yet) ships nothing: restic has no empty snapshot to make.
    assert await ship_manifests(backup_cfg, restic, backup_engine, now=now) == ShipResult(False, None, 0)

    first = manifest_file(backup_cfg.manifests_dir)
    assert (await ship_manifests(backup_cfg, restic, backup_engine, now=now)).shipped
    unchanged = await ship_manifests(backup_cfg, restic, backup_engine, now=now + timedelta(minutes=5))
    assert unchanged == ShipResult(shipped=False, snapshot_id=None, files=1)
    assert len(restic.backups) == 1 and len(await shipments(backup_engine)) == 1

    # A new file: every file ships again, so the latest snapshot is the complete stream.
    second = manifest_file(backup_cfg.manifests_dir)
    again = await ship_manifests(backup_cfg, restic, backup_engine, now=now + timedelta(minutes=10))
    assert again == ShipResult(shipped=True, snapshot_id=f"{2:064x}", files=2)
    latest = (await shipments(backup_engine))[-1]
    assert await items(backup_engine, latest.id) == {first, second}
    assert len(restic.backups[1][0]) == 2

    # A shipped file whose bytes are not the shipped ones (migrate rewrote a lost file differently, say)
    # changes the stream too.
    (backup_cfg.manifests_dir / f"{second[0]}.json").write_bytes(b"{}")
    assert (await ship_manifests(backup_cfg, restic, backup_engine, now=now + timedelta(minutes=15))).shipped


async def test_a_second_shipper_waits_for_the_first_ones_receipt(
    backup_cfg: AppConfig, backup_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final review C1, its probe's order: shipper A lists `{M1}` and pauses at its database read; meanwhile a
    deletion's M2 is written and shipper B starts. B waits on the shipment lock (7261007) until A's receipt,
    then lists `{M1, M2}` itself, so the snapshots are made in the streams' order and the newest one holds
    every manifest: an older list can never become the newest snapshot after a newer one's receipt."""
    restic = FakeRestic(backup_cfg)
    m1 = manifest_file(backup_cfg.manifests_dir)
    paused, resume, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    read_items = shipper._latest_items
    reads = 0

    async def latest_items(engine: AsyncEngine) -> set[tuple[uuid.UUID, bytes]]:
        nonlocal reads
        reads += 1
        if reads == 1:  # shipper A, after its listing
            paused.set()
            await resume.wait()
        return await read_items(engine)

    class Spy:
        """The shipper's logger, which also tells the test when a shipper waits for the lock."""

        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def info(self, event: str, **fields: Any) -> None:
            if event == "manifest_ship_waiting":
                waiting.set()
            self.inner.info(event, **fields)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

    monkeypatch.setattr(shipper, "_latest_items", latest_items)
    monkeypatch.setattr(shipper, "log", Spy(shipper.log))
    now = utcnow()
    a = asyncio.create_task(ship_manifests(backup_cfg, restic, backup_engine, now=now))
    await asyncio.wait_for(paused.wait(), 10)
    m2 = manifest_file(backup_cfg.manifests_dir)
    b = asyncio.create_task(ship_manifests(backup_cfg, restic, backup_engine, now=now + timedelta(minutes=5)))
    waits = asyncio.create_task(waiting.wait())
    # B either waits on the lock or, unserialised, ships at once.
    await asyncio.wait((b, waits), timeout=10, return_when=asyncio.FIRST_COMPLETED)
    waits.cancel()
    resume.set()
    first, second = await asyncio.wait_for(asyncio.gather(a, b), 30)

    path = backup_cfg.manifests_dir
    assert [sorted(listed) for listed, _ in restic.backups] == [
        [str(path / f"{m1[0]}.json")],
        sorted(str(path / f"{u}.json") for u, _ in (m1, m2)),
    ]
    assert (first.files, second.files) == (1, 2)
    latest = (await shipments(backup_engine))[-1]
    assert latest.restic_snapshot_id == second.snapshot_id
    assert await items(backup_engine, latest.id) == {m1, m2}


async def test_a_restic_failure_writes_no_row(backup_cfg: AppConfig, backup_engine: AsyncEngine) -> None:
    manifest_file(backup_cfg.manifests_dir)
    restic = FakeRestic(backup_cfg, failure=ResticError("backup", "incomplete", 3))

    with pytest.raises(ResticError) as failed:
        await ship_manifests(backup_cfg, restic, backup_engine, now=utcnow())

    assert failed.value.code == "incomplete"
    assert await shipments(backup_engine) == []
    async with backup_engine.connect() as conn:
        assert (await conn.execute(sa.text("SELECT count(*) FROM manifest_shipment_items"))).scalar_one() == 0
    assert not list(backup_cfg.staging_dir.iterdir())  # the list file is gone on this path too


async def test_the_reconciler_then_marks_the_manifest_shipped(
    backup_cfg: AppConfig,
    backup_engine: AsyncEngine,
    engine: AsyncEngine,
    current_epoch: int,
    queue_app: Any,
) -> None:
    # A purge's manifest, as the worker leaves it: its file written and recorded, the manifest completed.
    async with engine.begin() as conn:
        manifest = await insert_manifest(conn, kind="job_tombstone", request={"job_ids": [1]}, now=utcnow())
    written = content(str(manifest.manifest_uuid))
    sha = write_manifest_file(backup_cfg.manifests_dir, written)
    async with engine.begin() as conn:
        await record_manifest(conn, manifest, written, sha)
        await complete_manifest(conn, manifest.id)

    result = await ship_manifests(backup_cfg, FakeRestic(backup_cfg), backup_engine, now=utcnow())
    [shipment] = await shipments(backup_engine)
    assert result.snapshot_id == shipment.restic_snapshot_id

    context = ReconcileContext(engine=engine, queue=task_app(), defer=defer_in_tx, now=utcnow(), epoch=1)
    assert await manifest_shipping(context) == 1
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.text("SELECT status, shipment_id FROM deletion_manifests WHERE id = :i"),
                {"i": manifest.id},
            )
        ).one()
    assert (row.status, row.shipment_id) == ("shipped", shipment.id)
