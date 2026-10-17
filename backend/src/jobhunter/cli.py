"""Command line entry point (`python -m jobhunter.cli`, installed as the `jobhunter` script)."""

import argparse
import sys
from collections.abc import Sequence
from typing import Final, get_args

from jobhunter.config import Role, load_config, startup_or_exit

HTTP_CHECK_TIMEOUT_S: Final = 3


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobhunter")
    commands = parser.add_subparsers(dest="command", required=True)
    startup_check = commands.add_parser(
        "startup-check", help="run the startup assertions of a role (exit 78 when one fails)"
    )
    startup_check.add_argument("--role", required=True, choices=get_args(Role))
    commands.add_parser("migrate", help="bring the database to the code head and seed the defaults (§19.3)")
    commands.add_parser(
        "worker",
        help="run the worker: singleton lock, takeover, procrastinate queues (§3.3); SIGTERM drains it",
    )
    commands.add_parser(
        "worker-health",
        help="exit 0 when the worker heartbeat is at most 60 s old, else 1 (container HEALTHCHECK)",
    )
    http_check = commands.add_parser(
        "http-check", help="exit 0 when a GET of URL answers 2xx within 3 s, else 1 (container HEALTHCHECK)"
    )
    http_check.add_argument("url", help="the http or https URL to probe, e.g. http://127.0.0.1:8000/healthz")
    _add_user_commands(commands)
    return parser


def _add_user_commands(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    user = commands.add_parser("user", help="manage the single user (§16.2): create, set-password, totp")
    user_commands = user.add_subparsers(dest="user_command", required=True)
    stdin_help = "read the password from one line of stdin instead of prompting (refused in prod)"
    create = user_commands.add_parser("create", help="create the single user; asks for the password twice")
    create.add_argument("--username", required=True)
    create.add_argument("--password-stdin", action="store_true", help=stdin_help)
    set_password = user_commands.add_parser(
        "set-password", help="change the password and revoke every session; asks for it twice"
    )
    set_password.add_argument("--password-stdin", action="store_true", help=stdin_help)
    totp = user_commands.add_parser("totp", help="turn TOTP (authenticator app codes) on or off")
    totp_commands = totp.add_subparsers(dest="totp_command", required=True)
    totp_commands.add_parser(
        "enable", help="show the otpauth URI and a QR code, store the secret once a code verifies"
    )
    totp_commands.add_parser("disable", help="turn TOTP off and delete the secret")


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


def _http_check(url: str) -> int:
    """An api container HEALTHCHECK: one GET of `url`, with `HTTP_CHECK_TIMEOUT_S` for each socket operation.

    Quiet like `worker-health`: the standard library only (httpx is no runtime dependency), no logging setup,
    and a failure prints one stderr line that names the error class only. urllib raises `HTTPError` for every
    final status outside 2xx (after following redirects), so an answer that does not raise is a 2xx. Proxy
    variables are ignored: the probe asks the server in its own container.
    """
    try:
        # Imported here: the other commands do not need them.
        import urllib.parse
        import urllib.request

        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            raise ValueError("http-check probes http and https URLs only")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=HTTP_CHECK_TIMEOUT_S):
            pass
    except Exception as exc:  # noqa: BLE001 - the probe's exit status is its outcome; Docker records it
        print(f"http-check: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


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
    if args.command == "worker":
        # Imported here, like `migrate`: the worker pulls in procrastinate and langgraph.
        import asyncio

        from jobhunter.worker.main import worker_main

        raise SystemExit(asyncio.run(worker_main(startup_or_exit("worker"))))
    if args.command == "worker-health":
        return _worker_health()
    if args.command == "http-check":
        return _http_check(args.url)
    if args.command == "user":
        # Imported here: the user commands need argon2, pyotp, segno and the database layer.
        from jobhunter.cli_user import run_user_command

        return run_user_command(parser, args)
    parser.error(f"unknown command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
