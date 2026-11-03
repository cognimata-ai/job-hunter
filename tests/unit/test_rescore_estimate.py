"""The re-score estimate of a profile confirm (spec §12.6 step 2, §6.9.3, §15.7, §21.3, §21.4; plan Task 21,
M4 Task 15): the online and batch costs of a bulk re-score, the bulk budget, the ads of one batch and the
online pace, from the seeded settings (preset B)."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final

import pytest
from jobhunter.db.seed import load_settings_v1
from jobhunter.llm import budget
from jobhunter.llm.budget import BudgetParams
from jobhunter.profile.estimate import RescoreEstimate, central_batch_cost_per_target, estimate_for
from jobhunter.settings.llm_projection import central_cost_per_target
from jobhunter.settings.model import Settings

NOW: Final = datetime(2026, 10, 1, 0, 5, tzinfo=UTC)
MONTH: Final = date(2026, 10, 1)  # 31 days: the bucket accrues $0.125134 an hour
SETTINGS: Final = Settings.model_validate(load_settings_v1())
TOLERANCE: Final = Decimal("0.10")
CENTRAL: Final = central_cost_per_target(SETTINGS.scoring.model_policy)  # $0.052646
HOLD: Final = Decimal("0.030123")  # the seeded projected critic rate 0.450 x ĉ_followup $0.066939


def month_start_row() -> budget.BudgetRow:
    return budget.new_row(MONTH, BudgetParams.from_payload(SETTINGS.to_payload()), NOW)


def estimate_of(
    targets: int, row: budget.BudgetRow, *, c_hat: Decimal = CENTRAL, settings: Settings = SETTINGS
) -> RescoreEstimate:
    return estimate_for(
        settings, targets=targets, row=row, c_hat=c_hat, hold_per_item_usd=HOLD, next_version_no=3
    )


def test_preset_b_batch_cost_per_target_follows_21_4() -> None:
    """The generator in Batches at 50% with a cold cache, $0.016325; the online follow-ups (critic $0.0249,
    revise $0.0052); the 1% repairs of the online generator call, $0.000223: $0.046671 ≈ $0.0467."""
    assert SETTINGS.scoring.model_policy.preset == "B"
    assert central_batch_cost_per_target(SETTINGS.scoring.model_policy) == Decimal("0.046671")


def test_1000_targets_on_preset_b_cost_as_21_4_says() -> None:
    estimate = estimate_of(1000, month_start_row())
    assert abs(estimate.est_usd_online - Decimal("52.60")) <= TOLERANCE  # §21.4 "≈ $52.65"
    assert abs(estimate.est_usd_batch - Decimal("46.70")) <= TOLERANCE  # "≈ $46.67 with repairs"
    assert estimate.est_usd_online == 1000 * central_cost_per_target(SETTINGS.scoring.model_policy)


def test_the_bulk_budget_at_the_start_of_a_month_is_40_percent_of_the_scoring_share() -> None:
    estimate = estimate_of(1000, month_start_row())
    assert estimate == RescoreEstimate(
        targets=1000,
        est_usd_online=Decimal("52.646000"),
        est_usd_batch=Decimal("46.671000"),
        month_spend=Decimal("0.000000"),
        cap=Decimal("100.00"),
        bulk_budget_usd=Decimal("37.240000"),  # 40% x $93.10
        bulk_share=Decimal("0.4"),
        # $37.24 / ($0.077098 + $0.030123) = 347.3 (§21.4: "a first batch of about 347 ads")
        batch_targets=347,
        online_targets=653,
        # $0.125134 x 24 = $3.003216 a day over $0.052646 + $0.04923 / 10 = $0.057569 a target: 52.2
        online_per_day=52,
        next_version_no=3,
    )


def test_the_bulk_budget_counts_only_the_scoring_spend() -> None:
    """§21.4's second case: $38.20 counted, $37.00 of it scoring: 40% x ($93.10 - $37.00) ≈ $22.44."""
    row = month_start_row()
    row.counted_usd, row.scoring_counted_usd, row.nonscoring_counted_usd = (
        Decimal("38.20"),
        Decimal("37.00"),
        Decimal("1.20"),
    )
    estimate = estimate_of(0, row)
    assert (estimate.month_spend, estimate.bulk_budget_usd) == (Decimal("38.200000"), Decimal("22.440000"))


def test_the_bulk_budget_uses_the_month_rows_cap_and_never_goes_negative() -> None:
    row = month_start_row()
    row.cap_usd = Decimal("50.00")  # a cap the month row holds: A = $48.00, scoring share $45.60
    assert estimate_of(0, row).bulk_budget_usd == Decimal("18.240000")
    row.scoring_counted_usd = row.counted_usd = Decimal("60")
    estimate = estimate_of(0, row)
    assert (estimate.cap, estimate.bulk_budget_usd) == (Decimal("50.00"), Decimal("0.000000"))


@pytest.mark.parametrize("targets", [0, 1, 7])
def test_the_costs_are_the_count_times_the_rounded_unit_costs(targets: int) -> None:
    policy = SETTINGS.scoring.model_policy
    estimate = estimate_of(targets, month_start_row())
    assert estimate.targets == targets
    assert estimate.est_usd_online == targets * central_cost_per_target(policy)
    assert estimate.est_usd_batch == targets * central_batch_cost_per_target(policy)


def test_the_batch_holds_what_the_bulk_budget_fits_and_the_rest_is_online() -> None:
    """§21.4's second case: $22.44 holds 209 items of $0.107221 (the worst case and the hold)."""
    row = month_start_row()
    row.counted_usd, row.scoring_counted_usd, row.nonscoring_counted_usd = (
        Decimal("38.20"),
        Decimal("37.00"),
        Decimal("1.20"),
    )
    estimate = estimate_of(1180, row)
    assert (estimate.bulk_budget_usd, estimate.batch_targets, estimate.online_targets) == (
        Decimal("22.440000"),
        209,
        971,
    )
    small = estimate_of(150, row)  # below `batch_min_targets`: all online
    assert (small.batch_targets, small.online_targets) == (0, 150)


def test_the_online_pace_follows_c_hat_and_the_row_s_cap() -> None:
    row = month_start_row()
    assert estimate_of(0, row).online_per_day == 52
    # ĉ $0.10: $3.003216 / ($0.10 + $0.004923) = 28.6
    assert estimate_of(0, row, c_hat=Decimal("0.100000")).online_per_day == 28
    row.cap_usd = Decimal("50.00")  # A = $48.00: $45.60 over 744 h, $0.061290 an hour, $1.470960 a day: 25.6
    assert estimate_of(0, row).online_per_day == 25
    row.cap_usd = row.overrun_buffer_usd  # A = 0: nothing accrues
    assert estimate_of(0, row).online_per_day == 0
