"""Crash case C35, the dispatch-marker part (spec §18.3; plan Task 5): an operation stalls after its
reservation, the reconciler re-queues it and a second execution runs, then the first resumes. The first
execution's dispatch marker updates nothing and it never sends; exactly one call is billed.

It runs in one process, because a paused worker still holds the singleton lock, so a second worker process
could never start. A real zombie is another process with its own execution registry, so each execution here
gets an `ExecutionRegistry` of its own (`execute_operation(..., registry=…)`). Task 13 adds the full
extraction flow.
"""

import asyncio
from datetime import timedelta

import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.llm import gateway as gateway_module
from jobhunter.llm import spend_ledger
from jobhunter.worker import operations as ops
from jobhunter.worker.executions import EXECUTIONS, ExecutionRegistry
from jobhunter.worker.operations import execute_operation

from tests.support.collect import ctx
from tests.support.faults import PausePoint
from tests.support.llm import create_llm_call_operation, ledger_states, reclaim_stale_now, tries


async def test_reclaimed_operation_never_sends(engine, current_epoch, fake_llm, monkeypatch) -> None:
    # tests/support/llm.py: registers a gateway-calling handler for kind 'export' (M3 Task 19: 'eval_run' is
    # the eval runner's) for this test only; it closes over its gateway and runs with deps=None
    op_id = await create_llm_call_operation(engine)
    pause = PausePoint("gateway.after_reserve")
    monkeypatch.setattr(gateway_module, "afault_point", pause)
    first = asyncio.create_task(
        execute_operation(engine, op_id, epoch=current_epoch, registry=ExecutionRegistry())
    )
    await asyncio.wait_for(pause.reached.wait(), 10)
    # the reconciler's reclaim statement only: claim_id := NULL, attempts + 1
    await reclaim_stale_now(engine, op_id)
    await execute_operation(engine, op_id, epoch=current_epoch, registry=ExecutionRegistry())
    pause.resume()
    await first
    assert fake_llm.served == 1
    assert await ledger_states(engine, op_id) == ["not_billed:never_dispatched", "billed"]


async def test_reconciler_cancels_a_local_stalled_execution(
    engine, queue_app, current_epoch, fake_llm, monkeypatch
) -> None:
    op_id = await create_llm_call_operation(engine)
    pause = PausePoint("gateway.after_reserve")
    monkeypatch.setattr(gateway_module, "afault_point", pause)
    first = asyncio.create_task(execute_operation(engine, op_id, epoch=current_epoch))  # on EXECUTIONS
    await asyncio.wait_for(pause.reached.wait(), 10)
    assert EXECUTIONS.get(("operation", op_id)) is not None
    (reserved,) = await tries(engine)

    # the real `operations` reconciler, its heartbeat stale: reclaim, then cancel and await the local task
    later = utcnow() + ops.OPERATION_STALE + timedelta(seconds=1)
    assert await ops.reconcile_operations(ctx(engine, queue_app, now=later, epoch=current_epoch)) == 1
    await asyncio.wait_for(first, 10)  # the execution was lost: it returns without an owner write
    assert EXECUTIONS.get(("operation", op_id)) is None
    assert fake_llm.served == 0  # nothing was sent
    assert await ledger_states(engine, op_id) == ["reserved"]  # the cancelled call left its reservation

    # `llm_ledger_unsettled` releases it once 15 minutes old, because its recorded claim no longer holds
    created = await _created_at(engine, reserved.ledger_id)
    early = ctx(engine, queue_app, now=created + timedelta(minutes=14), epoch=current_epoch)
    assert await spend_ledger.reconcile_llm_ledger_unsettled(early) == 0
    late = ctx(engine, queue_app, now=created + timedelta(minutes=15, seconds=1), epoch=current_epoch)
    assert await spend_ledger.reconcile_llm_ledger_unsettled(late) == 1
    assert await ledger_states(engine, op_id) == ["not_billed:never_dispatched"]


async def _created_at(engine, ledger_id: int):
    async with engine.connect() as conn:
        result = await conn.execute(
            sa.text("SELECT created_at FROM llm_spend_ledger WHERE id = :i"), {"i": ledger_id}
        )
        return result.scalar_one()
