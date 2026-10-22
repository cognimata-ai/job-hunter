"""`profile_graph` (spec §9.1, §9.5; M2 plan Task 11, Deviations 25, 27 and 28).

**The graphs.** `build_profile_graphs(checkpointer, deps)` compiles them once per process into
`ProfileGraphs`:
- `text` (Task 11): `parse → strip_contacts → END`, where `parse` goes straight to `END` when it ended the
  draft itself (`failed`, or obsolete: the state's `outcome`);
- `extract` (Task 13): `check_pdf → extract_profile → validate → draft → END`, where each of the first three
  goes straight to `END` when it ended the phase itself (`failed`, `requeued` or obsolete);
- `propose` (Task 20): `propose_queries → END`, over a query proposal (`ProposalState`), not a draft. Its one
  node ends the proposal and the operation itself.

**The invoke shape** (§9.1). A handler invokes its graph with `invoke_config(kind, subject_id, ctx)`,
`{"configurable": {"thread_id": thread_id(…), "op": ctx}, "recursion_limit": RECURSION_LIMIT,
"max_concurrency": MAX_CONCURRENCY}`, and `durability="sync"`. LangGraph never
checkpoints `configurable` (it copies only its string and number values into a checkpoint's metadata, and the
context is neither), so a node reads its execution, the claim, `finish_in`, `requeue_in` and the worker's
`OperationDeps`, from `config["configurable"]["op"]` (`nodes.execution`), never from the state.

**The threads** (Deviation 27). `thread_id(kind, subject_id, operation_id, attempts)` is
`profile-<draft_id>-t<operation_id>-a<attempts>` for the text phase, `-p…` for the extract phase and
`queries-<proposal_id>-q<operation_id>-a<attempts>` for a proposal, called with the claim's `operation_id`
and `attempts`. Every execution of every operation gets a thread of its own, so no state of an earlier
operation or attempt reaches a later one through an omitted key or a reducer (`steps` adds up). Work done
before a crash is found through the receipts in the database (§9.1), not through the thread.
`thread_operation_id(thread)` reads the operation id back from a name `thread_id` wrote (`None` for any
other name), and `THREAD_PREFIXES` are its prefixes: the `profile_checkpoints` retention rule deletes a
thread by its own operation's end (Task 23, §20.1).

**The policies** (§9.1). A node without an LLM call takes `DB_RETRY` and its `TimeoutPolicy` (`NODE_TIMEOUTS`:
`parse` and `check_pdf` 120 s, `strip_contacts`, `validate` and `draft` 30 s; `add_node`). An LLM node
(`add_llm_node`, Tasks 13 and 20) takes no `RetryPolicy` (the gateway owns the retries, §10.4),
`LLM_NODE_TIMEOUT` (the §13.1 maximum `deadline_s` of 1,800 s plus a margin: a graph compiled once cannot take
the deadline an `execution` save changes at runtime, Deviation 28; the gateway enforces the active one per
call through `CallContext.deadline`) and `LLM_NODE_TRACE` (its input and output omitted from any trace).
`sanitized` and `add_llm_node` take a node of either state (`nodes.StateNode`).

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

import itertools
import re
from collections.abc import Callable, Mapping
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
from jobhunter.graphs.profile.nodes import Node, StateNode, log
from jobhunter.graphs.profile.nodes import check_pdf as check_pdf_node
from jobhunter.graphs.profile.nodes import draft as draft_node
from jobhunter.graphs.profile.nodes import extract_profile as extract_node
from jobhunter.graphs.profile.nodes import parse as parse_node
from jobhunter.graphs.profile.nodes import propose_queries as propose_node
from jobhunter.graphs.profile.nodes import strip_contacts as strip_node
from jobhunter.graphs.profile.nodes import validate as validate_node
from jobhunter.graphs.profile.state import ProfileState as ProfileState
from jobhunter.graphs.profile.state import ProposalState
from jobhunter.llm.errors import LLMError
from jobhunter.profile.errors import UploadRejected
from jobhunter.worker.operations import OperationContext

type ProfileGraph = CompiledStateGraph[ProfileState, None, ProfileState, ProfileState]
type ProposalGraph = CompiledStateGraph[ProposalState, None, ProposalState, ProposalState]
type ThreadKind = Literal["t", "p", "q"]

RECURSION_LIMIT: Final = 200  # §9.1
# §9.1's `max_concurrency`: every profile graph is a chain, one node at a time.
MAX_CONCURRENCY: Final = 1
# The §9.1 timeout of each node without an LLM call.
NODE_TIMEOUTS: Final[Mapping[str, timedelta]] = MappingProxyType(
    {
        parse_node.NAME: parse_node.NODE_TIMEOUT,
        strip_node.NAME: strip_node.NODE_TIMEOUT,
        check_pdf_node.NAME: check_pdf_node.NODE_TIMEOUT,
        validate_node.NAME: validate_node.NODE_TIMEOUT,
        draft_node.NAME: draft_node.NODE_TIMEOUT,
    }
)
# The §13.1 maximum `deadline_s` (1,800 s) plus a margin (Deviation 28).
LLM_NODE_TIMEOUT: Final = TimeoutPolicy(run_timeout=1800 + 60)
LLM_NODE_TRACE: Final = TracePolicy(process_inputs=omit_payload, process_outputs=omit_payload)
# The errors whose text is a code (module docstring); `asyncio.CancelledError` is no `Exception` at all.
PASS_THROUGH: Final = (FencedOut, GraphBubbleUp, LLMError, UploadRejected, TransitionConflict)

_THREAD_PREFIX: Final[Mapping[ThreadKind, str]] = MappingProxyType(
    {"t": "profile", "p": "profile", "q": "queries"}
)
THREAD_PREFIXES: Final = frozenset(_THREAD_PREFIX.values())
# The names `thread_id` writes: `<prefix>-<subject_id>-<kind><operation_id>-a<attempts>`, one alternative per
# prefix with its kinds; each number as `str(int)` writes it, the operation id the group `op`.
_NUMBER: Final = "(?:0|[1-9][0-9]*)"
_THREAD_NAME: Final = re.compile(
    "|".join(
        f"{re.escape(prefix)}-{_NUMBER}-[{''.join(k for k, p in _THREAD_PREFIX.items() if p == prefix)}]"
        for prefix in sorted(THREAD_PREFIXES)
    ).join(("(?:", f")(?P<op>{_NUMBER})-a{_NUMBER}"))
)


@dataclass(frozen=True)
class ProfileGraphs:
    """The compiled profile graphs: the text phase, the extract phase and the query proposal."""

    text: ProfileGraph
    extract: ProfileGraph
    propose: ProposalGraph


def thread_id(kind: ThreadKind, subject_id: int, operation_id: int, attempts: int) -> str:
    """The checkpoint thread of one execution (module docstring): the subject is the draft (`t`, `p`) or the
    proposal (`q`); `operation_id` and `attempts` are the claim's."""
    return f"{_THREAD_PREFIX[kind]}-{subject_id}-{kind}{operation_id}-a{attempts}"


def thread_operation_id(thread: str) -> int | None:
    """The operation id in the name of a thread `thread_id` wrote; `None` for any other name."""
    named = _THREAD_NAME.fullmatch(thread)
    return None if named is None else int(named["op"])


def invoke_config(kind: ThreadKind, subject_id: int, op: OperationContext) -> RunnableConfig:
    """The §9.1 invoke config of execution `op` on its own thread (module docstring)."""
    claim = op.claim
    return {
        "configurable": {
            "thread_id": thread_id(kind, subject_id, claim.operation_id, claim.attempts),
            "op": op,
        },
        "recursion_limit": RECURSION_LIMIT,
        "max_concurrency": MAX_CONCURRENCY,
    }


def sanitized[S, U](name: str, node: StateNode[S, U]) -> StateNode[S, U]:
    """`node` whose unclassified errors become `NodeFailed(name, <class>)` (module docstring)."""

    async def guarded(state: S, config: RunnableConfig) -> U:
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


def add_llm_node[S: (ProfileState, ProposalState), U](
    graph: StateGraph[S], name: str, node: StateNode[S, U]
) -> None:
    """Add the LLM node `name`: no `RetryPolicy`, `LLM_NODE_TIMEOUT` and `LLM_NODE_TRACE` (Tasks 13, 20)."""
    graph.add_node(name, sanitized(name, node), timeout=LLM_NODE_TIMEOUT, trace_policy=LLM_NODE_TRACE)


def after_parse(state: ProfileState) -> Literal["strip_contacts", "__end__"]:
    """`strip_contacts` unless `parse` ended the draft itself (its `outcome`)."""
    return "__end__" if state["outcome"] is not None else "strip_contacts"  # "__end__" is `END`


def _continue_unless_ended(next_node: str) -> Callable[[ProfileState], str]:
    """The route after a node that may end the phase itself: `next_node`, or `END` once `outcome` is set."""

    def route(state: ProfileState) -> str:
        return END if state["outcome"] is not None else next_node

    return route


def _text_graph(checkpointer: BaseCheckpointSaver[str], deps: ProfileDeps) -> ProfileGraph:
    graph = StateGraph(ProfileState)
    add_node(graph, parse_node.NAME, parse_node.make_parse(deps))
    add_node(graph, strip_node.NAME, strip_node.make_strip_contacts(deps))
    graph.add_edge(START, parse_node.NAME)
    graph.add_conditional_edges(parse_node.NAME, after_parse, [strip_node.NAME, END])
    graph.add_edge(strip_node.NAME, END)
    return graph.compile(checkpointer=checkpointer)


def _extract_graph(checkpointer: BaseCheckpointSaver[str], deps: ProfileDeps) -> ProfileGraph:
    graph = StateGraph(ProfileState)
    add_node(graph, check_pdf_node.NAME, check_pdf_node.make_check_pdf(deps))
    add_llm_node(graph, extract_node.NAME, extract_node.make_extract_profile(deps))
    add_node(graph, validate_node.NAME, validate_node.make_validate(deps))
    add_node(graph, draft_node.NAME, draft_node.make_draft(deps))
    graph.add_edge(START, check_pdf_node.NAME)
    chain = (check_pdf_node.NAME, extract_node.NAME, validate_node.NAME, draft_node.NAME)
    for node, next_node in itertools.pairwise(chain):
        graph.add_conditional_edges(node, _continue_unless_ended(next_node), [next_node, END])
    graph.add_edge(draft_node.NAME, END)
    return graph.compile(checkpointer=checkpointer)


def _propose_graph(checkpointer: BaseCheckpointSaver[str], deps: ProfileDeps) -> ProposalGraph:
    graph = StateGraph(ProposalState)
    add_llm_node(graph, propose_node.NAME, propose_node.make_propose_queries(deps))
    graph.add_edge(START, propose_node.NAME)
    graph.add_edge(propose_node.NAME, END)
    return graph.compile(checkpointer=checkpointer)


def build_profile_graphs(checkpointer: BaseCheckpointSaver[str], deps: ProfileDeps) -> ProfileGraphs:
    """The profile graphs over the process-wide `deps`, compiled with `checkpointer` (once per process)."""
    return ProfileGraphs(
        text=_text_graph(checkpointer, deps),
        extract=_extract_graph(checkpointer, deps),
        propose=_propose_graph(checkpointer, deps),
    )
