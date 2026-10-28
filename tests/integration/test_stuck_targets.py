"""The `stuck_targets` reconciler on PostgreSQL (spec §7.17 #3, §9.4.3 L3574, §17.4 L5504, §17.6 L5594; plan
M3 Task 18, A2 S4, Deviation 28, Rulings P10, P29).

An `in_progress` target whose claim heartbeat is older than 3 minutes and whose current attempt is still
`running` is reclaimed through the reclaim statements the takeover sweep shares (`cycles.reclaim`): `pending`
while charged attempts are left, else `failed_attempts_exhausted('stuck')` with its follow-up hold released;
the attempt ends `claim_lost('stale_heartbeat')`, `jobhunter_claim_reclaims_total` counts it after the
commit, and `scoring.stuck_target_repaired` (warn, subject the hour) is raised per pass with reclaims and
resolved by a pass after an hour without one. A target whose attempt a fenced publish ended `cancelled`
is left for the purge's chunk, `in_batch` is exempt, a fresh beat (a cycle's queued claims) is never
reclaimed, and the reconciler follows the fence rule.

Every datum is invented.
"""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.graphs.score import nodes as nodes_mod
from jobhunter.llm.gateway import LLMGateway
from jobhunter.observability import metrics
from jobhunter.scoring import cycles
from jobhunter.scoring import reconcilers as scoring_reconcilers
from jobhunter.worker.reconcilers import ReconcileContext
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.clock import ManualSleep
from tests.support.llm import FakeLLM, open_alerts
from tests.support.m3 import NOW, insert_attempt, insert_bulk_operation, insert_job, insert_submission
from tests.support.m3 import insert_target as insert_target_row
from tests.support.procs import await_until
from tests.support.reconcile import assert_fence_rule, run_reconciler
from tests.support.scoring import (
    ClaimedTarget,
    activate_score_tuple,
    attempt_end,
    claim_again,
    claimed_target,
    cycle_of,
    cycle_runner,
    followup_hold,
    hold_of,
    insert_target,
    insert_targets,
    reclaim_now,
    synthetic_c_draft,
    target_of,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _task_app(queue_app: procrastinate.App) -> procrastinate.App:
    """The process's task app, which a reconciler's context names (`run_reconciler`)."""
    return queue_app


ALERT: Final = "scoring.stuck_target_repaired"
_AGE: Final = sa.text(
    "UPDATE score_targets SET claim_heartbeat_at = now() - make_interval(secs => :s) WHERE id = :t"
)
_CHARGED: Final = sa.text("UPDATE score_targets SET charged_attempts = :n WHERE id = :t")
# Every column a reclaim may change, for the equivalence of the statements (ids and times aside).
_RECLAIMED_ROW: Final = sa.text(
    "SELECT state, state_reason, attempt_no, charged_attempts, claim_id, claim_epoch, "
    "claim_heartbeat_at, lane, always_include_reason, override, not_before, transient_failures, "
    "resume_draft_id, published_result_id, batch_submission_id FROM score_targets WHERE id = :t"
)


def hour(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H")


def reclaims() -> float:
    return metrics.REGISTRY.get_sample_value("jobhunter_claim_reclaims_total") or 0.0


async def stuck(engine: AsyncEngine, *, seconds: int = 240, charged: int = 0) -> ClaimedTarget:
    """A claimed target (attempt 1 `running`) whose heartbeat is `seconds` old, with `charged` attempts."""
    claimed = await claimed_target(engine)
    async with engine.begin() as conn:
        await conn.execute(_AGE, {"s": seconds, "t": claimed.target_id})
        await conn.execute(_CHARGED, {"n": charged, "t": claimed.target_id})
    return claimed


async def run(engine: AsyncEngine, now: datetime) -> int:
    return await run_reconciler("stuck_targets", engine, now=now, epoch=1)


def ctx(engine: AsyncEngine, queue_app: procrastinate.App, *, epoch: int = 1) -> ReconcileContext:
    return ReconcileContext(engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow(), epoch=epoch)


async def test_a_stale_claim_is_reclaimed_to_pending_its_attempt_claim_lost_and_counted(
    engine: AsyncEngine, current_epoch: int
) -> None:
    claimed = await stuck(engine, charged=3)  # charged attempts left: 3 < 4
    before, now = reclaims(), utcnow()
    assert await run(engine, now) == 1
    target = await target_of(engine, claimed.target_id)
    assert (target.state, target.state_reason, target.claim_id, target.claim_epoch) == (
        "pending",
        None,
        None,
        None,
    )
    assert (target.claim_heartbeat_at, target.charged_attempts, target.attempt_no) == (None, 3, 1)
    ended = await attempt_end(engine, claimed.attempt_id)
    assert (ended.outcome, ended.outcome_detail) == ("claim_lost", "stale_heartbeat")
    assert ended.ended_at is not None
    assert reclaims() == before + 1  # counted once its transaction committed
    assert await open_alerts(engine, ALERT) == [(f"{ALERT}:{hour(now)}", "warn", 1)]
    assert await run(engine, now) == 0  # repaired once
    assert reclaims() == before + 1


async def test_a_stale_claim_without_charged_attempts_left_fails_stuck_and_releases_its_hold(
    engine: AsyncEngine, current_epoch: int
) -> None:
    claimed = await stuck(engine, charged=4)
    async with engine.begin() as conn:
        operation, item = await followup_hold(
            conn, claimed.llm_tuple_id, claimed.target_id, hold=Decimal("0.040000")
        )
    assert await run(engine, utcnow()) == 1
    target = await target_of(engine, claimed.target_id)
    assert (target.state, target.state_reason, target.claim_id) == (
        "failed_attempts_exhausted",
        "stuck",
        None,
    )
    ended = await attempt_end(engine, claimed.attempt_id)
    assert (ended.outcome, ended.outcome_detail) == ("claim_lost", "stale_heartbeat")
    released = await hold_of(engine, operation, item)  # a terminal move gives the follow-up hold back
    assert released.followup_held_usd == 0 and released.hold_released_at is not None


async def test_a_claim_younger_than_three_minutes_is_left_alone(
    engine: AsyncEngine, current_epoch: int
) -> None:
    claimed = await stuck(engine, seconds=170)
    before = reclaims()
    assert await run(engine, utcnow()) == 0
    assert (await target_of(engine, claimed.target_id)).state == "in_progress"
    assert (await attempt_end(engine, claimed.attempt_id)).outcome == "running"
    assert reclaims() == before and await open_alerts(engine, ALERT) == []


async def test_a_target_whose_fenced_publish_cancelled_its_attempt_is_left_for_the_purge(
    engine: AsyncEngine, current_epoch: int
) -> None:
    # A2 S4: the fenced publish ended the attempt `cancelled('purge_fenced')`, the target left `in_progress`
    claimed = await stuck(engine, seconds=600)
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "UPDATE score_attempts SET outcome = 'cancelled', outcome_detail = 'purge_fenced', "
                "ended_at = now() WHERE id = :a"
            ),
            {"a": claimed.attempt_id},
        )
    assert await run(engine, utcnow()) == 0
    target = await target_of(engine, claimed.target_id)
    assert (target.state, str(target.claim_id)) == ("in_progress", claimed.claim_id)
    assert (await attempt_end(engine, claimed.attempt_id)).outcome == "cancelled"
    assert await open_alerts(engine, ALERT) == []


async def test_an_in_batch_target_is_exempt(engine: AsyncEngine, current_epoch: int) -> None:
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        operation = await insert_bulk_operation(conn, tuple_id)
        submission = await insert_submission(
            conn,
            tuple_id,
            operation,
            state="submitted",
            submit_started_at=NOW,
            provider_batch_id="msgbatch_stuck_exempt",
        )
        target = await insert_target_row(
            conn,
            await insert_job(conn),
            tuple_id,
            state="in_batch",
            batch_submission_id=submission,
            attempt_no=1,
            claim_heartbeat_at=NOW - timedelta(hours=1),  # an old beat; no claim
        )
        attempt = await insert_attempt(conn, target, mode="batch")
    assert await run(engine, utcnow()) == 0
    assert (await target_of(engine, target)).state == "in_batch"
    assert (await attempt_end(engine, attempt)).outcome == "running"


async def test_the_hourly_alert_counts_each_pass_with_reclaims_and_resolves_after_an_hour_without(
    engine: AsyncEngine, current_epoch: int
) -> None:
    await stuck(engine)
    now = utcnow()
    assert await run(engine, now) == 1
    await stuck(engine)
    await stuck(engine)
    assert await run(engine, now) == 2  # the same hour: one more occurrence of the same alert
    assert await open_alerts(engine, ALERT) == [(f"{ALERT}:{hour(now)}", "warn", 2)]
    async with engine.connect() as conn:
        context = (
            await conn.execute(sa.text("SELECT context FROM alerts WHERE code = :c"), {"c": ALERT})
        ).scalar_one()
    assert context == {"reclaimed": 1, "pending": 1, "exhausted": 0}  # the first raise's, ids and counts only
    assert await run(engine, now + timedelta(minutes=30)) == 0  # a reclaim within the last hour: kept
    assert await open_alerts(engine, ALERT) == [(f"{ALERT}:{hour(now)}", "warn", 2)]
    assert await run(engine, now + timedelta(minutes=61)) == 0  # an hour without reclaims: resolved
    assert await open_alerts(engine, ALERT) == []
    await stuck(engine)
    later = now + timedelta(hours=2)
    assert await run(engine, later) == 1  # a later hour's reclaims open that hour's alert
    assert await open_alerts(engine, ALERT) == [(f"{ALERT}:{hour(later)}", "warn", 1)]


async def test_the_stuck_reclaim_is_the_reclaim_of_the_helper_and_of_the_takeover_sweep(
    engine: AsyncEngine, current_epoch: int
) -> None:
    # Rulings P29, P10: `reclaim_now` (Task 9's helper), `stuck_targets` and the takeover sweep
    # `score_targets` (Task 11) move equal claims to equal rows; the attempt's detail names the reclaimer.
    async def reclaimed(claimed: ClaimedTarget) -> tuple[Any, ...]:
        async with engine.connect() as conn:
            row = (await conn.execute(_RECLAIMED_ROW, {"t": claimed.target_id})).one()
        ended = await attempt_end(engine, claimed.attempt_id)
        return (*row, ended.outcome)

    by_helper = await stuck(engine, charged=2)
    await reclaim_now(engine, by_helper.target_id)
    by_reconciler = await stuck(engine, charged=2)
    assert await run(engine, utcnow()) == 1
    by_sweep = await stuck(engine, charged=2)
    async with engine.begin() as conn:
        assert await cycles.sweep_older_epoch_claims(conn, 2) == 1  # a takeover by epoch 2
    expected = await reclaimed(by_helper)
    assert expected[:2] == ("pending", None) and expected[-1] == "claim_lost"
    assert await reclaimed(by_reconciler) == expected
    assert await reclaimed(by_sweep) == expected
    details = [(await attempt_end(engine, c.attempt_id)).outcome_detail for c in (by_helper, by_reconciler)]
    assert details == ["stale_heartbeat", "stale_heartbeat"]
    assert (await attempt_end(engine, by_sweep.attempt_id)).outcome_detail == cycles.WORKER_TAKEOVER
    again = await claim_again(engine, by_reconciler.target_id)  # the reclaimed target is claimable again
    assert again.attempt_no == 2


async def test_the_cycle_s_queued_claims_are_beaten_and_never_reclaimed(
    engine: AsyncEngine,
    current_epoch: int,
    fake_llm: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Ruling P10: the reconciler half of Task 11's `test_queued_claims_are_beaten_before_they_run`. Six
    # claims, three running and blocked, three waiting for a fan-out slot, for four minutes of the injected
    # clock: every claim's beat keeps its heartbeat fresh, so `stuck_targets` reclaims none of them.
    gateway: LLMGateway = fake_llm.gateway(engine)
    sleep = ManualSleep()
    runner = await cycle_runner(engine, gateway, sleep=sleep)
    targets = await insert_targets(engine, 6, lane="score_now")
    for target in targets:
        fake_llm.answer("generate", target.job_revision_id, synthetic_c_draft())
    blocked = Gate(nodes_mod.BEFORE_DRAFT)
    monkeypatch.setattr(nodes_mod, "afault_point", blocked)
    cycle_id = await runner.start("score_now")
    running = asyncio.create_task(runner.run(cycle_id))
    try:
        await sleep.until(6)  # every claim's beat waits for its first period
        await await_until(lambda: asyncio.sleep(0, result=blocked.arrived == 3), 10, 0.02)
        before = reclaims()
        for period in range(1, 9):  # 8 x 30 s
            async with engine.begin() as conn:  # the half minute passes for the rows, as for the beats
                await conn.execute(
                    sa.text(
                        "UPDATE score_targets SET claim_heartbeat_at = claim_heartbeat_at "
                        "- interval '30 seconds' WHERE state = 'in_progress'"
                    )
                )
            # the oldest a beat ever gets (a period since the last one): none is stuck, also a queued claim's
            assert await run(engine, utcnow()) == 0, period
            assert sleep.release_all() == 6
            await sleep.until(6 * (period + 1))  # every beat is done and sleeps again
        assert reclaims() == before
    except BaseException:  # a failed check leaves no cycle running into the next test
        blocked.open()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        raise
    blocked.open()
    final = await asyncio.wait_for(running, 120)
    assert len(final["results"]) == 6
    assert (await cycle_of(engine, cycle_id)).report["outcomes"] == {"published_accepted": 6}


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


async def test_stuck_targets_keeps_the_fence_rule(
    engine: AsyncEngine, current_epoch: int, queue_app: procrastinate.App
) -> None:
    claimed = await stuck(engine)
    await assert_fence_rule(
        "stuck_targets",
        "tick",
        ctx(engine, queue_app, epoch=current_epoch + 1),
        ctx(engine, queue_app),
        scoring_reconcilers,
    )
    assert (await target_of(engine, claimed.target_id)).state == "in_progress"  # neither check wrote
    assert await open_alerts(engine, ALERT) == []
    pending = await insert_target(engine)  # a pending target is never a reclaim's
    assert await run(engine, utcnow()) == 1
    assert (await target_of(engine, pending.target_id)).state == "pending"
