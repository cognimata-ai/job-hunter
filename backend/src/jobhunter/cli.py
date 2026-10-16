"""Command line entry point (`python -m jobhunter.cli`, installed as the `jobhunter` script)."""

import argparse
import sys
from collections.abc import Sequence
from typing import get_args

from jobhunter.config import Role, load_config, startup_or_exit


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobhunter")
    commands = parser.add_subparsers(dest="command", required=True)
    startup_check = commands.add_parser(
        "startup-check", help="run the startup assertions of a role (exit 78 when one fails)"
    )
    startup_check.add_argument("--role", required=True, choices=get_args(Role))
    commands.add_parser("migrate", help="bring the database to the code head and seed the defaults (§19.3)")
    commands.add_parser(
        "worker-health",
        help="exit 0 when the worker heartbeat is at most 60 s old, else 1 (container HEALTHCHECK)",
    )
    return parser


def _worker_health() -> int:
    """The worker's container HEALTHCHECK: one read of `worker_heartbeat` as the application role.

    It runs every few seconds, so it stays quiet: no logging setup and no startup assertions, and a failure
    prints one stderr line that names the error class only (never a DSN, a message or a traceback).
    """
    try:
        # Imported here: only this probe (and the worker) needs psycopg and the worker modules.
        from jobhunter.worker.lock import heartbeat_is_fresh

        fresh = heartbeat_is_fresh(load_config("worker").dsn("psycopg"))
    except Exception as exc:  # noqa: BLE001 - the probe's exit status is its outcome; Docker records it
        print(f"worker-health: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0 if fresh else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "startup-check":
        startup_or_exit(args.role)
        return 0
    if args.command == "migrate":
        # Imported here: migrate pulls in procrastinate, langgraph and alembic, which `startup-check` (the
        # healthcheck path of every container) must not load.
        from jobhunter.db.migrate import run_migrate

        run_migrate(startup_or_exit("migrate"))
        return 0
    if args.command == "worker-health":
        return _worker_health()
    parser.error(f"unknown command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
