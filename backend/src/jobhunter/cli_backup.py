"""The `backup` commands of the backup container (spec §19.5; plan M5 Tasks 10 and 24, Deviations 28-30).

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
- `jobhunter backup scheduler`, the backup service's command (Deviation 29), first ends the rows a dead
  container left `running` (`scheduler.fail_interrupted`, once the database answers), then runs the jobs of
  `SCHEDULED_JOBS` on their crons (`scheduler.run_scheduler`) until SIGTERM or SIGINT, and prints
  `{"stopped": true}` (exit 0). A job's failure is logged and the scheduler goes on.

A failure prints `{"error": "<code>"}` (exit 1): `not_configured` without `RESTIC_REPOSITORY`, a set's or a
prune's `<step>:<error class>` with its `set_id` or `run_id`, a `ResticError` code (`backup.restic`),
`database` for a database error, else `unexpected` with the error's class (never its message).
"""

import argparse
import asyncio
import json
import signal
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict
from types import MappingProxyType
from typing import Final

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from jobhunter.backup.backup_set import BackupSetFailed, run_backup_set
from jobhunter.backup.prune import PruneFailed, run_prune
from jobhunter.backup.restic import Restic, ResticError
from jobhunter.backup.runner import CommandRunner, SubprocessRunner
from jobhunter.backup.scheduler import BackupJob, JobName, fail_interrupted, run_scheduler, wait_for_database
from jobhunter.backup.shipper import ship_manifests
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
    return await ship_manifests(cfg, restic, engine, now=utcnow())


async def _prune_job(cfg: AppConfig, restic: Restic, runner: CommandRunner, engine: AsyncEngine) -> int:
    return await run_prune(cfg, restic, engine, clock=utcnow)


# The scheduler's job table: what `jobhunter backup scheduler` runs (Ruling T24-2 (h); the restore test joins
# with its own command).
SCHEDULED_JOBS: Final[Mapping[JobName, _Job]] = MappingProxyType(
    {"set": _set_job, "ship": _ship_job, "prune": _prune_job}
)


async def _ship(cfg: AppConfig, restic: Restic) -> Printed:
    engine = _engine(cfg)
    try:
        result = await ship_manifests(cfg, restic, engine, now=utcnow())
    finally:
        await engine.dispose()
    return dict(asdict(result))


async def _prune(cfg: AppConfig, restic: Restic) -> Printed:
    engine = _engine(cfg)
    try:
        return {"run_id": await run_prune(cfg, restic, engine, clock=utcnow)}
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
        return await _scheduler(cfg, restic, runner), True
    except BackupSetFailed as failed:
        return {"error": failed.error, "set_id": failed.set_id}, False
    except PruneFailed as failed:
        return {"error": failed.error, "run_id": failed.run_id}, False
    except ResticError as failed:
        return {"error": failed.code}, False
    except SQLAlchemyError:
        return {"error": "database"}, False


def run_backup_command(args: argparse.Namespace) -> int:
    """`backup init | ship | set | prune | scheduler`; the exit status is the outcome (module docstring)."""
    cfg = startup_or_exit("backup")
    configure_logging(cfg.log_level, stream=sys.stderr)
    try:
        printed, ok = asyncio.run(_run(cfg, args))
    except Exception as exc:  # noqa: BLE001 - the printed code is the outcome; never a message or traceback
        printed, ok = {"error": "unexpected", "error_class": type(exc).__name__}, False
    print(json.dumps(printed))
    return 0 if ok else 1
