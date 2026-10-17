"""The `sessions` commands (spec §16.3, §16.4, §19): `revoke-all` and `unlock`.

`cli.main` imports this module only when a `sessions` command runs (ruling T15-i). Both commands start like
the api role does (the application database role) and run in one transaction:

- `revoke-all` revokes every session (`sessions.revoke_all`): every browser logs in again;
- `unlock` deletes every login lockout under the throttle lock (`throttle.unlock_all`) and resolves the global
  `auth.lockout` alert: the escape hatch of §16.3.

Messages go to stdout and stderr and name counts and exception classes only.
"""

import argparse
import asyncio
import sys

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.auth import sessions, throttle
from jobhunter.config import AppConfig, startup_or_exit
from jobhunter.db.session import make_engine
from jobhunter.observability.alerts import resolve_alert


async def _revoke_all(conn: AsyncConnection) -> str:
    return f"revoked {await sessions.revoke_all(conn)} session(s)"


async def _unlock(conn: AsyncConnection) -> str:
    released = await throttle.unlock_all(conn)
    resolved = await resolve_alert(conn, code=throttle.LOCKOUT_ALERT_CODE, subject=throttle.GLOBAL_SCOPE)
    alert = "resolved the global auth.lockout alert" if resolved else "no global auth.lockout alert was open"
    return f"released {released} lockout(s); {alert}"


async def _in_transaction(cfg: AppConfig, command: str) -> str:
    engine = make_engine(cfg, pool_size=1)
    try:
        async with engine.begin() as conn:
            return await (_revoke_all(conn) if command == "revoke-all" else _unlock(conn))
    finally:
        await engine.dispose()


def run_sessions_command(args: argparse.Namespace) -> int:
    """`sessions revoke-all | unlock`; the exit status is the outcome.

    A database error prints its class only: SQLAlchemy puts the bound parameters into its messages.
    """
    cfg = startup_or_exit("api")
    try:
        print(asyncio.run(_in_transaction(cfg, args.sessions_command)))
    except SQLAlchemyError as exc:
        print(f"jobhunter: database error ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0
