import uuid

import pytest
import sqlalchemy as sa
from jobhunter.errors import FencedOut
from jobhunter.worker import epoch as epoch_module
from jobhunter.worker.epoch import WorkerIdentity, claim_epoch_and_sweep, register_takeover_sweep
from jobhunter.worker.lock import LockSession

from tests.support.procs import wait_until

pytestmark = pytest.mark.integration


async def test_epoch_increments_and_records_holder(engine, make_config, migrated) -> None:
    # Each claim is made by a session that holds the singleton lock, as the worker does (ruling T8-h).
    dsn = make_config("worker", migrated).dsn("psycopg")
    first, second = LockSession(dsn), LockSession(dsn)
    try:
        first_pid, second_pid = first.connect(), second.connect()
        assert first.try_lock()
        e1, _ = await claim_epoch_and_sweep(engine, WorkerIdentity(uuid.uuid4(), "h", 1, first_pid, "0.1.0"))
        first.close()
        wait_until(second.try_lock, 5)
        e2, _ = await claim_epoch_and_sweep(engine, WorkerIdentity(uuid.uuid4(), "h", 2, second_pid, "0.1.0"))
        assert e2 == e1 + 1
        async with engine.connect() as c:
            assert tuple(
                (
                    await c.execute(sa.text("SELECT worker_epoch, lock_backend_pid FROM worker_heartbeat"))
                ).one()
            ) == (e2, second_pid)
    finally:
        first.close()
        second.close()


async def test_claim_by_a_pid_without_the_lock_is_fenced_out_before_any_write(
    engine, make_config, migrated, monkeypatch
) -> None:
    monkeypatch.setattr(epoch_module, "_SWEEPS", dict(epoch_module._SWEEPS))  # the sweep below is this test's
    swept: list[int] = []

    @register_takeover_sweep("recording")
    async def recording(conn, epoch: int) -> int:
        swept.append(epoch)
        return 0

    dsn = make_config("worker", migrated).dsn("psycopg")
    holder, other = LockSession(dsn), LockSession(dsn)
    row = sa.text(
        "SELECT worker_instance_id, worker_epoch, pid, lock_backend_pid, heartbeat_at FROM worker_heartbeat"
    )
    try:
        holder_pid, other_pid = holder.connect(), other.connect()
        assert holder.try_lock()
        e1, counts = await claim_epoch_and_sweep(
            engine, WorkerIdentity(uuid.uuid4(), "h", 1, holder_pid, "0.1.0")
        )
        assert swept == [e1] and counts["recording"] == 0
        async with engine.connect() as c:
            before = (await c.execute(row)).one()
        # A live backend that does not hold the lock, and a pid that is no backend at all.
        for pid in (other_pid, 1):
            with pytest.raises(FencedOut) as fenced:
                await claim_epoch_and_sweep(engine, WorkerIdentity(uuid.uuid4(), "h", 2, pid, "0.1.0"))
            assert fenced.value.owner == "worker_lock"
        async with engine.connect() as c:
            assert (await c.execute(row)).one() == before
        assert swept == [e1]
    finally:
        holder.close()
        other.close()


async def test_heartbeat_holds_only_for_the_lock_holding_session_of_the_current_epoch(
    engine, make_config, migrated
) -> None:
    dsn = make_config("worker", migrated).dsn("psycopg")
    holder, other = LockSession(dsn), LockSession(dsn)
    try:
        pid = holder.connect()
        other.connect()
        assert holder.try_lock() and not other.try_lock()
        me = uuid.uuid4()
        epoch, _ = await claim_epoch_and_sweep(engine, WorkerIdentity(me, "h", 1, pid, "0.1.0"))
        assert holder.heartbeat(me, epoch)
        assert not other.heartbeat(me, epoch)  # the row is ours, but this session does not hold the lock
        await claim_epoch_and_sweep(engine, WorkerIdentity(uuid.uuid4(), "h", 2, pid, "0.1.0"))
        assert not holder.heartbeat(me, epoch)  # a newer epoch owns the row: no row is updated
        holder.close()  # closing the lock session releases the lock
        wait_until(other.try_lock, 5)
    finally:
        holder.close()
        other.close()
