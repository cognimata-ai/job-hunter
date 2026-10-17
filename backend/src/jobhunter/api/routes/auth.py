"""`/api/auth`: login, logout and the session (spec §14.2, §16.2-§16.4).

A login runs in two phases, so that what it writes persists whatever the answer is (Global Constraints):

1. The credentials are verified outside any transaction: `find_user` on a connection that is returned before
   the Argon2 work, then `verify_password` (or `dummy_verify` for an unknown username, so both cases cost
   one Argon2 verification, §16.3), then the TOTP code when TOTP is on. No connection is held meanwhile.
2. `decide_login` runs in one `engine.begin()` block that ends normally and so commits the attempt, any
   lockout and alert it wrote, the rehash and the session. Only after the commit does the route turn the
   outcome into a response or a `Problem`.

Every failure is the same generic 401. The client address is `request.client.host`, which uvicorn's proxy
headers middleware sets from the trusted proxy's `X-Forwarded-For` only (§16.3, V20). The login is checked
for `Origin` by the edge; it needs no `X-JH-Request` (§16.5).

Logs carry the outcome and the client address only (ruling T16-b): never the username, password, TOTP code,
token, cookie or username HMAC.
"""

import asyncio
import ipaddress
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from starlette.responses import Response

from jobhunter.api.deps import get_config, get_engine, require_session
from jobhunter.api.errors import Problem
from jobhunter.auth import sessions, throttle, totp, users
from jobhunter.auth.passwords import dummy_verify, verify_password
from jobhunter.auth.sessions import SessionInfo
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.db.sql import q
from jobhunter.observability.logging import get_logger

# Where a request without a usable client address is counted: one shared bucket, so such clients cannot
# escape the per-address throttle (uvicorn over TCP always has an address; a misconfiguration may not).
UNKNOWN_CLIENT_IP: Final = "0.0.0.0"  # noqa: S104 - an address recorded, not one bound to
INVALID_CREDENTIALS: Final = "Invalid credentials"

# The user row must still be what the verification saw (the same hash, TOTP still on or still off): a
# password changed (and its sessions revoked) or TOTP turned on meanwhile rejects the login. The row lock
# (an UPDATE, or FOR UPDATE) makes a concurrent `set_password` wait for this login's commit, so that its
# revocation also covers the session created here.
_CLAIM_TOTP_STEP = q(
    "UPDATE users SET totp_last_step = :step "
    "WHERE id = 1 AND password_hash = :verified_hash AND totp_enabled "
    "AND (totp_last_step IS NULL OR totp_last_step < :step) RETURNING id"
)
_CONFIRM_WITHOUT_TOTP = q(
    "SELECT id FROM users WHERE id = 1 AND password_hash = :verified_hash AND NOT totp_enabled FOR UPDATE"
)

router = APIRouter(prefix="/api/auth")
log = get_logger("jobhunter.auth")


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=1024)
    totp: str | None = Field(default=None, pattern=r"^\d{6}$")


class SessionOut(BaseModel):
    username: str
    totp_enabled: bool
    idle_expires_at: datetime
    absolute_expires_at: datetime


@dataclass(frozen=True)
class Locked:
    retry_after: timedelta


@dataclass(frozen=True)
class Rejected:
    pass


@dataclass(frozen=True)
class Accepted:
    token: str = field(repr=False)


LoginOutcome = Locked | Rejected | Accepted


async def decide_login(
    conn: AsyncConnection,
    *,
    ip: str,
    username_hmac: bytes,
    credentials_ok: bool,
    new_hash: str | None,
    pepper: bytes,
    user_agent: str | None,
    now: datetime,
    verified_hash: str | None,
    totp_step: int | None,
) -> LoginOutcome:
    """Decide a login whose credentials were verified already, and write what it decided.

    `credentials_ok` is the verification's result: the password, and the TOTP code when TOTP is on.
    `verified_hash` is the stored hash the password was verified against, and `totp_step` the step of the
    accepted code (`totp.matched_step`), `None` when TOTP is off. It never raises a `Problem`: the caller
    commits, then answers.

    1. `throttle.serialize`, the first statement (ruling T16-c);
    2. a lockout in force → `Locked`, whatever the credentials (nothing is recorded);
    3. a success also needs the user row unchanged and, with TOTP, a step later than the last one used
       (ruling T16-a): otherwise it counts as a failure;
    4. `record_attempt`, which may lock out and raise `auth.lockout`; a failure → `Rejected`;
    5. a success stores the rehash (only over the verified hash) and a new session → `Accepted`.
    """
    await throttle.serialize(conn)
    retry_after = await throttle.active_lockout(conn, ip, now=now)
    if retry_after is not None:
        return Locked(retry_after)
    success = credentials_ok and await _user_unchanged(conn, verified_hash, totp_step)
    await throttle.record_attempt(conn, ip=ip, username_hmac=username_hmac, success=success, now=now)
    if not success:
        return Rejected()
    if new_hash is not None:
        await users.update_hash(conn, new_hash, expected_hash=verified_hash)
    token = await sessions.create_session(conn, pepper=pepper, ip=ip, user_agent=user_agent, now=now)
    return Accepted(token)


async def _user_unchanged(conn: AsyncConnection, verified_hash: str | None, totp_step: int | None) -> bool:
    if verified_hash is None:
        return False
    if totp_step is None:
        return (
            await conn.execute(_CONFIRM_WITHOUT_TOTP, {"verified_hash": verified_hash})
        ).first() is not None
    claimed = await conn.execute(_CLAIM_TOTP_STEP, {"verified_hash": verified_hash, "step": totp_step})
    return claimed.first() is not None


def client_ip(request: Request) -> str:
    """The client address of `request` in canonical form (an IPv4-mapped IPv6 address as IPv4)."""
    host = request.client.host if request.client is not None else None
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        return UNKNOWN_CLIENT_IP
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(address)


def _data_keys(cfg: AppConfig) -> list[bytes]:
    """The data key, and the previous one during a rotation: the keys a TOTP secret may be sealed with."""
    keys = [cfg.secret_b64("data_key")]
    if "data_key_previous" in cfg.secrets:
        keys.append(cfg.secret_b64("data_key_previous"))
    return keys


@dataclass(frozen=True)
class _Verified:
    ok: bool
    new_hash: str | None = field(default=None, repr=False)
    verified_hash: str | None = field(default=None, repr=False)
    totp_step: int | None = None


async def _verify_credentials(
    engine: AsyncEngine, cfg: AppConfig, body: LoginIn, *, at: datetime
) -> _Verified:
    """Phase 1: verify the credentials without holding a connection or a transaction during the hash."""
    async with engine.connect() as conn:
        user = await users.find_user(conn, body.username)
    if user is None:
        await asyncio.to_thread(dummy_verify, body.password)
        return _Verified(ok=False)
    ok, new_hash = await asyncio.to_thread(verify_password, user.password_hash, body.password)
    if not ok:
        return _Verified(ok=False)
    if not user.totp_enabled:
        return _Verified(ok=True, new_hash=new_hash, verified_hash=user.password_hash)
    secret = users.totp_secret(user, keys=_data_keys(cfg))
    step = None if secret is None or body.totp is None else totp.matched_step(secret, body.totp, at=at)
    if step is None:
        return _Verified(ok=False)
    return _Verified(ok=True, new_hash=new_hash, verified_hash=user.password_hash, totp_step=step)


@router.post(
    "/login",
    status_code=204,
    response_class=Response,
    responses={401: {"description": "Invalid credentials"}, 429: {"description": "Too many failed logins"}},
)
async def login(
    body: LoginIn,
    request: Request,
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    cfg: Annotated[AppConfig, Depends(get_config)],
) -> Response:
    """Log in: 204 with the session cookie, 401 for any failure, 429 while locked out (`retry_after_s`)."""
    now = utcnow()
    ip = client_ip(request)
    session_secret = cfg.secret_b64("session_secret")
    verified = await _verify_credentials(engine, cfg, body, at=now)
    async with engine.begin() as conn:
        outcome = await decide_login(
            conn,
            ip=ip,
            username_hmac=throttle.username_hmac(body.username, session_secret),
            credentials_ok=verified.ok,
            new_hash=verified.new_hash,
            pepper=sessions.session_pepper(session_secret),
            user_agent=request.headers.get("user-agent"),
            now=now,
            verified_hash=verified.verified_hash,
            totp_step=verified.totp_step,
        )
    # Committed: the attempt, any lockout and alert, the rehash and the session persist from here on.
    match outcome:
        case Locked(retry_after=retry_after):
            log.warning("login", outcome="locked", ip=ip)
            retry_after_s = max(1, math.ceil(retry_after.total_seconds()))
            raise Problem(
                429,
                "throttled",
                "Too many failed logins",
                headers={"Retry-After": str(retry_after_s)},
                extra={"retry_after_s": retry_after_s},
            )
        case Rejected():
            log.warning("login", outcome="rejected", ip=ip)
            raise Problem(401, "unauthenticated", INVALID_CREDENTIALS)
        case Accepted(token=token):
            log.info("login", outcome="accepted", ip=ip)
            response = Response(status_code=204)
            response.headers.append("Set-Cookie", sessions.session_cookie(token))
            return response


@router.post("/logout", status_code=204, response_class=Response, dependencies=[Depends(require_session)])
async def logout(
    request: Request,
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    cfg: Annotated[AppConfig, Depends(get_config)],
) -> Response:
    """Delete the session and clear its cookie (a valid session is required, like on every `/api` route)."""
    token = request.cookies.get(sessions.SESSION_COOKIE, "")
    async with engine.begin() as conn:
        await sessions.delete_session(
            conn, token, pepper=sessions.session_pepper(cfg.secret_b64("session_secret"))
        )
    response = Response(status_code=204)
    response.headers.append("Set-Cookie", sessions.CLEARED_SESSION_COOKIE)
    return response


@router.get("/session", response_model=SessionOut)
async def session(info: Annotated[SessionInfo, Depends(require_session)]) -> SessionOut:
    """The current session: who is logged in, whether TOTP is on, and when the session expires."""
    return SessionOut(
        username=info.username,
        totp_enabled=info.totp_enabled,
        idle_expires_at=info.idle_expires_at.astimezone(UTC),
        absolute_expires_at=info.absolute_expires_at.astimezone(UTC),
    )
