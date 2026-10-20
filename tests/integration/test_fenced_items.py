"""§18.2 "Fenced raw items" (Fable round 3): the source fence and the job fence of identity resolution."""

from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.dedup.identity import resolve_deferred_items, resolve_run_items
from jobhunter.graphs.collect.nodes.resolve_identity import make_resolve_identity
from jobhunter.graphs.collect.state import initial_state
from jobhunter.worker.runs import finalize_run
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import STEPSTONE_1, make_deps, pending_item, queued_run, resolved

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


async def row_version(engine: AsyncEngine, raw_item_id: int) -> str:
    """The raw item row's `xmin`: it changes with every update of the row."""
    async with engine.connect() as c:
        return str(
            (
                await c.execute(sa.text("SELECT xmin::text FROM raw_items WHERE id = :i"), {"i": raw_item_id})
            ).scalar_one()
        )


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
    counts = await resolve_run_items(engine, owner, clock=utcnow)
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
    untouched = {item: await row_version(engine, item) for item in (held, still)}
    assert await resolve_deferred_items(engine, later) == {}  # both still fenced: not selected
    assert {item: await row_version(engine, item) for item in (held, still)} == untouched
    async with engine.begin() as c:
        await c.execute(RELEASE, {"f": h_fence})
    assert await resolve_deferred_items(engine, later) == {"linked": 1}
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
    assert await row_version(engine, still) == untouched[still]  # and is never touched
    assert await item_row(engine, adzuna) == ("out_of_scope", "source_purged", False, None)  # never deferred
    # the limit bounds a pass; items of the run itself are not "earlier"
    assert await resolve_deferred_items(engine, later, limit=0) == {}
    async with engine.begin() as c:
        await c.execute(RELEASE, {"f": k_fence})
        await c.execute(RELEASE, {"f": source_fence})
    # node 6 of the later run counts the deferred pass apart from the run's own items (Minor 5)
    node = make_resolve_identity(make_deps(engine, lambda instance: pytest.fail("no adapter is used")))
    update = await node(initial_state(later.run_id, later.attempt_no))
    counters = update["counters"]
    assert (counters["deferred_relinked"], counters["deferred_still_fenced"], counters["jobs_created"]) == (
        1,
        0,
        0,
    )
    assert [counters[f"raw_items_{d}"] for d in ("linked", "out_of_scope", "rejected", "deferred")] == [
        0,
        0,
        0,
        0,
    ]
    relinked = await item_row(engine, still)
    assert relinked[:3] == ("linked", None, False) and relinked[3] is not None


async def test_a_fenced_backlog_larger_than_the_limit_never_starves_a_released_item(
    engine, current_epoch
) -> None:
    """Minor 1: the selection leaves the still-fenced items out before its `LIMIT`."""
    owner = await queued_run(engine)
    at = utcnow() - timedelta(hours=6)
    for native_id, hours in (("F", 0), ("R", 1)):  # F's job stays fenced, R's is released
        item = await pending_item(
            engine, owner, "jsearch", {"native_id": native_id}, fetched_at=at + timedelta(hours=hours)
        )
        assert await resolved(engine, owner, item) == "linked"
    job_f, job_r = (
        (await jobs_of_native(engine, "F"))["jsearch"],
        (await jobs_of_native(engine, "R"))["jsearch"],
    )
    await fence(engine, "jobs", jobs=(job_f,))
    r_fence = await fence(engine, "jobs", jobs=(job_r,))
    backlog = [  # three fenced items, older than the released one, more than the pass's limit of 2
        await pending_item(
            engine, owner, "serpapi", {"native_id": "F"}, fetched_at=at + timedelta(hours=2 + n)
        )
        for n in range(3)
    ]
    released = await pending_item(
        engine, owner, "serpapi", {"native_id": "R"}, fetched_at=at + timedelta(hours=5)
    )
    assert await resolve_run_items(engine, owner, clock=utcnow) == {"deferred": 4}
    async with engine.begin() as c:
        await finalize_run(c, owner, status="succeeded", reason=None, report={})
        await c.execute(RELEASE, {"f": r_fence})
    later = await queued_run(engine)
    versions = {item: await row_version(engine, item) for item in backlog}
    assert await resolve_deferred_items(engine, later, limit=2) == {"linked": 1}
    assert (await item_row(engine, released))[0] == "linked"
    assert await jobs_of_native(engine, "R") == {"jsearch": job_r, "serpapi": job_r}
    for item in backlog:
        assert await item_row(engine, item) == ("deferred", "job_fenced", True, None)
    assert {item: await row_version(engine, item) for item in backlog} == versions


# --- Final wave F5 (ledger L294): the SQL selection of released items follows the Python job set ------------


async def test_the_released_selection_follows_the_posting_branch(engine, current_epoch) -> None:
    """An item whose only path to a fenced job is its posting (the same instance and native id, no key) is
    deferred by the Python job set and left out of the selection until the fence is released."""
    owner = await queued_run(engine)
    at = utcnow() - timedelta(hours=6)
    first = await pending_item(engine, owner, "jsearch", {"native_id": "P"}, fetched_at=at)
    assert await resolved(engine, owner, first) == "linked"
    job = (await jobs_of_native(engine, "P"))["jsearch"]
    released = await fence(engine, "jobs", jobs=(job,))
    keyless = await pending_item(
        engine, owner, "jsearch", {"native_id": "P", "raw_keys": ()}, fetched_at=at + timedelta(hours=1)
    )
    assert await resolve_run_items(engine, owner, clock=utcnow) == {"deferred": 1}
    assert await item_row(engine, keyless) == ("deferred", "job_fenced", True, None)
    async with engine.begin() as c:
        await finalize_run(c, owner, status="succeeded", reason=None, report={})
    later = await queued_run(engine)
    version = await row_version(engine, keyless)
    assert await resolve_deferred_items(engine, later) == {}  # not selected: the row is untouched
    assert await row_version(engine, keyless) == version
    async with engine.begin() as c:
        await c.execute(RELEASE, {"f": released})
    assert await resolve_deferred_items(engine, later) == {"linked": 1}
    assert (await item_row(engine, keyless))[0] == "linked"


ROOT_OF = sa.text("UPDATE jobs SET merged_into_job_id = :root WHERE id = :job")


async def test_the_released_selection_follows_a_key_to_its_merged_root(engine, current_epoch) -> None:
    """An item whose key's owner is merged into a fenced root (the key read before the merge moved it, a
    state made by hand here) is deferred by the Python job set, which follows the owner to its root, and the
    selection follows it to that root as well: the item waits for the root's fence, not the owner's."""
    owner = await queued_run(engine)
    at = utcnow() - timedelta(hours=6)
    for source, native_id, hours in (("jsearch", "M", 0), ("adzuna", "9", 1)):
        item = await pending_item(
            engine, owner, source, {"native_id": native_id}, fetched_at=at + timedelta(hours=hours)
        )
        assert await resolved(engine, owner, item) == "linked"
    merged, root = (
        (await jobs_of_native(engine, "M"))["jsearch"],
        (await jobs_of_native(engine, "9"))["adzuna"],
    )
    async with engine.begin() as c:
        await c.execute(ROOT_OF, {"root": root, "job": merged})  # the key `htidocid:M` stays on `merged`
    released = await fence(engine, "jobs", jobs=(root,))  # the root only: `merged` itself is not fenced
    async with engine.connect() as c:
        fenced = sa.text("SELECT is_fenced(:j)")
        assert [(await c.execute(fenced, {"j": j})).scalar_one() for j in (merged, root)] == [False, True]
    held = await pending_item(
        engine, owner, "serpapi", {"native_id": "M"}, fetched_at=at + timedelta(hours=2)
    )  # its one key, `htidocid:M`, is owned by `merged`
    assert await resolve_run_items(engine, owner, clock=utcnow) == {"deferred": 1}
    async with engine.begin() as c:
        await finalize_run(c, owner, status="succeeded", reason=None, report={})
    later = await queued_run(engine)
    version = await row_version(engine, held)
    assert await resolve_deferred_items(engine, later) == {}
    assert await row_version(engine, held) == version
    async with engine.begin() as c:
        await c.execute(RELEASE, {"f": released})
    assert await resolve_deferred_items(engine, later) == {"linked": 1}
    assert await jobs_of_native(engine, "M") == {"jsearch": merged, "serpapi": root}
