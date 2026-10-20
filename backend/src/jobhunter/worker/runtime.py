"""What a task body needs from the worker process (spec §3.3), without any I/O.

`worker_main` (Task 13) composes a `WorkerRuntime` once the lock is held and the epoch is claimed, and hands
it to procrastinate as `additional_context={"runtime": runtime}`. Every task body reads it as
`context.additional_context["runtime"]`, so Tasks 10 to 12 can type their bodies before the process exists.

`request_shutdown` asks the process for its graceful stop from inside it, as SIGTERM does from outside: a task
body whose admission or claim the epoch fence refused (`EPOCH_SUPERSEDED`, ruling T24-a) proves that this
worker has been superseded (§3.3.1 item 5), so its loops must stop fetching.
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
    """Ask the worker process for its graceful stop (what SIGTERM does): `shutdown.coordinate` then stops
    admission and the procrastinate loops, which fetch no further job and let the running ones end.

    Logged once, `shutdown_requested` at `warning` with `reason`; a stop already requested is left as it is.
    """
    if state.requested.is_set():
        return
    log.warning("shutdown_requested", reason=reason)
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
