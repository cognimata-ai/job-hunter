"""The collection graph (spec §3.3.3, §9.1, §9.2; spike V6) and the M0 no-op graph.

`CollectState` and `CollectUpdate` are defined in `state` and imported here, so `graph.CollectState` and the
`CollectGraph` alias name the one definition. A node's parameter is named `state`: LangGraph's node protocols
name it so, and `mypy --strict` matches the name.

`build_collect_graph` wires the §9.2 edges: `START → plan → fetch_primary`, a `Send("fetch_cell", …)` per
planned primary spec (`nodes.fetch.route_after_primary`, or straight on when there is none), `→ plan_fallback
→ [fetch_fallback → a fetch_cell Send per fallback spec | parse_normalize]`, then `parse_normalize →
resolve_identity → embed → hard_filters → enqueue_targets → finalize → END`; after a `fetch_cell`,
`route_after_cell` goes to `plan_fallback` in the primary phase and to `parse_normalize` in the fallback one.
Every node has its §9.2 timeout, `fetch_cell` its per-traversal one (`TIMEOUTS["fetch_cell"]`, which the
node applies itself from the moment the traversal holds its semaphore, to all of the cell's work after it),
so a Send's wait behind the traversals ahead of it never counts against it (a LangGraph timeout would time
that wait too). The nodes that
make no source or embedder call retry a serialization failure, a deadlock or a lost connection (`DB_RETRY`,
`is_retryable_db_error`), and nothing else.
`parse_normalize` is `nodes.parse_normalize` (Task 15), `resolve_identity` is `nodes.resolve_identity` (Task
18), `embed` is `nodes.embed` (Task 22), `hard_filters` is `nodes.hard_filters` (Task 16), `enqueue_targets`
is `nodes.enqueue_targets` and `finalize` is `nodes.finalize` (Task 23): the graph ends the run itself, in
`finalize`'s one owner-fenced transaction (after the fault point `collect.before_finalize`), and
`execute_run` finds that end in the database (plan Deviation 16). Every node is timed (`timed`, on a
monotonic clock): its seconds go to `node_durations`, which the run report shows.

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

The no-op graph is `start` → `finalize` (the module's own `finalize`, which only passes the fault point
`collect.before_finalize` and leaves the run's end to `execute_run`). It does no work; it proves the run
lifecycle around it (admission, heartbeat, fenced finalize) for the M0 tests.
"""

import time
from collections.abc import Callable, Mapping
from datetime import timedelta
from types import MappingProxyType
from typing import Final, Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import TimeoutPolicy

from jobhunter.embeddings import EmbedderError
from jobhunter.errors import DomainError
from jobhunter.faults import afault_point
from jobhunter.graphs.collect.deps import CollectDeps
from jobhunter.graphs.collect.nodes import Node
from jobhunter.graphs.collect.nodes.embed import NODE_TIMEOUT as EMBED_TIMEOUT
from jobhunter.graphs.collect.nodes.embed import make_embed
from jobhunter.graphs.collect.nodes.enqueue_targets import make_enqueue_targets
from jobhunter.graphs.collect.nodes.fetch import (
    CELL_TIMEOUT,
    make_fetch_cell,
    make_fetch_fallback,
    make_fetch_primary,
    route_after_cell,
    route_after_fallback,
    route_after_primary,
)
from jobhunter.graphs.collect.nodes.finalize import make_finalize
from jobhunter.graphs.collect.nodes.hard_filters import make_hard_filters
from jobhunter.graphs.collect.nodes.parse_normalize import make_parse_normalize
from jobhunter.graphs.collect.nodes.plan import make_plan
from jobhunter.graphs.collect.nodes.plan_fallback import make_plan_fallback, route
from jobhunter.graphs.collect.nodes.resolve_identity import make_resolve_identity
from jobhunter.graphs.collect.state import CollectState as CollectState
from jobhunter.graphs.collect.state import CollectUpdate as CollectUpdate
from jobhunter.graphs.collect.state import FetchCellInput, FetchOutcome
from jobhunter.graphs.sanitize import DB_RETRY as DB_RETRY
from jobhunter.graphs.sanitize import is_retryable_db_error as is_retryable_db_error
from jobhunter.graphs.sanitize import sanitized
from jobhunter.observability.logging import get_logger
from jobhunter.sources.base import SourceError

type CollectGraph = CompiledStateGraph[CollectState, None, CollectState, CollectState]

SOURCE_ERRORS: Final = "source_errors"  # the counter of source errors that reached a node's error handler
EMBEDDER_ERRORS: Final = "embedder_errors"  # the counter of embedder errors that reached it
_MINUTE: Final = timedelta(minutes=1)
# The §9.2 timeout of each node; a fetch node's is per traversal, so `fetch_cell` has it too (applied by the
# node itself from its semaphore on, `make_fetch_cell(cell_timeout=...)`, not as a LangGraph `TimeoutPolicy`).
TIMEOUTS: Final[Mapping[str, timedelta]] = MappingProxyType(
    {
        "plan": timedelta(seconds=60),
        "fetch_primary": 15 * _MINUTE,
        "fetch_cell": CELL_TIMEOUT,
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


# The errors of a collection node that reach LangGraph unchanged (`guarded`): their text is a code or ids.
# Every `DomainError` (`FencedOut`, which ends the attempt `lost`; `OutcomeConflict`, which `execute_run` ends
# `failed_invariant('I7')`; `InvariantViolation`, `TransitionConflict`), LangGraph's own control flow, and a
# fetch traversal's `TimeoutError` (§9.2's timeout, raised by the node itself). `asyncio.CancelledError` is
# no `Exception`, so it is never caught.
PASS_THROUGH: Final = (DomainError, GraphBubbleUp, TimeoutError)


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


def timed[S: Mapping[str, object]](
    name: str, node: _Node[S], *, clock: Callable[[], float] = time.monotonic
) -> _Node[S]:
    """`node` whose update also carries `node_durations: {name: seconds}`, the time it took on `clock`; the
    reducer adds up a node's runs (the `fetch_cell` Sends). A run that raises reports nothing: its error
    propagates, and a retry is timed on its own."""

    async def measured(state: S) -> CollectUpdate:
        started = clock()
        update = await node(state)
        timed_update: CollectUpdate = {**update, "node_durations": {name: clock() - started}}
        return timed_update

    return measured


def guarded[S: Mapping[str, object]](name: str, node: _Node[S]) -> _Node[S]:
    """`node` behind the content-free stand-in of its errors (`graphs.sanitize.sanitized`, final review
    FR-12): outside `error_handled`, so a typed error still becomes its persisted outcome first, and an error
    outside `PASS_THROUGH` reaches LangGraph, and so `checkpoint_writes`, as `NodeFailed` (logged
    `collect_node_failed` with its class). `DB_RETRY` retries the stand-in of a retryable database error as
    it did the original; the run ends as it did (`failed('bug')`)."""
    return sanitized(name, node, passing=PASS_THROUGH, log=log, event="collect_node_failed")


async def start(state: CollectState) -> CollectUpdate:
    return {"steps": ["start"]}


async def finalize(state: CollectState) -> CollectUpdate:
    """The no-op graph's `finalize`: the fault point only; `execute_run` ends the run."""
    await afault_point("collect.before_finalize")
    return {"steps": ["finalize"]}


def build_collect_graph(
    checkpointer: BaseCheckpointSaver[str], deps: CollectDeps, *, timer: Callable[[], float] = time.monotonic
) -> CollectGraph:
    """The §9.2 collection graph over `deps`, compiled with `checkpointer`; `timer` times its nodes (see the
    module docstring)."""
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
        "enqueue_targets": make_enqueue_targets(deps),
        "finalize": make_finalize(deps),
    }
    for name, node in nodes.items():
        graph.add_node(
            name,
            guarded(name, timed(name, error_handled(name, node), clock=timer)),
            timeout=TimeoutPolicy(run_timeout=TIMEOUTS[name]),
            retry_policy=None if name in IO_NODES else DB_RETRY,
        )
    cell = make_fetch_cell(deps, cell_timeout=TIMEOUTS["fetch_cell"])  # its own, from its semaphore on
    graph.add_node(  # so no TimeoutPolicy: it would time the wait for the semaphore too
        "fetch_cell",
        guarded("fetch_cell", timed("fetch_cell", error_handled("fetch_cell", cell), clock=timer)),
        input_schema=FetchCellInput,
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
