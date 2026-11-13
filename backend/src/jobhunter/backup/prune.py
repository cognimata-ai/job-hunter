"""The weekly prune of the backup sets and the `manifests` snapshots (spec §19.5 Retention; plan M5 Task 24,
R13): `jobhunter backup prune`, and the scheduler's `prune` job (`JOBHUNTER_BACKUP_PRUNE_CRON`).

`run_prune` records a `backup_runs` row of kind `prune`, `running` as `jobhunter_backup`, then forgets in two
steps. First the manifest stream's snapshots strictly older than the latest receipt's (final fix C1, the
reason below), under the shipment lock (`shipper.ship_lock`, final fix D1): it reads the newest shipment's
`restic_snapshot_id`, lists `restic snapshots --tag manifests`, and runs `restic forget -- <id>…` on those
whose time is before the receipted one's, if any. With no receipt it forgets none; a receipted snapshot
restic does not list is repaired, below. Then `restic forget --tag set --keep-daily <d> --keep-weekly <w>
--keep-monthly <m> --prune`, the counts from `JOBHUNTER_BACKUP_KEEP_DAILY` / `_WEEKLY` / `_MONTHLY`
(`cfg.backup`; 7, 4 and 3 by default), whose prune removes the data both forgets left unreferenced. Its
filter is the `set` tag, which every backup set's snapshot carries (`backup_set.SET_TAG`) and no `manifests`
snapshot does. The run then becomes `succeeded` with its report: the sets' three counts,
`manifests_forgotten` and `receipts_removed` (the shipment ids of the false receipts it removed); or `failed`
with `error` = `prune:<error class>`, and `PruneFailed` is raised. A prune's failure raises no alert (§17.6
has none): the health page shows the run. It holds the backup jobs' lock for its whole run
(`backup_set.backup_job_lock`): while another backup job runs it writes no row and raises `BackupJobBusy`.

**One step with the shipments** (final fix D1 (a)). The prune takes the shipment lock (7261007) inside its job
lock (7261006), the order `db.migrate` registers (`LOCK_ORDER`; a shipper never takes the job lock), and holds
it from its receipt read through its listing and its forget of the `manifests` snapshots; it releases it
before the sets' forget. No shipment lands in between: one would otherwise be receipted after the read and,
timestamped before the anchor by a clock stepped back, forgotten as older (the final re-review's R2-C1). A
shipment meanwhile waits for the prune, then ships.

**A false receipt is removed** (final fix D1 (b)). A latest receipt whose snapshot restic does not list
proves nothing, and the shipper's unchanged-stream check would trust it for ever. So, in the same locked step
and one transaction, the prune removes it: the receipt row locked (`FOR UPDATE`, so the `manifest_shipping`
reconciler cannot mark a manifest with it meanwhile), each manifest it shipped returned `shipped ->
completed` (`shipped_at` and `shipment_id` cleared, `completed_at` kept), its items deleted, then the row;
then the receipt below it, while restic does not list that one's snapshot either, until one is listed or
none remains. Each is logged `manifest_receipt_removed` with its shipment id, snapshot id, item count and the
number of manifests returned (ids and counts only). It forgets no `manifests` snapshot in that run. The
reconciler then raises `privacy.manifest_unshipped` for a returned manifest completed more than 1 h ago, the
UI reports it not confirmed offsite again, and the next ship finds the stream unlike the latest receipt's
items and uploads all of it.

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
from typing import Final

from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.backup.backup_set import SET_TAG, BackupJobFailed, backup_job_lock
from jobhunter.backup.restic import Restic
from jobhunter.backup.shipper import MANIFESTS_TAG, ship_lock
from jobhunter.config import AppConfig
from jobhunter.db.sql import q
from jobhunter.db.transitions import transition, transition_where
from jobhunter.observability.logging import get_logger

_START: Final = q(
    "INSERT INTO backup_runs (kind, status, started_at) VALUES ('prune', 'running', :started_at) RETURNING id"
)

# The latest receipt: the newest shipment's (module docstring, "Why the receipt anchors it"); locked when the
# prune may remove it ("A false receipt is removed").
_LATEST_RECEIPT: Final = q("SELECT id, restic_snapshot_id FROM manifest_shipments ORDER BY id DESC LIMIT 1")
_LATEST_RECEIPT_LOCKED: Final = q(
    "SELECT id, restic_snapshot_id FROM manifest_shipments ORDER BY id DESC LIMIT 1 FOR UPDATE"
)
_ITEM_COUNT: Final = q("SELECT count(*) FROM manifest_shipment_items WHERE shipment_id = :shipment")
_REMOVE_ITEMS: Final = q("DELETE FROM manifest_shipment_items WHERE shipment_id = :shipment")
_REMOVE_RECEIPT: Final = q("DELETE FROM manifest_shipments WHERE id = :shipment")

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


@dataclass(frozen=True)
class _ManifestsPruned:
    forgotten: int
    removed: tuple[int, ...]


async def _remove_false_receipts(engine: AsyncEngine, listed: Collection[str]) -> list[_FalseReceipt]:
    """In one transaction, the latest receipt while restic does not list its snapshot: its manifests returned
    to `completed`, its items and its row deleted; then the one below it (module docstring)."""
    removed: list[_FalseReceipt] = []
    async with engine.begin() as conn:
        while (receipt := (await conn.execute(_LATEST_RECEIPT_LOCKED)).first()) is not None:
            if receipt.restic_snapshot_id in listed:
                break
            params = {"shipment": int(receipt.id)}
            items = int((await conn.execute(_ITEM_COUNT, params)).scalar_one())
            returned = await transition_where(
                conn,
                "deletion_manifests",
                ["shipped"],
                "completed",
                where="t.shipment_id = :shipment",
                params=params,
            )
            await conn.execute(_REMOVE_ITEMS, params)
            await conn.execute(_REMOVE_RECEIPT, params)
            removed.append(_FalseReceipt(int(receipt.id), receipt.restic_snapshot_id, items, len(returned)))
    return removed


async def _prune_manifests(
    cfg: AppConfig, restic: Restic, engine: AsyncEngine, run_id: int
) -> _ManifestsPruned:
    """Under the shipment lock: forget the `manifests` snapshots strictly older than the latest receipt's, or
    remove the false receipts when restic does not list its snapshot (module docstring)."""
    async with ship_lock(cfg, holder="prune"):
        async with engine.connect() as conn:
            latest = (await conn.execute(_LATEST_RECEIPT)).first()
        if latest is None:
            log.info("manifests_forget_skipped", run_id=run_id, reason="no_receipt")
            return _ManifestsPruned(0, ())
        snapshots = await restic.snapshots(tags=[MANIFESTS_TAG])
        anchor = next((snapshot for snapshot in snapshots if snapshot.id == latest.restic_snapshot_id), None)
        if anchor is None:
            removed = await _remove_false_receipts(engine, {snapshot.id for snapshot in snapshots})
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
            return _ManifestsPruned(0, tuple(receipt.shipment_id for receipt in removed))
        older = [snapshot.id for snapshot in snapshots if snapshot.time < anchor.time]
        if older:
            await restic.forget_snapshots(older)
        return _ManifestsPruned(len(older), ())


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
        manifests = await _prune_manifests(cfg, restic, engine, run_id)
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
            "receipts_removed": list(manifests.removed),
        }
        await transition(conn, "backup_runs", run_id, ["running"], "succeeded", set_={"report": report})
    log.info("backup_prune_succeeded", run_id=run_id)
    return run_id
