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
    # Only for the annotations: `shutdown` imports this module, and the M2 dependencies (Task 11) pull in the
    # SDK and modules that import the task blueprint, which imports this one.
    from jobhunter.graphs.profile.graph import ProfileGraphs
    from jobhunter.graphs.score.deps import ScoreDeps
    from jobhunter.graphs.score.graph import ScoreGraphs
    from jobhunter.llm.batches import BatchesClient
    from jobhunter.llm.gateway import LLMGateway
    from jobhunter.observability.deadman import DeadmanPinger
    from jobhunter.observability.ntfy import AlertDispatcher
    from jobhunter.profile.parse_client import ParseClient
    from jobhunter.profile.storage import CvStore
    from jobhunter.worker.operations import OperationDeps
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
    # the process's client, shared by the `embed` node, the `awaiting_embedding` and `profile_vectors`
    # reconcilers and the `embed_profile` handler (through `operation_deps`)
    embedder: EmbedderClient | None
    # M2 (Task 11): the process's gateway, docparse client, CV store and profile graphs, and the operation
    # dependencies `run_operation` hands to every execution (the same objects).
    gateway: "LLMGateway"
    parse_client: "ParseClient"
    cv_store: "CvStore"
    profile_graphs: "ProfileGraphs"
    operation_deps: "OperationDeps"
    # M3 (Task 11): the score graphs, compiled once (the cycle graph and the subgraph its wrapper invokes),
    # and their process-wide deps, which `score_cycle` runs every cycle on.
    score_graphs: "ScoreGraphs"
    score_deps: "ScoreDeps"
    # M3 (Task 27): the process's one Message Batches client, on the gateway's client, which `batch_poll` and
    # `batch_reconcile_ambiguous` use and `operation_deps` hands to the `batch_submit` handler.
    batches_client: "BatchesClient"
    # M5 (Task 4): the alert dispatcher `worker_main` runs beside the queue loops when `NTFY_TOPIC_URL` is
    # set; `None` without it (and for the runtimes tests build without one).
    dispatcher: "AlertDispatcher | None" = None
    # M5 (Task 5): the dead-man pinger, when both healthchecks secrets are set (`None` without them, and for
    # the runtimes tests build without one): `run_collect`'s `_finalize`, the reconcilers' `deadman_ping` and
    # `deadman_sync` use it, as the collection graph's `finalize` node does through its `CollectDeps`.
    deadman: "DeadmanPinger | None" = None
