"""Spike V11 (spec §22; plan Task 5): a LangGraph `TimeoutPolicy` cancels an in-flight streaming gateway call,
and the gateway settles the try `ambiguous`, with no task of the call left behind.

The stream stalls after its first event (`stall_after_first_event`) under the production 120 s read timeout,
so only the node's 2 s `run_timeout` can end it. LangGraph raises `NodeTimeoutError` at once and cancels the
node's task without awaiting it, so the gateway's settlement runs in that cancelled task just after the error;
the test waits for exactly the tasks the call left and measures both moments (`docs/spikes/V11.md`).
"""

import asyncio
import time
from typing import TypedDict

import pytest
from langgraph.errors import NodeTimeoutError
from langgraph.graph import END, START, StateGraph
from langgraph.types import TimeoutPolicy

from tests.support.llm import (
    GATEWAY_NODE,
    FakeLLM,
    active_execution_config,
    call_context,
    gateway_prompt,
    tries,
)

RUN_TIMEOUT_S = 2.0


class State(TypedDict, total=False):
    llm_call_id: int


async def test_v11_timeout_policy_cancels_the_stream_and_the_try_settles_ambiguous(
    engine, current_epoch, op_owner, fake_llm: FakeLLM
) -> None:
    gateway = fake_llm.gateway(engine)  # the production 120 s read timeout: the fake stalls past the policy
    ec = await active_execution_config(engine)
    fake_llm.scenario("stall_after_first_event")

    async def call(state: State) -> State:
        result = await gateway.call(GATEWAY_NODE, gateway_prompt(), call_context(op_owner, ec))
        return {"llm_call_id": result.llm_call_id}

    builder = StateGraph(State)
    builder.add_node("call", call, timeout=TimeoutPolicy(run_timeout=RUN_TIMEOUT_S))
    builder.add_edge(START, "call")
    builder.add_edge("call", END)
    graph = builder.compile()

    before = asyncio.all_tasks()
    started = time.monotonic()
    with pytest.raises(NodeTimeoutError):
        await graph.ainvoke({})
    raised_after = time.monotonic() - started
    # LangGraph cancelled the node's task without awaiting it: wait for what the call left, and nothing else
    left = asyncio.all_tasks() - before
    if left:
        _, pending = await asyncio.wait(left, timeout=5)
        assert not pending
    settled_after = time.monotonic() - started

    assert RUN_TIMEOUT_S <= raised_after < RUN_TIMEOUT_S + 1
    assert settled_after < RUN_TIMEOUT_S + 1  # the cancellation landed and settled within 1 s of the deadline
    assert asyncio.all_tasks() - before == set()  # no task created by the call is left
    (row,) = await tries(engine)
    assert (row.state, row.evidence, row.settled_usd) == ("ambiguous", None, None)  # it keeps its reservation
    assert (row.status, row.error_code, row.cost_usd) == ("ambiguous", "cancelled", row.reserved_usd)
    assert row.first_event_at is not None  # the response had begun: the request was sent
    assert fake_llm.served == 1
