"""The `backup` commands of the backup container (spec §19.5; plan M5 Task 10, Deviations 28-30).

`cli.main` parses them and imports this module only when one runs. Each starts as the `backup` role
(`startup_or_exit("backup")`: its secrets, and both S3 keys for an `s3:` repository), logs to stderr and
prints one JSON line on stdout:

- `jobhunter backup init` initialises the restic repository when restic reports it absent
  (`Restic.init_if_absent`, never an existing one) and prints `{"created": true|false}` (exit 0). The runbook
  runs it once, before the first backup; no job runs it on its own.
- `jobhunter backup ship` ships the deletion manifests (`shipper.ship_manifests`) and prints `{"shipped",
  "snapshot_id", "files"}` (exit 0).

A failure prints `{"error": "<code>"}` (exit 1): `not_configured` without `RESTIC_REPOSITORY`, a
`ResticError` code (`backup.restic`), `database` for a database error, else `unexpected` with the error's
class (never its message).
"""

import argparse
import asyncio
import json
import sys
from dataclasses import asdict

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from jobhunter.backup.restic import Restic, ResticError
from jobhunter.backup.runner import SubprocessRunner
from jobhunter.backup.shipper import ship_manifests
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig, startup_or_exit
from jobhunter.observability.logging import configure_logging
from jobhunter.types import JSONValue

Printed = dict[str, JSONValue]


async def _ship(cfg: AppConfig, restic: Restic) -> Printed:
    engine = create_async_engine(cfg.dsn("sqlalchemy"), poolclass=NullPool, hide_parameters=True)
    try:
        result = await ship_manifests(cfg, restic, engine, now=utcnow())
    finally:
        await engine.dispose()
    return dict(asdict(result))


async def _run(cfg: AppConfig, args: argparse.Namespace) -> tuple[Printed, bool]:
    """The command of `args`: what it prints and whether it succeeded."""
    if cfg.restic is None:
        return {"error": "not_configured"}, False
    restic = Restic(cfg, SubprocessRunner())
    try:
        if args.backup_command == "init":
            return {"created": await restic.init_if_absent()}, True
        return await _ship(cfg, restic), True
    except ResticError as failed:
        return {"error": failed.code}, False
    except SQLAlchemyError:
        return {"error": "database"}, False


def run_backup_command(args: argparse.Namespace) -> int:
    """`backup init | ship`; the exit status is the outcome (module docstring)."""
    cfg = startup_or_exit("backup")
    configure_logging(cfg.log_level, stream=sys.stderr)
    try:
        printed, ok = asyncio.run(_run(cfg, args))
    except Exception as exc:  # noqa: BLE001 - the printed code is the outcome; never a message or traceback
        printed, ok = {"error": "unexpected", "error_class": type(exc).__name__}, False
    print(json.dumps(printed))
    return 0 if ok else 1
