"""TOTP (RFC 6238, spec §16.2): six digits, 30 s steps, one step of clock drift accepted either way."""

import re
from datetime import datetime
from typing import Final

import pyotp

ISSUER: Final = "Job Hunter"
VALID_WINDOW: Final = 1
_CODE: Final = re.compile(r"[0-9]{6}")


def new_secret() -> str:
    """A new random secret, base32 as authenticator apps expect it (160 bits)."""
    return str(pyotp.random_base32())


def provisioning_uri(secret: str, username: str) -> str:
    """The `otpauth://totp/...` URI that authenticator apps read from the QR code."""
    return str(pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=ISSUER))


def verify(secret: str, code: str, *, at: datetime) -> bool:
    """Whether `code` is the code of the step of `at`, or of the step before or after it.

    `at` is explicit (the caller passes its clock) and must be timezone-aware: pyotp would read a naive
    time as local time. Anything but exactly six ASCII digits is wrong without further ado.
    """
    if at.utcoffset() is None:
        raise ValueError("at must be timezone-aware")
    if _CODE.fullmatch(code) is None:
        return False
    return bool(pyotp.TOTP(secret).verify(code, for_time=at, valid_window=VALID_WINDOW))
