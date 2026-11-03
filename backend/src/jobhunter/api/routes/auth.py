"""`/api/auth`: login, logout and the session (spec §14.2, §16.2-§16.4).

A login runs in phases, so that what it writes persists whatever the answer is (Global Constraints):

0. A read-only lockout pre-check (`throttle.peek_lockout`, its own short transaction): a locked-out client
   gets its 429 before any Argon2 work. Being locked out does not depend on the credentials, so the early
   answer reveals nothing about them.
   Then, when TOTP is on and the login carries no code (`totp` absent, `null` or empty), it gets 401
   `totp_required` at once (§15.2), before the gate and any Argon2 work. That answer records nothing (no
   attempt, so no lockout) and comes from the single user's flag alone (`users.totp_enabled_for_any`, which
   names no user), so it is the same for any username and password: it tells only that TOTP is on. A code
   that is not six digits is refused by the request's validation (422) before the login runs at all; a login
   with a well-formed code goes on, and its every failure is the generic 401 below.
   Then a slot of the process's Argon2 gate (`argon2_gate`, ruling T21-e): two logins verify at once and
   eight wait; one more gets 429 `throttled` (`Retry-After: 1`) at once, before any Argon2 work. A login that
   gets a slot repeats the pre-check first: a burst ahead of it in the queue may have locked it out by then.
1. Holding the slot, the credentials are verified outside any transaction, at one cost whatever is right or
   wrong until every factor matched: `find_user` on a connection that is returned before the Argon2 work; the
   TOTP secret decrypted (before the password, so an unreadable secret does not depend on it); exactly one
   Argon2 verification (`check_password`, or `dummy_verify` for an unknown username, §16.3); then the TOTP
   code; and only when all of that matched, the rehash (`hash_password`) of a hash made with other parameters.
   Every Argon2 computation runs on the gate's threads.
2. `decide_login` runs in one `engine.begin()` block that ends normally and so commits the attempt, any
   lockout and alert it wrote, the rehash and the session; it re-checks the lockout under the throttle
   lock, which is what decides. Only after the commit does the route turn the outcome into a response or a
   `Problem`.

A TOTP secret that no configured data key opens (a misconfiguration) fails the login like a wrong code and
raises the critical alert `auth.totp_key_mismatch` (subject `totp`), for a right and a wrong password alike.

A successful login replaces the session whose cookie the request carried (rotation, §16.4): that session is
deleted in the transaction that creates the new one.

Every other failure is the same generic 401 `unauthenticated`. The client address is `request.client.host`,
which uvicorn's proxy headers middleware sets from the trusted proxy's `X-Forwarded-For` only (§16.3, V20).
The login is checked for `Origin` by the edge; it needs no `X-JH-Request` (§16.5).

Logs carry the outcome and the client address only (ruling T16-b): never the username, password, TOTP code,
token, cookie or username HMAC.
"""

import ipaddress
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from starlette.responses import Response

from jobhunter.api.argon2_gate import Argon2Gate, GateFull
from jobhunter.api.deps import get_argon2_gate, get_config, get_engine, require_session
from jobhunter.api.errors import Problem
from jobhunter.api.openapi import (
    BODY_LIMIT_RESPONSES,
    CSRF_RESPONSES,
    LOGIN_CSRF_RESPONSES,
    SESSION_RESPONSES,
)
from jobhunter.auth import sessions, throttle, totp, users
from jobhunter.auth.passwords import check_password, dummy_verify, hash_password
from jobhunter.auth.sessions import SessionInfo
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.crypto import DecryptError, data_keys
from jobhunter.observability.alerts import raise_alert
from jobhunter.observability.logging import get_logger

# Where a request without a usable client address is counted: one shared bucket, so such clients cannot
# escape the per-address throttle (uvicorn over TCP always has an address; a misconfiguration may not).
UNKNOWN_CLIENT_IP: Final = "0.0.0.0"  # noqa: S104 - an address recorded, not one bound to
INVALID_CREDENTIALS: Final = "Invalid credentials"
TOTP_REQUIRED: Final = "TOTP code required"
# What a login refused by the full Argon2 gate is told to wait: a slot frees within one verification or two.
BUSY_RETRY_AFTER_S: Final = 1
TOTP_KEY_ALERT_CODE: Final = "auth.totp_key_mismatch"

router = APIRouter(prefix="/api/auth")
log = get_logger("jobhunter.auth")


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=1024)
    totp: str | None = Field(
        default=None,
        pattern=r"^(\d{6})?$",
        description="The six-digit code of the authenticator app, needed when TOTP is on; an empty string "
        "counts as no code",
    )

    @field_validator("totp", mode="before")
    @classmethod
    def _empty_code_is_no_code(cls, value: object) -> object:
        # Only the empty string: a code of any other shape than six digits stays a validation error (422).
        return None if value == "" else value


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
    verified_totp_secret_enc: bytes | None,
    previous_token: str | None,
) -> LoginOutcome:
    """Decide a login whose credentials were verified already, and write what it decided.

    `credentials_ok` is the verification's result: the password, and the TOTP code when TOTP is on.
    `verified_hash` is the stored hash the password was verified against, `totp_step` the step of the
    accepted code (`totp.matched_step`), `None` when TOTP is off, and `verified_totp_secret_enc` the sealed
    TOTP secret the code was verified with (as stored; `None` without TOTP). `previous_token` is the session
    cookie the request carried, if any. It never raises a `Problem`: the caller commits, then answers.

    1. `throttle.serialize`, the first statement (ruling T16-c);
    2. a lockout in force → `Locked`, whatever the credentials (nothing is recorded);
    3. a success also needs the user row unchanged (`users.confirm_unchanged`: the hash, the TOTP state and
       the sealed secret) and, with TOTP, a step later than the last one used (`users.claim_totp_step`,
       ruling T16-a): otherwise it counts as a failure;
    4. `record_attempt`, which may lock out and raise `auth.lockout`; a failure → `Rejected`;
    5. a success stores the rehash (only over the verified hash), deletes the session of `previous_token`
       (rotation) and creates a new one → `Accepted`.
    """
    await throttle.serialize(conn)
    retry_after = await throttle.active_lockout(conn, ip, now=now)
    if retry_after is not None:
        return Locked(retry_after)
    success = (
        credentials_ok
        and verified_hash is not None
        and await users.confirm_unchanged(
            conn,
            expected_hash=verified_hash,
            totp_enabled=totp_step is not None,
            totp_secret_enc=verified_totp_secret_enc,
        )
        and (totp_step is None or await users.claim_totp_step(conn, totp_step))
    )
    await throttle.record_attempt(conn, ip=ip, username_hmac=username_hmac, success=success, now=now)
    if not success:
        return Rejected()
    if new_hash is not None:
        await users.update_hash(conn, new_hash, expected_hash=verified_hash)
    if previous_token:
        await sessions.delete_session(conn, previous_token, pepper=pepper)
    token = await sessions.create_session(conn, pepper=pepper, ip=ip, user_agent=user_agent, now=now)
    return Accepted(token)


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


@dataclass(frozen=True)
class _Verified:
    ok: bool
    new_hash: str | None = field(default=None, repr=False)
    verified_hash: str | None = field(default=None, repr=False)
    totp_step: int | None = None
    totp_secret_enc: bytes | None = field(default=None, repr=False)
    totp_key_mismatch: bool = False


async def _verify_credentials(
    engine: AsyncEngine, cfg: AppConfig, gate: Argon2Gate, body: LoginIn, *, at: datetime
) -> _Verified:
    """Phase 1: verify the credentials without holding a connection or a transaction during the hash.

    Until every factor matched it costs one Argon2 verification, whatever is right or wrong; the rehash
    comes last. The caller holds a slot of `gate`, whose threads run every Argon2 computation.
    """
    async with engine.connect() as conn:
        user = await users.find_user(conn, body.username)
    if user is None:
        await gate.run(dummy_verify, body.password)
        return _Verified(ok=False)
    secret, key_mismatch = None, False
    if user.totp_enabled:
        try:
            secret = users.totp_secret(user, keys=data_keys(cfg))
        except DecryptError as exc:
            log.error("totp_secret_unreadable", error_class=type(exc).__name__)
            key_mismatch = True
    ok, needs_rehash = await gate.run(check_password, user.password_hash, body.password)
    if not ok or key_mismatch:
        return _Verified(ok=False, totp_key_mismatch=key_mismatch)
    step = None
    if user.totp_enabled:
        step = None if secret is None or body.totp is None else totp.matched_step(secret, body.totp, at=at)
        if step is None:
            return _Verified(ok=False)
    new_hash = await gate.run(hash_password, body.password) if needs_rehash else None
    return _Verified(
        ok=True,
        new_hash=new_hash,
        verified_hash=user.password_hash,
        totp_step=step,
        totp_secret_enc=user.totp_secret_enc,
    )


def _throttled(ip: str, *, outcome: str, title: str, retry_after_s: int) -> Problem:
    log.warning("login", outcome=outcome, ip=ip)
    return Problem(
        429,
        "throttled",
        title,
        headers={"Retry-After": str(retry_after_s)},
        extra={"retry_after_s": retry_after_s},
    )


def _locked(ip: str, retry_after: timedelta) -> Problem:
    retry_after_s = max(1, math.ceil(retry_after.total_seconds()))
    return _throttled(ip, outcome="locked", title="Too many failed logins", retry_after_s=retry_after_s)


async def _refuse_if_locked_out(engine: AsyncEngine, ip: str, *, now: datetime) -> None:
    """The read-only lockout pre-check: raise the 429 of a lockout in force (nothing is recorded)."""
    async with engine.connect() as conn:  # read-only, rolled back when the block ends
        retry_after = await throttle.peek_lockout(conn, ip, now=now)
    if retry_after is not None:
        raise _locked(ip, retry_after)


async def _refuse_if_totp_code_missing(engine: AsyncEngine, body: LoginIn, ip: str) -> None:
    """Raise 401 `totp_required` when TOTP is on and `body` has no code (nothing is recorded).

    It reads the single user's flag only, never the username or the password: its answer and its cost are
    the same for any of them.
    """
    if body.totp is not None:
        return
    async with engine.connect() as conn:  # read-only, rolled back when the block ends
        required = await users.totp_enabled_for_any(conn)
    if required:
        log.warning("login", outcome="totp_required", ip=ip)
        raise Problem(401, "totp_required", TOTP_REQUIRED)


@router.post(
    "/login",
    status_code=204,
    response_class=Response,
    responses={
        401: {
            "description": "Invalid credentials (`unauthenticated`), or TOTP is on and no code was sent "
            "(`totp_required`)"
        },
        429: {"description": "Locked out, or too many logins at once (`throttled`, `retry_after_s`)"},
        **LOGIN_CSRF_RESPONSES,
        **BODY_LIMIT_RESPONSES,
    },
)
async def login(
    body: LoginIn,
    request: Request,
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    cfg: Annotated[AppConfig, Depends(get_config)],
    gate: Annotated[Argon2Gate, Depends(get_argon2_gate)],
) -> Response:
    """Log in: 204 with the session cookie; 401 `totp_required` when TOTP is on and no code was sent, 401
    `unauthenticated` for any other failure; 429 when locked out or busy."""
    now = utcnow()
    ip = client_ip(request)
    await _refuse_if_locked_out(engine, ip, now=now)  # phase 0
    await _refuse_if_totp_code_missing(engine, body, ip)  # before the gate: no Argon2 work, nothing recorded
    session_secret = cfg.secret_b64("session_secret")
    try:
        async with gate.slot():
            await _refuse_if_locked_out(engine, ip, now=now)  # again: the queue may have taken a while
            verified = await _verify_credentials(engine, cfg, gate, body, at=now)
    except GateFull:
        raise _throttled(
            ip, outcome="busy", title="Too many logins at once", retry_after_s=BUSY_RETRY_AFTER_S
        ) from None
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
            verified_totp_secret_enc=verified.totp_secret_enc,
            previous_token=request.cookies.get(sessions.SESSION_COOKIE),
        )
        if verified.totp_key_mismatch:  # after the throttle lock, the first statement (ruling T16-c)
            await raise_alert(
                conn,
                code=TOTP_KEY_ALERT_CODE,
                severity="critical",
                subject="totp",
                message="the TOTP secret does not open with the configured data keys",
            )
    # Committed: the attempt, any lockout and alert, the rehash and the session persist from here on.
    match outcome:
        case Locked(retry_after=retry_after):
            raise _locked(ip, retry_after)
        case Rejected():
            log.warning("login", outcome="rejected", ip=ip)
            raise Problem(401, "unauthenticated", INVALID_CREDENTIALS)
        case Accepted(token=token):
            log.info("login", outcome="accepted", ip=ip)
            response = Response(status_code=204)
            response.headers.append("Set-Cookie", sessions.session_cookie(token))
            return response


@router.post(
    "/logout",
    status_code=204,
    response_class=Response,
    dependencies=[Depends(require_session)],
    responses={**SESSION_RESPONSES, **CSRF_RESPONSES},
)
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


@router.get("/session", response_model=SessionOut, responses=SESSION_RESPONSES)
async def session(info: Annotated[SessionInfo, Depends(require_session)]) -> SessionOut:
    """The current session: who is logged in, whether TOTP is on, and when the session expires."""
    return SessionOut(
        username=info.username,
        totp_enabled=info.totp_enabled,
        idle_expires_at=info.idle_expires_at.astimezone(UTC),
        absolute_expires_at=info.absolute_expires_at.astimezone(UTC),
    )
