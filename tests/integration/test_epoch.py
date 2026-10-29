"""Worker epochs and the takeover (spec §3.3.1 steps 3-6, §3.3.3; plan M0 Tasks 8, 21, M3 Task 27, Rulings
T8-h, T13-b, T13-d, T21-c, Codex r4 B1, r5 B10, r6 B5, r7 B4, Carries T11-b, T11-e).

The claim commits the epoch alone; every registered sweep then runs in a transaction of its own, in
registration order, opened with `fence_epoch` and `active_config FOR SHARE` (M3 Task 27). The two fixtures of
that split are asserted apart: the lock order (an old `batch_submit` preparation and an old online `fail`
waiting for a month row a test session holds, a configuration writer queued behind the shares, the
`operations` sweep before and after the `batch_submissions` sweep) and the lifecycle of a dead epoch's
submissions without concurrency. Every datum is invented.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass
from typing import Any, Final

import httpx
import procrastinate
import psycopg
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.sql import LOCK_NOT_AVAILABLE, pg_error
from jobhunter.errors import FencedOut
from jobhunter.graphs.score.deps import ScoreDeps
from jobhunter.graphs.score.nodes import claim as claim_node
from jobhunter.graphs.score.nodes import fail as fail_mod
from jobhunter.graphs.score.state import initial_state
from jobhunter.llm import spend_ledger
from jobhunter.llm.budget import month_of
from jobhunter.llm.fake import BatchScript
from jobhunter.scoring import batch_operations as batch_ops
from jobhunter.scoring import batches as batches_mod
from jobhunter.scoring import cycles
from jobhunter.scoring.admission import Picked
from jobhunter.scoring.routing import CLAIM_LOST
from jobhunter.worker import epoch as epoch_module
from jobhunter.worker import operations as ops
from jobhunter.worker import runs as runs_module
from jobhunter.worker.epoch import (
    Swept,
    TakeoverSweep,
    WorkerIdentity,
    claim_epoch_and_sweep,
    register_takeover_sweep,
)
from jobhunter.worker.executions import ExecutionRegistry
from jobhunter.worker.lock import LockSession
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.batches import batch_ledger, bulk_with_targets, make_due, submission_rows
from tests.support.faults import PausePoint, SimulatedKill, killing_at, no_fault
from tests.support.llm import FakeLLM
from tests.support.logs import logged
from tests.support.m3 import active_ids
from tests.support.pg import LockWait, PgServer, blocked_or_done, lock_wait_details, pg_one, relation_locks
from tests.support.procs import await_until, wait_until
from tests.support.profile import OpRunner
from tests.support.reconcile import run_reconciler
from tests.support.scoring import (
    ClaimedTarget,
    claimed_target,
    cycle_of,
    insert_targets,
    score_config,
    score_deps,
    target_of,
    targets_by_state,
)
from tests.support.settings import active_row, change_interval, held_save

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


async def test_every_sweep_transaction_runs_under_the_10_s_lock_timeout(
    engine, worker_lock, monkeypatch
) -> None:
    """Ruling T13-b, Task 27: each sweep's own transaction sets the takeover's `lock_timeout` again."""
    monkeypatch.setattr(epoch_module, "_SWEEPS", {})  # only the probes below sweep
    seen: list[tuple[str, str]] = []

    async def probe(conn, epoch: int) -> int:
        timeout = (await conn.execute(sa.text("SHOW lock_timeout"))).scalar_one()
        seen.append(
            (timeout, (await conn.execute(sa.text("SELECT pg_current_xact_id()::text"))).scalar_one())
        )
        return 0

    register_takeover_sweep("first_probe")(probe)
    register_takeover_sweep("second_probe")(lambda conn, epoch: probe(conn, epoch))
    await claim_epoch_and_sweep(engine, WorkerIdentity(uuid.uuid4(), "h", 1, worker_lock.backend_pid, "v"))
    assert [timeout for timeout, _ in seen] == ["10s", "10s"]
    assert seen[0][1] != seen[1][1]  # two transactions


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
    engine, worker_lock, make_config, migrated, monkeypatch, caplog, table
) -> None:
    """M3 Task 27: A's claim commits alone, so its sweeps run after B's claim may have committed: a sweep of A
    that B's newer epoch fences out ends A's takeover with `FencedOut` (A is superseded and does not start),
    and one that ran before B's claim committed is swept by B's own sweeps. Either way B is the holder."""
    caplog.set_level(logging.INFO)
    if table == "earlier_row":
        pg_one(migrated, "app", _EARLIER_WORKERS_ROW)
    monkeypatch.setattr(epoch_module, "_SWEEPS", {})
    swept: list[int] = []

    @register_takeover_sweep("recording")
    async def recording(conn, epoch: int) -> int:
        swept.append(epoch)
        return 0

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
        try:
            await asyncio.wait_for(a_claim, 5)
        except FencedOut as fenced:  # A's sweep came after B's claim: A is superseded
            assert fenced.worker_superseded
        (a_epoch,) = [
            e["epoch"]
            for e in logged(caplog, "worker_epoch_claimed")
            if e["instance_id"] == str(a.instance_id)
        ]
        b_epoch, _ = await asyncio.wait_for(b_claim, 5)
        assert b_epoch in swept and set(swept) <= {a_epoch, b_epoch}  # B swept; A only while it was current
        # B claimed last: its epoch is the newest and the row records B, so B's heartbeat still holds.
        assert (b_epoch > a_epoch, pg_one(migrated, "app", _RECORDED_HOLDER)) == (
            True,
            (b_epoch, b.instance_id, b.lock_backend_pid),
        )
        assert b_session.heartbeat(b.instance_id, b_epoch) is True
        assert b_waited
    finally:
        b_session.close()


# --- M3 Task 27 (Codex r4 B1): the epoch claim commits alone, then one fenced transaction per sweep ---------

WAIT_S: Final = 30.0
BATCH: Final = 200  # `scoring.batch_min_targets`, the smallest batch the settings allow
# The production registration order (`worker.main._REGISTERING_MODULES`): `worker.operations`, `worker.runs`,
# `scoring.cycles` (its two), `scoring.batch_operations`.
PRODUCTION_ORDER: Final = ("operations", "runs", "score_targets", "score_cycles", "batch_submissions")
# The owner rows a sweep takes (step 5) and the run rows: none of them before `active_config` (step 2).
OWNER_TABLES: Final = frozenset({"operations", "batch_submissions", "score_cycles", "score_targets", "runs"})
_PID: Final = sa.text("SELECT pg_backend_pid()")
_XID: Final = sa.text("SELECT pg_current_xact_id()::text")
_XACT_STATUS: Final = "SELECT pg_xact_status(CAST(%s AS xid8))"
_EPOCH: Final = "SELECT worker_epoch FROM worker_heartbeat"
_HOLD_MONTH: Final = sa.text("SELECT 1 FROM llm_budget WHERE month = :m FOR UPDATE")
_OPERATION: Final = sa.text("SELECT status, claim_id, worker_epoch, attempts FROM operations WHERE id = :o")
# What the server logs for a deadlock and for a lock wait past `lock_timeout` (`log_error_verbosity = terse`).
_SERVER_ERRORS: Final = ("deadlock detected", "canceling statement due to lock timeout")


def real_sweeps() -> dict[str, TakeoverSweep]:
    """The five takeover sweeps of the worker, by name (their modules are imported here)."""
    return {
        "operations": ops.sweep_older_epoch_operations,
        "runs": runs_module.sweep_older_epoch_runs,
        "score_targets": cycles.sweep_older_epoch_claims,
        "score_cycles": cycles.sweep_older_epoch_cycles,
        "batch_submissions": batch_ops.sweep_lost_submissions,
    }


def claimant(lock: LockSession, pid: int) -> WorkerIdentity:
    assert lock.backend_pid is not None
    return WorkerIdentity(uuid.uuid4(), "h", pid, lock.backend_pid, "v")


@dataclass(frozen=True)
class SweepStart:
    """What a sweep's transaction held when the sweep's own statements began: its backend and transaction,
    the relations it had locked, whether another session read the new epoch, and the state of the previous
    sweep's transaction (`None` for the first sweep)."""

    name: str
    pid: int
    xid: str
    relations: frozenset[str]
    epoch_visible: bool
    previous: str | None


class Observed:
    """The real sweeps in a given order, each wrapped to record its `SweepStart` before it runs, and held at
    `pause` (a `PausePoint` named after a sweep) when given."""

    def __init__(self, server: PgServer, epoch: int, pause: PausePoint | None = None) -> None:
        self.server, self.epoch, self.pause = server, epoch, pause
        self.starts: list[SweepStart] = []

    def sweeps(self, order: tuple[str, ...]) -> dict[str, TakeoverSweep]:
        real = real_sweeps()
        return {name: self._observed(name, real[name]) for name in order}

    def _observed(self, name: str, sweep: TakeoverSweep) -> TakeoverSweep:
        async def observed(conn: Any, epoch: int) -> int | Swept:
            pid = int((await conn.execute(_PID)).scalar_one())
            xid = str((await conn.execute(_XID)).scalar_one())
            previous = None
            if self.starts:
                status = pg_one(self.server, "superuser", _XACT_STATUS, self.starts[-1].xid)
                previous = None if status is None else str(status[0])
            visible = pg_one(self.server, "app", _EPOCH) == (self.epoch,)  # another session reads it
            locked = relation_locks(self.server, pid)
            self.starts.append(SweepStart(name, pid, xid, locked, visible, previous))
            if self.pause is not None:
                await self.pause(name)
            return await sweep(conn, epoch)

        return observed

    def assert_one_fenced_transaction_each(self, order: tuple[str, ...]) -> None:
        """Every sweep ran in order, in a transaction of its own that began after the epoch claim committed
        and after the previous sweep's transaction committed, holding the heartbeat row (`fence_epoch`) and
        `active_config` before any owner row."""
        assert [start.name for start in self.starts] == list(order)
        assert len({start.xid for start in self.starts}) == len(order)
        for start in self.starts:
            assert start.epoch_visible, start
            assert {"worker_heartbeat", "active_config"} <= start.relations, start
            assert not start.relations & OWNER_TABLES, start
        assert [start.previous for start in self.starts] == [None] + ["committed"] * (len(order) - 1)


class WaitSampler:
    """Every lock wait of the running sweep's backend on an owner row, sampled while `until` runs, with
    whether that backend held `active_config` at the time."""

    def __init__(self, server: PgServer, observed: Observed) -> None:
        self.server, self.observed = server, observed
        self.waits: list[tuple[str, str, bool]] = []

    async def run(self, until: "asyncio.Task[Any]") -> None:
        while not until.done():
            if self.observed.starts:
                current = self.observed.starts[-1]
                for wait in lock_wait_details(self.server):
                    if wait.pid == current.pid and wait.relation in OWNER_TABLES:
                        held = "active_config" in relation_locks(self.server, wait.pid)
                        self.waits.append((current.name, wait.relation, held))
            await asyncio.sleep(0.05)


def server_errors(server: PgServer) -> int:
    """How many deadlocks and lock timeouts the server has logged so far."""
    logs = server.logs()
    return sum(logs.count(message) for message in _SERVER_ERRORS)


class Background:
    """The tasks a test starts; any still running when the test ends (it failed) is cancelled and awaited, so
    no held transaction outlives its test."""

    def __init__(self) -> None:
        self.tasks: list[asyncio.Task[Any]] = []

    def __call__[T](self, work: Coroutine[Any, Any, T]) -> "asyncio.Task[T]":
        task = asyncio.create_task(work)
        self.tasks.append(task)
        return task

    async def close(self) -> None:
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


@pytest.fixture
async def background() -> AsyncIterator[Background]:
    spawned = Background()
    try:
        yield spawned
    finally:
        await spawned.close()


async def waits(server: PgServer, *tasks: "asyncio.Task[Any]", count: int = 1) -> list[LockWait]:
    """The lock waits once `count` statements wait (no task may end meanwhile: its error is raised here)."""

    async def ready() -> bool:
        return len(lock_wait_details(server)) >= count or any(t.done() for t in tasks)

    await await_until(ready, WAIT_S, 0.02)
    for task in tasks:
        if task.done():
            task.result()  # raises the task's own error
            raise AssertionError("a task that should wait ended")
    return lock_wait_details(server)


def month_waits(found: list[LockWait]) -> list[LockWait]:
    """The waits at the month row (step 6): on the row, or an `ensure_month` insert's wait for the
    transaction that holds it (that wait names no tuple)."""
    return [
        w
        for w in found
        if w.relation == "llm_budget" or (w.relation is None and "INTO llm_budget" in w.query)
    ]


async def run_fail(deps: ScoreDeps, claimed: ClaimedTarget, error_code: str) -> Any:
    """The online `fail` node of `claimed`'s attempt with `error_code` (its own transaction)."""
    state = initial_state(claimed.ref, claimed.attempt_id)
    state["error_code"] = error_code
    return await fail_mod.make_fail(deps)(state, await score_config(deps, claimed))


async def save_and_commit(engine: AsyncEngine, client: httpx.AsyncClient) -> int:
    """A settings save (`held_save`: the real `save_settings`, `active_config FOR UPDATE` first) committed at
    once; its version id."""
    async with held_save(engine, client, change_interval) as save:
        await save.commit()
        return save.version_id


async def operation_row(engine: AsyncEngine, operation_id: int) -> tuple[Any, ...]:
    async with engine.connect() as conn:
        return tuple((await conn.execute(_OPERATION, {"o": operation_id})).one())


@pytest.mark.parametrize("order", ["operations_first", "batch_submissions_first"])
async def test_each_takeover_sweep_is_its_own_fenced_transaction_the_lock_order(
    engine: AsyncEngine,
    migrated: PgServer,
    worker_lock: LockSession,
    current_epoch: int,
    queue_app: procrastinate.App,
    op_runner: OpRunner,
    fake_llm: FakeLLM,
    logged_in_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    background: Background,
    order: str,
) -> None:
    """Codex r4 B1, r5 B10, r6 B5, r7 B4. A test session holds the month row (as `budget_release` would); an
    old `batch_submit` execution waits for it inside its `prepare`, holding its operation row (step 5); an old
    online attempt waits for it inside its `fail`, holding its target row (step 5); a settings save queues
    behind their `active_config` shares. The new epoch's takeover runs, and the test session lets the month
    row go. In both orders of the `operations` and `batch_submissions` sweeps: the epoch is committed before
    the first sweep, each sweep's transaction holds `active_config` before any owner row (also while it waits
    for one) and nothing of the previous sweep's, the old operation is reclaimed, nobody deadlocks or times
    out, and the old attempt's target ends by the order the sessions committed in. The old execution, held
    after its prepare committed until the takeover ended, finds its claim gone at `submit_start`; its
    `preparing` row, which may have committed after the `batch_submissions` sweep scanned, is `abandoned`
    once the tick reconciler has run."""
    sweep_order = (
        PRODUCTION_ORDER if order == "operations_first" else ("batch_submissions", *PRODUCTION_ORDER[:-1])
    )
    errors_before = server_errors(migrated)
    now = utcnow()
    month = month_of(now)
    setup = await bulk_with_targets(engine, BATCH)
    assert setup.operation_id is not None
    online = await claimed_target(engine)  # an online claim of epoch 1, in no bulk operation
    async with engine.begin() as conn:
        await spend_ledger.ensure_month(conn, month, await spend_ledger.current_params(conn), now)
    deps = await score_deps(engine, fake_llm.gateway(engine))
    fake_llm.batches(BatchScript(create="ok"))
    # the old execution is held after its prepare committed, before `submit_start` (Codex r7 B4)
    after_prepare = PausePoint(batch_ops.AFTER_PREPARE_COMMIT)
    monkeypatch.setattr(batch_ops, "afault_point", after_prepare)
    started: list[object] = []
    real_submit_start = batches_mod.submit_start

    async def recording_submit_start(*args: Any, **kwargs: Any) -> Any:
        try:
            outcome = await real_submit_start(*args, **kwargs)
        except FencedOut as fenced:
            started.append(fenced)
            raise
        started.append(outcome)
        return outcome

    monkeypatch.setattr(batches_mod, "submit_start", recording_submit_start)
    paused = PausePoint("operations")
    observed = Observed(migrated, epoch=current_epoch + 1, pause=paused)
    monkeypatch.setattr(epoch_module, "_SWEEPS", observed.sweeps(sweep_order))

    holder = await engine.connect()
    try:
        await holder.begin()
        await holder.execute(
            _HOLD_MONTH, {"m": month}
        )  # the month row FOR UPDATE, as budget_release takes it
        old_handler = background(
            ops.execute_operation(
                engine,
                setup.operation_id,
                epoch=current_epoch,
                deps=op_runner.deps,
                registry=ExecutionRegistry(),
                heartbeat_period_s=3600,
            )
        )
        (prepare_wait,) = month_waits(await waits(migrated, old_handler))
        preparer = prepare_wait.pid
        assert "operations" in relation_locks(migrated, preparer)  # its operation row (step 5), held
        old_fail = background(run_fail(deps, online, "budget_exhausted"))
        found = month_waits(await waits(migrated, old_handler, old_fail, count=2))
        assert len(found) == 2  # the fail holds its target (step 5) and waits for the month row (step 6)
        writer = background(save_and_commit(engine, logged_in_client))
        found = await waits(migrated, old_handler, old_fail, writer, count=3)
        (config_wait,) = [w for w in found if w.relation == "active_config"]
        assert config_wait.key == "1"  # the save queues behind the old transactions' shares
        takeover = background(claim_epoch_and_sweep(engine, claimant(worker_lock, 2)))
        sampler = WaitSampler(migrated, observed)
        sampling = background(sampler.run(takeover))
        await asyncio.wait_for(paused.reached.wait(), WAIT_S)
        # a third session reads the epoch while the `operations` sweep is held: the claim committed alone
        assert pg_one(migrated, "app", _EPOCH) == (current_epoch + 1,)
        operations_sweep = observed.starts[-1]
        assert operations_sweep.name == "operations"
        paused.resume()

        async def sweep_waits_for_the_operation() -> bool:
            if takeover.done():
                return True
            return any(
                w.pid == operations_sweep.pid and w.relation == "operations"
                for w in lock_wait_details(migrated)
            )

        await await_until(sweep_waits_for_the_operation, WAIT_S, 0.02)
        assert not takeover.done()
        (sweep_wait,) = [w for w in lock_wait_details(migrated) if w.pid == operations_sweep.pid]
        assert sweep_wait.key == str(setup.operation_id) and preparer in sweep_wait.blocking_pids
        assert "active_config" in relation_locks(migrated, operations_sweep.pid)
    finally:
        await holder.rollback()  # the month row goes
        await holder.close()
    epoch, counts = await asyncio.wait_for(takeover, WAIT_S)
    failed = await asyncio.wait_for(old_fail, WAIT_S)
    version = await asyncio.wait_for(writer, WAIT_S)
    await asyncio.wait_for(sampling, WAIT_S)

    assert epoch == current_epoch + 1 and counts["operations"] == 1  # the old operation, reclaimed
    observed.assert_one_fenced_transaction_each(sweep_order)
    assert ("operations", "operations", True) in sampler.waits
    assert all(held for _, _, held in sampler.waits), sampler.waits  # never an owner row before the config
    assert await operation_row(engine, setup.operation_id) == ("pending", None, None, 1)
    # the old attempt's target, by the order the sessions committed in: its fail first, or the reclaim
    target = await target_of(engine, online.target_id)
    if failed.update["outcome"] == CLAIM_LOST:
        assert target.state == "pending" and counts["score_targets"] == 1
    else:
        assert (target.state, target.state_reason) == ("budget_deferred", "budget_exhausted")
        assert counts["score_targets"] == 0
    assert (await active_row(engine)).settings_version_id == version  # the queued save went through

    # the old execution is still held after its prepare committed; the tick reconciler runs once
    assert after_prepare.reached.is_set() and not old_handler.done()
    recovered = await run_reconciler("batch_submissions", engine, now=utcnow(), epoch=epoch)
    assert counts["batch_submissions"] + recovered == 1  # the sweep or the reconciler, whichever saw it
    (sub,) = await submission_rows(engine)
    assert (sub.state, sub.provider_batch_id) == ("abandoned", None)
    after_prepare.resume()
    await asyncio.wait_for(old_handler, WAIT_S)
    (refused,) = started
    assert isinstance(refused, FencedOut) and refused.owner == "operation"  # its claim was taken back
    (sub,) = await submission_rows(engine)
    assert sub.state == "abandoned" and fake_llm.state.batches == {}  # nothing was ever sent
    assert await operation_row(engine, setup.operation_id) == ("pending", None, None, 1)
    assert logged(caplog, "operation_handler_failed") == []
    assert server_errors(migrated) == errors_before  # no deadlock, no lock timeout


@pytest.mark.parametrize("left", ["preparing", "submit_started"])
async def test_each_takeover_sweep_is_its_own_fenced_transaction_the_lifecycle(
    engine: AsyncEngine,
    migrated: PgServer,
    worker_lock: LockSession,
    current_epoch: int,
    queue_app: procrastinate.App,
    op_runner: OpRunner,
    fake_llm: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
    left: str,
) -> None:
    """Codex r5 B10, under no concurrency: the worker of epoch 1 dies with its submission `preparing` or
    `submit_started`. The takeover, one fenced transaction per sweep, reclaims the operation, abandons a
    `preparing` submission (its reservations released `never_dispatched`, its targets `pending`) and makes a
    `submit_started` one `submit_ambiguous` with `{"error_class": "execution_lost"}`, its spend still counted;
    neither is sent, and the operation run again never sends the ambiguous one."""
    setup = await bulk_with_targets(engine, BATCH)
    assert setup.operation_id is not None
    fake_llm.batches(BatchScript(create="ok"))
    point = batch_ops.AFTER_PREPARE_COMMIT if left == "preparing" else batch_ops.AFTER_SUBMIT_START
    monkeypatch.setattr(batch_ops, "afault_point", killing_at(point))
    with pytest.raises(SimulatedKill):
        await op_runner.run_one(setup.operation_id)  # the process dies there: its operation stays running
    (sub,) = await submission_rows(engine)
    assert sub.state == left
    month = month_of(utcnow())
    counted_before = await counted_usd(engine, month)
    observed = Observed(migrated, epoch=current_epoch + 1)
    monkeypatch.setattr(epoch_module, "_SWEEPS", observed.sweeps(PRODUCTION_ORDER))
    epoch, counts = await claim_epoch_and_sweep(engine, claimant(worker_lock, 2))
    assert (epoch, counts["operations"], counts["batch_submissions"]) == (current_epoch + 1, 1, 1)
    observed.assert_one_fenced_transaction_each(PRODUCTION_ORDER)
    (after,) = await submission_rows(engine)
    ledger = await batch_ledger(engine)
    if left == "preparing":
        assert after.state == "abandoned"
        assert ledger == [("not_billed", "never_dispatched")] * BATCH
        assert await targets_by_state(engine, setup.llm_tuple_id) == {"pending": BATCH}
    else:
        assert (after.state, after.reconcile_report) == (
            "submit_ambiguous",
            {"error_class": "execution_lost"},
        )
        assert [state for state, _ in ledger] == ["dispatched"] * BATCH
        assert await counted_usd(engine, month) == counted_before >= after.reserved_usd > 0  # still counted
        assert await targets_by_state(engine, setup.llm_tuple_id) == {"in_batch": BATCH}
    assert after.provider_batch_id is None and fake_llm.state.batches == {}  # neither was sent
    if left == "submit_started":
        monkeypatch.setattr(batch_ops, "afault_point", no_fault)
        op_runner.epoch = epoch
        await make_due(engine)
        await op_runner.run_one(setup.operation_id)  # the reclaimed operation runs again
        (again,) = await submission_rows(engine)
        assert again.state == "submit_ambiguous" and fake_llm.state.batches == {}  # never resent


async def counted_usd(engine: AsyncEngine, month: Any) -> Any:
    async with engine.connect() as conn:
        found = await conn.execute(
            sa.text("SELECT counted_usd FROM llm_budget WHERE month = :m"), {"m": month}
        )
        return found.scalar_one()


async def test_a_failing_sweep_ends_the_takeover_and_the_next_start_sweeps_again(
    engine: AsyncEngine,
    worker_lock: LockSession,
    current_epoch: int,
    queue_app: procrastinate.App,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A sweep that raises ends the takeover with its error: the epoch stays claimed and the earlier sweeps'
    commits stay, the later ones never ran. The next start claims the next epoch and runs every sweep again
    (each is idempotent over the epochs before its own)."""
    async with engine.begin() as conn:
        op, _ = await ops.create_operation(conn, kind="export", payload={}, dedup_key="t27", defer=None)
        claimed = await ops.claim_operation(conn, op, epoch=current_epoch)
    assert claimed is not None
    await insert_targets(engine, 1, lane="score_now")
    async with engine.begin() as conn:  # a running cycle of epoch 1
        cycle_id = await cycles.start_cycle(
            conn, trigger="score_now", now=utcnow(), epoch=current_epoch, fake_llm=True
        )
    assert cycle_id is not None
    failures = [RuntimeError("detail that must not be logged")]
    ran: list[tuple[str, int]] = []

    async def failing(conn: Any, epoch: int) -> int:
        ran.append(("failing", epoch))
        if failures:
            raise failures.pop()
        return 0

    async def orphaning(conn: Any, epoch: int) -> int | Swept:
        ran.append(("score_cycles", epoch))
        return await cycles.sweep_older_epoch_cycles(conn, epoch)

    monkeypatch.setattr(
        epoch_module,
        "_SWEEPS",
        {"operations": ops.sweep_older_epoch_operations, "failing": failing, "score_cycles": orphaning},
    )
    with pytest.raises(RuntimeError):
        await claim_epoch_and_sweep(engine, claimant(worker_lock, 2))
    async with engine.connect() as conn:
        assert (await conn.execute(sa.text(_EPOCH))).scalar_one() == current_epoch + 1  # claimed
    assert await operation_row(engine, op) == ("pending", None, None, 1)  # the earlier sweep's commit
    assert ran == [("failing", current_epoch + 1)]  # the later sweep never ran
    assert (await cycle_of(engine, cycle_id)).status == "running"
    (event,) = logged(caplog, "takeover_sweep_failed")
    assert (event["sweep"], event["epoch"], event["error_class"]) == (
        "failing",
        current_epoch + 1,
        "RuntimeError",
    )
    assert "must not be logged" not in caplog.text

    epoch, counts = await claim_epoch_and_sweep(engine, claimant(worker_lock, 3))
    assert epoch == current_epoch + 2
    assert counts == {"operations": 0, "failing": 0, "score_cycles": 1}
    assert ran[1:] == [("failing", epoch), ("score_cycles", epoch)]
    row = await cycle_of(engine, cycle_id)
    assert (row.status, row.status_reason) == ("failed", "orphaned")


async def test_a_superseded_workers_fail_cycle_and_the_takeover_sweeps_never_deadlock(
    engine: AsyncEngine,
    migrated: PgServer,
    worker_lock: LockSession,
    current_epoch: int,
    queue_app: procrastinate.App,
    monkeypatch: pytest.MonkeyPatch,
    background: Background,
) -> None:
    """Carries T11-b and T11-e. A superseded but still live worker ends its cycle for a bug (`fail_cycle`:
    the cycle row, step 5, then its claims) and is held between the two while the new epoch's takeover runs.
    The `score_targets` sweep reclaims the claims in a transaction of its own and commits; the `score_cycles`
    sweep then waits for the cycle row holding `active_config` and none of the target rows (in one takeover
    transaction it held them, out of the schedule's order: T11-b), so the old `fail_cycle` finds its claims
    reclaimed and ends instead of waiting for the takeover, which waited for it (the deadlock of T11-e)."""
    errors_before = server_errors(migrated)
    targets = await insert_targets(engine, 2, lane="score_now")
    async with engine.begin() as conn:
        cycle_id = await cycles.start_cycle(
            conn, trigger="score_now", now=utcnow(), epoch=current_epoch, fake_llm=True
        )
        assert cycle_id is not None
        active = await active_ids(conn)
        claimed = await claim_node.claim_round(
            conn,
            [Picked(t.target_id, lane="score_now") for t in targets],
            epoch=current_epoch,
            cycle_id=cycle_id,
            execution_config_id=active.execution_config_id,
            evaluation_policy_id=active.evaluation_policy_id,
        )
    assert len(claimed) == 2
    holding, go = asyncio.Event(), asyncio.Event()
    real_reclaim = cycles.reclaim

    async def held_reclaim(conn: Any, kind: Any, params: Any, *, detail: str, **kwargs: Any) -> Any:
        if detail == cycles.CYCLE_FAILED:  # the old worker's own end, after its cycle row
            holding.set()
            await asyncio.wait_for(go.wait(), WAIT_S)
        return await real_reclaim(conn, kind, params, detail=detail, **kwargs)

    monkeypatch.setattr(cycles, "reclaim", held_reclaim)

    async def fail_for_a_bug() -> int | None:
        async with engine.begin() as conn:
            return await cycles.fail_cycle(conn, cycle_id, epoch=current_epoch, error_class="NodeFailed")

    old = background(fail_for_a_bug())
    await asyncio.wait_for(holding.wait(), WAIT_S)
    observed = Observed(migrated, epoch=current_epoch + 1)
    monkeypatch.setattr(epoch_module, "_SWEEPS", observed.sweeps(PRODUCTION_ORDER))
    takeover = background(claim_epoch_and_sweep(engine, claimant(worker_lock, 2)))
    try:

        async def cycles_sweep_waits() -> bool:
            return takeover.done() or (
                bool(observed.starts)
                and observed.starts[-1].name == "score_cycles"
                and any(w.pid == observed.starts[-1].pid for w in lock_wait_details(migrated))
            )

        await await_until(cycles_sweep_waits, WAIT_S, 0.02)
        assert not takeover.done()
        sweep = observed.starts[-1]
        (wait,) = [w for w in lock_wait_details(migrated) if w.pid == sweep.pid]
        assert (wait.relation, wait.key) == ("score_cycles", str(cycle_id))  # the old owner's cycle row
        locked = relation_locks(migrated, sweep.pid)
        assert "active_config" in locked and "score_targets" not in locked  # T11-b: no target row held
        for target in targets:  # reclaimed and committed by the `score_targets` sweep's own transaction
            assert (await target_of(engine, target.target_id)).state == "pending"
    finally:
        go.set()
    assert (
        await asyncio.wait_for(old, WAIT_S) == 0
    )  # its claims were gone: it reclaimed none, waited for none
    epoch, counts = await asyncio.wait_for(takeover, WAIT_S)
    assert (epoch, counts["score_targets"], counts["score_cycles"]) == (current_epoch + 1, 2, 0)
    observed.assert_one_fenced_transaction_each(PRODUCTION_ORDER)
    row = await cycle_of(engine, cycle_id)
    assert (row.status, row.status_reason) == ("failed", "bug")
    async with engine.connect() as conn:
        attempts = (
            await conn.execute(sa.text("SELECT outcome, outcome_detail FROM score_attempts ORDER BY id"))
        ).all()
    assert [tuple(a) for a in attempts] == [("claim_lost", "worker_takeover")] * 2
    assert server_errors(migrated) == errors_before  # no deadlock, no lock timeout
