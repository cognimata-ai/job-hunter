"""The target sweeps on PostgreSQL (spec §7.16.3, §8.7, §9.6.2, §9.6.3; plan Task 13, Deviation 7, Codex r1
#21, #36, Rulings P9, P52, Controller carries T12-a, T12-c): `scoring.targets`' expiry, refilter, regroup and
exhaustion sweeps, the four of them as `admission.admit` runs them, and the `expiry_sweep` reconciler.

Each direction of every sweep moves exactly its targets, and a target with an override other than `none`
is left alone in every case (the exhaustion sweep excepted: an override buys no fifth attempt). The reverse
expiry clears the reason; an expired follow-up target's hold is released once; `stale` and `unknown`
availability never expire; an unchanged deferred member stays deferred across repeated sweeps. `admit` runs
the four in order, bumps the ranking once, and locks the bulk rows of the hold-releasing candidates before
its first target row (Ruling P52).

Every datum is invented.
"""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.sql import q
from jobhunter.scoring import admission, cycles, targets
from jobhunter.scoring.cycles import CycleRow
from jobhunter.scoring.targets import ExpirySwept, RefilterSwept, RegroupSwept
from jobhunter.worker.reconcilers import ReconcileContext
from jobhunter.worker.tasks import defer_in_tx, task_app
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.m3 import insert_cycle
from tests.support.pg import PgServer, lock_wait_details
from tests.support.procs import await_until
from tests.support.reconcile import assert_fence_rule, run_reconciler
from tests.support.scoring import (
    activate_score_tuple,
    aged_job,
    filter_result,
    followup_hold,
    hold_of,
    member_group,
    state_target,
    swept_of,
)
from tests.support.settings import generation

pytestmark = pytest.mark.integration

MAX_AGE = 30  # the seeded `scope.max_ad_age_days`
OVERRIDE = "score_now"


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


async def execute(engine: AsyncEngine, sql: str, **params: object) -> None:
    async with engine.begin() as conn:
        await conn.execute(sa.text(sql), params)


async def state(engine: AsyncEngine, target_id: int) -> tuple[str, str | None]:
    row = await swept_of(engine, target_id)
    return row.state, row.state_reason


async def expire(engine: AsyncEngine, tuple_id: int, now: datetime, max_age: int = MAX_AGE) -> ExpirySwept:
    async with engine.begin() as conn:
        return await targets.expiry_sweep(conn, llm_tuple_id=tuple_id, max_ad_age_days=max_age, now=now)


async def refilter(engine: AsyncEngine, tuple_id: int, scope: int) -> RefilterSwept:
    async with engine.begin() as conn:
        return await targets.refilter_sweep(conn, llm_tuple_id=tuple_id, scope_version_id=scope)


async def regroup(engine: AsyncEngine, tuple_id: int) -> RegroupSwept:
    async with engine.begin() as conn:
        return await targets.regroup_sweep(conn, llm_tuple_id=tuple_id)


async def exhaust(engine: AsyncEngine, tuple_id: int) -> int:
    async with engine.begin() as conn:
        return await targets.exhaustion_sweep(conn, llm_tuple_id=tuple_id)


# ---- expiry (§9.6.3) --------------------------------------------------------------------------------------


async def test_expiry_expires_closed_and_too_old_targets_and_revives_them_when_that_clears(
    engine: AsyncEngine, tuple_id: int, now: datetime
) -> None:
    async with engine.begin() as conn:
        closed = await aged_job(conn, now=now, availability="closed")
        made = {
            "closed": await state_target(conn, closed, tuple_id, "pending"),
            "old": await state_target(
                conn, await aged_job(conn, now=now, first_seen_days=31), tuple_id, "budget_deferred"
            ),
            # first seen two days ago, posted 31 days ago: the posted date decides
            "posted_old": await state_target(
                conn, await aged_job(conn, now=now, first_seen_days=2, posted_days=31), tuple_id, "pending"
            ),
            "posted_within": await state_target(
                conn, await aged_job(conn, now=now, first_seen_days=2, posted_days=29), tuple_id, "pending"
            ),
            "fresh": await state_target(conn, await aged_job(conn, now=now), tuple_id, "budget_deferred"),
            "guarded": await state_target(
                conn,
                await aged_job(conn, now=now, first_seen_days=40, availability="closed"),
                tuple_id,
                "pending",
                override=OVERRIDE,
            ),
        }
    assert await expire(engine, tuple_id, now) == ExpirySwept(expired=3, revived=0)
    assert await state(engine, made["closed"]) == ("expired_unscored", "closed")
    assert await state(engine, made["old"]) == ("expired_unscored", "too_old")  # its budget reason replaced
    assert await state(engine, made["posted_old"]) == ("expired_unscored", "too_old")
    assert await state(engine, made["posted_within"]) == ("pending", None)
    assert await state(engine, made["fresh"]) == ("budget_deferred", "budget_exhausted")
    assert await state(engine, made["guarded"]) == ("pending", None)  # an override: never swept
    assert await expire(engine, tuple_id, now) == ExpirySwept(expired=0, revived=0)  # idempotent

    # the conditions clear: the job is re-sighted, and `max_ad_age_days` is raised
    await execute(engine, "UPDATE jobs SET availability = 'active' WHERE id = :j", j=closed.job_id)
    assert await expire(engine, tuple_id, now, max_age=60) == ExpirySwept(expired=0, revived=3)
    for name in ("closed", "old", "posted_old"):
        assert await state(engine, made[name]) == ("pending", None), name  # the reason is cleared (A2 A6)
    assert await state(engine, made["guarded"]) == ("pending", None)


async def test_the_reverse_expiry_leaves_an_override_alone(
    engine: AsyncEngine, tuple_id: int, now: datetime
) -> None:
    async with engine.begin() as conn:
        job = await aged_job(conn, now=now)
        protected = await state_target(conn, job, tuple_id, "expired_unscored", override=OVERRIDE)
        plain = await state_target(conn, await aged_job(conn, now=now), tuple_id, "expired_unscored")
    assert await expire(engine, tuple_id, now) == ExpirySwept(expired=0, revived=1)
    assert await state(engine, protected) == ("expired_unscored", "too_old")
    assert await state(engine, plain) == ("pending", None)


async def test_stale_and_unknown_availability_never_expire(
    engine: AsyncEngine, tuple_id: int, now: datetime
) -> None:
    async with engine.begin() as conn:
        stale = await state_target(
            conn, await aged_job(conn, now=now, availability="stale"), tuple_id, "pending"
        )
        unknown = await aged_job(conn, now=now, availability="unknown")
        deferred = await state_target(conn, unknown, tuple_id, "budget_deferred")
        # re-sighted as stale: aggregator postings are never re-confirmed, so stale revives it
        back = await aged_job(conn, now=now, availability="stale")
        expired = await state_target(conn, back, tuple_id, "expired_unscored", state_reason="closed")
    assert await expire(engine, tuple_id, now) == ExpirySwept(expired=0, revived=1)
    assert await state(engine, stale) == ("pending", None)
    assert await state(engine, deferred) == ("budget_deferred", "budget_exhausted")
    assert await state(engine, expired) == ("pending", None)


async def test_an_expired_followup_target_releases_its_hold_once(
    engine: AsyncEngine, tuple_id: int, now: datetime
) -> None:
    # Rulings P9, P52: the forward move releases the hold of the target's unreleased `followup` item
    async with engine.begin() as conn:
        job = await aged_job(conn, now=now, availability="closed")
        target = await state_target(conn, job, tuple_id, "pending", lane="batch_followup")
        operation, item = await followup_hold(conn, tuple_id, target, hold=Decimal("0.250000"))
        await conn.execute(
            sa.text("UPDATE bulk_operations SET followup_held_usd = 0.75 WHERE id = :b"), {"b": operation}
        )
    assert await expire(engine, tuple_id, now) == ExpirySwept(expired=1, revived=0)
    held = await hold_of(engine, operation, item)
    assert held.followup_held_usd == Decimal("0.500000") and held.hold_released_at is not None
    released_at = held.hold_released_at
    assert await expire(engine, tuple_id, now) == ExpirySwept(expired=0, revived=0)
    assert tuple(await hold_of(engine, operation, item)) == (Decimal("0.500000"), released_at)  # once


# ---- refilter (§7.16.3, A2 R7, Deviation 24) -------------------------------------------------------------


async def test_refilter_moves_failing_targets_out_and_passing_ones_back(
    engine: AsyncEngine, tuple_id: int, scope: int, now: datetime
) -> None:
    async with engine.begin() as conn:
        failing = [await aged_job(conn, now=now) for _ in range(5)]
        passing = [await aged_job(conn, now=now) for _ in range(3)]
        for job in failing:
            await filter_result(conn, job, scope, passed=False)
        for job in passing:
            await filter_result(conn, job, scope, passed=True)
        unchecked = await aged_job(conn, now=now)  # no result for the scope: no positive evidence
        group = await member_group(conn, passing[2], [failing[2]])
        made = {
            "pending": await state_target(conn, failing[0], tuple_id, "pending"),
            "deferred": await state_target(conn, failing[1], tuple_id, "budget_deferred"),
            "duplicate": await state_target(
                conn, failing[2], tuple_id, "deferred_duplicate", deferred_group_id=group
            ),
            "guarded_out": await state_target(conn, failing[3], tuple_id, "pending", override=OVERRIDE),
            "outside": await state_target(conn, failing[4], tuple_id, "failed_request"),  # no from-state
            "back": await state_target(
                conn, passing[0], tuple_id, "filtered_out", filter_scope_version_id=scope
            ),
            "guarded_back": await state_target(
                conn,
                passing[1],
                tuple_id,
                "filtered_out",
                override="score_anyway",
                filter_scope_version_id=scope,
            ),
            "unchecked": await state_target(conn, unchecked, tuple_id, "pending"),
        }
    assert await refilter(engine, tuple_id, scope) == RefilterSwept(filtered=3, unfiltered=1)
    for name in ("pending", "deferred", "duplicate"):
        row = await swept_of(engine, made[name])
        assert (row.state, row.filter_scope_version_id, row.deferred_group_id) == (
            "filtered_out",
            scope,
            None,
        )
    assert (await swept_of(engine, made["deferred"])).state_reason == "budget_exhausted"  # the reason is kept
    assert (await swept_of(engine, made["guarded_out"])).state == "pending"
    assert (await swept_of(engine, made["outside"])).state == "failed_request"
    back = await swept_of(engine, made["back"])
    assert (back.state, back.filter_scope_version_id) == ("pending", None)
    guarded = await swept_of(engine, made["guarded_back"])
    assert (guarded.state, guarded.filter_scope_version_id) == ("filtered_out", scope)
    assert (await swept_of(engine, made["unchecked"])).state == "pending"
    assert await refilter(engine, tuple_id, scope) == RefilterSwept(filtered=0, unfiltered=0)


# ---- regroup (§8.7, L3261) -----------------------------------------------------------------------------


async def test_regroup_defers_deferrable_members_and_returns_the_others(
    engine: AsyncEngine, tuple_id: int, now: datetime
) -> None:
    async with engine.begin() as conn:
        representative, member, individual, guarded, late = [await aged_job(conn, now=now) for _ in range(5)]
        group = await member_group(conn, representative, [member, individual, guarded, late])
        await conn.execute(
            sa.text("UPDATE duplicate_group_members SET score_individually = true WHERE job_id = :j"),
            {"j": individual.job_id},
        )
        made = {
            "representative": await state_target(conn, representative, tuple_id, "pending"),
            "member": await state_target(conn, member, tuple_id, "budget_deferred"),
            "individual": await state_target(conn, individual, tuple_id, "pending"),
            "guarded": await state_target(conn, guarded, tuple_id, "pending", override="individual"),
            "late": await state_target(conn, late, tuple_id, "failed_request"),  # not an entering state
        }
    assert await regroup(engine, tuple_id) == RegroupSwept(deferred=1, undeferred=0)
    deferred = await swept_of(engine, made["member"])
    assert (deferred.state, deferred.deferred_group_id, deferred.state_reason) == (
        "deferred_duplicate",
        group,
        "budget_exhausted",
    )
    for name in ("representative", "individual", "guarded"):
        assert (await swept_of(engine, made[name])).state == "pending", name
    assert (await swept_of(engine, made["late"])).state == "failed_request"


@pytest.mark.parametrize("change", ["dissolved", "elected", "score_individually", "left"])
async def test_a_deferred_member_returns_when_its_deferral_no_longer_holds(
    engine: AsyncEngine, tuple_id: int, now: datetime, change: str
) -> None:
    async with engine.begin() as conn:
        representative, member, other = [await aged_job(conn, now=now) for _ in range(3)]
        group = await member_group(conn, representative, [member, other])
        target = await state_target(conn, member, tuple_id, "deferred_duplicate", deferred_group_id=group)
        guarded = await state_target(
            conn, other, tuple_id, "deferred_duplicate", deferred_group_id=group, override=OVERRIDE
        )
    statement = {
        "dissolved": "UPDATE duplicate_groups SET status = 'dissolved' WHERE id = :g",
        "elected": "UPDATE duplicate_groups SET representative_job_id = :member WHERE id = :g",
        "score_individually": (
            "UPDATE duplicate_group_members SET score_individually = true WHERE job_id = :member"
        ),
        "left": "DELETE FROM duplicate_group_members WHERE job_id = :member",
    }[change]
    params: dict[str, object] = {"g": group, "member": member.job_id}
    await execute(engine, statement, **{k: v for k, v in params.items() if f":{k}" in statement})
    assert await regroup(engine, tuple_id) == RegroupSwept(deferred=0, undeferred=1)
    back = await swept_of(engine, target)
    assert (back.state, back.deferred_group_id) == ("pending", None)
    kept = await swept_of(engine, guarded)
    assert (kept.state, kept.deferred_group_id) == ("deferred_duplicate", group)


async def test_an_unchanged_deferred_member_survives_repeated_sweeps(
    engine: AsyncEngine, tuple_id: int, now: datetime
) -> None:
    # Codex r1 #36: the state guard is for entering only, so a deferred member is never undone by a sweep
    async with engine.begin() as conn:
        representative, member = await aged_job(conn, now=now), await aged_job(conn, now=now)
        group = await member_group(conn, representative, [member])
        await state_target(conn, representative, tuple_id, "pending")
        target = await state_target(conn, member, tuple_id, "pending")
    assert await regroup(engine, tuple_id) == RegroupSwept(deferred=1, undeferred=0)
    for sweep in range(10):
        if sweep == 5:  # the representative is updated (re-sighted, its election refreshed)
            await execute(
                engine,
                "UPDATE jobs SET last_seen_at = now(), availability = 'active' WHERE id = :j",
                j=representative.job_id,
            )
            await execute(
                engine,
                "UPDATE duplicate_groups SET elected_at = now(), updated_at = now() WHERE id = :g",
                g=group,
            )
        assert await regroup(engine, tuple_id) == RegroupSwept(deferred=0, undeferred=0)
    row = await swept_of(engine, target)
    assert (row.state, row.deferred_group_id) == ("deferred_duplicate", group)


# ---- exhaustion (Deviation 7, Codex r1 #21) ----------------------------------------------------------


async def test_exhaustion_ends_targets_with_four_charged_attempts_and_releases_their_holds(
    engine: AsyncEngine, tuple_id: int, now: datetime
) -> None:
    async with engine.begin() as conn:
        rejected = await state_target(
            conn, await aged_job(conn, now=now), tuple_id, "pending", charged_attempts=4
        )
        three = await state_target(
            conn, await aged_job(conn, now=now), tuple_id, "pending", charged_attempts=3
        )
        deferred = await state_target(
            conn, await aged_job(conn, now=now), tuple_id, "budget_deferred", charged_attempts=4
        )
        guarded = await state_target(
            conn, await aged_job(conn, now=now), tuple_id, "pending", charged_attempts=4, override=OVERRIDE
        )
        operation, item = await followup_hold(conn, tuple_id, rejected, hold=Decimal("0.100000"))
    assert await exhaust(engine, tuple_id) == 3
    for target in (rejected, deferred, guarded):  # an override buys no fifth attempt
        assert await state(engine, target) == ("failed_attempts_exhausted", "attempts_exhausted")
    assert await state(engine, three) == ("pending", None)
    held = await hold_of(engine, operation, item)
    assert held.followup_held_usd == Decimal("0.000000") and held.hold_released_at is not None
    assert await exhaust(engine, tuple_id) == 0


# ---- the four as `admit` runs them (Controller carries T12-a, T12-c) ----------------------------------


async def owned(conn: AsyncConnection, cycle_id: int) -> CycleRow:
    """The cycle as the `admit` node holds it: `active_config FOR SHARE`, then the owner row (epoch 1)."""
    await cycles.lock_active_scoring(conn)
    cycle = await cycles.lock_cycle(conn, cycle_id, epoch=1)
    assert cycle is not None
    return cycle


async def test_admit_runs_the_four_sweeps_and_bumps_the_ranking_once(
    engine: AsyncEngine, tuple_id: int, scope: int, now: datetime
) -> None:
    async with engine.begin() as conn:
        closed = await state_target(
            conn, await aged_job(conn, now=now, availability="closed"), tuple_id, "pending"
        )
        failing = await aged_job(conn, now=now)
        await filter_result(conn, failing, scope, passed=False)
        filtered = await state_target(conn, failing, tuple_id, "pending")
        representative, member = await aged_job(conn, now=now), await aged_job(conn, now=now)
        await member_group(conn, representative, [member])
        deferred = await state_target(conn, member, tuple_id, "pending")
        spent = await state_target(
            conn, await aged_job(conn, now=now), tuple_id, "pending", charged_attempts=4
        )
        cycle_id = await insert_cycle(conn, tuple_id)
    before = await generation(engine)
    async with engine.begin() as conn:
        picked = await admission.admit(
            conn, await owned(conn, cycle_id), now=now, sweeps=admission.CYCLE_SWEEPS
        )
    assert picked == []  # nothing is left pending
    assert await state(engine, closed) == ("expired_unscored", "closed")
    assert (await swept_of(engine, filtered)).state == "filtered_out"
    assert (await swept_of(engine, deferred)).state == "deferred_duplicate"
    assert await state(engine, spent) == ("failed_attempts_exhausted", "attempts_exhausted")
    assert await generation(engine) == before + 1  # once, last


async def test_admit_locks_the_bulk_rows_before_its_first_target_row(
    engine: AsyncEngine, migrated: PgServer, tuple_id: int, now: datetime
) -> None:
    # Controller carry T12-c, Ruling P52: the exhaustion sweep runs fourth, after expiry has locked its
    # targets; the bulk row of an exhaustion candidate's hold is locked before the expiry's first target row
    async with engine.begin() as conn:
        expiring = await state_target(
            conn, await aged_job(conn, now=now, availability="closed"), tuple_id, "pending"
        )
        spent = await state_target(
            conn, await aged_job(conn, now=now), tuple_id, "pending", charged_attempts=4
        )
        operation, item = await followup_hold(conn, tuple_id, spent, hold=Decimal("0.200000"))
        cycle_id = await insert_cycle(conn, tuple_id)
    async with engine.connect() as holder:  # a writer of the operation's accounts holds its row
        await holder.execute(sa.text("BEGIN"))
        await holder.execute(
            q("SELECT id FROM bulk_operations WHERE id = :b FOR NO KEY UPDATE"), {"b": operation}
        )
        holder_pid = int((await holder.execute(sa.text("SELECT pg_backend_pid()"))).scalar_one())

        async def run_admit() -> int:
            async with engine.begin() as conn:
                pid = int((await conn.execute(sa.text("SELECT pg_backend_pid()"))).scalar_one())
                waiting.set_result(pid)
                await admission.admit(
                    conn, await owned(conn, cycle_id), now=now, sweeps=admission.CYCLE_SWEEPS
                )
                return pid

        waiting: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        admitting = asyncio.create_task(run_admit())
        pid = await asyncio.wait_for(waiting, 10)

        async def waits() -> bool:
            return admitting.done() or any(w.pid == pid for w in lock_wait_details(migrated))

        await await_until(waits, 10, 0.02)
        assert not admitting.done()
        (wait,) = [w for w in lock_wait_details(migrated) if w.pid == pid]
        assert (wait.relation, wait.key, wait.blocking_pids) == (
            "bulk_operations",
            str(operation),
            (holder_pid,),
        )
        # the expiry candidate's row is not locked yet: another session takes it at once
        async with engine.connect() as probe:
            await probe.execute(sa.text("BEGIN"))
            locked = await probe.execute(
                sa.text("SELECT id FROM score_targets WHERE id = :t FOR UPDATE NOWAIT"), {"t": expiring}
            )
            assert locked.scalar_one() == expiring
            await probe.execute(sa.text("ROLLBACK"))
        await holder.execute(sa.text("ROLLBACK"))
    assert await asyncio.wait_for(admitting, 10) == pid
    assert await state(engine, expiring) == ("expired_unscored", "closed")
    assert await state(engine, spent) == ("failed_attempts_exhausted", "attempts_exhausted")
    held = await hold_of(engine, operation, item)
    assert held.followup_held_usd == Decimal("0.000000") and held.hold_released_at is not None


# ---- the `expiry_sweep` reconciler (L5513) -----------------------------------------------------------


def ctx(engine: AsyncEngine, *, now: datetime, epoch: int = 1) -> ReconcileContext:
    return ReconcileContext(engine=engine, queue=task_app(), defer=defer_in_tx, now=now, epoch=epoch)


async def test_the_expiry_sweep_reconciler_sweeps_the_active_tuple_with_the_active_max_age(
    engine: AsyncEngine, queue_app: procrastinate.App, current_epoch: int, tuple_id: int, now: datetime
) -> None:
    async with engine.begin() as conn:
        old = await state_target(conn, await aged_job(conn, now=now, first_seen_days=31), tuple_id, "pending")
        young = await state_target(
            conn, await aged_job(conn, now=now, first_seen_days=29), tuple_id, "pending"
        )
        back = await state_target(
            conn, await aged_job(conn, now=now, first_seen_days=10), tuple_id, "expired_unscored"
        )
    before = await generation(engine)
    assert await run_reconciler("expiry_sweep", engine, now=now, epoch=current_epoch) == 2
    assert await state(engine, old) == ("expired_unscored", "too_old")
    assert await state(engine, young) == ("pending", None)
    assert await state(engine, back) == ("pending", None)
    assert await generation(engine) == before + 1
    assert await run_reconciler("expiry_sweep", engine, now=now, epoch=current_epoch) == 0
    assert await generation(engine) == before + 1  # nothing moved: no bump
    later = now + timedelta(days=2)
    await assert_fence_rule(
        "expiry_sweep",
        "heavy",
        ctx(engine, now=later, epoch=current_epoch + 1),
        ctx(engine, now=later),
        targets,
    )
    assert await state(engine, young) == ("pending", None)  # neither fenced pass wrote anything
