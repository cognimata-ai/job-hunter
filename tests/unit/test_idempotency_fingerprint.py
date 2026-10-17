from jobhunter.api_idempotency import request_fingerprint


def test_key_order_does_not_matter() -> None:
    assert request_fingerprint({"a": 1, "b": [1, 2]}, {}) == request_fingerprint({"b": [1, 2], "a": 1}, {})
    assert request_fingerprint({"a": 1}, {}) != request_fingerprint({"a": 2}, {})
