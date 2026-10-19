"""The error mappings of the acquisition protocol (Task 12, fix round 1): the breaker kind of an error and the
error of a non-2xx status."""

from typing import get_args

from jobhunter.sources.acquire import error_code, failure_kind, status_error
from jobhunter.sources.base import SourceError
from jobhunter.sources.breaker import FailureKind


def test_failure_kind_is_the_one_breaker_mapping() -> None:
    for kind in get_args(FailureKind):
        assert failure_kind(SourceError(kind, code="x")) == kind
    assert (
        failure_kind(SourceError("ambiguous", code="timeout")) == "transient"
    )  # a timeout opens the breaker too
    assert failure_kind(SourceError("quota_exhausted", code="provider_error")) is None  # no breaker write
    assert (
        error_code(SourceError("ambiguous", code="timeout")) == "ambiguous"
    )  # the ledger keeps it ambiguous


def test_status_errors() -> None:
    def kind(status: int, billing: str, operation: str = "list", evidence: str | None = None) -> str:
        return status_error(status, billing, operation=operation, evidence=evidence).kind  # type: ignore[arg-type]

    assert kind(404, "free") == "not_found"  # a board or feed that is gone
    assert kind(404, "free", operation="detail") == "client"  # a withdrawn posting of a board that is there
    assert kind(404, "paid") == kind(404, "quota") == "client"
    assert kind(404, "paid", evidence="rapidapi_gateway_404") == "transient"
    assert [kind(s, "paid") for s in (429, 401, 403, 500, 503, 302, 400)] == [
        "rate_limited",
        "auth",
        "auth",
        "transient",
        "transient",
        "client",
        "client",
    ]
