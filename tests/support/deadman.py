"""The dead-man switch's database side in tests (M5 Task 5): scheduled runs, the `deadman_state` row, one pass
of the `deadman_ping` reconciler, the `deadman_sync` jobs and a procrastinate worker of `aux` that runs them.

Every run is inserted as the tick inserts one (a `queued` collection of the active configuration, or a manual
one queued earlier) and moved on by the code under test only; every slot is distinct (`runs_slot_uq`).
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Final

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.observability.deadman import DeadmanPinger
from jobhunter.worker import reconcilers, runs
from jobhunter.worker.reconcilers import ReconcileContext
from jobhunter.worker.runs import RunOwner
from jobhunter.worker.runtime import ShutdownState
from jobhunter.worker.tasks import defer_in_tx, job_task_name
from sqlalchemy.ext.asyncio import AsyncEngine

WAIT_S: Final = 60.0

_SCHEDULED: Final = sa.text(
    "INSERT INTO runs (trigger, scheduled_slot, status, include_paid, settings_version_id, scope_version_id, "
    "llm_tuple_id) SELECT :trigger, :slot, 'queued', false, settings_version_id, scope_version_id, "
    "llm_tuple_id FROM active_config RETURNING id"
)
# A manual collection queued `:ago` before the database's now (the window O18 reads is its queued_at).
_MANUAL_QUEUED_AGO: Final = sa.text(
    "INSERT INTO runs (trigger, scheduled_slot, status, include_paid, settings_version_id, scope_version_id, "
    "llm_tuple_id, queued_at) SELECT 'manual', NULL, 'queued', false, settings_version_id, scope_version_id, "
    "llm_tuple_id, now() - CAST(:ago AS interval) FROM active_config RETURNING id, queued_at"
)
_STATE: Final = sa.text(
    "SELECT applied_interval_s, applied_timeout_s, applied_grace_s, applied_paused, last_pinged_run_id, "
    "synced_at, last_error_code, next_try_at FROM deadman_state WHERE id = 1"
)
_SYNC_JOBS: Final = sa.text(
    "SELECT id, queue_name, queueing_lock, status FROM procrastinate_jobs WHERE task_name = :t ORDER BY id"
)


async def scheduled_run(engine: AsyncEngine, *, slot: datetime, trigger: str = "schedule") -> int:
    """A `queued` collection of `trigger` (`schedule` or `catch_up`) for `slot`, not admitted."""
    async with engine.begin() as conn:
        return int((await conn.execute(_SCHEDULED, {"trigger": trigger, "slot": slot})).scalar_one())


async def manual_queued_ago(engine: AsyncEngine, ago: str) -> tuple[int, datetime]:
    """A `queued` manual collection whose `queued_at` is `ago` (an interval) before now; its id and
    `queued_at`."""
    async with engine.begin() as conn:
        row = (await conn.execute(_MANUAL_QUEUED_AGO, {"ago": ago})).one()
    return int(row.id), row.queued_at


async def admitted(engine: AsyncEngine, run_id: int) -> RunOwner:
    """`run_id` admitted as attempt 1 by worker epoch 1 (`runs.start_attempt`)."""
    async with engine.begin() as conn:
        admission, owner = await runs.start_attempt(conn, run_id, 1, epoch=1)
    if admission != "started" or owner is None:
        raise RuntimeError(f"admitted: run {run_id} was not admitted ({admission})")
    return owner


async def deadman_state(engine: AsyncEngine) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(_STATE)).one()


async def tick(
    engine: AsyncEngine, queue: procrastinate.App, pinger: DeadmanPinger | None, *, now: datetime
) -> int:
    """One pass of the `deadman_ping` reconciler at `now` as worker epoch 1, with `pinger`."""
    _, reconcile = reconcilers.REGISTRY["deadman_ping"]
    ctx = ReconcileContext(engine=engine, queue=queue, defer=defer_in_tx, now=now, epoch=1, deadman=pinger)
    return await reconcile(ctx)


async def sync_jobs(engine: AsyncEngine) -> list[Any]:
    """The `deadman_sync` jobs, oldest first."""
    async with engine.connect() as conn:
        return list((await conn.execute(_SYNC_JOBS, {"t": job_task_name("deadman_sync")})).all())


def runtime_of(engine: AsyncEngine, pinger: DeadmanPinger | None) -> SimpleNamespace:
    """The `WorkerRuntime` fields the `deadman_sync` body reads, of worker epoch 1."""
    return SimpleNamespace(engine=engine, epoch=1, shutdown=ShutdownState(), deadman=pinger)


async def run_aux(app: procrastinate.App, runtime: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """A procrastinate worker of `aux` on `runtime`, as `worker_main` runs one, until no job is left. Its own
    periodic deferrer finds no task: a test defers what it runs."""
    with monkeypatch.context() as patched:
        patched.setattr(app.periodic_registry, "periodic_tasks", {})
        await asyncio.wait_for(
            app.run_worker_async(
                queues=["aux"],
                concurrency=1,
                wait=False,
                listen_notify=False,
                install_signal_handlers=False,
                additional_context={"runtime": runtime},
                name="test-aux",
            ),
            WAIT_S,
        )
