import signal
import sys

import pytest
import sqlalchemy as sa
from jobhunter.worker.tasks import release_stalled_jobs

pytestmark = [pytest.mark.crash, pytest.mark.slow]


async def test_v2_killed_worker_job_is_released(engine, queue_app, migrated, proc_env, procs) -> None:
    p = procs.spawn([sys.executable, "-m", "tests.crash.stall_worker"], proc_env("worker", migrated))
    p.wait_event("stall_job_started", 30)  # acknowledgement from inside the job
    p.signal(signal.SIGKILL)
    assert p.exit_within(10) == -signal.SIGKILL
    async with engine.begin() as c:
        await c.execute(
            sa.text("UPDATE procrastinate_workers SET last_heartbeat = now() - interval '5 minutes'")
        )
    assert await release_stalled_jobs(queue_app) == 1
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT status FROM procrastinate_jobs"))).scalar_one() == "todo"
