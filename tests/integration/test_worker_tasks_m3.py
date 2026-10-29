"""The M3 tasks in the worker (spec §3.3.2, §3.3.5, §9.3, §10.10, §11.13 L4632; plan Task 27, Rulings P33,
T24-a, Codex r1 #17, r2 B25): the periodic `batch_poll`, `batch_reconcile_ambiguous` and `canary_daily`, each
deferred by procrastinate's periodic deferrer at its cron's latest instant and run by a real procrastinate
worker of its queue on the fake; each starting with the worker's epoch fence and doing nothing after the
admission stop; `score_cycle` under the `score` lock, so a second cycle's job waits for the first; and a lone
stalled `score_cycle` job failed, never retried, its cycle failed `orphaned`, and no later delivery invoking
the old cycle's thread again.

The worker loops run on a runtime of real dependencies (`runtime`): the operation runner's (the committed
recordings, a fake Batches API shared through the test's fake state) and score graphs on the test's fake
LLM, compiled on a real checkpointer. Every datum is invented.
"""

import asyncio
import dataclasses
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Final, cast

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.graphs.checkpointer import open_saver
from jobhunter.graphs.score import graph as graph_mod
from jobhunter.graphs.score.graph import build_score_graphs
from jobhunter.graphs.score.nodes import claim as claim_node
from jobhunter.llm import fake
from jobhunter.llm.fake import BatchScript
from jobhunter.llm.prompts.render import prompt_schema_v2
from jobhunter.scoring import batch_adoption, batch_results, cycles, eval_calibration, golden
from jobhunter.worker import tasks
from jobhunter.worker.runtime import ShutdownState, WorkerRuntime
from jobhunter.worker.tasks import defer_in_tx, job_task_name, release_stalled_jobs
from procrastinate.job_context import JobContext
from procrastinate.periodic import PeriodicDeferrer
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.batches import (
    PUBLISHED_BODY,
    bulk_with_targets,
    submission_rows,
    submit_batch,
)
from tests.support.eval import insert_cases
from tests.support.faults import PausePoint
from tests.support.llm import FakeLLM
from tests.support.logs import logged
from tests.support.pg import PgServer
from tests.support.procs import await_until
from tests.support.profile import OpRunner
from tests.support.reconcile import run_reconciler
from tests.support.scoring import (
    PendingTarget,
    activate_score_tuple,
    cycle_of,
    insert_targets,
    score_deps,
    synthetic_c_draft,
    target_of,
)

pytestmark = pytest.mark.integration

WAIT_S: Final = 60.0
BATCH: Final = 200  # `scoring.batch_min_targets`, the smallest batch the settings allow
SONNET: Final = "claude-sonnet-5-5"
# Each periodic M3 task: its cron, its queue and its queueing lock (spec §3.3.2; Tasks 17 and 20).
PERIODIC: Final = {
    "batch_poll": ("*/5 * * * *", "aux", "batch_poll"),
    "batch_reconcile_ambiguous": ("*/15 * * * *", "aux", "batch_reconcile_ambiguous"),
    "canary_daily": ("41 6 * * *", "tick", "canary_daily"),
}
_JOBS: Final = sa.text(
    "SELECT id, queue_name, lock, queueing_lock, args, status FROM procrastinate_jobs WHERE task_name = :t "
    "ORDER BY id"
)
_EVENTS: Final = sa.text("SELECT type, at FROM procrastinate_events WHERE job_id = :j ORDER BY id")
_THREAD_CHECKPOINTS: Final = sa.text("SELECT count(*) FROM checkpoints WHERE thread_id = :t")
_CANARY_RUNS: Final = sa.text("SELECT id, kind, status FROM eval_runs WHERE kind = 'canary' ORDER BY id")


@pytest.fixture
async def runtime(
    engine: AsyncEngine,
    migrated: PgServer,
    queue_app: procrastinate.App,
    op_runner: OpRunner,
    fake_llm: FakeLLM,
    current_epoch: int,
) -> AsyncIterator[WorkerRuntime]:
    """The worker's runtime of epoch 1 on real dependencies (module docstring); no collection graph."""
    deps = op_runner.deps
    async with open_saver(migrated.dsn("app")) as saver:
        score = dataclasses.replace(await score_deps(engine, fake_llm.gateway(engine)), saver=saver)
        yield WorkerRuntime(
            cfg=deps.cfg,
            engine=engine,
            queue=queue_app,
            saver=saver,
            collect_graph=cast(Any, None),
            epoch=current_epoch,
            shutdown=ShutdownState(),
            embedder=None,
            gateway=deps.gateway,
            parse_client=deps.parse_client,
            cv_store=deps.cv_store,
            profile_graphs=deps.profile_graphs,
            operation_deps=deps,
            score_graphs=build_score_graphs(score),
            score_deps=score,
            batches_client=deps.batches_client,
        )


async def defer_by_cron(app: procrastinate.App, name: str) -> int:
    """`name`'s job deferred as procrastinate's periodic deferrer defers it, for its cron's latest instant (at
    most two days back, so the daily cron has one); that instant, the job's `timestamp`."""
    deferrer = PeriodicDeferrer(app.periodic_registry, max_delay=2 * 86_400)
    due = [
        (periodic, at)
        for periodic, at in deferrer.get_previous_tasks(at=time.time())
        if periodic.task.name == job_task_name(cast(Any, name))
    ]
    assert len(due) == 1, due
    await deferrer.defer_jobs(due)
    return due[0][1]


async def jobs_of(engine: AsyncEngine, name: str) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(_JOBS, {"t": job_task_name(cast(Any, name))})).all())


async def run_queue(
    app: procrastinate.App, runtime: WorkerRuntime, queue: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A procrastinate worker of `queue` on `runtime`, as `worker_main` runs one, until no job is left
    (`wait=False`). Its own periodic deferrer finds no task: the test deferred the job itself."""
    with monkeypatch.context() as patched:
        patched.setattr(app.periodic_registry, "periodic_tasks", {})
        await asyncio.wait_for(
            app.run_worker_async(
                queues=[queue],
                concurrency=1,
                wait=False,
                listen_notify=False,
                install_signal_handlers=False,
                additional_context={"runtime": runtime},
                name=f"test-{queue}",
            ),
            WAIT_S,
        )


def assert_at_its_cron(name: str, timestamp: int) -> None:
    at = datetime.fromtimestamp(timestamp, UTC)
    assert at.second == 0, at
    if name == "batch_poll":
        assert at.minute % 5 == 0, at
    elif name == "batch_reconcile_ambiguous":
        assert at.minute % 15 == 0, at
    else:
        assert (at.hour, at.minute) == (6, 41), at


async def deferred_job(engine: AsyncEngine, app: procrastinate.App, name: str) -> Any:
    """`name`'s job deferred by its cron, checked against its queue and queueing lock; its row."""
    timestamp = await defer_by_cron(app, name)
    assert_at_its_cron(name, timestamp)
    (job,) = await jobs_of(engine, name)
    _, queue, queueing_lock = PERIODIC[name]
    assert (job.queue_name, job.lock, job.queueing_lock, job.args, job.status) == (
        queue,
        None,
        queueing_lock,
        {"timestamp": timestamp},
        "todo",
    )
    return job


# ---- each periodic task, deferred by its cron, runs its body on the fake ----------------------------------


async def test_batch_poll_is_deferred_by_its_cron_and_polls_on_the_fake(
    engine: AsyncEngine,
    queue_app: procrastinate.App,
    op_runner: OpRunner,
    fake_llm: FakeLLM,
    runtime: WorkerRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted = await submit_batch(engine, op_runner, fake_llm, BATCH, polls_until_ended=0)
    job = await deferred_job(engine, queue_app, "batch_poll")
    await run_queue(queue_app, runtime, "aux", monkeypatch)
    assert [(row.id, row.status) for row in await jobs_of(engine, "batch_poll")] == [(job.id, "succeeded")]
    assert [row.state for row in await submission_rows(engine)] == ["reconciled"]
    stored = fake_llm.state.batches[submitted.provider_id]
    assert stored.retrievals >= 1 and stored.results_read >= 1  # the worker's client polled it


async def test_batch_reconcile_ambiguous_is_deferred_by_its_cron_and_adopts_on_the_fake(
    engine: AsyncEngine,
    queue_app: procrastinate.App,
    op_runner: OpRunner,
    fake_llm: FakeLLM,
    runtime: WorkerRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await bulk_with_targets(engine, BATCH)
    for target in setup.targets:
        fake_llm.answer("generate", target.job_revision_id, PUBLISHED_BODY)
    fake_llm.batches(BatchScript(create="ambiguous_created", polls_until_ended=0))
    assert setup.operation_id is not None
    await op_runner.run_one(setup.operation_id)  # the create's answer is lost: `submit_ambiguous`
    assert [row.state for row in await submission_rows(engine)] == ["submit_ambiguous"]
    await deferred_job(engine, queue_app, "batch_reconcile_ambiguous")
    await run_queue(queue_app, runtime, "aux", monkeypatch)
    assert [row.status for row in await jobs_of(engine, "batch_reconcile_ambiguous")] == ["succeeded"]
    (sub,) = await submission_rows(engine)
    assert sub.state == "reconciled" and sub.provider_batch_id is not None  # adopted by its custom ids


async def test_canary_daily_is_deferred_by_its_cron_and_starts_a_canary_run(
    engine: AsyncEngine,
    queue_app: procrastinate.App,
    runtime: WorkerRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with engine.begin() as conn:
        await activate_score_tuple(conn)
    (case,) = await insert_cases(engine, 1)
    async with engine.begin() as conn:
        await golden.set_flags(conn, case.id, active=None, is_canary=True, now=utcnow())
    await deferred_job(engine, queue_app, "canary_daily")
    await run_queue(queue_app, runtime, "tick", monkeypatch)
    assert [row.status for row in await jobs_of(engine, "canary_daily")] == ["succeeded"]
    async with engine.connect() as conn:
        runs = [tuple(row)[1:] for row in await conn.execute(_CANARY_RUNS)]
    assert runs == [("canary", "running")]  # its `eval_run` operation waits on `aux`
    (operation,) = await jobs_of(engine, "run_operation")
    assert (operation.queue_name, operation.status) == ("aux", "todo")


# ---- the fence first, nothing after the admission stop ----------------------------------------------------


def job_context(runtime: Any) -> JobContext:
    return cast(JobContext, SimpleNamespace(additional_context={"runtime": runtime}))


def recording_bodies(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """What each periodic task delegates to, replaced by a recorder of its name (the bodies themselves are
    `test_batch_poll.py`'s, `test_batch_ambiguous.py`'s and `test_canaries.py`'s)."""
    called: list[str] = []

    def recorder(name: str) -> Callable[..., Awaitable[None]]:
        async def record(*args: object, **kwargs: object) -> None:
            called.append(name)

        return record

    monkeypatch.setattr(batch_results, "poll_task", recorder("batch_poll"))
    monkeypatch.setattr(batch_adoption, "reconcile_ambiguous_task", recorder("batch_reconcile_ambiguous"))
    monkeypatch.setattr(eval_calibration, "start_canary_run", recorder("canary_daily"))
    return called


TASKS: Final[dict[str, Callable[..., Awaitable[None]]]] = {
    "batch_poll": tasks.batch_poll,
    "batch_reconcile_ambiguous": tasks.batch_reconcile_ambiguous,
    "canary_daily": tasks.canary_daily,
}


@pytest.mark.parametrize("name", list(PERIODIC))
async def test_a_periodic_m3_task_starts_with_the_epoch_fence_and_skips_after_the_admission_stop(
    engine: AsyncEngine,
    current_epoch: int,
    fake_llm: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    name: str,
) -> None:
    """§3.3.5: no new LLM call or batch submit after the admission stop; Ruling T24-a: a superseded worker's
    task does nothing and requests this worker's stop; the current worker's runs its body."""
    caplog.set_level(logging.INFO)
    called = recording_bodies(monkeypatch)
    body = TASKS[name]

    def runtime(epoch: int, *, stopped: bool = False) -> SimpleNamespace:
        shutdown = ShutdownState(admission_stopped=stopped)
        return SimpleNamespace(
            engine=engine, cfg=fake_llm.cfg, epoch=epoch, shutdown=shutdown, batches_client=None
        )

    stopped = runtime(current_epoch, stopped=True)
    await body(job_context(stopped), timestamp=0)
    assert called == [] and not stopped.shutdown.requested.is_set()
    assert [(e["task"], e["reason"]) for e in logged(caplog, "periodic_task_skipped")] == [
        (name, "admission_stopped")
    ]
    superseded = runtime(current_epoch - 1)
    await body(job_context(superseded), timestamp=0)
    assert called == [] and superseded.shutdown.requested.is_set()  # it stops itself, having done nothing
    current = runtime(current_epoch)
    await body(job_context(current), timestamp=0)
    assert called == [name] and not current.shutdown.requested.is_set()


# ---- score_cycle under the `score` lock ------------------------------------------------------------------


def stall(fake_llm: FakeLLM, *targets: PendingTarget) -> None:
    """Each target's generator stream holds open after its first event, until its call is cancelled."""
    _, schemas = prompt_schema_v2()
    generator = schemas["generator"]
    assert isinstance(generator, dict)
    fake_llm.record("generate", SONNET, synthetic_c_draft(), schema=generator)
    for target in targets:
        fake.answer(
            fake.answer_key("generate", {"job_revision_id": target.job_revision_id}, 0),
            "stall_after_first_event",
        )


def answer_all(fake_llm: FakeLLM, targets: list[PendingTarget]) -> None:
    for target in targets:
        fake_llm.answer("generate", target.job_revision_id, synthetic_c_draft())


async def events_of(engine: AsyncEngine, job_id: int) -> dict[str, datetime]:
    async with engine.connect() as conn:
        return {str(r.type): r.at for r in await conn.execute(_EVENTS, {"j": job_id})}


async def test_score_cycle_takes_the_score_lock_so_a_second_cycle_job_waits_for_the_first(
    engine: AsyncEngine,
    queue_app: procrastinate.App,
    fake_llm: FakeLLM,
    runtime: WorkerRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex r2 B25: `score_cycle(trigger)` runs under the procrastinate lock `score`, with the queueing lock
    `score_cycle:<trigger>`. A worker of the `score` queue with room for two jobs runs one cycle at a time:
    the second trigger's job stays `todo` while the first cycle runs, and starts once it ended."""
    answer_all(fake_llm, await insert_targets(engine, 2, lane="score_now"))
    async with engine.begin() as conn:
        assert await cycles.request_cycle(conn, trigger="score_now", defer=defer_in_tx)
        assert await cycles.request_cycle(conn, trigger="manual", defer=defer_in_tx)
    first, second = await jobs_of(engine, "score_cycle")
    assert [(j.queue_name, j.lock, j.queueing_lock, j.args) for j in (first, second)] == [
        ("score", "score", "score_cycle:score_now", {"trigger": "score_now"}),
        ("score", "score", "score_cycle:manual", {"trigger": "manual"}),
    ]
    held = PausePoint(claim_node.AFTER_CLAIM)  # the first cycle holds after its claim
    monkeypatch.setattr(claim_node, "afault_point", held)
    monkeypatch.setattr(queue_app.periodic_registry, "periodic_tasks", {})
    worker = asyncio.create_task(
        queue_app.run_worker_async(
            queues=["score"],
            concurrency=2,  # room for both: only the lock keeps the second one back
            fetch_job_polling_interval=0.1,
            listen_notify=False,
            install_signal_handlers=False,
            additional_context={"runtime": runtime},
            name="test-score",
        )
    )
    try:
        await asyncio.wait_for(held.reached.wait(), WAIT_S)
        await asyncio.sleep(0.5)  # five polls of the worker, with room for a second job
        assert [j.status for j in await jobs_of(engine, "score_cycle")] == ["doing", "todo"]
        held.resume()

        async def both_ended() -> bool:
            return [j.status for j in await jobs_of(engine, "score_cycle")] == ["succeeded", "succeeded"]

        await await_until(both_ended, WAIT_S, 0.1)
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
    ended = await events_of(engine, first.id)
    assert (await events_of(engine, second.id))["started"] >= ended["succeeded"]
    async with engine.connect() as conn:
        statuses = (
            (await conn.execute(sa.text("SELECT status FROM score_cycles ORDER BY id"))).scalars().all()
        )
    assert statuses[0] == "completed"  # the second, with nothing left to admit, started none or an empty one


# ---- the stalled-job rule (Codex r1 #17) -------------------------------------------------------------------


async def test_a_lone_stalled_score_cycle_job_is_failed_not_retried(
    engine: AsyncEngine,
    queue_app: procrastinate.App,
    fake_llm: FakeLLM,
    runtime: WorkerRuntime,
    current_epoch: int,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A worker died running a `score_cycle` job, no job waits on its queueing lock: the stalled job ends
    `failed` (`NO_STALL_RETRY`), never retried; the cycle it left `running` is failed `orphaned` by the
    reconciler; and a later delivery of the task (a retried job's, however it came) starts a fresh cycle on
    a thread of its own: the old cycle's thread is never invoked again."""
    targets = await insert_targets(engine, 2, lane="score_now")
    stall(fake_llm, *targets)
    async with engine.begin() as conn:
        assert await cycles.request_cycle(conn, trigger="score_now", defer=defer_in_tx)
        await conn.execute(sa.text("UPDATE procrastinate_jobs SET status = 'doing'"))  # its worker is gone
    invoked: list[int] = []
    real_invoke = graph_mod.invoke_cycle

    async def invoke(graph: Any, ctx: Any) -> Any:
        invoked.append(ctx.cycle_id)
        return await real_invoke(graph, ctx)

    monkeypatch.setattr(graph_mod, "invoke_cycle", invoke)
    dying = asyncio.create_task(tasks.score_cycle(job_context(runtime), trigger="score_now"))

    async def both_streams_open() -> bool:
        async with engine.connect() as conn:
            count = sa.text("SELECT count(*) FROM llm_calls WHERE status = 'dispatched'")
            return int((await conn.execute(count)).scalar_one()) == 2

    await await_until(both_streams_open, WAIT_S, 0.1)
    dying.cancel()  # the process dies with its cycle running
    with suppress(asyncio.CancelledError):
        await dying
    (old,) = invoked
    assert (await cycle_of(engine, old)).status == "running"
    async with engine.connect() as conn:
        old_thread = f"cycle-{old}"
        checkpoints = int((await conn.execute(_THREAD_CHECKPOINTS, {"t": old_thread})).scalar_one())
    assert checkpoints > 0

    assert await release_stalled_jobs(queue_app) == 0  # not retried
    assert [j.status for j in await jobs_of(engine, "score_cycle")] == ["failed"]
    assert [e["task_name"] for e in logged(caplog, "stalled_job_failed")] == ["jh:score_cycle"]
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE score_cycles SET heartbeat_at = now() - interval '4 minutes' WHERE id = :c"),
            {"c": old},
        )
    assert await run_reconciler("orphaned_cycles", engine, now=utcnow(), epoch=current_epoch) == 1
    row = await cycle_of(engine, old)
    assert (row.status, row.status_reason) == ("failed", "orphaned")
    for target in targets:
        assert (await target_of(engine, target.target_id)).state == "pending"  # its claims reclaimed

    answer_all(fake_llm, targets)  # the next delivery's calls are answered
    await tasks.score_cycle(job_context(runtime), trigger="score_now")
    assert len(invoked) == 2 and invoked[0] == old
    new = invoked[1]
    assert new != old  # a fresh cycle on its own thread, never the old one's
    async with engine.connect() as conn:
        assert int((await conn.execute(_THREAD_CHECKPOINTS, {"t": old_thread})).scalar_one()) == checkpoints
    assert (await cycle_of(engine, new)).status == "completed"
    for target in targets:
        assert (await target_of(engine, target.target_id)).state == "published_accepted"
