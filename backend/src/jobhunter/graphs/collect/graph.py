"""The collection graph (spec §3.3.3, §9.1, §9.2; spike V6) and the M0 no-op graph.

`CollectState` and `CollectUpdate` are defined in `state` and imported here, so `graph.CollectState` and the
`CollectGraph` alias name the one definition. A node's parameter is named `state`: LangGraph's node protocols
name it so, and `mypy --strict` matches the name.

`build_collect_graph` wires the §9.2 edges: `START → plan → fetch_primary`, a `Send("fetch_cell", …)` per
planned primary spec (`nodes.fetch.route_after_primary`, or straight on when there is none), `→ plan_fallback
→ [fetch_fallback → a fetch_cell Send per fallback spec | parse_normalize]`, then `parse_normalize →
resolve_identity → embed → hard_filters → enqueue_targets → finalize → END`; after a `fetch_cell`,
`route_after_cell` goes to `plan_fallback` in the primary phase and to `parse_normalize` in the fallback one.
Every node has its §9.2 timeout; the nodes that make no source or embedder call retry a serialization
failure, a deadlock or a lost connection (`DB_RETRY`, `is_retryable_db_error`), and nothing else.
`parse_normalize` is `nodes.parse_normalize` (Task 15), `resolve_identity` is `nodes.resolve_identity` (Task
18), `embed` is `nodes.embed` (Task 22) and `hard_filters` is `nodes.hard_filters` (Task 16);
`enqueue_targets` and `finalize` are pass-through functions here until their tasks wire them (`finalize`
keeps the fault point `collect.before_finalize`).

**Error handling (§9.1).** Every node runs inside `error_handled`, which turns a typed error that reached it
into a persisted outcome (the checkpointed update) and lets everything else propagate. A `SourceError` gives
a `failed` `FetchOutcome` for the fetch request the node was sent and the `source_errors` counter; an
`EmbedderError` (`bad_response` or `checksum`: the `embed` node handles `unavailable` itself and raised the
alert for either) the `embedder_errors` counter, its revisions left `awaiting_embedding`, so the run is
`partial` (§17.1). A `Bug` (anything unclassified) fails the run, `FencedOut` ends the attempt quietly, and
`OutcomeConflict`, raised by the reducer outside any node, reaches `execute_run`. It wraps the node rather
than using LangGraph's `error_handler=`: LangGraph 1.2.15 runs such a handler, but its async executor still
re-raises the handled error at the end of the run when the failing node ran next to others in its step (the
`fetch_cell` fan-out).

The no-op graph is `start` → `finalize`. It does no work; it proves the run lifecycle around it (admission,
heartbeat, fenced finalize) and gives the crash tests the fault point `collect.before_finalize`.
"""

from collections.abc import Mapping
from datetime import timedelta
from types import MappingProxyType
from typing import Final, Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import RetryPolicy, TimeoutPolicy
from sqlalchemy.exc import DBAPIError

from jobhunter.db.sql import is_serialization_failure
from jobhunter.embeddings import EmbedderError
from jobhunter.faults import afault_point
from jobhunter.graphs.collect.deps import CollectDeps
from jobhunter.graphs.collect.nodes import Node
from jobhunter.graphs.collect.nodes.embed import NODE_TIMEOUT as EMBED_TIMEOUT
from jobhunter.graphs.collect.nodes.embed import make_embed
from jobhunter.graphs.collect.nodes.fetch import (
    make_fetch_cell,
    make_fetch_fallback,
    make_fetch_primary,
    route_after_cell,
    route_after_fallback,
    route_after_primary,
)
from jobhunter.graphs.collect.nodes.hard_filters import make_hard_filters
from jobhunter.graphs.collect.nodes.parse_normalize import make_parse_normalize
from jobhunter.graphs.collect.nodes.plan import make_plan
from jobhunter.graphs.collect.nodes.plan_fallback import make_plan_fallback, route
from jobhunter.graphs.collect.nodes.resolve_identity import make_resolve_identity
from jobhunter.graphs.collect.state import CollectState as CollectState
from jobhunter.graphs.collect.state import CollectUpdate as CollectUpdate
from jobhunter.graphs.collect.state import FetchCellInput, FetchOutcome
from jobhunter.observability.logging import get_logger
from jobhunter.sources.base import SourceError

type CollectGraph = CompiledStateGraph[CollectState, None, CollectState, CollectState]

SOURCE_ERRORS: Final = "source_errors"  # the counter of source errors that reached a node's error handler
EMBEDDER_ERRORS: Final = "embedder_errors"  # the counter of embedder errors that reached it
_MINUTE: Final = timedelta(minutes=1)
# The §9.2 timeout of each node; a fetch node's is per traversal, so `fetch_cell` has it too.
TIMEOUTS: Final[Mapping[str, timedelta]] = MappingProxyType(
    {
        "plan": timedelta(seconds=60),
        "fetch_primary": 15 * _MINUTE,
        "fetch_cell": 15 * _MINUTE,
        "plan_fallback": timedelta(seconds=30),
        "fetch_fallback": 15 * _MINUTE,
        "parse_normalize": 10 * _MINUTE,
        "resolve_identity": 20 * _MINUTE,
        "embed": EMBED_TIMEOUT,  # 20 min: the node's own budget stops it before (`nodes.embed.BUDGET`)
        "hard_filters": 5 * _MINUTE,
        "enqueue_targets": 5 * _MINUTE,
        "finalize": timedelta(seconds=60),
    }
)
# The nodes that call a source or the embedder: a retry could repeat the call, so they have no RetryPolicy.
IO_NODES: Final = frozenset({"fetch_primary", "fetch_cell", "fetch_fallback", "embed"})

log = get_logger("jobhunter.graphs.collect")


def is_retryable_db_error(exc: BaseException) -> bool:
    """A serialization failure (40001), a deadlock (40P01) or a lost connection: the non-I/O retry (§9.1)."""
    return is_serialization_failure(exc) or (isinstance(exc, DBAPIError) and exc.connection_invalidated)


DB_RETRY: Final = RetryPolicy(
    max_attempts=3, initial_interval=0.5, backoff_factor=2, jitter=True, retry_on=is_retryable_db_error
)


class _Node[S](Protocol):
    async def __call__(self, state: S) -> CollectUpdate: ...


def error_handled[S: Mapping[str, object]](name: str, node: _Node[S]) -> _Node[S]:
    """`node` with the §9.1 error handler (see the module docstring): a `SourceError` becomes the update
    `{steps: [name], counters: {source_errors: 1}}`, plus a `failed` outcome for the fetch request the node
    was sent (`fetch_request_id`); an `EmbedderError` becomes `{steps: [name], counters: {embedder_errors:
    1}}`; every other exception propagates."""

    async def handled(state: S) -> CollectUpdate:
        try:
            return await node(state)
        except SourceError as err:  # the class, kind and code only: a message may carry content
            log.warning("node_source_error", node=name, error_kind=err.kind, error_code=err.code)
            update: CollectUpdate = {"steps": [name], "counters": {SOURCE_ERRORS: 1}}
            fetch_request_id = state.get("fetch_request_id")
            if isinstance(fetch_request_id, int):
                work_id = f"fr:{fetch_request_id}"
                failed: FetchOutcome = {"work_id": work_id, "status": "failed", "items": 0, "new_postings": 0}
                update["outcomes"] = {work_id: failed}
            return update
        except EmbedderError as err:  # the kind and code only, as for a source error
            log.warning("node_embedder_error", node=name, error_kind=err.kind, error_code=err.code)
            return {"steps": [name], "counters": {EMBEDDER_ERRORS: 1}}

    return handled


async def start(state: CollectState) -> CollectUpdate:
    return {"steps": ["start"]}


async def finalize(state: CollectState) -> CollectUpdate:
    await afault_point("collect.before_finalize")
    return {"steps": ["finalize"]}


def _pass_through(name: str) -> Node:
    """The node `name` until its task wires it: it records that it ran and changes nothing else."""

    async def node(state: CollectState) -> CollectUpdate:
        return {"steps": [name]}

    return node


def build_collect_graph(checkpointer: BaseCheckpointSaver[str], deps: CollectDeps) -> CollectGraph:
    """The §9.2 collection graph over `deps`, compiled with `checkpointer` (see the module docstring)."""
    graph = StateGraph(CollectState)
    nodes: dict[str, Node] = {
        "plan": make_plan(deps),
        "fetch_primary": make_fetch_primary(deps),
        "plan_fallback": make_plan_fallback(deps),
        "fetch_fallback": make_fetch_fallback(deps),
        "parse_normalize": make_parse_normalize(deps),
        "resolve_identity": make_resolve_identity(deps),
        "embed": make_embed(deps),
        "hard_filters": make_hard_filters(deps),
        "enqueue_targets": _pass_through("enqueue_targets"),
        "finalize": finalize,
    }
    for name, node in nodes.items():
        graph.add_node(
            name,
            error_handled(name, node),
            timeout=TimeoutPolicy(run_timeout=TIMEOUTS[name]),
            retry_policy=None if name in IO_NODES else DB_RETRY,
        )
    graph.add_node(
        "fetch_cell",
        error_handled("fetch_cell", make_fetch_cell(deps)),
        input_schema=FetchCellInput,
        timeout=TimeoutPolicy(run_timeout=TIMEOUTS["fetch_cell"]),
    )
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "fetch_primary")
    graph.add_conditional_edges("fetch_primary", route_after_primary, ["fetch_cell", "plan_fallback"])
    graph.add_conditional_edges("fetch_cell", route_after_cell, ["plan_fallback", "parse_normalize"])
    graph.add_conditional_edges("plan_fallback", route, ["fetch_fallback", "parse_normalize"])
    graph.add_conditional_edges("fetch_fallback", route_after_fallback, ["fetch_cell", "parse_normalize"])
    for before, after in (
        ("parse_normalize", "resolve_identity"),
        ("resolve_identity", "embed"),
        ("embed", "hard_filters"),
        ("hard_filters", "enqueue_targets"),
        ("enqueue_targets", "finalize"),
    ):
        graph.add_edge(before, after)
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


def build_noop_collect_graph(checkpointer: BaseCheckpointSaver[str]) -> CollectGraph:
    """The no-op collection graph, `start` → `finalize`, compiled with `checkpointer`."""
    graph = StateGraph(CollectState)
    graph.add_node("start", start)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "start")
    graph.add_edge("start", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)
