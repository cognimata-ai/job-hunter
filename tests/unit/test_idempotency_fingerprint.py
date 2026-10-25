import hashlib

import pytest
from jobhunter.api.errors import Problem
from jobhunter.api_idempotency import check_idempotency_key, request_fingerprint
from jobhunter.db.seed import canonical_json
from jobhunter.types import JSONValue


def test_key_order_does_not_matter() -> None:
    assert request_fingerprint({"a": 1, "b": [1, 2]}, {}) == request_fingerprint({"b": [1, 2], "a": 1}, {})
    assert request_fingerprint({"a": 1}, {}) != request_fingerprint({"a": 2}, {})


@pytest.mark.parametrize(
    ("body", "path"),
    [
        ({"a": 1, "b": [1, 2]}, {}),
        (
            {"payload": {"label": "Köln-Mülheim", "note": "größer ≥ 💶", "n": None, "ok": True}},
            {"draft_id": 7},
        ),
        ({}, {"draft_id": 1}),
    ],
)
def test_a_valid_request_keeps_its_canonical_json_fingerprint(
    body: JSONValue, path: dict[str, JSONValue]
) -> None:
    """Stored fingerprints of valid requests stay byte-identical: the digest of `canonical_json` (M0)."""
    expected = hashlib.sha256(canonical_json({"body": body, "path": path})).digest()
    assert request_fingerprint(body, path) == expected


def test_a_lone_surrogate_is_fingerprinted_not_raised() -> None:
    """A JSON body can carry a lone surrogate (`"\\ud800"`); `canonical_json` cannot encode it."""
    with pytest.raises(UnicodeEncodeError):
        canonical_json({"body": {"k": "Prak\ud800tikum"}, "path": {}})
    first = request_fingerprint({"k": "Prak\ud800tikum"}, {})
    assert first == request_fingerprint({"k": "Prak\ud800tikum"}, {}) and len(first) == 32
    assert (
        first
        != request_fingerprint({"k": "Prak\udc00tikum"}, {})
        != request_fingerprint({"k": "Praktikum"}, {})
    )


@pytest.mark.parametrize("key", [None, "", "short", "x" * 129, "has space in it!!!", "sixteen-chars-ok\n"])
def test_a_malformed_key_is_refused_without_the_database(key: str | None) -> None:
    with pytest.raises(Problem) as refused:
        check_idempotency_key(key)
    assert (refused.value.status, refused.value.code) == (400, "idempotency_key_required")


def test_a_well_formed_key_passes() -> None:
    assert check_idempotency_key("sixteen-chars_ok") == "sixteen-chars_ok"
    assert check_idempotency_key("k" * 128) == "k" * 128
