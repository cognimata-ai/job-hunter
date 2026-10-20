import asyncio
import contextlib
import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
import pytest
import respx
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.seed import canonical_json
from jobhunter.errors import FencedOut
from jobhunter.graphs.collect.deps import CollectDeps
from jobhunter.graphs.collect.graph import TIMEOUTS, build_collect_graph
from jobhunter.graphs.collect.nodes import fetch as fetch_node
from jobhunter.graphs.collect.nodes.fetch import (
    acquire_context,
    load_planned,
    make_fetch_cell,
    traverse_and_record,
)
from jobhunter.graphs.collect.nodes.parse_normalize import make_parse_normalize
from jobhunter.graphs.collect.nodes.plan import make_plan
from jobhunter.graphs.collect.nodes.plan_fallback import make_plan_fallback
from jobhunter.graphs.collect.nodes.resolve_identity import make_resolve_identity
from jobhunter.graphs.collect.state import initial_state
from jobhunter.observability.metrics import REGISTRY
from jobhunter.sources import acquire, coverage, traverse
from jobhunter.sources.acquire import Denied, acquire_page
from jobhunter.sources.adzuna import SEARCH_URL as ADZUNA_URL
from jobhunter.sources.adzuna import AdzunaAdapter
from jobhunter.sources.arbeitnow import FEED_URL, ArbeitnowAdapter
from jobhunter.sources.ats import adapter_for
from jobhunter.sources.ats.smartrecruiters import SmartRecruitersAdapter
from jobhunter.sources.base import element_sha256
from jobhunter.sources.breaker import record_failure
from jobhunter.sources.fixtures import fixture_transport
from jobhunter.sources.instances import InstanceRow, list_instances
from jobhunter.sources.ratelimit import SMARTRECRUITERS_DETAIL, SOURCE_LIMITS, RateLimiter
from jobhunter.sources.serpapi import SerpApiAdapter
from jobhunter.worker import runs
from jobhunter.worker.runs import RunOwner, finalize_run
from jobhunter.worker.tasks import defer_in_tx
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.clock import FakeClock
from tests.support.collect import (
    TEST_QUERY,
    JobFactory,
    instance_id,
    make_deps,
    queued_run,
    seed_inputs,
    supersede,
)
from tests.support.faults import PausePoint
from tests.support.quota import SIX_H, insert_window, jsearch_window
from tests.support.sources import (
    Adapters,
    fixture_variant,
    load_fixture,
    serpapi_account_response,
    served_requests,
)

pytestmark = pytest.mark.integration
GAPS = sa.text(
    "SELECT i.kind, i.gap_status, i.reason FROM coverage_intervals i "
    "JOIN fetch_requests f ON f.cell_id = i.cell_id WHERE f.id = :i"
)
RUN_INTERVALS = sa.text(
    "SELECT kind, gap_status, reason, lower(span), upper(span), gap_cursor FROM coverage_intervals "
    "WHERE run_id = :r ORDER BY id"
)
PAGES = sa.text(
    "SELECT f.page_no, f.status, f.request_class, f.new_native_ids, l.request_class AS ledger_class "
    "FROM fetch_requests f LEFT JOIN quota_ledger l ON l.id = f.quota_ledger_id WHERE f.run_id = :r "
    "AND f.cell_id = :c ORDER BY f.id"
)
CELL_OF = sa.text("SELECT cell_id FROM fetch_requests WHERE id = :i")
SOURCES_OF = sa.text(
    "SELECT s.source_id, f.id, f.cell_id FROM fetch_requests f JOIN source_instances s "
    "ON s.id = f.source_instance_id WHERE f.id = ANY(:ids)"
)
H = timedelta(hours=1)


async def plan_ids(deps: CollectDeps, owner: RunOwner) -> list[int]:
    return (await make_plan(deps)(initial_state(owner.run_id, owner.attempt_no)))["primary_fetch_request_ids"]


async def rows(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> list[tuple[object, ...]]:
    async with engine.connect() as c:
        return [tuple(r) for r in await c.execute(statement, params)]


async def cell_of(engine: AsyncEngine, fetch_request_id: int) -> int:
    async with engine.connect() as c:
        return int((await c.execute(CELL_OF, {"i": fetch_request_id})).scalar_one())


async def by_source(engine: AsyncEngine, ids: list[int]) -> dict[str, tuple[int, int]]:
    """The planned requests `ids` by source: `(fetch request id, cell id)`."""
    async with engine.connect() as c:
        return {r.source_id: (int(r.id), int(r.cell_id)) for r in await c.execute(SOURCES_OF, {"ids": ids})}


async def finish(engine: AsyncEngine, owner: RunOwner) -> None:
    async with engine.begin() as c:
        await finalize_run(c, owner, status="succeeded", reason=None, report={})


def fetched(source: str, status: str) -> float:
    labels = {"source": source, "status": status}
    return REGISTRY.get_sample_value("jobhunter_fetch_requests_total", labels) or 0.0


def page1_ids() -> list[str]:
    doc = json.loads(load_fixture("jsearch", "search_v2_page1")[2])
    return [job["job_uid"] for job in doc["data"]["jobs"]]


async def test_jsearch_page_two_rule(
    engine, current_epoch, adapters: Adapters, job_factory: JobFactory
) -> None:
    await seed_inputs(engine, queries=(TEST_QUERY,), enabled=("jsearch",))
    deps = make_deps(engine, adapters)
    first = await queued_run(engine)
    (fid,) = await plan_ids(deps, first)
    cell = await cell_of(engine, fid)
    before = fetched("jsearch", "succeeded")
    result = await traverse_and_record(deps, fid)
    assert fetched("jsearch", "succeeded") == before + 2  # FETCH_REQUESTS: one per page
    # page 1's 10 job_uids are all new: page 2 is requested (a normal reservation), and its cursor is left
    assert (result.end, result.reason, result.pages, result.items, len(result.outcomes)) == (
        "cursor",
        "page_cap",
        2,
        19,
        2,
    )
    assert await rows(engine, PAGES, r=first.run_id, c=cell) == [
        (1, "succeeded", "probe", 10, "probe"),
        (2, "succeeded", "normal", 9, "normal"),
    ]
    await finish(engine, first)
    for uid in page1_ids()[:4]:  # 4 of page 1's ids are postings already: 6 new, below PAGE2_MIN_NEW
        await job_factory(native_id=uid)
    second = await queued_run(engine)
    (fid,) = await plan_ids(deps, second)
    result = await traverse_and_record(deps, fid)
    assert (result.end, result.reason, result.pages, len(result.outcomes)) == ("cursor", "page_cap", 1, 1)
    assert [r[:4] for r in await rows(engine, PAGES, r=second.run_id, c=cell)] == [
        (1, "succeeded", "normal", 6)
    ]
    cursor = json.loads(load_fixture("jsearch", "search_v2_page1")[2])["data"]["cursor"]
    (gap,) = await rows(engine, RUN_INTERVALS, r=second.run_id)
    assert (gap[:3], gap[5]) == (("gap", "open", "page_cap"), {"cursor": cursor})  # one open gap, page 2 left


def adzuna_full_page(count: int) -> httpx.Response:
    status, headers, body = load_fixture("adzuna", "search_p1_rpp100")
    return httpx.Response(status, headers=headers, json={**json.loads(body), "count": count})


def arbeitnow_page(case: str, **first_item: object) -> httpx.Response:
    status, headers, body = load_fixture("arbeitnow", case)
    doc = json.loads(body)
    doc["data"][0] = {**doc["data"][0], **first_item}
    return httpx.Response(status, headers=headers, json=doc)


def feed_times() -> tuple[datetime, datetime]:
    """The oldest and the newest `created_at` of the fixture feed's pages 1 and 2."""
    times = [
        datetime.fromtimestamp(job["created_at"], UTC)
        for case in ("page1", "page2")
        for job in json.loads(load_fixture("arbeitnow", case)[2])["data"]
    ]
    return min(times), max(times)


COVERED_ROW = sa.text(
    "INSERT INTO coverage_intervals (cell_id, kind, span) VALUES (:c, 'covered', tstzrange(:l, :u, '[)'))"
)


async def test_adzuna_page_cap_and_arbeitnow_overlap(
    engine, current_epoch, fake_http, respx_mock: respx.MockRouter, adapters: Adapters
) -> None:
    await seed_inputs(engine, queries=(TEST_QUERY,), enabled=("adzuna", "arbeitnow"))
    oldest, newest = feed_times()
    # the Arbeitnow traversals' clock: the feed's items are fresh then (the ad age decides what is abandoned)
    feed_now = newest + H
    respx_mock.get(url__regex=rf"^{re.escape(ADZUNA_URL)}/[123]\?").mock(return_value=adzuna_full_page(1000))
    async with engine.connect() as c:
        adzuna, arbeitnow = await instance_id(c, "adzuna"), await instance_id(c, "arbeitnow")
    async with httpx.AsyncClient() as client:  # respx serves it
        adzuna_adapter = AdzunaAdapter(
            client, app_id=b"fake", app_key=b"fake", instance_id=adzuna, clock=utcnow
        )
        respx_feed = ArbeitnowAdapter(client, instance_id=arbeitnow, clock=utcnow)

        def first_adapters(instance: InstanceRow):
            return adzuna_adapter if instance.source_id == "adzuna" else adapters(instance)

        deps = make_deps(engine, first_adapters)
        feed_deps = replace(deps, clock=lambda: feed_now)
        owner = await queued_run(engine, include_paid=False)
        planned = await by_source(engine, await plan_ids(deps, owner))
        assert set(planned) == {"adzuna", "arbeitnow"}
        # Adzuna: 3 full pages, page 3 is the cap → unknown, with a gap
        result = await traverse_and_record(deps, planned["adzuna"][0])
        assert (result.end, result.reason, result.pages, result.items) == (
            "unknown",
            "unknown_continuation",
            3,
            150,
        )
        assert [r[:3] for r in await rows(engine, RUN_INTERVALS, r=owner.run_id)] == [
            ("gap", "open", "unknown_continuation")
        ]
        # Arbeitnow, a covered cell (its coverage reaches into page 1): read to the end of the feed (page 3)
        feed_fid, feed_cell = planned["arbeitnow"]
        async with engine.begin() as c:
            await c.execute(COVERED_ROW, {"c": feed_cell, "l": oldest - 72 * H, "u": oldest + H})
        result = await traverse_and_record(feed_deps, feed_fid)
        assert (result.end, result.reason, result.pages, result.covered) == ("end", None, 3, (oldest, newest))
        assert (
            result.firehose is not None and result.firehose[1] == []
        )  # no gap: the reads reach the coverage
        assert [r[:5] for r in await rows(engine, RUN_INTERVALS, r=owner.run_id)][1:] == [
            ("covered", None, None, oldest, newest)
        ]
        await finish(engine, owner)
        # a read stopped by the cap (page 3 still has a next page) proves nothing: an open gap, no covered row
        later = newest + 30 * timedelta(minutes=1)
        respx_mock.get(FEED_URL, params={"page": "1"}).mock(
            return_value=arbeitnow_page("page1", created_at=int(later.timestamp()))
        )
        respx_mock.get(FEED_URL, params={"page": "2"}).mock(return_value=arbeitnow_page("page2"))
        respx_mock.get(FEED_URL, params={"page": "3"}).mock(return_value=arbeitnow_page("page2"))
        deps = make_deps(engine, lambda instance: respx_feed)
        feed_deps = replace(deps, clock=lambda: feed_now)
        second = await queued_run(engine, include_paid=False)
        planned = await by_source(engine, await plan_ids(deps, second))
        result = await traverse_and_record(feed_deps, planned["arbeitnow"][0])
        assert (result.end, result.reason, result.pages, result.covered) == ("cursor", "page_cap", 3, None)
        assert [r[:5] for r in await rows(engine, RUN_INTERVALS, r=second.run_id)] == [
            ("gap", "open", "page_cap", newest, later)  # from the coverage's frontier to the newest item read
        ]


SNAPSHOTS = sa.text(
    "SELECT s.status, s.member_count, s.incomplete_reason, (SELECT count(*) FROM ats_snapshot_members m "
    "WHERE m.snapshot_id = s.id) FROM ats_snapshots s ORDER BY s.id"
)


async def test_ats_snapshot_written_once_and_complete(
    engine, current_epoch, queue_app, adapters: Adapters, fixture_log
) -> None:
    await seed_inputs(engine, companies=(("greenhouse", "getyourguide"),), enabled=("ats",))
    deps = make_deps(engine, adapters)
    owner = await queued_run(engine, include_paid=False)
    (fid,) = await plan_ids(deps, owner)
    result = await traverse_and_record(deps, fid)
    assert result.snapshot is not None and (result.end, result.snapshot.complete) == ("end", True)
    assert await rows(engine, SNAPSHOTS) == [("complete", 15, None, 15)]
    sent = served_requests(fixture_log)
    assert [r["case"] for r in sent] == ["greenhouse_getyourguide"]
    resumed = await supersede(engine, owner)  # a takeover: the next attempt traverses the cell again
    assert await plan_ids(deps, resumed) == [fid]
    again = await traverse_and_record(deps, fid, attempt_no=resumed.attempt_no)
    assert again.snapshot == result.snapshot and again.outcomes == result.outcomes  # the page replayed
    assert (
        await rows(engine, SNAPSHOTS) == [("complete", 15, None, 15)] and served_requests(fixture_log) == sent
    )


DETAILS = sa.text(
    "SELECT operation, detail_native_id, parent_fetch_request_id, status FROM fetch_requests "
    "WHERE run_id = :r ORDER BY id"
)


async def test_smartrecruiters_detail_calls_hang_off_their_list_page(
    engine, current_epoch, respx_mock: respx.MockRouter
) -> None:
    """A list item in Germany gets its detail call, with the list page as its parent; the others (outside
    Germany) none, and membership comes from the list page alone."""
    await seed_inputs(engine, companies=(("smartrecruiters", "DeliveryHero"),), enabled=("ats",))
    status, headers, body = load_fixture("ats", "smartrecruiters_deliveryhero_list")
    listing = json.loads(body)
    berlin = {"city": "Berlin", "country": "de", "remote": False, "fullLocation": "Berlin, Germany"}
    listing["content"][0]["location"] = berlin
    respx_mock.get("https://api.smartrecruiters.com/v1/companies/DeliveryHero/postings").mock(
        return_value=httpx.Response(status, headers=headers, json=listing)
    )
    first_id = listing["content"][0]["id"]
    status, headers, body = load_fixture("ats", "smartrecruiters_deliveryhero_detail_1")
    respx_mock.get(f"https://api.smartrecruiters.com/v1/companies/DeliveryHero/postings/{first_id}").mock(
        return_value=httpx.Response(status, headers=headers, content=body)
    )
    async with engine.connect() as c:
        board = next(i for i in await list_instances(c) if i.source_id == "smartrecruiters")
    async with httpx.AsyncClient() as client:
        adapter = adapter_for(board, client, clock=utcnow)
        assert isinstance(adapter, SmartRecruitersAdapter)
        deps = make_deps(engine, lambda instance: adapter)
        owner = await queued_run(engine, include_paid=False)
        (fid,) = await plan_ids(deps, owner)
        result = await traverse_and_record(deps, fid)
    assert result.snapshot is not None and (result.snapshot.complete, result.snapshot.member_count) == (
        True,
        10,
    )
    assert (result.pages, len(result.outcomes)) == (1, 2)
    assert await rows(engine, DETAILS, r=owner.run_id) == [
        ("list", None, None, "succeeded"),
        ("detail", first_id, fid, "succeeded"),
    ]


async def test_a_half_open_instance_probes_with_one_page(engine, current_epoch, adapters: Adapters) -> None:
    await seed_inputs(engine, queries=(TEST_QUERY,), enabled=("arbeitnow",))
    async with engine.begin() as c:
        feed = await instance_id(c, "arbeitnow")
        for _ in range(5):  # the free threshold: the breaker opens
            await record_failure(c, feed, "transient", billing="free", now=utcnow() - 7 * H)
    deps = make_deps(engine, adapters)
    owner = await queued_run(engine, include_paid=False)
    (fid,) = await plan_ids(deps, owner)  # half open six hours later: one spec, its probe
    result = await traverse_and_record(deps, fid)
    assert (result.end, result.reason, result.pages, len(result.outcomes)) == ("cursor", "page_cap", 1, 1)
    assert result.covered is None and result.firehose is not None  # a probe's page proves nothing


async def test_serpapi_traversal_reads_the_account_after_its_last_search(
    engine, current_epoch, respx_mock: respx.MockRouter, serpapi_fixture_adapter: SerpApiAdapter
) -> None:
    await seed_inputs(engine, queries=(TEST_QUERY,), enabled=("jsearch", "serpapi"))
    async with (
        engine.begin() as c
    ):  # JSearch is closed: its cell's skipped(breaker_open) is fallback-eligible
        await record_failure(c, await instance_id(c, "jsearch"), "auth", billing="paid", now=utcnow())
    owner = await queued_run(engine)
    deps = make_deps(engine, lambda instance: serpapi_fixture_adapter)
    state = initial_state(owner.run_id, owner.attempt_no)
    assert (await make_plan(deps)(state))["primary_fetch_request_ids"] == []
    (fallback,) = (await make_plan_fallback(deps)(state))["fallback_fetch_request_ids"]
    reads = respx_mock["serpapi_account"].call_count  # plan_fallback's own read
    result = await traverse_and_record(deps, fallback)
    # page 1's ids are all new: page 2 by JSearch's rule, and no page 3
    assert (result.end, result.reason, result.pages) == ("cursor", "page_cap", 2)
    assert respx_mock["serpapi_search"].call_count == 2
    assert respx_mock["serpapi_account"].call_count == reads + 2 + 1  # before each search, and after the last


async def test_shutdown_after_reservation_before_dispatch_releases_and_sends_nothing(
    engine, current_epoch, queue_app, jsearch_fixture_adapter, fixture_log, monkeypatch
) -> None:  # Review Focus 4
    await seed_inputs(engine, queries=("python developer",), enabled=("jsearch",))
    owner, stopped, pause = await queued_run(engine), [False], PausePoint("collect.after_reserve")
    monkeypatch.setattr(acquire, "afault_point", pause)
    deps = make_deps(engine, lambda instance: jsearch_fixture_adapter, admission_stopped=lambda: stopped[0])
    (fid,) = (await make_plan(deps)(initial_state(owner.run_id, owner.attempt_no)))[
        "primary_fetch_request_ids"
    ]
    task = asyncio.create_task(make_fetch_cell(deps)({"fetch_request_id": fid}))
    await asyncio.wait_for(pause.reached.wait(), 10)
    stopped[0] = True
    pause.resume()
    await task  # the deploy's SIGTERM lands between the reservation and the dispatch marker
    async with engine.connect() as c:
        row = (
            await c.execute(
                sa.text(
                    "SELECT f.status, f.skip_reason, f.request_key, l.state, l.evidence "
                    "FROM fetch_requests f JOIN quota_ledger l ON l.id = f.quota_ledger_id WHERE f.id = :i"
                ),
                {"i": fid},
            )
        ).one()
        gap = (await c.execute(GAPS, {"i": fid})).one()
    assert (row.status, row.skip_reason, row.state, row.evidence) == (
        "skipped",
        "shutdown",
        "not_billed",
        "never_dispatched",
    )  # the user is not billed
    assert served_requests(fixture_log) == [] and tuple(gap) == (
        "gap",
        "open",
        "shutdown",
    )  # nothing sent; the traversal ended skipped('shutdown')
    async with engine.begin() as c:
        attempt = await runs.begin_new_attempt(
            c, owner.run_id, epoch=1, end_reason="worker_takeover", defer=defer_in_tx
        )
    assert attempt is not None
    replanned = (await make_plan(deps)(initial_state(owner.run_id, attempt)))["primary_fetch_request_ids"]
    async with engine.connect() as c:
        found = (
            await c.execute(
                sa.text(
                    "SELECT id, attempt_seq, status FROM fetch_requests WHERE request_key = :k "
                    "ORDER BY attempt_seq"
                ),
                {"k": row.request_key},
            )
        ).all()
    assert [(r.attempt_seq, r.status) for r in found] == [(1, "skipped"), (2, "planned")]
    assert found[1].id in replanned  # the next attempt re-plans the key


async def test_contract_violation_is_visible_to_plan_fallback_in_the_same_run(
    engine, current_epoch, jsearch_html_adapter, adapters
) -> None:  # Review Focus 3
    await seed_inputs(engine, queries=("python developer",), enabled=("jsearch", "serpapi"))
    owner = await queued_run(engine)
    deps = make_deps(
        engine,
        lambda instance: jsearch_html_adapter if instance.source_id == "jsearch" else adapters(instance),
    )
    state = initial_state(owner.run_id, owner.attempt_no)
    (primary,) = (await make_plan(deps)(state))[
        "primary_fetch_request_ids"
    ]  # the JSearch spec: no SerpApi one
    await make_fetch_cell(deps)({"fetch_request_id": primary})  # served the HTML error page
    fallback = (await make_plan_fallback(deps)(state))["fallback_fetch_request_ids"]
    async with engine.connect() as c:
        failed = (
            await c.execute(
                sa.text(
                    "SELECT f.status, f.error_code, s.health, c.query_id, c.location_key "
                    "FROM fetch_requests f JOIN source_instances s ON s.id = f.source_instance_id "
                    "JOIN coverage_cells c ON c.id = f.cell_id WHERE f.id = :i"
                ),
                {"i": primary},
            )
        ).one()
        planned = (
            await c.execute(
                sa.text(
                    "SELECT f.phase, s.source_id, c.query_id, c.location_key FROM fetch_requests f "
                    "JOIN source_instances s ON s.id = f.source_instance_id "
                    "JOIN coverage_cells c ON c.id = f.cell_id WHERE f.id = ANY(:ids)"
                ),
                {"ids": fallback},
            )
        ).all()
    assert (failed.status, failed.error_code, failed.health) == ("failed", "contract", "contract_broken")
    assert [tuple(p) for p in planned] == [
        ("fallback", "serpapi", failed.query_id, failed.location_key)
    ]  # the same run falls back to SerpApi for that cell


# ---- fix round 1 ------------------------------------------------------------------------------------------

PRIMARY = sa.text(
    "SELECT id, cell_id, status, skip_reason, error_code, quota_ledger_id FROM fetch_requests "
    "WHERE run_id = :r AND phase = 'primary' ORDER BY id"
)
CELL_PLACES = sa.text("SELECT query_id, location_key FROM coverage_cells WHERE id = ANY(:ids) ORDER BY 1, 2")
FALLBACK_CELLS = sa.text("SELECT cell_id FROM fetch_requests WHERE id = ANY(:ids)")
CELL_INTERVALS = sa.text(
    "SELECT kind, gap_status, reason FROM coverage_intervals WHERE cell_id = :c ORDER BY id"
)


async def test_an_open_breaker_denies_the_next_cell_and_both_cells_fall_back(
    engine, current_epoch, jsearch_html_adapter, adapters: Adapters, fixture_log
) -> None:
    """C1, Ruling T14-b: the first cell's HTML page opens the JSearch breaker at once (`contract_broken`); the
    second cell's planned request is skipped `breaker_open` with nothing reserved or sent, and both cells fall
    back to SerpApi in the same run."""
    await seed_inputs(engine, queries=(TEST_QUERY, "data engineer"), enabled=("jsearch", "serpapi"))
    await insert_window(engine, jsearch_window(utcnow(), limit=200, reserve=20, tokens=10, interval_s=SIX_H))
    owner = await queued_run(engine)
    deps = make_deps(
        engine,
        lambda instance: jsearch_html_adapter if instance.source_id == "jsearch" else adapters(instance),
    )
    state = initial_state(owner.run_id, owner.attempt_no)
    first, second = (await make_plan(deps)(state))["primary_fetch_request_ids"]
    for fid in (first, second):
        await make_fetch_cell(deps)({"fetch_request_id": fid})
    rows_ = await rows(engine, PRIMARY, r=owner.run_id)
    assert [r[2:] for r in rows_] == [
        ("failed", None, "contract", rows_[0][5]),
        ("skipped", "breaker_open", None, None),  # no ledger entry: nothing was reserved
    ]
    assert await rows(engine, CELL_INTERVALS, c=rows_[1][1]) == [("gap", "open", "source_failed")]
    searches = [r for r in served_requests(fixture_log) if r["host"] == "jsearch.p.rapidapi.com"]
    assert len(searches) == 1  # exactly one search: the first cell's page 1
    fallback = (await make_plan_fallback(deps)(state))["fallback_fetch_request_ids"]
    async with engine.connect() as c:
        jsearch_places = (await c.execute(CELL_PLACES, {"ids": [r[1] for r in rows_]})).all()
        cells = list((await c.execute(FALLBACK_CELLS, {"ids": fallback})).scalars())
        serpapi_places = (await c.execute(CELL_PLACES, {"ids": cells})).all()
    assert len(fallback) == 2 and serpapi_places == jsearch_places  # both cells fall back


SNAPSHOT_COUNT = sa.text("SELECT count(*) FROM ats_snapshots")


async def test_a_board_404_writes_no_snapshot_and_its_open_breaker_sends_nothing_more(
    engine, current_epoch, adapters: Adapters, fixture_log
) -> None:
    """C1 (a free source) and M7: the board's list 404 (`not_found`, open at once) writes no snapshot and ages
    nothing; any later new attempt for the instance in the run (a later page, a retry) is skipped
    `breaker_open` without a request."""
    await seed_inputs(engine, companies=(("greenhouse", "acme-board"),), enabled=("ats",))
    deps = make_deps(engine, adapters)
    owner = await queued_run(engine, include_paid=False)
    (fid,) = await plan_ids(deps, owner)
    result = await traverse_and_record(deps, fid)
    assert (result.end, result.reason, result.pages, result.snapshot) == ("failed", "not_found", 0, None)
    async with engine.connect() as c:
        assert (await c.execute(SNAPSHOT_COUNT)).scalar_one() == 0
    planned = await load_planned(engine, fid, attempt_no=None)
    assert planned.instance.health == "not_found"
    adapter = adapters(planned.instance)
    later = replace(adapter.plan(planned.plan), page_no=2)
    denied = await acquire_page(
        acquire_context(deps, planned, adapter),
        later,
        cell_id=planned.plan.cell_id,
        phase="primary",
        planned_id=None,
    )
    assert isinstance(denied, Denied) and denied.skip_reason == "breaker_open"
    boards = [r["case"] for r in served_requests(fixture_log) if r["host"] == "boards-api.greenhouse.io"]
    assert boards == ["greenhouse_404"]  # the one request of the run to the board


async def test_an_interrupted_board_traversal_writes_no_snapshot_until_the_resumed_read(
    engine, current_epoch, queue_app, adapters: Adapters, monkeypatch
) -> None:
    """M1: a shutdown between a board's pages writes no snapshot (an incomplete one would be the run's,
    written once); the resumed attempt's read to the end is recorded, complete."""
    await seed_inputs(engine, companies=(("lever", "leverdemo"),), enabled=("ats",), lever_region="eu")
    stopped, pause = [False], PausePoint("collect.after_settle")
    monkeypatch.setattr(acquire, "afault_point", pause)
    deps = make_deps(engine, adapters, admission_stopped=lambda: stopped[0])
    owner = await queued_run(engine, include_paid=False)
    (fid,) = await plan_ids(deps, owner)
    task = asyncio.create_task(traverse_and_record(deps, fid))
    await asyncio.wait_for(pause.reached.wait(), 10)  # page 1 (100 postings) is settled
    stopped[0] = True
    pause.resume()
    result = await task
    assert (result.end, result.reason, result.pages) == ("skipped", "shutdown", 1)
    assert result.snapshot is not None and not result.snapshot.complete
    assert await rows(engine, SNAPSHOTS) == []
    stopped[0] = False
    resumed = await supersede(engine, owner)
    assert await plan_ids(deps, resumed) == [fid]
    result = await traverse_and_record(deps, fid, attempt_no=resumed.attempt_no)
    assert (result.end, result.pages) == ("end", 2)
    assert await rows(engine, SNAPSHOTS) == [("complete", 100, None, 100)]


def listing_in_germany() -> dict[str, Any]:
    """The recorded DeliveryHero list, its first posting in Berlin (the others are outside Germany)."""
    listing: dict[str, Any] = json.loads(load_fixture("ats", "smartrecruiters_deliveryhero_list")[2])
    listing["content"][0]["location"] = {"city": "Berlin", "country": "de", "fullLocation": "Berlin, Germany"}
    return listing


async def test_a_lost_owner_in_a_detail_call_is_raised_unwrapped(
    engine, current_epoch, respx_mock: respx.MockRouter, monkeypatch
) -> None:
    """M2: a `FencedOut` of one detail call leaves the traversal as itself, not inside an `ExceptionGroup`."""
    await seed_inputs(engine, companies=(("smartrecruiters", "DeliveryHero"),), enabled=("ats",))
    respx_mock.get("https://api.smartrecruiters.com/v1/companies/DeliveryHero/postings").mock(
        return_value=httpx.Response(200, json=listing_in_germany())
    )
    real = acquire_page

    async def fenced_details(ctx, spec, **arguments):
        if spec.operation == "detail":
            raise FencedOut("run")
        return await real(ctx, spec, **arguments)

    monkeypatch.setattr(traverse, "acquire_page", fenced_details)
    async with engine.connect() as c:
        board = next(i for i in await list_instances(c) if i.source_id == "smartrecruiters")
    async with httpx.AsyncClient() as client:
        adapter = adapter_for(board, client, clock=utcnow)
        deps = make_deps(engine, lambda instance: adapter)
        owner = await queued_run(engine, include_paid=False)
        (fid,) = await plan_ids(deps, owner)
        with pytest.raises(FencedOut):
            await traverse_and_record(deps, fid)


async def test_a_failed_page_two_leaves_page_one_cursor_in_the_gap(
    engine, current_epoch, jsearch_variant
) -> None:
    """M4: the gap after a served page 1 whose page 2 failed keeps page 1's continuation cursor (§5.6)."""
    await seed_inputs(engine, queries=(TEST_QUERY,), enabled=("jsearch",))
    adapter = jsearch_variant({"search_v2_page2": "search_v2_html"})
    deps = make_deps(engine, lambda instance: adapter)
    owner = await queued_run(engine)
    (fid,) = await plan_ids(deps, owner)
    result = await traverse_and_record(deps, fid)
    cursor = json.loads(load_fixture("jsearch", "search_v2_page1")[2])["data"]["cursor"]
    assert (result.end, result.reason, result.pages, result.cursor_out) == ("failed", "contract", 1, cursor)
    (gap,) = await rows(engine, RUN_INTERVALS, r=owner.run_id)
    assert (gap[:3], gap[5]) == (("gap", "open", "source_failed"), {"cursor": cursor})


async def test_a_shutdown_between_two_search_pages_leaves_a_shutdown_gap(
    engine, current_epoch, adapters: Adapters, monkeypatch
) -> None:
    """M7: admission stops after page 1 settled; page 2 is never requested, the gap is `shutdown`."""
    await seed_inputs(engine, queries=(TEST_QUERY,), enabled=("jsearch",))
    stopped, pause = [False], PausePoint("collect.after_settle")
    monkeypatch.setattr(acquire, "afault_point", pause)
    deps = make_deps(engine, adapters, admission_stopped=lambda: stopped[0])
    owner = await queued_run(engine)
    (fid,) = await plan_ids(deps, owner)
    task = asyncio.create_task(traverse_and_record(deps, fid))
    await asyncio.wait_for(pause.reached.wait(), 10)
    stopped[0] = True
    pause.resume()
    result = await task
    assert (result.end, result.reason, result.pages, len(result.outcomes)) == ("skipped", "shutdown", 1, 1)
    cursor = json.loads(load_fixture("jsearch", "search_v2_page1")[2])["data"]["cursor"]
    (gap,) = await rows(engine, RUN_INTERVALS, r=owner.run_id)
    assert (gap[:3], gap[5]) == (("gap", "open", "shutdown"), {"cursor": cursor})


async def test_a_half_open_jsearch_probe_reads_one_page(engine, current_epoch, adapters: Adapters) -> None:
    """M7: page 1's ids are all new, yet a half-open instance's probe reads that page only."""
    await seed_inputs(engine, queries=(TEST_QUERY,), enabled=("jsearch",))
    async with engine.begin() as c:  # an auth failure opens at once; it is half open 6 h later
        await record_failure(c, await instance_id(c, "jsearch"), "auth", billing="paid", now=utcnow() - 7 * H)
    deps = make_deps(engine, adapters)
    owner = await queued_run(engine)
    (fid,) = await plan_ids(deps, owner)
    result = await traverse_and_record(deps, fid)
    assert (result.end, result.reason, result.pages, len(result.outcomes)) == ("cursor", "page_cap", 1, 1)
    assert await rows(engine, PAGES, r=owner.run_id, c=await cell_of(engine, fid)) == [
        (1, "succeeded", "probe", 10, "probe")
    ]


async def no_wait(seconds: float) -> None:
    return None


LEVER_EU: Final = "https://api.eu.lever.co/v0/postings/leverdemo"
ATTEMPTS_BY_PAGE = sa.text(
    "SELECT page_no, attempt_seq, status, http_status FROM fetch_requests WHERE run_id = :r "
    "ORDER BY page_no, attempt_seq"
)


async def test_lever_membership_spans_its_pages_with_one_final_attempt_each(
    engine, current_epoch, respx_mock: respx.MockRouter
) -> None:
    """M7 (carry 4): a Lever EU board of two pages, page 2 served on its second attempt: the snapshot's
    membership has both pages, numbered from 1, each by its final attempt, and is complete."""
    await seed_inputs(engine, companies=(("lever", "leverdemo"),), enabled=("ats",), lever_region="eu")
    status, headers, body = load_fixture("ats", "lever_eu_leverdemo")
    respx_mock.get(LEVER_EU, params={"skip": "0"}).mock(
        return_value=httpx.Response(status, headers=headers, content=body)
    )
    status, headers, body = load_fixture("ats", "lever_eu_end")
    respx_mock.get(LEVER_EU, params={"skip": "100"}).mock(
        side_effect=[httpx.Response(503), httpx.Response(status, headers=headers, content=body)]
    )
    async with engine.connect() as c:
        board = next(i for i in await list_instances(c) if i.source_id == "lever")
    async with httpx.AsyncClient() as client:
        adapter = adapter_for(board, client, clock=utcnow)
        deps = replace(make_deps(engine, lambda instance: adapter), sleep=no_wait)  # the free retry's backoff
        owner = await queued_run(engine, include_paid=False)
        (fid,) = await plan_ids(deps, owner)
        result = await traverse_and_record(deps, fid)
    assert (result.end, result.pages) == ("end", 2)
    assert result.snapshot is not None and (result.snapshot.complete, result.snapshot.member_count) == (
        True,
        100,
    )
    assert await rows(engine, ATTEMPTS_BY_PAGE, r=owner.run_id) == [
        (1, 1, "succeeded", 200),
        (2, 1, "failed", 503),
        (2, 2, "succeeded", 200),
    ]
    assert await rows(engine, SNAPSHOTS) == [("complete", 100, None, 100)]


async def test_an_arbeitnow_failure_partway_proves_nothing_and_leaves_an_open_gap(
    engine, current_epoch, respx_mock: respx.MockRouter, arbeitnow_adapter: ArbeitnowAdapter
) -> None:
    """M7: page 2 of the feed fails (404, `not_found`): the reads are concluded as stopped short (`remaining`,
    never `end`), so no covered row is written and what page 1 read is an open gap."""
    await seed_inputs(engine, enabled=("arbeitnow",))
    status, headers, body = load_fixture("arbeitnow", "page1")
    respx_mock.get(FEED_URL, params={"page": "1"}).mock(
        return_value=httpx.Response(status, headers=headers, content=body)
    )
    respx_mock.get(FEED_URL, params={"page": "2"}).mock(
        return_value=httpx.Response(404, json={"error": "gone"})
    )
    times = [datetime.fromtimestamp(job["created_at"], UTC) for job in json.loads(body)["data"]]
    deps = make_deps(engine, lambda instance: arbeitnow_adapter)
    feed_deps = replace(deps, clock=lambda: max(times) + H)
    owner = await queued_run(engine, include_paid=False)
    (fid,) = await plan_ids(deps, owner)
    result = await traverse_and_record(feed_deps, fid)
    assert (result.end, result.reason, result.pages, result.covered) == ("failed", "not_found", 1, None)
    assert [r[:5] for r in await rows(engine, RUN_INTERVALS, r=owner.run_id)] == [
        ("gap", "open", "source_failed", min(times), max(times))
    ]


async def test_an_arbeitnow_read_to_the_end_covers_nothing_newer_than_its_page_was_made(
    engine, current_epoch, respx_mock: respx.MockRouter, arbeitnow_adapter: ArbeitnowAdapter
) -> None:
    """Fix wave F4 (ledger L137, §5.6): the feed's one page, a cache hit made an hour before it was served
    (`Date` less `Age`) and before its newest item, ends the feed. The covered span stops at that fill
    instant; the newer part of what was read is an open gap, not coverage."""
    await seed_inputs(engine, enabled=("arbeitnow",))
    status, headers, body = load_fixture("arbeitnow", "page1")
    doc = json.loads(body)
    doc["links"]["next"] = None  # the end of the feed
    times = [datetime.fromtimestamp(job["created_at"], UTC) for job in doc["data"]]
    oldest, newest = min(times), max(times)
    made = newest - 30 * timedelta(minutes=1)
    served = made + H
    cached = {**headers, "date": served.strftime("%a, %d %b %Y %H:%M:%S GMT"), "age": "3600"}
    respx_mock.get(FEED_URL, params={"page": "1"}).mock(
        return_value=httpx.Response(status, headers=cached, json=doc)
    )
    deps = replace(make_deps(engine, lambda instance: arbeitnow_adapter), clock=lambda: served)
    owner = await queued_run(engine, include_paid=False)
    (fid,) = await plan_ids(deps, owner)
    result = await traverse_and_record(deps, fid)
    assert (result.end, result.pages, result.covered) == ("end", 1, (oldest, made))
    assert [r[:5] for r in await rows(engine, RUN_INTERVALS, r=owner.run_id)] == [
        ("gap", "open", "page_cap", made, newest),  # read, but newer than the page's content: not proven
        ("covered", None, None, oldest, made),
    ]


async def test_detail_acquisitions_never_run_more_than_the_gate_admits(
    engine, current_epoch, respx_mock: respx.MockRouter, monkeypatch
) -> None:
    """Fix round 2: a list page with 10 postings in Germany has 10 detail calls, of which at most
    `SMARTRECRUITERS_DETAIL.concurrency` (4) are inside `acquire_page` (and its database work) at once."""
    await seed_inputs(engine, companies=(("smartrecruiters", "DeliveryHero"),), enabled=("ats",))
    listing = listing_in_germany()
    for item in listing["content"]:
        item["location"] = listing["content"][0]["location"]
    respx_mock.get("https://api.smartrecruiters.com/v1/companies/DeliveryHero/postings").mock(
        return_value=httpx.Response(200, json=listing)
    )
    real, active, peak, made = acquire_page, [0], [0], [0]

    async def counted(ctx, spec, **arguments):
        if spec.operation != "detail":
            return await real(ctx, spec, **arguments)
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        for _ in range(5):  # yields, as the protocol's database round trips do
            await asyncio.sleep(0)
        active[0] -= 1
        made[0] += 1
        return Denied(-made[0], "breaker_open", replayed=False)

    monkeypatch.setattr(traverse, "acquire_page", counted)
    async with engine.connect() as c:
        board = next(i for i in await list_instances(c) if i.source_id == "smartrecruiters")
    async with httpx.AsyncClient() as client:
        adapter = adapter_for(board, client, clock=utcnow)
        deps = make_deps(engine, lambda instance: adapter)
        owner = await queued_run(engine, include_paid=False)
        (fid,) = await plan_ids(deps, owner)
        await traverse_and_record(deps, fid)
    assert (made[0], peak[0]) == (10, SMARTRECRUITERS_DETAIL.concurrency)


async def test_two_boards_traversed_at_once_share_the_one_detail_gate(
    engine, current_epoch, respx_mock: respx.MockRouter, monkeypatch
) -> None:
    """Fix wave F4 (ledger L216): the gate is the process's (`RateLimiter.gate`), not the traversal's, so two
    SmartRecruiters boards traversed at once (the host's two fetch slots) together have at most
    `SMARTRECRUITERS_DETAIL.concurrency` (4) detail calls inside `acquire_page`, not 4 each. The detail calls
    wait until both boards' list pages are in, so both boards' details compete for the gate at once."""
    slugs = ("DeliveryHero", "SecondBoard")
    await seed_inputs(engine, companies=[("smartrecruiters", slug) for slug in slugs], enabled=("ats",))
    listing = listing_in_germany()
    for item in listing["content"]:
        item["location"] = listing["content"][0]["location"]
    for slug in slugs:
        respx_mock.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings").mock(
            return_value=httpx.Response(200, json=listing)
        )
    real, active, peak, made = acquire_page, [0], [0], [0]
    entered: set[int | None] = set()  # the boards (their list pages) whose detail calls have started
    both_entered = asyncio.Event()

    async def counted(ctx, spec, **arguments):
        if spec.operation != "detail":
            return await real(ctx, spec, **arguments)
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        entered.add(spec.parent_fetch_request_id)
        if len(entered) == len(slugs):
            both_entered.set()
        # held until the other board's details start too: at once with a gate per traversal (8 inside),
        # never with the process's one gate (4 inside, the other board waits), which the timeout then ends
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(both_entered.wait(), 1.0)
        active[0] -= 1
        made[0] += 1
        return Denied(-made[0], "breaker_open", replayed=False)

    monkeypatch.setattr(traverse, "acquire_page", counted)
    clock = FakeClock(0.0)

    async def no_wait(seconds: float) -> None:  # the host's 5 requests/s pass without real time
        clock.now += seconds

    async with engine.connect() as c:
        boards = [i for i in await list_instances(c) if i.source_id == "smartrecruiters"]
    async with httpx.AsyncClient() as client:
        by_instance = {board.id: adapter_for(board, client, clock=utcnow) for board in boards}
        deps = replace(
            make_deps(engine, lambda instance: by_instance[instance.id]),
            limiter=RateLimiter(SOURCE_LIMITS.values(), clock=clock, sleep=no_wait),
        )
        owner = await queued_run(engine, include_paid=False)
        ids = await plan_ids(deps, owner)
        assert len(ids) == 2
        await asyncio.gather(*(traverse_and_record(deps, fid) for fid in ids))
    assert (made[0], peak[0]) == (20, SMARTRECRUITERS_DETAIL.concurrency)


# ---- final review F3 --------------------------------------------------------------------------------------

DETAIL_MARKERS = sa.text(
    "SELECT p.native_id, p.list_item_sha256, p.detail_fetched_at, "
    "(SELECT max(r.fetched_at) FROM raw_responses r JOIN fetch_requests f ON f.id = r.fetch_request_id "
    "WHERE f.operation = 'detail' AND f.detail_native_id = p.native_id) AS detailed_at "
    "FROM postings p WHERE p.source_instance_id = :i ORDER BY p.native_id"
)


async def detail_markers(engine: AsyncEngine, instance: int) -> dict[str, tuple[bytes, datetime]]:
    """The change markers of the instance's postings by native id: `(list_item_sha256, detail_fetched_at)`,
    where `detail_fetched_at` must be the arrival of the posting's latest detail response."""
    async with engine.connect() as c:
        found = (await c.execute(DETAIL_MARKERS, {"i": instance})).all()
    assert all(r.detail_fetched_at == r.detailed_at for r in found)
    return {r.native_id: (bytes(r.list_item_sha256), r.detail_fetched_at) for r in found}


async def test_smartrecruiters_details_are_fetched_again_only_when_new_changed_or_older_than_seven_days(
    engine, current_epoch, tmp_path, fixture_log
) -> None:
    """§5.3.5 (final review F3, Important A): identity writes the change markers of a detail
    observation (the list item's `element_sha256` and the detail's fetch time), so a second run of an
    unchanged board makes no detail call; a changed list item, and a detail older than 7 days, are fetched
    again. The board is served by the fixture transport over a copy of the recordings whose first two
    postings are in Germany (the two with recorded details)."""
    await seed_inputs(engine, companies=(("smartrecruiters", "DeliveryHero"),), enabled=("ats",))
    fixtures = fixture_variant(tmp_path, "ats", {})
    listing = json.loads(load_fixture("ats", "smartrecruiters_deliveryhero_list")[2])
    berlin = {"city": "Berlin", "country": "de", "remote": False, "fullLocation": "Berlin, Germany"}
    for item in listing["content"][:2]:
        item["location"] = berlin

    def serve(doc: dict[str, Any]) -> None:
        (fixtures / "ats" / "smartrecruiters_deliveryhero_list.json").write_text(json.dumps(doc), "utf-8")

    def marker(index: int) -> bytes:
        return element_sha256(canonical_json(listing["content"][index]))

    serve(listing)
    first, second = (str(item["id"]) for item in listing["content"][:2])
    async with engine.connect() as c:
        board = next(i for i in await list_instances(c) if i.source_id == "smartrecruiters")
    async with httpx.AsyncClient(transport=fixture_transport(fixtures, log_path=fixture_log)) as client:
        adapter = adapter_for(board, client, clock=utcnow)
        deps = make_deps(engine, lambda instance: adapter)

        async def run(clock=utcnow) -> list[str]:
            """One run of the board (plan, traverse on `clock`, parse, resolve); the detail cases it sent."""
            sent = len(served_requests(fixture_log))
            owner = await queued_run(engine, include_paid=False)
            state = initial_state(owner.run_id, owner.attempt_no)
            (fid,) = await plan_ids(deps, owner)
            await traverse_and_record(replace(deps, clock=clock), fid)
            await make_parse_normalize(deps)(state)
            await make_resolve_identity(deps)(state)
            await finish(engine, owner)
            return sorted(r["case"] for r in served_requests(fixture_log)[sent:] if "_detail_" in r["case"])

        details = ["smartrecruiters_deliveryhero_detail_1", "smartrecruiters_deliveryhero_detail_2"]
        assert await run() == details  # new postings
        markers = await detail_markers(engine, board.id)
        assert {native_id: sha for native_id, (sha, _) in markers.items()} == {
            first: marker(0),
            second: marker(1),
        }
        assert await run() == []  # unchanged and fresh: no detail call
        listing["content"][1]["name"] = f"{listing['content'][1]['name']} (updated)"
        serve(listing)
        assert await run() == details[1:]  # the changed list item only
        changed = await detail_markers(engine, board.id)
        assert {native_id: sha for native_id, (sha, _) in changed.items()} == {
            first: marker(0),
            second: marker(1),
        }
        assert changed[first][1] == markers[first][1]
        assert changed[second][1] > markers[second][1]  # the hash and the time moved forward together
        assert await run(lambda: utcnow() + timedelta(days=8)) == details  # older than 7 days


FIRST_REQUEST = sa.text(
    "SELECT page_no, status, spec ->> 'cursor_in' AS cursor_in FROM fetch_requests WHERE run_id = :r "
    "AND cell_id = :c ORDER BY id LIMIT 1"
)
CONTINUATION = sa.text("SELECT continuation FROM coverage_cells WHERE id = :c")
OPEN_ALERT = sa.text("SELECT severity FROM alerts WHERE dedup_key = :k AND resolved_at IS NULL")
REFILL = sa.text("UPDATE quota_windows SET tokens = capacity WHERE provider = 'jsearch' AND is_current")


async def finalize_and_finish(engine: AsyncEngine, owner: RunOwner) -> None:
    """The run's cell statistics (`finalize_cells`, which writes a JSearch cell's continuation) and its end,
    in one transaction, as the finalize node commits them."""
    async with engine.begin() as c:
        await coverage.finalize_cells(c, owner.run_id, now=utcnow())
        await finalize_run(c, owner, status="succeeded", reason=None, report={})


async def continuation_of(engine: AsyncEngine, cell: int) -> object:
    async with engine.connect() as c:
        return (await c.execute(CONTINUATION, {"c": cell})).scalar_one()


async def first_request(engine: AsyncEngine, owner: RunOwner, cell: int) -> tuple[object, ...]:
    """`(page_no, status, cursor_in)` of the run's first request of the cell: the page the planner chose."""
    (found,) = await rows(engine, FIRST_REQUEST, r=owner.run_id, c=cell)
    return found


async def test_a_page_one_cursor_below_the_page_two_rule_is_not_kept_for_reuse(
    engine, current_epoch, adapters: Adapters, job_factory: JobFactory
) -> None:
    """§5.3.1 (final review F3, Important B): page 1 with 3 new of its 10 job_uids admits no page 2, so its
    cursor is no continuation: the next run within the reuse TTL plans page 1 with a fresh window, not page
    2 from the cursor."""
    await seed_inputs(engine, queries=(TEST_QUERY,), enabled=("jsearch",))
    for uid in page1_ids()[:7]:  # 7 of page 1's ids are postings already: 3 new
        await job_factory(native_id=uid)
    deps = make_deps(engine, adapters)
    first = await queued_run(engine)
    (fid,) = await plan_ids(deps, first)
    cell = await cell_of(engine, fid)
    result = await traverse_and_record(deps, fid)
    assert (result.end, result.reason, result.pages) == ("cursor", "page_cap", 1)
    assert [r[:4] for r in await rows(engine, PAGES, r=first.run_id, c=cell)] == [
        (1, "succeeded", "probe", 3)
    ]
    await finalize_and_finish(engine, first)
    assert await continuation_of(engine, cell) is None
    second = await queued_run(engine)
    await plan_ids(deps, second)
    assert await first_request(engine, second, cell) == (1, "planned", None)


async def test_a_page_two_the_rule_admitted_but_could_not_reserve_is_reused(
    engine, current_epoch, adapters: Adapters, job_factory: JobFactory
) -> None:
    """§5.3.1 (final review F3, Important B): page 1 with 8 new ids admits page 2, whose reservation is
    denied (`paced`: one token in the bucket); the next run within the reuse TTL plans that page 2 from page
    1's cursor."""
    await seed_inputs(engine, queries=(TEST_QUERY,), enabled=("jsearch",))
    await insert_window(engine, jsearch_window(utcnow(), limit=200, reserve=20, tokens=1, interval_s=SIX_H))
    for uid in page1_ids()[:2]:  # 8 new
        await job_factory(native_id=uid)
    deps = make_deps(engine, adapters)
    first = await queued_run(engine)
    (fid,) = await plan_ids(deps, first)
    cell = await cell_of(engine, fid)
    result = await traverse_and_record(deps, fid)
    assert (result.end, result.reason, result.pages) == ("skipped", "paced", 1)
    assert [r[:4] for r in await rows(engine, PAGES, r=first.run_id, c=cell)] == [
        (1, "succeeded", "normal", 8),
        (2, "skipped", "normal", None),
    ]
    await finalize_and_finish(engine, first)
    cursor = json.loads(load_fixture("jsearch", "search_v2_page1")[2])["data"]["cursor"]
    async with engine.begin() as c:
        await c.execute(REFILL)
    second = await queued_run(engine)
    await plan_ids(deps, second)
    assert await first_request(engine, second, cell) == (2, "planned", cursor)


async def test_no_continuation_leads_past_page_two(engine, current_epoch, adapters: Adapters) -> None:
    """§5.3.1 (final review F3, Important B): a traversal that read page 2 (with a cursor) leaves no
    continuation, so the next run never plans page 3."""
    await seed_inputs(engine, queries=(TEST_QUERY,), enabled=("jsearch",))
    deps = make_deps(engine, adapters)
    first = await queued_run(engine)
    (fid,) = await plan_ids(deps, first)
    cell = await cell_of(engine, fid)
    result = await traverse_and_record(deps, fid)
    assert (result.end, result.reason, result.pages) == ("cursor", "page_cap", 2)
    await finalize_and_finish(engine, first)
    assert await continuation_of(engine, cell) is None
    second = await queued_run(engine)
    await plan_ids(deps, second)
    assert await first_request(engine, second, cell) == (1, "planned", None)


async def test_an_auth_failure_of_the_account_read_after_the_last_search_opens_the_breaker(
    engine, current_epoch, respx_mock: respx.MockRouter, serpapi_fixture_adapter: SerpApiAdapter
) -> None:
    """Final review F3 (F2 re-review, out-of-scope observation 1): the account read after a traversal's last
    search that fails as `auth` (a 401: the key was revoked mid-run) is counted by the breaker, which opens
    `auth_failed` with `source.auth_failed` at once, as the read before a search does (Ruling FR-l); the
    traversal's own result stands."""
    await seed_inputs(engine, queries=(TEST_QUERY,), enabled=("jsearch", "serpapi"))
    async with engine.begin() as c:  # JSearch is closed: its cell falls back to SerpApi
        await record_failure(c, await instance_id(c, "jsearch"), "auth", billing="paid", now=utcnow())
        serpapi = await instance_id(c, "serpapi")
    revoked = httpx.Response(401, json={"error": "Invalid API key."})
    # plan_fallback's read, the read before each of the two searches, then the read after the last one
    respx_mock["serpapi_account"].side_effect = [*(serpapi_account_response() for _ in range(3)), revoked]
    owner = await queued_run(engine)
    deps = make_deps(engine, lambda instance: serpapi_fixture_adapter)
    state = initial_state(owner.run_id, owner.attempt_no)
    await make_plan(deps)(state)
    (fallback,) = (await make_plan_fallback(deps)(state))["fallback_fetch_request_ids"]
    result = await traverse_and_record(deps, fallback)
    assert (result.end, result.reason, result.pages) == ("cursor", "page_cap", 2)
    assert respx_mock["serpapi_account"].call_count == 4
    async with engine.connect() as c:
        health = await c.execute(
            sa.text("SELECT health, last_error_code FROM source_instances WHERE id = :i"), {"i": serpapi}
        )
        assert tuple(health.one()) == ("auth_failed", "auth")
        alert = await c.execute(OPEN_ALERT, {"k": f"source.auth_failed:instance:{serpapi}"})
        assert alert.scalar_one() == "critical"


async def test_no_account_read_follows_a_search_that_opened_the_breaker(
    engine, current_epoch, respx_mock: respx.MockRouter, serpapi_fixture_adapter: SerpApiAdapter
) -> None:
    """Fix wave F4 (ledger L216): the account read after a traversal's last search is a send to SerpApi
    too, so it is gated like the read before a search: page 1 is served, page 2 and its one retry answer 503,
    which opens the breaker (paid threshold 2), and then no read follows."""
    await seed_inputs(engine, queries=(TEST_QUERY,), enabled=("jsearch", "serpapi"))
    async with engine.begin() as c:  # JSearch is closed: its cell falls back to SerpApi
        await record_failure(c, await instance_id(c, "jsearch"), "auth", billing="paid", now=utcnow())
        serpapi = await instance_id(c, "serpapi")
    status, headers, body = load_fixture("serpapi", "google_jobs_hl_en")
    respx_mock["serpapi_search"].side_effect = [
        httpx.Response(status, headers=headers, content=body),
        httpx.Response(503),
        httpx.Response(503),
    ]
    owner = await queued_run(engine)
    deps = make_deps(engine, lambda instance: serpapi_fixture_adapter)
    state = initial_state(owner.run_id, owner.attempt_no)
    await make_plan(deps)(state)
    (fallback,) = (await make_plan_fallback(deps)(state))["fallback_fetch_request_ids"]
    result = await traverse_and_record(deps, fallback)
    assert (result.end, result.reason, result.pages) == ("failed", "transient", 1)
    assert respx_mock["serpapi_search"].call_count == 3
    # plan_fallback's read and the reads before page 1, page 2 and its retry; none after the last search
    assert respx_mock["serpapi_account"].call_count == 4
    async with engine.connect() as c:
        health = await c.execute(sa.text("SELECT health FROM source_instances WHERE id = :i"), {"i": serpapi})
        assert health.scalar_one() == "breaker_open"


async def test_fetch_requests_counts_the_pages_an_attempt_sent_never_a_replayed_one(
    engine, current_epoch, queue_app, adapters: Adapters
) -> None:
    """Final review F3, Minor 6: `FETCH_REQUESTS` counts a page when this attempt sent it; the resumed
    attempt, which replays both stored pages without HTTP, counts nothing."""
    await seed_inputs(engine, queries=(TEST_QUERY,), enabled=("jsearch",))
    deps = make_deps(engine, adapters)
    owner = await queued_run(engine)
    (fid,) = await plan_ids(deps, owner)
    before = fetched("jsearch", "succeeded")
    result = await traverse_and_record(deps, fid)
    assert result.pages == 2 and fetched("jsearch", "succeeded") == before + 2
    resumed = await supersede(engine, owner)
    assert await plan_ids(deps, resumed) == [fid]
    again = await traverse_and_record(deps, fid, attempt_no=resumed.attempt_no)
    assert again.pages == 2 and [o["status"] for o in again.outcomes] == ["succeeded", "succeeded"]
    assert fetched("jsearch", "succeeded") == before + 2  # the replays sent nothing


async def test_fetch_requests_counts_a_page_this_attempt_denied_but_not_its_replay(
    engine, current_epoch, queue_app, adapters: Adapters, job_factory: JobFactory
) -> None:
    """Fix wave F4 (the controller's FETCH_REQUESTS item): §17.5 names `fetch_requests_total{source,status}`,
    and a receipt's status includes `skipped`, so a page this attempt denied (page 2 `paced`: one token) is
    a `skipped` sample; a resumed attempt that finds both receipts again counts neither."""
    await seed_inputs(engine, queries=(TEST_QUERY,), enabled=("jsearch",))
    await insert_window(engine, jsearch_window(utcnow(), limit=200, reserve=20, tokens=1, interval_s=SIX_H))
    for uid in page1_ids()[:2]:  # 8 new: page 2 is admitted by the page-2 rule, then paced
        await job_factory(native_id=uid)
    deps = make_deps(engine, adapters)
    owner = await queued_run(engine)
    (fid,) = await plan_ids(deps, owner)
    before = {status: fetched("jsearch", status) for status in ("succeeded", "skipped")}
    result = await traverse_and_record(deps, fid)
    assert (result.end, result.reason) == ("skipped", "paced")
    assert [o["status"] for o in result.attempted] == ["succeeded", "skipped"]
    assert {status: fetched("jsearch", status) - n for status, n in before.items()} == {
        "succeeded": 1,
        "skipped": 1,
    }
    resumed = await supersede(engine, owner)
    assert await plan_ids(deps, resumed) == [fid]
    again = await traverse_and_record(deps, fid, attempt_no=resumed.attempt_no)
    assert [o["status"] for o in again.outcomes] == ["succeeded", "skipped"] and again.attempted == []
    assert {status: fetched("jsearch", status) - n for status, n in before.items()} == {
        "succeeded": 1,
        "skipped": 1,
    }


async def test_the_traversal_timeout_starts_once_the_traversal_holds_its_semaphore(
    engine, current_epoch, adapters: Adapters, monkeypatch
) -> None:
    """Fix wave F4 (ledger L205/L216, §9.2 "15 min per traversal"): `fetch_cell` times the traversal from
    the moment it holds its source's semaphore, so its wait behind another traversal never counts against
    it; the graph node has no LangGraph timeout that would time the wait. With a timeout of 0 s, a traversal
    that waited for the semaphore and then needs no wait completes, and one that waits at all times out."""
    await seed_inputs(engine, queries=(TEST_QUERY,), enabled=("jsearch",))
    deps = make_deps(engine, adapters)
    owner = await queued_run(engine)
    (fid,) = await plan_ids(deps, owner)
    monkeypatch.setattr(fetch_node, "TRAVERSAL_TIMEOUT", timedelta(0))
    done = traverse.TraversalResult(
        outcomes=[],
        end="skipped",
        reason="shutdown",
        pages=0,
        items=0,
        cursor_out=None,
        snapshot=None,
        firehose=None,
    )

    async def at_once(ctx, plan, first_fetch_request_id, *, limiter):
        return done

    monkeypatch.setattr(fetch_node, "traverse_cell", at_once)
    slot = deps.fetch_ctx.sources["jsearch"]
    async with slot:  # another JSearch traversal holds the source's one slot meanwhile
        waiting = asyncio.create_task(traverse_and_record(deps, fid))
        for _ in range(20):
            await asyncio.sleep(0)
        assert not waiting.done()
    assert await waiting is done

    async def slow(ctx, plan, first_fetch_request_id, *, limiter):
        await asyncio.sleep(0)  # any wait inside the traversal counts
        return done

    monkeypatch.setattr(fetch_node, "traverse_cell", slow)
    with pytest.raises(TimeoutError):
        await traverse_and_record(deps, fid)
    graph = build_collect_graph(InMemorySaver(), deps)
    assert graph.builder.nodes["fetch_cell"].timeout is None
    assert graph.builder.nodes["fetch_primary"].timeout is not None
    assert fetch_node.TRAVERSAL_TIMEOUT == timedelta(0) and TIMEOUTS["fetch_cell"] == timedelta(minutes=15)
