"""A worker that stalls inside a job, for the V2 crash test (`python -m tests.crash.stall_worker`).

It starts like the worker (`startup_or_exit("worker")`: startup assertions, JSON logs on stdout), defers one
`jh_stall:stall` job on queue `aux` and runs a worker for that queue with a 1 s heartbeat. The job prints
`{"event": "stall_job_started"}` (the test's readiness acknowledgement) and sleeps for 600 s, so the test
kills the process while the job is in `doing`. The task is registered on an app of its own, not on the task
app of `jobhunter.worker.tasks`.
"""

import asyncio
import json
from typing import Final

import procrastinate
from jobhunter.config import startup_or_exit

STALL_TASK: Final = "jh_stall:stall"
STALL_S: Final = 600
HEARTBEAT_INTERVAL_S: Final = 1


def build_app(dsn: str) -> procrastinate.App:
    app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=dsn, min_size=1, max_size=4))

    @app.task(name=STALL_TASK, queue="aux")
    async def stall() -> None:
        print(json.dumps({"event": "stall_job_started"}), flush=True)
        await asyncio.sleep(STALL_S)

    return app


async def main() -> int:
    cfg = startup_or_exit("worker")
    app = build_app(cfg.dsn("psycopg"))
    async with app.open_async():
        await app.configure_task(STALL_TASK).defer_async()
        await app.run_worker_async(queues=["aux"], update_heartbeat_interval=HEARTBEAT_INTERVAL_S)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
