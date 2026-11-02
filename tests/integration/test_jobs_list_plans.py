"""The pinned plans of the Jobs list engine (plan M4 Task 4, P1-P8; spec §14.3).

2,000 cards seeded set-based (`bulk_seed_cards`: 80 % scored, 40 groups, German and English ads), analysed,
on a server of their own: ANALYZE writes `pg_class` statistics in place, which no reset takes back, so the
shared server's planner estimates would change for every later test (`test_views_m3.py`). Each page runs
through `select_page` in a read snapshot with sequential scans off (`test_views_m3.py:200-218`), and every
statement it ran is EXPLAINed with its own parameters in the same snapshot, found by the tag its SQL starts
with (`/* jobs_list.<segment> */`):

- P1, P2: a second page of a score sort walks `det_rank_mid` (`det_rank_low`) with the scope and the row
  comparison in its `Index Cond`, and sorts nothing;
- P3: a page of the unscored section walks `score_targets_unscored_rank`;
- P4: the `posted` sort walks `jobs_list_posted_any`, in its dated and its undated branch;
- P5: a second page of the `first_seen` sort walks `jobs_list_first_seen` with the row comparison and sorts
  nothing;
- P6: a keyword page reads `job_revisions_tsv` and `jobs_title_trgm` by bitmap index scans;
- P8: each count of a first page aggregates a `Limit` (the cap).
"""

import asyncio
import re
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any, Final

import pytest
import sqlalchemy as sa
from jobhunter.db.migrate import run_migrate
from jobhunter.db.session import make_null_engine
from jobhunter.db.snapshot import read_snapshot
from jobhunter.jobs.listing import Position, select_page
from jobhunter.jobs.query import JobsQuery
from sqlalchemy import event

from tests.support.jobs_list import BulkSeed, bulk_seed_cards
from tests.support.pg import PgServer, config_for, remove_pg_server, start_pg_server

pytestmark = pytest.mark.integration

CARDS: Final = 2_000
_TAG: Final = re.compile(r"/\* (jobs_list\.\w+) \*/")
_SORT_NODE: Final = re.compile(r"^\s*(?:->\s+)?(?:Incremental )?Sort\b", re.MULTILINE)


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[PgServer, BulkSeed]]:
    server = start_pg_server(tmp_path_factory.mktemp("jobs-list-plans"))
    try:
        run_migrate(config_for(server, "migrate"))
        yield server, asyncio.run(_seed(server))
    finally:
        remove_pg_server(server)


async def _seed(server: PgServer) -> BulkSeed:
    engine = make_null_engine(server.dsn("migrator", driver="sqlalchemy"))
    try:
        seed = await bulk_seed_cards(engine, CARDS, scored_share=0.8, groups=40, langs=("de", "en"))
        async with engine.connect() as c:
            await c.execute(sa.text("ANALYZE"))  # as the owner
            await c.commit()
    finally:
        await engine.dispose()
    return seed


def month(seed: BulkSeed) -> tuple[datetime | None, datetime | None]:
    """The default window (`30d`) at the time of the seed."""
    return seed.now - timedelta(days=30), None


async def explained(
    server: PgServer,
    query: JobsQuery,
    *,
    window: tuple[datetime | None, datetime | None],
    after: Position | None = None,
    with_counts: bool = False,
    limit: int = 20,
) -> tuple[dict[str, str], Position | None]:
    """The plan of every tagged statement `select_page` ran for this page, by tag, and the page's next
    position."""
    engine = make_null_engine(server.dsn("migrator", driver="sqlalchemy"))
    statements: list[tuple[str, Any]] = []

    def record(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool) -> None:
        statements.append((statement, parameters))

    try:
        async with read_snapshot(engine) as conn:
            await conn.execute(sa.text("SET LOCAL enable_seqscan = off"))
            event.listen(engine.sync_engine, "before_cursor_execute", record)
            try:
                selection = await select_page(
                    conn, query, window=window, after=after, limit=limit, with_counts=with_counts
                )
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", record)
            assert selection.rows, "the page is empty: the plan of an empty walk proves nothing"
            plans: dict[str, str] = {}
            for statement, parameters in statements:
                if (tag := _TAG.search(statement)) is not None:
                    rows = await conn.exec_driver_sql("EXPLAIN " + statement, parameters)
                    plans[tag.group(1)] = "\n".join(str(line) for line in rows.scalars())
    finally:
        await engine.dispose()
    return plans, selection.next


async def second_page(
    server: PgServer, query: JobsQuery, window: tuple[datetime | None, datetime | None]
) -> dict[str, str]:
    _, after = await explained(server, query, window=window)
    assert after is not None
    plans, _ = await explained(server, query, window=window, after=after)
    return plans


@pytest.mark.parametrize(
    ("sort", "index", "column"),
    [("score_mid", "det_rank_mid", "score_mid"), ("score_low", "det_rank_low", "score_low")],
)
async def test_p1_p2_a_score_page_walks_its_rank_index_without_a_sort(
    seeded: tuple[PgServer, BulkSeed], sort: str, index: str, column: str
) -> None:
    server, seed = seeded
    plans = await second_page(server, JobsQuery(sort=sort), month(seed))
    plan = plans["jobs_list.scored"]
    assert f"Index Scan using {index} on det_results d" in plan, plan
    cond = re.search(r"Index Cond: (.*)", plan[plan.index(f"Index Scan using {index}") :])
    assert cond is not None, plan
    assert re.search(r"\bscope_version_id = ", cond.group(1)), plan
    assert re.search(rf"ROW\((?:d\.)?{column}, (?:d\.)?job_id\) < ROW\(", cond.group(1)), plan
    assert _SORT_NODE.search(plan) is None, plan


async def test_p3_the_unscored_section_walks_its_index(seeded: tuple[PgServer, BulkSeed]) -> None:
    server, seed = seeded
    inside = Position("unscored", 0.95, max(seed.card_ids))
    plans, _ = await explained(server, JobsQuery(), window=month(seed), after=inside)
    plan = plans["jobs_list.unscored_ranked"]
    assert "score_targets_unscored_rank" in plan, plan
    assert re.search(r"Index Cond: .*ROW\((?:t\.)?similarity, (?:t\.)?job_id\) < ROW\(", plan), plan


async def test_p4_the_posted_sort_walks_the_posted_index_in_both_branches(
    seeded: tuple[PgServer, BulkSeed],
) -> None:
    server, seed = seeded
    plans = await second_page(server, JobsQuery(sort="posted"), month(seed))
    dated = plans["jobs_list.posted"]
    assert "Index Scan using jobs_list_posted_any on jobs j" in dated, dated
    assert re.search(r"Index Cond: .*ROW\((?:j\.)?posted_at_effective, (?:j\.)?id\) < ROW\(", dated), dated
    assert _SORT_NODE.search(dated) is None, dated
    undated = Position("all", None, max(seed.card_ids))
    plans, _ = await explained(server, JobsQuery(sort="posted"), window=month(seed), after=undated)
    plan = plans["jobs_list.posted_unknown"]
    assert "Index Scan using jobs_list_posted_any on jobs j" in plan, plan
    assert "posted_at_effective IS NULL" in plan, plan
    assert _SORT_NODE.search(plan) is None, plan


async def test_p5_the_first_seen_sort_walks_its_index_without_a_sort(
    seeded: tuple[PgServer, BulkSeed],
) -> None:
    server, seed = seeded
    plan = (await second_page(server, JobsQuery(sort="first_seen"), month(seed)))["jobs_list.first_seen"]
    assert "Index Scan using jobs_list_first_seen on jobs j" in plan, plan
    assert re.search(r"Index Cond: .*ROW\((?:j\.)?first_seen_at, (?:j\.)?id\) < ROW\(", plan), plan
    assert _SORT_NODE.search(plan) is None, plan


@pytest.mark.parametrize("sort", ["score_mid", "posted", "first_seen"])
async def test_p6_a_keyword_page_reads_both_text_indexes(
    seeded: tuple[PgServer, BulkSeed], sort: str
) -> None:
    server, _ = seeded
    plans, _ = await explained(server, JobsQuery(q="Entwickler", sort=sort), window=(None, None))
    assert plans
    for tag, plan in plans.items():
        assert "Bitmap Index Scan on job_revisions_tsv" in plan, (tag, plan)
        assert "Bitmap Index Scan on jobs_title_trgm" in plan, (tag, plan)


async def test_p8_each_count_aggregates_a_limit(seeded: tuple[PgServer, BulkSeed]) -> None:
    server, seed = seeded
    plans, _ = await explained(server, JobsQuery(), window=month(seed), with_counts=True)
    for tag in ("jobs_list.count", "jobs_list.count_unscored"):
        lines = plans[tag].splitlines()
        assert lines[0].startswith("Aggregate"), plans[tag]
        assert lines[1].lstrip().startswith("->  Limit"), plans[tag]


async def test_the_bulk_seed_holds_its_cards(seeded: tuple[PgServer, BulkSeed]) -> None:
    """`bulk_seed_cards` (which the p95 tests of Tasks 5 and 6 consume) made `CARDS` cards, 80 % of them
    scored, and the members of its groups are no cards: every filter opened, the counts say so."""
    server, seed = seeded
    every = JobsQuery(
        posted="any",
        status=["unseen", "seen", "saved", "applied", "hidden"],
        availability=["active", "unknown", "stale", "closed"],
        scoring_state=["scored", "unscored", "filtered", "failed", "expired", "not_a_job_ad"],
    )
    engine = make_null_engine(server.dsn("migrator", driver="sqlalchemy"))
    try:
        async with read_snapshot(engine) as conn:
            first = await select_page(
                conn, every, window=(None, None), after=None, limit=50, with_counts=True
            )
            members = await conn.execute(
                sa.text("SELECT count(*) FROM score_targets WHERE state = 'deferred_duplicate'")
            )
            deferred = int(members.scalar_one())
    finally:
        await engine.dispose()
    assert (len(seed.card_ids), len(seed.scored_ids), len(seed.representative_ids)) == (CARDS, 1_600, 40)
    assert first.counts is not None
    assert (first.counts.total, first.counts.unscored, first.counts.capped) == (CARDS, CARDS - 1_600, False)
    assert deferred == 40
    assert {row.section for row in first.rows} == {"scored"}
    assert {row.job_id for row in first.rows} <= set(seed.scored_ids)
