import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.transitions import transition
from jobhunter.quota.planner import plan_run
from jobhunter.run_settings import load_run_settings
from jobhunter.sources.acquire import Served, acquire_page
from jobhunter.sources.ats.common import SnapshotDraft
from jobhunter.sources.base import CoverageWindow, RequestSpec
from jobhunter.sources.coverage import (
    CellStats,
    CoverageEffect,
    JSearchContinuation,
    abandon_old_gaps,
    finalize_cells,
    record_ats_snapshot,
    record_search_traversal,
)
from jobhunter.sources.instances import list_instances
from jobhunter.sources.jsearch import JSearchAdapter
from jobhunter.sources.receipts import insert_fetch_request
from jobhunter.sources.traverse import End, TraversalResult
from jobhunter.worker.runs import RunOwner, finalize_run
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import (
    TEST_QUERY,
    JobFactory,
    acquire_ctx,
    jsearch_cell,
    page1_spec,
    planned_request,
    queued_run,
    seed_inputs,
    source_cell,
)
from tests.support.sources import load_fixture

pytestmark = pytest.mark.integration
DAY, H = timedelta(days=1), timedelta(hours=1)
DAY0 = datetime(2026, 9, 1, 6, tzinfo=UTC)  # day 0 06:00
INTERVALS = sa.text(
    "SELECT kind, gap_status, reason, lower(span), upper(span), gap_cursor, closed_at IS NOT NULL AS closed "
    "FROM coverage_intervals WHERE cell_id = :c ORDER BY id"
)
CELL = sa.text(
    "SELECT last_covered_at, consecutive_incomplete, yield_ema, zero_yield_streak, last_attempted_at, "
    "last_success_at, continuation, last_exploration_at FROM coverage_cells WHERE id = :c"
)
COVERED = sa.text("UPDATE coverage_cells SET last_covered_at = :at WHERE id = :c")
ALERT = sa.text("SELECT severity FROM alerts WHERE dedup_key = :k AND resolved_at IS NULL")


def traversal(end: End, reason: str | None = None, cursor: str | None = None) -> TraversalResult:
    return TraversalResult(
        outcomes=[],
        end=end,
        reason=reason,
        pages=1,
        items=10,
        cursor_out=cursor,
        snapshot=None,
        firehose=None,
    )


async def intervals(engine: AsyncEngine, cell: int) -> list[tuple[object, ...]]:
    async with engine.connect() as c:
        return [tuple(r) for r in await c.execute(INTERVALS, {"c": cell})]


async def cell_row(engine: AsyncEngine, cell: int) -> sa.Row[Any]:
    async with engine.connect() as c:
        return (await c.execute(CELL, {"c": cell})).one()


async def record(
    engine: AsyncEngine, cell: int, window: CoverageWindow, result, run_id: int
) -> CoverageEffect:
    async with engine.begin() as c:
        return await record_search_traversal(c, cell, window=window, result=result, run_id=run_id)


async def finalize(engine: AsyncEngine, run_id: int, now: datetime) -> dict[int, CellStats]:
    async with engine.begin() as c:
        return await finalize_cells(c, run_id, now=now)


async def finish(engine: AsyncEngine, owner: RunOwner) -> None:
    async with engine.begin() as c:
        await finalize_run(c, owner, status="succeeded", reason=None, report={})


async def a_request(engine: AsyncEngine, owner: RunOwner, cell: int, key: str) -> int:
    """A JSearch request of the run for the cell (planned, never dispatched): the cell is part of the run."""
    return await planned_request(
        engine, owner, provider="jsearch", key=key.encode().ljust(32, b"-"), attempt_seq=1, cell_id=cell
    )


async def test_search_coverage_rules(engine, current_epoch) -> None:
    covered_cell, capped_cell, old_cell = [
        await source_cell(engine, "jsearch", location_key=key) for key in ("remote-de", "berlin", "hamburg")
    ]
    day1, day3 = DAY0 + DAY, DAY0 + 3 * DAY
    window = (DAY0, day3)  # the 3days window of the run at day 3 06:00, as its planned spec holds it
    async with engine.begin() as c:
        for cell in (covered_cell, capped_cell):
            await c.execute(COVERED, {"at": day1, "c": cell})
    owner = await queued_run(engine)
    for cell in (covered_cell, capped_cell, old_cell):
        await a_request(engine, owner, cell, f"cell-{cell}")
    # (a) END: the window is covered
    assert await record(engine, covered_cell, window, traversal("end"), owner.run_id) == CoverageEffect(
        0, 0, 1
    )
    assert await intervals(engine, covered_cell) == [("covered", None, None, DAY0, day3, None, False)]
    # (b) a cursor left at the page cap: the open gap from the old coverage to the window's end, once
    capped = traversal("cursor", "page_cap", cursor="cursor-2")
    assert await record(engine, capped_cell, window, capped, owner.run_id) == CoverageEffect(1, 0, 0)
    assert await record(engine, capped_cell, window, capped, owner.run_id) == CoverageEffect(
        0, 0, 0
    )  # unique span
    assert await intervals(engine, capped_cell) == [
        ("gap", "open", "page_cap", day1, day3, {"cursor": "cursor-2"}, False)
    ]
    # (d) a gap older than the ad age is abandoned, with its alert
    old_window = (DAY0 - 60 * DAY, DAY0 - 40 * DAY)
    failed = traversal("failed", "transient")
    assert await record(engine, old_cell, old_window, failed, owner.run_id) == CoverageEffect(1, 0, 0)
    async with engine.begin() as c:
        assert await abandon_old_gaps(c, now=day3, max_ad_age_days=30) == 1
    assert await intervals(engine, old_cell) == [
        ("gap", "abandoned", "source_failed", *old_window, None, True)
    ]
    async with engine.connect() as c:
        alert = (await c.execute(ALERT, {"k": f"coverage.gap_abandoned:cell:{old_cell}"})).one_or_none()
    assert alert is not None and tuple(alert) == ("warn",)
    stats = await finalize(engine, owner.run_id, day3)
    assert stats[covered_cell] == CellStats(requests=0, new_postings=0, reached_end=False, covered=True)
    assert stats[capped_cell] == CellStats(requests=0, new_postings=0, reached_end=False, covered=False)
    covered_row, capped_row = await cell_row(engine, covered_cell), await cell_row(engine, capped_cell)
    assert (covered_row.last_covered_at, covered_row.consecutive_incomplete) == (day3, 0)  # W.lower <= day 1
    assert (capped_row.last_covered_at, capped_row.consecutive_incomplete) == (day1, 1)  # unchanged, +1
    # (c) a later END whose window covers the gap closes it
    await finish(engine, owner)
    later = await queued_run(engine)
    await a_request(engine, later, capped_cell, "later")
    wider = (DAY0, day3 + DAY)
    assert await record(engine, capped_cell, wider, traversal("end"), later.run_id) == CoverageEffect(0, 1, 1)
    assert [(kind, status, closed) for kind, status, *_, closed in await intervals(engine, capped_cell)] == [
        ("gap", "closed", True),
        ("covered", None, False),
    ]
    await finalize(engine, later.run_id, day3 + DAY)
    capped_row = await cell_row(engine, capped_cell)
    assert (capped_row.last_covered_at, capped_row.consecutive_incomplete) == (day3 + DAY, 0)


ABANDONED = sa.text(
    "SELECT lower(span), upper(span) FROM coverage_intervals WHERE cell_id = :c "
    "AND reason = 'beyond_provider_window' ORDER BY lower(span)"
)
OVERLAPS = sa.text(
    "SELECT count(*) FROM coverage_intervals a JOIN coverage_intervals b ON a.cell_id = b.cell_id "
    "AND a.id < b.id AND a.span && b.span WHERE a.cell_id = :c AND a.kind = 'gap' AND b.kind = 'gap'"
)
SPEC = sa.text("SELECT spec FROM fetch_requests WHERE id = :i")


async def plan_at(engine: AsyncEngine, owner: RunOwner, now: datetime) -> RequestSpec:
    """The run's one planned JSearch spec, planned at `now` by the real planner (Task 13)."""
    async with engine.begin() as c:
        version = (
            await c.execute(
                sa.text("SELECT settings_version_id FROM runs WHERE id = :r"), {"r": owner.run_id}
            )
        ).scalar_one()
        report = await plan_run(
            c,
            owner=owner,
            include_paid=True,
            settings=await load_run_settings(c, version),
            instances=await list_instances(c),
            now=now,
            account=None,
        )
        (planned,) = report.primary_fetch_request_ids
        return RequestSpec.from_json((await c.execute(SPEC, {"i": planned})).scalar_one())


async def test_beyond_provider_window_lets_contiguity_advance_and_never_overlaps(
    engine, current_epoch
) -> None:
    """(e) and carry 5: the parts abandoned at planning (`beyond_provider_window`) of successive runs follow
    one another while `last_covered_at` stays put, and an END traversal advances past all of them."""
    await seed_inputs(engine, queries=(TEST_QUERY,), enabled=("jsearch",))
    cell = await jsearch_cell(engine)  # the remote-de cell of the query, which the planner plans
    now, covered = utcnow(), utcnow() - 40 * DAY
    async with engine.begin() as c:
        await c.execute(COVERED, {"at": covered, "c": cell})
    first = await queued_run(engine)
    spec = await plan_at(engine, first, now)
    assert spec.window == (now - 720 * H, now) and spec.params["date_posted"] == "month"
    await finalize(engine, first.run_id, now)  # the run never got to the cell (say, a deploy): no coverage
    await finish(engine, first)
    second = await queued_run(engine)
    spec = await plan_at(engine, second, now + 6 * H)
    async with engine.connect() as c:
        abandoned = [tuple(r) for r in await c.execute(ABANDONED, {"c": cell})]
        overlaps = (await c.execute(OVERLAPS, {"c": cell})).scalar_one()
    # [last_covered_at, now - 720 h), then from there on to the second run's bound: they never overlap
    assert abandoned == [(covered, now - 720 * H), (now - 720 * H, now + 6 * H - 720 * H)] and overlaps == 0
    assert (await cell_row(engine, cell)).last_covered_at == covered
    assert spec.window == (now + 6 * H - 720 * H, now + 6 * H)
    await record(engine, cell, spec.window, traversal("end"), second.run_id)
    await finalize(engine, second.run_id, now + 6 * H)
    # W.lower lies after last_covered_at, but only abandoned gaps lie between them
    assert (await cell_row(engine, cell)).last_covered_at == now + 6 * H


RAW_ITEM = sa.text(
    "INSERT INTO raw_items (raw_response_id, item_index, native_id, item_sha256, disposition, disposed_at) "
    "VALUES (:r, :i, :n, :sha, 'linked', now()) RETURNING id"
)
REQUEST = sa.text("SELECT raw_response_id, new_postings, created_at FROM fetch_requests WHERE id = :i")
FETCHED = sa.text("SELECT fetched_at FROM raw_responses WHERE id = :i")


async def test_cell_stats_are_finalized_from_durable_rows_once(
    engine, current_epoch, job_factory: JobFactory, jsearch_fixture_adapter: JSearchAdapter
) -> None:
    owner = await queued_run(engine)
    cell = await jsearch_cell(engine)
    spec = await page1_spec(engine, cell)
    ctx = acquire_ctx(engine, owner, jsearch_fixture_adapter, billing="paid", provider="jsearch")
    served = await acquire_page(ctx, spec, cell_id=cell, phase="primary", planned_id=None)
    assert isinstance(served, Served)  # the run's one dispatched request of the cell
    async with engine.begin() as c:
        request = (await c.execute(REQUEST, {"i": served.fetch_request_id})).one()
        fetched_at = (await c.execute(FETCHED, {"i": request.raw_response_id})).scalar_one()
        items = [
            int(
                (
                    await c.execute(
                        RAW_ITEM,
                        {
                            "r": request.raw_response_id,
                            "i": index,
                            "n": f"uid-{index}",
                            "sha": bytes([index]) * 32,
                        },
                    )
                ).scalar_one()
            )
            for index in range(4)
        ]
    for index in range(3):  # three postings first seen through the run's raw items
        await job_factory(native_id=f"uid-{index}", observations=[(fetched_at, items[index])])
    # an older posting, re-sighted by the run: not new
    await job_factory(native_id="uid-3", observations=[(fetched_at - 10 * DAY, None), (fetched_at, items[3])])
    async with engine.begin() as c:  # the cell explores (§5.5.3): its attempt is the latest exploration
        await c.execute(sa.text("UPDATE queries SET exploration = true WHERE text = :t"), {"t": TEST_QUERY})
    stats = await finalize(engine, owner.run_id, utcnow())
    assert stats == {cell: CellStats(requests=1, new_postings=3, reached_end=False, covered=False)}
    row = await cell_row(engine, cell)
    async with engine.connect() as c:
        assert (await c.execute(REQUEST, {"i": served.fetch_request_id})).one().new_postings == 3
    assert row.yield_ema == pytest.approx(0.3 * 3 + 0.7 * 0.5) and row.yield_ema == pytest.approx(1.25)
    # the run read page 1 only, whose cursor was left: its traversal did not reach END (fix round 1, M3)
    assert (row.zero_yield_streak, row.consecutive_incomplete) == (0, 1)
    assert row.last_attempted_at == request.created_at and row.last_success_at is not None
    assert row.last_exploration_at == request.created_at
    cursor = json.loads(load_fixture("jsearch", "search_v2_page1")[2])["data"]["cursor"]
    assert spec.window is not None
    assert (
        row.continuation == JSearchContinuation(cursor, fetched_at, spec.window, 2).dump()
    )  # page 2's cursor
    # the third run in a row that leaves the paid cell incomplete raises `coverage.too_broad`; a covered run
    # resolves it
    await finish(engine, owner)
    window_end = utcnow()
    for run in range(1, 5):
        later = await queued_run(engine)
        await a_request(engine, later, cell, f"run-{run}")
        window = (window_end - 7 * DAY, window_end + run * H)
        result = traversal("end") if run == 4 else traversal("cursor", "page_cap", f"c{run}")
        await record(engine, cell, window, result, later.run_id)
        await finalize(engine, later.run_id, utcnow())
        await finish(engine, later)
        incomplete = (await cell_row(engine, cell)).consecutive_incomplete
        async with engine.connect() as c:
            alert = (await c.execute(ALERT, {"k": f"coverage.too_broad:cell:{cell}"})).one_or_none()
        assert (incomplete, None if alert is None else alert.severity) == {
            1: (2, None),
            2: (3, "info"),
            3: (4, "info"),
            4: (0, None),
        }[run]
    assert (await cell_row(engine, cell)).yield_ema == pytest.approx(1.25)  # no dispatched request since


SKIP = sa.text(
    "SELECT id, cell_id, source_instance_id, request_key, spec FROM fetch_requests WHERE run_id = :r"
)


async def test_a_shutdown_skip_out_of_attempts_is_recorded_as_a_gap(engine, current_epoch) -> None:
    """§7.16.5 (carry 5): a `skipped(shutdown)` spec already at attempt 8 is not planned again; its window is
    recorded as the open gap `shutdown` (by the §5.6 open-gap rule), once."""
    await seed_inputs(engine, queries=(TEST_QUERY,), enabled=("jsearch",))
    cell = await jsearch_cell(engine)
    owner, now = await queued_run(engine), utcnow()
    spec = await plan_at(engine, owner, now)
    assert spec.window is not None
    async with engine.begin() as c:
        row = (await c.execute(SKIP, {"r": owner.run_id})).one()
        await transition(
            c, "fetch_requests", row.id, ["planned"], "skipped", set_={"skip_reason": "shutdown"}
        )
        for attempt_seq in range(2, 9):  # every later attempt of the key ended by a shutdown too
            await insert_fetch_request(
                c,
                owner=owner,
                cell_id=cell,
                source_instance_id=int(row.source_instance_id),
                phase="primary",
                spec=spec,
                request_key=bytes(row.request_key),
                attempt_seq=attempt_seq,
                status="skipped",
                skip_reason="shutdown",
            )
    for _ in range(2):  # the plan is a receipt: planning again records nothing new
        async with engine.begin() as c:
            version = (
                await c.execute(
                    sa.text("SELECT settings_version_id FROM runs WHERE id = :r"), {"r": owner.run_id}
                )
            ).scalar_one()
            report = await plan_run(
                c,
                owner=owner,
                include_paid=True,
                settings=await load_run_settings(c, version),
                instances=await list_instances(c),
                now=now,
                account=None,
            )
        assert (report.primary_fetch_request_ids, report.skipped, report.replanned) == (
            [],
            {"shutdown": 1},
            0,
        )
    assert await intervals(engine, cell) == [("gap", "open", "shutdown", *spec.window, None, False)]


# ---- fix round 1 ------------------------------------------------------------------------------------------

GAP_ROWS = sa.text(
    "SELECT gap_status, reason, lower(span), upper(span), gap_cursor, run_id FROM coverage_intervals "
    "WHERE cell_id = :c AND kind = 'gap' AND reason <> 'beyond_provider_window' ORDER BY id"
)


async def test_an_open_gap_straddling_the_provider_window_does_not_block_contiguity(
    engine, current_epoch
) -> None:
    """I2 (§5.6): when the planner abandons the part beyond 720 h, an open gap that straddles that bound is
    abandoned and its newer part stays open, so the run's END closes it and `last_covered_at` advances."""
    await seed_inputs(engine, queries=(TEST_QUERY,), enabled=("jsearch",))
    cell = await jsearch_cell(engine)
    now, covered = utcnow(), utcnow() - 40 * DAY
    async with engine.begin() as c:
        await c.execute(COVERED, {"at": covered, "c": cell})
    first = await queued_run(engine)
    earlier = await plan_at(engine, first, now - 6 * H)
    assert earlier.window is not None
    await record(engine, cell, earlier.window, traversal("cursor", "page_cap", "c1"), first.run_id)
    await finalize(engine, first.run_id, now - 6 * H)
    await finish(engine, first)
    boundary = now - 720 * H  # where the second run's window starts
    second = await queued_run(engine)
    spec = await plan_at(engine, second, now)
    assert spec.window == (boundary, now)
    async with engine.connect() as c:
        gaps = [tuple(r) for r in await c.execute(GAP_ROWS, {"c": cell})]
    assert gaps == [
        ("abandoned", "page_cap", now - 6 * H - 720 * H, now - 6 * H, {"cursor": "c1"}, first.run_id),
        ("open", "page_cap", boundary, now - 6 * H, {"cursor": "c1"}, first.run_id),  # its newer part
    ]
    assert await record(engine, cell, spec.window, traversal("end"), second.run_id) == CoverageEffect(0, 1, 1)
    await finalize(engine, second.run_id, now)
    assert (await cell_row(engine, cell)).last_covered_at == now


async def test_a_traversal_short_of_end_counts_as_incomplete_on_a_repeated_gap(
    engine, current_epoch, jsearch_fixture_adapter: JSearchAdapter
) -> None:
    """M3: a run whose traversal of the cell did not reach END counts, also when its gap repeats a span an
    earlier run recorded (no new row)."""
    cell = await jsearch_cell(engine)
    window = (DAY0, DAY0 + 3 * DAY)  # the same window twice (a reused cursor's traversal)
    for run in (1, 2):
        owner = await queued_run(engine)
        spec = await page1_spec(engine, cell)
        ctx = acquire_ctx(engine, owner, jsearch_fixture_adapter, billing="paid", provider="jsearch")
        assert isinstance(
            await acquire_page(ctx, spec, cell_id=cell, phase="primary", planned_id=None), Served
        )
        effect = await record(engine, cell, window, traversal("cursor", "page_cap", "c"), owner.run_id)
        assert effect.opened == (1 if run == 1 else 0)
        await finalize(engine, owner.run_id, utcnow())
        await finish(engine, owner)
        assert (await cell_row(engine, cell)).consecutive_incomplete == run


BOARD_CELL = sa.text("INSERT INTO coverage_cells (source_instance_id) VALUES (:i) RETURNING id")
SNAPSHOT_GAPS = sa.text(
    "SELECT gap_status, lower(span), upper(span), lower_inc(span), upper_inc(span) FROM coverage_intervals "
    "WHERE cell_id = :c ORDER BY id"
)
STARTED = sa.text("SELECT started_at FROM runs WHERE id = :r")


async def test_successive_incomplete_snapshots_open_gaps_that_follow_one_another(
    engine, current_epoch
) -> None:
    """M5: an incomplete snapshot's gap is `[start, taken_at)`, starting where the board's previous one ended
    (the run's start for the first); a complete snapshot closes them, and a replay writes nothing more."""
    await seed_inputs(engine, companies=(("greenhouse", "getyourguide"),), enabled=("ats",))
    async with engine.begin() as c:
        board = next(i for i in await list_instances(c) if i.source_id == "greenhouse")
        cell = int((await c.execute(BOARD_CELL, {"i": board.id})).scalar_one())
    incomplete = SnapshotDraft(frozenset({"a"}), 1, False, "no_end")
    complete = SnapshotDraft(frozenset({"a", "b"}), 2, True, None)
    taken: list[datetime] = []
    for draft in (incomplete, incomplete, complete):
        owner = await queued_run(engine, include_paid=False)
        taken.append(utcnow())
        async with engine.begin() as c:
            started = (await c.execute(STARTED, {"r": owner.run_id})).scalar_one()
            first = await record_ats_snapshot(
                c, cell, board.id, draft, run_id=owner.run_id, taken_at=taken[-1]
            )
            again = await record_ats_snapshot(
                c, cell, board.id, draft, run_id=owner.run_id, taken_at=taken[-1]
            )
        assert again == first  # once per board and run
        if len(taken) == 1:
            first_start = started
        await finish(engine, owner)
    assert await rows(engine, SNAPSHOT_GAPS, cell) == [
        ("closed", first_start, taken[0], True, False),
        ("closed", taken[0], taken[1], True, False),  # from the previous gap's end: no overlap
    ]


async def rows(engine: AsyncEngine, statement: sa.TextClause, cell: int) -> list[tuple[object, ...]]:
    async with engine.connect() as c:
        return [tuple(r) for r in await c.execute(statement, {"c": cell})]
