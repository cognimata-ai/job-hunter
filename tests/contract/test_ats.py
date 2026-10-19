import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from jobhunter.normalize.keys import AdKey
from jobhunter.sources import ats
from jobhunter.sources.ats.common import AtsAdapter, membership
from jobhunter.sources.ats.lever import LeverAdapter
from jobhunter.sources.ats.smartrecruiters import DETAIL_MAX_AGE, SmartRecruitersAdapter
from jobhunter.sources.base import (
    Capabilities,
    Continuation,
    ContractViolation,
    DuplicateInPage,
    OutOfScope,
    Parsed,
    RawPage,
    Rejected,
    RequestSpec,
    SourceAdapter,
    SourceError,
)
from jobhunter.sources.fixtures import fixture_transport
from jobhunter.sources.http import HttpOutcome
from jobhunter.sources.instances import InstanceRow
from jobhunter.sources.ratelimit import ATS_HOST_LIMIT, SMARTRECRUITERS_DETAIL

from tests.support.sources import (
    FIXTURES,
    adapter_for_fixture,
    fixture_page,
    make_cell_plan,
    parsed_of,
    served_requests,
)
from tests.unit.test_sources_base import SPEC

pytestmark = pytest.mark.contract
T = datetime(2026, 10, 16, 9, 47, tzinfo=UTC)


@pytest.mark.parametrize(
    ("ats", "case", "n", "key_ns"),
    [
        ("greenhouse", "greenhouse_getyourguide", 15, "ats:greenhouse:getyourguide"),
        ("lever", "lever_leverdemo", 10, "ats:lever:global:leverdemo"),
        ("ashby", "ashby_n8n", 42, "ats:ashby:n8n"),
        ("personio", "personio_everphone", 8, "ats:personio:everphone"),
        ("recruitee", "recruitee_bunq", 22, "ats:recruitee:bunq"),
    ],
)
def test_board_contract(ats, case, n, key_ns) -> None:
    a, page = adapter_for_fixture(ats, case), fixture_page("ats", case, SPEC, fetched_at=T)
    a.check_envelope(page)
    ids = a.peek_native_ids(page)
    out = a.parse(page)
    assert (
        all(ids)
        and len(out) == len(ids)
        and (n is None or len(out) == n)
        and a.continuation(page) == (Continuation.END, None)
    )
    parsed = [o.normalized for o in out if isinstance(o, Parsed)]
    assert len(parsed) == len(out)
    assert all(
        p.raw_keys == (AdKey(key_ns, p.native_id),)
        and p.posted_precision == "exact"
        and p.description_completeness == "full"
        for p in parsed
    )
    assert sum(len(p.description_text) >= 200 for p in parsed) / len(parsed) >= 0.9
    draft = membership([page], a, reached_end=True)
    assert draft.complete and draft.members == frozenset(ids)


def test_structured_fields_per_ats() -> None:
    lever = parsed_of("lever", "lever_leverdemo")
    assert any(p.work_mode_source == "structured" for p in lever) and any(
        p.remote_countries for p in lever if "remote" in p.work_mode_possible
    )
    rec = parsed_of("recruitee", "recruitee_bunq")
    assert all(p.work_mode_source == "structured" for p in rec) and any(
        p.remote_countries == ("NL",) for p in rec
    )
    per = parsed_of("personio", "personio_everphone")
    assert all(p.work_mode_source == "none" for p in per) and any(
        "seniority" in p.source_structured for p in per
    )
    gh = parsed_of("greenhouse", "greenhouse_getyourguide")
    assert any(p.requisition_id for p in gh) and all(p.work_mode_source == "none" for p in gh)


def test_incomplete_snapshots_and_xml_entities() -> None:
    gh = adapter_for_fixture("greenhouse", "greenhouse_count_mismatch")
    assert (
        membership(
            [fixture_page("ats", "greenhouse_count_mismatch", SPEC, fetched_at=T)], gh, reached_end=True
        ).incomplete_reason
        == "count_mismatch"
    )
    with pytest.raises(ContractViolation, match="xml_entity"):
        adapter_for_fixture("personio", "personio_entity").check_envelope(
            fixture_page("ats", "personio_entity", SPEC, fetched_at=T)
        )
    assert (
        membership(
            [fixture_page("ats", "lever_leverdemo", SPEC, fetched_at=T)],
            adapter_for_fixture("lever", "lever_leverdemo"),
            reached_end=False,
        ).incomplete_reason
        == "no_end"
    )


def _with_items(page: RawPage, change: Any) -> RawPage:
    """`page` with every item of its `content` array replaced by `change(item)`."""
    doc = json.loads(page.body)
    doc["content"] = [change(item) for item in doc["content"]]
    return replace(page, body=json.dumps(doc).encode("utf-8"))


def test_smartrecruiters_list_and_details() -> None:
    a = adapter_for_fixture("smartrecruiters", "smartrecruiters_deliveryhero_list")
    assert isinstance(a, SmartRecruitersAdapter)
    recorded = fixture_page("ats", "smartrecruiters_deliveryhero_list", SPEC, fetched_at=T)
    assert a.continuation(recorded) == (Continuation.END, None) and len(a.peek_native_ids(recorded)) == 10
    # Deviation (spec §5.3.5 first): the ten recorded postings are all outside Germany and none is remote
    # (Dubai, Buenos Aires, ... Barcelona; hybrid at most), so each is OutOfScope with no detail call; the
    # plan's detail counts (9 and 10) are asserted on the same postings made remote, which puts them in scope.
    assert [o.reason for o in a.parse(recorded) if isinstance(o, OutOfScope)] == ["not_germany"] * 10
    assert a.detail_specs(recorded, {}, now=T) == []
    page = _with_items(recorded, lambda item: {**item, "location": {**item["location"], "remote": True}})
    first = a.peek_native_ids(page)[0]
    assert first is not None
    known = {first: (a.list_item_sha256(page, 0), T)}  # unchanged and fresh: no detail call
    specs = a.detail_specs(page, known, now=T)
    assert len(specs) == 9 and all(s.operation == "detail" and s.detail_native_id for s in specs)
    stale = {first: (a.list_item_sha256(page, 0), T - timedelta(days=8))}
    assert len(a.detail_specs(page, stale, now=T)) == 10
    detail = a.parse_detail(fixture_page("ats", "smartrecruiters_deliveryhero_detail_1", SPEC, fetched_at=T))
    assert isinstance(detail, Parsed) and len(detail.normalized.description_text) >= 200


def test_smartrecruiters_pagination_variants() -> None:
    a = adapter_for_fixture("smartrecruiters", "smartrecruiters_list_paged")
    first = fixture_page(
        "ats", "smartrecruiters_list_paged", a.plan(ats_cell("smartrecruiters")), fetched_at=T
    )
    assert a.continuation(first) == (Continuation.CURSOR, "100")
    following = a.next_page(first.spec, "100")
    assert following is not None and following.params["offset"] == "100" and following.page_no == 2
    second = fixture_page("ats", "smartrecruiters_list_offset100", following, fetched_at=T)
    assert a.continuation(second) == (Continuation.END, None)
    # the two variants split the ten trimmed postings: 5 at offset 0 and 5 at offset 100, `totalFound` 10
    recorded = fixture_page("ats", "smartrecruiters_deliveryhero_list", SPEC, fetched_at=T)
    union = frozenset(a.peek_native_ids(first)) | frozenset(a.peek_native_ids(second))
    assert len(a.peek_native_ids(first)) == len(a.peek_native_ids(second)) == 5
    assert union == frozenset(a.peek_native_ids(recorded))
    draft = membership([first, second], a, reached_end=True)
    assert draft.complete and draft.members == union and draft.member_count == 10


def _lever_page(count: int) -> RawPage:
    body = json.dumps([{"id": f"posting-{i}", "text": f"Role {i}"} for i in range(count)]).encode()
    return RawPage(SPEC, 200, {"content-type": "application/json"}, body, T)


def test_lever_eu_site_and_the_full_page_rule() -> None:
    eu = adapter_for_fixture("lever", "lever_eu_leverdemo")
    page = fixture_page("ats", "lever_eu_leverdemo", SPEC, fetched_at=T)
    out = eu.parse(page)
    parsed = [o.normalized for o in out if isinstance(o, Parsed)]
    assert len(out) == len(parsed) == 100
    assert all(p.raw_keys == (AdKey("ats:lever:eu:leverdemo", p.native_id),) for p in parsed)
    assert eu.continuation(page) == (Continuation.CURSOR, "100")  # a full recorded page goes on
    assert eu.continuation(fixture_page("ats", "lever_eu_end", SPEC, fetched_at=T)) == (
        Continuation.END,
        None,
    )
    lever = adapter_for_fixture("lever", "lever_leverdemo")
    assert lever.continuation(_lever_page(100)) == (Continuation.CURSOR, "100")
    following = lever.next_page(lever.plan(ats_cell("lever")), "100")
    assert following is not None and following.params["skip"] == "100" and following.page_no == 2
    assert lever.continuation(replace(_lever_page(100), spec=following)) == (Continuation.CURSOR, "200")
    assert lever.continuation(_lever_page(99)) == (Continuation.END, None)
    assert lever.continuation(_lever_page(0)) == (Continuation.END, None)


# Beyond the brief: the request of every board, the envelope gate on every read path, the membership rules,
# the SmartRecruiters detail rules and the item contract.


def ats_cell(source_id: str, instance_id: int = 1) -> Any:
    return make_cell_plan(
        source_id=source_id,
        instance_id=instance_id,
        query_text=None,
        query_lang=None,
        location_key=None,
        location_label=None,
        radius_km=None,
        bucket=None,
        request_class="free",
    )


def instance_row(source_id: str, instance_key: str, display_name: str | None = "Acme") -> InstanceRow:
    region, _, site = instance_key.rpartition(":")
    return InstanceRow(
        id=1,
        source_id=source_id,
        instance_key=instance_key,
        watchlist_company_id=3,
        health="ok",
        half_open_after=None,
        board_slug=site,
        lever_region=region or None,
        display_name=display_name,
    )


BOARDS = [
    (
        "greenhouse",
        "getyourguide",
        "boards-api.greenhouse.io",
        "/v1/boards/getyourguide/jobs",
        "content=true",
    ),
    ("lever", "global:leverdemo", "api.lever.co", "/v0/postings/leverdemo", "limit=100&mode=json&skip=0"),
    ("lever", "eu:leverdemo", "api.eu.lever.co", "/v0/postings/leverdemo", "limit=100&mode=json&skip=0"),
    ("ashby", "n8n", "api.ashbyhq.com", "/posting-api/job-board/n8n", "includeCompensation=true"),
    ("personio", "everphone", "everphone.jobs.personio.de", "/xml", ""),
    ("recruitee", "bunq", "bunq.recruitee.com", "/api/offers/", ""),
    (
        "smartrecruiters",
        "DeliveryHero",
        "api.smartrecruiters.com",
        "/v1/companies/DeliveryHero/postings",
        "limit=100&offset=0",
    ),
]


@pytest.mark.parametrize(("source_id", "key", "host", "path", "query"), BOARDS)
async def test_every_board_is_requested_at_its_fixed_api_host(
    source_id, key, host, path, query, tmp_path
) -> None:
    log = tmp_path / "served.jsonl"
    async with httpx.AsyncClient(transport=fixture_transport(FIXTURES, log_path=log)) as client:
        a = ats.adapter_for(instance_row(source_id, key), client, clock=lambda: T)
        outcome = await a.fetch(a.plan(ats_cell(source_id)))
    [served] = served_requests(log)
    assert (served["host"], served["path"], served["query"]) == (host, path, query)
    assert outcome.error is None and outcome.page is not None and outcome.page.status == 200
    assert outcome.page.fetched_at == T and a.host == host
    a.check_envelope(outcome.page)
    assert all(a.peek_native_ids(outcome.page))


@pytest.mark.parametrize(
    ("source_id", "key", "start", "moved"),
    [
        ("personio", "everphone", "everphone.jobs.personio.de", "everphone-gmbh.jobs.personio.de"),
        ("greenhouse", "getyourguide", "boards-api.greenhouse.io", "boards-api.greenhouse.io"),
        ("recruitee", "bunq", "bunq.recruitee.com", "bunq-careers.recruitee.com"),
    ],
)
async def test_only_a_personio_board_follows_a_redirect_within_its_host_family(
    source_id, key, start, moved
) -> None:
    sent: list[str] = []
    serve = fixture_transport(FIXTURES)

    async def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.host)
        if request.url.host == start:
            return httpx.Response(301, headers={"location": f"https://{moved}/moved{request.url.path}"})
        return await serve.handle_async_request(
            httpx.Request("GET", f"https://{start}{request.url.path.removeprefix('/moved')}")
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        a = ats.adapter_for(instance_row(source_id, key), client, clock=lambda: T)
        outcome = await a.fetch(a.plan(ats_cell(source_id)))
    assert outcome.page is not None
    if source_id == "personio":
        assert sent == [start, moved] and outcome.page.status == 200
        a.check_envelope(outcome.page)
    else:  # the redirect is a page of its own, never followed
        assert sent == [start] and outcome.page.status == 301


async def test_a_smartrecruiters_detail_is_requested_by_its_id(tmp_path) -> None:
    log = tmp_path / "served.jsonl"
    async with httpx.AsyncClient(transport=fixture_transport(FIXTURES, log_path=log)) as client:
        a = ats.adapter_for(instance_row("smartrecruiters", "DeliveryHero"), client, clock=lambda: T)
        assert isinstance(a, SmartRecruitersAdapter)
        listing = fixture_page(
            "ats", "smartrecruiters_deliveryhero_list", a.plan(ats_cell("smartrecruiters")), fetched_at=T
        )
        page = _with_items(listing, lambda item: {**item, "location": {**item["location"], "remote": True}})
        [spec, *_] = a.detail_specs(page, {}, now=T, parent_fetch_request_id=41)
        outcome = await a.fetch(spec)
    assert [(s["path"], s["query"], s["case"]) for s in served_requests(log)] == [
        ("/v1/companies/DeliveryHero/postings/744000156183719", "", "smartrecruiters_deliveryhero_detail_1")
    ]
    assert outcome.page is not None
    a.check_envelope(outcome.page)
    assert a.peek_native_ids(outcome.page) == ["744000156183719"]
    assert a.continuation(outcome.page) == (Continuation.END, None)
    [item] = a.parse(outcome.page)
    assert isinstance(item, Parsed) and item.native_id == "744000156183719"
    assert item.normalized.raw_keys == (AdKey("ats:smartrecruiters:DeliveryHero", "744000156183719"),)


def test_capabilities_and_the_free_no_ops() -> None:
    for source_id, key, *_ in BOARDS:
        a = ats.adapter_for(instance_row(source_id, key), httpx.AsyncClient(), clock=lambda: T)
        caps = a.capabilities()
        assert isinstance(a, AtsAdapter) and caps.source_id == source_id
        assert (caps.billing, caps.unit_per_page, caps.can_close, caps.max_concurrency) == (
            "free",
            0,
            True,
            2,
        )
        assert ATS_HOST_LIMIT in caps.rate_limits and caps.date_semantics == "exact"
        assert a.settle(HttpOutcome(None, SourceError("ambiguous", code="timeout"))) == ("not_billed", None)
        assert a.observation(fixture_page("ats", "greenhouse_getyourguide", SPEC, fetched_at=T)) is None
        assert a.key("7") == AdKey(f"ats:{source_id}:{key}", "7")
        typed: SourceAdapter = a  # the §5.1 protocol
        assert isinstance(typed.capabilities(), Capabilities)
    sr = ats.adapter_for(
        instance_row("smartrecruiters", "DeliveryHero"), httpx.AsyncClient(), clock=lambda: T
    )
    assert SMARTRECRUITERS_DETAIL in sr.capabilities().rate_limits
    assert (sr.capabilities().pagination, sr.capabilities().description) == ("offset", "detail_call")


def test_adapter_for_reads_the_board_from_the_instance() -> None:
    lever = ats.adapter_for(instance_row("lever", "eu:acme"), httpx.AsyncClient(), clock=lambda: T)
    assert isinstance(lever, LeverAdapter) and (lever.board, lever.host) == ("eu:acme", "api.eu.lever.co")
    with pytest.raises(ValueError, match="not an ATS"):
        ats.adapter_for(instance_row("jsearch", "default"), httpx.AsyncClient(), clock=lambda: T)
    with pytest.raises(ValueError, match="region"):
        ats.adapter_for(instance_row("lever", "acme"), httpx.AsyncClient(), clock=lambda: T)
    with pytest.raises(ValueError, match="slug"):
        ats.adapter_for(instance_row("greenhouse", "../x"), httpx.AsyncClient(), clock=lambda: T)
    with pytest.raises(ValueError, match="instance"):
        lever.plan(ats_cell("lever", instance_id=2))
    with pytest.raises(ValueError, match="cell"):
        lever.plan(ats_cell("greenhouse"))


def _page(body: bytes | str, *, spec: RequestSpec = SPEC, content_type: str = "application/json") -> RawPage:
    raw = body.encode("utf-8") if isinstance(body, str) else body
    return RawPage(spec, 200, {"content-type": content_type}, raw, T)


BROKEN = [
    ("greenhouse", "[]"),
    ("greenhouse", '{"jobs": []}'),
    ("greenhouse", '{"jobs": {}, "meta": {"total": 0}}'),
    ("greenhouse", '{"jobs": [], "meta": {"total": "0"}}'),
    ("lever", '{"data": []}'),
    ("ashby", "[]"),
    ("ashby", '{"jobs": null}'),
    ("recruitee", '{"offers": {}}'),
    ("smartrecruiters", '{"content": []}'),
    ("smartrecruiters", '{"content": [], "totalFound": 0, "offset": "0", "limit": 100}'),
    ("greenhouse", "<html><body>502 Bad Gateway</body></html>"),
    ("personio", "<html><body>502 Bad Gateway</body></html>"),
    ("personio", '<?xml version="1.0"?><jobs><position><id>1</id></position></jobs>'),
    ("personio", '<?xml version="1.0"?><workzag-jobs><position>'),
]


@pytest.mark.parametrize(("source_id", "body"), BROKEN)
def test_a_broken_envelope_is_a_contract_violation_on_every_read_path(source_id, body) -> None:
    a = adapter_for_fixture(source_id, f"{source_id}_acme")
    page = _page(body)
    for read in (a.check_envelope, a.peek_native_ids, a.parse, a.continuation):
        with pytest.raises(ContractViolation):
            read(page)
    assert membership([page], a, reached_end=True).incomplete_reason == "envelope"


ERRORS = [
    ("greenhouse", '{"status": 404, "error": "Job not found"}'),
    ("lever", '{"ok": false, "error": "Document not found"}'),
    ("ashby", '{"success": false, "error": "Job board not found"}'),
    ("recruitee", '{"error": "Not found"}'),
    ("smartrecruiters", '{"message": "Company not found"}'),
]


@pytest.mark.parametrize(("source_id", "body"), ERRORS)
def test_a_provider_error_delivered_with_a_2xx_is_transient_never_a_page(source_id, body) -> None:
    a = adapter_for_fixture(source_id, f"{source_id}_acme")
    page = _page(body)
    for read in (a.check_envelope, a.peek_native_ids, a.parse, a.continuation):
        with pytest.raises(SourceError) as raised:
            read(page)
        assert not isinstance(raised.value, ContractViolation)
        assert (raised.value.kind, raised.value.code, raised.value.status) == (
            "transient",
            "provider_error",
            200,
        )
    assert membership([page], a, reached_end=True).incomplete_reason == "envelope"


def test_an_empty_board_is_a_complete_empty_snapshot() -> None:
    a = adapter_for_fixture("greenhouse", "greenhouse_acme")
    draft = membership([_page('{"jobs": [], "meta": {"total": 0}}')], a, reached_end=True)
    assert (draft.complete, draft.members, draft.member_count, draft.incomplete_reason) == (
        True,
        frozenset(),
        0,
        None,
    )


BESIDE_ITEMS = [
    ("greenhouse", '{"jobs": [], "meta": {"total": 0}, "error": "Job board is unavailable"}'),
    ("greenhouse", '{"jobs": [{"id": 1, "title": "Engineer"}], "meta": {"total": 1}, "error": "partial"}'),
    ("ashby", '{"jobs": [], "error": "Job board not found"}'),
    ("recruitee", '{"offers": [], "error": "Not found"}'),
    ("smartrecruiters", '{"offset": 0, "limit": 100, "totalFound": 0, "content": [], "message": "x"}'),
]


@pytest.mark.parametrize(("source_id", "body"), BESIDE_ITEMS)
def test_an_error_report_beside_the_items_is_the_provider_error_never_a_board(source_id, body) -> None:
    # T11-c: an error beside an (empty) item list is no proof that the board is empty
    a = adapter_for_fixture(source_id, f"{source_id}_acme")
    page = _page(body)
    for read in (a.check_envelope, a.peek_native_ids, a.parse, a.continuation):
        with pytest.raises(SourceError) as raised:
            read(page)
        assert (raised.value.kind, raised.value.code) == ("transient", "provider_error")
    draft = membership([page], a, reached_end=True)
    assert (draft.complete, draft.incomplete_reason) == (False, "envelope")


def _sr_items(count: int, start: int = 0) -> list[dict[str, Any]]:
    return [
        _sr_item(str(744000000000000 + i), city="Berlin", country="de") for i in range(start, start + count)
    ]


def _sr_listing(a: AtsAdapter, items: list[dict[str, Any]], *, offset: int, total: int) -> RawPage:
    """A SmartRecruiters list page of `a` at `offset` (the spec asks for it) with `totalFound = total`."""
    spec = a.plan(ats_cell("smartrecruiters"))
    if offset:
        following = a.next_page(spec, str(offset))
        assert following is not None
        spec = following
    doc = {"offset": offset, "limit": 100, "totalFound": total, "content": items}
    return _page(json.dumps(doc), spec=spec)


def test_a_short_page_mid_listing_is_a_count_mismatch_with_its_members_kept() -> None:
    # T11-c (the review's probe): 60 of 100 at offset 0, then 50 at offset 100, of `totalFound` 150
    a = adapter_for_fixture("smartrecruiters", "smartrecruiters_acme")
    first = _sr_listing(a, _sr_items(60), offset=0, total=150)
    assert a.continuation(first) == (Continuation.CURSOR, "100")
    second = _sr_listing(a, _sr_items(50, start=100), offset=100, total=150)
    assert a.continuation(second) == (Continuation.END, None)
    draft = membership([first, second], a, reached_end=True)
    assert (draft.complete, draft.incomplete_reason, draft.member_count) == (False, "count_mismatch", 110)
    assert draft.members == frozenset(a.peek_native_ids(first)) | frozenset(a.peek_native_ids(second))


def test_an_empty_page_below_the_total_ends_the_board_as_a_count_mismatch() -> None:
    a = adapter_for_fixture("smartrecruiters", "smartrecruiters_acme")
    first = _sr_listing(a, _sr_items(100), offset=0, total=150)
    empty = _sr_listing(a, [], offset=100, total=150)
    assert a.continuation(empty) == (Continuation.END, None)  # the board has nothing more to give
    draft = membership([first, empty], a, reached_end=True)
    assert (draft.complete, draft.incomplete_reason, draft.member_count) == (False, "count_mismatch", 100)


def test_a_total_that_changes_between_pages_is_a_count_mismatch() -> None:
    a = adapter_for_fixture("smartrecruiters", "smartrecruiters_acme")
    first = _sr_listing(a, _sr_items(100), offset=0, total=150)
    second = _sr_listing(a, _sr_items(50, start=100), offset=100, total=140)  # 150 members, 150 then 140
    assert a.continuation(second) == (Continuation.END, None)
    draft = membership([first, second], a, reached_end=True)
    assert (draft.complete, draft.incomplete_reason, draft.member_count) == (False, "count_mismatch", 150)
    whole = _sr_listing(a, _sr_items(50, start=100), offset=100, total=150)
    assert membership([first, whole], a, reached_end=True).complete  # the same board, the totals agreeing


def test_an_offset_the_board_did_not_ask_for_breaks_the_contract() -> None:
    a = adapter_for_fixture("smartrecruiters", "smartrecruiters_acme")
    assert isinstance(a, SmartRecruitersAdapter)
    asked = _sr_listing(a, _sr_items(50, start=100), offset=100, total=150)
    page = replace(asked, body=asked.body.replace(b'"offset": 100', b'"offset": 0'))
    reads = (a.check_envelope, a.peek_native_ids, a.parse, a.continuation, lambda p: a.list_item_sha256(p, 0))
    for read in (*reads, lambda p: a.detail_specs(p, {}, now=T)):
        with pytest.raises(ContractViolation, match="offset_mismatch"):
            read(page)
    first = _sr_listing(a, _sr_items(100), offset=0, total=150)
    assert membership([first, page], a, reached_end=True).incomplete_reason == "envelope"


def test_membership_needs_the_end_proven_by_the_last_of_contiguous_pages() -> None:
    sr = adapter_for_fixture("smartrecruiters", "smartrecruiters_acme")
    first = _sr_listing(sr, _sr_items(100), offset=0, total=150)
    second = _sr_listing(sr, _sr_items(50, start=100), offset=100, total=150)
    assert membership([first, second], sr, reached_end=True).complete
    # the caller's flag alone proves nothing: the last page must say END
    assert membership([first], sr, reached_end=True).incomplete_reason == "no_end"
    lever = adapter_for_fixture("lever", "lever_acme")
    assert membership([_lever_page(100)], lever, reached_end=True).incomplete_reason == "no_end"
    assert membership([_lever_page(99)], lever, reached_end=True).complete
    # one page per page number, from 1 without a gap
    third = replace(second, spec=replace(second.spec, page_no=3))
    assert membership([first, third], sr, reached_end=True).incomplete_reason == "no_end"
    assert membership([first, first, second], sr, reached_end=True).incomplete_reason == "no_end"
    gh = adapter_for_fixture("greenhouse", "greenhouse_acme")
    later = _page('{"jobs": [], "meta": {"total": 0}}', spec=replace(SPEC, page_no=2))
    assert membership([later], gh, reached_end=True).incomplete_reason == "no_end"
    assert membership([second, first], sr, reached_end=True).complete  # the order of the list is not proof


def test_xml_entities_and_dtds_are_refused_on_every_read_path() -> None:
    a = adapter_for_fixture("personio", "personio_entity")
    page = fixture_page("ats", "personio_entity", SPEC, fetched_at=T)
    for read in (a.check_envelope, a.peek_native_ids, a.parse, a.continuation):
        with pytest.raises(ContractViolation, match="xml_entity"):
            read(page)
    external = '<?xml version="1.0"?><!DOCTYPE x SYSTEM "http://example.invalid/x.dtd"><workzag-jobs/>'
    with pytest.raises(ContractViolation, match="xml_entity"):
        a.check_envelope(_page(external, content_type="text/xml"))
    html = "<!DOCTYPE html><html><body>Service unavailable</body></html>"
    with pytest.raises(ContractViolation, match="not_xml"):
        a.check_envelope(_page(html, content_type="text/html"))
    assert membership([page], a, reached_end=True).incomplete_reason == "envelope"


def _gh_job(job_id: object, **extra: Any) -> dict[str, Any]:
    return {"id": job_id, "title": "Engineer", "first_published": "2026-10-01T08:00:00-04:00", **extra}


def test_membership_rules_one_to_five() -> None:
    gh = adapter_for_fixture("greenhouse", "greenhouse_acme")
    ok = _page(json.dumps({"jobs": [_gh_job(1), _gh_job(2)], "meta": {"total": 2}}))
    gone = replace(ok, status=404)
    unnamed = _page(json.dumps({"jobs": [_gh_job(1), _gh_job(None)], "meta": {"total": 2}}))
    assert membership([ok], gh, reached_end=True).members == frozenset({"1", "2"})
    assert membership([gone], gh, reached_end=True).incomplete_reason == "non_2xx"
    missing = membership([unnamed], gh, reached_end=True)
    assert (missing.complete, missing.incomplete_reason, missing.members) == (
        False,
        "missing_id",
        frozenset({"1"}),
    )
    # every member is kept whatever the reason; the reason is the first rule that fails, in the §5.3.5 order
    assert membership([ok, gone], gh, reached_end=False).incomplete_reason == "non_2xx"
    assert membership([unnamed], gh, reached_end=False).incomplete_reason == "no_end"
    assert membership([], gh, reached_end=True).incomplete_reason == "no_end"  # no page, no end reached
    # membership comes from the list pages only: a failed SmartRecruiters detail does not taint it
    sr = adapter_for_fixture("smartrecruiters", "smartrecruiters_deliveryhero_list")
    listing = fixture_page("ats", "smartrecruiters_deliveryhero_list", SPEC, fetched_at=T)
    detail_spec = replace(
        SPEC, operation="detail", detail_native_id="744000156183719", parent_fetch_request_id=4
    )
    failed_detail = RawPage(detail_spec, 404, {}, b'{"message": "not found"}', T)
    draft = membership([listing, failed_detail], sr, reached_end=True)
    assert draft.complete and draft.member_count == 10


def test_items_without_an_id_or_title_are_rejected_and_repeats_are_duplicates() -> None:
    gh = adapter_for_fixture("greenhouse", "greenhouse_acme")
    jobs = [_gh_job(1), _gh_job(None), _gh_job(1), {**_gh_job(3), "title": None}, "x", _gh_job(True)]
    out = gh.parse(_page(json.dumps({"jobs": jobs, "meta": {"total": 6}})))
    assert [type(o) for o in out] == [Parsed, Rejected, DuplicateInPage, Rejected, Rejected, Rejected]
    assert isinstance(out[2], DuplicateInPage) and out[2].first_index == 0
    assert all(o.reason_code == "item_contract" for o in out if isinstance(o, Rejected))
    assert gh.peek_native_ids(_page(json.dumps({"jobs": jobs, "meta": {"total": 6}}))) == [
        "1",
        None,
        "1",
        "3",
        None,
        None,
    ]


def test_descriptions_are_sanitised_and_work_modes_come_from_structured_fields() -> None:
    gh = parsed_of("greenhouse", "greenhouse_getyourguide")
    assert all(p.description_html_sanitized and "&lt;" not in p.description_html_sanitized for p in gh)
    assert all("<" not in p.description_text and p.company_raw == "GetYourGuide" for p in gh)
    assert all(p.primary_link and p.primary_link.startswith("https://job-boards.greenhouse.io/") for p in gh)
    assert all(set(p.source_structured) <= {"requisition_id", "language"} for p in gh)
    assert any(p.source_structured.get("language") == "en" for p in gh)
    lever = parsed_of("lever", "lever_leverdemo")
    assert {frozenset(p.work_mode_possible) for p in lever} >= {frozenset({"remote"}), frozenset({"hybrid"})}
    assert all(p.remote_countries == () for p in lever if p.work_mode_possible != frozenset({"remote"}))
    eu = parsed_of("lever", "lever_eu_leverdemo")  # every posting `unspecified`: no stated remote work
    assert all(p.work_mode_source == "none" and p.remote_countries == () for p in eu)
    assert any("Skill Set:" in p.description_text for p in lever)  # the lists are part of the description
    assert all(p.company_raw == "leverdemo" for p in lever)  # no company field: the board's name
    ashby = parsed_of("ashby", "ashby_n8n")
    assert all(p.work_mode_source == "structured" and "remote" in p.work_mode_possible for p in ashby)
    assert all(p.employment_types == ("full_time",) and p.employment_source == "structured" for p in ashby)
    per = parsed_of("personio", "personio_everphone")
    assert all(p.primary_link == f"https://everphone.jobs.personio.de/job/{p.native_id}" for p in per)
    assert all(
        set(p.source_structured) <= {"seniority", "employmentType", "yearsOfExperience", "occupationCategory"}
        for p in per
    )
    assert any(p.employment_types == ("full_time", "internship") for p in per)
    assert all(p.locations and p.locations[0].resolution == "unique" for p in per)  # the postal code
    rec = parsed_of("recruitee", "recruitee_bunq")
    assert all(p.work_mode_possible == frozenset({"hybrid"}) for p in rec)
    assert any(p.employment_types == ("internship",) for p in rec)


def _sr_page(items: list[dict[str, Any]], *, total: int | None = None, spec: RequestSpec = SPEC) -> RawPage:
    doc = {"offset": 0, "limit": 100, "totalFound": len(items) if total is None else total, "content": items}
    return _page(json.dumps(doc), spec=spec)


def _sr_item(posting_id: str, **location: Any) -> dict[str, Any]:
    return {
        "id": posting_id,
        "name": "Engineer",
        "releasedDate": "2026-10-01T00:00:00Z",
        "location": location,
    }


def test_smartrecruiters_scope_rule_and_detail_freshness() -> None:
    a = adapter_for_fixture("smartrecruiters", "smartrecruiters_acme")
    assert isinstance(a, SmartRecruitersAdapter)
    items = [
        _sr_item("1", city="Berlin", country="de", remote=False),
        _sr_item("2", city="Madrid", country="es", remote=False, hybrid=True),  # outside, not remote
        _sr_item("3", city="Madrid", country="es", remote=True),  # outside but remote: in scope
        _sr_item("4", fullLocation="Paris, France"),  # no country code: the text is positive evidence
        _sr_item("5"),  # no location at all: in scope
        _sr_item("6", city="Berlin", country="de"),
    ]
    page = _sr_page(items, spec=replace(SPEC, page_no=3, request_class="free"))
    out = a.parse(page)
    assert [(type(o).__name__, getattr(o, "reason", None)) for o in out] == [
        ("OutOfScope", "detail_call"),
        ("OutOfScope", "not_germany"),
        ("OutOfScope", "detail_call"),
        ("OutOfScope", "not_germany"),
        ("OutOfScope", "detail_call"),
        ("OutOfScope", "detail_call"),
    ]
    assert all(o.item_ts == datetime(2026, 10, 1, tzinfo=UTC) for o in out)
    fresh, changed = T - DETAIL_MAX_AGE, b"\x00" * 32
    known = {
        "1": (a.list_item_sha256(page, 0), fresh),  # at the age limit: still fresh
        "3": (changed, T),  # its list item changed
        "6": (a.list_item_sha256(page, 5), None),  # never fetched in detail
    }
    specs = a.detail_specs(page, known, now=T, parent_fetch_request_id=99)
    assert [s.detail_native_id for s in specs] == ["3", "5", "6"]
    assert all(
        (s.operation, s.page_no, s.parent_fetch_request_id, s.request_class, s.params, s.cell_id, s.window)
        == ("detail", 3, 99, "free", {}, SPEC.cell_id, SPEC.window)
        for s in specs
    )
    older = {"1": (a.list_item_sha256(page, 0), fresh - timedelta(seconds=1))}
    assert "1" in [s.detail_native_id for s in a.detail_specs(page, older, now=T)]
    assert (
        a.list_item_sha256(page, 0) != a.list_item_sha256(page, 5) and len(a.list_item_sha256(page, 0)) == 32
    )
    # a hash depends on the item, not on the order of its keys
    reordered = _sr_page([dict(reversed(list(items[0].items())))])
    assert a.list_item_sha256(reordered, 0) == a.list_item_sha256(page, 0)
    # an id that cannot name a path segment never becomes a detail request
    odd = _sr_page([_sr_item("../../admin", country="de"), _sr_item("7", country="de")])
    assert [s.detail_native_id for s in a.detail_specs(odd, {}, now=T)] == ["7"]


def test_a_smartrecruiters_detail_must_be_the_posting_asked_for() -> None:
    a = adapter_for_fixture("smartrecruiters", "smartrecruiters_deliveryhero_detail_1")
    assert isinstance(a, SmartRecruitersAdapter)
    page = fixture_page("ats", "smartrecruiters_deliveryhero_detail_1", SPEC, fetched_at=T)
    asked = replace(SPEC, operation="detail", detail_native_id="744000156183719", parent_fetch_request_id=1)
    a.check_envelope(replace(page, spec=asked))
    with pytest.raises(ContractViolation, match="detail_id_mismatch"):
        a.check_envelope(replace(page, spec=replace(asked, detail_native_id="744000156094379")))
    detail = a.parse_detail(page)
    assert isinstance(detail, Parsed)
    posting = detail.normalized
    assert posting.company_raw == "Delivery Hero" and posting.posted_precision == "exact"
    assert posting.work_mode_possible == frozenset({"onsite"}) and posting.work_mode_source == "structured"
    assert posting.source_structured == {"seniority": "mid_senior_level"}
    assert posting.employment_types == ("full_time",)
    # the coordinates are the location; Dubai is in no gazetteer list (§8.12), so it stays unresolved
    assert [loc.label for loc in posting.locations] == ["Dubai, Dubai, United Arab Emirates"]
    assert posting.locations[0].resolution == "unresolved" and posting.location_kind == "city"
    assert (
        posting.primary_link
        == "https://jobs.smartrecruiters.com/DeliveryHero/744000156183719-analytics-engineer-ii"
    )
    hybrid = a.parse_detail(fixture_page("ats", "smartrecruiters_deliveryhero_detail_2", SPEC, fetched_at=T))
    assert isinstance(hybrid, Parsed) and hybrid.normalized.work_mode_possible == frozenset({"hybrid"})


def test_fixture_helpers_read_the_board_from_the_case_name() -> None:
    lever = adapter_for_fixture("lever", "lever_eu_leverdemo")
    assert isinstance(lever, LeverAdapter) and lever.board == "eu:leverdemo"
    assert adapter_for_fixture("lever", "lever_leverdemo").board == "global:leverdemo"
    assert adapter_for_fixture("smartrecruiters", "smartrecruiters_deliveryhero_list").board == "DeliveryHero"
    assert adapter_for_fixture("personio", "personio_everphone").host == "everphone.jobs.personio.de"
    assert len(parsed_of("greenhouse", "greenhouse_getyourguide")) == 15
    assert parsed_of("smartrecruiters", "smartrecruiters_deliveryhero_list") == []  # all out of scope
