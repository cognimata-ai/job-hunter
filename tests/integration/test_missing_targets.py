"""The `missing_targets` reconciler on PostgreSQL (spec §9.7, §17.4, §7.17 #2, §20.2 L6358; plan Task 14,
L3718, L5509, L5714, C36): `scoring.targets.reconcile_missing_targets`.

For the active tuple, every current revision of a retained, unmerged job without a live target gets one
(created, or a superseded one revived), in chunks of 500; a fenced job is skipped; a published target is left
as it is, so the reconciler never causes a second paid score of retained work (the fake LLM serves nothing);
while the tuple's `create_targets_for_tuple` operation is live it leaves the creation to it; the worker
epoch fences it.

Every datum is invented.
"""

import dataclasses
from datetime import datetime
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.scoring import targets
from jobhunter.worker.operations import create_operation
from jobhunter.worker.reconcilers import ReconcileContext
from jobhunter.worker.tasks import defer_in_tx, task_app
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.llm import FakeLLM
from tests.support.m3 import insert_job, insert_published
from tests.support.reconcile import assert_fence_rule, run_reconciler
from tests.support.scoring import (
    activate_score_tuple,
    fence_jobs,
    revisioned_jobs,
    state_target,
    target_row,
    targets_by_state,
)
from tests.support.settings import generation

pytestmark = pytest.mark.integration


@pytest.fixture
def now() -> datetime:
    return utcnow()


@pytest.fixture
async def tuple_id(engine: AsyncEngine) -> int:
    async with engine.begin() as conn:
        return await activate_score_tuple(conn)


async def reconcile(engine: AsyncEngine, now: datetime, epoch: int) -> int:
    return await run_reconciler("missing_targets", engine, now=now, epoch=epoch)


async def test_a_missing_target_is_created_and_a_fenced_job_skipped(
    engine: AsyncEngine,
    current_epoch: int,
    queue_app: object,
    tuple_id: int,
    now: datetime,
    fake_llm: FakeLLM,
) -> None:
    async with engine.begin() as conn:
        missing = await insert_job(conn)
        fenced = await insert_job(conn)
        published_job = await insert_job(conn)
        superseded = await insert_job(conn)
        await fence_jobs(conn, fenced.job_id)
        published = await insert_published(conn, published_job, tuple_id)
        revivable = await state_target(
            conn, superseded, tuple_id, "cancelled", state_reason="revision_superseded"
        )
    before = await generation(engine)
    assert await reconcile(engine, now, current_epoch) == 2  # one created, one revived
    assert await targets_by_state(engine, tuple_id) == {"pending": 2, "published_accepted": 1}
    async with engine.connect() as conn:
        by_job: dict[Any, Any] = dict(
            (
                await conn.execute(
                    sa.text("SELECT job_id, state FROM score_targets WHERE llm_tuple_id = :t"),
                    {"t": tuple_id},
                )
            ).all()
        )
    assert by_job == {
        missing.job_id: "pending",
        superseded.job_id: "pending",
        published_job.job_id: "published_accepted",
    }
    assert (await target_row(engine, revivable)).state == "pending"
    assert (await target_row(engine, published.target_id)).state == "published_accepted"
    assert await generation(engine) == before + 1
    # retained work is never scored twice: nothing reached the LLM, and a second pass finds nothing
    assert await reconcile(engine, now, current_epoch) == 0
    assert fake_llm.served == 0


async def test_it_works_in_chunks_of_500(
    engine: AsyncEngine, current_epoch: int, queue_app: object, tuple_id: int, now: datetime
) -> None:
    await revisioned_jobs(engine, 520)
    assert await reconcile(engine, now, current_epoch) == 520
    assert await targets_by_state(engine, tuple_id) == {"pending": 520}


async def test_it_leaves_the_creation_to_a_live_create_targets_for_tuple(
    engine: AsyncEngine, current_epoch: int, queue_app: object, tuple_id: int, now: datetime
) -> None:
    async with engine.begin() as conn:
        await insert_job(conn)
        await create_operation(
            conn,
            kind="create_targets_for_tuple",
            payload={"llm_tuple_id": tuple_id, "bulk_operation_id": 0},
            dedup_key=f"create_targets_for_tuple:{tuple_id}",
            defer=None,
        )
    assert await reconcile(engine, now, current_epoch) == 0
    assert await targets_by_state(engine, tuple_id) == {}


async def test_without_an_active_tuple_it_does_nothing(
    engine: AsyncEngine, current_epoch: int, queue_app: object, now: datetime
) -> None:
    async with engine.begin() as conn:
        await insert_job(conn)
    assert await reconcile(engine, now, current_epoch) == 0
    async with engine.connect() as conn:
        assert (await conn.execute(sa.text("SELECT count(*) FROM score_targets"))).scalar_one() == 0


async def test_the_reconciler_follows_the_fence_rule(
    engine: AsyncEngine, current_epoch: int, queue_app: object, tuple_id: int, now: datetime
) -> None:
    async with engine.begin() as conn:
        job = await insert_job(conn)
    current = ReconcileContext(
        engine=engine, queue=task_app(), defer=defer_in_tx, now=now, epoch=current_epoch
    )
    superseded = dataclasses.replace(current, epoch=current_epoch + 1)
    await assert_fence_rule("missing_targets", "heavy", superseded, current, targets)
    async with engine.connect() as conn:  # neither check wrote a target
        found = await conn.execute(
            sa.text("SELECT count(*) FROM score_targets WHERE job_id = :j"), {"j": job.job_id}
        )
        assert found.scalar_one() == 0
