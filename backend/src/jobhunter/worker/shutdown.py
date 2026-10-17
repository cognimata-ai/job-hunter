"""The shutdown coordinator of the worker process (spec §3.3.5).

`coordinate` waits until the process is asked to stop (`ShutdownState.requested`, set by the SIGTERM and
SIGINT handlers) or one of the procrastinate worker loops ends by itself, whichever comes first. Then:
1. it stops admission (`admission_stopped`): graphs start no new dispatch from here on;
2. it cancels every worker loop. A cancelled procrastinate loop stops fetching, waits up to `GRACE_S` for its
   running jobs and then cancels the rest;
3. it waits for the loops, a little longer than `GRACE_S`.

It rewrites no state: work that did not finish is resumed by the next worker's takeover sweep (§3.3.1).
"""

import asyncio
from collections.abc import Sequence
from typing import Final

from jobhunter.observability.logging import get_logger
from jobhunter.worker.runtime import ShutdownState

# How long a stopping procrastinate loop waits for its running jobs (`shutdown_graceful_timeout`); within
# Compose's `stop_grace_period: 120s`.
GRACE_S: Final = 100
# How much longer than `GRACE_S` the coordinator waits for the loops to end after cancelling them.
_LOOP_END_MARGIN_S: Final = 10

log = get_logger("jobhunter.worker.shutdown")


def _error_class(task: asyncio.Task[None]) -> str | None:
    """The class of the exception that ended `task`, `None` when it returned or was cancelled.

    Reading the exception also marks it retrieved, so asyncio does not report it again.
    """
    if task.cancelled():
        return None
    error = task.exception()
    return None if error is None else type(error).__name__


async def coordinate(worker_tasks: Sequence[asyncio.Task[None]], state: ShutdownState) -> int:
    """Run the shutdown of §3.3.5 once it is requested or a loop ends; 0 after a request, else 1.

    A loop that ends by itself (it returned or raised) is logged `worker_loop_ended` with its task name and
    error class, never a message. Loops still running `GRACE_S` plus a margin after their cancellation are
    logged `worker_loops_not_stopped` and left to the process exit.
    """
    requested = asyncio.create_task(state.requested.wait(), name="jh-shutdown-requested")
    try:
        done, _ = await asyncio.wait({*worker_tasks, requested}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        requested.cancel()
    state.admission_stopped = True
    was_requested = state.requested.is_set()
    ended = [task for task in worker_tasks if task in done] if not was_requested else []
    for task in ended:
        log.error("worker_loop_ended", worker=task.get_name(), error_class=_error_class(task))
    for task in worker_tasks:
        task.cancel()
    _, pending = await asyncio.wait(worker_tasks, timeout=GRACE_S + _LOOP_END_MARGIN_S)
    for task in worker_tasks:
        if task in ended or task in pending:
            continue
        if (error_class := _error_class(task)) is not None:
            log.warning("worker_loop_failed_while_stopping", worker=task.get_name(), error_class=error_class)
    if pending:
        log.error("worker_loops_not_stopped", workers=sorted(task.get_name() for task in pending))
    return 0 if was_requested else 1
