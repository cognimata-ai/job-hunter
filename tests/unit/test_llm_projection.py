"""The LLM projection of the settings dry-run (spec §6.8, §6.9.2, §21; plan Task 16).

`central_cost_per_target` is the §21.1/§21.2 formula, the token assumptions times `llm.pricing`; it is
pinned against §21.2's per-target figures (B $0.0526, A $0.0793, C $0.0194, each within $0.0001) and, exactly,
against the arithmetic below. `targets_per_month` is §21.3's `floor(scoring share / (ĉ + 0.04923 / 10))`.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from jobhunter.llm import bounds
from jobhunter.settings.llm_projection import (
    DEFAULT_ARRIVAL_PER_MONTH,
    LlmProjection,
    cap_binding,
    central_cost_per_target,
    llm_projection,
)
from jobhunter.settings.model import ModelPolicy, Settings

OCTOBER = datetime(2026, 10, 22, 9, tzinfo=UTC)  # 31 days
NOVEMBER = datetime(2026, 11, 10, 9, tzinfo=UTC)  # 30 days
TOLERANCE = Decimal("0.0001")


def policy(preset: str, generate: str, revise: str, critic: str, *, light: str = "low") -> ModelPolicy:
    def node(model: str) -> dict[str, str]:
        return {"model": model, "effort": "medium"}

    return ModelPolicy.model_validate(
        {
            "preset": preset,
            "nodes": {
                "generate": node(generate),
                "revise": node(revise),
                "critic": node(critic),
                "extract_profile": node("claude-opus-5-5"),
                "propose_queries": node("claude-opus-5-5"),
            },
            "critic_light_effort": light,
        }
    )


OPUS, SONNET, HAIKU = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"
PRESET_A = policy("A", OPUS, OPUS, OPUS)
PRESET_B = ModelPolicy()  # the default (§10.3): Sonnet generator and reviser, Opus critic, light effort low
PRESET_C = policy("C", HAIKU, HAIKU, SONNET)


def settings(**llm: Any) -> Settings:
    return Settings.model_validate({"llm": llm}) if llm else Settings()


def test_preset_b_is_the_central_figure_of_section_21_2() -> None:
    # generate 2,200 x $2 + 4,500 x $0.20 + 1,700 x $10 = $0.0223; critic 0.45 x (0.35 x $0.0305 + 0.65 x
    # $0.0405 + 0.455 x $0.0405) = $0.024942375; revise 0.45 x 0.455 x $0.0253 = $0.005180175; repairs 1% of
    # the generator, $0.000223: $0.05264555, rounded half-up to $0.000001
    assert PRESET_B.preset == "B" and PRESET_B.nodes.generate.model == SONNET
    cost = central_cost_per_target(PRESET_B)
    assert abs(cost - Decimal("0.0526")) <= TOLERANCE
    assert cost == Decimal("0.052646")


def test_preset_a_matches_section_21_2() -> None:
    # generator $0.0437 and reviser $0.0497 on Opus/medium, the critic as in B: $0.07925545
    cost = central_cost_per_target(PRESET_A)
    assert abs(cost - Decimal("0.0793")) <= TOLERANCE
    assert cost == Decimal("0.079255")
    # "With every critic round at medium (literally 'all Opus/medium') it is $0.0808"
    all_medium = central_cost_per_target(policy("A", OPUS, OPUS, OPUS, light="medium"))
    assert abs(all_medium - Decimal("0.0808")) <= TOLERANCE


def test_preset_c_matches_section_21_2() -> None:
    # Haiku generator and reviser with the 30% tokenizer uplift ($0.0014495, $0.0016445), the Sonnet critic
    # ($0.0207 medium, $0.0157 low) with the same 35% light share; §21.2's funnel for the weaker generator:
    # critic rate 0.55, q1 0.45, q2 0.35
    cost = central_cost_per_target(PRESET_C)
    assert abs(cost - Decimal("0.0194")) <= TOLERANCE
    assert cost == Decimal("0.019352")


def test_the_figures_do_not_depend_on_the_prompt_overhead(monkeypatch: pytest.MonkeyPatch) -> None:
    before = [central_cost_per_target(p) for p in (PRESET_A, PRESET_B, PRESET_C)]
    monkeypatch.setattr(
        bounds, "PROMPT_OVERHEAD_TOKENS", dict.fromkeys(bounds.PROMPT_OVERHEAD_TOKENS, 50_000)
    )
    assert [central_cost_per_target(p) for p in (PRESET_A, PRESET_B, PRESET_C)] == before


def test_the_preset_label_alone_changes_no_cost() -> None:
    assert central_cost_per_target(PRESET_B.model_copy(update={"preset": "custom"})) == Decimal("0.052646")


def test_targets_per_month_for_b_under_the_default_cap() -> None:
    p = llm_projection(
        settings(),
        now=OCTOBER,
        c_hat=central_cost_per_target(PRESET_B),
        projected_critic_rate=Decimal("0.450"),
        arrival_per_month=DEFAULT_ARRIVAL_PER_MONTH,
    )
    # 93.10 / (0.052646 + 0.004923) = 1617.19 (§21.3: about 1,620)
    assert p.targets_per_month == 1617 and round(p.targets_per_month, -1) == 1620
    assert p.arrival_per_month == 3000 and cap_binding(p)
    assert p.c_hat == Decimal("0.052646") and p.projected_critic_rate == Decimal("0.450")
    # the scoring spend of the 1,617 targets with their cache writes, under the $93.10 scoring share
    assert p.projected_month_spend == Decimal("93.089073")


def test_bucket_rate_and_capacity() -> None:
    def at(now: datetime, **schedule: Any) -> LlmProjection:
        s = Settings.model_validate({"schedule": schedule}) if schedule else Settings()
        return llm_projection(
            s, now=now, c_hat=Decimal("0.052646"), projected_critic_rate=Decimal("0.450"), arrival_per_month=0
        )

    # §6.9.2: rate = $93.10 over the month; B_max = max(2 x rate x 6 h, $1.00)
    october = at(OCTOBER)
    assert october.bucket_rate_per_h == Decimal("0.125134")  # 93.10 / 744 h
    assert october.bucket_capacity == Decimal("1.501613")
    november = at(NOVEMBER)
    assert november.bucket_rate_per_h == Decimal("0.129306")  # 93.10 / 720 h
    assert november.bucket_capacity == Decimal("1.551667")
    assert at(OCTOBER, interval_hours=1).bucket_capacity == Decimal("1.00")  # the floor applies at 1 h


def test_a_cap_at_or_below_the_buffer_admits_nothing_and_binds() -> None:
    """Ruling 4: with `cap_usd ≤ overrun_buffer_usd` the admissible budget `A` is not positive."""
    for cap in ("1.00", "2.00"):
        p = llm_projection(
            settings(cap_usd=cap, overrun_buffer_usd="2.00"),
            now=OCTOBER,
            c_hat=Decimal("0.052646"),
            projected_critic_rate=Decimal("0.450"),
            arrival_per_month=0,  # it binds even without any arrival
        )
        assert p.targets_per_month == 0 and p.projected_month_spend == 0
        assert p.bucket_rate_per_h == 0 and p.bucket_capacity == Decimal("1.00")
        assert cap_binding(p)


def test_a_cap_that_covers_the_arrivals_does_not_bind() -> None:
    # $200: A = $198, scoring share $188.10, 3,267 targets for 3,000 arrivals
    p = llm_projection(
        settings(cap_usd="200.00"),
        now=OCTOBER,
        c_hat=Decimal("0.052646"),
        projected_critic_rate=Decimal("0.450"),
        arrival_per_month=DEFAULT_ARRIVAL_PER_MONTH,
    )
    assert p.targets_per_month == 3267 and not cap_binding(p)
    assert p.projected_month_spend == Decimal("172.707000")  # 3,000 x $0.057569
