"""Score now, Retry, Score individually, `budget_release` and the alert acknowledgement on PostgreSQL (spec
§6.9.4, §7.16.3, §8.7, §9.6.4, §17.6; plan Task 13, A2 §3.7, T18, Deviation 24, Codex r1 #19, #20, r4 B8,
r5 B4, Rulings P13, P47, Controller ruling T11-c).

`score_now` moves every accepted state of §9.6.4's table to `pending` with its override, lane and
`override_at` (the Retry edge resets the charged attempts, a backoff is cleared), refuses `filtered_out`
without `force`, a claimed, batched, published or cancelled target and a job without a target, defers one
`score_cycle(score_now)` job that a second request finds waiting, and answers `budget_exhausted` with
nothing changed when the scoring headroom is below `r_first`, reading a fresh month's zero row without
writing it. `budget_release` creates a fresh month's row and releases against it, resolves a previous
month's cap alert at the first tick of the next, and resolves the current one when headroom returns, so a
second denial in a month pauses without a second alert. A `budget_release` holding the month row and a
`score_now` on a `budget_deferred` target never wait on each other on the month row, in either order.
`retry` resets the charged attempts; `score_individually` sets the membership flag and the override;
`acknowledge_alert` lifts the bound pause.

Every datum is invented.
"""

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.llm import budget, spend_ledger
from jobhunter.llm.budget import month_of
from jobhunter.llm.errors import BudgetExhausted
from jobhunter.llm.spend_ledger import BOUND_VIOLATED_ALERT, CAP_REACHED_ALERT, TargetAttemptOwner
from jobhunter.observability.alerts import acknowledge_alert, raise_alert
from jobhunter.scoring import cycles, targets
from jobhunter.scoring.targets import ScoreNow, ScoreNowRefused
from jobhunter.worker.tasks import defer_in_tx, job_task_name
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.faults import PausePoint
from tests.support.fixtures import count_rows
from tests.support.llm import budget_rows, open_alerts, save_budget, scoring_kw
from tests.support.m3 import Job, insert_claimed, insert_published
from tests.support.pg import PgServer, lock_wait_details
from tests.support.procs import await_until
from tests.support.reconcile import run_reconciler
from tests.support.scoring import (
    activate_score_tuple,
    aged_job,
    member_group,
    state_target,
    swept_of,
)
from tests.support.settings import generation

pytestmark = pytest.mark.integration

RETRYABLE = (
    "refused",
    "failed_max_tokens",
    "failed_invalid_output",
    "failed_request",
    "failed_transient",
    "failed_attempts_exhausted",
)
SCORE_CYCLE = job_task_name("score_cycle")


@pytest.fixture
def now() -> datetime:
    return utcnow()


@pytest.fixture
async def tuple_id(engine: AsyncEngine) -> int:
    async with engine.begin() as conn:
        return await activate_score_tuple(conn)


@pytest.fixture
async def scope(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        found = await conn.execute(sa.text("SELECT scope_version_id FROM active_config WHERE id = 1"))
        return int(found.scalar_one())


async def act(
    engine: AsyncEngine, job_id: int, now: datetime, *, force: bool = False
) -> ScoreNow | ScoreNowRefused:
    async with engine.begin() as conn:
        return await targets.score_now(conn, job_id=job_id, force=force, now=now, defer=defer_in_tx)


async def queued(engine: AsyncEngine) -> list[tuple[str | None, str]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            sa.text("SELECT queueing_lock, status FROM procrastinate_jobs WHERE task_name = :t ORDER BY id"),
            {"t": SCORE_CYCLE},
        )
        return [(r.queueing_lock, r.status) for r in rows]


async def target_job(engine: AsyncEngine, tuple_id: int, state: str, **columns: Any) -> tuple[Job, int]:
    async with engine.begin() as conn:
        job = await aged_job(conn)
        return job, await state_target(conn, job, tuple_id, state, **columns)


# ---- §9.6.4's table (A2 §3.7) ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "override"),
    [
        ("pending", "score_now"),
        ("budget_deferred", "score_now"),
        ("expired_unscored", "score_now"),
        ("deferred_duplicate", "score_now"),
        ("filtered_out", "score_anyway"),
        *((state, "score_now") for state in RETRYABLE),
    ],
)
async def test_every_accepted_state_moves_to_pending_with_its_override_and_lane(
    engine: AsyncEngine,
    queue_app: procrastinate.App,
    tuple_id: int,
    scope: int,
    now: datetime,
    state: str,
    override: str,
) -> None:
    columns: dict[str, Any] = {
        "charged_attempts": 4 if state == "failed_attempts_exhausted" else 2,
        "not_before": now + timedelta(hours=1),  # a transient backoff (Controller ruling T11-c)
        "transient_failures": 2,
        "lane": "oldest",
    }
    async with engine.begin() as conn:
        job = await aged_job(conn)
        if state == "deferred_duplicate":
            columns["deferred_group_id"] = await member_group(conn, await aged_job(conn), [job])
        if state == "filtered_out":
            columns["filter_scope_version_id"] = scope
        if state == "pending":
            columns["state_reason"] = "batch_max_tokens"  # a pending target's own reason is kept
        target = await state_target(conn, job, tuple_id, state, **columns)
    before = await generation(engine)
    assert await act(engine, job.job_id, now, force=state == "filtered_out") == ScoreNow(target, "pending")
    row = await swept_of(engine, target)
    assert (row.state, row.override, row.override_at, row.lane) == ("pending", override, now, "score_now")
    assert (row.not_before, row.transient_failures) == (None, 2)  # the backoff cleared, its count kept
    assert row.charged_attempts == (0 if state in RETRYABLE else 2)  # the Retry edge (L2880)
    assert (row.deferred_group_id, row.filter_scope_version_id) == (None, None)
    assert row.state_reason == ("batch_max_tokens" if state == "pending" else None)
    assert await queued(engine) == [("score_cycle:score_now", "todo")]
    assert await generation(engine) == before + 1


async def test_filtered_out_without_force_requires_it_and_changes_nothing(
    engine: AsyncEngine, queue_app: procrastinate.App, tuple_id: int, scope: int, now: datetime
) -> None:
    job, target = await target_job(engine, tuple_id, "filtered_out", filter_scope_version_id=scope)
    before, ranked = await swept_of(engine, target), await generation(engine)
    assert await act(engine, job.job_id, now) == ScoreNowRefused("filtered_requires_force")
    assert await swept_of(engine, target) == before
    assert await queued(engine) == [] and await generation(engine) == ranked


@pytest.mark.parametrize("state", ["in_progress", "published_accepted", "cancelled"])
async def test_a_claimed_published_or_cancelled_target_is_a_state_conflict(
    engine: AsyncEngine, queue_app: procrastinate.App, tuple_id: int, now: datetime, state: str
) -> None:
    async with engine.begin() as conn:
        if state == "in_progress":
            claimed = await insert_claimed(conn, tuple_id)
            job_id = int(
                (
                    await conn.execute(
                        sa.text("SELECT job_id FROM score_targets WHERE id = :t"), {"t": claimed.target_id}
                    )
                ).scalar_one()
            )
            target = claimed.target_id
        elif state == "published_accepted":
            job = await aged_job(conn)
            job_id, target = job.job_id, (await insert_published(conn, job, tuple_id)).target_id
        else:
            job = await aged_job(conn)
            job_id, target = job.job_id, await state_target(conn, job, tuple_id, state)
    before = await swept_of(engine, target)
    assert await act(engine, job_id, now) == ScoreNowRefused("conflict_state")
    assert await swept_of(engine, target) == before and await queued(engine) == []


async def test_a_job_without_a_target_of_the_active_tuple_is_not_found(
    engine: AsyncEngine, queue_app: procrastinate.App, tuple_id: int, now: datetime
) -> None:
    job, _ = await target_job(engine, tuple_id, "pending")
    async with engine.begin() as conn:
        bare = await aged_job(conn)
    assert await act(engine, bare.job_id, now) == ScoreNowRefused("not_found")
    assert await act(engine, 10**12, now) == ScoreNowRefused("not_found")
    async with engine.begin() as conn:  # no active tuple: no target of it
        await conn.execute(
            sa.text("UPDATE active_config SET llm_tuple_id = NULL, profile_version_id = NULL WHERE id = 1")
        )
    assert await act(engine, job.job_id, now) == ScoreNowRefused("not_found")
    assert await queued(engine) == []


async def test_one_score_now_cycle_job_is_queued_and_a_second_request_finds_it(
    engine: AsyncEngine, queue_app: procrastinate.App, tuple_id: int, now: datetime
) -> None:
    first, one = await target_job(engine, tuple_id, "pending")
    second, two = await target_job(engine, tuple_id, "budget_deferred")
    assert await act(engine, first.job_id, now) == ScoreNow(one, "pending")
    assert await act(engine, second.job_id, now) == ScoreNow(two, "pending")  # served by the waiting job
    assert await queued(engine) == [("score_cycle:score_now", "todo")]
    assert (await swept_of(engine, two)).override == "score_now"


async def test_without_headroom_score_now_is_budget_exhausted_and_changes_nothing(
    engine: AsyncEngine, queue_app: procrastinate.App, tuple_id: int, now: datetime
) -> None:
    # L3678: the pre-check against r_first, before the target is touched
    job, target = await target_job(engine, tuple_id, "budget_deferred")
    await save_budget(engine, now=now, cap_usd="2.10")  # A = $0.10, a scoring share of $0.095 < r_first
    before, ranked = await swept_of(engine, target), await generation(engine)
    assert await act(engine, job.job_id, now) == ScoreNowRefused("budget_exhausted")
    assert await swept_of(engine, target) == before
    assert await queued(engine) == [] and await generation(engine) == ranked


async def test_score_now_on_a_fresh_month_needs_no_row(
    engine: AsyncEngine, queue_app: procrastinate.App, tuple_id: int, now: datetime
) -> None:
    # Codex r4 B8: `read_month_or_new` answers a month without a row with its zero row, and writes none
    job, target = await target_job(engine, tuple_id, "expired_unscored")
    assert await count_rows(engine, "llm_budget") == 0
    assert await act(engine, job.job_id, now) == ScoreNow(target, "pending")
    assert await count_rows(engine, "llm_budget") == 0


# ---- budget_release (§6.9.4, L1201, L5506) ---------------------------------------------------------------


async def test_budget_release_on_a_fresh_month_creates_its_row(
    engine: AsyncEngine, queue_app: procrastinate.App, current_epoch: int, tuple_id: int, now: datetime
) -> None:
    deferred = [(await target_job(engine, tuple_id, "budget_deferred"))[1] for _ in range(3)]
    assert await count_rows(engine, "llm_budget") == 0
    before = await generation(engine)
    assert await run_reconciler("budget_release", engine, now=now, epoch=current_epoch) == 3
    (row,) = await budget_rows(engine, month_of(now))
    assert row is not None and row.counted_usd == 0  # created by `ensure_month`, released against
    for target in deferred:
        released = await swept_of(engine, target)
        assert (released.state, released.state_reason) == ("pending", None)
    assert await generation(engine) == before + 1
    assert await run_reconciler("budget_release", engine, now=now, epoch=current_epoch) == 0
    assert await generation(engine) == before + 1  # nothing released: no bump


async def test_budget_release_returns_the_k_oldest_deferred_targets(
    engine: AsyncEngine, queue_app: procrastinate.App, current_epoch: int, tuple_id: int, now: datetime
) -> None:
    deferred = [(await target_job(engine, tuple_id, "budget_deferred"))[1] for _ in range(3)]
    async with engine.begin() as conn:  # the oldest deferral first: the third, then the first
        for target, minutes in zip(deferred, (20, 5, 30), strict=True):
            await conn.execute(
                sa.text("UPDATE score_targets SET state_changed_at = :at WHERE id = :t"),
                {"at": now - timedelta(minutes=minutes), "t": target},
            )
    r_first = await active_r_first(engine)
    # a scoring share of 2.5 r_first: k = 2
    await save_budget(engine, now=now, cap_usd=str(cents_up(2 + Decimal("2.5") * r_first / Decimal("0.95"))))
    assert await run_reconciler("budget_release", engine, now=now, epoch=current_epoch) == 2
    states = [(await swept_of(engine, t)).state for t in deferred]
    assert states == ["pending", "budget_deferred", "pending"]


async def test_a_previous_months_cap_alert_does_not_pause_the_new_month(
    engine: AsyncEngine, queue_app: procrastinate.App, current_epoch: int, tuple_id: int
) -> None:
    # Codex r5 B4: the ledger decides the pause, and the first tick of November resolves October's alert
    october, november = datetime(2026, 10, 30, 12, 0, tzinfo=UTC), datetime(2026, 11, 1, 0, 5, tzinfo=UTC)
    await target_job(engine, tuple_id, "pending")
    async with engine.begin() as conn:
        await spend_ledger.ensure_month(
            conn, date(2026, 10, 1), await spend_ledger.current_params(conn), october
        )
        await spend_ledger.raise_cap_reached(conn, date(2026, 10, 1), quarantined_usd=Decimal(0), now=october)
    assert [a[0] for a in await open_alerts(engine, CAP_REACHED_ALERT)] == ["llm.cap_reached:2026-10-01"]
    async with engine.connect() as conn:
        assert await cycles.pause_reason(conn, fake_llm=True, now=november) is None  # before any tick
    await run_reconciler("budget_release", engine, now=november, epoch=current_epoch)
    assert await open_alerts(engine, CAP_REACHED_ALERT) == []
    async with engine.begin() as conn:
        cycle_id = await cycles.start_cycle(
            conn, trigger="manual", now=november, epoch=current_epoch, fake_llm=True
        )
    assert cycle_id is not None


async def test_a_second_denial_in_a_month_pauses_without_a_second_alert(
    engine: AsyncEngine,
    queue_app: procrastinate.App,
    current_epoch: int,
    target_owner: TargetAttemptOwner,
    now: datetime,
) -> None:
    await save_budget(engine, now=now, cap_usd="3.00")  # A = $1.00, a scoring share of $0.95
    month = month_of(now)
    r_first = await active_r_first(engine)

    async def reserve_big() -> spend_ledger.Reservation:
        async with engine.begin() as conn:  # $0.85 on Opus: the headroom left is below r_first
            return await spend_ledger.reserve(
                conn, **scoring_kw(target_owner, now=now, bound=130_000, max_tokens=10_000)
            )

    async def deny() -> None:
        with pytest.raises(BudgetExhausted):
            async with engine.begin() as conn:
                await spend_ledger.reserve(
                    conn, **scoring_kw(target_owner, now=now, bound=10_000, max_tokens=5_000)
                )
        async with engine.begin() as conn:  # what the denied attempt's `fail` raises (Task 10)
            await spend_ledger.raise_cap_reached(conn, month, quarantined_usd=Decimal(0), now=now)

    async def pause() -> str | None:
        async with engine.connect() as conn:
            return await cycles.pause_reason(conn, fake_llm=True, now=now)

    async def headroom() -> Decimal:
        (row,) = await budget_rows(engine, month)
        async with engine.connect() as conn:
            p = await spend_ledger.current_params(conn)
        return budget.headroom(row, "scoring", p)

    held = await reserve_big()
    assert await headroom() < r_first
    await deny()
    assert await pause() == "cap_reached" and len(await open_alerts(engine, CAP_REACHED_ALERT)) == 1
    (marked,) = await budget_rows(engine, month)
    alerted_at = marked.cap_reached_alerted_at
    assert alerted_at is not None
    async with engine.begin() as conn:  # a settlement restores the headroom
        await spend_ledger.release_never_dispatched(conn, held, now=now)
    await run_reconciler("budget_release", engine, now=now, epoch=current_epoch)
    assert await open_alerts(engine, CAP_REACHED_ALERT) == [] and await pause() is None
    await reserve_big()
    await deny()
    assert await pause() == "cap_reached"
    assert await open_alerts(engine, CAP_REACHED_ALERT) == []  # no second alert this month
    assert await count_rows(engine, "alerts", code=CAP_REACHED_ALERT) == 1
    (again,) = await budget_rows(engine, month)
    assert again.cap_reached_alerted_at == alerted_at


@pytest.mark.parametrize("first", ["budget_release", "score_now"])
async def test_score_now_and_budget_release_never_wait_on_each_other(
    engine: AsyncEngine,
    migrated: PgServer,
    queue_app: procrastinate.App,
    current_epoch: int,
    tuple_id: int,
    now: datetime,
    monkeypatch: pytest.MonkeyPatch,
    first: str,
) -> None:
    # Codex r1 #19, #20: score_now holds active_config, the idempotency row and the target row, never the
    # month row; budget_release holds the month row and skips a target another transaction holds
    job, target = await target_job(engine, tuple_id, "budget_deferred")
    if first == "budget_release":
        paused = PausePoint(targets.RELEASED_POINT)
        monkeypatch.setattr(targets, "afault_point", paused)
        release = asyncio.create_task(run_reconciler("budget_release", engine, now=now, epoch=current_epoch))
        await asyncio.wait_for(paused.reached.wait(), 10)  # it holds the month row and the released target
        scoring = asyncio.create_task(act(engine, job.job_id, now))

        async def waiting() -> bool:
            return scoring.done() or bool(lock_wait_details(migrated))

        await await_until(waiting, 10, 0.02)
        waits = lock_wait_details(migrated)
        assert waits and all(w.relation != "llm_budget" for w in waits)  # on the target row only
        assert {w.relation for w in waits if w.relation is not None} == {"score_targets"}
        paused.resume()
        assert await asyncio.wait_for(release, 10) == 1
        assert await asyncio.wait_for(scoring, 10) == ScoreNow(target, "pending")
    else:
        holding, done = asyncio.Event(), asyncio.Event()

        async def score_and_hold() -> ScoreNow | ScoreNowRefused:
            async with engine.begin() as conn:
                result = await targets.score_now(
                    conn, job_id=job.job_id, force=False, now=now, defer=defer_in_tx
                )
                holding.set()
                await asyncio.wait_for(done.wait(), 10)
                return result

        scoring = asyncio.create_task(score_and_hold())
        await asyncio.wait_for(holding.wait(), 10)  # the target row is held, uncommitted
        assert (
            await asyncio.wait_for(run_reconciler("budget_release", engine, now=now, epoch=current_epoch), 10)
            == 0
        )
        assert lock_wait_details(migrated) == []
        done.set()
        assert await asyncio.wait_for(scoring, 10) == ScoreNow(target, "pending")
    row = await swept_of(engine, target)
    assert (row.state, row.override, row.lane) == ("pending", "score_now", "score_now")


# ---- retry, score individually, the acknowledgement --------------------------------------------------


async def test_retry_resets_the_charged_attempts_and_requests_a_score_now_cycle(
    engine: AsyncEngine, queue_app: procrastinate.App, tuple_id: int, now: datetime
) -> None:
    _, target = await target_job(engine, tuple_id, "failed_attempts_exhausted", charged_attempts=4)
    _, pending = await target_job(engine, tuple_id, "pending")
    async with engine.begin() as conn:
        assert await targets.retry(conn, target_id=target, now=now, defer=defer_in_tx) == ScoreNow(
            target, "pending"
        )
    row = await swept_of(engine, target)
    assert (row.state, row.state_reason, row.charged_attempts) == ("pending", None, 0)
    assert (row.override, row.override_at, row.lane) == ("score_now", now, "score_now")
    assert await queued(engine) == [("score_cycle:score_now", "todo")]
    async with engine.begin() as conn:
        assert await targets.retry(conn, target_id=pending, now=now, defer=defer_in_tx) == ScoreNowRefused(
            "conflict_state"
        )
        assert await targets.retry(conn, target_id=10**12, now=now, defer=defer_in_tx) == ScoreNowRefused(
            "not_found"
        )


async def test_score_individually_sets_the_membership_flag_and_the_override(
    engine: AsyncEngine, queue_app: procrastinate.App, tuple_id: int, now: datetime
) -> None:
    async with engine.begin() as conn:
        representative, member, outsider = await aged_job(conn), await aged_job(conn), await aged_job(conn)
        group = await member_group(conn, representative, [member])
        target = await state_target(conn, member, tuple_id, "deferred_duplicate", deferred_group_id=group)
        await state_target(conn, outsider, tuple_id, "pending")
    async with engine.begin() as conn:
        moved = await targets.score_individually(
            conn, group_id=group, job_id=member.job_id, now=now, defer=defer_in_tx
        )
    assert moved == ScoreNow(target, "pending")
    row = await swept_of(engine, target)
    assert (row.state, row.deferred_group_id, row.override, row.override_at, row.lane) == (
        "pending",
        None,
        "individual",
        now,
        "score_now",
    )
    async with engine.connect() as conn:
        flag = await conn.execute(
            sa.text("SELECT score_individually FROM duplicate_group_members WHERE job_id = :j"),
            {"j": member.job_id},
        )
        assert flag.scalar_one() is True
    assert await queued(engine) == [("score_cycle:score_now", "todo")]
    async with engine.begin() as conn:  # the regroup sweep leaves it alone from now on
        assert await targets.regroup_sweep(conn, llm_tuple_id=tuple_id) == targets.RegroupSwept(0, 0)
        assert await targets.score_individually(
            conn, group_id=group, job_id=outsider.job_id, now=now, defer=defer_in_tx
        ) == ScoreNowRefused("not_found")


async def test_acknowledge_alert_lifts_the_pause_and_keeps_the_alert_open(
    engine: AsyncEngine, tuple_id: int, now: datetime
) -> None:
    # Ruling P13: the score-lane pause of an unacknowledged bound violation ends with its acknowledgement
    async with engine.begin() as conn:
        alert = await raise_alert(
            conn,
            code=BOUND_VIOLATED_ALERT,
            severity="critical",
            subject="model:claude-sonnet-5-5",
            message="x",
        )
    async with engine.connect() as conn:
        assert await cycles.pause_reason(conn, fake_llm=True, now=now) == "reservation_bound_violated"
    async with engine.begin() as conn:
        assert await acknowledge_alert(conn, alert, now=now) is True
    async with engine.begin() as conn:
        assert await cycles.pause_reason(conn, fake_llm=True, now=now) is None
        assert await acknowledge_alert(conn, alert, now=now + timedelta(minutes=1)) is True  # idempotent
        row = (
            await conn.execute(
                sa.text("SELECT acknowledged_at, resolved_at FROM alerts WHERE id = :a"), {"a": alert}
            )
        ).one()
        assert (row.acknowledged_at, row.resolved_at) == (now, None)
        await conn.execute(sa.text("UPDATE alerts SET resolved_at = now() WHERE id = :a"), {"a": alert})
        assert await acknowledge_alert(conn, alert, now=now) is False  # resolved: nothing to acknowledge
        assert await acknowledge_alert(conn, 10**12, now=now) is False


# ---- helpers -------------------------------------------------------------------------------------------


def cents_up(usd: Decimal) -> Decimal:
    return usd.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


async def active_r_first(engine: AsyncEngine) -> Decimal:
    async with engine.connect() as conn:
        config = await cycles.scoring_config(conn)
    return budget.r_first(config.model_policy, config.execution)
