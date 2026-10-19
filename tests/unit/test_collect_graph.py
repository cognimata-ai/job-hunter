import pytest
import sqlalchemy as sa
from jobhunter.errors import FencedOut
from jobhunter.graphs.collect.graph import error_handled, is_retryable_db_error
from jobhunter.graphs.collect.nodes.plan_fallback import route
from jobhunter.graphs.collect.state import CollectState, CollectUpdate, FetchCellInput, initial_state
from jobhunter.sources.base import ContractViolation, SourceError


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
