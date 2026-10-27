"""The `create_targets_for_tuple` operation on PostgreSQL (spec §12.6 step 4, §9.7, §10.10, §6.9.3; plan Task
14, Codex r1 #32, r3 B20, A2 R4): its handler in `scoring.targets`, run through `execute_operation`.

The handler creates (or revives) a target of the tuple for every current revision of a retained, unmerged,
non-synthetic job, in chunks of 500, `active` jobs first, then the saved or applied ones, then the rest, each
group by job id; it records `{"done", "total"}` after every chunk; a tuple superseded meanwhile ends it
`succeeded` as obsolete; and once every chunk is done, with the Batches branch on and at least
`batch_min_targets` pending targets of the operation, it records the `batch_submit` operation, whose job is
deferred under the `batch` lock at every enqueue.

Every datum is invented.
"""

import asyncio
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.transitions import transition
from jobhunter.scoring import targets
from jobhunter.types import JSONValue
from jobhunter.worker import operations
from jobhunter.worker.operations import create_operation, execute_operation
from jobhunter.worker.tasks import defer_in_tx, job_task_name
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.faults import PausePoint
from tests.support.m3 import insert_bulk_operation, insert_job
from tests.support.reconcile import run_reconciler
from tests.support.scoring import (
    activate_score_tuple,
    activate_scoring_settings,
    operations_of,
    revisioned_jobs,
    targets_by_state,
)

pytestmark = pytest.mark.integration

AFTER_CHUNK = "create_targets.after_chunk"
_BATCH_JOB = sa.text(
    "SELECT lock, queueing_lock FROM procrastinate_jobs WHERE task_name = :task "
    "AND args @> jsonb_build_object('operation_id', CAST(:i AS bigint)) AND status = 'todo'"
)


@pytest.fixture
async def tuple_id(engine: AsyncEngine) -> int:
    async with engine.begin() as conn:
        return await activate_score_tuple(conn)


@pytest.fixture
async def bulk_id(engine: AsyncEngine, tuple_id: int) -> int:
    async with engine.begin() as conn:
        return await insert_bulk_operation(conn, tuple_id)


async def request(engine: AsyncEngine, tuple_id: int, bulk_id: int) -> int:
    """The operation as `activate_tuple` records it (its job left to the test's own execution)."""
    async with engine.begin() as conn:
        op_id, created = await create_operation(
            conn,
            kind="create_targets_for_tuple",
            payload={"llm_tuple_id": tuple_id, "bulk_operation_id": bulk_id},
            dedup_key=f"create_targets_for_tuple:{tuple_id}",
            defer=None,
        )
    assert created
    return op_id


async def run(engine: AsyncEngine, op_id: int, epoch: int) -> Any:
    await execute_operation(engine, op_id, epoch=epoch)
    async with engine.connect() as conn:
        return (
            await conn.execute(sa.text("SELECT status, progress FROM operations WHERE id = :i"), {"i": op_id})
        ).one()


async def creation_order(engine: AsyncEngine, tuple_id: int) -> list[int]:
    """The jobs of the tuple's targets in the order their targets were made."""
    async with engine.connect() as conn:
        found = await conn.execute(
            sa.text("SELECT job_id FROM score_targets WHERE llm_tuple_id = :t ORDER BY id"), {"t": tuple_id}
        )
        return [int(j) for j in found.scalars()]


async def test_active_jobs_come_first_then_the_saved_or_applied_then_the_rest(
    engine: AsyncEngine, current_epoch: int, tuple_id: int, bulk_id: int
) -> None:
    async with engine.begin() as conn:
        made = {}
        for name, availability in (
            ("rest_a", "stale"),
            ("saved", "closed"),
            ("active_a", "active"),
            ("applied", "unknown"),
            ("active_b", "active"),
            ("rest_b", "closed"),
        ):
            job = await insert_job(conn)
            await conn.execute(
                sa.text("UPDATE jobs SET availability = :a WHERE id = :j"),
                {"a": availability, "j": job.job_id},
            )
            made[name] = job.job_id
        await conn.execute(
            sa.text("INSERT INTO user_job_actions (job_id, saved_at) VALUES (:j, now())"),
            {"j": made["saved"]},
        )
        await conn.execute(
            sa.text("INSERT INTO user_job_actions (job_id, applied_at, seen_at) VALUES (:j, now(), now())"),
            {"j": made["applied"]},
        )
        await conn.execute(  # a seen job is not a saved one
            sa.text("INSERT INTO user_job_actions (job_id, seen_at) VALUES (:j, now())"),
            {"j": made["rest_a"]},
        )
        synthetic = await insert_job(conn, synthetic_eval=True)  # an adversarial golden case: never a target
        merged = await insert_job(conn, merged_into_job_id=made["rest_b"])
    ended = await run(engine, await request(engine, tuple_id, bulk_id), current_epoch)
    assert ended.status == "succeeded"
    names = ["active_a", "active_b", "saved", "applied", "rest_a", "rest_b"]
    assert await creation_order(engine, tuple_id) == [made[name] for name in names]
    assert {synthetic.job_id, merged.job_id}.isdisjoint(await creation_order(engine, tuple_id))
    assert ended.progress == {"done": 6, "total": 6}
    async with engine.connect() as conn:
        bulk_ids = (
            await conn.execute(
                sa.text("SELECT DISTINCT bulk_operation_id FROM score_targets WHERE llm_tuple_id = :t"),
                {"t": tuple_id},
            )
        ).scalars()
        assert list(bulk_ids) == [bulk_id]


async def test_every_chunk_records_its_progress(
    engine: AsyncEngine,
    current_epoch: int,
    tuple_id: int,
    bulk_id: int,
    monkeypatch: pytest.MonkeyPatch,
    queue_app: object,  # 620 pending targets: the batch_submit job is deferred at the end
) -> None:
    await revisioned_jobs(engine, 620)
    recorded: list[Mapping[str, JSONValue]] = []
    set_progress = operations.set_progress  # the name `targets` calls

    async def spy(
        conn: AsyncConnection, claim: operations.OperationClaim, progress: Mapping[str, JSONValue]
    ) -> None:
        recorded.append(dict(progress))
        await set_progress(conn, claim, progress)

    monkeypatch.setattr(targets, "set_progress", spy)
    ended = await run(engine, await request(engine, tuple_id, bulk_id), current_epoch)
    assert recorded == [{"done": 500, "total": 620}, {"done": 620, "total": 620}]
    assert (ended.status, ended.progress) == ("succeeded", {"done": 620, "total": 620})
    assert await targets_by_state(engine, tuple_id) == {"pending": 620}


async def test_a_tuple_superseded_mid_way_ends_the_operation_obsolete(
    engine: AsyncEngine, current_epoch: int, tuple_id: int, bulk_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    await revisioned_jobs(engine, 520)
    pause = PausePoint(AFTER_CHUNK)
    monkeypatch.setattr(targets, "afault_point", pause)
    op_id = await request(engine, tuple_id, bulk_id)
    running = asyncio.create_task(run(engine, op_id, current_epoch))
    await asyncio.wait_for(pause.reached.wait(), 30)
    async with engine.begin() as conn:  # a confirm makes another tuple active meanwhile
        await conn.execute(
            sa.text("UPDATE active_config SET profile_version_id = NULL, llm_tuple_id = NULL WHERE id = 1")
        )
    pause.resume()
    ended = await asyncio.wait_for(running, 30)
    assert (ended.status, ended.progress) == ("succeeded", {"skipped": "tuple_superseded"})
    assert await targets_by_state(engine, tuple_id) == {"pending": 500}  # the first chunk only


async def test_batch_submit_is_recorded_at_batch_min_targets_and_its_job_takes_the_batch_lock(
    engine: AsyncEngine, current_epoch: int, tuple_id: int, bulk_id: int, queue_app: object
) -> None:
    jobs = await revisioned_jobs(engine, 200)
    async with engine.begin() as conn:  # one job's ad language is excluded: 199 targets end up pending
        await conn.execute(
            sa.text("UPDATE jobs SET ad_language = 'other' WHERE id = :j"), {"j": jobs[0].job_id}
        )
    assert (await run(engine, await request(engine, tuple_id, bulk_id), current_epoch)).status == "succeeded"
    assert await targets_by_state(engine, tuple_id) == {"pending": 199, "filtered_out": 1}
    assert await operations_of(engine, "batch_submit") == []  # 199 < batch_min_targets (200)

    async with engine.begin() as conn:  # the 200th is pending again; the Batches branch is switched off
        found = await conn.execute(
            sa.text("SELECT id FROM score_targets WHERE job_id = :j AND llm_tuple_id = :t"),
            {"j": jobs[0].job_id, "t": tuple_id},
        )
        await transition(conn, "score_targets", int(found.scalar_one()), ["filtered_out"], "pending")
        await activate_scoring_settings(conn, batch_bulk_rescore=False)
    assert (await run(engine, await request(engine, tuple_id, bulk_id), current_epoch)).status == "succeeded"
    assert await operations_of(engine, "batch_submit") == []

    async with engine.begin() as conn:
        await activate_scoring_settings(conn, batch_bulk_rescore=True)
    assert (await run(engine, await request(engine, tuple_id, bulk_id), current_epoch)).status == "succeeded"
    (submit,) = await operations_of(engine, "batch_submit")
    assert (submit.status, submit.payload) == (
        "pending",
        {"bulk_operation_id": bulk_id, "llm_tuple_id": tuple_id},
    )
    async with engine.connect() as conn:
        dedup = (
            await conn.execute(sa.text("SELECT dedup_key FROM operations WHERE id = :i"), {"i": submit.id})
        ).scalar_one()
        job = (await conn.execute(_BATCH_JOB, {"task": job_task_name("run_operation"), "i": submit.id})).one()
    assert dedup == f"batch_submit:{bulk_id}:1"
    assert (job.lock, job.queueing_lock) == ("batch", f"op:{submit.id}")  # OPERATION_LOCKS, first enqueue

    # Codex r3 B20: the `operations` reconciler's re-enqueue of a lost job keeps the lock
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "DELETE FROM procrastinate_jobs WHERE task_name = :task "
                "AND args @> jsonb_build_object('operation_id', CAST(:i AS bigint))"
            ),
            {"task": job_task_name("run_operation"), "i": submit.id},
        )
    assert (
        await run_reconciler("operations", engine, now=utcnow() + timedelta(minutes=5), epoch=current_epoch)
        >= 1
    )
    async with engine.connect() as conn:
        again = (
            await conn.execute(_BATCH_JOB, {"task": job_task_name("run_operation"), "i": submit.id})
        ).one()
    assert again.lock == "batch"


async def test_an_operation_of_another_kind_is_deferred_without_a_lock(
    engine: AsyncEngine, tuple_id: int, bulk_id: int, queue_app: object
) -> None:
    async with engine.begin() as conn:
        op_id, _ = await create_operation(
            conn,
            kind="create_targets_for_tuple",
            payload={"llm_tuple_id": tuple_id, "bulk_operation_id": bulk_id},
            dedup_key=f"create_targets_for_tuple:{tuple_id}",
            defer=defer_in_tx,
        )
    async with engine.connect() as conn:
        job = (await conn.execute(_BATCH_JOB, {"task": job_task_name("run_operation"), "i": op_id})).one()
    assert (job.lock, job.queueing_lock) == (None, f"op:{op_id}")
