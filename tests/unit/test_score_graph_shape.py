"""The shape of `score_graph` (spec §9.1, §9.3; plan Task 11, Codex r1 #12-#14, r2 B6, B25): its nodes and
edges, the nodes' timeouts and policies, the routing of a claim round and of its end, the invoke config, the
state's keys and its outcome reducer, and the compiled subgraph that the `score_target` wrapper invokes.

The fan-out ends in one join: `score_target -> admit` is a plain edge, so `admit` runs once after every claim
of the round, and `more` decides on the joined state. A conditional edge out of `score_target` itself runs its
path once per task, on that task's own writes alone (LangGraph 1.2.15), so a decision made there could differ
between the round's tasks and start `admit` and `finalize_cycle` together.
"""

import time
from datetime import timedelta
from typing import cast, get_type_hints

import pytest
from jobhunter.clock import utcnow
from jobhunter.graphs.outcomes import OutcomeConflict, merge_outcomes
from jobhunter.graphs.sanitize import DB_RETRY
from jobhunter.graphs.score.context import ScoreContext
from jobhunter.graphs.score.deps import ScoreDeps
from jobhunter.graphs.score.graph import (
    CYCLE_NODE_TIMEOUTS,
    MAX_CLAIM_ROUNDS,
    MAX_CONCURRENCY,
    RECURSION_LIMIT,
    build_score_graphs,
    fan_out,
    invoke_config,
    more,
    thread_id,
)
from jobhunter.graphs.score.state import ClaimRef, CycleState, TargetOutcome
from jobhunter.graphs.score.subgraph import NODE_TIMEOUTS as TARGET_NODE_TIMEOUTS
from jobhunter.llm.gateway import LLMGateway
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START
from langgraph.types import Send, TimeoutPolicy
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.scoring import NoClaims


def deps() -> ScoreDeps:
    return ScoreDeps(
        engine=cast(AsyncEngine, object()),
        gateway=cast(LLMGateway, object()),
        clock=utcnow,
        monotonic=time.monotonic,
        saver=InMemorySaver(),
    )


def ref(target_id: int) -> ClaimRef:
    return {"target_id": target_id, "claim_id": f"claim-{target_id}", "attempt_no": 1}


def state(**fields: object) -> CycleState:
    base: dict[str, object] = {
        "cycle_id": 7,
        "worker_epoch": 1,
        "llm_tuple_id": 3,
        "scope_version_id": 1,
        "evaluation_policy_id": 1,
        "claimed": [],
        "results": {},
        "claim_rounds": 0,
        "current_round": [],
        "round_claims": [],
    }
    return cast(CycleState, {**base, **fields})


def test_the_cycle_graph_is_admit_claim_the_fan_out_and_finalize() -> None:
    drawn = build_score_graphs(deps()).cycle.get_graph()
    edges = {(edge.source, edge.target, edge.conditional) for edge in drawn.edges}
    assert edges == {
        (START, "admit", False),
        ("admit", "claim", True),  # `more`: the round admitted targets
        ("admit", "finalize_cycle", True),  # `more`: nothing (more) to claim
        ("claim", "score_target", True),  # `fan_out`: one Send per claim of the round
        ("claim", "finalize_cycle", True),  # `fan_out`: the round claimed nothing
        ("score_target", "admit", False),  # the join: the next claim round
        ("finalize_cycle", END, False),
    }


def test_the_cycle_nodes_have_their_timeouts_and_retries() -> None:
    nodes = build_score_graphs(deps()).cycle.builder.nodes
    assert set(nodes) == {"admit", "claim", "score_target", "finalize_cycle"}
    assert dict(CYCLE_NODE_TIMEOUTS) == {
        "admit": timedelta(seconds=120),
        "claim": timedelta(seconds=60),
        "finalize_cycle": timedelta(seconds=60),
    }
    for name in ("admit", "claim", "finalize_cycle"):
        spec = nodes[name]
        assert spec.retry_policy == DB_RETRY
        assert spec.timeout == TimeoutPolicy(run_timeout=CYCLE_NODE_TIMEOUTS[name].total_seconds())
    # The wrapper of one attempt: never retried (a same-claim re-run would pay a second call, Controller carry
    # T9-b) and bounded by its subgraph's own node timeouts, never cut off as a whole.
    wrapper = nodes["score_target"]
    assert wrapper.retry_policy is None and wrapper.timeout is None
    assert wrapper.input_schema is ClaimRef


def test_the_subgraph_is_compiled_once_and_is_no_node_of_the_cycle_graph() -> None:
    """The `score_target` node is the wrapper, never the compiled subgraph registered directly (Codex r2
    B25); the subgraph keeps its own nodes and timeouts and inherits the cycle's checkpointer."""
    graphs = build_score_graphs(deps())
    steps = {name for name in graphs.target.builder.nodes if not name.startswith("__error_handler__")}
    assert steps == {"load_context", "generate", "validate", "critic", "revise", "publish", "fail"}
    assert graphs.target.checkpointer is None
    assert graphs.cycle.checkpointer is not None
    assert TARGET_NODE_TIMEOUTS["load_context"] == timedelta(seconds=60)
    assert graphs.cycle.builder.nodes["score_target"].runnable is not graphs.target


def test_fan_out_sends_one_score_target_per_claim_of_the_round() -> None:
    claims = [ref(11), ref(12), ref(13)]
    sent = fan_out(state(round_claims=claims, claimed=[ref(1), *claims]))
    assert sent == [Send("score_target", claim) for claim in claims]  # the round's, not the history
    assert fan_out(state(round_claims=[], claimed=[ref(1)])) == "finalize_cycle"


def test_more_claims_while_the_round_admitted_and_finalizes_otherwise() -> None:
    admitted = [{"target_id": 5, "lane": "score_now", "always_include_reason": None}]
    assert more(state(current_round=admitted, claim_rounds=1)) == "claim"
    assert more(state(current_round=[], claim_rounds=1)) == "finalize_cycle"
    assert MAX_CLAIM_ROUNDS == 5


def test_the_invoke_config_names_the_cycle_thread_and_its_limits() -> None:
    context = ScoreContext(
        cycle_id=42,
        worker_epoch=3,
        llm_tuple_id=1,
        scope_version_id=1,
        evaluation_policy_id=1,
        deps=deps(),
        claims=NoClaims(),
    )
    config = invoke_config(context)
    assert thread_id(42) == "cycle-42"
    assert config == {
        "configurable": {"thread_id": "cycle-42", "cycle": context},
        "recursion_limit": 200,
        "max_concurrency": 3,
    }
    assert (RECURSION_LIMIT, MAX_CONCURRENCY) == (200, 3)


def test_the_cycle_state_is_the_spec_s_with_the_claim_rounds() -> None:
    assert list(get_type_hints(CycleState)) == [
        "cycle_id",
        "worker_epoch",
        "llm_tuple_id",
        "scope_version_id",
        "evaluation_policy_id",
        "claimed",
        "results",
        "claim_rounds",
        "current_round",
        "round_claims",
    ]
    reducers = {
        name: hint.__metadata__[0]
        for name, hint in get_type_hints(CycleState, include_extras=True).items()
        if hasattr(hint, "__metadata__")
    }
    assert reducers["results"] is merge_outcomes
    assert reducers["claimed"]([ref(1)], [ref(2)]) == [ref(1), ref(2)]  # the history appends
    assert set(reducers) == {"results", "claimed"}


def outcome(target_id: int, attempt_outcome: str) -> TargetOutcome:
    return {
        "target_id": target_id,
        "attempt_no": 1,
        "outcome": attempt_outcome,
        "state": "published_accepted",
        "lane": "similarity",
        "cost_usd": "0.010000",
    }


def test_the_results_reducer_keeps_an_equal_outcome_once_and_refuses_a_conflict() -> None:
    """`merge_outcomes`, generic over the outcome type, is the collection graph's reducer and the cycle's."""
    first = {"t:1:a1": outcome(1, "published_accepted")}
    second = {"t:2:a1": outcome(2, "claim_lost")}
    assert merge_outcomes(first, second) == merge_outcomes(second, first) == {**first, **second}
    assert merge_outcomes(first, dict(first)) == first
    with pytest.raises(OutcomeConflict) as conflict:
        merge_outcomes(first, {"t:1:a1": outcome(1, "published_contested")})
    assert conflict.value.work_id == "t:1:a1"
