"""The deletion-manifest shipper of the backup container (spec §19.5 step 5; plan M5 Task 10, R14, Deviation
30): `jobhunter backup ship`, every 5 minutes in prod and on demand on the dev stack (`$DC run --rm shipper`).

**One shipper at a time** (final review C1): `ship_manifests` holds the shipment lock `SHIP_LOCK_KEY`
(7261007, a one-key session advisory lock on a dedicated autocommit connection as `jobhunter_backup`,
`ship_lock`) from its listing of the stream through the upload and its receipt, which it writes on that same
connection (`HeldShipLock.transaction`, final fix E1). A second shipper (the
scheduler's while one runs by hand, say) waits for it, polling every `LOCK_POLL_S` with no snapshot held and
logging `manifest_ship_waiting` once, then lists the stream itself: it never skips, so a `backup ship` that
exits 0 shipped, or found shipped, every file there when it started. "Found shipped" holds under the lock
too: before it returns an unchanged (or empty) stream, it confirms that its session still holds the lock
(`HeldShipLock.confirm`, the final re-review's M4-3), so no prune removed the receipt it compared with
meanwhile; a session that ended stops it with `ShipLockLost`, as below. The snapshots are therefore made in the
order of the lists they hold, and an older list never becomes the newest snapshot after a newer list's
receipt. Shipping takes no backup job lock (`backup_set.BACKUP_JOB_LOCK_KEY`): restic's own repository lock
orders its upload against a prune's. The prune takes this shipment lock too, inside its job lock, from its
receipt read through its forget of the `manifests` snapshots (`backup.prune`, final fix D1), so a shipment
never lands between the prune's choice of the latest receipt and its forget; a shipper meanwhile waits for it
as for another shipper. That is the lock order `db.migrate` registers (`LOCK_ORDER`). Just before restic
starts, the shipper confirms on the lock's connection that its session is alive and still holds the lock
(`HeldShipLock.confirm`, final fix D2): a session that ended (a PostgreSQL restart, a cut connection) took
the lock with it, and another shipper may hold it now, so it stops with `ShipLockLost`, uploading nothing.

**The receipt commits under the lock or not at all** (final fix E1; the final re-review's R3-C1). A session
can end during the upload, after that confirmation. A prune may then take the free lock, anchor on the
latest receipt and forget the new snapshot as older (a clock stepped back), or another shipper may ship a
newer list. So the receipt is written in one transaction on the lock's own session: while the session lives
it holds the lock, so a receipt that commits did so under it; a session that ended took the lock and the
transaction with it, and the shipper stops with `ShipLockLost`, writing nothing. Its snapshot then has no
receipt, which claims nothing, and the next ship uploads the stream again. The read of the latest receipt's
items, before the upload, may run on another connection: only a live lock session writes a receipt, so none
changes while this one lives, and once it has ended this shipper writes none, nor reports the stream
unchanged (the confirmation above).

`ship_manifests` reads the stream: the regular files of `cfg.manifests_dir` named `<manifest_uuid>.json` with
the uuid in canonical form (`privacy.manifests.manifest_file_name`), never a `*.tmp` being written (its name
ends in `.json.tmp`), each hashed (sha256). When the stream's `(uuid, sha256)` set equals the items of the
latest shipment, or the stream is empty (no purge yet: restic makes no snapshot of nothing), nothing is
shipped. Otherwise it lists every file of the stream, one absolute path per line, in a file of
`cfg.staging_dir` (removed afterwards), runs one `restic backup --files-from-verbatim <list> --tag
manifests`, and then, in one transaction on the lock's session, inserts a `manifest_shipments` row (the
snapshot id, and `shipped_at` read from the clock then, under the lock, so a shipper that waited never
records the time it started at and shipments agree in time with their order, final fix B10) with one
`manifest_shipment_items` row per file: exactly the files the snapshot holds, since a listed file that cannot
be read makes restic exit 3 and no row is written then. Every run ships every file, so the latest shipment's
snapshot is the complete stream, and coverage is exact: a manifest whose file did not exist yet has no item.
The worker's `manifest_shipping` reconciler then marks `shipped` each completed manifest an item names with
its file's hash.

A restic failure raises `ResticError` before any row is written. On exit 3 restic has still saved an
incomplete snapshot tagged `manifests` (the files it could read): no row names it, and until the next run
restic's newest `manifests` snapshot is that one. A restore therefore reads the union of every `manifests`
snapshot, never the newest alone (`backup.restore.restore_stream`). The next run ships a complete one, since
the stream still differs from the latest shipment's items. A file of another name (a stray
`notes.json`, a directory) is left out: its name is no uuid an item could hold. The log lines carry counts and
the shipment id only.
"""

import asyncio
import hashlib
import os
import stat
import tempfile
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import psycopg
from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.backup.restic import Restic
from jobhunter.config import AppConfig
from jobhunter.db.sql import q
from jobhunter.observability.logging import get_logger

MANIFESTS_TAG: Final = "manifests"
# The shipment lock (module docstring; the project's advisory keys are listed in `db.migrate`).
SHIP_LOCK_KEY: Final = 7261007
LOCK_POLL_S: Final = 0.5
_SUFFIX: Final = ".json"
# Whether this session holds the shipment lock: a one-key advisory lock's bigint is `classid` (its high 32
# bits) and `objid` (its low 32 bits), with `objsubid = 1`.
_HOLDS_SHIP_LOCK: Final = (
    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted AND objsubid = 1 "
    "AND pid = pg_backend_pid() "
    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
    "AND ((classid::bigint << 32) | objid::bigint) = %s::bigint)"
)
_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC

_LATEST_ITEMS: Final = q(
    "SELECT manifest_uuid, file_sha256 FROM manifest_shipment_items "
    "WHERE shipment_id = (SELECT max(id) FROM manifest_shipments)"
)
# The receipt, written on the lock's session (psycopg's own parameters).
_SHIPMENT: Final = (
    "INSERT INTO manifest_shipments (restic_snapshot_id, shipped_at) VALUES (%s, %s) RETURNING id"
)
_ITEM: Final = (
    "INSERT INTO manifest_shipment_items (shipment_id, manifest_uuid, file_sha256) VALUES (%s, %s, %s)"
)

log = get_logger("jobhunter.backup.shipper")


class ShipLockLost(Exception):
    """The shipment lock's session ended, or no longer holds the lock, before an irreversible step or before
    the transaction on its session committed: the step was not taken (no upload, no forget), and nothing of
    the transaction was written (no receipt, none removed)."""


@dataclass(frozen=True)
class HeldShipLock:
    """The shipment lock as `ship_lock` holds it, on its own connection."""

    conn: psycopg.AsyncConnection[Any]
    holder: str

    async def confirm(self) -> None:
        """Check, on the lock's connection, that the session is alive and still holds the lock; `ShipLockLost`
        otherwise (final fix D2). A session that ended took the lock with it, and another may hold it now."""
        try:
            row = await (await self.conn.execute(_HOLDS_SHIP_LOCK, (SHIP_LOCK_KEY,))).fetchone()
        except psycopg.OperationalError as exc:
            log.warning("ship_lock_lost", lock_key=SHIP_LOCK_KEY, holder=self.holder)
            raise ShipLockLost from exc
        if row is None or not row[0]:
            log.warning("ship_lock_lost", lock_key=SHIP_LOCK_KEY, holder=self.holder)
            raise ShipLockLost

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[psycopg.AsyncConnection[Any]]:
        """A transaction on the lock's own session, for the receipts' reads and writes (final fix E1): while
        the session lives it holds the lock, so what commits here commits under it; a session that ended
        before the COMMIT took the lock and the transaction with it, and nothing of it is written:
        `ShipLockLost`. Another database error is raised as it is."""
        try:
            async with self.conn.transaction():
                yield self.conn
        except psycopg.OperationalError as exc:
            if not (self.conn.broken or self.conn.closed):
                raise
            log.warning("ship_lock_lost", lock_key=SHIP_LOCK_KEY, holder=self.holder)
            raise ShipLockLost from exc


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


async def _try_ship_lock(conn: psycopg.AsyncConnection[Any]) -> bool:
    row = await (await conn.execute("SELECT pg_try_advisory_lock(%s::bigint)", (SHIP_LOCK_KEY,))).fetchone()
    return row is not None and bool(row[0])


@asynccontextmanager
async def ship_lock(cfg: AppConfig, *, holder: str = "ship") -> AsyncIterator[HeldShipLock]:
    """`SHIP_LOCK_KEY` held for the block, on a dedicated autocommit connection as `jobhunter_backup`
    (`jobhunter-backup-<holder>`: a shipper, or the prune); while another holds it, waited for (module
    docstring). The block confirms it still holds it before each irreversible step (`HeldShipLock.confirm`).
    Released and closed on every path; closing the connection would drop it too."""
    conn = await psycopg.AsyncConnection.connect(
        cfg.dsn("psycopg", db_role="backup"), autocommit=True, application_name=f"jobhunter-backup-{holder}"
    )
    try:
        waiting = False
        while not await _try_ship_lock(conn):  # polls the database, so there is no event to wait for
            if not waiting:
                log.info("manifest_ship_waiting", lock_key=SHIP_LOCK_KEY, holder=holder)
                waiting = True
            await asyncio.sleep(LOCK_POLL_S)
        try:
            yield HeldShipLock(conn, holder)
        finally:
            # A lost connection took the lock with it; do not let that hide the error that ended the shipment.
            with suppress(psycopg.OperationalError):
                await conn.execute("SELECT pg_advisory_unlock(%s::bigint)", (SHIP_LOCK_KEY,))
    finally:
        await conn.close()


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


async def _receipt(
    held: HeldShipLock, snapshot_id: str, shipped_at: datetime, files: Sequence[_StreamFile]
) -> int:
    """The receipt of `snapshot_id`, which holds exactly `files`: its `manifest_shipments` row and one item
    per file, in one transaction on the lock's session (module docstring); the shipment's id."""
    async with held.transaction() as conn:
        row = await (await conn.execute(_SHIPMENT, (snapshot_id, shipped_at))).fetchone()
        if row is None:
            raise RuntimeError("the receipt's INSERT returned no id")
        shipment = int(row[0])
        async with conn.cursor() as cursor:
            await cursor.executemany(_ITEM, [(shipment, file.manifest_uuid, file.sha256) for file in files])
    return shipment


async def _backup(cfg: AppConfig, restic: Restic, files: Sequence[_StreamFile], held: HeldShipLock) -> str:
    """One `manifests` snapshot of exactly `files`, through a list file in the staging directory, once the
    shipment lock is confirmed held just before restic starts."""
    listing = _write_list(cfg.staging_dir, files)
    try:
        await held.confirm()
        return await restic.backup(files_from=listing, tags=[MANIFESTS_TAG])
    finally:
        _remove(listing)


async def ship_manifests(
    cfg: AppConfig, restic: Restic, engine: AsyncEngine, *, clock: Callable[[], datetime]
) -> ShipResult:
    """Ship the manifest stream unless it is unchanged, under the shipment lock from the listing through the
    receipt, which commits on the lock's session or not at all (module docstring); the receipt's
    `shipped_at` is read from `clock` under the lock."""
    async with ship_lock(cfg) as held:
        files = _stream(cfg.manifests_dir)
        current = {(file.manifest_uuid, file.sha256) for file in files}
        if not files or current == await _latest_items(engine):
            await held.confirm()  # the lock still held, so the receipt compared with is still there
            log.info("manifests_unchanged", files=len(files))
            return ShipResult(shipped=False, snapshot_id=None, files=len(files))
        snapshot_id = await _backup(cfg, restic, files, held)
        shipment = await _receipt(held, snapshot_id, clock(), files)
    log.info("manifests_shipped", files=len(files), shipment_id=shipment)
    return ShipResult(shipped=True, snapshot_id=snapshot_id, files=len(files))
