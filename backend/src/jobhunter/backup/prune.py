"""The weekly prune of the backup sets and the `manifests` snapshots (spec §19.5 Retention; plan M5 Task 24,
R13): `jobhunter backup prune`, and the scheduler's `prune` job (`JOBHUNTER_BACKUP_PRUNE_CRON`).

`run_prune` records a `backup_runs` row of kind `prune`, `running` as `jobhunter_backup`, then runs two
forgets. First `restic forget --tag manifests --group-by host --keep-last 10` (`MANIFESTS_KEPT`, final fix
B7): the manifest stream's snapshots beyond the newest ten, for the reason `MANIFESTS_KEPT` gives. Then
`restic forget --tag set --keep-daily <d> --keep-weekly <w> --keep-monthly <m> --prune`, the counts from
`JOBHUNTER_BACKUP_KEEP_DAILY` / `_WEEKLY` / `_MONTHLY` (`cfg.backup`; 7, 4 and 3 by default), whose prune
removes the data both forgets left unreferenced. Its filter is the `set` tag, which every backup set's
snapshot carries (`backup_set.SET_TAG`) and no `manifests` snapshot does. The run then becomes `succeeded`
with the counts it applied as its report (the sets' three and `keep_manifests`), or `failed` with `error` =
`prune:<error class>`, and `PruneFailed` is raised. A prune's failure raises no alert (§17.6 has none): the
health page shows the run. It holds the backup jobs' lock for its whole run (`backup_set.backup_job_lock`):
while another backup job runs it writes no row and raises `BackupJobBusy`.
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

# The `manifests` snapshots the prune keeps, the newest (final fix B7). A restore reads the union of every
# `manifests` snapshot (`restore.restore_stream`), so their number bounds its work, and without a bound it
# grows with every shipment. Forgetting the older ones loses no manifest: shipments are serialised (the
# shipment lock, `shipper.SHIP_LOCK_KEY`) and every run ships every file of the stream, which only grows (a
# manifest file is written once and never removed), so each complete snapshot holds every manifest any earlier
# snapshot held. The newest ten hold the latest complete one unless ten incomplete ones (restic's exit 3,
# which needs a file to turn unreadable between the shipper's hash and restic's read) followed it.
MANIFESTS_KEPT: Final = 10

log = get_logger("jobhunter.backup.prune")


class PruneFailed(BackupJobFailed):
    """The prune run `run_id` is `failed` with `error`."""

    def __init__(self, run_id: int, error: str) -> None:
        super().__init__(error)
        self.run_id = run_id


async def run_prune(
    cfg: AppConfig, restic: Restic, engine: AsyncEngine, *, clock: Callable[[], datetime]
) -> int:
    """Forget the `manifests` snapshots beyond the newest `MANIFESTS_KEPT` and the backup sets beyond the
    counts kept, and prune what they alone held (module docstring); the run's id. `PruneFailed` once the run
    is `failed`, `BackupJobBusy` (nothing written) while another backup job runs."""
    async with backup_job_lock(cfg, "prune"):
        return await _prune(cfg, restic, engine, clock=clock)


async def _prune(
    cfg: AppConfig, restic: Restic, engine: AsyncEngine, *, clock: Callable[[], datetime]
) -> int:
    sets = {
        "keep_daily": cfg.backup.keep_daily,
        "keep_weekly": cfg.backup.keep_weekly,
        "keep_monthly": cfg.backup.keep_monthly,
    }
    keep = {**sets, "keep_manifests": MANIFESTS_KEPT}
    async with engine.begin() as conn:
        run_id = int((await conn.execute(_START, {"started_at": clock()})).scalar_one())
    log.info("backup_prune_started", run_id=run_id, **keep)
    try:
        await restic.forget_last(tag=MANIFESTS_TAG, keep_last=MANIFESTS_KEPT)
        await restic.forget_prune(tag=SET_TAG, **sets)
    except Exception as exc:  # recorded as `prune:<error class>`, then raised as PruneFailed
        error = f"prune:{type(exc).__name__}"
        async with engine.begin() as conn:
            await transition(conn, "backup_runs", run_id, ["running"], "failed", reason=error)
        log.error("backup_prune_failed", run_id=run_id, error=error)
        raise PruneFailed(run_id, error) from exc
    async with engine.begin() as conn:
        await transition(conn, "backup_runs", run_id, ["running"], "succeeded", set_={"report": keep})
    log.info("backup_prune_succeeded", run_id=run_id)
    return run_id
