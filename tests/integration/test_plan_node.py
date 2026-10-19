import asyncio
import time
from dataclasses import replace
from datetime import timedelta

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.session import make_null_engine
from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.errors import FencedOut, InvariantViolation
from jobhunter.graphs.checkpointer import open_saver
from jobhunter.graphs.collect import graph as collect_graph
from jobhunter.graphs.collect.deps import CollectDeps, FetchContext
from jobhunter.graphs.collect.graph import build_collect_graph, is_retryable_db_error
from jobhunter.graphs.collect.nodes.plan_fallback import make_plan_fallback
from jobhunter.graphs.collect.state import CollectUpdate, FetchCellInput, initial_state
from jobhunter.quota import ledger
from jobhunter.quota.fallback import cell_outcome
from jobhunter.quota.planner import PlanReport, ensure_cells, plan_fallback, plan_run
from jobhunter.run_settings import load_run_settings
from jobhunter.sources.base import RawPage, RequestSpec, SourceAdapter, SourceError, request_key
from jobhunter.sources.breaker import HALF_OPEN_AFTER, record_failure
from jobhunter.sources.coverage import JSearchContinuation
from jobhunter.sources.instances import InstanceRow, list_instances
from jobhunter.sources.jsearch import CURSOR_REUSE_TTL
from jobhunter.sources.ratelimit import SOURCE_LIMITS, RateLimiter
from jobhunter.sources.receipts import insert_fetch_request, store_response
from jobhunter.sources.serpapi import SerpApiAdapter
from jobhunter.worker import runs
from jobhunter.worker.runs import finalize_run

from tests.support.collect import TEST_QUERY, instance_id, queued_run, request_sent, seed_inputs
from tests.support.quota import SETTINGS, SIX_H, account, insert_window, jsearch_window

pytestmark = pytest.mark.integration
ROWS = sa.text(
    "SELECT status, skip_reason FROM fetch_requests WHERE run_id = :r AND source_instance_id = :i ORDER BY id"
)
REQUESTS = sa.text(
    "SELECT f.id, s.source_id, f.phase, f.cell_id, f.status, f.skip_reason, f.request_class, f.page_no, "
    "f.attempt_seq, f.request_key, f.spec FROM fetch_requests f JOIN source_instances s "
    "ON s.id = f.source_instance_id WHERE f.run_id = :r ORDER BY f.id"
)
CELL_SOURCES = sa.text(
    "SELECT s.source_id FROM coverage_cells c JOIN source_instances s ON s.id = c.source_instance_id "
    "ORDER BY c.id"
)
ACTIVE_VERSION = sa.text("SELECT settings_version_id FROM active_config WHERE id = 1")
GAPS = sa.text(
    "SELECT kind, gap_status, reason, lower(span), upper(span), run_id, closed_at IS NOT NULL AS closed "
    "FROM coverage_intervals WHERE cell_id = :c ORDER BY id"
)
OPEN_GAP = sa.text(
    "INSERT INTO coverage_intervals (cell_id, kind, span, gap_status, reason) "
    "VALUES (:c, 'gap', tstzrange(:lower, :upper, '[)'), 'open', 'page_cap')"
)
CELL_STATE = q(
    "UPDATE coverage_cells SET continuation = :doc, last_covered_at = :covered WHERE id = :c", jsonb=["doc"]
)
COVERED = sa.text("UPDATE coverage_cells SET last_covered_at = :covered WHERE id = :c")
QUERY_OF = sa.text("SELECT q.text FROM coverage_cells c JOIN queries q ON q.id = c.query_id WHERE c.id = :c")
H = timedelta(hours=1)


async def plan_once(engine, owner, now, *, include_paid: bool = False) -> PlanReport:
    """One plan_run of the owner's run, as the plan node calls it (the account read is not made here)."""
    async with engine.begin() as c:
        version = (
            await c.execute(
                sa.text("SELECT settings_version_id FROM runs WHERE id = :r"), {"r": owner.run_id}
            )
        ).scalar_one()
        return await plan_run(
            c,
            owner=owner,
            include_paid=include_paid,
            settings=await load_run_settings(c, version),
            instances=await list_instances(c),
            now=now,
            account=None,
        )


async def requests(engine, run_id: int):
    async with engine.connect() as c:
        return (await c.execute(REQUESTS, {"r": run_id})).all()


async def gaps(engine, cell_id: int):
    async with engine.connect() as c:
        return [tuple(r) for r in (await c.execute(GAPS, {"c": cell_id})).all()]


async def finish(engine, owner) -> None:
    async with engine.begin() as c:
        await finalize_run(c, owner, status="succeeded", reason=None, report={})


async def test_plan_creates_cells_and_specs_for_every_enabled_source(
    engine, current_epoch, fake_http
) -> None:
    await seed_inputs(engine, queries=(TEST_QUERY,), companies=(("greenhouse", "getyourguide"),))
    owner, now = await queued_run(engine), utcnow()
    report = await plan_once(engine, owner, now, include_paid=True)
    async with engine.connect() as c:
        assert sorted((await c.execute(CELL_SOURCES)).scalars()) == [
            "adzuna",
            "arbeitnow",
            "greenhouse",
            "jsearch",
            "serpapi",
        ]
    rows = await requests(engine, owner.run_id)
    by_source = {r.source_id: r for r in rows}
    assert len(rows) == 4 and sorted(by_source) == [
        "adzuna",
        "arbeitnow",
        "greenhouse",
        "jsearch",
    ]  # serpapi 0
    assert {(r.status, r.skip_reason, r.phase, r.page_no, r.attempt_seq) for r in rows} == {
        ("planned", None, "primary", 1, 1)
    }
    jsearch = RequestSpec.from_json(by_source["jsearch"].spec)
    assert by_source["jsearch"].request_class == jsearch.request_class == "probe"  # the window is unvalidated
    assert jsearch.params["date_posted"] == "month" and jsearch.window == (now - 720 * H, now)
    assert jsearch.params["query"] == "python developer remote Germany" and jsearch.cursor_in is None
    adzuna = RequestSpec.from_json(by_source["adzuna"].spec)
    assert (by_source["adzuna"].request_class, adzuna.params["max_days_old"]) == ("normal", "30")
    assert (
        adzuna.window == (now - timedelta(days=30), now)
        and adzuna.params["what"] == "python developer remote"
    )
    assert by_source["arbeitnow"].request_class == by_source["greenhouse"].request_class == "free"
    assert all(bytes(r.request_key) == request_key(RequestSpec.from_json(r.spec), owner.run_id) for r in rows)
    assert report == PlanReport([r.id for r in rows], {}, 4, 0)
    assert await gaps(engine, by_source["jsearch"].cell_id) == []  # a never-covered cell abandons nothing
    await finish(engine, owner)
    free = await queued_run(engine, include_paid=False)
    await plan_once(engine, free, utcnow())
    rows = await requests(engine, free.run_id)
    # the default instances' ids follow no fixed order across database resets: compare by source
    assert sorted((r.source_id, r.status) for r in rows) == [
        ("adzuna", "planned"),
        ("arbeitnow", "planned"),
        ("greenhouse", "planned"),
    ]  # no paid source in a free run; Adzuna (quota, not paid) still planned


async def test_plan_is_a_receipt_and_replans_shutdown_skips(engine, current_epoch, fake_http) -> None:
    await seed_inputs(engine, queries=(TEST_QUERY,), companies=(("greenhouse", "getyourguide"),))
    owner, now = await queued_run(engine, include_paid=False), utcnow()
    first = await plan_once(engine, owner, now)
    before = await requests(engine, owner.run_id)
    assert first == PlanReport([r.id for r in before], {}, 3, 0)
    assert await plan_once(engine, owner, now + 5 * timedelta(minutes=1)) == first
    assert await requests(engine, owner.run_id) == before  # the receipt: nothing inserted, nothing changed
    by_source = {r.source_id: r for r in before}
    async with engine.begin() as c:
        for source, reason in (("arbeitnow", "shutdown"), ("adzuna", "paced")):
            await transition(
                c,
                "fetch_requests",
                by_source[source].id,
                ["planned"],
                "skipped",
                set_={"skip_reason": reason},
            )
    replanned = await plan_once(engine, owner, now + 10 * timedelta(minutes=1))
    after = await requests(engine, owner.run_id)
    (new,) = after[len(before) :]
    assert (new.source_id, new.attempt_seq, new.status, new.skip_reason, new.spec) == (
        "arbeitnow",
        2,
        "planned",
        None,
        by_source["arbeitnow"].spec,
    )
    assert bytes(new.request_key) == bytes(by_source["arbeitnow"].request_key)  # the same logical request
    # the paced Adzuna spec is a denial, not re-planned and not fetched; the re-planned key takes its new row
    assert replanned == PlanReport([new.id, by_source["greenhouse"].id], {"paced": 1}, 3, 1)
    assert await plan_once(engine, owner, now + 15 * timedelta(minutes=1)) == PlanReport(
        [new.id, by_source["greenhouse"].id], {"paced": 1}, 3, 0
    )


async def test_plan_reuses_a_fresh_jsearch_cursor(engine, current_epoch, fake_http) -> None:
    await seed_inputs(engine, queries=(TEST_QUERY,), enabled=("jsearch",))
    now = utcnow()
    covered, window = now - 49 * H, (now - 73 * H, now - H)  # the 3days window of the traversal it continues
    async with engine.begin() as c:
        settings = await load_run_settings(c, (await c.execute(ACTIVE_VERSION)).scalar_one())
        (cell,) = await ensure_cells(c, settings, await list_instances(c))
        await c.execute(OPEN_GAP, {"c": cell.id, "lower": covered, "upper": window[1]})
        fresh = JSearchContinuation(cursor="cursor-2", obtained_at=now - H, window=window, page_no=2)
        await c.execute(CELL_STATE, {"doc": fresh.dump(), "covered": covered, "c": cell.id})
    first = await queued_run(engine)
    await plan_once(engine, first, now, include_paid=True)
    (row,) = await requests(engine, first.run_id)
    spec = RequestSpec.from_json(row.spec)
    assert (row.status, row.page_no, spec.cursor_in, spec.page_no, spec.window) == (
        "planned",
        2,
        "cursor-2",
        2,
        window,
    )
    assert spec.params["date_posted"] == "3days"  # the query of the traversal the cursor continues
    await finish(engine, first)
    stale = JSearchContinuation(
        cursor="cursor-2", obtained_at=now - CURSOR_REUSE_TTL - timedelta(minutes=1), window=window, page_no=2
    )
    async with engine.begin() as c:
        await c.execute(CELL_STATE, {"doc": stale.dump(), "covered": covered, "c": cell.id})
    second = await queued_run(engine)
    await plan_once(engine, second, now, include_paid=True)
    (row,) = await requests(engine, second.run_id)
    spec = RequestSpec.from_json(row.spec)
    assert (row.page_no, spec.cursor_in, spec.window, spec.params["date_posted"]) == (
        1,
        None,
        (now - 168 * H, now),
        "week",
    )  # page 1 with a fresh window: needs 49 h + 24 h
    assert [(g[0], g[1], g[2]) for g in await gaps(engine, cell.id)] == [
        ("gap", "open", "page_cap")
    ]  # kept (§5.3.1)


async def test_plan_abandons_the_part_beyond_the_provider_window(engine, current_epoch) -> None:
    await seed_inputs(engine, queries=(TEST_QUERY,), enabled=("jsearch",))
    now = utcnow()
    async with engine.begin() as c:
        settings = await load_run_settings(c, (await c.execute(ACTIVE_VERSION)).scalar_one())
        (cell,) = await ensure_cells(c, settings, await list_instances(c))
        await c.execute(COVERED, {"covered": now - 800 * H, "c": cell.id})
    owner = await queued_run(engine)
    await plan_once(engine, owner, now, include_paid=True)
    (row,) = await requests(engine, owner.run_id)
    assert RequestSpec.from_json(row.spec).window == (now - 720 * H, now)
    assert await gaps(engine, cell.id) == [
        ("gap", "abandoned", "beyond_provider_window", now - 800 * H, now - 720 * H, owner.run_id, True)
    ]


async def test_plan_skips_closed_instances_and_probes_half_open(engine, current_epoch) -> None:
    """Review Focus 5, the planning half."""
    await seed_inputs(engine, companies=(("greenhouse", "getyourguide"),), enabled=("ats",))
    now = utcnow()
    async with engine.begin() as c:
        gh = await instance_id(c, "greenhouse", "getyourguide")
        assert (
            await record_failure(c, gh, "not_found", billing="free", now=now) == "not_found"
        )  # the board answered 404 (Task 11)
    first = await queued_run(engine, include_paid=False)
    await plan_once(engine, first, now + timedelta(hours=1))
    async with engine.begin() as c:
        assert [tuple(r) for r in (await c.execute(ROWS, {"r": first.run_id, "i": gh})).all()] == [
            ("skipped", "breaker_open")
        ]  # no request before half-open
        await finalize_run(c, first, status="succeeded", reason=None, report={})
    second = await queued_run(engine, include_paid=False)
    await plan_once(engine, second, now + HALF_OPEN_AFTER + timedelta(minutes=1))
    async with engine.connect() as c:
        assert [tuple(r) for r in (await c.execute(ROWS, {"r": second.run_id, "i": gh})).all()] == [
            ("planned", None)
        ]  # exactly one probe spec


async def test_a_disabled_source_gives_no_rows(engine, current_epoch) -> None:
    await seed_inputs(engine, queries=(TEST_QUERY,), enabled=("jsearch", "adzuna"))
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE source_instances SET health = 'disabled' WHERE source_id = 'adzuna'"))
    owner = await queued_run(engine)
    report = await plan_once(engine, owner, utcnow(), include_paid=True)
    (row,) = await requests(engine, owner.run_id)  # no Adzuna row; SerpApi, Arbeitnow and ATS are off
    assert row.source_id == "jsearch" and report == PlanReport([row.id], {}, 1, 0)


async def _sent(engine, owner, fetch_request_id: int, *, now) -> None:
    """Reserve (a retry-class entry: no bucket), mark it dispatched and move the request `dispatched`."""
    async with engine.begin() as c:
        spec = RequestSpec.from_json(
            (
                await c.execute(
                    sa.text("SELECT spec FROM fetch_requests WHERE id = :i"), {"i": fetch_request_id}
                )
            ).scalar_one()
        )
        reserved = await ledger.reserve(
            c,
            "jsearch",
            owner=owner,
            request_key=request_key(spec, owner.run_id),
            attempt_seq=1,
            request_class="retry",
            fetch_request_id=fetch_request_id,
            settings=SETTINGS,
            interval_s=SIX_H,
            now=now,
        )
        assert isinstance(reserved, ledger.Reservation)
        await request_sent(c, fetch_request_id, reserved.entry_id)
        await ledger.mark_dispatched(c, reserved.entry_id, owner)


async def test_plan_fallback_follows_the_eligibility_table(engine, current_epoch, fake_http) -> None:
    outcomes = {
        "python developer": "exhausted",
        "data engineer": "contract_broken",
        "platform engineer": "paced",
        "site reliability engineer": "ambiguous",
    }
    await seed_inputs(engine, queries=tuple(outcomes), enabled=("jsearch", "serpapi"))
    owner, now = await queued_run(engine), utcnow()
    await plan_once(engine, owner, now, include_paid=True)
    await insert_window(
        engine, jsearch_window(now - timedelta(days=1), limit=200, reserve=20, tokens=2, interval_s=SIX_H)
    )
    rows = await requests(engine, owner.run_id)
    async with engine.connect() as c:
        text = {r.id: (await c.execute(QUERY_OF, {"c": r.cell_id})).scalar_one() for r in rows}
    request = {text[r.id]: r for r in rows}
    assert len(rows) == 4 and {r.status for r in rows} == {"planned"}
    for query in ("data engineer", "site reliability engineer"):
        await _sent(engine, owner, request[query].id, now=now)
    async with engine.begin() as c:
        await transition(
            c,
            "fetch_requests",
            request["python developer"].id,
            ["planned"],
            "skipped",
            set_={"skip_reason": "exhausted"},
        )
        await transition(
            c,
            "fetch_requests",
            request["platform engineer"].id,
            ["planned"],
            "skipped",
            set_={"skip_reason": "paced"},
        )
        await transition(
            c,
            "fetch_requests",
            request["data engineer"].id,
            ["dispatched"],
            "failed",
            set_={"error_code": "contract"},
        )
        await transition(
            c, "fetch_requests", request["site reliability engineer"].id, ["dispatched"], "ambiguous"
        )
        js = await instance_id(c, "jsearch")
        assert await record_failure(c, js, "contract", billing="paid", now=now) == "contract_broken"
    async with engine.begin() as c:
        assert {q: await cell_outcome(c, owner.run_id, request[q].cell_id) for q in outcomes} == outcomes
        version = (await c.execute(ACTIVE_VERSION)).scalar_one()
        fallback = await plan_fallback(
            c,
            owner=owner,
            settings=await load_run_settings(c, version),
            instances=await list_instances(c),
            now=now,
            account=account(now, now + timedelta(days=20)),
        )
    planned = [r for r in await requests(engine, owner.run_id) if r.phase == "fallback"]
    async with engine.connect() as c:
        served = sorted([(await c.execute(QUERY_OF, {"c": r.cell_id})).scalar_one() for r in planned])
        alert = (
            await c.execute(
                sa.text(
                    "SELECT severity FROM alerts WHERE dedup_key = 'quota.fallback_active:provider:serpapi' "
                    "AND resolved_at IS NULL"
                )
            )
        ).all()
    assert served == [
        "data engineer",
        "python developer",
    ]  # exhausted and contract_broken; paced and ambiguous none
    assert {(r.source_id, r.status, r.page_no) for r in planned} == {("serpapi", "planned", 1)}
    assert fallback == [r.id for r in planned] and [tuple(a) for a in alert] == [("info",)]
    async with engine.begin() as c:  # the fallback is a receipt too
        again = await plan_fallback(
            c,
            owner=owner,
            settings=await load_run_settings(c, version),
            instances=await list_instances(c),
            now=now,
            account=account(now, now + timedelta(days=20)),
        )
    assert again == fallback and len(await requests(engine, owner.run_id)) == 6


async def test_cell_outcome_reads_the_receipts_the_window_and_the_health(engine, current_epoch) -> None:
    """Ruling T13-a: a failure reads the instance's health first, then a 429 on an exhausted window."""
    queries = ("awaiting", "rate limited", "server error", "served page one", "never fetched")
    await seed_inputs(engine, queries=queries, enabled=("jsearch",))
    owner, now = await queued_run(engine), utcnow()
    await plan_once(engine, owner, now, include_paid=True)
    await insert_window(
        engine, jsearch_window(now - timedelta(days=1), limit=200, reserve=20, tokens=2, interval_s=SIX_H)
    )
    rows = await requests(engine, owner.run_id)
    async with engine.connect() as c:
        request = {(await c.execute(QUERY_OF, {"c": r.cell_id})).scalar_one(): r for r in rows}
    for query in ("rate limited", "server error", "served page one"):
        await _sent(engine, owner, request[query].id, now=now)
    served = request["served page one"]
    async with engine.begin() as c:
        await transition(
            c,
            "fetch_requests",
            request["awaiting"].id,
            ["planned"],
            "skipped",
            set_={"skip_reason": "awaiting_window_validation"},
        )
        await transition(
            c,
            "fetch_requests",
            request["rate limited"].id,
            ["dispatched"],
            "failed",
            set_={"error_code": "rate_limited", "http_status": 429},
        )
        await transition(
            c,
            "fetch_requests",
            request["server error"].id,
            ["dispatched"],
            "failed",
            set_={"error_code": "transient", "http_status": 503},
        )
        page1 = RequestSpec.from_json(served.spec)
        await store_response(
            c,
            served.id,
            RawPage(page1, 200, {}, b"{}", now),
            source_instance_id=page1.source_instance_id,
            ledger_entry_id=None,
            succeeded=True,
            items=0,
        )
        await transition(c, "fetch_requests", served.id, ["dispatched"], "succeeded")
        page2 = replace(page1, page_no=2, cursor_in="next", request_class="normal")
        await insert_fetch_request(
            c,
            owner=owner,
            cell_id=page2.cell_id,
            source_instance_id=page2.source_instance_id,
            phase="primary",
            spec=page2,
            request_key=request_key(page2, owner.run_id),
            attempt_seq=1,
            status="skipped",
            skip_reason="exhausted",
        )

    async def outcomes() -> dict[str, str]:
        async with engine.connect() as c:
            return {q: await cell_outcome(c, owner.run_id, request[q].cell_id) for q in queries[:4]}

    assert await outcomes() == {
        "awaiting": "awaiting_probe_in_flight",
        "rate limited": "transient",  # a 429 while the window is not exhausted
        "server error": "transient",
        "served page one": "served",  # its page 2 was denied `exhausted`: the cell was served all the same
    }
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE quota_windows SET probe_failed = true WHERE provider = 'jsearch'"))
        await c.execute(
            sa.text("UPDATE quota_windows SET state = 'exhausted_until_reset' WHERE provider = 'jsearch'")
        )
    assert await outcomes() == {
        "awaiting": "awaiting_after_failed_probe",
        "rate limited": "exhausted",  # a 429 with nothing left: provider-exhausted (§6.6)
        "server error": "transient",
        "served page one": "served",
    }
    async with engine.begin() as c:
        js = await instance_id(c, "jsearch")
        for _ in range(2):
            health = await record_failure(c, js, "transient", billing="paid", now=now)
        assert health == "breaker_open"
    assert await outcomes() == {
        "awaiting": "awaiting_after_failed_probe",
        "rate limited": "breaker_open",  # the health first: a breaker that opened in this run
        "server error": "breaker_open",
        "served page one": "served",
    }
    async with engine.connect() as c:
        with pytest.raises(InvariantViolation):  # still planned: plan_fallback runs after every spec ended
            await cell_outcome(c, owner.run_id, request["never fetched"].cell_id)
        with pytest.raises(LookupError):
            await cell_outcome(c, owner.run_id + 1, request["never fetched"].cell_id)


async def test_serialization_failures_deadlocks_and_lost_connections_are_retryable(migrated) -> None:
    engine = make_null_engine(migrated.dsn("app", driver="sqlalchemy"))
    raised: dict[str, sa.exc.DBAPIError] = {}
    async with engine.connect() as c:
        for code in ("40001", "40P01", "23505", "57014"):
            with pytest.raises(sa.exc.DBAPIError) as info:
                async with c.begin_nested():
                    await c.execute(
                        sa.text(f"DO $$ BEGIN RAISE EXCEPTION 'injected' USING ERRCODE = '{code}'; END $$")
                    )
            raised[code] = info.value
        await c.rollback()
    await engine.dispose()
    assert {code: is_retryable_db_error(exc) for code, exc in raised.items()} == {
        "40001": True,
        "40P01": True,
        "23505": False,
        "57014": False,
    }
    raised["23505"].connection_invalidated = True  # a lost connection: whatever the code, run it again
    assert is_retryable_db_error(raised["23505"])


PHASES = ("primary", "fallback")
GRAPH_NODES = [
    "plan",
    "fetch_primary",
    "fetch_cell",
    "fetch_cell",
    "fetch_cell",
    "plan_fallback",
    "fetch_fallback",
    "fetch_cell",
    "parse_normalize",
    "resolve_identity",
    "embed",
    "hard_filters",
    "enqueue_targets",
    "finalize",
]


BY_QUERY = sa.text(
    "SELECT s.source_id, q.text, f.status, f.skip_reason, f.request_class FROM fetch_requests f "
    "JOIN source_instances s ON s.id = f.source_instance_id JOIN coverage_cells c ON c.id = f.cell_id "
    "JOIN queries q ON q.id = c.query_id WHERE f.run_id = :r ORDER BY f.id"
)


async def by_query(engine, run_id: int, source_id: str) -> list[tuple[str, str, str | None, str]]:
    """The run's requests of `source_id` in insertion order: (query, status, skip reason, class)."""
    async with engine.connect() as c:
        rows = (await c.execute(BY_QUERY, {"r": run_id})).all()
    return [(r.text, r.status, r.skip_reason, r.request_class) for r in rows if r.source_id == source_id]


async def test_a_denied_projection_is_a_skip_with_its_code(engine, current_epoch) -> None:
    """Minor 2 (a): with one token in the bucket the second spec is `paced`; a used-up window `exhausted`."""
    await seed_inputs(engine, queries=("first query", "second query"), enabled=("jsearch",))
    now = utcnow()
    await insert_window(engine, jsearch_window(now, limit=200, reserve=20, tokens=1, interval_s=SIX_H))
    owner = await queued_run(engine)
    report = await plan_once(engine, owner, now, include_paid=True)
    # both never attempted: the same priority, so the order is the cells' (the queries')
    assert await by_query(engine, owner.run_id, "jsearch") == [
        ("first query", "planned", None, "normal"),
        ("second query", "skipped", "paced", "normal"),
    ]
    assert report.skipped == {"paced": 1} and len(report.primary_fetch_request_ids) == 1
    await finish(engine, owner)
    async with engine.begin() as c:  # a limit of 0: used + 1 > limit
        await c.execute(
            sa.text("UPDATE quota_windows SET limit_units = 0, reserve_units = 0 WHERE provider = 'jsearch'")
        )
    second = await queued_run(engine)
    assert (await plan_once(engine, second, now, include_paid=True)).skipped == {"exhausted": 2}
    assert {status for _, status, _, _ in await by_query(engine, second.run_id, "jsearch")} == {"skipped"}


async def test_a_half_open_paid_instance_gets_exactly_one_spec(engine, current_epoch) -> None:
    """Minor 2 (b): before half-open every cell is `skipped(breaker_open)`; then only the first is planned."""
    queries = ("first query", "second query", "third query")
    await seed_inputs(engine, queries=queries, enabled=("jsearch",))
    now = utcnow()
    async with engine.begin() as c:
        assert (
            await record_failure(c, await instance_id(c, "jsearch"), "auth", billing="paid", now=now)
            == "auth_failed"
        )
    blocked = await queued_run(engine)
    await plan_once(engine, blocked, now + H, include_paid=True)
    assert [
        (text, status, skip) for text, status, skip, _ in await by_query(engine, blocked.run_id, "jsearch")
    ] == [(text, "skipped", "breaker_open") for text in queries]
    await finish(engine, blocked)
    probe = await queued_run(engine)
    report = await plan_once(engine, probe, now + HALF_OPEN_AFTER + timedelta(minutes=1), include_paid=True)
    assert [
        (text, status, skip) for text, status, skip, _ in await by_query(engine, probe.run_id, "jsearch")
    ] == [
        ("first query", "planned", None),
        ("second query", "skipped", "breaker_open"),
        ("third query", "skipped", "breaker_open"),
    ]
    assert report.skipped == {"breaker_open": 2} and len(report.primary_fetch_request_ids) == 1


LEDGER_ENTRY = sa.text(
    "INSERT INTO quota_ledger (provider, window_start, kind, request_class, request_key, attempt_seq, units, "
    "state, created_at, dispatched_at, settled_at) VALUES ('jsearch', :ws, 'request', 'normal', :key, 1, 1, "
    "'billed', :at, :at, :at)"
)
ATTEMPTED = sa.text("UPDATE coverage_cells SET last_attempted_at = :at WHERE id = :c")


async def test_plan_run_orders_paid_cells_by_priority_and_adzuna_stalest_first(engine, current_epoch) -> None:
    """Minor 2 (c): the planned rows' insertion order is `order_paid`'s (with the exploration rule) and the
    Adzuna rotation's."""
    await seed_inputs(engine, queries=("qa", "qb", "qc", "qd"), enabled=("jsearch", "adzuna"))
    now = utcnow()
    window = jsearch_window(now - timedelta(days=1), limit=200, reserve=20, tokens=2, interval_s=SIX_H)
    await insert_window(engine, window)
    jsearch_seen = {"qa": now - 2 * H, "qc": now - 10 * H, "qd": now - H}  # qb never: priority qb, qc, qa, qd
    adzuna_seen = {"qb": now - 10 * H, "qc": now - 2 * H, "qd": now - 30 * H}  # qa never: qa, qd, qb, qc
    async with engine.begin() as c:
        settings = await load_run_settings(c, (await c.execute(ACTIVE_VERSION)).scalar_one())
        cells = {
            (cell.source_id, cell.query_text): cell
            for cell in await ensure_cells(c, settings, await list_instances(c))
        }
        for source, seen in (("jsearch", jsearch_seen), ("adzuna", adzuna_seen)):
            for text, at in seen.items():
                await c.execute(ATTEMPTED, {"at": at, "c": cells[(source, text)].id})
        await c.execute(sa.text("UPDATE queries SET exploration = true WHERE text = 'qd'"))
        for n in range(10):  # ten normal JSearch units and no exploration request yet
            await c.execute(LEDGER_ENTRY, {"ws": window.window_start, "key": bytes([n]) * 32, "at": now - H})
    first = await queued_run(engine)
    await plan_once(engine, first, now, include_paid=True)
    assert [text for text, *_ in await by_query(engine, first.run_id, "jsearch")] == ["qd", "qb", "qc", "qa"]
    assert [text for text, *_ in await by_query(engine, first.run_id, "adzuna")] == ["qa", "qd", "qb", "qc"]
    await finish(engine, first)
    async with engine.begin() as c:  # an exploration request after those units: back to priority order
        await c.execute(
            sa.text("UPDATE coverage_cells SET last_exploration_at = :at WHERE id = :c"),
            {"at": now - timedelta(minutes=30), "c": cells[("jsearch", "qd")].id},
        )
    second = await queued_run(engine)
    await plan_once(engine, second, now, include_paid=True)
    assert [text for text, *_ in await by_query(engine, second.run_id, "jsearch")] == ["qb", "qc", "qa", "qd"]


QUEUED_RUN = sa.text(
    "INSERT INTO runs (trigger, scheduled_slot, status, include_paid, settings_version_id, scope_version_id) "
    "SELECT 'manual', NULL, 'queued', :paid, settings_version_id, scope_version_id FROM active_config "
    "RETURNING id"
)
SOURCE_OF = sa.text(
    "SELECT s.source_id FROM fetch_requests f JOIN source_instances s ON s.id = f.source_instance_id "
    "WHERE f.id = :i"
)


async def queued_run_id(engine, *, include_paid: bool) -> int:
    """A queued manual run over the active configuration, not admitted (`execute_run` starts it)."""
    async with engine.begin() as c:
        return int((await c.execute(QUEUED_RUN, {"paid": include_paid})).scalar_one())


def no_adapter(instance: InstanceRow) -> SourceAdapter:
    raise AssertionError(f"no adapter is needed here, asked for {instance.source_id}")


def collect_deps(engine, adapters=no_adapter) -> CollectDeps:
    return CollectDeps(
        engine=engine,
        adapters=adapters,
        embedder=None,
        clock=utcnow,
        sleep=asyncio.sleep,
        admission_stopped=lambda: False,
        limiter=RateLimiter(SOURCE_LIMITS.values(), clock=time.monotonic, sleep=asyncio.sleep),
        fetch_ctx=FetchContext(),
    )


async def run_graph(engine, migrated, deps: CollectDeps, rid: int):
    """`execute_run` of attempt 1 on the collection graph; the admission and the attempt's final state."""
    async with open_saver(migrated.dsn("app")) as saver:
        graph = build_collect_graph(saver, deps)
        admission = await runs.execute_run(engine, graph, rid, 1, epoch=1)
        state = (await graph.aget_state({"configurable": {"thread_id": f"run-{rid}-a1"}})).values
    return admission, state


async def run_row(engine, rid: int):
    async with engine.connect() as c:
        return tuple(
            (await c.execute(sa.text("SELECT status, report FROM runs WHERE id = :r"), {"r": rid})).one()
        )


async def test_graph_runs_plan_fetch_and_pass_through_nodes_on_the_real_worker_protocol(
    engine, current_epoch, migrated, adapters, respx_mock, serpapi_fixture_adapter: SerpApiAdapter
) -> None:
    await seed_inputs(engine, queries=(TEST_QUERY,), companies=(("greenhouse", "getyourguide"),))
    async with (
        engine.begin() as c
    ):  # JSearch is closed: its cell's skipped(breaker_open) is fallback-eligible
        jsearch = await instance_id(c, "jsearch")
        assert await record_failure(c, jsearch, "auth", billing="paid", now=utcnow()) == "auth_failed"
    rid = await queued_run_id(engine, include_paid=True)

    def fixture_adapters(instance: InstanceRow) -> SourceAdapter:
        return serpapi_fixture_adapter if instance.source_id == "serpapi" else adapters(instance)

    admission, state = await run_graph(engine, migrated, collect_deps(engine, fixture_adapters), rid)
    assert admission == "started"
    rows = await requests(engine, rid)
    # the pass-through finalize leaves the end to execute_run
    assert await run_row(engine, rid) == ("succeeded", {"noop": True})
    assert sorted((r.phase, r.source_id, r.page_no, r.status, r.skip_reason) for r in rows) == [
        ("fallback", "serpapi", 1, "succeeded", None),
        ("fallback", "serpapi", 2, "succeeded", None),  # page 1's ids are new: page 2 (§5.3.1)
        ("primary", "adzuna", 1, "succeeded", None),
        ("primary", "adzuna", 2, "succeeded", None),  # the fixture routes' empty page 2: the end
        ("primary", "arbeitnow", 1, "succeeded", None),
        ("primary", "arbeitnow", 2, "succeeded", None),
        ("primary", "arbeitnow", 3, "succeeded", None),  # page 3 is the end of the fixture feed
        ("primary", "greenhouse", 1, "succeeded", None),
        ("primary", "jsearch", 1, "skipped", "breaker_open"),
    ]
    planned = {  # the planner's rows: every phase's first pages, but the skipped JSearch spec
        phase: [r.id for r in rows if r.phase == phase and r.page_no == 1 and r.status != "skipped"]
        for phase in PHASES
    }
    assert state["steps"] == GRAPH_NODES  # every node, one fetch_cell per planned spec of each phase
    assert state["primary_fetch_request_ids"] == planned["primary"]
    assert state["fallback_fetch_request_ids"] == planned["fallback"] and state["plan_done"] is True
    fetched = [r for r in rows if r.status == "succeeded"]
    assert sorted(state["outcomes"]) == sorted(f"fr:{r.id}" for r in fetched)  # one outcome per page
    assert {o["status"] for o in state["outcomes"].values()} == {"succeeded"}
    assert state["counters"]["fetch_pages"] == len(fetched) and state["final_status"] is None
    async with engine.connect() as c:
        assert state["settings_version_id"] == (await c.execute(ACTIVE_VERSION)).scalar_one()
    # plan_fallback's read (`plan` reads none, Ruling T13-c), one before each search and one after the last
    assert respx_mock["serpapi_account"].call_count == 1 + 2 + 1


async def test_plan_fallback_reads_its_inputs_from_the_run_row(engine, current_epoch) -> None:
    await seed_inputs(engine, queries=(TEST_QUERY,), enabled=("jsearch", "serpapi"))
    owner = await queued_run(engine, include_paid=False)
    # the state's settings id is still 0: the node reads the run's own version and include_paid
    update = await make_plan_fallback(collect_deps(engine))(initial_state(owner.run_id, owner.attempt_no))
    assert update == {
        "fallback_fetch_request_ids": [],
        "steps": ["plan_fallback"],
    }  # a free run reads no account


def failing_fetch_cell(engine, source_id: str, error: Exception):
    """A `fetch_cell` that raises `error` for the request of `source_id` and passes through the others."""

    async def fetch_cell(state: FetchCellInput) -> CollectUpdate:
        async with engine.connect() as c:
            if (await c.execute(SOURCE_OF, {"i": state["fetch_request_id"]})).scalar_one() == source_id:
                raise error
        return {"steps": ["fetch_cell"]}

    return fetch_cell


async def seed_free_inputs(engine) -> None:
    """Three free-run cells (Adzuna, Arbeitnow, a Greenhouse board): three concurrent `fetch_cell` Sends."""
    await seed_inputs(
        engine,
        queries=(TEST_QUERY,),
        companies=(("greenhouse", "getyourguide"),),
        enabled=("adzuna", "arbeitnow", "ats"),
    )


async def test_a_source_error_in_one_fetch_cell_send_is_its_outcome_and_the_run_goes_on(
    engine, current_epoch, migrated, monkeypatch
) -> None:
    """Minor 4: the wrapper, not LangGraph's `error_handler=`, survives a failure next to other Sends."""
    await seed_free_inputs(engine)
    rid = await queued_run_id(engine, include_paid=False)
    error = SourceError("transient", code="http_503", status=503)
    monkeypatch.setattr(
        collect_graph, "make_fetch_cell", lambda deps: failing_fetch_cell(engine, "arbeitnow", error)
    )
    admission, state = await run_graph(engine, migrated, collect_deps(engine), rid)
    (failed,) = [r.id for r in await requests(engine, rid) if r.source_id == "arbeitnow"]
    assert admission == "started" and await run_row(engine, rid) == ("succeeded", {"noop": True})
    assert state["outcomes"] == {
        f"fr:{failed}": {"work_id": f"fr:{failed}", "status": "failed", "items": 0, "new_postings": 0}
    }
    assert state["counters"] == {"source_errors": 1}
    assert state["steps"].count("fetch_cell") == 3 and state["steps"][-1] == "finalize"


async def test_fenced_out_in_one_fetch_cell_send_ends_the_attempt(
    engine, current_epoch, migrated, monkeypatch
) -> None:
    await seed_free_inputs(engine)
    rid = await queued_run_id(engine, include_paid=False)
    monkeypatch.setattr(
        collect_graph,
        "make_fetch_cell",
        lambda deps: failing_fetch_cell(engine, "arbeitnow", FencedOut("run")),
    )
    admission, state = await run_graph(engine, migrated, collect_deps(engine), rid)
    assert admission == "started" and "finalize" not in state.get("steps", [])
    assert await run_row(engine, rid) == ("running", None)  # lost: no owner write, the next owner ends it
