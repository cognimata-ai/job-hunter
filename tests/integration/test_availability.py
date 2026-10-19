import hashlib
from datetime import datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.availability import rules
from jobhunter.availability.rules import (
    JOB_ORDER,
    SightingEffect,
    apply_sighting,
    recompute_job_availability,
    stale_sweep,
)
from jobhunter.clock import utcnow
from jobhunter.db.transitions import transition
from jobhunter.errors import FencedOut
from jobhunter.worker import main as worker_main
from jobhunter.worker import reconcilers as r
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.collect import activate_collection_settings, add_posting, ctx, job_lockable

pytestmark = pytest.mark.integration

DAY = timedelta(days=1)
POSTING = sa.text(
    "SELECT p.job_id, p.availability, p.availability_reason, p.availability_changed_at, p.seen_count, "
    "p.last_seen_at, p.latest_observation_id, j.availability AS job_availability "
    "FROM postings p JOIN jobs j ON j.id = p.job_id WHERE p.id = :p"
)
JOB = sa.text("SELECT availability, availability_changed_at FROM jobs WHERE id = :j")
OBSERVATION = sa.text(
    "INSERT INTO posting_observations (posting_id, source_instance_id, raw_response_sha256, observed_at, "
    "normalized, content_hash, completeness, quality) SELECT p.id, p.source_instance_id, :sha, :at, '{}', "
    ":sha, 'full', 'ok' FROM postings p WHERE p.id = :p RETURNING id"
)


async def one(engine: AsyncEngine, statement: sa.TextClause, **params: Any) -> Any:
    async with engine.connect() as c:
        return (await c.execute(statement, params)).one()


async def generation(engine: AsyncEngine) -> int:
    async with engine.connect() as c:
        return int(
            (await c.execute(sa.text("SELECT generation FROM ranking_state WHERE id = 1"))).scalar_one()
        )


async def sighted(engine: AsyncEngine, posting_id: int, at: datetime) -> SightingEffect:
    """A new observation of the posting at `at` and its sighting, as identity's step 4 applies it."""
    async with engine.begin() as c:
        sha = hashlib.sha256(f"{posting_id}@{at.isoformat()}".encode()).digest()
        observation = int(
            (await c.execute(OBSERVATION, {"p": posting_id, "at": at, "sha": sha})).scalar_one()
        )
        return await apply_sighting(c, posting_id, observed_at=at, observation_id=observation)


async def test_sighting_rules_and_job_derivation(engine, current_epoch, posting_factory) -> None:
    assert JOB_ORDER == ("active", "unknown", "stale", "closed")
    now = utcnow()

    # a stale posting sighted after its change: revived at the sighting's time, counted, its latest
    stale = await posting_factory(
        engine,
        source_id="adzuna",
        native_id="s1",
        availability="stale",
        last_seen_at=now - 40 * DAY,
        availability_changed_at=now - 9 * DAY,
    )
    seen = now - timedelta(hours=1)
    assert await sighted(engine, stale, seen) == SightingEffect(revived=True, latest_moved=True)
    row = await one(engine, POSTING, p=stale)
    assert (row.availability, row.availability_reason, row.availability_changed_at) == ("active", None, seen)
    assert (row.seen_count, row.last_seen_at) == (2, seen)
    assert row.job_availability == "stale"  # the job derives where its postings change, not here

    # a closed posting sighted before its change: unchanged, but counted; a replay never moves backwards
    closed_at = now - timedelta(hours=1)
    closed = await posting_factory(
        engine,
        source_id="jsearch",
        native_id="c1",
        availability="closed",
        last_seen_at=now - timedelta(hours=3),
        availability_changed_at=closed_at,
    )
    before = await one(engine, POSTING, p=closed)
    late = now - timedelta(hours=4)
    assert await sighted(engine, closed, late) == SightingEffect(revived=False, latest_moved=False)
    row = await one(engine, POSTING, p=closed)
    assert (row.availability, row.availability_changed_at, row.seen_count) == ("closed", closed_at, 2)
    assert (row.last_seen_at, row.latest_observation_id) == (
        before.last_seen_at,
        before.latest_observation_id,
    )
    # between the latest observation and the change: the latest moves, the posting stays closed
    between = now - timedelta(hours=2)
    assert await sighted(engine, closed, between) == SightingEffect(revived=False, latest_moved=True)
    row = await one(engine, POSTING, p=closed)
    assert (row.availability, row.seen_count, row.last_seen_at) == ("closed", 3, between)

    # a job derives the best state of its postings: (closed, unknown) is unknown, (closed, stale) stale
    for other_state, expected in (("unknown", "unknown"), ("stale", "stale")):
        job = (
            await one(
                engine,
                POSTING,
                p=await posting_factory(
                    engine,
                    source_id="jsearch",
                    native_id=f"closed-{other_state}",
                    availability="closed",
                    last_seen_at=now - 5 * DAY,
                    availability_changed_at=now - 5 * DAY,
                ),
            )
        ).job_id
        async with engine.begin() as c:
            other, _ = await add_posting(c, job, "arbeitnow", observed_at=now - 4 * DAY)
            await transition(c, "postings", other, ["active"], other_state)
        started = utcnow()
        async with engine.begin() as c:
            assert await recompute_job_availability(c, job) is True
        derived = await one(engine, JOB, j=job)
        assert derived.availability == expected and derived.availability_changed_at >= started
        async with engine.begin() as c:
            assert await recompute_job_availability(c, job) is False  # nothing changed
        assert (await one(engine, JOB, j=job)).availability_changed_at == derived.availability_changed_at


async def aggregator_posting(
    posting_factory, engine: AsyncEngine, source_id: str, native_id: str, age: timedelta
):
    seen = utcnow() - age
    return await posting_factory(
        engine,
        source_id=source_id,
        native_id=native_id,
        availability="active",
        last_seen_at=seen,
        availability_changed_at=seen,
    )


async def test_stale_sweep_touches_aggregators_only(engine, current_epoch, posting_factory) -> None:
    old = await aggregator_posting(posting_factory, engine, "adzuna", "a1", 31 * DAY)
    recent = await aggregator_posting(posting_factory, engine, "adzuna", "a2", 29 * DAY)
    board = await aggregator_posting(posting_factory, engine, "greenhouse", "g1", 100 * DAY)
    gen = await generation(engine)
    started = utcnow()
    async with engine.begin() as c:
        assert await stale_sweep(c, now=utcnow(), stale_ttl_days=30) == 1
    row = await one(engine, POSTING, p=old)
    assert (row.availability, row.availability_reason, row.job_availability) == ("stale", None, "stale")
    assert row.availability_changed_at >= started  # the database's now()
    assert (await one(engine, POSTING, p=recent)).availability == "active"
    assert (await one(engine, POSTING, p=board)).availability == "active"  # an ATS posting never goes stale
    assert await generation(engine) == gen + 1
    async with engine.begin() as c:
        assert await stale_sweep(c, now=utcnow(), stale_ttl_days=30) == 0
    assert await generation(engine) == gen + 1  # nothing changed: no bump

    # bounded batches: three more, `limit=2`: 2, then 1, then none
    for source_id in ("jsearch", "serpapi", "arbeitnow"):
        await aggregator_posting(posting_factory, engine, source_id, f"{source_id}-old", 45 * DAY)
    counts = []
    for _ in range(3):
        async with engine.begin() as c:
            counts.append(await stale_sweep(c, now=utcnow(), stale_ttl_days=30, limit=2))
    assert counts == [2, 1, 0]
    assert rules.STALE_BATCH == 1000


async def test_stale_sweep_takes_the_job_rows_before_its_guard(
    engine, current_epoch, posting_factory
) -> None:
    old = await aggregator_posting(posting_factory, engine, "adzuna", "a1", 31 * DAY)
    job = (await one(engine, POSTING, p=old)).job_id
    seen: list[bool] = []

    async def probe(conn: AsyncConnection) -> None:
        seen.append(await job_lockable(engine, job))

    async with engine.begin() as c:
        assert await stale_sweep(c, now=utcnow(), stale_ttl_days=30, guard=probe) == 1
    assert seen == [False]  # the guard ran while this transaction held the job row (Ruling T19-a)

    other = await aggregator_posting(posting_factory, engine, "jsearch", "j1", 31 * DAY)

    async def fenced(conn: AsyncConnection) -> None:
        raise FencedOut("worker_epoch")

    with pytest.raises(FencedOut):
        async with engine.begin() as c:
            await stale_sweep(c, now=utcnow(), stale_ttl_days=30, guard=fenced)
    assert (await one(engine, POSTING, p=other)).availability == "active"


async def test_the_stale_sweep_reconciler_reads_the_ttl_and_is_fenced(
    engine, current_epoch, queue_app, posting_factory
) -> None:
    assert r.REGISTRY["stale_sweep"][0] == {"tick"}  # and `stale_sweep_daily` (fix round 1, M-2)
    assert "jobhunter.availability.rules" in worker_main._REGISTERING_MODULES
    reconcile = r.REGISTRY["stale_sweep"][1]
    old = await aggregator_posting(posting_factory, engine, "adzuna", "a1", 31 * DAY)
    assert await reconcile(ctx(engine, queue_app, epoch=current_epoch + 1)) == 0  # a superseded worker
    assert (await one(engine, POSTING, p=old)).availability == "active"
    assert await reconcile(ctx(engine, queue_app)) == 1  # `stale_ttl_days` null: `max_ad_age_days`, 30
    assert (await one(engine, POSTING, p=old)).availability == "stale"
    assert await reconcile(ctx(engine, queue_app)) == 0


# ---- fix round 1 ------------------------------------------------------------------------------------------


async def test_the_stale_sweep_reconciler_uses_a_set_stale_ttl_days(
    engine, current_epoch, queue_app, posting_factory
) -> None:
    """M-7: `collection.stale_ttl_days` 10 (not null): 11 days unseen goes stale, 9 days does not."""
    await activate_collection_settings(engine, stale_ttl_days=10)
    old = await aggregator_posting(posting_factory, engine, "adzuna", "a1", 11 * DAY)
    recent = await aggregator_posting(posting_factory, engine, "jsearch", "j1", 9 * DAY)
    assert await r.REGISTRY["stale_sweep"][1](ctx(engine, queue_app)) == 1
    assert (await one(engine, POSTING, p=old)).availability == "stale"
    assert (await one(engine, POSTING, p=recent)).availability == "active"


async def test_the_daily_stale_sweep_loops_over_batches_and_the_tick_runs_one(
    engine, current_epoch, queue_app, posting_factory, monkeypatch
) -> None:
    """M-2: the tick sweeps one batch; the daily run sweeps batch after batch until a short one."""
    assert r.REGISTRY["stale_sweep"][0] == {"tick"} and r.REGISTRY["stale_sweep_daily"][0] == {"daily"}
    monkeypatch.setattr(rules, "STALE_BATCH", 2)
    made = [
        await aggregator_posting(posting_factory, engine, source_id, f"{source_id}-{n}", 40 * DAY)
        for n in range(2)
        for source_id in ("adzuna", "jsearch", "arbeitnow")
    ]
    assert (await r.run_reconcilers(ctx(engine, queue_app), "tick"))["stale_sweep"] == 2
    states = [(await one(engine, POSTING, p=p)).availability for p in made]
    assert sorted(states) == ["active"] * 4 + ["stale"] * 2
    daily = r.REGISTRY["stale_sweep_daily"][1]
    assert await daily(ctx(engine, queue_app, epoch=current_epoch + 1)) == 0  # a superseded worker
    counts = await r.run_reconcilers(ctx(engine, queue_app), "daily")
    assert counts["stale_sweep_daily"] == 4  # batches of 2, 2, then an empty one
    assert {(await one(engine, POSTING, p=p)).availability for p in made} == {"stale"}
