"""The deletion-manifest shipper of the backup container (spec §19.5 step 5; plan M5 Task 10, R14, Deviation
30): `jobhunter backup ship`, every 5 minutes in prod and on demand on the dev stack (`$DC run --rm shipper`).

`ship_manifests` reads the stream: the regular files of `cfg.manifests_dir` named `<manifest_uuid>.json` with
the uuid in canonical form (`privacy.manifests.manifest_file_name`), never a `*.tmp` being written (its name
ends in `.json.tmp`), each hashed (sha256). When the stream's `(uuid, sha256)` set equals the items of the
latest shipment, or the stream is empty (no purge yet: restic makes no snapshot of nothing), nothing is
shipped. Otherwise it lists every file of the stream, one absolute path per line, in a file of
`cfg.staging_dir` (removed afterwards), runs one `restic backup --files-from-verbatim <list> --tag
manifests`, and then, in one transaction as `jobhunter_backup`, inserts a `manifest_shipments` row (the
snapshot id, `shipped_at` = `now`) with one `manifest_shipment_items` row per file: exactly the files the
snapshot holds, since a listed file that cannot be read makes restic exit 3 and no row is written then. Every
run ships every file, so the latest shipment's snapshot is the complete stream, and coverage is exact: a
manifest whose file did not exist yet has no item. The worker's `manifest_shipping` reconciler then marks
`shipped` each completed manifest an item names with its file's hash.

A restic failure raises `ResticError` before any row is written. On exit 3 restic has still saved an
incomplete snapshot tagged `manifests` (the files it could read): no row names it, but until the next run
restic's newest `manifests` snapshot is that one. The next run ships a complete one, since the stream still
differs from the latest shipment's items. A file of another name (a stray
`notes.json`, a directory) is left out: its name is no uuid an item could hold. The log lines carry counts and
the shipment id only.
"""

import hashlib
import os
import stat
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.backup.restic import Restic
from jobhunter.config import AppConfig
from jobhunter.db.sql import q
from jobhunter.observability.logging import get_logger

MANIFESTS_TAG: Final = "manifests"
_SUFFIX: Final = ".json"
_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC

_LATEST_ITEMS: Final = q(
    "SELECT manifest_uuid, file_sha256 FROM manifest_shipment_items "
    "WHERE shipment_id = (SELECT max(id) FROM manifest_shipments)"
)
_SHIPMENT: Final = q(
    "INSERT INTO manifest_shipments (restic_snapshot_id, shipped_at) VALUES (:snapshot, :now) RETURNING id"
)
_ITEM: Final = q(
    "INSERT INTO manifest_shipment_items (shipment_id, manifest_uuid, file_sha256) "
    "VALUES (:shipment, :uuid, :sha)"
)

log = get_logger("jobhunter.backup.shipper")


@dataclass(frozen=True)
class ShipResult:
    shipped: bool
    snapshot_id: str | None
    files: int


@dataclass(frozen=True, order=True)
class _StreamFile:
    manifest_uuid: uuid.UUID
    path: Path
    sha256: bytes


def _manifest_uuid(name: str) -> uuid.UUID | None:
    """The uuid a stream file's name holds (`<uuid>.json`, canonical lower-case form), or `None`."""
    if not name.endswith(_SUFFIX):
        return None
    stem = name.removesuffix(_SUFFIX)
    try:
        parsed = uuid.UUID(stem)
    except ValueError:
        return None
    return parsed if str(parsed) == stem else None


def _sha256(path: Path) -> bytes:
    fd = os.open(path, _READ_FLAGS)
    with os.fdopen(fd, "rb") as handle:
        return hashlib.sha256(handle.read()).digest()


def _stream(directory: Path) -> list[_StreamFile]:
    """The manifest files of `directory`, by uuid (module docstring)."""
    files = []
    with os.scandir(directory) as entries:
        for entry in entries:
            manifest_uuid = _manifest_uuid(entry.name)
            if manifest_uuid is None or not stat.S_ISREG(entry.stat(follow_symlinks=False).st_mode):
                continue
            path = directory / entry.name
            files.append(_StreamFile(manifest_uuid, path, _sha256(path)))
    return sorted(files)


async def _latest_items(engine: AsyncEngine) -> set[tuple[uuid.UUID, bytes]]:
    async with engine.connect() as conn:
        rows = (await conn.execute(_LATEST_ITEMS)).all()
    return {(row.manifest_uuid, bytes(row.file_sha256)) for row in rows}


def _write_list(directory: Path, files: Sequence[_StreamFile]) -> Path:
    """A new file in `directory` that names each of `files`, one absolute path per line."""
    fd, name = tempfile.mkstemp(prefix=".manifests-", suffix=".list", dir=directory)
    listing = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.writelines(f"{file.path}\n" for file in files)
    except BaseException:
        _remove(listing)
        raise
    return listing


def _remove(path: Path) -> None:
    path.unlink(missing_ok=True)


async def _backup(cfg: AppConfig, restic: Restic, files: Sequence[_StreamFile]) -> str:
    """One `manifests` snapshot of exactly `files`, through a list file in the staging directory."""
    listing = _write_list(cfg.staging_dir, files)
    try:
        return await restic.backup(files_from=listing, tags=[MANIFESTS_TAG])
    finally:
        _remove(listing)


async def ship_manifests(cfg: AppConfig, restic: Restic, engine: AsyncEngine, *, now: datetime) -> ShipResult:
    """Ship the manifest stream unless it is unchanged (module docstring)."""
    files = _stream(cfg.manifests_dir)
    current = {(file.manifest_uuid, file.sha256) for file in files}
    if not files or current == await _latest_items(engine):
        log.info("manifests_unchanged", files=len(files))
        return ShipResult(shipped=False, snapshot_id=None, files=len(files))
    snapshot_id = await _backup(cfg, restic, files)
    async with engine.begin() as conn:
        shipment = int((await conn.execute(_SHIPMENT, {"snapshot": snapshot_id, "now": now})).scalar_one())
        await conn.execute(
            _ITEM, [{"shipment": shipment, "uuid": file.manifest_uuid, "sha": file.sha256} for file in files]
        )
    log.info("manifests_shipped", files=len(files), shipment_id=shipment)
    return ShipResult(shipped=True, snapshot_id=snapshot_id, files=len(files))
