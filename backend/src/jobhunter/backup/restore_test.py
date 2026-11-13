"""The monthly restore test (spec §19.5 "Backup sets" and "Restore order", §22 V22; plan M5 Task 26, R15, R23,
Deviations 29, 46 and 55, Ruling T26-1): `jobhunter backup restore-test`, and the scheduler's `restore_test`
job (`JOBHUNTER_RESTORE_TEST_CRON`, the first Sunday of each month).

`run_restore_test` holds the backup jobs' lock (`backup_set.backup_job_lock`: while another job runs it writes
nothing and raises `BackupJobBusy`), records a `backup_runs` row of kind `restore_test`, `running`, as the
backup role, then restores the latest set into a scratch database and directory, never the application's:
- `scratch`: as `jobhunter_restore` (on the `postgres` database), a leftover `jobhunter_restore_test` dropped
  and `CREATE DATABASE jobhunter_restore_test TEMPLATE jobhunter_template`; `<staging_dir>/restore_test` made
  fresh with `set/`, `manifests/`, `cv/` and `exports/` (the last empty: a set holds no export archive);
- `stream`, `set`, `cv`: the manifest stream (the union of every `manifests` snapshot, through
  `<staging_dir>/restore_test/stream`, with the same conflict check) and the latest set into those
  directories by the same subfolder restores as `restore run` (`backup.restore`), with the same empty-stream
  rule; the run row names the set (`backup_set_id`), and the report the stream's snapshot and manifest counts;
- `pg_restore` and `verify_restored` against the scratch database as `jobhunter_restore`;
- `migrate`: `run_migrate_async(cfg, database="jobhunter_restore_test", steps={"sequences", "alembic",
  "replay"}, manifest_dir=…, cv_dir=…, exports_dir=…, db_role="restore")`, in process with the backup role's
  configuration (its `db_restore` secret), never the CLI; the replay has no external effect (Deviation 56), so
  the backup role needs no Anthropic or healthchecks secret;
- `exports`: `restore run`'s step 7b against the scratch database and directory;
- `verify_replayed` on the scratch stream and directories.
Then, after a failure too, the scratch database is dropped (as `jobhunter_restore`, `WITH (FORCE)`) and the
scratch directory removed, and the run becomes `succeeded` with its report (the set and snapshot, the stream,
both verification reports, the migrate steps and the exports ended: ids, codes and counts only), or `failed`
with `error` = `<step>:<code>` (the step's `RestoreFailed` code, or an unforeseen error's class) and the
report so far, and `RestoreTestFailed` is raised. An exception outside every step (the cleanup's included)
fails the run too, as `unexpected:<error class>`: a run is never left `running` by its own process. A failed
run raises `backup.restore_test_failed` through the worker's `backup_health` reconciler (Task 7): no alert is
raised here.
"""

import shutil
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Final

import psycopg
from psycopg import sql
from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.backup.backup_set import SET_STAGING, BackupJobFailed, backup_job_lock
from jobhunter.backup.restic import Restic
from jobhunter.backup.restore import (
    STREAM_SCRATCH,
    VERIFY_FAILED,
    RestoreFailed,
    RestoreStep,
    check_restored,
    end_restored_exports,
    load_dump,
    null_engine,
    restore_cv_files,
    restore_set,
    restore_step,
    restore_stream,
    restored_failure,
)
from jobhunter.backup.runner import CommandRunner
from jobhunter.config import AppConfig
from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.observability.logging import get_logger
from jobhunter.privacy.manifests import FileRoots
from jobhunter.privacy.restore_verify import verify_replayed
from jobhunter.types import JSONValue

RESTORE_TEST_DB: Final = "jobhunter_restore_test"
RESTORE_TEST_STAGING: Final = "restore_test"
TEMPLATE_DB: Final = "jobhunter_template"
# The error's step for a failure outside every step (`unexpected:<error class>`, review M7).
UNEXPECTED: Final = "unexpected"
SCRATCH_DIRS: Final = ("set", "manifests", "cv", "exports")

_START: Final = q(
    "INSERT INTO backup_runs (kind, status, started_at) VALUES ('restore_test', 'running', :at) RETURNING id"
)
_NAME_SET: Final = q(
    "UPDATE backup_runs SET backup_set_id = :set WHERE id = :run AND status = 'running' "
    "AND EXISTS (SELECT 1 FROM backup_sets WHERE id = :set)"
)

log = get_logger("jobhunter.backup.restore_test")


class RestoreTestFailed(BackupJobFailed):
    """The restore-test run `run_id` is `failed` with `error`."""

    def __init__(self, run_id: int, error: str) -> None:
        super().__init__(error)
        self.run_id = run_id


async def _admin(cfg: AppConfig, *statements: sql.Composed) -> None:
    """`statements` as `jobhunter_restore` on the `postgres` database, each in autocommit (a database is
    created and dropped outside a transaction)."""
    dsn = cfg.dsn("psycopg", db_role="restore", database="postgres")
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        for statement in statements:
            await conn.execute(statement)


def _drop() -> sql.Composed:
    return sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(RESTORE_TEST_DB))


async def _scratch_database(cfg: AppConfig) -> None:
    create = sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
        sql.Identifier(RESTORE_TEST_DB), sql.Identifier(TEMPLATE_DB)
    )
    await _admin(cfg, _drop(), create)


def _scratch_directory(scratch: Path) -> dict[str, Path]:
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(mode=0o700)
    dirs = {name: scratch / name for name in SCRATCH_DIRS}
    for directory in dirs.values():
        directory.mkdir(mode=0o700)
    return dirs


async def _steps(
    cfg: AppConfig,
    restic: Restic,
    runner: CommandRunner,
    engine: AsyncEngine,
    run_id: int,
    report: dict[str, JSONValue],
    *,
    now: datetime,
) -> None:
    """The restore test's steps (module docstring), each recording what it found in `report`."""
    # Imported here: `migrate` loads procrastinate, langgraph and alembic, which only this step needs.
    from jobhunter.db.migrate import run_migrate_async

    with restore_step("scratch"):
        await _scratch_database(cfg)
        dirs = _scratch_directory(cfg.staging_dir / RESTORE_TEST_STAGING)
    with restore_step("stream"):
        stream = await restore_stream(
            restic,
            subfolder=cfg.manifests_dir,
            target=dirs["manifests"],
            scratch=cfg.staging_dir / RESTORE_TEST_STAGING / STREAM_SCRATCH,
        )
        report["stream"] = (
            None if stream is None else {"snapshots": len(stream.snapshot_ids), "manifests": stream.manifests}
        )
    with restore_step("set"):
        staged = await restore_set(
            restic, set_id=None, subfolder=cfg.staging_dir / SET_STAGING, target=dirs["set"]
        )
        report.update(set_id=staged.set_id, restic_snapshot_id=staged.snapshot.id)
        async with engine.begin() as conn:
            await conn.execute(_NAME_SET, {"set": staged.set_id, "run": run_id})
    with restore_step("cv"):
        report["cv_files"] = await restore_cv_files(
            restic, staged, subfolder=cfg.cv_store_dir, target=dirs["cv"]
        )
    with restore_step("pg_restore"):
        await load_dump(cfg, runner, staged, db_role="restore", database=RESTORE_TEST_DB)
    scratch = null_engine(cfg, db_role="restore", database=RESTORE_TEST_DB)
    try:
        with restore_step("verify_restored"):
            restored = await check_restored(scratch, staged, empty_stream=stream is None)
            report["restored"] = {"ok": restored.ok, "failures": list(restored.failures)}
            if not restored.ok:
                raise restored_failure(restored)
        with restore_step("migrate"):
            ran = await run_migrate_async(
                cfg,
                now=now,
                database=RESTORE_TEST_DB,
                steps=frozenset({"sequences", "alembic", "replay"}),
                manifest_dir=dirs["manifests"],
                cv_dir=dirs["cv"],
                exports_dir=dirs["exports"],
                db_role="restore",
            )
            report["migrate"] = list(ran)
        with restore_step("exports"):
            ended = await end_restored_exports(scratch, dirs["exports"])
            report["exports"] = {
                "expired": ended.expired,
                "failed": ended.failed,
                "files_removed": ended.files_removed,
            }
        with restore_step("verify_replayed"):
            async with scratch.connect() as conn:
                files = FileRoots(cv_store=dirs["cv"], exports=dirs["exports"])
                replayed = await verify_replayed(conn, dirs["manifests"], files=files)
            report["replayed"] = {"ok": replayed.ok, "failures": list(replayed.failures)}
            if not replayed.ok:
                raise RestoreFailed("verify_replayed", VERIFY_FAILED)
    finally:
        await scratch.dispose()


async def _clean_up(cfg: AppConfig) -> None:
    """The scratch database dropped and the scratch directory removed, after a failure too; a cleanup that
    fails is logged, and the next restore test's start drops what it left."""
    try:
        await _admin(cfg, _drop())
    except psycopg.Error as exc:
        log.warning("restore_test_scratch_not_dropped", error_class=type(exc).__name__)
    shutil.rmtree(cfg.staging_dir / RESTORE_TEST_STAGING, ignore_errors=True)


async def _end(engine: AsyncEngine, run_id: int, report: dict[str, JSONValue], *, error: str | None) -> None:
    async with engine.begin() as conn:
        if error is None:
            await transition(conn, "backup_runs", run_id, ["running"], "succeeded", set_={"report": report})
        else:
            await transition(
                conn, "backup_runs", run_id, ["running"], "failed", reason=error, set_={"report": report}
            )


async def run_restore_test(
    cfg: AppConfig,
    restic: Restic,
    runner: CommandRunner,
    engine: AsyncEngine,
    *,
    clock: Callable[[], datetime],
) -> int:
    """The monthly restore test (module docstring) with the backup role's configuration and `engine`; the
    run's id. `RestoreTestFailed` once the run is `failed`, `BackupJobBusy` (nothing written) while another
    backup job runs."""
    async with backup_job_lock(cfg, "restore_test"):
        async with engine.begin() as conn:
            run_id = int((await conn.execute(_START, {"at": clock()})).scalar_one())
        log.info("restore_test_started", run_id=run_id)
        report: dict[str, JSONValue] = {}
        error: str | None = None
        failed_at: RestoreStep | None = None
        cause: Exception | None = None
        try:
            try:
                await _steps(cfg, restic, runner, engine, run_id, report, now=clock())
            finally:
                await _clean_up(cfg)
        except RestoreFailed as failed:
            error, failed_at, cause = f"{failed.step}:{failed.error}", failed.step, failed
        except Exception as exc:  # noqa: BLE001 - outside every step: recorded `unexpected:<class>`, re-raised
            error, cause = f"{UNEXPECTED}:{type(exc).__name__}", exc
        await _end(engine, run_id, report, error=error)
        if error is not None:
            log.error("restore_test_failed", run_id=run_id, step=failed_at, error=error)
            raise RestoreTestFailed(run_id, error) from cause
        log.info("restore_test_succeeded", run_id=run_id, set_id=report.get("set_id"))
        return run_id
