import base64
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from jobhunter.normalize.keys import AdKey
from jobhunter.normalize.urls import canonical_url
from jobhunter.quota.windows import AccountObservation
from jobhunter.sources.base import (
    Continuation,
    ContractViolation,
    DuplicateInPage,
    Parsed,
    RawPage,
    Rejected,
    SourceError,
)
from jobhunter.sources.fixtures import fixture_transport
from jobhunter.sources.http import HttpOutcome
from jobhunter.sources.jsearch import JSearchAdapter
from jobhunter.sources.parsing.dates import parse_relative
from jobhunter.sources.serpapi import SERPAPI_HL, SerpApiAdapter

from tests.support.sources import FIXTURES, fixture_page, load_fixture, make_cell_plan, served_requests
from tests.unit.test_sources_base import SPEC

pytestmark = pytest.mark.contract
T = datetime(2026, 10, 16, 9, 47, tzinfo=UTC)
RECORDED_DATE = datetime(2026, 10, 18, 20, 50, 48, tzinfo=UTC)  # the Date header of google_jobs_hl_en


def adapter() -> SerpApiAdapter:
    return SerpApiAdapter(httpx.AsyncClient(), api_key=b"k", instance_id=1, clock=lambda: T)


def test_hl_en_page_contract_and_v3_overlap() -> None:
    a, page = adapter(), fixture_page("serpapi", "google_jobs_hl_en", SPEC, fetched_at=T)
    a.check_envelope(page)
    outcomes = a.parse(page)
    ids = a.peek_native_ids(page)
    assert len(outcomes) == 10 and all(len(i) >= 20 for i in ids if i)
    assert a.continuation(page)[0] is Continuation.CURSOR
    parsed = [o.normalized for o in outcomes if isinstance(o, Parsed)]
    assert all(p.native_id == uid for p, uid in zip(parsed, ids, strict=False))
    assert all(p.raw_keys[0] == AdKey("htidocid", p.native_id) for p in parsed)
    via_links = [p.primary_link for p in parsed]
    assert all(link is None or "utm_" not in link for link in via_links)
    js = JSearchAdapter(httpx.AsyncClient(), api_key=b"k", instance_id=1, clock=lambda: T)
    jsearch_ids = set(js.peek_native_ids(fixture_page("jsearch", "search_v2_page1", SPEC, fetched_at=T)))
    # the V3 decision record (docs/spikes/V3.md): one outcome per recording
    assert SERPAPI_HL == ("en" if len(set(ids) & jsearch_ids) >= 3 else "de")


def recorded_doc() -> dict[str, Any]:
    _, _, body = load_fixture("serpapi", "google_jobs_hl_en")
    doc: dict[str, Any] = json.loads(body)
    return doc


def page_of(doc: Any, *, headers: dict[str, str] | None = None, status: int = 200) -> RawPage:
    sent = {"content-type": "application/json; charset=utf-8"} if headers is None else headers
    return RawPage(SPEC, status, sent, json.dumps(doc).encode(), T)


def test_primary_link_is_the_via_option_only() -> None:
    # `via` is the publisher the ad was found on; it is listed third, behind two other publishers' links
    options = [
        {"title": "LinkedIn", "link": "https://de.linkedin.com/jobs/view/python-dev-at-acme-4479346594"},
        {"title": "Indeed", "link": "https://de.indeed.com/viewjob?jk=bfe45906d63eb7a6&utm_source=scrubbed"},
        {
            "title": "Stepstone",
            "link": "https://www.stepstone.de/stellenangebote--Python-Dev-Berlin-ACME--14625102-inline.html"
            "?utm_campaign=scrubbed&utm_source=scrubbed&utm_medium=scrubbed",
        },
        {"title": "XING", "link": "https://www.xing.com/jobs/berlin-python-dev-123456789"},
    ]
    doc = recorded_doc()
    doc["jobs_results"][0] = {**doc["jobs_results"][0], "via": "Stepstone", "apply_options": options}
    outcomes = adapter().parse(page_of(doc))
    assert isinstance(outcomes[0], Parsed)
    posting = outcomes[0].normalized
    assert (
        posting.primary_link
        == "https://stepstone.de/stellenangebote--Python-Dev-Berlin-ACME--14625102-inline.html"
    )
    assert posting.publisher == "Stepstone"
    # the keys are the htidocid and the via option's: no LinkedIn, Indeed or XING key, whatever the links hold
    assert posting.raw_keys == (AdKey("htidocid", posting.native_id), AdKey("pub:stepstone.de", "14625102"))
    assert (
        "4479346594" not in posting.model_dump_json() and "bfe45906d63eb7a6" not in posting.model_dump_json()
    )


def opts(*titles: str) -> list[dict[str, str]]:
    return [
        {"title": t, "link": f"https://example.org/{i}?utm_source=x&id={i}"} for i, t in enumerate(titles)
    ]


@pytest.mark.parametrize(
    "via, titles, expected",
    [
        ("Stepstone", ("A", "B", "Stepstone"), "https://example.org/2?id=2"),
        ("via Stepstone", ("A", "Stepstone"), "https://example.org/1?id=1"),
        ("VIA stepstone", ("A", "STEPSTONE"), "https://example.org/1?id=1"),
        ("  Via   Stepstone ", ("A", "Stepstone"), "https://example.org/1?id=1"),
        ("Stepstone", ("A", "B"), "https://example.org/0?id=0"),  # no option is the via one: the first
        ("Stepstone", ("Stepstone jobs", "Stepstone"), "https://example.org/1?id=1"),  # equal, not a prefix
        (None, ("A", "B"), "https://example.org/0?id=0"),
    ],
)
def test_the_via_option_is_matched_case_insensitively_after_dropping_a_via_prefix(
    via: str | None, titles: tuple[str, ...], expected: str
) -> None:
    doc = recorded_doc()
    job = {**doc["jobs_results"][0], "apply_options": opts(*titles)}
    job.pop("via")
    if via is not None:
        job["via"] = via
    doc["jobs_results"] = [job]
    (outcome,) = adapter().parse(page_of(doc))
    assert isinstance(outcome, Parsed) and outcome.normalized.primary_link == expected


def test_an_item_without_apply_options_has_no_primary_link_and_only_its_htidocid() -> None:
    doc = recorded_doc()
    doc["jobs_results"] = [{k: v for k, v in doc["jobs_results"][0].items() if k != "apply_options"}]
    (outcome,) = adapter().parse(page_of(doc))
    assert isinstance(outcome, Parsed) and outcome.normalized.primary_link is None
    assert outcome.normalized.raw_keys == (AdKey("htidocid", outcome.native_id),)


def test_empty_results_are_a_page_not_a_violation_and_account_is_free() -> None:
    a, page = adapter(), fixture_page("serpapi", "google_jobs_empty", SPEC, fetched_at=T)
    a.check_envelope(page)
    assert a.parse(page) == [] and a.peek_native_ids(page) == []
    assert a.continuation(page) == (Continuation.END, None)
    assert a.settle(HttpOutcome(page, None)) == ("billed", None)  # empty results are billed


async def test_account_reads_the_recorded_fields_for_free(tmp_path: Path) -> None:
    log = tmp_path / "served.jsonl"
    async with httpx.AsyncClient(transport=fixture_transport(FIXTURES, log_path=log)) as client:
        a = SerpApiAdapter(client, api_key=b"TOPSECRETKEY", instance_id=1, clock=lambda: T)
        account = await a.account()
    assert account == AccountObservation(
        observed_at=T,
        plan_renewal_date=datetime(2026, 11, 8, tzinfo=UTC),
        searches_per_month=250,
        plan_searches_left=248,
        this_hour_searches=0,
        account_rate_limit_per_hour=250,
    )
    (served,) = served_requests(log)
    assert (served["method"], served["host"], served["path"]) == ("GET", "serpapi.com", "/account.json")
    assert served["case"] == "account_before" and "TOPSECRET" not in served["query"]


def test_missing_date_header_falls_back_to_fetched_at() -> None:
    a, page = adapter(), fixture_page("serpapi", "google_jobs_hl_en", SPEC, fetched_at=T)
    assert "date" in page.headers
    undated = replace(page, headers={k: v for k, v in page.headers.items() if k != "date"})
    dated = [o.normalized for o in a.parse(page) if isinstance(o, Parsed)]
    fallback = [o.normalized for o in a.parse(undated) if isinstance(o, Parsed)]
    assert len(fallback) == 10 and all(p.posted_raw is not None for p in fallback)
    for with_header, without in zip(dated, fallback, strict=True):
        assert with_header.posted_raw == without.posted_raw
        assert without.posted_raw is not None
        expected_without = parse_relative(without.posted_raw, reference=T)
        expected_with = parse_relative(without.posted_raw, reference=RECORDED_DATE)
        assert expected_without is not None and expected_with is not None
        assert without.posted_at == expected_without.at and with_header.posted_at == expected_with.at
        assert without.posted_at != with_header.posted_at  # the two references are days apart


# ---- the items of a page ----------------------------------------------------------------------------


def job_id(htidocid: str | None, **extra: Any) -> str:
    blob = {
        "job_title": "x",
        "company_name": "y",
        "gl": "de",
        "hl": "en",
        "fc": "scrubbed",
        "fcv": "3",
        **extra,
    }
    if htidocid is not None:
        blob["htidocid"] = htidocid
    return base64.b64encode(json.dumps(blob).encode()).decode()


LONG = "Wir suchen Verstärkung für unser Backend-Team und freuen uns auf Ihre Bewerbung. " * 4
ITEM: dict[str, Any] = {
    "title": "Python Developer (m/w/d)",
    "company_name": "ACME GmbH",
    "location": "Berlin",
    "via": "Stepstone",
    "description": LONG,
    "extensions": ["2 days ago", "Full-time"],
    "apply_options": [
        {
            "title": "Stepstone",
            "link": "https://www.stepstone.de/stellenangebote--Python-Developer-Berlin-ACME--123456-inline.html"
            "?utm_campaign=scrubbed",
        }
    ],
    "job_id": job_id("UID-1AAAAAAAAAAAAAAAAAAAA=="),
}


def item(**changes: Any) -> dict[str, Any]:
    return {**ITEM, **changes}


def items_page(items: list[Any], *, headers: dict[str, str] | None = None) -> RawPage:
    doc = {"search_metadata": {"status": "Success"}, "jobs_results": items}
    return page_of(doc, headers={"date": "Sun, 18 Oct 2026 20:50:48 GMT"} if headers is None else headers)


def parsed_one(**changes: Any) -> Any:
    (outcome,) = adapter().parse(items_page([item(**changes)]))
    assert isinstance(outcome, Parsed), outcome
    return outcome.normalized


def test_identity_is_the_decoded_htidocid_and_the_fc_token_is_discarded() -> None:
    posting = parsed_one(job_id=job_id("ABCDEFGHIJKLMNOPQRSTUVWX", fc="SESSIONTOKEN", fc_id="X"))
    assert posting.native_id == "ABCDEFGHIJKLMNOPQRSTUVWX"
    assert posting.raw_keys[0] == AdKey("htidocid", "ABCDEFGHIJKLMNOPQRSTUVWX")
    assert "SESSIONTOKEN" not in posting.model_dump_json()
    assert posting.title_raw == "Python Developer (m/w/d)" and posting.company_raw == "ACME GmbH"
    assert posting.description_completeness == "full" and posting.description_html_sanitized is None
    assert posting.salary is None and posting.publisher == "Stepstone" and posting.requisition_id is None


def test_every_recorded_item_parses_with_its_chips() -> None:
    page = fixture_page("serpapi", "google_jobs_hl_en", SPEC, fetched_at=T)
    outcomes = adapter().parse(page)
    assert [o.item_index for o in outcomes] == list(range(10)) and all(
        isinstance(o, Parsed) for o in outcomes
    )
    parsed = [o.normalized for o in outcomes if isinstance(o, Parsed)]
    assert {p.posted_precision for p in parsed} == {"relative_day"}
    assert parsed[0].posted_raw == "2 days ago" and parsed[0].posted_at == datetime(
        2026, 10, 15, 22, tzinfo=UTC
    )
    assert all(p.item_ts == p.posted_at for p in parsed)
    assert all(p.employment_types == ("full_time",) and p.employment_source == "text" for p in parsed)
    assert all(
        p.work_mode_possible == {"remote", "hybrid", "onsite"} and p.work_mode_source == "none"
        for p in parsed
    )
    salaries = [p.salary for p in parsed if p.salary is not None]
    assert [(s.min, s.max, s.currency, s.period) for s in salaries] == [
        (65000.0, 85000.0, "EUR", "year"),
        (90000.0, 130000.0, "EUR", "year"),
    ]
    assert all(p.locations and p.locations[0].source == "serpapi" for p in parsed)
    assert sum(p.description_quality == "ok" for p in parsed) >= 9


def test_german_chips_are_read_too() -> None:
    posting = parsed_one(extensions=["vor 3 Stunden", "Vollzeit", "Homeoffice", "60.000 € pro Jahr"])
    assert posting.posted_precision == "hour" and posting.posted_raw == "vor 3 Stunden"
    assert posting.posted_at == datetime(2026, 10, 18, 17, 50, 48, tzinfo=UTC)
    assert posting.employment_types == ("full_time",) and posting.employment_source == "text"
    assert posting.work_mode_possible == {"remote", "hybrid"} and posting.work_mode_source == "text"
    assert posting.salary is not None and (posting.salary.min, posting.salary.currency) == (60000.0, "EUR")


def test_detected_extensions_win_over_the_chips_when_present() -> None:
    posting = parsed_one(
        extensions=["5 days ago", "Full-time", "€10K a year"],
        detected_extensions={
            "posted_at": "1 day ago",
            "schedule_type": "Part-time",
            "work_from_home": True,
            "salary": "70K\u201380K EUR a year",
        },
    )
    assert posting.posted_raw == "1 day ago" and posting.posted_at == datetime(2026, 10, 16, 22, tzinfo=UTC)
    # the merged employment types are not all from the structured field (the chip adds `full_time`)
    assert posting.employment_types == ("part_time", "full_time") and posting.employment_source == "text"
    assert posting.work_mode_possible == {"remote", "hybrid"} and posting.work_mode_source == "text"
    assert posting.salary is not None and (posting.salary.min, posting.salary.max) == (70000.0, 80000.0)


def test_the_work_from_home_flag_is_text_evidence_like_the_chips() -> None:
    flag = parsed_one(extensions=[], detected_extensions={"work_from_home": True})
    assert flag.work_mode_possible == {"remote", "hybrid"} and flag.work_mode_source == "text"
    chip = parsed_one(extensions=["Work from home"])
    assert chip.work_mode_possible == {"remote", "hybrid"} and chip.work_mode_source == "text"
    for off in ({"work_from_home": False}, {"work_from_home": "true"}, {}):
        other = parsed_one(extensions=[], detected_extensions=off)
        assert other.work_mode_possible == {"remote", "hybrid", "onsite"} and other.work_mode_source == "none"


@pytest.mark.parametrize(
    "chips, schedule, types, source",
    [
        (
            ["Full-time"],
            "Part-time",
            ("part_time", "full_time"),
            "text",
        ),  # a chip adds a type: not all structured
        ([], "Part-time", ("part_time",), "structured"),
        (
            ["Teilzeit"],
            "Part-time",
            ("part_time",),
            "structured",
        ),  # the chip only repeats the structured type
        (["Part-time", "Internship"], "Part-time", ("part_time", "internship"), "text"),
        (["Full-time"], None, ("full_time",), "text"),
        (["Full-time"], "Weekends", ("full_time",), "text"),  # a schedule that names no type adds nothing
        ([], "Weekends", (), "none"),
        ([], None, (), "none"),
    ],
)
def test_employment_is_structured_only_when_every_type_came_from_the_structured_field(
    chips: list[str], schedule: str | None, types: tuple[str, ...], source: str
) -> None:
    detected = None if schedule is None else {"schedule_type": schedule}
    posting = parsed_one(extensions=chips, detected_extensions=detected)
    assert (posting.employment_types, posting.employment_source) == (types, source)


def test_detected_extensions_alone_are_enough() -> None:
    posting = parsed_one(
        extensions=[], detected_extensions={"posted_at": "2 hours ago", "schedule_type": "Contractor"}
    )
    assert (posting.posted_precision, posting.employment_types) == ("hour", ("contract",))


@pytest.mark.parametrize("extensions", [None, [], "2 days ago", [3, None], ["unrelated chip"]])
def test_chips_that_say_nothing_leave_the_fields_unknown(extensions: Any) -> None:
    posting = parsed_one(extensions=extensions)
    assert (posting.posted_at, posting.posted_precision, posting.posted_raw, posting.item_ts) == (
        None,
        "unknown",
        None,
        None,
    )
    assert (posting.employment_types, posting.employment_source, posting.salary) == ((), "none", None)
    assert posting.work_mode_possible == {"remote", "hybrid", "onsite"} and posting.work_mode_source == "none"


def test_a_malformed_detected_extensions_value_is_ignored_not_fatal() -> None:
    for bad in ("text", ["list"], 5):
        assert parsed_one(detected_extensions=bad).posted_raw == "2 days ago"


def test_item_failures_reject_the_item_and_never_the_page() -> None:
    a = adapter()
    items: list[Any] = [
        item(job_id=job_id("A" * 24)),
        "not an object",
        {k: v for k, v in item().items() if k != "job_id"},
        item(job_id=17),
        item(job_id="!!! not base64 !!!"),
        item(job_id=base64.b64encode(b"[1, 2]").decode()),
        item(job_id=job_id(None)),
        item(job_id=job_id("")),
        item(job_id=job_id("B" * 24), title=None),
        item(job_id=job_id("C" * 24), company_name=5),
        item(job_id=job_id("D" * 24)),
    ]
    page = items_page(items)
    a.check_envelope(page)
    outcomes = a.parse(page)
    assert [o.item_index for o in outcomes] == list(range(11))
    assert [type(o) for o in outcomes] == [Parsed] + [Rejected] * 9 + [Parsed]
    rejected = [o for o in outcomes if isinstance(o, Rejected)]
    assert {o.reason_code for o in rejected} == {"item_contract"} and all(o.detail for o in rejected)
    assert [o.native_id for o in rejected] == [None] * 7 + ["B" * 24, "C" * 24]
    assert a.peek_native_ids(page) == ["A" * 24] + [None] * 7 + ["B" * 24, "C" * 24, "D" * 24]


def test_a_repeated_htidocid_is_a_duplicate_of_its_first_occurrence() -> None:
    same = job_id("A" * 24, fc="OTHERSESSION")
    outcomes = adapter().parse(items_page([item(job_id=job_id("A" * 24)), item(job_id=same, title="Other")]))
    assert isinstance(outcomes[0], Parsed) and isinstance(outcomes[1], DuplicateInPage)
    assert (outcomes[1].native_id, outcomes[1].first_index) == ("A" * 24, 0)


def test_a_coordinate_shaped_location_that_is_out_of_range_never_fails_the_job() -> None:
    for text in ("95.10115, 11.5", "99.5, 200.1"):
        (location,) = parsed_one(location=text).locations
        assert (
            location.resolution == "unresolved" and location.candidates == [] and location.postal_code is None
        )


def test_locations_and_their_absence() -> None:
    assert parsed_one(location="Berlin (+2 others)").location_kind == "city"
    assert parsed_one(location="Anywhere").location_kind == "country_wide"
    unknown = parsed_one(location=None)
    assert unknown.location_kind == "unknown" and unknown.locations == []


# ---- the envelope -----------------------------------------------------------------------------------

NO_RESULTS = "Google hasn't returned any results for this query."


@pytest.mark.parametrize(
    "mutate, code",
    [
        (lambda d: d.pop("search_metadata"), "search_metadata_missing"),
        (lambda d: d.update(search_metadata="Success"), "search_metadata_missing"),
        (lambda d: d.pop("jobs_results"), "jobs_results_missing"),
        (lambda d: (d.pop("jobs_results"), d.update(error=None)), "jobs_results_missing"),
        # an error text without `search_metadata.status == "Error"` is not a provider error report
        (lambda d: (d.pop("jobs_results"), d.update(error="Something went wrong.")), "jobs_results_missing"),
        (
            lambda d: (d.pop("jobs_results"), d["search_metadata"].update(status="Error")),
            "jobs_results_missing",  # status Error without an `error` string
        ),
        (
            lambda d: (
                d.pop("jobs_results"),
                d["search_metadata"].update(status="Error"),
                d.update(error=["Your account has run out of searches."]),
            ),
            "jobs_results_missing",  # an `error` that is not a string
        ),
        (
            lambda d: (
                d.pop("jobs_results"),
                d["search_metadata"].update(status=500),
                d.update(error="Your account has run out of searches."),
            ),
            "jobs_results_missing",  # a status that is not the string "Error"
        ),
        (
            lambda d: (d.pop("search_metadata"), d.pop("jobs_results"), d.update(error="Boom")),
            "search_metadata_missing",
        ),
        (lambda d: d.update(jobs_results={"0": {}}), "jobs_results_not_list"),
        (lambda d: d.update(jobs_results=None), "jobs_results_not_list"),
        (lambda d: d.update(serpapi_pagination="next"), "pagination_invalid"),
        (lambda d: d.update(serpapi_pagination={"next_page_token": ""}), "pagination_invalid"),
        (lambda d: d.update(serpapi_pagination={"next_page_token": 3}), "pagination_invalid"),
    ],
)
def test_an_envelope_that_breaks_the_contract_is_a_violation(
    mutate: Callable[[dict[str, Any]], Any], code: str
) -> None:
    doc = recorded_doc()
    mutate(doc)
    with pytest.raises(ContractViolation) as exc:
        adapter().check_envelope(page_of(doc))
    assert (exc.value.kind, exc.value.code, exc.value.status) == ("contract", code, 200)


def test_drift_lists_and_non_json_bodies_are_violations() -> None:
    a = adapter()
    with pytest.raises(ContractViolation, match="search_metadata_missing"):
        a.check_envelope(fixture_page("serpapi", "google_jobs_drift", SPEC, fetched_at=T))
    with pytest.raises(ContractViolation, match="not_object"):
        a.check_envelope(page_of([]))
    with pytest.raises(ContractViolation, match="not_json"):
        a.check_envelope(RawPage(SPEC, 200, {}, b"<html>502 Bad Gateway</html>", T))


def error_page(message: Any, *, status: int = 200, state: str = "Error") -> RawPage:
    return page_of({"search_metadata": {"status": state}, "error": message}, status=status)


@pytest.mark.parametrize("status", [200, 201])
@pytest.mark.parametrize(
    "message", ["Something went wrong.", "Invalid API key. Your API key should be here."]
)
def test_a_provider_error_delivered_with_a_2xx_is_a_transient_failure_not_a_contract_violation(
    message: str, status: int
) -> None:
    a, page = adapter(), error_page(message, status=status)
    with pytest.raises(SourceError) as exc:
        a.check_envelope(page)
    assert not isinstance(exc.value, ContractViolation)
    assert (exc.value.kind, exc.value.code, exc.value.status) == ("transient", "provider_error", status)
    assert message not in str(exc.value)  # the provider's text is never copied into the error
    for read in (a.parse, a.peek_native_ids, a.continuation):  # nothing reads it as an empty page either
        with pytest.raises(SourceError) as again:
            read(page)
        assert (again.value.kind, again.value.code) == ("transient", "provider_error")
    assert a.settle(HttpOutcome(page, None)) == ("billed", None)  # billing is unchanged: a 2xx is billed


def test_running_out_of_searches_at_200_is_the_quota_exhausted_kind() -> None:
    for message in ("Your account has run out of searches.", "YOUR ACCOUNT HAS RUN OUT OF SEARCHES."):
        page = error_page(message)
        with pytest.raises(SourceError) as exc:
            adapter().check_envelope(page)
        assert not isinstance(exc.value, ContractViolation)
        assert (exc.value.kind, exc.value.code, exc.value.status) == (
            "quota_exhausted",
            "provider_error",
            200,
        )
        assert "searches" not in str(exc.value)


def test_a_no_results_error_wins_over_the_error_status() -> None:
    page = error_page(NO_RESULTS)
    adapter().check_envelope(page)
    assert adapter().parse(page) == [] and adapter().continuation(page) == (Continuation.END, None)


@pytest.mark.parametrize("message", ["Aucun résultat pour cette recherche.", "No jobs here, sorry.", None])
def test_google_jobs_fully_empty_is_a_no_results_signal_whatever_the_message_says(
    message: str | None,
) -> None:
    a = adapter()
    doc: dict[str, Any] = {
        "search_metadata": {"status": "Success"},
        "search_information": {"jobs_results_state": "Fully empty"},
    }
    if message is not None:
        doc["error"] = message
    page = page_of(doc)
    a.check_envelope(page)
    assert a.parse(page) == [] and a.peek_native_ids(page) == []
    assert a.continuation(page) == (Continuation.END, None)
    # the structured signal also outranks a status of Error, as the message does
    doc["search_metadata"] = {"status": "Error"}
    doc["error"] = message or "x"
    a.check_envelope(page_of(doc))


@pytest.mark.parametrize(
    "information",
    [{"jobs_results_state": "Results for exact spelling"}, {"jobs_results_state": None}, {}, "Fully empty"],
)
def test_any_other_search_information_is_no_signal(information: Any) -> None:
    page = page_of({"search_metadata": {"status": "Success"}, "search_information": information})
    with pytest.raises(ContractViolation, match="jobs_results_missing"):
        adapter().check_envelope(page)


def test_a_page_without_results_is_valid_only_when_google_says_so() -> None:
    a = adapter()
    for message in (NO_RESULTS, "GOOGLE HASN\u2019T RETURNED ANY RESULTS FOR THIS QUERY."):
        page = page_of({"search_metadata": {"status": "Success"}, "error": message})
        a.check_envelope(page)
        assert a.parse(page) == [] and a.continuation(page) == (Continuation.END, None)


def test_the_end_forms_and_a_token_are_valid_envelopes() -> None:
    a = adapter()
    meta: dict[str, Any] = {"status": "Success"}
    extras: list[dict[str, Any]] = [
        {},
        {"serpapi_pagination": {}},
        {"serpapi_pagination": {"next_page_token": "T"}},
    ]
    for extra in extras:
        a.check_envelope(page_of({"search_metadata": meta, "jobs_results": [], **extra}))


def test_a_next_page_token_continues_and_its_absence_ends() -> None:
    a = adapter()
    assert a.continuation(fixture_page("serpapi", "google_jobs_hl_en", SPEC, fetched_at=T)) == (
        Continuation.CURSOR,
        recorded_doc()["serpapi_pagination"]["next_page_token"],
    )
    base = {"search_metadata": {"status": "Success"}, "jobs_results": [item()]}
    assert a.continuation(page_of(base)) == (Continuation.END, None)
    assert a.continuation(page_of({**base, "serpapi_pagination": {"current": 2}})) == (Continuation.END, None)
    assert a.continuation(page_of({**base, "serpapi_pagination": {"next_page_token": None}})) == (
        Continuation.END,
        None,
    )


def test_a_malformed_token_is_never_read_as_the_end() -> None:
    page = page_of({"search_metadata": {}, "jobs_results": [], "serpapi_pagination": {"next_page_token": 3}})
    with pytest.raises(ContractViolation, match="pagination_invalid"):
        adapter().continuation(page)


# ---- settlement and observation ---------------------------------------------------------------------


def outcome_of(status: int) -> HttpOutcome:
    return HttpOutcome(page_of({}, status=status), None)


@pytest.mark.parametrize("status", [200, 201, 204])
def test_a_2xx_is_billed(status: int) -> None:
    a = adapter()
    assert a.settle(outcome_of(status)) == ("billed", None)
    cut = HttpOutcome(page_of({}, status=status), ContractViolation("body_too_large", status=status))
    assert a.settle(cut) == ("billed", None)


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 408, 429, 500, 502, 503, 504])
def test_every_other_status_is_ambiguous(status: int) -> None:
    assert adapter().settle(outcome_of(status)) == ("ambiguous", None)


def test_the_gateway_404_marker_of_rapidapi_means_nothing_for_serpapi() -> None:
    page = page_of({}, status=404, headers={"x-rapidapi-proxy-response": "true"})
    assert adapter().settle(HttpOutcome(page, None)) == ("ambiguous", None)


def test_only_a_connect_error_before_any_byte_is_not_billed() -> None:
    a = adapter()
    refused = HttpOutcome(None, SourceError("transient", code="connect", before_send=True))
    assert a.settle(refused) == ("not_billed", "connect_failed_before_send")
    for error in (
        SourceError("transient", code="connect", before_send=False),
        SourceError("ambiguous", code="timeout"),
        SourceError("ambiguous", code="deadline"),
        SourceError("ambiguous", code="transport"),
    ):
        assert a.settle(HttpOutcome(None, error)) == ("ambiguous", None)


def test_the_search_response_has_no_observation_because_the_account_read_supplies_it() -> None:
    a = adapter()
    assert a.observation(fixture_page("serpapi", "google_jobs_hl_en", SPEC, fetched_at=T)) is None
    assert a.observation(page_of({}, status=429)) is None


# ---- fetch ------------------------------------------------------------------------------------------


async def test_fetch_adds_the_key_at_send_time_and_the_token_for_a_continuation(
    tmp_path: Path, log_events: Callable[[], list[dict[str, Any]]]
) -> None:
    log, sent = tmp_path / "served.jsonl", []

    async def capture(request: httpx.Request) -> None:
        sent.append(request)

    transport = fixture_transport(FIXTURES, log_path=log)
    async with httpx.AsyncClient(transport=transport, event_hooks={"request": [capture]}) as client:
        a = SerpApiAdapter(client, api_key=b"TOPSECRETKEY-abcdefghijklmnop", instance_id=1, clock=lambda: T)
        spec = a.plan(make_cell_plan(source_id="serpapi", bucket=None))
        assert "api_key" not in spec.params and "TOPSECRET" not in json.dumps(spec.to_json())
        first = await a.fetch(spec)
        next_spec = a.next_page(spec, "TOKEN-1")
        assert next_spec is not None
        second = await a.fetch(next_spec)
    assert [dict(r.url.params) for r in sent] == [
        {
            "q": "python developer Berlin",
            "gl": "de",
            "hl": "en",
            "engine": "google_jobs",
            "api_key": "TOPSECRETKEY-abcdefghijklmnop",
        },
        {
            "q": "python developer Berlin",
            "gl": "de",
            "hl": "en",
            "engine": "google_jobs",
            "next_page_token": "TOKEN-1",
            "api_key": "TOPSECRETKEY-abcdefghijklmnop",
        },
    ]
    assert all((r.method, r.url.host, r.url.path) == ("GET", "serpapi.com", "/search.json") for r in sent)
    assert first.page is not None and second.page is not None and first.error is None
    a.check_envelope(first.page)
    assert a.settle(first) == ("billed", None) and len(a.parse(first.page)) == 10
    served = served_requests(log)
    assert "api_key" not in "".join(s["query"] for s in served) and "TOPSECRET" not in "".join(
        str(e) for e in log_events()
    )
    assert "next_page_token=TOKEN-1" in served[1]["query"]


def test_the_adapter_does_not_show_its_key() -> None:
    a = SerpApiAdapter(httpx.AsyncClient(), api_key=b"TOPSECRETKEY", instance_id=1, clock=lambda: T)
    assert "TOPSECRET" not in repr(a)


async def test_a_refused_connection_and_a_timeout_settle_through_the_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["q"] == "refused":
            raise httpx.ConnectError("refused")
        raise httpx.ReadTimeout("slow")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        a = SerpApiAdapter(client, api_key=b"k", instance_id=1, clock=lambda: T)
        spec = a.plan(make_cell_plan(source_id="serpapi", bucket=None))
        refused = await a.fetch(replace(spec, params={**spec.params, "q": "refused"}))
        lost = await a.fetch(spec)
    assert a.settle(refused) == ("not_billed", "connect_failed_before_send")
    assert a.settle(lost) == ("ambiguous", None)


# ---- the account read -------------------------------------------------------------------------------


def account_adapter(client: httpx.AsyncClient) -> SerpApiAdapter:
    return SerpApiAdapter(client, api_key=b"TOPSECRETKEY", instance_id=1, clock=lambda: T)


def account_body(**changes: Any) -> dict[str, Any]:
    doc = {
        "plan_renewal_date": "2026-11-08",
        "searches_per_month": 250,
        "plan_searches_left": 200,
        "this_hour_searches": 3,
        "account_rate_limit_per_hour": 250,
    }
    return {**doc, **changes}


async def read_account(handler: Callable[[httpx.Request], httpx.Response]) -> AccountObservation:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await account_adapter(client).account()


async def test_the_account_read_sends_only_the_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=account_body())

    account = await read_account(handler)
    assert dict(seen[0].url.params) == {"api_key": "TOPSECRETKEY"} and seen[0].url.path == "/account.json"
    assert (account.plan_searches_left, account.this_hour_searches) == (200, 3)


@pytest.mark.parametrize("renewal", ["2026-11-08", "2026-11-08T00:00:00Z", "2026-11-08T02:00:00+02:00"])
async def test_the_renewal_date_is_a_utc_midnight(renewal: str) -> None:
    account = await read_account(lambda r: httpx.Response(200, json=account_body(plan_renewal_date=renewal)))
    assert account.plan_renewal_date == datetime(2026, 11, 8, tzinfo=UTC)


@pytest.mark.parametrize(
    "changes",
    [
        {"plan_renewal_date": None},
        {"plan_renewal_date": "next month"},
        {"plan_renewal_date": 20261108},
        {"searches_per_month": "250"},
        {"plan_searches_left": None},
        {"this_hour_searches": True},
        {"account_rate_limit_per_hour": 1.5},
    ],
)
async def test_an_account_body_that_breaks_the_contract_is_a_violation(changes: dict[str, Any]) -> None:
    with pytest.raises(ContractViolation, match="account_envelope"):
        await read_account(lambda r: httpx.Response(200, json=account_body(**changes)))


async def test_an_account_body_missing_a_field_or_not_an_object_is_a_violation() -> None:
    body = account_body()
    del body["this_hour_searches"]
    with pytest.raises(ContractViolation, match="account_envelope"):
        await read_account(lambda r: httpx.Response(200, json=body))
    with pytest.raises(ContractViolation, match="account_envelope"):
        await read_account(lambda r: httpx.Response(200, json=[body]))
    with pytest.raises(ContractViolation, match="not_json"):
        await read_account(lambda r: httpx.Response(200, text="<html>502</html>"))


@pytest.mark.parametrize(
    "status, kind",
    [
        (401, "auth"),
        (403, "auth"),
        (429, "rate_limited"),
        (500, "transient"),
        (503, "transient"),
        (404, "client"),
    ],
)
async def test_an_account_read_that_fails_raises_so_that_no_search_is_admitted(
    status: int, kind: str
) -> None:
    with pytest.raises(SourceError) as exc:
        await read_account(lambda r: httpx.Response(status, json={"error": "nope"}))
    assert (exc.value.kind, exc.value.status) == (kind, status) and exc.value.code == f"account_http_{status}"


async def test_an_account_read_that_never_arrives_raises_the_transport_error() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(SourceError) as refused:
        await read_account(refuse)
    assert (refused.value.kind, refused.value.before_send) == ("transient", True)
    with pytest.raises(SourceError) as lost:
        await read_account(slow)
    assert lost.value.kind == "ambiguous"


def test_canonical_links_of_the_recorded_via_options() -> None:
    doc = recorded_doc()
    page = page_of(doc)
    expected = [
        canonical_url(next(o["link"] for o in j["apply_options"] if o["title"] == j["via"]))
        for j in doc["jobs_results"]
    ]
    assert [o.normalized.primary_link for o in adapter().parse(page) if isinstance(o, Parsed)] == expected
