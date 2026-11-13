"""The weekly prune of the backup sets and the `manifests` snapshots (spec §19.5 Retention; plan M5 Task 24,
R13): `jobhunter backup prune`, and the scheduler's `prune` job (`JOBHUNTER_BACKUP_PRUNE_CRON`).

`run_prune` records a `backup_runs` row of kind `prune`, `running` as `jobhunter_backup`, then forgets in two
steps. First the manifest stream's snapshots strictly older than the latest receipt's (final fix C1, the
reason below): it reads the newest shipment's `restic_snapshot_id`, lists `restic snapshots --tag manifests`,
and runs `restic forget -- <id>…` on those whose time is before the receipted one's, if any. With no receipt,
or a receipted snapshot restic does not list, it forgets none. Then `restic forget --tag set --keep-daily <d>
--keep-weekly <w> --keep-monthly <m> --prune`, the counts from `JOBHUNTER_BACKUP_KEEP_DAILY` / `_WEEKLY` /
`_MONTHLY` (`cfg.backup`; 7, 4 and 3 by default), whose prune removes the data both forgets left unreferenced.
Its filter is the `set` tag, which every backup set's snapshot carries (`backup_set.SET_TAG`) and no
`manifests` snapshot does. The run then becomes `succeeded` with the counts it applied as its report (the
sets' three and `manifests_forgotten`), or `failed` with `error` = `prune:<error class>`, and `PruneFailed` is
raised. A prune's failure raises no alert (§17.6 has none): the health page shows the run. It holds the backup
jobs' lock for its whole run (`backup_set.backup_job_lock`): while another backup job runs it writes no row
and raises `BackupJobBusy`.

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

from collections.abc import Callable
from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.backup.backup_set import SET_TAG, BackupJobFailed, backup_job_lock
from jobhunter.backup.restic import Restic
from jobhunter.backup.shipper import MANIFESTS_TAG
from jobhunter.config import AppConfig
from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.observability.logging import get_logger

_START: Final = q(
    "INSERT INTO backup_runs (kind, status, started_at) VALUES ('prune', 'running', :started_at) RETURNING id"
)

# The latest receipt's snapshot: the newest shipment's (module docstring, "Why the receipt anchors it").
_LATEST_RECEIPT: Final = q("SELECT restic_snapshot_id FROM manifest_shipments ORDER BY id DESC LIMIT 1")

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


async def _forget_before_receipt(restic: Restic, engine: AsyncEngine, run_id: int) -> int:
    """Forget the `manifests` snapshots strictly older than the latest receipt's (module docstring); how
    many."""
    async with engine.connect() as conn:
        receipted = (await conn.execute(_LATEST_RECEIPT)).scalar_one_or_none()
    if receipted is None:
        log.info("manifests_forget_skipped", run_id=run_id, reason="no_receipt")
        return 0
    snapshots = await restic.snapshots(tags=[MANIFESTS_TAG])
    anchor = next((snapshot for snapshot in snapshots if snapshot.id == receipted), None)
    if anchor is None:
        log.warning("manifests_forget_skipped", run_id=run_id, reason="receipt_not_listed")
        return 0
    older = [snapshot.id for snapshot in snapshots if snapshot.time < anchor.time]
    if older:
        await restic.forget_snapshots(older)
    return len(older)


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
        forgotten = await _forget_before_receipt(restic, engine, run_id)
        await restic.forget_prune(tag=SET_TAG, **sets)
    except Exception as exc:  # recorded as `prune:<error class>`, then raised as PruneFailed
        error = f"prune:{type(exc).__name__}"
        async with engine.begin() as conn:
            await transition(conn, "backup_runs", run_id, ["running"], "failed", reason=error)
        log.error("backup_prune_failed", run_id=run_id, error=error)
        raise PruneFailed(run_id, error) from exc
    async with engine.begin() as conn:
        report = {**sets, "manifests_forgotten": forgotten}
        await transition(conn, "backup_runs", run_id, ["running"], "succeeded", set_={"report": report})
    log.info("backup_prune_succeeded", run_id=run_id)
    return run_id
