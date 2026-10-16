import pytest
import sqlalchemy as sa
from jobhunter.worker.tasks import defer_in_tx, delete_finished_jobs, job_task_name, live_job_exists

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
