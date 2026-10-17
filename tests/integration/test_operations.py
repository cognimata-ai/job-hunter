import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.errors import FencedOut
from jobhunter.worker import operations as ops
from jobhunter.worker.epoch import WorkerIdentity, claim_epoch_and_sweep
from jobhunter.worker.executions import EXECUTIONS
from jobhunter.worker.reconcilers import ReconcileContext, run_reconcilers
from jobhunter.worker.tasks import defer_in_tx, live_job_exists

from tests.support.procs import await_until

pytestmark = pytest.mark.integration
ROW = sa.text(
    "SELECT status, claim_id, worker_epoch, heartbeat_at, attempts, last_error_code "
    "FROM operations WHERE id=:i"
)


async def new_op(engine, defer=defer_in_tx, kind: ops.OperationKind = "export") -> tuple[int, bool]:
    async with engine.begin() as c:
        return await ops.create_operation(
            c, kind=kind, payload={"export_id": 1, "note": "ü"}, dedup_key=f"{kind}:1", defer=defer
        )


async def claim(engine, op: int):
    async with engine.begin() as c:
        return await ops.claim_operation(c, op, epoch=1)


async def age(engine, op: int, minutes: int) -> None:
    async with engine.begin() as c:
        await c.execute(
            sa.text("UPDATE operations SET heartbeat_at = now() - make_interval(mins => :m) WHERE id=:i"),
            {"m": minutes, "i": op},
        )


async def row(engine, op: int):
    async with engine.connect() as c:
        return tuple((await c.execute(ROW, {"i": op})).one())


def _logged(caplog, event: str) -> list[dict[str, Any]]:
    return [rec.msg for rec in caplog.records if isinstance(rec.msg, dict) and rec.msg.get("event") == event]


async def test_create_dedups_and_defers_only_new_rows(engine, queue_app) -> None:
    (a, created_a), (b, created_b) = await new_op(engine), await new_op(engine)
    assert a == b and (created_a, created_b) == (True, False)
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM procrastinate_jobs"))).scalar_one() == 1
        assert (
            await c.execute(sa.text("SELECT payload FROM operations WHERE id=:i"), {"i": a})
        ).scalar_one() == {"export_id": 1, "note": "ü"}


async def test_completion_failure_and_exhaustion_clear_ownership(engine, queue_app) -> None:
    op, _ = await new_op(engine)
    c1 = await claim(engine, op)
    async with engine.begin() as c:
        await ops.finish_operation(c, c1, status="succeeded")
    assert (await row(engine, op))[:4] == ("succeeded", None, None, None)
    op2, _ = await new_op(engine, kind="purge")
    c2 = await claim(engine, op2)
    async with engine.begin() as c:
        await ops.finish_operation(c, c2, status="failed", error_code="bad_input")
    assert (await row(engine, op2))[:4] == ("failed", None, None, None)
    op3, _ = await new_op(engine, kind="eval_run")
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE operations SET attempts = 9 WHERE id=:i"), {"i": op3})
    c3 = await claim(engine, op3)
    async with engine.begin() as c:
        assert await ops.requeue_operation(c, c3, error_code="transient", not_before=utcnow()) == "failed"
    assert await row(engine, op3) == ("failed", None, None, None, 10, "attempts_exhausted")
    async with engine.connect() as c:
        assert (
            await c.execute(sa.text("SELECT severity FROM alerts WHERE code='operation.failed'"))
        ).scalar_one() == "warn"


async def test_operation_aba_old_execution_is_fenced(engine, queue_app) -> None:
    # §18.2 Operation ABA, C35 (M0 part)
    op, _ = await new_op(engine)
    a = await claim(engine, op)
    await age(engine, op, 4)
    async with engine.begin() as c:
        assert await ops.reclaim_stale_operations(c, now=utcnow()) == [op]
    b = await claim(engine, op)
    assert b is not None and a is not None and b.claim_id != a.claim_id
    async with engine.begin() as c:
        assert await ops.heartbeat_operation(c, a) is False
        with pytest.raises(FencedOut):
            await ops.lock_operation_owner(c, a)
    for call in (
        lambda c: ops.finish_operation(c, a, status="succeeded"),
        lambda c: ops.requeue_operation(c, a, error_code="x", not_before=utcnow()),
    ):
        async with engine.begin() as c:
            with pytest.raises(FencedOut):
                await call(c)
    async with engine.begin() as c:
        await ops.lock_operation_owner(c, b)
        await ops.finish_operation(c, b, status="succeeded")
    assert (await row(engine, op))[0::4] == ("succeeded", 1)


async def test_takeover_sweep_returns_older_epoch_operations(engine, queue_app, worker_lock) -> None:
    op, _ = await new_op(engine)
    e1, _ = await claim_epoch_and_sweep(
        engine, WorkerIdentity(uuid.uuid4(), "h", 1, worker_lock.backend_pid, "v")
    )
    async with engine.begin() as c:
        assert await ops.claim_operation(c, op, epoch=e1)
    e2, counts = await claim_epoch_and_sweep(
        engine, WorkerIdentity(uuid.uuid4(), "h", 2, worker_lock.backend_pid, "v")
    )
    assert (
        e2 == e1 + 1
        and counts["operations"] == 1
        and (await row(engine, op))[:4] == ("pending", None, None, None)
    )


async def test_c30_reconciler_redefers_lost_job_after_two_minutes(engine, queue_app) -> None:
    op, _ = await new_op(engine, defer=None)

    def ctx(dt: timedelta) -> ReconcileContext:
        return ReconcileContext(engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow() + dt, epoch=1)

    await run_reconcilers(ctx(timedelta(minutes=1)), "tick")
    async with engine.connect() as c:
        assert not await live_job_exists(c, "run_operation", args_match={"operation_id": op})
    await run_reconcilers(ctx(timedelta(minutes=2, seconds=1)), "tick")
    async with engine.connect() as c:
        assert await live_job_exists(c, "run_operation", args_match={"operation_id": op})


async def test_lost_ownership_stops_execution_without_owner_writes(engine, queue_app, monkeypatch) -> None:
    handler_started, cancelled = asyncio.Event(), asyncio.Event()

    async def handler(ctx: ops.OperationContext) -> None:
        handler_started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setitem(ops.HANDLERS, "export", handler)
    op, _ = await new_op(engine)
    run = asyncio.create_task(ops.execute_operation(engine, op, epoch=1, heartbeat_period_s=0.2))
    await handler_started.wait()

    async def reclaimed() -> bool:  # injected clock 4 min ahead: a live heartbeat cannot un-age the row
        async with engine.begin() as c:
            return await ops.reclaim_stale_operations(c, now=utcnow() + timedelta(minutes=4)) == [op]

    await await_until(reclaimed, 5)  # retried only while SKIP LOCKED meets an in-flight heartbeat
    await asyncio.wait_for(run, 5)
    assert cancelled.is_set() and EXECUTIONS.get(("operation", op)) is None
    assert await row(engine, op) == ("pending", None, None, None, 1, None)
    assert not [t for t in asyncio.all_tasks() if t.get_name() == f"heartbeat:operation:{op}"]


async def test_unknown_kind_fails_with_no_handler_and_alert(engine, queue_app, monkeypatch) -> None:
    monkeypatch.delitem(ops.HANDLERS, "purge", raising=False)
    op, _ = await new_op(engine, kind="purge")
    await ops.execute_operation(engine, op, epoch=1)
    assert (await row(engine, op))[0::5] == ("failed", "no_handler")
    async with engine.connect() as c:  # the persisted, deduplicated alert
        assert (
            await c.execute(
                sa.text(
                    "SELECT severity FROM alerts WHERE code='operation.failed' AND dedup_key=:k "
                    "AND resolved_at IS NULL"
                ),
                {"k": f"operation.failed:operation:{op}"},
            )
        ).scalar_one() == "warn"


# Beyond the brief: the other execute_operation outcomes, the reconciler's local cancel, and cancellation.


async def test_execute_operation_finishes_a_returning_handler(engine, queue_app, monkeypatch) -> None:
    seen: list[tuple[str, dict[str, Any], bool]] = []

    async def handler(ctx: ops.OperationContext) -> None:
        async with ctx.engine.begin() as c:
            await ops.lock_operation_owner(c, ctx.claim)  # the handler owns the row while it runs
        seen.append((ctx.claim.kind, dict(ctx.claim.payload), EXECUTIONS.get(("operation", op)) is not None))

    monkeypatch.setitem(ops.HANDLERS, "export", handler)
    op, _ = await new_op(engine)
    await ops.execute_operation(engine, op, epoch=1)
    assert seen == [("export", {"export_id": 1, "note": "ü"}, True)]
    assert await row(engine, op) == ("succeeded", None, None, None, 0, None)
    assert EXECUTIONS.get(("operation", op)) is None
    await ops.execute_operation(engine, op, epoch=1)  # a second delivery finds nothing to claim
    assert len(seen) == 1


async def test_unexpected_handler_error_requeues_as_bug_with_a_delay(
    engine, queue_app, monkeypatch, caplog
) -> None:
    async def handler(ctx: ops.OperationContext) -> None:
        raise RuntimeError("payload detail that must not be logged")

    monkeypatch.setitem(ops.HANDLERS, "purge", handler)
    op, _ = await new_op(engine, kind="purge")
    before = utcnow()
    await ops.execute_operation(engine, op, epoch=1)
    assert await row(engine, op) == ("pending", None, None, None, 1, "bug")
    async with engine.connect() as c:
        not_before = (
            await c.execute(sa.text("SELECT not_before FROM operations WHERE id=:i"), {"i": op})
        ).scalar_one()
    assert not_before >= before + timedelta(minutes=1)
    assert await claim(engine, op) is None  # not claimable before its not_before
    [logged] = _logged(caplog, "operation_handler_failed")
    assert (logged["operation_id"], logged["error_class"]) == (op, "RuntimeError")
    assert "must not be logged" not in caplog.text


async def test_reconciler_reclaims_a_stale_operation_and_cancels_its_local_execution(
    engine, queue_app, monkeypatch
) -> None:
    handler_started, cancelled = asyncio.Event(), asyncio.Event()

    async def handler(ctx: ops.OperationContext) -> None:
        handler_started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setitem(ops.HANDLERS, "export", handler)
    op, _ = await new_op(engine)
    # The heartbeat never fires: only the reconciler can stop this execution.
    run = asyncio.create_task(ops.execute_operation(engine, op, epoch=1, heartbeat_period_s=3600))
    await handler_started.wait()
    await age(engine, op, 4)
    ctx = ReconcileContext(engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow(), epoch=1)
    counts = await run_reconcilers(ctx, "tick")
    assert counts["operations"] == 1
    assert cancelled.is_set()  # the handler was cancelled and awaited inside the reconciler
    await asyncio.wait_for(run, 5)
    assert EXECUTIONS.get(("operation", op)) is None
    assert await row(engine, op) == ("pending", None, None, None, 1, None)


async def test_execution_already_live_locally_requeues_the_new_claim(engine, queue_app, monkeypatch) -> None:
    ran: list[int] = []

    async def handler(ctx: ops.OperationContext) -> None:
        ran.append(ctx.claim.operation_id)

    monkeypatch.setitem(ops.HANDLERS, "export", handler)
    op, _ = await new_op(engine)
    gate = asyncio.Event()

    async def old_execution() -> None:  # an earlier execution of this operation that has not stopped yet
        await gate.wait()

    old = EXECUTIONS.start(("operation", op), "old-claim", old_execution())
    try:
        await ops.execute_operation(engine, op, epoch=1)
    finally:
        gate.set()
        await old
    assert ran == []
    assert await row(engine, op) == ("pending", None, None, None, 1, "already_executing")


async def test_cancel_operation_fences_the_running_claim(engine, queue_app) -> None:
    op, _ = await new_op(engine)
    a = await claim(engine, op)
    async with engine.begin() as c:
        assert await ops.cancel_operation(c, op, reason="superseded") is True
        assert await ops.cancel_operation(c, op, reason="superseded") is False  # already final
    assert await row(engine, op) == ("cancelled", None, None, None, 0, "superseded")
    async with engine.begin() as c:
        assert await ops.heartbeat_operation(c, a) is False
    async with engine.begin() as c:
        with pytest.raises(FencedOut):
            await ops.finish_operation(c, a, status="succeeded")
    op2, _ = await new_op(engine, kind="purge")
    async with engine.begin() as c:
        assert await ops.cancel_operation(c, op2, reason="delete_everything") is True  # pending -> cancelled
    assert (await row(engine, op2))[0::5] == ("cancelled", "delete_everything")
    assert (await new_op(engine, kind="purge"))[1] is True  # a cancelled row no longer holds the dedup key
