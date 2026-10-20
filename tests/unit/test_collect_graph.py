import asyncio
import dataclasses
from collections.abc import Callable
from datetime import timedelta
from typing import Any, cast

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.embeddings import EmbedderError
from jobhunter.errors import FencedOut
from jobhunter.graphs.collect.deps import CollectDeps
from jobhunter.graphs.collect.graph import (
    EMBEDDER_ERRORS,
    IO_NODES,
    SOURCE_ERRORS,
    TIMEOUTS,
    error_handled,
    is_retryable_db_error,
    timed,
)
from jobhunter.graphs.collect.nodes.enqueue_targets import make_enqueue_targets
from jobhunter.graphs.collect.nodes.finalize import WARNING_COUNTERS
from jobhunter.graphs.collect.nodes.plan_fallback import ACCOUNT_READ_TIMEOUT_S, route, serpapi_account
from jobhunter.graphs.collect.state import CollectState, CollectUpdate, FetchCellInput, initial_state
from jobhunter.quota.windows import AccountObservation
from jobhunter.run_settings import SourcesEnabled
from jobhunter.sources.base import ContractViolation, SourceAdapter, SourceError
from jobhunter.sources.instances import InstanceRow
from jobhunter.sources.serpapi import SerpApiAdapter

from tests.support.quota import seeded_run_settings


def test_the_initial_state_is_empty_but_for_the_attempt() -> None:
    assert initial_state(7, 2) == {
        "run_id": 7,
        "attempt_no": 2,
        "settings_version_id": 0,
        "scope_version_id": 0,
        "llm_tuple_id": None,
        "plan_done": False,
        "primary_fetch_request_ids": [],
        "fallback_fetch_request_ids": [],
        "outcomes": {},
        "counters": {},
        "steps": [],
        "node_durations": {},
        "final_status": None,
    }


def test_plan_fallback_routes_to_fetch_fallback_only_with_specs() -> None:
    state = initial_state(1, 1)
    assert route(state) == "parse_normalize"
    assert route({**state, "fallback_fetch_request_ids": [4]}) == "fetch_fallback"


async def test_the_error_handler_persists_a_source_error_as_an_outcome() -> None:
    async def failing(state: CollectState) -> CollectUpdate:
        raise SourceError("transient", code="provider_error", status=200)

    async def broken_cell(state: FetchCellInput) -> CollectUpdate:
        raise ContractViolation("not_json", status=200)

    assert await error_handled("plan", failing)(initial_state(1, 1)) == {
        "steps": ["plan"],
        "counters": {"source_errors": 1},
    }
    assert await error_handled("fetch_cell", broken_cell)({"fetch_request_id": 9}) == {
        "steps": ["fetch_cell"],
        "counters": {"source_errors": 1},
        "outcomes": {"fr:9": {"work_id": "fr:9", "status": "failed", "items": 0, "new_postings": 0}},
    }


async def test_the_error_handler_persists_an_embedder_error_as_the_nodes_outcome() -> None:
    async def failing(state: CollectState) -> CollectUpdate:
        raise EmbedderError("bad_response", code="dimension")

    assert await error_handled("embed", failing)(initial_state(1, 1)) == {
        "steps": ["embed"],
        "counters": {"embedder_errors": 1},
    }


def test_the_embed_node_has_its_section_9_2_timeout_and_no_retry() -> None:
    assert TIMEOUTS["embed"] == timedelta(minutes=20) and "embed" in IO_NODES


@pytest.mark.parametrize("error", [FencedOut("run"), RuntimeError("a bug"), KeyError("x")])
async def test_the_error_handler_lets_everything_else_propagate(error: Exception) -> None:
    async def node(state: CollectState) -> CollectUpdate:
        raise error

    with pytest.raises(type(error)):
        await error_handled("plan", node)(initial_state(1, 1))


def test_only_database_errors_that_may_run_again_are_retried() -> None:
    lost = sa.exc.DBAPIError("SELECT 1", {}, ConnectionError("reset"), connection_invalidated=True)
    other = sa.exc.DBAPIError("SELECT 1", {}, ConnectionError("refused"))
    assert is_retryable_db_error(lost)
    assert not is_retryable_db_error(other)
    assert not is_retryable_db_error(RuntimeError("40001"))
    assert not is_retryable_db_error(SourceError("transient", code="http_503", status=503))


SERPAPI = InstanceRow(
    id=1,
    source_id="serpapi",
    instance_key="default",
    watchlist_company_id=None,
    health="ok",
    half_open_after=None,
    board_slug=None,
    lever_region=None,
    display_name=None,
)


async def _never(request: httpx.Request) -> httpx.Response:
    await asyncio.Event().wait()  # a read that never answers
    raise AssertionError("unreachable")


def _server_error(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json={"error": "down"})


def _bug(request: httpx.Request) -> httpx.Response:
    raise RuntimeError("a bug in the transport")


async def _account(handler: Callable[[httpx.Request], Any], **options: Any) -> AccountObservation | None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SerpApiAdapter(client, api_key=b"fake", instance_id=SERPAPI.id, clock=utcnow)
        return await serpapi_account(
            lambda instance: adapter, seeded_run_settings(), [SERPAPI], now=utcnow(), **options
        )


async def test_the_fallback_account_read_is_bounded_and_a_failed_read_is_no_read() -> None:
    """Minor 3: an expired read (here after 10 ms) or a failed one is `None`: the projection fails closed."""
    assert ACCOUNT_READ_TIMEOUT_S == 10.0
    assert await _account(_never, timeout_s=0.01) is None
    assert await _account(_server_error) is None  # a SourceError
    with pytest.raises(RuntimeError, match="a bug"):  # never swallowed
        await _account(_bug)


class _RaisingAccount:
    """A SerpApi adapter whose account read raises `error` (it satisfies `AccountAdapter` and no more)."""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def account(self) -> AccountObservation:
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        FencedOut("run"),
        sa.exc.DBAPIError("SELECT 1", {}, ConnectionError("reset"), connection_invalidated=True),
        asyncio.CancelledError(),
    ],
    ids=["fenced_out", "dbapi_error", "cancelled"],
)
async def test_a_lost_owner_a_database_error_or_a_cancellation_propagates_from_the_account_read(
    error: BaseException,
) -> None:
    """Fix wave F4 (ledger L196): only a `SourceError` or the read's own timeout is "no read"; a fenced
    owner, a database error and a cancellation propagate, from the read or from `on_auth_failure`."""
    adapter = cast(SourceAdapter, _RaisingAccount(error))
    with pytest.raises(type(error)):
        await serpapi_account(lambda instance: adapter, seeded_run_settings(), [SERPAPI], now=utcnow())
    revoked = cast(SourceAdapter, _RaisingAccount(SourceError("auth", code="http_401", status=401)))

    async def failing(instance_id: int) -> None:
        raise error

    with pytest.raises(type(error)):
        await serpapi_account(
            lambda instance: revoked, seeded_run_settings(), [SERPAPI], now=utcnow(), on_auth_failure=failing
        )


async def test_no_account_is_read_when_serpapi_is_off() -> None:
    disabled = dataclasses.replace(SERPAPI, health="disabled")
    sources = SourcesEnabled(jsearch=True, serpapi=False, adzuna=True, arbeitnow=True, ats=True)
    switched_off = dataclasses.replace(seeded_run_settings(), sources=sources)

    def no_adapter(instance: InstanceRow) -> SourceAdapter:
        raise AssertionError("no read")

    now = utcnow()
    assert await serpapi_account(no_adapter, seeded_run_settings(), [disabled], now=now) is None
    assert await serpapi_account(no_adapter, switched_off, [SERPAPI], now=now) is None
    assert await serpapi_account(no_adapter, seeded_run_settings(), [], now=now) is None
    # final review F3, Minor 4: an instance its breaker blocks gets no read either (the read is a send)
    for health in ("auth_failed", "breaker_open"):
        blocked = dataclasses.replace(SERPAPI, health=health, half_open_after=now + timedelta(hours=1))
        assert await serpapi_account(no_adapter, seeded_run_settings(), [blocked], now=now) is None


# ---- Task 23 ----------------------------------------------------------------------------------------------


async def test_a_timed_node_reports_its_seconds_and_keeps_its_update() -> None:
    ticks = iter([10.0, 12.5])

    async def node(state: CollectState) -> CollectUpdate:
        return {"steps": ["plan"], "counters": {"x": 1}}

    measured = timed("plan", node, clock=lambda: next(ticks))
    assert await measured(initial_state(1, 1)) == {
        "steps": ["plan"],
        "counters": {"x": 1},
        "node_durations": {"plan": 2.5},
    }


async def test_a_timed_node_lets_its_error_propagate() -> None:
    async def node(state: CollectState) -> CollectUpdate:
        raise FencedOut("run")

    with pytest.raises(FencedOut):
        await timed("finalize", node)(initial_state(1, 1))


async def test_enqueue_targets_records_its_step_and_refuses_a_tuple_until_m3() -> None:
    node = make_enqueue_targets(cast(CollectDeps, None))
    assert await node(initial_state(1, 1)) == {"steps": ["enqueue_targets"]}
    with pytest.raises(NotImplementedError, match="M3"):
        await node({**initial_state(1, 1), "llm_tuple_id": 4})


def test_the_report_warns_of_the_error_handlers_counters() -> None:
    assert WARNING_COUNTERS == (SOURCE_ERRORS, EMBEDDER_ERRORS)
