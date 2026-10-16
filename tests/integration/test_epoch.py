import uuid

import pytest
import sqlalchemy as sa
from jobhunter.errors import FencedOut
from jobhunter.worker import epoch as epoch_module
from jobhunter.worker.epoch import WorkerIdentity, claim_epoch_and_sweep, register_takeover_sweep
from jobhunter.worker.lock import LockSession

from tests.support.pg import pg_one
from tests.support.procs import wait_until

pytestmark = pytest.mark.integration


def test_worker_lock_fixture_holds_the_singleton_lock(worker_lock, make_config, migrated) -> None:
    assert isinstance(worker_lock.backend_pid, int)
    assert pg_one(
        migrated,
        "superuser",
        "SELECT pid FROM pg_locks WHERE locktype='advisory' AND objid=7261001 AND granted",
    ) == (worker_lock.backend_pid,)
    other = LockSession(make_config("worker", migrated).dsn("psycopg"))
    try:
        other.connect()
        assert not other.try_lock()  # held by the fixture's session
    finally:
        other.close()


async def test_epoch_increments_and_records_holder(engine, worker_lock) -> None:
    # Each claim is made with the pid of a session that holds the singleton lock, as the worker does (T8-h).
    lock_pid = worker_lock.backend_pid
    e1, _ = await claim_epoch_and_sweep(engine, WorkerIdentity(uuid.uuid4(), "h", 1, lock_pid, "0.1.0"))
    second = WorkerIdentity(uuid.uuid4(), "h", 2, lock_pid, "0.1.0")
    e2, _ = await claim_epoch_and_sweep(engine, second)
    assert e2 == e1 + 1
    async with engine.connect() as c:
        assert tuple(
            (await c.execute(sa.text("SELECT worker_epoch, lock_backend_pid FROM worker_heartbeat"))).one()
        ) == (e2, lock_pid)
        recorded = (await c.execute(sa.text("SELECT worker_instance_id, pid FROM worker_heartbeat"))).one()
        assert tuple(recorded) == (second.instance_id, 2)  # the latest claimant is the recorded holder


async def test_claim_by_a_pid_without_the_lock_is_fenced_out_before_any_write(
    engine, worker_lock, make_config, migrated, monkeypatch
) -> None:
    monkeypatch.setattr(epoch_module, "_SWEEPS", dict(epoch_module._SWEEPS))  # the sweep below is this test's
    swept: list[int] = []

    @register_takeover_sweep("recording")
    async def recording(conn, epoch: int) -> int:
        swept.append(epoch)
        return 0

    other = LockSession(make_config("worker", migrated).dsn("psycopg"))
    row = sa.text(
        "SELECT worker_instance_id, worker_epoch, pid, lock_backend_pid, heartbeat_at FROM worker_heartbeat"
    )
    try:
        other_pid = other.connect()
        e1, counts = await claim_epoch_and_sweep(
            engine, WorkerIdentity(uuid.uuid4(), "h", 1, worker_lock.backend_pid, "0.1.0")
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
        other.close()


async def test_heartbeat_holds_only_for_the_lock_holding_session_of_the_current_epoch(
    engine, worker_lock, make_config, migrated
) -> None:
    other = LockSession(make_config("worker", migrated).dsn("psycopg"))
    try:
        other.connect()
        me = uuid.uuid4()
        epoch, _ = await claim_epoch_and_sweep(
            engine, WorkerIdentity(me, "h", 1, worker_lock.backend_pid, "0.1.0")
        )
        assert worker_lock.heartbeat(me, epoch)
        assert not other.heartbeat(me, epoch)  # the row is ours, but this session does not hold the lock
        await claim_epoch_and_sweep(
            engine, WorkerIdentity(uuid.uuid4(), "h", 2, worker_lock.backend_pid, "0.1.0")
        )
        assert not worker_lock.heartbeat(me, epoch)  # a newer epoch owns the row: no row is updated
        worker_lock.close()  # closing the lock session releases the lock; the fixture's close is a no-op
        wait_until(other.try_lock, 5)
    finally:
        other.close()
