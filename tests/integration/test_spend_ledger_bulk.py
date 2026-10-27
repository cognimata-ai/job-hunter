"""Bulk accounting, the scoring ledger columns and the cap-reached marker on PostgreSQL (spec §6.9.1-6.9.4,
§10.7; plan Task 7): a scoring reservation of a funded target debits `bulk_operations.spent_usd` instead of
the USD bucket while it fits, its settlement and its release credit the same account, the attempt's
`cost_usd` follows every settlement, `admitted_buffer_usd` audits the admission, `llm.cap_reached` is raised
once per month, and the reservation's lock order is `active_config` → owner → month → bulk row → ledger."""

import asyncio
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.sql import LOCK_NOT_AVAILABLE, pg_error
from jobhunter.errors import FencedOut
from jobhunter.llm import bounds, budget, spend_ledger
from jobhunter.llm.budget import month_of
from jobhunter.llm.errors import BudgetExhausted
from jobhunter.llm.pricing import MODELS, Usage, settled_usd, worst_case_usd
from jobhunter.llm.spend_ledger import (
    BOUND_VIOLATED_ALERT,
    CAP_REACHED_ALERT,
    CallOutcome,
    EvalRunOwner,
    Reservation,
    TargetAttemptOwner,
    change_buffer,
    current_params,
    ensure_month,
    funded_bulk_operation_id,
    mark_dispatched,
    raise_cap_reached,
    recompute,
    release_never_dispatched,
    reserve,
    resolve_cap_reached,
    resolve_stale_cap_alerts,
    settle,
)
from jobhunter.settings.model import ExecutionConfig, ModelPolicy
from jobhunter.worker import operations as ops
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import ctx
from tests.support.llm import (
    USAGE,
    attempt_and_target,
    batch_kw,
    budget_rows,
    claim_owner,
    completed_call,
    count_rows,
    ledger_row,
    new_submission_owner,
    new_target_owner,
    open_alerts,
    reclaim,
    reclaim_target,
    reserve_kw,
    save_budget,
    score_tuple,
    scoring_kw,
)
from tests.support.m2 import insert_eval_run
from tests.support.m3 import NOW, insert_batch_item, insert_bulk_operation, insert_submission
from tests.support.pg import PgServer, lock_wait_details, relation_locks
from tests.support.procs import await_until

pytestmark = pytest.mark.integration
MINUTE = timedelta(minutes=1)
# claude-opus-5-5, 4,000 input tokens at the cache-write $5 and 1,000 output tokens at $20 per MTok
R = Decimal("0.04")
LOCK_MONTH = sa.text("SELECT 1 FROM llm_budget WHERE month = :key FOR UPDATE")
LOCK_BULK = sa.text("SELECT 1 FROM bulk_operations WHERE id = :key FOR UPDATE")
LOCK_TARGET = sa.text("SELECT 1 FROM score_targets WHERE id = :key FOR UPDATE")
LOCK_ACTIVE_CONFIG = sa.text("SELECT 1 FROM active_config WHERE id = :key FOR UPDATE")
NOWAIT = {
    "active_config": sa.text("SELECT 1 FROM active_config WHERE id = :key FOR UPDATE NOWAIT"),
    "month": sa.text("SELECT 1 FROM llm_budget WHERE month = :key FOR UPDATE NOWAIT"),
    "bulk": sa.text("SELECT 1 FROM bulk_operations WHERE id = :key FOR UPDATE NOWAIT"),
    "target": sa.text("SELECT 1 FROM score_targets WHERE id = :key FOR UPDATE NOWAIT"),
}
POLICY_B = ModelPolicy()
EXECUTION_DEFAULT = ExecutionConfig()


async def reserved(engine: AsyncEngine, kw: Mapping[str, object]) -> Reservation:
    async with engine.begin() as conn:
        return await reserve(conn, **kw)  # type: ignore[arg-type]


async def dispatched(engine: AsyncEngine, kw: Mapping[str, object]) -> Reservation:
    async with engine.begin() as conn:
        res = await reserve(conn, **kw)  # type: ignore[arg-type]
        await mark_dispatched(conn, res, kw["owner"], now=kw["now"], first_try_of_logical_call=True)  # type: ignore[arg-type]
    return res


async def settled(
    engine: AsyncEngine, res: Reservation, outcome: str, *, now: datetime, usage: Usage = USAGE
):
    async with engine.begin() as conn:
        if outcome == "billed":
            call = completed_call()
            return await settle(conn, res, outcome="billed", evidence=None, usage=usage, call=call, now=now)
        if outcome == "not_billed":
            error = CallOutcome("error", http_status=529, error_code="http_529")
            return await settle(
                conn,
                res,
                outcome="not_billed",
                evidence="error_response_no_content",
                usage=None,
                call=error,
                now=now,
            )
        lost = CallOutcome("ambiguous", error_code="stream_interrupted")
        return await settle(conn, res, outcome="ambiguous", evidence=None, usage=None, call=lost, now=now)


async def bulk_row(engine: AsyncEngine, bulk_id: int) -> sa.Row[tuple[object, ...]]:
    async with engine.connect() as conn:
        found = await conn.execute(
            sa.text("SELECT state, budget_usd, spent_usd FROM bulk_operations WHERE id = :id"),
            {"id": bulk_id},
        )
        return found.one()


async def entry_bulk_id(engine: AsyncEngine, res: Reservation) -> int | None:
    async with engine.connect() as conn:
        found = await conn.execute(
            sa.text("SELECT bulk_operation_id FROM llm_spend_ledger WHERE id = :id"), {"id": res.ledger_id}
        )
        value = found.scalar_one()
    return None if value is None else int(value)


async def new_bulk(engine: AsyncEngine, *, budget_usd: Decimal, state: str = "active") -> int:
    async with engine.begin() as conn:
        return await insert_bulk_operation(conn, await score_tuple(conn), state=state, budget_usd=budget_usd)


async def month_ready(engine: AsyncEngine, now: datetime) -> date:
    """The month of `now`, created from the current settings (`ensure_month`, P47)."""
    async with engine.begin() as conn:
        await ensure_month(conn, month_of(now), await current_params(conn), now)
    return month_of(now)


async def no_drift(engine: AsyncEngine, month: date) -> bool:
    async with engine.connect() as conn:
        return await recompute(conn, month) is None


def funded_kw(
    owner: TargetAttemptOwner, bulk_id: int | None, now: datetime, **overrides: Any
) -> dict[str, Any]:
    return scoring_kw(owner, now=now, bound=4000, max_tokens=1000, bulk_operation_id=bulk_id, **overrides)


# ---- the allocation (§6.9.3, L1187-1191) ------------------------------------------------------------------


async def test_a_funded_reservation_debits_the_allocation_not_the_bucket_while_it_fits(
    engine, clean_db, migrated: PgServer
) -> None:
    t = utcnow()
    month = await month_ready(engine, t)
    bulk = await new_bulk(engine, budget_usd=2 * R)
    owners = [await new_target_owner(engine, bulk_operation_id=bulk) for _ in range(3)]
    (before,) = await budget_rows(engine, month)
    assert worst_case_usd("claude-opus-5-5", input_tokens_bound=4000, max_tokens=1000) == R

    first = await dispatched(engine, funded_kw(owners[0], bulk, t))
    second = await dispatched(engine, funded_kw(owners[1], bulk, t))  # fits exactly: 0.08 ≤ 0.08
    third = await dispatched(engine, funded_kw(owners[2], bulk, t))  # would exceed: the bucket's
    assert [await entry_bulk_id(engine, r) for r in (first, second, third)] == [bulk, bulk, None]
    assert (await bulk_row(engine, bulk)).spent_usd == 2 * R
    (after,) = await budget_rows(engine, month)
    assert after.bucket_usd == before.bucket_usd - R  # only the unfunded one debited the bucket
    assert (after.counted_usd, after.scoring_counted_usd) == (3 * R, 3 * R)  # the hard cap counts all three

    # settlement credits the account each entry debited, like the bucket (L1187)
    await settled(engine, first, "billed", now=t)  # $0.008
    await settled(engine, second, "not_billed", now=t)
    await settled(engine, third, "billed", now=t)
    cost = settled_usd("claude-opus-5-5", USAGE)
    assert (await bulk_row(engine, bulk)).spent_usd == cost
    (final,) = await budget_rows(engine, month)
    assert final.bucket_usd == before.bucket_usd - cost
    assert final.counted_usd == 2 * cost
    assert await no_drift(engine, month)


async def test_eight_concurrent_reservations_never_admit_past_the_allocation(
    engine, clean_db, migrated: PgServer
) -> None:
    t = utcnow()
    month = await month_ready(engine, t)
    bulk = await new_bulk(engine, budget_usd=Decimal("3.5") * R)  # three fit, the fourth would not
    owners = [await new_target_owner(engine, bulk_operation_id=bulk) for _ in range(8)]

    async def session(owner: TargetAttemptOwner) -> Reservation:
        return await reserved(engine, funded_kw(owner, bulk, t))

    async with engine.connect() as blocker:  # the barrier: all 8 wait for the month row at once
        await blocker.begin()
        await blocker.execute(LOCK_MONTH, {"key": month})
        tasks = [asyncio.create_task(session(owner)) for owner in owners]

        async def all_waiting() -> bool:
            return len(lock_wait_details(migrated)) >= len(tasks) or any(task.done() for task in tasks)

        await await_until(all_waiting, 30)
        assert not any(task.done() for task in tasks)
        await blocker.rollback()
    results = await asyncio.gather(*tasks)
    funded = [r for r in results if await entry_bulk_id(engine, r) == bulk]
    assert len(funded) == 3
    spent = (await bulk_row(engine, bulk)).spent_usd
    assert spent == 3 * R <= Decimal("3.5") * R
    (row,) = await budget_rows(engine, month)
    assert row.counted_usd == 8 * R
    assert await no_drift(engine, month)


async def test_a_billed_overrun_at_a_full_allocation_is_counted_in_full(
    engine, clean_db, migrated: PgServer
) -> None:
    """Codex r1 #28 (L1164): a cost above the reservation is charged in full to the account the entry
    debited, even above `budget_usd` (no CHECK stops it); the entry carries the violation and the alert is
    raised in the same transaction; nothing is rolled back."""
    t = utcnow()
    month = await month_ready(engine, t)
    r = worst_case_usd("claude-opus-5-5", input_tokens_bound=1000, max_tokens=256)
    assert r == Decimal("0.010120")  # 1,000 x $5 + 256 x $20 per MTok
    bulk = await new_bulk(engine, budget_usd=r)  # reserved to the last microdollar
    owner = await new_target_owner(engine, bulk_operation_id=bulk)
    res = await dispatched(
        engine, scoring_kw(owner, now=t, bound=1000, max_tokens=256, bulk_operation_id=bulk)
    )
    assert (await bulk_row(engine, bulk)).spent_usd == r == (await bulk_row(engine, bulk)).budget_usd
    # 4 x $4 + 505 x $20 + 1 x $5 per MTok = $0.010121: one microdollar above the reservation
    over = Usage(input_tokens=4, output_tokens=505, cache_creation_input_tokens=1, cache_read_input_tokens=0)
    assert settled_usd("claude-opus-5-5", over) == r + Decimal("0.000001")
    await settled(engine, res, "billed", now=t, usage=over)
    row = await ledger_row(engine, res.ledger_id)
    assert (row.state, row.settled_usd, row.evidence) == (
        "billed",
        r + Decimal("0.000001"),
        "reservation_bound_violated",
    )
    bulk_after = await bulk_row(engine, bulk)
    assert bulk_after.spent_usd == bulk_after.budget_usd + Decimal("0.000001")
    assert await open_alerts(engine, BOUND_VIOLATED_ALERT) == [
        ("llm.reservation_bound_violated:model:claude-opus-5-5", "critical", 1)
    ]
    attempt, _ = await attempt_and_target(engine, owner)
    assert attempt.cost_usd == r + Decimal("0.000001")
    assert await no_drift(engine, month)


@pytest.mark.parametrize("state", ["completed", "cancelled"])
async def test_a_finished_bulk_operation_funds_nothing(engine, clean_db, migrated: PgServer, state) -> None:
    """P25: only an `active` operation funds a reservation; a finished one is refused as a full one is."""
    t = utcnow()
    month = await month_ready(engine, t)
    bulk = await new_bulk(engine, budget_usd=Decimal(5), state=state)
    owner = await new_target_owner(engine, bulk_operation_id=bulk)
    (before,) = await budget_rows(engine, month)
    res = await reserved(engine, funded_kw(owner, bulk, t))
    assert await entry_bulk_id(engine, res) is None
    assert (await bulk_row(engine, bulk)).spent_usd == 0
    (after,) = await budget_rows(engine, month)
    assert after.bucket_usd == before.bucket_usd - R


async def test_an_owner_is_funded_by_its_own_bulk_operation_only(
    engine, clean_db, migrated: PgServer
) -> None:
    """A score attempt names its target's bulk operation and a batch submission its own; any other is refused
    under the owner lock, before anything is written."""
    t = utcnow()
    bulk = await new_bulk(engine, budget_usd=Decimal(5))
    other = await new_bulk(engine, budget_usd=Decimal(5), state="completed")
    unfunded = await new_target_owner(engine)  # a target of no bulk operation
    funded = await new_target_owner(engine, bulk_operation_id=bulk)
    for owner, named in ((unfunded, bulk), (funded, other)):
        with pytest.raises(ValueError, match="own bulk operation"):
            await reserved(engine, funded_kw(owner, named, t))
    sub = await new_submission_owner(engine, items=1, bulk_operation_id=bulk)
    with pytest.raises(ValueError, match="own bulk operation"):
        await reserved(engine, batch_kw(sub, sub.items[0], now=t, bound=4000, bulk_operation_id=other))
    assert await count_rows(engine, "llm_spend_ledger") == 0
    assert await count_rows(engine, "llm_budget") == 0  # refused before the month row was created
    assert (await bulk_row(engine, bulk)).spent_usd == 0
    res = await reserved(engine, funded_kw(funded, bulk, t))  # its own one funds it
    assert await entry_bulk_id(engine, res) == bulk


async def test_a_nonscoring_reservation_never_names_a_bulk_operation(engine, op_owner, clean_db) -> None:
    bulk = await new_bulk(engine, budget_usd=Decimal(5))
    with pytest.raises(ValueError, match="bulk"):
        await reserved(
            engine, reserve_kw(op_owner, now=utcnow(), bound=4000, max_tokens=1000, bulk_operation_id=bulk)
        )
    assert await count_rows(engine, "llm_spend_ledger") == 0


async def test_a_released_funded_reservation_credits_the_allocation_once(
    engine, queue_app, current_epoch, migrated: PgServer
) -> None:
    """Codex r2 B11: an abandoned preparation or a cancelled funded reservation leaves no allocation debit;
    the `reserved → not_billed` transition is the idempotence guard."""
    t = utcnow()
    month = await month_ready(engine, t)
    bulk = await new_bulk(engine, budget_usd=Decimal(5))
    owner = await new_target_owner(engine, bulk_operation_id=bulk)
    (before,) = await budget_rows(engine, month)
    res = await reserved(engine, funded_kw(owner, bulk, t))
    assert (await bulk_row(engine, bulk)).spent_usd == R
    async with engine.begin() as conn:
        await release_never_dispatched(conn, res, now=t)
    async with engine.begin() as conn:  # a second release writes nothing
        await release_never_dispatched(conn, res, now=t)
    assert (await bulk_row(engine, bulk)).spent_usd == 0
    (after,) = await budget_rows(engine, month)
    assert (after.counted_usd, after.bucket_usd) == (0, before.bucket_usd)  # the bucket never moved

    # the reconciler's release of a lost owner's funded reservation credits it too
    lost = await reserved(engine, funded_kw(owner, bulk, t))
    await reclaim_target(engine, owner)
    assert await spend_ledger.reconcile_llm_ledger_unsettled(ctx(engine, queue_app, now=t + 16 * MINUTE)) == 1
    assert (await ledger_row(engine, lost.ledger_id)).state == "not_billed"
    assert (await bulk_row(engine, bulk)).spent_usd == 0
    assert await no_drift(engine, month)


async def test_a_funded_follow_up_is_a_target_with_an_unreleased_followup_item(
    engine, clean_db, migrated: PgServer
) -> None:
    """Codex r1 #27: the durable funding link is a `batch_items` row of the target with `decision =
    'followup'` and `hold_released_at IS NULL`; only such a target's online attempt passes its bulk id."""
    t = utcnow()
    bulk = await new_bulk(engine, budget_usd=Decimal(5))
    funded, released, published, plain = [
        await new_target_owner(engine, attempt_no=2, bulk_operation_id=bulk, lane="batch_followup")
        for _ in range(4)
    ]
    async with engine.begin() as conn:
        tuple_id = await score_tuple(conn)
        submission = await insert_submission(
            conn,
            tuple_id,
            bulk,
            state="reconciled",
            request_count=3,
            submit_started_at=NOW,
            provider_batch_id="msgbatch_followup",
            results_complete_at=NOW,
            reconciled_at=NOW,
        )
        processed = {"result_type": "succeeded", "processed_at": NOW, "hold_usd": Decimal("0.03")}
        await insert_batch_item(conn, submission, funded.target_id, 1, decision="followup", **processed)
        await insert_batch_item(
            conn, submission, released.target_id, 1, decision="followup", hold_released_at=NOW, **processed
        )
        await insert_batch_item(conn, submission, published.target_id, 1, decision="published", **processed)
        ids = [
            await funded_bulk_operation_id(conn, target_id=o.target_id)
            for o in (funded, released, published, plain)
        ]
    assert ids == [bulk, None, None, None]
    res = [
        await reserved(engine, funded_kw(o, i, t))
        for o, i in zip((funded, plain), (ids[0], ids[3]), strict=True)
    ]
    assert [await entry_bulk_id(engine, r) for r in res] == [bulk, None]
    assert (await bulk_row(engine, bulk)).spent_usd == R


# ---- the eval run under its operation's claim (Codex r1 #29) -----------------------------------------------


async def test_an_eval_run_owner_with_a_claim_reserves_nothing_once_the_operation_was_reclaimed(
    engine, queue_app, current_epoch
) -> None:
    t = utcnow()
    async with engine.begin() as conn:
        op_id, _ = await ops.create_operation(
            conn, kind="eval_run", payload={}, dedup_key="eval-claim-reserve", defer=None
        )
        eval_run_id = await insert_eval_run(conn)
    claimed = await claim_owner(engine, op_id, epoch=current_epoch)
    owner = EvalRunOwner(eval_run_id, claim=claimed.claim)
    first = await reserved(engine, reserve_kw(owner, now=t, bound=4000, max_tokens=2000))
    await reclaim(engine, claimed)
    with pytest.raises(FencedOut) as fenced:
        await reserved(engine, reserve_kw(owner, now=t, bound=4000, max_tokens=2000))
    assert fenced.value.owner == "eval_run"
    assert await count_rows(engine, "llm_spend_ledger") == 1
    with pytest.raises(FencedOut):
        async with engine.begin() as conn:
            await mark_dispatched(conn, first, owner, now=t)
    # its reservation is the reconciler's after 15 minutes, though the eval run itself is still running
    assert await spend_ledger.reconcile_llm_ledger_unsettled(ctx(engine, queue_app, now=t + 16 * MINUTE)) == 1
    row = await ledger_row(engine, first.ledger_id)
    assert (row.state, row.evidence) == ("not_billed", "never_dispatched")
    assert row.input_refs["owner"] == {
        "kind": "eval_run",
        "id": eval_run_id,
        "operation_id": op_id,
        "claim_id": str(claimed.claim.claim_id),
    }


# ---- the ledger columns (plan Task 1, M2 Carry-forward) ----------------------------------------------------


async def test_admitted_buffer_usd_is_the_month_rows_buffer_at_admission(engine, op_owner) -> None:
    t = utcnow()
    await save_budget(engine, now=t, overrun_buffer_usd="3.00")
    first = await reserved(engine, reserve_kw(op_owner, now=t, bound=4000, max_tokens=1000))
    async with engine.begin() as conn:
        await change_buffer(conn, month=month_of(t), overrun_buffer_usd=Decimal("4.00"), now=t)
    second = await reserved(engine, reserve_kw(op_owner, now=t, bound=4000, max_tokens=1000))
    async with engine.connect() as conn:
        rows = await conn.execute(
            sa.text("SELECT admitted_buffer_usd FROM llm_spend_ledger WHERE id = ANY(:ids) ORDER BY id"),
            {"ids": [first.ledger_id, second.ledger_id]},
        )
        assert [row.admitted_buffer_usd for row in rows] == [Decimal("3.00"), Decimal("4.00")]


async def test_the_attempts_cost_follows_every_settlement(
    engine, target_owner, queue_app, current_epoch
) -> None:
    """L3911: `score_attempts.cost_usd` is the sum over the attempt's tries, an ambiguous try at its reserved
    amount: billed `+ s`, ambiguous `+ r`, late `ambiguous → billed` `+ (s - r)`, late `ambiguous →
    not_billed` `- r`; a refund adds nothing."""
    t = utcnow()
    s = settled_usd("claude-opus-5-5", USAGE)

    async def cost() -> Decimal:
        attempt, _ = await attempt_and_target(engine, target_owner)
        return Decimal(attempt.cost_usd)

    kw = scoring_kw(target_owner, now=t, bound=4000, max_tokens=1000)
    billed, refunded, late_billed, late_refunded, aged = [await dispatched(engine, kw) for _ in range(5)]
    await settled(engine, billed, "billed", now=t)
    assert await cost() == s
    await settled(engine, refunded, "not_billed", now=t)
    assert await cost() == s
    await settled(engine, late_billed, "ambiguous", now=t)
    await settled(engine, late_refunded, "ambiguous", now=t)
    assert await cost() == s + 2 * R
    await settled(engine, late_billed, "billed", now=t)  # its usage turns up later
    assert await cost() == 2 * s + R
    async with engine.begin() as conn:  # provider evidence of no charge, after the ambiguous settlement
        await settle(
            conn,
            late_refunded,
            outcome="not_billed",
            evidence="error_response_no_content",
            usage=None,
            call=CallOutcome("error", http_status=500, error_code="http_500"),
            now=t,
        )
    assert await cost() == 2 * s
    # the reconciler's ambiguous counts at its reservation too, so its late settlement stays exact
    assert await spend_ledger.reconcile_llm_ledger_unsettled(ctx(engine, queue_app, now=t + 16 * MINUTE)) == 1
    assert (await ledger_row(engine, aged.ledger_id)).state == "ambiguous"
    assert await cost() == 2 * s + R
    await settled(engine, aged, "billed", now=t + 17 * MINUTE)
    assert await cost() == 3 * s
    assert await no_drift(engine, month_of(t))


# ---- the cap reached (§6.9.4, L1199; Codex r5 B4) ----------------------------------------------------------


async def cap_reached_rows(engine: AsyncEngine) -> list[tuple[str, bool, Mapping[str, object]]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            sa.text(
                "SELECT dedup_key, resolved_at IS NULL AS open, context FROM alerts "
                "WHERE code = :c ORDER BY id"
            ),
            {"c": CAP_REACHED_ALERT},
        )
        return [(row.dedup_key, bool(row.open), row.context) for row in rows]


async def test_cap_reached_is_raised_once_per_month_and_resolving_keeps_the_marker(
    engine, target_owner
) -> None:
    t = utcnow()
    month = month_of(t)
    await save_budget(engine, now=t, cap_usd="2.03")  # A = $0.03: a scoring try of $0.04 is denied
    raised, resolved = [], []
    for _ in range(2):  # denial → recovery → denial, inside one month
        with pytest.raises(BudgetExhausted):
            await reserved(engine, scoring_kw(target_owner, now=t, bound=4000, max_tokens=1000))
        async with engine.begin() as conn:
            raised.append(await raise_cap_reached(conn, month, quarantined_usd=Decimal("1.250000"), now=t))
        async with engine.begin() as conn:  # headroom returns
            resolved.append(await resolve_cap_reached(conn, month))
    assert (raised, resolved) == ([True, False], [True, False])  # one alert for the month, resolved once
    assert await cap_reached_rows(engine) == [
        (f"llm.cap_reached:{month.isoformat()}", False, {"quarantined_batch_usd": "1.250000"})
    ]
    (row,) = await budget_rows(engine, month)
    assert row.cap_reached_alerted_at == t  # the once-only marker is never cleared


async def test_raise_cap_reached_returns_whether_it_raised(engine, clean_db) -> None:
    t = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
    month = await month_ready(engine, t)
    async with engine.begin() as conn:
        assert await raise_cap_reached(conn, month, quarantined_usd=Decimal("0.5"), now=t)
        assert not await raise_cap_reached(conn, month, quarantined_usd=Decimal("0.5"), now=t)
    assert await open_alerts(engine, CAP_REACHED_ALERT) == [("llm.cap_reached:2026-10-01", "critical", 1)]


async def test_stale_cap_alerts_are_resolved_by_the_next_month(engine, clean_db) -> None:
    oct10 = datetime(2026, 10, 10, tzinfo=UTC)
    nov2 = datetime(2026, 11, 2, tzinfo=UTC)
    october, november = await month_ready(engine, oct10), await month_ready(engine, nov2)
    async with engine.begin() as conn:
        assert await raise_cap_reached(conn, october, quarantined_usd=Decimal(0), now=oct10)
        assert await raise_cap_reached(conn, november, quarantined_usd=Decimal(0), now=nov2)
    async with engine.begin() as conn:
        assert await resolve_stale_cap_alerts(conn, current=november) == 1
        assert await resolve_stale_cap_alerts(conn, current=november) == 0
    assert await open_alerts(engine, CAP_REACHED_ALERT) == [("llm.cap_reached:2026-11-01", "critical", 1)]


# ---- r_first and ĉ_followup (§6.9.3, §6.9.4, §21.4; P25) --------------------------------------------------


def test_r_first_and_c_hat_followup_are_the_hand_computed_literals(monkeypatch) -> None:
    monkeypatch.setattr(bounds, "PROMPT_OVERHEAD_TOKENS", dict.fromkeys(MODELS, 2_640))
    # r_first, from the llm/pricing.py rates: the generate node is claude-sonnet-5-5 (input $2.00, cache write
    # $2.50, output $10.00 per MTok); its first stage is 6,000 tokens. The input bound is the §21.4 component
    # sizes at the overhead: 2,640 + 36,800 - 1,000 = 38,440 tokens, at max(input, cache write) = $2.50:
    # 38,440 x 2.50 / 10^6 = $0.096100; 6,000 x 10.00 / 10^6 = $0.060000; together $0.156100.
    assert budget.r_first(POLICY_B, EXECUTION_DEFAULT) == Decimal("0.156100")
    # ĉ_followup, from the llm/pricing.py rates and the §21.1 tokens (cached prefix read at $0.20 per MTok):
    # full critic (claude-opus-5-5, medium): 3,400 x 4 + 4,500 x 0.20 + 1,300 x 20 = 40,500 → $0.040500;
    # light critic (low): 3,400 x 4 + 4,500 x 0.20 + 800 x 20 = 30,500 → $0.030500;
    # revision (claude-sonnet-5-5): 3,700 x 2 + 4,500 x 0.20 + 1,700 x 10 = 25,300 → $0.025300;
    # follow-ups per reviewed draft q1 + q1 x q2 = 0.35 + 0.105 = 0.455;
    # first round 0.35 x 0.0305 + 0.65 x 0.0405 = $0.037000;
    # ĉ_followup = (0.037 + 0.455 x 0.0405) + 0.455 x 0.0253 = 0.0554275 + 0.0115115 = $0.066939.
    assert budget.c_hat_followup(POLICY_B) == Decimal("0.066939")


# ---- the lock order (§3.3.3, §9.1; P49) --------------------------------------------------------------------


async def backend_pid(conn) -> int:
    return int((await conn.execute(sa.text("SELECT pg_backend_pid()"))).scalar_one())


async def nowait_refused(engine: AsyncEngine, which: str, key: object) -> bool:
    async with engine.connect() as conn:
        try:
            await conn.execute(NOWAIT[which], {"key": key})
        except sa.exc.OperationalError as exc:
            assert pg_error(exc).sqlstate == LOCK_NOT_AVAILABLE
            return True
        finally:
            await conn.rollback()
    return False


async def test_lock_order_owner_month_bulk(engine, clean_db, migrated: PgServer) -> None:
    """`reserve` takes `active_config` → the owner row → the month row → the bulk row → the ledger rows: held
    at each step in turn, it waits on that row (`lock_wait_details`), holding every earlier one and none of
    the later ones (the row locks by `NOWAIT`, the tables it has touched by its relation locks): at the bulk
    step it has not touched `llm_spend_ledger` or `llm_calls` yet."""
    t = utcnow()
    month = await month_ready(engine, t)
    bulk = await new_bulk(engine, budget_usd=Decimal(5))
    owner = await new_target_owner(engine, bulk_operation_id=bulk)
    order = ["active_config", "target", "month", "bulk"]  # as reserve takes them
    tables = {
        "active_config": "active_config",
        "target": "score_targets",
        "month": "llm_budget",
        "bulk": "bulk_operations",
    }
    keys: dict[str, object] = {"active_config": 1, "target": owner.target_id, "month": month, "bulk": bulk}
    locks = {
        "active_config": LOCK_ACTIVE_CONFIG,
        "target": LOCK_TARGET,
        "month": LOCK_MONTH,
        "bulk": LOCK_BULK,
    }
    for step, name in enumerate(order):
        async with engine.connect() as blocker:
            await blocker.begin()
            await blocker.execute(locks[name], {"key": keys[name]})
            pid = await backend_pid(blocker)
            task = asyncio.create_task(reserved(engine, funded_kw(owner, bulk, t)))

            async def waiting(task: asyncio.Task[Reservation] = task) -> bool:
                return bool(lock_wait_details(migrated)) or task.done()

            await await_until(waiting, 10, 0.02)
            assert not task.done()
            (wait,) = lock_wait_details(migrated)
            assert (wait.relation, wait.key, wait.blocking_pids) == (tables[name], str(keys[name]), (pid,))
            touched = relation_locks(migrated, wait.pid)
            for earlier in order[:step]:  # every earlier row is held by the waiting reservation
                assert tables[earlier] in touched, earlier
                assert await nowait_refused(engine, earlier, keys[earlier]), earlier
            for later in order[step + 1 :]:  # no later one is taken, nor its table touched
                assert tables[later] not in touched, later
                assert not await nowait_refused(engine, later, keys[later]), later
            assert not touched & {"llm_spend_ledger", "llm_calls"}, step  # no ledger row before the bulk row
            await blocker.rollback()
        res = await asyncio.wait_for(task, 10)
        async with engine.begin() as conn:  # the allocation is free again for the next step
            await release_never_dispatched(conn, res, now=t)
