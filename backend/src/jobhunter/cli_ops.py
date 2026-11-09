"""The `ops` commands (spec §17.6, §17.7; plan M5 Task 5, O-ops Q14): the V15 checks the deployment runs
against the user's ntfy and healthchecks accounts, before the first scheduled slot (Carry-forward 1).

`cli.main` parses them (its parsers need no import of this module) and imports this module only when one runs.
Both start like the worker (the worker-role config and its startup assertions, `docker compose run --rm
worker jobhunter ops …`), log to stderr, and print one JSON line on stdout:

- `jobhunter ops notify-test` publishes one push of the test alert (`TEST_ALERT`, code `test`, warn: the
  title `Job Hunter - test` at priority 3) to `NTFY_TOPIC_URL`, in one try (`NtfyClient.publish`), and prints
  `{"sent": true}` (exit 0) or `{"sent": false, "error": "<code>"}` (exit 1): `not_configured` without a
  topic, else `NtfyError`'s code (`timeout`, `connect`, `invalid_request`, `http_<status>`).
- `jobhunter ops deadman-sync [--show] [--interval-hours N]` brings the dead-man check in line with
  `schedule_state` (`deadman.apply_check`) under the dead-man lock (`deadman.deadman_lock`, so no sync of the
  worker runs meanwhile). Only when no database server answered (fix round 1, M4: an `OSError`, or an error
  without a SQLSTATE while no server accepts a connection on the configured host and port: refused, no such
  host, a connect timeout) does it apply the period of N hours (1 to 168, `schedule_state`'s range;
  `cli._interval_hours`), enabled. A server that answered with an error (a SQLSTATE, or a refusal while
  connecting: the password, the role, the database, a grant) fails it with `database`, N or not, so a period
  it was not asked for never replaces the schedule's. psycopg reports no SQLSTATE for any failure while
  connecting, so a short TCP probe of the server's port (`_server_listens`) tells the two apart. With `--show`
  it prints the check as read back and where its period came from, `{"timeout_s", "grace_s", "paused",
  "source"}` with `source` `schedule_state` or `interval_hours` (exit 0). A failure prints `{"error":
  "<code>"}` (exit 1): `not_configured` without both healthchecks secrets, `ping_url_form` for a ping URL that
  does not end in the check's uuid, `database`, `no_period` without a database server or N, else
  `DeadmanApiError`'s code. It records nothing in the database: the worker's tick finds the check in line and
  stamps it at its next sync.

Neither prints the topic URL, the ping URL, the check's uuid, the ntfy token or the API key, on any path
(Q14): the outputs are codes, numbers and booleans, an unexpected error prints `{"error": "unexpected",
"error_class": <its class>}` (never its message, which can name a URL), and the logs mask the secrets
(`config.startup_or_exit` registers them).
"""

import argparse
import asyncio
import json
import sys
from contextlib import AsyncExitStack, suppress
from dataclasses import asdict
from typing import Final, Literal

from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from jobhunter.config import AppConfig, startup_or_exit
from jobhunter.db.sql import pg_error
from jobhunter.observability import deadman
from jobhunter.observability.deadman import CheckState, DeadmanApiError, HealthchecksClient
from jobhunter.observability.logging import configure_logging
from jobhunter.observability.ntfy import DueAlert, NtfyClient, NtfyError
from jobhunter.observability.ntfy import make_http_client as make_ntfy_http_client
from jobhunter.types import JSONValue

TEST_ALERT: Final = DueAlert(0, "test", "warn", "Job Hunter test notification")
DB_CONNECT_TIMEOUT_S: Final = 10  # psycopg's `connect_timeout`: a silent host is no server either
SERVER_PROBE_TIMEOUT_S: Final = 3.0

Source = Literal["schedule_state", "interval_hours"]

Printed = dict[str, JSONValue]


class _Refused(Exception):
    """The command cannot run as configured: `code` is what it prints."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def notify_test(cfg: AppConfig) -> Printed:
    """`ops notify-test` (module docstring): what it prints."""
    if cfg.ntfy_topic_url is None or cfg.public_origin is None:
        return {"sent": False, "error": "not_configured"}
    # ASCII: `ntfy_token_header_safe` checked it at startup, as for the worker's dispatcher.
    token = cfg.secrets.get("ntfy_token", b"").decode("ascii", errors="replace").strip() or None
    async with make_ntfy_http_client(user_agent=cfg.http_user_agent) as http:
        client = NtfyClient(http, topic_url=cfg.ntfy_topic_url, token=token, public_origin=cfg.public_origin)
        try:
            await client.publish(TEST_ALERT)
        except NtfyError as refused:
            return {"sent": False, "error": refused.code}
    return {"sent": True}


async def _server_listens(host: str, port: int) -> bool:
    """Whether a server accepts a TCP connection on `host`:`port` within `SERVER_PROBE_TIMEOUT_S`."""
    try:
        async with asyncio.timeout(SERVER_PROBE_TIMEOUT_S):
            _, writer = await asyncio.open_connection(host, port)
    except (OSError, TimeoutError):
        return False
    writer.close()
    with suppress(OSError):
        await writer.wait_closed()
    return True


async def _server_answered(cfg: AppConfig, error: Exception) -> bool:
    """Whether the database error `error` came from a server that answered (module docstring, M4): a
    SQLSTATE, or, for an error without one (psycopg gives none for a failure while connecting), a server
    listening on the configured host and port. An `OSError` is no answer."""
    if isinstance(error, OSError):
        return False
    if isinstance(error, DBAPIError) and pg_error(error).sqlstate is not None:
        return True
    return await _server_listens(cfg.db.host, cfg.db.port)


async def deadman_sync(cfg: AppConfig, *, hours: int | None) -> tuple[CheckState, Source]:
    """`ops deadman-sync` (module docstring): the check as read back and the source of the period applied;
    `_Refused` or `DeadmanApiError` when it failed."""
    secrets = deadman.healthchecks_secrets(cfg)
    if secrets is None:
        raise _Refused("not_configured")
    ping_url, api_key = secrets
    engine = create_async_engine(
        cfg.dsn("sqlalchemy"),
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"connect_timeout": DB_CONNECT_TIMEOUT_S},
    )
    try:
        async with deadman.make_http_client(user_agent=cfg.http_user_agent) as http, AsyncExitStack() as held:
            try:
                client = HealthchecksClient(
                    http, ping_url=ping_url, api_url=cfg.healthchecks_api_url, api_key=api_key
                )
            except ValueError:
                raise _Refused("ping_url_form") from None
            no_server = False
            try:
                await held.enter_async_context(deadman.deadman_lock(engine))
                async with engine.connect() as conn:
                    schedule = await deadman.read_schedule(conn)
            except (SQLAlchemyError, OSError) as error:
                if await _server_answered(cfg, error):
                    raise _Refused("database") from None
                no_server = True
            if no_server:  # no database server here, or none yet: the period given, enabled
                if hours is None:
                    raise _Refused("no_period")
                check = await deadman.apply_check(client, interval_s=hours * 3600, enabled=True)
                return check, "interval_hours"
            if schedule is None:  # `migrate` creates the table and seeds its row together
                raise _Refused("database")
            interval_s, enabled = schedule
            return await deadman.apply_check(client, interval_s=interval_s, enabled=enabled), "schedule_state"
    finally:
        await engine.dispose()


async def _run(cfg: AppConfig, args: argparse.Namespace) -> tuple[Printed | None, bool]:
    """The command of `args`: what it prints (`None`: nothing) and whether it succeeded."""
    if args.ops_command == "notify-test":
        printed = await notify_test(cfg)
        return printed, printed.get("sent") is True
    try:
        check, source = await deadman_sync(cfg, hours=args.interval_hours)
    except (_Refused, DeadmanApiError) as failed:
        return {"error": failed.code}, False
    return ({**asdict(check), "source": source} if args.show else None), True


def run_ops_command(args: argparse.Namespace) -> int:
    """`ops notify-test | deadman-sync`; the exit status is the outcome (module docstring)."""
    cfg = startup_or_exit("worker")
    configure_logging(cfg.log_level, stream=sys.stderr)
    try:
        printed, ok = asyncio.run(_run(cfg, args))
    except Exception as exc:  # noqa: BLE001 - the printed code is the outcome; never a message or traceback (Q14)
        printed, ok = {"error": "unexpected", "error_class": type(exc).__name__}, False
        if args.ops_command == "notify-test":
            printed = {"sent": False, **printed}
    if printed is not None:
        print(json.dumps(printed))
    return 0 if ok else 1
