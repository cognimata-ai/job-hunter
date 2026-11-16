"""The `manifest_shipping` reconciler (spec §17.4, §18.2 "manifest shipment coverage", §19.5 steps 5-7; plan
M5 Task 9, Deviation 43): a `completed` manifest becomes `shipped` only when a shipment item names its uuid
with an equal file hash, with the earliest such shipment; one completed more than 1 h ago without such an item
raises `privacy.manifest_unshipped` (critical, subject `manifest:<uuid>`), resolved once it ships. It runs
every tick.

The shipments are written as the backup container's shipper writes them: one `manifest_shipments` row and an
item per file the snapshot held. The snapshot ids are stand-ins.
"""

import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.privacy.manifests import (
    ManifestContent,
    ManifestRow,
    complete_manifest,
    insert_manifest,
    record_manifest,
    write_manifest_file,
)
from jobhunter.privacy.reconcilers import MANIFEST_UNSHIPPED_ALERT
from jobhunter.worker import reconcilers
from jobhunter.worker.main import _REGISTERING_MODULES
from jobhunter.worker.reconcilers import ReconcileContext
from jobhunter.worker.tasks import defer_in_tx, task_app
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

_SHIPMENT: Final = sa.text("INSERT INTO manifest_shipments (restic_snapshot_id) VALUES (:s) RETURNING id")
_ITEM: Final = sa.text(
    "INSERT INTO manifest_shipment_items (shipment_id, manifest_uuid, file_sha256) VALUES (:s, :u, :h)"
)
_MANIFEST: Final = sa.text(
    "SELECT status, shipment_id, shipped_at, completed_at FROM deletion_manifests WHERE id = :i"
)
_ALERTS: Final = sa.text(
    "SELECT severity, dedup_key, occurrences, resolved_at, context FROM alerts WHERE code = :c ORDER BY id"
)


async def ship(engine: AsyncEngine, *items: tuple[ManifestRow, bytes]) -> int:
    """One shipment holding exactly `items` (each a manifest's uuid and the hash of the file shipped)."""
    async with engine.begin() as conn:
        shipment = int((await conn.execute(_SHIPMENT, {"s": "manifests-snapshot"})).scalar_one())
        for manifest, sha in items:
            await conn.execute(_ITEM, {"s": shipment, "u": manifest.manifest_uuid, "h": sha})
    return shipment


async def executing(engine: AsyncEngine) -> ManifestRow:
    async with engine.begin() as conn:
        return await insert_manifest(conn, kind="job_tombstone", request={"job_ids": [1]}, now=utcnow())


async def complete(engine: AsyncEngine, manifest: ManifestRow, directory: Path) -> bytes:
    """The manifest's file written and recorded, then the manifest completed (its fences have none)."""
    content: ManifestContent = {
        "format": 1,
        "manifest_uuid": str(manifest.manifest_uuid),
        "kind": manifest.kind,
        "admitted_at": manifest.created_at.isoformat(),
        "fenced_at": manifest.created_at.isoformat(),
        "max_ids": {"jobs": 1},
        "operations": [{"op": "row_delete", "table": "jobs", "ids": [1], "bound": {"by": "id"}}],
    }
    sha = write_manifest_file(directory, content)
    async with engine.begin() as conn:
        await record_manifest(conn, manifest, content, sha)
        await complete_manifest(conn, manifest.id)
    return sha


async def reconcile(engine: AsyncEngine, now: datetime) -> int:
    """One pass of the registered `manifest_shipping` reconciler at `now`, as the worker's tick runs it."""
    cadences, fn = reconcilers.REGISTRY["manifest_shipping"]
    assert cadences == {"tick"}
    return await fn(ReconcileContext(engine=engine, queue=task_app(), defer=defer_in_tx, now=now, epoch=1))


async def state(engine: AsyncEngine, manifest: ManifestRow) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(_MANIFEST, {"i": manifest.id})).one()


async def unshipped_alerts(engine: AsyncEngine) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(_ALERTS, {"c": MANIFEST_UNSHIPPED_ALERT})).all())


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    root = tmp_path / "manifests"
    root.mkdir(mode=0o700)
    return root


def test_the_worker_registers_it_on_the_tick() -> None:
    assert "jobhunter.privacy.reconcilers" in _REGISTERING_MODULES
    assert reconcilers.REGISTRY["manifest_shipping"][0] == {"tick"}


async def test_a_manifest_ships_only_on_an_item_with_an_equal_hash(
    engine: AsyncEngine, current_epoch: int, queue_app: Any, directory: Path
) -> None:
    now = utcnow()
    # A commits first; B is written and completed while A's file does not exist yet.
    a = await executing(engine)
    b = await executing(engine)
    b_sha = await complete(engine, b, directory)
    covers_b = await ship(engine, (b, b_sha))  # the snapshot held B's file only
    a_sha = await complete(engine, a, directory)

    assert await reconcile(engine, now) == 1
    assert ((await state(engine, b)).status, (await state(engine, b)).shipment_id) == ("shipped", covers_b)
    assert (await state(engine, a)).status == "completed"  # never inferred from a later or higher id

    # An item that names A with another hash (a file that is not the one recorded) ships nothing.
    await ship(engine, (a, hashlib.sha256(b"another file").digest()), (b, b_sha))
    assert await reconcile(engine, now) == 0
    assert (await state(engine, a)).status == "completed"

    # Two later shipments hold A's file: A ships with the earliest of them; B keeps its own shipment.
    covers_a = await ship(engine, (a, a_sha), (b, b_sha))
    await ship(engine, (a, a_sha), (b, b_sha))
    assert await reconcile(engine, now) == 1
    shipped = await state(engine, a)
    assert (shipped.status, shipped.shipment_id) == ("shipped", covers_a)
    assert shipped.shipped_at is not None
    assert (await state(engine, b)).shipment_id == covers_b

    assert await reconcile(engine, now) == 0  # nothing left to do
    assert await unshipped_alerts(engine) == []


async def test_unshipped_after_1h_raises_and_shipping_resolves(
    engine: AsyncEngine, current_epoch: int, queue_app: Any, directory: Path
) -> None:
    manifest = await executing(engine)
    sha = await complete(engine, manifest, directory)
    completed_at = (await state(engine, manifest)).completed_at
    pending = await executing(engine)  # still executing: not this reconciler's concern

    assert await reconcile(engine, completed_at + timedelta(minutes=59)) == 0
    assert await unshipped_alerts(engine) == []

    late = completed_at + timedelta(hours=1, seconds=1)
    assert await reconcile(engine, late) == 1
    assert await reconcile(engine, late + timedelta(minutes=1)) == 0  # the open alert is not raised again
    [alert] = await unshipped_alerts(engine)
    assert (alert.severity, alert.dedup_key, alert.occurrences, alert.resolved_at) == (
        "critical",
        f"{MANIFEST_UNSHIPPED_ALERT}:manifest:{manifest.manifest_uuid}",
        1,
        None,
    )
    assert alert.context == {"manifest_id": manifest.id}

    await ship(engine, (manifest, sha))
    assert await reconcile(engine, late + timedelta(minutes=2)) == 2  # shipped, and its alert resolved
    assert (await state(engine, manifest)).status == "shipped"
    [alert] = await unshipped_alerts(engine)
    assert alert.resolved_at is not None
    assert (await state(engine, pending)).status == "executing"
