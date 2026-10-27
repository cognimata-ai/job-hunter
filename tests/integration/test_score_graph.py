"""`score_graph` on PostgreSQL with the fake LLM and keyed answers (spec §9.3, §9.4.3; plan Task 11, Review
Focus 1 and 5, Codex r1 #12-#14, r2 B5, B6, Rulings P10, P34, Controller carries T9-b, T9-c): three pending
targets of different jobs scored by one cycle.

A cycle claims three, scores them concurrently and completes with a report whose lane and outcome counts
match, one `TargetOutcome` per work id and `spent_usd` the attempts' cost; a budget denial ends one target
`budget_deferred` and the cycle still completes; a Score now that arrives during the fan-out is admitted in
the next claim round. The three graph-level heartbeat cases (Ruling P10): a heartbeat loss during a blocked
stream cancels that call alone, queued claims are beaten before they run, and a cancelled cycle cancels its
children and stops every beat. A child's failure becomes its outcome (the attempt reclaimed); the cycle task
(`graph.execute_cycle`) runs one cycle under its heartbeat, fails it for a bug with its claims reclaimed,
stops when its beat is refused, and starts nothing while admission is stopped.

Every datum is invented.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from decimal import ROUND_CEILING, Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.graphs.score import graph as graph_mod
from jobhunter.graphs.score import nodes as nodes_mod
from jobhunter.graphs.score import subgraph as subgraph_mod
from jobhunter.graphs.score.nodes import claim as claim_node
from jobhunter.graphs.score.nodes.load_context import make_load_context as real_make_load_context
from jobhunter.graphs.score.state import TargetState, work_id
from jobhunter.llm import fake
from jobhunter.llm import gateway as gateway_mod
from jobhunter.llm.budget import month_of
from jobhunter.llm.gateway import LLMGateway
from jobhunter.llm.prompts.render import prompt_schema_v2
from jobhunter.llm.spend_ledger import CAP_REACHED_ALERT
from jobhunter.scoring import cycles
from jobhunter.worker import tasks
from jobhunter.worker.executions import EXECUTIONS
from jobhunter.worker.runtime import ShutdownState
from langchain_core.runnables import RunnableConfig
from procrastinate.job_context import JobContext
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.clock import ManualSleep
from tests.support.faults import PausePoint, SimulatedCrash, raising_at
from tests.support.llm import FakeLLM, open_alerts, save_budget, tries
from tests.support.logs import logged
from tests.support.procs import await_until
from tests.support.scoring import (
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


# ---- a child's failure (Controller carries T9-b, T9-c) ----------------------------------------------------


async def test_a_child_failure_becomes_its_outcome_and_the_cycle_goes_on(
    engine: AsyncEngine,
    gateway: LLMGateway,
    fake_llm: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    failing, *siblings = await insert_targets(engine, 3)
    answer_all(fake_llm, siblings)
    real = real_make_load_context

    def make_load_context(deps: Any) -> Any:
        node = real(deps)

        async def load_context(state: TargetState, config: RunnableConfig) -> Any:
            if state["target_id"] == failing.target_id:  # a deterministic node's unmapped error
                raise RuntimeError("synthetic failure 4111-1111-1111-1111")
            return await node(state, config)

        return load_context

    monkeypatch.setattr(subgraph_mod, "make_load_context", make_load_context)
    runner = await cycle_runner(engine, gateway)  # the graphs built with the failing node
    cycle_id = await runner.start("manual")
    final = await runner.run(cycle_id)
    outcome = final["results"][work_id(failing.target_id, 1)]
    assert (outcome["outcome"], outcome["state"]) == ("claim_lost", "pending")  # reclaimed for a new attempt
    row = await cycle_of(engine, cycle_id)
    assert row.status == "completed"
    assert row.report["outcomes"] == {"claim_lost": 1, "published_accepted": 2}
    target = await target_of(engine, failing.target_id)
    assert (target.state, target.claim_id, target.charged_attempts) == ("pending", None, 0)
    attempt = sa.text("SELECT outcome, outcome_detail FROM score_attempts WHERE target_id = :t")
    async with engine.connect() as conn:
        assert tuple((await conn.execute(attempt, {"t": failing.target_id})).one()) == (
            "claim_lost",
            "subgraph_failed",
        )
    [failed] = logged(caplog, "score_target_failed")
    assert (failed["target_id"], failed["error_class"]) == (failing.target_id, "NodeFailed")
    assert "4111" not in caplog.text


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
