"""TOTP (RFC 6238, spec §16.2): six digits, 30 s steps, one step of clock drift accepted either way.

`matched_step` names the step a code belongs to, so that a login can refuse a code whose step it has already
accepted once (RFC 6238 §5.2, ruling T16-a: `users.totp_last_step`).
"""

import hmac
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


def matched_step(secret: str, code: str, *, at: datetime) -> int | None:
    """The time step (`unix time // 30`) whose code `code` is, within `VALID_WINDOW` steps of `at`, or `None`.

    `at` is explicit (the caller passes its clock) and must be timezone-aware: pyotp would read a naive
    time as local time. Anything but exactly six ASCII digits is wrong without further ado. When two steps of
    the window happen to share a code, the later step is the one named, so a replay of that code can never
    name a later step than its first use did.
    """
    if at.utcoffset() is None:
        raise ValueError("at must be timezone-aware")
    if _CODE.fullmatch(code) is None:
        return None
    otp = pyotp.TOTP(secret)
    current = int(otp.timecode(at))
    for step in range(current + VALID_WINDOW, current - VALID_WINDOW - 1, -1):
        if hmac.compare_digest(str(otp.generate_otp(step)), code):
            return step
    return None


def verify(secret: str, code: str, *, at: datetime) -> bool:
    """Whether `code` is the code of the step of `at`, or of the step before or after it (`matched_step`)."""
    return matched_step(secret, code, at=at) is not None
