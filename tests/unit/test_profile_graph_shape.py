"""The shape of `profile_graph` (spec §9.1, §9.5; plan Task 11, Deviations 25, 27 and 28): the text graph's
nodes, edges and policies, the LLM nodes' policies, the thread names, and the node wrapper `sanitized` that
keeps an exception's content out of `checkpoint_writes`.

Tasks 13 and 20 add the `extract` and `propose` graphs to `ProfileGraphs`, and their shapes here.
"""

import asyncio
import dataclasses
import logging
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast

import psycopg
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.errors import FencedOut, NodeFailed, TransitionConflict
from jobhunter.graphs.collect.graph import DB_RETRY, is_retryable_db_error
from jobhunter.graphs.profile.deps import ProfileDeps
from jobhunter.graphs.profile.graph import (
    LLM_NODE_TIMEOUT,
    LLM_NODE_TRACE,
    NODE_TIMEOUTS,
    ProfileGraphs,
    add_llm_node,
    build_profile_graphs,
    sanitized,
    thread_id,
)
from jobhunter.graphs.profile.state import ProfileState, ProfileUpdate, initial_state
from jobhunter.llm.errors import BudgetExhausted, InvalidOutput, LLMTransient, Refused
from jobhunter.profile.errors import UploadRejected
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphBubbleUp, GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import TimeoutPolicy, omit_payload
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.logs import logged

# An invented word of a synthetic CV: it must never reach a checkpoint or a log.
MARKER = "Quastenflosser"
NO_CONFIG: RunnableConfig = {"configurable": {}}


def graphs() -> ProfileGraphs:
    return build_profile_graphs(
        InMemorySaver(), ProfileDeps(engine=cast(AsyncEngine, object()), clock=utcnow)
    )


def test_profile_graphs_hold_the_text_graph() -> None:
    """Tasks 13 and 20 add `extract` and `propose` with their nodes; no placeholder graph exists before."""
    assert [field.name for field in dataclasses.fields(ProfileGraphs)] == ["text"]


def test_the_text_graph_is_parse_then_strip_contacts() -> None:
    drawn = graphs().text.get_graph()
    edges = {(edge.source, edge.target, edge.conditional) for edge in drawn.edges}
    # `parse` goes on to `strip_contacts` unless it ended the draft itself (failed, or obsolete)
    assert edges == {
        (START, "parse", False),
        ("parse", "strip_contacts", True),
        ("parse", END, True),
        ("strip_contacts", END, False),
    }


def test_the_text_nodes_retry_database_errors_within_their_timeouts() -> None:
    nodes = graphs().text.builder.nodes
    assert set(nodes) == {"parse", "strip_contacts"}
    assert dict(NODE_TIMEOUTS) == {"parse": timedelta(seconds=120), "strip_contacts": timedelta(seconds=30)}
    for name, seconds in (("parse", 120), ("strip_contacts", 30)):
        spec = nodes[name]
        assert spec.retry_policy == DB_RETRY
        assert spec.timeout == TimeoutPolicy(run_timeout=seconds)
        assert spec.trace_policy is None


def test_an_llm_node_has_no_retry_the_maximum_deadline_and_traces_nothing() -> None:
    """Deviation 28: a graph compiled once cannot take the active `deadline_s`, so an LLM node's timeout is
    the §13.1 maximum (1,800 s) plus a margin, and the gateway enforces the deadline per call. §9.1: the
    gateway owns the retries and the node's input and output are omitted from any trace (Tasks 13 and 20)."""
    assert LLM_NODE_TIMEOUT == TimeoutPolicy(run_timeout=1800 + 60)
    assert (LLM_NODE_TRACE.process_inputs, LLM_NODE_TRACE.process_outputs) == (omit_payload, omit_payload)
    graph = StateGraph(ProfileState)
    add_llm_node(graph, "extract_profile", _returning)
    spec = graph.nodes["extract_profile"]
    assert (spec.retry_policy, spec.timeout, spec.trace_policy) == (None, LLM_NODE_TIMEOUT, LLM_NODE_TRACE)


def test_every_execution_has_a_thread_of_its_own() -> None:
    """Deviation 27: the operation and its attempt are in the name, so no state of an earlier operation or
    attempt is ever read by a later one."""
    assert thread_id("t", 7, 42, 0) == "profile-7-t42-a0"
    assert thread_id("t", 7, 42, 1) == "profile-7-t42-a1"
    assert thread_id("p", 7, 43, 2) == "profile-7-p43-a2"
    assert thread_id("q", 9, 44, 0) == "queries-9-q44-a0"


def test_the_initial_state_holds_ids_only() -> None:
    assert initial_state(7, 42) == {
        "draft_id": 7,
        "operation_id": 42,
        "llm_call_id": None,
        "steps": [],
        "outcome": None,
    }


# --- sanitized (Deviation 25) ------------------------------------------------------------------------------


async def _returning(state: ProfileState, config: RunnableConfig) -> ProfileUpdate:
    return {"steps": ["returning"]}


def raising(error: BaseException) -> Any:
    async def node(state: ProfileState, config: RunnableConfig) -> ProfileUpdate:
        raise error

    return node


@pytest.mark.parametrize(
    "error",
    [
        FencedOut("operation"),
        asyncio.CancelledError(),
        GraphBubbleUp(),
        GraphInterrupt(),
        Refused("cyber"),
        LLMTransient("http_529"),
        InvalidOutput("structural"),
        BudgetExhausted("nonscoring", Decimal(0)),
        UploadRejected("too_short"),
        TransitionConflict("profile_drafts", 7, "failed"),
    ],
    ids=type,
)
async def test_errors_whose_text_is_a_code_pass_through_unchanged(error: BaseException) -> None:
    with pytest.raises(type(error)) as raised:
        await sanitized("parse", raising(error))(initial_state(7, 42), NO_CONFIG)
    assert raised.value is error


def statement_error() -> sa.exc.StatementError:
    """An SQLAlchemy error as a failed write of CV text raises it: the driver's message quotes the value."""
    return sa.exc.StatementError(
        f'(psycopg.DataError) invalid input value: "{MARKER}"',
        "UPDATE cv_files SET stripped_text = %(t)s WHERE id = %(i)s",
        {"t": f"Projekt {MARKER}", "i": 1},
        ValueError(MARKER),
    )


@pytest.mark.parametrize(
    "make_error", [statement_error, lambda: ValueError(MARKER), lambda: KeyError(MARKER)]
)
async def test_any_other_error_becomes_node_failed_with_its_class_only(make_error: Any, caplog) -> None:
    caplog.set_level(logging.INFO)
    error = make_error()
    assert MARKER in repr(error)  # what LangGraph would store without the wrapper (`repr`, its serializer)
    with pytest.raises(NodeFailed) as raised:
        await sanitized("strip_contacts", raising(error))(initial_state(7, 42), NO_CONFIG)
    failed = raised.value
    assert (failed.node, failed.error_class) == ("strip_contacts", type(error).__name__)
    # raised after the `except` block: nothing refers back to the original
    assert failed.__cause__ is None and failed.__context__ is None
    assert MARKER not in str(failed) and MARKER not in repr(failed)
    [event] = logged(caplog, "profile_node_failed")
    assert (event["node"], event["error_class"]) == ("strip_contacts", type(error).__name__)
    assert MARKER not in caplog.text


async def test_a_node_that_returns_is_left_alone() -> None:
    assert await sanitized("parse", _returning)(initial_state(7, 42), NO_CONFIG) == {"steps": ["returning"]}


class _SerializationFailure(psycopg.Error):
    sqlstate = "40001"


def serialization_failure() -> sa.exc.OperationalError:
    return sa.exc.OperationalError(
        "UPDATE cv_files SET stripped_text = %(t)s", {"t": MARKER}, _SerializationFailure(MARKER)
    )


def test_the_stand_in_of_a_retryable_database_error_is_retryable() -> None:
    assert is_retryable_db_error(NodeFailed("parse", "OperationalError", retryable_db_error=True))
    assert not is_retryable_db_error(NodeFailed("parse", "ValueError"))


async def test_the_db_retry_retries_a_node_through_the_wrapper(caplog) -> None:
    """The retry policy sees the stand-in, never the original: a serialization failure is retried, and the
    retry's log line (LangGraph logs the error's text, at INFO) names the stand-in only."""
    caplog.set_level(logging.INFO)
    calls: list[int] = []

    async def flaky(state: ProfileState, config: RunnableConfig) -> ProfileUpdate:
        calls.append(1)
        if len(calls) == 1:
            raise serialization_failure()
        return {"steps": ["flaky"]}

    graph = StateGraph(ProfileState)
    graph.add_node("flaky", sanitized("flaky", flaky), retry_policy=DB_RETRY)
    graph.add_edge(START, "flaky")
    graph.add_edge("flaky", END)
    out = await graph.compile().ainvoke(initial_state(7, 42))
    assert len(calls) == 2 and out["steps"] == ["flaky"]
    assert "Retrying task flaky" in caplog.text and "NodeFailed" in caplog.text
    assert MARKER not in caplog.text


async def test_a_non_retryable_error_is_not_retried() -> None:
    calls: list[int] = []

    async def failing(state: ProfileState, config: RunnableConfig) -> ProfileUpdate:
        calls.append(1)
        raise ValueError(MARKER)

    graph = StateGraph(ProfileState)
    graph.add_node("failing", sanitized("failing", failing), retry_policy=DB_RETRY)
    graph.add_edge(START, "failing")
    graph.add_edge("failing", END)
    with pytest.raises(NodeFailed):
        await graph.compile().ainvoke(initial_state(7, 42))
    assert len(calls) == 1
