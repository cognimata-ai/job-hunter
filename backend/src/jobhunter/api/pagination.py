"""The signed keyset cursor of the paged lists (spec §14.3; plan Task 17, Deviation 3).

A cursor is `base64url(JSON) + "." + base64url(HMAC-SHA256(key, JSON))`, base64url without padding, where
JSON is the body's canonical JSON (`db.seed.canonical_json`: sorted keys, no whitespace) and the key is
`HKDF(session_secret, "cursor")` (`cursor_key`). The body holds the cursor version `"v": 2`, the list's
`sort`, `dir` and `section`, the keyset position `last_key`, and `filters_hash`, the hash of the request's
filters (`filters_hash`), so a cursor is valid only for the list and the filters it was made for.

`decode_cursor` refuses with 400 `invalid_cursor` anything but a cursor this server signed for that sort and
those filters: a malformed one, a bad signature, another cursor version, another `sort` or another
`filters_hash`. The caller checks the rest of the body (its `last_key` shape). M2's lists are id-ordered and
use the `"section": "all"` form only; M4 adds the job sorts and their `scored`/`unscored` sections and `gen`.
"""

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from jobhunter.api.errors import Problem
from jobhunter.config import AppConfig
from jobhunter.crypto import hkdf_sha256
from jobhunter.db.seed import canonical_json
from jobhunter.types import JSONValue

CURSOR_VERSION: Final = 2
SECTION_ALL: Final = "all"
_KEY_INFO: Final = b"cursor"
_PART: Final = re.compile(r"[A-Za-z0-9_-]+")


def cursor_key(cfg: AppConfig) -> bytes:
    """The cursors' HMAC key: `HKDF(session_secret, "cursor")`, independent of the session pepper."""
    return hkdf_sha256(cfg.secret_b64("session_secret"), _KEY_INFO)


def filters_hash(filters: Mapping[str, JSONValue]) -> str:
    """The hex SHA-256 of the canonical JSON of a list request's filters (`{}` for none)."""
    return hashlib.sha256(canonical_json(dict(filters))).hexdigest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(part: str) -> bytes:
    if _PART.fullmatch(part) is None:
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _signature(key: bytes, raw: bytes) -> bytes:
    return hmac.new(key, raw, hashlib.sha256).digest()


def encode_cursor(body: Mapping[str, JSONValue], *, key: bytes) -> str:
    """The signed cursor of `body` (module docstring)."""
    raw = canonical_json(dict(body))
    return f"{_b64(raw)}.{_b64(_signature(key, raw))}"


def invalid_cursor() -> Problem:
    return Problem(
        400,
        "invalid_cursor",
        "Invalid cursor",
        detail="the cursor is not one this list issued for these filters: request the first page again",
    )


def decode_cursor(cursor: str, *, key: bytes, sort: str, filters_hash: str) -> Mapping[str, JSONValue]:
    """The body of `cursor`, once its signature, version, `sort` and `filters_hash` hold; else 400
    `invalid_cursor` (module docstring). The body is read-only."""
    data, dot, signature = cursor.partition(".")
    try:
        if not dot:
            raise ValueError("no signature")
        raw, mac = _unb64(data), _unb64(signature)
    except (ValueError, binascii.Error):
        raise invalid_cursor() from None
    if not hmac.compare_digest(mac, _signature(key, raw)):
        raise invalid_cursor()
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):  # signed by this server, so never in practice
        raise invalid_cursor() from None
    if (
        not isinstance(body, dict)
        or body.get("v") != CURSOR_VERSION
        or body.get("sort") != sort
        or body.get("filters_hash") != filters_hash
    ):
        raise invalid_cursor()
    return MappingProxyType(body)
