"""The local execution registry and the owned-execution protocol (spec §3.3.3).

`ExecutionRegistry` keeps at most one live asyncio task per execution key (`("run", id)` or
`("operation", id)`), together with the token of the owner it runs for (a run's `attempt_no`, an operation's
`claim_id`). A finished task leaves the registry by itself. The worker's reconcilers use it to stop a local
execution whose row they took away (`cancel_and_await`), and the shutdown uses `cancel_all`.

`run_owned` is the one protocol by which an owner runs its work (Task 10 `execute_operation`, Task 12
`execute_run`): the work runs as the registry's task for the key, next to a heartbeat task whose loss cancels
it, and the outcome tells the owner what is left to write:
- `returned`: the work ended normally; the owner records its completion (fenced by its token);
- `lost`: the work was cancelled by someone other than the caller (a refused or failed heartbeat, a
  reconciler's `cancel_and_await`, the shutdown's `cancel_all`) or raised `FencedOut`. Ownership is gone or
  being taken away, so the owner makes **no** further owner write;
- `raised`: the work raised another exception, which the owner maps to its retry or failure path.

Cancellation never hides the caller's own: when the task that awaits (`cancel_and_await`, `cancel_all`,
`run_owned`) is cancelled itself, the `CancelledError` propagates. `run_owned` first cancels the work and
waits for it (bounded), so no work outlives its owner. Waiting is done with `asyncio.wait`, which never
raises the awaited task's own exception or cancellation, so neither can be mistaken for the caller's.
"""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Final, Literal

from jobhunter.errors import DomainError, FencedOut
from jobhunter.observability.logging import get_logger
from jobhunter.worker.heartbeat import heartbeat_loop

ExecKey = tuple[Literal["run", "operation"], int]

log = get_logger("jobhunter.worker.executions")


def _label(key: ExecKey) -> str:
    return f"{key[0]}:{key[1]}"


class AlreadyExecuting(DomainError):
    """A live local task already executes this key; the new work was not started (its coroutine is closed)."""

    def __init__(self, key: ExecKey) -> None:
        super().__init__(f"already executing: {_label(key)}")
        self.key = key


async def _settle(task: asyncio.Task[None], label: str, timeout_s: float, *, event: str) -> bool:
    """Wait up to `timeout_s` for the (cancelled) `task` to end; `False`, logged as `event`, if it did not.

    The caller's own cancellation propagates out of `asyncio.wait`.
    """
    done, _ = await asyncio.wait({task}, timeout=timeout_s)
    if not done:
        log.warning(event, key=label)
        return False
    return True


class ExecutionRegistry:
    """At most one live task per `ExecKey`, with the token of the owner it runs for."""

    def __init__(self) -> None:
        self._live: dict[ExecKey, tuple[object, asyncio.Task[None]]] = {}

    def get(self, key: ExecKey) -> tuple[object, asyncio.Task[None]] | None:
        """The token and the task executing `key`, or `None` when no live task does."""
        entry = self._live.get(key)
        if entry is None or entry[1].done():
            return None
        return entry

    def start(self, key: ExecKey, token: object, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Run `coro` as the task of `key` for the owner `token`.

        Raises `AlreadyExecuting`, having closed `coro` unstarted, when a live task already holds `key`.
        """
        if self.get(key) is not None:
            coro.close()
            raise AlreadyExecuting(key)
        task = asyncio.create_task(coro, name=f"execution:{_label(key)}")
        self._live[key] = (token, task)
        task.add_done_callback(lambda done: self._forget(key, done))
        return task

    def _forget(self, key: ExecKey, task: asyncio.Task[None]) -> None:
        entry = self._live.get(key)
        if entry is not None and entry[1] is task:  # never the entry of a later task for the same key
            del self._live[key]

    async def cancel_and_await(self, key: ExecKey, *, timeout_s: float = 30.0) -> bool:
        """Cancel the live task of `key` and wait up to `timeout_s` for it to end.

        `True` when a live task was cancelled and has ended. `False` when no live task holds `key`, or when
        the task ignores the cancellation past `timeout_s` (logged `execution_cancel_timeout` with the key
        only; it stays registered while it runs).
        """
        entry = self.get(key)
        if entry is None:
            return False
        task = entry[1]
        task.cancel()
        return await _settle(task, _label(key), timeout_s, event="execution_cancel_timeout")

    async def cancel_all(self, *, timeout_s: float = 30.0) -> int:
        """Cancel every live task and wait up to `timeout_s` for them; the number that ended in time.

        Each task still running after `timeout_s` is logged `execution_cancel_timeout` with its key only.
        """
        live = [(key, task) for key, (_, task) in list(self._live.items()) if not task.done()]
        if not live:
            return 0
        for _, task in live:
            task.cancel()
        done, _ = await asyncio.wait({task for _, task in live}, timeout=timeout_s)
        for key, task in live:
            if task not in done:
                log.warning("execution_cancel_timeout", key=_label(key))
        return len(done)


EXECUTIONS: Final = ExecutionRegistry()


@dataclass(frozen=True)
class ExecutionOutcome:
    """How an owned execution ended; `error` is set for `raised` only."""

    status: Literal["returned", "lost", "raised"]
    error: Exception | None = None


def _outcome(task: asyncio.Task[None]) -> ExecutionOutcome:
    if task.cancelled():
        return ExecutionOutcome("lost")
    error = task.exception()
    if error is None:
        return ExecutionOutcome("returned")
    if isinstance(error, FencedOut):
        return ExecutionOutcome("lost")
    if not isinstance(error, Exception):
        raise error  # KeyboardInterrupt, SystemExit: not an outcome of the work
    return ExecutionOutcome("raised", error)


async def run_owned(
    key: ExecKey,
    token: object,
    work: Coroutine[Any, Any, None],
    *,
    beat: Callable[[], Awaitable[bool]],
    period_s: float,
    registry: ExecutionRegistry = EXECUTIONS,
    cancel_timeout_s: float = 30.0,
) -> ExecutionOutcome:
    """Run `work` as the registry's task for `key` and `token`, with a heartbeat; how it ended.

    1. `registry.start(key, token, work)` (raises `AlreadyExecuting`, with `work` closed, before anything
       else starts), plus a heartbeat task named `heartbeat:<kind>:<id>` that runs `heartbeat_loop(beat,
       period_s=period_s)` and cancels the work when a beat is refused or fails.
    2. Wait for the work. Its cancellation by anyone but the caller, or a `FencedOut` from it, is `lost`;
       another exception is `raised` (never logged here with its message); a normal end is `returned`.
       When the caller itself is cancelled, the work is cancelled and awaited (up to `cancel_timeout_s`)
       and the `CancelledError` propagates.
    3. Always: the heartbeat task is cancelled and awaited (up to `cancel_timeout_s`) before this returns,
       so no beat of this owner runs once the caller makes its completion write.
    """
    task = registry.start(key, token, work)

    def lost() -> None:
        task.cancel()

    heartbeat = asyncio.create_task(
        heartbeat_loop(beat, period_s=period_s, on_lost=lost), name=f"heartbeat:{_label(key)}"
    )
    try:
        await asyncio.wait({task})
    except asyncio.CancelledError:
        # The caller is cancelled (for example the job, at shutdown): no work outlives its owner.
        task.cancel()
        if await _settle(task, _label(key), cancel_timeout_s, event="execution_cancel_timeout"):
            _retrieve(task, key)
        raise
    finally:
        heartbeat.cancel()
        await _settle(heartbeat, _label(key), cancel_timeout_s, event="heartbeat_cancel_timeout")
    outcome = _outcome(task)
    if outcome.status == "lost":
        log.info("execution_lost", key=_label(key))
    return outcome


def _retrieve(task: asyncio.Task[None], key: ExecKey) -> None:
    """Mark the ended `task`'s exception as retrieved, logging its class: the caller's cancellation wins."""
    if not task.cancelled() and (error := task.exception()) is not None:
        log.warning("execution_failed_while_cancelled", key=_label(key), error_class=type(error).__name__)
