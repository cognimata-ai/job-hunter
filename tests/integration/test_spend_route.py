"""`GET /api/system/spend` and the `llm` section of `GET /api/system/health` (spec §14.2, §17.5; plan Task
16): the month row read without a lock (`spend_ledger.read_month`), the totals from the ledger. M3 Task 21:
`c_hat` with its source and `critic_rate_7d` of the active tuple, with and without history, the bulk
operation, the quarantined batch spend, the pause reason, and the eval spend apart from live scoring (Carry
T19-d)."""

import asyncio
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.seed import load_settings_v1
from jobhunter.llm import budget
from jobhunter.llm.budget import BudgetParams, month_of
from jobhunter.llm.spend_ledger import (
    BOUND_VIOLATED_ALERT,
    CallOutcome,
    EvalRunOwner,
    mark_dispatched,
    reserve,
    settle,
)
from jobhunter.observability.alerts import raise_alert
from jobhunter.settings.llm_projection import central_cost_per_target
from jobhunter.settings.model import ModelPolicy
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.eval import insert_cases
from tests.support.llm import USAGE, budget_rows, completed_call, reserve_kw, save_budget
from tests.support.m3 import (
    insert_attempt,
    insert_bulk_operation,
    insert_id,
    insert_job,
    insert_scoring_call,
    insert_submission,
    insert_target,
)
from tests.support.scoring import activate_score_tuple

pytestmark = pytest.mark.integration

LOCK_MONTH = sa.text("SELECT 1 FROM llm_budget WHERE month = :m FOR UPDATE")
LOCK_ACTIVE_CONFIG = sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE")


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """One clock for both routes, so the bucket is accrued to the same instant."""
    t = utcnow()
    monkeypatch.setattr("jobhunter.api.routes.spend.utcnow", lambda: t)
    monkeypatch.setattr("jobhunter.api.routes.system.utcnow", lambda: t)
    return t


async def billed_and_ambiguous(engine: AsyncEngine, owner, t: datetime) -> None:
    """Two dispatched `extract_profile` tries of $0.04 each (4,000 x $5 + 1,000 x $20 per MTok on Opus): one
    billed at $0.008, one lost (`ambiguous`, counted at its reservation)."""
    kw = reserve_kw(owner, now=t, bound=4000, max_tokens=1000)
    reservations = []
    for _ in range(2):
        async with engine.begin() as conn:
            res = await reserve(conn, **kw)
            await mark_dispatched(conn, res, owner, now=t)
        reservations.append(res)
    async with engine.begin() as conn:
        await settle(
            conn, reservations[0], outcome="billed", evidence=None, usage=USAGE, call=completed_call(), now=t
        )
        await settle(
            conn,
            reservations[1],
            outcome="ambiguous",
            evidence="stream_lost",
            usage=None,
            call=CallOutcome("ambiguous"),
            now=t,
        )


def usd(amount: Decimal) -> str:
    return format(amount.quantize(budget.USD_QUANTUM, rounding=ROUND_HALF_UP), "f")


async def spend(client) -> dict[str, Any]:
    r = await client.get("/api/system/spend")
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


async def test_the_totals_and_the_headroom_match_the_ledger(logged_in_client, engine, op_owner, now) -> None:
    await billed_and_ambiguous(engine, op_owner, now)
    body = await spend(logged_in_client)
    (row,) = await budget_rows(engine, month_of(now))
    p = BudgetParams.from_payload(load_settings_v1())
    budget.accrue_row(row, p, now)  # the route accrues in memory to its own clock
    assert body["month"] == month_of(now).isoformat()
    assert (body["billed"], body["ambiguous"], body["reserved"]) == ("0.008000", "0.040000", "0.000000")
    assert Decimal(body["counted"]) == row.counted_usd == Decimal("0.048")  # billed + ambiguous
    assert body["cap"] == "100.00"
    # the scoring headroom (§6.9.4): A - counted - the unspent non-scoring reserve; this spend is non-scoring
    assert Decimal(body["headroom"]) == budget.headroom(row, "scoring", p) == Decimal("93.10")
    assert body["bucket"] == {
        "usd": format(row.bucket_usd, "f"),
        "capacity": usd(budget.bucket_capacity(p, row.month)),
        "rate_per_h": usd(budget.rate_per_s(p, row.month) * 3600),
    }
    assert body["c_hat"] == format(central_cost_per_target(ModelPolicy()), "f") == "0.052646"
    assert body["projected_critic_rate"] == "0.450"
    assert (body["critic_rate_7d"], body["bulk_operation"], body["quarantined_batch_usd"]) == (
        None,
        None,
        "0.000000",  # no quarantined batch submission (M3 Task 17: `batches.quarantined_usd`)
    )
    assert body["by_model_effort_role"] == [
        {
            "model": "claude-opus-5-5",
            "effort": "medium",
            "role": "extract_profile",
            "calls": 2,
            "counted": "0.048000",
        }
    ]
    health = (await logged_in_client.get("/api/system/health")).json()
    assert health["llm"] == {
        "month": body["month"],
        "counted": body["counted"],
        "cap": body["cap"],
        "headroom": body["headroom"],
        "bucket_usd": body["bucket"]["usd"],
    }


async def test_a_month_without_a_row_reports_the_settings_cap_and_no_spend(
    logged_in_client, engine, now
) -> None:
    (row,) = await budget_rows(engine, month_of(now))
    assert row is None
    body = await spend(logged_in_client)
    p = BudgetParams.from_payload(load_settings_v1())
    full = format(budget.new_row(month_of(now), p, now).bucket_usd, "f")
    zero = "0.000000"
    assert (body["counted"], body["billed"], body["ambiguous"], body["reserved"]) == (zero, zero, zero, zero)
    assert (body["cap"], body["headroom"], body["bucket"]["usd"]) == ("100.00", "93.100000", full)
    assert body["by_model_effort_role"] == []
    health = (await logged_in_client.get("/api/system/health")).json()
    assert health["llm"] == {
        "month": month_of(now).isoformat(),
        "counted": "0.000000",
        "cap": "100.00",
        "headroom": "93.100000",
        "bucket_usd": full,
    }
    (row,) = await budget_rows(engine, month_of(now))
    assert row is None  # nothing was stored


async def test_a_saved_cap_is_the_row_s_cap(logged_in_client, engine, now) -> None:
    await save_budget(engine, now=now, cap_usd="40.00")
    body = await spend(logged_in_client)
    assert (body["cap"], body["headroom"]) == ("40.00", "36.100000")  # (40 - 2) x 0.95


async def test_a_cap_at_or_below_the_buffer_accrues_nothing(logged_in_client, engine, now) -> None:
    """`A = $1.00 - $2.00 < 0` admits nothing: the bucket rate is 0, never negative (as in the dry-run), the
    capacity its floor and the headroom 0."""
    await save_budget(engine, now=now, cap_usd="1.00")
    body = await spend(logged_in_client)
    assert (body["cap"], body["headroom"]) == ("1.00", "0.000000")
    assert (body["bucket"]["rate_per_h"], body["bucket"]["capacity"]) == ("0.000000", "1.000000")


async def test_spend_takes_no_lock_and_needs_a_session(
    api_client, logged_in_client, engine, op_owner, now
) -> None:
    assert (await api_client.get("/api/system/spend")).status_code == 401
    await billed_and_ambiguous(engine, op_owner, now)
    async with engine.connect() as blocker:
        await blocker.begin()
        await blocker.execute(LOCK_ACTIVE_CONFIG)  # a settings save in progress
        await blocker.execute(LOCK_MONTH, {"m": month_of(now)})  # a reservation in progress
        body = await asyncio.wait_for(spend(logged_in_client), 10)
        await blocker.rollback()
    assert body["counted"] == "0.048000"


async def test_a_later_settlement_moves_the_figures(logged_in_client, engine, op_owner, now) -> None:
    await billed_and_ambiguous(engine, op_owner, now)
    kw = reserve_kw(op_owner, now=now + timedelta(seconds=1), bound=4000, max_tokens=1000)
    async with engine.begin() as conn:
        await reserve(conn, **kw)  # in flight: counted at its reservation
    body = await spend(logged_in_client)
    assert (body["counted"], body["reserved"]) == ("0.088000", "0.040000")
    assert body["by_model_effort_role"][0]["calls"] == 3


# ---- M3 Task 21: ĉ, the critic rate, the bulk operation, the quarantine, the pause and the eval spend ------


async def history(engine: AsyncEngine, tuple_id: int, *, ended: datetime) -> None:
    """50 online, charged attempts of the tuple under the active evaluation policy that ended published at
    `ended`, half at $0.10 and half at $0.20, the first ten with a dispatched first-round critic call."""
    async with engine.begin() as conn:
        job = await insert_job(conn)
        target = await insert_target(conn, job, tuple_id, state="refused", refusal_category="other")
        for attempt_no in range(1, 51):
            attempt = await insert_attempt(
                conn,
                target,
                attempt_no=attempt_no,
                outcome="published_accepted",
                charged=True,
                cost_usd=Decimal("0.100000") if attempt_no % 2 else Decimal("0.200000"),
                started_at=ended - timedelta(minutes=5),
                ended_at=ended,
            )
            if attempt_no <= 10:
                _, call = await insert_scoring_call(
                    conn, attempt_id=attempt, target_id=target, purpose="critic"
                )
                await conn.execute(
                    sa.text("UPDATE llm_calls SET round = 1, status = 'dispatched' WHERE id = :c"),
                    {"c": call},
                )


async def test_without_history_c_hat_is_central_and_there_is_no_critic_rate_or_bulk_operation(
    logged_in_client, engine, now
) -> None:
    body = await spend(logged_in_client)
    assert (body["c_hat"], body["c_hat_source"]) == ("0.052646", "central")
    assert (body["critic_rate_7d"], body["bulk_operation"], body["quarantined_batch_usd"]) == (
        None,
        None,
        "0.000000",
    )
    assert body["pause_reason"] == "schema_unverified"  # this api is a live process: nothing verified yet
    assert body["eval_by_model_role"] == []
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
    await history(engine, tuple_id, ended=now - timedelta(days=8))  # outside the trailing 7 days
    body = await spend(logged_in_client)
    assert (body["c_hat"], body["c_hat_source"], body["critic_rate_7d"]) == ("0.052646", "central", None)


async def test_with_history_c_hat_and_the_critic_rate_are_the_tuple_s_and_the_bulk_operation_is_shown(
    logged_in_client, engine, now
) -> None:
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        bulk_id = await insert_bulk_operation(
            conn, tuple_id, budget_usd=Decimal("37.240000"), spent_usd=Decimal("1.500000")
        )
        await conn.execute(
            sa.text("UPDATE bulk_operations SET followup_held_usd = 0.25 WHERE id = :b"), {"b": bulk_id}
        )
        submission = await insert_submission(
            conn,
            tuple_id,
            bulk_id,
            state="submit_ambiguous",
            submit_started_at=now,
            reserved_usd=Decimal("0.012000"),
        )
        await raise_alert(
            conn,
            code=BOUND_VIOLATED_ALERT,
            severity="critical",
            subject="model:claude-sonnet-5-5",
            message="test",
        )
    await history(engine, tuple_id, ended=now - timedelta(days=1))
    body = await spend(logged_in_client)
    assert (body["c_hat"], body["c_hat_source"]) == ("0.150000", "history")  # 25 x $0.10 and 25 x $0.20
    assert body["critic_rate_7d"] == "0.2000"  # 10 of 50
    assert body["bulk_operation"] == {
        "id": bulk_id,
        "state": "active",
        "budget_usd": "37.240000",
        "spent_usd": "1.500000",
        "followup_held_usd": "0.250000",
        "llm_tuple_id": tuple_id,
    }
    assert body["quarantined_batch_usd"] == "0.012000" and submission
    assert body["pause_reason"] == "reservation_bound_violated"


async def test_eval_spend_is_reported_apart_from_live_scoring_and_outlives_its_case(
    logged_in_client, engine, eval_owner, now
) -> None:
    """Carry T19-d: an eval case's calls are non-scoring tries of the eval run; they never join the live
    scoring rows of the same model, effort and role, and once the case is deleted (its call rows go with it by
    cascade) their ledger entries still count, in the eval spend, never as a row with no effort."""
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        job = await insert_job(conn)
        target = await insert_target(conn, job, tuple_id)
        attempt = await insert_attempt(conn, target)
        await insert_scoring_call(
            conn, attempt_id=attempt, target_id=target
        )  # live: Sonnet, medium, generate
        run = await insert_id(
            conn,
            "eval_runs",
            {"kind": "golden", "route": "production", "status": "running", "repetitions": 2},
        )
    (case,) = await insert_cases(engine, 1)
    owner = EvalRunOwner(run)
    kw = reserve_kw(
        owner,
        now=now,
        bound=4000,
        max_tokens=1000,
        purpose="generate",
        model="claude-sonnet-5-5",
        deadline_s=600,
        golden_case_id=case.id,
    )
    async with engine.begin() as conn:
        reservation = await reserve(conn, **kw)
        await mark_dispatched(conn, reservation, owner, now=now)
    async with engine.begin() as conn:
        await settle(
            conn, reservation, outcome="billed", evidence=None, usage=USAGE, call=completed_call(), now=now
        )
    body = await spend(logged_in_client)
    live = {
        "model": "claude-sonnet-5-5",
        "effort": "medium",
        "role": "generate",
        "calls": 1,
        "counted": "0.100000",
    }
    assert body["by_model_effort_role"] == [live]
    (eval_row,) = body["eval_by_model_role"]
    assert (eval_row["model"], eval_row["role"], eval_row["calls"]) == ("claude-sonnet-5-5", "generate", 1)
    counted = eval_row["counted"]
    assert Decimal(counted) > 0
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE eval_runs SET status = 'completed', finished_at = now() WHERE id = :r"),
            {"r": run},
        )
    r = await logged_in_client.delete(f"/api/golden-cases/{case.id}")
    assert r.status_code == 204, r.text
    async with engine.connect() as conn:
        calls = (
            await conn.execute(sa.text("SELECT count(*) FROM llm_calls WHERE eval_run_id = :r"), {"r": run})
        ).scalar_one()
    assert calls == 0  # gone with the case
    body = await spend(logged_in_client)
    assert body["by_model_effort_role"] == [live]  # no effort-less row
    assert body["eval_by_model_role"] == [
        {"model": "claude-sonnet-5-5", "role": "generate", "calls": 1, "counted": counted}
    ]
