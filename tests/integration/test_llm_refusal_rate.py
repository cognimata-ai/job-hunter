"""`llm_refusal_rate` (heavy; spec §17.6, plan Task 5): more than 5% refusals among the completed calls of the
last 24 h, with at least 20 of them, raises `llm.refusal_rate` (warn); a rate at or below 5% resolves it."""

from datetime import datetime, timedelta

import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.llm import gateway
from jobhunter.llm.gateway import REFUSAL_RATE_ALERT, reconcile_refusal_rate
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import ctx
from tests.support.llm import open_alerts
from tests.support.reconcile import assert_fence_rule

_BUDGET = sa.text(
    "INSERT INTO llm_budget (month, cap_usd, overrun_buffer_usd, bucket_usd, bucket_at) "
    "VALUES (date_trunc('month', CAST(:t AS timestamptz) AT TIME ZONE 'UTC')::date, 100, 2, 1, :t) "
    "ON CONFLICT DO NOTHING"
)
_LEDGER = sa.text(
    "INSERT INTO llm_spend_ledger (budget_month, kind, purpose, purpose_class, model_requested, state, "
    "reserved_usd, settled_usd, admitted_cap_usd, counted_before_usd, created_at) "
    "VALUES (date_trunc('month', CAST(:t AS timestamptz) AT TIME ZONE 'UTC')::date, 'request', "
    "'extract_profile', 'nonscoring', 'claude-opus-5-5', 'billed', 0.1, 0.01, 100, 0, :t) RETURNING id"
)
_CALL = sa.text(
    "INSERT INTO llm_calls (purpose, execution_config_id, model_requested, model_served, effort, max_tokens, "
    "transport, prompt_template_version, rendered_prompt_sha256, input_refs, input_tokens_bound, status, "
    "stop_reason, output_tokens, spend_ledger_id, started_at) "
    "VALUES ('extract_profile', (SELECT execution_config_id FROM active_config), 'claude-opus-5-5', "
    "'claude-opus-5-5', 'medium', 1000, 'stream', 'test@1', '\\x00', '{}', 2000, :status, :stop, 10, "
    ":ledger, :t)"
)


async def add_calls(
    engine: AsyncEngine, n: int, *, refused: int = 0, at: datetime, status: str = "completed"
) -> None:
    """`n` calls started at `at`, the first `refused` of them refusals, the others `end_turn`."""
    async with engine.begin() as conn:
        for i in range(n):
            await conn.execute(_BUDGET, {"t": at})
            ledger = (await conn.execute(_LEDGER, {"t": at})).scalar_one()
            stop = "refusal" if i < refused else "end_turn"
            await conn.execute(_CALL, {"status": status, "stop": stop, "ledger": ledger, "t": at})


async def check(engine, queue_app, now: datetime) -> int:
    return await reconcile_refusal_rate(ctx(engine, queue_app, now=now))


KEY = "llm.refusal_rate:llm"


async def test_19_calls_are_too_few_whatever_their_refusals(engine, queue_app, current_epoch) -> None:
    now = utcnow()
    await add_calls(engine, 19, refused=2, at=now - timedelta(hours=1))
    assert await check(engine, queue_app, now) == 0
    assert await open_alerts(engine, REFUSAL_RATE_ALERT) == []


async def test_two_refusals_in_20_raise_the_alert_once(engine, queue_app, current_epoch) -> None:
    now = utcnow()
    await add_calls(engine, 20, refused=2, at=now - timedelta(hours=1))
    assert await check(engine, queue_app, now) == 1
    assert await open_alerts(engine, REFUSAL_RATE_ALERT) == [(KEY, "warn", 1)]
    assert await check(engine, queue_app, now) == 0  # still true: no new occurrence
    assert await open_alerts(engine, REFUSAL_RATE_ALERT) == [(KEY, "warn", 1)]


async def test_one_refusal_in_20_is_5_percent_and_not_above(engine, queue_app, current_epoch) -> None:
    now = utcnow()
    await add_calls(engine, 20, refused=1, at=now - timedelta(hours=1))
    assert await check(engine, queue_app, now) == 0
    assert await open_alerts(engine, REFUSAL_RATE_ALERT) == []


async def test_only_completed_calls_of_the_last_24_h_count(engine, queue_app, current_epoch) -> None:
    now = utcnow()
    await add_calls(engine, 10, refused=10, at=now - timedelta(hours=25))  # outside the window
    await add_calls(engine, 10, status="error", at=now - timedelta(hours=1))  # no response
    await add_calls(engine, 19, refused=1, at=now - timedelta(hours=1))
    assert await check(engine, queue_app, now) == 0
    assert await open_alerts(engine, REFUSAL_RATE_ALERT) == []


async def test_a_rate_falling_to_5_percent_resolves_the_alert(engine, queue_app, current_epoch) -> None:
    now = utcnow()
    await add_calls(engine, 20, refused=2, at=now - timedelta(hours=2))
    assert await check(engine, queue_app, now) == 1
    await add_calls(engine, 19, at=now - timedelta(hours=1))  # 2 of 39: 5.1%, still above
    assert await check(engine, queue_app, now) == 0
    assert await open_alerts(engine, REFUSAL_RATE_ALERT) == [(KEY, "warn", 1)]
    await add_calls(engine, 1, at=now - timedelta(hours=1))  # 2 of 40: 5%, no longer above
    assert await check(engine, queue_app, now) == 1
    assert await open_alerts(engine, REFUSAL_RATE_ALERT) == []
    # once the refusals leave the window there is no rate to judge, and nothing opens again
    assert await check(engine, queue_app, now + timedelta(hours=23)) == 0
    assert await open_alerts(engine, REFUSAL_RATE_ALERT) == []


async def test_the_refusal_rate_reconciler_follows_the_fence_rule(engine, queue_app, current_epoch) -> None:
    now = utcnow()
    await add_calls(engine, 20, refused=2, at=now - timedelta(hours=1))
    superseded = ctx(engine, queue_app, now=now, epoch=current_epoch + 1)
    await assert_fence_rule("llm_refusal_rate", "heavy", superseded, ctx(engine, queue_app, now=now), gateway)
    assert await open_alerts(engine, REFUSAL_RATE_ALERT) == []  # neither check wrote anything
    assert await check(engine, queue_app, now) == 1
