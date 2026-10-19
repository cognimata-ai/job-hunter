from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db import ranking
from jobhunter.dedup.merge import merge_jobs, resolve_conflict, unmerge
from jobhunter.errors import FencedOut, InvariantViolation
from jobhunter.normalize.keys import AdKey
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import (
    STEPSTONE_1,
    STEPSTONE_2,
    key_pair,
    pending_item,
    queued_run,
    resolved,
)

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
        reversal = await unmerge(c, event.id, now=utcnow())
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
            await unmerge(c, event.id, now=utcnow())
    with pytest.raises(InvariantViolation):
        async with engine.begin() as c:
            await unmerge(c, reversal, now=utcnow())


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
        await resolve_conflict(c, first.id, "separate", now=utcnow())
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
        await resolve_conflict(c, second.id, "merge", now=utcnow())
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
            await resolve_conflict(c, second.id, "separate", now=utcnow())


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
        await unmerge(c, event.id, now=utcnow())
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
    for hours, name in enumerate(names):
        item = await pending_item(
            engine, owner, "jsearch", {"native_id": f"G{name}"}, fetched_at=at + timedelta(hours=hours)
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
            now=utcnow(),
        )
        await merge_jobs(
            c,
            from_job_id=jobs["E"],
            into_job_id=jobs["A"],
            kind="user_confirmed_merge",
            evidence={},
            now=utcnow(),
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
                now=utcnow(),
            )
    with pytest.raises(InvariantViolation):  # a merged job is never merged again
        async with engine.begin() as c:
            await merge_jobs(
                c,
                from_job_id=jobs["B"],
                into_job_id=jobs["C"],
                kind="user_confirmed_merge",
                evidence={},
                now=utcnow(),
            )
