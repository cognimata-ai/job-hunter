"""M1 Carry-forward 17 (plan Task 23): the daily retention deletes a large backlog in bounded batches, each a
fenced transaction of its own, so the fence never holds a takeover back for long.

The threads are a finished run's 1,200 attempts, each with one `checkpoints` row (the saver deletes a thread
by its id, whatever its rows hold); the jobs are 2,500 finished procrastinate jobs. A takeover is simulated as
the worker's claim takes the row: `FOR UPDATE` on `worker_heartbeat`, then the epoch increment.
"""

from datetime import timedelta
from typing import Any, Final

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.sql import LOCK_NOT_AVAILABLE, pg_error
from jobhunter.errors import FencedOut
from jobhunter.graphs.checkpointer import open_saver
from jobhunter.privacy import retention
from jobhunter.privacy.retention import JOB_BATCH, RULES, THREAD_BATCH, run_retention_daily
from jobhunter.profile.storage import CvStore
from jobhunter.worker.epoch import fence_epoch
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.logs import logged

pytestmark = pytest.mark.integration

THREADS: Final = 1200
JOBS: Final = 2500
CURRENT_EPOCH: Final = 1

_RUN: Final = sa.text(
    "INSERT INTO runs (trigger, status, include_paid, settings_version_id, scope_version_id, attempt_no, "
    "queued_at, started_at, finished_at) SELECT 'manual', 'succeeded', false, settings_version_id, "
    "scope_version_id, :n, now() - interval '20 days', now() - interval '20 days', "
    "now() - interval '15 days' FROM active_config RETURNING id"
)
_ATTEMPTS: Final = sa.text(
    "INSERT INTO run_attempts (run_id, attempt_no, worker_epoch, thread_id) "
    "SELECT :r, n, 1, 'run-' || :r || '-a' || n FROM generate_series(1, :n) AS n"
)
_CHECKPOINTS: Final = sa.text(
    "INSERT INTO checkpoints (thread_id, checkpoint_id, checkpoint) "
    "SELECT thread_id, '1', '{}' FROM run_attempts WHERE run_id = :r"
)
_THREADS_LEFT: Final = sa.text("SELECT count(DISTINCT thread_id) FROM checkpoints")
_TAKEOVER: Final = (
    sa.text("SELECT 1 FROM worker_heartbeat WHERE id = 1 FOR UPDATE"),
    sa.text("UPDATE worker_heartbeat SET worker_epoch = worker_epoch + 1 WHERE id = 1"),
)


@pytest.fixture
def cv_store(cv_store_dir: Any) -> CvStore:
    return CvStore(cv_store_dir)


async def ended_run_threads(engine: AsyncEngine, n: int) -> None:
    """A run that ended 15 days ago with `n` attempts, each attempt's thread with a checkpoint row."""
    async with engine.begin() as c:
        run_id = int((await c.execute(_RUN, {"n": n})).scalar_one())
        await c.execute(_ATTEMPTS, {"r": run_id, "n": n})
        await c.execute(_CHECKPOINTS, {"r": run_id})


async def threads_left(engine: AsyncEngine) -> int:
    async with engine.connect() as c:
        return int((await c.execute(_THREADS_LEFT)).scalar_one())


async def take_over(engine: AsyncEngine) -> None:
    async with engine.begin() as c:
        for statement in _TAKEOVER:
            await c.execute(statement)


def counting_fences(monkeypatch: pytest.MonkeyPatch, before: dict[int, Any] | None = None) -> list[int]:
    """`retention.fence_epoch` counting its calls (one per transaction); `before[n]`, when given, runs just
    before the n-th call (1-based)."""
    calls: list[int] = []

    async def fence(conn: Any, epoch: int) -> None:
        calls.append(epoch)
        if before is not None and len(calls) in before:
            await before[len(calls)]()
        await fence_epoch(conn, epoch)

    monkeypatch.setattr(retention, "fence_epoch", fence)
    return calls


async def test_1200_threads_take_three_fenced_transactions(
    engine, current_epoch, migrated, monkeypatch
) -> None:
    """500, 500 and 200 threads: three batches, each holding the fence while the saver deletes it (a claim's
    `FOR UPDATE` on the row cannot be taken meanwhile)."""
    assert THREAD_BATCH == 500
    await ended_run_threads(engine, THREADS)
    fences = counting_fences(monkeypatch)
    refused: list[str] = []
    async with open_saver(migrated.dsn("app")) as saver:
        real_delete = saver.adelete_thread

        async def delete(thread: str) -> None:
            if not refused:  # the first thread of the first batch: the fence is held
                try:
                    async with engine.begin() as c:
                        await c.execute(
                            sa.text("SELECT 1 FROM worker_heartbeat WHERE id = 1 FOR UPDATE NOWAIT")
                        )
                except sa.exc.DBAPIError as exc:
                    refused.append(pg_error(exc).sqlstate or "")
            await real_delete(thread)

        monkeypatch.setattr(saver, "adelete_thread", delete)
        deleted = await retention._purge_threads(engine, saver, utcnow() - timedelta(days=14), CURRENT_EPOCH)
    assert deleted == THREADS
    assert fences == [CURRENT_EPOCH] * 3
    assert refused == [LOCK_NOT_AVAILABLE]
    assert await threads_left(engine) == 0


async def test_a_takeover_between_batches_stops_the_rule_and_the_next_run_resumes(
    engine, current_epoch, queue_app, migrated, cv_store, monkeypatch, caplog
) -> None:
    """The takeover commits after the first batch: the second batch's fence refuses the old epoch, so the
    rule stops after the batch in flight (500 threads gone, `FencedOut`, `retention_fenced`); the new
    worker's next pass deletes the other 700."""
    await ended_run_threads(engine, THREADS)
    monkeypatch.setattr(retention, "RULES", tuple(rule for rule in RULES if rule.name == "checkpoints"))

    async def takeover() -> None:
        await take_over(engine)

    counting_fences(monkeypatch, before={2: takeover})
    async with open_saver(migrated.dsn("app")) as saver:
        with pytest.raises(FencedOut) as fenced:
            await run_retention_daily(
                engine, queue_app, saver, cv_store=cv_store, now=utcnow(), epoch=CURRENT_EPOCH
            )
        assert fenced.value.owner == "worker_epoch"
        assert await threads_left(engine) == THREADS - THREAD_BATCH
        assert [e["rule"] for e in logged(caplog, "retention_fenced")] == ["checkpoints"]
        monkeypatch.setattr(retention, "fence_epoch", fence_epoch)  # the new worker's pass, uncounted
        report = await run_retention_daily(
            engine, queue_app, saver, cv_store=cv_store, now=utcnow(), epoch=CURRENT_EPOCH + 1
        )
    assert report == {"checkpoints": THREADS - THREAD_BATCH}
    assert await threads_left(engine) == 0


_JOBS: Final = sa.text(
    "INSERT INTO procrastinate_jobs (queue_name, task_name, status, args) "
    "SELECT 'tick', 'jh:retention_daily', 'succeeded', '{}' FROM generate_series(1, :n)"
)
_AGE_EVENTS: Final = sa.text(
    "INSERT INTO procrastinate_events (job_id, type, at) "
    "SELECT id, 'succeeded', now() - interval '8 days' FROM procrastinate_jobs"
)


async def test_2500_finished_jobs_take_three_fenced_transactions(
    engine, current_epoch, queue_app, monkeypatch
) -> None:
    assert JOB_BATCH == 1000
    async with engine.begin() as c:
        await c.execute(_JOBS, {"n": JOBS})  # inserted as ended: no `deferred` event of procrastinate's
        await c.execute(_AGE_EVENTS)
    fences = counting_fences(monkeypatch)
    deleted = await retention._delete_finished_jobs(engine, queue_app, timedelta(days=7), CURRENT_EPOCH)
    assert deleted == JOBS
    assert fences == [CURRENT_EPOCH] * 3
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM procrastinate_jobs"))).scalar_one() == 0
