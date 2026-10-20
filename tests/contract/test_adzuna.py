import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from jobhunter.normalize.keys import AdKey
from jobhunter.sources.adzuna import PAGE_CAP, RESULTS_PER_PAGE, AdzunaAdapter
from jobhunter.sources.base import (
    Capabilities,
    Continuation,
    ContractViolation,
    DuplicateInPage,
    Parsed,
    RateLimit,
    RawPage,
    Rejected,
    SourceAdapter,
    SourceError,
)
from jobhunter.sources.fixtures import fixture_transport
from jobhunter.sources.http import HttpOutcome
from jobhunter.sources.parsing.dates import response_date

from tests.support.sources import FIXTURES, fixture_page, make_cell_plan, served_requests
from tests.unit.test_sources_base import SPEC

pytestmark = pytest.mark.contract
T = datetime(2026, 10, 16, 9, 47, tzinfo=UTC)


def adapter() -> AdzunaAdapter:
    return AdzunaAdapter(httpx.AsyncClient(), app_id=b"i", app_key=b"k", instance_id=1, clock=lambda: T)


def adzuna_cell(**changes: Any) -> Any:
    return make_cell_plan(**{"source_id": "adzuna", "bucket": None, "max_days_old": 3, **changes})


def test_rpp100_is_capped_at_50_and_everything_is_partial() -> None:
    a, page = adapter(), fixture_page("adzuna", "search_p1_rpp100", SPEC, fetched_at=T)
    a.check_envelope(page)
    out = a.parse(page)
    parsed = [o.normalized for o in out if isinstance(o, Parsed)]
    assert (
        len(out) == 50 and len(parsed) >= 48 and all(p.description_completeness == "partial" for p in parsed)
    )
    assert sum(len(p.description_text) <= 500 for p in parsed) == len(parsed)
    assert all(p.posted_precision == "ingest" and p.item_ts == p.posted_at for p in parsed)
    assert all(
        p.primary_link is None
        and p.outbound_url_private is not None
        and p.raw_keys == (AdKey("adzuna", p.native_id),)
        for p in parsed
    )
    # The brief asserts a `predicted` salary here; V18 recorded none (`salary_is_predicted` is "0" on all 50
    # results, 8 of them with stated bounds), so the recording asserts the stated ones and
    # test_salary_bounds_are_eur_per_year_and_predicted_when_adzuna_says_so the predicted mapping.
    salaries = [p.salary for p in parsed if p.salary is not None]
    assert len(salaries) == 8 and all(
        s.kind == "stated" and (s.currency, s.period) == ("EUR", "year") for s in salaries
    )
    assert all(c.resolution == "unique" for p in parsed for c in p.locations if p.locations)
    # re-ingests have different ids: distinct postings, grouped later (§8.7)
    assert sum(isinstance(o, DuplicateInPage) for o in out) == 0
    # the recording holds more than one page (V18)
    doc = page.json()
    assert isinstance(doc, dict) and isinstance(doc["count"], int) and doc["count"] > 50
    assert a.continuation(page) == (Continuation.CURSOR, "2")


@pytest.mark.parametrize(
    "case, count",
    [
        ("search_p1_rpp100", 50),
        ("search_p1_days1", 10),
        ("search_p1_distance10", 50),
        ("search_p1_distance50", 50),
        ("adzuna_reingest_pair", 50),
        ("search_empty", 0),
    ],
)
def test_every_recording_parses_whole(case: str, count: int) -> None:
    # tests/contract/README.md: 0 rejected items in every Adzuna file
    a, page = adapter(), fixture_page("adzuna", case, SPEC, fetched_at=T)
    a.check_envelope(page)
    out = a.parse(page)
    assert len(out) == count and [o.item_index for o in out] == list(range(count))
    assert not any(isinstance(o, Rejected) for o in out) and a.peek_native_ids(page) == [
        o.native_id for o in out
    ]
    recorded_at = response_date(page.headers, T)  # the recording's `Date` header
    assert all(o.item_ts is not None and o.item_ts <= recorded_at for o in out)


def test_the_reingest_pair_is_two_postings() -> None:
    page = fixture_page("adzuna", "adzuna_reingest_pair", SPEC, fetched_at=T)
    out = adapter().parse(page)
    first, last = out[0], out[-1]
    assert isinstance(first, Parsed) and isinstance(last, Parsed)
    assert (first.native_id, last.native_id) == ("5933514068", "5933514069")
    assert first.normalized.title_raw == last.normalized.title_raw and not any(
        isinstance(o, DuplicateInPage) for o in out
    )


def test_settlement_bills_every_status() -> None:
    a = adapter()
    assert a.settle(HttpOutcome(fixture_page("adzuna", "search_401", SPEC, fetched_at=T), None)) == (
        "billed",
        None,
    )
    assert a.settle(HttpOutcome(None, SourceError("transient", code="connect", before_send=True))) == (
        "not_billed",
        "connect_failed_before_send",
    )
    page = fixture_page("adzuna", "search_p1_rpp100", SPEC, fetched_at=T)
    for status in (200, 404, 429, 500, 503):
        assert a.settle(HttpOutcome(replace(page, status=status), None)) == ("billed", None)
    # dispatched and lost, cut off, or connected on a redirect hop: Adzuna counted the hit
    for error in (
        SourceError("ambiguous", code="timeout"),
        SourceError("ambiguous", code="transport"),
        SourceError("transient", code="connect", before_send=False),
    ):
        assert a.settle(HttpOutcome(None, error)) == ("billed", None)
    assert a.settle(HttpOutcome(page, ContractViolation("body_too_large", status=200))) == ("billed", None)


def test_plan_params_and_remote_cell() -> None:
    a = adapter()
    cell = adzuna_cell()
    spec = a.plan(cell)
    assert dict(spec.params) == {
        "what": "python developer",
        "where": "Berlin",
        "distance": "25",
        "max_days_old": "3",
        "sort_by": "date",
        "results_per_page": "50",
    }
    assert (spec.page_no, spec.cursor_in, spec.window, spec.request_class) == (1, None, cell.window, "normal")
    remote = a.plan(
        adzuna_cell(query_text="python", location_key="remote-de", location_label=None, radius_km=None)
    )
    assert remote.params["what"] == "python remote" and "where" not in remote.params
    assert "distance" not in remote.params  # a distance measures from a `where`
    for planned in (spec, remote):
        assert not {"app_id", "app_key"} & set(planned.params)
        assert "app_id" not in json.dumps(planned.to_json()) and "app_key" not in json.dumps(
            planned.to_json()
        )


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"max_days_old": None}, "max_days_old"),
        ({"max_days_old": 0}, "max_days_old"),
        ({"query_text": None}, "query"),
        ({"query_text": ""}, "query"),
        ({"location_label": None}, "city"),
        ({"radius_km": None}, "radius"),
        ({"source_id": "jsearch"}, "adzuna"),
        ({"instance_id": 2}, "instance"),
    ],
)
def test_a_cell_that_cannot_be_asked_for_is_refused(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        adapter().plan(adzuna_cell(**changes))


def test_next_page_is_the_page_the_cursor_names_as_a_normal_request() -> None:
    a = adapter()
    first = a.plan(adzuna_cell(request_class="probe"))
    second = a.next_page(first, "2")
    assert second is not None and (second.page_no, second.cursor_in, second.request_class) == (
        2,
        None,
        "normal",
    )
    assert second.params == first.params and second.window == first.window
    assert a.next_page(first, None) is None and a.next_page(first, "") is None
    for cursor in ("0", "-2", "x"):
        with pytest.raises(ValueError):
            a.next_page(first, cursor)


def results_page(results: Any, *, count: Any = 516, page_no: int = 1) -> RawPage:
    doc = {
        "__CLASS__": "Adzuna::API::Response::JobSearchResults",
        "count": count,
        "mean": 1.0,
        "results": results,
    }
    body = json.dumps(doc).encode()
    return RawPage(replace(SPEC, page_no=page_no), 200, {"content-type": "application/json"}, body, T)


def test_continuation_ends_on_a_short_page_or_at_the_count() -> None:
    a = adapter()
    full = json.loads(fixture_page("adzuna", "search_p1_rpp100", SPEC, fetched_at=T).body)["results"]
    assert len(full) == RESULTS_PER_PAGE == 50
    assert a.continuation(results_page(full, count=516, page_no=2)) == (Continuation.CURSOR, "3")
    assert a.continuation(results_page(full, count=150, page_no=3)) == (
        Continuation.END,
        None,
    )  # 3 * 50 >= 150
    assert a.continuation(results_page(full, count=151, page_no=3)) == (Continuation.CURSOR, "4")
    assert a.continuation(results_page(full[:49], count=516)) == (Continuation.END, None)
    assert a.continuation(fixture_page("adzuna", "search_empty", replace(SPEC, page_no=2), fetched_at=T)) == (
        Continuation.END,
        None,
    )
    assert PAGE_CAP == 3


@pytest.mark.parametrize(
    "doc",
    [
        [],
        {"count": 1},
        {"results": []},
        {"results": {}, "count": 1},
        {"results": [], "count": "1"},
        {"results": [], "count": True},
        {"results": [], "count": -1},
        {"results": [], "count": 1.5},
    ],
)
def test_a_broken_envelope_is_a_contract_violation_on_every_read_path(doc: Any) -> None:
    page = RawPage(SPEC, 200, {"content-type": "application/json"}, json.dumps(doc).encode(), T)
    a = adapter()
    for read in (a.check_envelope, a.peek_native_ids, a.parse, a.continuation):
        with pytest.raises(ContractViolation):
            read(page)


def test_a_body_that_is_not_json_is_a_contract_violation() -> None:
    page = RawPage(SPEC, 200, {"content-type": "text/html"}, b"<html>502 Bad Gateway</html>", T)
    with pytest.raises(ContractViolation, match="not_json"):
        adapter().check_envelope(page)


def test_adzunas_own_error_report_with_a_2xx_is_a_failed_attempt_not_a_broken_contract() -> None:
    # T9-a: the documented exception object (the `search_401` body), delivered with a 200
    page = replace(fixture_page("adzuna", "search_401", SPEC, fetched_at=T), status=200)
    a = adapter()
    for read in (a.check_envelope, a.peek_native_ids, a.parse, a.continuation):
        with pytest.raises(SourceError) as raised:
            read(page)
        assert not isinstance(raised.value, ContractViolation)
        assert (raised.value.kind, raised.value.code, raised.value.status) == (
            "transient",
            "provider_error",
            200,
        )
        assert "Authorisation" not in str(raised.value)


async def test_fetch_adds_the_keys_at_send_time_only_and_logs_none(
    tmp_path: Path, log_events: Callable[[], list[dict[str, Any]]]
) -> None:
    log, sent = tmp_path / "served.jsonl", []

    async def capture(request: httpx.Request) -> None:
        sent.append(request)

    transport = fixture_transport(FIXTURES, log_path=log)
    async with httpx.AsyncClient(transport=transport, event_hooks={"request": [capture]}) as client:
        a = AdzunaAdapter(
            client, app_id=b"TOPSECRETID-0123", app_key=b"TOPSECRETKEY-abcdef", instance_id=1, clock=lambda: T
        )
        spec = a.plan(adzuna_cell())
        first = await a.fetch(spec)
        second_spec = a.next_page(spec, "2")
        assert second_spec is not None
        second = await a.fetch(second_spec)
    assert [r.url.path for r in sent] == ["/v1/api/jobs/de/search/1", "/v1/api/jobs/de/search/2"]
    assert all((r.method, r.url.scheme, r.url.host) == ("GET", "https", "api.adzuna.com") for r in sent)
    assert dict(sent[0].url.params) == {
        **spec.params,
        "app_id": "TOPSECRETID-0123",
        "app_key": "TOPSECRETKEY-abcdef",
    }
    assert first.page is not None and second.page is not None and first.page.fetched_at == T
    assert [s["case"] for s in served_requests(log)] == ["adzuna_reingest_pair", "search_empty"]
    assert "TOPSECRET" not in log.read_text() and "TOPSECRET" not in json.dumps(log_events())
    assert "TOPSECRET" not in json.dumps(spec.to_json()) and "TOPSECRET" not in repr(a)
    assert a.settle(first) == ("billed", None) and a.continuation(second.page) == (Continuation.END, None)


async def test_a_refused_connection_is_not_billed_and_a_timeout_is() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/1"):
            raise httpx.ConnectError("refused")
        raise httpx.ReadTimeout("slow")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        a = AdzunaAdapter(client, app_id=b"i", app_key=b"k", instance_id=1, clock=lambda: T)
        spec = a.plan(adzuna_cell())
        refused = await a.fetch(spec)
        lost = await a.fetch(replace(spec, page_no=2))
    assert a.settle(refused) == ("not_billed", "connect_failed_before_send")
    assert a.settle(lost) == ("billed", None)


def test_capabilities_and_observation() -> None:
    a = adapter()
    assert a.capabilities() == Capabilities(
        "adzuna", "quota", 1, "page", 50, "ingest", "partial", False, 1, [RateLimit("adzuna", 1.0, 1)]
    )
    assert a.observation(fixture_page("adzuna", "search_p1_rpp100", SPEC, fetched_at=T)) is None
    conforming: SourceAdapter = a  # checked by mypy: the protocol of Deviation 25
    assert conforming.capabilities().source_id == "adzuna"


# ---- the items of a page ----------------------------------------------------------------------------

REDIRECT = "https://www.adzuna.de/details/5933514068?utm_medium=scrubbed&utm_source=scrubbed"
ITEM: dict[str, Any] = {
    "__CLASS__": "Adzuna::API::Response::Job",
    "id": "5933514068",
    "title": "Senior Software Engineer (f/m/d)",
    "company": {"__CLASS__": "Adzuna::API::Response::Company", "display_name": "Holoplot"},
    "created": "2026-10-15T18:42:17Z",
    "description": "The product you will work on is a new type of sound system. " * 8 + "…",
    "redirect_url": REDIRECT,
    "adref": "[token:1]",
    "latitude": 52.43792,
    "longitude": 13.38046,
    "location": {"display_name": "Mariendorf, Berlin", "area": ["Deutschland", "Berlin", "Mariendorf"]},
    "category": {"label": "IT-Stellen", "tag": "it-jobs"},
    "contract_time": "full_time",
    "salary_is_predicted": "0",
}


def item(**changes: Any) -> dict[str, Any]:
    return {**ITEM, **changes}


def without(*keys: str) -> dict[str, Any]:
    return {key: value for key, value in ITEM.items() if key not in keys}


def parsed_one(job: dict[str, Any]) -> Any:
    (outcome,) = adapter().parse(results_page([job], count=1))
    assert isinstance(outcome, Parsed), outcome
    return outcome.normalized


def test_identity_is_the_id_and_the_redirect_url_is_only_the_private_outbound_link() -> None:
    posting = parsed_one(ITEM)
    assert posting.native_id == "5933514068" and posting.raw_keys == (AdKey("adzuna", "5933514068"),)
    assert (
        posting.outbound_url_private == REDIRECT
        and posting.primary_link is None
        and posting.publisher is None
    )
    assert (posting.title_raw, posting.company_raw) == ("Senior Software Engineer (f/m/d)", "Holoplot")
    assert parsed_one(item(id=5933514068)).native_id == "5933514068"
    assert parsed_one(without("redirect_url")).outbound_url_private is None
    # the redirect's own ad id never becomes a key, whatever the item's id
    other = parsed_one(item(id="77", redirect_url="https://www.adzuna.de/details/99?x=1"))
    assert other.raw_keys == (AdKey("adzuna", "77"),) and "99" not in json.dumps(
        [k.value for k in other.raw_keys]
    )


def test_created_is_the_ingestion_time() -> None:
    posting = parsed_one(ITEM)
    assert posting.posted_at == datetime(2026, 10, 15, 18, 42, 17, tzinfo=UTC) == posting.item_ts
    assert (posting.posted_precision, posting.posted_raw) == ("ingest", "2026-10-15T18:42:17Z")
    for unusable in (None, 17, "yesterday"):
        posting = parsed_one(item(created=unusable))
        assert (posting.posted_at, posting.posted_precision, posting.item_ts) == (None, "unknown", None)


def test_the_description_is_partial_text_through_the_quality_gate() -> None:
    posting = parsed_one(ITEM)
    assert posting.description_completeness == "partial" and posting.description_quality == "ok"
    # Adzuna's cut mark is dropped: the stored text stays within the 500 characters Adzuna sent
    assert posting.description_html_sanitized is None and posting.description_text.endswith("sound system.")
    assert len(posting.description_text) < len(ITEM["description"])
    short = parsed_one(item(description="Python."))
    assert (short.description_completeness, short.description_quality) == ("partial", "garbage")


def test_salary_bounds_are_eur_per_year_and_predicted_when_adzuna_says_so() -> None:
    stated = parsed_one(item(salary_min=60000, salary_max=86000))
    assert stated.salary is not None and (stated.salary.min, stated.salary.max, stated.salary.kind) == (
        60000,
        86000,
        "stated",
    )
    assert (stated.salary.currency, stated.salary.period, stated.salary.annual_eur_max) == (
        "EUR",
        "year",
        86000,
    )
    predicted = parsed_one(item(salary_min=51234.56, salary_max=51234.56, salary_is_predicted="1"))
    assert predicted.salary is not None and predicted.salary.kind == "predicted"
    assert parsed_one(item(salary_min=40000, salary_is_predicted=1)).salary.kind == "predicted"
    assert parsed_one(item(salary_min=40000, salary_is_predicted=True)).salary.kind == "stated"
    assert (
        parsed_one(ITEM).salary is None
        and parsed_one(item(salary_min="60000", salary_max=None)).salary is None
    )
    assert parsed_one(item(salary_min=0, salary_max=False)).salary is None


def test_coordinates_are_one_unique_location_and_the_label_the_fallback() -> None:
    posting = parsed_one(ITEM)
    (location,) = posting.locations
    assert (location.label, location.resolution, posting.location_kind) == (
        "Mariendorf, Berlin",
        "unique",
        "city",
    )
    (candidate,) = location.candidates
    assert (candidate.country_code, candidate.lat, candidate.lon) == ("DE", 52.43792, 13.38046)
    text = parsed_one(item(latitude=None, longitude=None, location={"display_name": "Berlin, Deutschland"}))
    assert [(loc.label, loc.resolution) for loc in text.locations] == [("Berlin, Deutschland", "unique")]
    off_the_map = parsed_one(item(latitude=95.0, location={"display_name": "Berlin"}))
    assert [loc.resolution for loc in off_the_map.locations] == ["unique"]
    nothing = parsed_one(without("latitude", "longitude", "location"))
    assert (nothing.locations, nothing.location_kind) == ([], "unknown")


def test_employment_industry_and_work_mode() -> None:
    posting = parsed_one(item(contract_type="contract"))
    assert posting.employment_types == ("full_time", "contract") and posting.employment_source == "structured"
    assert posting.source_structured == {"industry": "it-jobs"}
    assert posting.work_mode_possible == {"remote", "hybrid", "onsite"} and posting.work_mode_source == "none"
    plain = parsed_one(item(contract_time=None, category={"tag": "unknown", "label": "Unbekannt"}))
    assert (plain.employment_types, plain.employment_source, plain.source_structured) == ((), "none", {})
    assert parsed_one(without("category")).source_structured == {}


def test_item_failures_reject_the_item_and_never_the_page() -> None:
    a = adapter()
    jobs: list[Any] = [
        item(id="1"),
        "not an object",
        without("id"),
        item(id=""),
        item(id=True),
        item(id=1.5),
        item(id="2", title=None),
        item(id="3", company={}),
        item(id="4", company="Holoplot"),
        item(id="5"),
    ]
    page = results_page(jobs)
    a.check_envelope(page)
    outcomes = a.parse(page)
    assert [o.item_index for o in outcomes] == list(range(10))
    assert [type(o) for o in outcomes] == [Parsed] + [Rejected] * 8 + [Parsed]
    rejected = [o for o in outcomes if isinstance(o, Rejected)]
    assert {o.reason_code for o in rejected} == {"item_contract"} and all(o.detail for o in rejected)
    assert [o.native_id for o in rejected] == [None, None, None, None, None, "2", "3", "4"]
    assert all("adzuna.de" not in o.detail for o in rejected)
    assert a.peek_native_ids(page) == ["1", None, None, None, None, None, "2", "3", "4", "5"]


def test_a_repeated_id_is_a_duplicate_of_its_first_occurrence() -> None:
    outcomes = adapter().parse(results_page([item(id="1"), item(id="2"), item(id="1", title="Other")]))
    assert [type(o) for o in outcomes] == [Parsed, Parsed, DuplicateInPage]
    assert isinstance(outcomes[2], DuplicateInPage) and outcomes[2].first_index == 0
