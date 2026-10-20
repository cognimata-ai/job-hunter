# tests/unit/test_sources_base.py
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from typing import Any

import pytest
from jobhunter.sources.base import (
    ContractViolation,
    NormalizedPosting,
    RawPage,
    RequestSpec,
    SourceError,
    request_key,
)
from pydantic import ValidationError

SPEC = RequestSpec(
    source_instance_id=1,
    operation="list",
    cell_id=7,
    page_no=1,
    params={"query": "x in Berlin, Germany", "country": "de"},
    cursor_in=None,
    detail_native_id=None,
    parent_fetch_request_id=None,
    window=None,
    request_class="normal",
)
T = datetime(2026, 10, 16, 9, 47, tzinfo=UTC)


def test_request_key_is_stable_and_cursor_sensitive() -> None:
    assert request_key(SPEC, 42) == request_key(RequestSpec.from_json(SPEC.to_json()), 42)
    assert len(request_key(SPEC, 42)) == 32
    assert request_key(SPEC, 42) != request_key(SPEC, 43)
    assert request_key(SPEC, 42) != request_key(RequestSpec(**{**SPEC.__dict__, "cursor_in": "c"}), 42)
    assert "x-rapidapi-key" not in str(SPEC.to_json())
    assert SPEC.to_json()["params"] == {"country": "de", "query": "x in Berlin, Germany"}


def test_request_key_covers_the_identity_fields_and_nothing_else() -> None:
    def key(**changes: object) -> bytes:
        return request_key(RequestSpec(**{**SPEC.__dict__, **changes}), 42)

    base = key()
    # §5.1: two detail calls of one board and page never collide; params and the page are identity.
    assert key(detail_native_id="a") != key(detail_native_id="b") != base
    assert key(params={"query": "y"}) != base and key(page_no=2) != base and key(cell_id=8) != base
    assert key(operation="detail") != base and key(source_instance_id=2) != base
    # The insertion order of the params is not identity; the window, the class and the parent are not either.
    assert key(params={"country": "de", "query": "x in Berlin, Germany"}) == base
    assert key(window=(T, T), request_class="retry", parent_fetch_request_id=9) == base


def test_spec_json_round_trip_keeps_every_field() -> None:
    spec = RequestSpec(
        source_instance_id=3,
        operation="detail",
        cell_id=11,
        page_no=2,
        params={"b": "2", "a": "1"},
        cursor_in="cur",
        detail_native_id="744000",
        parent_fetch_request_id=5,
        window=(datetime(2026, 10, 1, tzinfo=UTC), T),
        request_class="probe",
    )
    doc = spec.to_json()
    assert list(spec.params) == ["a", "b"] and doc["window"] == ["2026-10-01T00:00:00+00:00", T.isoformat()]
    assert RequestSpec.from_json(doc) == spec


def test_a_spec_is_declared_unhashable() -> None:
    """Fix wave F4 (ledger L62): `params` is a read-only mapping, so a spec has no hash; the class says so
    instead of carrying a dataclass `__hash__` that fails only when called."""
    assert RequestSpec.__hash__ is None
    with pytest.raises(TypeError, match="unhashable"):
        hash(SPEC)
    assert RequestSpec(**SPEC.__dict__) == SPEC  # equality stays by value (frozen: see the posting test)


@pytest.mark.parametrize("name", ["api_key", "app_id", "app_key", "API_KEY", "App_Key"])
def test_a_spec_never_holds_a_provider_secret_parameter(name: str) -> None:
    with pytest.raises(ValueError, match=name):
        RequestSpec(**{**SPEC.__dict__, "params": {name: "v"}})


def test_spec_from_json_refuses_an_unknown_operation() -> None:
    with pytest.raises(ValueError, match="operation"):
        RequestSpec.from_json({**SPEC.to_json(), "operation": "delete"})


def page(body: bytes, content_type: str = "application/json", status: int = 200) -> RawPage:
    return RawPage(spec=SPEC, status=status, headers={"content-type": content_type}, body=body, fetched_at=T)


def test_raw_page_json_is_parsed_once_and_text_follows_the_charset() -> None:
    p = page(b'{"a": [1, 2]}')
    assert p.json() == {"a": [1, 2]} and p.json() is p.json()
    assert page("Grüße".encode("latin-1"), "text/plain; charset=ISO-8859-1").text() == "Grüße"
    assert page("Grüße".encode()).text() == "Grüße"
    assert page(b"\xff ok", "text/plain; charset=no-such-codec").text() == "� ok"


@pytest.mark.parametrize(
    "body",
    [
        b"<html><body>Service Unavailable</body></html>",
        b"",
        b'{"a": NaN}',  # not JSON (RFC 8259), and PostgreSQL's jsonb refuses it
        b"[" * 100_000,  # nested deeper than the parser recurses
        b"\xff\xfe\xfa",
    ],
)
def test_raw_page_json_of_a_non_json_body_is_a_contract_violation(body: bytes) -> None:
    with pytest.raises(ContractViolation, match="not_json") as exc:
        page(body, status=503).json()
    assert (exc.value.kind, exc.value.code, exc.value.status) == ("contract", "not_json", 503)


def test_source_errors_name_kind_code_and_status_only() -> None:
    err = SourceError("transient", code="connect", before_send=True)
    assert (err.kind, err.code, err.status, err.before_send) == ("transient", "connect", None, True)
    assert str(err) == "transient: connect"
    assert (
        str(SourceError("rate_limited", code="http_429", status=429)) == "rate_limited: http_429 (HTTP 429)"
    )
    violation = ContractViolation("missing_data", status=200)
    assert isinstance(violation, SourceError) and violation.before_send is False
    assert str(violation) == "contract: missing_data (HTTP 200)"


POSTING: dict[str, Any] = {
    "native_id": "uid-1",
    "title_raw": "Python Developer (m/w/d)",
    "company_raw": "ACME GmbH",
    "locations": [],
    "location_kind": "unknown",
    "work_mode_possible": frozenset({"remote", "hybrid", "onsite"}),
    "work_mode_source": "none",
    "remote_countries": (),
    "employment_types": ("full_time",),
    "employment_source": "structured",
    "salary": None,
    "posted_at": T,
    "posted_precision": "day",
    "posted_raw": "2 days ago",
    "item_ts": T,
    "description_text": "text",
    "description_html_sanitized": None,
    "description_completeness": "full",
    "description_quality": "short",
    "ad_language_structured": None,
    "primary_link": "https://example.org/jobs/1",
    "outbound_url_private": None,
    "publisher": "Example",
    "raw_keys": (),
    "requisition_id": None,
    "source_structured": {"seniority": "senior", "years": [3, 5]},
}


def test_normalized_posting_is_frozen_and_validates_its_enums() -> None:
    posting = NormalizedPosting(**POSTING)
    assert posting.employment_types == ("full_time",) and posting.source_structured["years"] == [3, 5]
    with pytest.raises(ValidationError):
        posting.native_id = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        NormalizedPosting(**{**POSTING, "employment_types": ("full_time", "gig")})
    with pytest.raises(ValidationError):
        NormalizedPosting(**{**POSTING, "description_completeness": "detail_call"})
    with pytest.raises(FrozenInstanceError):
        SPEC.page_no = 2  # type: ignore[misc]
