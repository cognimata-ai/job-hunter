"""The ranking bumps of the batch lifecycle on PostgreSQL (spec §7.1 rev 3.6; plan M4 Task 3, B30).

A target state change bumps the ranking generation only when it moves the target to another bucket of the
Jobs list (`ranking.crosses_bucket`). The batch's own moves stay in the unscored bucket, so none of these
transactions bumps: the preparation (`pending → in_batch`), a rejected submission, an abandoned one and a
released one (each `in_batch → pending`; a cancelled target is left as it is). The module's one move across a
bucket, the `bulk_operations` reconciler's exhaustion sweep (`pending → failed_attempts_exhausted`, Carry
T16-h), still bumps, once.

Each test reads the generation before and after (`tests.support.settings.generation`). The targets are three
synthetic ads of the score tuple in one bulk operation, prepared with `batch_min_targets` 1; every datum is
invented.
"""

from typing import Any, Final

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db import ranking
from jobhunter.llm.batches import Ambiguous, Rejected
from jobhunter.llm.gateway import LLMGateway
from jobhunter.scoring import batches, cycles
from jobhunter.settings.dryrun import active_settings
from jobhunter.settings.model import Settings
from jobhunter.worker.operations import OperationClaim
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.batches import BulkTargets, bulk_with_targets, prepared_submission, submission_rows
from tests.support.llm import FakeLLM, _read_execution_config
from tests.support.reconcile import run_reconciler
from tests.support.scoring import targets_by_state
from tests.support.settings import generation

pytestmark = pytest.mark.integration
N: Final = 3
_EXHAUST: Final = sa.text("UPDATE score_targets SET charged_attempts = 4 WHERE bulk_operation_id = :b")


@pytest.fixture
async def gateway(engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int) -> LLMGateway:
    async with engine.connect() as conn:  # `scoring_kw` names the seeded execution config
        await _read_execution_config(conn)
    return fake_llm.gateway(engine)


@pytest.fixture
async def setup(engine: AsyncEngine, current_epoch: int, queue_app: procrastinate.App) -> BulkTargets:
    return await bulk_with_targets(engine, N)


async def small(engine: AsyncEngine) -> Settings:
    """The active settings with `batch_min_targets` 1, unvalidated (a direct `prepare` takes them)."""
    async with engine.connect() as conn:
        active = await active_settings(conn)
    return active.model_copy(update={"scoring": active.scoring.model_copy(update={"batch_min_targets": 1})})


async def prepared(
    engine: AsyncEngine, gateway: LLMGateway, setup: BulkTargets
) -> tuple[int, OperationClaim]:
    """The setup's targets prepared into one `preparing` submission: its id and the operation's claim."""
    result, claim = await prepared_submission(engine, gateway, setup, settings=await small(engine))
    assert isinstance(result, batches.Prepared), result
    return result.submission_id, claim


async def started(engine: AsyncEngine, submission_id: int, claim: OperationClaim) -> None:
    async with engine.begin() as conn:
        assert (
            await batches.submit_start(conn, submission_id, operation=claim, now=utcnow()) == "submit_started"
        )


def test_the_batch_s_own_moves_stay_in_the_unscored_bucket() -> None:
    assert not ranking.crosses_bucket("pending", "in_batch")
    assert not ranking.crosses_bucket("in_batch", "pending")
    assert ranking.crosses_bucket("pending", "failed_attempts_exhausted")


async def test_the_preparation_does_not_bump(
    engine: AsyncEngine, gateway: LLMGateway, setup: BulkTargets
) -> None:
    before = await generation(engine)
    await prepared(engine, gateway, setup)
    assert await targets_by_state(engine, setup.llm_tuple_id) == {"in_batch": N}
    assert await generation(engine) == before


async def test_a_rejected_submission_does_not_bump(
    engine: AsyncEngine, gateway: LLMGateway, setup: BulkTargets
) -> None:
    submission_id, claim = await prepared(engine, gateway, setup)
    await started(engine, submission_id, claim)
    before = await generation(engine)
    async with engine.begin() as conn:
        rejected = Rejected(400, "invalid_request_error", "test: refused")
        await batches.record_rejected(conn, submission_id, rejected, operation=claim, now=utcnow())
    assert [row.state for row in await submission_rows(engine)] == ["submit_rejected"]
    assert await targets_by_state(engine, setup.llm_tuple_id) == {"pending": N}
    assert await generation(engine) == before


async def test_an_abandoned_submission_does_not_bump(
    engine: AsyncEngine, gateway: LLMGateway, setup: BulkTargets
) -> None:
    submission_id, _ = await prepared(engine, gateway, setup)
    before = await generation(engine)
    async with engine.begin() as conn:
        assert await batches.abandon(conn, submission_id, now=utcnow()) is True
    assert [row.state for row in await submission_rows(engine)] == ["abandoned"]
    assert await targets_by_state(engine, setup.llm_tuple_id) == {"pending": N}
    assert await generation(engine) == before


async def test_a_released_submission_does_not_bump(
    engine: AsyncEngine, gateway: LLMGateway, setup: BulkTargets
) -> None:
    submission_id, claim = await prepared(engine, gateway, setup)
    await started(engine, submission_id, claim)
    async with engine.begin() as conn:
        await batches.record_ambiguous(
            conn, submission_id, Ambiguous("test_lost_answer"), operation=claim, now=utcnow()
        )
    before = await generation(engine)
    async with engine.begin() as conn:  # the route's first lock (Task 21)
        await cycles.lock_active_scoring(conn)
        await batches.release(conn, submission_id, now=utcnow(), defer=None)
    assert [row.state for row in await submission_rows(engine)] == ["released"]
    assert await targets_by_state(engine, setup.llm_tuple_id) == {"pending": N}
    assert await generation(engine) == before


async def test_the_exhaustion_sweep_crosses_a_bucket_and_bumps_once(
    engine: AsyncEngine, current_epoch: int, queue_app: procrastinate.App
) -> None:
    """Carry T16-h: the `bulk_operations` reconciler's request again sweeps the targets whose charged
    attempts are used up, `pending → failed_attempts_exhausted` (the failed bucket), and bumps once."""
    made = await bulk_with_targets(engine, N, operation=False)
    async with engine.begin() as conn:
        await conn.execute(_EXHAUST, {"b": made.bulk_operation_id})
    before = await generation(engine)
    repaired: Any = await run_reconciler("bulk_operations", engine, now=utcnow(), epoch=current_epoch)
    assert repaired == 1
    assert await targets_by_state(engine, made.llm_tuple_id) == {"failed_attempts_exhausted": N}
    assert await generation(engine) == before + 1
