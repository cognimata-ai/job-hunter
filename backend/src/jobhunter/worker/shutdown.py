"""The shutdown coordinator of the worker process (spec §3.3.5).

`coordinate` waits until the process is asked to stop (`ShutdownState.requested`, set by the SIGTERM and
SIGINT handlers) or one of the procrastinate worker loops ends by itself, whichever comes first. Then:
1. it stops admission (`admission_stopped`): graphs start no new dispatch from here on;
2. it starts the stop's one deadline (`StopDeadline`, `STOP_BUDGET_S` from now, in
   `ShutdownState.stop_deadline`);
3. it cancels every worker loop. A cancelled procrastinate loop stops fetching, waits up to `GRACE_S` for its
   running jobs and then cancels the rest;
4. it waits for the loops until `GRACE_S` after the stop began, so that at least `TEARDOWN_S` of the budget is
   left for the worker's teardown (`worker.main`), whose every wait takes at most what is left of it.

So the whole stop, up to the lock session's close, takes at most `STOP_BUDGET_S`: within Compose's
`stop_grace_period: 120s`, so the worker exits by itself and is never killed (137).

It rewrites no state: work that did not finish is resumed by the next worker's takeover sweep (§3.3.1).
"""

import asyncio
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from jobhunter.observability.logging import get_logger
from jobhunter.worker.runtime import ShutdownState

# The whole stop, from its start to the lock session's close; Compose's `stop_grace_period` is 120 s.
STOP_BUDGET_S: Final = 110
# How long a stopping procrastinate loop waits for its running jobs (`shutdown_graceful_timeout`, §3.3.5
# step 3); the coordinator waits for the loops just as long.
GRACE_S: Final = 100
# What the budget keeps for the teardown at least: cancelling what remains, the watchdog, the closes.
TEARDOWN_S: Final = STOP_BUDGET_S - GRACE_S

log = get_logger("jobhunter.worker.shutdown")


@dataclass(frozen=True)
class StopDeadline:
    """The one deadline of the stop: every wait of the stop takes at most what is left of it.

    `at` is a reading of `clock` (monotonic seconds); tests inject the clock.
    """

    at: float
    clock: Callable[[], float] = time.monotonic

    @classmethod
    def start(cls, clock: Callable[[], float] = time.monotonic) -> "StopDeadline":
        """The deadline of a stop that begins now: `STOP_BUDGET_S` from now."""
        return cls(at=clock() + STOP_BUDGET_S, clock=clock)

    def remaining(self, cap: float = math.inf) -> float:
        """The seconds left until the deadline, at most `cap`; 0 once it has passed."""
        return max(0.0, min(cap, self.at - self.clock()))


def _error_class(task: asyncio.Task[None]) -> str | None:
    """The class of the exception that ended `task`, `None` when it returned or was cancelled.

    Reading the exception also marks it retrieved, so asyncio does not report it again.
    """
    if task.cancelled():
        return None
    error = task.exception()
    return None if error is None else type(error).__name__


async def coordinate(
    worker_tasks: Sequence[asyncio.Task[None]],
    state: ShutdownState,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Run the shutdown of §3.3.5 once it is requested or a loop ends; 0 after a request, else 1.

    A loop that ends by itself (it returned or raised) is logged `worker_loop_ended` with its task name and
    error class, never a message. Loops still running `GRACE_S` after the stop began are logged
    `worker_loops_not_stopped` and left to the process exit. `state.stop_deadline` holds the stop's deadline
    (read from `clock`) for the teardown.
    """
    requested = asyncio.create_task(state.requested.wait(), name="jh-shutdown-requested")
    try:
        done, _ = await asyncio.wait({*worker_tasks, requested}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        requested.cancel()
    state.admission_stopped = True
    deadline = state.stop_deadline = StopDeadline.start(clock)
    was_requested = state.requested.is_set()
    ended = [task for task in worker_tasks if task in done] if not was_requested else []
    for task in ended:
        log.error("worker_loop_ended", worker=task.get_name(), error_class=_error_class(task))
    for task in worker_tasks:
        task.cancel()
    # Until `GRACE_S` after the stop began: the rest of the budget, `TEARDOWN_S` at least, is the teardown's.
    _, pending = await asyncio.wait(worker_tasks, timeout=max(0.0, deadline.remaining() - TEARDOWN_S))
    for task in worker_tasks:
        if task in ended or task in pending:
            continue
        if (error_class := _error_class(task)) is not None:
            log.warning("worker_loop_failed_while_stopping", worker=task.get_name(), error_class=error_class)
    if pending:
        log.error("worker_loops_not_stopped", workers=sorted(task.get_name() for task in pending))
    return 0 if was_requested else 1
