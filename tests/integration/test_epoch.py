import asyncio
import uuid

import psycopg
import pytest
import sqlalchemy as sa
from jobhunter.db.sql import LOCK_NOT_AVAILABLE, pg_error
from jobhunter.errors import FencedOut
from jobhunter.worker import epoch as epoch_module
from jobhunter.worker.epoch import Swept, WorkerIdentity, claim_epoch_and_sweep, register_takeover_sweep
from jobhunter.worker.lock import LockSession

from tests.support.faults import PausePoint
from tests.support.logs import logged
from tests.support.pg import blocked_or_done, pg_one
from tests.support.procs import await_until, wait_until

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


# --- Ruling T13-b: the claim cannot wait forever behind a frozen holder's row locks -------------------------


async def test_the_claim_transaction_runs_under_a_10_s_lock_timeout(engine, worker_lock, monkeypatch) -> None:
    monkeypatch.setattr(epoch_module, "_SWEEPS", {})  # only the probe below sweeps
    seen: list[str] = []

    @register_takeover_sweep("lock_timeout_probe")
    async def probe(conn, epoch: int) -> int:
        seen.append((await conn.execute(sa.text("SHOW lock_timeout"))).scalar_one())
        return 0

    await claim_epoch_and_sweep(engine, WorkerIdentity(uuid.uuid4(), "h", 1, worker_lock.backend_pid, "v"))
    assert seen == ["10s"]


async def test_a_row_lock_held_past_the_lock_timeout_fails_the_claim_with_55p03_and_writes_nothing(
    engine, worker_lock, migrated
) -> None:
    lock_pid = worker_lock.backend_pid
    first, _ = await claim_epoch_and_sweep(engine, WorkerIdentity(uuid.uuid4(), "h", 1, lock_pid, "v"))
    with psycopg.connect(migrated.dsn("superuser")) as holder:
        holder.execute("SELECT 1 FROM worker_heartbeat WHERE id = 1 FOR UPDATE")  # held until the block ends
        with pytest.raises(sa.exc.OperationalError) as blocked:
            await claim_epoch_and_sweep(
                engine, WorkerIdentity(uuid.uuid4(), "h", 2, lock_pid, "v"), lock_timeout_ms=200
            )
        assert pg_error(blocked.value).sqlstate == LOCK_NOT_AVAILABLE
    async with engine.connect() as c:
        assert tuple((await c.execute(sa.text("SELECT worker_epoch, pid FROM worker_heartbeat"))).one()) == (
            first,
            1,
        )


# --- Ruling T13-d: an after-commit action cannot mask the committed takeover -------------------------------


async def test_a_failing_after_commit_action_neither_masks_the_takeover_nor_skips_the_others(
    engine, worker_lock, monkeypatch, caplog
) -> None:
    monkeypatch.setattr(epoch_module, "_SWEEPS", {})  # only the two sweeps below
    observed: list[int] = []

    def explode() -> None:
        raise RuntimeError("detail that must not be logged")

    @register_takeover_sweep("first")
    async def first(conn, epoch: int) -> Swept:
        return Swept(count=1, after_commit=explode)

    @register_takeover_sweep("second")
    async def second(conn, epoch: int) -> Swept:
        return Swept(count=2, after_commit=lambda: observed.append(epoch))

    epoch, counts = await claim_epoch_and_sweep(
        engine, WorkerIdentity(uuid.uuid4(), "h", 1, worker_lock.backend_pid, "v")
    )
    assert counts == {"first": 1, "second": 2} and observed == [epoch]
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT worker_epoch FROM worker_heartbeat"))).scalar_one() == epoch
    (event,) = logged(caplog, "takeover_after_commit_failed")
    assert (event["sweep"], event["error_class"], event["epoch"]) == ("first", "RuntimeError", epoch)
    assert "must not be logged" not in caplog.text


# --- Final fix wave, item 3: a claim that passed the lock check is serialised before the next holder's -----

# What an earlier worker left behind: the row exists, its epoch is 7.
_EARLIER_WORKERS_ROW = (
    "INSERT INTO worker_heartbeat (id, worker_instance_id, worker_epoch, hostname, pid, lock_backend_pid, "
    "app_version, started_at, heartbeat_at) VALUES (1, gen_random_uuid(), 7, 'h', 1, 1, 'v', now(), now())"
)
_RECORDED_HOLDER = "SELECT worker_epoch, worker_instance_id, lock_backend_pid FROM worker_heartbeat"


@pytest.mark.parametrize("table", ["earlier_row", "empty"])
async def test_a_claimant_that_lost_its_lock_after_the_check_cannot_evict_the_next_holder(
    engine, worker_lock, make_config, migrated, monkeypatch, table
) -> None:
    if table == "earlier_row":
        pg_one(migrated, "app", _EARLIER_WORKERS_ROW)
    pause = PausePoint("takeover.before_epoch_write")
    monkeypatch.setattr(epoch_module, "afault_point", pause)
    a = WorkerIdentity(uuid.uuid4(), "a", 1, worker_lock.backend_pid, "v")
    a_claim = asyncio.create_task(claim_epoch_and_sweep(engine, a))
    # A proved it holds the lock (under the row lock) and waits before its epoch write ...
    await asyncio.wait_for(pause.reached.wait(), 5)
    # ... while its lock session dies, so B can take the lock and claim.
    pg_one(migrated, "superuser", "SELECT pg_terminate_backend(%s, 5000)", worker_lock.backend_pid)
    b_session = LockSession(make_config("worker", migrated).dsn("psycopg"))
    try:
        b_session.connect()
        wait_until(b_session.try_lock, 5)
        b = WorkerIdentity(uuid.uuid4(), "b", 2, b_session.backend_pid or 0, "v")
        b_claim = asyncio.create_task(claim_epoch_and_sweep(engine, b))
        try:
            await await_until(lambda: blocked_or_done(migrated, b_claim), 5)
            b_waited = not b_claim.done()  # B's claim waits for A's, instead of committing before it
        finally:
            pause.resume()
        a_epoch, _ = await asyncio.wait_for(a_claim, 5)
        b_epoch, _ = await asyncio.wait_for(b_claim, 5)
        # B claimed last: its epoch is the newest and the row records B, so B's heartbeat still holds.
        assert (b_epoch > a_epoch, pg_one(migrated, "app", _RECORDED_HOLDER)) == (
            True,
            (b_epoch, b.instance_id, b.lock_backend_pid),
        )
        assert b_session.heartbeat(b.instance_id, b_epoch) is True
        assert b_waited
    finally:
        b_session.close()
