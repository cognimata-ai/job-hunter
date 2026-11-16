"""The weekly prune of the backup sets and the `manifests` snapshots (spec §19.5 Retention; plan M5 Task 24,
R13): `jobhunter backup prune`, and the scheduler's `prune` job (`JOBHUNTER_BACKUP_PRUNE_CRON`).

`run_prune` records a `backup_runs` row of kind `prune`, `running` as `jobhunter_backup`, then forgets in two
steps. First the manifest stream's snapshots strictly older than the latest receipt's (final fix C1, the
reason below), under the shipment lock (`shipper.ship_lock`, final fix D1): it reads the newest shipment's
`restic_snapshot_id`, lists `restic snapshots --tag manifests`, and runs `restic forget -- <id>…` on those
whose time is before the receipted one's, if any. With no receipt it forgets none; a receipted snapshot restic
does not list is repaired, below. Then `restic forget --tag set --keep-daily <d> --keep-weekly <w>
--keep-monthly <m> --prune`, the counts from `JOBHUNTER_BACKUP_KEEP_DAILY` / `_WEEKLY` / `_MONTHLY`
(`cfg.backup`; 7, 4 and 3 by default), whose prune removes the data both forgets left unreferenced. Its filter
is the `set` tag, which every backup set's snapshot carries (`backup_set.SET_TAG`) and no `manifests` snapshot
does. The run then becomes `succeeded` with its report: the sets' three counts, `manifests_forgotten`,
`manifests_skipped` (why it forgot none: `no_receipt`, `receipt_not_listed`, or `null` when it did not skip;
final fix D3) and `receipts_removed` (the shipment ids of the false receipts it removed); or `failed` with
`error` = `prune:<error class>`, and `PruneFailed` is raised. A prune's failure raises no alert (§17.6 has
none): the health page shows the run. It holds the backup jobs' lock for its whole run
(`backup_set.backup_job_lock`): while another backup job runs it writes no row and raises `BackupJobBusy`.

**One step with the shipments** (final fix D1 (a)). The prune takes the shipment lock (7261007) inside its job
lock (7261006), the order `db.migrate` registers (`LOCK_ORDER`; a shipper never takes the job lock), and holds
it from its receipt read through its listing and its forget of the `manifests` snapshots; it releases it
before the sets' forget. No shipment lands in between: one would otherwise be receipted after the read and,
timestamped before the anchor by a clock stepped back, forgotten as older (the final re-review's R2-C1). A
shipment meanwhile waits for the prune, then ships.

**On the lock's session** (final fix E1; the final re-review's R3-C1). The lock's session can end at any
point (a PostgreSQL restart, a cut connection), and a shipment may land once it has. So the prune reads the
latest receipt, and removes false receipts, in transactions on that session
(`shipper.HeldShipLock.transaction`): while it lives it holds the lock, so what it read is still the latest
receipt when the removal commits, and a session that ended took the lock and the removal with it (the run
fails `prune:ShipLockLost`, nothing removed). Its decision is taken against the one listing it made under the
lock, and it never takes the lock again to go on with that listing. The removal walks only the receipts at or
below the one it read, and the forget passes only ids from that listing, so a snapshot a later shipment made
is never forgotten by the run. Just before the forget, restic's and not the database's, it confirms it still
holds the lock (`shipper.HeldShipLock.confirm`, final fix D2): a lock session that ended took the lock with
it, so the run fails `prune:ShipLockLost`, with nothing forgotten. A shipment that loses its lock in turn
writes no receipt (`shipper`), so no snapshot the prune lists, forgets or not, gains one afterwards.

**A false receipt is removed** (final fix D1 (b)). A latest receipt whose snapshot restic does not list
proves nothing, and the shipper's unchanged-stream check would trust it for ever. So, in the same locked step
and one transaction on the lock's session, the prune removes it: the receipt row locked (`FOR UPDATE`, so the
`manifest_shipping`
reconciler cannot mark a manifest with it meanwhile), each manifest it shipped returned `shipped ->
completed` by `unship_manifest` (`shipped_at` and `shipment_id` cleared, `completed_at` kept; migration
0012, final fix E2: the backup role's one way to change a manifest, as it holds no UPDATE on them), its
items deleted, then the row; then the receipt below it, while restic does not list that one's snapshot
either, until one is listed or none remains. Each is logged `manifest_receipt_removed` with its shipment
id, snapshot id, item count and the number of manifests returned (ids and counts only). It forgets no
`manifests` snapshot in that run. The reconciler then raises `privacy.manifest_unshipped` for a returned
manifest completed more than 1 h ago, the UI reports it not confirmed offsite again, and the next ship finds
the stream unlike the latest receipt's items and uploads all of it.

**Why the receipt anchors it.** A restore reads the union of every `manifests` snapshot
(`restore.restore_stream`), so forgetting the older ones bounds its work, which would otherwise grow with
every shipment. The latest receipt names a complete snapshot: a receipt is written only after restic saved
every listed file (`shipper`), shipments are serialised (the shipment lock, `shipper.SHIP_LOCK_KEY`), and
every run lists every file of the stream, which only grows (a manifest file is written once and never
removed). So that snapshot holds every manifest any shipment confirmed up to it, and every older snapshot's
files besides. A newer snapshot, incomplete (restic's exit 3, no receipt) or not, can only add to the union,
so it is kept. No count of snapshots is a proof of completeness: ten incomplete snapshots after the receipted
one would otherwise evict it (the final re-review's R-C1). The union stays small unless shipments keep
failing, and then `privacy.manifest_unshipped` pages.
"""

from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

import psycopg
from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.backup.backup_set import SET_TAG, BackupJobFailed, backup_job_lock
from jobhunter.backup.restic import Restic
from jobhunter.backup.shipper import MANIFESTS_TAG, HeldShipLock, ship_lock
from jobhunter.config import AppConfig
from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.observability.logging import get_logger

_START: Final = q(
    "INSERT INTO backup_runs (kind, status, started_at) VALUES ('prune', 'running', :started_at) RETURNING id"
)

# The receipts, read and removed on the lock's session (module docstring; psycopg's own parameters). The
# latest receipt: the newest shipment's ("Why the receipt anchors it"); then, locked when the prune may remove
# it ("A false receipt is removed"), the latest at or below the one read under the lock.
_LATEST_RECEIPT: Final = "SELECT id, restic_snapshot_id FROM manifest_shipments ORDER BY id DESC LIMIT 1"
_RECEIPT_LOCKED: Final = (
    "SELECT id, restic_snapshot_id FROM manifest_shipments WHERE id <= %s ORDER BY id DESC LIMIT 1 FOR UPDATE"
)
_ITEM_COUNT: Final = "SELECT count(*) FROM manifest_shipment_items WHERE shipment_id = %s"
# Each manifest the receipt shipped, returned to `completed` by `unship_manifest` (migration 0012, the backup
# role's one way to change a manifest, final fix E2): the ids first, then the function on each.
_UNSHIP: Final = (
    "WITH shipped AS MATERIALIZED (SELECT id FROM deletion_manifests "
    "WHERE shipment_id = %s AND status = 'shipped') "
    "SELECT count(*) FILTER (WHERE unship_manifest(id)) FROM shipped"
)
_REMOVE_ITEMS: Final = "DELETE FROM manifest_shipment_items WHERE shipment_id = %s"
_REMOVE_RECEIPT: Final = "DELETE FROM manifest_shipments WHERE id = %s"

log = get_logger("jobhunter.backup.prune")


class PruneFailed(BackupJobFailed):
    """The prune run `run_id` is `failed` with `error`."""

    def __init__(self, run_id: int, error: str) -> None:
        super().__init__(error)
        self.run_id = run_id


async def run_prune(
    cfg: AppConfig, restic: Restic, engine: AsyncEngine, *, clock: Callable[[], datetime]
) -> int:
    """Forget the `manifests` snapshots older than the latest receipt's and the backup sets beyond the counts
    kept, and prune what they alone held (module docstring); the run's id. `PruneFailed` once the run is
    `failed`, `BackupJobBusy` (nothing written) while another backup job runs."""
    async with backup_job_lock(cfg, "prune"):
        return await _prune(cfg, restic, engine, clock=clock)


@dataclass(frozen=True)
class _FalseReceipt:
    """A receipt the prune removed: its shipment and snapshot ids, its item count and the manifests it
    returned to `completed`."""

    shipment_id: int
    snapshot_id: str
    items: int
    manifests: int


# Why the prune forgot no `manifests` snapshot, in its report's `manifests_skipped` (final fix D3).
SkipReason = Literal["no_receipt", "receipt_not_listed"]


@dataclass(frozen=True)
class _ManifestsPruned:
    forgotten: int
    skipped: SkipReason | None
    removed: tuple[int, ...]


async def _count(conn: psycopg.AsyncConnection[Any], statement: str, shipment: int) -> int:
    row = await (await conn.execute(statement, (shipment,))).fetchone()
    return int(row[0]) if row is not None else 0


async def _remove_false_receipts(
    held: HeldShipLock, latest: int, listed: Collection[str]
) -> list[_FalseReceipt]:
    """In one transaction on the lock's session, the latest receipt at or below `latest` (the one read under
    the lock) while `listed` (the listing taken under the lock) lacks its snapshot: its manifests returned to
    `completed`, its items and its row deleted; then the one below it (module docstring). `ShipLockLost`, with
    nothing removed, once the session has ended."""
    removed: list[_FalseReceipt] = []
    async with held.transaction() as conn:
        while (receipt := await (await conn.execute(_RECEIPT_LOCKED, (latest,))).fetchone()) is not None:
            shipment, snapshot_id = int(receipt[0]), str(receipt[1])
            if snapshot_id in listed:
                break
            items = await _count(conn, _ITEM_COUNT, shipment)
            returned = await _count(conn, _UNSHIP, shipment)
            await conn.execute(_REMOVE_ITEMS, (shipment,))
            await conn.execute(_REMOVE_RECEIPT, (shipment,))
            removed.append(_FalseReceipt(shipment, snapshot_id, items, returned))
    return removed


async def _latest_receipt(held: HeldShipLock) -> tuple[int, str] | None:
    """The latest receipt's shipment and snapshot ids, read on the lock's session (module docstring)."""
    async with held.transaction() as conn:
        row = await (await conn.execute(_LATEST_RECEIPT)).fetchone()
    return None if row is None else (int(row[0]), str(row[1]))


async def _prune_manifests(cfg: AppConfig, restic: Restic, run_id: int) -> _ManifestsPruned:
    """Under the shipment lock, on its session: forget the `manifests` snapshots strictly older than the
    latest receipt's, or remove the false receipts when restic does not list its snapshot (module
    docstring)."""
    async with ship_lock(cfg, holder="prune") as held:
        latest = await _latest_receipt(held)
        if latest is None:
            log.info("manifests_forget_skipped", run_id=run_id, reason="no_receipt")
            return _ManifestsPruned(0, "no_receipt", ())
        latest_id, latest_snapshot = latest
        snapshots = await restic.snapshots(tags=[MANIFESTS_TAG])
        anchor = next((snapshot for snapshot in snapshots if snapshot.id == latest_snapshot), None)
        if anchor is None:
            # no confirmation first: the removal commits on the lock's session or not at all
            removed = await _remove_false_receipts(held, latest_id, {snapshot.id for snapshot in snapshots})
            for receipt in removed:
                log.warning(
                    "manifest_receipt_removed",
                    run_id=run_id,
                    shipment_id=receipt.shipment_id,
                    snapshot_id=receipt.snapshot_id,
                    items=receipt.items,
                    manifests=receipt.manifests,
                )
            log.warning("manifests_forget_skipped", run_id=run_id, reason="receipt_not_listed")
            return _ManifestsPruned(
                0, "receipt_not_listed", tuple(receipt.shipment_id for receipt in removed)
            )
        older = [snapshot.id for snapshot in snapshots if snapshot.time < anchor.time]
        if older:
            await held.confirm()
            await restic.forget_snapshots(older)
        return _ManifestsPruned(len(older), None, ())


async def _prune(
    cfg: AppConfig, restic: Restic, engine: AsyncEngine, *, clock: Callable[[], datetime]
) -> int:
    sets = {
        "keep_daily": cfg.backup.keep_daily,
        "keep_weekly": cfg.backup.keep_weekly,
        "keep_monthly": cfg.backup.keep_monthly,
    }
    async with engine.begin() as conn:
        run_id = int((await conn.execute(_START, {"started_at": clock()})).scalar_one())
    log.info("backup_prune_started", run_id=run_id, **sets)
    try:
        manifests = await _prune_manifests(cfg, restic, run_id)
        await restic.forget_prune(tag=SET_TAG, **sets)
    except Exception as exc:  # recorded as `prune:<error class>`, then raised as PruneFailed
        error = f"prune:{type(exc).__name__}"
        async with engine.begin() as conn:
            await transition(conn, "backup_runs", run_id, ["running"], "failed", reason=error)
        log.error("backup_prune_failed", run_id=run_id, error=error)
        raise PruneFailed(run_id, error) from exc
    async with engine.begin() as conn:
        report = {
            **sets,
            "manifests_forgotten": manifests.forgotten,
            "manifests_skipped": manifests.skipped,
            "receipts_removed": list(manifests.removed),
        }
        await transition(conn, "backup_runs", run_id, ["running"], "succeeded", set_={"report": report})
    log.info("backup_prune_succeeded", run_id=run_id)
    return run_id
