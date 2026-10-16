import uuid

import pytest
import sqlalchemy as sa
from jobhunter.worker.epoch import WorkerIdentity, claim_epoch_and_sweep
from jobhunter.worker.lock import LockSession

from tests.support.procs import wait_until

pytestmark = pytest.mark.integration


async def test_epoch_increments_and_records_holder(engine) -> None:
    e1, _ = await claim_epoch_and_sweep(engine, WorkerIdentity(uuid.uuid4(), "h", 1, 4242, "0.1.0"))
    e2, _ = await claim_epoch_and_sweep(engine, WorkerIdentity(uuid.uuid4(), "h", 2, 4343, "0.1.0"))
    assert e2 == e1 + 1
    async with engine.connect() as c:
        assert tuple(
            (await c.execute(sa.text("SELECT worker_epoch, lock_backend_pid FROM worker_heartbeat"))).one()
        ) == (e2, 4343)


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
