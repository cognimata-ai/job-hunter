"""Sessions (spec §16.4): a random token in the `__Host-jh_session` cookie, an HMAC of it in `sessions`.

The token is `secrets.token_urlsafe(32)` (256 bits). The database stores only
`HMAC-SHA256(token, session_pepper(session_secret))`, so a copy of the table opens no session. A session
lives 12 h after its last use (`idle_expires_at`, sliding at most once a minute) and at most 30 d after it
was created (`absolute_expires_at`); the slide never passes the absolute bound (the CHECK of §7.2).

All functions run on the caller's connection and leave the transaction to the caller. A token, its HMAC or the
cookie never reaches a log line or an exception message; `SessionInfo` hides the HMAC from its `repr`.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.crypto import hkdf_sha256
from jobhunter.db.sql import q

SESSION_COOKIE: Final = "__Host-jh_session"
IDLE: Final = timedelta(hours=12)
ABSOLUTE: Final = timedelta(days=30)
SLIDE_EVERY: Final = timedelta(minutes=1)
COOKIE_MAX_AGE_S: Final = 2_592_000
TOKEN_BYTES: Final = 32

# `__Host-`: Secure, Path=/ and no Domain, so only this exact origin ever receives the cookie.
_COOKIE_ATTRIBUTES: Final = "Path=/; Secure; HttpOnly; SameSite=Strict"
CLEARED_SESSION_COOKIE: Final = f"{SESSION_COOKIE}=; Max-Age=0; {_COOKIE_ATTRIBUTES}"

_INSERT = q(
    "INSERT INTO sessions (token_hmac, user_id, created_at, last_seen_at, idle_expires_at, "
    "absolute_expires_at, ip, user_agent_sha256) "
    "VALUES (:token_hmac, 1, :now, :now, :idle_expires_at, :absolute_expires_at, CAST(:ip AS inet), "
    ":user_agent_sha256)"
)
_SELECT_VALID = q(
    "SELECT s.last_seen_at, s.idle_expires_at, s.absolute_expires_at, u.username, u.totp_enabled "
    "FROM sessions s JOIN users u ON u.id = s.user_id "
    "WHERE s.token_hmac = :token_hmac AND s.revoked_at IS NULL "
    "AND s.idle_expires_at > :now AND s.absolute_expires_at > :now"
)
# The `last_seen_at` condition repeats the check the caller made: of two requests that both saw a stale
# `last_seen_at`, the second one waits for the first one's row lock and then slides nothing.
_SLIDE = q(
    "UPDATE sessions SET last_seen_at = :now, idle_expires_at = LEAST(:idle_expires_at, absolute_expires_at) "
    "WHERE token_hmac = :token_hmac AND last_seen_at <= :slide_before RETURNING idle_expires_at"
)
_DELETE = q("DELETE FROM sessions WHERE token_hmac = :token_hmac")
_REVOKE_ALL = q("UPDATE sessions SET revoked_at = now() WHERE revoked_at IS NULL")


@dataclass(frozen=True)
class SessionInfo:
    token_hmac: bytes = field(repr=False)
    username: str
    totp_enabled: bool
    idle_expires_at: datetime
    absolute_expires_at: datetime


def session_pepper(session_secret: bytes) -> bytes:
    """The HMAC key of the session tokens: `HKDF(session_secret, "session")`."""
    return hkdf_sha256(session_secret, b"session")


def token_hmac(token: str, pepper: bytes) -> bytes:
    """`HMAC-SHA256(token, pepper)`: what `sessions.token_hmac` stores for `token`."""
    return hmac.new(pepper, token.encode("utf-8"), hashlib.sha256).digest()


def session_cookie(token: str) -> str:
    """The `Set-Cookie` value that gives the browser `token` for `COOKIE_MAX_AGE_S` (the absolute bound)."""
    return f"{SESSION_COOKIE}={token}; Max-Age={COOKIE_MAX_AGE_S}; {_COOKIE_ATTRIBUTES}"


async def create_session(
    conn: AsyncConnection, *, pepper: bytes, ip: str | None, user_agent: str | None, now: datetime
) -> str:
    """Store a new session of the single user and return its token (the cookie value).

    `ip` is the real client address (§16.3); `user_agent` is stored as its SHA-256 only.
    """
    token = secrets.token_urlsafe(TOKEN_BYTES)
    await conn.execute(
        _INSERT,
        {
            "token_hmac": token_hmac(token, pepper),
            "now": now,
            "idle_expires_at": now + IDLE,
            "absolute_expires_at": now + ABSOLUTE,
            "ip": ip,
            "user_agent_sha256": None
            if user_agent is None
            else hashlib.sha256(user_agent.encode("utf-8")).digest(),
        },
    )
    return token


async def validate_session(
    conn: AsyncConnection, token: str, *, pepper: bytes, now: datetime, slide: bool = True
) -> SessionInfo | None:
    """The session of `token`, or `None` when there is none, or it is revoked or past either expiry.

    When its last use is at least `SLIDE_EVERY` ago, it slides: `last_seen_at = now` and `idle_expires_at =
    least(now + IDLE, absolute_expires_at)`. The caller commits that write. With `slide=False` (a background
    request's validation) it only reads: both expiries are checked alike, nothing is written and no row is
    locked, and the session comes back as stored.
    """
    hashed = token_hmac(token, pepper)
    row = (await conn.execute(_SELECT_VALID, {"token_hmac": hashed, "now": now})).first()
    if row is None:
        return None
    idle_expires_at = row.idle_expires_at
    if slide and now - row.last_seen_at >= SLIDE_EVERY:
        slid = await conn.execute(
            _SLIDE,
            {
                "token_hmac": hashed,
                "now": now,
                "idle_expires_at": now + IDLE,
                "slide_before": now - SLIDE_EVERY,
            },
        )
        slid_to = slid.scalar_one_or_none()  # None: a concurrent request slid it first
        if slid_to is not None:
            idle_expires_at = slid_to
    return SessionInfo(
        token_hmac=hashed,
        username=row.username,
        totp_enabled=row.totp_enabled,
        idle_expires_at=idle_expires_at,
        absolute_expires_at=row.absolute_expires_at,
    )


async def delete_session(conn: AsyncConnection, token: str, *, pepper: bytes) -> None:
    """Delete the session of `token` (logout); nothing happens when there is none."""
    await conn.execute(_DELETE, {"token_hmac": token_hmac(token, pepper)})


async def revoke_all(conn: AsyncConnection) -> int:
    """Revoke every session that is not revoked yet; return how many that were."""
    return (await conn.execute(_REVOKE_ALL)).rowcount
