"""`score_graph` on PostgreSQL with the fake LLM and keyed answers (spec §9.3, §9.4.3; plan Task 11, Review
Focus 1 and 5, Codex r1 #12-#14, r2 B5, B6, Rulings P10, P34, T11-a, T11-f, T11-g, Controller carries T9-b,
T9-c): pending targets of different jobs scored by one cycle.

A cycle claims three, scores them concurrently and completes with a report whose lane and outcome counts
match, one `TargetOutcome` per work id and `spent_usd` the attempts' cost; a budget denial ends one target
`budget_deferred` and the cycle still completes; a Score now that arrives during the fan-out is admitted in
the next claim round. The three graph-level heartbeat cases (Ruling P10): a heartbeat loss during a blocked
stream cancels that call alone, queued claims are beaten before they run, and a cancelled cycle cancels its
children and stops every beat, with a claim still queued for a slot too. A child's end is classified (Ruling
T11-a): a node's timeout backs its target off (`failed_transient` at the attempt limit), an unclassified
failure ends it `failed_invalid_output('bug')` for good, and the claim's own fence is `claim_lost` without an
error; a retryable database error that outlasted a node's retries backs its target off (Ruling T11-g),
`fail`'s own path outside the subgraph is tried up to three times and bounded by the `fail` node's timeout,
and an `attach` that raises leaves no attempt running. The cycle task (`graph.execute_cycle`) runs one
cycle under its heartbeat, fails it for a bug with its claims reclaimed, stops when its beat is refused, and
starts nothing while admission is stopped.

Every datum is invented.
"""

import asyncio
import gc
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.transitions import transition as real_transition
from jobhunter.errors import NodeFailed
from jobhunter.graphs.sanitize import DB_RETRY
from jobhunter.graphs.score import graph as graph_mod
from jobhunter.graphs.score import nodes as nodes_mod
from jobhunter.graphs.score import subgraph as subgraph_mod
from jobhunter.graphs.score.claims import ClaimHeartbeats
from jobhunter.graphs.score.nodes import claim as claim_node
from jobhunter.graphs.score.nodes import fail as fail_node
from jobhunter.graphs.score.state import TargetState, work_id
from jobhunter.llm import fake
from jobhunter.llm import gateway as gateway_mod
from jobhunter.llm.budget import month_of
from jobhunter.llm.errors import ClaimLost
from jobhunter.llm.gateway import LLMGateway
from jobhunter.llm.prompts.render import prompt_schema_v2
from jobhunter.llm.spend_ledger import CAP_REACHED_ALERT
from jobhunter.scoring import cycles
from jobhunter.worker import tasks
from jobhunter.worker.executions import EXECUTIONS
from jobhunter.worker.runtime import ShutdownState
from langchain_core.runnables import RunnableConfig
from procrastinate.job_context import JobContext
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.clock import ManualSleep
from tests.support.faults import PausePoint, SimulatedCrash, raising_at
from tests.support.llm import FakeLLM, open_alerts, save_budget, tries
from tests.support.logs import logged
from tests.support.procs import await_until
from tests.support.scoring import (
    ClaimedTarget,
    CycleRunner,
    PendingTarget,
    claim_again,
    claimed_target,
    cycle_of,
    cycle_runner,
    insert_targets,
    invoke_subgraph,
    reclaim_now,
    synthetic_c_draft,
    target_of,
)

pytestmark = pytest.mark.integration
SONNET = "claude-sonnet-5-5"


@pytest.fixture
async def gateway(engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int) -> LLMGateway:
    return fake_llm.gateway(engine)


@pytest.fixture
async def runner(engine: AsyncEngine, gateway: LLMGateway) -> CycleRunner:
    return await cycle_runner(engine, gateway)


def answer_all(fake_llm: FakeLLM, targets: list[PendingTarget]) -> None:
    for target in targets:
        fake_llm.answer("generate", target.job_revision_id, synthetic_c_draft())


def stall(fake_llm: FakeLLM, *targets: PendingTarget) -> None:
    """Each target's generator stream holds open after its first event, until its call is cancelled."""
    _, schemas = prompt_schema_v2()
    generator = schemas["generator"]
    assert isinstance(generator, dict)
    fake_llm.record("generate", SONNET, synthetic_c_draft(), schema=generator)
    for target in targets:
        key = fake.answer_key("generate", {"job_revision_id": target.job_revision_id}, 0)
        fake.answer(key, "stall_after_first_event")


class Barrier:
    """An `afault_point` replacement that holds every arrival at `point` until `parties` have arrived (so
    the calls that reach it are in flight together), then lets them all pass."""

    def __init__(self, point: str, parties: int) -> None:
        self.point, self.parties, self.arrived = point, parties, 0
        self._all = asyncio.Event()

    async def __call__(self, name: str) -> None:
        if name != self.point:
            return
        self.arrived += 1
        if self.arrived >= self.parties:
            self._all.set()
        await asyncio.wait_for(self._all.wait(), 10)


class Gate:
    """An `afault_point` replacement that holds every arrival at `point` until `open()`."""

    def __init__(self, point: str) -> None:
        self.point, self.arrived = point, 0
        self._open = asyncio.Event()

    async def __call__(self, name: str) -> None:
        if name != self.point or self._open.is_set():
            return
        self.arrived += 1
        await self._open.wait()

    def open(self) -> None:
        self._open.set()


async def scalar(engine: AsyncEngine, sql: str, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(sa.text(sql), params)).scalar_one()


async def attempt_costs(engine: AsyncEngine, cycle_id: int) -> dict[int, Decimal]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            sa.text("SELECT target_id, cost_usd FROM score_attempts WHERE cycle_id = :c"), {"c": cycle_id}
        )
        return {int(r.target_id): Decimal(r.cost_usd) for r in rows}


def beat_tasks() -> list[asyncio.Task[Any]]:
    return [t for t in asyncio.all_tasks() if t.get_name().startswith("heartbeat:claim:") and not t.done()]


async def until(predicate: Callable[[], Awaitable[bool]]) -> None:
    await await_until(predicate, 10, 0.02)


async def settled_ambiguous(engine: AsyncEngine, calls: int) -> None:
    """Wait until `calls` cancelled streams are settled `ambiguous` (the gateway settles a cancelled stream
    in a task of its own, shielded from the cancellation)."""

    async def settled() -> bool:
        return [(t.state, t.error_code) for t in await tries(engine)].count(
            ("ambiguous", "cancelled")
        ) == calls

    await until(settled)


async def test_a_cycle_claims_three_scores_them_together_and_completes_with_its_report(
    engine: AsyncEngine, runner: CycleRunner, fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets = await insert_targets(engine, 3)
    answer_all(fake_llm, targets)
    together = Barrier(
        "gateway.after_dispatch", 3
    )  # max_concurrency=3: the three calls are in flight at once
    monkeypatch.setattr(gateway_mod, "afault_point", together)
    cycle_id = await runner.start("manual")
    final = await runner.run(cycle_id)
    assert together.arrived == 3
    row = await cycle_of(engine, cycle_id)
    costs = await attempt_costs(engine, cycle_id)
    assert set(costs) == {t.target_id for t in targets} and all(cost > 0 for cost in costs.values())
    spent = sum(costs.values(), Decimal(0))
    assert (row.status, row.status_reason, row.admitted) == ("completed", None, {"similarity": 3})
    assert row.spent_usd == spent
    report = row.report
    assert report["outcomes"] == {"published_accepted": 3}
    assert report["lanes"] == {"similarity": 3}
    assert Decimal(report["spent_usd"]) == spent
    assert report["c_hat"] == str(row.c_hat_usd) and report["admission_size"] == row.admission_size >= 3
    assert report["claim_rounds"] == 1 and final["claim_rounds"] == 1
    assert set(report["cache"]) == {"reads", "writes", "hit_ratio"}
    # Codex r1 #13: one TargetOutcome per work id, projected from the final state and the attempt row
    assert final["results"] == {
        work_id(t.target_id, 1): {
            "target_id": t.target_id,
            "attempt_no": 1,
            "outcome": "published_accepted",
            "state": "published_accepted",
            "lane": "similarity",
            "cost_usd": str(costs[t.target_id]),
        }
        for t in targets
    }
    assert sorted(ref["target_id"] for ref in final["claimed"]) == [t.target_id for t in targets]
    for target in targets:
        assert (await target_of(engine, target.target_id)).state == "published_accepted"
    assert beat_tasks() == [] and EXECUTIONS.get(("cycle", cycle_id)) is None


async def test_a_cycle_completes_after_a_budget_denial(
    engine: AsyncEngine, runner: CycleRunner, fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review Focus 1: the cap set so that the third reservation is denied
    targets = await insert_targets(engine, 3)
    answer_all(fake_llm, targets)
    calibration = await claimed_target(engine)  # the reservation one of these generator calls makes
    fake_llm.answer("generate", calibration.job_revision_id, synthetic_c_draft())
    assert (await invoke_subgraph(runner.deps, calibration))["outcome"] == "published_accepted"
    (first_try,) = await tries(engine)
    reservation = first_try.reserved_usd
    assert reservation > Decimal("0.02")  # a cent of the cap moves the limit by less than half of it
    cycle_id = await runner.start("manual")  # before the cap comes down: a cycle pauses at admission only
    now = utcnow()
    counted = Decimal(
        await scalar(engine, "SELECT counted_usd FROM llm_budget WHERE month = :m", m=month_of(now))
    )
    # the scoring limit 0.95 x (cap - $2.00) lands between `counted + 2r` and `counted + 3r`
    admissible = ((counted + Decimal("2.5") * reservation) / Decimal("0.95")).quantize(
        Decimal("0.01"), rounding=ROUND_CEILING
    )
    await save_budget(engine, now=now, cap_usd=str(admissible + 2))
    held = Gate("gateway.after_reserve")  # nothing settles until the third reservation was denied
    monkeypatch.setattr(gateway_mod, "afault_point", held)
    run = asyncio.create_task(runner.run(cycle_id))

    async def one_deferred() -> bool:
        states = [(await target_of(engine, t.target_id)).state for t in targets]
        return states.count("budget_deferred") == 1 or run.done()

    await until(one_deferred)
    assert held.arrived == 2
    held.open()
    final = await asyncio.wait_for(run, 30)
    states = sorted([(await target_of(engine, t.target_id)).state for t in targets])
    assert states == ["budget_deferred", "published_accepted", "published_accepted"]
    row = await cycle_of(engine, cycle_id)
    assert row.status == "completed"
    assert row.report["outcomes"] == {"budget_exhausted": 1, "published_accepted": 2}
    assert len(final["results"]) == 3
    assert [alert[1] for alert in await open_alerts(engine, CAP_REACHED_ALERT)] == ["critical"]


async def test_a_score_now_trigger_during_a_cycle_is_admitted_in_the_next_claim_round(
    engine: AsyncEngine, runner: CycleRunner, fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review Focus 5, Codex r1 #14: a later round re-admits; it never re-claims the first round's list
    targets = await insert_targets(engine, 3)
    answer_all(fake_llm, targets)
    blocked = PausePoint(nodes_mod.BEFORE_DRAFT)
    monkeypatch.setattr(nodes_mod, "afault_point", blocked)
    cycle_id = await runner.start("manual")
    run = asyncio.create_task(runner.run(cycle_id))
    await asyncio.wait_for(blocked.reached.wait(), 10)  # round 1's fan-out is blocked in `generate`
    (late,) = await insert_targets(engine, 1, lane="score_now")
    fake_llm.answer("generate", late.job_revision_id, synthetic_c_draft())
    blocked.resume()
    final = await asyncio.wait_for(run, 30)
    assert final["claim_rounds"] == 2
    assert len(final["results"]) == 4
    assert final["results"][work_id(late.target_id, 1)]["lane"] == "score_now"
    attempts = sa.text("SELECT target_id, count(*) FROM score_attempts GROUP BY target_id")
    async with engine.connect() as conn:
        counts = {int(r[0]): int(r[1]) for r in await conn.execute(attempts)}
    assert counts == {t.target_id: 1 for t in (*targets, late)}  # the first round's three claimed once
    row = await cycle_of(engine, cycle_id)
    assert row.admitted == {"similarity": 3, "score_now": 1}
    assert (row.report["claim_rounds"], row.report["lanes"]) == (2, {"similarity": 3, "score_now": 1})
    assert (await target_of(engine, late.target_id)).state == "published_accepted"


# ---- the three graph-level heartbeat cases (Ruling P10) -------------------------------------------------


async def test_heartbeat_loss_during_a_blocked_stream_cancels_the_call(
    engine: AsyncEngine, gateway: LLMGateway, fake_llm: FakeLLM
) -> None:
    # Codex r1 #12, r2 B6: the loss cancels the attached child alone; the parent graph goes on
    sleep = ManualSleep()
    runner = await cycle_runner(engine, gateway, sleep=sleep)
    blocked, *siblings = await insert_targets(engine, 3)
    answer_all(fake_llm, siblings)
    stall(fake_llm, blocked)
    cycle_id = await runner.start("manual")
    run = asyncio.create_task(runner.run(cycle_id))

    async def stream_open_and_siblings_published() -> bool:
        states = [(await target_of(engine, t.target_id)).state for t in siblings]
        calls = await scalar(
            engine,
            "SELECT count(*) FROM llm_calls WHERE target_id = :t AND status = 'dispatched'",
            t=blocked.target_id,
        )
        return states == ["published_accepted"] * 2 and calls == 1

    await until(stream_open_and_siblings_published)
    old_attempt = int(
        await scalar(engine, "SELECT id FROM score_attempts WHERE target_id = :t", t=blocked.target_id)
    )
    await reclaim_now(engine, blocked.target_id)  # a rival reclaims the target and claims it again
    await claim_again(engine, blocked.target_id)
    await until(lambda: asyncio.sleep(0, result=sleep.release_all() >= 1 or run.done()))
    final = await asyncio.wait_for(run, 30)  # no NodeCancelledError: the cycle went on
    assert final["results"][work_id(blocked.target_id, 1)]["outcome"] == "claim_lost"
    row = await cycle_of(engine, cycle_id)
    assert row.status == "completed"
    assert row.report["outcomes"] == {"claim_lost": 1, "published_accepted": 2}
    await settled_ambiguous(engine, 1)  # the cancelled stream, settled by the gateway
    written = sa.text("SELECT count(*) FROM score_drafts WHERE attempt_id = :a")
    async with engine.connect() as conn:
        assert (await conn.execute(written, {"a": old_attempt})).scalar_one() == 0  # nothing of the old claim
        published = sa.text("SELECT count(*) FROM llm_results WHERE target_id = :t")
        assert (await conn.execute(published, {"t": blocked.target_id})).scalar_one() == 0
    assert beat_tasks() == []


async def test_queued_claims_are_beaten_before_they_run(
    engine: AsyncEngine, gateway: LLMGateway, fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Codex r2 B5: sixty claims, three of them running and blocked for four minutes of the injected clock; a
    # claim waiting for a fan-out slot is beaten from its first second, so none reaches the reclaim predicate
    sleep = ManualSleep()
    runner = await cycle_runner(engine, gateway, sleep=sleep)
    targets = await insert_targets(engine, 60, lane="score_now")  # Score now: outside S
    answer_all(fake_llm, targets)
    blocked = Gate(nodes_mod.BEFORE_DRAFT)  # the three running attempts hold after their generator call
    monkeypatch.setattr(nodes_mod, "afault_point", blocked)
    cycle_id = await runner.start("score_now")
    run = asyncio.create_task(runner.run(cycle_id))
    await sleep.until(60)  # every claim's beat waits for its first period
    await until(lambda: asyncio.sleep(0, result=blocked.arrived == 3))
    older = (
        "SELECT count(*) FROM score_targets WHERE state = 'in_progress' "
        "AND claim_heartbeat_at < now() - interval '3 minutes'"
    )
    fresh = (
        "SELECT count(*) FROM score_targets WHERE state = 'in_progress' "
        "AND claim_heartbeat_at > now() - interval '10 seconds'"
    )
    for period in range(1, 9):  # 8 x 30 s
        async with engine.begin() as conn:  # the half minute passes for the rows, as it does for the beats
            await conn.execute(
                sa.text(
                    "UPDATE score_targets SET claim_heartbeat_at = claim_heartbeat_at "
                    "- interval '30 seconds' WHERE state = 'in_progress'"
                )
            )
        assert sleep.release_all() == 60
        await sleep.until(60 * (period + 1))  # every beat is done and sleeps again
        assert int(await scalar(engine, fresh)) == 60, period  # every claim's heartbeat advanced
    assert int(await scalar(engine, older)) == 0  # the 3-minute predicate reclaims none
    assert int(await scalar(engine, "SELECT count(*) FROM llm_calls")) == 3  # the other 57 still queue
    blocked.open()
    final = await asyncio.wait_for(run, 120)
    assert len(final["results"]) == 60
    assert (await cycle_of(engine, cycle_id)).report["outcomes"] == {"published_accepted": 60}
    assert beat_tasks() == []


async def test_a_cancelled_cycle_cancels_its_children_and_stops_every_beat(
    engine: AsyncEngine, gateway: LLMGateway, fake_llm: FakeLLM
) -> None:
    sleep = ManualSleep()
    runner = await cycle_runner(engine, gateway, sleep=sleep)
    targets = await insert_targets(engine, 2)
    stall(fake_llm, *targets)
    cycle_id = await runner.start("manual")
    run = asyncio.create_task(runner.run(cycle_id))

    async def both_open() -> bool:
        return int(await scalar(engine, "SELECT count(*) FROM llm_calls WHERE status = 'dispatched'")) == 2

    await until(both_open)
    assert len(beat_tasks()) == 2
    run.cancel()  # the worker's shutdown
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, 30)  # the cancellation propagates
    assert beat_tasks() == []
    await settled_ambiguous(engine, 2)
    assert len(await tries(engine)) == 2
    assert (await cycle_of(engine, cycle_id)).status == "running"  # no write: its next owner records it
    for target in targets:
        assert (await target_of(engine, target.target_id)).state == "in_progress"


# ---- a child's failure (Rulings T11-a, T11-f; Controller carries T9-b, T9-c) ----------------------------


def before_node(
    monkeypatch: pytest.MonkeyPatch, name: str, step: Callable[[TargetState], Awaitable[None]]
) -> None:
    """The subgraph's node `name` (built by its `make_<name>`) awaits `step(state)` first, in the graphs a
    `cycle_runner` builds after this."""
    maker = f"make_{name}"
    real = getattr(subgraph_mod, maker)

    def make(deps: Any) -> Any:
        node = real(deps)

        async def wrapped(state: TargetState, config: RunnableConfig) -> Any:
            await step(state)
            return await node(state, config)

        return wrapped

    monkeypatch.setattr(subgraph_mod, maker, make)


def child_tasks() -> list[asyncio.Task[Any]]:
    return [t for t in asyncio.all_tasks() if t.get_name().startswith("score_target:") and not t.done()]


async def attempts_of(engine: AsyncEngine, target_id: int) -> list[tuple[Any, ...]]:
    rows = sa.text(
        "SELECT attempt_no, outcome, outcome_detail FROM score_attempts WHERE target_id = :t ORDER BY id"
    )
    async with engine.connect() as conn:
        return [tuple(r) for r in await conn.execute(rows, {"t": target_id})]


async def test_a_node_timeout_backs_the_target_off_and_fails_it_transient_at_the_attempt_limit(
    engine: AsyncEngine,
    gateway: LLMGateway,
    fake_llm: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Ruling T11-a (a): a deterministic node's `NodeTimeoutError` (`validate`, after the generator call that
    # charged the attempt) takes the transient path of `fail` with the code `node_timeout`
    backed_off, spent, sibling = await insert_targets(engine, 3)
    answer_all(fake_llm, [backed_off, spent, sibling])
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE score_targets SET transient_failures = 2 WHERE id = :t"),
            {"t": backed_off.target_id},
        )
        await conn.execute(
            sa.text("UPDATE score_targets SET charged_attempts = 3 WHERE id = :t"), {"t": spent.target_id}
        )
    stalled = {backed_off.target_id, spent.target_id}

    async def stalls(state: TargetState) -> None:
        if state["target_id"] in stalled:
            await asyncio.sleep(60)  # past the node's run timeout below

    timeouts = {**subgraph_mod.NODE_TIMEOUTS, "validate": timedelta(seconds=2)}
    monkeypatch.setattr(subgraph_mod, "NODE_TIMEOUTS", MappingProxyType(timeouts))
    before_node(monkeypatch, "validate", stalls)
    runner = await cycle_runner(engine, gateway)
    cycle_id = await runner.start("manual")
    started = utcnow()
    final = await runner.run(cycle_id)
    ended = utcnow()
    first = await target_of(engine, backed_off.target_id)
    assert (first.state, first.state_reason, first.claim_id) == ("pending", None, None)
    assert (first.transient_failures, first.charged_attempts) == (3, 1)
    assert started + timedelta(hours=1) <= first.not_before <= ended + timedelta(hours=1)  # 15 min x 2^2
    last = await target_of(engine, spent.target_id)
    assert (last.state, last.state_reason, last.claim_id) == ("failed_transient", "node_timeout", None)
    assert (last.transient_failures, last.charged_attempts) == (0, 4)
    for target in (backed_off, spent):
        assert await attempts_of(engine, target.target_id) == [(1, "transient_error", "node_timeout")]
    assert [
        (
            final["results"][work_id(t.target_id, 1)]["outcome"],
            final["results"][work_id(t.target_id, 1)]["state"],
        )
        for t in (backed_off, spent, sibling)
    ] == [
        ("transient_error", "pending"),
        ("transient_error", "failed_transient"),
        ("published_accepted", "published_accepted"),
    ]
    row = await cycle_of(engine, cycle_id)
    assert row.status == "completed"
    assert row.report["outcomes"] == {"transient_error": 2, "published_accepted": 1}
    assert logged(caplog, "score_target_failed") == []  # a timeout is no bug


async def test_an_unclassified_failure_ends_the_target_failed_invalid_output_for_a_bug(
    engine: AsyncEngine,
    gateway: LLMGateway,
    fake_llm: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Rulings T11-a (d), T11-f: a code fault is terminal, `failed_invalid_output('bug')`, so a later claim
    # round does not admit it again, even in lane `score_now`
    (failing,) = await insert_targets(engine, 1, lane="score_now")
    (sibling,) = await insert_targets(engine, 1)
    answer_all(fake_llm, [sibling])

    async def raises(state: TargetState) -> None:
        if state["target_id"] == failing.target_id:  # a deterministic node's unmapped error
            raise RuntimeError("synthetic failure 4111-1111-1111-1111")

    before_node(monkeypatch, "load_context", raises)
    runner = await cycle_runner(engine, gateway)
    cycle_id = await runner.start("manual")
    final = await runner.run(cycle_id)
    assert final["claim_rounds"] == 1 and len(final["results"]) == 2
    outcome = final["results"][work_id(failing.target_id, 1)]
    assert (outcome["outcome"], outcome["state"]) == ("failed_invalid_output", "failed_invalid_output")
    target = await target_of(engine, failing.target_id)
    assert (target.state, target.state_reason, target.claim_id) == ("failed_invalid_output", "bug", None)
    assert await attempts_of(engine, failing.target_id) == [(1, "failed_invalid_output", "bug")]
    row = await cycle_of(engine, cycle_id)
    assert row.status == "completed"
    assert row.report["outcomes"] == {"failed_invalid_output": 1, "published_accepted": 1}
    [failed] = logged(caplog, "score_target_failed")
    assert (failed["target_id"], failed["error_class"]) == (failing.target_id, "NodeFailed")
    assert "4111" not in caplog.text


async def test_the_claim_s_own_fence_inside_the_attempt_is_claim_lost_and_no_error(
    engine: AsyncEngine,
    gateway: LLMGateway,
    fake_llm: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Ruling T11-a (b): `ClaimLost` (the claim predicate failed under an owner lock) is ownership loss: the
    # rival that reclaimed the target ended the attempt, and the target is the rival's now
    lost, sibling = await insert_targets(engine, 2)
    answer_all(fake_llm, [sibling])
    rival: list[ClaimedTarget] = []

    async def fenced(state: TargetState) -> None:
        if state["target_id"] == lost.target_id:
            await reclaim_now(engine, lost.target_id)
            rival.append(await claim_again(engine, lost.target_id))
            raise ClaimLost()

    before_node(monkeypatch, "load_context", fenced)
    runner = await cycle_runner(engine, gateway)
    cycle_id = await runner.start("manual")
    final = await runner.run(cycle_id)
    outcome = final["results"][work_id(lost.target_id, 1)]
    assert (outcome["outcome"], outcome["state"]) == ("claim_lost", "in_progress")
    target = await target_of(engine, lost.target_id)
    assert (target.state, str(target.claim_id), target.attempt_no) == ("in_progress", rival[0].claim_id, 2)
    assert await attempts_of(engine, lost.target_id) == [
        (1, "claim_lost", "stale_heartbeat"),
        (2, "running", None),
    ]
    row = await cycle_of(engine, cycle_id)
    assert row.status == "completed"
    assert row.report["outcomes"] == {"claim_lost": 1, "published_accepted": 1}
    assert logged(caplog, "score_target_failed") == []
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []


async def serialization_failure(conn: AsyncConnection) -> None:
    """A real serialization failure (SQLSTATE 40001) on `conn`, which rolls its transaction back."""
    await conn.execute(
        sa.text("DO $$ BEGIN RAISE EXCEPTION 'could not serialize access' USING ERRCODE = '40001'; END $$")
    )


async def test_a_retryable_db_error_past_the_node_s_retries_backs_the_target_off(
    engine: AsyncEngine,
    gateway: LLMGateway,
    fake_llm: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Ruling T11-g: a serialization failure that outlasted `DB_RETRY` (a `NodeFailed` with
    # `retryable_db_error`) is transient, code `db_transient`, never a bug
    failing, sibling = await insert_targets(engine, 2)
    answer_all(fake_llm, [sibling])
    conflicts: list[int] = []

    async def conflicting(state: TargetState) -> None:
        if state["target_id"] == failing.target_id:  # the node's database work, every try
            conflicts.append(state["target_id"])
            async with engine.begin() as conn:
                await serialization_failure(conn)

    before_node(monkeypatch, "load_context", conflicting)
    runner = await cycle_runner(engine, gateway)
    cycle_id = await runner.start("manual")
    started = utcnow()
    final = await runner.run(cycle_id)
    ended = utcnow()
    assert len(conflicts) == DB_RETRY.max_attempts == 3
    target = await target_of(engine, failing.target_id)
    assert (target.state, target.state_reason, target.claim_id) == ("pending", None, None)
    assert (target.transient_failures, target.charged_attempts) == (1, 0)
    assert started + timedelta(minutes=15) <= target.not_before <= ended + timedelta(minutes=15)
    assert await attempts_of(engine, failing.target_id) == [(1, "transient_error", "db_transient")]
    outcome = final["results"][work_id(failing.target_id, 1)]
    assert (outcome["outcome"], outcome["state"]) == ("transient_error", "pending")
    row = await cycle_of(engine, cycle_id)
    assert row.status == "completed"
    assert row.report["outcomes"] == {"transient_error": 1, "published_accepted": 1}
    assert logged(caplog, "score_target_failed") == []


class FailsToWrite:
    """`fail`'s `transition` of the target row `target_id`: its first `times` calls raise a serialization
    failure in `fail`'s own transaction (`hold_s`: or sleep that long first, a stalled lock wait)."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, target_id: int, *, times: int, hold_s: float = 0.0
    ) -> None:
        self.raised = 0
        real = real_transition  # the one `fail` imports

        async def transition(
            conn: AsyncConnection, table: str, row_id: int, *args: Any, **kwargs: Any
        ) -> Any:
            if table == "score_targets" and row_id == target_id and self.raised < times:
                self.raised += 1
                await asyncio.sleep(hold_s)
                await serialization_failure(conn)
            return await real(conn, table, row_id, *args, **kwargs)

        monkeypatch.setattr(fail_node, "transition", transition)


def raising_in_load_context(monkeypatch: pytest.MonkeyPatch, target: PendingTarget) -> None:
    async def raises(state: TargetState) -> None:
        if state["target_id"] == target.target_id:
            raise RuntimeError("synthetic failure")

    before_node(monkeypatch, "load_context", raises)


async def test_a_retryable_db_error_of_the_failure_path_is_tried_again(
    engine: AsyncEngine, gateway: LLMGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `fail`, run outside the subgraph, takes up to three tries on a retryable database error
    (failing,) = await insert_targets(engine, 1)
    raising_in_load_context(monkeypatch, failing)
    conflict = FailsToWrite(monkeypatch, failing.target_id, times=1)
    runner = await cycle_runner(engine, gateway)
    cycle_id = await runner.start("manual")
    final = await runner.run(cycle_id)
    assert conflict.raised == 1
    assert final["results"][work_id(failing.target_id, 1)]["outcome"] == "failed_invalid_output"
    target = await target_of(engine, failing.target_id)
    assert (target.state, target.state_reason) == ("failed_invalid_output", "bug")
    assert await attempts_of(engine, failing.target_id) == [(1, "failed_invalid_output", "bug")]
    assert (await cycle_of(engine, cycle_id)).status == "completed"


async def test_a_failure_path_that_keeps_failing_escapes_after_three_tries(
    engine: AsyncEngine, gateway: LLMGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    (failing,) = await insert_targets(engine, 1)
    raising_in_load_context(monkeypatch, failing)
    conflict = FailsToWrite(monkeypatch, failing.target_id, times=10)
    runner = await cycle_runner(engine, gateway)
    cycle_id = await runner.start("manual")
    with pytest.raises(NodeFailed, match="node score_target failed: OperationalError"):
        await runner.run(cycle_id)
    assert conflict.raised == 3
    assert (await target_of(engine, failing.target_id)).state == "in_progress"  # nothing written
    assert await attempts_of(engine, failing.target_id) == [(1, "running", None)]


async def test_a_stalled_failure_path_is_cut_at_the_fail_node_s_timeout(
    engine: AsyncEngine, gateway: LLMGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    (failing,) = await insert_targets(engine, 1)
    raising_in_load_context(monkeypatch, failing)
    timeouts = {**subgraph_mod.NODE_TIMEOUTS, "fail": timedelta(seconds=1)}
    monkeypatch.setattr(subgraph_mod, "NODE_TIMEOUTS", MappingProxyType(timeouts))
    FailsToWrite(monkeypatch, failing.target_id, times=1, hold_s=60)
    runner = await cycle_runner(engine, gateway)
    cycle_id = await runner.start("manual")
    with pytest.raises(NodeFailed, match="node score_target failed: TimeoutError"):
        await asyncio.wait_for(runner.run(cycle_id), 20)
    assert (await target_of(engine, failing.target_id)).state == "in_progress"


async def test_an_attach_that_raises_leaves_no_attempt_running(
    engine: AsyncEngine, gateway: LLMGateway, fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets = await insert_targets(engine, 1)
    stall(fake_llm, *targets)

    def attach(self: ClaimHeartbeats, ref: Any, child: Any) -> None:
        raise ValueError("synthetic attach failure")

    monkeypatch.setattr(ClaimHeartbeats, "attach", attach)
    runner = await cycle_runner(engine, gateway)
    cycle_id = await runner.start("manual")
    with pytest.raises(NodeFailed, match="score_target failed: ValueError"):
        await runner.run(cycle_id)
    assert child_tasks() == [] and beat_tasks() == []
    assert int(await scalar(engine, "SELECT count(*) FROM llm_calls")) == 0  # the attempt never ran


@pytest.mark.filterwarnings("ignore:coroutine 'arun_with_retry' was never awaited:RuntimeWarning")
async def test_a_cancelled_cycle_with_queued_claims_leaves_no_child_and_no_beat(
    engine: AsyncEngine, gateway: LLMGateway, fake_llm: FakeLLM
) -> None:
    # Ruling P10: four claims, three running (`MAX_CONCURRENCY`) and one waiting for a slot, when the worker
    # shuts down; LangGraph drops the waiting task's coroutine unawaited (the filter above)
    sleep = ManualSleep()
    runner = await cycle_runner(engine, gateway, sleep=sleep)
    targets = await insert_targets(engine, 4)
    stall(fake_llm, *targets)
    cycle_id = await runner.start("manual")
    run = asyncio.create_task(runner.run(cycle_id))

    async def three_open() -> bool:
        return int(await scalar(engine, "SELECT count(*) FROM llm_calls WHERE status = 'dispatched'")) == 3

    await until(three_open)
    assert len(beat_tasks()) == 4 and len(child_tasks()) == 3
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, 30)
    assert child_tasks() == [] and beat_tasks() == []
    await settled_ambiguous(engine, 3)
    gc.collect()  # the dropped coroutine is collected here, under this test's filter
    assert (await cycle_of(engine, cycle_id)).status == "running"
    for target in targets:
        assert (await target_of(engine, target.target_id)).state == "in_progress"


# ---- the cycle task (`execute_cycle`) ------------------------------------------------------------------


async def execute(
    runner: CycleRunner, *, admission_stopped: bool = False, heartbeat_period_s: float = 30.0
) -> int | None:
    return await graph_mod.execute_cycle(
        runner.graphs,
        runner.deps,
        trigger="manual",
        epoch=1,
        fake_llm=True,
        admission_stopped=lambda: admission_stopped,
        heartbeat_period_s=heartbeat_period_s,
    )


async def test_the_cycle_task_runs_one_cycle_under_its_heartbeat(
    engine: AsyncEngine, runner: CycleRunner, fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets = await insert_targets(engine, 2)
    answer_all(fake_llm, targets)
    seen: list[object] = []
    real_invoke = graph_mod.invoke_cycle

    async def invoke(graph: Any, ctx: Any) -> Any:
        seen.append(EXECUTIONS.get(("cycle", ctx.cycle_id)))  # the registry's task, for the worker's epoch
        return await real_invoke(graph, ctx)

    monkeypatch.setattr(graph_mod, "invoke_cycle", invoke)
    cycle_id = await execute(runner)
    assert cycle_id is not None
    assert len(seen) == 1 and seen[0] is not None and seen[0][0] == 1  # type: ignore[index]
    assert (await cycle_of(engine, cycle_id)).status == "completed"
    assert EXECUTIONS.get(("cycle", cycle_id)) is None


async def test_an_unclassified_failure_fails_the_cycle_for_a_bug_and_reclaims_its_claims(
    engine: AsyncEngine, runner: CycleRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets = await insert_targets(engine, 2)
    monkeypatch.setattr(claim_node, "afault_point", raising_at(claim_node.AFTER_CLAIM))
    cycle_id = await execute(runner)
    assert cycle_id is not None
    row = await cycle_of(engine, cycle_id)
    assert (row.status, row.status_reason) == ("failed", "bug")
    assert row.report == {"reason": "bug", "error_class": "NodeFailed"}
    assert [(a[1], a[2]) for a in await open_alerts(engine, cycles.FAILED_ALERT)] == [("critical", 1)]
    for target in targets:
        assert (await target_of(engine, target.target_id)).state == "pending"
    outcomes = sa.text("SELECT outcome, outcome_detail FROM score_attempts ORDER BY id")
    async with engine.connect() as conn:
        assert [tuple(r) for r in await conn.execute(outcomes)] == [("claim_lost", "cycle_failed")] * 2
    assert SimulatedCrash.__name__ not in str(row.report)


async def test_a_refused_cycle_beat_cancels_the_graph_and_writes_nothing_more(
    engine: AsyncEngine, runner: CycleRunner, fake_llm: FakeLLM
) -> None:
    targets = await insert_targets(engine, 1)
    stall(fake_llm, *targets)
    task = asyncio.create_task(execute(runner, heartbeat_period_s=0.05))

    async def stream_open() -> bool:
        return int(await scalar(engine, "SELECT count(*) FROM llm_calls WHERE status = 'dispatched'")) == 1

    await until(stream_open)
    async with engine.begin() as conn:  # an orphan sweep ended the cycle meanwhile
        await conn.execute(
            sa.text(
                "UPDATE score_cycles SET status = 'failed', status_reason = 'orphaned', finished_at = now(), "
                'report = \'{"reason": "orphaned"}\''
            )
        )
    cycle_id = await asyncio.wait_for(task, 30)
    assert cycle_id is not None
    row = await cycle_of(engine, cycle_id)
    assert (row.status, row.status_reason, row.report) == ("failed", "orphaned", {"reason": "orphaned"})
    assert await open_alerts(engine, cycles.FAILED_ALERT) == []
    await settled_ambiguous(engine, 1)  # the graph was cancelled


async def test_the_cycle_task_starts_nothing_while_admission_is_stopped(
    engine: AsyncEngine, runner: CycleRunner, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    await insert_targets(engine, 1)
    assert await execute(runner, admission_stopped=True) is None
    assert int(await scalar(engine, "SELECT count(*) FROM score_cycles")) == 0
    assert [e["reason"] for e in logged(caplog, "score_cycle_not_started")] == ["admission_stopped"]


async def test_the_score_cycle_task_runs_a_cycle_and_stops_a_superseded_worker(
    engine: AsyncEngine, runner: CycleRunner, fake_llm: FakeLLM, caplog: pytest.LogCaptureFixture
) -> None:

    targets = await insert_targets(engine, 1)
    answer_all(fake_llm, targets)

    def context(epoch: int) -> tuple[JobContext, ShutdownState]:
        shutdown = ShutdownState()
        runtime = SimpleNamespace(
            score_graphs=runner.graphs,
            score_deps=runner.deps,
            epoch=epoch,
            cfg=SimpleNamespace(fake_llm=True),
            shutdown=shutdown,
        )
        return cast(JobContext, SimpleNamespace(additional_context={"runtime": runtime})), shutdown

    with pytest.raises(ValueError, match="unknown score cycle trigger"):
        await tasks.score_cycle(context(1)[0], trigger="sometimes")
    stale, stale_shutdown = context(0)  # a superseded worker's delivery: its first transaction is fenced
    await tasks.score_cycle(stale, trigger="manual")
    assert stale_shutdown.requested.is_set()
    assert int(await scalar(engine, "SELECT count(*) FROM score_cycles")) == 0
    current, shutdown = context(1)
    await tasks.score_cycle(current, trigger="manual")
    assert not shutdown.requested.is_set()
    assert int(await scalar(engine, "SELECT count(*) FROM score_cycles WHERE status = 'completed'")) == 1
    assert (await target_of(engine, targets[0].target_id)).state == "published_accepted"
