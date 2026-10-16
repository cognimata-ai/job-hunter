"""Command line entry point (`python -m jobhunter.cli`, installed as the `jobhunter` script)."""

import argparse
from collections.abc import Sequence
from typing import get_args

from jobhunter.config import Role, startup_or_exit
from jobhunter.db.migrate import run_migrate


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobhunter")
    commands = parser.add_subparsers(dest="command", required=True)
    startup_check = commands.add_parser(
        "startup-check", help="run the startup assertions of a role (exit 78 when one fails)"
    )
    startup_check.add_argument("--role", required=True, choices=get_args(Role))
    commands.add_parser("migrate", help="bring the database to the code head and seed the defaults (§19.3)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "startup-check":
        startup_or_exit(args.role)
        return 0
    if args.command == "migrate":
        run_migrate(startup_or_exit("migrate"))
        return 0
    parser.error(f"unknown command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
