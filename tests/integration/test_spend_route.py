"""`GET /api/system/spend` and the `llm` section of `GET /api/system/health` (spec §14.2, §17.5; plan Task
16): the month row read without a lock (`spend_ledger.read_month`), the totals from the ledger."""

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
from jobhunter.llm.spend_ledger import CallOutcome, mark_dispatched, reserve, settle
from jobhunter.settings.llm_projection import central_cost_per_target
from jobhunter.settings.model import ModelPolicy
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.llm import USAGE, budget_rows, completed_call, reserve_kw, save_budget

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
        "0",
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
