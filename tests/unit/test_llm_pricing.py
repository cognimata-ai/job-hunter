"""Pricing of the LLM gateway (spec §10.7): the table, the Haiku tiers, the batch discount, the reservation
rounded up and the settlement rounded half-up, all in `Decimal`."""

from dataclasses import FrozenInstanceError
from decimal import Decimal
from typing import get_args

import pytest
from jobhunter.llm.pricing import (
    CONTEXT_WINDOW,
    MODELS,
    SUPPORTED_EFFORTS,
    Effort,
    Model,
    Price,
    Usage,
    bound_input_price,
    effort_of,
    price_for,
    settled_usd,
    worst_case_usd,
)

D = Decimal


def test_worst_case_uses_the_cache_write_price_and_rounds_up() -> None:
    assert worst_case_usd("claude-opus-5-5", input_tokens_bound=10_000, max_tokens=16_000) == D(
        "0.370000"
    )  # 10kx5.00 + 16kx20.00
    assert worst_case_usd("claude-sonnet-5-5", input_tokens_bound=1, max_tokens=1) == D(
        "0.000013"
    )  # 2.5e-6 + 1e-5 → up


def test_settlement_rounds_half_up_and_charges_each_token_class() -> None:
    u = Usage(
        input_tokens=1000, output_tokens=500, cache_creation_input_tokens=100, cache_read_input_tokens=2000
    )
    assert settled_usd("claude-opus-5-5", u) == D("0.014900")  # 0.004 + 0.010 + 0.0005 + 0.0004


def test_haiku_tiers_follow_the_bound_and_the_actual_prompt() -> None:
    assert price_for("claude-haiku-5-5", prompt_tokens=100_000).input == D("0.10")
    assert price_for("claude-haiku-5-5", prompt_tokens=100_001).input == D("0.50")
    assert price_for("claude-opus-5-5", prompt_tokens=1, batch=True).output == D("10.00")


def test_the_price_table_is_the_spec_table_verbatim() -> None:
    # (input, output, cache write, cache read) in USD per MTok, §10.7
    table: dict[Model, tuple[str, str, str, str]] = {
        "claude-opus-5-5": ("4.00", "20.00", "5.00", "0.20"),
        "claude-sonnet-5-5": ("2.00", "10.00", "2.50", "0.20"),
        "claude-fable-5-1": ("10.00", "50.00", "12.50", "0.25"),
    }
    for model, figures in table.items():
        want = Price(*(D(f) for f in figures))
        assert price_for(model, prompt_tokens=1) == want
        assert price_for(model, prompt_tokens=5_000_000) == want  # one tier only
    short = price_for("claude-haiku-5-5", prompt_tokens=100_000)
    assert short == Price(D("0.10"), D("0.50"), D("0.125"), D("0.01"))  # 1.25x and 0.1x input
    long = price_for("claude-haiku-5-5", prompt_tokens=100_001)
    assert long == Price(D("0.50"), D("2.50"), D("0.625"), D("0.05"))


def test_batch_halves_every_price_of_every_tier() -> None:
    for model in MODELS:
        for prompt_tokens in (1, 100_000, 100_001):
            full = price_for(model, prompt_tokens=prompt_tokens)
            half = price_for(model, prompt_tokens=prompt_tokens, batch=True)
            assert half == Price(full.input / 2, full.output / 2, full.cache_write / 2, full.cache_read / 2)


def test_prices_are_decimal_and_never_float() -> None:
    for model in MODELS:
        price = price_for(model, prompt_tokens=1)
        assert all(
            isinstance(p, Decimal) for p in (price.input, price.output, price.cache_write, price.cache_read)
        )


def test_worst_case_prices_input_at_the_larger_of_input_and_cache_write() -> None:
    # 1,000,000 tokens makes the figure the price per MTok itself; max_tokens is 0
    expected: dict[Model, Decimal] = {
        "claude-opus-5-5": D("5.000000"),
        "claude-sonnet-5-5": D("2.500000"),
        "claude-fable-5-1": D("12.500000"),
        "claude-haiku-5-5": D("0.625000"),  # the > 100K tier: 1.25 x 0.50 beats 0.50
    }
    for model, usd in expected.items():
        assert worst_case_usd(model, input_tokens_bound=1_000_000, max_tokens=0) == usd
    assert worst_case_usd("claude-haiku-5-5", input_tokens_bound=100_000, max_tokens=0) == D("0.012500")


def test_worst_case_picks_the_haiku_tier_by_the_bound() -> None:
    at = worst_case_usd("claude-haiku-5-5", input_tokens_bound=100_000, max_tokens=1_000)
    over = worst_case_usd("claude-haiku-5-5", input_tokens_bound=100_001, max_tokens=1_000)
    assert at == D("0.013000")  # 100k x 0.125 + 1k x 0.50
    assert over == D("0.065001")  # 100,001 x 0.625 + 1k x 2.50 = 65,000.625 micro-dollars, up


def test_worst_case_batch_halves_the_reservation() -> None:
    assert worst_case_usd("claude-opus-5-5", input_tokens_bound=10_000, max_tokens=16_000, batch=True) == D(
        "0.185000"
    )


def test_worst_case_adds_the_extra_before_it_rounds_up() -> None:
    plain = worst_case_usd("claude-sonnet-5-5", input_tokens_bound=1, max_tokens=1)
    assert plain == D("0.000013")
    # 0.0000125 + 0.1250004 = 0.1250129 → up to 0.125013; rounding each part up first would give 0.125014
    extra = D("0.1250004")
    assert worst_case_usd("claude-sonnet-5-5", input_tokens_bound=1, max_tokens=1, extra_usd=extra) == D(
        "0.125013"
    )


def test_worst_case_rounds_up_even_below_half_a_micro_dollar() -> None:
    # one batch Haiku token reserves 0.0625 micro-dollars (the cache-write price beats the input price)
    assert worst_case_usd("claude-haiku-5-5", input_tokens_bound=1, max_tokens=0, batch=True) == D("0.000001")
    assert worst_case_usd("claude-haiku-5-5", input_tokens_bound=0, max_tokens=1, batch=True) == D("0.000001")


def test_worst_case_never_rounds_an_exact_figure_up() -> None:
    assert worst_case_usd("claude-opus-5-5", input_tokens_bound=200_000, max_tokens=0) == D("1.000000")
    assert worst_case_usd("claude-opus-5-5", input_tokens_bound=200_001, max_tokens=0) == D("1.000005")


def test_worst_case_is_six_places() -> None:
    assert str(worst_case_usd("claude-opus-5-5", input_tokens_bound=10_000, max_tokens=16_000)) == "0.370000"
    assert str(worst_case_usd("claude-opus-5-5", input_tokens_bound=0, max_tokens=0)) == "0.000000"


@pytest.mark.parametrize(
    ("bound", "max_tokens", "extra"),
    [(-1, 1, D(0)), (1, -1, D(0)), (1, 1, D("-0.01"))],
)
def test_worst_case_refuses_a_negative_figure(bound: int, max_tokens: int, extra: Decimal) -> None:
    with pytest.raises(ValueError, match="negative"):
        worst_case_usd("claude-opus-5-5", input_tokens_bound=bound, max_tokens=max_tokens, extra_usd=extra)


def test_settlement_rounds_half_up_not_half_even() -> None:
    # one Sonnet cache-write token costs 2.5 micro-dollars: half-up gives 3, half-even would give 2
    assert settled_usd("claude-sonnet-5-5", Usage(0, 0, 1, 0)) == D("0.000003")
    # one batch Haiku cache-write token costs 0.0625 micro-dollars; eight cost exactly 0.5 → 1
    assert settled_usd("claude-haiku-5-5", Usage(0, 0, 8, 0), batch=True) == D("0.000001")
    assert settled_usd("claude-haiku-5-5", Usage(0, 0, 7, 0), batch=True) == D("0.000000")  # 0.4375 → down


def test_settlement_charges_each_token_class_at_its_own_price() -> None:
    each = {
        "input": settled_usd("claude-opus-5-5", Usage(1_000_000, 0, 0, 0)),
        "output": settled_usd("claude-opus-5-5", Usage(0, 1_000_000, 0, 0)),
        "cache_write": settled_usd("claude-opus-5-5", Usage(0, 0, 1_000_000, 0)),
        "cache_read": settled_usd("claude-opus-5-5", Usage(0, 0, 0, 1_000_000)),
    }
    assert each == {
        "input": D("4.000000"),
        "output": D("20.000000"),
        "cache_write": D("5.000000"),
        "cache_read": D("0.200000"),
    }


def test_settlement_tier_follows_the_whole_actual_prompt() -> None:
    # 40,000 + 30,000 + 30,001 = 100,001 prompt tokens: the > 100K tier, though no class is above 100K
    over = settled_usd("claude-haiku-5-5", Usage(40_000, 0, 30_000, 30_001))
    assert over == D("0.020000") + D("0.018750") + D("0.001500")  # 0.50, 0.625 and 0.05 per MTok
    under = settled_usd("claude-haiku-5-5", Usage(40_000, 0, 30_000, 30_000))
    assert under == D("0.004000") + D("0.003750") + D("0.000300")  # 0.10, 0.125 and 0.01 per MTok


def test_settlement_of_nothing_is_zero_at_six_places() -> None:
    assert str(settled_usd("claude-fable-5-1", Usage(0, 0, 0, 0))) == "0.000000"


def test_bound_input_price_is_the_larger_of_input_and_cache_write_of_the_top_tier() -> None:
    assert {m: bound_input_price(m, batch=False) for m in MODELS} == {
        "claude-opus-5-5": D("5.00"),
        "claude-sonnet-5-5": D("2.50"),
        "claude-haiku-5-5": D("0.625"),
        "claude-fable-5-1": D("12.50"),
    }
    assert {m: bound_input_price(m, batch=True) for m in MODELS} == {
        "claude-opus-5-5": D("2.50"),
        "claude-sonnet-5-5": D("1.25"),
        "claude-haiku-5-5": D("0.3125"),
        "claude-fable-5-1": D("6.25"),
    }


def test_the_constants_cover_exactly_the_four_models() -> None:
    assert (
        MODELS
        == get_args(Model)
        == (
            "claude-opus-5-5",
            "claude-sonnet-5-5",
            "claude-haiku-5-5",
            "claude-fable-5-1",
        )
    )
    assert set(CONTEXT_WINDOW) == set(SUPPORTED_EFFORTS) == set(MODELS)


def test_every_model_supports_all_five_efforts() -> None:
    assert get_args(Effort) == ("low", "medium", "high", "xhigh", "max")
    for model in MODELS:
        assert SUPPORTED_EFFORTS[model] == frozenset(get_args(Effort))


def test_the_context_window_is_one_million_tokens_per_model() -> None:
    assert dict(CONTEXT_WINDOW) == dict.fromkeys(MODELS, 1_000_000)


def test_the_value_objects_are_frozen() -> None:
    price = price_for("claude-opus-5-5", prompt_tokens=1)
    with pytest.raises(FrozenInstanceError):
        price.input = D(0)  # type: ignore[misc]
    usage = Usage(1, 2, 3, 4)
    with pytest.raises(FrozenInstanceError):
        usage.input_tokens = 0  # type: ignore[misc]


@pytest.mark.parametrize("effort", get_args(Effort))
def test_effort_of_reads_every_effort(effort: Effort) -> None:
    assert effort_of(effort) == effort


def test_effort_of_refuses_an_unknown_effort() -> None:
    with pytest.raises(ValueError, match="unknown effort"):
        effort_of("extreme")
