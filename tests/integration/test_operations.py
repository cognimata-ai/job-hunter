import asyncio
import uuid
from datetime import timedelta
from typing import Any, cast

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.sql import LOCK_NOT_AVAILABLE, pg_error
from jobhunter.errors import FencedOut
from jobhunter.worker import epoch as epoch_module
from jobhunter.worker import operations as ops
from jobhunter.worker.epoch import WorkerIdentity, claim_epoch_and_sweep
from jobhunter.worker.executions import EXECUTIONS
from jobhunter.worker.reconcilers import ReconcileContext, run_reconcilers
from jobhunter.worker.tasks import defer_in_tx, live_job_exists
from sqlalchemy.ext.asyncio import AsyncConnection

from tests.support.faults import PausePoint
from tests.support.logs import logged
from tests.support.pg import blocked_or_done
from tests.support.procs import await_until

# Every test here admits or claims as `epoch=1` (or a later claimed epoch): epoch 1 must be the current one.
pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("current_epoch")]
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


async def failed_alerts(engine) -> list[tuple[str, str, int]]:
    """`(dedup_key, severity, occurrences)` of every `operation.failed` alert, oldest first."""
    async with engine.connect() as c:
        rows = await c.execute(
            sa.text(
                "SELECT dedup_key, severity, occurrences FROM alerts "
                "WHERE code='operation.failed' ORDER BY id"
            )
        )
        return [(r.dedup_key, r.severity, r.occurrences) for r in rows]


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
    # Ruling T10-g: the direct `failed` finish alerts too, so each permanent failure has exactly one alert.
    assert await failed_alerts(engine) == [
        (f"operation.failed:operation:{op2}", "warn", 1),
        (f"operation.failed:operation:{op3}", "warn", 1),
    ]


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


# --- Final fix wave, item 1: the takeover waits for an old owner's row lock, it never skips the row ---------


async def _owner_holds_the_row(conn: AsyncConnection, claim_: ops.OperationClaim, how: str) -> None:
    """What an old owner's in-flight transaction holds on its row: `FOR SHARE` or a heartbeat's update."""
    if how == "owner_lock":
        await ops.lock_operation_owner(conn, claim_)
    else:
        assert await ops.heartbeat_operation(conn, claim_)


@pytest.mark.parametrize("how", ["owner_lock", "heartbeat"])
async def test_the_takeover_waits_for_an_older_owners_row_lock_and_then_fences_it(
    engine, queue_app, worker_lock, migrated, how
) -> None:
    op, _ = await new_op(engine)
    e1, _ = await claim_epoch_and_sweep(
        engine, WorkerIdentity(uuid.uuid4(), "h", 1, worker_lock.backend_pid, "v")
    )
    async with engine.begin() as c:
        old = await ops.claim_operation(c, op, epoch=e1)
    assert old is not None
    async with engine.connect() as holder:
        tx = await holder.begin()
        await _owner_holds_the_row(holder, old, how)  # the old owner is mid-transaction on its row
        takeover = asyncio.create_task(
            claim_epoch_and_sweep(engine, WorkerIdentity(uuid.uuid4(), "h", 2, worker_lock.backend_pid, "v"))
        )
        try:
            await await_until(lambda: blocked_or_done(migrated, takeover), 5)
            waited = not takeover.done()  # it waits for the row instead of committing without it
        finally:
            await tx.commit()
    e2, counts = await asyncio.wait_for(takeover, 5)
    assert (waited, e2, counts["operations"]) == (True, e1 + 1, 1)
    assert (await row(engine, op))[:5] == ("pending", None, None, None, 1)
    async with engine.begin() as c:  # the old claim is fenced: its heartbeat and finalization write nothing
        assert await ops.heartbeat_operation(c, old) is False
    async with engine.begin() as c:
        with pytest.raises(FencedOut):
            await ops.finish_operation(c, old, status="succeeded")
    assert (await row(engine, op))[:5] == ("pending", None, None, None, 1)


async def test_an_older_owners_row_lock_held_past_the_lock_timeout_fails_the_takeover_with_55p03(
    engine, queue_app, worker_lock
) -> None:
    op, _ = await new_op(engine)
    e1, _ = await claim_epoch_and_sweep(
        engine, WorkerIdentity(uuid.uuid4(), "h", 1, worker_lock.backend_pid, "v")
    )
    async with engine.begin() as c:
        old = await ops.claim_operation(c, op, epoch=e1)
    assert old is not None
    before = await row(engine, op)
    async with engine.connect() as holder:
        tx = await holder.begin()
        await ops.lock_operation_owner(holder, old)  # held until the claim gave up
        try:
            with pytest.raises(sa.exc.OperationalError) as blocked:
                await claim_epoch_and_sweep(
                    engine,
                    WorkerIdentity(uuid.uuid4(), "h", 2, worker_lock.backend_pid, "v"),
                    lock_timeout_ms=200,
                )
        finally:
            await tx.rollback()
    assert pg_error(blocked.value).sqlstate == LOCK_NOT_AVAILABLE
    # Nothing was committed: the epoch is still e1 and the operation still belongs to the old claim.
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT worker_epoch FROM worker_heartbeat"))).scalar_one() == e1
    assert await row(engine, op) == before and before[:3] == ("running", old.claim_id, e1)


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
    assert await failed_alerts(engine) == [(f"operation.failed:operation:{op}", "warn", 1)]  # raised once


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
    [event] = logged(caplog, "operation_handler_failed")
    assert (event["operation_id"], event["error_class"]) == (op, "RuntimeError")
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


# Fix round 1.


async def _swallowing_handler(
    claims: list[ops.OperationClaim], started: asyncio.Event, cancelled: asyncio.Event
):
    async def handler(ctx: ops.OperationContext) -> None:
        claims.append(ctx.claim)
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()  # swallowed: the handler returns as if it had finished

    return handler


async def assert_still_owned_by(engine, op: int, claim_: ops.OperationClaim) -> None:
    status, claim_id, epoch, heartbeat_at, attempts, error = await row(engine, op)
    assert (status, claim_id, epoch, attempts, error) == ("running", claim_.claim_id, 1, 0, None)
    assert heartbeat_at is not None


async def test_swallowed_cancel_after_a_failing_beat_writes_nothing(
    engine, queue_app, monkeypatch, caplog
) -> None:
    claims: list[ops.OperationClaim] = []
    started, cancelled = asyncio.Event(), asyncio.Event()
    monkeypatch.setitem(ops.HANDLERS, "export", await _swallowing_handler(claims, started, cancelled))

    async def failing_beat(conn, claim_: ops.OperationClaim) -> bool:
        await started.wait()
        raise ConnectionError("server closed the connection")

    monkeypatch.setattr(ops, "heartbeat_operation", failing_beat)
    op, _ = await new_op(engine)
    await asyncio.wait_for(ops.execute_operation(engine, op, epoch=1, heartbeat_period_s=0), 5)
    assert cancelled.is_set() and [m["error_class"] for m in logged(caplog, "heartbeat_failed")] == [
        "ConnectionError"
    ]
    # Lost, so no owner write: not `succeeded`. The stale reclaim takes it back later (attempts + 1).
    await assert_still_owned_by(engine, op, claims[0])
    assert EXECUTIONS.get(("operation", op)) is None


async def test_swallowed_cancel_from_cancel_all_writes_nothing(engine, queue_app, monkeypatch) -> None:
    claims: list[ops.OperationClaim] = []
    started, cancelled = asyncio.Event(), asyncio.Event()
    monkeypatch.setitem(ops.HANDLERS, "export", await _swallowing_handler(claims, started, cancelled))
    op, _ = await new_op(engine)
    run = asyncio.create_task(ops.execute_operation(engine, op, epoch=1, heartbeat_period_s=3600))
    await started.wait()
    assert await EXECUTIONS.cancel_all() == 1  # the shutdown's teardown
    await asyncio.wait_for(run, 5)
    assert cancelled.is_set()
    await assert_still_owned_by(engine, op, claims[0])  # no state is rewritten on shutdown (§3.3.5)


async def test_reconciler_leaves_a_local_execution_of_another_claim_alone(engine, queue_app) -> None:
    op, _ = await new_op(engine)
    assert await claim(engine, op) is not None
    await age(engine, op, 4)
    gate = asyncio.Event()

    async def other_claims_execution() -> None:
        await gate.wait()

    other = EXECUTIONS.start(("operation", op), "a-different-claim", other_claims_execution())
    try:
        ctx = ReconcileContext(engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow(), epoch=1)
        assert (await run_reconcilers(ctx, "tick"))["operations"] == 1  # the stale claim was taken back
        assert EXECUTIONS.get(("operation", op)) == ("a-different-claim", other)  # but not this task
        assert not other.done()
    finally:
        gate.set()
        await other
    assert (await row(engine, op))[0::4] == ("pending", 1)


JOBS_FOR = sa.text(
    "SELECT count(*) FROM procrastinate_jobs WHERE args @> jsonb_build_object('operation_id', :i)"
)
LOCK_NOWAIT = sa.text("SELECT id FROM operations WHERE id=:i FOR UPDATE NOWAIT")


async def test_redefer_neither_locks_nor_redefers_a_row_with_a_live_job(engine, queue_app) -> None:
    with_job, _ = await new_op(engine)  # deferred at creation: its job is live
    lost, _ = await new_op(engine, defer=None, kind="purge")
    due = utcnow() + timedelta(minutes=2, seconds=1)
    async with engine.connect() as c1:
        tx = await c1.begin()
        assert await ops._redefer_lost_jobs(c1, now=due, defer=defer_in_tx) == 1
        async with engine.connect() as c2:  # while the re-defer transaction is still open
            async with c2.begin():
                await c2.execute(LOCK_NOWAIT, {"i": with_job})  # never a candidate, so never locked
            with pytest.raises(sa.exc.OperationalError) as locked:
                async with c2.begin():
                    await c2.execute(LOCK_NOWAIT, {"i": lost})
            assert pg_error(locked.value).sqlstate == LOCK_NOT_AVAILABLE
        await tx.commit()
    async with engine.connect() as c:
        assert [(await c.execute(JOBS_FOR, {"i": i})).scalar_one() for i in (with_job, lost)] == [1, 1]


async def test_redefer_is_bounded_per_tick(engine, queue_app) -> None:
    async with engine.begin() as c:
        await c.execute(
            sa.text(
                "INSERT INTO operations (kind, payload, dedup_key, status) "
                "SELECT 'export', '{}'::jsonb, 'bulk:' || g, 'pending' FROM generate_series(1, :n) g"
            ),
            {"n": ops.REDEFER_BATCH + 1},
        )
    due = utcnow() + timedelta(minutes=2, seconds=1)
    async with engine.begin() as c:
        assert await ops._redefer_lost_jobs(c, now=due, defer=defer_in_tx) == ops.REDEFER_BATCH
    async with engine.begin() as c:  # the next tick takes the rest
        assert await ops._redefer_lost_jobs(c, now=due, defer=defer_in_tx) == 1
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM procrastinate_jobs"))).scalar_one() == 101


class _EndsTheLiveRowBeforeItIsRead:
    """A connection whose live row ends between the conflicting insert and the read of its id.

    That is what a concurrent finish does in READ COMMITTED; here it happens deterministically, once.
    """

    def __init__(self, conn: AsyncConnection, live_id: int) -> None:
        self.conn, self.live_id, self.ended = conn, live_id, False

    async def execute(self, statement, *args, **kwargs):
        if statement is ops._LIVE_ID and not self.ended:
            self.ended = True
            assert await ops.cancel_operation(self.conn, self.live_id, reason="superseded")
        return await self.conn.execute(statement, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.conn, name)


async def test_create_retries_when_the_conflicting_live_row_ended_before_it_was_read(
    engine, queue_app
) -> None:
    live, _ = await new_op(engine, defer=None)
    async with engine.begin() as c:
        proxy = _EndsTheLiveRowBeforeItIsRead(c, live)
        new, created = await ops.create_operation(
            cast(AsyncConnection, proxy),
            kind="export",
            payload={"export_id": 1},
            dedup_key="export:1",
            defer=defer_in_tx,
        )
    assert proxy.ended and created and new != live
    assert [(await row(engine, i))[0] for i in (live, new)] == ["cancelled", "pending"]
    async with engine.connect() as c:
        assert await live_job_exists(c, "run_operation", args_match={"operation_id": new})


# --- Final fix wave, item 2: a claim is fenced by the current worker epoch (ruling T21-c) ------------------


def claimant(worker_lock, n: int) -> WorkerIdentity:
    return WorkerIdentity(uuid.uuid4(), "h", n, worker_lock.backend_pid, "v")


async def test_a_worker_paused_before_its_claim_is_fenced_once_another_epoch_took_over(
    engine, queue_app, worker_lock, monkeypatch, log_events
) -> None:
    ran: list[int] = []

    async def handler(ctx: ops.OperationContext) -> None:
        ran.append(ctx.claim.operation_id)

    monkeypatch.setitem(ops.HANDLERS, "export", handler)
    op, _ = await new_op(engine)
    e1, _ = await claim_epoch_and_sweep(engine, claimant(worker_lock, 1))
    pause = PausePoint("operation.before_claim")
    monkeypatch.setattr(ops, "afault_point", pause)
    zombie = asyncio.create_task(ops.execute_operation(engine, op, epoch=e1))
    await asyncio.wait_for(pause.reached.wait(), 5)  # worker A (epoch e1) is about to claim the operation ...
    e2, counts = await claim_epoch_and_sweep(engine, claimant(worker_lock, 2))  # ... when B takes over
    assert counts["operations"] == 0  # still pending: the sweep has nothing to take
    pause.resume()
    await asyncio.wait_for(zombie, 5)
    assert ran == [] and (await row(engine, op))[:5] == ("pending", None, None, None, 0)  # no claim with e1
    fenced = [(e["epoch"], e["current_epoch"]) for e in log_events() if e["event"] == "worker_epoch_fenced"]
    assert fenced == [(e1, e2)]
    async with engine.begin() as c:  # B's claim of the same operation succeeds
        b = await ops.claim_operation(c, op, epoch=e2)
    assert b is not None and (await row(engine, op))[:3] == ("running", b.claim_id, e2)


async def test_a_claim_in_flight_at_the_takeover_commits_first_and_is_swept(
    engine, queue_app, worker_lock, migrated
) -> None:
    op, _ = await new_op(engine)
    e1, _ = await claim_epoch_and_sweep(engine, claimant(worker_lock, 1))
    async with engine.connect() as a:
        tx = await a.begin()
        old = await ops.claim_operation(a, op, epoch=e1)  # A's claim, not committed yet
        takeover = asyncio.create_task(claim_epoch_and_sweep(engine, claimant(worker_lock, 2)))
        try:
            await await_until(lambda: blocked_or_done(migrated, takeover), 5)
            waited = not takeover.done()  # the takeover waits for the claim ...
        finally:
            await tx.commit()
    _, counts = await asyncio.wait_for(takeover, 5)
    # ... and then sweeps it: the operation is pending again and A's claim is fenced.
    assert (old is not None, waited, counts["operations"]) == (True, True, 1)
    assert (await row(engine, op))[:5] == ("pending", None, None, None, 1)
    async with engine.begin() as c:
        assert old is not None and await ops.heartbeat_operation(c, old) is False


async def test_a_claim_during_a_takeover_waits_for_it_and_is_fenced(
    engine, queue_app, worker_lock, migrated, monkeypatch
) -> None:
    op, _ = await new_op(engine)
    e1, _ = await claim_epoch_and_sweep(engine, claimant(worker_lock, 1))
    pause = PausePoint("takeover.before_epoch_write")
    monkeypatch.setattr(epoch_module, "afault_point", pause)
    takeover = asyncio.create_task(claim_epoch_and_sweep(engine, claimant(worker_lock, 2)))
    # B holds the worker_heartbeat row; its epoch is not written yet.
    await asyncio.wait_for(pause.reached.wait(), 5)

    async def zombie_claim() -> ops.OperationClaim | None:
        async with engine.begin() as c:
            return await ops.claim_operation(c, op, epoch=e1)

    zombie = asyncio.create_task(zombie_claim())
    try:
        await await_until(lambda: blocked_or_done(migrated, zombie), 5)
        waited = not zombie.done()  # A's claim waits for the takeover ...
    finally:
        pause.resume()
    _, counts = await asyncio.wait_for(takeover, 5)
    # ... and then finds the new epoch: no claim, nothing written.
    assert (await asyncio.wait_for(zombie, 5), waited, counts["operations"]) == (None, True, 0)
    assert (await row(engine, op))[:5] == ("pending", None, None, None, 0)
