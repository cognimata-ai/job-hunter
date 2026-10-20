import asyncio
import uuid
from collections.abc import Callable
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.errors import FencedOut
from jobhunter.graphs.checkpointer import open_saver
from jobhunter.graphs.collect.graph import CollectState, CollectUpdate, build_noop_collect_graph
from jobhunter.observability.metrics import REGISTRY
from jobhunter.worker import epoch as epoch_module
from jobhunter.worker import runs
from jobhunter.worker.epoch import WorkerIdentity, claim_epoch_and_sweep
from jobhunter.worker.executions import EXECUTIONS
from jobhunter.worker.reconcilers import ReconcileContext, run_reconcilers
from jobhunter.worker.runtime import ShutdownState
from jobhunter.worker.tasks import defer_in_tx, job_task_name, live_job_exists, run_collect
from langgraph.graph import END, START, StateGraph
from procrastinate.job_context import JobContext

from tests.support.faults import PausePoint
from tests.support.pg import blocked_or_done, lock_waits
from tests.support.procs import await_until

# Every test here admits or claims as `epoch=1` (or a later claimed epoch): epoch 1 must be the current one.
pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("current_epoch")]


def gated_graph(gate: asyncio.Event, entered: asyncio.Event):
    async def wait(state: CollectState) -> CollectUpdate:
        entered.set()
        await gate.wait()
        return {"steps": ["wait"]}

    g = StateGraph(CollectState)
    g.add_node("wait", wait)
    g.add_edge(START, "wait")
    g.add_edge("wait", END)
    return g.compile()


def raising_graph(error: Exception):
    """A one-node graph (no checkpointer) whose node raises `error`."""

    async def boom(state: CollectState) -> CollectUpdate:
        raise error

    g = StateGraph(CollectState)
    g.add_node("boom", boom)
    g.add_edge(START, "boom")
    g.add_edge("boom", END)
    return g.compile()


def runs_total(status: str) -> float:
    """`jobhunter_runs_total{status}` in this process (0 before the first count)."""
    return REGISTRY.get_sample_value("jobhunter_runs_total", {"status": status}) or 0.0


def timed_runs() -> float:
    """How many run durations `jobhunter_run_duration_seconds` has observed in this process."""
    return REGISTRY.get_sample_value("jobhunter_run_duration_seconds_count") or 0.0


async def scalar(engine, sql: str, **params):
    async with engine.connect() as c:
        return (await c.execute(sa.text(sql), params)).scalar_one()


async def age_heartbeat(engine, rid: int) -> None:
    async with engine.begin() as c:
        await c.execute(
            sa.text("UPDATE runs SET heartbeat_at = now() - interval '4 minutes' WHERE id=:i"), {"i": rid}
        )


async def resume_three_times(engine, rid: int, *, epoch: int) -> None:
    """Start attempt 1 of `rid` for `epoch` and give it three new attempts: the next one exhausts it."""
    async with engine.begin() as c:
        await runs.start_attempt(c, rid, 1, epoch=epoch)
    for _ in range(3):
        async with engine.begin() as c:
            await runs.begin_new_attempt(c, rid, epoch=epoch, end_reason="heartbeat_stale", defer=defer_in_tx)


async def queued(engine) -> int:
    async with engine.begin() as c:
        row, created = await runs.request_manual_run(c, include_paid=False, defer=defer_in_tx)
        assert created
        return row.id


async def fetch(engine, rid: int):
    async with engine.connect() as c:
        return tuple(
            (
                await c.execute(
                    sa.text(
                        "SELECT status, attempt_no, resume_count, current_thread_id, report, status_reason "
                        "FROM runs WHERE id=:i"
                    ),
                    {"i": rid},
                )
            ).one()
        )


async def test_happy_path_noop_run(engine, queue_app, migrated) -> None:
    rid = await queued(engine)
    async with open_saver(migrated.dsn("app")) as saver:
        assert await runs.execute_run(engine, build_noop_collect_graph(saver), rid, 1, epoch=1) == "started"
        assert await saver.aget_tuple({"configurable": {"thread_id": f"run-{rid}-a1"}}) is not None
    assert (await fetch(engine, rid))[:5] == ("succeeded", 1, 0, f"run-{rid}-a1", {"noop": True})
    async with engine.connect() as c:
        assert tuple(
            (
                await c.execute(
                    sa.text("SELECT worker_epoch, end_reason FROM run_attempts WHERE run_id=:i"), {"i": rid}
                )
            ).one()
        ) == (1, "completed")


async def test_duplicate_delivery_while_executing_is_noop(engine, queue_app) -> None:
    rid, gate, entered = await queued(engine), asyncio.Event(), asyncio.Event()
    first = asyncio.create_task(runs.execute_run(engine, gated_graph(gate, entered), rid, 1, epoch=1))
    await asyncio.wait_for(
        entered.wait(), 5
    )  # bounded: a graph that never starts fails the test, never hangs it
    assert await runs.execute_run(engine, gated_graph(gate, entered), rid, 1, epoch=1) == "duplicate"
    gate.set()
    assert await first == "started"
    # The gated graph has no checkpointer: it succeeds only because no `durability` is passed (V6, T12-f).
    assert (await fetch(engine, rid))[:5] == ("succeeded", 1, 0, f"run-{rid}-a1", {"noop": True})
    assert await runs.execute_run(engine, gated_graph(gate, entered), rid, 1, epoch=1) == "finished"


async def test_superseded_attempt_job_is_noop(engine, queue_app) -> None:
    rid = await queued(engine)
    async with engine.begin() as c:
        assert (await runs.start_attempt(c, rid, 1, epoch=1))[0] == "started"
    async with engine.begin() as c:
        assert (
            await runs.begin_new_attempt(c, rid, epoch=1, end_reason="heartbeat_stale", defer=defer_in_tx)
            == 2
        )
    async with engine.begin() as c:
        assert (await runs.start_attempt(c, rid, 1, epoch=1)) == ("superseded", None)
    assert (await fetch(engine, rid))[:4] == ("running", 2, 1, f"run-{rid}-a2")


async def test_sweep_created_attempt_is_admitted_only_for_its_epoch(
    engine, queue_app, migrated, worker_lock
) -> None:
    rid = await queued(engine)
    e1, _ = await claim_epoch_and_sweep(
        engine, WorkerIdentity(uuid.uuid4(), "h", 1, worker_lock.backend_pid, "v")
    )
    async with engine.begin() as c:
        await runs.start_attempt(c, rid, 1, epoch=e1)
    e2, counts = await claim_epoch_and_sweep(
        engine, WorkerIdentity(uuid.uuid4(), "h", 2, worker_lock.backend_pid, "v")
    )
    assert counts["runs"] == 1
    async with engine.connect() as c:
        assert await live_job_exists(c, "run_collect", args_match={"run_id": rid, "attempt_no": 2})
    async with engine.begin() as c:
        assert (await runs.start_attempt(c, rid, 2, epoch=e1))[0] == "foreign_epoch"
    async with open_saver(migrated.dsn("app")) as saver:
        assert await runs.execute_run(engine, build_noop_collect_graph(saver), rid, 2, epoch=e2) == "resumed"
    async with engine.connect() as c:
        assert (
            await c.execute(
                sa.text("SELECT end_reason FROM run_attempts WHERE run_id=:i AND attempt_no=1"), {"i": rid}
            )
        ).scalar_one() == "worker_takeover"
    assert (await fetch(engine, rid))[:2] == ("succeeded", 2)


async def test_stale_recovery_cancels_live_old_graph_before_replacement(engine, queue_app, migrated) -> None:
    rid, gate, entered = await queued(engine), asyncio.Event(), asyncio.Event()
    old = asyncio.create_task(
        runs.execute_run(engine, gated_graph(gate, entered), rid, 1, epoch=1, heartbeat_period_s=3600)
    )
    await asyncio.wait_for(entered.wait(), 5)
    await age_heartbeat(engine, rid)
    await run_reconcilers(
        ReconcileContext(engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow(), epoch=1), "tick"
    )
    await asyncio.wait_for(old, 5)
    assert EXECUTIONS.get(("run", rid)) is None and (await fetch(engine, rid))[:2] == ("running", 2)
    async with open_saver(migrated.dsn("app")) as saver:
        assert await runs.execute_run(engine, build_noop_collect_graph(saver), rid, 2, epoch=1) == "resumed"
    assert (await fetch(engine, rid))[:2] == ("succeeded", 2)
    # The cancelled gated graph (no checkpointer) wrote no failure: it was lost, never `failed('bug')`.
    assert await scalar(engine, "SELECT count(*) FROM alerts WHERE code='run.failed'") == 0


async def test_old_owner_is_fenced(engine, queue_app) -> None:
    rid = await queued(engine)
    async with engine.begin() as c:
        _, old = await runs.start_attempt(c, rid, 1, epoch=1)
    async with engine.begin() as c:
        await runs.begin_new_attempt(c, rid, epoch=1, end_reason="heartbeat_stale", defer=defer_in_tx)
    assert old is not None
    async with engine.begin() as c:
        with pytest.raises(FencedOut):
            await runs.finalize_run(c, old, status="succeeded", reason=None, report={})
        assert await runs.heartbeat_run(c, old) is False


async def test_resume_exhausted_after_three_resumes(engine, queue_app) -> None:
    rid = await queued(engine)
    async with engine.begin() as c:
        await runs.start_attempt(c, rid, 1, epoch=1)
    for expected in (2, 3, 4, None):
        async with engine.begin() as c:
            assert (
                await runs.begin_new_attempt(c, rid, epoch=1, end_reason="heartbeat_stale", defer=defer_in_tx)
                == expected
            )
    assert (await fetch(engine, rid))[0::5] == ("failed", "resume_exhausted") and (await fetch(engine, rid))[
        2
    ] == 4
    async with engine.connect() as c:
        assert (
            await c.execute(sa.text("SELECT severity FROM alerts WHERE code='run.resume_exhausted'"))
        ).scalar_one() == "critical"
    # The exhausting take-back closed the last open attempt too.
    assert (
        await scalar(engine, "SELECT count(*) FROM run_attempts WHERE run_id=:i AND ended_at IS NULL", i=rid)
        == 0
    )
    assert (
        await scalar(engine, "SELECT end_reason FROM run_attempts WHERE run_id=:i AND attempt_no=4", i=rid)
        == "heartbeat_stale"
    )


async def test_queued_runs_reconciler_redefers_once_then_fails(engine, queue_app) -> None:
    rid = await queued(engine)

    def ctx(m: float) -> ReconcileContext:
        return ReconcileContext(
            engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow() + timedelta(minutes=m), epoch=1
        )

    async with engine.begin() as c:
        await c.execute(sa.text("DELETE FROM procrastinate_jobs"))
    failed, timed = runs_total("failed"), timed_runs()
    await run_reconcilers(ctx(2.1), "tick")
    assert (await fetch(engine, rid))[5] == "redeferred"
    async with engine.connect() as c:  # V1: the lost job was deferred again
        assert await live_job_exists(c, "run_collect", args_match={"run_id": rid, "attempt_no": 1})
    assert runs_total("failed") == failed  # a re-defer ends nothing
    async with engine.begin() as c:
        await c.execute(sa.text("DELETE FROM procrastinate_jobs"))
    await run_reconcilers(ctx(4.2), "tick")
    assert (await fetch(engine, rid))[0::5] == ("failed", "orphaned_queue")
    assert (
        await scalar(
            engine,
            "SELECT severity FROM alerts WHERE dedup_key=:k AND resolved_at IS NULL",
            k=f"run.orphaned_queue:run:{rid}",
        )
        == "warn"
    )
    # Counted after the reconciler's commit; never timed, because it never started.
    assert (runs_total("failed"), timed_runs()) == (failed + 1, timed)


async def test_stale_data_alert_after_grace_and_two_intervals(engine, queue_app) -> None:
    async with engine.connect() as c:  # the injected clock starts at the seed time, not at the wall clock
        seeded = (await c.execute(sa.text("SELECT min(created_at) FROM settings_versions"))).scalar_one()

    def ctx(h: float) -> ReconcileContext:
        return ReconcileContext(
            engine=engine, queue=queue_app, defer=defer_in_tx, now=seeded + timedelta(hours=h), epoch=1
        )

    await run_reconcilers(ctx(11), "tick")  # 6 h interval: within the 12 h grace
    async with engine.connect() as c:
        assert (
            await c.execute(sa.text("SELECT count(*) FROM alerts WHERE code='schedule.stale_data'"))
        ).scalar_one() == 0
    await run_reconcilers(ctx(13), "tick")
    async with engine.connect() as c:
        assert (
            await c.execute(
                sa.text(
                    "SELECT severity FROM alerts WHERE code='schedule.stale_data' AND resolved_at IS NULL"
                )
            )
        ).scalar_one() == "critical"
    # A run that succeeded at seed + 12.5 h (the injected clock's time) resolves it at seed + 13 h ...
    rid = await queued(engine)
    async with engine.begin() as c:
        _, owner = await runs.start_attempt(c, rid, 1, epoch=1)
        assert owner is not None
        await runs.finalize_run(c, owner, status="succeeded", reason=None, report={})
        await c.execute(
            sa.text("UPDATE runs SET finished_at = :t WHERE id = :i"),
            {"t": seeded + timedelta(hours=12.5), "i": rid},
        )
    await run_reconcilers(ctx(13), "tick")
    open_or_resolved = (
        "SELECT count(*) FROM alerts WHERE code='schedule.stale_data' AND (resolved_at IS NULL) = :o"
    )
    assert (
        await scalar(engine, open_or_resolved, o=True),
        await scalar(engine, open_or_resolved, o=False),
    ) == (0, 1)
    # ... and it is raised again once that success is older than twice the interval.
    await run_reconcilers(ctx(24.6), "tick")
    assert (
        await scalar(
            engine, "SELECT severity FROM alerts WHERE code='schedule.stale_data' AND resolved_at IS NULL"
        )
        == "critical"
    )


async def test_run_collect_body_executes_the_run_with_the_runtime(monkeypatch) -> None:
    calls: list[tuple[object, object, int, int, int]] = []

    async def fake_execute_run(
        engine: object,
        graph: object,
        run_id: int,
        attempt_no: int,
        *,
        epoch: int,
        on_superseded: Callable[[], None],
    ) -> str:
        calls.append((engine, graph, run_id, attempt_no, epoch))
        on_superseded()  # as an admission whose epoch the fence refused calls it
        return "foreign_epoch"

    monkeypatch.setattr(runs, "execute_run", fake_execute_run)
    engine, graph = object(), object()
    runtime = SimpleNamespace(
        engine=engine, collect_graph=graph, epoch=7, shutdown=ShutdownState()
    )  # the `WorkerRuntime` fields it reads
    context = cast(JobContext, SimpleNamespace(additional_context={"runtime": runtime}))
    await run_collect(context, run_id=5, attempt_no=2)
    assert calls == [(engine, graph, 5, 2, 7)]
    # Ruling T24-a: a superseded delivery asks this worker's runtime for the graceful stop.
    assert runtime.shutdown.requested.is_set()


async def test_graph_error_fails_the_run_as_bug_with_a_critical_alert(engine, queue_app) -> None:
    rid = await queued(engine)
    failed, timed = runs_total("failed"), timed_runs()
    assert (
        await runs.execute_run(engine, raising_graph(RuntimeError("node broke")), rid, 1, epoch=1)
        == "started"
    )
    row = await fetch(engine, rid)
    assert (row[0], row[4], row[5]) == ("failed", {}, "bug")
    assert (
        await scalar(
            engine,
            "SELECT severity FROM alerts WHERE dedup_key=:k AND resolved_at IS NULL",
            k=f"run.failed:run:{rid}",
        )
        == "critical"
    )
    assert (
        await scalar(engine, "SELECT ended_at IS NOT NULL FROM run_attempts WHERE run_id=:i", i=rid) is True
    )
    assert (runs_total("failed"), timed_runs()) == (failed + 1, timed + 1)


async def test_fenced_out_inside_the_graph_is_lost_without_owner_writes(engine, queue_app) -> None:
    rid = await queued(engine)
    before = (runs_total("succeeded"), runs_total("failed"), timed_runs())
    assert await runs.execute_run(engine, raising_graph(FencedOut("run")), rid, 1, epoch=1) == "started"
    # Lost: the run stays as the admission left it, the attempt open, no alert and no metric.
    assert await fetch(engine, rid) == ("running", 1, 0, f"run-{rid}-a1", None, None)
    assert await scalar(engine, "SELECT ended_at FROM run_attempts WHERE run_id=:i", i=rid) is None
    assert await scalar(engine, "SELECT count(*) FROM alerts") == 0
    assert (runs_total("succeeded"), runs_total("failed"), timed_runs()) == before


async def test_manual_run_request_returns_the_active_run_without_a_second_job(engine, queue_app) -> None:
    rid = await queued(engine)
    async with engine.begin() as c:
        row, created = await runs.request_manual_run(c, include_paid=True, defer=defer_in_tx)
    assert (row.id, row.status, row.trigger, created) == (rid, "queued", "manual", False)
    async with engine.begin() as c:
        await runs.start_attempt(c, rid, 1, epoch=1)
    async with engine.begin() as c:
        row, created = await runs.request_manual_run(c, include_paid=True, defer=defer_in_tx)
    assert (row.id, row.status, row.attempt_no, created) == (rid, "running", 1, False)
    jobs = "SELECT count(*) FROM procrastinate_jobs WHERE task_name=:t"
    assert await scalar(engine, jobs, t=job_task_name("run_collect")) == 1
    assert await scalar(engine, "SELECT count(*) FROM runs") == 1


async def test_stale_runs_counts_an_exhausted_run_after_its_commit(engine, queue_app) -> None:
    rid = await queued(engine)
    await resume_three_times(engine, rid, epoch=1)
    await age_heartbeat(engine, rid)
    failed, timed = runs_total("failed"), timed_runs()
    await run_reconcilers(
        ReconcileContext(engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow(), epoch=1), "tick"
    )
    assert (await fetch(engine, rid))[0::5] == ("failed", "resume_exhausted")
    assert (runs_total("failed"), timed_runs()) == (failed + 1, timed + 1)


async def test_an_exhausted_run_with_a_live_execution_is_counted_once_by_the_reconciler(
    engine, queue_app, log_events
) -> None:
    """F5 review, Important 1: `stale_runs` ends a run `resume_exhausted` while its execution is still live in
    this worker. The reconciler counts the end after its commit and cancels the execution, which is `lost`;
    `execute_run` must not count that end again, because the attempt's own `finalize_run` did not write it
    (the attempt closed `heartbeat_stale`), though the run keeps that attempt number."""
    rid, gate, entered = await queued(engine), asyncio.Event(), asyncio.Event()
    await resume_three_times(engine, rid, epoch=1)  # running at attempt 4, the next resume exhausts it
    failed, timed = runs_total("failed"), timed_runs()
    live = asyncio.create_task(
        runs.execute_run(engine, gated_graph(gate, entered), rid, 4, epoch=1, heartbeat_period_s=3600)
    )
    await asyncio.wait_for(entered.wait(), 5)
    await age_heartbeat(engine, rid)
    await run_reconcilers(
        ReconcileContext(engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow(), epoch=1), "tick"
    )
    assert await asyncio.wait_for(live, 5) == "resumed"
    assert (await fetch(engine, rid))[:2] == ("failed", 4) and (await fetch(engine, rid))[
        5
    ] == "resume_exhausted"
    assert (runs_total("failed"), timed_runs()) == (failed + 1, timed + 1)  # the reconciler's count only
    events = log_events()
    assert [e["key"] for e in events if e["event"] == "execution_lost"] == [f"run:{rid}"]
    assert not [e for e in events if e["event"] == "run_finished"]
    async with engine.connect() as c:
        assert await runs.run_completion(c, rid, 4) is None  # no end of the attempt's own


async def test_takeover_sweep_counts_an_exhausted_run_only_after_the_takeover_commits(
    engine, queue_app, worker_lock, monkeypatch
) -> None:
    rid = await queued(engine)
    e1, _ = await claim_epoch_and_sweep(
        engine, WorkerIdentity(uuid.uuid4(), "h", 1, worker_lock.backend_pid, "v")
    )
    await resume_three_times(engine, rid, epoch=e1)
    failed, timed = runs_total("failed"), timed_runs()

    async def failing_sweep(conn, epoch: int) -> int:
        raise RuntimeError("a later sweep fails")

    with monkeypatch.context() as m:  # the takeover rolls back after the `runs` sweep ended the run
        m.setattr(epoch_module, "_SWEEPS", {**epoch_module._SWEEPS, "failing": failing_sweep})
        with pytest.raises(RuntimeError):
            await claim_epoch_and_sweep(
                engine, WorkerIdentity(uuid.uuid4(), "h", 2, worker_lock.backend_pid, "v")
            )
    assert (await fetch(engine, rid))[:2] == ("running", 4)
    assert (runs_total("failed"), timed_runs()) == (failed, timed)  # nothing counted for a rolled-back end
    _, counts = await claim_epoch_and_sweep(
        engine, WorkerIdentity(uuid.uuid4(), "h", 3, worker_lock.backend_pid, "v")
    )
    assert counts["runs"] == 1 and (await fetch(engine, rid))[0::5] == ("failed", "resume_exhausted")
    assert (runs_total("failed"), timed_runs()) == (failed + 1, timed + 1)


async def test_live_execution_that_outlives_its_cancel_makes_the_delivery_a_duplicate(
    engine, queue_app, monkeypatch
) -> None:
    rid, gate, entered = await queued(engine), asyncio.Event(), asyncio.Event()
    release = asyncio.Event()

    async def lingering() -> None:
        await release.wait()

    async def cancel_timed_out(key, *, timeout_s: float = 30.0) -> bool:
        return False  # what `cancel_and_await` reports when the task is still running after its timeout

    older = EXECUTIONS.start(("run", rid), 0, lingering())  # an older local execution of the run
    monkeypatch.setattr(EXECUTIONS, "cancel_and_await", cancel_timed_out)
    try:
        assert await runs.execute_run(engine, gated_graph(gate, entered), rid, 1, epoch=1) == "duplicate"
        assert not entered.is_set()  # nothing ran
        assert (await fetch(engine, rid))[:2] == ("running", 1)  # admitted, never finalized
    finally:
        release.set()
        await older


# --- Final fix wave, item 2: admission is fenced by the current worker epoch (ruling T21-c) ----------------


def claimant(worker_lock, n: int) -> WorkerIdentity:
    return WorkerIdentity(uuid.uuid4(), "h", n, worker_lock.backend_pid, "v")


def open_gate_graph(entered: asyncio.Event):
    """A graph that returns at once, setting `entered` when it runs."""
    gate = asyncio.Event()
    gate.set()
    return gated_graph(gate, entered)


async def attempts(engine, rid: int) -> list[tuple[int, int, str | None]]:
    """`(attempt_no, worker_epoch, end_reason)` of every attempt of `rid`, in order."""
    async with engine.connect() as c:
        rows = await c.execute(
            sa.text(
                "SELECT attempt_no, worker_epoch, end_reason FROM run_attempts WHERE run_id=:i "
                "ORDER BY attempt_no"
            ),
            {"i": rid},
        )
        return [tuple(r) for r in rows]


async def test_a_worker_paused_before_admission_is_fenced_once_another_epoch_took_over(
    engine, queue_app, worker_lock, monkeypatch, log_events
) -> None:
    rid = await queued(engine)
    e1, _ = await claim_epoch_and_sweep(engine, claimant(worker_lock, 1))
    pause = PausePoint("collect.before_admission")
    monkeypatch.setattr(runs, "afault_point", pause)
    entered = asyncio.Event()
    zombie = asyncio.create_task(runs.execute_run(engine, open_gate_graph(entered), rid, 1, epoch=e1))
    await asyncio.wait_for(pause.reached.wait(), 5)  # worker A (epoch e1) is about to admit the run ...
    e2, counts = await claim_epoch_and_sweep(engine, claimant(worker_lock, 2))  # ... when B takes over
    assert counts["runs"] == 0  # the run is still queued: the sweep has nothing to take
    pause.resume()
    assert await asyncio.wait_for(zombie, 5) == "foreign_epoch"
    assert not entered.is_set() and await attempts(engine, rid) == []  # A ran nothing and wrote nothing
    fenced = [(e["epoch"], e["current_epoch"]) for e in log_events() if e["event"] == "worker_epoch_fenced"]
    assert fenced == [(e1, e2)]
    assert (await fetch(engine, rid))[:2] == ("queued", 0)
    async with engine.begin() as c:  # B's admission of the same work succeeds
        assert (await runs.start_attempt(c, rid, 1, epoch=e2))[0] == "started"
    assert await attempts(engine, rid) == [(1, e2, None)]


async def test_an_admission_in_flight_at_the_takeover_commits_first_and_is_swept(
    engine, queue_app, worker_lock, migrated
) -> None:
    rid = await queued(engine)
    a_ident = claimant(worker_lock, 1)
    e1, _ = await claim_epoch_and_sweep(engine, a_ident)
    async with engine.connect() as a:
        tx = await a.begin()
        admission, owner = await runs.start_attempt(a, rid, 1, epoch=e1)  # A's admission, not committed yet
        # The admission's share lock on the worker_heartbeat row never holds back the watchdog's heartbeat.
        assert worker_lock.heartbeat(a_ident.instance_id, e1) is True
        takeover = asyncio.create_task(claim_epoch_and_sweep(engine, claimant(worker_lock, 2)))
        try:
            await await_until(lambda: blocked_or_done(migrated, takeover), 5)
            waited = not takeover.done()  # the takeover waits for the admission ...
        finally:
            await tx.commit()
    e2, counts = await asyncio.wait_for(takeover, 5)
    # ... and then sweeps it: A's attempt ends, the run's new attempt belongs to B, A's owner is fenced.
    assert (admission, waited, counts["runs"]) == ("started", True, 1)
    assert await attempts(engine, rid) == [(1, e1, "worker_takeover"), (2, e2, None)]
    async with engine.begin() as c:
        assert owner is not None and await runs.heartbeat_run(c, owner) is False


async def test_an_admission_during_a_takeover_waits_for_it_and_is_fenced(
    engine, queue_app, worker_lock, migrated, monkeypatch
) -> None:
    rid = await queued(engine)
    e1, _ = await claim_epoch_and_sweep(engine, claimant(worker_lock, 1))
    pause = PausePoint("takeover.before_epoch_write")
    monkeypatch.setattr(epoch_module, "afault_point", pause)
    takeover = asyncio.create_task(claim_epoch_and_sweep(engine, claimant(worker_lock, 2)))
    # B holds the worker_heartbeat row; its epoch is not written yet.
    await asyncio.wait_for(pause.reached.wait(), 5)
    entered = asyncio.Event()
    zombie = asyncio.create_task(runs.execute_run(engine, open_gate_graph(entered), rid, 1, epoch=e1))
    try:
        await await_until(lambda: blocked_or_done(migrated, zombie), 5)
        waited = not zombie.done()  # A's admission waits for the takeover ...
    finally:
        pause.resume()
    e2, _ = await asyncio.wait_for(takeover, 5)
    # ... and then finds epoch e2: it is fenced and writes nothing.
    assert (await asyncio.wait_for(zombie, 5), waited) == ("foreign_epoch", True)
    assert not entered.is_set() and await attempts(engine, rid) == []
    async with engine.begin() as c:
        assert (await runs.start_attempt(c, rid, 1, epoch=e2))[0] == "started"


async def test_a_superseded_epochs_stale_runs_reconciler_takes_nothing(
    engine, queue_app, worker_lock
) -> None:
    rid = await queued(engine)
    e1, _ = await claim_epoch_and_sweep(engine, claimant(worker_lock, 1))
    e2, _ = await claim_epoch_and_sweep(engine, claimant(worker_lock, 2))
    async with engine.begin() as c:
        assert (await runs.start_attempt(c, rid, 1, epoch=e2))[0] == "started"
    await age_heartbeat(engine, rid)

    def ctx(epoch: int) -> ReconcileContext:
        return ReconcileContext(engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow(), epoch=epoch)

    # A zombie of epoch e1 neither stamps its epoch on a new attempt nor ends B's.
    assert await runs.reconcile_stale_runs(ctx(e1)) == 0
    assert await attempts(engine, rid) == [(1, e2, None)]
    async with engine.begin() as c:
        with pytest.raises(FencedOut):
            await runs.begin_new_attempt(c, rid, epoch=e1, end_reason="heartbeat_stale", defer=defer_in_tx)
    assert await attempts(engine, rid) == [(1, e2, None)]
    # The current epoch's reconciler does take it.
    assert await runs.reconcile_stale_runs(ctx(e2)) == 1
    assert await attempts(engine, rid) == [(1, e2, "heartbeat_stale"), (2, e2, None)]


# --- Final fix wave, item 5: C17's zombie write is refused (in one process, so the write surely happens) ---

RUN_END = sa.text("SELECT status, attempt_no, finished_at, report, status_reason FROM runs WHERE id=:i")


async def run_end(engine, rid: int) -> tuple[object, ...]:
    async with engine.connect() as c:
        return tuple((await c.execute(RUN_END, {"i": rid})).one())


async def test_a_zombie_execution_that_ends_after_a_takeover_is_fenced_at_its_finalize(
    engine, queue_app, worker_lock, log_events
) -> None:
    rid, gate, entered = await queued(engine), asyncio.Event(), asyncio.Event()
    e1, _ = await claim_epoch_and_sweep(engine, claimant(worker_lock, 1))
    zombie = asyncio.create_task(
        runs.execute_run(engine, gated_graph(gate, entered), rid, 1, epoch=e1, heartbeat_period_s=3600)
    )
    await asyncio.wait_for(entered.wait(), 5)  # the zombie's graph is mid-run, as C17's paused worker is
    e2, counts = await claim_epoch_and_sweep(engine, claimant(worker_lock, 2))
    assert counts["runs"] == 1
    # The replacement runs attempt 2 to its end. Its DB writes, not `execute_run`: in one process that would
    # first cancel the zombie's local execution, which a replacement worker cannot do.
    async with engine.begin() as c:
        admission, owner = await runs.start_attempt(c, rid, 2, epoch=e2)
        assert admission == "resumed" and owner is not None
        await runs.finalize_run(c, owner, status="succeeded", reason=None, report={"by": "replacement"})
    replaced = await run_end(engine, rid)
    gate.set()  # the zombie's graph returns, and the zombie tries to finalize attempt 1
    assert await asyncio.wait_for(zombie, 5) == "started"
    fenced = [(e["run_id"], e["attempt_no"], e["write"]) for e in log_events() if e["event"] == "run_fenced"]
    assert fenced == [(rid, 1, "finalize")]  # the zombie's write was attempted and refused ...
    assert await run_end(engine, rid) == replaced  # ... so the run keeps the replacement's end and report
    assert await attempts(engine, rid) == [(1, e1, "worker_takeover"), (2, e2, "completed")]


# --- Final fix wave, item 9: a reason from the queue does not survive into running and succeeded ----------


async def test_a_redeferred_run_that_then_runs_keeps_no_stale_reason(engine, queue_app, migrated) -> None:
    rid = await queued(engine)
    async with engine.begin() as c:
        await c.execute(sa.text("DELETE FROM procrastinate_jobs"))  # its job was lost
    ctx = ReconcileContext(
        engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow() + timedelta(minutes=2.1), epoch=1
    )
    await run_reconcilers(ctx, "tick")
    assert (await fetch(engine, rid))[0::5] == ("queued", runs.REDEFERRED)
    async with engine.begin() as c:
        admission, owner = await runs.start_attempt(c, rid, 1, epoch=1)
    assert admission == "started" and owner is not None
    assert (await fetch(engine, rid))[0::5] == ("running", None)  # cleared by queued -> running
    async with open_saver(migrated.dsn("app")) as saver:
        assert await runs.execute_run(engine, build_noop_collect_graph(saver), rid, 1, epoch=1) == "resumed"
    assert (await fetch(engine, rid))[0::5] == ("succeeded", None)


# --- Task 24: carry 4 (the run heartbeat covers a long node) and Task 23's review addenda 9 and 11 ---------


def one_node_graph(node: Any) -> Any:
    """A graph (no checkpointer) of the one node `work`."""
    g = StateGraph(CollectState)
    g.add_node("work", node)
    g.add_edge(START, "work")
    g.add_edge("work", END)
    return g.compile()


async def heartbeat_at(engine, rid: int):
    return await scalar(engine, "SELECT heartbeat_at FROM runs WHERE id=:i", i=rid)


async def test_the_run_heartbeat_beats_while_one_long_node_runs(engine, queue_app) -> None:
    """Carry 4: the heartbeat is a task of its own beside the graph (`run_owned`), so one node that runs for
    many periods (the fetch fan-out, `embed`) keeps the run's `heartbeat_at` fresh; no node beats itself."""
    rid, beats = await queued(engine), list[Any]()

    async def long_node(state: CollectState) -> CollectUpdate:
        admitted = await heartbeat_at(engine, rid)

        async def three_more_beats() -> bool:
            latest = await heartbeat_at(engine, rid)
            if latest > (beats[-1] if beats else admitted):
                beats.append(latest)
            return len(beats) >= 3

        await await_until(three_more_beats, 10, step_s=0.02)  # all within this one node
        return {"steps": ["work"]}

    graph = one_node_graph(long_node)
    assert await runs.execute_run(engine, graph, rid, 1, epoch=1, heartbeat_period_s=0.05) == "started"
    assert len(beats) == 3 and beats == sorted(beats)
    assert (await fetch(engine, rid))[:2] == ("succeeded", 1)


def ending_node(engine, *, then: str, server=None, observes: bool = True):
    """A node that ends the run itself, as the graph's `finalize` does (one owner-fenced transaction, the end
    observed after its commit through `runs.observe_graph_end`), and then raises (`then="raise"`) or, once a
    heartbeat waits for its transaction on `server`, commits and waits until it is cancelled (`then="wait"`:
    the refused beat cancels it). With `observes=False` it never observes the end, as when the cancel lands
    while the node still awaits its commit's reply (addendum 11)."""

    async def node(state: CollectState) -> CollectUpdate:
        owner = runs.RunOwner(state["run_id"], state["attempt_no"])
        async with engine.begin() as c:
            duration = await runs.finalize_run(
                c, owner, status="succeeded", reason=None, report={"by": "graph"}
            )
            if then == "wait":
                # The next beat waits for this transaction's lock on the run row ...
                async def a_beat_waits() -> bool:
                    return lock_waits(server) > 0

                await await_until(a_beat_waits, 5, step_s=0.01)
        if observes:
            runs.observe_graph_end(owner, runs.RunEnd(status="succeeded", duration_s=duration))
        if then == "raise":
            raise RuntimeError("the last checkpoint write failed")
        await asyncio.get_running_loop().create_future()  # ... and after the commit is refused: cancelled
        return {"steps": ["work"]}

    return node


async def run_failed_alerts(engine) -> int:
    return int(await scalar(engine, "SELECT count(*) FROM alerts WHERE code = 'run.failed'"))


async def test_an_error_after_the_graph_ended_its_run_writes_nothing_more(
    engine, queue_app, log_events
) -> None:
    """Addendum 9: `raised` looks the attempt's end up first; a graph that committed its finalize before the
    error (here, as if writing the last checkpoint failed) is neither `run_graph_failed` nor finalized
    again."""
    rid = await queued(engine)
    succeeded, failed = runs_total("succeeded"), runs_total("failed")
    graph = one_node_graph(ending_node(engine, then="raise"))
    assert await runs.execute_run(engine, graph, rid, 1, epoch=1) == "started"
    row = await fetch(engine, rid)
    assert (row[0], row[4]) == ("succeeded", {"by": "graph"})
    assert (runs_total("succeeded"), runs_total("failed")) == (succeeded + 1, failed)  # counted once
    events = log_events()
    assert not [e for e in events if e["event"] in ("run_graph_failed", "run_fenced", "run_finished")]
    [event] = [e for e in events if e["event"] == "run_graph_failed_after_end"]
    assert (event["run_id"], event["status"], event["error_class"]) == (rid, "succeeded", "RuntimeError")
    assert await run_failed_alerts(engine) == 0
    assert await attempts(engine, rid) == [(1, 1, "completed")]


async def test_a_heartbeat_blocked_by_the_finalize_loses_the_attempt_and_writes_nothing(
    engine, queue_app, migrated, log_events
) -> None:
    """Addendum 11: a beat that fires during the finalize transaction waits for its lock and, after the
    commit, finds the run ended: the beat is refused, the work is cancelled and the attempt is `lost`
    (`execution_lost`). That is harmless: `lost` writes nothing, so the run is never finalized again and its
    end is counted once, by the node that ended it."""
    rid = await queued(engine)
    succeeded, timed = runs_total("succeeded"), timed_runs()
    graph = one_node_graph(ending_node(engine, then="wait", server=migrated))
    assert await runs.execute_run(engine, graph, rid, 1, epoch=1, heartbeat_period_s=0.05) == "started"
    row = await fetch(engine, rid)
    assert (row[0], row[4]) == ("succeeded", {"by": "graph"})
    assert (runs_total("succeeded"), timed_runs()) == (succeeded + 1, timed + 1)
    events = log_events()
    assert [e["key"] for e in events if e["event"] == "execution_lost"] == [f"run:{rid}"]
    assert not [e for e in events if e["event"] in ("run_graph_failed", "run_fenced", "run_finished")]
    assert await run_failed_alerts(engine) == 0
    assert await attempts(engine, rid) == [(1, 1, "completed")]


@pytest.mark.parametrize("then", ["wait", "raise"])
async def test_an_end_the_graph_committed_but_never_observed_is_counted_once_by_execute_run(
    engine, queue_app, migrated, log_events, then: str
) -> None:
    """Addendum 11 (ledger L383): the refused beat's cancel, or an error, lands after the node's `finalize`
    committed but before the node observed the end. `execute_run` finds the attempt's end and counts it once
    (`RUNS`, `RUN_DURATION`) and logs `run_finished`; nothing is finalized again."""
    rid = await queued(engine)
    succeeded, timed = runs_total("succeeded"), timed_runs()
    graph = one_node_graph(ending_node(engine, then=then, server=migrated, observes=False))
    assert await runs.execute_run(engine, graph, rid, 1, epoch=1, heartbeat_period_s=0.05) == "started"
    row = await fetch(engine, rid)
    assert (row[0], row[4]) == ("succeeded", {"by": "graph"})
    assert (runs_total("succeeded"), timed_runs()) == (succeeded + 1, timed + 1)
    [finished] = [e for e in log_events() if e["event"] == "run_finished"]
    assert (finished["run_id"], finished["status"], finished["observed_by"]) == (
        rid,
        "succeeded",
        "execute_run",
    )
    assert await run_failed_alerts(engine) == 0
    assert await attempts(engine, rid) == [(1, 1, "completed")]


# --- Ruling FR-h: the M0 run reconcilers are fenced by the worker epoch -------------------------------------


async def test_the_queued_runs_and_stale_data_reconcilers_of_a_superseded_epoch_write_nothing(
    engine, queue_app
) -> None:
    from jobhunter.worker import reconcilers

    rid = await queued(engine)
    async with engine.connect() as c:
        seeded = (await c.execute(sa.text("SELECT min(created_at) FROM settings_versions"))).scalar_one()
    async with engine.begin() as c:
        await c.execute(sa.text("DELETE FROM procrastinate_jobs"))  # its job is lost: due for a re-defer
        await c.execute(sa.text("UPDATE worker_heartbeat SET worker_epoch = worker_epoch + 1"))  # a takeover
    late = max(utcnow() + timedelta(minutes=5), seeded + timedelta(hours=13))  # both due
    ctx = ReconcileContext(engine=engine, queue=queue_app, defer=defer_in_tx, now=late, epoch=1)
    for name in ("queued_runs", "stale_data"):
        with pytest.raises(FencedOut):
            await reconcilers.REGISTRY[name][1](ctx)
    assert (await fetch(engine, rid))[0::5] == ("queued", None)  # not re-deferred, not annotated
    async with engine.connect() as c:
        assert not await live_job_exists(c, "run_collect", args_match={"run_id": rid})
        assert (await c.execute(sa.text("SELECT count(*) FROM alerts"))).scalar_one() == 0
    # the current epoch's pass does both
    current = ReconcileContext(engine=engine, queue=queue_app, defer=defer_in_tx, now=late, epoch=2)
    assert await reconcilers.REGISTRY["queued_runs"][1](current) == 1
    assert await reconcilers.REGISTRY["stale_data"][1](current) == 1
