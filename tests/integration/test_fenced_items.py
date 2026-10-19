"""§18.2 "Fenced raw items" (Fable round 3): the source fence and the job fence of identity resolution."""

from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.dedup.identity import resolve_deferred_items, resolve_run_items
from jobhunter.worker.runs import finalize_run
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import STEPSTONE_1, pending_item, queued_run, resolved

pytestmark = pytest.mark.integration

ITEM = sa.text(
    "SELECT disposition, reason_code, normalized IS NOT NULL AS kept, observation_id FROM raw_items "
    "WHERE id = :i"
)
FENCE = sa.text(
    "INSERT INTO purge_fences (kind, source_id, job_ids, manifest_id) "
    "VALUES (:kind, :source, CAST(:jobs AS bigint[]), 1) RETURNING id"
)
RELEASE = sa.text("UPDATE purge_fences SET released_at = now() WHERE id = :f")


async def item_row(engine: AsyncEngine, raw_item_id: int) -> tuple[Any, ...]:
    async with engine.connect() as c:
        return tuple((await c.execute(ITEM, {"i": raw_item_id})).one())


async def fence(
    engine: AsyncEngine, kind: str, *, source: str | None = None, jobs: tuple[int, ...] = ()
) -> int:
    async with engine.begin() as c:
        return int(
            (await c.execute(FENCE, {"kind": kind, "source": source, "jobs": list(jobs)})).scalar_one()
        )


JOBS_OF_NATIVE = sa.text(
    "SELECT p.job_id, i.source_id FROM postings p JOIN source_instances i ON i.id = p.source_instance_id "
    "WHERE p.native_id = :n ORDER BY i.source_id"
)


async def jobs_of_native(engine: AsyncEngine, native_id: str) -> dict[str, int]:
    """The job of each posting with the native id, by source."""
    async with engine.connect() as c:
        rows = (await c.execute(JOBS_OF_NATIVE, {"n": native_id})).all()
    return {row.source_id: int(row.job_id) for row in rows}


async def test_source_fence_and_job_fence_dispositions(
    engine, current_epoch
) -> None:  # §18.2 "Fenced raw items"
    owner = await queued_run(engine)
    at = utcnow() - timedelta(hours=6)
    for native_id, hours in (("H", 0), ("K", 1)):  # the jobs behind keys H and K
        item = await pending_item(
            engine, owner, "jsearch", {"native_id": native_id}, fetched_at=at + timedelta(hours=hours)
        )
        assert await resolved(engine, owner, item) == "linked"
    job_h, job_k = (
        (await jobs_of_native(engine, "H"))["jsearch"],
        (await jobs_of_native(engine, "K"))["jsearch"],
    )

    # an active source fence for adzuna: an Adzuna item is out of scope, and nothing else happens
    source_fence = await fence(engine, "source", source="adzuna")
    adzuna = await pending_item(
        engine, owner, "adzuna", {"native_id": "9"}, fetched_at=at + timedelta(hours=2)
    )
    assert await resolved(engine, owner, adzuna) == "out_of_scope"
    assert await item_row(engine, adzuna) == ("out_of_scope", "source_purged", False, None)
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM postings"))).scalar_one() == 2

    # a jobs fence naming the job behind H (and one naming K's): items with those keys are deferred, kept
    h_fence, k_fence = await fence(engine, "jobs", jobs=(job_h,)), await fence(engine, "jobs", jobs=(job_k,))
    held = await pending_item(
        engine,
        owner,
        "serpapi",
        {"native_id": "H", "primary_link": STEPSTONE_1},
        fetched_at=at + timedelta(hours=3),
    )
    still = await pending_item(
        engine, owner, "serpapi", {"native_id": "K"}, fetched_at=at + timedelta(hours=4)
    )
    async with engine.begin() as c:
        assert (await c.execute(sa.text("SELECT generation FROM ranking_state"))).scalar_one() == 2
    counts = await resolve_run_items(engine, owner, now=utcnow(), clock=utcnow)
    assert counts == {"deferred": 2}
    for item in (held, still):
        assert await item_row(engine, item) == ("deferred", "job_fenced", True, None)
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM postings"))).scalar_one() == 2
        assert (await c.execute(sa.text("SELECT count(*) FROM ad_keys"))).scalar_one() == 2  # H and K only
        assert (await c.execute(sa.text("SELECT generation FROM ranking_state"))).scalar_one() == 2

    # the run finalizes with no pending item; a later run re-resolves the deferred items of earlier runs
    async with engine.begin() as c:
        await finalize_run(c, owner, status="succeeded", reason=None, report={})
    later = await queued_run(engine)
    assert await resolve_deferred_items(engine, later, now=utcnow()) == {"deferred": 2}  # both still fenced
    async with engine.begin() as c:
        await c.execute(RELEASE, {"f": h_fence})
    assert await resolve_deferred_items(engine, later, now=utcnow()) == {"linked": 1, "deferred": 1}
    linked = await item_row(engine, held)
    assert (linked[0], linked[1], linked[2], linked[3] is not None) == ("linked", None, False, True)
    # the SerpApi posting is on the JSearch posting's job
    assert await jobs_of_native(engine, "H") == {"jsearch": job_h, "serpapi": job_h}
    async with engine.connect() as c:
        sources = (
            await c.execute(sa.text("SELECT source_ids FROM jobs WHERE id = :j"), {"j": job_h})
        ).scalar_one()
    assert sources == ["jsearch", "serpapi"]
    assert await item_row(engine, still) == ("deferred", "job_fenced", True, None)  # stays deferred
    assert await item_row(engine, adzuna) == ("out_of_scope", "source_purged", False, None)  # never deferred
    # the limit bounds a pass; items of the run itself are not "earlier"
    assert await resolve_deferred_items(engine, later, now=utcnow(), limit=0) == {}
    async with engine.begin() as c:
        await c.execute(RELEASE, {"f": k_fence})
        await c.execute(RELEASE, {"f": source_fence})
    assert await resolve_deferred_items(engine, later, now=utcnow()) == {"linked": 1}
