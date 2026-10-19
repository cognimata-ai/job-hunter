import asyncio
import hashlib
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.transitions import transition
from jobhunter.dedup import identity, merge, revisions
from jobhunter.dedup.identity import collect_keys, resolve_item, tombstone_key_hash
from jobhunter.errors import FencedOut, InvariantViolation
from jobhunter.graphs.collect.nodes.resolve_identity import make_resolve_identity
from jobhunter.graphs.collect.state import initial_state
from jobhunter.normalize.keys import AdKey
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import (
    STEPSTONE_1,
    STEPSTONE_2,
    job_lockable,
    key_pair,
    make_deps,
    pending_item,
    queued_run,
    resolved,
    supersede,
)
from tests.support.faults import PausePoints
from tests.support.observations import normalized

pytestmark = pytest.mark.integration

GENERATION = sa.text("SELECT generation FROM ranking_state WHERE id = 1")
POSTING = sa.text(
    "SELECT p.id, p.job_id, p.seen_count, p.first_seen_at, p.last_seen_at, p.availability, "
    "p.availability_changed_at, p.latest_observation_id FROM postings p WHERE p.id = :p"
)
ITEM = sa.text(
    "SELECT disposition, reason_code, observation_id, normalized IS NULL AS dropped FROM raw_items "
    "WHERE id = :i"
)
OBSERVATION_OF = sa.text(
    "SELECT id, posting_id, observed_at FROM posting_observations WHERE raw_item_id = :i"
)
KEYS = sa.text("SELECT key_ns, key_value, exclusive, job_id FROM ad_keys ORDER BY key_ns, key_value")
KEY_SOURCES = sa.text(
    "SELECT key_ns, key_value, posting_id FROM ad_key_sources ORDER BY key_ns, key_value, posting_id"
)


async def fetch(engine: AsyncEngine, statement: sa.TextClause, **params: Any) -> list[Any]:
    async with engine.connect() as c:
        return list((await c.execute(statement, params)).all())


async def scalar(engine: AsyncEngine, statement: str, **params: Any) -> Any:
    async with engine.connect() as c:
        return (await c.execute(sa.text(statement), params)).scalar_one()


async def generation(engine: AsyncEngine) -> int:
    return int(await scalar(engine, "SELECT generation FROM ranking_state WHERE id = 1"))


def test_collect_keys_deduplicates_and_puts_exclusive_namespaces_first() -> None:
    posting = normalized(
        "x",
        raw_keys=(
            AdKey("htidocid", "H1"),
            AdKey("pub:stepstone.de", "1"),
            AdKey("htidocid", "H1"),
            AdKey("htidocid", "H2"),
            AdKey("adzuna", "9"),
        ),
    )
    assert collect_keys(posting) == [
        AdKey("pub:stepstone.de", "1"),
        AdKey("adzuna", "9"),
        AdKey("htidocid", "H1"),
        AdKey("htidocid", "H2"),
    ]
    assert tombstone_key_hash(AdKey("adzuna", "9")) == hashlib.sha256(b"adzuna\x009").digest()


async def test_new_job_posting_observation_keys_revision_and_filters(engine, current_epoch) -> None:
    owner = await queued_run(engine)
    item = await pending_item(
        engine,
        owner,
        "jsearch",
        {"native_id": "UID1", "primary_link": "https://stepstone.de/x--14607704-inline.html"},
    )
    async with engine.begin() as c:
        assert await resolve_item(c, owner=owner, raw_item_id=item) == "linked"
    async with engine.connect() as c:
        job = (
            await c.execute(
                sa.text(
                    "SELECT j.id, j.current_revision_id, j.availability, p.seen_count, p.availability "
                    "FROM jobs j JOIN postings p ON p.job_id = j.id"
                )
            )
        ).one()
        keys = (
            await c.execute(sa.text("SELECT key_ns, key_value, exclusive FROM ad_keys ORDER BY key_ns"))
        ).all()
        item_row = (
            await c.execute(
                sa.text(
                    "SELECT disposition, observation_id IS NOT NULL, normalized FROM raw_items WHERE id=:i"
                ),
                {"i": item},
            )
        ).one()
        hf = (
            await c.execute(
                sa.text("SELECT count(*) FROM hard_filter_results WHERE job_id=:j"), {"j": job.id}
            )
        ).scalar_one()
        gen = (await c.execute(sa.text("SELECT generation FROM ranking_state"))).scalar_one()
    assert (job.current_revision_id is not None, job.availability, job.seen_count, job[4]) == (
        True,
        "active",
        1,
        "active",
    )
    assert [tuple(k) for k in keys] == [("htidocid", "UID1", False), ("pub:stepstone.de", "14607704", True)]
    assert tuple(item_row) == ("linked", True, None) and hf == 1 and gen == 1
    # the observation: sighting time, provenance copy, the item's content hash; the keys' provenance
    [observation] = await fetch(engine, OBSERVATION_OF, i=item)
    fetched_at, sha = (
        await fetch(
            engine,
            sa.text(
                "SELECT r.fetched_at, r.body_sha256 FROM raw_items i "
                "JOIN raw_responses r ON r.id = i.raw_response_id "
                "WHERE i.id = :i"
            ),
            i=item,
        )
    )[0]
    stored = (
        await fetch(
            engine,
            sa.text(
                "SELECT raw_response_sha256, content_hash, completeness, quality FROM posting_observations "
                "WHERE id = :o"
            ),
            o=observation.id,
        )
    )[0]
    assert observation.observed_at == fetched_at and bytes(stored.raw_response_sha256) == sha
    assert (len(bytes(stored.content_hash)), stored.completeness, stored.quality) == (32, "full", "ok")
    assert [tuple(r) for r in await fetch(engine, KEY_SOURCES)] == [
        ("htidocid", "UID1", observation.posting_id),
        ("pub:stepstone.de", "14607704", observation.posting_id),
    ]
    assert await scalar(engine, "SELECT first_seen_at FROM jobs WHERE id = :j", j=job.id) == fetched_at


async def test_second_sighting_and_replay_are_idempotent(engine, current_epoch) -> None:
    owner = await queued_run(engine)
    day0 = utcnow() - timedelta(days=2)
    first = await pending_item(engine, owner, "jsearch", {"native_id": "UID2"}, fetched_at=day0)
    assert await resolved(engine, owner, first) == "linked"
    assert await generation(engine) == 1  # a new job changes the list
    [(_, posting_id, _)] = await fetch(engine, OBSERVATION_OF, i=first)  # (id, posting_id, observed_at)

    second = await pending_item(
        engine, owner, "jsearch", {"native_id": "UID2"}, fetched_at=day0 + timedelta(days=1)
    )
    assert await resolved(engine, owner, second) == "linked"
    [(observation, same_posting, _)] = await fetch(engine, OBSERVATION_OF, i=second)
    [posting] = await fetch(engine, POSTING, p=posting_id)
    assert same_posting == posting_id
    assert (
        posting.seen_count,
        posting.first_seen_at,
        posting.last_seen_at,
        posting.latest_observation_id,
    ) == (
        2,
        day0,
        day0 + timedelta(days=1),
        observation,
    )
    assert await scalar(engine, "SELECT count(*) FROM posting_observations") == 2
    assert await scalar(engine, "SELECT count(*) FROM jobs") == 1
    assert await scalar(engine, "SELECT last_seen_at FROM jobs") == day0 + timedelta(days=1)
    # nothing the list shows changed: no new revision, no bump (Ruling T18-a, the other direction)
    assert await generation(engine) == 1
    assert await scalar(engine, "SELECT count(*) FROM job_revisions") == 1

    state = sa.text(
        "SELECT (SELECT array_agg(ROW(id, seen_count, last_seen_at, latest_observation_id)::text) "
        "FROM postings), (SELECT count(*) FROM posting_observations), "
        "(SELECT array_agg(ROW(id, updated_at)::text) FROM jobs), (SELECT generation FROM ranking_state), "
        "(SELECT array_agg(ROW(id, disposition, disposed_at)::text ORDER BY id) "
        "FROM raw_items), (SELECT count(*) FROM ad_key_sources)"
    )
    before = await fetch(engine, state)
    assert await resolved(engine, owner, second) == "linked"  # a replay of a linked item
    assert await fetch(engine, state) == before

    # an observation that already names a pending item: the item is linked to it, and nothing else happens
    third = await pending_item(
        engine, owner, "jsearch", {"native_id": "UID2"}, fetched_at=day0 + timedelta(days=2)
    )
    async with engine.begin() as c:
        made = (
            await c.execute(
                sa.text(
                    "INSERT INTO posting_observations (posting_id, raw_item_id, source_instance_id, "
                    "raw_response_sha256, observed_at, normalized, content_hash, completeness, quality) "
                    "SELECT posting_id, :i, source_instance_id, raw_response_sha256, observed_at, "
                    "normalized, "
                    "content_hash, completeness, quality FROM posting_observations WHERE id = :o RETURNING id"
                ),
                {"i": third, "o": observation},
            )
        ).scalar_one()
    assert await resolved(engine, owner, third) == "linked"
    assert tuple((await fetch(engine, ITEM, i=third))[0]) == ("linked", None, made, True)
    [again] = await fetch(engine, POSTING, p=posting_id)
    assert (again.seen_count, again.latest_observation_id) == (2, observation)  # no sighting counted


async def test_strong_key_merge_and_exclusive_conflict(engine, current_epoch) -> None:
    owner = await queued_run(engine)
    at = utcnow() - timedelta(days=1)
    pair = await key_pair(engine, owner, at=at)
    assert pair.a < pair.b
    sources_before = [tuple(r) for r in await fetch(engine, KEY_SOURCES)]
    gen = await generation(engine)

    # an Adzuna item with key adzuna:9 (P = B) whose link carries A's stepstone 1
    merging = await pending_item(
        engine,
        owner,
        "adzuna",
        {"native_id": "9", "primary_link": STEPSTONE_1},
        fetched_at=at + timedelta(hours=2),
    )
    assert await resolved(engine, owner, merging) == "linked"
    [event] = await fetch(
        engine, sa.text("SELECT id, kind, from_job_id, into_job_id, posting_ids, evidence FROM merge_events")
    )
    assert (event.kind, event.from_job_id, event.into_job_id, event.posting_ids) == (
        "strong_key_merge",
        pair.b,
        pair.a,
        [pair.posting_b],
    )
    assert event.evidence["snapshot"]["ad_keys"] == [
        {"key_ns": "adzuna", "key_value": "9", "exclusive": True}
    ]
    assert event.evidence["snapshot"]["group_memberships"] == []
    assert event.evidence["rule"] == "strong_key" and event.evidence["raw_item_id"] == merging
    assert await scalar(engine, "SELECT merged_into_job_id FROM jobs WHERE id = :b", b=pair.b) == pair.a
    assert await scalar(engine, "SELECT job_id FROM postings WHERE id = :p", p=pair.posting_b) == pair.a
    assert {r.job_id for r in await fetch(engine, KEYS)} == {pair.a}  # every key owned by A
    # the merge leaves the provenance as it was; the item added its posting's stepstone row before it
    assert [tuple(r) for r in await fetch(engine, KEY_SOURCES)] == sorted(
        [*sources_before, ("pub:stepstone.de", "1", pair.posting_b)]
    )
    a_row = (
        await fetch(
            engine,
            sa.text("SELECT source_ids, first_seen_at, last_seen_at FROM jobs WHERE id = :a"),
            a=pair.a,
        )
    )[0]
    assert (a_row.source_ids, a_row.first_seen_at, a_row.last_seen_at) == (
        ["adzuna", "jsearch"],
        at,
        at + timedelta(hours=2),
    )  # A recomputed over both postings
    assert await scalar(engine, "SELECT count(*) FROM hard_filter_results WHERE job_id = :a", a=pair.a) == 1
    assert await generation(engine) == gen + 1  # the merge changed the list, bumped once

    # a SerpApi item with htidocid H and stepstone 2: no merge, a conflict on a new job
    serp = await pending_item(
        engine,
        owner,
        "serpapi",
        {"native_id": "H", "primary_link": STEPSTONE_2},
        fetched_at=at + timedelta(hours=3),
    )
    assert await resolved(engine, owner, serp) == "linked"
    assert await scalar(engine, "SELECT count(*) FROM merge_events") == 1
    [(_, serp_posting, _)] = await fetch(engine, OBSERVATION_OF, i=serp)
    new_job = await scalar(engine, "SELECT job_id FROM postings WHERE id = :p", p=serp_posting)
    assert new_job not in (pair.a, pair.b)
    [conflict] = await fetch(
        engine,
        sa.text(
            "SELECT id, status, posting_id, posting_job_id, candidate_job_ids, evidence "
            "FROM identity_conflicts"
        ),
    )
    assert (conflict.status, conflict.posting_id, conflict.posting_job_id, conflict.candidate_job_ids) == (
        "open",
        serp_posting,
        new_job,
        [pair.a],
    )
    assert conflict.evidence["conflicting_keys"] == [
        {"key_ns": "pub:stepstone.de", "observation": ["2"], "jobs": [{"job_id": pair.a, "values": ["1"]}]}
    ]
    assert await fetch(
        engine,
        sa.text("SELECT severity, resolved_at FROM alerts WHERE dedup_key = :k"),
        k=f"dedup.identity_conflict:conflict:{conflict.id}",
    ) == [("info", None)]
    # key ownership is unchanged on a conflict: H stays A's, the new stepstone 2 is the new job's
    owners = {(r.key_ns, r.key_value): r.job_id for r in await fetch(engine, KEYS)}
    assert (owners[("htidocid", "H")], owners[("pub:stepstone.de", "2")]) == (pair.a, new_job)

    # the same posting sighted again: it stays on its job, and the open conflict is not repeated
    again = await pending_item(
        engine,
        owner,
        "serpapi",
        {"native_id": "H", "primary_link": STEPSTONE_2},
        fetched_at=at + timedelta(hours=4),
    )
    assert await resolved(engine, owner, again) == "linked"
    assert await scalar(engine, "SELECT count(*) FROM identity_conflicts") == 1
    assert await scalar(engine, "SELECT job_id FROM postings WHERE id = :p", p=serp_posting) == new_job


async def test_different_decision_blocks_the_merge(engine, current_epoch) -> None:
    owner = await queued_run(engine)
    at = utcnow() - timedelta(days=1)
    pair = await key_pair(engine, owner, at=at)
    async with engine.begin() as c:
        await c.execute(
            sa.text(
                "INSERT INTO dedup_decisions (job_a, job_b, decision, source) "
                "VALUES (:a, :b, 'different', 'user_split')"
            ),
            {"a": pair.a, "b": pair.b},
        )
    # the Adzuna posting again, now carrying A's stepstone 1: it stays on its own job B, nothing merges
    item = await pending_item(
        engine,
        owner,
        "adzuna",
        {"native_id": "9", "primary_link": STEPSTONE_1},
        fetched_at=at + timedelta(hours=2),
    )
    assert await resolved(engine, owner, item) == "linked"
    assert await scalar(engine, "SELECT job_id FROM postings WHERE id = :p", p=pair.posting_b) == pair.b
    assert await scalar(engine, "SELECT count(*) FROM merge_events") == 0
    assert await scalar(engine, "SELECT count(*) FROM identity_conflicts") == 0
    assert await scalar(engine, "SELECT count(*) FROM jobs WHERE merged_into_job_id IS NOT NULL") == 0
    owners = {(r.key_ns, r.key_value): r.job_id for r in await fetch(engine, KEYS)}
    assert owners[("pub:stepstone.de", "1")] == pair.a  # ownership unchanged
    # a new posting carrying keys of both: a job of its own, no merge, no conflict (interpretation)
    other = await pending_item(
        engine,
        owner,
        "arbeitnow",
        {"raw_keys": (AdKey("htidocid", "H"), AdKey("adzuna", "9"))},
        fetched_at=at + timedelta(hours=3),
    )
    assert await resolved(engine, owner, other) == "linked"
    [(_, other_posting, _)] = await fetch(engine, OBSERVATION_OF, i=other)
    assert await scalar(engine, "SELECT job_id FROM postings WHERE id = :p", p=other_posting) not in (
        pair.a,
        pair.b,
    )
    assert await scalar(engine, "SELECT count(*) FROM merge_events") == 0
    assert await scalar(engine, "SELECT count(*) FROM identity_conflicts") == 0


async def test_tombstone_key_restores_the_flag(engine, current_epoch) -> None:
    owner = await queued_run(engine)
    async with engine.begin() as c:
        tombstone = (
            await c.execute(
                sa.text(
                    "INSERT INTO job_tombstones (former_job_id, user_actions) "
                    'VALUES (424242, \'{"hidden_at": "2026-09-01T00:00:00+00:00"}\') RETURNING id'
                )
            )
        ).scalar_one()
        await c.execute(
            sa.text("INSERT INTO tombstone_keys (key_hash, tombstone_id) VALUES (:h, :t)"),
            {"h": tombstone_key_hash(AdKey("adzuna", "9")), "t": tombstone},
        )
    item = await pending_item(engine, owner, "adzuna", {"native_id": "9"})
    assert await resolved(engine, owner, item) == "linked"
    assert await scalar(engine, "SELECT restored_from_tombstone_id FROM jobs") == tombstone
    # no hit, no flag
    other = await pending_item(engine, owner, "adzuna", {"native_id": "10"})
    assert await resolved(engine, owner, other) == "linked"
    assert (
        await scalar(
            engine,
            "SELECT j.restored_from_tombstone_id FROM jobs j JOIN postings p ON p.job_id = j.id "
            "WHERE p.native_id = '10'",
        )
        is None
    )


async def test_late_sighting_never_revives(engine, current_epoch) -> None:
    owner = await queued_run(engine)
    changed = utcnow() - timedelta(hours=1)  # T
    first = await pending_item(
        engine, owner, "jsearch", {"native_id": "UID3"}, fetched_at=changed - timedelta(minutes=30)
    )
    assert await resolved(engine, owner, first) == "linked"
    [(latest, posting_id, _)] = await fetch(engine, OBSERVATION_OF, i=first)
    async with engine.begin() as c:
        await transition(c, "postings", posting_id, ["active"], "closed", reason="ats_absent")
        await c.execute(
            sa.text("UPDATE postings SET availability_changed_at = :t WHERE id = :p"),
            {"t": changed, "p": posting_id},
        )
    late = await pending_item(
        engine, owner, "jsearch", {"native_id": "UID3"}, fetched_at=changed - timedelta(hours=1)
    )
    assert await resolved(engine, owner, late) == "linked"
    [posting] = await fetch(engine, POSTING, p=posting_id)
    assert (posting.availability, posting.availability_changed_at, posting.seen_count) == (
        "closed",
        changed,
        2,
    )
    assert (posting.latest_observation_id, posting.last_seen_at) == (latest, changed - timedelta(minutes=30))
    assert (
        await scalar(engine, "SELECT count(*) FROM posting_observations WHERE posting_id = :p", p=posting_id)
        == 2
    )
    # the job's availability derives from its postings
    assert await scalar(engine, "SELECT availability FROM jobs WHERE id = :j", j=posting.job_id) == "closed"
    # a sighting after the change revives it, changed at the sighting's own time (Ruling T18-d)
    sighted = utcnow() - timedelta(minutes=5)
    fresh = await pending_item(engine, owner, "jsearch", {"native_id": "UID3"}, fetched_at=sighted)
    assert await resolved(engine, owner, fresh) == "linked"
    [revived] = await fetch(engine, POSTING, p=posting_id)
    assert (revived.availability, revived.seen_count, revived.availability_changed_at) == (
        "active",
        3,
        sighted,
    )
    assert (
        await scalar(engine, "SELECT availability_reason FROM postings WHERE id = :p", p=posting_id) is None
    )
    assert await scalar(engine, "SELECT availability FROM jobs WHERE id = :j", j=posting.job_id) == "active"


async def test_superseded_owner_is_fenced(engine, current_epoch, queue_app) -> None:
    owner = await queued_run(engine)
    item = await pending_item(engine, owner, "jsearch", {"native_id": "UID4"})
    await supersede(engine, owner)
    with pytest.raises(FencedOut):
        async with engine.begin() as c:
            await resolve_item(c, owner=owner, raw_item_id=item)
    assert await scalar(engine, "SELECT count(*) FROM jobs") == 0
    assert tuple((await fetch(engine, ITEM, i=item))[0]) == ("pending", None, None, False)


async def test_an_identity_error_rejects_the_item_and_writes_nothing_else(
    engine, current_epoch, monkeypatch
) -> None:
    owner = await queued_run(engine)
    item = await pending_item(engine, owner, "jsearch", {"native_id": "UID5"})

    async def broken(*args: object, **kwargs: object) -> None:
        raise InvariantViolation("injected")

    monkeypatch.setattr(revisions, "recompute_job", broken)
    assert await resolved(engine, owner, item) == "rejected"
    assert tuple((await fetch(engine, ITEM, i=item))[0]) == ("rejected", "identity_error", None, True)
    for table in ("jobs", "postings", "posting_observations", "ad_keys"):
        assert await scalar(engine, f"SELECT count(*) FROM {table}") == 0  # noqa: S608 (a constant table name)
    assert await generation(engine) == 0


async def test_the_node_resolves_the_run_items_and_counts(engine, current_epoch) -> None:
    owner = await queued_run(engine)
    at = utcnow() - timedelta(hours=5)
    await pending_item(
        engine, owner, "jsearch", {"native_id": "H", "primary_link": STEPSTONE_1}, fetched_at=at
    )
    await pending_item(engine, owner, "adzuna", {"native_id": "9"}, fetched_at=at + timedelta(hours=1))
    await pending_item(
        engine,
        owner,
        "adzuna",
        {"native_id": "9", "primary_link": STEPSTONE_1},
        fetched_at=at + timedelta(hours=2),
    )
    await pending_item(
        engine,
        owner,
        "serpapi",
        {"native_id": "H", "primary_link": STEPSTONE_2},
        fetched_at=at + timedelta(hours=3),
    )
    node = make_resolve_identity(make_deps(engine, lambda instance: pytest.fail("no adapter is used")))
    update = await node(initial_state(owner.run_id, owner.attempt_no))
    assert update["steps"] == ["resolve_identity"]
    assert update["counters"] == {
        "raw_items_linked": 4,
        "raw_items_out_of_scope": 0,
        "raw_items_rejected": 0,
        "raw_items_deferred": 0,
        "deferred_relinked": 0,  # no deferred item of an earlier run
        "deferred_out_of_scope": 0,
        "deferred_rejected": 0,
        "deferred_still_fenced": 0,
        "jobs_created": 3,  # A, B, and the conflict's own job
        "merges": 1,
        "conflicts": 1,
        "revisions_new": await scalar(engine, "SELECT count(*) FROM job_revisions"),
        "availability_closed": 0,  # the snapshot pass (Task 19): the run has no snapshot
        "availability_unknown": 0,
        "availability_revived": 0,
        "mass_events_opened": 0,
    }
    assert await scalar(engine, "SELECT count(*) FROM raw_items WHERE disposition = 'pending'") == 0
    again = await node(initial_state(owner.run_id, owner.attempt_no))  # nothing is left pending
    assert again["counters"]["raw_items_linked"] == 0 and again["counters"]["jobs_created"] == 0


JSEARCH_JOB = sa.text(
    "SELECT p.job_id FROM postings p JOIN source_instances i ON i.id = p.source_instance_id "
    "WHERE i.source_id = 'jsearch' AND p.native_id = :n"
)


async def jobs_of(engine: AsyncEngine, *native_ids: str) -> list[int]:
    """The job of the JSearch posting of each native id."""
    async with engine.connect() as c:
        return [(await c.execute(JSEARCH_JOB, {"n": native_id})).scalar_one() for native_id in native_ids]


async def test_an_exclusive_conflict_between_two_jobs_of_j(engine, current_epoch) -> None:
    """Minor 8: the observation carries no exclusive key; two jobs of `J` hold disjoint stepstone ids."""
    owner = await queued_run(engine)
    at = utcnow() - timedelta(hours=5)
    for hours, (native_id, link) in enumerate((("H1", STEPSTONE_1), ("H2", STEPSTONE_2))):
        item = await pending_item(
            engine,
            owner,
            "jsearch",
            {"native_id": native_id, "primary_link": link},
            fetched_at=at + timedelta(hours=hours),
        )
        assert await resolved(engine, owner, item) == "linked"
    a, b = await jobs_of(engine, "H1", "H2")
    linking = await pending_item(
        engine,
        owner,
        "arbeitnow",
        {"raw_keys": (AdKey("htidocid", "H1"), AdKey("htidocid", "H2"))},
        fetched_at=at + timedelta(hours=2),
    )
    assert await resolved(engine, owner, linking) == "linked"
    [(_, posting, _)] = await fetch(engine, OBSERVATION_OF, i=linking)
    x = await scalar(engine, "SELECT job_id FROM postings WHERE id = :p", p=posting)
    assert x not in (a, b) and await scalar(engine, "SELECT count(*) FROM merge_events") == 0
    [conflict] = await fetch(
        engine, sa.text("SELECT posting_job_id, candidate_job_ids, evidence FROM identity_conflicts")
    )
    assert (conflict.posting_job_id, conflict.candidate_job_ids) == (x, [a, b])
    assert conflict.evidence["conflicting_keys"] == [
        {
            "key_ns": "pub:stepstone.de",
            "observation": [],
            "jobs": [{"job_id": a, "values": ["1"]}, {"job_id": b, "values": ["2"]}],
        }
    ]
    owners = {(r.key_ns, r.key_value): r.job_id for r in await fetch(engine, KEYS)}
    assert (owners[("htidocid", "H1")], owners[("htidocid", "H2")]) == (a, b)  # ownership unchanged


async def test_a_job_merged_before_the_lock_sends_a_fresh_ordered_round(
    engine, current_epoch, monkeypatch
) -> None:
    """Minor 2: a round whose set changed is rolled back to its savepoint (its row locks released), and the
    next round locks the whole new set in one ordered statement."""
    owner = await queued_run(engine)
    at = utcnow() - timedelta(hours=5)
    for hours, native_id in enumerate(("HA", "HB", "HC")):
        item = await pending_item(
            engine, owner, "jsearch", {"native_id": native_id}, fetched_at=at + timedelta(hours=hours)
        )
        assert await resolved(engine, owner, item) == "linked"
    a, b, c = await jobs_of(engine, "HA", "HB", "HC")
    linking = await pending_item(
        engine,
        owner,
        "arbeitnow",
        {"raw_keys": (AdKey("htidocid", "HA"), AdKey("htidocid", "HB"))},
        fetched_at=at + timedelta(hours=3),
    )
    calls: list[tuple[list[int], list[int]]] = []
    real_lock = merge.lock_jobs

    async def recording(conn, job_ids, *, with_groups_of=()):
        calls.append((sorted(job_ids), sorted(with_groups_of)))
        return await real_lock(conn, job_ids, with_groups_of=with_groups_of)

    monkeypatch.setattr(merge, "lock_jobs", recording)
    pauses = PausePoints(identity.BEFORE_JOB_LOCK, identity.IN_ITEM_TX)
    monkeypatch.setattr(identity, "afault_point", pauses)
    resolving = asyncio.create_task(resolved(engine, owner, linking))
    await asyncio.wait_for(pauses[identity.BEFORE_JOB_LOCK].reached.wait(), 10)
    # between the read (J = {A, B}) and the lock, B is merged into C and commits
    async with engine.begin() as conn:
        await merge.merge_jobs(conn, from_job_id=b, into_job_id=c, kind="user_confirmed_merge", evidence={})
    pauses[identity.BEFORE_JOB_LOCK].resume()
    await asyncio.wait_for(pauses[identity.IN_ITEM_TX].reached.wait(), 10)
    # the first round's lock on B went with its savepoint; the second round holds A and C
    assert [await job_lockable(engine, job) for job in (a, b, c)] == [False, True, False]
    pauses[identity.IN_ITEM_TX].resume()
    assert await asyncio.wait_for(resolving, 10) == "linked"
    assert calls[-2:] == [([a, b], [b]), ([a, c], [c])]  # each round: one ordered statement
    events = await fetch(
        engine, sa.text("SELECT kind, from_job_id, into_job_id FROM merge_events ORDER BY id")
    )
    assert [tuple(e) for e in events] == [("user_confirmed_merge", b, c), ("strong_key_merge", c, a)]
    [(_, posting, _)] = await fetch(engine, OBSERVATION_OF, i=linking)
    assert await scalar(engine, "SELECT job_id FROM postings WHERE id = :p", p=posting) == a
