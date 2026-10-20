"""Test helpers over the scrubbed source fixtures (spec §18.4; Task 6); Tasks 11 to 15 add fixtures here.

`load_fixture` reads a case the way the fixture transport serves it (`fixtures.load_case`); `fixture_page`
turns it into the `RawPage` an adapter parses; `mock_fixture` serves it on a respx route; `served_requests`
reads the fixture transport's request log (Task 5), each `case` reduced to its name (`search_v2_page1`);
`make_cell_plan` builds the `CellPlan` an adapter's `plan` takes (Task 9: a JSearch cell for Berlin, the
`week` bucket), with any field changed.

Task 11: `adapter_for_fixture` is the ATS adapter of the board an `ats/` case was recorded from (its slug, and
for Lever its region, read from the case name), instance 1, on a client it never uses; `parsed_of` is the
postings that adapter parses from the case. The `fake_http` fixture configures the HTTP layer for fake mode
(`http.configure`, the fixture transport over `FIXTURES`) and restores the process-wide state it found.

Task 12: `fixture_log` is a fresh request log under `tmp_path` (the crash tests of Task 14 hand it to their
workers as `JOBHUNTER_FIXTURE_LOG`): every adapter fixture on the fixture transport logs its real sends to it,
which `served_requests` reads back. `fixture_variant` copies one source's recordings and serves some cases as
others (with headers changed). The adapter fixtures are the adapters of the default instances, clock
`utcnow`, keys `b"fake"`: `jsearch_fixture_adapter` on the fixture transport over `FIXTURES`,
`jsearch_html_adapter` the same over a variant in which page 1 is the HTML error page `search_v2_html`,
`jsearch_variant` a factory of such variants, `serpapi_fixture_adapter` on respx (the routes
`serpapi_account` and `serpapi_search`, so the account reads can be counted; the recorded account's renewal is
moved 20 days ahead of today by `serpapi_account_response`, so its window is current whatever the date) and
`arbeitnow_adapter` on a client that respx mocks (the test adds the routes).

Task 14: `fake_http` also logs every request its fixture transport serves to `fixture_log`; `adapters` is the
fixture-backed adapter factory of the fetch nodes (`Callable[[InstanceRow], SourceAdapter]`): every adapter on
`fake_http`'s fixture transport (one `http.make_client` per source), clock `utcnow`, keys `b"fake"`. Task 24:
`fake_http` yields its fake-mode worker configuration, and `adapters` is the worker's own factory on it
(`registry.build_adapter` over `registry.make_clients`).

Task 15: `served_response` acquires one page for a run through the one-page protocol (`acquire_page`) and
returns its stored response; `arbeitnow_spec` is a page spec of the Arbeitnow default instance's cell. Two
fixtures give an admitted run (`queued_run`) with one stored response, as `(RunOwner, raw_response_id)`:
`stored_arbeitnow_page`, the recorded Arbeitnow `page1` (30 elements) served on respx, and `crafted_page`, a
JSearch page derived from the recorded `search_v2_page1` (`crafted_jsearch_jobs`: five jobs, element 3 repeats
element 1's `job_uid`, element 4, the fifth, has none), served on respx as page 1 of the `remote-de` cell.
"""

import json
import re
import shutil
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
import respx
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig, load_config
from jobhunter.db.sql import q
from jobhunter.quota.windows import Provider
from jobhunter.sources import ats as ats_adapters
from jobhunter.sources import http, registry
from jobhunter.sources.acquire import acquire_page
from jobhunter.sources.arbeitnow import FEED_URL, ArbeitnowAdapter
from jobhunter.sources.ats.common import AtsAdapter
from jobhunter.sources.base import (
    Billing,
    CellPlan,
    NormalizedPosting,
    Parsed,
    RawPage,
    RequestSpec,
    SourceAdapter,
)
from jobhunter.sources.fixtures import fixture_transport, load_case, renewed_account
from jobhunter.sources.instances import InstanceRow
from jobhunter.sources.jsearch import SEARCH_URL as JSEARCH_URL
from jobhunter.sources.jsearch import JSearchAdapter
from jobhunter.sources.serpapi import ACCOUNT_URL, SEARCH_URL, SerpApiAdapter
from jobhunter.sources.watchlist_patterns import AtsType
from jobhunter.types import JSONValue, get_list, get_object
from jobhunter.worker.runs import RunOwner
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import acquire_ctx, instance_id, jsearch_cell, page1_spec, queued_run, source_cell

FIXTURES: Final = Path("tests/contract/fixtures")
_NOW: Final = datetime(2026, 10, 16, 9, 47, tzinfo=UTC)


def load_fixture(source: str, case: str) -> tuple[int, dict[str, str], bytes]:
    """The status, the headers (lower-case names, `content-type` always set) and the body of a case."""
    return load_case(FIXTURES, f"{source}/{case}")


def fixture_page(source: str, case: str, spec: RequestSpec, *, fetched_at: datetime) -> RawPage:
    """The recorded response of `<source>/<case>` as the page of `spec`, arrived at `fetched_at`."""
    status, headers, body = load_fixture(source, case)
    return RawPage(spec, status, headers, body, fetched_at)


def mock_fixture(
    respx_mock: respx.MockRouter, method: str, url_pattern: str | re.Pattern[str], source: str, case: str
) -> respx.Route:
    """A respx route answering `method url_pattern` with the case (a URL without query matches any query)."""
    status, headers, body = load_fixture(source, case)
    route = respx_mock.route(method=method, url=url_pattern)
    route.mock(return_value=httpx.Response(status, headers=headers, content=body))
    return route


def served_requests(log_path: Path) -> list[dict[str, str]]:
    """The lines of the fixture transport's request log, every value a string, `case` the case name only
    (`""` for a request that no route matched); an absent log is no request."""
    if not log_path.exists():
        return []
    served: list[dict[str, str]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        case = entry.get("case")
        served.append(
            {
                **{key: "" if value is None else str(value) for key, value in entry.items()},
                "case": "" if case is None else str(case).rsplit("/", 1)[-1],
            }
        )
    return served


def make_cell_plan(**changes: Any) -> CellPlan:
    """A JSearch cell for `python developer` in Berlin, bucket `week`, with `changes` applied."""
    base = CellPlan(
        cell_id=7,
        source_id="jsearch",
        instance_id=1,
        query_text="python developer",
        query_lang="en",
        location_key="berlin",
        location_label="Berlin",
        radius_km=25,
        phase="primary",
        request_class="normal",
        bucket="week",
        window=(datetime(2026, 10, 9, 9, 47, tzinfo=UTC), _NOW),
        max_days_old=None,
    )
    return replace(base, **changes)


# The boards the `ats/` cases were recorded from, by the first word of the case name after the ATS, where the
# board's slug is not that word as it stands (SmartRecruiters slugs keep their case).
_RECORDED_SLUGS: Final = {"deliveryhero": "DeliveryHero"}
_FIXTURE_SPEC: Final = RequestSpec(
    source_instance_id=1,
    operation="list",
    cell_id=7,
    page_no=1,
    params={},
    cursor_in=None,
    detail_native_id=None,
    parent_fetch_request_id=None,
    window=None,
    request_class="free",
)


def adapter_for_fixture(ats: AtsType, case: str) -> AtsAdapter:
    """The adapter of the board `ats/<case>` was recorded from: `<ats>_<slug>_…` (`lever_eu_<site>_…` for
    a Lever EU board), instance 1, clock `utcnow`, on a client it never uses."""
    prefix = f"{ats}_"
    if not case.startswith(prefix):
        raise ValueError(f"case {case!r} is not a {ats} case")
    rest = case.removeprefix(prefix)
    region = "global"
    if ats == "lever" and rest.startswith("eu_"):
        region, rest = "eu", rest.removeprefix("eu_")
    word = rest.split("_", 1)[0]
    slug = _RECORDED_SLUGS.get(word, word)
    instance = InstanceRow(
        id=1,
        source_id=ats,
        instance_key=f"{region}:{slug}" if ats == "lever" else slug,
        watchlist_company_id=None,
        health="ok",
        half_open_after=None,
        board_slug=slug,
        lever_region=region if ats == "lever" else None,
        display_name=None,
    )
    return ats_adapters.adapter_for(instance, httpx.AsyncClient(), clock=utcnow)


def parsed_of(ats: AtsType, case: str) -> list[NormalizedPosting]:
    """The postings the adapter of `ats/<case>` parses from that case (its `Parsed` items, in order)."""
    page = fixture_page("ats", case, _FIXTURE_SPEC, fetched_at=_NOW)
    return [o.normalized for o in adapter_for_fixture(ats, case).parse(page) if isinstance(o, Parsed)]


@pytest.fixture
def fixture_log(tmp_path: Path) -> Path:
    """A request log of the fixture transport that no request has been written to yet."""
    return tmp_path / "fixture-requests.jsonl"


@pytest.fixture
def fake_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture_log: Path) -> Iterator[AppConfig]:
    """`http.configure` in fake mode over `FIXTURES` (every new client is served by the fixture transport,
    which logs to `fixture_log`); the process-wide configuration it found is restored after the test. Yields
    that fake-mode worker configuration (no secret)."""
    env = {
        "JOBHUNTER_ENV": "test",
        "JOBHUNTER_DB_NAME": "jobhunter",
        "JOBHUNTER_FAKE_SOURCES": "1",
        "JOBHUNTER_FIXTURES_DIR": str(FIXTURES.resolve()),
        "JOBHUNTER_FIXTURE_LOG": str(fixture_log),
    }
    monkeypatch.setattr(http, "_configuration", http._configuration)  # restored on teardown
    cfg = load_config("worker", env, tmp_path / "no-secrets")
    http.configure(cfg)
    yield cfg


Adapters = Callable[[InstanceRow], SourceAdapter]


@pytest.fixture
async def adapters(fake_http: AppConfig) -> AsyncIterator[Adapters]:
    """The adapter of an instance on `fake_http`'s fixture transport: the worker's factory
    (`registry.build_adapter` on `registry.make_clients`), clock `utcnow`, keys `b"fake"` (Task 24)."""
    clients = registry.make_clients(fake_http)

    def adapter_of(instance: InstanceRow) -> SourceAdapter:
        return registry.build_adapter(instance, fake_http, clients)

    try:
        yield adapter_of
    finally:
        for made in clients.values():
            await made.aclose()


# A case served as another one: the other case's name, or the name with headers replaced.
Variant = str | tuple[str, Mapping[str, str]]


def fixture_variant(root: Path, source: str, cases: Mapping[str, Variant]) -> Path:
    """A fixtures directory under `root` holding `source`'s recordings, in which each case named in `cases`
    is the recording it maps to (its body and headers, the given headers replaced); returns the directory."""
    target = root / "fixtures"
    shutil.copytree(FIXTURES / source, target / source)
    for case, variant in cases.items():
        other, changed = (variant, {}) if isinstance(variant, str) else variant
        for stale in (target / source).glob(f"{case}.*"):
            stale.unlink()
        for recorded in (FIXTURES / source).glob(f"{other}.*"):
            if recorded.name.endswith(".meta.json"):
                continue
            suffix = recorded.name.removeprefix(other)
            copy = target / source / f"{case}{suffix}"
            if suffix == ".headers.json":
                doc = json.loads(recorded.read_text("utf-8"))
                doc["headers"] = {**doc["headers"], **changed}
                copy.write_text(json.dumps(doc, indent=2), "utf-8")
            else:
                shutil.copyfile(recorded, copy)
    return target


_DEFAULT_INSTANCE: Final = q(
    "SELECT id FROM source_instances WHERE source_id = :s AND instance_key = 'default'"
)


async def _default_instance(engine: AsyncEngine, source_id: str) -> int:
    """The id of the source's `default` instance (ids change with every database reset)."""
    async with engine.connect() as conn:
        return int((await conn.execute(_DEFAULT_INSTANCE, {"s": source_id})).scalar_one())


JSearchVariant = Callable[[Mapping[str, Variant]], JSearchAdapter]


@pytest.fixture
async def jsearch_variant(
    engine: AsyncEngine, fixture_log: Path, tmp_path: Path
) -> AsyncIterator[JSearchVariant]:
    """`jsearch_variant({"search_v2_page1": "search_v2_html"})`: a JSearch adapter of the default instance
    over a `fixture_variant` of the JSearch recordings, logging to `fixture_log`."""
    instance = await _default_instance(engine, "jsearch")
    clients: list[httpx.AsyncClient] = []

    def make(cases: Mapping[str, Variant]) -> JSearchAdapter:
        directory = fixture_variant(tmp_path / f"variant-{len(clients)}", "jsearch", cases)
        client = httpx.AsyncClient(transport=fixture_transport(directory, log_path=fixture_log))
        clients.append(client)
        return JSearchAdapter(client, api_key=b"fake", instance_id=instance, clock=utcnow)

    try:
        yield make
    finally:
        for client in clients:
            await client.aclose()


@pytest.fixture
async def jsearch_fixture_adapter(engine: AsyncEngine, fixture_log: Path) -> AsyncIterator[JSearchAdapter]:
    """The JSearch adapter of the default instance on the fixture transport over `FIXTURES`."""
    instance = await _default_instance(engine, "jsearch")
    async with httpx.AsyncClient(transport=fixture_transport(FIXTURES, log_path=fixture_log)) as client:
        yield JSearchAdapter(client, api_key=b"fake", instance_id=instance, clock=utcnow)


@pytest.fixture
def jsearch_html_adapter(jsearch_variant: JSearchVariant) -> JSearchAdapter:
    """The JSearch adapter whose page 1 is the HTML error page delivered with a 200 (`search_v2_html`).

    Its `x-ratelimit-requests-remaining` is 199 instead of the recorded 198: the provider has counted this one
    request, as the fresh test ledger has, so the response is no evidence of unaccounted usage and the ledger
    holds the request's own entry only (no `adjustment` row besides it)."""
    return jsearch_variant({"search_v2_page1": ("search_v2_html", {"x-ratelimit-requests-remaining": "199"})})


def serpapi_account_response(case: str = "account_before", **changes: JSONValue) -> httpx.Response:
    """The recorded SerpApi account read `case` as a response, its renewal
    `fixtures.ACCOUNT_RENEWAL_AHEAD` after today (UTC, `fixtures.renewed_account`, as the fake mode's
    transport serves it) and its other fields changed by `changes`."""
    status, headers, body = load_fixture("serpapi", case)
    return httpx.Response(status, headers=headers, content=renewed_account(body, utcnow().date(), **changes))


@pytest.fixture
async def serpapi_fixture_adapter(
    engine: AsyncEngine, respx_mock: respx.MockRouter
) -> AsyncIterator[SerpApiAdapter]:
    """The SerpApi adapter of the default instance; respx answers `account.json` (route `serpapi_account`,
    `serpapi_account_response()`) and `search.json` (route `serpapi_search`, `google_jobs_hl_en`)."""
    instance = await _default_instance(engine, "serpapi")
    respx_mock.get(ACCOUNT_URL, name="serpapi_account").mock(side_effect=lambda _: serpapi_account_response())
    status, headers, body = load_fixture("serpapi", "google_jobs_hl_en")
    search = respx_mock.get(SEARCH_URL, name="serpapi_search")
    search.mock(return_value=httpx.Response(status, headers=headers, content=body))
    async with httpx.AsyncClient() as client:
        yield SerpApiAdapter(client, api_key=b"fake", instance_id=instance, clock=utcnow)


@pytest.fixture
async def arbeitnow_adapter(
    engine: AsyncEngine, respx_mock: respx.MockRouter
) -> AsyncIterator[ArbeitnowAdapter]:
    """The Arbeitnow adapter of the default instance on a client that respx mocks (the test adds routes)."""
    instance = await _default_instance(engine, "arbeitnow")
    async with httpx.AsyncClient() as client:
        yield ArbeitnowAdapter(client, instance_id=instance, clock=utcnow)


# ---- Task 15 ----------------------------------------------------------------------------------------------

StoredPage = tuple[RunOwner, int]
_RAW_RESPONSE_OF: Final = q("SELECT raw_response_id FROM fetch_requests WHERE id = :i")


async def served_response(
    engine: AsyncEngine,
    owner: RunOwner,
    adapter: SourceAdapter,
    spec: RequestSpec,
    *,
    billing: Billing,
    provider: Provider | None,
) -> int:
    """The `raw_responses` id of `spec`'s page, acquired for `owner` through `acquire_page` (`acquire_ctx`);
    a page that failed with a response (a `failed('contract')` 200) has its stored response too."""
    ctx = acquire_ctx(engine, owner, adapter, billing=billing, provider=provider)
    result = await acquire_page(ctx, spec, cell_id=spec.cell_id, phase="primary", planned_id=None)
    async with engine.connect() as conn:
        raw_response_id = (await conn.execute(_RAW_RESPONSE_OF, {"i": result.fetch_request_id})).scalar()
    if raw_response_id is None:
        raise RuntimeError(f"served_response: the page of request {result.fetch_request_id} stored nothing")
    return int(raw_response_id)


async def arbeitnow_spec(engine: AsyncEngine, adapter: ArbeitnowAdapter, page_no: int = 1) -> RequestSpec:
    """Page `page_no` of the Arbeitnow default instance's cell (created when absent), a free request."""
    cell = await source_cell(engine, "arbeitnow", query=None, location_key=None)
    async with engine.connect() as conn:
        instance = await instance_id(conn, "arbeitnow")
    now = utcnow()
    plan = make_cell_plan(
        cell_id=cell,
        source_id="arbeitnow",
        instance_id=instance,
        query_text=None,
        query_lang=None,
        location_key=None,
        location_label=None,
        radius_km=None,
        request_class="free",
        bucket=None,
        window=(now - timedelta(days=1), now),
    )
    first = adapter.plan(plan)
    return first if page_no == 1 else replace(first, page_no=page_no)


@pytest.fixture
async def stored_arbeitnow_page(
    engine: AsyncEngine, current_epoch: int, arbeitnow_adapter: ArbeitnowAdapter, respx_mock: respx.MockRouter
) -> StoredPage:
    """An admitted run with one served Arbeitnow page-1 receipt (the recorded `page1`, 30 elements)."""
    mock_fixture(respx_mock, "GET", FEED_URL, "arbeitnow", "page1")
    owner = await queued_run(engine)
    spec = await arbeitnow_spec(engine, arbeitnow_adapter)
    return owner, await served_response(engine, owner, arbeitnow_adapter, spec, billing="free", provider=None)


def crafted_jsearch_jobs() -> list[JSONValue]:
    """Five jobs of the recorded `search_v2_page1`: element 3 is the recorded fourth job with element 1's
    `job_uid`, element 4 the recorded fifth job without its `job_uid`."""
    jobs = get_list(get_object(json.loads(load_fixture("jsearch", "search_v2_page1")[2]), "data"), "jobs")
    first, second, third, fourth, fifth = (dict(get_object(job, "")) for job in jobs[:5])
    del fifth["job_uid"]
    return [first, second, third, {**fourth, "job_uid": second["job_uid"]}, fifth]


@pytest.fixture
async def crafted_page(engine: AsyncEngine, current_epoch: int, respx_mock: respx.MockRouter) -> StoredPage:
    """An admitted run with one served JSearch page-1 receipt whose body is the recorded `search_v2_page1`
    with `crafted_jsearch_jobs` as its jobs. Its `x-ratelimit-requests-remaining` is 199 (the fresh ledger's
    count), so the observation adjusts nothing."""
    status, headers, body = load_fixture("jsearch", "search_v2_page1")
    doc = json.loads(body)
    doc["data"]["jobs"] = crafted_jsearch_jobs()
    respx_mock.get(JSEARCH_URL).mock(
        return_value=httpx.Response(
            status,
            headers={**headers, "x-ratelimit-requests-remaining": "199"},
            content=json.dumps(doc).encode("utf-8"),
        )
    )
    owner = await queued_run(engine)
    spec = await page1_spec(engine, await jsearch_cell(engine))
    async with httpx.AsyncClient() as client:
        adapter = JSearchAdapter(client, api_key=b"fake", instance_id=spec.source_instance_id, clock=utcnow)
        return owner, await served_response(engine, owner, adapter, spec, billing="paid", provider="jsearch")
