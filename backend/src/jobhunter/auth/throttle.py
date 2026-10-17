"""Login throttling in the database (spec §16.3).

Every login attempt is a `login_attempts` row: the client address, an HMAC of the username (never the raw
value) and whether it succeeded. Failures lock out:

- per address: `IP_MAX_FAILURES` failures of one address within `IP_WINDOW` → `login_lockouts('ip:<ip>')`
  for `IP_LOCK`;
- globally: `GLOBAL_MAX_FAILURES` failures of all addresses within `GLOBAL_WINDOW` →
  `login_lockouts('global')` for `GLOBAL_LOCK`, and the critical alert `auth.lockout` (subject `global`).

Counting and locking out is a read followed by a write, so two concurrent failures could both count one short
of a threshold and neither lock out. Every login decision therefore holds the transaction advisory lock
`THROTTLE_LOCK_KEY` (`serialize`), taken first in its transaction: decisions run one after another, and each
counts the committed attempts of the ones before it (READ COMMITTED: a statement after the lock wait sees
what the lock holder committed). `jobhunter sessions unlock` (`unlock_all`) is the escape hatch.

All functions run on the caller's connection and leave the transaction to the caller.
"""

import hashlib
import hmac
import unicodedata
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.crypto import hkdf_sha256
from jobhunter.db.sql import q
from jobhunter.observability.alerts import raise_alert, resolve_alert

THROTTLE_LOCK_KEY: Final = 7261002
IP_MAX_FAILURES: Final = 5
IP_WINDOW: Final = timedelta(minutes=15)
IP_LOCK: Final = timedelta(minutes=15)
GLOBAL_MAX_FAILURES: Final = 20
GLOBAL_WINDOW: Final = timedelta(hours=1)
GLOBAL_LOCK: Final = timedelta(hours=1)

LOCKOUT_ALERT_CODE: Final = "auth.lockout"
GLOBAL_SCOPE: Final = "global"

_SERIALIZE = q("SELECT pg_advisory_xact_lock(CAST(:key AS bigint))")
# An expired lockout is deleted when it is next looked at; an expired global one resolves its alert then.
_DELETE_EXPIRED = q(
    "DELETE FROM login_lockouts WHERE scope IN ('global', :ip_scope) AND locked_until <= :now RETURNING scope"
)
_LATEST_LOCK_END = q("SELECT max(locked_until) FROM login_lockouts WHERE scope IN ('global', :ip_scope)")
_INSERT_ATTEMPT = q(
    "INSERT INTO login_attempts (at, ip, username_hmac, success) "
    "VALUES (:now, CAST(:ip AS inet), :username_hmac, :success)"
)
_IP_FAILURES = q(
    "SELECT count(*) FROM login_attempts WHERE ip = CAST(:ip AS inet) AND NOT success AND at > :since"
)
_ALL_FAILURES = q("SELECT count(*) FROM login_attempts WHERE NOT success AND at > :since")
# A lockout is only ever extended, never shortened.
_LOCK_OUT = q(
    "INSERT INTO login_lockouts (scope, locked_until, reason) VALUES (:scope, :locked_until, :reason) "
    "ON CONFLICT (scope) DO UPDATE SET locked_until = GREATEST(login_lockouts.locked_until, "
    "EXCLUDED.locked_until), reason = EXCLUDED.reason"
)
_UNLOCK_ALL = q("DELETE FROM login_lockouts")


def ip_scope(ip: str) -> str:
    return f"ip:{ip}"


def username_hmac(username: str, session_secret: bytes) -> bytes:
    """HMAC-SHA256 of the casefolded NFC `username` under `HKDF(session_secret, "login-username")`."""
    key = hkdf_sha256(session_secret, b"login-username")
    folded = unicodedata.normalize("NFC", username).casefold()
    return hmac.new(key, folded.encode("utf-8"), hashlib.sha256).digest()


async def serialize(conn: AsyncConnection) -> None:
    """Wait for, then hold until the transaction ends, the login throttle lock (advisory key 7261002)."""
    await conn.execute(_SERIALIZE, {"key": THROTTLE_LOCK_KEY})


async def active_lockout(conn: AsyncConnection, ip: str, *, now: datetime) -> timedelta | None:
    """How long `ip` is still locked out (the later of its own and the global lockout), or `None`.

    Lockouts of `ip` and the global one that have expired by `now` are deleted; when the global one is among
    them, the `auth.lockout` alert is resolved. Call it after `serialize`.
    """
    expired = await conn.execute(_DELETE_EXPIRED, {"ip_scope": ip_scope(ip), "now": now})
    if GLOBAL_SCOPE in expired.scalars().all():
        await resolve_alert(conn, code=LOCKOUT_ALERT_CODE, subject=GLOBAL_SCOPE)
    locked_until = (await conn.execute(_LATEST_LOCK_END, {"ip_scope": ip_scope(ip)})).scalar_one()
    return None if locked_until is None else locked_until - now


async def record_attempt(
    conn: AsyncConnection, *, ip: str, username_hmac: bytes, success: bool, now: datetime
) -> None:
    """Record an attempt; after a failure, lock out `ip` or everyone when a threshold is reached.

    It requires `serialize` earlier in the same transaction: only then do the counts include every attempt
    decided before this one.
    """
    await conn.execute(
        _INSERT_ATTEMPT, {"now": now, "ip": ip, "username_hmac": username_hmac, "success": success}
    )
    if success:
        return  # only a failure can reach a threshold
    ip_failures = (await conn.execute(_IP_FAILURES, {"ip": ip, "since": now - IP_WINDOW})).scalar_one()
    if ip_failures >= IP_MAX_FAILURES:
        await conn.execute(
            _LOCK_OUT, {"scope": ip_scope(ip), "locked_until": now + IP_LOCK, "reason": "ip_failures"}
        )
    all_failures = (await conn.execute(_ALL_FAILURES, {"since": now - GLOBAL_WINDOW})).scalar_one()
    if all_failures >= GLOBAL_MAX_FAILURES:
        await conn.execute(
            _LOCK_OUT, {"scope": GLOBAL_SCOPE, "locked_until": now + GLOBAL_LOCK, "reason": "global_failures"}
        )
        await raise_alert(
            conn,
            code=LOCKOUT_ALERT_CODE,
            severity="critical",
            subject=GLOBAL_SCOPE,
            message="global login lockout",
        )


async def unlock_all(conn: AsyncConnection) -> int:
    """Delete every lockout (`jobhunter sessions unlock`); return how many there were.

    It takes the throttle lock itself, so it never interleaves with a login decision. The recorded attempts
    stay: a failure within the windows after an unlock counts them again.
    """
    await serialize(conn)
    return (await conn.execute(_UNLOCK_ALL)).rowcount
