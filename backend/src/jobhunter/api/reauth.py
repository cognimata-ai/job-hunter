"""The re-authentication of a destructive privacy request (spec §20.4, §16.3; plan M5 Task 18, M5-U4, D13,
Deviations 3 and 21): `verify_reauth`, which `POST /api/privacy/delete-everything` and `POST
/api/privacy/purge-source` run once their phrase, fingerprint and stored response were checked.

The request carries the password and, with TOTP on, a code; the session says who the user is (the single
user), so no username is sent. It runs as the login runs its own (`api.routes.auth`), in phases, so what it
records persists whatever the request does next:

0. a read-only lockout pre-check (`auth.credentials.lockout_in_force`): an address locked out gets the
   login's 429 `throttled` (`auth.lockout_problem`) before any Argon2 work;
1. holding a slot of the process's Argon2 gate (a full gate is 429 `throttled` with `Retry-After: 1`, before
   any Argon2 work; the pre-check is repeated once the slot is held), without a transaction: the user read on
   a connection returned before the hash, the TOTP secret decrypted (`auth.credentials.read_totp_secret`: a
   secret no configured data key opens fails it and raises `auth.totp_key_mismatch`, as at the login,
   `raise_totp_key_alert`), one Argon2 verification of the password
   (`check_password`; `dummy_verify` without a user), then the code, which must match a step of the window
   (`totp.matched_step`). No rehash: it is no login;
2. one transaction of its own, committed before the answer: `throttle.serialize` first, then the lockout
   again under it (`active_lockout`, which decides: 429), then the login's own success check
   (`auth.credentials.credentials_hold`: the user row as the verification saw it, then the code's step
   claimed; a step at or before the last one used fails, RFC 6238 §5.2). A failure records one failed attempt
   (`throttle.record_attempt(success=False)`, the HMAC of the user's name), which may lock the address out
   (five within 15 min) or everyone (`auth.lockout`), and answers 403 `reauth_failed`; a success records no
   attempt (only the step it used).

A missing code with TOTP on is a failure like a wrong one (the login's 401 `totp_required` is the login's
own). Logs carry the outcome only (final review M1): never the client address, which a failed attempt's
`login_attempts` row holds, and never the password, the code or the name.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.api.argon2_gate import Argon2Gate, GateFull
from jobhunter.api.edge import throttled_problem
from jobhunter.api.errors import Problem
from jobhunter.api.routes.auth import BUSY_RETRY_AFTER_S, lockout_problem
from jobhunter.auth import throttle, totp, users
from jobhunter.auth.credentials import (
    credentials_hold,
    lockout_in_force,
    raise_totp_key_alert,
    read_totp_secret,
)
from jobhunter.auth.passwords import check_password, dummy_verify
from jobhunter.config import AppConfig
from jobhunter.crypto import data_keys
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


def _failed() -> Problem:
    log.warning("reauth", outcome="rejected")
    return Problem(403, "reauth_failed", REAUTH_FAILED)


async def _refuse_if_locked_out(engine: AsyncEngine, ip: str, *, now: datetime) -> None:
    """Phase 0: the 429 of a lockout in force, read-only (nothing is recorded)."""
    retry_after = await lockout_in_force(engine, ip, now=now)
    if retry_after is not None:
        log.warning("reauth", outcome="locked")
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
    secret, key_mismatch = read_totp_secret(user, keys=data_keys(cfg))
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
        log.warning("reauth", outcome="locked")
        return lockout_problem(retry_after)
    success = await credentials_hold(
        conn,
        credentials_ok=verified.ok,
        verified_hash=verified.verified_hash,
        totp_step=verified.totp_step,
        totp_secret_enc=verified.totp_secret_enc,
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
        log.warning("reauth", outcome="busy")
        raise throttled_problem(BUSY, retry_after_s=BUSY_RETRY_AFTER_S) from None
    async with engine.begin() as conn:  # committed whatever the request does next (Ruling T18-1 (e))
        outcome = await _decide(
            conn, verified, ip=ip, session_secret=cfg.secret_b64("session_secret"), now=now
        )
        if verified.totp_key_mismatch:  # after the throttle lock (ruling T16-c)
            await raise_totp_key_alert(conn)
    if isinstance(outcome, Problem):
        raise outcome
    if not outcome:
        raise _failed()
    log.info("reauth", outcome="accepted")
