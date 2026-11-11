"""The weekly prune of the backup sets (spec §19.5 Retention; plan M5 Task 24, R13): `jobhunter backup
prune`, and the scheduler's `prune` job (`JOBHUNTER_BACKUP_PRUNE_CRON`).

`run_prune` records a `backup_runs` row of kind `prune`, `running` as `jobhunter_backup`, then runs one
`restic forget --tag set --keep-daily <d> --keep-weekly <w> --keep-monthly <m> --prune`, the counts from
`JOBHUNTER_BACKUP_KEEP_DAILY` / `_WEEKLY` / `_MONTHLY` (`cfg.backup`; 7, 4 and 3 by default). The filter is
the `set` tag, which every backup set's snapshot carries (`backup_set.SET_TAG`) and no `manifests` snapshot
does: the manifest stream is never forgotten (it holds ids and hashes only). The run then becomes `succeeded`
with the counts it applied as its report, or `failed` with `error` = `prune:<error class>`, and `PruneFailed`
is raised. A prune's failure raises no alert (§17.6 has none): the health page shows the run.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.backup.backup_set import SET_TAG, BackupJobFailed
from jobhunter.backup.restic import Restic
from jobhunter.config import AppConfig
from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.observability.logging import get_logger

_START: Final = q(
    "INSERT INTO backup_runs (kind, status, started_at) VALUES ('prune', 'running', :started_at) RETURNING id"
)

log = get_logger("jobhunter.backup.prune")


class PruneFailed(BackupJobFailed):
    """The prune run `run_id` is `failed` with `error`."""

    def __init__(self, run_id: int, error: str) -> None:
        super().__init__(error)
        self.run_id = run_id


async def run_prune(
    cfg: AppConfig, restic: Restic, engine: AsyncEngine, *, clock: Callable[[], datetime]
) -> int:
    """Forget the backup sets beyond the counts kept and prune what they alone held (module docstring); the
    run's id. `PruneFailed` once the run is `failed`."""
    keep = {
        "keep_daily": cfg.backup.keep_daily,
        "keep_weekly": cfg.backup.keep_weekly,
        "keep_monthly": cfg.backup.keep_monthly,
    }
    async with engine.begin() as conn:
        run_id = int((await conn.execute(_START, {"started_at": clock()})).scalar_one())
    log.info("backup_prune_started", run_id=run_id, **keep)
    try:
        await restic.forget_prune(tag=SET_TAG, **keep)
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
