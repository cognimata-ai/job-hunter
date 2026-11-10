"""Backup sets, prune and restore-test runs, manifest shipments and manifests for the tests (M5 Task 7).

Each row is written in the state a test needs, with the times it gives, and satisfies migration 0010's CHECKs
as the backup role and a purge leave it: a `succeeded` set has its snapshot, dump hash, snapshot time and row
counts; a `completed` manifest its file hash and completion time; a `shipped` one its shipment, whose items
name the manifest's uuid and file hash (§19.5). Every value is a stand-in: no snapshot id or hash is real.
Each helper commits its rows and returns the id.
"""

from datetime import datetime, timedelta
from typing import Final, Literal

import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.observability.backup_health import BackupStatus
from sqlalchemy.ext.asyncio import AsyncEngine

_SET: Final = sa.text(
    "INSERT INTO backup_sets (status, restic_snapshot_id, dump_sha256, db_snapshot_at, row_counts, error, "
    "started_at, finished_at) VALUES (:status, :snapshot, :sha, :db_snapshot_at, CAST(:row_counts AS jsonb), "
    ":error, :started_at, :finished_at) RETURNING id"
)
_RUN: Final = sa.text(
    "INSERT INTO backup_runs (kind, backup_set_id, status, error, started_at, finished_at) "
    "VALUES (:kind, :set_id, :status, :error, :started_at, :finished_at) RETURNING id"
)
_SHIPMENT: Final = sa.text(
    "INSERT INTO manifest_shipments (restic_snapshot_id, shipped_at) VALUES ('manifests-snapshot', :at) "
    "RETURNING id"
)
_SHIPPED: Final = sa.text(
    "WITH m AS (INSERT INTO deletion_manifests (manifest_uuid, kind, status, file_sha256, completed_at, "
    "shipped_at, shipment_id) VALUES (gen_random_uuid(), 'source_purge', 'shipped', '\\x02', :completed_at, "
    ":shipped_at, :shipment) RETURNING manifest_uuid, file_sha256) "
    "INSERT INTO manifest_shipment_items (shipment_id, manifest_uuid, file_sha256) "
    "SELECT :shipment, manifest_uuid, file_sha256 FROM m"
)
_COMPLETED: Final = sa.text(
    "INSERT INTO deletion_manifests (manifest_uuid, kind, status, file_sha256, completed_at) "
    "VALUES (gen_random_uuid(), 'source_purge', :status, :sha, :completed_at) RETURNING id"
)


async def _insert(engine: AsyncEngine, statement: sa.TextClause, params: dict[str, object]) -> int:
    async with engine.begin() as conn:
        return int((await conn.execute(statement, params)).scalar_one())


async def backup_set(engine: AsyncEngine, status: BackupStatus, finished_at: datetime | None = None) -> int:
    """A backup set: `succeeded` with its snapshot, dump hash and row counts, `failed` with its error code, or
    `running` (pass no `finished_at`)."""
    done = status == "succeeded"
    params: dict[str, object] = {
        "status": status,
        "snapshot": "set-snapshot" if done else None,
        "sha": b"\x01" if done else None,
        "db_snapshot_at": finished_at if done else None,
        "row_counts": '{"jobs": 1}' if done else None,
        "error": "restic_failed" if status == "failed" else None,
        "started_at": utcnow() if finished_at is None else finished_at - timedelta(minutes=10),
        "finished_at": finished_at,
    }
    return await _insert(engine, _SET, params)


async def backup_run(
    engine: AsyncEngine,
    status: BackupStatus,
    finished_at: datetime | None = None,
    *,
    kind: Literal["restore_test", "prune"] = "restore_test",
    set_id: int | None = None,
) -> int:
    """A restore-test (or prune) run, of the set `set_id`."""
    params: dict[str, object] = {
        "kind": kind,
        "set_id": set_id,
        "status": status,
        "error": "verify_failed" if status == "failed" else None,
        "started_at": utcnow() if finished_at is None else finished_at - timedelta(minutes=30),
        "finished_at": finished_at,
    }
    return await _insert(engine, _RUN, params)


async def shipment(engine: AsyncEngine, shipped_at: datetime, *, manifests: int) -> int:
    """A manifest shipment of `manifests` shipped manifests, each named by an item of it."""
    async with engine.begin() as conn:
        shipment_id = int((await conn.execute(_SHIPMENT, {"at": shipped_at})).scalar_one())
        for _ in range(manifests):
            params = {"completed_at": shipped_at - timedelta(hours=1), "shipped_at": shipped_at}
            await conn.execute(_SHIPPED, {**params, "shipment": shipment_id})
    return shipment_id


async def manifest(
    engine: AsyncEngine, status: Literal["executing", "completed"], completed_at: datetime | None = None
) -> int:
    """A manifest not shipped: `completed` at `completed_at`, with its file hash, or still `executing`."""
    sha = b"\x02" if status == "completed" else None
    return await _insert(engine, _COMPLETED, {"status": status, "sha": sha, "completed_at": completed_at})
