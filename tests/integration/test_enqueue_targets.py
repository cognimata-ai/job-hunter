"""Node 9, `enqueue_targets`, and I5 on the run's tuple (spec §9.2 node 9, §9.7, §17.2 I5, §17.3; plan Task
14, R9, Deviation 9, A2 R2): `graphs.collect.nodes.enqueue_targets` and `finalize`'s I5.

With the run's tuple active, the node gives every current revision of the run's jobs (created or changed in
the run) a target of that tuple, its similarity from the profile's vectors, and cancels the unpublished
targets of the revisions that stopped being current (`revision_superseded`); a fenced job is skipped and
counted. When a confirm superseded the run's tuple meanwhile, the node creates nothing (the confirm's
`create_targets_for_tuple` owns the new tuple) and I5 passes with `"targets": "tuple_superseded"`. I5 fails
for a revision of the run left without a target. The run report's `revisions.targets` counts what the node
did.

Every datum is invented.
"""

import asyncio
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.graphs.collect.nodes.enqueue_targets import make_enqueue_targets
from jobhunter.graphs.collect.nodes.finalize import InvariantResult, check_invariants, run_revisions
from jobhunter.graphs.collect.state import CollectUpdate, initial_state
from jobhunter.sources.base import SourceAdapter
from jobhunter.sources.instances import InstanceRow
from jobhunter.worker.runs import RunOwner
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import make_deps, queued_run, run_to_the_end
from tests.support.m3 import (
    Job,
    insert_attempt,
    insert_batch_item,
    insert_bulk_operation,
    insert_claimed,
    insert_job,
    insert_published,
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
    followup_hold,
    hold_of,
    member_group,
    profile_of_tuple,
    profile_vector_at,
    state_target,
    target_row,
)
from tests.support.settings import confirm_new_profile

pytestmark = pytest.mark.integration

_REVISION = sa.text(
    "INSERT INTO job_revisions (job_id, revision_no, material_hash, llm_input, description_text, "
    "description_completeness, ad_language, embedding_status) VALUES (:j, :n, :h, '{}', "
    "'Erfundene Testanzeige, neu.', 'full', 'de', 'awaiting_embedding') RETURNING id"
)
# a job's current revision changes, as identity changes it (`jobs.updated_at` with it)
_CURRENT = sa.text("UPDATE jobs SET current_revision_id = :r, updated_at = now() WHERE id = :j")
_TARGETS_OF_JOB = sa.text(
    "SELECT job_revision_id, state, state_reason FROM score_targets WHERE job_id = :j AND llm_tuple_id = :t "
    "ORDER BY job_revision_id"
)


def no_adapters(instance: InstanceRow) -> SourceAdapter:
    raise AssertionError("node 9 fetches nothing")


async def run_node(engine: AsyncEngine, owner: RunOwner, tuple_id: int | None) -> CollectUpdate:
    node = make_enqueue_targets(make_deps(engine, no_adapters))
    return await node({**initial_state(owner.run_id, owner.attempt_no), "llm_tuple_id": tuple_id})


async def i5(engine: AsyncEngine, owner: RunOwner) -> InvariantResult:
    async with engine.connect() as conn:
        revisions = await run_revisions(conn, owner.run_id)
        results = await check_invariants(conn, owner.run_id, outcome_conflicts=0, revisions=revisions)
    return {r.code: r for r in results}["I5"]


async def new_revision(engine: AsyncEngine, job: Job) -> int:
    async with engine.begin() as conn:
        revision = int((await conn.execute(_REVISION, {"j": job.job_id, "n": 2, "h": b"\x02"})).scalar_one())
        await conn.execute(_CURRENT, {"r": revision, "j": job.job_id})
    return revision


async def targets_of_job(engine: AsyncEngine, job: Job, tuple_id: int) -> list[tuple[Any, ...]]:
    async with engine.connect() as conn:
        return [tuple(r) for r in await conn.execute(_TARGETS_OF_JOB, {"j": job.job_id, "t": tuple_id})]


@pytest.fixture
async def tuple_id(engine: AsyncEngine) -> int:
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        await profile_vector_at(conn, await profile_of_tuple(conn, tuple_id), "summary", (1023, 1.0))
    return tuple_id


async def test_a_run_creates_targets_for_its_tuple_with_similarity(
    engine: AsyncEngine, current_epoch: int, tuple_id: int
) -> None:
    async with engine.begin() as conn:
        before_run = await insert_job(conn)  # not the run's: no target
    owner = await queued_run(engine)
    async with engine.begin() as conn:
        embedded = await insert_job(conn)
        awaiting = await insert_job(conn)
        fenced = await insert_job(conn)
        excluded = await insert_job(conn)  # an ad language the scope excludes: created filtered_out
        member = await insert_job(conn)  # a member of before_run's group: created deferred_duplicate
        await conn.execute(
            sa.text("UPDATE jobs SET ad_language = 'other' WHERE id = :j"), {"j": excluded.job_id}
        )
        await member_group(conn, before_run, [member])
        await embed_revision(
            conn, embedded.revision_id, await embedding_at(conn, "run", (0, 0.6), (1023, 0.8))
        )
        await fence_jobs(conn, fenced.job_id)
    assert (await i5(engine, owner)).ok is False  # four revisions of the run have no target yet
    update = await run_node(engine, owner, tuple_id)
    assert update["steps"] == ["enqueue_targets"]
    assert update["counters"] == {
        "targets_created": 4,
        "targets_skipped_fenced": 1,
        # Ruling T14-a: the report's "targets created by state" (§17.3 L5489)
        "targets_state_deferred_duplicate": 1,
        "targets_state_filtered_out": 1,
        "targets_state_pending": 2,
    }
    made = {
        job.job_id: await targets_of_job(engine, job, tuple_id)
        for job in (embedded, awaiting, fenced, before_run, excluded, member)
    }
    assert made[embedded.job_id] == [(embedded.revision_id, "pending", None)]
    assert made[awaiting.job_id] == [(awaiting.revision_id, "pending", None)]
    assert made[excluded.job_id] == [(excluded.revision_id, "filtered_out", None)]
    assert made[member.job_id] == [(member.revision_id, "deferred_duplicate", None)]
    assert made[fenced.job_id] == [] and made[before_run.job_id] == []
    async with engine.connect() as conn:
        similarities: dict[Any, Any] = dict(
            (
                await conn.execute(
                    sa.text("SELECT job_id, similarity FROM score_targets WHERE llm_tuple_id = :t"),
                    {"t": tuple_id},
                )
            ).all()
        )
    assert (
        similarities[embedded.job_id] == pytest.approx(0.8, abs=2e-3)
        and similarities[awaiting.job_id] is None
    )
    assert await i5(engine, owner) == InvariantResult(
        "I5", True, {"llm_tuple_id": tuple_id, "revisions": 4, "missing": 0}
    )
    # a second pass (a database retry, a resumed attempt) finds nothing to do
    assert (await run_node(engine, owner, tuple_id))["counters"] == {"targets_skipped_fenced": 1}


async def test_the_superseded_revisions_unpublished_target_is_cancelled(
    engine: AsyncEngine, current_epoch: int, tuple_id: int
) -> None:
    async with engine.begin() as conn:
        changed, republished = await insert_job(conn), await insert_job(conn)
        old_target = await state_target(conn, changed, tuple_id, "pending")
        published = await insert_published(conn, republished, tuple_id)
    owner = await queued_run(engine)
    changed_revision = await new_revision(engine, changed)
    republished_revision = await new_revision(engine, republished)
    update = await run_node(engine, owner, tuple_id)
    assert update["counters"] == {"targets_created": 2, "targets_cancelled": 1, "targets_state_pending": 2}
    assert await targets_of_job(engine, changed, tuple_id) == [
        (changed.revision_id, "cancelled", "revision_superseded"),
        (changed_revision, "pending", None),
    ]
    assert await targets_of_job(engine, republished, tuple_id) == [
        (republished.revision_id, "published_accepted", None),  # a published target is terminal
        (republished_revision, "pending", None),
    ]
    assert (await target_row(engine, old_target)).state == "cancelled"
    assert (await target_row(engine, published.target_id)).state == "published_accepted"


async def test_i5_passes_after_a_mid_run_confirm(
    engine: AsyncEngine, current_epoch: int, tuple_id: int, queue_app: object
) -> None:
    # R9: a confirm during the run makes another tuple active; the run's own tuple is superseded, and the
    # confirm's create_targets_for_tuple owns the new one
    owner = await queued_run(engine)
    async with engine.begin() as conn:
        job = await insert_job(conn)
    _, new_tuple = await confirm_new_profile(engine)
    update = await run_node(engine, owner, tuple_id)
    assert update["counters"] == {"targets_skipped_tuple_superseded": 1}
    assert (
        await targets_of_job(engine, job, tuple_id) == []
        and await targets_of_job(engine, job, new_tuple) == []
    )
    assert await i5(engine, owner) == InvariantResult(
        "I5", True, {"llm_tuple_id": tuple_id, "targets": "tuple_superseded"}
    )


async def test_i5_is_vacuous_for_a_run_without_a_tuple(engine: AsyncEngine, current_epoch: int) -> None:
    owner = await queued_run(engine)  # no profile is active: the run has no tuple
    async with engine.begin() as conn:
        await insert_job(conn)
    assert (await run_node(engine, owner, None)) == {"steps": ["enqueue_targets"]}
    assert await i5(engine, owner) == InvariantResult("I5", True, {"llm_tuple_id": None})


async def test_a_run_reports_the_targets_it_made(
    engine: AsyncEngine,
    current_epoch: int,
    tuple_id: int,
    fake_http: object,
    fake_embedder: object,
    collect_deps: Any,
) -> None:
    rid = await run_to_the_end(engine, collect_deps)
    async with engine.connect() as c:
        status, report = (
            await c.execute(sa.text("SELECT status, report FROM runs WHERE id = :i"), {"i": rid})
        ).one()
        by_state = {
            str(state): int(n)
            for state, n in await c.execute(
                sa.text("SELECT state, count(*) FROM score_targets WHERE llm_tuple_id = :t GROUP BY state"),
                {"t": tuple_id},
            )
        }
    targets = sum(by_state.values())
    assert status == "succeeded" and targets > 0
    assert report["revisions"]["targets"] == {
        "created": targets,
        "revived": 0,
        "cancelled": 0,
        "skipped": {},
        "by_state": by_state,  # Ruling T14-a: the run's created and revived targets by their state
    }
    assert report["execution"]["invariants"]["I5"] == {
        "ok": True,
        "detail": {"llm_tuple_id": tuple_id, "revisions": targets, "missing": 0},
    }


async def test_a_superseded_revisions_in_progress_and_in_batch_targets_are_cancelled(
    engine: AsyncEngine, current_epoch: int, tuple_id: int
) -> None:
    # §7.16.3 and L3979: a revision supersession cancels the claim (its attempt ends `cancelled`) and the
    # batched target individually, without cancelling the batch
    async with engine.begin() as conn:
        claimed = await insert_claimed(conn, tuple_id)
        of_claim = sa.text("SELECT job_id, job_revision_id FROM score_targets WHERE id = :t")
        found = (await conn.execute(of_claim, {"t": claimed.target_id})).one()
        claimed_job = Job(int(found.job_id), int(found.job_revision_id))
        bulk_id = await insert_bulk_operation(conn, tuple_id)
        submission = await insert_submission(
            conn,
            tuple_id,
            bulk_id,
            state="submitted",
            submit_started_at=utcnow(),
            provider_batch_id="msgbatch_t14_n9",
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
    owner = await queued_run(engine)
    await new_revision(engine, claimed_job)
    await new_revision(engine, batched_job)
    update = await run_node(engine, owner, tuple_id)
    assert update["counters"] == {"targets_created": 2, "targets_cancelled": 2, "targets_state_pending": 2}
    claim = await target_row(engine, claimed.target_id)
    assert (claim.state, claim.state_reason, claim.claim_id) == ("cancelled", "revision_superseded", None)
    in_batch = await target_row(engine, batched)
    assert (in_batch.state, in_batch.state_reason, in_batch.batch_submission_id) == (
        "cancelled",
        "revision_superseded",
        None,
    )
    assert await attempt_outcomes(engine, tuple_id) == {"cancelled": 2}
    async with engine.connect() as conn:
        submitted = (
            await conn.execute(
                sa.text("SELECT state, cancel_requested_at FROM batch_submissions WHERE id = :s"),
                {"s": submission},
            )
        ).one()
    assert tuple(submitted) == ("submitted", None)  # the batch itself goes on


async def test_node_9_locks_the_bulk_rows_before_its_first_target_row(
    engine: AsyncEngine, migrated: PgServer, current_epoch: int, tuple_id: int
) -> None:
    # Ruling P52, carry T13-a: the superseded revision's cancellation (bulk rows first) runs before the
    # creations, so while a bulk row of it is held no target row is locked yet, not even the revivable
    # target of the job's new current revision
    async with engine.begin() as conn:
        job = await insert_job(conn)
        old_target = await state_target(conn, job, tuple_id, "pending", lane="batch_followup")
        operation, item = await followup_hold(conn, tuple_id, old_target, hold=Decimal("0.100000"))
    owner = await queued_run(engine)
    revision = await new_revision(engine, job)
    async with engine.begin() as conn:  # the new revision had a target once: a revival, not an insert
        revivable = await state_target(
            conn, Job(job.job_id, revision), tuple_id, "cancelled", state_reason="revision_superseded"
        )
    async with engine.connect() as holder:
        await holder.execute(sa.text("BEGIN"))
        await holder.execute(
            sa.text("SELECT id FROM bulk_operations WHERE id = :b FOR NO KEY UPDATE"), {"b": operation}
        )
        holder_pid = int((await holder.execute(sa.text("SELECT pg_backend_pid()"))).scalar_one())
        before = {int(w.pid) for w in lock_wait_details(migrated)}
        running = asyncio.create_task(run_node(engine, owner, tuple_id))

        async def waits() -> bool:
            return running.done() or any(
                w.relation == "bulk_operations" and w.blocking_pids == (holder_pid,)
                for w in lock_wait_details(migrated)
                if w.pid not in before
            )

        await await_until(waits, 10, 0.02)
        assert not running.done()
        async with engine.connect() as probe:  # neither target row is locked yet
            await probe.execute(sa.text("BEGIN"))
            for target in (old_target, revivable):
                found = await probe.execute(
                    sa.text("SELECT id FROM score_targets WHERE id = :t FOR UPDATE NOWAIT"), {"t": target}
                )
                assert found.scalar_one() == target
            await probe.execute(sa.text("ROLLBACK"))
        await holder.execute(sa.text("ROLLBACK"))
    update = await asyncio.wait_for(running, 10)
    assert update["counters"] == {"targets_cancelled": 1, "targets_revived": 1, "targets_state_pending": 1}
    assert (await target_row(engine, revivable)).state == "pending"
    held = await hold_of(engine, operation, item)
    assert held.followup_held_usd == Decimal("0.000000") and held.hold_released_at is not None
