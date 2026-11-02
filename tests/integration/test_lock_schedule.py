"""The lock schedule of the score lane on PostgreSQL (Global Constraints, §3.3.3, §9.1; plan M3 Task 18,
Deviation 38, Codex r1 #18, #19, #20, Carries T14-c, T16-c).

Each pair of transactions runs in two sessions, in both interleavings, and neither waits on a lock the other
holds out of the schedule's order: the one that comes second waits (if at all) on the first lock the two
share, holding nothing the first one still needs (`tests.support.pg.lock_wait_details`: the relation and row
waited on and who blocks it; `FOR UPDATE NOWAIT` probes of the rows the first one takes later), and both
commit.

- a settings save (the real `save_settings`, `active_config FOR UPDATE`, step 2) against
  `batch_items.process_item` publishing;
- `budget_release` (the month row, then target rows `SKIP LOCKED`) against `score_now` (`active_config`, then
  the target row, never the month row);
- a takeover holding the worker-heartbeat row `FOR UPDATE` against a `score_now` request (the API stamps no
  epoch: it never waits) and against `start_cycle` (its fence, step 1, waits first);
- `batches.prepare` (the month and bulk rows, then its candidates `SKIP LOCKED`) against an online `fail` of
  one of its candidates (the target, step 5, then the month row, step 6), and the stated exception itself: a
  candidate whose row another transaction holds while it is still `pending` is skipped, never waited for;
- the confirm's `activate_tuple` (`active_config FOR UPDATE`) against a cycle's `claim` (`active_config FOR
  SHARE`, the cycle, then the targets);
- Carry T14-c: an owner's `publish` or `fail` (the target row, then the bulk row of its follow-up hold)
  against node 9's cancellation (the bulk row first, Ruling P52, then the target row): forced into the
  deadlock, PostgreSQL aborts one and that side's retry of a deadlock (`DB_RETRY`, `is_retryable_db_error`)
  completes it; the other order waits on the target row only;
- Carry T16-c: `batches.abandon` against node 9's `targets._cancel` of a batched target: both take the target
  rows before the attempts, so whichever comes second waits on the target row.
- Carry T13-a (Task 22): an owner's `publish` against a merge's cancellation meets at the job row (`FOR
  SHARE` against the merge's `FOR UPDATE`, step 4) whichever comes first; an owner's `fail`, which takes no
  job row, against the merge (the job rows, the bulk row of the follow-up hold, then the target row, Ruling
  P52) is T14-c's pair: forced into the deadlock, one side is aborted and its retry completes it.

Every datum is invented.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any, Final, cast

import httpx
import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db import ranking
from jobhunter.db.sql import LOCK_NOT_AVAILABLE, pg_error
from jobhunter.dedup import merge
from jobhunter.errors import FencedOut
from jobhunter.graphs.sanitize import is_retryable_db_error
from jobhunter.graphs.score.context import ScoreContext
from jobhunter.graphs.score.deps import ScoreDeps
from jobhunter.graphs.score.nodes import claim as claim_node
from jobhunter.graphs.score.nodes import fail as fail_mod
from jobhunter.graphs.score.nodes import publish as publish_node
from jobhunter.graphs.score.state import CycleState, TargetState, initial_state
from jobhunter.llm import bulk
from jobhunter.llm import fake as fake_mod
from jobhunter.llm.budget import month_of
from jobhunter.llm.fake import BatchScript
from jobhunter.llm.gateway import LLMGateway
from jobhunter.scoring import batches, cycles, targets
from jobhunter.settings.dryrun import active_settings
from jobhunter.settings.model import Settings
from jobhunter.worker import epoch as epoch_mod
from jobhunter.worker.epoch import WorkerIdentity, claim_epoch_and_sweep
from jobhunter.worker.lock import LockSession
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.batches import (
    bulk_with_targets,
    decisions,
    poll_once,
    prepared_submission,
    submission_rows,
    submit_batch,
)
from tests.support.faults import PausePoint
from tests.support.llm import FakeLLM
from tests.support.m3 import active_ids, insert_cycle, insert_job
from tests.support.pg import LockWait, PgServer, lock_wait_details
from tests.support.procs import await_until
from tests.support.profile import OpRunner
from tests.support.reconcile import run_reconciler
from tests.support.scoring import (
    ClaimedTarget,
    NoClaims,
    activate_score_tuple,
    aged_job,
    attempt_end,
    claim_again,
    claimed_target,
    cycle_of,
    followup_hold,
    hold_of,
    insert_targets,
    score_config,
    score_deps,
    state_at_terminal,
    state_target,
    swept_of,
    synthetic_c_draft,
    target_of,
)
from tests.support.settings import active_row, change_interval, confirm_new_profile, held_save

pytestmark = pytest.mark.integration

WAIT_S: Final = 30.0
BATCH: Final = 200
_LOCK_RANKING: Final = "SELECT 1 FROM ranking_state WHERE id = 1 FOR UPDATE"
_LOCK_ACTIVE_CONFIG: Final = "SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE"
_PID: Final = sa.text("SELECT pg_backend_pid()")
_PROBES: Final[dict[str, str]] = {
    "active_config": "SELECT 1 FROM active_config WHERE id = :key FOR UPDATE NOWAIT",
    "jobs": "SELECT 1 FROM jobs WHERE id = :key FOR UPDATE NOWAIT",
    "score_targets": "SELECT 1 FROM score_targets WHERE id = :key FOR UPDATE NOWAIT",
    "score_attempts": "SELECT 1 FROM score_attempts WHERE id = :key FOR UPDATE NOWAIT",
    "llm_budget": "SELECT 1 FROM llm_budget WHERE month = :key FOR UPDATE NOWAIT",
    "bulk_operations": "SELECT 1 FROM bulk_operations WHERE id = :key FOR UPDATE NOWAIT",
    "worker_heartbeat": "SELECT 1 FROM worker_heartbeat WHERE id = :key FOR UPDATE NOWAIT",
}


# ---- the harness -------------------------------------------------------------------------------------------


async def pid_of(conn: AsyncConnection) -> int:
    return int((await conn.execute(_PID)).scalar_one())


async def free(engine: AsyncEngine, table: str, key: object) -> bool:
    """Whether `table`'s row `key` can be locked `FOR UPDATE` right now: no other transaction holds it."""
    async with engine.connect() as probe:
        await probe.begin()
        try:
            await probe.execute(sa.text(_PROBES[table]), {"key": key})
        except sa.exc.OperationalError as refused:
            assert pg_error(refused).sqlstate == LOCK_NOT_AVAILABLE
            return False
        finally:
            await probe.rollback()
    return True


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


def one_wait(found: list[LockWait], relation: str) -> LockWait:
    matching = [w for w in found if w.relation == relation]
    assert len(matching) == 1, found
    return matching[0]


def month_wait(found: list[LockWait]) -> LockWait:
    """The one wait at the month row (step 6): on the row itself, or the `ensure_month` insert's wait for the
    transaction that updated the row (`ON CONFLICT DO NOTHING` waits for it; that wait names no tuple)."""
    matching = [
        w
        for w in found
        if w.relation == "llm_budget" or (w.relation is None and "INTO llm_budget" in w.query)
    ]
    assert len(matching) == 1, found
    return matching[0]


@asynccontextmanager
async def holding(engine: AsyncEngine, statement: str, **params: object) -> AsyncIterator[int]:
    """A transaction that holds the rows of `statement` until the block ends (rolled back); its pid."""
    async with engine.connect() as blocker:
        await blocker.begin()
        await blocker.execute(sa.text(statement), params)
        try:
            yield await pid_of(blocker)
        finally:
            await blocker.rollback()


class Held:
    """A transaction run in a task that holds its locks after `work` until `release()` (or until it is
    cancelled), then commits: `held` is set once `work` returned, `pid` is its backend."""

    def __init__(self, engine: AsyncEngine, work: Callable[[AsyncConnection], Awaitable[Any]]) -> None:
        self.engine, self.work = engine, work
        self.held, self._release = asyncio.Event(), asyncio.Event()
        self.pid = 0
        self.result: Any = None
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> Any:
        async with self.engine.begin() as conn:
            self.pid = await pid_of(conn)
            self.result = await self.work(conn)
            self.held.set()
            await asyncio.wait_for(self._release.wait(), 60)
        return self.result

    async def holding(self) -> None:
        done, _ = await asyncio.wait(
            {self.task, asyncio.create_task(self.held.wait())}, timeout=WAIT_S, return_when="FIRST_COMPLETED"
        )
        if self.task in done:
            self.task.result()
            raise AssertionError("the held transaction ended before it held its locks")
        assert self.held.is_set()

    async def commit(self) -> Any:
        self._release.set()
        return await asyncio.wait_for(self.task, WAIT_S)


def background[T](work: Coroutine[Any, Any, T]) -> "asyncio.Task[T]":
    return asyncio.create_task(work)


async def settle(*tasks: "asyncio.Task[Any]") -> list[Any]:
    return [await asyncio.wait_for(t, WAIT_S) for t in tasks]


@pytest.fixture(autouse=True)
def _task_app(queue_app: procrastinate.App) -> procrastinate.App:
    """The process's task app (`defer_in_tx` and the reconcilers' contexts name it)."""
    return queue_app


@pytest.fixture
async def gateway(engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int) -> LLMGateway:
    return fake_llm.gateway(engine)


@pytest.fixture
async def deps(engine: AsyncEngine, gateway: LLMGateway) -> ScoreDeps:
    return await score_deps(engine, gateway)


async def save_and_commit(engine: AsyncEngine, client: httpx.AsyncClient) -> int:
    """A settings save (`held_save`: the real `save_settings`, `active_config FOR UPDATE` first) committed at
    once; its version id."""
    async with held_save(engine, client, change_interval) as save:
        await save.commit()
        return save.version_id


# ---- a settings activation against process_item publishing (Codex r1 #18) --------------------------------


@pytest.mark.parametrize("first", ["activation", "process_item"])
async def test_an_activation_and_process_item_publishing_never_wait_out_of_order(
    engine: AsyncEngine,
    migrated: PgServer,
    op_runner: OpRunner,
    fake_llm: FakeLLM,
    current_epoch: int,
    logged_in_client: httpx.AsyncClient,
    first: str,
) -> None:
    # the activation is a real settings save (`held_save`: `save_settings` in a transaction left open); the
    # batch is the smallest the settings allow (`batch_min_targets`, 200)
    submitted = await submit_batch(engine, op_runner, fake_llm, BATCH)
    target = submitted.setup.targets[0]
    fake_mod.batches(BatchScript(polls_until_ended=0))  # the next poll ends it and processes its items
    month = month_of(utcnow())
    if first == "activation":
        async with held_save(engine, logged_in_client, change_interval) as save:  # active_config FOR UPDATE
            polling = background(poll_once(engine, fake_llm))
            wait = one_wait(await waits(migrated, polling), "active_config")
            assert wait.key == "1" and len(wait.blocking_pids) == 1  # process_item's first lock (step 2)
            for table, key in (
                ("jobs", target.job_id),
                ("score_targets", target.target_id),
                ("llm_budget", month),
                ("bulk_operations", submitted.setup.bulk_operation_id),
            ):
                assert await free(engine, table, key), table  # it took nothing of a later step
            await save.commit()
            version = save.version_id
        await settle(polling)
    else:
        async with holding(engine, _LOCK_RANKING) as blocker:  # its last lock (step 11)
            polling = background(poll_once(engine, fake_llm))
            wait = one_wait(await waits(migrated, polling), "ranking_state")
            assert blocker in wait.blocking_pids
            publisher = wait.pid  # an item's publish holds every lock but the last
            assert not await free(engine, "bulk_operations", submitted.setup.bulk_operation_id)
            saving = background(save_and_commit(engine, logged_in_client))
            found = await waits(migrated, polling, saving, count=2)
            wait = one_wait(found, "active_config")
            assert wait.key == "1" and wait.blocking_pids == (publisher,)  # the save's first lock
        _, version = await settle(polling, saving)
    assert await decisions(engine) == ["published"] * BATCH
    assert (await target_of(engine, target.target_id)).state == "published_accepted"
    assert (await active_row(engine)).settings_version_id == version  # the save went through


# ---- budget_release against score_now (Codex r1 #19, #20) ------------------------------------------------


@pytest.mark.parametrize("first", ["budget_release", "score_now"])
async def test_budget_release_and_score_now_never_wait_on_each_other_out_of_order(
    engine: AsyncEngine,
    migrated: PgServer,
    current_epoch: int,
    monkeypatch: pytest.MonkeyPatch,
    first: str,
) -> None:
    now = utcnow()
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        job = await aged_job(conn, now=now)
        target = await state_target(conn, job, tuple_id, "budget_deferred")

    async def score_now(conn: AsyncConnection) -> object:
        return await targets.score_now(conn, job_id=job.job_id, force=False, now=now, defer=defer_in_tx)

    if first == "budget_release":
        paused = PausePoint(targets.RELEASED_POINT)
        monkeypatch.setattr(targets, "afault_point", paused)
        release = background(run_reconciler("budget_release", engine, now=now, epoch=current_epoch))
        await asyncio.wait_for(paused.reached.wait(), WAIT_S)  # the month row and the released target held
        scoring = background(_in_transaction(engine, score_now))
        found = await waits(migrated, scoring)
        assert {w.relation for w in found} == {"score_targets"}  # never the month row (step 6)
        assert one_wait(found, "score_targets").key == str(target)
        paused.resume()
        assert await settle(release, scoring) == [1, targets.ScoreNow(target, "pending")]
    else:
        held = Held(engine, score_now)
        await held.holding()  # active_config FOR SHARE and the target row, uncommitted
        released = await asyncio.wait_for(
            run_reconciler("budget_release", engine, now=now, epoch=current_epoch), WAIT_S
        )
        assert released == 0 and lock_wait_details(migrated) == []  # it skipped the held target
        assert await held.commit() == targets.ScoreNow(target, "pending")
    row = await swept_of(engine, target)
    assert (row.state, row.override, row.lane) == ("pending", "score_now", "score_now")


async def _in_transaction(engine: AsyncEngine, work: Callable[[AsyncConnection], Awaitable[Any]]) -> Any:
    async with engine.begin() as conn:
        return await work(conn)


# ---- a takeover against a score_now request and against start_cycle ---------------------------------------


def claimant(lock: LockSession) -> WorkerIdentity:
    assert lock.backend_pid is not None
    return WorkerIdentity(uuid.uuid4(), "h", 2, lock.backend_pid, "v")


@pytest.mark.parametrize("first", ["takeover", "score_now"])
async def test_a_takeover_and_a_score_now_request_never_wait_on_each_other(
    engine: AsyncEngine,
    migrated: PgServer,
    current_epoch: int,
    worker_lock: LockSession,
    monkeypatch: pytest.MonkeyPatch,
    first: str,
) -> None:
    # Deviation 39: an API transaction stamps no epoch, so it takes no part of the worker-heartbeat fence
    claimed = await claimed_target(engine)  # an epoch-1 claim: the takeover's targets sweep has work
    now = utcnow()
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        job = await aged_job(conn, now=now)
        target = await state_target(conn, job, tuple_id, "pending")

    async def score_now(conn: AsyncConnection) -> object:
        return await targets.score_now(conn, job_id=job.job_id, force=False, now=now, defer=defer_in_tx)

    if first == "takeover":
        paused = PausePoint("takeover.before_epoch_write")
        monkeypatch.setattr(epoch_mod, "afault_point", paused)
        takeover = background(claim_epoch_and_sweep(engine, claimant(worker_lock)))
        await asyncio.wait_for(paused.reached.wait(), WAIT_S)  # the heartbeat row FOR UPDATE, held
        assert not await free(engine, "worker_heartbeat", 1)
        result = await asyncio.wait_for(_in_transaction(engine, score_now), WAIT_S)  # never waits
        assert result == targets.ScoreNow(target, "pending")
        paused.resume()
        (taken,) = await settle(takeover)
    else:
        held = Held(engine, score_now)
        await held.holding()  # active_config FOR SHARE and the target row, uncommitted
        taken = await asyncio.wait_for(claim_epoch_and_sweep(engine, claimant(worker_lock)), WAIT_S)
        assert lock_wait_details(migrated) == []
        assert await held.commit() == targets.ScoreNow(target, "pending")
    epoch, swept = taken
    assert epoch == 2 and swept["score_targets"] == 1  # the epoch-1 claim, never the Score now target
    assert (await target_of(engine, claimed.target_id)).state == "pending"
    assert (await swept_of(engine, target)).override == "score_now"


@pytest.mark.parametrize("first", ["takeover", "start_cycle"])
async def test_a_takeover_and_start_cycle_meet_at_the_heartbeat_fence_first(
    engine: AsyncEngine,
    migrated: PgServer,
    current_epoch: int,
    worker_lock: LockSession,
    monkeypatch: pytest.MonkeyPatch,
    first: str,
) -> None:
    await insert_targets(engine, 1, lane="score_now")  # a Score now start needs no bucket

    async def start(conn: AsyncConnection) -> int | None:
        return await cycles.start_cycle(conn, trigger="score_now", now=utcnow(), epoch=1, fake_llm=True)

    if first == "takeover":
        paused = PausePoint("takeover.before_epoch_write")
        monkeypatch.setattr(epoch_mod, "afault_point", paused)
        takeover = background(claim_epoch_and_sweep(engine, claimant(worker_lock)))
        await asyncio.wait_for(paused.reached.wait(), WAIT_S)
        starting = background(_in_transaction(engine, start))
        wait = one_wait(await waits(migrated, starting), "worker_heartbeat")
        assert wait.key == "1"  # step 1, its fence, before anything else
        assert await free(engine, "active_config", 1)  # it holds nothing of step 2
        paused.resume()
        await settle(takeover)
        with pytest.raises(FencedOut) as fenced:
            await asyncio.wait_for(starting, WAIT_S)
        assert fenced.value.worker_superseded
        async with engine.connect() as conn:
            assert (await conn.execute(sa.text("SELECT count(*) FROM score_cycles"))).scalar_one() == 0
    else:
        async with holding(engine, _LOCK_ACTIVE_CONFIG) as blocker:  # holds the start after its fence
            starting = background(_in_transaction(engine, start))
            wait = one_wait(await waits(migrated, starting), "active_config")
            assert blocker in wait.blocking_pids
            fenced_by = wait.pid
            takeover = background(claim_epoch_and_sweep(engine, claimant(worker_lock)))
            wait = one_wait(await waits(migrated, starting, takeover, count=2), "worker_heartbeat")
            assert wait.key == "1" and wait.blocking_pids == (fenced_by,)  # the takeover's first lock
        cycle_id, (epoch, swept) = await settle(starting, takeover)
        assert cycle_id is not None and epoch == 2 and swept["score_cycles"] == 1
        row = await cycle_of(engine, cycle_id)  # started under epoch 1, then swept by the takeover
        assert (row.status, row.status_reason) == ("failed", "orphaned")


# ---- prepare against an online fail of one of its candidates (Codex r2 B8) --------------------------------


async def run_fail(deps: ScoreDeps, claimed: ClaimedTarget, error_code: str) -> Any:
    state = initial_state(claimed.ref, claimed.attempt_id)
    state["error_code"] = error_code
    return await fail_mod.make_fail(deps)(state, await score_config(deps, claimed))


@pytest.mark.parametrize("first", ["prepare", "fail"])
async def test_prepare_and_an_online_fail_of_a_candidate_never_deadlock(
    engine: AsyncEngine,
    migrated: PgServer,
    gateway: LLMGateway,
    deps: ScoreDeps,
    monkeypatch: pytest.MonkeyPatch,
    first: str,
) -> None:
    # the fail of a denied reservation: the target (step 5), then the month row (step 6); the prepare: the
    # month and bulk rows, then its candidates `FOR UPDATE SKIP LOCKED` (Deviation 38's stated exception)
    setup = await bulk_with_targets(engine, 2)
    batched, failing = setup.targets
    month = month_of(utcnow())
    settings = await _settings_with(engine, batch_min_targets=1)
    if first == "prepare":
        paused = PausePoint(batches.AFTER_CANDIDATES)
        monkeypatch.setattr(batches, "afault_point", paused)
        preparing = background(prepared_submission(engine, gateway, setup, settings=settings))
        await asyncio.wait_for(paused.reached.wait(), WAIT_S)  # it read both candidates and holds the month
        claimed = await claim_again(engine, failing.target_id)  # a cycle claims one of its candidates
        failing_task = background(run_fail(deps, claimed, "budget_exhausted"))
        wait = month_wait(await waits(migrated, failing_task))  # step 6 after its target (step 5)
        assert wait.key in (None, month.isoformat())
        assert not await free(engine, "score_targets", failing.target_id)
        paused.resume()
        (prepared, _), _ = await settle(preparing, failing_task)
    else:
        claimed = await claim_again(engine, failing.target_id)
        async with holding(engine, _LOCK_RANKING) as blocker:  # the fail's last lock
            failing_task = background(run_fail(deps, claimed, "budget_exhausted"))
            assert blocker in one_wait(await waits(migrated, failing_task), "ranking_state").blocking_pids
            assert not await free(engine, "llm_budget", month)  # it holds its target and the month row
            preparing = background(prepared_submission(engine, gateway, setup, settings=settings))
            wait = month_wait(await waits(migrated, failing_task, preparing, count=2))
            assert wait.key in (None, month.isoformat())
            assert await free(engine, "score_targets", batched.target_id)  # no target before its month row
        _, (prepared, _) = await settle(failing_task, preparing)
    assert isinstance(prepared, batches.Prepared) and prepared.request_count == 1  # the failing one skipped
    assert (await target_of(engine, batched.target_id)).state == "in_batch"
    target = await target_of(engine, failing.target_id)
    assert (target.state, target.state_reason) == ("budget_deferred", "budget_exhausted")


async def test_prepare_never_waits_for_a_candidate_another_transaction_holds(
    engine: AsyncEngine, migrated: PgServer, gateway: LLMGateway
) -> None:
    # Deviation 38's stated exception: the preparation takes its candidates `FOR UPDATE SKIP LOCKED` after
    # the month and bulk rows, so a candidate whose row another transaction holds while it is still
    # `pending` (a sweep's or a user move's step-10 lock) is skipped, never waited for
    setup = await bulk_with_targets(engine, 2)
    batched, held_one = setup.targets
    settings = await _settings_with(engine, batch_min_targets=1)
    async with holding(engine, "SELECT 1 FROM score_targets WHERE id = :t FOR UPDATE", t=held_one.target_id):
        prepared, _ = await asyncio.wait_for(
            prepared_submission(engine, gateway, setup, settings=settings), WAIT_S
        )
        assert lock_wait_details(migrated) == []
    assert isinstance(prepared, batches.Prepared) and prepared.request_count == 1
    assert (await target_of(engine, batched.target_id)).state == "in_batch"
    assert (await target_of(engine, held_one.target_id)).state == "pending"


async def _settings_with(engine: AsyncEngine, **scoring: Any) -> Settings:
    """The active settings with `scoring` fields replaced, unvalidated (a direct `prepare` takes them)."""
    async with engine.connect() as conn:
        active = await active_settings(conn)
    return active.model_copy(update={"scoring": active.scoring.model_copy(update=scoring)})


# ---- the confirm's activate_tuple against a cycle's claim (Codex r1 #20) ----------------------------------


async def run_claim(deps: ScoreDeps, cycle_id: int, target_id: int) -> Any:
    """The `claim` node of a round admitting `target_id` (`claim_node.make_claim`, its own transaction)."""
    async with deps.engine.connect() as conn:
        active = await active_ids(conn)
        tuple_id = int(
            (
                await conn.execute(
                    sa.text("SELECT llm_tuple_id FROM score_cycles WHERE id = :c"), {"c": cycle_id}
                )
            ).scalar_one()
        )
    context = ScoreContext(
        cycle_id, 1, tuple_id, active.scope_version_id, active.evaluation_policy_id, deps, NoClaims()
    )
    state = {
        "current_round": [{"target_id": target_id, "lane": "similarity", "always_include_reason": None}],
        "claim_rounds": 0,
    }
    return await claim_node.make_claim(deps)(cast(CycleState, state), {"configurable": {"cycle": context}})


@pytest.mark.parametrize("first", ["activation", "claim"])
async def test_the_confirm_s_tuple_activation_and_a_cycle_s_claim_meet_at_active_config_first(
    engine: AsyncEngine, migrated: PgServer, deps: ScoreDeps, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    (pending,) = await insert_targets(engine, 1)
    async with engine.begin() as conn:
        cycle_id = await insert_cycle(conn, pending.llm_tuple_id)
    if first == "activation":
        paused = PausePoint(targets.AFTER_CANCEL_POINT)
        monkeypatch.setattr(targets, "afault_point", paused)
        confirming = background(confirm_new_profile(engine))
        await asyncio.wait_for(paused.reached.wait(), WAIT_S)  # active_config FOR UPDATE, the old targets
        claiming = background(run_claim(deps, cycle_id, pending.target_id))
        wait = one_wait(await waits(migrated, claiming), "active_config")
        assert wait.key == "1"  # step 2, after its fence alone
        paused.resume()
        _, update = await settle(confirming, claiming)
        assert update["claimed"] == []  # the superseded tuple's target was cancelled meanwhile
        target = await target_of(engine, pending.target_id)
        assert (target.state, target.state_reason) == ("cancelled", "tuple_superseded")
    else:
        async with holding(engine, _LOCK_RANKING) as blocker:  # the claim's last lock
            claiming = background(run_claim(deps, cycle_id, pending.target_id))
            wait = one_wait(await waits(migrated, claiming), "ranking_state")
            assert blocker in wait.blocking_pids
            claimer = wait.pid
            assert not await free(engine, "score_targets", pending.target_id)  # the claimed target, held
            confirming = background(confirm_new_profile(engine))
            wait = one_wait(await waits(migrated, claiming, confirming, count=2), "active_config")
            assert wait.key == "1" and wait.blocking_pids == (claimer,)  # the confirm's first lock
        update, _ = await settle(claiming, confirming)
        (ref,) = update["claimed"]
        target = await target_of(engine, pending.target_id)  # claimed, then cancelled by the activation
        assert (target.state, target.state_reason, target.claim_id) == ("cancelled", "tuple_superseded", None)
        async with engine.connect() as conn:
            attempt = (
                await conn.execute(
                    sa.text("SELECT outcome, outcome_detail FROM score_attempts WHERE target_id = :t"),
                    {"t": pending.target_id},
                )
            ).one()
        assert (attempt.outcome, attempt.outcome_detail, ref["attempt_no"]) == (
            "cancelled",
            "tuple_superseded",
            1,
        )


# ---- Carry T14-c: an owner's fail against node 9's cancellation -----------------------------------------


async def node_9_cancel(engine: AsyncEngine, job_id: int, revision_id: int) -> Any:
    """Node 9's transaction for a revision that stopped being current (`enqueue_targets`, step 3):
    `active_config FOR SHARE`, the job row `FOR SHARE` (step 4), then `targets.cancel_targets` (the bulk rows
    of the candidates' follow-up holds, then the target rows) and the ranking bump last. Its owner row (the
    run's) takes no part in the pair."""
    async with engine.begin() as conn:
        active = await cycles.lock_active_scoring(conn)
        assert active.llm_tuple_id is not None
        await targets.lock_jobs(conn, [job_id])
        cancelled = await targets.cancel_targets(
            conn,
            llm_tuple_id=active.llm_tuple_id,
            revision_ids=[revision_id],
            reason="revision_superseded",
            now=utcnow(),
        )
        await ranking.bump(conn)
        return cancelled


class Retried:
    """`DB_RETRY`'s rule on a direct call (the graph runs the `fail` node and node 9 under it): a retryable
    database error (a deadlock, 40P01) is tried again, up to three tries; the retries it took are kept."""

    def __init__(self) -> None:
        self.retried: list[str] = []

    async def run(self, name: str, work: Callable[[], Awaitable[Any]]) -> Any:
        for attempt in range(3):
            try:
                return await work()
            except Exception as exc:
                if attempt == 2 or not is_retryable_db_error(exc):
                    raise
                self.retried.append(
                    f"{name}:{pg_error(exc).sqlstate}" if isinstance(exc, sa.exc.DBAPIError) else name
                )
        raise AssertionError("unreachable")


async def run_publish(deps: ScoreDeps, claimed: ClaimedTarget, state: TargetState) -> Any:
    """The `publish` node itself, called as the subgraph calls it."""
    return await publish_node.make_publish(deps)(state, await score_config(deps, claimed))


@pytest.mark.parametrize("first", ["owner", "node_9"])
@pytest.mark.parametrize("owner_node", ["fail", "publish"])
async def test_an_owner_publish_or_fail_and_node_9_s_cancellation_never_deadlock_unrecoverably(
    engine: AsyncEngine,
    migrated: PgServer,
    deps: ScoreDeps,
    fake_llm: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
    owner_node: str,
    first: str,
) -> None:
    # Carry T14-c: the owner (its publish or its fail) takes the target row (step 5), then its follow-up
    # hold's bulk row (step 7); node 9, never the owner, takes the bulk row first (Ruling P52), then the
    # target row
    claimed = await claimed_target(engine)
    async with engine.begin() as conn:
        await activate_score_tuple(conn)
        operation, item = await followup_hold(
            conn, claimed.llm_tuple_id, claimed.target_id, hold=Decimal("0.05")
        )
    owner_work: Callable[[], Awaitable[Any]]
    if owner_node == "publish":
        fake_llm.answer("generate", claimed.job_revision_id, synthetic_c_draft())
        state = await state_at_terminal(deps, claimed, monkeypatch)  # an attempt that reaches `publish`
        owner_work = lambda: run_publish(deps, claimed, state)  # noqa: E731
    else:
        owner_work = lambda: run_fail(deps, claimed, "invalid_output")  # noqa: E731
    real_release = bulk.release_target_hold
    gate = asyncio.Event()
    paused: dict[str, asyncio.Task[Any] | None] = {"task": None}
    reached = asyncio.Event()

    async def held_release(conn: AsyncConnection, target_id: int) -> Decimal:
        if asyncio.current_task() is paused["task"] and not gate.is_set():
            reached.set()  # the target row is held; the bulk row comes next
            await gate.wait()
        return await real_release(conn, target_id)

    monkeypatch.setattr(bulk, "release_target_hold", held_release)
    retries = Retried()
    cancel_work = lambda: node_9_cancel(engine, claimed.job_id, claimed.job_revision_id)  # noqa: E731
    if first == "owner":
        owner = background(retries.run("owner", owner_work))
        paused["task"] = owner
        await asyncio.wait_for(reached.wait(), WAIT_S)  # the owner holds the target row
        cancelling = background(retries.run("node_9", cancel_work))
        wait = one_wait(await waits(migrated, owner, cancelling), "score_targets")
        assert wait.key == str(claimed.target_id)
        assert not await free(engine, "bulk_operations", operation)  # node 9 took the bulk row first
        gate.set()  # the owner now asks for the bulk row: the cycle PostgreSQL breaks
        await settle(owner, cancelling)
        assert retries.retried in (["owner:40P01"], ["node_9:40P01"])  # one side aborted, then retried
    else:
        cancelling = background(retries.run("node_9", cancel_work))
        paused["task"] = cancelling
        await asyncio.wait_for(reached.wait(), WAIT_S)  # node 9 holds the bulk row and the target row
        owner = background(retries.run("owner", owner_work))
        wait = one_wait(await waits(migrated, cancelling, owner), "score_targets")
        assert wait.key == str(claimed.target_id)  # the owner's step 5: it holds nothing else
        gate.set()
        await settle(cancelling, owner)
        assert retries.retried == []
    target = await target_of(engine, claimed.target_id)
    ended = await attempt_end(engine, claimed.attempt_id)
    # the outcome of the two in a serial order (L3431): cancelled first (the owner's retry finds its claim
    # lost and writes nothing); published first (left alone: only a published target is); failed first (its
    # attempt keeps `failed_invalid_output`, then the failed target of the superseded revision cancelled)
    if first == "node_9" or retries.retried == ["owner:40P01"]:
        assert (target.state, target.state_reason) == ("cancelled", "revision_superseded")
        assert (ended.outcome, ended.outcome_detail) == ("cancelled", "revision_superseded")
    elif owner_node == "publish":
        assert retries.retried == ["node_9:40P01"]
        assert (target.state, ended.outcome) == ("published_accepted", "published_accepted")
    else:
        assert retries.retried == ["node_9:40P01"]
        assert (target.state, target.state_reason) == ("cancelled", "revision_superseded")
        assert (ended.outcome, ended.outcome_detail) == ("failed_invalid_output", "invalid_output")
    released = await hold_of(engine, operation, item)  # given back exactly once
    assert released.followup_held_usd == 0 and released.hold_released_at is not None


# ---- Carry T16-c: abandon against node 9's cancellation of a batched target ------------------------------


@pytest.mark.parametrize("first", ["abandon", "node_9"])
async def test_abandon_and_node_9_s_cancellation_take_the_target_rows_before_the_attempts(
    engine: AsyncEngine, migrated: PgServer, gateway: LLMGateway, first: str
) -> None:
    setup = await bulk_with_targets(engine, 1)
    (target,) = setup.targets
    prepared, _ = await prepared_submission(
        engine, gateway, setup, settings=await _settings_with(engine, batch_min_targets=1)
    )
    assert isinstance(prepared, batches.Prepared)
    submission = prepared.submission_id
    async with engine.connect() as conn:
        attempt = (
            await conn.execute(
                sa.text("SELECT id FROM score_attempts WHERE target_id = :t AND mode = 'batch'"),
                {"t": target.target_id},
            )
        ).scalar_one()

    async def abandon(conn: AsyncConnection) -> bool:
        return await batches.abandon(conn, submission, now=utcnow())

    if first == "abandon":
        async with holding(engine, _LOCK_RANKING) as blocker:  # abandon's last lock
            abandoning = background(_in_transaction(engine, abandon))
            assert blocker in one_wait(await waits(migrated, abandoning), "ranking_state").blocking_pids
            assert not await free(engine, "score_targets", target.target_id)
            cancelling = background(node_9_cancel(engine, target.job_id, target.job_revision_id))
            wait = one_wait(await waits(migrated, abandoning, cancelling, count=2), "score_targets")
            assert wait.key == str(target.target_id)
            assert await free(engine, "score_attempts", attempt) is False  # abandon ended it; held
        abandoned, cancelled = await settle(abandoning, cancelling)
        assert abandoned is True and (cancelled.targets, cancelled.attempts) == (1, 0)
    else:
        held = Held(
            engine,
            lambda conn: targets.cancel_targets(
                conn,
                llm_tuple_id=setup.llm_tuple_id,
                revision_ids=[target.job_revision_id],
                reason="revision_superseded",
                now=utcnow(),
            ),
        )
        await held.holding()  # the target and its attempt cancelled, uncommitted
        abandoning = background(_in_transaction(engine, abandon))
        wait = one_wait(await waits(migrated, abandoning), "score_targets")
        assert wait.key == str(target.target_id) and wait.blocking_pids == (held.pid,)
        cancelled = await held.commit()
        (abandoned,) = await settle(abandoning)
        assert abandoned is True and (cancelled.targets, cancelled.attempts) == (1, 1)
    row = await target_of(engine, target.target_id)
    assert (row.state, row.state_reason) == ("cancelled", "revision_superseded")
    (submitted,) = await submission_rows(engine)
    assert submitted.state == "abandoned"


# ---- Carry T13-a (Task 22): an owner's publish or fail against a merge's cancellation ---------------------


async def merge_cancel(engine: AsyncEngine, from_job_id: int, into_job_id: int) -> int:
    """A standalone merge (`merge.merge_jobs`): `active_config FOR SHARE`, both job rows `FOR UPDATE` (step
    4), the bulk rows of the from-job's follow-up holds (step 7, Ruling P52), then its targets cancelled
    `job_merged` (step 10), and the ranking bump last. The jobs have no posting: nothing to recompute."""
    async with engine.begin() as conn:
        event = await merge.merge_jobs(
            conn,
            from_job_id=from_job_id,
            into_job_id=into_job_id,
            kind="user_confirmed_merge",
            evidence={},
            recompute=False,
        )
        await ranking.bump(conn)
        return event


@pytest.mark.parametrize("first", ["owner", "merge"])
@pytest.mark.parametrize("owner_node", ["fail", "publish"])
async def test_an_owner_publish_or_fail_and_a_merge_s_cancellation_never_deadlock_unrecoverably(
    engine: AsyncEngine,
    migrated: PgServer,
    deps: ScoreDeps,
    fake_llm: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
    owner_node: str,
    first: str,
) -> None:
    # the publish takes the job row FOR SHARE (step 1a) before its target row: the merge's FOR UPDATE meets
    # it there; the fail takes the target row (step 5), then the bulk row (step 7), as against node 9
    claimed = await claimed_target(engine)
    async with engine.begin() as conn:
        await activate_score_tuple(conn)
        operation, item = await followup_hold(
            conn, claimed.llm_tuple_id, claimed.target_id, hold=Decimal("0.05")
        )
        into = await insert_job(conn)
    owner_work: Callable[[], Awaitable[Any]]
    if owner_node == "publish":
        fake_llm.answer("generate", claimed.job_revision_id, synthetic_c_draft())
        state = await state_at_terminal(deps, claimed, monkeypatch)
        owner_work = lambda: run_publish(deps, claimed, state)  # noqa: E731
    else:
        owner_work = lambda: run_fail(deps, claimed, "invalid_output")  # noqa: E731
    real_release = bulk.release_target_hold
    gate = asyncio.Event()
    paused: dict[str, asyncio.Task[Any] | None] = {"task": None}
    reached = asyncio.Event()

    async def held_release(conn: AsyncConnection, target_id: int) -> Decimal:
        if asyncio.current_task() is paused["task"] and not gate.is_set():
            reached.set()  # the target row is held; the bulk row comes next (owner) or is held (merge)
            await gate.wait()
        return await real_release(conn, target_id)

    monkeypatch.setattr(bulk, "release_target_hold", held_release)
    retries = Retried()
    merge_work = lambda: merge_cancel(engine, claimed.job_id, into.job_id)  # noqa: E731
    met_at = "jobs" if owner_node == "publish" else "score_targets"
    met_key = str(claimed.job_id if owner_node == "publish" else claimed.target_id)
    if first == "owner":
        owner = background(retries.run("owner", owner_work))
        paused["task"] = owner
        await asyncio.wait_for(reached.wait(), WAIT_S)  # the owner holds the target row
        merging = background(retries.run("merge", merge_work))
        wait = one_wait(await waits(migrated, owner, merging), met_at)
        assert wait.key == met_key
        # the publish's job row stops the merge before its bulk row; past the fail, the merge took it
        assert await free(engine, "bulk_operations", operation) is (owner_node == "publish")
        gate.set()
        await settle(owner, merging)
        if owner_node == "publish":
            assert retries.retried == []
        else:
            assert retries.retried in (["owner:40P01"], ["merge:40P01"])
    else:
        merging = background(retries.run("merge", merge_work))
        paused["task"] = merging
        await asyncio.wait_for(reached.wait(), WAIT_S)  # the merge holds its job, bulk and target rows
        owner = background(retries.run("owner", owner_work))
        wait = one_wait(await waits(migrated, merging, owner), met_at)
        assert wait.key == met_key  # the owner's first lock it shares: it holds nothing the merge needs
        gate.set()
        await settle(merging, owner)
        assert retries.retried == []
    target = await target_of(engine, claimed.target_id)
    ended = await attempt_end(engine, claimed.attempt_id)
    # the outcome of the two in a serial order: merged first (the owner finds its claim lost and writes
    # nothing); published first (left alone: only a published target is); failed first (its attempt keeps
    # `failed_invalid_output`, then the failed target of the merged job cancelled)
    if first == "merge" or retries.retried == ["owner:40P01"]:
        assert (target.state, target.state_reason) == ("cancelled", "job_merged")
        assert (ended.outcome, ended.outcome_detail) == ("cancelled", "job_merged")
    elif owner_node == "publish":
        assert (target.state, ended.outcome) == ("published_accepted", "published_accepted")
    else:
        assert retries.retried == ["merge:40P01"]
        assert (target.state, target.state_reason) == ("cancelled", "job_merged")
        assert (ended.outcome, ended.outcome_detail) == ("failed_invalid_output", "invalid_output")
    released = await hold_of(engine, operation, item)  # given back exactly once
    assert released.followup_held_usd == 0 and released.hold_released_at is not None
    async with engine.connect() as conn:  # the merge itself always went through
        merged_into = await conn.execute(
            sa.text("SELECT merged_into_job_id FROM jobs WHERE id = :j"), {"j": claimed.job_id}
        )
        assert merged_into.scalar_one() == into.job_id
