"""What a task body needs from the worker process (spec §3.3), without any I/O.

`worker_main` (Task 13) composes a `WorkerRuntime` once the lock is held and the epoch is claimed, and hands
it to procrastinate as `additional_context={"runtime": runtime}`. Every task body reads it as
`context.additional_context["runtime"]`, so Tasks 10 to 12 can type their bodies before the process exists.

`request_shutdown` is the one way to ask the process for its graceful stop: the SIGTERM and SIGINT handlers
call it from outside (the reason is the signal's name), and the worker itself once an epoch fence refused it
(`EPOCH_SUPERSEDED`, ruling T24-a): a task body whose admission or claim was refused, a fenced tick or
reconciler cadence, the start's cadence. That proves this worker has been superseded (§3.3.1 item 5), so its
loops must stop fetching.
"""

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import procrastinate
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.config import AppConfig
from jobhunter.embeddings import EmbedderClient
from jobhunter.observability.logging import get_logger

if TYPE_CHECKING:
    # Only for the annotation: `shutdown` imports this module.
    from jobhunter.worker.shutdown import StopDeadline


@dataclass
class ShutdownState:
    """Shutdown progress, shared by the signal handler and the task bodies.

    `admission_stopped` is true once the worker no longer admits new work; `requested` is set when the process
    was asked to shut down; `stop_deadline` is the stop's one deadline once the stop began
    (`shutdown.coordinate`).
    """

    admission_stopped: bool = False
    requested: asyncio.Event = field(default_factory=asyncio.Event)
    stop_deadline: "StopDeadline | None" = None


# The reason of the stop a superseded worker requests of itself (ruling T24-a).
EPOCH_SUPERSEDED: Final = "epoch_superseded"

log = get_logger("jobhunter.worker.runtime")


def request_shutdown(state: ShutdownState, *, reason: str) -> None:
    """Ask the worker process for its graceful stop: `shutdown.coordinate` then stops admission and the
    procrastinate loops, which fetch no further job and let the running ones end.

    Logged once, `shutdown_requested` with `reason` (one shape for every request): at `warning` for the stop
    a superseded worker requests of itself (`EPOCH_SUPERSEDED`), at `info` for an operator's signal (its
    name). A stop already requested is left as it is.
    """
    if state.requested.is_set():
        return
    if reason == EPOCH_SUPERSEDED:
        log.warning("shutdown_requested", reason=reason)
    else:
        log.info("shutdown_requested", reason=reason)
    state.requested.set()


@dataclass
class WorkerRuntime:
    cfg: AppConfig
    engine: AsyncEngine
    queue: procrastinate.App
    saver: AsyncPostgresSaver
    collect_graph: CompiledStateGraph[Any, Any, Any, Any]
    epoch: int
    shutdown: ShutdownState
    embedder: EmbedderClient | None  # the process's client (the `embed` node and the reconciler share it)
