"""Target creation, revival and cancellation on PostgreSQL (spec §7.16.3, §9.7, §9.2 node 9; plan Task 14, R4,
Deviation 7, Rulings P9, P52, Controller carry T10-f): `scoring.targets.create_or_revive`, `cancel_targets`,
and `scoring.similarity`.

A creation lands in `pending`, `filtered_out` (the job's failed result for the scope, computed inline when
the row is missing) or `deferred_duplicate` (a deferrable group member; a member flagged
`score_individually` is created `pending`); a second creation changes nothing. A target cancelled for a
supersession or a merge revives into a fresh life, every R4 column reset and the CAS counter kept; a purged
or a published target never revives. A fenced, a merged and a superseded revision are skipped. The
similarity is the best cosine of the revision against the profile's vectors, `NULL` while it awaits its
embedding. `cancel_targets` moves every unpublished target it matches (`in_batch` of a tuple supersession
left to the batch cancel), ends the running attempts `cancelled`, releases each follow-up hold once, and
locks the bulk rows before its first target row.

Every datum is invented.
"""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.sql import q
from jobhunter.scoring import cycles, similarity, targets
from jobhunter.scoring.targets import Cancelled, Created, Revived, Skipped, Unchanged
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.m3 import (
    Job,
    insert_attempt,
    insert_batch_item,
    insert_bulk_operation,
    insert_claimed,
    insert_draft,
    insert_job,
    insert_published,
    insert_scoring_call,
    insert_submission,
)
from tests.support.pg import PgServer, lock_wait_details
from tests.support.procs import await_until
from tests.support.scoring import (
    activate_score_tuple,
    attempt_outcomes,
    embed_revision,
    embedding_at,
    fence_jobs,
    filter_result,
    followup_hold,
    hold_of,
    member_group,
    profile_of_tuple,
    profile_vector_at,
    state_target,
    target_row,
)

pytestmark = pytest.mark.integration

REVIVABLE = ("tuple_superseded", "revision_superseded", "job_merged")
NOT_CANCELLED = ("pending", "budget_deferred", "expired_unscored", "refused")


@pytest.fixture
def now() -> datetime:
    return utcnow()


@pytest.fixture
async def tuple_id(engine: AsyncEngine) -> int:
    async with engine.begin() as conn:
        return await activate_score_tuple(conn)


@pytest.fixture
async def scope(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        found = await conn.execute(sa.text("SELECT scope_version_id FROM active_config WHERE id = 1"))
        return int(found.scalar_one())


async def a_job(engine: AsyncEngine, **columns: object) -> Job:
    async with engine.begin() as conn:
        job = await insert_job(conn)
        for column, value in columns.items():  # the test's own column names
            sql = f"UPDATE jobs SET {column} = :v WHERE id = :j"  # noqa: S608
            await conn.execute(sa.text(sql), {"v": value, "j": job.job_id})
    return job


async def create(
    engine: AsyncEngine,
    job: Job,
    tuple_id: int,
    *,
    revision_id: int | None = None,
    bulk_operation_id: int | None = None,
) -> Created | Revived | Unchanged | Skipped:
    """`create_or_revive` of the job's current revision (or `revision_id`) under `active_config FOR SHARE`,
    with the active scope and the tuple's profile, as its callers run it."""
    async with engine.begin() as conn:
        active = await cycles.lock_active_scoring(conn)
        return await targets.create_or_revive(
            conn,
            job_id=job.job_id,
            job_revision_id=job.revision_id if revision_id is None else revision_id,
            llm_tuple_id=tuple_id,
            profile_version_id=await profile_of_tuple(conn, tuple_id),
            scope_version_id=active.scope_version_id,
            bulk_operation_id=bulk_operation_id,
            now=utcnow(),
        )


async def execute(engine: AsyncEngine, sql: str, **params: object) -> None:
    async with engine.begin() as conn:
        await conn.execute(sa.text(sql), params)


async def scalar(engine: AsyncEngine, sql: str, **params: object) -> object:
    async with engine.connect() as conn:
        return (await conn.execute(sa.text(sql), params)).scalar()


# ---- creation ----------------------------------------------------------------------------------------------


async def test_a_job_that_passes_its_filters_is_created_pending_in_the_bulk_operation(
    engine: AsyncEngine, tuple_id: int, scope: int
) -> None:
    job = await a_job(engine)
    async with engine.begin() as conn:
        await filter_result(conn, job, scope, passed=True)
        bulk_id = await insert_bulk_operation(conn, tuple_id)
    made = await create(engine, job, tuple_id, bulk_operation_id=bulk_id)
    assert isinstance(made, Created) and made.state == "pending"
    row = await target_row(engine, made.target_id)
    assert (row.job_id, row.job_revision_id, row.llm_tuple_id) == (job.job_id, job.revision_id, tuple_id)
    assert (row.state, row.state_reason, row.override, row.override_at, row.lane) == (
        "pending",
        None,
        "none",
        None,
        None,
    )
    assert (row.bulk_operation_id, row.similarity, row.attempt_no, row.charged_attempts) == (
        bulk_id,
        None,
        0,
        0,
    )


async def test_a_failed_filter_result_creates_the_target_filtered_out(
    engine: AsyncEngine, tuple_id: int, scope: int
) -> None:
    job = await a_job(engine)
    async with engine.begin() as conn:
        await filter_result(conn, job, scope, passed=False)
    made = await create(engine, job, tuple_id)
    assert isinstance(made, Created) and made.state == "filtered_out"
    row = await target_row(engine, made.target_id)
    assert (row.state, row.filter_scope_version_id, row.deferred_group_id) == ("filtered_out", scope, None)


async def test_a_missing_filter_result_is_computed_inline(
    engine: AsyncEngine, tuple_id: int, scope: int
) -> None:
    # A1 issue 21: no row for the scope yet; the job's own facts decide (an ad language the scope excludes)
    excluded = await a_job(engine, ad_language="other")
    accepted = await a_job(engine)
    out = await create(engine, excluded, tuple_id)
    kept = await create(engine, accepted, tuple_id)
    assert isinstance(out, Created) and out.state == "filtered_out"
    assert isinstance(kept, Created) and kept.state == "pending"
    async with engine.connect() as conn:
        written = (
            await conn.execute(
                sa.text(
                    "SELECT job_id, passed, reasons FROM hard_filter_results WHERE scope_version_id = :s "
                    "AND job_id = ANY(CAST(:jobs AS bigint[])) ORDER BY job_id"
                ),
                {"s": scope, "jobs": [excluded.job_id, accepted.job_id]},
            )
        ).all()
    assert [(r.job_id, r.passed, list(r.reasons)) for r in written] == [
        (excluded.job_id, False, ["ad_language_excluded"]),
        (accepted.job_id, True, []),
    ]


async def test_a_deferrable_group_member_is_created_deferred_and_its_representative_pending(
    engine: AsyncEngine, tuple_id: int, scope: int
) -> None:
    representative, member = await a_job(engine), await a_job(engine)
    async with engine.begin() as conn:
        group = await member_group(conn, representative, [member])
        for job in (representative, member):
            await filter_result(conn, job, scope, passed=True)
    deferred = await create(engine, member, tuple_id)
    leading = await create(engine, representative, tuple_id)
    assert isinstance(deferred, Created) and deferred.state == "deferred_duplicate"
    assert (await target_row(engine, deferred.target_id)).deferred_group_id == group
    assert isinstance(leading, Created) and leading.state == "pending"


async def test_a_member_flagged_score_individually_is_created_pending(
    engine: AsyncEngine, tuple_id: int, scope: int
) -> None:
    representative, member = await a_job(engine), await a_job(engine)
    async with engine.begin() as conn:
        group = await member_group(conn, representative, [member])
        await filter_result(conn, member, scope, passed=True)
        await conn.execute(
            sa.text(
                "UPDATE duplicate_group_members SET score_individually = true "
                "WHERE group_id = :g AND job_id = :j"
            ),
            {"g": group, "j": member.job_id},
        )
    made = await create(engine, member, tuple_id)
    assert isinstance(made, Created) and made.state == "pending"
    assert (await target_row(engine, made.target_id)).deferred_group_id is None


async def test_a_second_creation_changes_nothing(engine: AsyncEngine, tuple_id: int, scope: int) -> None:
    job = await a_job(engine)
    async with engine.begin() as conn:
        await filter_result(conn, job, scope, passed=True)
        bulk_id = await insert_bulk_operation(conn, tuple_id)
    first = await create(engine, job, tuple_id)
    again = await create(engine, job, tuple_id, bulk_operation_id=bulk_id)
    assert isinstance(first, Created)
    assert again == Unchanged(first.target_id, "pending")
    assert (await target_row(engine, first.target_id)).bulk_operation_id is None  # the existing life is kept


# ---- revival -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("reason", REVIVABLE)
async def test_a_superseded_or_merged_target_revives_into_a_fresh_life(
    engine: AsyncEngine, tuple_id: int, scope: int, reason: str
) -> None:
    # R4, Deviation 7: every column of the old life is reset, the CAS counter kept, the new operation taken
    job = await a_job(engine)
    async with engine.begin() as conn:
        await filter_result(conn, job, scope, passed=True)
        old_bulk = await insert_bulk_operation(conn, tuple_id, state="cancelled")
        new_bulk = await insert_bulk_operation(conn, tuple_id)
        target = await state_target(
            conn,
            job,
            tuple_id,
            "cancelled",
            override="score_now",
            state_reason=reason,
            attempt_no=2,
            charged_attempts=3,
            transient_failures=2,
            not_before=utcnow() + timedelta(hours=1),
            refusal_category="general_harms",
            lane="similarity",
            bulk_operation_id=old_bulk,
        )
        attempt = await insert_attempt(conn, target, attempt_no=2, outcome="cancelled", outcome_detail=reason)
        _, call = await insert_scoring_call(conn, attempt_id=attempt, target_id=target)
        draft = await insert_draft(conn, attempt, call)
        await conn.execute(
            sa.text("UPDATE score_targets SET resume_draft_id = :d WHERE id = :t"), {"d": draft, "t": target}
        )
    revived = await create(engine, job, tuple_id, bulk_operation_id=new_bulk)
    assert revived == Revived(target, "pending")
    row = await target_row(engine, target)
    assert (row.state, row.state_reason, row.override, row.override_at, row.lane) == (
        "pending",
        None,
        "none",
        None,
        None,
    )
    assert (row.charged_attempts, row.transient_failures, row.not_before) == (0, 0, None)
    assert (row.resume_draft_id, row.refusal_category, row.bulk_operation_id) == (None, None, new_bulk)
    assert row.attempt_no == 2  # never reset


async def test_a_revival_is_corrected_by_the_creation_rules(
    engine: AsyncEngine, tuple_id: int, scope: int
) -> None:
    job = await a_job(engine)
    async with engine.begin() as conn:
        await filter_result(conn, job, scope, passed=False)
        target = await state_target(conn, job, tuple_id, "cancelled", state_reason="tuple_superseded")
    revived = await create(engine, job, tuple_id)
    assert revived == Revived(target, "filtered_out")
    assert (await target_row(engine, target)).filter_scope_version_id == scope


async def test_a_purged_or_published_target_never_revives(
    engine: AsyncEngine, tuple_id: int, scope: int
) -> None:
    purged_job, published_job = await a_job(engine), await a_job(engine)
    async with engine.begin() as conn:
        await filter_result(conn, purged_job, scope, passed=True)
        purged = await state_target(conn, purged_job, tuple_id, "cancelled", state_reason="purged")
        published = await insert_published(conn, published_job, tuple_id)
    assert await create(engine, purged_job, tuple_id) == Unchanged(purged, "cancelled")
    assert await create(engine, published_job, tuple_id) == Unchanged(
        published.target_id, "published_accepted"
    )
    assert (await target_row(engine, purged)).state_reason == "purged"


# ---- skipped -----------------------------------------------------------------------------------------------


async def test_a_fenced_merged_or_superseded_revision_is_skipped(engine: AsyncEngine, tuple_id: int) -> None:
    fenced, merged, moved, target_of_merge = [await a_job(engine) for _ in range(4)]
    async with engine.begin() as conn:
        await fence_jobs(conn, fenced.job_id)
        await conn.execute(
            sa.text("UPDATE jobs SET merged_into_job_id = :into WHERE id = :j"),
            {"into": target_of_merge.job_id, "j": merged.job_id},
        )
    assert await create(engine, fenced, tuple_id) == Skipped("fenced")
    assert await create(engine, merged, tuple_id) == Skipped("merged")
    # a revision of another job is no current revision of this one
    assert await create(engine, moved, tuple_id, revision_id=target_of_merge.revision_id) == Skipped(
        "not_current"
    )
    assert await scalar(engine, "SELECT count(*) FROM score_targets") == 0


async def test_a_tuple_that_is_not_the_active_one_creates_nothing(engine: AsyncEngine, tuple_id: int) -> None:
    job = await a_job(engine)
    await execute(
        engine, "UPDATE active_config SET profile_version_id = NULL, llm_tuple_id = NULL WHERE id = 1"
    )
    assert await create(engine, job, tuple_id) == Skipped("tuple_inactive")
    assert await scalar(engine, "SELECT count(*) FROM score_targets") == 0


# ---- similarity --------------------------------------------------------------------------------------------


async def test_similarity_is_the_best_cosine_against_the_profile_vectors(
    engine: AsyncEngine, tuple_id: int, scope: int
) -> None:
    embedded, awaiting = await a_job(engine), await a_job(engine)
    async with engine.begin() as conn:
        profile = await profile_of_tuple(conn, tuple_id)
        await profile_vector_at(conn, profile, "summary", (1023, 1.0))
        await profile_vector_at(conn, profile, "title:0", (0, 1.0))
        await embed_revision(
            conn, embedded.revision_id, await embedding_at(conn, "rev", (0, 0.6), (1023, 0.8))
        )
        for job in (embedded, awaiting):
            await filter_result(conn, job, scope, passed=True)
    made = await create(engine, embedded, tuple_id)
    waiting = await create(engine, awaiting, tuple_id)
    assert isinstance(made, Created) and isinstance(waiting, Created)
    assert (await target_row(engine, made.target_id)).similarity == pytest.approx(0.8, abs=2e-3)
    assert (await target_row(engine, waiting.target_id)).similarity is None
    async with engine.connect() as conn:
        assert await similarity.similarity_for(
            conn, job_revision_id=embedded.revision_id, profile_version_id=profile
        ) == pytest.approx(0.8, abs=2e-3)
        assert (
            await similarity.similarity_for(
                conn, job_revision_id=awaiting.revision_id, profile_version_id=profile
            )
            is None
        )


async def test_a_profile_without_vectors_gives_no_similarity(engine: AsyncEngine, tuple_id: int) -> None:
    job = await a_job(engine)
    async with engine.begin() as conn:
        await embed_revision(conn, job.revision_id, await embedding_at(conn, "rev", (1023, 1.0)))
        profile = await profile_of_tuple(conn, tuple_id)
        assert (
            await similarity.similarity_for(conn, job_revision_id=job.revision_id, profile_version_id=profile)
            is None
        )


async def test_fill_missing_similarity_sets_it_once_the_embedding_exists(
    engine: AsyncEngine, tuple_id: int, scope: int
) -> None:
    jobs = [await a_job(engine) for _ in range(3)]
    async with engine.begin() as conn:
        profile = await profile_of_tuple(conn, tuple_id)
        await profile_vector_at(conn, profile, "summary", (1023, 1.0))
        for job in jobs:
            await filter_result(conn, job, scope, passed=True)
    made = [await create(engine, job, tuple_id) for job in jobs]
    assert all(isinstance(m, Created) for m in made)
    async with engine.begin() as conn:
        for n, job in enumerate(jobs[:2]):
            await embed_revision(
                conn, job.revision_id, await embedding_at(conn, f"rev{n}", (1022, 0.6), (1023, 0.8))
            )
    async with engine.begin() as conn:
        assert await similarity.fill_missing_similarity(conn, llm_tuple_id=tuple_id, limit=1) == 1
    async with engine.begin() as conn:
        assert await similarity.fill_missing_similarity(conn, llm_tuple_id=tuple_id) == 1
        assert await similarity.fill_missing_similarity(conn, llm_tuple_id=tuple_id) == 0  # the third awaits
    values = [(await target_row(engine, m.target_id)).similarity for m in made if isinstance(m, Created)]
    assert values[:2] == [pytest.approx(0.8, abs=2e-3)] * 2 and values[2] is None


# ---- cancellation ------------------------------------------------------------------------------------------


async def test_cancel_targets_moves_every_unpublished_target_of_the_tuple(
    engine: AsyncEngine, tuple_id: int, scope: int
) -> None:
    async with engine.begin() as conn:
        bulk_id = await insert_bulk_operation(conn, tuple_id)
        plain = {
            state: await state_target(conn, await insert_job(conn), tuple_id, state)
            for state in NOT_CANCELLED
        }
        rep, member = await insert_job(conn), await insert_job(conn)
        group = await member_group(conn, rep, [member])
        plain["deferred_duplicate"] = await state_target(
            conn, member, tuple_id, "deferred_duplicate", deferred_group_id=group
        )
        filtered = await state_target(
            conn, await insert_job(conn), tuple_id, "filtered_out", filter_scope_version_id=scope
        )
        failed = await state_target(conn, await insert_job(conn), tuple_id, "failed_request")
        claimed = await insert_claimed(conn, tuple_id)
        submission = await insert_submission(
            conn,
            tuple_id,
            bulk_id,
            state="submitted",
            submit_started_at=utcnow(),
            provider_batch_id="msgbatch_t14_cancel",
        )
        batched_job = await insert_job(conn)
        batched = await state_target(
            conn,
            batched_job,
            tuple_id,
            "in_batch",
            batch_submission_id=submission,
            attempt_no=1,
            lane="batch",
        )
        await insert_attempt(conn, batched, mode="batch", lane="batch")
        await insert_batch_item(conn, submission, batched)
        published = await insert_published(conn, await insert_job(conn), tuple_id)
    async with engine.begin() as conn:
        done = await targets.cancel_targets(
            conn, llm_tuple_id=tuple_id, reason="tuple_superseded", now=utcnow()
        )
    assert done == Cancelled(targets=len(plain) + 3, attempts=1)  # + filtered, failed, claimed
    for target in (*plain.values(), filtered, failed, claimed.target_id):
        row = await target_row(engine, target)
        assert (row.state, row.state_reason) == ("cancelled", "tuple_superseded"), target
        assert (row.deferred_group_id, row.filter_scope_version_id, row.claim_id) == (None, None, None)
    # the claim's attempt ends cancelled with the reason (Controller carry T10-f: never another outcome)
    async with engine.connect() as conn:
        ended = (
            await conn.execute(
                sa.text("SELECT outcome, outcome_detail, ended_at FROM score_attempts WHERE id = :a"),
                {"a": claimed.attempt_id},
            )
        ).one()
    assert (ended.outcome, ended.outcome_detail) == (
        "cancelled",
        "tuple_superseded",
    ) and ended.ended_at is not None
    # the batch cancel owns the in_batch target of a tuple supersession; a published target is terminal
    assert (await target_row(engine, batched)).state == "in_batch"
    assert (await target_row(engine, published.target_id)).state == "published_accepted"
    assert await attempt_outcomes(engine, tuple_id) == {"cancelled": 1, "running": 1, "published_accepted": 1}


async def test_a_revision_supersession_cancels_its_in_batch_target_individually(
    engine: AsyncEngine, tuple_id: int
) -> None:
    # L3979: a revision supersession, a merge or a purge cancels the affected in_batch targets one by one,
    # without cancelling the batch
    async with engine.begin() as conn:
        bulk_id = await insert_bulk_operation(conn, tuple_id)
        submission = await insert_submission(
            conn,
            tuple_id,
            bulk_id,
            state="submitted",
            submit_started_at=utcnow(),
            provider_batch_id="msgbatch_t14_rev",
        )
        job = await insert_job(conn)
        batched = await state_target(
            conn, job, tuple_id, "in_batch", batch_submission_id=submission, attempt_no=1, lane="batch"
        )
        attempt = await insert_attempt(conn, batched, mode="batch", lane="batch")
        await insert_batch_item(conn, submission, batched)
        kept = await state_target(conn, await insert_job(conn), tuple_id, "pending")
    async with engine.begin() as conn:
        done = await targets.cancel_targets(
            conn,
            llm_tuple_id=tuple_id,
            revision_ids=[job.revision_id],
            reason="revision_superseded",
            now=utcnow(),
        )
    assert done == Cancelled(targets=1, attempts=1)
    row = await target_row(engine, batched)
    assert (row.state, row.state_reason, row.batch_submission_id) == (
        "cancelled",
        "revision_superseded",
        None,
    )
    assert (await target_row(engine, kept)).state == "pending"
    assert await scalar(engine, "SELECT outcome FROM score_attempts WHERE id = :a", a=attempt) == "cancelled"
    assert (
        await scalar(engine, "SELECT cancel_requested_at FROM batch_submissions WHERE id = :s", s=submission)
        is None
    )


async def test_cancel_targets_matches_jobs_and_revisions_within_the_tuple(
    engine: AsyncEngine, tuple_id: int
) -> None:
    async with engine.begin() as conn:
        by_job, by_revision, untouched = [await insert_job(conn) for _ in range(3)]
        targets_made = {
            job.job_id: await state_target(conn, job, tuple_id, "pending")
            for job in (by_job, by_revision, untouched)
        }
    async with engine.begin() as conn:
        assert await targets.cancel_targets(
            conn, job_ids=[by_job.job_id], reason="job_merged", now=utcnow()
        ) == Cancelled(1, 0)
        assert await targets.cancel_targets(
            conn, revision_ids=[by_revision.revision_id], reason="purged", now=utcnow()
        ) == Cancelled(1, 0)
    states = {job_id: (await target_row(engine, t)).state_reason for job_id, t in targets_made.items()}
    assert states == {by_job.job_id: "job_merged", by_revision.job_id: "purged", untouched.job_id: None}
    with pytest.raises(ValueError, match="matches every target"):
        async with engine.begin() as conn:
            await targets.cancel_targets(conn, reason="purged", now=utcnow())


async def test_cancel_targets_releases_a_cancelled_follow_up_target_hold_once(
    engine: AsyncEngine, tuple_id: int
) -> None:
    async with engine.begin() as conn:
        target = await state_target(conn, await insert_job(conn), tuple_id, "pending", lane="batch_followup")
        operation, item = await followup_hold(conn, tuple_id, target, hold=Decimal("0.250000"))
        await conn.execute(
            sa.text("UPDATE bulk_operations SET followup_held_usd = 0.4 WHERE id = :b"), {"b": operation}
        )
    async with engine.begin() as conn:
        assert await targets.cancel_targets(
            conn, llm_tuple_id=tuple_id, reason="tuple_superseded", now=utcnow()
        ) == Cancelled(1, 0)
    held = await hold_of(engine, operation, item)
    assert held.followup_held_usd == Decimal("0.150000") and held.hold_released_at is not None
    async with engine.begin() as conn:  # a second cancellation finds nothing to move and releases nothing
        assert await targets.cancel_targets(
            conn, llm_tuple_id=tuple_id, reason="tuple_superseded", now=utcnow()
        ) == Cancelled(0, 0)
    assert (await hold_of(engine, operation, item)).followup_held_usd == Decimal("0.150000")


async def test_cancel_targets_locks_the_bulk_rows_before_its_first_target_row(
    engine: AsyncEngine, migrated: PgServer, tuple_id: int
) -> None:
    # Ruling P52, carry T13-a: a cancellation moves targets not as their owner and releases holds, so the bulk
    # rows of its candidates' holds come first; while one is held, no target row of it is locked yet
    async with engine.begin() as conn:
        held_target = await state_target(conn, await insert_job(conn), tuple_id, "pending")
        other = await state_target(conn, await insert_job(conn), tuple_id, "budget_deferred")
        operation, item = await followup_hold(conn, tuple_id, held_target, hold=Decimal("0.100000"))
    async with engine.connect() as holder:
        await holder.execute(sa.text("BEGIN"))
        await holder.execute(
            q("SELECT id FROM bulk_operations WHERE id = :b FOR NO KEY UPDATE"), {"b": operation}
        )
        holder_pid = int((await holder.execute(sa.text("SELECT pg_backend_pid()"))).scalar_one())
        waiting: asyncio.Future[int] = asyncio.get_running_loop().create_future()

        async def run_cancel() -> Cancelled:
            async with engine.begin() as conn:
                waiting.set_result(int((await conn.execute(sa.text("SELECT pg_backend_pid()"))).scalar_one()))
                return await targets.cancel_targets(
                    conn, llm_tuple_id=tuple_id, reason="tuple_superseded", now=utcnow()
                )

        cancelling = asyncio.create_task(run_cancel())
        pid = await asyncio.wait_for(waiting, 10)

        async def waits() -> bool:
            return cancelling.done() or any(w.pid == pid for w in lock_wait_details(migrated))

        await await_until(waits, 10, 0.02)
        assert not cancelling.done()
        (wait,) = [w for w in lock_wait_details(migrated) if w.pid == pid]
        assert (wait.relation, wait.key, wait.blocking_pids) == (
            "bulk_operations",
            str(operation),
            (holder_pid,),
        )
        async with engine.connect() as probe:  # neither target row is locked yet
            await probe.execute(sa.text("BEGIN"))
            for target in (held_target, other):
                found = await probe.execute(
                    sa.text("SELECT id FROM score_targets WHERE id = :t FOR UPDATE NOWAIT"), {"t": target}
                )
                assert found.scalar_one() == target
            await probe.execute(sa.text("ROLLBACK"))
        await holder.execute(sa.text("ROLLBACK"))
    assert await asyncio.wait_for(cancelling, 10) == Cancelled(2, 0)
    held = await hold_of(engine, operation, item)
    assert held.followup_held_usd == Decimal("0.000000") and held.hold_released_at is not None
