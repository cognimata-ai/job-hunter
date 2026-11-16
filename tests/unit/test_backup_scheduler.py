"""The backup container's scheduler (spec §19.2, §19.5; plan M5 Task 24, Deviation 29, R15, Rulings T24-1
(a) and T24-2 (h)): `next_due` on each job's cron (UTC), the restore test on the first Sunday only, one job
at a time, a late ship skipped rather than queued, a stop between jobs, and, at the start, the rows a dead
container left `running` ended `failed` (`interrupted`) with their staging cleared, once the database
answers.

The scheduler's own tests run on a fake clock: `sleep` advances it, and a job spends time by advancing it too,
so nothing waits for real. The start's cleanup runs on the shared database, as the backup role.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest
import sqlalchemy as sa
from jobhunter.backup.backup_set import BackupSetFailed
from jobhunter.backup.restore_test import RESTORE_TEST_DB
from jobhunter.backup.scheduler import (
    JOB_NAMES,
    BackupJob,
    Interrupted,
    backup_jobs,
    fail_interrupted,
    next_due,
    run_scheduler,
    wait_for_database,
)
from jobhunter.cli_backup import SCHEDULED_JOBS
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig, BackupSettings, StartupCheckFailed, load_config
from jobhunter.db.session import make_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from tests.support.backups import backup_run, backup_set
from tests.support.logs import logged
from tests.support.pg import PgServer, config_for, pg_one, pg_rows

ENV: Final = {
    "JOBHUNTER_ENV": "test",
    "JOBHUNTER_DB_HOST": "h",
    "JOBHUNTER_DB_PORT": "5432",
    "JOBHUNTER_DB_NAME": "jobhunter",
}
SETTINGS: Final = (
    "JOBHUNTER_BACKUP_CRON",
    "JOBHUNTER_MANIFEST_SHIP_CRON",
    "JOBHUNTER_BACKUP_PRUNE_CRON",
    "JOBHUNTER_RESTORE_TEST_CRON",
    "JOBHUNTER_BACKUP_KEEP_DAILY",
    "JOBHUNTER_BACKUP_KEEP_WEEKLY",
    "JOBHUNTER_BACKUP_KEEP_MONTHLY",
)
DEFAULTS: Final = BackupSettings(
    set_cron="15 1 * * *",
    ship_cron="*/5 * * * *",
    prune_cron="30 2 * * 0",
    restore_test_cron="30 3 * * 0",
    keep_daily=7,
    keep_weekly=4,
    keep_monthly=3,
)
_SETS: Final = sa.text("SELECT id, status, error, finished_at IS NOT NULL AS finished FROM backup_sets")
_RUNS: Final = sa.text("SELECT id, status, error, finished_at IS NOT NULL AS finished FROM backup_runs")


def at(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def backup_config(tmp_path: Path, **env: str) -> AppConfig:
    return load_config("backup", {**ENV, **env}, tmp_path)


class FakeTime:
    """The scheduler's clock and sleep: a sleep advances the clock at once; a job spends time the same way."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def clock(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        await asyncio.sleep(0)

    async def spend(self, seconds: float) -> None:
        for _ in range(3):  # other tasks get their turn while the job runs
            await asyncio.sleep(0)
        self.now += timedelta(seconds=seconds)


def test_next_due_per_cron(tmp_path: Path) -> None:
    cfg = backup_config(tmp_path)
    assert cfg.backup == DEFAULTS
    jobs = {job.name: job for job in backup_jobs(cfg)}
    assert list(jobs) == ["set", "ship", "prune", "restore_test"]
    wednesday = at(2026, 11, 11, 1, 12)
    set_, ship, prune, restore_test = jobs.values()
    assert next_due([set_], now=wednesday) == (set_, at(2026, 11, 11, 1, 15))
    assert next_due([set_], now=at(2026, 11, 11, 1, 15)) == (set_, at(2026, 11, 12, 1, 15))  # strictly after
    assert next_due([ship], now=wednesday + timedelta(seconds=30)) == (ship, at(2026, 11, 11, 1, 15))
    assert next_due([prune], now=wednesday) == (prune, at(2026, 11, 15, 2, 30))  # the Sunday
    assert next_due([restore_test], now=wednesday) == (restore_test, at(2026, 12, 6, 3, 30))  # first Sunday
    # Of several jobs, the one due first; at 01:15 the set and the ship tie, and the table's order decides.
    every = list(jobs.values())
    assert next_due(every, now=wednesday) == (set_, at(2026, 11, 11, 1, 15))
    assert next_due(every, now=at(2026, 11, 11, 1, 15)) == (ship, at(2026, 11, 11, 1, 20))
    assert next_due([prune, restore_test], now=at(2026, 11, 29, 2, 31)) == (prune, at(2026, 12, 6, 2, 30))
    # A setting replaces its default.
    custom = backup_config(
        tmp_path, JOBHUNTER_BACKUP_CRON="0 4 * * *", JOBHUNTER_BACKUP_PRUNE_CRON="45 5 * * 3"
    )
    by_name = {job.name: job for job in backup_jobs(custom)}
    assert next_due([by_name["set"]], now=wednesday) == (by_name["set"], at(2026, 11, 11, 4, 0))
    assert next_due([by_name["prune"]], now=wednesday) == (by_name["prune"], at(2026, 11, 11, 5, 45))
    assert backup_jobs(custom, ("prune", "set")) == (by_name["prune"], by_name["set"])


def test_empty_settings_take_the_defaults_and_bad_ones_are_refused(tmp_path: Path) -> None:
    """T24-1 (a): compose passes an optional variable as `${NAME:-}`, so an empty value is unset."""
    assert backup_config(tmp_path, **dict.fromkeys(SETTINGS, "")).backup == DEFAULTS
    refused = (
        ("JOBHUNTER_BACKUP_CRON", "daily"),
        ("JOBHUNTER_MANIFEST_SHIP_CRON", "*/5 * * *"),  # four fields
        ("JOBHUNTER_BACKUP_PRUNE_CRON", "30 2 * * 0 2026"),  # six: croniter would read seconds
        ("JOBHUNTER_RESTORE_TEST_CRON", "61 3 * * 0"),
        ("JOBHUNTER_BACKUP_KEEP_DAILY", "0"),  # a prune never forgets every set of a period
        ("JOBHUNTER_BACKUP_KEEP_WEEKLY", "-1"),
        ("JOBHUNTER_BACKUP_KEEP_MONTHLY", "three"),
    )
    for name, value in refused:
        with pytest.raises(StartupCheckFailed) as failed:
            backup_config(tmp_path, **{name: value})
        assert failed.value.check == "env_valid", name
        assert failed.value.detail.startswith(name), name
    kept = backup_config(
        tmp_path,
        JOBHUNTER_BACKUP_KEEP_DAILY="14",
        JOBHUNTER_BACKUP_KEEP_WEEKLY="8",
        JOBHUNTER_BACKUP_KEEP_MONTHLY="12",
    ).backup
    assert (kept.keep_daily, kept.keep_weekly, kept.keep_monthly) == (14, 8, 12)


def test_env_valid_and_the_scheduler_share_one_cron_validator(tmp_path: Path) -> None:
    """Ruling T26-1 (a) T24-4 M7: a cron the configuration accepts is one the scheduler can schedule, and one
    it refuses is refused at the container's start (`env_valid`) for the same reason: six fields (croniter
    would read seconds), and for the restore test a cron with no tick in a month's first seven days (the
    scheduler would otherwise stop at its first job). The same monthly cron is fine for a job without the
    first-week rule. The scheduler's table runs every job, the restore test included."""
    refusals = (
        ("JOBHUNTER_RESTORE_TEST_CRON", "30 3 15 * *", "must fire in the first seven days of a month"),
        ("JOBHUNTER_RESTORE_TEST_CRON", "0 0 29 2 *", "must fire in the first seven days of a month"),
        ("JOBHUNTER_BACKUP_CRON", "0 * * * * *", "must be a cron expression of five fields"),
    )
    for name, cron, reason in refusals:
        with pytest.raises(StartupCheckFailed) as failed:
            backup_config(tmp_path, **{name: cron})
        assert (failed.value.check, failed.value.detail) == ("env_valid", f"{name} {reason}")
        job = BackupJob("restore_test" if name == "JOBHUNTER_RESTORE_TEST_CRON" else "set", cron)
        with pytest.raises(ValueError, match=reason):
            next_due([job], now=at(2026, 11, 23))
    accepted = backup_config(
        tmp_path, JOBHUNTER_BACKUP_PRUNE_CRON="30 3 15 * *", JOBHUNTER_RESTORE_TEST_CRON="0 0 1 1 *"
    )
    prune, restore_test = backup_jobs(accepted, ("prune", "restore_test"))
    assert next_due([prune], now=at(2026, 11, 23)) == (prune, at(2026, 12, 15, 3, 30))
    assert next_due([restore_test], now=at(2026, 11, 23)) == (restore_test, at(2027, 1, 1))
    assert tuple(SCHEDULED_JOBS) == JOB_NAMES


async def rows(engine: AsyncEngine, statement: sa.TextClause) -> dict[int, tuple[str, str | None, bool]]:
    async with engine.connect() as conn:
        return {row.id: (row.status, row.error, row.finished) for row in await conn.execute(statement)}


async def test_rows_left_running_by_a_dead_container_fail_at_start(
    migrated: PgServer, clean_db: None, engine: AsyncEngine, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    cfg = config_for(migrated, "backup", JOBHUNTER_STAGING_DIR=str(staging))
    succeeded = await backup_set(engine, "succeeded", utcnow() - timedelta(days=1))
    left_set = await backup_set(engine, "running")
    left_prune = await backup_run(engine, "running", kind="prune")
    left_test = await backup_run(engine, "running", set_id=succeeded)
    failed_prune = await backup_run(engine, "failed", utcnow() - timedelta(days=2), kind="prune")
    (staging / "set").mkdir(mode=0o700)  # the dead set's staging
    (staging / "set" / "jobhunter.dump").write_bytes(b"half a dump")
    cache = staging / ".restic-cache"
    cache.mkdir(mode=0o700)
    (cache / "index").write_bytes(b"restic's cache")

    backup_engine = make_engine(cfg, pool_size=1)
    try:
        interrupted = await fail_interrupted(cfg, backup_engine)
        again = await fail_interrupted(cfg, backup_engine)  # a second start finds nothing left
    finally:
        await backup_engine.dispose()

    assert interrupted == Interrupted(sets=(left_set,), runs=(left_prune, left_test))
    assert again == Interrupted(sets=(), runs=())
    assert await rows(engine, _SETS) == {
        succeeded: ("succeeded", None, True),
        left_set: ("failed", "interrupted", True),
    }
    assert await rows(engine, _RUNS) == {
        left_prune: ("failed", "interrupted", True),
        left_test: ("failed", "interrupted", True),
        failed_prune: ("failed", "verify_failed", True),
    }
    assert sorted(path.name for path in staging.iterdir()) == [".restic-cache"]  # the set's staging cleared
    assert (cache / "index").read_bytes() == b"restic's cache"  # the cache kept


_SCRATCH_DB: Final = f"SELECT count(*) FROM pg_database WHERE datname = '{RESTORE_TEST_DB}'"  # noqa: S608


async def test_an_interrupted_restore_tests_leftovers_are_removed_at_the_next_start(
    migrated: PgServer, clean_db: None, engine: AsyncEngine, tmp_path: Path
) -> None:
    """Final review I1: a restore test killed mid-run (a restart past the stop grace, an OOM) leaves the
    scratch database `jobhunter_restore_test`, with every row, and `<staging>/restore_test` with its
    `jobhunter.dump`; a killed `restore run` leaves `<staging>/restore` with its own. The next start drops the
    database and removes both directories, logging their names only; restic's cache stays, and a second start
    finds and logs nothing."""
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    cfg = config_for(migrated, "backup", JOBHUNTER_STAGING_DIR=str(staging))
    pg_rows(
        migrated, "restore", f"CREATE DATABASE {RESTORE_TEST_DB} TEMPLATE jobhunter_template", db="postgres"
    )
    pg_rows(migrated, "restore", "CREATE TABLE copied (x int)", db=RESTORE_TEST_DB)
    for leftover in ("restore_test/set", "restore"):
        (staging / leftover).mkdir(mode=0o700, parents=True)
        (staging / leftover / "jobhunter.dump").write_bytes(b"a full copy of the database")
    (staging / "restore_test" / "manifests").mkdir(mode=0o700)
    cache = staging / ".restic-cache"
    cache.mkdir(mode=0o700)
    (cache / "index").write_bytes(b"restic's cache")
    run = await backup_run(engine, "running")

    backup_engine = make_engine(cfg, pool_size=1)
    try:
        with capture_logs() as events:
            interrupted = await fail_interrupted(cfg, backup_engine)
            again = await fail_interrupted(cfg, backup_engine)
    finally:
        await backup_engine.dispose()

    assert interrupted.runs == (run,) and again == Interrupted(sets=(), runs=())
    assert pg_one(migrated, "superuser", _SCRATCH_DB, db="postgres") == (0,)
    assert sorted(path.name for path in staging.iterdir()) == [".restic-cache"]
    assert (cache / "index").read_bytes() == b"restic's cache"
    removed = [e for e in events if e["event"] == "restore_leftovers_removed"]
    assert [(e["database"], e["staging"]) for e in removed] == [
        (["jobhunter_restore_test"], ["restore_test", "restore"])
    ]


async def test_the_start_waits_for_the_database() -> None:
    """The backup service starts with the stack, not after `migrate` (compose.yaml): the start's cleanup is
    tried again every 5 s while the database refuses it, for up to 300 s; a stop ends the wait."""
    start = at(2026, 11, 11, 1, 0)
    time = FakeTime(start)
    tries: list[datetime] = []

    async def cleanup() -> str:
        tries.append(time.now)
        if len(tries) < 3:
            raise OperationalError("SELECT 1", {}, ConnectionRefusedError())
        return "cleaned"

    assert (
        await wait_for_database(cleanup, clock=time.clock, sleep=time.sleep, stop=asyncio.Event())
        == "cleaned"
    )
    assert tries == [start, start + timedelta(seconds=5), start + timedelta(seconds=10)]

    async def refused() -> str:
        tries.append(time.now)
        raise OperationalError("SELECT 1", {}, ConnectionRefusedError())

    tries.clear()
    time.now = start
    with pytest.raises(OperationalError):
        await wait_for_database(refused, clock=time.clock, sleep=time.sleep, stop=asyncio.Event())
    assert (len(tries), tries[-1]) == (61, start + timedelta(seconds=300))

    stop = asyncio.Event()

    async def stopped() -> str:
        stop.set()  # a SIGTERM while the database is down
        raise OperationalError("SELECT 1", {}, ConnectionRefusedError())

    assert await wait_for_database(stopped, clock=time.clock, sleep=time.sleep, stop=stop) is None


async def test_the_restore_test_runs_on_the_first_sunday_only(tmp_path: Path) -> None:
    cfg = backup_config(tmp_path)
    time = FakeTime(at(2026, 11, 23))  # a Monday; the cron's Sundays: Nov 29, Dec 6, 13, 20, 27, Jan 3
    stop = asyncio.Event()
    runs: list[tuple[str, datetime]] = []

    async def run_job(job: BackupJob) -> None:
        runs.append((job.name, time.now))
        if len(runs) == 2:
            stop.set()

    await run_scheduler(
        cfg, clock=time.clock, sleep=time.sleep, run_job=run_job, stop=stop, names=("restore_test",)
    )
    assert runs == [("restore_test", at(2026, 12, 6, 3, 30)), ("restore_test", at(2027, 1, 3, 3, 30))]
    job = BackupJob("restore_test", cfg.backup.restore_test_cron)
    assert next_due([job], now=at(2026, 10, 31)) == (job, at(2026, 11, 1, 3, 30))  # a first Sunday on the 1st
    assert next_due([job], now=at(2027, 1, 4)) == (job, at(2027, 2, 7, 3, 30))  # and on the 7th
    # The rule is the restore test's: the same cron for another job fires every Sunday.
    weekly = BackupJob("prune", cfg.backup.restore_test_cron)
    assert next_due([weekly], now=at(2026, 11, 23)) == (weekly, at(2026, 11, 29, 3, 30))
    # A cron that never fires in a month's first seven days is refused rather than searched for ever.
    with pytest.raises(ValueError, match="first seven days"):
        next_due([BackupJob("restore_test", "30 3 15 * *")], now=at(2026, 11, 23))


async def test_one_job_at_a_time(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    cfg = backup_config(tmp_path, JOBHUNTER_BACKUP_PRUNE_CRON="*/10 * * * *")
    time = FakeTime(at(2026, 11, 11, 1, 14))
    stop = asyncio.Event()
    running: list[str] = []
    runs: list[tuple[str, datetime]] = []

    async def run_job(job: BackupJob) -> None:
        assert not running, f"{job.name} started while {running} ran"
        running.append(job.name)
        runs.append((job.name, time.now))
        await time.spend(25 * 60 if job.name == "set" else 60)
        running.remove(job.name)
        if len(runs) == 3:
            stop.set()

    caplog.set_level("INFO")
    await run_scheduler(
        cfg, clock=time.clock, sleep=time.sleep, run_job=run_job, stop=stop, names=("set", "prune")
    )
    # The prune came due at 01:20, 01:30 and 01:40 while the set ran (01:15 to 01:40): it waited, then ran
    # once, not once per tick; its next tick is the first after it started.
    assert runs == [
        ("set", at(2026, 11, 11, 1, 15)),
        ("prune", at(2026, 11, 11, 1, 40)),
        ("prune", at(2026, 11, 11, 1, 50)),
    ]
    assert [(e["job"], e["outcome"], e["duration_s"]) for e in logged(caplog, "backup_job")] == [
        ("set", "succeeded", 1500.0),
        ("prune", "succeeded", 60.0),
        ("prune", "succeeded", 60.0),
    ]


async def test_a_failing_job_is_logged_and_the_next_one_runs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = backup_config(tmp_path)
    time = FakeTime(at(2026, 11, 11, 1, 14))
    stop = asyncio.Event()
    runs: list[tuple[str, datetime]] = []

    async def run_job(job: BackupJob) -> None:
        runs.append((job.name, time.now))
        if job.name == "set":
            await time.spend(30)
            raise BackupSetFailed(7, "dump:CommandFailed")
        stop.set()

    caplog.set_level("INFO")
    await run_scheduler(
        cfg, clock=time.clock, sleep=time.sleep, run_job=run_job, stop=stop, names=("set", "ship")
    )
    # The ship, due at 01:15 too, ran once the failed set had ended.
    assert runs == [
        ("set", at(2026, 11, 11, 1, 15)),
        ("ship", at(2026, 11, 11, 1, 15) + timedelta(seconds=30)),
    ]
    failed, shipped = logged(caplog, "backup_job")
    assert {key: failed[key] for key in ("job", "outcome", "duration_s", "error_class", "error")} == {
        "job": "set",
        "outcome": "failed",
        "duration_s": 30.0,
        "error_class": "BackupSetFailed",
        "error": "dump:CommandFailed",
    }
    assert (shipped["job"], shipped["outcome"]) == ("ship", "succeeded")


async def test_a_late_ship_is_skipped_not_queued(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    cfg = backup_config(tmp_path)  # the set at 01:15, the ship every 5 minutes
    time = FakeTime(at(2026, 11, 11, 1, 14))
    stop = asyncio.Event()
    runs: list[tuple[str, datetime]] = []

    async def run_job(job: BackupJob) -> None:
        runs.append((job.name, time.now))
        if job.name == "set":
            await time.spend(12 * 60)  # 01:15 to 01:27: the ship's ticks of 01:15, 01:20 and 01:25 pass
        if len(runs) == 4:
            stop.set()

    caplog.set_level("INFO")
    await run_scheduler(
        cfg, clock=time.clock, sleep=time.sleep, run_job=run_job, stop=stop, names=("set", "ship")
    )
    # 01:15 and 01:20 were skipped (each one's next tick came before it could run); 01:25's ran late, once.
    assert runs == [
        ("set", at(2026, 11, 11, 1, 15)),
        ("ship", at(2026, 11, 11, 1, 27)),
        ("ship", at(2026, 11, 11, 1, 30)),
        ("ship", at(2026, 11, 11, 1, 35)),
    ]
    assert [(e["job"], e["outcome"]) for e in logged(caplog, "backup_job")] == [
        ("set", "succeeded"),
        ("ship", "skipped"),
        ("ship", "skipped"),
        ("ship", "succeeded"),
        ("ship", "succeeded"),
        ("ship", "succeeded"),
    ]


async def test_a_stop_ends_between_jobs(tmp_path: Path) -> None:
    cfg = backup_config(tmp_path)
    time = FakeTime(at(2026, 11, 11, 1, 14))
    stop = asyncio.Event()
    finished: list[str] = []

    async def run_job(job: BackupJob) -> None:
        stop.set()  # a SIGTERM while the set runs
        await time.spend(60)
        finished.append(job.name)

    await run_scheduler(
        cfg, clock=time.clock, sleep=time.sleep, run_job=run_job, stop=stop, names=("set", "ship")
    )
    # The set ran to its end; the ship, due at 01:15 too, never started.
    assert finished == ["set"]

    waiting = asyncio.Event()

    async def sleep_until_stopped(seconds: float) -> None:
        waiting.set()  # the signal arrives during the wait, which never ends by itself
        await asyncio.Event().wait()

    async def never(job: BackupJob) -> None:
        raise AssertionError(f"{job.name} ran after the stop")

    stop = asyncio.Event()
    scheduler = asyncio.create_task(
        run_scheduler(cfg, clock=time.clock, sleep=sleep_until_stopped, run_job=never, stop=stop)
    )
    await asyncio.wait_for(waiting.wait(), timeout=5)
    stop.set()
    await asyncio.wait_for(scheduler, timeout=5)
