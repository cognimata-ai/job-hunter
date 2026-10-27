"""The `score_target` subgraph (spec §9.4; plan Task 9, Deviations 16 and 17): one claimed target's attempt,
from `load_context` to `publish` or `fail`.

**The nodes.** `load_context`, `validate`, `publish` and `fail` make no LLM call: each takes `DB_RETRY` and
its `TimeoutPolicy` (`NODE_TIMEOUTS`, in seconds: `load_context` 60, `validate` 120, `publish` 120, `fail`
60; A2 G1). The LLM nodes `generate`, `critic` and `revise` take no `RetryPolicy` (the gateway owns the
retries, L3394), `LLM_NODE_TIMEOUT` (the profile graph's: twice the §13.1 maximum `deadline_s` plus a margin;
the active deadline is enforced per call through `CallContext.deadline`) and `LLM_NODE_TRACE` (their input
and output omitted from any trace). Every node is wrapped by `sanitized` (M2 Deviation 25) with this graph's
`PASS_THROUGH`: an error whose text is a code passes, any other is logged with its class only and replaced by
`NodeFailed`.

**The routing** (L3546-3564, Deviation 17) is `scoring.routing`'s: each node returns a `Command` to the step
it gives (`destinations` name them), `START → load_context`, and `publish → END`, `fail → END`.

**`publish` and `fail`** (Task 10) are `nodes.publish` and `nodes.fail`, built through `make_publish` and
`make_fail` (module attributes, read when the graph is built, so a test may replace them).

**A node's deadline** (Controller carry T9-c). A `TimeoutPolicy` cancellation of an LLM node surfaces as
LangGraph's `NodeTimeoutError`, outside the node (LangGraph cancels the node's task, whose in-flight stream
the gateway settles `ambiguous`, M2 V11), so the node cannot map it itself. Each LLM node has an error handler
(`on_timeout`) that routes it to `fail` with its `llm_step` code (`llm_transient`: the target backs off, C16);
every other error of the node fails the run as it would without the handler (re-raised as it came). The
deterministic nodes' timeouts (a stalled database) are not mapped: the run fails and the claim is reclaimed.

The graph is compiled once per process with `checkpointer=None` (L3376): as the cycle graph's subgraph it
inherits that graph's checkpointer (Task 11).
"""

from collections.abc import Mapping
from datetime import timedelta
from types import MappingProxyType
from typing import Final, cast

from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphBubbleUp, NodeError, NodeTimeoutError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, TimeoutPolicy

from jobhunter.errors import FencedOut, TransitionConflict
from jobhunter.graphs.profile.graph import LLM_NODE_TIMEOUT, LLM_NODE_TRACE
from jobhunter.graphs.sanitize import DB_RETRY
from jobhunter.graphs.sanitize import sanitized as shared_sanitized
from jobhunter.graphs.score.deps import ScoreDeps
from jobhunter.graphs.score.llm_step import error_code_of
from jobhunter.graphs.score.nodes import Node, log
from jobhunter.graphs.score.nodes import fail as fail_node
from jobhunter.graphs.score.nodes import publish as publish_node
from jobhunter.graphs.score.nodes.critic import make_critic
from jobhunter.graphs.score.nodes.generate import make_generate
from jobhunter.graphs.score.nodes.load_context import make_load_context
from jobhunter.graphs.score.nodes.revise import make_revise
from jobhunter.graphs.score.nodes.validate import make_validate
from jobhunter.graphs.score.state import TargetState
from jobhunter.llm.errors import LLMError
from jobhunter.scoring.routing import Route

type ScoreTargetGraph = CompiledStateGraph[TargetState, None, TargetState, TargetState]

NODE_TIMEOUTS: Final[Mapping[str, timedelta]] = MappingProxyType(
    {
        "load_context": timedelta(seconds=60),
        "validate": timedelta(seconds=120),
        "publish": timedelta(seconds=120),
        "fail": timedelta(seconds=60),
    }
)
# The errors whose text is a code (the profile graph's list); `asyncio.CancelledError` is no `Exception`.
PASS_THROUGH: Final = (FencedOut, GraphBubbleUp, LLMError, TransitionConflict)
# Where each node's `Command` may go (`routing`).
DESTINATIONS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "load_context": ("generate", "validate", "publish", "fail", END),
        "generate": ("generate", "validate", "fail"),
        "validate": ("critic", "revise", "publish", "fail"),
        "critic": ("critic", "revise", "publish", "fail"),
        "revise": ("revise", "validate", "publish", "fail"),
    }
)


def make_publish(deps: ScoreDeps) -> Node:
    """The `publish` node (`nodes.publish`)."""
    return publish_node.make_publish(deps)


def make_fail(deps: ScoreDeps) -> Node:
    """The `fail` node (`nodes.fail`)."""
    return fail_node.make_fail(deps)


def on_timeout(name: str) -> Node:
    """The error handler of the LLM node `name` (module docstring): a `NodeTimeoutError` goes to `fail` with
    its code; any other error is raised again. LangGraph passes the failure as the parameter `error`, which it
    recognises by its name and its annotation `NodeError` exactly (so the handler is typed as the node it
    stands in for: the state and the config)."""

    async def handler(state: TargetState, config: RunnableConfig, error: NodeError) -> Command[Route]:
        if not isinstance(error.error, NodeTimeoutError):
            raise error.error
        code = error_code_of(error.error)
        log.warning(
            "score_node_timed_out",
            node=name,
            target_id=state["target_id"],
            attempt_id=state["attempt_id"],
            error_code=code,
            timeout_s=error.error.timeout,
        )
        return Command(goto="fail", update={"error_code": code})

    return cast(Node, handler)


def sanitized(name: str, node: Node) -> Node:
    """`node` whose unclassified errors become `NodeFailed(name, <class>)` (module docstring)."""
    return shared_sanitized(name, node, passing=PASS_THROUGH, log=log, event="score_node_failed")


def _add_node(graph: StateGraph[TargetState], name: str, node: Node) -> None:
    graph.add_node(
        name,
        sanitized(name, node),
        retry_policy=DB_RETRY,
        timeout=TimeoutPolicy(run_timeout=NODE_TIMEOUTS[name]),
        destinations=DESTINATIONS.get(name),
    )


def _add_llm_node(graph: StateGraph[TargetState], name: str, node: Node) -> None:
    graph.add_node(
        name,
        sanitized(name, node),
        timeout=LLM_NODE_TIMEOUT,
        trace_policy=LLM_NODE_TRACE,
        destinations=DESTINATIONS[name],
        error_handler=on_timeout(name),
    )


def build_score_target(deps: ScoreDeps) -> ScoreTargetGraph:
    """The subgraph over the process-wide `deps` (module docstring), compiled with `checkpointer=None`."""
    graph = StateGraph(TargetState)
    _add_node(graph, "load_context", make_load_context(deps))
    _add_llm_node(graph, "generate", make_generate(deps))
    _add_node(graph, "validate", make_validate(deps))
    _add_llm_node(graph, "critic", make_critic(deps))
    _add_llm_node(graph, "revise", make_revise(deps))
    _add_node(graph, "publish", make_publish(deps))
    _add_node(graph, "fail", make_fail(deps))
    graph.add_edge(START, "load_context")
    graph.add_edge("publish", END)
    graph.add_edge("fail", END)
    return graph.compile(checkpointer=None)
