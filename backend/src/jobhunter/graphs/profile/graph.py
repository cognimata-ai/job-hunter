"""`profile_graph` (spec §9.1, §9.5; M2 plan Task 11, Deviations 25, 27 and 28).

**The graphs.** `build_profile_graphs(checkpointer, deps)` compiles them once per process into
`ProfileGraphs`. This task defines `text`: `parse → strip_contacts → END`, where `parse` goes straight to
`END` when it ended the draft itself (`failed`, or obsolete: the state's `outcome`). Tasks 13 and 20 add the
fields
`extract` (`check_pdf → extract_profile → validate → draft → END`) and `propose` (`propose_queries → END`)
with their nodes; no graph exists here before its nodes do.

**The invoke shape** (§9.1). A handler invokes its graph with `config={"configurable": {"thread_id":
thread_id(…), "op": ctx}, "recursion_limit": RECURSION_LIMIT}` and `durability="sync"`. LangGraph never
checkpoints `configurable` (it copies only its string and number values into a checkpoint's metadata, and the
context is neither), so a node reads its execution, the claim, `finish_in`, `requeue_in` and the worker's
`OperationDeps`, from `config["configurable"]["op"]` (`nodes.execution`), never from the state.

**The threads** (Deviation 27). `thread_id(kind, subject_id, operation_id, attempts)` is
`profile-<draft_id>-t<operation_id>-a<attempts>` for the text phase, `-p…` for the extract phase and
`queries-<proposal_id>-q<operation_id>-a<attempts>` for a proposal, called with the claim's `operation_id`
and `attempts`. Every execution of every operation gets a thread of its own, so no state of an earlier
operation or attempt reaches a later one through an omitted key or a reducer (`steps` adds up). Work done
before a crash is found through the receipts in the database (§9.1), not through the thread.

**The policies** (§9.1). A node without an LLM call takes `DB_RETRY` and its `TimeoutPolicy`
(`NODE_TIMEOUTS`: `parse` 120 s, `strip_contacts` 30 s; `add_node`). An LLM node (`add_llm_node`, Tasks 13
and 20) takes no `RetryPolicy` (the gateway owns the retries, §10.4), `LLM_NODE_TIMEOUT` (the §13.1 maximum
`deadline_s` of 1,800 s plus a margin: a graph compiled once cannot take the deadline an `execution` save
changes at runtime, Deviation 28; the gateway enforces the active one per call through
`CallContext.deadline`) and `LLM_NODE_TRACE` (its input and output omitted from any trace).

**`sanitized`** (Deviation 25) wraps every profile node. LangGraph 1.2.15 stores the exception that escapes
a node in `checkpoint_writes` (the write `(__error__, exc)` of `PregelRunner.commit`), through its
`JsonPlusSerializer`, which encodes an exception as its `repr`: the class name and the arguments, never its
traceback, its frames' locals, its `__cause__` or its `__context__`. An SQLAlchemy or Pydantic error can quote
bound values (CV text, profile fields) in that text, and LangGraph's own retry log line quotes the error's
text too. So only the errors whose text is a code pass through unchanged: `FencedOut`, LangGraph's
`GraphBubbleUp` (its interrupts and commands), the M2 domain errors `LLMError` with its subclasses,
`UploadRejected` and `TransitionConflict`, and `asyncio.CancelledError` (a `BaseException`, never caught).
Any other error is logged with its class only and replaced by `errors.NodeFailed(node, error_class)`, raised
after the `except` block, so neither `__cause__` nor `__context__` refers to the original. A passing
`LLMError` keeps its traceback in memory, and the gateway's frames hold the prompt in their locals; nothing
persists that traceback (the serializer stores the `repr`; the handler logs the class only), so it is not
stripped. The stand-in of a retryable database error (`NodeFailed.retryable_db_error`) is one that
`DB_RETRY` retries.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType
from typing import Final, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import TimeoutPolicy, TracePolicy, omit_payload

from jobhunter.errors import FencedOut, NodeFailed, TransitionConflict
from jobhunter.graphs.collect.graph import DB_RETRY, is_retryable_db_error
from jobhunter.graphs.profile.deps import ProfileDeps
from jobhunter.graphs.profile.nodes import Node, log
from jobhunter.graphs.profile.nodes import parse as parse_node
from jobhunter.graphs.profile.nodes import strip_contacts as strip_node
from jobhunter.graphs.profile.state import ProfileState as ProfileState
from jobhunter.graphs.profile.state import ProfileUpdate
from jobhunter.llm.errors import LLMError
from jobhunter.profile.errors import UploadRejected

type ProfileGraph = CompiledStateGraph[ProfileState, None, ProfileState, ProfileState]
type ThreadKind = Literal["t", "p", "q"]

RECURSION_LIMIT: Final = 200  # §9.1
# The §9.1 timeout of each node without an LLM call.
NODE_TIMEOUTS: Final[Mapping[str, timedelta]] = MappingProxyType(
    {parse_node.NAME: parse_node.NODE_TIMEOUT, strip_node.NAME: strip_node.NODE_TIMEOUT}
)
# The §13.1 maximum `deadline_s` (1,800 s) plus a margin (Deviation 28).
LLM_NODE_TIMEOUT: Final = TimeoutPolicy(run_timeout=1800 + 60)
LLM_NODE_TRACE: Final = TracePolicy(process_inputs=omit_payload, process_outputs=omit_payload)
# The errors whose text is a code (module docstring); `asyncio.CancelledError` is no `Exception` at all.
PASS_THROUGH: Final = (FencedOut, GraphBubbleUp, LLMError, UploadRejected, TransitionConflict)

_THREAD_PREFIX: Final[Mapping[ThreadKind, str]] = MappingProxyType(
    {"t": "profile", "p": "profile", "q": "queries"}
)


@dataclass(frozen=True)
class ProfileGraphs:
    """The compiled profile graphs. Task 13 adds `extract` and Task 20 `propose`."""

    text: ProfileGraph


def thread_id(kind: ThreadKind, subject_id: int, operation_id: int, attempts: int) -> str:
    """The checkpoint thread of one execution (module docstring): the subject is the draft (`t`, `p`) or the
    proposal (`q`); `operation_id` and `attempts` are the claim's."""
    return f"{_THREAD_PREFIX[kind]}-{subject_id}-{kind}{operation_id}-a{attempts}"


def sanitized(name: str, node: Node) -> Node:
    """`node` whose unclassified errors become `NodeFailed(name, <class>)` (module docstring)."""

    async def guarded(state: ProfileState, config: RunnableConfig) -> ProfileUpdate:
        try:
            return await node(state, config)
        except PASS_THROUGH:
            raise
        except Exception as exc:  # noqa: BLE001 - replaced by a content-free stand-in, raised below
            log.error("profile_node_failed", node=name, error_class=type(exc).__name__)
            failure = NodeFailed(name, type(exc).__name__, retryable_db_error=is_retryable_db_error(exc))
        # Outside the handler: the stand-in has no `__context__`, and the original is unreachable from it.
        raise failure

    return guarded


def add_node(graph: StateGraph[ProfileState], name: str, node: Node) -> None:
    """Add the node `name`, which makes no LLM call, with `DB_RETRY` and its `NODE_TIMEOUTS` entry."""
    graph.add_node(
        name,
        sanitized(name, node),
        retry_policy=DB_RETRY,
        timeout=TimeoutPolicy(run_timeout=NODE_TIMEOUTS[name]),
    )


def add_llm_node(graph: StateGraph[ProfileState], name: str, node: Node) -> None:
    """Add the LLM node `name`: no `RetryPolicy`, `LLM_NODE_TIMEOUT` and `LLM_NODE_TRACE` (Tasks 13, 20)."""
    graph.add_node(name, sanitized(name, node), timeout=LLM_NODE_TIMEOUT, trace_policy=LLM_NODE_TRACE)


def after_parse(state: ProfileState) -> Literal["strip_contacts", "__end__"]:
    """`strip_contacts` unless `parse` ended the draft itself (its `outcome`)."""
    return "__end__" if state["outcome"] is not None else "strip_contacts"  # "__end__" is `END`


def _text_graph(checkpointer: BaseCheckpointSaver[str], deps: ProfileDeps) -> ProfileGraph:
    graph = StateGraph(ProfileState)
    add_node(graph, parse_node.NAME, parse_node.make_parse(deps))
    add_node(graph, strip_node.NAME, strip_node.make_strip_contacts(deps))
    graph.add_edge(START, parse_node.NAME)
    graph.add_conditional_edges(parse_node.NAME, after_parse, [strip_node.NAME, END])
    graph.add_edge(strip_node.NAME, END)
    return graph.compile(checkpointer=checkpointer)


def build_profile_graphs(checkpointer: BaseCheckpointSaver[str], deps: ProfileDeps) -> ProfileGraphs:
    """The profile graphs over the process-wide `deps`, compiled with `checkpointer` (once per process)."""
    return ProfileGraphs(text=_text_graph(checkpointer, deps))
