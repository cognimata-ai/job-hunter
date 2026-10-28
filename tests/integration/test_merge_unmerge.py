import asyncio
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db import ranking
from jobhunter.dedup import identity, merge
from jobhunter.dedup.merge import merge_jobs, resolve_conflict, unmerge
from jobhunter.errors import FencedOut, InvariantViolation
from jobhunter.normalize.keys import AdKey
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import (
    STEPSTONE_1,
    STEPSTONE_2,
    job_lockable,
    key_pair,
    pending_item,
    queued_run,
    resolved,
)
from tests.support.faults import PausePoint
from tests.support.groups import BERLIN
from tests.support.m3 import insert_job
from tests.support.pg import PgServer, lock_wait_details
from tests.support.procs import await_until

pytestmark = pytest.mark.integration

KEYS = sa.text("SELECT key_ns, key_value, job_id FROM ad_keys ORDER BY key_ns, key_value")
EVENTS = sa.text(
    "SELECT id, kind, from_job_id, into_job_id, posting_ids, evidence, reversed_by_event_id "
    "FROM merge_events "
    "ORDER BY id"
)
CONFLICTS = sa.text(
    "SELECT id, status, posting_id, posting_job_id, candidate_job_ids, resolved_at FROM identity_conflicts "
    "ORDER BY id"
)
ALERT = sa.text("SELECT severity, resolved_at FROM alerts WHERE dedup_key = :k ORDER BY id")
POSTING_OF_ITEM = sa.text(
    "SELECT o.posting_id, p.job_id FROM posting_observations o JOIN postings p ON p.id = o.posting_id "
    "WHERE o.raw_item_id = :i"
)
GROUP = sa.text(
    "INSERT INTO duplicate_groups (representative_job_id, status, election_reason) "
    "VALUES (:rep, 'active', 'initial') RETURNING id"
)
MEMBER = sa.text("INSERT INTO duplicate_group_members (group_id, job_id, evidence) VALUES (:g, :j, '{}')")
IN_GROUP = sa.text("UPDATE jobs SET duplicate_group_id = :g WHERE id = :j")


async def fetch(engine: AsyncEngine, statement: sa.TextClause, **params: Any) -> list[Any]:
    async with engine.connect() as c:
        return list((await c.execute(statement, params)).all())


async def scalar(engine: AsyncEngine, statement: str, **params: Any) -> Any:
    async with engine.connect() as c:
        return (await c.execute(sa.text(statement), params)).scalar_one()


async def generation(engine: AsyncEngine) -> int:
    return int(await scalar(engine, "SELECT generation FROM ranking_state WHERE id = 1"))


async def owners(engine: AsyncEngine) -> dict[tuple[str, str], int]:
    return {(r.key_ns, r.key_value): r.job_id for r in await fetch(engine, KEYS)}


async def job_of(engine: AsyncEngine, raw_item_id: int) -> tuple[int, int]:
    """The posting and the job of the observation the raw item became."""
    [(posting, job)] = await fetch(engine, POSTING_OF_ITEM, i=raw_item_id)
    return posting, job


async def test_unmerge_restores_postings_keys_and_blocks_a_remerge(engine, current_epoch) -> None:
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
    [event] = await fetch(engine, EVENTS)
    assert (event.kind, event.from_job_id, event.into_job_id) == ("strong_key_merge", pair.b, pair.a)
    assert set((await owners(engine)).values()) == {pair.a}
    gen = await generation(engine)

    async with engine.begin() as c:
        reversal = await unmerge(c, event.id)
    assert await scalar(engine, "SELECT merged_into_job_id FROM jobs WHERE id = :b", b=pair.b) is None
    assert await scalar(engine, "SELECT job_id FROM postings WHERE id = :p", p=pair.posting_b) == pair.b
    assert await scalar(engine, "SELECT job_id FROM postings WHERE id = :p", p=pair.posting_a) == pair.a
    # ownership by the earliest provenance row among the two jobs' postings: stepstone 1 was A's first
    assert await owners(engine) == {
        ("adzuna", "9"): pair.b,
        ("htidocid", "H"): pair.a,
        ("pub:stepstone.de", "1"): pair.a,
    }
    assert (
        await scalar(engine, "SELECT count(*) FROM ad_key_sources WHERE posting_id = :p", p=pair.posting_b)
        == 2
    )
    assert await fetch(engine, sa.text("SELECT job_a, job_b, decision, source FROM dedup_decisions")) == [
        (pair.a, pair.b, "different", "unmerge")
    ]
    original, back = await fetch(engine, EVENTS)
    assert original.reversed_by_event_id == reversal == back.id
    assert (back.kind, back.from_job_id, back.into_job_id, back.posting_ids, back.reversed_by_event_id) == (
        "unmerge",
        pair.b,
        pair.a,
        [pair.posting_b],
        None,
    )
    assert back.evidence["reverses_event_id"] == event.id
    # both jobs recomputed over their own postings, with their projections, and the list bumped once
    rows = {
        r.id: r
        for r in await fetch(
            engine, sa.text("SELECT id, source_ids, first_seen_at, last_seen_at, availability FROM jobs")
        )
    }
    assert (rows[pair.a].source_ids, rows[pair.a].last_seen_at) == (["jsearch"], at)
    assert (rows[pair.b].source_ids, rows[pair.b].first_seen_at, rows[pair.b].last_seen_at) == (
        ["adzuna"],
        at + timedelta(hours=1),
        at + timedelta(hours=2),
    )
    assert await scalar(engine, "SELECT count(*) FROM hard_filter_results WHERE job_id = :b", b=pair.b) == 1
    assert await generation(engine) == gen + 1

    # a new sighting carrying both keys creates no merge: the different decision keeps them apart
    again = await pending_item(
        engine,
        owner,
        "adzuna",
        {"native_id": "9", "primary_link": STEPSTONE_1},
        fetched_at=at + timedelta(hours=3),
    )
    assert await resolved(engine, owner, again) == "linked"
    assert len(await fetch(engine, EVENTS)) == 2
    assert await job_of(engine, again) == (pair.posting_b, pair.b)
    # an event is reversed once, and an unmerge event is not a merge
    with pytest.raises(InvariantViolation):
        async with engine.begin() as c:
            await unmerge(c, event.id)
    with pytest.raises(InvariantViolation):
        async with engine.begin() as c:
            await unmerge(c, reversal)


async def test_resolve_conflict_separate_and_merge(engine, current_epoch) -> None:
    owner = await queued_run(engine)
    at = utcnow() - timedelta(days=1)
    pair = await key_pair(engine, owner, at=at)
    serp = await pending_item(
        engine,
        owner,
        "serpapi",
        {"native_id": "H", "primary_link": STEPSTONE_2},
        fetched_at=at + timedelta(hours=2),
    )
    assert await resolved(engine, owner, serp) == "linked"
    _, x = await job_of(engine, serp)
    [first] = await fetch(engine, CONFLICTS)
    gen = await generation(engine)

    async with engine.begin() as c:
        await resolve_conflict(c, first.id, "separate")
    [separated] = await fetch(engine, CONFLICTS)
    assert (separated.status, separated.resolved_at is not None) == ("resolved_separate", True)
    assert await fetch(engine, sa.text("SELECT job_a, job_b, decision, source FROM dedup_decisions")) == [
        (min(pair.a, x), max(pair.a, x), "different", "conflict_resolution")
    ]
    [(_, resolved_at)] = await fetch(engine, ALERT, k=f"dedup.identity_conflict:conflict:{first.id}")
    assert resolved_at is not None
    assert await generation(engine) == gen  # nothing in the list changed
    # the posting sighted again: the decision stands, no new conflict
    again = await pending_item(
        engine,
        owner,
        "serpapi",
        {"native_id": "H", "primary_link": STEPSTONE_2},
        fetched_at=at + timedelta(hours=3),
    )
    assert await resolved(engine, owner, again) == "linked"
    assert len(await fetch(engine, CONFLICTS)) == 1 and (await job_of(engine, again))[1] == x

    # a second conflict (htidocid H with stepstone 3), resolved as a merge into the posting's job
    third = await pending_item(
        engine,
        owner,
        "arbeitnow",
        {"raw_keys": (AdKey("htidocid", "H"), AdKey("pub:stepstone.de", "3"))},
        fetched_at=at + timedelta(hours=4),
    )
    assert await resolved(engine, owner, third) == "linked"
    _, y = await job_of(engine, third)
    second = (await fetch(engine, CONFLICTS))[1]
    assert (second.status, second.posting_job_id, second.candidate_job_ids) == ("open", y, [pair.a])
    gen = await generation(engine)
    async with engine.begin() as c:
        await resolve_conflict(c, second.id, "merge")
    merged = (await fetch(engine, CONFLICTS))[1]
    assert merged.status == "resolved_merged" and merged.resolved_at is not None
    assert (await fetch(engine, CONFLICTS))[0].status == "resolved_separate"  # history is not rewritten
    [event] = [e for e in await fetch(engine, EVENTS) if e.kind == "user_confirmed_merge"]
    assert (event.from_job_id, event.into_job_id, event.posting_ids) == (pair.a, y, [pair.posting_a])
    assert event.evidence["conflict_id"] == second.id
    assert await scalar(engine, "SELECT merged_into_job_id FROM jobs WHERE id = :a", a=pair.a) == y
    keys = await owners(engine)
    assert keys[("htidocid", "H")] == keys[("pub:stepstone.de", "1")] == keys[("pub:stepstone.de", "3")] == y
    assert keys[("pub:stepstone.de", "2")] == x and keys[("adzuna", "9")] == pair.b
    assert await scalar(engine, "SELECT source_ids FROM jobs WHERE id = :y", y=y) == ["arbeitnow", "jsearch"]
    assert await scalar(engine, "SELECT count(*) FROM hard_filter_results WHERE job_id = :y", y=y) == 1
    [(_, resolved_at)] = await fetch(engine, ALERT, k=f"dedup.identity_conflict:conflict:{second.id}")
    assert resolved_at is not None and await generation(engine) == gen + 1
    with pytest.raises(InvariantViolation):  # a resolved conflict stays resolved
        async with engine.begin() as c:
            await resolve_conflict(c, second.id, "separate")


async def test_the_merged_jobs_field_conflict_alert_is_resolved_and_reopens_on_unmerge(
    engine, current_epoch
) -> None:
    """Carry 7: `merge_jobs` resolves the from-job's open `dedup.field_conflict` alerts."""
    owner = await queued_run(engine)
    at = utcnow() - timedelta(days=1)
    remote = {"work_mode_possible": frozenset({"remote"}), "work_mode_source": "structured"}
    onsite = {"work_mode_possible": frozenset({"onsite"}), "work_mode_source": "structured"}
    items = [
        await pending_item(engine, owner, "jsearch", {"native_id": "HA"}, fetched_at=at),
        await pending_item(
            engine, owner, "jsearch", {"native_id": "HB", **remote}, fetched_at=at + timedelta(hours=1)
        ),
        await pending_item(
            engine, owner, "serpapi", {"native_id": "HB", **onsite}, fetched_at=at + timedelta(hours=2)
        ),
    ]
    for item in items:
        assert await resolved(engine, owner, item) == "linked"
    (_, a), (_, b) = await job_of(engine, items[0]), await job_of(engine, items[1])
    assert (await job_of(engine, items[2]))[1] == b and a < b
    key_a, key_b = (f"dedup.field_conflict:job:{job}:work_mode_possible" for job in (a, b))
    assert await fetch(engine, ALERT, k=key_b) == [("info", None)]

    link = await pending_item(
        engine,
        owner,
        "arbeitnow",
        {"raw_keys": (AdKey("htidocid", "HA"), AdKey("htidocid", "HB"))},
        fetched_at=at + timedelta(hours=3),
    )
    assert await resolved(engine, owner, link) == "linked"
    [event] = await fetch(engine, EVENTS)
    assert (event.from_job_id, event.into_job_id) == (b, a)
    [(_, b_resolved)] = await fetch(engine, ALERT, k=key_b)
    assert b_resolved is not None  # the merged-away job's selection no longer stands
    assert await fetch(engine, ALERT, k=key_a) == [("info", None)]  # A carries the conflict now

    async with engine.begin() as c:
        await unmerge(c, event.id)
    reopened = await fetch(engine, ALERT, k=key_b)
    assert len(reopened) == 2 and reopened[1] == ("info", None)  # B conflicts again on its own
    [(_, a_resolved)] = await fetch(engine, ALERT, k=key_a)
    assert a_resolved is not None


async def test_merge_takes_the_from_job_out_of_its_group_and_refuses_fenced_jobs(
    engine, current_epoch
) -> None:
    owner = await queued_run(engine)
    at = utcnow() - timedelta(days=3)
    names = ("A", "B", "C", "D", "E", "F")
    jobs: dict[str, int] = {}
    # one place for all (G2), so the members confirm against each new representative (Task 20)
    berlin = {"locations": [BERLIN], "location_kind": "city"}
    for hours, name in enumerate(names):
        item = await pending_item(
            engine,
            owner,
            "jsearch",
            {"native_id": f"G{name}", **berlin},
            fetched_at=at + timedelta(hours=hours),
        )
        assert await resolved(engine, owner, item) == "linked"
        jobs[name] = (await job_of(engine, item))[1]
    async with engine.begin() as c:
        big = (await c.execute(GROUP, {"rep": jobs["B"]})).scalar_one()
        small = (await c.execute(GROUP, {"rep": jobs["E"]})).scalar_one()
        for group, members in ((big, "BCD"), (small, "EF")):
            for name in members:
                await c.execute(MEMBER, {"g": group, "j": jobs[name]})
                await c.execute(IN_GROUP, {"g": group, "j": jobs[name]})

    async with engine.begin() as c:
        event = await merge_jobs(
            c,
            from_job_id=jobs["B"],
            into_job_id=jobs["A"],
            kind="user_confirmed_merge",
            evidence={},
        )
        await merge_jobs(
            c,
            from_job_id=jobs["E"],
            into_job_id=jobs["A"],
            kind="user_confirmed_merge",
            evidence={},
        )
        await ranking.bump(c)
    [snapshot] = await fetch(engine, sa.text("SELECT evidence FROM merge_events WHERE id = :e"), e=event)
    [membership] = snapshot.evidence["snapshot"]["group_memberships"]
    assert (membership["group_id"], membership["job_id"], membership["representative_job_id"]) == (
        big,
        jobs["B"],
        jobs["B"],
    )
    assert snapshot.evidence["snapshot"]["ad_keys"] == [
        {"key_ns": "htidocid", "key_value": "GB", "exclusive": False}
    ]
    # neither job has user actions or a target (Task 22)
    assert snapshot.evidence["snapshot"]["user_job_actions"] == []
    assert snapshot.evidence["snapshot"]["non_terminal_target_ids"] == []
    # the representative left: the group elects the next member (C, first seen before D)
    groups = {
        r.id: r
        for r in await fetch(
            engine,
            sa.text(
                "SELECT id, status, representative_job_id, unscorable_reason, election_reason "
                "FROM duplicate_groups"
            ),
        )
    }
    assert (groups[big].status, groups[big].representative_job_id, groups[big].election_reason) == (
        "active",
        jobs["C"],
        "member_left",
    )
    memberships = await fetch(
        engine, sa.text("SELECT group_id, job_id FROM duplicate_group_members ORDER BY job_id")
    )
    assert [tuple(m) for m in memberships] == [(big, jobs["C"]), (big, jobs["D"])]
    # a group left with one member is dissolved, and that member leaves it
    assert (groups[small].status, groups[small].election_reason) == ("dissolved", "member_left")
    assert (
        await scalar(
            engine,
            "SELECT count(*) FROM jobs "
            "WHERE duplicate_group_id = :g OR (id = ANY(:ids) AND duplicate_group_id IS NOT NULL)",
            g=small,
            ids=[jobs["B"], jobs["E"], jobs["F"]],
        )
        == 0
    )

    async with engine.begin() as c:
        await c.execute(
            sa.text(
                "INSERT INTO purge_fences (kind, job_ids, manifest_id) "
                "VALUES ('jobs', ARRAY[:j]::bigint[], 1)"
            ),
            {"j": jobs["D"]},
        )
    with pytest.raises(FencedOut):
        async with engine.begin() as c:
            await merge_jobs(
                c,
                from_job_id=jobs["D"],
                into_job_id=jobs["A"],
                kind="user_confirmed_merge",
                evidence={},
            )
    with pytest.raises(InvariantViolation):  # a merged job is never merged again
        async with engine.begin() as c:
            await merge_jobs(
                c,
                from_job_id=jobs["B"],
                into_job_id=jobs["C"],
                kind="user_confirmed_merge",
                evidence={},
            )


async def jsearch_jobs(
    engine: AsyncEngine, owner: Any, at: Any, *native_ids: str, **overrides: Any
) -> list[int]:
    """One JSearch job per native id, resolved in that order (so their ids ascend), an hour apart."""
    jobs: list[int] = []
    for hours, native_id in enumerate(native_ids):
        item = await pending_item(
            engine,
            owner,
            "jsearch",
            {"native_id": native_id, **overrides},
            fetched_at=at + timedelta(hours=hours),
        )
        assert await resolved(engine, owner, item) == "linked"
        jobs.append((await job_of(engine, item))[1])
    return jobs


async def group_of(engine: AsyncEngine, representative: int, members: tuple[int, ...]) -> int:
    async with engine.begin() as c:
        group = int((await c.execute(GROUP, {"rep": representative})).scalar_one())
        for job in members:
            await c.execute(MEMBER, {"g": group, "j": job})
            await c.execute(IN_GROUP, {"g": group, "j": job})
    return group


async def test_step_5_takes_a_lower_id_member_in_the_merge_jobs_one_lock_statement(
    engine, current_epoch, monkeypatch
) -> None:
    """Ruling T18-c: the member a dissolving group leaves behind has a lower id than both merge jobs. The
    recorded `lock_jobs` call carries the claim: one statement locked the member with the two jobs, in
    ascending id order (the member first), and `lock_jobs` ran once. The NOWAIT probe at the
    item's crash point shows less: that the identity transaction holds the member's row by then, and no
    unrelated job's; not when or by which statement the row was locked."""
    owner = await queued_run(engine)
    at = utcnow() - timedelta(hours=8)
    member, a, b, unrelated = await jsearch_jobs(engine, owner, at, "GM", "HA", "HB", "GU")
    assert member < a < b
    group = await group_of(engine, b, (b, member))
    calls: list[tuple[list[int], list[int], list[int]]] = []
    real_lock = merge.lock_jobs

    async def recording(conn, job_ids, *, with_groups_of=()):
        locked = await real_lock(conn, job_ids, with_groups_of=with_groups_of)
        calls.append((sorted(job_ids), sorted(with_groups_of), sorted(locked)))
        return locked

    monkeypatch.setattr(merge, "lock_jobs", recording)
    pause = PausePoint(identity.IN_ITEM_TX)
    monkeypatch.setattr(identity, "afault_point", pause)
    linking = await pending_item(
        engine,
        owner,
        "arbeitnow",
        {"raw_keys": (AdKey("htidocid", "HA"), AdKey("htidocid", "HB"))},
        fetched_at=at + timedelta(hours=5),
    )
    resolving = asyncio.create_task(resolved(engine, owner, linking))
    await asyncio.wait_for(pause.reached.wait(), 10)
    # by the crash point the identity transaction holds the member's row, and only the rows it needs
    assert [await job_lockable(engine, job) for job in (member, a, b, unrelated)] == [
        False,
        False,
        False,
        True,
    ]
    pause.resume()
    assert await asyncio.wait_for(resolving, 10) == "linked"
    # what proves the order: the one lock call, ascending, the member first
    assert calls == [([a, b], [b], [member, a, b])]
    assert await scalar(engine, "SELECT status FROM duplicate_groups WHERE id = :g", g=group) == "dissolved"
    assert await scalar(engine, "SELECT count(*) FROM duplicate_group_members") == 0
    assert await scalar(engine, "SELECT duplicate_group_id FROM jobs WHERE id = :m", m=member) is None

    # the assertion path: a caller that did not lock a member gets InvariantViolation, and nothing is written
    y, x = await jsearch_jobs(engine, owner, at + timedelta(hours=6), "GY", "GX")
    other = await group_of(engine, x, (x, y))
    with pytest.raises(InvariantViolation, match="not locked"):
        async with engine.begin() as c:
            await merge.lock_jobs(c, (x, a))
            await merge_jobs(
                c, from_job_id=x, into_job_id=a, kind="user_confirmed_merge", evidence={}, locked=(x, a)
            )
    assert await scalar(engine, "SELECT status FROM duplicate_groups WHERE id = :g", g=other) == "active"
    assert await scalar(engine, "SELECT merged_into_job_id FROM jobs WHERE id = :x", x=x) is None
    assert (
        await scalar(engine, "SELECT count(*) FROM duplicate_group_members WHERE group_id = :g", g=other) == 2
    )


async def test_resolve_conflict_judges_the_posting_jobs_pairs_and_merges_every_candidate(
    engine, current_epoch
) -> None:
    """Ruling T18-e and Minor 8: a conflict with two candidates."""
    owner = await queued_run(engine)
    at = utcnow() - timedelta(hours=8)
    a = (await jsearch_jobs(engine, owner, at, "H1", primary_link=STEPSTONE_1))[0]
    b = (await jsearch_jobs(engine, owner, at + timedelta(hours=1), "H2", primary_link=STEPSTONE_2))[0]
    both = {"raw_keys": (AdKey("htidocid", "H1"), AdKey("htidocid", "H2"))}
    first = await pending_item(engine, owner, "arbeitnow", both, fetched_at=at + timedelta(hours=2))
    assert await resolved(engine, owner, first) == "linked"
    _, x = await job_of(engine, first)
    [conflict] = await fetch(engine, CONFLICTS)
    assert (conflict.posting_job_id, conflict.candidate_job_ids) == (x, [a, b])
    async with engine.begin() as c:
        await resolve_conflict(c, conflict.id, "separate")
    # the user judged the posting's job against each candidate, not the candidates against each other
    assert await fetch(engine, sa.text("SELECT job_a, job_b FROM dedup_decisions ORDER BY job_a, job_b")) == [
        (a, x),
        (b, x),
    ]

    second = await pending_item(engine, owner, "arbeitnow", both, fetched_at=at + timedelta(hours=3))
    assert await resolved(engine, owner, second) == "linked"  # A and B are not judged apart: a new conflict
    _, y = await job_of(engine, second)
    later = (await fetch(engine, CONFLICTS))[1]
    assert (later.status, later.posting_job_id, later.candidate_job_ids) == ("open", y, [a, b])
    async with engine.begin() as c:
        await resolve_conflict(c, later.id, "merge")
    events = await fetch(engine, EVENTS)
    assert [(e.kind, e.from_job_id, e.into_job_id) for e in events] == [
        ("user_confirmed_merge", a, y),
        ("user_confirmed_merge", b, y),
    ]
    assert (await fetch(engine, CONFLICTS))[1].status == "resolved_merged"
    assert (await fetch(engine, CONFLICTS))[0].status == "resolved_separate"
    merged = await fetch(
        engine, sa.text("SELECT id, merged_into_job_id FROM jobs WHERE id = ANY(:ids)"), ids=[a, b]
    )
    assert {r.id: r.merged_into_job_id for r in merged} == {a: y, b: y}
    # every key moved to Y: X's posting supplied only keys A and B owned
    assert set((await owners(engine)).values()) == {y}


async def test_no_eligible_member_left_records_no_active_member(engine, current_epoch) -> None:
    """Minor 8: the representative leaves and every other member is closed."""
    owner = await queued_run(engine)
    at = utcnow() - timedelta(hours=8)
    a, rep, closed_one, closed_two = await jsearch_jobs(engine, owner, at, "NA", "NR", "N1", "N2")
    group = await group_of(engine, rep, (rep, closed_one, closed_two))
    async with engine.begin() as c:
        await c.execute(
            sa.text("UPDATE jobs SET availability = 'closed' WHERE id = ANY(:ids)"),
            {"ids": [closed_one, closed_two]},
        )
        await merge_jobs(c, from_job_id=rep, into_job_id=a, kind="user_confirmed_merge", evidence={})
        await ranking.bump(c)
    row = (
        await fetch(
            engine,
            sa.text(
                "SELECT status, representative_job_id, unscorable_reason, election_reason "
                "FROM duplicate_groups"
            ),
        )
    )[0]
    assert tuple(row) == ("active", None, "no_active_member", "member_left")
    assert (
        await scalar(engine, "SELECT count(*) FROM duplicate_group_members WHERE group_id = :g", g=group) == 2
    )


async def test_a_merge_never_touches_a_synthetic_golden_case_job(engine: AsyncEngine) -> None:
    """Task 19's synthetic jobs have no postings and never merge (Task 22): either side refused, nothing
    written."""
    async with engine.begin() as c:
        synthetic = await insert_job(c, synthetic_eval=True)
        real = await insert_job(c)
    for from_job, into_job in ((synthetic, real), (real, synthetic)):
        with pytest.raises(InvariantViolation, match="synthetic"):
            async with engine.begin() as c:
                await merge_jobs(
                    c,
                    from_job_id=from_job.job_id,
                    into_job_id=into_job.job_id,
                    kind="user_confirmed_merge",
                    evidence={},
                    recompute=False,
                )
    assert await scalar(engine, "SELECT count(*) FROM merge_events") == 0
    assert await scalar(engine, "SELECT count(*) FROM jobs WHERE merged_into_job_id IS NOT NULL") == 0


async def test_a_standalone_merge_takes_active_config_before_the_job_rows(
    engine: AsyncEngine, migrated: PgServer
) -> None:
    """Task 22 (Task 15's live hook, the score lane's step 2): a merge that locks its own jobs takes
    `active_config FOR SHARE` first, so under an activation's `FOR UPDATE` it waits holding no job row."""
    async with engine.begin() as c:
        source, into = await insert_job(c), await insert_job(c)

    async def merging() -> int:
        async with engine.begin() as c:
            return await merge_jobs(
                c,
                from_job_id=source.job_id,
                into_job_id=into.job_id,
                kind="user_confirmed_merge",
                evidence={},
                recompute=False,
            )

    async with engine.connect() as holder:
        await holder.execute(sa.text("BEGIN"))
        await holder.execute(sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE"))
        task = asyncio.create_task(merging())

        async def waiting() -> bool:
            return task.done() or any(w.relation == "active_config" for w in lock_wait_details(migrated))

        await await_until(waiting, 10, 0.02)
        assert not task.done()
        assert [await job_lockable(engine, job.job_id) for job in (source, into)] == [True, True]
        await holder.execute(sa.text("ROLLBACK"))
    assert await asyncio.wait_for(task, 10) > 0
    assert await scalar(engine, "SELECT merged_into_job_id FROM jobs WHERE id = :j", j=source.job_id) == (
        into.job_id
    )
