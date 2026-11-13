"""The `backup` commands of the backup container and `restore run` of the restore container (spec §19.5; plan
M5 Tasks 10, 24 and 26, Deviations 28-30 and 46).

`cli.main` parses them and imports this module only when one runs. Each starts as the `backup` role
(`startup_or_exit("backup")`: its secrets, and both S3 keys for an `s3:` repository), logs to stderr and
prints one JSON line on stdout:

- `jobhunter backup init` initialises the restic repository when restic reports it absent
  (`Restic.init_if_absent`, never an existing one) and prints `{"created": true|false}` (exit 0). The runbook
  runs it once, before the first backup; no job runs it on its own.
- `jobhunter backup ship` ships the deletion manifests (`shipper.ship_manifests`) and prints `{"shipped",
  "snapshot_id", "files"}` (exit 0).
- `jobhunter backup set` takes one backup set (`backup_set.run_backup_set`) and prints `{"set_id"}` (exit 0).
- `jobhunter backup prune` forgets the sets beyond the counts kept (`prune.run_prune`) and prints `{"run_id"}`
  (exit 0).
- `jobhunter backup restore-test` runs the monthly restore test (`restore_test.run_restore_test`) and prints
  `{"run_id"}` (exit 0).
- `jobhunter backup scheduler`, the backup service's command (Deviation 29), first ends the rows a dead
  container left `running` (`scheduler.fail_interrupted`, once the database answers), then runs the jobs of
  `SCHEDULED_JOBS` on their crons (`scheduler.run_scheduler`) until SIGTERM or SIGINT, and prints
  `{"stopped": true}` (exit 0). A job's failure is logged and the scheduler goes on.

A failure prints `{"error": "<code>"}` (exit 1): `not_configured` without `RESTIC_REPOSITORY`, a set's,
a prune's or a restore test's `<step>:<error class or code>` with its `set_id` or `run_id`, `backup_job_busy`
while another backup job runs (nothing was written; Ruling T26-1 (a) T24-4 M1), `ship_lock_lost` when the
shipment lock's session ended during a ship (`shipper.ShipLockLost`: nothing was written, and the next ship
repeats it; the final re-review's M4-1), a `ResticError` code (`backup.restic`), `database` for a database
error (SQLAlchemy's, or psycopg's on a lock's own connection), else `unexpected` with the error's class
(never its message).

`jobhunter restore run [--set <id>]` (`run_restore_command`), the `restore` service's command, starts as the
`restore` role (the migrator's login, restic) and runs `restore.run_restore`: it prints `{"restored": true,
"set_id", "data_key_ids"}` (exit 0), the key ids in hex only, for the operator's `crypto rotate` passes; or
`{"error", "step"}` with the step that stopped it (exit 1). `jobhunter restore verify --set <id> --phase
restored|replayed [--database <db>]` (`run_restore_verify_command`, review M4) starts as the same role and
runs `restore.restore_verify`: it prints the phase's report `{"phase", "ok", "failures"}` (exit 0 when it
holds, else 1), or `{"error"}` for a phase that cannot run (exit 1).
"""

import argparse
import asyncio
import json
import signal
import sys
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import asdict
from types import MappingProxyType
from typing import Any, Final

import psycopg
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from jobhunter.backup.backup_set import BackupJobBusy, BackupSetFailed, run_backup_set
from jobhunter.backup.prune import PruneFailed, run_prune
from jobhunter.backup.restic import Restic, ResticError
from jobhunter.backup.restore import RestoreFailed, restore_verify, run_restore
from jobhunter.backup.restore_test import RestoreTestFailed, run_restore_test
from jobhunter.backup.runner import CommandRunner, SubprocessRunner
from jobhunter.backup.scheduler import BackupJob, JobName, fail_interrupted, run_scheduler, wait_for_database
from jobhunter.backup.shipper import ShipLockLost, ship_manifests
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig, startup_or_exit
from jobhunter.observability.logging import configure_logging
from jobhunter.types import JSONValue

Printed = dict[str, JSONValue]
_Job = Callable[[AppConfig, Restic, CommandRunner, AsyncEngine], Awaitable[object]]


def _engine(cfg: AppConfig) -> AsyncEngine:
    return create_async_engine(cfg.dsn("sqlalchemy"), poolclass=NullPool, hide_parameters=True)


async def _set_job(cfg: AppConfig, restic: Restic, runner: CommandRunner, engine: AsyncEngine) -> int:
    return await run_backup_set(cfg, restic, runner, clock=utcnow)


async def _ship_job(cfg: AppConfig, restic: Restic, runner: CommandRunner, engine: AsyncEngine) -> object:
    return await ship_manifests(cfg, restic, engine, clock=utcnow)


async def _prune_job(cfg: AppConfig, restic: Restic, runner: CommandRunner, engine: AsyncEngine) -> int:
    return await run_prune(cfg, restic, engine, clock=utcnow)


async def _restore_test_job(
    cfg: AppConfig, restic: Restic, runner: CommandRunner, engine: AsyncEngine
) -> int:
    return await run_restore_test(cfg, restic, runner, engine, clock=utcnow)


# The scheduler's job table: what `jobhunter backup scheduler` runs (Ruling T24-2 (h); the restore test since
# Task 26, on the first Sunday of each month).
SCHEDULED_JOBS: Final[Mapping[JobName, _Job]] = MappingProxyType(
    {"set": _set_job, "ship": _ship_job, "prune": _prune_job, "restore_test": _restore_test_job}
)


async def _ship(cfg: AppConfig, restic: Restic) -> Printed:
    engine = _engine(cfg)
    try:
        result = await ship_manifests(cfg, restic, engine, clock=utcnow)
    finally:
        await engine.dispose()
    return dict(asdict(result))


async def _prune(cfg: AppConfig, restic: Restic) -> Printed:
    engine = _engine(cfg)
    try:
        return {"run_id": await run_prune(cfg, restic, engine, clock=utcnow)}
    finally:
        await engine.dispose()


async def _restore_test(cfg: AppConfig, restic: Restic, runner: CommandRunner) -> Printed:
    engine = _engine(cfg)
    try:
        return {"run_id": await run_restore_test(cfg, restic, runner, engine, clock=utcnow)}
    finally:
        await engine.dispose()


async def _scheduler(cfg: AppConfig, restic: Restic, runner: CommandRunner) -> Printed:
    """The scheduler until SIGTERM or SIGINT (module docstring)."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGTERM, signal.SIGINT)
    for signum in signals:
        loop.add_signal_handler(signum, stop.set)
    engine = _engine(cfg)

    async def run_job(job: BackupJob) -> None:
        await SCHEDULED_JOBS[job.name](cfg, restic, runner, engine)

    try:
        started = await wait_for_database(
            lambda: fail_interrupted(cfg, engine), clock=utcnow, sleep=asyncio.sleep, stop=stop
        )
        if started is not None:  # `None`: stopped while the database was not there yet
            await run_scheduler(
                cfg,
                clock=utcnow,
                sleep=asyncio.sleep,
                run_job=run_job,
                stop=stop,
                names=tuple(SCHEDULED_JOBS),
            )
    finally:
        await engine.dispose()
        for signum in signals:
            loop.remove_signal_handler(signum)
    return {"stopped": True}


async def _run(cfg: AppConfig, args: argparse.Namespace) -> tuple[Printed, bool]:
    """The command of `args`: what it prints and whether it succeeded."""
    if cfg.restic is None:
        return {"error": "not_configured"}, False
    runner = SubprocessRunner()
    restic = Restic(cfg, runner)
    command = args.backup_command
    try:
        if command == "init":
            return {"created": await restic.init_if_absent()}, True
        if command == "ship":
            return await _ship(cfg, restic), True
        if command == "set":
            return {"set_id": await run_backup_set(cfg, restic, runner, clock=utcnow)}, True
        if command == "prune":
            return await _prune(cfg, restic), True
        if command == "restore-test":
            return await _restore_test(cfg, restic, runner), True
        return await _scheduler(cfg, restic, runner), True
    except BackupJobBusy as busy:
        return {"error": busy.error}, False
    except BackupSetFailed as failed:
        return {"error": failed.error, "set_id": failed.set_id}, False
    except (PruneFailed, RestoreTestFailed) as failed:
        return {"error": failed.error, "run_id": failed.run_id}, False
    except ShipLockLost:
        return {"error": "ship_lock_lost"}, False
    except ResticError as failed:
        return {"error": failed.code}, False
    except (SQLAlchemyError, psycopg.Error):
        return {"error": "database"}, False


def _outcome(cfg: AppConfig, run: Callable[[], Coroutine[Any, Any, tuple[Printed, bool]]]) -> int:
    """`run` with the role's logging on stderr; its one JSON line printed, and its exit status."""
    configure_logging(cfg.log_level, stream=sys.stderr)
    try:
        printed, ok = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - the printed code is the outcome; never a message or traceback
        printed, ok = {"error": "unexpected", "error_class": type(exc).__name__}, False
    print(json.dumps(printed))
    return 0 if ok else 1


def run_backup_command(args: argparse.Namespace) -> int:
    """`backup init | ship | set | prune | restore-test | scheduler`; the exit status is the outcome (module
    docstring)."""
    cfg = startup_or_exit("backup")
    return _outcome(cfg, lambda: _run(cfg, args))


async def _restore_run(cfg: AppConfig, set_id: int | None) -> tuple[Printed, bool]:
    if cfg.restic is None:
        return {"error": "not_configured"}, False
    runner = SubprocessRunner()
    try:
        result = await run_restore(cfg, Restic(cfg, runner), runner, set_id=set_id, clock=utcnow)
    except RestoreFailed as failed:
        return {"error": failed.error, "step": failed.step}, False
    return {"restored": True, "set_id": result.set_id, "data_key_ids": list(result.data_key_ids)}, True


def run_restore_command(args: argparse.Namespace) -> int:
    """`restore run [--set <id>]` as the `restore` role (module docstring); the exit status is the outcome."""
    cfg = startup_or_exit("restore")
    return _outcome(cfg, lambda: _restore_run(cfg, args.set))


async def _restore_verify(cfg: AppConfig, args: argparse.Namespace) -> tuple[Printed, bool]:
    restic = None if cfg.restic is None else Restic(cfg, SubprocessRunner())
    try:
        report = await restore_verify(cfg, restic, set_id=args.set, phase=args.phase, database=args.database)
    except RestoreFailed as failed:
        return {"error": failed.error}, False
    failures: list[JSONValue] = [*report.failures]
    return {"phase": report.phase, "ok": report.ok, "failures": failures}, report.ok


def run_restore_verify_command(args: argparse.Namespace) -> int:
    """`restore verify --set <id> --phase restored|replayed [--database <db>]` as the `restore` role (module
    docstring); the exit status is the outcome."""
    cfg = startup_or_exit("restore")
    return _outcome(cfg, lambda: _restore_verify(cfg, args))
