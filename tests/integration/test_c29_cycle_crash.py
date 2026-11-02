"""C29 in-process (spec §9.3 L3447, §18.2; plan Task 11, Rulings P34, Controller carry T9-b): a crash right
after a claim round committed, then a second epoch's takeover.

`raising_at("score.claim.after_claim")` stands for the worker killed between the claim's commit and the first
beat: the claims are `in_progress` under epoch 1 and the cycle `running`. The takeover of epoch 2 fails the
cycle `failed('orphaned')`, returns the targets to `pending` and ends their attempts `claim_lost`; the next
tick then requests a cycle and a new one starts, because the one-running index is free. The crashed cycle's
thread is never invoked again (the takeover writes; nothing resumes). C29 on the real worker is Task 28's.

Every datum is invented.
"""

import uuid

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.errors import NodeFailed
from jobhunter.graphs.score.nodes import claim as claim_node
from jobhunter.llm.gateway import LLMGateway
from jobhunter.worker.epoch import WorkerIdentity, claim_epoch_and_sweep
from jobhunter.worker.lock import LockSession
from jobhunter.worker.tasks import defer_in_tx
from jobhunter.worker.tick import tick_score
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.faults import raising_at
from tests.support.fixtures import count_rows
from tests.support.llm import FakeLLM
from tests.support.scoring import cycle_of, cycle_runner, insert_targets, target_of

pytestmark = pytest.mark.integration


def claimant(lock: LockSession, pid: int) -> WorkerIdentity:
    """A worker claiming the next epoch with the test's singleton lock."""
    assert lock.backend_pid is not None
    return WorkerIdentity(uuid.uuid4(), "h", pid, lock.backend_pid, "v")


@pytest.fixture
async def gateway(engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int) -> LLMGateway:
    return fake_llm.gateway(engine)


async def test_a_crash_after_the_claim_is_taken_over_and_the_next_tick_starts_a_new_cycle(
    engine: AsyncEngine,
    gateway: LLMGateway,
    queue_app: procrastinate.App,
    worker_lock: LockSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets = await insert_targets(engine, 3)
    runner = await cycle_runner(engine, gateway)
    crashed = await runner.start("periodic")
    monkeypatch.setattr(claim_node, "afault_point", raising_at(claim_node.AFTER_CLAIM))
    with pytest.raises(NodeFailed):  # the stand-in of the killed process
        await runner.run(crashed)
    for target in targets:
        assert (await target_of(engine, target.target_id)).state == "in_progress"
    assert (await cycle_of(engine, crashed)).status == "running"

    epoch, counts = await claim_epoch_and_sweep(engine, claimant(worker_lock, 2))
    assert epoch == 2 and (counts["score_targets"], counts["score_cycles"]) == (3, 1)
    row = await cycle_of(engine, crashed)
    assert (row.status, row.status_reason) == ("failed", "orphaned")
    for target in targets:
        moved = await target_of(engine, target.target_id)
        assert (moved.state, moved.claim_id, moved.attempt_no) == ("pending", None, 1)
    outcomes = sa.text("SELECT outcome FROM score_attempts ORDER BY id")
    async with engine.connect() as conn:
        assert list((await conn.execute(outcomes)).scalars()) == ["claim_lost"] * 3

    report = await tick_score(engine, now=utcnow(), epoch=2, defer=defer_in_tx, fake_llm=True)
    assert (report.requested, report.pause_reason) == (True, None)
    assert await count_rows(engine, "procrastinate_jobs", task_name="jh:score_cycle") == 1
    runner.epoch = 2
    fresh = await runner.start("periodic")  # the one-running index is free
    assert fresh != crashed and (await cycle_of(engine, fresh)).worker_epoch == 2
