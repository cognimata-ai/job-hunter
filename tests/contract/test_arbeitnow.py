import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from jobhunter.normalize.keys import AdKey
from jobhunter.sources.arbeitnow import (
    LOW_REMAINING,
    PAGE_SIZE,
    ArbeitnowAdapter,
    FirehoseState,
    Leg,
    conclude,
    covered_span,
    page_cap,
    plan_legs,
    step,
)
from jobhunter.sources.base import (
    Capabilities,
    Continuation,
    ContractViolation,
    DuplicateInPage,
    OutOfScope,
    Parsed,
    RateLimit,
    RawPage,
    Rejected,
    SourceAdapter,
    SourceError,
)
from jobhunter.sources.fixtures import fixture_transport
from jobhunter.sources.http import HttpOutcome

from tests.support.sources import FIXTURES, fixture_page, make_cell_plan, served_requests
from tests.unit.test_sources_base import SPEC

pytestmark = pytest.mark.contract
T = datetime(2026, 10, 16, 9, 47, tzinfo=UTC)


def adapter() -> ArbeitnowAdapter:
    return ArbeitnowAdapter(httpx.AsyncClient(), instance_id=1, clock=lambda: T)


def arbeitnow_cell(**changes: Any) -> Any:
    base = {
        "source_id": "arbeitnow",
        "query_text": None,
        "query_lang": None,
        "location_key": None,
        "location_label": None,
        "radius_km": None,
        "bucket": None,
        "request_class": "free",
    }
    return make_cell_plan(**{**base, **changes})


def test_page_contract_scope_rule_and_sanitised_html() -> None:
    a, page = adapter(), fixture_page("arbeitnow", "page1", SPEC, fetched_at=T)
    a.check_envelope(page)
    out = a.parse(page)
    assert len(out) == 30 and all(o.item_ts is not None for o in out)
    assert a.continuation(page) == (Continuation.CURSOR, "2")
    oos = [o for o in out if isinstance(o, OutOfScope)]
    assert oos and all(o.reason == "not_germany" for o in oos)
    parsed = [o.normalized for o in out if isinstance(o, Parsed)]
    assert all(
        "<script" not in (p.description_html_sanitized or "") and p.posted_precision == "exact"
        for p in parsed
    )
    # an empty location stays in scope
    assert all(p.raw_keys == (AdKey("arbeitnow", p.native_id.rsplit("-", 1)[1]),) for p in parsed)
    assert any(not p.locations for p in parsed)
    assert a.remaining_low(fixture_page("arbeitnow", "page1_remaining5", SPEC, fetched_at=T))
    assert not a.remaining_low(page)
    with pytest.raises(ContractViolation):
        a.check_envelope(fixture_page("arbeitnow", "page_truncated", SPEC, fetched_at=T))


def test_the_recorded_pages_parse_whole() -> None:
    a = adapter()
    for case, page_no, follows in (("page1", 1, "2"), ("page2", 2, "3")):
        page = fixture_page("arbeitnow", case, replace(SPEC, page_no=page_no), fetched_at=T)
        a.check_envelope(page)
        out = a.parse(page)
        assert [o.item_index for o in out] == list(range(30)) and not any(
            isinstance(o, Rejected) for o in out
        )
        assert a.peek_native_ids(page) == [o.native_id for o in out]
        assert a.continuation(page) == (Continuation.CURSOR, follows)
        # the item time is created_at, exact to the second, for every item
        recorded: list[dict[str, Any]] = json.loads(page.body)["data"]
        assert [o.item_ts for o in out] == [datetime.fromtimestamp(i["created_at"], UTC) for i in recorded]
        parsed = [o.normalized for o in out if isinstance(o, Parsed)]
        assert all(p.description_completeness == "full" and p.item_ts == p.posted_at for p in parsed)
        assert all(p.description_quality == "ok" for p in parsed)


def test_entity_escaped_employer_html_is_unescaped_once_then_sanitised() -> None:
    # 114 of V8's 600 recorded items carry the employer's HTML entity-escaped, followed by Arbeitnow's own
    # real-HTML footer (page2's first item is one)
    page = fixture_page("arbeitnow", "page2", replace(SPEC, page_no=2), fetched_at=T)
    first = adapter().parse(page)[0]
    assert isinstance(first, Parsed)
    posting = first.normalized
    assert posting.description_html_sanitized is not None
    assert posting.description_html_sanitized.startswith("<p><strong>About SiTime</strong></p>")
    assert "&lt;" not in posting.description_html_sanitized and "<p>" not in posting.description_text
    assert posting.description_text.startswith("About SiTime\nSiTime is the Precision Timing company.")
    assert "Jobs in Germany" in posting.description_text  # the footer survives as text


def test_the_end_of_the_feed_is_an_empty_page_or_a_missing_next_link() -> None:
    a = adapter()
    end = fixture_page("arbeitnow", "page_end", replace(SPEC, page_no=3), fetched_at=T)
    a.check_envelope(end)
    assert (
        a.continuation(end) == (Continuation.END, None)
        and a.parse(end) == []
        and a.peek_native_ids(end) == []
    )
    data = json.loads(fixture_page("arbeitnow", "page1", SPEC, fetched_at=T).body)
    no_next = {**data, "links": {**data["links"], "next": None}}
    assert a.continuation(doc_page(no_next)) == (Continuation.END, None)
    missing = {**data, "links": {key: v for key, v in data["links"].items() if key != "next"}}
    assert a.continuation(doc_page(missing)) == (Continuation.END, None)
    empty_with_next = {**data, "data": []}
    assert a.continuation(doc_page(empty_with_next)) == (Continuation.END, None)


@pytest.mark.parametrize(
    "change",
    [
        {"data": None},
        {"data": {}},
        {"meta": None},
        {"meta": []},
        {"links": None},
        {"links": "https://www.arbeitnow.com"},
        {"links": {"next": 3}},
        {"links": {"next": ""}},
    ],
)
def test_a_broken_envelope_is_a_contract_violation_on_every_read_path(change: dict[str, Any]) -> None:
    data = json.loads(fixture_page("arbeitnow", "page1", SPEC, fetched_at=T).body)
    page = doc_page({key: v for key, v in {**data, **change}.items() if v is not None})
    a = adapter()
    for read in (a.check_envelope, a.peek_native_ids, a.parse, a.continuation):
        with pytest.raises(ContractViolation):
            read(page)


@pytest.mark.parametrize("body", [b"[]", b'"page"', b"<html>Bad Gateway</html>"])
def test_a_body_that_is_not_the_envelope_object_is_a_contract_violation(body: bytes) -> None:
    with pytest.raises(ContractViolation):
        adapter().check_envelope(RawPage(SPEC, 200, {"content-type": "application/json"}, body, T))


@pytest.mark.parametrize(
    "remaining, low",
    [("5", True), ("0", True), (f"{LOW_REMAINING + 1}", False), ("49", False), ("", False), ("n/a", False)],
)
def test_remaining_low_reads_the_rate_limit_header(remaining: str, low: bool) -> None:
    page = fixture_page("arbeitnow", "page1", SPEC, fetched_at=T)
    headers = {**page.headers, "x-ratelimit-remaining": remaining}
    assert adapter().remaining_low(replace(page, headers=headers)) is low
    without = {k: v for k, v in page.headers.items() if k != "x-ratelimit-remaining"}
    assert adapter().remaining_low(replace(page, headers=without)) is False


async def test_a_fixture_run_reads_pages_one_and_two_then_the_end_and_is_covered(tmp_path: Path) -> None:
    # Ruling T10-a, part 4 (Tasks 23 and 25 rely on it): the fixture transport serves page >= 3 as `page_end`,
    # so a fixture run reads pages 1, 2 and the end, within the smallest page cap, and its read is covered.
    log = tmp_path / "served.jsonl"
    async with httpx.AsyncClient(transport=fixture_transport(FIXTURES, log_path=log)) as client:
        a = ArbeitnowAdapter(client, instance_id=1, clock=lambda: T)
        state = FirehoseState(newest_covered_upper=None, gaps=())
        (leg,) = plan_legs(state, n_newer={}, now=T, max_ad_age_days=30)
        assert leg == Leg("fresh", None, 1)
        spec, cap, times, pages = a.plan(arbeitnow_cell()), page_cap(3600), [], 0
        while True:
            outcome = await a.fetch(spec)
            assert outcome.error is None and outcome.page is not None and outcome.page.status == 200
            pages += 1
            a.check_envelope(outcome.page)
            items = [o.item_ts for o in a.parse(outcome.page) if o.item_ts is not None]
            times += items
            kind, cursor = a.continuation(outcome.page)
            if kind is Continuation.END:
                break
            assert step(
                items, leg, state, first_item_older_than_target=False, last_item_newer_than_target=False
            ) == ("continue")
            assert pages < cap
            next_spec = a.next_page(spec, cursor)
            assert next_spec is not None
            spec = next_spec
    assert [s["case"] for s in served_requests(log)] == ["page1", "page2", "page_end"]
    assert [s["query"] for s in served_requests(log)] == ["page=1", "page=2", "page=3"]
    assert pages == 3 == cap and len(times) == 60
    after, gaps = conclude(
        state, [(leg, times)], pages=pages, cap=cap, stopped_by="end", now=T, max_ad_age_days=30
    )
    assert gaps == [] and after == FirehoseState(newest_covered_upper=max(times), gaps=())
    assert covered_span([(leg, times)], stopped_by="end") == (min(times), max(times))


async def test_fetch_asks_for_the_page_of_the_spec_and_nothing_else() -> None:
    sent: list[httpx.Request] = []

    async def capture(request: httpx.Request) -> None:
        sent.append(request)

    transport = fixture_transport(FIXTURES)
    async with httpx.AsyncClient(transport=transport, event_hooks={"request": [capture]}) as client:
        a = ArbeitnowAdapter(client, instance_id=1, clock=lambda: T)
        spec = a.plan(arbeitnow_cell())
        later = a.next_page(spec, "7")
        assert later is not None
        first, seventh = await a.fetch(spec), await a.fetch(later)
    assert [str(r.url) for r in sent] == [
        "https://www.arbeitnow.com/api/job-board-api?page=1",
        "https://www.arbeitnow.com/api/job-board-api?page=7",
    ]
    assert all(r.method == "GET" for r in sent)
    assert first.page is not None and first.page.fetched_at == T and seventh.page is not None


def test_plan_is_page_one_of_the_cell_with_its_window_and_class() -> None:
    cell = arbeitnow_cell(cell_id=42)
    spec = adapter().plan(cell)
    assert (spec.page_no, spec.cell_id, spec.source_instance_id, spec.operation) == (1, 42, 1, "list")
    assert dict(spec.params) == {} and spec.cursor_in is None and spec.window == cell.window
    assert spec.request_class == "free"
    with pytest.raises(ValueError, match="arbeitnow"):
        adapter().plan(arbeitnow_cell(source_id="jsearch"))
    with pytest.raises(ValueError, match="instance"):
        adapter().plan(arbeitnow_cell(instance_id=2))


def test_next_page_is_the_page_the_cursor_names() -> None:
    a = adapter()
    first = a.plan(arbeitnow_cell())
    second = a.next_page(first, "2")
    assert second is not None and (second.page_no, second.cursor_in) == (2, None)
    assert second.params == first.params and second.request_class == "free" and second.window == first.window
    assert a.next_page(first, None) is None and a.next_page(first, "") is None
    for cursor in ("0", "-1", "two"):
        with pytest.raises(ValueError):
            a.next_page(first, cursor)


def test_a_free_source_bills_nothing_and_observes_no_quota() -> None:
    a, page = adapter(), fixture_page("arbeitnow", "page1", SPEC, fetched_at=T)
    assert a.settle(HttpOutcome(page, None)) == ("not_billed", None)
    assert a.settle(HttpOutcome(None, SourceError("ambiguous", code="timeout"))) == ("not_billed", None)
    assert a.observation(page) is None


def test_capabilities_and_constants_are_the_contract() -> None:
    assert adapter().capabilities() == Capabilities(
        "arbeitnow", "free", 0, "page", 300, "exact", "full", False, 1, [RateLimit("arbeitnow", 0.5, 1)]
    )
    assert (PAGE_SIZE, LOW_REMAINING) == (300, 5)
    conforming: SourceAdapter = adapter()  # checked by mypy: the protocol of Deviation 25
    assert conforming.capabilities().source_id == "arbeitnow"


# ---- the items of a page ----------------------------------------------------------------------------

ITEM: dict[str, Any] = {
    "slug": "python-developer-berlin-123456",
    "company_name": "ACME GmbH",
    "title": "Python Developer (m/w/d)",
    "description": "<p>Wir suchen <strong>Verstärkung</strong> für unser Backend-Team.</p>"
    "<script>alert(1)</script><p>" + "Wir freuen uns auf Ihre Bewerbung. " * 8 + "</p>",
    "remote": False,
    "url": "https://www.arbeitnow.com/jobs/companies/acme-gmbh/python-developer-berlin-123456",
    "tags": ["Python"],
    "job_types": ["Mid", "fulltime permanent"],
    "location": "Berlin",
    "created_at": 1792108800,
}


def item(**changes: Any) -> dict[str, Any]:
    return {**ITEM, **changes}


def doc_page(doc: Any, *, page_no: int = 1) -> RawPage:
    return RawPage(
        replace(SPEC, page_no=page_no), 200, {"content-type": "application/json"}, json.dumps(doc).encode(), T
    )


def items_page(items: list[Any]) -> RawPage:
    links = {
        "first": "x",
        "last": None,
        "prev": None,
        "next": "https://www.arbeitnow.com/api/job-board-api?page=2",
    }
    return doc_page({"data": items, "links": links, "meta": {"current_page": 1, "per_page": 300}})


def parsed_one(**changes: Any) -> Any:
    (outcome,) = adapter().parse(items_page([item(**changes)]))
    assert isinstance(outcome, Parsed), outcome
    return outcome.normalized


def test_identity_is_the_slug_and_the_key_its_numeric_suffix() -> None:
    posting = parsed_one()
    assert posting.native_id == "python-developer-berlin-123456"
    assert posting.raw_keys == (AdKey("arbeitnow", "123456"),)
    assert (
        posting.primary_link
        == "https://arbeitnow.com/jobs/companies/acme-gmbh/python-developer-berlin-123456"
    )
    assert (posting.title_raw, posting.company_raw, posting.outbound_url_private) == (
        "Python Developer (m/w/d)",
        "ACME GmbH",
        None,
    )
    assert parsed_one(slug="python-developer-berlin").raw_keys == ()
    assert parsed_one(url=None).primary_link is None


def test_created_at_is_exact_and_the_item_time() -> None:
    posting = parsed_one()
    assert posting.posted_at == datetime(2026, 10, 16, tzinfo=UTC) == posting.item_ts
    assert posting.posted_precision == "exact"
    for unusable in (None, True, "1792108800", 10**15):
        posting = parsed_one(created_at=unusable)
        assert (posting.posted_at, posting.posted_precision, posting.item_ts) == (None, "unknown", None)


def test_the_description_is_sanitised_html_and_its_text() -> None:
    posting = parsed_one()
    assert (
        posting.description_html_sanitized is not None and "<script" not in posting.description_html_sanitized
    )
    assert "alert" not in posting.description_text and posting.description_text.startswith(
        "Wir suchen Verstärkung"
    )
    assert posting.description_completeness == "full" and posting.description_quality == "ok"
    empty = parsed_one(description=None)
    assert (empty.description_text, empty.description_html_sanitized, empty.description_quality) == (
        "",
        None,
        "garbage",
    )
    # a description that only mentions an escaped tag later is not unescaped
    literal = parsed_one(description="<p>Use &lt;b&gt; for bold.</p>")
    assert literal.description_text == "Use <b> for bold."


def test_the_remote_flag_is_structured_evidence_only_when_true() -> None:
    remote = parsed_one(remote=True)
    assert remote.work_mode_possible == {"remote"} and remote.work_mode_source == "structured"
    for flag in (False, None, "true"):
        posting = parsed_one(remote=flag)
        assert (
            posting.work_mode_possible == {"remote", "hybrid", "onsite"}
            and posting.work_mode_source == "none"
        )


def test_job_types_are_text_evidence_of_the_employment_types() -> None:
    posting = parsed_one(
        job_types=["Mid", "fulltime permanent", "Freiberufler / selbstständiger", "freelance"]
    )
    assert posting.employment_types == ("full_time", "freelance") and posting.employment_source == "text"
    assert parsed_one(job_types=["parttime fixed term"]).employment_types == ("part_time",)
    none = parsed_one(job_types=[])
    assert (none.employment_types, none.employment_source) == ((), "none")
    assert parsed_one(job_types="fulltime").employment_types == ()


def test_only_positive_evidence_of_a_place_outside_germany_is_out_of_scope() -> None:
    a = adapter()
    outcomes = a.parse(
        items_page(
            [
                item(slug="a-1", location="London"),
                item(slug="b-2", location="Zürich, Schweiz"),
                item(slug="c-3", location="Berlin"),
                item(slug="d-4", location=""),
                item(slug="e-5", location="Remote job"),
                item(slug="f-6", location="Nowhere Particular"),
                item(slug="g-7", location=None),
                item(slug="h-8", location="London; Berlin"),
            ]
        )
    )
    assert [type(o) for o in outcomes] == [OutOfScope, OutOfScope] + [Parsed] * 6
    assert all(
        o.reason == "not_germany" and o.item_ts is not None for o in outcomes if isinstance(o, OutOfScope)
    )
    kinds = [o.normalized.location_kind for o in outcomes if isinstance(o, Parsed)]
    assert kinds == ["city", "unknown", "remote", "city", "unknown", "multi"]


def test_item_failures_reject_the_item_and_never_the_page() -> None:
    a = adapter()
    items: list[Any] = [
        item(slug="a-1"),
        "not an object",
        item(slug=None),
        item(slug=""),
        item(slug=17),
        item(slug="b-2", title=None),
        item(slug="c-3", company_name=7),
        item(slug="d-4"),
    ]
    page = items_page(items)
    a.check_envelope(page)
    outcomes = a.parse(page)
    assert [o.item_index for o in outcomes] == list(range(8))
    assert [type(o) for o in outcomes] == [Parsed] + [Rejected] * 6 + [Parsed]
    rejected = [o for o in outcomes if isinstance(o, Rejected)]
    assert {o.reason_code for o in rejected} == {"item_contract"} and all(o.detail for o in rejected)
    assert [o.native_id for o in rejected] == [None, None, None, None, "b-2", "c-3"]
    assert a.peek_native_ids(page) == ["a-1", None, None, None, None, "b-2", "c-3", "d-4"]


def test_a_repeated_slug_is_a_duplicate_of_its_first_occurrence() -> None:
    outcomes = adapter().parse(
        items_page([item(slug="a-1"), item(slug="b-2"), item(slug="a-1", title="Other")])
    )
    assert [type(o) for o in outcomes] == [Parsed, Parsed, DuplicateInPage]
    assert isinstance(outcomes[2], DuplicateInPage) and outcomes[2].first_index == 0
    out_of_scope_first = adapter().parse(items_page([item(slug="a-1", location="London"), item(slug="a-1")]))
    assert [type(o) for o in out_of_scope_first] == [OutOfScope, DuplicateInPage]


def test_peek_reads_the_slugs_without_normalising() -> None:
    page = fixture_page("arbeitnow", "page1", SPEC, fetched_at=T)
    recorded: list[dict[str, Any]] = json.loads(page.body)["data"]
    assert adapter().peek_native_ids(page) == [i["slug"] for i in recorded]
