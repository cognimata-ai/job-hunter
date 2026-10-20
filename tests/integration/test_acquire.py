import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.transitions import transition
from jobhunter.errors import FencedOut
from jobhunter.quota import ledger
from jobhunter.sources import acquire, ats, http
from jobhunter.sources.acquire import AcquireContext, Ambiguous, Denied, Failed, Served, acquire_page
from jobhunter.sources.arbeitnow import FEED_URL
from jobhunter.sources.base import CellPlan, RequestSpec, SourceError, request_key
from jobhunter.sources.breaker import record_success
from jobhunter.sources.instances import instance_for_company, list_instances
from jobhunter.sources.jsearch import SEARCH_URL as JSEARCH_URL
from jobhunter.sources.jsearch import JSearchAdapter
from jobhunter.sources.receipts import fetch_attempts, insert_fetch_request, load_response, store_response
from jobhunter.sources.serpapi import SerpApiAdapter
from jobhunter.worker import runs
from jobhunter.worker.runs import RunOwner
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.collect import (
    acquire_ctx,
    instance_id,
    jsearch_cell,
    page1_spec,
    queued_run,
    source_cell,
    supersede,
)
from tests.support.faults import PausePoint
from tests.support.quota import SIX_H, insert_window, jsearch_window, seeded_run_settings
from tests.support.sources import load_fixture, serpapi_account_response, served_requests

pytestmark = pytest.mark.integration
JSEARCH = sa.text(
    "SELECT id, health, consecutive_failures FROM source_instances "
    "WHERE source_id = 'jsearch' AND instance_key = 'default'"
)
# Every attempt of a request key, oldest first, with its ledger entry (none for a free source).
ATTEMPTS = sa.text(
    "SELECT f.attempt_seq, f.request_class, f.status, f.skip_reason, f.error_code, f.http_status, "
    "l.state, l.evidence, l.request_class AS ledger_class FROM fetch_requests f "
    "LEFT JOIN quota_ledger l ON l.id = f.quota_ledger_id WHERE f.request_key = :k ORDER BY f.attempt_seq"
)
WINDOW = sa.text(
    "SELECT state, probe_used, last_observed_at, hour_observed_at, observed_remaining, observed_this_hour, "
    "observed_limit, conservative_until FROM quota_windows WHERE provider = :p AND is_current"
)
WEEK = 7 * 24 * 3600


def roomy(ctx: AcquireContext) -> AcquireContext:
    """`ctx` with a schedule interval of a week, so the JSearch bucket admits every normal request of a test
    (the seeded 6 h interval holds about 3)."""
    return replace(ctx, settings=seeded_run_settings(interval_s=WEEK))


def jsearch_ctx(engine: AsyncEngine, owner: RunOwner, adapter: JSearchAdapter) -> AcquireContext:
    """A roomy JSearch context of `owner`."""
    return roomy(acquire_ctx(engine, owner, adapter, billing="paid", provider="jsearch"))


async def attempts(engine: AsyncEngine, spec: RequestSpec, owner: RunOwner) -> list[tuple[object, ...]]:
    async with engine.connect() as c:
        rows = await c.execute(ATTEMPTS, {"k": request_key(spec, owner.run_id)})
        return [tuple(r) for r in rows]


async def close_breaker(engine: AsyncEngine, instance: int) -> None:
    """A success of the instance: its breaker closed again, so the next scenario starts from `ok` (the
    runtime gate of Ruling T14-b blocks every new attempt while the breaker is open)."""
    async with engine.begin() as c:
        await record_success(c, instance, now=utcnow())


async def scalar(engine: AsyncEngine, statement: str, **params: object) -> object:
    async with engine.connect() as c:
        return (await c.execute(sa.text(statement), params)).scalar_one()


async def window(engine: AsyncEngine, provider: str) -> sa.Row[tuple[object, ...]]:
    async with engine.connect() as c:
        return (await c.execute(WINDOW, {"p": provider})).one()


async def finish(engine: AsyncEngine, owner: RunOwner) -> None:
    """End the run, so that the next `queued_run` may start (one active collection run at a time)."""
    async with engine.begin() as c:
        await runs.finalize_run(c, owner, status="succeeded", reason=None, report={})


async def open_alerts(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as c:
        return set(
            (await c.execute(sa.text("SELECT dedup_key FROM alerts WHERE resolved_at IS NULL"))).scalars()
        )


async def stop_at(
    point: str, monkeypatch: pytest.MonkeyPatch, ctx: AcquireContext, spec: RequestSpec
) -> None:
    """Run `acquire_page` up to `point` and kill it there, as a worker that dies at the fault point."""
    pause = PausePoint(point)
    monkeypatch.setattr(acquire, "afault_point", pause)
    task = asyncio.create_task(
        acquire_page(ctx, spec, cell_id=spec.cell_id, phase="primary", planned_id=None)
    )
    await asyncio.wait_for(pause.reached.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_paid_page_reserve_marker_send_settle(engine, current_epoch, jsearch_fixture_adapter) -> None:
    owner, cell = await queued_run(engine), await jsearch_cell(engine)
    ctx = acquire_ctx(engine, owner, jsearch_fixture_adapter, billing="paid", provider="jsearch")
    r = await acquire_page(
        ctx, await page1_spec(engine, cell), cell_id=cell, phase="primary", planned_id=None
    )
    assert isinstance(r, Served) and not r.replayed and r.page.status == 200
    async with engine.connect() as c:
        fr = (
            await c.execute(
                sa.text(
                    "SELECT status, items, continuation, http_status, raw_response_id, quota_ledger_id "
                    "FROM fetch_requests WHERE id=:i"
                ),
                {"i": r.fetch_request_id},
            )
        ).one()
        led = (
            await c.execute(
                sa.text("SELECT state, request_class, http_status FROM quota_ledger WHERE id=:i"),
                {"i": fr.quota_ledger_id},
            )
        ).one()
        rr = (
            await c.execute(
                sa.text("SELECT ledger_entry_id, item_count, http_status FROM raw_responses WHERE id=:i"),
                {"i": fr.raw_response_id},
            )
        ).one()
    assert (fr.status, fr.items, fr.continuation, fr.http_status) == (
        "succeeded",
        10,
        "cursor",
        200,
    )  # the reserved class is the ledger row's
    assert tuple(led) == ("billed", "probe", 200) and tuple(rr) == (fr.quota_ledger_id, 10, 200)


RECEIPT = sa.text(
    "SELECT f.status, f.dispatched_at IS NOT NULL, l.state, l.dispatched_at IS NOT NULL, "
    "(SELECT count(*) FROM raw_responses r WHERE r.fetch_request_id = f.id) FROM fetch_requests f "
    "JOIN quota_ledger l ON l.id = f.quota_ledger_id WHERE f.request_key = :k"
)


async def test_intermediate_states_at_each_fault_point(
    engine, current_epoch, jsearch_fixture_adapter, fixture_log, monkeypatch
) -> None:
    owner, cell = await queued_run(engine), await jsearch_cell(engine)
    ctx = jsearch_ctx(engine, owner, jsearch_fixture_adapter)
    spec = await page1_spec(engine, cell)
    # point -> (request, its marker, entry, its marker, stored responses) while paused there, and the sends
    expected = {
        "collect.after_reserve": (("reserved", False, "reserved", False, 0), 0),
        "collect.after_dispatch_marker": (("dispatched", True, "dispatched", True, 0), 0),
        "collect.after_response": (("dispatched", True, "dispatched", True, 0), 1),
        "collect.after_settle": (("succeeded", True, "billed", True, 1), 1),
    }
    for n, (point, (state, sends)) in enumerate(expected.items()):
        page = replace(spec, params={**spec.params, "query": f"{spec.params['query']} {n}"})  # its own key
        key = request_key(page, owner.run_id)
        pause = PausePoint(point)
        monkeypatch.setattr(acquire, "afault_point", pause)
        sent = len(served_requests(fixture_log))
        task = asyncio.create_task(acquire_page(ctx, page, cell_id=cell, phase="primary", planned_id=None))
        await asyncio.wait_for(pause.reached.wait(), 10)
        async with engine.connect() as c:
            assert tuple((await c.execute(RECEIPT, {"k": key})).one()) == state, point
        assert len(served_requests(fixture_log)) == sent + sends, point  # the send follows the marker
        pause.resume()
        result = await task
        assert isinstance(result, Served) and not result.replayed
        done = ("succeeded", True, "billed", True, 1)
        async with engine.connect() as c:
            assert tuple((await c.execute(RECEIPT, {"k": key})).one()) == done
        assert len(served_requests(fixture_log)) == sent + 1


async def test_replay_order_of_rule_two(
    engine,
    current_epoch,
    queue_app,
    jsearch_fixture_adapter,
    jsearch_variant,
    fixture_log,
    monkeypatch,
    log_events,
) -> None:
    cell = await jsearch_cell(engine)
    spec = await page1_spec(engine, cell)

    # (a) a settled 2xx row: the stored response is replayed by the next attempt, without HTTP
    owner = await queued_run(engine)
    seen: list[Sequence[str | None]] = []

    async def known(conn: AsyncConnection, ids: Sequence[str | None]) -> int:
        seen.append(ids)
        return 4  # four of the page's ten ids are known postings

    first = await acquire_page(
        jsearch_ctx(engine, owner, jsearch_fixture_adapter),
        spec,
        cell_id=cell,
        phase="primary",
        planned_id=None,
        known_native_ids=known,
    )
    assert isinstance(first, Served) and first.new_native_ids == 6 and len(seen) == 1 and len(seen[0]) == 10
    sent = served_requests(fixture_log)
    restarted = await supersede(engine, owner)
    later = utcnow()  # the new attempt plans the page again: the window moves, the request key does not
    again = await acquire_page(
        jsearch_ctx(engine, restarted, jsearch_fixture_adapter),
        replace(spec, window=(later - timedelta(days=7), later)),
        cell_id=cell,
        phase="primary",
        planned_id=first.fetch_request_id,
        known_native_ids=known,
    )
    assert isinstance(again, Served) and again.replayed and again.fetch_request_id == first.fetch_request_id
    assert (again.page.status, again.page.body, again.new_native_ids) == (200, first.page.body, 6)
    assert served_requests(fixture_log) == sent and len(seen) == 1  # no HTTP, no second peek
    replays = [e["fetch_request_id"] for e in log_events() if e["event"] == "page_replayed"]
    assert replays == [first.fetch_request_id]
    assert await attempts(engine, spec, owner) == [
        (1, "normal", "succeeded", None, None, 200, "billed", None, "probe")
    ]
    await finish(engine, restarted)

    # (b) a paid row dispatched by attempt 1 that died before its send: ambiguous, never re-dispatched
    owner = await queued_run(engine)
    await stop_at(
        "collect.after_dispatch_marker",
        monkeypatch,
        jsearch_ctx(engine, owner, jsearch_fixture_adapter),
        spec,
    )
    restarted = await supersede(engine, owner)
    lost = await acquire_page(
        jsearch_ctx(engine, restarted, jsearch_fixture_adapter),
        spec,
        cell_id=cell,
        phase="primary",
        planned_id=None,
    )
    assert isinstance(lost, Ambiguous)
    rows = [(1, "normal", "ambiguous", None, "unsettled", None, "ambiguous", None, "normal")]
    assert await attempts(engine, spec, owner) == rows
    assert "quota.ambiguous_attempt:provider:jsearch" in await open_alerts(engine)
    still = await acquire_page(
        jsearch_ctx(engine, restarted, jsearch_fixture_adapter),
        spec,
        cell_id=cell,
        phase="primary",
        planned_id=None,
    )
    assert still == lost and await attempts(engine, spec, owner) == rows  # resolved again: nothing written
    assert served_requests(fixture_log) == sent  # never sent
    await finish(engine, restarted)

    # (c) a reserved row of a superseded owner: released, skipped(superseded), then attempt 2 is served
    owner = await queued_run(engine)
    await stop_at(
        "collect.after_reserve", monkeypatch, jsearch_ctx(engine, owner, jsearch_fixture_adapter), spec
    )
    restarted = await supersede(engine, owner)
    served = await acquire_page(
        jsearch_ctx(engine, restarted, jsearch_fixture_adapter),
        spec,
        cell_id=cell,
        phase="primary",
        planned_id=None,
    )
    assert isinstance(served, Served) and not served.replayed
    assert await attempts(engine, spec, owner) == [
        (1, "normal", "skipped", "superseded", None, None, "not_billed", "never_dispatched", "normal"),
        (2, "normal", "succeeded", None, None, 200, "billed", None, "normal"),
    ]
    assert len(served_requests(fixture_log)) == len(sent) + 1
    await finish(engine, restarted)

    # (d) a settled 429 row (the provider throttled, quota left): one retry-class attempt, and no third
    throttled = jsearch_variant(
        {"search_v2_page1": ("search_v2_429", {"x-ratelimit-requests-remaining": "150"})}
    )
    owner = await queued_run(engine)
    await stop_at("collect.after_settle", monkeypatch, jsearch_ctx(engine, owner, throttled), spec)
    assert await attempts(engine, spec, owner) == [
        (1, "normal", "failed", None, "rate_limited", 429, "ambiguous", None, "normal")
    ]
    restarted = await supersede(engine, owner)
    retried = await acquire_page(
        jsearch_ctx(engine, restarted, throttled), spec, cell_id=cell, phase="primary", planned_id=None
    )
    assert isinstance(retried, Failed) and (retried.error_code, retried.http_status, retried.replayed) == (
        "rate_limited",
        429,
        False,
    )
    retry_rows = [
        (1, "normal", "failed", None, "rate_limited", 429, "ambiguous", None, "normal"),
        (2, "retry", "failed", None, "rate_limited", 429, "ambiguous", None, "retry"),
    ]
    assert await attempts(engine, spec, owner) == retry_rows
    final = await acquire_page(
        jsearch_ctx(engine, restarted, throttled), spec, cell_id=cell, phase="primary", planned_id=None
    )
    assert final == replace(retried, replayed=True) and await attempts(engine, spec, owner) == retry_rows
    await finish(engine, restarted)
    # the same within one call: the 429, its one retry, and the Failed of the second 429 (the two 429s above
    # opened the breaker, which would deny this run's attempt: it starts closed)
    await close_breaker(engine, spec.source_instance_id)
    owner = await queued_run(engine)
    in_call = await acquire_page(
        jsearch_ctx(engine, owner, throttled), spec, cell_id=cell, phase="primary", planned_id=None
    )
    assert isinstance(in_call, Failed) and (in_call.error_code, in_call.replayed) == ("rate_limited", False)
    assert await attempts(engine, spec, owner) == retry_rows
    assert len(served_requests(fixture_log)) == len(sent) + 1 + 4  # two 429s in each of (d)'s runs


def arbeitnow_plan(cell: int, instance: int) -> CellPlan:
    now = utcnow()
    return CellPlan(
        cell_id=cell,
        source_id="arbeitnow",
        instance_id=instance,
        query_text=None,
        query_lang=None,
        location_key=None,
        location_label=None,
        radius_km=None,
        phase="primary",
        request_class="free",
        bucket=None,
        window=(now - timedelta(days=1), now),
        max_days_old=None,
    )


async def test_free_source_retries_and_backoff(engine, current_epoch, arbeitnow_adapter, respx_mock) -> None:
    owner = await queued_run(engine)
    cell = await source_cell(engine, "arbeitnow", query=None, location_key=None)
    async with engine.connect() as c:
        instance = await instance_id(c, "arbeitnow")
    page1 = arbeitnow_adapter.plan(arbeitnow_plan(cell, instance))
    page = [arbeitnow_adapter.next_page(page1, str(n)) or page1 for n in (1, 2, 3, 4)]
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    ctx = acquire_ctx(engine, owner, arbeitnow_adapter, billing="free", provider=None, sleep=sleep)
    feed = respx_mock.get(FEED_URL)
    status, headers, body = load_fixture("arbeitnow", "page1")
    ok = httpx.Response(status, headers=headers, content=body)
    throttled: list[int] = []

    @asynccontextmanager
    async def throttle() -> AsyncIterator[None]:  # the caller's rate limit, around every send
        throttled.append(feed.call_count)
        yield

    async def run(spec: RequestSpec) -> object:
        return await acquire_page(
            ctx, spec, cell_id=cell, phase="primary", planned_id=None, throttle=throttle
        )

    feed.side_effect = [httpx.Response(503), httpx.Response(503), ok]
    assert isinstance(await run(page[0]), Served)
    assert await attempts(engine, page[0], owner) == [
        (1, "free", "failed", None, "transient", 503, None, None, None),
        (2, "free", "failed", None, "transient", 503, None, None, None),
        (3, "free", "succeeded", None, None, 200, None, None, None),
    ]
    assert len(sleeps) == 2 and 0.75 <= sleeps[0] <= 1.25 and 3.0 <= sleeps[1] <= 5.0  # 1 s, 4 s, ±25 %

    sleeps.clear()
    feed.side_effect = [
        httpx.Response(429, headers={"retry-after": "30"}),
        httpx.Response(429, headers={"retry-after": "120"}),
        ok,
    ]
    assert isinstance(await run(page[1]), Served)
    assert sleeps == [30.0, 60.0]  # Retry-After honoured, at most 60 s

    sleeps.clear()
    feed.side_effect = [httpx.Response(503)] * 3
    failed = await run(page[2])
    assert isinstance(failed, Failed) and (failed.error_code, failed.http_status) == ("transient", 503)
    assert len(await attempts(engine, page[2], owner)) == 3 and len(sleeps) == 2  # three attempts at most

    sleeps.clear()
    feed.side_effect = [httpx.Response(404, json={"message": "Not Found"})]
    missing = await run(page[3])
    assert isinstance(missing, Failed) and (missing.error_code, missing.http_status, missing.replayed) == (
        "not_found",
        404,
        False,
    )
    assert len(await attempts(engine, page[3], owner)) == 1 and sleeps == []  # no retry on a 404
    assert feed.call_count == 3 + 3 + 3 + 1 and throttled == list(range(10))  # each send throttled once
    health = await scalar(engine, "SELECT health FROM source_instances WHERE id = :i", i=instance)
    assert health == "not_found"  # a 404 opens the breaker at once


async def test_a_free_page_found_dispatched_is_closed_ambiguous_and_redispatched(
    engine, current_epoch, queue_app, arbeitnow_adapter, respx_mock, monkeypatch
) -> None:  # C2b's mechanism (Ruling P5)
    owner = await queued_run(engine)
    cell = await source_cell(engine, "arbeitnow", query=None, location_key=None)
    async with engine.connect() as c:
        spec = arbeitnow_adapter.plan(arbeitnow_plan(cell, await instance_id(c, "arbeitnow")))
    status, headers, body = load_fixture("arbeitnow", "page1")
    feed = respx_mock.get(FEED_URL).mock(return_value=httpx.Response(status, headers=headers, content=body))
    ctx = acquire_ctx(engine, owner, arbeitnow_adapter, billing="free", provider=None)
    await stop_at("collect.after_dispatch_marker", monkeypatch, ctx, spec)
    assert feed.call_count == 0
    restarted = await supersede(engine, owner)
    r = await acquire_page(
        acquire_ctx(engine, restarted, arbeitnow_adapter, billing="free", provider=None),
        spec,
        cell_id=cell,
        phase="primary",
        planned_id=None,
    )
    assert isinstance(r, Served) and not r.replayed and feed.call_count == 1  # re-dispatched once
    assert await attempts(engine, spec, owner) == [
        (1, "free", "ambiguous", None, "unsettled", None, None, None, None),
        (2, "free", "succeeded", None, None, 200, None, None, None),
    ]


async def test_denial_and_shutdown_send_nothing(
    engine, current_epoch, jsearch_fixture_adapter, fixture_log
) -> None:
    owner, cell = await queued_run(engine), await jsearch_cell(engine)
    spec = await page1_spec(engine, cell)
    now = utcnow()
    exhausted = replace(
        jsearch_window(now, limit=200, reserve=20, tokens=2, interval_s=SIX_H), state="exhausted_until_reset"
    )
    await insert_window(engine, exhausted)
    ctx = acquire_ctx(engine, owner, jsearch_fixture_adapter, billing="paid", provider="jsearch")
    denied = await acquire_page(ctx, spec, cell_id=cell, phase="primary", planned_id=None)
    assert isinstance(denied, Denied) and denied.skip_reason == "exhausted"
    assert await attempts(engine, spec, owner) == [
        (1, "normal", "skipped", "exhausted", None, None, None, None, None)
    ]
    other = replace(spec, page_no=2)
    stopped = acquire_ctx(
        engine, owner, jsearch_fixture_adapter, billing="paid", provider="jsearch", stopped=lambda: True
    )
    shutdown = await acquire_page(stopped, other, cell_id=cell, phase="primary", planned_id=None)
    assert isinstance(shutdown, Denied) and shutdown.skip_reason == "shutdown"
    assert await attempts(engine, other, owner) == [
        (1, "normal", "skipped", "shutdown", None, None, None, None, None)
    ]
    assert await scalar(engine, "SELECT count(*) FROM quota_ledger") == 0
    assert served_requests(fixture_log) == []
    # a denial is the request's receipt: resolving it again replays the denial
    assert await acquire_page(ctx, spec, cell_id=cell, phase="primary", planned_id=None) == denied
    assert len(await attempts(engine, spec, owner)) == 1


async def test_shutdown_after_the_reservation_releases_it_and_sends_nothing(
    engine, current_epoch, jsearch_fixture_adapter, fixture_log
) -> None:  # Review Focus 4's mechanism (Task 14 pins it on the node)
    owner, cell = await queued_run(engine), await jsearch_cell(engine)
    spec = await page1_spec(engine, cell)
    checks = iter([False, True])  # admitted before the reservation, stopped right after it
    ctx = acquire_ctx(
        engine,
        owner,
        jsearch_fixture_adapter,
        billing="paid",
        provider="jsearch",
        stopped=lambda: next(checks),
    )
    r = await acquire_page(ctx, spec, cell_id=cell, phase="primary", planned_id=None)
    assert isinstance(r, Denied) and r.skip_reason == "shutdown"
    assert await attempts(engine, spec, owner) == [
        (1, "normal", "skipped", "shutdown", None, None, "not_billed", "never_dispatched", "probe")
    ]
    assert served_requests(fixture_log) == []  # the user is not billed
    assert (await window(engine, "jsearch")).probe_used is False  # the next request probes at once


async def test_an_own_reservation_left_by_an_earlier_call_is_continued(
    engine, current_epoch, jsearch_fixture_adapter, fixture_log, monkeypatch
) -> None:
    """A call of this attempt that died after its reservation (e.g. a lost connection before the marker,
    then the node retried) left nothing sent: the next call continues that reservation, no second entry."""
    owner, cell = await queued_run(engine), await jsearch_cell(engine)
    spec = await page1_spec(engine, cell)
    ctx = acquire_ctx(engine, owner, jsearch_fixture_adapter, billing="paid", provider="jsearch")
    await stop_at("collect.after_reserve", monkeypatch, ctx, spec)
    r = await acquire_page(ctx, spec, cell_id=cell, phase="primary", planned_id=None)
    assert isinstance(r, Served) and not r.replayed
    assert await attempts(engine, spec, owner) == [
        (1, "normal", "succeeded", None, None, 200, "billed", None, "probe")
    ]
    assert await scalar(engine, "SELECT count(*) FROM quota_ledger WHERE kind = 'request'") == 1
    assert len(served_requests(fixture_log)) == 1


async def test_stale_owner_dispatch_marker_is_fenced(
    engine, current_epoch, queue_app, jsearch_fixture_adapter, monkeypatch, fixture_log
) -> None:  # C28, in process
    owner, cell = await queued_run(engine), await jsearch_cell(engine)
    spec = await page1_spec(engine, cell)
    pause = PausePoint("collect.after_reserve")
    monkeypatch.setattr(acquire, "afault_point", pause)
    ctx = acquire_ctx(engine, owner, jsearch_fixture_adapter, billing="paid", provider="jsearch")
    task = asyncio.create_task(acquire_page(ctx, spec, cell_id=cell, phase="primary", planned_id=None))
    await asyncio.wait_for(pause.reached.wait(), 10)
    async with engine.begin() as c:
        attempt = await runs.begin_new_attempt(
            c, owner.run_id, epoch=1, end_reason="worker_takeover", defer=defer_in_tx
        )
    assert attempt == 2
    pause.resume()
    with pytest.raises(FencedOut):
        await task
    assert served_requests(fixture_log) == []  # the stale owner never sent
    assert await attempts(engine, spec, owner) == [
        (1, "normal", "reserved", None, None, None, "reserved", None, "probe")
    ]
    new = acquire_ctx(
        engine, RunOwner(owner.run_id, attempt), jsearch_fixture_adapter, billing="paid", provider="jsearch"
    )
    r = await acquire_page(new, spec, cell_id=cell, phase="primary", planned_id=None)
    assert isinstance(r, Served) and not r.replayed
    # attempt 2 is the probe again: the release restored the window's probe
    assert await attempts(engine, spec, owner) == [
        (1, "normal", "skipped", "superseded", None, None, "not_billed", "never_dispatched", "probe"),
        (2, "normal", "succeeded", None, None, 200, "billed", None, "probe"),
    ]
    assert [r["case"] for r in served_requests(fixture_log)] == ["search_v2_page1"]


async def test_contract_violation_fails_the_request_and_opens_the_breaker(
    engine, current_epoch, jsearch_html_adapter
) -> None:  # Review Focus 3
    owner, cell = await queued_run(engine), await jsearch_cell(engine)
    spec = await page1_spec(engine, cell)
    r = await acquire_page(
        acquire_ctx(engine, owner, jsearch_html_adapter, billing="paid", provider="jsearch"),
        spec,
        cell_id=cell,
        phase="primary",
        planned_id=None,
    )
    assert isinstance(r, Failed) and (r.error_code, r.http_status, r.replayed) == (
        "contract",
        200,
        False,
    )  # returned, never raised
    async with engine.connect() as c:
        fr = (
            await c.execute(
                sa.text(
                    "SELECT status, error_code, raw_response_id, quota_ledger_id "
                    "FROM fetch_requests WHERE id=:i"
                ),
                {"i": r.fetch_request_id},
            )
        ).one()
        rr = (
            await c.execute(
                sa.text("SELECT item_count, parsed_at IS NOT NULL FROM raw_responses WHERE id=:i"),
                {"i": fr.raw_response_id},
            )
        ).one()
        body = (await load_response(c, fr.raw_response_id, spec)).body
        led = (
            await c.execute(
                sa.text("SELECT state, http_status FROM quota_ledger WHERE id=:i"), {"i": fr.quota_ledger_id}
            )
        ).one()
        js = (await c.execute(JSEARCH)).one()
        alert = (
            await c.execute(
                sa.text("SELECT severity FROM alerts WHERE dedup_key = :k AND resolved_at IS NULL"),
                {"k": f"source.contract_broken:instance:{js.id}"},
            )
        ).scalar_one()
    assert (
        (fr.status, fr.error_code) == ("failed", "contract")
        and tuple(rr) == (0, True)
        and 0 < len(body) <= 4096
    )  # the first 4 KiB, never parsed
    assert tuple(led) == ("billed", 200) and (js.health, alert) == (
        "contract_broken",
        "critical",
    )  # a 200 is billed whatever the body: billing ≠ envelope validity


async def test_contract_failure_is_replayed_after_restart(
    engine, current_epoch, queue_app, jsearch_html_adapter, fixture_log
) -> None:  # Review Focus 3
    owner, cell = await queued_run(engine), await jsearch_cell(engine)
    spec = await page1_spec(engine, cell)
    first = await acquire_page(
        acquire_ctx(engine, owner, jsearch_html_adapter, billing="paid", provider="jsearch"),
        spec,
        cell_id=cell,
        phase="primary",
        planned_id=None,
    )
    sent = served_requests(fixture_log)
    async with engine.connect() as c:
        failures = (await c.execute(JSEARCH)).one().consecutive_failures
    async with engine.begin() as c:
        attempt = await runs.begin_new_attempt(
            c, owner.run_id, epoch=1, end_reason="worker_takeover", defer=defer_in_tx
        )  # a deploy restarts the run
    assert attempt == 2
    again = await acquire_page(
        acquire_ctx(
            engine,
            runs.RunOwner(owner.run_id, attempt),
            jsearch_html_adapter,
            billing="paid",
            provider="jsearch",
        ),
        spec,
        cell_id=cell,
        phase="primary",
        planned_id=None,
    )
    assert isinstance(again, Failed) and (again.fetch_request_id, again.error_code, again.replayed) == (
        first.fetch_request_id,
        "contract",
        True,
    )
    async with engine.connect() as c:
        rows = (
            await c.execute(
                sa.text("SELECT (SELECT count(*) FROM fetch_requests), (SELECT count(*) FROM quota_ledger)")
            )
        ).one()
        after = (await c.execute(JSEARCH)).one().consecutive_failures
    assert (
        len(sent) == 1
        and served_requests(fixture_log) == sent
        and tuple(rows) == (1, 1)
        and after == failures
    )  # no second send, receipt, entry or breaker write


async def test_status_is_classified_before_the_envelope(engine, current_epoch, respx_mock) -> None:
    """A non-2xx response is classified by its status: its body (HTML, an error JSON) is never judged as an
    envelope, so it does not break the contract (T9 carry)."""
    owner, cell = await queued_run(engine), await jsearch_cell(engine)
    spec = await page1_spec(engine, cell)
    await insert_window(engine, jsearch_window(utcnow(), limit=200, reserve=20, tokens=10, interval_s=SIX_H))
    html = {"content-type": "text/html"}
    route = respx_mock.get(JSEARCH_URL)
    async with httpx.AsyncClient() as client:
        adapter = JSearchAdapter(client, api_key=b"fake", instance_id=spec.source_instance_id, clock=utcnow)
        ctx = jsearch_ctx(engine, owner, adapter)
        route.side_effect = [httpx.Response(404, headers=html, content=b"<html>gone</html>")]
        missing = await acquire_page(ctx, spec, cell_id=cell, phase="primary", planned_id=None)
        assert isinstance(missing, Failed) and (missing.error_code, missing.http_status) == ("client", 404)
        async with engine.connect() as c:  # counted toward the threshold, not contract_broken
            assert tuple((await c.execute(JSEARCH)).one())[1:] == ("degraded", 1)
        route.side_effect = [httpx.Response(401, headers=html, content=b"<html>no</html>")]
        refused = await acquire_page(
            ctx, replace(spec, page_no=2), cell_id=cell, phase="primary", planned_id=None
        )
        assert isinstance(refused, Failed) and (refused.error_code, refused.http_status) == ("auth", 401)
        async with engine.connect() as c:
            assert (await c.execute(JSEARCH)).one().health == "auth_failed"
    assert route.call_count == 2  # neither is retried


async def test_timeouts_count_for_the_breaker_and_a_billed_provider_error_is_never_retried(
    engine, current_epoch, respx_mock
) -> None:
    owner, cell = await queued_run(engine), await jsearch_cell(engine)
    spec = await page1_spec(engine, cell)
    await insert_window(engine, jsearch_window(utcnow(), limit=200, reserve=20, tokens=10, interval_s=SIX_H))
    route = respx_mock.get(JSEARCH_URL)
    async with httpx.AsyncClient() as client:
        adapter = JSearchAdapter(client, api_key=b"fake", instance_id=spec.source_instance_id, clock=utcnow)
        ctx = jsearch_ctx(engine, owner, adapter)
        # a 200 that carries the provider's own error: transient and billed, so never retried (T9 carry)
        route.side_effect = [httpx.Response(200, json={"status": "ERROR", "error": {"message": "x"}})]
        report = await acquire_page(ctx, spec, cell_id=cell, phase="primary", planned_id=None)
        assert isinstance(report, Failed) and (report.error_code, report.http_status) == ("transient", 200)
        assert [a[2:8] for a in await attempts(engine, spec, owner)] == [
            ("failed", None, "transient", 200, "billed", None)
        ]
        # timeouts: the ledger keeps them ambiguous, the breaker counts them transient, open at 2 (T5 carry)
        route.side_effect = httpx.ReadTimeout("read timed out")
        await close_breaker(engine, spec.source_instance_id)  # the 200 error counted one failure already
        for page_no in (2, 3):
            timed_out = await acquire_page(
                ctx, replace(spec, page_no=page_no), cell_id=cell, phase="primary", planned_id=None
            )
            assert isinstance(timed_out, Ambiguous)
            assert [a[2:7] for a in await attempts(engine, replace(spec, page_no=page_no), owner)] == [
                ("ambiguous", None, "ambiguous", None, "ambiguous")
            ]
        # open: the next new attempt is skipped, nothing reserved or sent (Ruling T14-b)
        blocked = await acquire_page(
            ctx, replace(spec, page_no=4), cell_id=cell, phase="primary", planned_id=None
        )
        assert isinstance(blocked, Denied) and blocked.skip_reason == "breaker_open"
        assert [a[2:7] for a in await attempts(engine, replace(spec, page_no=4), owner)] == [
            ("skipped", "breaker_open", None, None, None)
        ]
    assert route.call_count == 3  # no retry of a 200, none of a timeout, nothing while open
    async with engine.connect() as c:
        assert tuple((await c.execute(JSEARCH)).one())[1:] == ("breaker_open", 2)


async def test_serpapi_quota_exhausted_writes_no_breaker_row(
    engine, current_epoch, serpapi_fixture_adapter, respx_mock
) -> None:
    owner = await queued_run(engine)
    cell = await source_cell(engine, "serpapi")
    spec = await serpapi_spec(engine, serpapi_fixture_adapter, cell)
    out_of_searches = {
        "search_metadata": {"status": "Error"},
        "error": "Your account has run out of searches.",
    }
    respx_mock.routes["serpapi_search"].mock(return_value=httpx.Response(200, json=out_of_searches))
    ctx = acquire_ctx(engine, owner, serpapi_fixture_adapter, billing="paid", provider="serpapi")
    r = await acquire_page(ctx, spec, cell_id=cell, phase="fallback", planned_id=None)
    assert isinstance(r, Failed) and (r.error_code, r.http_status) == ("quota_exhausted", 200)
    assert [a[2:7] for a in await attempts(engine, spec, owner)] == [
        ("failed", None, "quota_exhausted", 200, "billed")
    ]
    breaker_row = "SELECT health, consecutive_failures FROM source_instances WHERE id = :i"
    async with engine.connect() as c:  # no breaker write
        assert tuple((await c.execute(sa.text(breaker_row), {"i": spec.source_instance_id})).one()) == (
            "ok",
            0,
        )


async def serpapi_spec(engine: AsyncEngine, adapter: SerpApiAdapter, cell: int) -> RequestSpec:
    """The SerpApi page-1 spec of the `remote-de` cell (the fallback phase)."""
    now = utcnow()
    async with engine.connect() as c:
        instance = await instance_id(c, "serpapi")
    return adapter.plan(
        CellPlan(
            cell_id=cell,
            source_id="serpapi",
            instance_id=instance,
            query_text="python developer",
            query_lang="en",
            location_key="remote-de",
            location_label=None,
            radius_km=None,
            phase="fallback",
            request_class="normal",
            bucket=None,
            window=(now - timedelta(days=7), now),
            max_days_old=None,
        )
    )


async def test_serpapi_account_is_read_before_each_search_and_after_the_last(
    engine, current_epoch, serpapi_fixture_adapter, respx_mock
) -> None:
    cell = await source_cell(engine, "serpapi")
    spec = await serpapi_spec(engine, serpapi_fixture_adapter, cell)
    account, search = respx_mock.routes["serpapi_account"], respx_mock.routes["serpapi_search"]

    # no account read, no search: §5.3.2 fails closed
    blind = await queued_run(engine)
    account.side_effect = [httpx.Response(500)]
    denied = await acquire_page(
        acquire_ctx(engine, blind, serpapi_fixture_adapter, billing="paid", provider="serpapi"),
        spec,
        cell_id=cell,
        phase="fallback",
        planned_id=None,
    )
    assert isinstance(denied, Denied) and denied.skip_reason == "window_cap"
    assert (account.call_count, search.call_count) == (1, 0)
    assert [a[2:4] for a in await attempts(engine, spec, blind)] == [("skipped", "window_cap")]
    async with engine.connect() as c:
        assert (await c.execute(WINDOW, {"p": "serpapi"})).first() is None  # nothing observed
    await finish(engine, blind)

    owner = await queued_run(engine)
    ctx = acquire_ctx(engine, owner, serpapi_fixture_adapter, billing="paid", provider="serpapi")
    account.side_effect = [serpapi_account_response()] + [serpapi_account_response("account_after")] * 2
    first = await acquire_page(ctx, spec, cell_id=cell, phase="fallback", planned_id=None)
    assert isinstance(first, Served)
    w1 = await window(engine, "serpapi")
    cursor = serpapi_fixture_adapter.continuation(first.page)[1]
    second_spec = serpapi_fixture_adapter.next_page(spec, cursor)
    assert second_spec is not None
    second = await acquire_page(ctx, second_spec, cell_id=cell, phase="fallback", planned_id=None)
    assert isinstance(second, Served)
    w2 = await window(engine, "serpapi")
    # the read after the last search of the traversal (traverse_cell's, Task 14)
    after = await serpapi_fixture_adapter.account()
    async with engine.begin() as c:
        await ledger.record_account(c, after, settings=ctx.settings, now=utcnow())
    w3 = await window(engine, "serpapi")
    paths = [call.request.url.path for call in respx_mock.calls]
    assert paths == ["/account.json"] + ["/account.json", "/search.json"] * 2 + ["/account.json"]
    assert w1.hour_observed_at < w2.hour_observed_at < w3.hour_observed_at  # every read is applied
    assert w1.last_observed_at < w2.last_observed_at < w3.last_observed_at
    assert (w1.state, w1.observed_remaining, w1.observed_this_hour) == ("validated", 248, 0)
    assert (w2.observed_remaining, w2.observed_this_hour) == (247, 1)
    assert (w3.observed_remaining, w3.observed_this_hour, w3.hour_observed_at) == (247, 1, after.observed_at)

    # step 0: a read older than the window's hour_observed_at changes nothing, not even foreign usage
    alerts = await open_alerts(engine)
    stale = replace(
        after,
        observed_at=w3.hour_observed_at - timedelta(minutes=5),
        this_hour_searches=40,
        plan_searches_left=100,
    )
    async with engine.begin() as c:
        await ledger.record_account(c, stale, settings=ctx.settings, now=utcnow())
    assert await window(engine, "serpapi") == w3 and await open_alerts(engine) == alerts


async def test_acquire_never_raises_a_source_error_for_an_account_read(
    engine, current_epoch, serpapi_fixture_adapter, respx_mock
) -> None:
    """An account body that breaks its contract is a closed window as well, never an exception."""
    owner = await queued_run(engine)
    cell = await source_cell(engine, "serpapi")
    spec = await serpapi_spec(engine, serpapi_fixture_adapter, cell)
    respx_mock.routes["serpapi_account"].side_effect = [httpx.Response(200, content=b"<html>")]
    try:
        r = await acquire_page(
            acquire_ctx(engine, owner, serpapi_fixture_adapter, billing="paid", provider="serpapi"),
            spec,
            cell_id=cell,
            phase="fallback",
            planned_id=None,
        )
    except SourceError as exc:  # pragma: no cover - the failure this test exists for
        pytest.fail(f"acquire_page raised {exc!r}")
    assert isinstance(r, Denied) and r.skip_reason == "window_cap"
    assert respx_mock.routes["serpapi_search"].call_count == 0


async def test_a_serpapi_account_read_without_its_key_opens_the_breaker_as_auth_failed(
    engine, current_epoch, respx_mock
) -> None:
    """Ruling FR-l: the account read of a SerpApi adapter without its key is refused locally (nothing sent),
    and the refusal is visible: the breaker counts an `auth` failure, which opens `auth_failed` with the
    critical `source.auth_failed` at once, and the request is `skipped('breaker_open')`, not `window_cap`."""
    owner = await queued_run(engine)
    cell = await source_cell(engine, "serpapi")
    async with engine.connect() as c:
        instance = await instance_id(c, "serpapi")
    async with httpx.AsyncClient() as client:
        adapter = SerpApiAdapter(client, api_key=b"", instance_id=instance, clock=utcnow)
        spec = await serpapi_spec(engine, adapter, cell)
        r = await acquire_page(
            acquire_ctx(engine, owner, adapter, billing="paid", provider="serpapi"),
            spec,
            cell_id=cell,
            phase="fallback",
            planned_id=None,
        )
    assert isinstance(r, Denied) and r.skip_reason == "breaker_open"
    assert not respx_mock.calls  # neither the account read nor a search was sent
    async with engine.connect() as c:
        health = await c.execute(
            sa.text("SELECT health, last_error_code FROM source_instances WHERE id = :i"), {"i": instance}
        )
        assert tuple(health.one()) == ("auth_failed", "auth")
        alert = await c.execute(
            sa.text("SELECT severity FROM alerts WHERE code = 'source.auth_failed' AND resolved_at IS NULL")
        )
        assert alert.scalar_one() == "critical"
        assert (await c.execute(sa.text("SELECT count(*) FROM quota_ledger"))).scalar_one() == 0


class Ticking:
    """A clock that moves one second at every reading, from `now`; the test may set `now` to jump it."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


OUT_OF_SEARCHES = {"search_metadata": {"status": "Error"}, "error": "Your account has run out of searches."}


async def test_serpapi_out_of_searches_exhausts_the_window_until_its_reset(
    engine, current_epoch, serpapi_fixture_adapter, respx_mock
) -> None:  # fix round 1, I1
    clock = Ticking(utcnow())
    renewal = (clock.now + timedelta(days=5)).date()
    account, search = respx_mock.routes["serpapi_account"], respx_mock.routes["serpapi_search"]

    def read(left: int, renews: date) -> httpx.Response:
        return serpapi_account_response(plan_renewal_date=renews.isoformat(), plan_searches_left=left)

    owner, cell = await queued_run(engine), await source_cell(engine, "serpapi")
    async with engine.connect() as c:
        instance = await instance_id(c, "serpapi")
    async with httpx.AsyncClient() as client:
        adapter = SerpApiAdapter(client, api_key=b"fake", instance_id=instance, clock=clock)
        spec = await serpapi_spec(engine, adapter, cell)
        ctx = acquire_ctx(engine, owner, adapter, billing="paid", provider="serpapi", clock=clock)

        async def search_page(page_no: int) -> object:
            page = replace(spec, page_no=page_no)
            return await acquire_page(ctx, page, cell_id=cell, phase="fallback", planned_id=None)

        account.side_effect = [read(240, renewal), read(200, renewal)]
        search.side_effect = [httpx.Response(200, json=OUT_OF_SEARCHES)]
        first = await search_page(1)
        assert isinstance(first, Failed) and first.error_code == "quota_exhausted"
        assert (await window(engine, "serpapi")).state == "exhausted_until_reset"
        # (a) the next search of the run is denied, even when a lagging account read still shows searches left
        lagging = await search_page(2)
        assert isinstance(lagging, Denied) and lagging.skip_reason == "exhausted"
        assert search.call_count == 1 and (await window(engine, "serpapi")).state == "exhausted_until_reset"
        # (b) no permanent lockout: past the renewal, the next month's read admits a search again
        clock.now = datetime.combine(renewal, time(1), tzinfo=UTC)
        account.side_effect = [read(250, renewal + timedelta(days=31))]
        status, headers, body = load_fixture("serpapi", "google_jobs_hl_en")
        search.side_effect = [httpx.Response(status, headers=headers, content=body)]
        renewed = await search_page(3)
        assert isinstance(renewed, Served) and search.call_count == 2
    async with engine.connect() as c:
        current = (
            await c.execute(
                sa.text(
                    "SELECT state, window_start FROM quota_windows WHERE provider = 'serpapi' AND is_current"
                )
            )
        ).one()
    assert tuple(current) == ("validated", datetime.combine(renewal, time(0), tzinfo=UTC))


SMARTRECRUITERS = sa.text(
    "INSERT INTO watchlist_companies "
    "(display_name, company_key, ats_type, board_slug, detection, validation_status) "
    "VALUES ('Delivery Hero', 'delivery hero', 'smartrecruiters', 'DeliveryHero', 'manual', 'ok') "
    "RETURNING id"
)
INSTANCE_CELL = sa.text("INSERT INTO coverage_cells (source_instance_id) VALUES (:i) RETURNING id")


async def test_a_free_detail_404_is_a_client_failure_not_a_missing_board(
    engine, current_epoch, fake_http
) -> None:  # fix round 1, I2: `not_found` is a board (list) 404 only
    owner = await queued_run(engine)
    async with engine.begin() as c:
        iid = await instance_for_company(c, (await c.execute(SMARTRECRUITERS)).scalar_one())
        cell = int((await c.execute(INSTANCE_CELL, {"i": iid})).scalar_one())
        row = next(i for i in await list_instances(c) if i.id == iid)
    now = utcnow()
    plan = CellPlan(
        cell_id=cell,
        source_id="smartrecruiters",
        instance_id=iid,
        query_text=None,
        query_lang=None,
        location_key=None,
        location_label=None,
        radius_km=None,
        phase="primary",
        request_class="free",
        bucket=None,
        window=(now, now),
        max_days_old=None,
    )
    async with http.make_client("smartrecruiters") as client:
        adapter = ats.adapter_for(row, client, clock=utcnow)
        listing = adapter.plan(plan)
        async with engine.begin() as c:
            parent = await insert_fetch_request(
                c,
                owner=owner,
                cell_id=cell,
                source_instance_id=iid,
                phase="primary",
                spec=listing,
                request_key=request_key(listing, owner.run_id),
                attempt_seq=1,
                status="planned",
            )
        detail = replace(
            listing,
            operation="detail",
            params={},
            cursor_in=None,
            detail_native_id="744000000000001",  # a withdrawn posting: the fixture routes answer 404
            parent_fetch_request_id=parent,
        )
        ctx = acquire_ctx(engine, owner, adapter, billing="free", provider=None)
        r = await acquire_page(ctx, detail, cell_id=cell, phase="primary", planned_id=None)
    assert isinstance(r, Failed) and (r.error_code, r.http_status, r.replayed) == ("client", 404, False)
    async with engine.connect() as c:
        breaker_row = sa.text("SELECT health, consecutive_failures FROM source_instances WHERE id = :i")
        assert tuple((await c.execute(breaker_row, {"i": iid})).one()) == (
            "degraded",
            1,
        )  # the board is there


async def jsearch_on_respx(engine: AsyncEngine, client: httpx.AsyncClient, owner: RunOwner) -> AcquireContext:
    """A roomy JSearch context on `client` (respx-mocked), over a validated window."""
    await insert_window(engine, jsearch_window(utcnow(), limit=200, reserve=20, tokens=10, interval_s=SIX_H))
    async with engine.connect() as c:
        instance = await instance_id(c, "jsearch")
    return jsearch_ctx(
        engine, owner, JSearchAdapter(client, api_key=b"fake", instance_id=instance, clock=utcnow)
    )


async def test_a_paid_gateway_404_is_transient_not_billed_and_not_retried(
    engine, current_epoch, respx_mock
) -> None:  # fix round 1, M6
    owner, cell = await queued_run(engine), await jsearch_cell(engine)
    spec = await page1_spec(engine, cell)
    route = respx_mock.get(JSEARCH_URL)
    gateway = {"x-rapidapi-proxy-response": "true", "content-type": "application/json"}
    route.side_effect = [httpx.Response(404, headers=gateway, json={"message": "Endpoint does not exist"})]
    async with httpx.AsyncClient() as client:
        ctx = await jsearch_on_respx(engine, client, owner)
        r = await acquire_page(ctx, spec, cell_id=cell, phase="primary", planned_id=None)
    assert isinstance(r, Failed) and (r.error_code, r.http_status, r.replayed) == ("transient", 404, False)
    assert await attempts(engine, spec, owner) == [
        (1, "normal", "failed", None, "transient", 404, "not_billed", "rapidapi_gateway_404", "normal")
    ]
    assert route.call_count == 1  # §5.2: a paid retry follows a 429 or a 5xx only
    async with engine.connect() as c:
        assert tuple((await c.execute(JSEARCH)).one())[1:] == ("degraded", 1)  # a transient failure


async def test_a_paid_5xx_earns_exactly_one_retry(
    engine, current_epoch, respx_mock
) -> None:  # fix round 1, M7
    owner, cell = await queued_run(engine), await jsearch_cell(engine)
    spec = await page1_spec(engine, cell)
    route = respx_mock.get(JSEARCH_URL)
    status, headers, body = load_fixture("jsearch", "search_v2_page1")
    async with httpx.AsyncClient() as client:
        ctx = await jsearch_on_respx(engine, client, owner)
        route.side_effect = [httpx.Response(503), httpx.Response(503)]
        failed = await acquire_page(ctx, spec, cell_id=cell, phase="primary", planned_id=None)
        assert isinstance(failed, Failed) and (failed.error_code, failed.http_status) == ("transient", 503)
        assert await attempts(engine, spec, owner) == [
            (1, "normal", "failed", None, "transient", 503, "ambiguous", None, "normal"),
            (2, "retry", "failed", None, "transient", 503, "ambiguous", None, "retry"),
        ]
        assert route.call_count == 2  # one retry, never a third attempt
        await close_breaker(engine, spec.source_instance_id)  # the two 503s opened it (paid threshold 2)
        recovered = replace(spec, page_no=2)
        route.side_effect = [httpx.Response(502), httpx.Response(status, headers=headers, content=body)]
        served = await acquire_page(ctx, recovered, cell_id=cell, phase="primary", planned_id=None)
        assert isinstance(served, Served) and route.call_count == 4
        assert await attempts(engine, recovered, owner) == [
            (1, "normal", "failed", None, "transient", 502, "ambiguous", None, "normal"),
            (2, "retry", "succeeded", None, None, 200, "billed", None, "retry"),
        ]


async def test_a_paid_connect_error_before_send_is_not_billed_and_not_retried(
    engine, current_epoch, respx_mock
) -> None:  # fix round 1, M7
    owner, cell = await queued_run(engine), await jsearch_cell(engine)
    spec = await page1_spec(engine, cell)
    route = respx_mock.get(JSEARCH_URL)
    route.side_effect = httpx.ConnectError("connection refused")
    async with httpx.AsyncClient() as client:
        ctx = await jsearch_on_respx(engine, client, owner)
        r = await acquire_page(ctx, spec, cell_id=cell, phase="primary", planned_id=None)
    assert isinstance(r, Failed) and (r.error_code, r.http_status) == ("transient", None)
    assert await attempts(engine, spec, owner) == [
        (1, "normal", "failed", None, "transient", None, "not_billed", "connect_failed_before_send", "normal")
    ]
    assert route.call_count == 1


async def test_an_exception_between_send_and_settlement_leaves_the_row_dispatched(
    engine, current_epoch, jsearch_fixture_adapter, fixture_log
) -> None:  # fix round 1, M7: an error in process (no process death) after the send
    owner, cell = await queued_run(engine), await jsearch_cell(engine)
    spec = await page1_spec(engine, cell)
    ctx = acquire_ctx(engine, owner, jsearch_fixture_adapter, billing="paid", provider="jsearch")

    async def broken(conn: AsyncConnection, ids: Sequence[str | None]) -> int:
        raise RuntimeError("a bug after the send")

    with pytest.raises(RuntimeError, match="a bug after the send"):
        await acquire_page(ctx, spec, cell_id=cell, phase="primary", planned_id=None, known_native_ids=broken)
    assert await attempts(engine, spec, owner) == [
        (1, "normal", "dispatched", None, None, None, "dispatched", None, "probe")
    ]
    assert len(served_requests(fixture_log)) == 1
    again = await acquire_page(ctx, spec, cell_id=cell, phase="primary", planned_id=None)
    assert isinstance(again, Ambiguous)
    assert await attempts(engine, spec, owner) == [
        (1, "normal", "ambiguous", None, "unsettled", None, "ambiguous", None, "probe")
    ]
    assert len(served_requests(fixture_log)) == 1  # the paid page is never sent twice


RAISE = "DO $$ BEGIN RAISE EXCEPTION 'injected' USING ERRCODE = '{}'; END $$"


async def test_a_serialization_failure_in_the_settlement_is_retried_with_the_response_in_hand(
    engine, current_epoch, jsearch_fixture_adapter, fixture_log, monkeypatch
) -> None:  # fix round 1, M1
    owner, cell = await queued_run(engine), await jsearch_cell(engine)
    spec = await page1_spec(engine, cell)
    ctx = jsearch_ctx(engine, owner, jsearch_fixture_adapter)
    real = store_response  # what `acquire` calls
    injected: list[str] = ["40P01"]  # the SQLSTATEs the next settlement transactions fail with, in order

    async def flaky(conn, *args, **kwargs):
        if injected:
            await conn.execute(sa.text(RAISE.format(injected.pop(0))))
        return await real(conn, *args, **kwargs)

    monkeypatch.setattr(acquire, "store_response", flaky)
    r = await acquire_page(ctx, spec, cell_id=cell, phase="primary", planned_id=None)
    assert isinstance(r, Served) and not r.replayed  # settled by the second transaction
    assert await attempts(engine, spec, owner) == [
        (1, "normal", "succeeded", None, None, 200, "billed", None, "probe")
    ]
    assert len(served_requests(fixture_log)) == 1
    assert await scalar(engine, "SELECT count(*) FROM quota_ledger WHERE kind = 'request'") == 1

    # at most three transactions: the third failure is raised, the row stays dispatched (then ambiguous)
    second = replace(spec, page_no=2)
    injected[:] = ["40001", "40P01", "40001"]
    with pytest.raises(sa.exc.DBAPIError):
        await acquire_page(ctx, second, cell_id=cell, phase="primary", planned_id=None)
    assert injected == [] and [a[2] for a in await attempts(engine, second, owner)] == ["dispatched"]

    # anything else, a connection failure first of all, is never retried (its commit outcome is unknown)
    third = replace(spec, page_no=3)
    injected[:] = ["08006", "40P01"]
    with pytest.raises(sa.exc.DBAPIError):
        await acquire_page(ctx, third, cell_id=cell, phase="primary", planned_id=None)
    assert injected == ["40P01"] and [a[2] for a in await attempts(engine, third, owner)] == ["dispatched"]
    assert len(served_requests(fixture_log)) == 3  # one send each


async def test_the_new_id_count_is_part_of_the_settlement_so_a_failed_count_keeps_the_response(
    engine, current_epoch, jsearch_fixture_adapter, fixture_log
) -> None:  # final review F3, Minor 1
    """The known-postings count (`new_native_ids`) is read in the settlement transaction: a serialization
    failure there is retried with the response in hand, never a paid response lost to an ambiguous entry."""
    owner, cell = await queued_run(engine), await jsearch_cell(engine)
    spec = await page1_spec(engine, cell)
    calls: list[int] = []

    async def known(conn: AsyncConnection, ids: Sequence[str | None]) -> int:
        calls.append(len(calls))
        if len(calls) == 1:  # the first count fails as the server's serialization failure
            async with engine.connect() as c:
                await c.execute(sa.text(RAISE.format("40001")))
        return 4

    r = await acquire_page(
        jsearch_ctx(engine, owner, jsearch_fixture_adapter),
        spec,
        cell_id=cell,
        phase="primary",
        planned_id=None,
        known_native_ids=known,
    )
    assert isinstance(r, Served) and not r.replayed and r.new_native_ids == 6 and calls == [0, 1]
    assert await attempts(engine, spec, owner) == [
        (1, "normal", "succeeded", None, None, 200, "billed", None, "probe")
    ]
    assert len(served_requests(fixture_log)) == 1


async def test_a_receipt_moved_under_the_resolution_is_resolved_again(
    engine, current_epoch, queue_app, arbeitnow_adapter, respx_mock, monkeypatch
) -> None:  # fix round 1, M5
    owner = await queued_run(engine)
    cell = await source_cell(engine, "arbeitnow", query=None, location_key=None)
    async with engine.connect() as c:
        spec = arbeitnow_adapter.plan(arbeitnow_plan(cell, await instance_id(c, "arbeitnow")))
    status, headers, body = load_fixture("arbeitnow", "page1")
    feed = respx_mock.get(FEED_URL).mock(return_value=httpx.Response(status, headers=headers, content=body))
    await stop_at(
        "collect.after_dispatch_marker",
        monkeypatch,
        acquire_ctx(engine, owner, arbeitnow_adapter, billing="free", provider=None),
        spec,
    )
    restarted = await supersede(engine, owner)
    real = fetch_attempts  # what `acquire` calls
    moved: list[int] = []

    async def racing(conn, key):
        history = await real(conn, key)
        if not moved:  # another actor closes the dispatched row right after this read
            moved.append(history[-1].id)
            async with engine.begin() as other:
                await transition(
                    other,
                    "fetch_requests",
                    moved[0],
                    ["dispatched"],
                    "ambiguous",
                    set_={"error_code": "unsettled"},
                )
        return history

    monkeypatch.setattr(acquire, "fetch_attempts", racing)
    r = await acquire_page(
        acquire_ctx(engine, restarted, arbeitnow_adapter, billing="free", provider=None),
        spec,
        cell_id=cell,
        phase="primary",
        planned_id=None,
    )
    assert isinstance(r, Served) and not r.replayed and feed.call_count == 1  # not FencedOut: resolved again
    assert await attempts(engine, spec, owner) == [
        (1, "free", "ambiguous", None, "unsettled", None, None, None, None),
        (2, "free", "succeeded", None, None, 200, None, None, None),
    ]
