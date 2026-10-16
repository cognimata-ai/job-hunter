"""Reconcilers (spec §17.4): idempotent repairs that run on a cadence and make durable intents come true.

A reconciler is an async function of a `ReconcileContext` that returns how many rows it repaired. It opens its
own transactions on `ctx.engine`. `run_reconcilers` runs those registered for a cadence, in registration
order, and isolates them from each other: a reconciler that raises is recorded as `-1` and as the open alert
`reconciler.failed` (subject: its name), written in a transaction of its own, and the others still run. The
alert and the log line carry the reconciler's name and the exception's class only, never its message. When
the reconciler succeeds again, the alert is resolved.

This module is a task boundary (ruff `BLE001` is allowed here), and the catch persists an outcome (§17.1).

`stalled_jobs` is registered here; the tasks that own the other repairs register theirs.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import procrastinate
from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.observability.alerts import raise_alert, resolve_alert
from jobhunter.observability.logging import get_logger
from jobhunter.worker.tasks import Deferrer, release_stalled_jobs

Cadence = Literal["start", "tick", "heavy", "daily"]

FAILED_ALERT_CODE = "reconciler.failed"

log = get_logger("jobhunter.worker.reconcilers")


@dataclass(frozen=True)
class ReconcileContext:
    engine: AsyncEngine
    queue: procrastinate.App
    defer: Deferrer
    now: datetime
    epoch: int


ReconcilerFn = Callable[[ReconcileContext], Awaitable[int]]

# Insertion ordered: reconcilers run in registration order.
REGISTRY: dict[str, tuple[frozenset[Cadence], ReconcilerFn]] = {}


def reconciler(name: str, *cadences: Cadence) -> Callable[[ReconcilerFn], ReconcilerFn]:
    """Decorator: register the function under `name` to run at each of `cadences`."""

    def register(fn: ReconcilerFn) -> ReconcilerFn:
        registered = REGISTRY.get(name)
        if registered is not None and registered[1] is not fn:
            raise ValueError(f"another reconciler is already registered as {name!r}")
        REGISTRY[name] = (frozenset(cadences), fn)
        return fn

    return register


async def _persist_failure(ctx: ReconcileContext, name: str, exc: Exception) -> None:
    """Raise (or count one more occurrence of) the alert for `name`, in a transaction of its own."""
    error_class = type(exc).__name__
    async with ctx.engine.begin() as conn:
        await raise_alert(
            conn,
            code=FAILED_ALERT_CODE,
            severity="warn",
            subject=name,
            message=f"reconciler {name} failed",
            context={"reconciler": name, "error_class": error_class},
        )
    # Not the exception's message and not its traceback: either may carry content.
    log.error("reconciler_failed", reconciler=name, error_class=error_class)


async def run_reconcilers(ctx: ReconcileContext, cadence: Cadence) -> dict[str, int]:
    """Run the reconcilers of `cadence`; the repaired count of each by name, `-1` for one that failed."""
    counts: dict[str, int] = {}
    for name, (cadences, fn) in list(REGISTRY.items()):
        if cadence not in cadences:
            continue
        try:
            counts[name] = await fn(ctx)
        except Exception as exc:
            await _persist_failure(ctx, name, exc)
            counts[name] = -1
            continue
        async with ctx.engine.begin() as conn:
            await resolve_alert(conn, code=FAILED_ALERT_CODE, subject=name)
    return counts


@reconciler("stalled_jobs", "start", "tick")
async def stalled_jobs(ctx: ReconcileContext) -> int:
    """Release the jobs of dead workers: the one stalled-job path (spike V2), at start and on every tick."""
    return await release_stalled_jobs(ctx.queue)
