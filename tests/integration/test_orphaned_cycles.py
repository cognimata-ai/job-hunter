"""Orphaned score cycles on PostgreSQL (spec §3.3.1 steps 3-4, §3.3.4, §7.6 L1718, §9.4.3, §17.4 L5502, §17.6
L5596; plan Task 11, Codex r4 B1, Ruling P52): the `orphaned_cycles` reconciler and the two takeover sweeps.

A running cycle whose heartbeat is 4 minutes old fails `orphaned` with its report closed, its claims are
reclaimed (`pending`, or `failed_attempts_exhausted` with the follow-up hold released when no charged attempt
is left) with their attempts `claim_lost`, its local execution is cancelled, and `score_cycle.orphaned`
(warn) is raised; the next completed cycle resolves it, and `score_cycle.failed` with it (Carry T25-b). A
fresh cycle is left alone. The takeover sweeps of an older epoch do the same for every claim and cycle of that
epoch. The reconciler follows the fence rule.

Every datum is invented.
"""

import asyncio
import uuid
from decimal import Decimal
from typing import Any

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.graphs.score.nodes import claim as claim_node
from jobhunter.graphs.score.nodes.claim import Claimed
from jobhunter.observability import metrics
from jobhunter.scoring import cycles
from jobhunter.scoring.admission import Picked
from jobhunter.worker.epoch import WorkerIdentity, claim_epoch_and_sweep
from jobhunter.worker.executions import EXECUTIONS
from jobhunter.worker.lock import LockSession
from jobhunter.worker.reconcilers import ReconcileContext
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.llm import FakeLLM, open_alerts
from tests.support.m3 import NOW, active_ids, insert_batch_item, insert_bulk_operation, insert_submission
from tests.support.reconcile import assert_fence_rule, run_reconciler
from tests.support.scoring import (
    PendingTarget,
    claimed_target,
    cycle_of,
    cycle_runner,
    insert_targets,
    score_llm_tuple,
    synthetic_c_draft,
    target_of,
)

pytestmark = pytest.mark.integration


def claimant(lock: LockSession, pid: int) -> WorkerIdentity:
    """A worker claiming the next epoch with the test's singleton lock."""
    assert lock.backend_pid is not None
    return WorkerIdentity(uuid.uuid4(), "h", pid, lock.backend_pid, "v")


ORPHANED = cycles.ORPHANED_ALERT


def reclaims() -> float:
    """`jobhunter_claim_reclaims_total` (Ruling T18-a: every reclaim counts, after its commit)."""
    return metrics.REGISTRY.get_sample_value("jobhunter_claim_reclaims_total") or 0.0


async def started(engine: AsyncEngine, *, epoch: int = 1) -> int:
    async with engine.begin() as conn:
        cycle_id = await cycles.start_cycle(
            conn, trigger="score_now", now=utcnow(), epoch=epoch, fake_llm=True
        )
    assert cycle_id is not None
    return cycle_id


async def claim(engine: AsyncEngine, cycle_id: int, targets: list[PendingTarget]) -> list[Claimed]:
    async with engine.begin() as conn:
        active = await active_ids(conn)
        return await claim_node.claim_round(
            conn,
            [Picked(t.target_id, lane="score_now") for t in targets],
            epoch=1,
            cycle_id=cycle_id,
            execution_config_id=active.execution_config_id,
            evaluation_policy_id=active.evaluation_policy_id,
        )


async def beat_aged(engine: AsyncEngine, cycle_id: int, minutes: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE score_cycles SET heartbeat_at = now() - make_interval(mins => :m) WHERE id = :c"),
            {"m": minutes, "c": cycle_id},
        )


async def attempts_of(engine: AsyncEngine, cycle_id: int) -> list[tuple[str, str | None]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            sa.text("SELECT outcome, outcome_detail FROM score_attempts WHERE cycle_id = :c ORDER BY id"),
            {"c": cycle_id},
        )
        return [(r.outcome, r.outcome_detail) for r in rows]


async def test_a_cycle_with_a_stale_heartbeat_fails_orphaned_and_its_claims_are_reclaimed(
    engine: AsyncEngine, current_epoch: int, queue_app: procrastinate.App
) -> None:
    targets = await insert_targets(engine, 2, lane="score_now")
    cycle_id = await started(engine)
    claimed = await claim(engine, cycle_id, targets)
    assert len(claimed) == 2
    await beat_aged(engine, cycle_id, 2)
    assert await run_reconciler("orphaned_cycles", engine, now=utcnow(), epoch=1) == 0  # fresh enough
    assert (await cycle_of(engine, cycle_id)).status == "running"
    local = EXECUTIONS.start(("cycle", cycle_id), 1, asyncio.sleep(3600))  # the hung cycle's execution
    await beat_aged(engine, cycle_id, 4)
    before = reclaims()
    assert await run_reconciler("orphaned_cycles", engine, now=utcnow(), epoch=1) == 1
    assert reclaims() == before + 2  # its two claims, counted once the pass committed
    row = await cycle_of(engine, cycle_id)
    assert (row.status, row.status_reason, row.report) == ("failed", "orphaned", {"reason": "orphaned"})
    assert row.finished_at is not None
    for target in targets:
        reclaimed = await target_of(engine, target.target_id)
        assert (reclaimed.state, reclaimed.claim_id, reclaimed.claim_epoch) == ("pending", None, None)
    assert await attempts_of(engine, cycle_id) == [("claim_lost", "cycle_orphaned")] * 2
    assert await open_alerts(engine, ORPHANED) == [(f"{ORPHANED}:cycle:{cycle_id}", "warn", 1)]
    assert local.cancelled() and EXECUTIONS.get(("cycle", cycle_id)) is None  # its execution was cancelled
    assert await run_reconciler("orphaned_cycles", engine, now=utcnow(), epoch=1) == 0  # once


async def test_the_next_completed_cycle_resolves_the_orphan_alert(
    engine: AsyncEngine, current_epoch: int, queue_app: procrastinate.App, fake_llm: FakeLLM
) -> None:
    first = await insert_targets(engine, 1, lane="score_now")
    orphan = await started(engine)
    await beat_aged(engine, orphan, 4)
    assert await run_reconciler("orphaned_cycles", engine, now=utcnow(), epoch=1) == 1
    assert len(await open_alerts(engine, ORPHANED)) == 1
    runner = await cycle_runner(engine, fake_llm.gateway(engine))
    for target in (*first, *await insert_targets(engine, 1)):
        fake_llm.answer("generate", target.job_revision_id, synthetic_c_draft())
    completed = await runner.start("manual")  # the one-running index is free again
    await runner.run(completed)
    assert (await cycle_of(engine, completed)).status == "completed"
    assert await open_alerts(engine, ORPHANED) == []


async def test_only_a_completed_cycle_resolves_the_failed_and_the_orphaned_alerts(
    engine: AsyncEngine, current_epoch: int, queue_app: procrastinate.App, fake_llm: FakeLLM
) -> None:
    """Carry T25-b (spec §17.6 L5617, L5596): `score_cycle.failed` resolves at the next completed cycle, in
    the same transaction as `score_cycle.orphaned`. A cycle that ends any other way resolves neither: an
    orphaned end leaves the failed alert open, a failed end the orphaned one."""
    first = await insert_targets(engine, 1, lane="score_now")
    failed = await started(engine)
    async with engine.begin() as conn:
        assert await cycles.fail_cycle(conn, failed, epoch=1, error_class="NodeFailed") == 0
    orphan = await started(engine)
    await beat_aged(engine, orphan, 4)
    assert await run_reconciler("orphaned_cycles", engine, now=utcnow(), epoch=1) == 1
    assert len(await open_alerts(engine, cycles.FAILED_ALERT)) == 1  # the orphaned end resolved neither
    again = await started(engine)
    async with engine.begin() as conn:
        assert await cycles.fail_cycle(conn, again, epoch=1, error_class="NodeFailed") == 0
    assert len(await open_alerts(engine, ORPHANED)) == 1  # nor did a failed end
    assert len(await open_alerts(engine, cycles.FAILED_ALERT)) == 2
    runner = await cycle_runner(engine, fake_llm.gateway(engine))
    for target in (*first, *await insert_targets(engine, 1)):
        fake_llm.answer("generate", target.job_revision_id, synthetic_c_draft())
    completed = await runner.start("manual")
    await runner.run(completed)
    assert (await cycle_of(engine, completed)).status == "completed"
    assert await open_alerts(engine, ORPHANED) == [] and await open_alerts(engine, cycles.FAILED_ALERT) == []


async def test_the_takeover_sweeps_fail_older_epoch_cycles_and_reclaim_their_claims(
    engine: AsyncEngine, current_epoch: int, queue_app: procrastinate.App, worker_lock: LockSession
) -> None:
    targets = await insert_targets(engine, 2, lane="score_now")
    cycle_id = await started(engine)
    claimed = await claim(engine, cycle_id, targets)
    # a claim of no cycle (another epoch-1 owner's), whose attempts are used up, holding a follow-up hold
    async with engine.begin() as conn:
        tuple_id = await score_llm_tuple(conn)
        operation = await insert_bulk_operation(conn, tuple_id)
        await conn.execute(
            sa.text("UPDATE bulk_operations SET followup_held_usd = 0.3 WHERE id = :b"), {"b": operation}
        )
        submission = await insert_submission(
            conn,
            tuple_id,
            operation,
            state="reconciled",
            submit_started_at=NOW,
            provider_batch_id=f"msgbatch_{uuid.uuid4().hex[:12]}",
            results_complete_at=NOW,
            reconciled_at=NOW,
        )
    spent = await claimed_target(engine, bulk_operation_id=operation)
    async with engine.begin() as conn:
        item = await insert_batch_item(
            conn,
            submission,
            spent.target_id,
            attempt_no=spent.attempt_no + 1,
            result_type="succeeded",
            processed_at=NOW,
            decision="followup",
            hold_usd=Decimal("0.100000"),
        )
        await conn.execute(
            sa.text("UPDATE score_targets SET charged_attempts = 4 WHERE id = :t"), {"t": spent.target_id}
        )
    before = reclaims()
    epoch, counts = await claim_epoch_and_sweep(engine, claimant(worker_lock, 2))
    assert epoch == 2 and (counts["score_targets"], counts["score_cycles"]) == (3, 1)
    assert reclaims() == before + 3  # the takeover's reclaims, counted after its commit
    row = await cycle_of(engine, cycle_id)
    assert (row.status, row.status_reason, row.report) == ("failed", "orphaned", {"reason": "orphaned"})
    assert [(await target_of(engine, c.id)).state for c in claimed] == ["pending", "pending"]
    assert await attempts_of(engine, cycle_id) == [("claim_lost", "worker_takeover")] * 2
    exhausted = await target_of(engine, spent.target_id)
    assert (exhausted.state, exhausted.state_reason, exhausted.claim_id) == (
        "failed_attempts_exhausted",
        "attempts_exhausted",
        None,
    )
    async with engine.connect() as conn:
        hold = (
            await conn.execute(
                sa.text(
                    "SELECT i.hold_released_at IS NOT NULL, b.followup_held_usd FROM batch_items i, "
                    "bulk_operations b WHERE i.id = :i AND b.id = :b"
                ),
                {"i": item, "b": operation},
            )
        ).one()
        lost = (
            await conn.execute(
                sa.text("SELECT outcome, outcome_detail FROM score_attempts WHERE id = :a"),
                {"a": spent.attempt_id},
            )
        ).one()
    assert tuple(hold) == (True, Decimal("0.200000"))  # the terminal move releases the hold once
    assert tuple(lost) == ("claim_lost", "worker_takeover")
    assert await open_alerts(engine, ORPHANED) == [(f"{ORPHANED}:cycle:{cycle_id}", "warn", 1)]
    _, again = await claim_epoch_and_sweep(engine, claimant(worker_lock, 3))
    assert (again["score_targets"], again["score_cycles"]) == (0, 0)  # idempotent over `< :epoch`
    assert reclaims() == before + 3


async def test_the_orphaned_cycles_reconciler_follows_the_fence_rule(
    engine: AsyncEngine, current_epoch: int, queue_app: procrastinate.App
) -> None:
    await insert_targets(engine, 1, lane="score_now")
    orphan = await started(engine)
    await beat_aged(engine, orphan, 4)

    def ctx(epoch: int) -> ReconcileContext:
        return ReconcileContext(engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow(), epoch=epoch)

    await assert_fence_rule("orphaned_cycles", "tick", ctx(current_epoch + 1), ctx(current_epoch), cycles)
    row: Any = await cycle_of(engine, orphan)
    assert row.status == "running"  # neither check wrote anything
