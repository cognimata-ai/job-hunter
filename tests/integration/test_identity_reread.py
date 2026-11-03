"""Identity's read-then-lock round re-reads the posting's job and the key owners under the lock (spec §8.6
steps 2 and 3, §8.6.2; plan M4 Task 8, M1 Carry-forward 10).

An unmerge moves postings and re-owns keys without merging any job of the round's set `J`: the jobs the
round read stay unmerged, so only a re-read of `P`'s job and of the owners of `K` sees it. A round whose
re-read differs from its read is rolled back to its savepoint and the next round reads afresh, so the item
is linked to the restored job (with the round's lock on the job it read released), never refused for a
posting that is no longer on the decided job, nor linked to a job that no longer owns its key.

Every datum is invented.
"""

import asyncio
from datetime import timedelta
from typing import Any, Final

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.dedup import identity, merge
from jobhunter.normalize.keys import AdKey
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import STEPSTONE_1, job_lockable, key_pair, pending_item, queued_run, resolved
from tests.support.faults import PausePoints

pytestmark = pytest.mark.integration

WAIT_S: Final = 10.0
EVENTS: Final = sa.text("SELECT id, kind, from_job_id, into_job_id FROM merge_events ORDER BY id")
OBSERVED: Final = sa.text(
    "SELECT p.id AS posting_id, p.job_id FROM posting_observations o JOIN postings p ON p.id = o.posting_id "
    "WHERE o.raw_item_id = :i"
)
OWNER_OF: Final = sa.text("SELECT job_id FROM ad_keys WHERE key_ns = :ns AND key_value = :v")


async def rows(engine: AsyncEngine, statement: sa.TextClause, **params: Any) -> list[Any]:
    async with engine.connect() as c:
        return list((await c.execute(statement, params)).all())


@pytest.mark.parametrize("sighting", ["the_posting", "a_new_posting_of_its_key"])
async def test_the_reread_round_rereads_the_posting_job_and_key_owners(
    engine: AsyncEngine, current_epoch: int, monkeypatch: pytest.MonkeyPatch, sighting: str
) -> None:
    owner = await queued_run(engine)
    at = utcnow() - timedelta(days=1)
    pair = await key_pair(engine, owner, at=at)
    merging = await pending_item(
        engine,
        owner,
        "adzuna",
        {"native_id": "9", "primary_link": STEPSTONE_1},
        fetched_at=at + timedelta(hours=2),
    )
    assert await resolved(engine, owner, merging) == "linked"
    [event] = await rows(engine, EVENTS)
    assert (event.kind, event.from_job_id, event.into_job_id) == ("strong_key_merge", pair.b, pair.a)
    # P (Adzuna 9) is on A now, and its key `adzuna:9` is A's
    assert [tuple(r) for r in await rows(engine, OWNER_OF, ns="adzuna", v="9")] == [(pair.a,)]
    fields: dict[str, Any]
    if sighting == "the_posting":  # P sighted again: its job is read
        source, fields = "adzuna", {"native_id": "9"}
    else:  # a new posting carrying P's key: the key's owner is read
        source, fields = "arbeitnow", {"native_id": "N", "raw_keys": (AdKey("adzuna", "9"),)}
    item = await pending_item(engine, owner, source, fields, fetched_at=at + timedelta(hours=3))

    calls: list[list[int]] = []
    real_lock = merge.lock_jobs

    async def recording(conn: Any, job_ids: Any, *, with_groups_of: Any = ()) -> dict[int, int | None]:
        calls.append(sorted(job_ids))
        return await real_lock(conn, job_ids, with_groups_of=with_groups_of)

    monkeypatch.setattr(merge, "lock_jobs", recording)
    pauses = PausePoints(identity.BEFORE_JOB_LOCK, identity.IN_ITEM_TX)
    monkeypatch.setattr(identity, "afault_point", pauses)
    resolving = asyncio.create_task(resolved(engine, owner, item))
    await asyncio.wait_for(pauses[identity.BEFORE_JOB_LOCK].reached.wait(), WAIT_S)
    # between the read (J = {A}) and the lock, the merge is undone: P goes back to B, and B owns `adzuna:9`
    # again; A stays unmerged, so the round's own checks on J pass
    async with engine.begin() as conn:
        await merge.unmerge(conn, event.id)
    assert [tuple(r) for r in await rows(engine, OWNER_OF, ns="adzuna", v="9")] == [(pair.b,)]
    pauses[identity.BEFORE_JOB_LOCK].resume()
    await asyncio.wait_for(pauses[identity.IN_ITEM_TX].reached.wait(), WAIT_S)
    # the first round's lock on A went with its savepoint; the second round holds B
    assert [await job_lockable(engine, job) for job in (pair.a, pair.b)] == [True, False]
    pauses[identity.IN_ITEM_TX].resume()
    assert await asyncio.wait_for(resolving, WAIT_S) == "linked"
    assert calls[-2:] == [[pair.a], [pair.b]]  # the re-read sent a fresh round to the restored job
    [observed] = await rows(engine, OBSERVED, i=item)
    assert observed.job_id == pair.b
    if sighting == "the_posting":
        assert observed.posting_id == pair.posting_b
    assert len(await rows(engine, EVENTS)) == 2  # the merge and its unmerge: no merge since
    assert [tuple(r) for r in await rows(engine, OWNER_OF, ns="adzuna", v="9")] == [(pair.b,)]
