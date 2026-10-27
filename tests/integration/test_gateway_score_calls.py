"""Score calls through the real gateway on the recorded-SSE fake (spec §9.4.2, §9.4.3, §10.2, §10.3, §10.7;
plan Task 7): a `generate` call of a `TargetAttemptOwner` writes its `llm_calls` row with the attempt, the
target, the round and the logical call number, and moves the attempt's counters in its dispatch markers; a
lost claim reserves or sends nothing; a funded follow-up debits its allocation; the pinned configuration is
read by id; and `reservation_for` states the reservation `call()` makes."""

import asyncio
import time
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.llm import gateway as gateway_module
from jobhunter.llm.errors import ClaimLost
from jobhunter.llm.gateway import CallContext, NodeConfig, WireSchema, pinned_node_config
from jobhunter.llm.pricing import Usage, settled_usd
from jobhunter.llm.spend_ledger import TargetAttemptOwner
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.faults import PausePoint
from tests.support.llm import (
    FakeLLM,
    GatewayOut,
    active_execution_config,
    attempt_and_target,
    gateway_prompt,
    new_target_owner,
    reclaim_target,
    score_tuple,
    tries,
)
from tests.support.m3 import insert_bulk_operation

pytestmark = pytest.mark.integration
SCORE_NODE = NodeConfig(
    purpose="generate",
    model="claude-sonnet-5-5",
    effort="medium",
    max_tokens=6000,
    max_tokens_retry=12000,
    deadline_s=600,
    schema=WireSchema.from_model(GatewayOut),
)
SYNTHETIC_USAGE = Usage(120, 60, 0, 0)  # `synthetic_sse`'s usage
# The §7.11 owner CHECK of `llm_calls`, evaluated on a stored row.
OWNER_CHECK = sa.text(
    "SELECT (purpose NOT IN ('generate','revise','critic') OR eval_run_id IS NOT NULL "
    "OR (attempt_id IS NOT NULL AND target_id IS NOT NULL "
    "AND (transport = 'batch' OR logical_call_no IS NOT NULL))) AS holds, "
    "attempt_id, target_id, round, logical_call_no, transport, input_refs FROM llm_calls WHERE id = :id"
)


def score_context(owner: TargetAttemptOwner, ec: int, **overrides: Any) -> CallContext:
    values: dict[str, Any] = {
        "owner": owner,
        "purpose_class": "scoring",
        "execution_config_id": ec,
        "deadline": time.monotonic() + SCORE_NODE.deadline_s,
        "attempt_id": owner.attempt_id,
        "target_id": owner.target_id,
        "round": 0,
        "logical_call_no": 1,
        **overrides,
    }
    return CallContext(**values)


@pytest.fixture
async def ec(engine: AsyncEngine) -> int:
    return await active_execution_config(engine)


async def test_a_generate_call_records_its_attempt_and_moves_the_counters(
    engine, fake_llm: FakeLLM, target_owner, ec
) -> None:
    result = await fake_llm.gateway(engine).call(
        SCORE_NODE, gateway_prompt(), score_context(target_owner, ec)
    )
    (row,) = await tries(engine)
    assert (row.state, row.status, row.logical_call_no, row.transport_try) == ("billed", "completed", 1, 1)
    async with engine.connect() as conn:
        call = (await conn.execute(OWNER_CHECK, {"id": row.call_id})).one()
    assert call.holds
    assert (call.attempt_id, call.target_id, call.round, call.logical_call_no, call.transport) == (
        target_owner.attempt_id,
        target_owner.target_id,
        0,
        1,
        "stream",
    )
    assert call.input_refs["owner"] == target_owner.durable() and call.input_refs["deadline_s"] == 600
    attempt, target = await attempt_and_target(engine, target_owner)
    assert (attempt.logical_calls, attempt.http_calls, attempt.charged, target.charged_attempts) == (
        1,
        1,
        True,
        1,
    )
    cost = settled_usd("claude-sonnet-5-5", SYNTHETIC_USAGE)
    assert attempt.cost_usd == cost == result.cost_usd
    # the attempt's next logical call (its first critic round is call 2): one more of each
    await fake_llm.gateway(engine).call(
        SCORE_NODE, gateway_prompt(), score_context(target_owner, ec, logical_call_no=2, round=1)
    )
    attempt, target = await attempt_and_target(engine, target_owner)
    assert (attempt.logical_calls, attempt.http_calls, target.charged_attempts) == (2, 2, 1)
    assert attempt.cost_usd == 2 * cost


@pytest.mark.parametrize(
    ("script", "http_calls", "billed_tries"),
    [
        (("overloaded_529", "ok"), 2, 1),  # a retry of the same logical call
        (("max_tokens", "ok"), 2, 2),  # the enlarged stage of the same logical call
    ],
    ids=["retry", "enlarged_stage"],
)
async def test_the_tries_of_one_logical_call_count_it_once(
    engine, fake_llm: FakeLLM, target_owner, ec, script, http_calls, billed_tries
) -> None:
    fake_llm.scenario(*script)
    result = await fake_llm.gateway(engine).call(
        SCORE_NODE, gateway_prompt(), score_context(target_owner, ec)
    )
    rows = await tries(engine)
    assert [r.logical_call_no for r in rows] == [1] * http_calls
    attempt, target = await attempt_and_target(engine, target_owner)
    assert (attempt.logical_calls, attempt.http_calls, target.charged_attempts) == (1, http_calls, 1)
    billed = [r for r in rows if r.state == "billed"]
    assert len(billed) == billed_tries
    assert (
        attempt.cost_usd == sum((r.settled_usd or Decimal(0) for r in billed), Decimal(0)) == result.cost_usd
    )


async def test_a_call_resumed_at_its_enlarged_stage_counts_its_logical_call(
    engine, fake_llm: FakeLLM, target_owner, ec
) -> None:
    await fake_llm.gateway(engine).call(
        SCORE_NODE, gateway_prompt(), score_context(target_owner, ec, start_stage="enlarged")
    )
    (row,) = await tries(engine)
    assert (row.max_tokens_stage, row.max_tokens) == ("enlarged", 12000)
    attempt, _ = await attempt_and_target(engine, target_owner)
    assert (attempt.logical_calls, attempt.http_calls) == (1, 1)


async def test_a_lost_claim_reserves_nothing(engine, fake_llm: FakeLLM, target_owner, ec) -> None:
    await reclaim_target(engine, target_owner)
    with pytest.raises(ClaimLost):
        await fake_llm.gateway(engine).call(SCORE_NODE, gateway_prompt(), score_context(target_owner, ec))
    assert await tries(engine) == [] and fake_llm.served == 0
    attempt, target = await attempt_and_target(engine, target_owner)
    assert (attempt.logical_calls, attempt.http_calls, attempt.charged, target.charged_attempts) == (
        0,
        0,
        False,
        0,
    )


async def test_a_claim_lost_before_the_dispatch_marker_sends_nothing_and_charges_nothing(
    engine, fake_llm: FakeLLM, target_owner, ec, monkeypatch
) -> None:
    pause = PausePoint("gateway.after_reserve")
    monkeypatch.setattr(gateway_module, "afault_point", pause)
    call = asyncio.create_task(
        fake_llm.gateway(engine).call(SCORE_NODE, gateway_prompt(), score_context(target_owner, ec))
    )
    await asyncio.wait_for(pause.reached.wait(), 10)
    await reclaim_target(engine, target_owner)
    pause.resume()
    with pytest.raises(ClaimLost):
        await asyncio.wait_for(call, 10)
    (row,) = await tries(engine)
    assert (row.state, row.evidence, row.status) == ("not_billed", "never_dispatched", "cancelled")
    assert fake_llm.served == 0
    attempt, target = await attempt_and_target(engine, target_owner)
    assert (attempt.http_calls, attempt.charged, attempt.cost_usd, target.charged_attempts) == (
        0,
        False,
        0,
        0,
    )


async def test_a_funded_follow_up_debits_its_allocation(engine, fake_llm: FakeLLM, clean_db, ec) -> None:
    async with engine.begin() as conn:
        bulk = await insert_bulk_operation(conn, await score_tuple(conn), budget_usd=Decimal(5))
    owner = await new_target_owner(engine, attempt_no=2, bulk_operation_id=bulk, lane="batch_followup")
    await fake_llm.gateway(engine).call(
        SCORE_NODE, gateway_prompt(), score_context(owner, ec, bulk_operation_id=bulk)
    )
    (row,) = await tries(engine)
    async with engine.connect() as conn:
        entry_bulk = (
            await conn.execute(
                sa.text("SELECT bulk_operation_id FROM llm_spend_ledger WHERE id = :id"),
                {"id": row.ledger_id},
            )
        ).scalar_one()
        spent = (
            await conn.execute(sa.text("SELECT spent_usd FROM bulk_operations WHERE id = :id"), {"id": bulk})
        ).scalar_one()
    assert entry_bulk == bulk and spent == row.settled_usd  # reserved, then settled against the allocation


async def test_the_pinned_node_configuration_is_read_by_id_not_from_the_active_one(engine, clean_db) -> None:
    """L3813: an attempt's calls use the model policy of its tuple and the execution config it pinned at the
    claim, even after another config became active."""
    schema = WireSchema.from_model(GatewayOut)
    async with engine.begin() as conn:
        active = (
            await conn.execute(sa.text("SELECT model_policy_id, execution_config_id FROM active_config"))
        ).one()
        pinned = int(
            (
                await conn.execute(
                    sa.text(
                        "INSERT INTO execution_configs (config_hash, payload) "
                        "SELECT '\\x07', jsonb_set(payload, '{critic,max_tokens}', '7000') "
                        "FROM execution_configs WHERE id = :e RETURNING id"
                    ),
                    {"e": active.execution_config_id},
                )
            ).scalar_one()
        )
        node = await pinned_node_config(
            conn,
            "critic",
            model_policy_id=int(active.model_policy_id),
            execution_config_id=pinned,
            schema=schema,
            effort="low",
        )
        current = await pinned_node_config(
            conn,
            "critic",
            model_policy_id=int(active.model_policy_id),
            execution_config_id=int(active.execution_config_id),
            schema=schema,
        )
    assert (node.model, node.effort, node.max_tokens, node.max_tokens_retry, node.deadline_s) == (
        "claude-opus-5-5",
        "low",
        7000,
        12000,
        600,
    )
    assert (current.effort, current.max_tokens) == ("medium", 6000)


async def test_reservation_for_is_what_the_first_try_reserves(
    engine, fake_llm: FakeLLM, target_owner, ec
) -> None:
    gw = fake_llm.gateway(engine)
    prompt = gateway_prompt("A synthetic ad of some length. " * 40, cached_prefix=True)
    first = await gw.reservation_for(SCORE_NODE, prompt)
    enlarged = await gw.reservation_for(SCORE_NODE, prompt, stage="enlarged")
    fake_llm.scenario("max_tokens", "ok")
    await gw.call(SCORE_NODE, prompt, score_context(target_owner, ec))
    assert [r.reserved_usd for r in await tries(engine)] == [first, enlarged]
    assert enlarged - first == Decimal("0.060000")  # 6,000 more output tokens at $10 per MTok
