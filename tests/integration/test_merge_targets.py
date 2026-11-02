"""The score targets of a merged job (spec §8.6.1 steps 1 and 4, §8.6.2 step 5, §7.16.3 L2901-2906, §10.10
L3979; plan M3 Task 22, Deviation 14, Rulings P8, P52, Carries T10-f, T13-a).

- A merge cancels the from-job's unpublished targets as `job_merged`: `pending`, `failed_*`, `in_progress`
  (the claim cleared, the running attempt `cancelled`) and `in_batch` (one by one: the batch pin cleared, its
  attempt `cancelled`, the submission untouched); a published target stays, and the ids it cancelled are in
  the snapshot. A cancelled follow-up target's hold is given back once.
- A late batch result of a target the merge cancelled is fenced (`ignored_fenced`): it settles its spend from
  the item's result (`billed`, or `not_billed(batch_item_expired)`) and publishes nothing; the target stays
  cancelled.
- Ruling P52: a transaction that merges several jobs (identity resolution, `resolve_conflict(merge)`,
  `groups.confirm_group`) locks the bulk rows of every merged job's candidates before its first target row.
- An unmerge revives the `job_merged` target of the from-job's restored current revision under the active
  tuple, and leaves a `tuple_superseded` one of an older tuple alone.
- Ruling T22-e (§9.7 L3713-3714): a merge or an unmerge outside a run that moves a job's current revision
  cancels the previous revision's unpublished targets `revision_superseded` (a published one stays) and gives
  the new current revision its target; the bulk rows of those targets' holds are locked with the merge's own,
  before the first target row.

Every datum is invented.
"""

import asyncio
import hashlib
from collections import Counter
from collections.abc import Callable, Coroutine
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db import ranking
from jobhunter.dedup import groups
from jobhunter.dedup.merge import merge_jobs, resolve_conflict, unmerge
from jobhunter.llm import budget, spend_ledger
from jobhunter.llm.fake import BatchScript
from jobhunter.normalize.keys import AdKey
from jobhunter.scoring import targets
from jobhunter.scoring.targets import Cancelled
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.batches import (
    entries_by_custom_id,
    item_rows,
    poll_once,
    submission_rows,
    submission_state,
    submit_batch,
    targets_by_custom_id,
)
from tests.support.collect import STEPSTONE_1, STEPSTONE_2, key_pair, pending_item, queued_run, resolved
from tests.support.llm import FakeLLM
from tests.support.m3 import (
    NOW,
    Job,
    insert_attempt,
    insert_batch_item,
    insert_bulk_operation,
    insert_job,
    insert_published,
    insert_submission,
    insert_target,
)
from tests.support.pg import PgServer, lock_wait_details
from tests.support.procs import await_until
from tests.support.profile import OpRunner
from tests.support.scoring import (
    activate_score_tuple,
    attempt_end,
    followup_hold,
    hold_of,
    state_target,
    target_row,
)
from tests.support.settings import confirm_new_profile

pytestmark = pytest.mark.integration

BATCH = 200  # the smallest batch the settings allow (`batch_min_targets`)
_REVISION = sa.text(
    "INSERT INTO job_revisions (job_id, revision_no, material_hash, llm_input, description_text, "
    "description_completeness, ad_language, embedding_status) VALUES (:j, :n, :h, '{}', "
    "'Erfundene Testanzeige.', 'full', 'de', 'awaiting_embedding') RETURNING id"
)
_CURRENT_REVISION = sa.text("SELECT current_revision_id FROM jobs WHERE id = :j")
_EVENT = sa.text("SELECT id, from_job_id, into_job_id, evidence FROM merge_events WHERE id = :e")
_JOB_OF_ITEM = sa.text(
    "SELECT p.job_id FROM posting_observations o JOIN postings p ON p.id = o.posting_id "
    "WHERE o.raw_item_id = :i"
)
_RESULTS_OF = sa.text("SELECT count(*) FROM llm_results WHERE target_id = ANY(CAST(:ids AS bigint[]))")
_PID = sa.text("SELECT pg_backend_pid()")
_TRY_TARGET = sa.text("SELECT id FROM score_targets WHERE id = :t FOR UPDATE NOWAIT")
_GROUP = sa.text(
    "INSERT INTO duplicate_groups (representative_job_id, status, election_reason) "
    "VALUES (:rep, 'active', 'initial') RETURNING id"
)
_MEMBER = sa.text("INSERT INTO duplicate_group_members (group_id, job_id, evidence) VALUES (:g, :j, '{}')")
_IN_GROUP = sa.text("UPDATE jobs SET duplicate_group_id = :g WHERE id = :j")


@pytest.fixture
def queued(queue_app: procrastinate.App) -> None:
    """The task app the batch poll's follow-on and cycle requests defer on."""


async def older_revision(conn: AsyncConnection, job: Job, revision_no: int) -> Job:
    """Another revision of the job (not its current one), as a `Job` naming it."""
    digest = hashlib.sha256(f"{job.job_id}:{revision_no}".encode()).digest()
    params = {"j": job.job_id, "n": revision_no, "h": digest}
    return Job(job.job_id, int((await conn.execute(_REVISION, params)).scalar_one()))


async def scalar(engine: AsyncEngine, statement: sa.TextClause, **params: Any) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(statement, params)).scalar_one()


async def event_row(engine: AsyncEngine, event_id: int) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(_EVENT, {"e": event_id})).one()


async def merged(engine: AsyncEngine, from_job_id: int, into_job_id: int) -> int:
    """A standalone `user_confirmed_merge` of jobs without postings (nothing to recompute), in a transaction
    of its own with its ranking bump; the event id."""
    async with engine.begin() as conn:
        event = await merge_jobs(
            conn,
            from_job_id=from_job_id,
            into_job_id=into_job_id,
            kind="user_confirmed_merge",
            evidence={},
            recompute=False,
        )
        await ranking.bump(conn)
    return event


async def current_job(engine: AsyncEngine, job_id: int) -> Job:
    return Job(job_id, int(await scalar(engine, _CURRENT_REVISION, j=job_id)))


async def test_a_merge_cancels_every_unpublished_target_of_the_from_job_and_records_them(
    engine: AsyncEngine,
) -> None:
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        bulk_id = await insert_bulk_operation(conn, tuple_id)
        submission = await insert_submission(
            conn,
            tuple_id,
            bulk_id,
            state="submitted",
            submit_started_at=utcnow(),
            provider_batch_id="msgbatch_t22_merge",
        )
        source, into = await insert_job(conn), await insert_job(conn)
        revisions = [source] + [await older_revision(conn, source, n) for n in range(2, 7)]
        pending = await state_target(conn, revisions[0], tuple_id, "pending")
        claim = uuid4()
        claimed = await insert_target(
            conn,
            revisions[1],
            tuple_id,
            state="in_progress",
            claim_id=claim,
            claim_epoch=1,
            claimed_at=NOW,
            claim_heartbeat_at=NOW,
            attempt_no=1,
            lane="similarity",
        )
        claimed_attempt = await insert_attempt(
            conn, claimed, claim_id=claim, claim_epoch=1, lane="similarity"
        )
        batched = await state_target(
            conn,
            revisions[2],
            tuple_id,
            "in_batch",
            batch_submission_id=submission,
            attempt_no=1,
            lane="batch",
        )
        batched_attempt = await insert_attempt(conn, batched, mode="batch", lane="batch")
        await insert_batch_item(conn, submission, batched)
        failed = await state_target(conn, revisions[3], tuple_id, "failed_request")
        published = await insert_published(conn, revisions[4], tuple_id)
        superseded = await state_target(
            conn, revisions[5], tuple_id, "cancelled", state_reason="tuple_superseded"
        )
        kept = await state_target(conn, into, tuple_id, "pending")
    event = await merged(engine, source.job_id, into.job_id)

    for target in (pending, claimed, batched, failed):
        row = await target_row(engine, target)
        assert (row.state, row.state_reason) == ("cancelled", "job_merged"), target
        assert (row.claim_id, row.batch_submission_id) == (None, None)
    # the running attempts end `cancelled` (Carry T10-f: a non-owner writes no other outcome)
    for attempt in (claimed_attempt, batched_attempt):
        ended = await attempt_end(engine, attempt)
        assert (ended.outcome, ended.outcome_detail) == ("cancelled", "job_merged")
    # the in_batch target is cancelled alone: its submission and its item are the batch's (L3979)
    (row,) = await submission_rows(engine)
    assert (row.state, row.cancel_requested_at) == ("submitted", None)
    items = await item_rows(engine)
    assert (items[f"t{batched}_a1"].decision, items[f"t{batched}_a1"].hold_released_at) == (None, None)
    # published targets stay as history; a target cancelled already and the into-job's are left alone
    assert (await target_row(engine, published.target_id)).state == "published_accepted"
    still = await target_row(engine, superseded)
    assert (still.state, still.state_reason) == ("cancelled", "tuple_superseded")
    assert (await target_row(engine, kept)).state == "pending"
    snapshot = (await event_row(engine, event)).evidence["snapshot"]
    assert snapshot["non_terminal_target_ids"] == sorted((pending, claimed, batched, failed))


async def test_a_merge_gives_a_cancelled_follow_up_targets_hold_back_once(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        source, into = await insert_job(conn), await insert_job(conn)
        target = await state_target(conn, source, tuple_id, "pending", lane="batch_followup")
        operation, item = await followup_hold(conn, tuple_id, target, hold=Decimal("0.250000"))
        await conn.execute(
            sa.text("UPDATE bulk_operations SET followup_held_usd = 0.4 WHERE id = :b"), {"b": operation}
        )
    await merged(engine, source.job_id, into.job_id)
    assert (await target_row(engine, target)).state_reason == "job_merged"
    held = await hold_of(engine, operation, item)
    assert held.followup_held_usd == Decimal("0.150000") and held.hold_released_at is not None
    async with engine.begin() as conn:  # nothing is left to move or to give back
        assert await targets.cancel_targets(
            conn, job_ids=[source.job_id], reason="job_merged", now=utcnow()
        ) == Cancelled(0, 0)
    assert (await hold_of(engine, operation, item)).followup_held_usd == Decimal("0.150000")


async def test_a_late_batch_result_of_a_merged_target_settles_its_spend_and_publishes_nothing(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, queued: None
) -> None:
    """Ruling P8: the merge cancels the `in_batch` targets one by one; their items, decided later, are
    fenced by Task 17's item decision (`ignored_fenced`) and settle what the provider reports."""
    submitted = await submit_batch(engine, op_runner, fake_llm, BATCH)
    succeeded, expired = submitted.setup.targets[:2]
    async with engine.begin() as conn:
        into = await insert_job(conn)
    for target in (succeeded, expired):
        await merged(engine, target.job_id, into.job_id)
    assert await submission_state(engine, submitted.submission_id) == "submitted"
    (row,) = await submission_rows(engine)
    assert row.cancel_requested_at is None  # the batch itself is not cancelled
    gone = (submitted.custom_id(succeeded), submitted.custom_id(expired))
    ids = submitted.custom_ids
    fake_llm.batches(
        BatchScript(items={**dict.fromkeys(ids, "succeeded"), gone[1]: "expired"}, polls_until_ended=0)
    )
    await poll_once(engine, fake_llm)

    assert await submission_state(engine, submitted.submission_id) == "reconciled"
    items = await item_rows(engine)
    assert Counter(item.decision for item in items.values()) == {"published": BATCH - 2, "ignored_fenced": 2}
    assert (items[gone[0]].result_type, items[gone[1]].result_type) == ("succeeded", "expired")
    assert all(items[c].hold_released_at is not None for c in gone)  # each item's hold given back
    entries = await entries_by_custom_id(engine)
    assert (entries[gone[0]].state, entries[gone[0]].evidence) == ("billed", None)
    assert (entries[gone[1]].state, entries[gone[1]].evidence) == ("not_billed", "batch_item_expired")
    rows = await targets_by_custom_id(engine)
    for custom_id in gone:  # not resurrected; the attempt keeps the merge's end
        assert (rows[custom_id].state, rows[custom_id].state_reason) == ("cancelled", "job_merged")
        assert (rows[custom_id].outcome, rows[custom_id].outcome_detail) == ("cancelled", "job_merged")
        assert rows[custom_id].batch_submission_id is None
    assert await scalar(engine, _RESULTS_OF, ids=[succeeded.target_id, expired.target_id]) == 0
    async with engine.connect() as conn:
        assert await spend_ledger.recompute(conn, budget.month_of(utcnow())) is None


async def jsearch_job(engine: AsyncEngine, owner: Any, native_id: str, at: Any, **overrides: Any) -> int:
    item = await pending_item(engine, owner, "jsearch", {"native_id": native_id, **overrides}, fetched_at=at)
    assert await resolved(engine, owner, item) == "linked"
    return int(await scalar(engine, _JOB_OF_ITEM, i=item))


async def two_merges(
    engine: AsyncEngine, owner: Any, at: Any, caller: str
) -> tuple[int, int, Callable[[], Coroutine[Any, Any, object]]]:
    """Two jobs that one transaction of `caller` merges away, in that order, and that transaction (not run
    yet): identity's sighting of three jobs' keys, `resolve_conflict(merge)` of a conflict with two
    candidates, or `groups.confirm_group` of a group of three."""
    if caller == "resolve_conflict":
        a = await jsearch_job(engine, owner, "H1", at, primary_link=STEPSTONE_1)
        b = await jsearch_job(engine, owner, "H2", at + timedelta(hours=1), primary_link=STEPSTONE_2)
        both = {"raw_keys": (AdKey("htidocid", "H1"), AdKey("htidocid", "H2"))}
        item = await pending_item(engine, owner, "arbeitnow", both, fetched_at=at + timedelta(hours=2))
        assert await resolved(engine, owner, item) == "linked"
        conflict = int(await scalar(engine, sa.text("SELECT id FROM identity_conflicts")))

        async def resolving() -> None:
            async with engine.begin() as conn:
                await resolve_conflict(conn, conflict, "merge")

        return a, b, resolving
    a, b, c = [await jsearch_job(engine, owner, f"H{n}", at + timedelta(hours=n)) for n in range(3)]
    assert a < b < c
    if caller == "identity":
        keys = tuple(AdKey("htidocid", f"H{n}") for n in range(3))
        linking = await pending_item(
            engine, owner, "arbeitnow", {"raw_keys": keys}, fetched_at=at + timedelta(hours=4)
        )
        return b, c, lambda: resolved(engine, owner, linking)
    async with engine.begin() as conn:
        group = int((await conn.execute(_GROUP, {"rep": a})).scalar_one())
        for job in (a, b, c):
            await conn.execute(_MEMBER, {"g": group, "j": job})
            await conn.execute(_IN_GROUP, {"g": group, "j": job})

    async def confirming() -> int:
        async with engine.begin() as conn:
            return await groups.confirm_group(conn, group)

    return b, c, confirming


@pytest.mark.parametrize("caller", ["identity", "resolve_conflict", "confirm_group"])
async def test_a_transaction_of_several_merges_locks_every_hold_row_before_its_first_target_row(
    engine: AsyncEngine, migrated: PgServer, current_epoch: int, caller: str
) -> None:
    """Ruling P52 (carry T13-a): one transaction merges two jobs away, the second with a follow-up hold whose
    bulk row is held elsewhere. The transaction waits for that row while neither merged job's target row is
    locked yet: the bulk rows of both merges precede the first target row of the transaction."""
    owner = await queued_run(engine)
    first, second, work = await two_merges(engine, owner, utcnow() - timedelta(hours=8), caller)
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
    first_job, second_job = await current_job(engine, first), await current_job(engine, second)
    async with engine.begin() as conn:
        first_target = await state_target(conn, first_job, tuple_id, "pending")
        second_target = await state_target(conn, second_job, tuple_id, "pending", lane="batch_followup")
        operation, item = await followup_hold(conn, tuple_id, second_target, hold=Decimal("0.100000"))
    async with engine.connect() as holder:
        await holder.execute(sa.text("BEGIN"))
        await holder.execute(
            sa.text("SELECT id FROM bulk_operations WHERE id = :b FOR NO KEY UPDATE"), {"b": operation}
        )
        holder_pid = int((await holder.execute(_PID)).scalar_one())
        merging: asyncio.Task[object] = asyncio.create_task(work())

        async def waiting() -> bool:
            return merging.done() or any(w.relation == "bulk_operations" for w in lock_wait_details(migrated))

        await await_until(waiting, 10, 0.02)
        assert not merging.done()
        (wait,) = [w for w in lock_wait_details(migrated) if w.relation == "bulk_operations"]
        assert (wait.key, wait.blocking_pids) == (str(operation), (holder_pid,))
        async with engine.connect() as probe:  # no target row of either merge is locked yet
            await probe.execute(sa.text("BEGIN"))
            for target in (first_target, second_target):
                assert (await probe.execute(_TRY_TARGET, {"t": target})).scalar_one() == target
            await probe.execute(sa.text("ROLLBACK"))
        await holder.execute(sa.text("ROLLBACK"))
    await asyncio.wait_for(merging, 10)
    for target in (first_target, second_target):
        assert (await target_row(engine, target)).state_reason == "job_merged"
    held = await hold_of(engine, operation, item)
    assert held.followup_held_usd == Decimal("0.000000") and held.hold_released_at is not None


async def test_an_unmerge_revives_the_job_merged_target_of_the_restored_current_revision(
    engine: AsyncEngine, current_epoch: int
) -> None:
    owner = await queued_run(engine)
    at = utcnow() - timedelta(days=1)
    async with engine.begin() as conn:
        old_tuple = await activate_score_tuple(conn)
    pair = await key_pair(engine, owner, at=at)
    b = await current_job(engine, pair.b)
    async with engine.begin() as conn:
        superseded = await state_target(conn, b, old_tuple, "pending")
    _, tuple_id = await confirm_new_profile(engine)  # the old tuple's target: cancelled `tuple_superseded`
    async with engine.begin() as conn:
        target = await state_target(conn, b, tuple_id, "pending", lane="batch_followup", charged_attempts=2)
    merging = await pending_item(
        engine,
        owner,
        "adzuna",
        {"native_id": "9", "primary_link": STEPSTONE_1},
        fetched_at=at + timedelta(hours=2),
    )
    assert await resolved(engine, owner, merging) == "linked"
    async with engine.connect() as conn:
        event_id = int((await conn.execute(sa.text("SELECT id FROM merge_events"))).scalar_one())
    assert (await target_row(engine, target)).state_reason == "job_merged"
    assert (await event_row(engine, event_id)).evidence["snapshot"]["non_terminal_target_ids"] == [target]

    async with engine.begin() as conn:
        await unmerge(conn, event_id)
    assert (await current_job(engine, pair.b)) == b  # B's revision is current again
    revived = await target_row(engine, target)
    assert (revived.state, revived.state_reason, revived.override, revived.lane) == (
        "pending",
        None,
        "none",
        None,
    )
    assert revived.charged_attempts == 0  # a fresh life (R4)
    old = await target_row(engine, superseded)
    assert (old.state, old.state_reason) == ("cancelled", "tuple_superseded")
    async with engine.connect() as conn:
        live = await conn.execute(
            sa.text("SELECT count(*) FROM score_targets WHERE job_id = :j AND llm_tuple_id = :t"),
            {"j": pair.b, "t": tuple_id},
        )
        assert live.scalar_one() == 1  # revived, not created anew


# ---- fix round 1: a merge or an unmerge outside a run supersedes revisions (Ruling T22-e) ----------------

FULL_TEXT = "Erfundene Stelle mit vollständiger Beschreibung der Aufgaben im Team " * 5
PARTIAL_TEXT = "Erfundene Stelle mit einer gekürzten Beschreibung der Aufgaben " * 4


async def described_pair(engine: AsyncEngine, owner: Any, at: Any) -> tuple[int, int]:
    """F, with a full description, then P, with a partial one (so F's id is the lower): merged into P, F
    supplies P's description, and P's current revision moves; P's own revision returns on the unmerge."""
    full = await jsearch_job(
        engine, owner, "HF", at, description_text=FULL_TEXT, description_completeness="full"
    )
    partial = await jsearch_job(
        engine,
        owner,
        "HP",
        at + timedelta(hours=1),
        description_text=PARTIAL_TEXT,
        description_completeness="partial",
    )
    assert full < partial
    return full, partial


async def merge_described(engine: AsyncEngine, full: int, partial: int) -> int:
    """F merged into P by a standalone merge (out of a run), P's rollups recomputed; the ranking bumped."""
    async with engine.begin() as conn:
        event = await merge_jobs(
            conn, from_job_id=full, into_job_id=partial, kind="user_confirmed_merge", evidence={}
        )
        await ranking.bump(conn)
    return event


@pytest.mark.parametrize("old_state", ["pending", "published"])
async def test_a_merge_out_of_a_run_supersedes_the_into_jobs_moved_revision(
    engine: AsyncEngine, current_epoch: int, old_state: str
) -> None:
    owner = await queued_run(engine)
    full, partial = await described_pair(engine, owner, utcnow() - timedelta(hours=8))
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
    before = await current_job(engine, partial)
    async with engine.begin() as conn:
        if old_state == "pending":
            old_target = await state_target(conn, before, tuple_id, "pending")
        else:
            old_target = (await insert_published(conn, before, tuple_id)).target_id
    await merge_described(engine, full, partial)

    after = await current_job(engine, partial)
    assert after.revision_id != before.revision_id  # F's description moved P's current revision
    old = await target_row(engine, old_target)
    if old_state == "pending":
        assert (old.state, old.state_reason) == ("cancelled", "revision_superseded")
    else:
        assert old.state == "published_accepted"  # published targets stay as history
    async with engine.connect() as conn:
        new = (
            await conn.execute(
                sa.text(
                    "SELECT state, llm_tuple_id FROM score_targets WHERE job_revision_id = :r AND job_id = :j"
                ),
                {"r": after.revision_id, "j": partial},
            )
        ).one()
    assert (new.state, new.llm_tuple_id) == ("pending", tuple_id)


@pytest.mark.parametrize("caller", ["confirm_group", "resolve_conflict"])
async def test_the_other_merges_out_of_a_run_supersede_the_moved_revision(
    engine: AsyncEngine, current_epoch: int, caller: str
) -> None:
    """Ruling T22-e for the callers that recompute the into-job themselves: `groups.confirm_group` (F and P in
    a group represented by P) and `resolve_conflict(merge)` (a partial posting X whose keys name two jobs with
    full descriptions and different StepStone ids)."""
    owner = await queued_run(engine)
    at = utcnow() - timedelta(hours=8)
    if caller == "confirm_group":
        full, into = await described_pair(engine, owner, at)
        async with engine.begin() as conn:
            group = int((await conn.execute(_GROUP, {"rep": into})).scalar_one())
            for job in (full, into):
                await conn.execute(_MEMBER, {"g": group, "j": job})
                await conn.execute(_IN_GROUP, {"g": group, "j": job})
    else:
        for n, link in ((1, STEPSTONE_1), (2, STEPSTONE_2)):
            await jsearch_job(
                engine, owner, f"H{n}", at + timedelta(hours=n), primary_link=link, description_text=FULL_TEXT
            )
        keys = {
            "raw_keys": (AdKey("htidocid", "H1"), AdKey("htidocid", "H2")),
            "description_text": PARTIAL_TEXT,
            "description_completeness": "partial",
        }
        item = await pending_item(engine, owner, "arbeitnow", keys, fetched_at=at + timedelta(hours=3))
        assert await resolved(engine, owner, item) == "linked"
        into = int(await scalar(engine, _JOB_OF_ITEM, i=item))
        conflict = int(await scalar(engine, sa.text("SELECT id FROM identity_conflicts")))
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
    before = await current_job(engine, into)
    async with engine.begin() as conn:
        old_target = await state_target(conn, before, tuple_id, "pending")
    async with engine.begin() as conn:
        if caller == "confirm_group":
            await groups.confirm_group(conn, group)
        else:
            await resolve_conflict(conn, conflict, "merge")
    after = await current_job(engine, into)
    assert after.revision_id != before.revision_id
    old = await target_row(engine, old_target)
    assert (old.state, old.state_reason) == ("cancelled", "revision_superseded")
    async with engine.connect() as conn:
        found = await conn.execute(
            sa.text("SELECT state FROM score_targets WHERE job_revision_id = :r AND llm_tuple_id = :t"),
            {"r": after.revision_id, "t": tuple_id},
        )
        assert found.scalar_one() == "pending"


async def test_an_unmerge_supersedes_the_moved_revisions_of_both_jobs(
    engine: AsyncEngine, current_epoch: int
) -> None:
    owner = await queued_run(engine)
    full, partial = await described_pair(engine, owner, utcnow() - timedelta(hours=8))
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
    f_job, p_before = await current_job(engine, full), await current_job(engine, partial)
    async with engine.begin() as conn:
        f_target = await state_target(conn, f_job, tuple_id, "pending")
        p_old = await state_target(conn, p_before, tuple_id, "pending")
    event = await merge_described(engine, full, partial)
    p_merged = await current_job(engine, partial)
    async with engine.connect() as conn:
        p_new = int(
            (
                await conn.execute(
                    sa.text("SELECT id FROM score_targets WHERE job_revision_id = :r"),
                    {"r": p_merged.revision_id},
                )
            ).scalar_one()
        )
    assert (await target_row(engine, f_target)).state_reason == "job_merged"
    assert (await target_row(engine, p_old)).state_reason == "revision_superseded"

    async with engine.begin() as conn:
        await unmerge(conn, event)
    assert await current_job(engine, partial) == p_before  # P's own revision is current again
    assert await current_job(engine, full) == f_job
    gone = await target_row(engine, p_new)
    assert (gone.state, gone.state_reason) == ("cancelled", "revision_superseded")
    for target in (p_old, f_target):  # each job's current revision has its target back
        back = await target_row(engine, target)
        assert (back.state, back.state_reason) == ("pending", None), target


@pytest.mark.parametrize("operation", ["merge", "unmerge"])
async def test_the_superseded_revisions_hold_rows_are_locked_before_the_first_target_row(
    engine: AsyncEngine, migrated: PgServer, current_epoch: int, operation: str
) -> None:
    """Ruling T22-e with P52: the target of P's revision that the merge (or the unmerge) supersedes holds a
    follow-up hold whose bulk row is held elsewhere. The transaction waits for it while F's target row (the
    merge's own cancellation, the unmerge's revival, both earlier in it) is not locked yet."""
    owner = await queued_run(engine)
    full, partial = await described_pair(engine, owner, utcnow() - timedelta(hours=8))
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
    f_job, p_job = await current_job(engine, full), await current_job(engine, partial)
    async with engine.begin() as conn:
        f_target = await state_target(conn, f_job, tuple_id, "pending")
        superseded = await state_target(conn, p_job, tuple_id, "pending", lane="batch_followup")
    work: Callable[[], Coroutine[Any, Any, object]]
    if operation == "merge":
        work = lambda: merge_described(engine, full, partial)  # noqa: E731
    else:
        event = await merge_described(engine, full, partial)
        p_merged = await current_job(engine, partial)
        async with engine.connect() as conn:
            superseded = int(
                (
                    await conn.execute(
                        sa.text("SELECT id FROM score_targets WHERE job_revision_id = :r"),
                        {"r": p_merged.revision_id},
                    )
                ).scalar_one()
            )

        async def unmerging() -> int:
            async with engine.begin() as conn:
                return await unmerge(conn, event)

        work = unmerging
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE score_targets SET lane = 'batch_followup' WHERE id = :t"), {"t": superseded}
        )
        operation_id, item = await followup_hold(conn, tuple_id, superseded, hold=Decimal("0.100000"))
    async with engine.connect() as holder:
        await holder.execute(sa.text("BEGIN"))
        await holder.execute(
            sa.text("SELECT id FROM bulk_operations WHERE id = :b FOR NO KEY UPDATE"), {"b": operation_id}
        )
        holder_pid = int((await holder.execute(_PID)).scalar_one())
        task: asyncio.Task[object] = asyncio.create_task(work())

        async def waiting() -> bool:
            return task.done() or any(w.relation == "bulk_operations" for w in lock_wait_details(migrated))

        await await_until(waiting, 10, 0.02)
        assert not task.done()
        (wait,) = [w for w in lock_wait_details(migrated) if w.relation == "bulk_operations"]
        assert (wait.key, wait.blocking_pids) == (str(operation_id), (holder_pid,))
        async with engine.connect() as probe:  # no target row is locked yet
            await probe.execute(sa.text("BEGIN"))
            for target in (f_target, superseded):
                assert (await probe.execute(_TRY_TARGET, {"t": target})).scalar_one() == target
            await probe.execute(sa.text("ROLLBACK"))
        await holder.execute(sa.text("ROLLBACK"))
    await asyncio.wait_for(task, 10)
    gone = await target_row(engine, superseded)
    assert (gone.state, gone.state_reason) == ("cancelled", "revision_superseded")
    held = await hold_of(engine, operation_id, item)
    assert held.followup_held_usd == Decimal("0.000000") and held.hold_released_at is not None
