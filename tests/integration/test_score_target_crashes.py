"""The per-target crash cases in process (spec §18.3 C8, C10-C13, C16, C28; plan Task 10): a crash at each
fault point of one attempt, then the reclaim of L3572 and a new claim (`tests.support.scoring.reclaim_now`,
`claim_again`), with the ledger, receipts and states each case expects. C9, C14 and C17 run on the real worker
in Task 28.

Every datum is invented.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.errors import NodeFailed
from jobhunter.graphs.score import nodes as score_nodes
from jobhunter.graphs.score import subgraph
from jobhunter.graphs.score.deps import ScoreDeps
from jobhunter.graphs.score.nodes import publish as publish_node
from jobhunter.graphs.score.state import initial_state
from jobhunter.llm import fake, spend_ledger
from jobhunter.llm import gateway as gateway_module
from jobhunter.llm.prompts.render import prompt_schema_v2
from jobhunter.scoring import publish as publish_mod
from langgraph.types import TimeoutPolicy
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import ctx
from tests.support.faults import PausePoint, no_fault, raising_at
from tests.support.llm import FakeLLM, tries
from tests.support.scoring import (
    attempt_end,
    attempt_of,
    claim_again,
    claimed_target,
    count,
    ended_with,
    invoke_subgraph,
    reclaim_now,
    score_config,
    score_deps,
    synthetic_c_draft,
    target_of,
)

pytestmark = pytest.mark.integration
FIXED = datetime(2026, 10, 27, 9, 30, tzinfo=UTC)
_CALLS = sa.text(
    "SELECT c.purpose, c.attempt_id, l.state, l.evidence FROM llm_calls c "
    "JOIN llm_spend_ledger l ON l.id = c.spend_ledger_id WHERE c.target_id = :t ORDER BY c.id"
)


@pytest.fixture
async def deps(engine: AsyncEngine, fake_llm: FakeLLM, clean_db: None) -> ScoreDeps:
    return await score_deps(engine, fake_llm.gateway(engine))


async def calls(engine: AsyncEngine, target_id: int) -> list[tuple[Any, ...]]:
    async with engine.connect() as conn:
        return [tuple(row) for row in await conn.execute(_CALLS, {"t": target_id})]


async def test_c8_crash_after_the_reservation_before_dispatch(
    engine: AsyncEngine,
    deps: ScoreDeps,
    fake_llm: FakeLLM,
    current_epoch: int,
    queue_app: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed = await claimed_target(engine)
    fake_llm.answer("generate", claimed.job_revision_id, synthetic_c_draft())
    monkeypatch.setattr(gateway_module, "afault_point", raising_at("gateway.after_reserve"))
    with pytest.raises(NodeFailed):
        await invoke_subgraph(deps, claimed)  # the node raises; the attempt stays running
    monkeypatch.setattr(gateway_module, "afault_point", no_fault)
    assert (await attempt_end(engine, claimed.attempt_id)).outcome == "running"
    await reclaim_now(engine, claimed.target_id)  # the L3572 reclaim
    reconciled = await spend_ledger.reconcile_llm_ledger_unsettled(
        ctx(engine, queue_app, now=utcnow() + timedelta(minutes=16))
    )
    assert reconciled == 1
    assert [(t.state, t.evidence) for t in await tries(engine)] == [("not_billed", "never_dispatched")]
    assert fake_llm.served == 0
    new = await claim_again(engine, claimed.target_id)
    final = await invoke_subgraph(deps, new)
    assert final["outcome"] == "published_accepted"
    assert (await target_of(engine, claimed.target_id)).state == "published_accepted"


async def test_c10_crash_before_the_draft_regenerates_and_publishes(
    engine: AsyncEngine, deps: ScoreDeps, fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    claimed = await claimed_target(engine)
    fake_llm.answer("generate", claimed.job_revision_id, synthetic_c_draft())
    monkeypatch.setattr(score_nodes, "afault_point", raising_at(score_nodes.BEFORE_DRAFT))
    with pytest.raises(NodeFailed):
        await invoke_subgraph(deps, claimed)
    monkeypatch.setattr(score_nodes, "afault_point", no_fault)
    await reclaim_now(engine, claimed.target_id)
    new = await claim_again(engine, claimed.target_id)
    assert (await invoke_subgraph(deps, new))["outcome"] == "published_accepted"
    assert await calls(engine, claimed.target_id) == [
        ("generate", claimed.attempt_id, "billed", None),
        ("generate", new.attempt_id, "billed", None),  # paid twice, within the attempt budget
    ]
    assert await count(engine, "score_drafts") == 1 and await count(engine, "llm_results") == 1


async def test_c11_crash_after_the_draft_resumes_without_a_new_generate_call(
    engine: AsyncEngine, deps: ScoreDeps, fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    claimed = await claimed_target(engine)
    fake_llm.answer("generate", claimed.job_revision_id, synthetic_c_draft())
    monkeypatch.setattr(score_nodes, "afault_point", raising_at(score_nodes.AFTER_DRAFT))
    with pytest.raises(NodeFailed):
        await invoke_subgraph(deps, claimed)
    monkeypatch.setattr(score_nodes, "afault_point", no_fault)
    await reclaim_now(engine, claimed.target_id)
    new = await claim_again(engine, claimed.target_id)
    final = await invoke_subgraph(deps, new)
    assert final["outcome"] == "published_accepted" and final["resumed"] is True
    assert fake_llm.served == 1 and await count(engine, "llm_results") == 1


async def test_c12_crash_before_the_publish_commit_republishes_idempotently(
    engine: AsyncEngine, deps: ScoreDeps, fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    claimed = await claimed_target(engine)
    fake_llm.answer("generate", claimed.job_revision_id, synthetic_c_draft())
    monkeypatch.setattr(publish_mod, "afault_point", raising_at(publish_mod.BEFORE_COMMIT))
    with pytest.raises(NodeFailed):
        await invoke_subgraph(deps, claimed)
    monkeypatch.setattr(publish_mod, "afault_point", no_fault)
    assert await count(engine, "llm_results") == 0 and await count(engine, "score_drafts") == 1
    assert (await target_of(engine, claimed.target_id)).state == "in_progress"  # rolled back
    await reclaim_now(engine, claimed.target_id)
    new = await claim_again(engine, claimed.target_id)  # after the reclaim
    final = await invoke_subgraph(deps, new)  # resumes from the draft, no new generate call
    assert final["outcome"] == "published_accepted"
    assert fake_llm.served == 1 and await count(engine, "llm_results") == 1


async def test_c13_after_the_commit_nothing_changes(
    engine: AsyncEngine, deps: ScoreDeps, fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    claimed = await claimed_target(engine)
    fake_llm.answer("generate", claimed.job_revision_id, synthetic_c_draft())
    monkeypatch.setattr(publish_node, "afault_point", raising_at(publish_mod.AFTER_COMMIT))
    with pytest.raises(NodeFailed):
        await invoke_subgraph(deps, claimed)
    monkeypatch.setattr(publish_node, "afault_point", no_fault)
    target = await target_of(engine, claimed.target_id)
    assert target.state == "published_accepted" and await count(engine, "llm_results") == 1
    # a second publish of the same attempt is a claim_lost no-op (the unique 5-tuple is never reached)
    state = initial_state(claimed.ref, claimed.attempt_id)
    async with engine.connect() as conn:
        draft = (await conn.execute(sa.text("SELECT id FROM score_drafts"))).scalar_one()
        prompt = (
            await conn.execute(
                sa.text("SELECT prompt_schema_version_id FROM llm_tuples WHERE id = :u"),
                {"u": claimed.llm_tuple_id},
            )
        ).scalar_one()
    state.update({"current_draft_id": draft, "publish_mode": "accepted", "prompt_schema_version_id": prompt})
    command = await publish_node.make_publish(deps)(state, await score_config(deps, claimed))
    assert ended_with(command) == "claim_lost"
    assert await count(engine, "llm_results") == 1
    assert (await target_of(engine, claimed.target_id)).published_result_id == target.published_result_id


async def test_c16_node_deadline_cancels_the_stream_and_defers_with_backoff(
    engine: AsyncEngine, fake_llm: FakeLLM, clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await score_deps(
        engine, fake_llm.gateway(engine)
    )  # the 120 s read timeout: only the policy ends it
    deps = ScoreDeps(made.engine, made.gateway, lambda: FIXED, made.monotonic, made.saver)
    _, schemas = prompt_schema_v2()
    generator = schemas["generator"]
    assert isinstance(generator, dict)
    fake_llm.record("generate", "claude-sonnet-5-5", synthetic_c_draft(), schema=generator)
    claimed = await claimed_target(engine)
    key = fake.answer_key("generate", {"job_revision_id": claimed.job_revision_id}, 0)
    fake.answer(key, "stall_after_first_event")
    monkeypatch.setattr(subgraph, "LLM_NODE_TIMEOUT", TimeoutPolicy(run_timeout=1))  # pinned to 1 s
    before = asyncio.all_tasks()
    final = await asyncio.wait_for(invoke_subgraph(deps, claimed), 30)
    left = asyncio.all_tasks() - before  # the cancelled node's task settles its try
    if left:
        _, pending = await asyncio.wait(left, timeout=10)
        assert not pending
    assert (final["error_code"], final["outcome"]) == ("llm_transient", "transient_error")
    (row,) = await tries(engine)
    assert (row.state, row.status, row.error_code) == ("ambiguous", "ambiguous", "cancelled")
    assert fake_llm.served == 1
    target = await target_of(engine, claimed.target_id)
    assert (target.state, target.not_before, target.transient_failures) == (
        "pending",
        FIXED + timedelta(minutes=15),
        1,
    )
    ended = await attempt_end(engine, claimed.attempt_id)
    assert (ended.outcome, ended.outcome_detail) == ("transient_error", "llm_transient")
    assert (await attempt_of(engine, claimed.attempt_id)).cost_usd == row.reserved_usd  # counted ambiguous


async def test_c28_stale_owner_between_reservation_and_dispatch_marker(
    engine: AsyncEngine, deps: ScoreDeps, fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    claimed = await claimed_target(engine)
    fake_llm.answer("generate", claimed.job_revision_id, synthetic_c_draft())
    pause = PausePoint("gateway.after_reserve")
    monkeypatch.setattr(gateway_module, "afault_point", pause)
    old = asyncio.create_task(invoke_subgraph(deps, claimed))
    await asyncio.wait_for(pause.reached.wait(), 10)
    await reclaim_now(engine, claimed.target_id)  # a rival reclaims and re-claims the target
    new = await claim_again(engine, claimed.target_id)
    pause.resume()
    final_old = await asyncio.wait_for(old, 10)
    assert final_old["outcome"] == "claim_lost" and final_old["error_code"] == "claim_lost"
    assert fake_llm.served == 0  # the old dispatch marker refused: nothing was sent
    final_new = await invoke_subgraph(deps, new)  # the new owner's reservation proceeds
    assert final_new["outcome"] == "published_accepted" and fake_llm.served == 1
    assert await calls(engine, claimed.target_id) == [
        ("generate", claimed.attempt_id, "not_billed", "never_dispatched"),
        ("generate", new.attempt_id, "billed", None),
    ]
    assert (await attempt_end(engine, claimed.attempt_id)).outcome == "claim_lost"  # the reclaim's
