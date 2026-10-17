import asyncio
import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import cast

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.errors import FencedOut
from jobhunter.graphs.checkpointer import open_saver
from jobhunter.graphs.collect.graph import CollectState, CollectUpdate, build_noop_collect_graph
from jobhunter.worker import runs
from jobhunter.worker.epoch import WorkerIdentity, claim_epoch_and_sweep
from jobhunter.worker.executions import EXECUTIONS
from jobhunter.worker.reconcilers import ReconcileContext, run_reconcilers
from jobhunter.worker.tasks import defer_in_tx, live_job_exists, run_collect
from langgraph.graph import END, START, StateGraph
from procrastinate.job_context import JobContext

pytestmark = pytest.mark.integration


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
    await entered.wait()
    assert await runs.execute_run(engine, gated_graph(gate, entered), rid, 1, epoch=1) == "duplicate"
    gate.set()
    assert await first == "started"
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
    await entered.wait()
    async with engine.begin() as c:
        await c.execute(
            sa.text("UPDATE runs SET heartbeat_at = now() - interval '4 minutes' WHERE id=:i"), {"i": rid}
        )
    await run_reconcilers(
        ReconcileContext(engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow(), epoch=1), "tick"
    )
    await asyncio.wait_for(old, 5)
    assert EXECUTIONS.get(("run", rid)) is None and (await fetch(engine, rid))[:2] == ("running", 2)
    async with open_saver(migrated.dsn("app")) as saver:
        assert await runs.execute_run(engine, build_noop_collect_graph(saver), rid, 2, epoch=1) == "resumed"
    assert (await fetch(engine, rid))[:2] == ("succeeded", 2)


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


async def test_queued_runs_reconciler_redefers_once_then_fails(engine, queue_app) -> None:
    rid = await queued(engine)

    def ctx(m: float) -> ReconcileContext:
        return ReconcileContext(
            engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow() + timedelta(minutes=m), epoch=1
        )

    async with engine.begin() as c:
        await c.execute(sa.text("DELETE FROM procrastinate_jobs"))
    await run_reconcilers(ctx(2.1), "tick")
    assert (await fetch(engine, rid))[5] == "redeferred"
    async with engine.begin() as c:
        await c.execute(sa.text("DELETE FROM procrastinate_jobs"))
    await run_reconcilers(ctx(4.2), "tick")
    assert (await fetch(engine, rid))[0::5] == ("failed", "orphaned_queue")


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


async def test_run_collect_body_executes_the_run_with_the_runtime(monkeypatch) -> None:
    calls: list[tuple[object, object, int, int, int]] = []

    async def fake_execute_run(
        engine: object, graph: object, run_id: int, attempt_no: int, *, epoch: int
    ) -> str:
        calls.append((engine, graph, run_id, attempt_no, epoch))
        return "started"

    monkeypatch.setattr(runs, "execute_run", fake_execute_run)
    engine, graph = object(), object()
    runtime = SimpleNamespace(
        engine=engine, collect_graph=graph, epoch=7
    )  # the `WorkerRuntime` fields it reads
    context = cast(JobContext, SimpleNamespace(additional_context={"runtime": runtime}))
    await run_collect(context, run_id=5, attempt_no=2)
    assert calls == [(engine, graph, 5, 2, 7)]
