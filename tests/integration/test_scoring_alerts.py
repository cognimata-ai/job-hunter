"""The admission and scoring alerts, `critic_rate_7d` and the bulk counter check on PostgreSQL (spec §6.9.1,
§9.6.5 L3686-3688, §11.13 L4633-4635, §17.4, §17.6 L5595; plan M3 Task 18, A1 issue 29, A2 A8, Deviations
19, 28, Codex r3 B15, Ruling P32).

Each alert's condition and its resolution on seeded rows, with the injected clock:
- `admission.exploration_hits` (warn): over 14 days, ten or more published targets whose publishing attempt
  ran in the `exploration` lane, more than 25% with a current instance's `score_mid` at the relevance
  threshold or above; resolved at 25% or less;
- `admission.high_sim_expired` (warn): three or more targets expired in the last 24 h with a similarity at the
  70th percentile of the similarity-lane admissions of the last 7 days or above (the attempts' lane, never the
  target's); resolved after 24 h without another;
- `admission.backlog_drain` (info): the pending targets would take longer than `max_ad_age_days` to drain at
  the last 7 days' admissions; raised at most once a day (the subject is the UTC date);
- `scoring.feedback_disagreement` (warn): over 14 days with ten or more labels, more than 30% `poor` at `M ≥
  τ` or `good` at `M < τ - 20`;
- `scoring.distribution_shift` (info): the 7-day mean `score_mid` more than 10 from the previous 28 days';
- `scoring.critic_rate_high` (warn): `critic_rate_7d` (first-round critic calls per charged attempt, `None`
  below 50 attempts) above 1.5 times the policy's `projected_critic_rate`, by `scoring_drift` and at every
  completed cycle (`finalize_cycle`, Ruling P32).
`budget_counters` also corrects an active bulk operation's drifted `spent_usd` from its ledger entries under
the bulk row's lock, with `llm.budget_counter_drift`. Both reconcilers follow the fence rule.

Every datum is invented.
"""

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.llm import spend_ledger
from jobhunter.llm.gateway import LLMGateway
from jobhunter.observability import metrics
from jobhunter.scoring import cycles
from jobhunter.scoring import reconcilers as scoring_reconcilers
from jobhunter.worker.reconcilers import ReconcileContext
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.llm import FakeLLM, open_alerts
from tests.support.m3 import (
    active_ids,
    insert_attempt,
    insert_bulk_operation,
    insert_det_result,
    insert_id,
    insert_job,
    insert_published,
    insert_scoring_call,
    insert_target,
)
from tests.support.reconcile import assert_fence_rule, run_reconciler
from tests.support.scoring import (
    activate_score_tuple,
    activate_test_policy,
    cycle_of,
    cycle_runner,
    insert_targets,
    synthetic_c_draft,
)

pytestmark = pytest.mark.integration

NOW: Final = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
TAU: Final = 60  # the seeded relevance threshold (settings v1)
MAX_AD_AGE_DAYS: Final = 30  # the seeded `scope.max_ad_age_days`
EXPLORATION: Final = "admission.exploration_hits"
HIGH_SIM: Final = "admission.high_sim_expired"
BACKLOG: Final = "admission.backlog_drain"
FEEDBACK: Final = "scoring.feedback_disagreement"
SHIFT: Final = "scoring.distribution_shift"
CRITIC: Final = "scoring.critic_rate_high"
DRIFT: Final = spend_ledger.COUNTER_DRIFT_ALERT


@pytest.fixture(autouse=True)
def _task_app(queue_app: procrastinate.App) -> procrastinate.App:
    """The process's task app, which a reconciler's context names (`run_reconciler`)."""
    return queue_app


@pytest.fixture
async def tuple_id(engine: AsyncEngine, current_epoch: int) -> int:
    """The score tuple made active, under the test evaluation policy (projected critic rate 0.3)."""
    async with engine.begin() as conn:
        await activate_test_policy(conn)
        return await activate_score_tuple(conn)


async def admission(engine: AsyncEngine, now: datetime = NOW) -> int:
    return await run_reconciler("admission_alerts", engine, now=now, epoch=1)


async def drift(engine: AsyncEngine, now: datetime = NOW) -> int:
    return await run_reconciler("scoring_drift", engine, now=now, epoch=1)


def ctx(engine: AsyncEngine, queue_app: procrastinate.App, *, epoch: int = 1) -> ReconcileContext:
    return ReconcileContext(engine=engine, queue=queue_app, defer=defer_in_tx, now=NOW, epoch=epoch)


async def context_of(engine: AsyncEngine, code: str) -> dict[str, Any]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            sa.text("SELECT context FROM alerts WHERE code = :c AND resolved_at IS NULL"), {"c": code}
        )
        (context,) = rows.scalars()
        return dict(context)


async def all_alerts(engine: AsyncEngine, code: str) -> list[tuple[str, bool]]:
    """Every alert of `code`, open or resolved, oldest first: its dedup key and whether it is open."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            sa.text("SELECT dedup_key, resolved_at IS NULL AS open FROM alerts WHERE code = :c ORDER BY id"),
            {"c": code},
        )
        return [(r.dedup_key, bool(r.open)) for r in rows]


async def scope_id(conn: AsyncConnection) -> int:
    return (await active_ids(conn)).scope_version_id


# ---- admission.exploration_hits ---------------------------------------------------------------------------


async def published_in_lane(
    conn: AsyncConnection, tuple_id: int, *, lane: str, mid: int | None, at: datetime
) -> None:
    """A result of the tuple published at `at` by an attempt of `lane`, its current instance under the active
    scope scored `mid` (none when `None`)."""
    job = await insert_job(conn)
    published = await insert_published(conn, job, tuple_id)
    await conn.execute(
        sa.text("UPDATE score_attempts SET lane = :l WHERE id = :a"), {"l": lane, "a": published.attempt_id}
    )
    await conn.execute(
        sa.text("UPDATE llm_results SET published_at = :at WHERE id = :r"),
        {"at": at, "r": published.result_id},
    )
    if mid is not None:
        score = Decimal(mid)
        await insert_det_result(
            conn, published.result_id, job.job_id, await scope_id(conn), low=score, high=score
        )


async def test_exploration_hits_above_a_quarter_of_ten_raise_and_a_quarter_resolves(
    engine: AsyncEngine, tuple_id: int
) -> None:
    async with engine.begin() as conn:
        for mid in (TAU, TAU + 5, 80):  # 3 hits
            await published_in_lane(conn, tuple_id, lane="exploration", mid=mid, at=NOW - timedelta(days=2))
        for _ in range(6):  # 6 misses
            await published_in_lane(
                conn, tuple_id, lane="exploration", mid=TAU - 1, at=NOW - timedelta(days=3)
            )
        # not counted: the similarity lane (the attempt's lane), and a result older than 14 days
        await published_in_lane(conn, tuple_id, lane="similarity", mid=90, at=NOW - timedelta(days=1))
        await published_in_lane(conn, tuple_id, lane="exploration", mid=90, at=NOW - timedelta(days=15))
    assert await admission(engine) == 0  # 9 counted: too few
    assert await open_alerts(engine, EXPLORATION) == []
    async with engine.begin() as conn:  # the tenth, a verified not-a-job-ad (no score): 3 of 10, 30% > 25%
        await published_in_lane(conn, tuple_id, lane="exploration", mid=None, at=NOW - timedelta(days=1))
    assert await admission(engine) == 1
    assert await open_alerts(engine, EXPLORATION) == [(f"{EXPLORATION}:tuple:{tuple_id}", "warn", 1)]
    assert await context_of(engine, EXPLORATION) == {"llm_tuple_id": tuple_id, "published": 10, "hits": 3}
    assert await admission(engine) == 0  # once
    async with engine.begin() as conn:  # two more misses: 3 of 12, exactly 25%: below the threshold
        for _ in range(2):
            await published_in_lane(conn, tuple_id, lane="exploration", mid=10, at=NOW - timedelta(days=1))
    assert await admission(engine) == 1
    assert await open_alerts(engine, EXPLORATION) == []


async def test_a_retried_target_s_lane_is_not_its_publishing_attempt_s(
    engine: AsyncEngine, tuple_id: int
) -> None:
    # Codex r3 B15: `score_targets.lane` is overwritten by a retry; the publishing attempt's lane counts
    async with engine.begin() as conn:
        for _ in range(10):
            await published_in_lane(conn, tuple_id, lane="similarity", mid=90, at=NOW - timedelta(days=1))
        await conn.execute(sa.text("UPDATE score_targets SET lane = 'exploration'"))
    assert await admission(engine) == 0
    assert await open_alerts(engine, EXPLORATION) == []


# ---- admission.high_sim_expired ---------------------------------------------------------------------------


async def similarity_admission(
    conn: AsyncConnection, tuple_id: int, similarity: float, started: datetime
) -> None:
    """A target of `similarity` with an attempt admitted in the `similarity` lane at `started`."""
    target = await insert_target(
        conn, await insert_job(conn), tuple_id, similarity=similarity, lane="exploration"
    )
    await insert_attempt(
        conn, target, lane="similarity", outcome="claim_lost", started_at=started, ended_at=started
    )


async def expired(conn: AsyncConnection, tuple_id: int, similarity: float, at: datetime) -> None:
    await insert_target(
        conn,
        await insert_job(conn),
        tuple_id,
        state="expired_unscored",
        state_reason="too_old",
        state_changed_at=at,
        similarity=similarity,
    )


async def test_three_high_similarity_expiries_in_a_day_raise_and_a_quiet_day_resolves(
    engine: AsyncEngine, tuple_id: int
) -> None:
    async with engine.begin() as conn:
        for tenth in range(1, 11):  # 0.1 .. 1.0: the 70th percentile (interpolated) is 0.73
            await similarity_admission(conn, tuple_id, tenth / 10, NOW - timedelta(days=2))
        await similarity_admission(conn, tuple_id, 0.01, NOW - timedelta(days=8))  # outside the 7 days
        # an attempt of another lane is no similarity-lane admission, whatever the target's lane says
        other = await insert_target(conn, await insert_job(conn), tuple_id, similarity=0.0, lane="similarity")
        await insert_attempt(
            conn, other, lane="oldest", outcome="claim_lost", started_at=NOW - timedelta(days=1)
        )
        for similarity in (0.75, 0.8):
            await expired(conn, tuple_id, similarity, NOW - timedelta(hours=3))
        await expired(conn, tuple_id, 0.72, NOW - timedelta(hours=2))  # below the cut-off
        await expired(conn, tuple_id, 0.95, NOW - timedelta(hours=25))  # more than 24 h ago
    assert await admission(engine) == 0  # two
    assert await open_alerts(engine, HIGH_SIM) == []
    async with engine.begin() as conn:
        await expired(conn, tuple_id, 0.73, NOW - timedelta(hours=1))  # at the cut-off: the third
    assert await admission(engine) == 1
    assert await open_alerts(engine, HIGH_SIM) == [(f"{HIGH_SIM}:tuple:{tuple_id}", "warn", 1)]
    context = await context_of(engine, HIGH_SIM)
    assert context == {"llm_tuple_id": tuple_id, "expired": 3, "similarity_p70": 0.73}
    # 22 h later two of the three left the 24 h window: no alert to raise, but one recurred within 24 h
    assert await admission(engine, NOW + timedelta(hours=22)) == 0
    assert len(await open_alerts(engine, HIGH_SIM)) == 1
    # 24 h after the last one: 24 h without recurrence
    assert await admission(engine, NOW + timedelta(hours=23, minutes=1)) == 1
    assert await open_alerts(engine, HIGH_SIM) == []


# ---- admission.backlog_drain ------------------------------------------------------------------------------


async def pending_targets(conn: AsyncConnection, tuple_id: int, n: int) -> None:
    for _ in range(n):
        await insert_target(conn, await insert_job(conn), tuple_id)


async def admitted(conn: AsyncConnection, tuple_id: int, n: int, *, at: datetime) -> None:
    """`n` attempts started at `at` (admissions of the tuple), on one target."""
    target = await insert_target(
        conn, await insert_job(conn), tuple_id, state="refused", refusal_category="other"
    )
    for attempt_no in range(1, n + 1):
        await insert_attempt(
            conn, target, attempt_no=attempt_no, outcome="refused", started_at=at, ended_at=at
        )


async def test_a_backlog_that_outlasts_the_ad_age_is_raised_at_most_once_a_day(
    engine: AsyncEngine, tuple_id: int
) -> None:
    async with engine.begin() as conn:
        await admitted(conn, tuple_id, 7, at=NOW - timedelta(days=3))  # one a day
        await admitted(conn, tuple_id, 5, at=NOW - timedelta(days=8))  # before the 7 days
        await pending_targets(conn, tuple_id, MAX_AD_AGE_DAYS)  # 30 days to drain: not more than 30
    assert await admission(engine) == 0
    assert await all_alerts(engine, BACKLOG) == []
    async with engine.begin() as conn:
        await pending_targets(conn, tuple_id, 1)  # 31 days
    assert await admission(engine) == 1
    today = f"{BACKLOG}:{NOW.date().isoformat()}"
    assert await open_alerts(engine, BACKLOG) == [(today, "info", 1)]
    assert await context_of(engine, BACKLOG) == {
        "llm_tuple_id": tuple_id,
        "pending": 31,
        "admitted_7d": 7,
        "max_ad_age_days": MAX_AD_AGE_DAYS,
    }
    assert await admission(engine, NOW + timedelta(hours=1)) == 0  # still open: no second raise
    async with engine.begin() as conn:
        await admitted(conn, tuple_id, 1, at=NOW - timedelta(hours=1))  # 8 a week: 27 days, below
    assert await admission(engine, NOW + timedelta(hours=2)) == 1
    assert await open_alerts(engine, BACKLOG) == []
    async with engine.begin() as conn:
        await pending_targets(conn, tuple_id, 10)  # above again on the same day: not raised again
    assert await admission(engine, NOW + timedelta(hours=3)) == 0
    assert await all_alerts(engine, BACKLOG) == [(today, False)]
    tomorrow = NOW + timedelta(days=1)  # the next UTC day: raised again
    assert await admission(engine, tomorrow) == 1
    assert await all_alerts(engine, BACKLOG) == [
        (today, False),
        (f"{BACKLOG}:{tomorrow.date().isoformat()}", True),
    ]


async def test_a_backlog_without_admissions_never_drains(engine: AsyncEngine, tuple_id: int) -> None:
    assert await admission(engine) == 0  # nothing pending
    async with engine.begin() as conn:
        await pending_targets(conn, tuple_id, 1)
    assert await admission(engine) == 1  # nothing admitted in 7 days: it never drains
    assert len(await open_alerts(engine, BACKLOG)) == 1


# ---- scoring.feedback_disagreement ------------------------------------------------------------------------


async def feedback(conn: AsyncConnection, label: str, m: float | None, at: datetime) -> None:
    snapshot = None if m is None else {"L": m - 5, "M": m, "U": m + 5, "coverage": 0.9, "confidence": "high"}
    await insert_id(
        conn,
        "score_feedback",
        {"kind": "label", "label": label, "score_snapshot": snapshot, "created_at": at},
    )


async def test_feedback_disagreement_above_thirty_percent_of_ten_labels_raises_and_resolves(
    engine: AsyncEngine, tuple_id: int
) -> None:
    day = NOW - timedelta(days=1)
    async with engine.begin() as conn:
        for label, m in (("poor", TAU), ("poor", 95.0), ("good", TAU - 20.5)):  # three disagreements
            await feedback(conn, label, m, day)
        for label, m in (("good", TAU - 20), ("poor", TAU - 0.5), ("ok", 99.0), ("good", 90.0)):  # agreements
            await feedback(conn, label, m, day)
        await feedback(conn, "poor", None, day)  # no score: no disagreement
        await feedback(conn, "poor", 90.0, NOW - timedelta(days=15))  # outside the 14 days
    assert await drift(engine) == 0  # 8 labels: too few
    async with engine.begin() as conn:
        await feedback(conn, "poor", 70.0, day)  # 4 of 9
        await feedback(conn, "ok", 70.0, day)  # 4 of 10: 40% > 30%
    assert await drift(engine) == 1
    assert await open_alerts(engine, FEEDBACK) == [(f"{FEEDBACK}:labels", "warn", 1)]
    assert await context_of(engine, FEEDBACK) == {"labels": 10, "disagreeing": 4}
    async with engine.begin() as conn:
        for _ in range(4):  # 4 of 14: 28.6%
            await feedback(conn, "ok", 50.0, day)
    assert await drift(engine) == 1
    assert await open_alerts(engine, FEEDBACK) == []


# ---- scoring.distribution_shift ---------------------------------------------------------------------------


async def test_a_seven_day_mean_more_than_ten_from_the_previous_four_weeks_raises_info(
    engine: AsyncEngine, tuple_id: int
) -> None:
    async with engine.begin() as conn:
        for mid, days in ((50, 10), (60, 20), (55, 34)):  # previous 28 days: mean 55
            await published_in_lane(conn, tuple_id, lane="similarity", mid=mid, at=NOW - timedelta(days=days))
        await published_in_lane(
            conn, tuple_id, lane="similarity", mid=0, at=NOW - timedelta(days=36)
        )  # older
        for mid in (64, 66):  # last 7 days: mean 65
            await published_in_lane(conn, tuple_id, lane="similarity", mid=mid, at=NOW - timedelta(days=1))
    assert await drift(engine) == 0  # exactly 10: not more
    async with engine.begin() as conn:
        await published_in_lane(conn, tuple_id, lane="similarity", mid=68, at=NOW - timedelta(days=2))  # 66
    assert await drift(engine) == 1
    assert await open_alerts(engine, SHIFT) == [(f"{SHIFT}:tuple:{tuple_id}", "info", 1)]
    assert await context_of(engine, SHIFT) == {
        "llm_tuple_id": tuple_id,
        "mean_7d": "66.00",
        "mean_prior_28d": "55.00",
    }
    async with engine.begin() as conn:
        await published_in_lane(conn, tuple_id, lane="similarity", mid=40, at=NOW - timedelta(days=1))  # 59.5
    assert await drift(engine) == 1
    assert await open_alerts(engine, SHIFT) == []


# ---- critic_rate_7d and scoring.critic_rate_high ----------------------------------------------------------


async def critic_call(
    conn: AsyncConnection, attempt_id: int, target_id: int, *, round_: int, status: str
) -> None:
    """A critic call of the attempt in `round_`, `reserved` or moved on to `status`."""
    _, call = await insert_scoring_call(conn, attempt_id=attempt_id, target_id=target_id, purpose="critic")
    await conn.execute(sa.text("UPDATE llm_calls SET round = :r WHERE id = :c"), {"r": round_, "c": call})
    if status != "reserved":
        await conn.execute(
            sa.text("UPDATE llm_calls SET status = :s WHERE id = :c"), {"s": status, "c": call}
        )


async def attempts(
    conn: AsyncConnection,
    tuple_id: int,
    n: int,
    *,
    critic: int = 0,
    ended: datetime = NOW - timedelta(days=1),
    charged: bool = True,
    policy: int | None = None,
) -> int:
    """`n` ended attempts of one new target of the tuple under the active (or the given) evaluation policy,
    the first `critic` of them with a dispatched first-round critic call; the target's id."""
    target = await insert_target(
        conn, await insert_job(conn), tuple_id, state="refused", refusal_category="other"
    )
    evaluation_policy = (await active_ids(conn)).evaluation_policy_id if policy is None else policy
    for attempt_no in range(1, n + 1):
        attempt = await insert_attempt(
            conn,
            target,
            attempt_no=attempt_no,
            outcome="refused",
            charged=charged,
            started_at=ended - timedelta(minutes=5),
            ended_at=ended,
            evaluation_policy_id=evaluation_policy,
        )
        if attempt_no <= critic:
            await critic_call(conn, attempt, target, round_=1, status="dispatched")
    return target


async def rate(engine: AsyncEngine, tuple_id: int, now: datetime = NOW) -> tuple[Decimal | None, int]:
    async with engine.connect() as conn:
        policy = (await active_ids(conn)).evaluation_policy_id
        return await cycles.critic_rate_7d(conn, llm_tuple_id=tuple_id, evaluation_policy_id=policy, now=now)


async def test_critic_rate_7d_counts_first_round_critic_calls_per_charged_attempt(
    engine: AsyncEngine, tuple_id: int
) -> None:
    async with engine.begin() as conn:
        target = await attempts(conn, tuple_id, 49, critic=10)
        # not counted: uncharged attempts, attempts outside the 7 days, attempts of another policy
        await attempts(conn, tuple_id, 3, critic=3, charged=False)
        await attempts(conn, tuple_id, 3, critic=3, ended=NOW - timedelta(days=8))
        other_policy = await activate_test_policy(conn, t2_band=7)
        await attempts(conn, tuple_id, 3, critic=3, policy=other_policy)
        await activate_test_policy(conn)  # the test policy active again
    assert await rate(engine, tuple_id) == (None, 49)  # fewer than 50 attempts: no rate
    async with engine.begin() as conn:
        last = await attempts(conn, tuple_id, 1)  # the fiftieth
        (attempt,) = (
            await conn.execute(sa.text("SELECT id FROM score_attempts WHERE target_id = :t"), {"t": last})
        ).scalars()
        # its critic calls count once and only a dispatched first round: a second try of round 1 (the
        # same logical call), a reserved (never sent) one and a round-2 call
        await critic_call(conn, attempt, last, round_=1, status="dispatched")
        await critic_call(conn, attempt, last, round_=1, status="dispatched")
        await critic_call(conn, attempt, last, round_=2, status="dispatched")
        (attempt_2,) = (
            await conn.execute(
                sa.text("SELECT id FROM score_attempts WHERE target_id = :t AND attempt_no = 11"),
                {"t": target},
            )
        ).scalars()
        await critic_call(conn, attempt_2, target, round_=1, status="reserved")
    assert await rate(engine, tuple_id) == (Decimal("0.2200"), 50)  # 11 of 50
    # they ended a day before `NOW`: inside the trailing 7 days until 6 days after it
    assert await rate(engine, tuple_id, NOW + timedelta(days=6)) == (Decimal("0.2200"), 50)
    assert await rate(engine, tuple_id, NOW + timedelta(days=6, minutes=1)) == (None, 0)


async def test_critic_rate_high_above_one_and_a_half_times_the_projection_raises_and_resolves(
    engine: AsyncEngine, tuple_id: int
) -> None:
    # the test policy projects 0.3: the line is 0.45
    async with engine.begin() as conn:
        await attempts(conn, tuple_id, 49, critic=30)  # 30 of 49: too few attempts
    assert await drift(engine) == 0
    async with engine.begin() as conn:
        await attempts(conn, tuple_id, 1)  # 30 of 50: 0.6 > 0.45
    assert await drift(engine) == 1
    policy = (await active_ids_of(engine)).evaluation_policy_id
    assert await open_alerts(engine, CRITIC) == [(f"{CRITIC}:tuple:{tuple_id}:policy:{policy}", "warn", 1)]
    assert await context_of(engine, CRITIC) == {
        "llm_tuple_id": tuple_id,
        "evaluation_policy_id": policy,
        "critic_rate_7d": "0.6000",
        "attempts": 50,
        "projected_critic_rate": "0.300",
    }
    assert await drift(engine) == 0  # once
    async with engine.begin() as conn:
        await attempts(conn, tuple_id, 17, critic=0)  # 30 of 67: 0.448 <= 0.45
    assert await drift(engine) == 1
    assert await open_alerts(engine, CRITIC) == []


async def active_ids_of(engine: AsyncEngine) -> Any:
    async with engine.connect() as conn:
        return await active_ids(conn)


async def test_every_completed_cycle_evaluates_the_critic_rate(
    engine: AsyncEngine, current_epoch: int, fake_llm: FakeLLM
) -> None:
    # Ruling P32: `finalize_cycle` evaluates `scoring.critic_rate_high` in its transaction
    gateway: LLMGateway = fake_llm.gateway(engine)
    runner = await cycle_runner(engine, gateway)
    (target,) = await insert_targets(engine, 1)
    fake_llm.answer("generate", target.job_revision_id, synthetic_c_draft())
    async with engine.begin() as conn:  # yesterday's 50 attempts, 40 of them reviewed: 0.8 > 0.45
        await attempts(conn, target.llm_tuple_id, 50, critic=40, ended=utcnow() - timedelta(days=1))
    assert await open_alerts(engine, CRITIC) == []
    cycle_id = await runner.start("manual")
    await runner.run(cycle_id)
    assert (await cycle_of(engine, cycle_id)).status == "completed"
    (alert,) = await open_alerts(engine, CRITIC)
    assert alert[0].startswith(f"{CRITIC}:tuple:{target.llm_tuple_id}:policy:")
    assert (await context_of(engine, CRITIC))["attempts"] == 51  # the cycle's own attempt counted too


# ---- budget_counters: the bulk operations' spent_usd ----------------------------------------------------


async def ledger_entry(
    conn: AsyncConnection, bulk_operation_id: int, *, state: str, reserved: str, settled: str | None
) -> None:
    """A funded scoring entry of the bulk operation moved to `state` with `settled_usd`."""
    target = await insert_target(conn, await insert_job(conn), await activate_score_tuple(conn))
    attempt = await insert_attempt(conn, target)
    ledger, _ = await insert_scoring_call(
        conn, attempt_id=attempt, target_id=target, bulk_operation_id=bulk_operation_id
    )
    await conn.execute(
        sa.text("UPDATE llm_spend_ledger SET reserved_usd = :r WHERE id = :l"),
        {"r": Decimal(reserved), "l": ledger},
    )
    path = {
        "reserved": [],
        "dispatched": ["dispatched"],
        "billed": ["dispatched", "billed"],
        "ambiguous": ["dispatched", "ambiguous"],
        "not_billed": ["not_billed"],
    }[state]
    for step in path:
        evidence = "error_response_no_content" if step == "not_billed" else None
        await conn.execute(
            sa.text("UPDATE llm_spend_ledger SET state = :s, settled_usd = :u, evidence = :e WHERE id = :l"),
            {
                "s": step,
                "u": None if settled is None else Decimal(settled),
                "e": evidence,
                "l": ledger,
            },
        )


async def spent(engine: AsyncEngine, bulk_operation_id: int) -> Decimal:
    async with engine.connect() as conn:
        found = await conn.execute(
            sa.text("SELECT spent_usd FROM bulk_operations WHERE id = :b"), {"b": bulk_operation_id}
        )
        return Decimal(found.scalar_one())


async def test_budget_counters_corrects_an_active_bulk_operation_s_spent_usd_from_its_ledger(
    engine: AsyncEngine, tuple_id: int
) -> None:
    # A1 issue 29: reserved, dispatched and ambiguous at their reservation, billed at its cost, not billed 0
    async with engine.begin() as conn:
        bulk = await insert_bulk_operation(conn, tuple_id, spent_usd=Decimal("0.450000"))
        await ledger_entry(conn, bulk, state="reserved", reserved="0.100000", settled=None)
        await ledger_entry(conn, bulk, state="dispatched", reserved="0.100000", settled=None)
        await ledger_entry(conn, bulk, state="ambiguous", reserved="0.200000", settled=None)
        await ledger_entry(conn, bulk, state="billed", reserved="0.100000", settled="0.050000")
        await ledger_entry(conn, bulk, state="not_billed", reserved="0.300000", settled="0")
        done = await insert_bulk_operation(conn, tuple_id, state="completed", spent_usd=Decimal("9"))
        await ledger_entry(conn, done, state="billed", reserved="0.100000", settled="0.100000")
    month = f"month:{NOW.date().replace(day=1).isoformat()}"
    await run_reconciler("budget_counters", engine, now=NOW, epoch=1)  # the month's counters, from the ledger
    assert [key for key, *_ in await open_alerts(engine, DRIFT)] == [f"{DRIFT}:{month}"]  # the bulk agrees
    assert await spent(engine, bulk) == Decimal("0.450000")
    async with engine.begin() as conn:
        await conn.execute(sa.text("UPDATE bulk_operations SET spent_usd = 1 WHERE id = :b"), {"b": bulk})
    assert await run_reconciler("budget_counters", engine, now=NOW, epoch=1) == 1
    assert await spent(engine, bulk) == Decimal("0.450000")
    assert await spent(engine, done) == Decimal("9")  # a finished operation is left as it is
    keys = [key for key, *_ in await open_alerts(engine, DRIFT)]
    assert keys == [f"{DRIFT}:{month}", f"{DRIFT}:bulk:{bulk}"]
    async with engine.connect() as conn:
        context = (
            await conn.execute(
                sa.text("SELECT context FROM alerts WHERE dedup_key = :k"), {"k": f"{DRIFT}:bulk:{bulk}"}
            )
        ).scalar_one()
    assert context == {
        "bulk_operation_id": bulk,
        "stored_spent_usd": "1.000000",
        "ledger_spent_usd": "0.450000",
    }
    assert await run_reconciler("budget_counters", engine, now=NOW, epoch=1) == 0
    assert await open_alerts(engine, DRIFT) == []  # a pass that corrects nothing resolves them


# ---- the fence rule ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["admission_alerts", "scoring_drift"])
async def test_the_alert_reconcilers_keep_the_fence_rule(
    engine: AsyncEngine, tuple_id: int, queue_app: procrastinate.App, name: str
) -> None:
    async with engine.begin() as conn:
        await pending_targets(conn, tuple_id, 1)  # a backlog that never drains (admission_alerts)
        for _ in range(10):  # ten disagreeing labels (scoring_drift)
            await feedback(conn, "poor", 90.0, NOW - timedelta(days=1))
    await assert_fence_rule(
        name, "heavy", ctx(engine, queue_app, epoch=2), ctx(engine, queue_app), scoring_reconcilers
    )
    async with engine.connect() as conn:
        assert (
            await conn.execute(sa.text("SELECT count(*) FROM alerts"))
        ).scalar_one() == 0  # nothing written
    assert await run_reconciler(name, engine, now=NOW, epoch=1) == 1


# ---- the score lane's gauges (§17.5 L5538-5539) ----------------------------------------------------------


def gauge(name: str, **labels: str) -> float | None:
    return metrics.REGISTRY.get_sample_value(name, labels)


async def test_metrics_refresh_sets_the_score_lane_gauges(engine: AsyncEngine, tuple_id: int) -> None:
    async with engine.begin() as conn:
        for hours in (2, 1):
            await insert_target(
                conn, await insert_job(conn), tuple_id, created_at=NOW - timedelta(hours=hours)
            )
        await attempts(conn, tuple_id, 49, critic=10)  # its target is `refused`
    assert await run_reconciler("metrics_refresh", engine, now=NOW, epoch=1) == 0
    assert gauge("jobhunter_score_targets", state="pending") == 2
    assert gauge("jobhunter_score_targets", state="refused") == 1
    assert gauge("jobhunter_score_targets", state="in_progress") == 0  # every state, 0 included
    assert gauge("jobhunter_oldest_pending_target_age_seconds") == 2 * 3600
    rate_7d = gauge("jobhunter_critic_rate_7d")
    assert rate_7d is not None and math.isnan(rate_7d)  # 49 attempts: no rate yet
    async with engine.begin() as conn:
        await attempts(conn, tuple_id, 1, critic=1)  # 11 of 50
        await conn.execute(
            sa.text(
                "UPDATE score_targets SET state = 'cancelled', state_reason = 'purged' "
                "WHERE state = 'pending'"
            )
        )
    assert await run_reconciler("metrics_refresh", engine, now=NOW, epoch=1) == 0
    assert gauge("jobhunter_critic_rate_7d") == 0.22
    assert gauge("jobhunter_score_targets", state="pending") == 0
    assert gauge("jobhunter_score_targets", state="cancelled") == 2
    assert gauge("jobhunter_oldest_pending_target_age_seconds") == 0  # none pending
