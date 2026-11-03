"""The confirm modal's batch and online figures (spec §6.9.3, §15.7's confirm modal, §21.3, §21.4), on the
seeded settings (preset B) and PostgreSQL.

- `batch_capacity`: the largest submission §6.9.3 admits under the batch allocation, each item at its
  worst-case reservation (the §21.4 sizing bound at the batch prices) plus its follow-up hold
  (`bulk.hold_per_item`), at most `batch_max_items`, and none below `batch_min_targets` or with batches off;
- `online_per_day`: the USD bucket's accrual a day over the expected cost of a target with its share of a
  cycle's cache writes (§21.3's pace), `ĉ` of the active tuple (`cycles.c_hat`, central without history).

Every datum is invented; no LLM call.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

import pytest
import sqlalchemy as sa
from jobhunter.db.seed import load_settings_v1
from jobhunter.llm import bulk
from jobhunter.llm.budget import month_of
from jobhunter.llm.spend_ledger import current_params, ensure_month
from jobhunter.profile.estimate import batch_capacity, batch_item_worst_usd, rescore_estimate
from jobhunter.settings.dryrun import active_settings
from jobhunter.settings.model import Settings
from jobhunter.types import JSONValue, get_object
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.m3 import insert_attempt
from tests.support.scoring import insert_targets

pytestmark = pytest.mark.integration

SETTINGS: Final = Settings.model_validate(load_settings_v1())
# The start of a month of 30 days: the bucket accrues $93.10 / 720 h = $0.129306 an hour.
NOVEMBER: Final = datetime(2026, 11, 1, 0, 5, tzinfo=UTC)
# 31 days: $93.10 / 744 h = $0.125134 an hour.
OCTOBER: Final = datetime(2026, 10, 1, 0, 5, tzinfo=UTC)
# §21.4's per-item figures under the seeded settings:
# - the worst case: the input bound 1,878 + 36,800 - 1,000 = 37,678 tokens at Sonnet's batch cache-write price
#   ($1.25/MTok) plus 6,000 output tokens at its batch output price ($5/MTok): (37,678 x 1.25 + 6,000 x 5) /
#   10^6 = $0.0770975, rounded up to $0.077098 (§21.4: "$0.0771");
# - the hold: the seeded projected critic rate 0.450 x ĉ_followup $0.066939 = $0.03012255, rounded half-up to
#   $0.030123 (§21.4: "0.45 x $0.0669 = $0.0301");
# - together $0.107221 per item (§21.4: "about $0.1072").
WORST: Final = Decimal("0.077098")
HOLD: Final = Decimal("0.030123")
PER_ITEM: Final = WORST + HOLD


def with_scoring(**scoring: JSONValue) -> Settings:
    """The seeded settings with `scoring.<key> = value` for each of `scoring`."""
    payload = load_settings_v1()
    payload["scoring"] = {**get_object(payload, "scoring"), **scoring}
    return Settings.model_validate(payload)


async def seeded_hold(engine: AsyncEngine) -> Decimal:
    async with engine.connect() as conn:
        policy = (await conn.execute(sa.text("SELECT evaluation_policy_id FROM active_config"))).scalar_one()
        return await bulk.hold_per_item(
            conn, evaluation_policy_id=int(policy), model_policy=SETTINGS.scoring.model_policy
        )


async def online_per_day(engine: AsyncEngine, now: datetime) -> int:
    async with engine.connect() as conn:
        estimate = await rescore_estimate(conn, await active_settings(conn), now=now)
    return estimate.online_per_day


async def test_batch_capacity_is_the_largest_admitted_n(engine: AsyncEngine) -> None:
    """§21.4's second case, 1,180 retained ads and an allocation of $22.44 incl. the follow-ups:
    $22.44 / $0.107221 = 209.29, so 209 items (209 x $0.107221 = $22.409189 ≤ $22.44 < $22.516410 = 210 x
    $0.107221), §21.4's "about 209 ads"."""
    assert SETTINGS.scoring.model_policy.preset == "B"
    hold = await seeded_hold(engine)
    assert (batch_item_worst_usd(SETTINGS), hold) == (WORST, HOLD)
    n = batch_capacity(SETTINGS, targets=1180, bulk_budget_usd=Decimal("22.44"), hold_per_item_usd=hold)
    assert n == 209
    assert n * PER_ITEM <= Decimal("22.44") < (n + 1) * PER_ITEM
    # §21.4's first case, the start of a month: $37.24 / $0.107221 = 347.3, "a first batch of about 347 ads"
    assert (
        batch_capacity(SETTINGS, targets=1180, bulk_budget_usd=Decimal("37.24"), hold_per_item_usd=hold)
        == 347
    )
    # fewer targets than the allocation holds: all of them; more than `batch_max_items` (500): 500
    assert (
        batch_capacity(SETTINGS, targets=250, bulk_budget_usd=Decimal("37.24"), hold_per_item_usd=hold) == 250
    )
    assert (
        batch_capacity(SETTINGS, targets=1180, bulk_budget_usd=Decimal(1000), hold_per_item_usd=hold) == 500
    )
    raised = with_scoring(batch_max_items=800)
    assert batch_capacity(raised, targets=1180, bulk_budget_usd=Decimal(1000), hold_per_item_usd=hold) == 800
    # the hold is the caller's: without one the same allocation holds 291 worst cases ($22.44 / $0.077098)
    assert (
        batch_capacity(SETTINGS, targets=1180, bulk_budget_usd=Decimal("22.44"), hold_per_item_usd=Decimal(0))
        == 291
    )


def test_batch_capacity_zero_when_off_or_below_minimum() -> None:
    def capacity(settings: Settings, *, targets: int = 1180, budget: str = "37.24") -> int:
        return batch_capacity(
            settings, targets=targets, bulk_budget_usd=Decimal(budget), hold_per_item_usd=HOLD
        )

    assert capacity(SETTINGS) == 347
    assert capacity(with_scoring(batch_bulk_rescore=False)) == 0
    # fewer targets than `batch_min_targets` (200)
    assert (capacity(SETTINGS, targets=199), capacity(SETTINGS, targets=200)) == (0, 200)
    # an allocation that holds 199 items ($21.444199 < 200 x $0.107221) submits none; one that holds 200 does
    assert (capacity(SETTINGS, budget="21.444199"), capacity(SETTINGS, budget="21.4442")) == (0, 200)
    assert capacity(SETTINGS, budget="0") == 0
    # a minimum above what fits: none
    assert capacity(with_scoring(batch_min_targets=348)) == 0
    assert capacity(with_scoring(batch_min_targets=347)) == 347
    with pytest.raises(ValueError, match="negative"):
        capacity(SETTINGS, targets=-1)


async def test_online_per_day_from_the_bucket_rate(engine: AsyncEngine) -> None:
    """§15.7's "about 53 per day" (§21.3: "about 53 targets per day at the central cost including cache
    writes"). In November the bucket accrues $0.129306 x 24 = $3.103344 a day; the central cost of a target
    with its share of a cycle's cache writes is $0.052646 + $0.04923 / 10 = $0.057569, and $3.103344 /
    $0.057569 = 53.9, so 53. October's 31 days accrue $0.125134 x 24 = $3.003216 a day: 52.2, so 52."""
    assert await online_per_day(engine, NOVEMBER) == 53
    assert await online_per_day(engine, OCTOBER) == 52
    # the month row's own cap: A = $50.00 - $2.00, its scoring share $45.60 accrues $0.063333 an hour in
    # November, $1.519992 a day: 26.4 targets
    async with engine.begin() as conn:
        await ensure_month(conn, month_of(NOVEMBER), await current_params(conn), NOVEMBER)
        await conn.execute(
            sa.text("UPDATE llm_budget SET cap_usd = 50.00 WHERE month = :m"), {"m": month_of(NOVEMBER)}
        )
    assert await online_per_day(engine, NOVEMBER) == 26
    # a cap at the buffer admits nothing: no accrual, none a day
    at_the_buffer = sa.text("UPDATE llm_budget SET cap_usd = overrun_buffer_usd WHERE month = :m")
    async with engine.begin() as conn:
        await conn.execute(at_the_buffer, {"m": month_of(NOVEMBER)})
    assert await online_per_day(engine, NOVEMBER) == 0


async def test_online_per_day_uses_the_active_tuples_history(engine: AsyncEngine, current_epoch: int) -> None:
    """From 20 published online attempts of the active tuple in the 7 days before `now`, `ĉ` is their mean
    cost: $0.10 a target, $0.104923 with the cache writes, so $3.103344 / $0.104923 = 29.6, 29 a day."""
    (pending,) = await insert_targets(engine, 1)  # the score tuple, made active
    async with engine.begin() as conn:
        for attempt_no in range(1, 21):
            await insert_attempt(
                conn,
                pending.target_id,
                attempt_no=attempt_no,
                outcome="published_accepted",
                charged=True,
                cost_usd=Decimal("0.100000"),
                ended_at=NOVEMBER - timedelta(days=1),
            )
    assert await online_per_day(engine, NOVEMBER) == 29
    # a week later the attempts are out of the window: the central cost again
    assert await online_per_day(engine, NOVEMBER + timedelta(days=7)) == 53
