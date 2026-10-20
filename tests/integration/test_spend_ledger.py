"""The LLM spend ledger on PostgreSQL (spec §6.9.1-6.9.2, §7.16.7, §17.4, §18.2): the serialised month
row, the counter table, cap edits, the month boundary, the owner fence and the two reconcilers."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.sql import LOCK_NOT_AVAILABLE, pg_error
from jobhunter.errors import FencedOut
from jobhunter.llm import budget, spend_ledger
from jobhunter.llm.budget import BudgetParams, month_of
from jobhunter.llm.errors import BudgetExhausted
from jobhunter.llm.pricing import Usage, worst_case_usd
from jobhunter.llm.spend_ledger import (
    BOUND_VIOLATED_ALERT,
    CAP_80PCT_ALERT,
    COUNTER_DRIFT_ALERT,
    Counters,
    Drift,
    Reservation,
    change_cap,
    current_params,
    ensure_month,
    mark_dispatched,
    owner_holds,
    read_month,
    recompute,
    release_never_dispatched,
    reserve,
    settle,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import ctx
from tests.support.llm import (
    USAGE,
    budget_rows,
    claim_owner,
    completed_call,
    count_rows,
    error_call,
    ledger_row,
    new_eval_owner,
    open_alerts,
    reclaim,
    reserve_kw,
    save_budget,
    scoring_kw,
)
from tests.support.logs import logged
from tests.support.pg import PgServer, blocked_or_done, lock_waits
from tests.support.procs import await_until
from tests.support.reconcile import assert_fence_rule

pytestmark = pytest.mark.integration
MINUTE = timedelta(minutes=1)
LOCK_MONTH = sa.text("SELECT 1 FROM llm_budget WHERE month = :m FOR UPDATE")
LOCK_MONTH_NOWAIT = sa.text("SELECT 1 FROM llm_budget WHERE month = :m FOR UPDATE NOWAIT")
LOCK_OPERATION = sa.text("SELECT 1 FROM operations WHERE id = :id FOR UPDATE")
LOCK_OPERATION_NOWAIT = sa.text("SELECT 1 FROM operations WHERE id = :id FOR UPDATE NOWAIT")
LOCK_ACTIVE_CONFIG_NOWAIT = sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE NOWAIT")


async def reserved(engine: AsyncEngine, kw: dict[str, object]) -> Reservation:
    async with engine.begin() as conn:
        return await reserve(conn, **kw)  # type: ignore[arg-type]


async def dispatched(engine: AsyncEngine, kw: dict[str, object]) -> Reservation:
    """A reservation whose dispatch marker committed, as the gateway makes it before the send."""
    async with engine.begin() as conn:
        res = await reserve(conn, **kw)  # type: ignore[arg-type]
        await mark_dispatched(conn, res, kw["owner"], now=kw["now"])  # type: ignore[arg-type]
    return res


async def billed(engine: AsyncEngine, res: Reservation, *, now: datetime, usage: Usage = USAGE) -> None:
    async with engine.begin() as conn:
        await settle(conn, res, outcome="billed", evidence=None, usage=usage, call=completed_call(), now=now)


async def drift(engine: AsyncEngine, month: date) -> Drift | None:
    async with engine.connect() as conn:
        return await recompute(conn, month)


async def unsettled_pass(engine: AsyncEngine, queue_app, now: datetime) -> int:
    return await spend_ledger.reconcile_llm_ledger_unsettled(ctx(engine, queue_app, now=now))


async def eval_status(engine: AsyncEngine, eval_run_id: int) -> str:
    async with engine.connect() as conn:
        result = await conn.execute(sa.text("SELECT status FROM eval_runs WHERE id = :i"), {"i": eval_run_id})
        return str(result.scalar_one())


async def nowait_refused(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> bool:
    """Whether `statement` (a `... NOWAIT` lock) is refused because another transaction holds the row."""
    async with engine.connect() as conn:
        try:
            await conn.execute(statement, params)
        except sa.exc.OperationalError as exc:
            assert pg_error(exc).sqlstate == LOCK_NOT_AVAILABLE
            return True
        finally:
            await conn.rollback()
    return False


# ---- serialisation (§6.9.1, §18.2) ------------------------------------------------------------------------


async def test_eight_concurrent_sessions_never_exceed_the_cap(engine, op_owner, migrated: PgServer) -> None:
    now = utcnow()
    month = month_of(now)
    small = BudgetParams(
        cap_usd=Decimal("1.00"),
        overrun_buffer_usd=Decimal("0.10"),
        nonscoring_reserve_pct=5,
        min_bucket_capacity_usd=Decimal("1.00"),
        collection_interval_s=6 * 3600,
    )
    async with engine.begin() as conn:
        await ensure_month(conn, month, small, now)  # A = $0.90 (the settings' 5 % reserve applies)
    # each try reserves $0.25: 10,000 x $5 (the cache-write price bounds input) + 10,000 x $20 per MTok
    assert worst_case_usd("claude-opus-5-5", input_tokens_bound=10_000, max_tokens=10_000) == Decimal("0.25")
    kinds = ["nonscoring", "scoring", "nonscoring", "cap", "scoring", "nonscoring", "scoring", "nonscoring"]

    async def session(kind: str) -> Reservation | BudgetExhausted | None:
        async with engine.begin() as conn:
            if kind == "cap":  # a save lowers the cap to $0.60 (A = $0.50) under the same lock
                await change_cap(
                    conn, month=month, cap_usd=Decimal("0.60"), overrun_buffer_usd=Decimal("0.10"), now=now
                )
                return None
            make = scoring_kw if kind == "scoring" else reserve_kw
            try:
                return await reserve(conn, **make(op_owner, now=now, bound=10_000, max_tokens=10_000))
            except BudgetExhausted as exc:
                return exc

    # The barrier: the month row is held while the 8 sessions start, so all of them wait for it at once.
    async with engine.connect() as blocker:
        await blocker.begin()
        await blocker.execute(LOCK_MONTH, {"m": month})
        tasks = [asyncio.create_task(session(kind)) for kind in kinds]

        async def all_waiting() -> bool:
            return lock_waits(migrated) >= len(kinds) or any(t.done() for t in tasks)

        await await_until(all_waiting, 30)
        assert not any(t.done() for t in tasks)  # nobody got past the month row
        await blocker.rollback()
    results = await asyncio.gather(*tasks)

    admitted = [r for r in results if isinstance(r, Reservation)]
    denied = [r for r in results if isinstance(r, BudgetExhausted)]
    assert (
        len(admitted) + len(denied) == 7 and 2 <= len(admitted) <= 3
    )  # A holds three before the edit, two after
    async with engine.connect() as conn:
        audit = (
            await conn.execute(
                sa.text(
                    "SELECT counted_before_usd, reserved_usd, admitted_cap_usd FROM llm_spend_ledger "
                    "ORDER BY counted_before_usd"
                )
            )
        ).all()
    # one admission at a time: each saw the counted spend of all the earlier ones
    assert [row.counted_before_usd for row in audit] == [Decimal("0.25") * n for n in range(len(admitted))]
    for row in audit:
        assert row.counted_before_usd + row.reserved_usd <= row.admitted_cap_usd - Decimal("0.10")
    (stored,) = await budget_rows(engine, month)
    assert stored is not None and stored.cap_usd == Decimal("0.60")
    assert stored.counted_usd == Decimal("0.25") * len(admitted) and budget.cap_holds(stored)
    assert await drift(engine, month) is None


async def test_lock_order_owner_then_month(engine, op_owner, migrated: PgServer) -> None:
    """§3.3.3: `active_config` → owner row → `llm_budget` → ledger rows, on the reservation path."""
    now = utcnow()
    month = month_of(now)
    op_id = op_owner.claim.operation_id
    async with engine.begin() as conn:
        await ensure_month(conn, month, await current_params(conn), now)

    # 1. The month row is held: the reservation waits there, already holding active_config and its owner row.
    async with engine.connect() as blocker:
        await blocker.begin()
        await blocker.execute(LOCK_MONTH, {"m": month})
        task = asyncio.create_task(
            reserved(engine, reserve_kw(op_owner, now=now, bound=4000, max_tokens=1000))
        )
        await await_until(lambda: blocked_or_done(migrated, task), 10, 0.02)
        assert not task.done()
        assert await nowait_refused(engine, LOCK_OPERATION_NOWAIT, id=op_id)  # FOR SHARE by the reservation
        assert await nowait_refused(engine, LOCK_ACTIVE_CONFIG_NOWAIT)  # FOR SHARE, taken first
        assert await count_rows(engine, "llm_spend_ledger") == 0  # no ledger row before the month row
        await blocker.rollback()
    await asyncio.wait_for(task, 10)

    # 2. The owner row is held (a takeover): the reservation waits there and has not touched the month row.
    async with engine.connect() as blocker:
        await blocker.begin()
        await blocker.execute(LOCK_OPERATION, {"id": op_id})
        task = asyncio.create_task(
            reserved(engine, reserve_kw(op_owner, now=now, bound=4000, max_tokens=1000))
        )
        await await_until(lambda: blocked_or_done(migrated, task), 10, 0.02)
        assert not task.done()
        assert not await nowait_refused(engine, LOCK_MONTH_NOWAIT, m=month)  # the month row is free
        await blocker.rollback()
    await asyncio.wait_for(task, 10)
    assert await count_rows(engine, "llm_spend_ledger") == 2


# ---- the month boundary and cap edits (Review Focus 4) ----------------------------------------------------


async def test_settlement_after_rollover_adjusts_the_reservation_month(engine, op_owner) -> None:
    t = datetime(2026, 10, 31, 23, 59, 30, tzinfo=UTC)
    async with engine.begin() as conn:
        res = await reserve(conn, **reserve_kw(op_owner, now=t, bound=4000, max_tokens=1000))
        await mark_dispatched(conn, res, op_owner, now=t)
    async with engine.begin() as conn:  # settled after midnight UTC
        await settle(
            conn,
            res,
            outcome="billed",
            evidence=None,
            usage=Usage(1000, 200, 0, 0),
            call=completed_call(),
            now=t + timedelta(minutes=2),
        )
    oct_row, nov_row = await budget_rows(engine, date(2026, 10, 1), date(2026, 11, 1))
    assert oct_row.counted_usd == Decimal("0.008000") and nov_row is None  # 1000x4 + 200x20 per MTok


async def test_lowered_cap_admits_nothing_but_settles_in_flight(engine, op_owner) -> None:
    t = utcnow()
    async with engine.begin() as conn:
        r1 = await reserve(conn, **reserve_kw(op_owner, now=t, bound=4000, max_tokens=1000))
        await mark_dispatched(conn, r1, op_owner, now=t)
    assert r1.reserved_usd == Decimal("0.04")
    async with engine.begin() as conn:  # a save lowers the cap below the counted $0.04
        await change_cap(
            conn, month=r1.month, cap_usd=Decimal("0.02"), overrun_buffer_usd=Decimal("0.01"), now=t + MINUTE
        )
    (row,) = await budget_rows(engine, r1.month)
    assert row is not None
    assert (row.cap_lowered_at, row.counted_at_cap_change_usd) == (t + MINUTE, Decimal("0.04"))
    with pytest.raises(BudgetExhausted) as exhausted:
        async with engine.begin() as conn:
            await reserve(conn, **reserve_kw(op_owner, now=t + 2 * MINUTE, bound=1000, max_tokens=256))
    assert (exhausted.value.purpose_class, exhausted.value.headroom_usd) == ("nonscoring", Decimal(0))
    async with engine.begin() as conn:  # the in-flight call still settles, against its own month
        await settle(
            conn, r1, outcome="billed", evidence=None, usage=USAGE, call=completed_call(), now=t + 3 * MINUTE
        )
    (row,) = await budget_rows(engine, r1.month)
    assert row is not None and row.counted_usd == Decimal("0.008")
    assert row.counted_at_cap_change_usd is not None
    assert row.counted_usd <= max(row.cap_usd, row.counted_at_cap_change_usd)
    assert (await ledger_row(engine, r1.ledger_id)).state == "billed"
    assert await count_rows(engine, "llm_spend_ledger") == 1  # the refused reservation wrote nothing
    assert await drift(engine, r1.month) is None


async def test_old_context_cap_cut_and_rollover(engine, op_owner) -> None:
    oct31 = datetime(2026, 10, 31, 23, 50, tzinfo=UTC)
    nov1 = datetime(2026, 11, 1, 0, 5, tzinfo=UTC)
    first = await dispatched(engine, reserve_kw(op_owner, now=oct31, bound=4000, max_tokens=1000))
    (oct_row,) = await budget_rows(engine, date(2026, 10, 1))
    assert oct_row is not None and oct_row.cap_usd == Decimal("100.00")
    await save_budget(engine, now=oct31 + 5 * MINUTE, cap_usd="10.00")
    (oct_row,) = await budget_rows(engine, date(2026, 10, 1))
    assert oct_row is not None and oct_row.cap_usd == Decimal("10.00")  # the save changed the October row
    async with engine.begin() as conn:  # the first try fails after midnight: October gets the refund
        await settle(
            conn,
            first,
            outcome="not_billed",
            evidence="error_response_no_content",
            usage=None,
            call=error_call(529),
            now=nov1 - MINUTE,
        )
    retry = await reserved(
        engine, reserve_kw(op_owner, now=nov1, bound=4000, max_tokens=1000, transport_try=2)
    )
    oct_row, nov_row = await budget_rows(engine, date(2026, 10, 1), date(2026, 11, 1))
    assert retry.month == date(2026, 11, 1)
    assert oct_row is not None and oct_row.counted_usd == 0
    assert nov_row is not None and (nov_row.cap_usd, nov_row.overrun_buffer_usd) == (
        Decimal("10.00"),
        Decimal("2.00"),
    )
    async with engine.begin() as conn:
        current = await current_params(conn)
    assert current.cap_usd == Decimal("10.00")
    assert nov_row.bucket_usd == budget.new_row(date(2026, 11, 1), current, nov1).bucket_usd  # starts full
    assert nov_row.counted_usd == retry.reserved_usd


async def test_cap_80pct_opens_at_a_reservation_and_a_raise_resolves_it(engine, op_owner) -> None:
    t = utcnow()
    async with engine.begin() as conn:
        await ensure_month(conn, month_of(t), await current_params(conn), t)
        await change_cap(
            conn, month=month_of(t), cap_usd=Decimal("0.05"), overrun_buffer_usd=Decimal(0), now=t
        )
    await reserved(engine, reserve_kw(op_owner, now=t, bound=4000, max_tokens=1000))  # $0.04 of $0.05
    assert await open_alerts(engine, CAP_80PCT_ALERT) == [("llm.cap_80pct:budget", "warn", 1)]
    await reserved(engine, reserve_kw(op_owner, now=t, bound=100, max_tokens=256))
    assert await open_alerts(engine, CAP_80PCT_ALERT) == [("llm.cap_80pct:budget", "warn", 1)]  # still open
    async with engine.begin() as conn:
        await change_cap(
            conn, month=month_of(t), cap_usd=Decimal("1.00"), overrun_buffer_usd=Decimal(0), now=t
        )
    assert await open_alerts(engine, CAP_80PCT_ALERT) == []


async def test_native_pdf_extra_comes_from_the_locked_month_row(engine, op_owner) -> None:
    t = utcnow()
    worst = worst_case_usd("claude-opus-5-5", input_tokens_bound=4000, max_tokens=1000)
    await save_budget(engine, now=t, overrun_buffer_usd="4.00")
    first = await reserved(
        engine, reserve_kw(op_owner, now=t, bound=4000, max_tokens=1000, native_pdf_extra=True)
    )
    assert first.reserved_usd == worst + Decimal("1.00")  # 0.25 x the row's $4.00, not the seeded $2.00
    async with engine.begin() as conn:  # the row alone changes: the reservation still reads the row
        await change_cap(
            conn, month=month_of(t), cap_usd=Decimal("100.00"), overrun_buffer_usd=Decimal("6.00"), now=t
        )
    second = await reserved(
        engine, reserve_kw(op_owner, now=t, bound=4000, max_tokens=1000, native_pdf_extra=True)
    )
    assert second.reserved_usd == worst + Decimal("1.50")
    plain = await reserved(engine, reserve_kw(op_owner, now=t, bound=4000, max_tokens=1000))
    assert plain.reserved_usd == worst


async def test_read_month_accrues_in_memory_takes_no_lock_and_writes_nothing(engine, op_owner) -> None:
    t = utcnow()
    month = month_of(t)
    async with engine.begin() as conn:
        p = await current_params(conn)
        assert await read_month(conn, month, p, now=t) is None  # no row yet
        await ensure_month(conn, month, p, t)
        await conn.execute(
            sa.text("UPDATE llm_budget SET bucket_usd = 0, bucket_at = :t WHERE month = :m"),
            {"t": t, "m": month},
        )
    (before,) = await budget_rows(engine, month)
    async with engine.connect() as blocker:
        await blocker.begin()
        await blocker.execute(LOCK_MONTH, {"m": month})  # a reservation holds the row
        async with engine.connect() as conn:
            row = await asyncio.wait_for(read_month(conn, month, p, now=t + timedelta(hours=1)), 5)
            await conn.rollback()
        await blocker.rollback()
    assert row is not None
    assert row.bucket_usd == budget.accrue(Decimal(0), t, t + timedelta(hours=1), p, month)
    assert row.bucket_usd > 0 and row.bucket_at == t + timedelta(hours=1)
    assert await budget_rows(engine, month) == (before,)  # nothing stored


# ---- settlement (§6.9.1 counter table, §10.7) -------------------------------------------------------------


async def test_bound_violation_is_charged_in_full_and_alerts(engine, op_owner) -> None:
    t = utcnow()
    res = await dispatched(engine, reserve_kw(op_owner, now=t, bound=1000, max_tokens=256))
    assert res.reserved_usd == Decimal("0.010120")  # 1,000 x $5 + 256 x $20 per MTok
    async with engine.begin() as conn:  # 5,000 input tokens on a 1,000-token bound: $0.02
        figures = await settle(
            conn,
            res,
            outcome="billed",
            evidence=None,
            usage=Usage(5000, 0, 0, 0),
            call=completed_call(),
            now=t,
        )
    row = await ledger_row(engine, res.ledger_id)
    assert (row.state, row.settled_usd, row.evidence) == (
        "billed",
        Decimal("0.02"),
        "reservation_bound_violated",
    )
    assert (row.call_status, row.cost_usd) == ("completed", Decimal("0.02"))
    (month,) = await budget_rows(engine, res.month)
    assert month is not None and month.counted_usd == Decimal("0.02")  # the full cost, from the buffer
    assert (figures.settled_usd, figures.reserved_usd) == (Decimal("0.02"), Decimal("0.010120"))
    assert await open_alerts(engine, BOUND_VIOLATED_ALERT) == [
        ("llm.reservation_bound_violated:model:claude-opus-5-5", "critical", 1)
    ]
    assert await drift(engine, res.month) is None


async def test_settlement_figures_and_the_counter_table(engine, op_owner) -> None:
    t = utcnow()
    a = await dispatched(engine, reserve_kw(op_owner, now=t, bound=4000, max_tokens=1000))
    b = await dispatched(engine, reserve_kw(op_owner, now=t, bound=4000, max_tokens=1000))
    c = await dispatched(engine, reserve_kw(op_owner, now=t, bound=4000, max_tokens=1000))
    d = await reserved(engine, reserve_kw(op_owner, now=t, bound=4000, max_tokens=1000))
    await billed(engine, a, now=t)
    async with engine.begin() as conn:
        await settle(
            conn,
            b,
            outcome="not_billed",
            evidence="connect_failed_before_send",
            usage=None,
            call=error_call(None),
            now=t,
        )
        figures = await settle(
            conn,
            c,
            outcome="ambiguous",
            evidence="stream_lost",
            usage=None,
            call=spend_ledger.CallOutcome("ambiguous"),
            now=t,
        )
    assert figures.spend_by_state == {
        "counted": Decimal("0.088"),  # a billed $0.008 + c ambiguous $0.04 + d reserved $0.04
        "billed": Decimal("0.008"),
        "ambiguous": Decimal("0.04"),
        "reserved": Decimal("0.04"),
    }
    assert (figures.settled_usd, figures.reserved_usd, figures.purpose) == (
        Decimal("0.04"),
        Decimal("0.04"),
        "extract_profile",
    )
    # the scoring headroom (§6.9.4): A - counted - the unspent non-scoring reserve; this non-scoring spend
    # comes out of its own reserve, so scoring keeps its whole $93.10
    assert figures.headroom_usd == Decimal("93.10")
    assert [(await ledger_row(engine, r.ledger_id)).state for r in (a, b, c, d)] == [
        "billed",
        "not_billed",
        "ambiguous",
        "reserved",
    ]
    assert (await ledger_row(engine, b.ledger_id)).call_status == "error"
    lost = await ledger_row(engine, c.ledger_id)
    assert (lost.cost_usd, lost.evidence) == (Decimal("0.04"), "stream_lost")  # it counts in full
    assert await drift(engine, a.month) is None
    await billed(engine, c, now=t)  # its usage turns up later: a billed row carries no ambiguous code
    late = await ledger_row(engine, c.ledger_id)
    assert (late.state, late.settled_usd, late.evidence, late.call_status) == (
        "billed",
        Decimal("0.008"),
        None,
        "completed",
    )
    assert await drift(engine, a.month) is None
    with pytest.raises(ValueError, match="evidence"):  # not_billed needs positive evidence of no charge
        async with engine.begin() as conn:
            await settle(conn, d, outcome="not_billed", evidence=None, usage=None, call=error_call(), now=t)
    with pytest.raises(ValueError, match="dispatched"):  # a reserved entry is released, never settled
        async with engine.begin() as conn:
            await settle(
                conn,
                d,
                outcome="not_billed",
                evidence="error_response_no_content",
                usage=None,
                call=error_call(),
                now=t,
            )


async def test_the_call_records_its_owner_and_deadline(engine, op_owner) -> None:
    t = utcnow()
    res = await reserved(
        engine, reserve_kw(op_owner, now=t, bound=4000, max_tokens=1000, input_refs={"x": 1})
    )
    refs = (await ledger_row(engine, res.ledger_id)).input_refs
    assert refs == {
        "x": 1,
        "owner": {
            "kind": "operation",
            "id": op_owner.claim.operation_id,
            "claim_id": str(op_owner.claim.claim_id),
        },
        "deadline_s": 900,
    }
    with pytest.raises(ValueError, match="owner"):
        await reserved(
            engine, reserve_kw(op_owner, now=t, bound=4000, max_tokens=1000, input_refs={"owner": 1})
        )


# ---- owners and fences (§3.3.3) ---------------------------------------------------------------------------


async def test_superseded_operation_owner_cannot_reserve_or_dispatch(engine, op_owner) -> None:
    t = utcnow()
    await reclaim(engine, op_owner)
    with pytest.raises(FencedOut) as fenced:
        await reserved(engine, reserve_kw(op_owner, now=t, bound=4000, max_tokens=1000))
    assert fenced.value.owner == "operation"
    for table in ("llm_spend_ledger", "llm_calls", "llm_budget"):  # the owner lock comes first
        assert await count_rows(engine, table) == 0

    # The next execution reserves; a reclaim then fences its dispatch marker, and its try is released.
    second = await claim_owner(engine, op_owner.claim.operation_id)
    res = await reserved(engine, reserve_kw(second, now=t, bound=4000, max_tokens=1000))
    await reclaim(engine, second)
    with pytest.raises(FencedOut):
        async with engine.begin() as conn:
            await mark_dispatched(conn, res, second, now=t)
    assert (await ledger_row(engine, res.ledger_id)).state == "reserved"  # nothing was written
    async with engine.begin() as conn:
        await release_never_dispatched(conn, res, now=t)
    row = await ledger_row(engine, res.ledger_id)
    assert (row.state, row.evidence, row.settled_usd, row.call_status) == (
        "not_billed",
        "never_dispatched",
        Decimal(0),
        "cancelled",
    )
    (month,) = await budget_rows(engine, res.month)
    assert month is not None and month.counted_usd == 0
    async with engine.begin() as conn:  # a second release writes nothing
        await release_never_dispatched(conn, res, now=t)
    assert await drift(engine, res.month) is None


async def test_a_global_purge_fence_refuses_reservations_and_dispatch_markers(engine, op_owner) -> None:
    t = utcnow()
    res = await reserved(engine, reserve_kw(op_owner, now=t, bound=4000, max_tokens=1000))
    async with engine.begin() as conn:
        await conn.execute(sa.text("INSERT INTO purge_fences (kind, manifest_id) VALUES ('global', 1)"))
    with pytest.raises(FencedOut) as fenced:
        await reserved(engine, reserve_kw(op_owner, now=t, bound=4000, max_tokens=1000))
    assert fenced.value.owner == "purge_fence"
    with pytest.raises(FencedOut):
        async with engine.begin() as conn:
            await mark_dispatched(conn, res, op_owner, now=t)
    assert await count_rows(engine, "llm_spend_ledger") == 1


async def test_owner_holds_tests_the_recorded_owner(engine, op_owner, eval_owner) -> None:
    async with engine.begin() as conn:
        assert await owner_holds(conn, op_owner.durable(), now=utcnow())
        assert await owner_holds(conn, eval_owner.durable(), now=utcnow())
        stale = {**op_owner.durable(), "claim_id": "00000000-0000-0000-0000-000000000000"}
        assert not await owner_holds(conn, stale, now=utcnow())
        assert not await owner_holds(conn, {"kind": "operation", "id": 10**9, "claim_id": "x"}, now=utcnow())
        assert not await owner_holds(conn, {"kind": "eval_run", "id": 10**9}, now=utcnow())
        with pytest.raises(ValueError, match="owner"):
            await owner_holds(conn, {"kind": "target", "id": 1}, now=utcnow())


# ---- the reconcilers (§17.4) ------------------------------------------------------------------------------


async def test_unsettled_entries_are_resolved_after_15_minutes(engine, queue_app, op_owner) -> None:
    t = utcnow()
    old = await reserved(engine, reserve_kw(op_owner, now=t, bound=4000, max_tokens=1000))
    await reclaim(engine, op_owner)  # a stale reclaim: the old execution's reservation is orphaned
    replacement = await claim_owner(engine, op_owner.claim.operation_id)
    kept = await reserved(engine, reserve_kw(replacement, now=t, bound=4000, max_tokens=1000))
    online = await dispatched(
        engine,
        reserve_kw(
            replacement, now=t, bound=4000, max_tokens=1000, purpose="propose_queries", deadline_s=300
        ),
    )
    states = [old, kept, online]
    assert await unsettled_pass(engine, queue_app, t + 14 * MINUTE) == 0
    assert [(await ledger_row(engine, r.ledger_id)).state for r in states] == [
        "reserved",
        "reserved",
        "dispatched",
    ]

    assert await unsettled_pass(engine, queue_app, t + 16 * MINUTE) == 2
    released, still, lost = [await ledger_row(engine, r.ledger_id) for r in states]
    assert (released.state, released.evidence, released.call_status) == (
        "not_billed",
        "never_dispatched",
        "cancelled",
    )
    assert (still.state, still.call_status) == ("reserved", "reserved")  # the same operation id, a live claim
    assert (lost.state, lost.call_status, lost.error_code) == ("ambiguous", "ambiguous", "unsettled")
    (month,) = await budget_rows(engine, old.month)
    assert month is not None and month.counted_usd == kept.reserved_usd + online.reserved_usd
    assert await drift(engine, old.month) is None
    assert await unsettled_pass(engine, queue_app, t + 30 * MINUTE) == 0  # nothing more to repair


async def test_dispatched_extraction_is_not_marked_ambiguous_inside_its_deadline(
    engine, queue_app, op_owner
) -> None:
    t = utcnow()
    res = await dispatched(engine, reserve_kw(op_owner, now=t, bound=4000, max_tokens=1000))  # deadline 900 s
    assert await unsettled_pass(engine, queue_app, t + 16 * MINUTE) == 0  # its stream may still be live
    assert (await ledger_row(engine, res.ledger_id)).state == "dispatched"
    assert await unsettled_pass(engine, queue_app, t + 21 * MINUTE) == 1  # past 900 s + 5 min
    row = await ledger_row(engine, res.ledger_id)
    assert (row.state, row.call_status) == ("ambiguous", "ambiguous")


async def test_late_settlement_after_the_reconciler(engine, queue_app, op_owner, caplog) -> None:
    t = utcnow()
    kw = reserve_kw(op_owner, now=t, bound=4000, max_tokens=1000, purpose="propose_queries", deadline_s=300)
    res = await dispatched(engine, kw)
    assert await unsettled_pass(engine, queue_app, t + 16 * MINUTE) == 1
    assert (await ledger_row(engine, res.ledger_id)).state == "ambiguous"
    (month,) = await budget_rows(engine, res.month)
    assert month is not None and month.counted_usd == res.reserved_usd  # ambiguous keeps the maximum charge

    async with engine.begin() as conn:  # the gateway's settlement arrives after the reconciler's
        figures = await settle(
            conn,
            res,
            outcome="billed",
            evidence=None,
            usage=USAGE,
            call=completed_call(),
            now=t + 17 * MINUTE,
        )
    row = await ledger_row(engine, res.ledger_id)
    assert (row.state, row.settled_usd, row.evidence) == ("billed", Decimal("0.008"), None)
    assert (row.call_status, row.cost_usd, row.error_code) == ("completed", Decimal("0.008"), None)
    (month,) = await budget_rows(engine, res.month)
    assert month is not None and month.counted_usd == Decimal("0.008") == figures.spend_by_state["counted"]
    assert await drift(engine, res.month) is None

    async with engine.begin() as conn:  # a duplicate settlement writes nothing
        await settle(
            conn,
            res,
            outcome="not_billed",
            evidence="error_response_no_content",
            usage=None,
            call=error_call(),
            now=t + 18 * MINUTE,
        )
    (event,) = logged(caplog, "llm_settle_duplicate")
    assert (event["ledger_id"], event["state"], event["outcome"]) == (res.ledger_id, "billed", "not_billed")
    assert (await ledger_row(engine, res.ledger_id)).state == "billed"
    assert await budget_rows(engine, res.month) == (month,)


async def test_abandoned_eval_run_releases_its_reservation(
    engine, queue_app, current_epoch, eval_owner
) -> None:
    t = utcnow()
    # a schema dry-run that died inside a call, before its dispatch marker
    stuck = await reserved(engine, reserve_kw(eval_owner, now=t, bound=4000, max_tokens=2000))
    # one that died between two completed cases, with no unsettled entry
    between = await new_eval_owner(engine)
    await billed(
        engine, await dispatched(engine, reserve_kw(between, now=t, bound=4000, max_tokens=2000)), now=t
    )
    # one whose dispatched call never settled
    streaming = await new_eval_owner(engine)
    live = await dispatched(engine, reserve_kw(streaming, now=t, bound=4000, max_tokens=2000))

    # 16 minutes on: the dispatched call is ambiguous (its recorded deadline is 300 s); the runs are young
    assert await unsettled_pass(engine, queue_app, t + 16 * MINUTE) == 1
    assert (await ledger_row(engine, live.ledger_id)).state == "ambiguous"
    assert (await ledger_row(engine, stuck.ledger_id)).state == "reserved"  # its owner still holds
    # an hour on: the three runs failed, so the stuck reservation's owner no longer holds
    assert await unsettled_pass(engine, queue_app, t + spend_ledger.EVAL_RUN_ABANDONED + MINUTE) == 4
    for owner in (eval_owner, between, streaming):
        assert await eval_status(engine, owner.eval_run_id) == "failed"
    row = await ledger_row(engine, stuck.ledger_id)
    assert (row.state, row.evidence, row.call_status) == ("not_billed", "never_dispatched", "cancelled")
    (month,) = await budget_rows(engine, stuck.month)
    assert month is not None and month.counted_usd == Decimal("0.008") + live.reserved_usd
    assert await drift(engine, stuck.month) is None


async def test_the_unsettled_reconciler_follows_the_fence_rule(
    engine, queue_app, current_epoch, op_owner
) -> None:
    t = utcnow()
    res = await reserved(engine, reserve_kw(op_owner, now=t, bound=4000, max_tokens=1000))
    await reclaim(engine, op_owner)
    later = t + 16 * MINUTE
    superseded = ctx(engine, queue_app, now=later, epoch=current_epoch + 1)
    await assert_fence_rule(
        "llm_ledger_unsettled", "tick", superseded, ctx(engine, queue_app, now=later), spend_ledger
    )
    assert (await ledger_row(engine, res.ledger_id)).state == "reserved"  # neither check wrote anything
    assert await unsettled_pass(engine, queue_app, later) == 1


async def test_drift_is_corrected_and_alerted(engine, queue_app, current_epoch, op_owner) -> None:
    t = utcnow()
    res = await dispatched(engine, reserve_kw(op_owner, now=t, bound=4000, max_tokens=1000))
    await billed(engine, res, now=t)
    await reserved(engine, reserve_kw(op_owner, now=t, bound=4000, max_tokens=1000))
    assert await drift(engine, res.month) is None
    async with engine.begin() as conn:  # a counter update lost by some defect
        await conn.execute(
            sa.text(
                "UPDATE llm_budget SET counted_usd = counted_usd + 1, nonscoring_counted_usd = "
                "nonscoring_counted_usd + 1 WHERE month = :m"
            ),
            {"m": res.month},
        )
    found = await drift(engine, res.month)
    assert found == Drift(
        month=res.month,
        stored=Counters(Decimal("1.048"), Decimal(0), Decimal("1.048")),
        ledger=Counters(Decimal("0.048"), Decimal(0), Decimal("0.048")),
    )
    heavy = ctx(engine, queue_app)
    await assert_fence_rule(
        "budget_counters", "heavy", ctx(engine, queue_app, epoch=current_epoch + 1), heavy, spend_ledger
    )
    assert await drift(engine, res.month) == found  # the fenced passes wrote nothing

    assert await spend_ledger.reconcile_budget_counters(heavy) == 1
    (month,) = await budget_rows(engine, res.month)
    assert month is not None
    assert (month.counted_usd, month.scoring_counted_usd, month.nonscoring_counted_usd) == (
        Decimal("0.048"),
        Decimal(0),
        Decimal("0.048"),
    )
    key = f"llm.budget_counter_drift:month:{res.month.isoformat()}"
    assert await open_alerts(engine, COUNTER_DRIFT_ALERT) == [(key, "critical", 1)]
    assert await spend_ledger.reconcile_budget_counters(heavy) == 0  # a clean pass resolves it
    assert await open_alerts(engine, COUNTER_DRIFT_ALERT) == []
