"""`SourceAdapter.elements` (ruling T15-a): the canonical bytes of each element of a page's item array."""

import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from xml.etree.ElementTree import canonicalize, tostring

import httpx
import pytest
from defusedxml.ElementTree import fromstring
from jobhunter.clock import utcnow
from jobhunter.db.seed import canonical_json
from jobhunter.sources.adzuna import AdzunaAdapter
from jobhunter.sources.arbeitnow import ArbeitnowAdapter
from jobhunter.sources.ats.smartrecruiters import SmartRecruitersAdapter
from jobhunter.sources.base import ContractViolation, RawPage, SourceAdapter, SourceError, element_sha256
from jobhunter.sources.jsearch import JSearchAdapter
from jobhunter.sources.serpapi import SerpApiAdapter
from jobhunter.sources.watchlist_patterns import AtsType
from jobhunter.types import JSONValue

from tests.support.sources import adapter_for_fixture, fixture_page, load_fixture
from tests.unit.test_sources_base import SPEC

T = datetime(2026, 10, 16, 9, 47, tzinfo=UTC)
Aggregators = dict[str, SourceAdapter]


@pytest.fixture
async def aggregators() -> AsyncIterator[Aggregators]:
    """The four aggregator adapters, instance 1, on a client that sends nothing and is closed afterwards."""
    async with httpx.AsyncClient() as client:
        yield {
            "jsearch": JSearchAdapter(client, api_key=b"fake", instance_id=1, clock=utcnow),
            "serpapi": SerpApiAdapter(client, api_key=b"fake", instance_id=1, clock=utcnow),
            "adzuna": AdzunaAdapter(client, app_id=b"fake", app_key=b"fake", instance_id=1, clock=utcnow),
            "arbeitnow": ArbeitnowAdapter(client, instance_id=1, clock=utcnow),
        }


def json_items(doc: JSONValue, *path: str) -> list[JSONValue]:
    node = doc
    for key in path:
        assert isinstance(node, dict)
        node = node[key]
    assert isinstance(node, list)
    return node


@pytest.mark.parametrize(
    ("source", "case", "path", "n"),
    [
        ("jsearch", "search_v2_page1", ("data", "jobs"), 10),
        ("serpapi", "google_jobs_hl_en", ("jobs_results",), 10),
        ("adzuna", "adzuna_reingest_pair", ("results",), None),
        ("arbeitnow", "page1", ("data",), 30),
    ],
)
def test_a_json_element_is_its_canonical_json(
    aggregators: Aggregators, source: str, case: str, path: tuple[str, ...], n: int | None
) -> None:
    adapter, page = aggregators[source], fixture_page(source, case, SPEC, fetched_at=T)
    expected = [canonical_json(item) for item in json_items(json.loads(page.body), *path)]
    elements = adapter.elements(page)
    assert elements == expected and (n is None or len(elements) == n)
    assert len(elements) == len(adapter.parse(page)) == len(adapter.peek_native_ids(page))


@pytest.mark.parametrize(
    ("ats", "case", "path"),
    [
        ("greenhouse", "greenhouse_getyourguide", ("jobs",)),
        ("lever", "lever_leverdemo", ()),
        ("ashby", "ashby_n8n", ("jobs",)),
        ("recruitee", "recruitee_bunq", ("offers",)),
        ("smartrecruiters", "smartrecruiters_deliveryhero_list", ("content",)),
    ],
)
def test_a_json_board_element_is_its_canonical_json(ats: AtsType, case: str, path: tuple[str, ...]) -> None:
    adapter, page = adapter_for_fixture(ats, case), fixture_page("ats", case, SPEC, fetched_at=T)
    expected = [canonical_json(item) for item in json_items(json.loads(page.body), *path)]
    elements = adapter.elements(page)
    assert elements == expected and elements
    assert len(elements) == len(adapter.parse(page)) == len(adapter.peek_native_ids(page))


@pytest.mark.parametrize(
    ("source", "case"),
    [
        ("jsearch", "search_v2_empty"),
        ("serpapi", "google_jobs_empty"),
        ("adzuna", "search_empty"),
        ("arbeitnow", "page_end"),
    ],
)
def test_a_page_without_results_has_no_elements(aggregators: Aggregators, source: str, case: str) -> None:
    adapter, page = aggregators[source], fixture_page(source, case, SPEC, fetched_at=T)
    assert adapter.elements(page) == [] == adapter.parse(page)


def test_elements_do_not_depend_on_key_order_or_whitespace(aggregators: Aggregators) -> None:
    adapter, page = aggregators["arbeitnow"], fixture_page("arbeitnow", "page1", SPEC, fetched_at=T)
    doc = json.loads(page.body)
    doc["data"] = [dict(reversed(list(job.items()))) for job in doc["data"]]
    rewritten = replace(page, body=json.dumps(doc, indent=3).encode("utf-8"))
    assert adapter.elements(rewritten) == adapter.elements(page)


def test_a_smartrecruiters_detail_page_is_one_element_and_list_items_share_the_helper() -> None:
    adapter = adapter_for_fixture("smartrecruiters", "smartrecruiters_deliveryhero_list")
    assert isinstance(adapter, SmartRecruitersAdapter)
    detail_spec = replace(
        SPEC, operation="detail", detail_native_id="744000156183719", parent_fetch_request_id=4
    )
    detail = fixture_page("ats", "smartrecruiters_deliveryhero_detail_1", detail_spec, fetched_at=T)
    assert adapter.elements(detail) == [canonical_json(json.loads(detail.body))]
    assert len(adapter.parse(detail)) == 1
    listing = fixture_page("ats", "smartrecruiters_deliveryhero_list", SPEC, fetched_at=T)
    elements = adapter.elements(listing)
    assert [adapter.list_item_sha256(listing, i) for i in range(len(elements))] == [
        hashlib.sha256(element).digest() for element in elements
    ]
    assert [element_sha256(e) for e in elements] == [hashlib.sha256(e).digest() for e in elements]


def test_a_personio_element_is_the_c14n_of_its_position() -> None:
    adapter = adapter_for_fixture("personio", "personio_everphone")
    page = fixture_page("ats", "personio_everphone", SPEC, fetched_at=T)
    positions = fromstring(page.body).findall("position")
    elements = adapter.elements(page)
    assert elements == [canonicalize(tostring(p, encoding="unicode")).encode("utf-8") for p in positions]
    assert len(elements) == len(adapter.parse(page)) == 8
    assert all(e.startswith(b"<position>") and e.endswith(b"</position>") for e in elements)
    # C14N 2.0: the attribute order, an empty element's form and the whitespace inside a tag do not matter
    one = (
        b'<workzag-jobs><position kind="a" lang="de"><id>1</id><name>Dev</name><x/></position></workzag-jobs>'
    )
    two = (
        b'<workzag-jobs >\n<position lang="de"  kind="a"><id>1</id><name>Dev</name><x></x></position>\n'
        b"</workzag-jobs>"
    )
    status, headers, _ = load_fixture("ats", "personio_everphone")
    first, second = (adapter.elements(RawPage(SPEC, status, headers, body, T)) for body in (one, two))
    assert first == second and len(first) == 1


@pytest.mark.parametrize(
    ("source", "case"),
    [("jsearch", "search_v2_drift"), ("jsearch", "search_v2_html"), ("arbeitnow", "page_truncated")],
)
def test_elements_read_the_envelope_as_check_envelope_does(
    aggregators: Aggregators, source: str, case: str
) -> None:
    adapter, page = aggregators[source], fixture_page(source, case, SPEC, fetched_at=T)
    with pytest.raises(SourceError) as checked:
        adapter.check_envelope(page)
    with pytest.raises(type(checked.value)) as read:
        adapter.elements(page)
    assert read.value.code == checked.value.code
    personio = adapter_for_fixture("personio", "personio_entity")
    with pytest.raises(ContractViolation, match="xml_entity"):
        personio.elements(fixture_page("ats", "personio_entity", SPEC, fetched_at=T))
