"""`jobhunter worker` that freezes inside an open fenced transaction, for C17's deterministic case:
`python -m tests.crash.frozen_worker`.

A worker paused by SIGSTOP freezes every coroutine wherever it is. One of them may be inside a fenced
transaction, for example the aux queue's operation claim or a heavy reconciler (C17's zombie was found
frozen in both). Such a transaction holds the `worker_heartbeat` row `FOR KEY SHARE` (`epoch.fence_epoch`).
Its session stays `idle in transaction`, and the takeover's epoch claim, which locks that row `FOR UPDATE`,
waits for it. This child makes that case certain.

Its pause (`JOBHUNTER_FAULT=<point>:pause`) first opens a transaction of its own as the worker's role and runs
the fence's statement in it (`epoch._CURRENT_EPOCH`). Then it prints `{"event": "frozen_transaction_open",
"backend_pid": <pid>}` and stops itself as the fault point does. Nothing in the process ever ends that
transaction: the server's `idle_in_transaction_session_timeout` (§19.1, 60 s) does. Once resumed, the process
closes that connection and goes on as `jobhunter worker` would.
"""

import asyncio
import json
from typing import Literal

import psycopg
from jobhunter import faults
from jobhunter.config import startup_or_exit
from jobhunter.worker import epoch


def main() -> int:
    cfg = startup_or_exit("worker")
    dsn = cfg.dsn("psycopg")
    stop = faults._stop_the_process

    def stop_inside_a_fenced_transaction(kind: Literal["exit", "pause"]) -> None:
        if kind != "pause":
            stop(kind)
            return
        conn = psycopg.connect(dsn)  # not autocommit: the fence's statement opens the transaction
        try:
            conn.execute(epoch._CURRENT_EPOCH)
            event = {"event": "frozen_transaction_open", "backend_pid": conn.info.backend_pid}
            print(json.dumps(event), flush=True)
            stop(kind)
        finally:
            conn.close()

    faults._stop_the_process = stop_inside_a_fenced_transaction
    from jobhunter.worker.main import worker_main  # imported as `jobhunter worker` imports it

    return asyncio.run(worker_main(cfg))


if __name__ == "__main__":
    raise SystemExit(main())
