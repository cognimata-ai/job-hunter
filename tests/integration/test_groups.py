import asyncio
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db import ranking
from jobhunter.dedup import groups, merge
from jobhunter.dedup.groups import GroupEffect
from jobhunter.errors import FencedOut
from jobhunter.graphs.collect.nodes.resolve_identity import make_resolve_identity
from jobhunter.graphs.collect.state import initial_state
from jobhunter.observability.metrics import REGISTRY
from jobhunter.worker import main as worker_main
from jobhunter.worker import reconcilers as r
from jobhunter.worker.epoch import fence_epoch
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.collect import ctx, job_lockable, make_deps, pending_item, queued_run, supersede
from tests.support.groups import BERLIN, FULL_TEXT, canonical_job, varied
from tests.support.logs import logged
from tests.support.pg import PgServer, lock_waits, pg_one
from tests.support.procs import await_until

pytestmark = pytest.mark.integration

GROUPS = sa.text(
    "SELECT id, status, representative_job_id, unscorable_reason, election_reason FROM duplicate_groups "
    "ORDER BY id"
)
MEMBERS = sa.text("SELECT job_id FROM duplicate_group_members WHERE group_id = :g ORDER BY job_id")
DECISIONS = sa.text("SELECT job_a, job_b, decision, source FROM dedup_decisions ORDER BY job_a, job_b")
NOTHING = GroupEffect(group_id=None, joined=False, created=False, left=(), reelected=False, ambiguous=False)
PARTIAL = varied(30) + "…"  # 26 shingles, every one of them in FULL_TEXT's 56


async def fetch(engine: AsyncEngine, statement: sa.TextClause, **params: Any) -> list[Any]:
    async with engine.connect() as c:
        return list((await c.execute(statement, params)).all())


async def scalar(engine: AsyncEngine, statement: str, **params: Any) -> Any:
    async with engine.connect() as c:
        return (await c.execute(sa.text(statement), params)).scalar_one()


async def generation(engine: AsyncEngine) -> int:
    return int(await scalar(engine, "SELECT generation FROM ranking_state WHERE id = 1"))


async def epoch_1(conn: AsyncConnection) -> None:
    await fence_epoch(conn, 1)


async def regroup(engine: AsyncEngine, job_id: int) -> GroupEffect:
    async with engine.begin() as conn:
        return await groups.regroup_job(conn, job_id, guard=epoch_1)


async def group_row(engine: AsyncEngine, group_id: int) -> tuple[Any, ...]:
    [row] = [g for g in await fetch(engine, GROUPS) if g.id == group_id]
    return (row.status, row.representative_job_id, row.unscorable_reason, row.election_reason)


async def members(engine: AsyncEngine, group_id: int) -> list[int]:
    return [int(m) for (m,) in await fetch(engine, MEMBERS, g=group_id)]


async def group_of_job(engine: AsyncEngine, job_id: int) -> int | None:
    group = await scalar(engine, "SELECT duplicate_group_id FROM jobs WHERE id = :j", j=job_id)
    return None if group is None else int(group)


def jobs_maker(engine: AsyncEngine, job_with_postings: Any, start_days_ago: int = 2) -> Callable[..., Any]:
    """`await make(source_id, **normalized overrides)`: a canonical job, each an hour after the last."""
    at = utcnow() - timedelta(days=start_days_ago)
    made: list[int] = []

    async def make(source_id: str, **overrides: Any) -> int:
        job_id = await canonical_job(
            engine, job_with_postings, source_id, observed_at=at + timedelta(hours=len(made)), **overrides
        )
        made.append(job_id)
        return job_id

    return make


def waiting(server: PgServer, *locktypes: str) -> int:
    """Lock requests of these `pg_locks` types that wait right now."""
    row = pg_one(
        server,
        "superuser",
        "SELECT count(*) FROM pg_locks WHERE NOT granted AND locktype = ANY(%s)",
        list(locktypes),
    )
    return 0 if row is None else int(str(row[0]))


def repairs_total() -> float:
    return REGISTRY.get_sample_value("jobhunter_dedup_group_repairs_total") or 0.0


def advisory_held(server: PgServer) -> int:
    row = pg_one(server, "superuser", "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted")
    return 0 if row is None else int(str(row[0]))


async def test_group_formation_election_twin_and_member_left(
    engine, current_epoch, job_with_postings, caplog
) -> None:
    caplog.set_level(logging.INFO)
    make = jobs_maker(engine, job_with_postings)
    a = await make("adzuna", description_text=PARTIAL, description_completeness="partial")
    b = await make("adzuna", description_text=PARTIAL, description_completeness="partial")  # the re-ingest
    # A's text, then 40 words of its own: it confirms against A (26/26) but not against C (26/66)
    m = await make(
        "adzuna",
        description_text=varied(30) + " " + varied(40, 100) + "…",
        description_completeness="partial",
    )
    c = await make("greenhouse", description_text=FULL_TEXT)
    d = await make("greenhouse", title_raw="Junior Software Engineer", description_text=FULL_TEXT)
    start = await generation(engine)

    formed = await regroup(engine, a)
    group = formed.group_id
    assert group is not None and formed == GroupEffect(group, False, True, (), False, False)
    # A and B share everything but the first sighting: the earliest is the representative
    assert await members(engine, group) == [a, b] and await group_row(engine, group) == (
        "active",
        a,
        None,
        "initial",
    )
    assert [await group_of_job(engine, job) for job in (a, b, m)] == [group, group, None]
    assert await generation(engine) == start + 1
    # B is already in the right group: confirmed, nothing written
    assert await regroup(engine, b) == GroupEffect(group, False, False, (), False, False)
    assert await generation(engine) == start + 1

    # M is partial: it joins, and no election follows
    assert await regroup(engine, m) == GroupEffect(group, True, False, (), False, False)
    assert await group_row(engine, group) == ("active", a, None, "initial")
    # C, the full-text twin of the partial representative, joins and is elected; M no longer confirms
    assert await regroup(engine, c) == GroupEffect(group, True, False, (m,), True, False)
    assert await group_row(engine, group) == ("active", c, None, "fuller_member_joined")
    assert await members(engine, group) == [a, b, c] and await group_of_job(engine, m) is None
    [left] = logged(caplog, "duplicate_group_member_left")
    assert (left["group_id"], left["job_id"], left["reason"]) == (group, m, "member_left")
    evidence = await scalar(engine, "SELECT evidence FROM duplicate_group_members WHERE job_id = :j", j=c)
    assert evidence == {
        "against_job_id": a,
        "title_similarity": 1.0,
        "text_score": 1.0,
        "location_rule": "shared_place",
        "requisition_match": False,
    }
    # D's seniority differs (G3): no group; M finds none either
    assert await regroup(engine, d) == NOTHING and await regroup(engine, m) == NOTHING
    assert await generation(engine) == start + 3


async def test_an_ambiguous_job_joins_the_better_group(
    engine, current_epoch, job_with_postings, caplog
) -> None:
    caplog.set_level(logging.INFO)
    make = jobs_maker(engine, job_with_postings)
    first = [await make("adzuna", description_text=PARTIAL, description_completeness="partial") for _ in "xy"]
    # words 30 to 69: 26 of their 36 shingles are in FULL_TEXT (0.72), none of them in PARTIAL
    tail = varied(40, 30) + "…"
    second = [await make("jsearch", description_text=tail, description_completeness="partial") for _ in "xy"]
    one, two = (await regroup(engine, first[0])).group_id, (await regroup(engine, second[0])).group_id
    assert one is not None and two is not None and one != two
    a = await make("greenhouse", description_text=FULL_TEXT)
    effect = await regroup(engine, a)
    assert effect == GroupEffect(one, True, False, (), True, True)  # the better score; a full twin, elected
    [event] = logged(caplog, "dedup.ambiguous_grouping")
    assert (event["job_id"], event["group_ids"]) == (a, [one, two])
    evidence = await scalar(engine, "SELECT evidence FROM duplicate_group_members WHERE job_id = :j", j=a)
    assert evidence["ambiguous_group_ids"] == [one, two] and evidence["against_job_id"] == first[0]
    assert await members(engine, two) == sorted(second)  # groups are never merged together


async def test_split_confirm_dissolve_and_reconciler(
    engine, current_epoch, job_with_postings, queue_app
) -> None:
    make = jobs_maker(engine, job_with_postings)
    p, q, s, t = [await make("jsearch", description_text=FULL_TEXT) for _ in "pqst"]
    group = (await regroup(engine, p)).group_id
    assert group is not None
    assert [(await regroup(engine, job)).joined for job in (s, t)] == [True, True]
    assert await members(engine, group) == [p, q, s, t]

    async with engine.begin() as conn:
        await groups.split_member(conn, group, s)
    assert [tuple(d) for d in await fetch(engine, DECISIONS)] == [
        (p, s, "different", "user_split"),
        (q, s, "different", "user_split"),
        (s, t, "different", "user_split"),
    ]
    assert await members(engine, group) == [p, q, t] and await group_of_job(engine, s) is None
    assert await regroup(engine, s) == NOTHING  # never re-joins
    async with engine.begin() as conn:
        await groups.split_member(conn, group, p)  # the representative: the group re-elects
    assert await group_row(engine, group) == ("active", q, None, "split")
    assert await members(engine, group) == [q, t]
    async with engine.begin() as conn:
        await groups.split_member(conn, group, t)  # a group left with one member is dissolved
    assert await group_row(engine, group) == ("dissolved", None, None, "split")
    assert await members(engine, group) == [] and await group_of_job(engine, q) is None

    x, y = [await make("jsearch", description_text=FULL_TEXT, company_raw="Globex") for _ in "xy"]
    confirmed = (await regroup(engine, x)).group_id
    assert confirmed is not None
    async with engine.begin() as conn:
        assert await groups.confirm_group(conn, confirmed) == x
    events = await fetch(engine, sa.text("SELECT kind, from_job_id, into_job_id FROM merge_events"))
    assert [tuple(e) for e in events] == [("user_confirmed_merge", y, x)]
    assert (x, y, "same", "user_confirm") in [tuple(d) for d in await fetch(engine, DECISIONS)]
    assert await scalar(engine, "SELECT merged_into_job_id FROM jobs WHERE id = :j", j=y) == x
    assert (await group_row(engine, confirmed))[0] == "dissolved"

    u, v, w = [await make("jsearch", description_text=FULL_TEXT, company_raw="Initech") for _ in "uvw"]
    repaired = (await regroup(engine, u)).group_id
    assert repaired is not None and (await regroup(engine, w)).joined
    close = sa.text("UPDATE jobs SET availability = 'closed' WHERE id = ANY(:ids)")
    async with engine.begin() as conn:
        await conn.execute(close, {"ids": [u]})
    reconcile = r.REGISTRY["group_representatives"][1]
    assert r.REGISTRY["group_representatives"][0] == {"heavy"}
    assert "jobhunter.dedup.groups" in worker_main._REGISTERING_MODULES
    assert await reconcile(ctx(engine, queue_app, epoch=current_epoch + 1)) == 0  # a superseded worker
    assert await group_row(engine, repaired) == ("active", u, None, "initial")
    repairs = repairs_total()
    start = await generation(engine)
    assert await reconcile(ctx(engine, queue_app)) == 1  # the representative is closed: one election
    assert await group_row(engine, repaired) == ("active", v, None, "reconciler_repair")
    assert await reconcile(ctx(engine, queue_app)) == 0
    async with engine.begin() as conn:
        await conn.execute(close, {"ids": [v, w]})
    assert await reconcile(ctx(engine, queue_app)) == 1  # none eligible
    assert await group_row(engine, repaired) == ("active", None, "no_active_member", "reconciler_repair")
    assert await reconcile(ctx(engine, queue_app)) == 0 and await members(engine, repaired) == [u, v, w]
    assert repairs_total() == repairs + 2 and await generation(engine) == start + 2


async def test_the_reconciler_repairs_a_group_the_stand_in_left(
    engine, current_epoch, job_with_postings, queue_app
) -> None:
    """Task 18's stand-in elected without revalidating anyone: here a `no_active_member` group it left, whose
    members are eligible again (reopened since). The reconciler elects (`eligible_again`) and revalidates the
    members against the new representative, which the stand-in never did."""
    make = jobs_maker(engine, job_with_postings)
    u = await make("greenhouse", description_text=FULL_TEXT)
    v = await make("jsearch", description_text=varied(60, 200))  # never confirmed against U
    w = await make("jsearch", description_text=FULL_TEXT)
    async with engine.begin() as conn:
        group = (
            await conn.execute(
                sa.text(
                    "INSERT INTO duplicate_groups (representative_job_id, status, unscorable_reason, "
                    "election_reason) VALUES (NULL, 'active', 'no_active_member', 'member_left') RETURNING id"
                )
            )
        ).scalar_one()
        for job in (u, v, w):
            await conn.execute(
                sa.text(
                    "INSERT INTO duplicate_group_members (group_id, job_id, evidence) VALUES (:g, :j, '{}')"
                ),
                {"g": group, "j": job},
            )
            await conn.execute(
                sa.text("UPDATE jobs SET duplicate_group_id = :g WHERE id = :j"), {"g": group, "j": job}
            )
    reconcile = r.REGISTRY["group_representatives"][1]
    assert await reconcile(ctx(engine, queue_app)) == 1
    assert await group_row(engine, group) == ("active", u, None, "eligible_again")
    assert await members(engine, group) == [u, w] and await group_of_job(engine, v) is None
    assert await reconcile(ctx(engine, queue_app)) == 0


async def test_a_merge_leaves_the_group_and_revalidates_against_the_new_representative(
    engine, current_epoch, job_with_postings
) -> None:
    """The real step 5 (it replaced Task 18's stand-in): the representative merges away, B is elected, and M,
    which confirmed against A, does not confirm against B and leaves."""
    make = jobs_maker(engine, job_with_postings)
    a = await make("greenhouse", description_text=FULL_TEXT)
    b, c = [await make("jsearch", description_text=PARTIAL, description_completeness="partial") for _ in "bc"]
    # words 20 to 59: all in A's text, 6 of B's 26 shingles in it
    m = await make("adzuna", description_text=varied(40, 20) + "…", description_completeness="partial")
    x = await make("jsearch", company_raw="Other", description_text=varied(60, 300))
    group = (await regroup(engine, a)).group_id
    assert group is not None
    for job in (c, m):
        assert (await regroup(engine, job)).joined
    assert await members(engine, group) == [a, b, c, m]
    async with engine.begin() as conn:
        await merge.merge_jobs(conn, from_job_id=a, into_job_id=x, kind="user_confirmed_merge", evidence={})
        await ranking.bump(conn)
    assert await group_row(engine, group) == ("active", b, None, "member_left")
    assert await members(engine, group) == [b, c] and await group_of_job(engine, m) is None


async def test_regroup_serialises_per_company(engine, current_epoch, job_with_postings, migrated) -> None:
    make = jobs_maker(engine, job_with_postings)
    a, b, c = [await make("jsearch", description_text=FULL_TEXT) for _ in "abc"]
    async with engine.begin() as first:
        formed = await groups.regroup_job(first, a, guard=epoch_1)
        assert formed.created and advisory_held(migrated) == 1  # the company's lock
        second = asyncio.create_task(regroup(engine, c))

        async def held_back() -> bool:
            return second.done() or waiting(migrated, "advisory") > 0

        await await_until(held_back, 10)
        assert not second.done() and lock_waits(migrated) > 0  # the second waits for the company lock
    joined = await asyncio.wait_for(second, 10)
    # it saw the first one's group once that committed
    assert joined == GroupEffect(formed.group_id, True, False, (), False, False)
    assert formed.group_id is not None and await members(engine, formed.group_id) == [a, b, c]


async def test_a_merge_and_a_regroup_of_one_company_never_deadlock(
    engine, current_epoch, job_with_postings, migrated
) -> None:
    """Ruling T20-a: a merge's leave-group takes no advisory lock, so whichever comes second waits on job
    rows, never on the company lock while it holds job rows."""
    make = jobs_maker(engine, job_with_postings)
    a, b, c, d = [await make("jsearch", description_text=FULL_TEXT) for _ in "abcd"]
    e, f = [await make("jsearch", description_text=varied(60, 200)) for _ in "ef"]  # a pair of their own
    x, y = [await make("jsearch", company_raw="Other", description_text=varied(60, 300)) for _ in "xy"]
    group = (await regroup(engine, a)).group_id
    assert (
        group is not None and (await regroup(engine, c)).joined and await members(engine, group) == [a, b, c]
    )

    # the merge first: it holds B's group's rows, and no advisory lock
    async with engine.begin() as merging:
        await merge.merge_jobs(
            merging, from_job_id=b, into_job_id=x, kind="user_confirmed_merge", evidence={}
        )
        assert advisory_held(migrated) == 0
        regrouping = asyncio.create_task(regroup(engine, d))

        async def rows_wait() -> bool:
            return regrouping.done() or waiting(migrated, "transactionid", "tuple") > 0

        await await_until(rows_wait, 10)
        # the regroup holds the company lock and waits for a job row
        assert not regrouping.done() and waiting(migrated, "advisory") == 0 and advisory_held(migrated) == 1
        await ranking.bump(merging)
    assert await asyncio.wait_for(regrouping, 10) == GroupEffect(group, True, False, (), False, False)

    # the regroup first: it holds the company lock and the rows of every candidate's group; the merge waits
    async with engine.begin() as regrouping_tx:
        paired = await groups.regroup_job(regrouping_tx, e, guard=epoch_1)
        assert paired.created and paired.group_id != group

        async def merge_c() -> None:
            async with engine.begin() as conn:
                await merge.merge_jobs(
                    conn, from_job_id=c, into_job_id=y, kind="user_confirmed_merge", evidence={}
                )
                await ranking.bump(conn)

        merging_task = asyncio.create_task(merge_c())

        async def merge_waits() -> bool:
            return merging_task.done() or waiting(migrated, "transactionid", "tuple") > 0

        await await_until(merge_waits, 10)
        assert not merging_task.done() and waiting(migrated, "advisory") == 0
    await asyncio.wait_for(merging_task, 10)
    assert await members(engine, group) == [a, d] and paired.group_id is not None
    assert await members(engine, paired.group_id) == [e, f]


async def test_the_node_groups_the_jobs_changed_in_the_run(
    engine, current_epoch, job_with_postings, queue_app
) -> None:
    make = jobs_maker(engine, job_with_postings, start_days_ago=3)
    old = await make("greenhouse", description_text=FULL_TEXT)
    # an old pair of another company, never grouped: no occurrence of theirs is of this run
    stale = [await make("greenhouse", company_raw="Elsewhere", description_text=FULL_TEXT) for _ in "xy"]
    owner = await queued_run(engine)
    at = utcnow() - timedelta(hours=2)
    same = {
        "title_raw": "Senior Software Engineer",
        "locations": [BERLIN],
        "location_kind": "city",
        "description_text": FULL_TEXT,
    }
    await pending_item(engine, owner, "jsearch", same, fetched_at=at)
    await pending_item(engine, owner, "arbeitnow", same, fetched_at=at + timedelta(minutes=5))
    node = make_resolve_identity(make_deps(engine, lambda instance: pytest.fail("no adapter is used")))
    update = await node(initial_state(owner.run_id, owner.attempt_no))
    counters = update["counters"]
    assert {k: counters[k] for k in groups.GROUPING_COUNTERS} == {
        "groups_formed": 1,  # the run's first job with the old one
        "groups_changed": 1,  # the second joined
        "elections": 0,
    }
    [group] = [g.id for g in await fetch(engine, GROUPS)]
    grouped = await members(engine, group)
    assert len(grouped) == 3 and grouped[0] == old
    assert [await group_of_job(engine, job) for job in stale] == [None, None]
    start = await generation(engine)
    again = await node(
        initial_state(owner.run_id, owner.attempt_no)
    )  # a resumed attempt re-runs it harmlessly
    assert {k: again["counters"][k] for k in groups.GROUPING_COUNTERS} == dict.fromkeys(
        groups.GROUPING_COUNTERS, 0
    )
    assert await generation(engine) == start and await members(engine, group) == grouped

    await supersede(engine, owner)  # the old attempt is fenced out after the job rows, having written nothing
    with pytest.raises(FencedOut):
        await groups.regroup_run_jobs(engine, owner)
    assert await generation(engine) == start


async def test_regroup_locks_the_job_and_every_member_in_one_statement_before_the_guard(
    engine, current_epoch, job_with_postings, monkeypatch
) -> None:
    """Carry 4: the job a regroup adds and the group's current members are locked by the one ordered
    statement, and the guard runs after it (Ruling T19-a)."""
    make = jobs_maker(engine, job_with_postings)
    a, b, c = [await make("jsearch", description_text=FULL_TEXT) for _ in "abc"]
    other = await make("jsearch", company_raw="Other", description_text=FULL_TEXT)
    group = (await regroup(engine, a)).group_id
    assert group is not None and await members(engine, group) == [a, b]
    calls: list[object] = []
    real_lock = merge.lock_jobs

    async def recording(conn, job_ids, *, with_groups_of=()):
        locked = await real_lock(conn, job_ids, with_groups_of=with_groups_of)
        calls.append(sorted(locked))
        return locked

    monkeypatch.setattr(merge, "lock_jobs", recording)
    reached, resume = asyncio.Event(), asyncio.Event()

    async def guard(conn: AsyncConnection) -> None:
        calls.append("guard")
        reached.set()
        await resume.wait()
        await fence_epoch(conn, 1)

    async def regroup_c() -> GroupEffect:
        async with engine.begin() as conn:
            return await groups.regroup_job(conn, c, guard=guard)

    task = asyncio.create_task(regroup_c())
    await asyncio.wait_for(reached.wait(), 10)
    # at the guard, the added job and both members are held, and nothing else
    assert [await job_lockable(engine, job) for job in (a, b, c, other)] == [False, False, False, True]
    resume.set()
    assert await asyncio.wait_for(task, 10) == GroupEffect(group, True, False, (), False, False)
    assert calls == [[a, b, c], "guard"]  # one ordered statement, ascending, then the guard


async def test_a_company_changed_before_the_lock_sends_a_fresh_round(
    engine, current_epoch, job_with_postings, migrated, monkeypatch
) -> None:
    """The round whose job changed company between its read and its lock is rolled back to its savepoint (its
    row locks and its company lock released), and the next round locks the new company's set."""
    make = jobs_maker(engine, job_with_postings)
    a, b = [await make("jsearch", description_text=FULL_TEXT) for _ in "ab"]
    x = await make("jsearch", company_raw="Other", description_text=FULL_TEXT)
    calls: list[list[int]] = []
    real_lock = merge.lock_jobs

    async def moving(conn, job_ids, *, with_groups_of=()):
        if not calls:  # between the first round's read and its lock, A becomes Other's
            async with engine.begin() as rival:
                await rival.execute(sa.text("UPDATE jobs SET company_key = 'other' WHERE id = :a"), {"a": a})
        locked = await real_lock(conn, job_ids, with_groups_of=with_groups_of)
        calls.append(sorted(locked))
        return locked

    monkeypatch.setattr(merge, "lock_jobs", moving)
    async with engine.begin() as conn:
        effect = await groups.regroup_job(conn, a, guard=epoch_1)
        assert advisory_held(migrated) == 1  # the first round's company lock went with its savepoint
        assert await job_lockable(engine, b)  # and so did its lock on B
    assert calls == [[a, b], [a, x]]
    assert effect.created and effect.group_id is not None and await members(engine, effect.group_id) == [a, x]


async def test_the_reconciler_locks_the_members_before_its_fence(
    engine, current_epoch, job_with_postings, queue_app, monkeypatch
) -> None:
    """Ruling T19-a: a reconciler pass that locks job rows fences after them."""
    make = jobs_maker(engine, job_with_postings)
    u, v = [await make("jsearch", description_text=FULL_TEXT) for _ in "uv"]
    group = (await regroup(engine, u)).group_id
    async with engine.begin() as conn:
        await conn.execute(sa.text("UPDATE jobs SET availability = 'closed' WHERE id = :u"), {"u": u})
    order: list[str] = []
    real_lock, real_fence = merge.lock_jobs, fence_epoch

    async def locking(conn, job_ids, *, with_groups_of=()):
        order.append("job rows")
        return await real_lock(conn, job_ids, with_groups_of=with_groups_of)

    async def fencing(conn, epoch):
        order.append("fence_epoch")
        await real_fence(conn, epoch)

    monkeypatch.setattr(merge, "lock_jobs", locking)
    monkeypatch.setattr(groups, "fence_epoch", fencing)
    assert await r.REGISTRY["group_representatives"][1](ctx(engine, queue_app)) == 1
    assert order == ["job rows", "fence_epoch"]
    assert group is not None and await group_row(engine, group) == ("active", v, None, "reconciler_repair")
