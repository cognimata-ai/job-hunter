import pytest
from jobhunter.api.errors import Problem
from jobhunter.api_idempotency import check_idempotency_key, request_fingerprint


def test_key_order_does_not_matter() -> None:
    assert request_fingerprint({"a": 1, "b": [1, 2]}, {}) == request_fingerprint({"b": [1, 2], "a": 1}, {})
    assert request_fingerprint({"a": 1}, {}) != request_fingerprint({"a": 2}, {})


@pytest.mark.parametrize("key", [None, "", "short", "x" * 129, "has space in it!!!", "sixteen-chars-ok\n"])
def test_a_malformed_key_is_refused_without_the_database(key: str | None) -> None:
    with pytest.raises(Problem) as refused:
        check_idempotency_key(key)
    assert (refused.value.status, refused.value.code) == (400, "idempotency_key_required")


def test_a_well_formed_key_passes() -> None:
    assert check_idempotency_key("sixteen-chars_ok") == "sixteen-chars_ok"
    assert check_idempotency_key("k" * 128) == "k" * 128
