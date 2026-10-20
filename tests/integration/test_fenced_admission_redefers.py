"""Task 24, M0 carry-forward 6 (plan Deviation 18, ruling (d)): a fenced admission or claim re-defers its job.

A delivery that a superseded worker epoch takes (`foreign_epoch`, or a claim that `fence_epoch` refuses) would
otherwise consume its procrastinate job, and the work would wait for a reconciler (2 to 3 min). Instead it
re-defers the same attempt or operation under its queueing lock, in a transaction of its own; a second
fenced delivery finds that job waiting and defers nothing more. An ordinary unclaimable operation is still
`None`, never `FencedOut`, and re-defers nothing.
"""

import uuid

import pytest
import sqlalchemy as sa
from jobhunter.errors import FencedOut
from jobhunter.graphs.checkpointer import open_saver
from jobhunter.graphs.collect.graph import build_noop_collect_graph
from jobhunter.worker import operations as ops
from jobhunter.worker import runs
from jobhunter.worker.epoch import WorkerIdentity, claim_epoch_and_sweep
from jobhunter.worker.tasks import job_task_name, live_job_exists

from tests.support.collect import manual_run_id

pytestmark = pytest.mark.integration

LIVE_JOBS = sa.text(
    "SELECT queueing_lock, args FROM procrastinate_jobs WHERE task_name = :t AND status IN ('todo', 'doing') "
    "ORDER BY id"
)


def claimant(worker_lock, n: int) -> WorkerIdentity:
    return WorkerIdentity(uuid.uuid4(), "h", n, worker_lock.backend_pid, "v")


async def live_jobs(engine, task: str) -> list[tuple[object, ...]]:
    async with engine.connect() as c:
        return [tuple(row) for row in await c.execute(LIVE_JOBS, {"t": job_task_name(task)})]  # type: ignore[arg-type]


async def test_foreign_epoch_run_and_fenced_claim_redefer_their_job(
    engine, queue_app, current_epoch, migrated, worker_lock, log_events
) -> None:
    run_id = await manual_run_id(engine)  # queued, and its job is gone (inserted without one)
    async with engine.begin() as c:
        op, created = await ops.create_operation(
            c, kind="export", payload={"export_id": 1}, dedup_key="export:fenced", defer=None
        )
    assert (
        created
        and await live_jobs(engine, "run_collect") == []
        and await live_jobs(engine, "run_operation") == []
    )
    e2, _ = await claim_epoch_and_sweep(engine, claimant(worker_lock, 2))  # epoch 1 is superseded
    assert e2 == current_epoch + 1
    async with open_saver(migrated.dsn("app")) as saver:
        graph = build_noop_collect_graph(saver)
        assert await runs.execute_run(engine, graph, run_id, 1, epoch=1) == "foreign_epoch"
        async with engine.connect() as c:
            assert await live_job_exists(c, "run_collect", args_match={"run_id": run_id, "attempt_no": 1})
        # A second fenced delivery of the same attempt finds that job waiting (queueing lock run:<id>:1).
        assert await runs.execute_run(engine, graph, run_id, 1, epoch=1) == "foreign_epoch"
    assert await live_jobs(engine, "run_collect") == [
        (f"run:{run_id}:1", {"run_id": run_id, "attempt_no": 1})
    ]
    redeferred = [e for e in log_events() if e["event"] == "run_redeferred"]
    assert [(e["run_id"], e["attempt_no"], e["deduplicated"]) for e in redeferred] == [
        (run_id, 1, False),
        (run_id, 1, True),
    ]
    # The run itself is untouched: still queued at attempt 0, and no attempt of epoch 1.
    async with engine.connect() as c:
        row = (
            await c.execute(sa.text("SELECT status, attempt_no FROM runs WHERE id = :i"), {"i": run_id})
        ).one()
        attempts = (await c.execute(sa.text("SELECT count(*) FROM run_attempts"))).scalar_one()
    assert (tuple(row), attempts) == (("queued", 0), 0)

    # The operation: a claim with the superseded epoch raises `FencedOut` (Deviation 18) ...
    async with engine.begin() as c:
        with pytest.raises(FencedOut):
            await ops.claim_operation(c, op, epoch=1)
    # ... and `execute_operation` re-defers `run_operation(op)` under `op:<id>`, once.
    await ops.execute_operation(engine, op, epoch=1)
    async with engine.connect() as c:
        assert await live_job_exists(c, "run_operation", args_match={"operation_id": op})
    await ops.execute_operation(engine, op, epoch=1)
    assert await live_jobs(engine, "run_operation") == [(f"op:{op}", {"operation_id": op})]
    redeferred = [e for e in log_events() if e["event"] == "operation_redeferred"]
    assert [(e["operation_id"], e["deduplicated"]) for e in redeferred] == [
        (op, False),
        (op, True),
    ]
    async with engine.connect() as c:
        status = (
            await c.execute(sa.text("SELECT status FROM operations WHERE id = :i"), {"i": op})
        ).scalar_one()
    assert status == "pending"

    # An ordinary unclaimable operation (already `running`) is `None` with the current epoch, not `FencedOut`.
    async with engine.begin() as c:
        claim = await ops.claim_operation(c, op, epoch=e2)
    assert claim is not None
    async with engine.begin() as c:
        assert await ops.claim_operation(c, op, epoch=e2) is None
    await ops.execute_operation(engine, op, epoch=e2)  # not claimable: nothing more is deferred
    assert await live_jobs(engine, "run_operation") == [(f"op:{op}", {"operation_id": op})]


async def test_a_foreign_attempt_seen_by_the_current_epoch_is_not_redeferred(
    engine, queue_app, current_epoch, migrated, log_events
) -> None:
    """Only a refused epoch re-defers. The other `foreign_epoch` (the run's attempt belongs to another epoch
    while this one is current; the takeover sweep leaves no such run) is not re-deferred: the job would be
    taken by this same worker again at once."""
    run_id = await manual_run_id(engine)
    async with engine.begin() as c:
        admission, _ = await runs.start_attempt(c, run_id, 1, epoch=current_epoch)
        assert admission == "started"
        # as if an older epoch's attempt had been left unswept
        await c.execute(sa.text("UPDATE run_attempts SET worker_epoch = 0 WHERE run_id = :i"), {"i": run_id})
    async with open_saver(migrated.dsn("app")) as saver:
        graph = build_noop_collect_graph(saver)
        assert await runs.execute_run(engine, graph, run_id, 1, epoch=current_epoch) == "foreign_epoch"
    assert await live_jobs(engine, "run_collect") == []
    assert [e for e in log_events() if e["event"] == "run_redeferred"] == []
