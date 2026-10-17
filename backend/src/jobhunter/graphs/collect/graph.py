"""The collection graph's state and the M0 no-op graph (spec §3.3.3; spike V6).

`CollectState` is the full state of a run attempt's graph; a node returns a `CollectUpdate`, the partial
update LangGraph merges into it (`steps` accumulates through its `operator.add` reducer). Every attempt runs
on its own checkpoint thread (`worker.runs.thread_id`), so the graph never reads an earlier attempt's
progress.

A node's parameter is named `state`: LangGraph's node protocols name it so, and `mypy --strict` matches the
name.

The no-op graph is `start` → `finalize`. It does no work; it proves the run lifecycle around it (admission,
heartbeat, fenced finalize) and gives the crash tests the fault point `collect.before_finalize`.
"""

import operator
from typing import Annotated, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from jobhunter.faults import afault_point


class CollectState(TypedDict):
    run_id: int
    attempt_no: int
    steps: Annotated[list[str], operator.add]


class CollectUpdate(TypedDict, total=False):
    run_id: int
    attempt_no: int
    steps: Annotated[list[str], operator.add]


type CollectGraph = CompiledStateGraph[CollectState, None, CollectState, CollectState]


async def start(state: CollectState) -> CollectUpdate:
    return {"steps": ["start"]}


async def finalize(state: CollectState) -> CollectUpdate:
    await afault_point("collect.before_finalize")
    return {"steps": ["finalize"]}


def build_noop_collect_graph(checkpointer: BaseCheckpointSaver[str]) -> CollectGraph:
    """The no-op collection graph, `start` → `finalize`, compiled with `checkpointer`."""
    graph = StateGraph(CollectState)
    graph.add_node("start", start)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "start")
    graph.add_edge("start", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)
