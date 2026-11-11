"""The re-authentication of a destructive privacy request (spec §20.4, §16.3; plan M5 Task 18, M5-U4, D13,
Deviations 3 and 21): `verify_reauth`, which `POST /api/privacy/delete-everything` and `POST
/api/privacy/purge-source` run once their phrase, fingerprint and stored response were checked.

The request carries the password and, with TOTP on, a code; the session says who the user is (the single
user), so no username is sent. It runs as the login runs its own (`api.routes.auth`), in phases, so what it
records persists whatever the request does next:

0. a read-only lockout pre-check (`throttle.peek_lockout`): an address locked out gets the login's 429
   `throttled` (`auth.lockout_problem`) before any Argon2 work;
1. holding a slot of the process's Argon2 gate (a full gate is 429 `throttled` with `Retry-After: 1`, before
   any Argon2 work; the pre-check is repeated once the slot is held), without a transaction: the user read on
   a connection returned before the hash, the TOTP secret decrypted (a secret no configured data key opens
   fails it and raises `auth.totp_key_mismatch`, as at the login), one Argon2 verification of the password
   (`check_password`; `dummy_verify` without a user), then the code, which must match a step of the window
   (`totp.matched_step`). No rehash: it is no login;
2. one transaction of its own, committed before the answer: `throttle.serialize` first, then the lockout
   again under it (`active_lockout`, which decides: 429), then, for verified credentials, the user row as the
   verification saw it (`users.confirm_unchanged`) and the code's step claimed (`users.claim_totp_step`: a
   step at or before the last one used fails, RFC 6238 §5.2). A failure records one failed attempt
   (`throttle.record_attempt(success=False)`, the HMAC of the user's name), which may lock the address out
   (five within 15 min) or everyone (`auth.lockout`), and answers 403 `reauth_failed`; a success records no
   attempt (only the step it used).

A missing code with TOTP on is a failure like a wrong one (the login's 401 `totp_required` is the login's
own). Logs carry the outcome and the client address only, never the password, the code or the name.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.api.argon2_gate import Argon2Gate, GateFull
from jobhunter.api.errors import Problem
from jobhunter.api.routes.auth import (
    BUSY_RETRY_AFTER_S,
    TOTP_KEY_ALERT_CODE,
    lockout_problem,
    throttled_problem,
)
from jobhunter.auth import throttle, totp, users
from jobhunter.auth.passwords import check_password, dummy_verify
from jobhunter.config import AppConfig
from jobhunter.crypto import DecryptError, data_keys
from jobhunter.observability.alerts import raise_alert
from jobhunter.observability.logging import get_logger

REAUTH_FAILED: Final = "Re-authentication failed"
BUSY: Final = "Too many re-authentications at once"

log = get_logger("jobhunter.api.reauth")


@dataclass(frozen=True)
class _Verified:
    ok: bool
    username: str = field(default="", repr=False)
    verified_hash: str | None = field(default=None, repr=False)
    totp_step: int | None = None
    totp_secret_enc: bytes | None = field(default=None, repr=False)
    totp_key_mismatch: bool = False


def _failed(ip: str) -> Problem:
    log.warning("reauth", outcome="rejected", ip=ip)
    return Problem(403, "reauth_failed", REAUTH_FAILED)


async def _refuse_if_locked_out(engine: AsyncEngine, ip: str, *, now: datetime) -> None:
    """Phase 0: the 429 of a lockout in force, read-only (nothing is recorded)."""
    async with engine.connect() as conn:  # read-only, rolled back when the block ends
        retry_after = await throttle.peek_lockout(conn, ip, now=now)
    if retry_after is not None:
        log.warning("reauth", outcome="locked", ip=ip)
        raise lockout_problem(retry_after)


async def _verify(
    engine: AsyncEngine,
    cfg: AppConfig,
    gate: Argon2Gate,
    *,
    password: str,
    totp_code: str | None,
    at: datetime,
) -> _Verified:
    """Phase 1 (module docstring): one Argon2 verification whatever is right or wrong, then the code."""
    async with engine.connect() as conn:
        user = await users.get_user(conn)
    if user is None:
        await gate.run(dummy_verify, password)
        return _Verified(ok=False)
    secret, key_mismatch = None, False
    if user.totp_enabled:
        try:
            secret = users.totp_secret(user, keys=data_keys(cfg))
        except DecryptError as exc:
            log.error("totp_secret_unreadable", error_class=type(exc).__name__)
            key_mismatch = True
    ok, _ = await gate.run(check_password, user.password_hash, password)
    step = None
    if user.totp_enabled and secret is not None and totp_code is not None:
        step = totp.matched_step(secret, totp_code, at=at)
    return _Verified(
        ok=ok and not key_mismatch and (step is not None or not user.totp_enabled),
        username=user.username,
        verified_hash=user.password_hash,
        totp_step=step,
        totp_secret_enc=user.totp_secret_enc,
        totp_key_mismatch=key_mismatch,
    )


async def _decide(
    conn: AsyncConnection, verified: _Verified, *, ip: str, session_secret: bytes, now: datetime
) -> bool | Problem:
    """Phase 2 (module docstring), in the caller's transaction: whether the re-authentication succeeded, or
    the 429 of a lockout in force (nothing recorded)."""
    await throttle.serialize(conn)
    retry_after = await throttle.active_lockout(conn, ip, now=now)
    if retry_after is not None:
        log.warning("reauth", outcome="locked", ip=ip)
        return lockout_problem(retry_after)
    success = (
        verified.ok
        and verified.verified_hash is not None
        and await users.confirm_unchanged(
            conn,
            expected_hash=verified.verified_hash,
            totp_enabled=verified.totp_step is not None,
            totp_secret_enc=verified.totp_secret_enc,
        )
        and (verified.totp_step is None or await users.claim_totp_step(conn, verified.totp_step))
    )
    if not success:
        name_hmac = throttle.username_hmac(verified.username, session_secret)
        await throttle.record_attempt(conn, ip=ip, username_hmac=name_hmac, success=False, now=now)
    return success


async def verify_reauth(
    engine: AsyncEngine,
    cfg: AppConfig,
    gate: Argon2Gate,
    *,
    ip: str,
    password: str,
    totp_code: str | None,
    now: datetime,
) -> None:
    """Re-authenticate the single user from `ip` at `now` (module docstring): `None` on success; 429
    `throttled` while the address or everyone is locked out (or the Argon2 gate is full), 403 `reauth_failed`
    for a wrong password or code, its failed attempt committed first."""
    await _refuse_if_locked_out(engine, ip, now=now)
    try:
        async with gate.slot():
            await _refuse_if_locked_out(engine, ip, now=now)  # again: the queue may have taken a while
            verified = await _verify(engine, cfg, gate, password=password, totp_code=totp_code, at=now)
    except GateFull:
        log.warning("reauth", outcome="busy", ip=ip)
        raise throttled_problem(BUSY, retry_after_s=BUSY_RETRY_AFTER_S) from None
    async with engine.begin() as conn:  # committed whatever the request does next (Ruling T18-1 (e))
        outcome = await _decide(
            conn, verified, ip=ip, session_secret=cfg.secret_b64("session_secret"), now=now
        )
        if verified.totp_key_mismatch:  # after the throttle lock, the transaction's first statement
            await raise_alert(
                conn,
                code=TOTP_KEY_ALERT_CODE,
                severity="critical",
                subject="totp",
                message="the TOTP secret does not open with the configured data keys",
            )
    if isinstance(outcome, Problem):
        raise outcome
    if not outcome:
        raise _failed(ip)
    log.info("reauth", outcome="accepted", ip=ip)
