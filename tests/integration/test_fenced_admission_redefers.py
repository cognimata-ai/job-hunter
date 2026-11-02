"""Task 24, M0 carry-forward 6 (plan Deviation 18, ruling (d)): a fenced admission or claim re-defers its job.

A delivery that a superseded worker epoch takes (`foreign_epoch`, or a claim that `fence_epoch` refuses) would
otherwise consume its procrastinate job, and the work would wait for a reconciler (2 to 3 min). Instead it
re-defers the same attempt or operation under its queueing lock, in a transaction of its own; a second
fenced delivery finds that job waiting and defers nothing more. An ordinary unclaimable operation is still
`None`, never `FencedOut`, and re-defers nothing.

Fix round 1, ruling T24-a: a refused epoch proves the process superseded (§3.3.1 item 5), so the delivery also
asks its own worker for the graceful stop (`on_superseded`: the task bodies request the runtime's shutdown,
logged once as `shutdown_requested` with the reason `epoch_superseded`). Its loops then stop fetching, and the
re-deferred job stays `todo` for the current worker: a zombie never re-takes its own `run_collect` under the
global `collect` lock.
"""

import asyncio
import uuid

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.errors import FencedOut
from jobhunter.graphs.checkpointer import open_saver
from jobhunter.graphs.collect.graph import build_noop_collect_graph
from jobhunter.graphs.score.deps import ScoreDeps
from jobhunter.graphs.score.graph import build_score_graphs
from jobhunter.worker import operations as ops
from jobhunter.worker import runs
from jobhunter.worker.epoch import WorkerIdentity, claim_epoch_and_sweep
from jobhunter.worker.runtime import EPOCH_SUPERSEDED, ShutdownState, WorkerRuntime, request_shutdown
from jobhunter.worker.shutdown import coordinate
from jobhunter.worker.tasks import defer_in_tx, defer_run_attempt, job_task_name, live_job_exists

from tests.support.collect import manual_run_id
from tests.support.llm import FAKE_LLM_DIR
from tests.support.profile import operation_deps

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


def stop_requests(log_events) -> list[tuple[str, str]]:
    """`(level, reason)` of every `shutdown_requested` event logged so far."""
    return [(e["level"], e["reason"]) for e in log_events() if e["event"] == "shutdown_requested"]


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
    zombie = ShutdownState()  # the shutdown of the superseded worker (epoch 1)

    def superseded() -> None:
        request_shutdown(zombie, reason=EPOCH_SUPERSEDED)

    async with open_saver(migrated.dsn("app")) as saver:
        graph = build_noop_collect_graph(saver)
        assert await runs.execute_run(engine, graph, run_id, 1, epoch=1, on_superseded=superseded) == (
            "foreign_epoch"
        )
        async with engine.connect() as c:
            assert await live_job_exists(c, "run_collect", args_match={"run_id": run_id, "attempt_no": 1})
        # Ruling T24-a: the superseded worker asks for its own graceful stop, logged once.
        assert zombie.requested.is_set() and stop_requests(log_events) == [("warning", "epoch_superseded")]
        # A second fenced delivery of the same attempt finds that job waiting (queueing lock run:<id>:1).
        assert await runs.execute_run(engine, graph, run_id, 1, epoch=1, on_superseded=superseded) == (
            "foreign_epoch"
        )
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
    # ... and `execute_operation` re-defers `run_operation(op)` under `op:<id>`, once, and asks for the stop.
    operations_zombie = ShutdownState()

    def operation_superseded() -> None:
        request_shutdown(operations_zombie, reason=EPOCH_SUPERSEDED)

    await ops.execute_operation(engine, op, epoch=1, on_superseded=operation_superseded)
    async with engine.connect() as c:
        assert await live_job_exists(c, "run_operation", args_match={"operation_id": op})
    assert operations_zombie.requested.is_set()
    await ops.execute_operation(engine, op, epoch=1, on_superseded=operation_superseded)
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
    current = ShutdownState()

    def current_superseded() -> None:
        request_shutdown(current, reason=EPOCH_SUPERSEDED)

    await ops.execute_operation(engine, op, epoch=e2, on_superseded=current_superseded)  # not claimable
    assert await live_jobs(engine, "run_operation") == [(f"op:{op}", {"operation_id": op})]  # nothing more
    assert not current.requested.is_set()  # an unclaimable operation proves nothing about the epoch
    # Each zombie's stop was requested and logged once, however often it was fenced.
    assert stop_requests(log_events) == [("warning", "epoch_superseded")] * 2


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
    shutdown = ShutdownState()
    async with open_saver(migrated.dsn("app")) as saver:
        graph = build_noop_collect_graph(saver)
        assert (
            await runs.execute_run(
                engine, graph, run_id, 1, epoch=current_epoch, on_superseded=shutdown.requested.set
            )
            == "foreign_epoch"
        )
    assert await live_jobs(engine, "run_collect") == []
    assert [e for e in log_events() if e["event"] == "run_redeferred"] == []
    assert not shutdown.requested.is_set()  # the current worker is not superseded: it keeps running


JOBS = sa.text("SELECT status, args FROM procrastinate_jobs WHERE task_name = :t ORDER BY id")


async def test_a_superseded_workers_collect_loop_stops_and_leaves_the_redeferred_job_to_the_current_worker(
    engine, queue_app, current_epoch, migrated, worker_lock, make_config, log_events, tmp_path
) -> None:
    """Ruling T24-a through the real procrastinate loop of the `collect` queue and the real coordinator: the
    zombie (epoch 1) takes the delivered `run_collect`, its admission is refused, the job is re-deferred and
    the zombie asks for its own stop; its loop stops fetching, so it never re-takes its own job under the
    `collect` lock, and the re-deferred job stays `todo` for the current worker (epoch 2)."""
    run_id = await manual_run_id(engine)
    async with engine.begin() as c:
        await defer_run_attempt(c, defer_in_tx, run_id, 1)  # the delivery the zombie will take
    await claim_epoch_and_sweep(engine, claimant(worker_lock, 2))
    assert job_task_name("run_collect") in queue_app.tasks  # the process's task app, with the blueprint
    cfg = make_config(
        "worker",
        JOBHUNTER_FAKE_LLM="1",
        JOBHUNTER_FAKE_LLM_DIR=str(FAKE_LLM_DIR),
        JOBHUNTER_PARSE_JOBS_DIR=str(tmp_path / "parse"),
        JOBHUNTER_CV_STORE_DIR=str(tmp_path / "cv"),
    )
    async with open_saver(migrated.dsn("app")) as saver, operation_deps(engine, cfg) as deps:
        score_deps = ScoreDeps(engine, deps.gateway, utcnow, deps.gateway.monotonic, saver)
        runtime = WorkerRuntime(
            cfg=cfg,
            engine=engine,
            queue=queue_app,
            saver=saver,
            collect_graph=build_noop_collect_graph(saver),
            epoch=1,
            shutdown=ShutdownState(),
            embedder=None,
            gateway=deps.gateway,
            parse_client=deps.parse_client,
            cv_store=deps.cv_store,
            profile_graphs=deps.profile_graphs,
            operation_deps=deps,
            score_graphs=build_score_graphs(score_deps),
            score_deps=score_deps,
            batches_client=deps.batches_client,
        )
        loop = asyncio.create_task(
            queue_app.run_worker_async(
                queues=["collect"],
                concurrency=1,
                install_signal_handlers=False,
                shutdown_graceful_timeout=10,
                additional_context={"runtime": runtime},
                name="zombie-collect",
            )
        )
        # The zombie's own stop ends the loop: `coordinate` returns 0 once the stop was requested.
        assert await asyncio.wait_for(coordinate([loop], runtime.shutdown), 30) == 0
    async with engine.connect() as c:
        jobs = [tuple(row) for row in await c.execute(JOBS, {"t": job_task_name("run_collect")})]
    args = {"run_id": run_id, "attempt_no": 1}
    assert jobs == [
        ("succeeded", args),
        ("todo", args),
    ]  # the delivery, and the one left to the current worker
    admissions = [e["admission"] for e in log_events() if e["event"] == "run_admission"]
    assert admissions == ["foreign_epoch"]  # the zombie took one job: never its re-deferred one
    assert stop_requests(log_events) == [("warning", "epoch_superseded")]
    async with engine.connect() as c:
        row = (
            await c.execute(sa.text("SELECT status, attempt_no FROM runs WHERE id = :i"), {"i": run_id})
        ).one()
    assert tuple(row) == ("queued", 0)


async def test_a_fence_of_another_owner_at_the_admission_or_the_claim_is_a_failure_never_a_supersession(
    engine, queue_app, current_epoch, migrated, monkeypatch, log_events
) -> None:
    """Fix wave F6 (F5 re-review, out-of-scope observation): only the worker's own fence
    (`FencedOut.worker_superseded`) proves a takeover. Should the admission's or the claim's fenced call ever
    raise another owner's fence, the delivery fails with it: nothing is re-deferred and the worker is never
    asked to stop. (Today both calls can only raise the epoch fence, which the test above covers.)"""
    run_id = await manual_run_id(engine)
    async with engine.begin() as c:
        op, _ = await ops.create_operation(
            c, kind="export", payload={"export_id": 1}, dedup_key="export:other-owner", defer=None
        )
    stops: list[str] = []

    def fenced_by(owner: str):
        async def fence(conn, epoch: int) -> None:
            raise FencedOut(owner=owner)

        return fence

    monkeypatch.setattr(runs, "fence_epoch", fenced_by("run"))
    async with open_saver(migrated.dsn("app")) as saver:
        graph = build_noop_collect_graph(saver)
        with pytest.raises(FencedOut) as admission:
            await runs.execute_run(
                engine, graph, run_id, 1, epoch=current_epoch, on_superseded=lambda: stops.append("run")
            )
    assert admission.value.owner == "run"
    monkeypatch.setattr(ops, "fence_epoch", fenced_by("operation"))
    with pytest.raises(FencedOut) as claim:
        await ops.execute_operation(
            engine, op, epoch=current_epoch, on_superseded=lambda: stops.append("operation")
        )
    assert claim.value.owner == "operation"
    assert stops == [] and stop_requests(log_events) == []
    assert await live_jobs(engine, "run_collect") == [] and await live_jobs(engine, "run_operation") == []
    events = {e["event"] for e in log_events()}
    assert not events & {"run_redeferred", "operation_redeferred", "run_admission"}
    async with engine.connect() as c:  # neither was admitted nor claimed
        run = (await c.execute(sa.text("SELECT status FROM runs WHERE id = :i"), {"i": run_id})).scalar_one()
        status = (
            await c.execute(sa.text("SELECT status FROM operations WHERE id = :i"), {"i": op})
        ).scalar_one()
    assert (run, status) == ("queued", "pending")
