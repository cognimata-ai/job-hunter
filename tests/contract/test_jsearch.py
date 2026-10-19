import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from jobhunter.normalize.keys import AdKey
from jobhunter.sources.base import (
    Continuation,
    ContractViolation,
    DuplicateInPage,
    Observation,
    Parsed,
    RawPage,
    Rejected,
    SourceError,
)
from jobhunter.sources.fixtures import fixture_transport
from jobhunter.sources.http import HttpOutcome
from jobhunter.sources.jsearch import PAGE2_MIN_NEW, JSearchAdapter, cursor_of, items_of
from jobhunter.types import get_object

from tests.support.sources import FIXTURES, fixture_page, make_cell_plan, mock_fixture, served_requests
from tests.unit.test_sources_base import SPEC

pytestmark = pytest.mark.contract
T = datetime(2026, 10, 16, 9, 47, tzinfo=UTC)


def adapter() -> JSearchAdapter:
    return JSearchAdapter(httpx.AsyncClient(), api_key=b"k", instance_id=1, clock=lambda: T)


def test_page_one_contract() -> None:
    a, page = adapter(), fixture_page("jsearch", "search_v2_page1", SPEC, fetched_at=T)
    a.check_envelope(page)
    ids = a.peek_native_ids(page)
    outcomes = a.parse(page)
    # the recorded envelope: items and cursor at the V9 paths, together
    assert len(items_of(page.json())) == 10 and isinstance(cursor_of(page.json()), str)
    assert len(ids) == 10 and all(ids) and a.continuation(page)[0] is Continuation.CURSOR
    assert (
        len(outcomes) == 10
        and sum(isinstance(o, Parsed) for o in outcomes) >= 9
        and [o.item_index for o in outcomes] == list(range(10))
    )
    parsed = [o.normalized for o in outcomes if isinstance(o, Parsed)]
    # job_uid, never the session blob
    assert all(p.native_id == uid for p, uid in zip(parsed, ids, strict=False))
    assert all(":" not in p.native_id for p in parsed)
    assert all(p.raw_keys[0].ns == "htidocid" and p.description_completeness == "full" for p in parsed)
    assert sum(len(p.description_text) >= 200 for p in parsed) / len(parsed) >= 0.9
    assert sum(p.posted_at is not None for p in parsed) / len(parsed) >= 0.9
    assert {p.posted_precision for p in parsed} <= {"hour", "day"} and any(p.locations for p in parsed)
    obs = a.observation(page)
    assert (
        obs is not None
        and obs.limit == 200
        and obs.remaining is not None
        and obs.reset_at is not None
        and obs.reset_at > T
    )


def test_empty_page_ends_and_variants_are_contract_failures() -> None:
    a = adapter()
    assert a.continuation(fixture_page("jsearch", "search_v2_empty", SPEC, fetched_at=T)) == (
        Continuation.END,
        None,
    )
    for case in ("search_v2_drift", "search_v2_html"):
        with pytest.raises(ContractViolation):
            a.check_envelope(fixture_page("jsearch", case, SPEC, fetched_at=T))


def test_settlement_rules() -> None:
    a = adapter()
    legacy = HttpOutcome(fixture_page("jsearch", "search_legacy_404", SPEC, fetched_at=T), None)
    assert a.settle(legacy) == ("not_billed", "rapidapi_gateway_404")
    too_many = HttpOutcome(fixture_page("jsearch", "search_v2_429", SPEC, fetched_at=T), None)
    assert a.settle(too_many) == ("ambiguous", None)
    refused = HttpOutcome(None, SourceError("transient", code="connect", before_send=True))
    assert a.settle(refused) == ("not_billed", "connect_failed_before_send")
    assert a.settle(HttpOutcome(None, SourceError("ambiguous", code="timeout"))) == ("ambiguous", None)


@respx.mock
async def test_fetch_sends_the_key_as_a_header_only_and_logs_no_key(
    log_events: Callable[[], list[dict[str, Any]]],
) -> None:
    route = mock_fixture(
        respx.mock, "GET", "https://jsearch.p.rapidapi.com/search-v2", "jsearch", "search_v2_page1"
    )
    async with httpx.AsyncClient() as client:
        a = JSearchAdapter(
            client,
            api_key=b"TOPSECRETKEY-abcdefghijklmnopqrstuvwxyz0123456789",
            instance_id=1,
            clock=lambda: T,
        )
        out = await a.fetch(SPEC)
    sent = route.calls.last.request
    assert sent.headers["x-rapidapi-key"].startswith("TOPSECRET") and "TOPSECRET" not in str(sent.url)
    assert out.page is not None and out.page.status == 200
    assert "TOPSECRET" not in "".join(str(e) for e in log_events()) and "cursor" not in get_object(
        SPEC.to_json(), "params"
    )


# ---- the items of a page ----------------------------------------------------------------------------

LONG = "Wir suchen Verstärkung für unser Backend-Team und freuen uns auf Ihre Bewerbung. " * 4
ITEM: dict[str, Any] = {
    "job_id": "BLOBVALUE:session-blob-that-changes-per-request",
    "job_uid": "UID-1",
    "job_title": "Python Developer (m/w/d)",
    "employer_name": "ACME GmbH",
    "job_publisher": "Stepstone",
    "job_employment_types": ["FULLTIME"],
    "job_apply_link": "https://www.stepstone.de/stellenangebote--Python-Developer-Berlin-ACME--123456-inline.html"
    "?utm_source=google&utm_medium=organic",
    "job_description": LONG,
    "job_is_remote": False,
    "job_posted_at": "vor 2 Tagen",
    "job_posted_at_timestamp": 1792108800,
    "job_location": "Berlin     •  über Stepstone",
    "job_city": None,
    "job_country": None,
}


def item(**changes: Any) -> dict[str, Any]:
    return {**ITEM, **changes}


def doc_page(
    doc: Any, *, status: int = 200, headers: dict[str, str] | None = None, fetched_at: datetime = T
) -> RawPage:
    sent = {"content-type": "application/json"} if headers is None else headers
    return RawPage(SPEC, status, sent, json.dumps(doc).encode(), fetched_at)


def items_page(items: list[Any], *, cursor: Any = "CUR") -> RawPage:
    return doc_page({"status": "OK", "data": {"jobs": items, "cursor": cursor}})


def parsed_one(**changes: Any) -> Any:
    (outcome,) = adapter().parse(items_page([item(**changes)]))
    assert isinstance(outcome, Parsed), outcome
    return outcome.normalized


def test_identity_is_the_job_uid_and_the_session_blob_is_ignored() -> None:
    posting = parsed_one()
    assert posting.native_id == "UID-1"
    assert "BLOBVALUE" not in posting.model_dump_json()
    # a different job_id of the same job_uid is the same ad
    first, second = adapter().parse(items_page([item(), item(job_id="OTHERBLOB:x")]))
    assert isinstance(first, Parsed) and isinstance(second, DuplicateInPage)
    assert (second.native_id, second.first_index, second.item_index) == ("UID-1", 0, 1)


def test_keys_are_the_uid_then_the_publisher_keys_of_the_apply_link() -> None:
    posting = parsed_one()
    assert posting.raw_keys == (AdKey("htidocid", "UID-1"), AdKey("pub:stepstone.de", "123456"))
    assert posting.primary_link == (
        "https://stepstone.de/stellenangebote--Python-Developer-Berlin-ACME--123456-inline.html"
    )
    assert posting.publisher == "Stepstone"
    unknown_host = parsed_one(job_apply_link="https://careers.example.org/jobs/7?utm_source=x&ref=1")
    assert unknown_host.raw_keys == (AdKey("htidocid", "UID-1"),)
    assert unknown_host.primary_link == "https://careers.example.org/jobs/7?ref=1"
    no_link = parsed_one(job_apply_link=None, job_publisher=None)
    assert no_link.primary_link is None and no_link.publisher is None
    assert no_link.raw_keys == (AdKey("htidocid", "UID-1"),)


def test_title_and_company_are_kept_as_the_source_wrote_them() -> None:
    posting = parsed_one(job_title="  Senior   Python Developer (m/w/d) ", employer_name="ACME GmbH & Co. KG")
    assert (posting.title_raw, posting.company_raw) == (
        "  Senior   Python Developer (m/w/d) ",
        "ACME GmbH & Co. KG",
    )


def test_item_failures_reject_the_item_and_never_the_page() -> None:
    a = adapter()
    items: list[Any] = [
        item(job_uid="A"),
        "not an object",
        item(job_uid=None),
        item(job_uid=""),
        item(job_uid=17),
        item(job_uid="B", job_title=None),
        item(job_uid="C", job_title=7),
        item(job_uid="D", employer_name=None),
        item(job_uid="E"),
    ]
    page = items_page(items)
    a.check_envelope(page)
    outcomes = a.parse(page)
    assert [o.item_index for o in outcomes] == list(range(9))
    assert [type(o) for o in outcomes] == [Parsed] + [Rejected] * 7 + [Parsed]
    rejected = [o for o in outcomes if isinstance(o, Rejected)]
    assert {o.reason_code for o in rejected} == {"item_contract"} and all(o.detail for o in rejected)
    assert [o.native_id for o in rejected] == [None, None, None, None, "B", "C", "D"]
    assert a.peek_native_ids(page) == ["A", None, None, None, None, "B", "C", "D", "E"]


def test_a_repeated_job_uid_is_a_duplicate_of_its_first_occurrence() -> None:
    outcomes = adapter().parse(
        items_page([item(job_uid="A"), item(job_uid="B"), item(job_uid="A", job_title="Other title")])
    )
    assert [type(o) for o in outcomes] == [Parsed, Parsed, DuplicateInPage]
    assert isinstance(outcomes[2], DuplicateInPage) and outcomes[2].first_index == 0


def test_a_rejected_first_occurrence_does_not_make_the_next_one_a_duplicate() -> None:
    outcomes = adapter().parse(items_page([item(job_uid="A", job_title=None), item(job_uid="A")]))
    assert [type(o) for o in outcomes] == [Rejected, Parsed]


@pytest.mark.parametrize(
    "relative, precision",
    [("vor 3 Stunden", "hour"), ("3 hours ago", "hour"), ("vor 2 Tagen", "day"), ("2 days ago", "day")],
)
def test_the_posted_date_is_the_timestamp_with_the_precision_of_the_relative_text(
    relative: str, precision: str
) -> None:
    posting = parsed_one(job_posted_at=relative, job_posted_at_timestamp=1792108800)
    assert posting.posted_at == datetime(2026, 10, 16, tzinfo=UTC)
    assert (posting.posted_precision, posting.posted_raw) == (precision, relative)
    assert posting.item_ts == posting.posted_at


@pytest.mark.parametrize("timestamp", [None, True, "1792108800", 10**15, 10**300])
def test_a_missing_or_unusable_timestamp_leaves_the_date_unknown(timestamp: Any) -> None:
    posting = parsed_one(job_posted_at_timestamp=timestamp)
    assert (posting.posted_at, posting.posted_precision, posting.item_ts) == (None, "unknown", None)
    assert posting.posted_raw == "vor 2 Tagen"


def test_the_remote_flag_is_structured_evidence_only_when_true() -> None:
    remote = parsed_one(job_is_remote=True)
    assert remote.work_mode_possible == {"remote"} and remote.work_mode_source == "structured"
    for flag in (False, None):
        other = parsed_one(job_is_remote=flag)
        assert other.work_mode_possible == {"remote", "hybrid", "onsite"} and other.work_mode_source == "none"
    assert remote.remote_countries == ()


@pytest.mark.parametrize(
    "types, expected",
    [
        (["FULLTIME"], ("full_time",)),
        (["PARTTIME", "FULLTIME"], ("part_time", "full_time")),
        (["CONTRACTOR"], ("contract",)),
        (["INTERN"], ("internship",)),
        (["FULLTIME", "FULLTIME"], ("full_time",)),
        (["FULLTIME", "TEMPORARY"], ("full_time",)),
    ],
)
def test_employment_types_are_mapped(types: list[str], expected: tuple[str, ...]) -> None:
    posting = parsed_one(job_employment_types=types)
    assert posting.employment_types == expected and posting.employment_source == "structured"


@pytest.mark.parametrize("types", [None, [], ["TEMPORARY"], "FULLTIME", [3]])
def test_no_mappable_employment_type_is_unknown(types: Any) -> None:
    posting = parsed_one(job_employment_types=types)
    assert posting.employment_types == () and posting.employment_source == "none"


def test_locations_come_from_the_city_or_the_parsed_job_location() -> None:
    berlin = parsed_one()
    assert berlin.location_kind == "city" and [loc.label for loc in berlin.locations] == ["Berlin"]
    assert berlin.locations[0].resolution == "unique" and berlin.locations[0].source == "jsearch"
    munich = parsed_one(job_city="München", job_country="DE", job_location="Irgendwo • über X")
    assert [loc.label for loc in munich.locations] == ["München"] and munich.locations[
        0
    ].resolution == "unique"
    anywhere = parsed_one(job_location="Beliebiger Ort   •  über LinkedIn")
    assert anywhere.location_kind == "country_wide"
    nowhere = parsed_one(job_location=None)
    assert nowhere.location_kind == "unknown" and nowhere.locations == []


@pytest.mark.parametrize("text", ["95.10115, 11.5", "99.5, 200.1"])
def test_a_coordinate_shaped_location_that_is_out_of_range_never_fails_the_job(text: str) -> None:
    posting = parsed_one(job_location=text)
    (location,) = posting.locations
    assert location.resolution == "unresolved" and location.candidates == [] and location.postal_code is None


def test_the_description_is_stored_as_normalised_lines_and_graded() -> None:
    long_text = "  Line one with plenty of useful words about the role.  \n\n\u00a0 Line   two \r\n" * 2
    posting = parsed_one(job_description=long_text)
    assert posting.description_text.splitlines()[:2] == [
        "Line one with plenty of useful words about the role.",
        "Line two",
    ]
    assert "\n\n" not in posting.description_text and posting.description_completeness == "full"
    assert posting.description_html_sanitized is None and posting.description_quality == "short"
    assert parsed_one().description_quality == "ok"
    for bad in (None, "", "short", "null\nnull\nnull\n" + LONG):
        assert parsed_one(job_description=bad).description_quality == "garbage"


def test_every_scrubbed_page_parses_without_rejections() -> None:
    a = adapter()
    for case in (
        "search_v2_page1",
        "search_v2_page2",
        "search_v2_page2_reused",
        "search_v2_page1_day2",
        "search_v2_page2_day2",
        "search_v2_today",
        "search_v2_empty",
    ):
        page = fixture_page("jsearch", case, SPEC, fetched_at=T)
        a.check_envelope(page)
        outcomes = a.parse(page)
        assert len(outcomes) == len(a.peek_native_ids(page)) and not any(
            isinstance(o, Rejected) for o in outcomes
        )
        assert all(isinstance(o, Parsed) for o in outcomes), case


# ---- the envelope -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "doc, code",
    [
        ([], "not_object"),
        ("OK", "not_object"),
        ({"data": {"jobs": [], "cursor": None}}, "status_invalid"),
        ({"status": None, "data": {"jobs": [], "cursor": None}}, "status_invalid"),
        ({"status": 200, "data": {"jobs": [], "cursor": None}}, "status_invalid"),
        ({"status": True, "data": {"jobs": [], "cursor": None}}, "status_invalid"),
        ({"status": ["OK"], "data": {"jobs": [], "cursor": None}}, "status_invalid"),
        ({"status": "OK"}, "items_missing"),
        ({"status": "OK", "data": {"cursor": "c"}}, "items_missing"),
        ({"status": "OK", "data": []}, "items_missing"),
        ({"status": "OK", "jobs": [], "cursor": "c"}, "items_missing"),  # the legacy layout is not v2's
        ({"status": "OK", "data": {"jobs": {"0": {}}, "cursor": "c"}}, "items_not_list"),
        ({"status": "OK", "data": {"jobs": None, "cursor": None}}, "items_not_list"),
        ({"status": "OK", "data": {"jobs": [], "cursor": ""}}, "cursor_invalid"),
        ({"status": "OK", "data": {"jobs": [], "cursor": 12}}, "cursor_invalid"),
        ({"status": "OK", "data": {"jobs": [], "cursor": ["c"]}}, "cursor_invalid"),
    ],
)
def test_an_envelope_that_breaks_the_contract_is_a_violation(doc: Any, code: str) -> None:
    with pytest.raises(ContractViolation) as exc:
        adapter().check_envelope(doc_page(doc))
    assert (exc.value.kind, exc.value.code, exc.value.status) == ("contract", code, 200)


@pytest.mark.parametrize("status", [200, 201])
@pytest.mark.parametrize(
    "doc",
    [
        {"status": "ERROR", "request_id": "r", "error": {"message": "Upstream failed", "code": 500}},
        {"status": "ERROR", "data": {"jobs": [], "cursor": None}},  # even with a well-formed data object
        {"status": "FAILED"},
        {"status": "error"},
        {"status": ""},
    ],
)
def test_a_provider_error_delivered_with_a_2xx_is_a_transient_failure_not_a_contract_violation(
    doc: dict[str, Any], status: int
) -> None:
    a, page = adapter(), doc_page(doc, status=status)
    with pytest.raises(SourceError) as exc:
        a.check_envelope(page)
    assert not isinstance(exc.value, ContractViolation)
    assert (exc.value.kind, exc.value.code, exc.value.status) == ("transient", "provider_error", status)
    assert "Upstream" not in str(exc.value)  # the provider's text is never copied into the error
    for read in (a.parse, a.peek_native_ids, a.continuation):  # nothing reads its items or cursor either
        with pytest.raises(SourceError) as again:
            read(page)
        assert (again.value.kind, again.value.code) == ("transient", "provider_error")
    assert a.settle(HttpOutcome(page, None)) == ("billed", None)  # billing is unchanged: a 2xx is billed


@pytest.mark.parametrize(
    "data",
    [{"jobs": [], "cursor": None}, {"jobs": []}, {"jobs": [{}], "cursor": "next"}],
)
def test_the_end_forms_and_a_cursor_are_valid_envelopes(data: dict[str, Any]) -> None:
    adapter().check_envelope(doc_page({"status": "OK", "data": data}))


def test_a_body_that_is_not_json_is_a_violation_and_never_an_empty_page() -> None:
    with pytest.raises(ContractViolation, match="not_json"):
        adapter().check_envelope(RawPage(SPEC, 200, {}, b"<html>502</html>", T))


# ---- continuation -----------------------------------------------------------------------------------


def test_an_explicit_cursor_decides_whatever_the_page_length() -> None:
    a = adapter()
    assert a.continuation(items_page([], cursor="CUR")) == (Continuation.CURSOR, "CUR")
    assert a.continuation(items_page([item()] * 3, cursor="CUR")) == (Continuation.CURSOR, "CUR")
    assert a.continuation(items_page([item()] * 10, cursor=None)) == (Continuation.END, None)
    assert a.continuation(doc_page({"status": "OK", "data": {"jobs": [item()] * 10}})) == (
        Continuation.END,
        None,
    )
    today = fixture_page("jsearch", "search_v2_today", SPEC, fetched_at=T)
    assert a.continuation(today) == (Continuation.END, None)


def test_a_malformed_cursor_is_never_read_as_the_end() -> None:
    with pytest.raises(ContractViolation, match="cursor_invalid"):
        adapter().continuation(items_page([item()], cursor=""))


def test_page_two_is_worth_a_request_when_seven_of_ten_ids_are_new() -> None:
    a = adapter()
    ids = {
        case: {i for i in a.peek_native_ids(fixture_page("jsearch", case, SPEC, fetched_at=T)) if i}
        for case in ("search_v2_page1", "search_v2_page2", "search_v2_page2_reused")
    }
    assert len(ids["search_v2_page2"] - ids["search_v2_page1"]) >= PAGE2_MIN_NEW
    assert len(ids["search_v2_page2_reused"] - ids["search_v2_page2"]) < PAGE2_MIN_NEW


# ---- the quota observation --------------------------------------------------------------------------


def counters(limit: str | None, remaining: str | None, reset: str | None) -> dict[str, str]:
    named = {
        "x-ratelimit-requests-limit": limit,
        "x-ratelimit-requests-remaining": remaining,
        "x-ratelimit-requests-reset": reset,
    }
    return {"content-type": "application/json"} | {k: v for k, v in named.items() if v is not None}


def test_the_observation_reads_the_three_counters_with_the_reset_as_seconds_from_arrival() -> None:
    page = fixture_page("jsearch", "search_v2_page1", SPEC, fetched_at=T)
    assert adapter().observation(page) == Observation(
        observed_at=T,
        status=200,
        reset_at=T + timedelta(seconds=2462649),
        limit=200,
        remaining=198,
    )


def test_a_429_is_observed_with_its_counters() -> None:
    page = fixture_page("jsearch", "search_v2_429", SPEC, fetched_at=T)
    assert adapter().observation(page) == Observation(
        observed_at=T, status=429, reset_at=T + timedelta(seconds=2462649), limit=200, remaining=0
    )


def test_a_429_without_counters_is_still_observed_but_a_response_without_counters_is_not() -> None:
    a = adapter()
    assert a.observation(doc_page({"message": "slow down"}, status=429, headers={})) == Observation(
        observed_at=T, status=429
    )
    gateway = fixture_page("jsearch", "search_legacy_404", SPEC, fetched_at=T)
    assert a.observation(gateway) is None
    assert a.observation(doc_page({}, headers={})) is None


@pytest.mark.parametrize(
    "headers, expected",
    [
        (counters("200", "150", None), Observation(T, 200, None, 200, 150)),
        (counters(None, None, "3600"), Observation(T, 200, T + timedelta(hours=1), None, None)),
        (counters("abc", "-3", "1.5"), None),
        (counters("200", "150", str(10**30)), Observation(T, 200, None, 200, 150)),
    ],
)
def test_a_counter_that_is_missing_or_malformed_is_left_out(
    headers: dict[str, str], expected: Observation | None
) -> None:
    assert adapter().observation(doc_page({}, headers=headers)) == expected


# ---- settlement -------------------------------------------------------------------------------------


def outcome_of(status: int, **headers: str) -> HttpOutcome:
    return HttpOutcome(doc_page({}, status=status, headers=headers), None)


@pytest.mark.parametrize("status", [200, 201, 204, 299])
def test_a_2xx_is_billed_even_when_it_is_empty_or_its_body_was_cut(status: int) -> None:
    a = adapter()
    assert a.settle(outcome_of(status)) == ("billed", None)
    cut = HttpOutcome(doc_page({}, status=status), ContractViolation("body_too_large", status=status))
    assert a.settle(cut) == ("billed", None)
    empty = fixture_page("jsearch", "search_v2_empty", SPEC, fetched_at=T)
    assert a.settle(HttpOutcome(empty, None)) == ("billed", None)


@pytest.mark.parametrize("status", [301, 302, 400, 401, 403, 404, 408, 422, 429, 500, 502, 503, 504])
def test_every_other_status_after_dispatch_is_ambiguous(status: int) -> None:
    assert adapter().settle(outcome_of(status)) == ("ambiguous", None)


@pytest.mark.parametrize("marker", ["True", "true", " TRUE "])
def test_only_a_gateway_404_is_not_billed(marker: str) -> None:
    a = adapter()
    assert a.settle(outcome_of(404, **{"x-rapidapi-proxy-response": marker})) == (
        "not_billed",
        "rapidapi_gateway_404",
    )
    # the marker on another status, or a false marker, is no evidence
    assert a.settle(outcome_of(500, **{"x-rapidapi-proxy-response": marker})) == ("ambiguous", None)
    assert a.settle(outcome_of(404, **{"x-rapidapi-proxy-response": "false"})) == ("ambiguous", None)
    assert a.settle(outcome_of(404)) == ("ambiguous", None)


def test_only_a_connect_error_before_any_byte_is_not_billed() -> None:
    a = adapter()
    refused = HttpOutcome(None, SourceError("transient", code="connect", before_send=True))
    assert a.settle(refused) == ("not_billed", "connect_failed_before_send")
    for error in (
        SourceError("transient", code="connect", before_send=False),  # a connect error on a redirect hop
        SourceError("ambiguous", code="timeout"),
        SourceError("ambiguous", code="deadline"),
        SourceError("ambiguous", code="transport"),
    ):
        assert a.settle(HttpOutcome(None, error)) == ("ambiguous", None)


async def test_a_refused_connection_and_a_timeout_settle_through_the_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["query"].startswith("refused"):
            raise httpx.ConnectError("refused")
        raise httpx.ReadTimeout("slow")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        a = JSearchAdapter(client, api_key=b"k", instance_id=1, clock=lambda: T)
        spec = a.plan(make_cell_plan())
        refused = await a.fetch(replace(spec, params={**spec.params, "query": "refused"}))
        lost = await a.fetch(spec)
    assert a.settle(refused) == ("not_billed", "connect_failed_before_send")
    assert a.settle(lost) == ("ambiguous", None)


# ---- fetch ------------------------------------------------------------------------------------------


async def test_fetch_asks_search_v2_and_adds_the_cursor_at_send_time(tmp_path: Path) -> None:
    log = tmp_path / "served.jsonl"
    async with httpx.AsyncClient(transport=fixture_transport(FIXTURES, log_path=log)) as client:
        a = JSearchAdapter(client, api_key=b"k", instance_id=1, clock=lambda: T)
        first_spec = a.plan(make_cell_plan(bucket="3days"))
        first = await a.fetch(first_spec)
        second_spec = a.next_page(first_spec, "CUR-1")
        assert second_spec is not None
        second = await a.fetch(second_spec)
    served = served_requests(log)
    assert [(s["method"], s["host"], s["path"], s["case"]) for s in served] == [
        ("GET", "jsearch.p.rapidapi.com", "/search-v2", "search_v2_page1"),
        ("GET", "jsearch.p.rapidapi.com", "/search-v2", "search_v2_page2"),
    ]
    first_query = httpx.QueryParams(served[0]["query"])
    assert dict(first_query) == {
        "query": "python developer in Berlin, Germany",
        "country": "de",
        "num_pages": "1",
        "date_posted": "3days",
    }
    assert dict(httpx.QueryParams(served[1]["query"])) == {**dict(first_query), "cursor": "CUR-1"}
    assert first.page is not None and second.page is not None and first.error is None and second.error is None
    a.check_envelope(second.page)
    assert len(a.parse(second.page)) == 9 and a.settle(second) == ("billed", None)


async def test_fetch_never_retries_and_sends_one_request() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, json={"message": "Too many requests"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        a = JSearchAdapter(client, api_key=b"k", instance_id=1, clock=lambda: T)
        out = await a.fetch(a.plan(make_cell_plan()))
    assert len(calls) == 1 and out.page is not None and out.page.status == 429
    assert calls[0].headers["x-rapidapi-host"] == "jsearch.p.rapidapi.com"
    assert calls[0].url.path == "/search-v2"


def test_the_adapter_does_not_show_its_key() -> None:
    a = JSearchAdapter(httpx.AsyncClient(), api_key=b"TOPSECRETKEY", instance_id=1, clock=lambda: T)
    assert "TOPSECRET" not in repr(a)
