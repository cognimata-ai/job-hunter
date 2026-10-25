"""The content-free stand-in of a collection node's error (spec §9.1, §16; final review FR-12).

Every node of the collection graph is `guarded` (`graphs.sanitize.sanitized`, the profile graphs' wrapper):
an error whose text is a code or ids passes through unchanged, so `execute_run` classifies it as before
(`FencedOut`: `lost`; `OutcomeConflict`: `failed_invariant('I7')`), and any other becomes `NodeFailed` with
its class only. tests/integration/test_collect_checkpoint_errors.py runs it through a real checkpointer.
"""

import asyncio
import logging
import time
from typing import Any

import psycopg
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.session import make_null_engine
from jobhunter.errors import FencedOut, InvariantViolation, NodeFailed, TransitionConflict
from jobhunter.graphs.collect import graph as collect_graph
from jobhunter.graphs.collect.deps import CollectDeps, FetchContext
from jobhunter.graphs.collect.graph import CollectUpdate, build_collect_graph, guarded
from jobhunter.graphs.collect.state import initial_state
from jobhunter.graphs.outcomes import OutcomeConflict
from jobhunter.graphs.sanitize import is_retryable_db_error
from jobhunter.sources.base import SourceAdapter
from jobhunter.sources.instances import InstanceRow
from jobhunter.sources.ratelimit import SOURCE_LIMITS, RateLimiter
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphBubbleUp, GraphInterrupt

from tests.support.logs import logged

MARKER = "Quastenflosser Beispiel GmbH"
STATE = initial_state(7, 1)


def raising(error: BaseException) -> Any:
    async def node(state: Any) -> CollectUpdate:
        raise error

    return node


def test_every_collection_node_is_guarded(monkeypatch) -> None:
    seen: list[str] = []
    real = collect_graph.guarded

    def spy(name: str, node: Any) -> Any:
        seen.append(name)
        return real(name, node)

    monkeypatch.setattr(collect_graph, "guarded", spy)

    def no_adapter(instance: InstanceRow) -> SourceAdapter:
        raise AssertionError("building the graph makes no source call")

    deps = CollectDeps(
        engine=make_null_engine("postgresql+psycopg://unused@localhost/unused"),  # never connected
        adapters=no_adapter,
        embedder=None,
        clock=utcnow,
        sleep=asyncio.sleep,
        admission_stopped=lambda: False,
        limiter=RateLimiter(SOURCE_LIMITS.values(), clock=time.monotonic, sleep=asyncio.sleep),
        fetch_ctx=FetchContext(),
    )
    graph = build_collect_graph(InMemorySaver(), deps)
    nodes = set(graph.builder.nodes)
    assert sorted(seen) == sorted(nodes) and len(nodes) == 11


@pytest.mark.parametrize(
    "error",
    [
        FencedOut("run"),
        OutcomeConflict("fr:1"),
        InvariantViolation("fetch request 3 was planned without a coverage window"),
        TransitionConflict("fetch_requests", 3, "succeeded"),
        GraphBubbleUp(),
        GraphInterrupt(),
        TimeoutError(),
        asyncio.CancelledError(),
    ],
    ids=type,
)
async def test_errors_whose_text_is_a_code_or_ids_pass_through_unchanged(error: BaseException) -> None:
    with pytest.raises(type(error)) as raised:
        await guarded("plan", raising(error))(STATE)
    assert raised.value is error


class _UniqueViolation(psycopg.Error):
    sqlstate = "23505"


class _SerializationFailure(psycopg.Error):
    sqlstate = "40001"


def db_error(orig: psycopg.Error) -> sa.exc.DBAPIError:
    """A driver error as the server reports a duplicate key: its DETAIL quotes the row's value."""
    return sa.exc.IntegrityError("INSERT INTO companies (company_key) VALUES (%(k)s)", None, orig)


@pytest.mark.parametrize(
    ("make_error", "retryable"),
    [
        (lambda: db_error(_UniqueViolation(f"duplicate key\nDETAIL:  Key (company_key)=({MARKER})")), False),
        (lambda: db_error(_SerializationFailure(f"could not serialize\nDETAIL:  {MARKER}")), True),
        (lambda: ValueError(MARKER), False),
    ],
    ids=["unique_violation", "serialization_failure", "value_error"],
)
async def test_any_other_error_becomes_node_failed_with_its_class_only(make_error, retryable, caplog) -> None:
    caplog.set_level(logging.INFO)
    error = make_error()
    assert MARKER in repr(error)  # what LangGraph would store without the wrapper
    with pytest.raises(NodeFailed) as raised:
        await guarded("resolve_identity", raising(error))(STATE)
    failed = raised.value
    assert (failed.node, failed.error_class) == ("resolve_identity", type(error).__name__)
    assert failed.retryable_db_error is retryable and is_retryable_db_error(failed) is retryable
    assert failed.__cause__ is None and failed.__context__ is None
    assert MARKER not in repr(failed)
    [event] = logged(caplog, "collect_node_failed")
    assert (event["node"], event["error_class"]) == ("resolve_identity", type(error).__name__)
    assert MARKER not in caplog.text


async def test_a_node_that_returns_is_left_alone() -> None:
    async def node(state: Any) -> CollectUpdate:
        return {"steps": ["plan"]}

    assert await guarded("plan", node)(STATE) == {"steps": ["plan"]}
