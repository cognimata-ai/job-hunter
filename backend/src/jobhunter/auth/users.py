"""The single user (spec §7.2, §16.2).

`users.id = 1` is enforced by a CHECK, so there is at most one row and no registration endpoint. All functions
run on the caller's connection and leave the transaction to the caller.

Usernames are stored NFC-normalised and without outer whitespace, in the case they were typed, and matched
without regard to case or Unicode form (`find_user`). Passwords go through `passwords`, which normalises them
and enforces the policy; the Argon2 hash runs in a worker thread, so it never blocks the event loop.

Nothing here puts a password, a hash, a TOTP secret or a username into an exception message. `UserRow` hides
the hash and the encrypted secret from its `repr`.
"""

import asyncio
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter import crypto
from jobhunter.auth.passwords import check_password_policy, hash_password
from jobhunter.db.sql import q

USERNAME_MIN_LENGTH: Final = 3
USERNAME_MAX_LENGTH: Final = 64
# What binds the encrypted TOTP secret to its purpose (see `crypto`).
TOTP_AAD: Final = b"totp"

_INSERT_USER = q(
    "INSERT INTO users (id, username, password_hash, password_changed_at) "
    "VALUES (1, :username, :password_hash, :now) ON CONFLICT (id) DO NOTHING RETURNING id"
)
_SELECT_USER = q("SELECT username, password_hash, totp_secret_enc, totp_enabled FROM users WHERE id = 1")
_SET_PASSWORD = q(
    "UPDATE users SET password_hash = :password_hash, password_changed_at = :now WHERE id = 1 RETURNING id"
)
_REVOKE_SESSIONS = q("UPDATE sessions SET revoked_at = :now WHERE revoked_at IS NULL")
_UPDATE_HASH = q("UPDATE users SET password_hash = :new_hash WHERE id = 1")
_UPDATE_HASH_FROM = q(
    "UPDATE users SET password_hash = :new_hash WHERE id = 1 AND password_hash = :expected_hash"
)
_ENABLE_TOTP = q("UPDATE users SET totp_secret_enc = :blob, totp_enabled = true WHERE id = 1 RETURNING id")
_DISABLE_TOTP = q(
    "UPDATE users SET totp_secret_enc = NULL, totp_enabled = false, totp_last_step = NULL WHERE id = 1 "
    "RETURNING id"
)


class UserExists(Exception):
    """`create_user` found the single user already there."""


class InvalidUsername(ValueError):
    """The username is not 3-64 characters long after normalisation; the message is the fixed code."""


class UserMissing(LookupError):
    """A change was asked of the single user, and there is none."""


@dataclass(frozen=True)
class UserRow:
    username: str
    password_hash: str = field(repr=False)
    totp_secret_enc: bytes | None = field(repr=False)
    totp_enabled: bool


def normalize_username(u: str) -> str:
    """NFC, without outer whitespace."""
    return unicodedata.normalize("NFC", u).strip()


def _caseless_key(u: str) -> str:
    # Canonical caseless matching (Unicode Standard 3.13): NFD(casefold(NFD(x))). `casefold` maps "ß" to
    # "ss", so "Straße" and "STRASSE" are one name. The NFD on both sides makes "ü" and "u" + U+0308 one
    # letter, even where case folding yields a different composition than the typed text had.
    folded = unicodedata.normalize("NFD", normalize_username(u)).casefold()
    return unicodedata.normalize("NFD", folded)


async def create_user(conn: AsyncConnection, username: str, password: str, *, now: datetime) -> None:
    """Create the single user, or raise `UserExists` (the transaction stays usable).

    Raises `InvalidUsername("username_length")` (a `ValueError`) for a username outside 3-64 characters after
    normalisation, and `WeakPassword` for a password that breaks the policy; nothing is written then.
    """
    name = normalize_username(username)
    if not USERNAME_MIN_LENGTH <= len(name) <= USERNAME_MAX_LENGTH:
        raise InvalidUsername("username_length")
    check_password_policy(password)
    password_hash = await asyncio.to_thread(hash_password, password)
    inserted = await conn.execute(
        _INSERT_USER, {"username": name, "password_hash": password_hash, "now": now}
    )
    if inserted.first() is None:
        raise UserExists


async def get_user(conn: AsyncConnection) -> UserRow | None:
    """The single user, whoever they are, or `None`."""
    row = (await conn.execute(_SELECT_USER)).first()
    if row is None:
        return None
    return UserRow(
        username=row.username,
        password_hash=row.password_hash,
        totp_secret_enc=row.totp_secret_enc,
        totp_enabled=row.totp_enabled,
    )


async def find_user(conn: AsyncConnection, username: str) -> UserRow | None:
    """The single user when `username` names them (ignoring case, Unicode form and outer spaces)."""
    row = await get_user(conn)
    if row is None or _caseless_key(row.username) != _caseless_key(username):
        return None
    return row


async def set_password(conn: AsyncConnection, password: str, *, now: datetime) -> int:
    """Replace the password and revoke every session; return how many sessions that revoked.

    Raises `WeakPassword` (nothing is written) or `UserMissing`.
    """
    check_password_policy(password)
    password_hash = await asyncio.to_thread(hash_password, password)
    changed = await conn.execute(_SET_PASSWORD, {"password_hash": password_hash, "now": now})
    if changed.first() is None:
        raise UserMissing
    revoked = await conn.execute(_REVOKE_SESSIONS, {"now": now})
    return revoked.rowcount


async def update_hash(conn: AsyncConnection, new_hash: str, *, expected_hash: str | None = None) -> None:
    """Store a rehash of the same password (`verify_password` returned it); nothing else changes.

    With `expected_hash`, the stored hash is replaced only while it still is that one: a rehash computed
    from the old hash must not undo a password changed in the meantime. A stale rehash is dropped silently.
    """
    if expected_hash is None:
        await conn.execute(_UPDATE_HASH, {"new_hash": new_hash})
    else:
        await conn.execute(_UPDATE_HASH_FROM, {"new_hash": new_hash, "expected_hash": expected_hash})


async def enable_totp(conn: AsyncConnection, secret_b32: str, *, data_key: bytes) -> None:
    """Store `secret_b32`, encrypted under `data_key`, and turn TOTP on. Raises `UserMissing`."""
    blob = crypto.encrypt(data_key, secret_b32.encode(), aad=TOTP_AAD)
    if (await conn.execute(_ENABLE_TOTP, {"blob": blob})).first() is None:
        raise UserMissing


async def disable_totp(conn: AsyncConnection) -> None:
    """Turn TOTP off, delete the secret and forget the last step a login used. Raises `UserMissing`."""
    if (await conn.execute(_DISABLE_TOTP)).first() is None:
        raise UserMissing


def totp_secret(row: UserRow, *, keys: Sequence[bytes]) -> str | None:
    """The base32 TOTP secret of `row`, decrypted with the key of `keys` that sealed it, or `None`.

    Raises `crypto.DecryptError` when no key opens it.
    """
    if row.totp_secret_enc is None:
        return None
    return crypto.decrypt(keys, row.totp_secret_enc, aad=TOTP_AAD).decode()
