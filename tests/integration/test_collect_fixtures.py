"""The collection fixtures of Task 16 that Tasks 19 and 20 use: `posting_factory`, `board` and `board20`."""

from datetime import timedelta

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.sources.fixtures import ROUTES

from tests.support.collect import BOARD_SLUG, Board

pytestmark = pytest.mark.integration

POSTING = sa.text(
    "SELECT p.id, p.job_id, p.availability, p.availability_changed_at, p.last_seen_at, p.first_seen_at, "
    "p.seen_count, p.latest_observation_id, i.source_id, i.instance_key, j.availability AS job_availability, "
    "j.availability_changed_at AS job_changed_at, j.structured_seniority, "
    "(SELECT count(*) FROM posting_observations o WHERE o.posting_id = p.id) AS observations, "
    "(SELECT o.observed_at FROM posting_observations o WHERE o.id = p.latest_observation_id) AS observed_at "
    "FROM postings p JOIN source_instances i ON i.id = p.source_instance_id JOIN jobs j ON j.id = p.job_id "
    "WHERE p.id = :id"
)
INSTANCE = sa.text(
    "SELECT i.id, i.source_id, i.instance_key, i.last_snapshot_active_count, i.health, c.board_slug "
    "FROM source_instances i JOIN watchlist_companies c ON c.id = i.watchlist_company_id"
)
BOARD_POSTINGS = sa.text(
    "SELECT p.native_id, p.id, p.availability, j.availability AS job_availability, j.id AS job_id "
    "FROM postings p JOIN jobs j ON j.id = p.job_id WHERE p.source_instance_id = :i ORDER BY p.native_id"
)
ENABLED = sa.text(
    "SELECT v.payload #> '{sources,enabled}' FROM active_config a "
    "JOIN settings_versions v ON v.id = a.settings_version_id"
)


async def test_posting_factory_makes_a_posting_with_its_job_and_one_observation(
    engine, current_epoch, posting_factory
) -> None:
    sighted = utcnow() - timedelta(days=31)
    changed = sighted - timedelta(days=2)
    posting = await posting_factory(
        engine,
        source_id="adzuna",
        native_id="ad-1",
        availability="stale",
        last_seen_at=sighted,
        availability_changed_at=changed,
        structured_seniority="associate",
    )
    async with engine.connect() as conn:
        row = (await conn.execute(POSTING, {"id": posting})).one()
    assert (row.source_id, row.instance_key, row.availability, row.job_availability) == (
        "adzuna",
        "default",
        "stale",
        "stale",
    )
    assert (row.last_seen_at, row.first_seen_at, row.observed_at) == (sighted, sighted, sighted)
    assert (row.availability_changed_at, row.job_changed_at) == (changed, changed)
    assert (row.observations, row.seen_count) == (1, 1) and row.latest_observation_id is not None
    assert row.structured_seniority == "associate"


async def test_posting_factory_creates_the_instance_of_an_ats_source_and_reuses_it(
    engine, current_epoch, posting_factory
) -> None:
    sighted = utcnow() - timedelta(days=100)
    ids = [
        await posting_factory(
            engine,
            source_id="greenhouse",
            native_id=native_id,
            availability="active",
            last_seen_at=sighted,
            availability_changed_at=sighted,
        )
        for native_id in ("x", "y")
    ]
    async with engine.connect() as conn:
        rows = [(await conn.execute(POSTING, {"id": posting})).one() for posting in ids]
        instances = (
            await conn.execute(
                sa.text("SELECT count(*) FROM source_instances WHERE source_id = 'greenhouse'")
            )
        ).scalar_one()
    assert [(r.source_id, r.instance_key) for r in rows] == [("greenhouse", "default")] * 2
    assert instances == 1 and rows[0].job_id != rows[1].job_id


async def assert_board(engine, board: Board, native_ids: list[str]) -> None:
    async with engine.connect() as conn:
        instance = (await conn.execute(INSTANCE)).one()
        postings = (await conn.execute(BOARD_POSTINGS, {"i": board.instance_id})).all()
        enabled = (await conn.execute(ENABLED)).scalar_one()
    assert (instance.id, instance.source_id, instance.board_slug) == (
        board.instance_id,
        "greenhouse",
        BOARD_SLUG,
    )
    assert (instance.instance_key, instance.health) == (BOARD_SLUG, "ok")
    assert instance.last_snapshot_active_count == len(native_ids)
    assert sorted(board.postings) == native_ids
    assert {p.native_id: p.id for p in postings} == dict(board.postings)
    assert {(p.availability, p.job_availability) for p in postings} == {("active", "active")}
    assert len({p.job_id for p in postings}) == len(native_ids)  # a job of its own each
    assert enabled == {"jsearch": False, "serpapi": False, "adzuna": False, "arbeitnow": False, "ats": True}


async def test_board_is_a_greenhouse_watchlist_board_with_three_active_postings(engine, board) -> None:
    await assert_board(engine, board, ["a", "b", "c"])


async def test_board20_has_twenty_active_postings(engine, board20) -> None:
    await assert_board(engine, board20, [f"p{n:02d}" for n in range(1, 21)])


def test_the_fixture_routes_answer_the_board_slug_with_greenhouse_404() -> None:
    request = httpx.Request("GET", f"https://boards-api.greenhouse.io/v1/boards/{BOARD_SLUG}/jobs")
    assert next(route for route in ROUTES if route.matches(request)).file == "ats/greenhouse_404"
