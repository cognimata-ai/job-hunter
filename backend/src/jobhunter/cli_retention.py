"""The `retention` command (spec §19.8 "Disk pressure", §20.1; M5 follow-ups Task F4 (a)):
`jobhunter retention run`, the retention pass now instead of at its next 03:17 UTC.

`cli.main` parses it (its parser needs no import of this module) and imports this module only when it runs. It
starts like the worker (the worker-role config and its startup assertions; the RUNBOOK runs it as
`$JH run --rm --no-deps worker jobhunter retention run`, so it has the worker's CV store and exports volumes),
logs to stderr, and runs `privacy.retention.run_retention_daily`, the pass of the scheduled `retention_daily`,
once, at `utcnow()`:

- **The same fence.** The pass is fenced by the current worker epoch, read from `worker_heartbeat` at its
  start, as the scheduled pass is fenced by its worker's epoch: each rule's transaction, each batch and each
  alert write starts with `epoch.fence_epoch`, which holds the heartbeat row `FOR KEY SHARE`, so no takeover
  commits while it writes. A takeover that commits meanwhile (a worker that starts again) stops it before its
  next write (`FencedOut`), as it stops the superseded worker's pass, and the next pass resumes. Without an
  epoch (no worker ever started on this database) it runs nothing: nothing could fence it.
- **The same rules and locks.** Each rule runs as in the scheduled pass (`privacy.retention`'s docstring): in
  a transaction of its own or in bounded batches, under the same row locks, `active_config` locks and
  purge-fence admissions, each rule idempotent, a failure reported `-1` and raised as `retention.failed`, a
  success resolving it. So it runs beside a running worker, the worker's own nightly pass included: what both
  find, one deletes and the other finds gone.
- **Only the pass.** The scheduled task runs the `daily` reconcilers after its pass; they stay the worker's.

**The output** is one JSON line on stdout: `{"counts": {<rule>: <count>, …}}` in `RULES`' order, `-1` for a
rule that failed or was skipped, exit 0 when no count is `-1`, else 1 (`$JH logs` names the rule:
`retention_rule_failed`). A pass that did not run, or stopped, prints `{"error": "<code>"}` and exits 1:
`no_worker_epoch`, `fenced_out` (a takeover stopped it; run it again), `database` (no database answered, or it
refused the pass's start), or `unexpected` with `error_class`. Counts and codes only: never a row, a path, a
DSN or an exception's message.
"""

import argparse
import asyncio
import json
import sys
from typing import Final

import psycopg
from procrastinate.exceptions import ConnectorException
from sqlalchemy.exc import SQLAlchemyError

from jobhunter.clock import utcnow
from jobhunter.config import AppConfig, startup_or_exit
from jobhunter.db.session import make_engine
from jobhunter.db.sql import q
from jobhunter.errors import FencedOut
from jobhunter.graphs.checkpointer import open_saver
from jobhunter.observability.logging import configure_logging
from jobhunter.privacy import export
from jobhunter.privacy.retention import run_retention_daily
from jobhunter.profile.storage import CvStore
from jobhunter.types import JSONValue
from jobhunter.worker.tasks import make_queue_app

ENGINE_POOL_SIZE: Final = 3
QUEUE_POOL_MAX_SIZE: Final = 2
# The current worker epoch, unlocked: each fenced transaction of the pass proves it again (`fence_epoch`).
_CURRENT_EPOCH: Final = q("SELECT worker_epoch FROM worker_heartbeat WHERE id = 1")
# What a database that does not answer, or refuses, raises while the pass starts: SQLAlchemy's, psycopg's (the
# saver's pool, its `PoolTimeout` included) and procrastinate's connector's.
_DATABASE_ERRORS: Final = (SQLAlchemyError, psycopg.Error, ConnectorException, OSError)

Printed = dict[str, JSONValue]


async def retention_run(cfg: AppConfig) -> tuple[Printed, bool]:
    """`retention run` (module docstring): what it prints and whether it succeeded."""
    dsn = cfg.dsn("psycopg")
    engine = make_engine(cfg, pool_size=ENGINE_POOL_SIZE)
    try:
        try:
            async with engine.connect() as conn:
                epoch = (await conn.execute(_CURRENT_EPOCH)).scalar_one_or_none()
            if epoch is None:
                return {"error": "no_worker_epoch"}, False
            export.configure(cfg)  # the exports volume the `exports` rule removes archives from
            queue = make_queue_app(dsn, max_size=QUEUE_POOL_MAX_SIZE)
            async with queue.open_async(), open_saver(dsn) as saver:
                report = await run_retention_daily(
                    engine,
                    queue,
                    saver,
                    cv_store=CvStore(cfg.cv_store_dir),
                    now=utcnow(),
                    epoch=int(epoch),
                )
        except FencedOut:
            return {"error": "fenced_out"}, False  # `retention_fenced` logged the rule and the counts so far
        except _DATABASE_ERRORS:
            return {"error": "database"}, False
    finally:
        await engine.dispose()
    counts: dict[str, JSONValue] = dict(report)
    return {"counts": counts}, all(count >= 0 for count in report.values())


def run_retention_command(args: argparse.Namespace) -> int:
    """`retention run`; the exit status is the outcome (module docstring)."""
    cfg = startup_or_exit("worker")
    configure_logging(cfg.log_level, stream=sys.stderr)
    try:
        printed, ok = asyncio.run(retention_run(cfg))
    except Exception as exc:  # noqa: BLE001 - the printed code is the outcome; never a message or traceback
        printed, ok = {"error": "unexpected", "error_class": type(exc).__name__}, False
    print(json.dumps(printed))
    return 0 if ok else 1
