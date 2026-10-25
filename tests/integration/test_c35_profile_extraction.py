"""Crash case C35 in full (spec §18.3; plan Task 13): a profile extraction stalls, the reconciler re-queues
it, a second execution runs, then the first resumes. Through the real `extract_profile` handler and its graph:
exactly one extraction is billed, the draft becomes `ready` once, and the first execution's late write updates
no draft row (its `FencedOut` ends it as a lost execution, with no owner write).

As in Task 5's pin (`test_c35_operation_stall.py`) it runs in one process: a paused worker still holds the
singleton lock, so a second worker process could never start. A real zombie is another process with its own
execution registry, so the paused execution gets a fresh `ExecutionRegistry` (`op_runner.run_one(…,
pause=…)`). Every datum is invented; the LLM is the fake transport on the synthetic stream.
"""

import asyncio
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.llm import fake
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.faults import PausePoint
from tests.support.llm import ledger_states, reclaim_stale_now, tries
from tests.support.profile import SYNTHETIC_CV, OpRunner, Upload, approve_text, draft_row

pytestmark = pytest.mark.integration


async def approved_draft(engine: AsyncEngine, op_runner: OpRunner, upload: Upload) -> tuple[int, int]:
    """A pasted `SYNTHETIC_CV` through the text phase, approved as stripped: the draft and its operation."""
    draft_id = await upload(SYNTHETIC_CV)
    await op_runner.run_pending()
    return draft_id, await approve_text(engine, draft_id)


async def row_version(engine: AsyncEngine, table: str, row_id: int) -> tuple[Any, ...]:
    """What a write would change: the row's status and the transaction that last wrote it."""
    async with engine.connect() as conn:
        statement = sa.text(
            f"SELECT status, xmin::text FROM {table} WHERE id = :i"  # noqa: S608 - constant table names
        )
        return tuple((await conn.execute(statement, {"i": row_id})).one())


async def stall_reclaim_and_resume(
    engine: AsyncEngine, op_runner: OpRunner, op_id: int, draft_id: int, point: str
) -> tuple[Any, ...]:
    """Execution 1 held at `point`; the reconciler's reclaim; execution 2 to its end; then execution 1
    resumed. Returns the draft's row version right after execution 2, which execution 1 must leave as it
    is."""
    pause = PausePoint(point)
    first = asyncio.create_task(op_runner.run_one(op_id, pause=pause))
    await asyncio.wait_for(pause.reached.wait(), 60)
    await reclaim_stale_now(engine, op_id)  # claim_id := NULL, attempts + 1
    await op_runner.run_one(op_id)
    after_second = await row_version(engine, "profile_drafts", draft_id)
    assert after_second[0] == "ready"
    pause.resume()
    await asyncio.wait_for(first, 30)
    return after_second


async def test_a_reclaimed_extraction_never_sends(engine, op_runner, upload) -> None:
    """The dispatch-marker part, through the handler: execution 1 stalls after its reservation, and its marker
    updates nothing once execution 2 has the claim."""
    draft_id, op_id = await approved_draft(engine, op_runner, upload)
    after_second = await stall_reclaim_and_resume(engine, op_runner, op_id, draft_id, "gateway.after_reserve")
    assert fake.state().served == 1
    assert await ledger_states(engine, op_id) == ["not_billed:never_dispatched", "billed"]
    assert await row_version(engine, "profile_drafts", draft_id) == after_second  # ready once
    draft = await draft_row(engine, draft_id)
    billed = [t for t in await tries(engine) if t.state == "billed"]
    assert [t.call_id for t in billed] == [draft.llm_call_id]
    async with engine.connect() as conn:
        status, attempts = (
            await conn.execute(sa.text("SELECT status, attempts FROM operations WHERE id = :i"), {"i": op_id})
        ).one()
    assert (status, attempts) == ("succeeded", 1)


async def test_a_reclaimed_extractions_late_draft_write_updates_no_row(engine, op_runner, upload) -> None:
    """The publish part: execution 1 paid for the extraction and stalls before its `draft` node's
    transaction; execution 2 reuses that paid output (the receipt) and publishes; execution 1's late write is
    fenced by the owner predicate and moves nothing."""
    draft_id, op_id = await approved_draft(engine, op_runner, upload)
    after_second = await stall_reclaim_and_resume(
        engine, op_runner, op_id, draft_id, "profile.draft.before_tx"
    )
    assert fake.state().served == 1
    assert await ledger_states(engine, op_id) == ["billed"]  # exactly one extraction billed
    assert await row_version(engine, "profile_drafts", draft_id) == after_second
