"""The heartbeat loop of an owned execution (spec §3.3.4): runs and operations beat every 30 s.

`heartbeat_loop` calls `beat` every `period_s`. `beat` is the owner's fenced heartbeat write (for example
`heartbeat_operation`), which returns `False` when it updated no row because another claim, attempt or epoch
owns the row now. On `False`, or when `beat` raises, the loop calls `on_lost()` once and returns: the owner
path (`executions.run_owned`) then stops the work and makes no further owner write.

This module is a task boundary (ruff `BLE001` is allowed here). The broad catch logs `heartbeat_failed` with
the exception's class only, never its message, and the outcome it leads to is persisted by the row's owner
path through `transition()`: the stale-heartbeat reclaim of an operation, or the new attempt of a run.
"""

import asyncio
from collections.abc import Awaitable, Callable

from jobhunter.observability.logging import get_logger

log = get_logger("jobhunter.worker.heartbeat")


async def heartbeat_loop(
    beat: Callable[[], Awaitable[bool]], *, period_s: float, on_lost: Callable[[], None]
) -> None:
    """Beat every `period_s` (the first beat one period after the start) until a beat fails or is refused.

    Cancelling the task that runs it stops it; `CancelledError` is no `Exception` and is never caught here.
    """
    while True:
        await asyncio.sleep(period_s)
        try:
            alive = await beat()
        except Exception as exc:
            # Not the message: a database error can carry statement parameters.
            log.warning("heartbeat_failed", error_class=type(exc).__name__)
            on_lost()
            return
        if not alive:
            on_lost()
            return
