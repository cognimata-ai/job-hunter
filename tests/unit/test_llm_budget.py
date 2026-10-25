"""The pure budget layer (spec §6.9.1, §6.9.2): the admission rules, the bucket and the counter table."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from importlib import resources

import pytest
from jobhunter.llm import budget
from jobhunter.llm.budget import BudgetParams, admissible, admits, bucket_capacity, month_of, rate_per_s
from jobhunter.llm.errors import (
    BudgetExhausted,
    InvalidOutput,
    LLMError,
    LLMRequestError,
    ModelMismatch,
    Refused,
)
from jobhunter.observability import metrics
from prometheus_client import generate_latest

P = BudgetParams(
    cap_usd=Decimal("100.00"),
    overrun_buffer_usd=Decimal("2.00"),
    nonscoring_reserve_pct=5,
    min_bucket_capacity_usd=Decimal("1.00"),
    collection_interval_s=6 * 3600,
)
OCT = date(2026, 10, 1)
T0 = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def budget_row():
    """`budget_row(counted=…, scoring=…, nonscoring=…, **columns)`: an October row with P's cap and buffer."""

    def make(
        *, counted: Decimal, scoring: Decimal, nonscoring: Decimal, **columns: object
    ) -> budget.BudgetRow:
        values: dict[str, object] = {
            "month": OCT,
            "cap_usd": P.cap_usd,
            "overrun_buffer_usd": P.overrun_buffer_usd,
            "counted_usd": counted,
            "scoring_counted_usd": scoring,
            "nonscoring_counted_usd": nonscoring,
            "bucket_usd": Decimal("1.00"),
            "bucket_at": T0,
            **columns,
        }
        return budget.BudgetRow(**values)  # type: ignore[arg-type]

    return make


def test_defaults_give_the_spec_shares() -> None:
    assert admissible(P) == Decimal("98.00")
    # 93.10 / (31 d x 24 h); §6.9.2 quotes ≈ 0.127 for a 30.5-day month
    assert round(rate_per_s(P, OCT) * 3600, 3) == Decimal("0.125")
    # 2 x rate x 6 h; ≈ 1.53 in §6.9.2's 30.5-day month
    assert round(bucket_capacity(P, OCT), 2) == Decimal("1.50")
    assert bucket_capacity(BudgetParams(**{**P.__dict__, "collection_interval_s": 3600}), OCT) == Decimal(
        "1.00"
    )
    assert month_of(datetime(2026, 10, 31, 23, 59, tzinfo=UTC)) == OCT


def test_scoring_never_uses_the_unspent_nonscoring_reserve(budget_row) -> None:
    row = budget_row(counted=Decimal("93.00"), scoring=Decimal("93.00"), nonscoring=Decimal(0))
    assert (
        admits(row, Decimal("0.10"), "scoring", P) is True
        and admits(row, Decimal("0.11"), "scoring", P) is False
    )
    assert (
        admits(row, Decimal("5.00"), "nonscoring", P) is True
        and admits(row, Decimal("5.01"), "nonscoring", P) is False
    )


def test_spent_nonscoring_reserve_no_longer_holds_back_scoring(budget_row) -> None:
    """Non-scoring spend draws the reserve down first: once $4.90 is spent, scoring may use the rest of A."""
    row = budget_row(counted=Decimal("97.90"), scoring=Decimal("93.00"), nonscoring=Decimal("4.90"))
    assert admits(row, Decimal("0.10"), "scoring", P) and not admits(row, Decimal("0.11"), "scoring", P)
    assert budget.headroom(row, "scoring", P) == budget.headroom(row, "nonscoring", P) == Decimal("0.10")


def test_months_are_utc_calendar_months() -> None:
    assert month_of(datetime(2026, 11, 1, 0, 30, tzinfo=UTC)) == date(2026, 11, 1)
    # 00:30 in Berlin on 1 November is still 31 October in UTC
    assert month_of(datetime.fromisoformat("2026-11-01T00:30:00+01:00")) == OCT
    assert budget.month_length_s(date(2028, 2, 1)) == 29 * 86400  # a leap year
    assert budget.month_length_s(date(2026, 12, 1)) == 31 * 86400
    assert budget.next_month(date(2026, 12, 1)) == date(2027, 1, 1)
    with pytest.raises(ValueError, match="aware"):
        month_of(datetime(2026, 10, 1))  # noqa: DTZ001 - the naive datetime is the case under test
    with pytest.raises(ValueError, match="first"):
        budget.month_length_s(date(2026, 10, 2))


def test_the_bucket_accrues_at_the_rate_up_to_its_capacity() -> None:
    rate, cap = rate_per_s(P, OCT), bucket_capacity(P, OCT)
    hour = T0 + timedelta(hours=1)
    assert budget.accrue(Decimal("-0.50"), T0, hour, P, OCT) == (Decimal("-0.50") + rate * 3600).quantize(
        budget.USD_QUANTUM, rounding="ROUND_FLOOR"
    )
    assert budget.accrue(Decimal("1.40"), T0, T0 + timedelta(hours=5), P, OCT) == cap.quantize(
        budget.USD_QUANTUM, rounding="ROUND_FLOOR"
    )
    assert budget.accrue(Decimal("0.20"), T0, T0, P, OCT) == Decimal("0.20")  # no time, no change
    assert budget.accrue(Decimal("0.20"), T0, T0 - timedelta(hours=1), P, OCT) == Decimal(
        "0.20"
    )  # never back
    assert budget.accrue(Decimal("9.00"), T0, T0, P, OCT) == cap.quantize(budget.USD_QUANTUM, "ROUND_FLOOR")


def test_a_new_month_starts_full_and_accrual_uses_the_rows_cap(budget_row) -> None:
    row = budget.new_row(OCT, P, T0)
    assert (row.cap_usd, row.overrun_buffer_usd, row.counted_usd, row.bucket_at) == (
        Decimal("100.00"),
        Decimal("2.00"),
        Decimal(0),
        T0,
    )
    assert row.bucket_usd == bucket_capacity(P, OCT).quantize(budget.USD_QUANTUM, "ROUND_FLOOR")
    # a save lowered the row's cap to $10: the row's own cap paces the bucket, not the params' $100
    low = budget_row(
        counted=Decimal(0),
        scoring=Decimal(0),
        nonscoring=Decimal(0),
        cap_usd=Decimal("10.00"),
        bucket_usd=Decimal(0),
    )
    budget.accrue_row(low, P, T0 + timedelta(hours=1))
    ten = BudgetParams(**{**P.__dict__, "cap_usd": Decimal("10.00")})
    assert low.bucket_usd == budget.accrue(Decimal(0), T0, T0 + timedelta(hours=1), ten, OCT)
    assert low.bucket_at == T0 + timedelta(hours=1)
    budget.accrue_row(low, P, T0)  # an earlier clock moves nothing back
    assert low.bucket_at == T0 + timedelta(hours=1)


def test_the_counter_table_of_6_9_1(budget_row) -> None:
    row = budget_row(
        counted=Decimal(0), scoring=Decimal(0), nonscoring=Decimal(0), bucket_usd=Decimal("1.00")
    )
    r, s = Decimal("0.040000"), Decimal("0.008000")
    budget.apply_count(row, r, "scoring", bucket=True)  # reservation of r
    assert (row.counted_usd, row.scoring_counted_usd, row.bucket_usd) == (r, r, Decimal("0.96"))
    budget.apply_count(
        row, budget.settlement_delta("billed", r, s), "scoring", bucket=True
    )  # billed at s ≤ r
    assert (row.counted_usd, row.scoring_counted_usd, row.bucket_usd) == (s, s, Decimal("0.992"))
    budget.apply_count(row, r, "nonscoring", bucket=False)  # non-scoring never touches the bucket
    budget.apply_count(row, budget.settlement_delta("not_billed", r, Decimal(0)), "nonscoring", bucket=False)
    assert (row.counted_usd, row.nonscoring_counted_usd, row.bucket_usd) == (s, Decimal(0), Decimal("0.992"))
    assert budget.settlement_delta("ambiguous", r, None) == 0  # keeps the maximum charge
    with pytest.raises(ValueError, match="settled"):
        budget.settlement_delta("billed", r, None)


def test_a_cap_lowered_below_counted_admits_nothing_until_counted_falls_below_it(budget_row) -> None:
    row = budget_row(counted=Decimal("40.00"), scoring=Decimal("30.00"), nonscoring=Decimal("10.00"))
    budget.change_cap(row, Decimal("10.00"), Decimal("2.00"), T0)
    assert (row.cap_lowered_at, row.counted_at_cap_change_usd) == (T0, Decimal("40.00"))
    assert budget.cap_holds(row)  # counted ≤ max(cap, counted_at_cap_change)
    assert not admits(row, Decimal("0.000001"), "nonscoring", P)
    assert budget.headroom(row, "nonscoring", P) == 0
    row.counted_usd, row.nonscoring_counted_usd = Decimal("37.00"), Decimal("7.00")  # in-flight calls settled
    assert budget.cap_holds(row) and not admits(row, Decimal("0.01"), "nonscoring", P)
    budget.change_cap(row, Decimal("100.00"), Decimal("2.00"), T0 + timedelta(hours=1))  # raised: at once
    assert row.cap_lowered_at == T0 and admits(row, Decimal("0.01"), "nonscoring", P)
    budget.change_cap(row, Decimal("50.00"), Decimal("2.00"), T0 + timedelta(hours=2))  # lowered, not below
    assert row.cap_lowered_at == T0 and row.counted_at_cap_change_usd == Decimal("40.00")
    row.counted_usd = Decimal("51.00")
    assert not budget.cap_holds(row)


def test_the_80_percent_share() -> None:
    row = budget.new_row(OCT, P, T0)
    row.counted_usd = row.nonscoring_counted_usd = Decimal("79.999999")
    assert not budget.cap_share_reached(row)
    row.counted_usd = row.nonscoring_counted_usd = Decimal("80.00")
    assert budget.cap_share_reached(row)
    row.cap_usd, row.counted_usd, row.nonscoring_counted_usd = Decimal(0), Decimal(0), Decimal(0)
    assert not budget.cap_share_reached(row)  # a zero cap spends nothing, so it never nears its end


def test_params_come_from_the_seeded_settings() -> None:
    text = resources.files("jobhunter.db").joinpath("seed_data/settings_v1.json").read_text(encoding="utf-8")
    assert BudgetParams.from_payload(json.loads(text)) == P
    with pytest.raises(TypeError):
        BudgetParams.from_payload({"llm": {"cap_usd": 100.5}, "schedule": {"interval_hours": 6}})


def test_errors_carry_codes_and_amounts_only() -> None:
    exhausted = BudgetExhausted("scoring", Decimal("0.10"))
    assert isinstance(exhausted, LLMError)
    assert (exhausted.purpose_class, exhausted.headroom_usd) == ("scoring", Decimal("0.10"))
    assert LLMRequestError(400).status == 400
    assert InvalidOutput("structural").code == "structural"
    assert Refused("cyber").category == "cyber"
    mismatch = ModelMismatch("claude-opus-5-5", "claude-sonnet-5-5")
    assert (mismatch.requested, mismatch.served) == ("claude-opus-5-5", "claude-sonnet-5-5")


def test_the_spend_series_have_their_17_5_names() -> None:
    from jobhunter.llm.spend_ledger import SettleFigures, record_settlement_metrics

    def ratio_count(purpose: str) -> float:
        sample = metrics.REGISTRY.get_sample_value(
            "jobhunter_llm_reservation_ratio_count", {"purpose": purpose}
        )
        return sample or 0.0

    before = ratio_count("critic")
    record_settlement_metrics(
        SettleFigures(
            settled_usd=Decimal("0.02"),
            reserved_usd=Decimal("0.04"),
            purpose="critic",
            spend_by_state={
                "counted": Decimal("1.5"),
                "billed": Decimal("1"),
                "ambiguous": Decimal("0.25"),
                "reserved": Decimal("0.25"),
            },
            bucket_usd=Decimal("-0.5"),
            headroom_usd=Decimal("91.6"),
        )
    )
    text = generate_latest(metrics.REGISTRY).decode()
    for state, value in (("counted", 1.5), ("billed", 1.0), ("ambiguous", 0.25), ("reserved", 0.25)):
        assert f'jobhunter_llm_spend_usd{{state="{state}"}} {value}' in text
    assert "jobhunter_llm_bucket_usd -0.5" in text and "jobhunter_llm_headroom_usd 91.6" in text
    assert ratio_count("critic") == before + 1
    assert metrics.REGISTRY.get_sample_value(
        "jobhunter_llm_reservation_ratio_bucket", {"purpose": "critic", "le": "0.5"}
    )
