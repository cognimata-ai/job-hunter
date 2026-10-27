"""The heartbeat loop of an owned execution (spec §3.3.4): runs and operations beat every 30 s.

`heartbeat_loop` calls `beat` every `period_s`. `beat` is the owner's fenced heartbeat write (for example
`heartbeat_operation`), which returns `False` when it updated no row because another claim, attempt or epoch
owns the row now. On `False`, or when `beat` raises, the loop calls `on_lost()` once and returns: the owner
path (`executions.run_owned`) then stops the work and makes no further owner write.

**Tolerated failures** (M3 Task 10, the claim heartbeat of `graphs.score.claims`): with `tolerated_failures =
n`, up to `n` raising beats in a row are each logged and the loop goes on; the `n + 1`-th in a row is the
loss, and a good beat restarts the count. A `False` is the loss at once whatever `n`. The default, 0, is the
behaviour above: the first raising beat is the loss. A tolerating loop's failure log also names the beat
(`label`), the failures in a row and the tolerance.

This module is a task boundary (ruff `BLE001` is allowed here). The broad catch logs `heartbeat_failed` with
the exception's class only, never its message, and the outcome it leads to is persisted by the row's owner
path through `transition()`: the stale-heartbeat reclaim of an operation, or the new attempt of a run.
"""

import asyncio
from collections.abc import Awaitable, Callable

from jobhunter.observability.logging import get_logger

log = get_logger("jobhunter.worker.heartbeat")


async def heartbeat_loop(
    beat: Callable[[], Awaitable[bool]],
    *,
    period_s: float,
    on_lost: Callable[[], None],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    tolerated_failures: int = 0,
    label: str | None = None,
) -> None:
    """Beat every `period_s` (the first beat one period after the start) until a beat is refused or fails
    more than `tolerated_failures` times in a row (module docstring). `sleep` waits out each period
    (`asyncio.sleep`; a test injects its own clock, M3 Task 10); `label` names the beat in a tolerating
    loop's failure log.

    Cancelling the task that runs it stops it; `CancelledError` is no `Exception` and is never caught here.
    """
    if tolerated_failures < 0:
        raise ValueError("tolerated_failures counts from 0")
    failures = 0
    while True:
        await sleep(period_s)
        try:
            alive = await beat()
        except Exception as exc:
            failures += 1
            # Not the message: a database error can carry statement parameters.
            if tolerated_failures:
                log.warning(
                    "heartbeat_failed",
                    error_class=type(exc).__name__,
                    heartbeat=label,
                    consecutive=failures,
                    tolerated=tolerated_failures,
                )
            else:
                log.warning("heartbeat_failed", error_class=type(exc).__name__)
            if failures > tolerated_failures:
                on_lost()
                return
            continue
        failures = 0
        if not alive:
            on_lost()
            return
