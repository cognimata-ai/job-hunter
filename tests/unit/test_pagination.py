"""The signed keyset cursor (spec §14.3; plan Task 17, Deviation 3): `base64url(JSON) + "." +
base64url(HMAC-SHA256(key, JSON))`, the key `HKDF(session_secret, "cursor")`."""

import base64
import hashlib
import hmac
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from jobhunter.api.errors import Problem
from jobhunter.api.pagination import CURSOR_VERSION, cursor_key, decode_cursor, encode_cursor, filters_hash
from jobhunter.config import AppConfig
from jobhunter.crypto import hkdf_sha256
from jobhunter.db.seed import canonical_json

KEY = hkdf_sha256(b"\x07" * 32, b"cursor")
FILTERS = filters_hash({"status": "open"})
BODY: dict[str, Any] = {
    "v": 2,
    "sort": "version_no",
    "dir": "desc",
    "section": "all",
    "last_key": [17],
    "filters_hash": FILTERS,
}


def _unb64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def invalid(cursor: str, *, sort: str = "version_no", filters: str = FILTERS, key: bytes = KEY) -> None:
    with pytest.raises(Problem) as caught:
        decode_cursor(cursor, key=key, sort=sort, filters_hash=filters)
    assert (caught.value.status, caught.value.code) == (400, "invalid_cursor")


def test_round_trip() -> None:
    cursor = encode_cursor(BODY, key=KEY)
    assert decode_cursor(cursor, key=KEY, sort="version_no", filters_hash=FILTERS) == BODY
    assert CURSOR_VERSION == 2


def test_the_format_is_the_json_and_its_hmac_in_base64url() -> None:
    cursor = encode_cursor(BODY, key=KEY)
    data, signature = cursor.split(".")
    assert _unb64(data) == canonical_json(BODY) and json.loads(_unb64(data)) == BODY
    assert _unb64(signature) == hmac.new(KEY, canonical_json(BODY), hashlib.sha256).digest()
    assert "=" not in cursor and "+" not in cursor and "/" not in cursor  # safe in a query string


def test_a_bad_signature_is_refused() -> None:
    data, signature = encode_cursor(BODY, key=KEY).split(".")
    forged = _b64(canonical_json({**BODY, "last_key": [3]}))
    invalid(f"{forged}.{signature}")
    invalid(f"{data}.{_b64(b'\x00' * 32)}")
    invalid(encode_cursor(BODY, key=hkdf_sha256(b"\x08" * 32, b"cursor")))


def test_a_different_filters_hash_or_sort_is_refused() -> None:
    cursor = encode_cursor(BODY, key=KEY)
    invalid(cursor, filters=filters_hash({"status": "resolved"}))
    invalid(cursor, sort="created_at")


def test_another_cursor_version_is_refused() -> None:
    invalid(encode_cursor({**BODY, "v": 1}, key=KEY))
    invalid(encode_cursor({key: value for key, value in BODY.items() if key != "v"}, key=KEY))


@pytest.mark.parametrize(
    "cursor",
    ["", ".", "abc", "a.b.c", "!!!.???", f"{_b64(b'[1, 2]')}.x", f"{_b64(b'not json')}.x"],
)
def test_malformed_cursors_are_refused(cursor: str) -> None:
    invalid(cursor)


def test_a_signed_body_that_is_no_object_is_refused() -> None:
    raw = b"[2]"
    invalid(f"{_b64(raw)}.{_b64(hmac.new(KEY, raw, hashlib.sha256).digest())}")


def test_filters_hash_is_the_canonical_json_digest() -> None:
    assert filters_hash({"b": 1, "a": [True]}) == filters_hash({"a": [True], "b": 1})
    assert filters_hash({}) == hashlib.sha256(b"{}").hexdigest()
    assert filters_hash({"a": 1}) != filters_hash({"a": True})


def test_cursor_key_is_hkdf_of_the_session_secret() -> None:
    secret = b"\x05" * 32
    cfg = cast(
        AppConfig, SimpleNamespace(secret_b64=lambda name: secret if name == "session_secret" else b"")
    )
    assert cursor_key(cfg) == hkdf_sha256(secret, b"cursor") != hkdf_sha256(secret, b"session")
