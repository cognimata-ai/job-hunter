from typing import Any

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.worker.tasks import (
    defer_in_tx,
    delete_finished_jobs,
    job_task_name,
    live_job_exists,
    make_queue_app,
    release_stalled_jobs,
)

pytestmark = pytest.mark.integration
COUNT = sa.text("SELECT count(*) FROM procrastinate_jobs WHERE task_name = :t")


async def test_v1_rollback_leaves_no_job(engine, queue_app) -> None:
    async with engine.connect() as c:
        tx = await c.begin()
        assert await defer_in_tx(
            c, "run_collect", lock="collect", queueing_lock="run:1:1", run_id=1, attempt_no=1
        )
        await tx.rollback()
        assert (await c.execute(COUNT, {"t": job_task_name("run_collect")})).scalar_one() == 0


async def test_v1_commit_leaves_exactly_one_job(engine, queue_app) -> None:
    async with engine.begin() as c:
        await defer_in_tx(c, "run_collect", lock="collect", queueing_lock="run:1:1", run_id=1, attempt_no=1)
    async with engine.connect() as c:
        assert (await c.execute(COUNT, {"t": "jh:run_collect"})).scalar_one() == 1
        assert await live_job_exists(c, "run_collect", args_match={"run_id": 1})
        assert not await live_job_exists(c, "run_collect", args_match={"run_id": 2})


async def test_already_enqueued_keeps_caller_transaction_usable(engine, queue_app) -> None:
    async with engine.begin() as c:
        assert await defer_in_tx(c, "run_operation", queueing_lock="op:7", operation_id=7) is not None
        assert await defer_in_tx(c, "run_operation", queueing_lock="op:7", operation_id=7) is None
        assert (await c.execute(sa.text("SELECT 1"))).scalar_one() == 1  # still usable
    async with engine.connect() as c:
        assert (await c.execute(COUNT, {"t": "jh:run_operation"})).scalar_one() == 1


async def test_delete_finished_jobs_keeps_recent(engine, queue_app) -> None:
    async with engine.begin() as c:
        await defer_in_tx(c, "retention_daily", timestamp=1)
        await defer_in_tx(c, "retention_daily", timestamp=2)
        await c.execute(sa.text("UPDATE procrastinate_jobs SET status='doing'"))
        await c.execute(sa.text("UPDATE procrastinate_jobs SET status='succeeded'"))
        await c.execute(
            sa.text(
                "UPDATE procrastinate_events SET at = now() - interval '8 days' "
                "WHERE job_id = (SELECT min(id) FROM procrastinate_jobs)"
            )
        )
    await delete_finished_jobs(queue_app, older_than_hours=168)
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM procrastinate_jobs"))).scalar_one() == 1


async def test_defer_in_tx_needs_no_open_app(engine, make_config) -> None:
    app = make_queue_app(make_config("worker").dsn("psycopg"))
    with pytest.raises(procrastinate.exceptions.AppNotOpen):  # the app's own pool is not open
        await app.job_manager.check_connection_async()
    async with engine.begin() as c:
        assert await defer_in_tx(c, "run_operation", operation_id=1) is not None
    async with engine.connect() as c:
        assert (await c.execute(COUNT, {"t": "jh:run_operation"})).scalar_one() == 1
    with pytest.raises(procrastinate.exceptions.AppNotOpen):  # and deferring did not open it
        await app.job_manager.check_connection_async()


def _logged(caplog, event: str) -> list[dict[str, Any]]:
    return [rec.msg for rec in caplog.records if isinstance(rec.msg, dict) and rec.msg["event"] == event]


async def _statuses(engine) -> dict[int, str]:
    async with engine.connect() as c:
        rows = await c.execute(sa.text("SELECT id, status FROM procrastinate_jobs"))
        return {row.id: row.status for row in rows}


@pytest.mark.parametrize("reverse", [False, True])
async def test_stalled_job_with_a_taken_queueing_lock_is_failed_and_the_rest_are_released(
    engine, queue_app, caplog, monkeypatch, reverse
) -> None:
    async with engine.begin() as c:
        # A dead worker's tick (doing) and the next tick, already waiting under the same queueing lock, plus
        # an unrelated stalled operation. No worker row: a doing job without a worker is stalled.
        superseded = await defer_in_tx(c, "tick", queueing_lock="tick", timestamp=1)
        await c.execute(sa.text("UPDATE procrastinate_jobs SET status = 'doing'"))
        waiting = await defer_in_tx(c, "tick", queueing_lock="tick", timestamp=2)
        operation = await defer_in_tx(c, "run_operation", operation_id=5)
        await c.execute(
            sa.text("UPDATE procrastinate_jobs SET status = 'doing' WHERE id = :i"), {"i": operation}
        )
    # The order in which the manager lists the two stalled jobs is not defined: neither may block the other.
    listed = queue_app.job_manager.get_stalled_jobs

    async def listing_in_order(*args, **kwargs):
        jobs = sorted(await listed(*args, **kwargs), key=lambda job: job.id)
        return jobs[::-1] if reverse else jobs

    monkeypatch.setattr(queue_app.job_manager, "get_stalled_jobs", listing_in_order)
    assert await release_stalled_jobs(queue_app) == 1
    assert await _statuses(engine) == {superseded: "failed", waiting: "todo", operation: "todo"}
    assert [(m["job_id"], m["task_name"]) for m in _logged(caplog, "stalled_job_superseded")] == [
        (superseded, "jh:tick")
    ]
    assert [(m["job_id"], m["task_name"]) for m in _logged(caplog, "stalled_job_released")] == [
        (operation, "jh:run_operation")
    ]


async def test_job_that_finished_after_the_listing_is_skipped(engine, queue_app, monkeypatch, caplog) -> None:
    async with engine.begin() as c:
        job_id = await defer_in_tx(c, "run_operation", operation_id=6)
        await c.execute(sa.text("UPDATE procrastinate_jobs SET status = 'doing'"))
    listed = queue_app.job_manager.get_stalled_jobs

    async def listing_then_finish(*args, **kwargs):
        jobs = list(await listed(*args, **kwargs))
        async with engine.begin() as c:  # the worker was only slow: its job ends after being listed
            await c.execute(sa.text("UPDATE procrastinate_jobs SET status = 'succeeded'"))
        return jobs

    monkeypatch.setattr(queue_app.job_manager, "get_stalled_jobs", listing_then_finish)
    assert await release_stalled_jobs(queue_app) == 0  # no error: nothing left to release
    assert await _statuses(engine) == {job_id: "succeeded"}
    assert [(m["job_id"], m["task_name"]) for m in _logged(caplog, "stalled_job_not_retryable")] == [
        (job_id, "jh:run_operation")
    ]
