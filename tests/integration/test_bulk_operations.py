"""The bulk operations on PostgreSQL (spec §6.9.3, §17.4; plan Task 14, R5, Deviation 8, A1 issue 19, Ruling
P9, Codex r4 B8): `llm.bulk.open`, `cancel`, `complete_if_done` and the `bulk_operations` reconciler.

`open` fixes the budget at `bulk_rescore_max_budget_share` of the scoring headroom of the month (the row
created when the month is fresh), the figure `dryrun.bulk_budget` gives; `complete_if_done` completes an
active operation only once its `create_targets_for_tuple` operation has ended, no target of it is `pending`,
`in_progress` or `in_batch` and no submission of it is in flight (a `budget_deferred` or `deferred_duplicate`
target does not hold it open), and then zeroes its follow-up holds and stamps every unreleased item; the
heavy reconciler applies it to the active operation.

Every datum is invented.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.seed import load_settings_v1
from jobhunter.db.transitions import transition
from jobhunter.llm import budget, bulk, spend_ledger
from jobhunter.llm.budget import BudgetParams
from jobhunter.settings.dryrun import bulk_budget
from jobhunter.settings.model import Settings
from jobhunter.worker.operations import cancel_operation, create_operation
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.m3 import (
    insert_attempt,
    insert_batch_item,
    insert_bulk_operation,
    insert_job,
    insert_submission,
)
from tests.support.reconcile import run_reconciler
from tests.support.scoring import activate_score_tuple, state_target

pytestmark = pytest.mark.integration

SETTINGS = Settings.model_validate(load_settings_v1())
P = BudgetParams.from_payload(load_settings_v1())
EARLIER = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
_BULK_ROW = sa.text(
    "SELECT kind, llm_tuple_id, state, budget_usd, spent_usd, followup_held_usd, finished_at "
    "FROM bulk_operations WHERE id = :b"
)


@pytest.fixture
def now() -> datetime:
    return utcnow()


@pytest.fixture
async def tuple_id(engine: AsyncEngine) -> int:
    async with engine.begin() as conn:
        return await activate_score_tuple(conn)


async def bulk_row(engine: AsyncEngine, bulk_operation_id: int) -> sa.Row[tuple[object, ...]]:
    async with engine.connect() as conn:
        return (await conn.execute(_BULK_ROW, {"b": bulk_operation_id})).one()


async def complete(engine: AsyncEngine, bulk_operation_id: int, now: datetime) -> bool:
    async with engine.begin() as conn:
        return await bulk.complete_if_done(conn, bulk_operation_id, now=now)


async def test_open_fixes_the_budget_at_the_bulk_share_of_the_scoring_headroom(
    engine: AsyncEngine, tuple_id: int, now: datetime
) -> None:
    month = budget.month_of(now)
    async with engine.begin() as conn:  # this month has $7.00 of scoring and $3.00 of non-scoring spend
        await spend_ledger.ensure_month(conn, month, P, now)
        await conn.execute(
            sa.text(
                "UPDATE llm_budget SET counted_usd = 10, scoring_counted_usd = 7, nonscoring_counted_usd = 3 "
                "WHERE month = :m"
            ),
            {"m": month},
        )
    async with engine.begin() as conn:
        opened = await bulk.open(conn, llm_tuple_id=tuple_id, settings=SETTINGS, now=now)
        row = await spend_ledger.read_month(conn, month, P, now=now)
    assert row is not None
    expected = bulk_budget(SETTINGS, budget.row_params(row, P), row)
    assert expected == Decimal("0.4") * (Decimal("98.00") * Decimal("0.95") - 7)  # $34.44 (§6.9.3)
    stored = await bulk_row(engine, opened)
    assert (stored.kind, stored.llm_tuple_id, stored.state, stored.finished_at) == (
        "tuple_rescore",
        tuple_id,
        "active",
        None,
    )
    assert stored.budget_usd == expected and (stored.spent_usd, stored.followup_held_usd) == (0, 0)


async def test_open_creates_the_row_of_a_fresh_month(
    engine: AsyncEngine, tuple_id: int, now: datetime
) -> None:
    # Codex r4 B8: `lock_month` raises on an absent row, so `open` ensures the month first
    async with engine.begin() as conn:
        await conn.execute(sa.text("DELETE FROM llm_budget"))
    async with engine.begin() as conn:
        opened = await bulk.open(conn, llm_tuple_id=tuple_id, settings=SETTINGS, now=now)
    assert (await bulk_row(engine, opened)).budget_usd == Decimal("37.240000")  # 0.4 x $93.10 (§21.4)
    async with engine.connect() as conn:
        months = (
            await conn.execute(
                sa.text("SELECT count(*) FROM llm_budget WHERE month = :m"), {"m": budget.month_of(now)}
            )
        ).scalar_one()
    assert months == 1


async def test_cancel_ends_an_active_operation_only(
    engine: AsyncEngine, tuple_id: int, now: datetime
) -> None:
    async with engine.begin() as conn:
        opened = await bulk.open(conn, llm_tuple_id=tuple_id, settings=SETTINGS, now=now)
    async with engine.begin() as conn:
        assert await bulk.cancel(conn, opened, now=now) is True
        assert await bulk.cancel(conn, opened, now=now) is False  # ended already
    row = await bulk_row(engine, opened)
    assert row.state == "cancelled" and row.finished_at is not None
    async with engine.begin() as conn:  # the singleton is free again
        assert await bulk.open(conn, llm_tuple_id=tuple_id, settings=SETTINGS, now=now) != opened


async def test_complete_if_done_waits_for_its_operation_and_for_its_last_in_batch_target(
    engine: AsyncEngine, tuple_id: int, now: datetime
) -> None:
    async with engine.begin() as conn:
        operation = await insert_bulk_operation(conn, tuple_id)
        await conn.execute(
            sa.text("UPDATE bulk_operations SET followup_held_usd = 0.3 WHERE id = :b"), {"b": operation}
        )
        creating, _ = await create_operation(
            conn,
            kind="create_targets_for_tuple",
            payload={"llm_tuple_id": tuple_id, "bulk_operation_id": operation},
            dedup_key=f"create_targets_for_tuple:{tuple_id}",
            defer=None,
        )
        pending = await state_target(
            conn, await insert_job(conn), tuple_id, "pending", bulk_operation_id=operation
        )
        # neither a budget-deferred nor a deferred duplicate holds the operation open (L1194, A1 issue 19)
        await state_target(
            conn, await insert_job(conn), tuple_id, "budget_deferred", bulk_operation_id=operation
        )
        submission = await insert_submission(
            conn,
            tuple_id,
            operation,
            state="submitted",
            submit_started_at=now,
            provider_batch_id="msgbatch_t14_complete",
            request_count=2,
        )
        batched = await state_target(
            conn,
            await insert_job(conn),
            tuple_id,
            "in_batch",
            batch_submission_id=submission,
            bulk_operation_id=operation,
            attempt_no=1,
            lane="batch",
        )
        await insert_attempt(conn, batched, mode="batch", lane="batch")
        held_item = await insert_batch_item(conn, submission, batched, hold_usd=Decimal("0.200000"))
        released_item = await insert_batch_item(
            conn, submission, pending, hold_usd=Decimal("0.100000"), hold_released_at=EARLIER
        )
    assert await complete(engine, operation, now) is False  # its create_targets_for_tuple is still pending
    async with engine.begin() as conn:
        assert await cancel_operation(conn, creating, reason="t14_test")
    assert await complete(engine, operation, now) is False  # a pending target
    async with engine.begin() as conn:
        await transition(conn, "score_targets", pending, ["pending"], "expired_unscored", reason="too_old")
    assert await complete(engine, operation, now) is False  # the in_batch target, in a submission in flight
    async with engine.begin() as conn:
        await transition(conn, "batch_submissions", submission, ["submitted"], "ended")
    assert await complete(engine, operation, now) is False  # the in_batch target still waits for its item
    async with engine.begin() as conn:
        await transition(
            conn, "score_targets", batched, ["in_batch"], "refused", set_={"refusal_category": "cyber"}
        )
    assert await complete(engine, operation, now) is False  # its submission is still in flight (`ended`)
    async with engine.begin() as conn:
        await transition(
            conn, "batch_submissions", submission, ["ended"], "reconciled", set_={"results_complete_at": now}
        )
    assert (await bulk_row(engine, operation)).state == "active"
    assert await complete(engine, operation, now) is True
    row = await bulk_row(engine, operation)
    assert (row.state, row.followup_held_usd) == ("completed", 0) and row.finished_at is not None
    async with engine.connect() as conn:
        stamps: dict[Any, Any] = dict(
            (
                await conn.execute(
                    sa.text("SELECT id, hold_released_at FROM batch_items WHERE submission_id = :s"),
                    {"s": submission},
                )
            ).all()
        )
    assert stamps == {held_item: now, released_item: EARLIER}  # a released item keeps its stamp
    assert await complete(engine, operation, now) is False  # completed already


async def test_an_operation_without_targets_completes_once_its_creation_ended(
    engine: AsyncEngine, tuple_id: int, now: datetime
) -> None:
    async with engine.begin() as conn:
        operation = await insert_bulk_operation(conn, tuple_id)
    assert await complete(engine, operation, now) is True  # no create_targets_for_tuple row: nothing waits


async def test_the_reconciler_completes_the_active_operation(
    engine: AsyncEngine, tuple_id: int, now: datetime, current_epoch: int, queue_app: object
) -> None:
    async with engine.begin() as conn:
        operation = await insert_bulk_operation(conn, tuple_id)
        await conn.execute(
            sa.text("UPDATE bulk_operations SET followup_held_usd = 0.3 WHERE id = :b"), {"b": operation}
        )
        target = await state_target(
            conn, await insert_job(conn), tuple_id, "pending", bulk_operation_id=operation
        )
    assert await run_reconciler("bulk_operations", engine, now=now, epoch=current_epoch) == 0
    async with engine.begin() as conn:
        await transition(conn, "score_targets", target, ["pending"], "expired_unscored", reason="too_old")
    assert await run_reconciler("bulk_operations", engine, now=now, epoch=current_epoch) == 1
    row = await bulk_row(engine, operation)
    assert (row.state, row.followup_held_usd) == ("completed", 0)
    assert await run_reconciler("bulk_operations", engine, now=now, epoch=current_epoch) == 0  # none active
