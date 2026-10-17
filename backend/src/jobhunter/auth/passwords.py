"""Passwords (spec §16.2, §16.3): Argon2id hashing, the policy, and constant-path verification.

Every function applies `normalize_password` (NFC) first, so a password typed as NFD on one keyboard and as NFC
on another is the same password. The policy is at least 12 characters (counted after normalisation) and not
among the 10,000 most common passwords (SecLists, `common_passwords.txt`), compared case-insensitively.

Nothing here puts a password or a hash into an exception message or a log line.
"""

import contextlib
import secrets
import unicodedata
from importlib.resources import files
from typing import Final, Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

ARGON2: Final = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16)

MIN_LENGTH: Final = 12
_COMMON_PASSWORDS_FILE: Final = "common_passwords.txt"


def _load_common_passwords() -> frozenset[str]:
    """The casefolded entries of `common_passwords.txt`; a whole line that starts with `#` is a comment."""
    text = files("jobhunter.auth").joinpath(_COMMON_PASSWORDS_FILE).read_text(encoding="utf-8")
    entries = (line.strip() for line in text.splitlines() if not line.startswith("#"))
    return frozenset(entry.casefold() for entry in entries if entry)


# Loaded once at import: a wheel that lacks the file fails when the process starts, not at the first login.
_COMMON_PASSWORDS: Final = _load_common_passwords()

# A hash of a random password nobody knows, made with ARGON2 itself at import, so that `dummy_verify` costs
# what a real verification costs, whatever the parameters are.
_DUMMY_HASH: Final = ARGON2.hash(secrets.token_urlsafe(32))


class WeakPassword(ValueError):
    """The password breaks the policy. `reason` says which rule; the message never contains the password."""

    def __init__(self, reason: Literal["too_short", "common"]) -> None:
        super().__init__(reason)
        self.reason = reason


def normalize_password(p: str) -> str:
    return unicodedata.normalize("NFC", p)


def check_password_policy(p: str) -> None:
    normalized = normalize_password(p)
    if len(normalized) < MIN_LENGTH:
        raise WeakPassword("too_short")
    if normalized.casefold() in _COMMON_PASSWORDS:
        raise WeakPassword("common")


def hash_password(p: str) -> str:
    return ARGON2.hash(normalize_password(p))


def check_password(stored: str, p: str) -> tuple[bool, bool]:
    """Whether `p` matches `stored`, and whether `stored` was made with other parameters (`needs_rehash`).

    Exactly one Argon2 verification, whatever the outcome: unlike `verify_password` it computes no new hash,
    so a login can postpone the rehash until every factor matched (a right password then costs what a wrong
    one does while the TOTP code is still unchecked). A wrong password and an unusable stored hash are both
    `(False, False)`; any other failure is a bug and propagates.
    """
    try:
        ARGON2.verify(stored, normalize_password(p))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False, False
    return True, ARGON2.check_needs_rehash(stored)


def verify_password(stored: str, p: str) -> tuple[bool, str | None]:
    """Whether `p` matches `stored`, and a new hash when `stored` was made with other parameters.

    A wrong password and an unusable stored hash are both `(False, None)`; any other failure is a bug and
    propagates. The new hash is only ever returned together with `True`.
    """
    ok, needs_rehash = check_password(stored, p)
    return ok, (hash_password(p) if needs_rehash else None)


def dummy_verify(p: str) -> None:
    """One Argon2 verification that cannot succeed, for the login of a username that does not exist.

    It takes what `verify_password` takes, so the response time does not tell the two cases apart (§16.3).
    """
    with contextlib.suppress(VerifyMismatchError):
        ARGON2.verify(_DUMMY_HASH, normalize_password(p))
