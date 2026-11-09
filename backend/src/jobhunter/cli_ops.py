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
  `schedule_state` (`deadman.apply_check`) under the dead-man lock (`deadman.deadman_lock`, so no sync of
  the worker runs meanwhile); when no database is reachable (before it exists), with the period of N hours
  (1 to 168, `schedule_state`'s range; `cli._interval_hours`), enabled. With `--show` it prints the check as
  read back,
  `{"timeout_s", "grace_s", "paused"}` (exit 0). A failure prints `{"error": "<code>"}` (exit 1):
  `not_configured` without both healthchecks secrets, `ping_url_form` for a ping URL that does not end in the
  check's uuid, `no_period` without a database or N, else `DeadmanApiError`'s code. It records nothing in
  the database: the worker's tick finds the check in line and stamps it at its next sync.

Neither prints the topic URL, the ping URL, the check's uuid, the ntfy token or the API key, on any path
(Q14): the outputs are codes, numbers and booleans, an unexpected error prints `{"error": "unexpected",
"error_class": <its class>}` (never its message, which can name a URL), and the logs mask the secrets
(`config.startup_or_exit` registers them).
"""

import argparse
import asyncio
import json
import sys
from contextlib import AsyncExitStack
from dataclasses import asdict
from typing import Final

from sqlalchemy.exc import SQLAlchemyError

from jobhunter.config import AppConfig, startup_or_exit
from jobhunter.db.session import make_null_engine
from jobhunter.observability import deadman
from jobhunter.observability.deadman import CheckState, DeadmanApiError, HealthchecksClient
from jobhunter.observability.logging import configure_logging
from jobhunter.observability.ntfy import DueAlert, NtfyClient, NtfyError
from jobhunter.observability.ntfy import make_http_client as make_ntfy_http_client
from jobhunter.types import JSONValue

TEST_ALERT: Final = DueAlert(0, "test", "warn", "Job Hunter test notification")

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


async def deadman_sync(cfg: AppConfig, *, hours: int | None) -> CheckState:
    """`ops deadman-sync` (module docstring): the check as read back; `_Refused` or `DeadmanApiError` when it
    failed."""
    secrets = deadman.healthchecks_secrets(cfg)
    if secrets is None:
        raise _Refused("not_configured")
    ping_url, api_key = secrets
    engine = make_null_engine(cfg.dsn("sqlalchemy"))
    try:
        async with deadman.make_http_client(user_agent=cfg.http_user_agent) as http, AsyncExitStack() as held:
            try:
                client = HealthchecksClient(
                    http, ping_url=ping_url, api_url=cfg.healthchecks_api_url, api_key=api_key
                )
            except ValueError:
                raise _Refused("ping_url_form") from None
            schedule: tuple[int, bool] | None = None
            try:
                await held.enter_async_context(deadman.deadman_lock(engine))
                async with engine.connect() as conn:
                    schedule = await deadman.read_schedule(conn)
            except (SQLAlchemyError, OSError):
                schedule = None  # no database here, or none yet: the period given, if any
            if schedule is None:
                if hours is None:
                    raise _Refused("no_period")
                schedule = (hours * 3600, True)
            interval_s, enabled = schedule
            return await deadman.apply_check(client, interval_s=interval_s, enabled=enabled)
    finally:
        await engine.dispose()


async def _run(cfg: AppConfig, args: argparse.Namespace) -> tuple[Printed | None, bool]:
    """The command of `args`: what it prints (`None`: nothing) and whether it succeeded."""
    if args.ops_command == "notify-test":
        printed = await notify_test(cfg)
        return printed, printed.get("sent") is True
    try:
        check = await deadman_sync(cfg, hours=args.interval_hours)
    except (_Refused, DeadmanApiError) as failed:
        return {"error": failed.code}, False
    return (dict(asdict(check)) if args.show else None), True


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
