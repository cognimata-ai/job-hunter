"""The credential checks the login and the re-authentication share (spec §16.2, §16.3; rulings T16-a and
T16-c; plan M5 Task 18, Ruling T18-3).

The login (`api.routes.auth`) and the privacy routes' re-authentication (`api.reauth`) decide with these same
functions, so a change to what accepts a password and a code changes both, and neither can grow weaker than
the other:

- `lockout_in_force(engine, ip, *, now)`: the read-only pre-check each runs before any Argon2 work
  (`throttle.peek_lockout`, on a connection of its own, rolled back): how long `ip` is still locked out, or
  `None`. The caller answers and logs it; only the check under `throttle.serialize` decides;
- `read_totp_secret(user, *, keys)`: the user's TOTP secret (`None` while TOTP is off), and whether no
  configured data key opens it (logged `totp_secret_unreadable` with the error class only). Such a secret
  fails the verification, for a right and a wrong password alike;
- `credentials_hold(conn, *, credentials_ok, verified_hash, totp_step, totp_secret_enc)`: the success check
  of a decision transaction, after `throttle.serialize` and the lockout check: the verification succeeded, the
  user row is still as it saw it (`users.confirm_unchanged`: the hash, the TOTP state and the sealed secret,
  locked `FOR UPDATE`, ruling T16-d) and, with TOTP, the code's step is claimed, so a step at or before the
  last one used fails (`users.claim_totp_step`, RFC 6238 §5.2, ruling T16-a). The claim is the last check: a
  failure uses no step;
- `raise_totp_key_alert(conn)`: the critical alert `auth.totp_key_mismatch` (subject `totp`) of a secret no
  key opens, raised in the decision transaction after the throttle lock (ruling T16-c).

All but the pre-check run on the caller's connection and leave the transaction to the caller. Nothing here
logs or raises a password, a code, a secret or a name.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.auth import throttle, users
from jobhunter.auth.users import UserRow
from jobhunter.crypto import DecryptError
from jobhunter.observability.alerts import raise_alert
from jobhunter.observability.logging import get_logger

TOTP_KEY_ALERT_CODE: Final = "auth.totp_key_mismatch"

log = get_logger("jobhunter.auth.credentials")


async def lockout_in_force(engine: AsyncEngine, ip: str, *, now: datetime) -> timedelta | None:
    """How long `ip` is still locked out at `now` (its own or the global lockout), or `None`; read-only, on a
    connection of its own (module docstring)."""
    async with engine.connect() as conn:  # read-only, rolled back when the block ends
        return await throttle.peek_lockout(conn, ip, now=now)


def read_totp_secret(user: UserRow, *, keys: Sequence[bytes]) -> tuple[str | None, bool]:
    """The user's TOTP secret (`None` while TOTP is off, or when no key of `keys` opens it), and whether no
    key opens it (module docstring)."""
    if not user.totp_enabled:
        return None, False
    try:
        return users.totp_secret(user, keys=keys), False
    except DecryptError as exc:
        log.error("totp_secret_unreadable", error_class=type(exc).__name__)
        return None, True


async def credentials_hold(
    conn: AsyncConnection,
    *,
    credentials_ok: bool,
    verified_hash: str | None,
    totp_step: int | None,
    totp_secret_enc: bytes | None,
) -> bool:
    """Whether a verification's credentials hold in the decision transaction (module docstring):
    `credentials_ok` (the password, and the code when TOTP is on), the user row still as the verification read
    it (`verified_hash`, TOTP on exactly when `totp_step` is set, `totp_secret_enc`), then `totp_step`
    claimed."""
    return (
        credentials_ok
        and verified_hash is not None
        and await users.confirm_unchanged(
            conn,
            expected_hash=verified_hash,
            totp_enabled=totp_step is not None,
            totp_secret_enc=totp_secret_enc,
        )
        and (totp_step is None or await users.claim_totp_step(conn, totp_step))
    )


async def raise_totp_key_alert(conn: AsyncConnection) -> None:
    """`auth.totp_key_mismatch` (critical, subject `totp`); call it after `throttle.serialize` (module
    docstring)."""
    await raise_alert(
        conn,
        code=TOTP_KEY_ALERT_CODE,
        severity="critical",
        subject="totp",
        message="the TOTP secret does not open with the configured data keys",
    )
