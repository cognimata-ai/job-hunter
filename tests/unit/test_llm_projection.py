"""The LLM projection of the settings dry-run (spec §6.8, §6.9.2, §21; plan Task 16).

`central_cost_per_target` is the §21.1/§21.2 formula, the token assumptions times `llm.pricing`; it is
pinned against §21.2's per-target figures (B $0.0526, A $0.0793, C $0.0194, each within $0.0001) and, exactly,
against the arithmetic below. `targets_per_month` is §21.3's `floor(scoring share / (ĉ + cache writes per
cycle / min_cycle_targets))`, the cache writes per policy (Ruling R16-2), pinned against §21.3 and §21.5.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from jobhunter.settings.llm_projection import (
    DEFAULT_ARRIVAL_PER_MONTH,
    LlmProjection,
    cache_writes_per_cycle,
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


def test_the_preset_label_alone_changes_no_cost() -> None:
    assert central_cost_per_target(PRESET_B.model_copy(update={"preset": "custom"})) == Decimal("0.052646")


def test_the_weaker_generator_funnel_follows_the_generator_model_not_the_label() -> None:
    # C's nodes labelled `custom` keep C's funnel and uplift; a `C` label on B's models keeps B's figure
    assert central_cost_per_target(policy("custom", HAIKU, HAIKU, SONNET)) == Decimal("0.019352")
    assert central_cost_per_target(policy("C", SONNET, SONNET, OPUS)) == Decimal("0.052646")
    # the funnel is the generator's: a Haiku reviser under a Sonnet generator keeps the default funnel
    # (0.0223 + 0.45 x (0.35 x 0.0157 + 0.65 x 0.0207 + 0.455 x 0.0207) + 0.45 x 0.455 x 0.0016445 + 0.000223)
    assert central_cost_per_target(policy("custom", SONNET, HAIKU, SONNET)) == Decimal("0.035626")


@pytest.mark.parametrize(
    ("name", "model_policy", "per_cycle"),
    [
        # Sonnet/medium $0.01035 + Opus/medium $0.0216 + 0.8 x Opus/low $0.0216 (§21.1: $0.04923)
        ("B", PRESET_B, Decimal("0.04923")),
        # one Opus/medium prefix for every node, Opus/low in 80% of cycles
        ("A", PRESET_A, Decimal("0.03888")),
        # Haiku/medium 1.3 x 4,500 x ($0.125 - $0.01) + Sonnet/medium $0.01035 + 0.8 x Sonnet/low $0.01035
        ("C", PRESET_C, Decimal("0.019303")),
        # every critic round at medium: the light prefix is the critic's own, written once
        ("A all medium", policy("A", OPUS, OPUS, OPUS, light="medium"), Decimal("0.0216")),
    ],
)
def test_cache_writes_per_cycle(name: str, model_policy: ModelPolicy, per_cycle: Decimal) -> None:
    assert cache_writes_per_cycle(model_policy) == per_cycle


def projected(model_policy: ModelPolicy, *, min_cycle_targets: int = 10) -> LlmProjection:
    s = Settings.model_validate(
        {
            "scoring": {
                "model_policy": model_policy.model_dump(mode="json"),
                "min_cycle_targets": min_cycle_targets,
            }
        }
    )
    return llm_projection(
        s,
        now=OCTOBER,
        c_hat=central_cost_per_target(model_policy),
        projected_critic_rate=Decimal("0.450"),
        arrival_per_month=DEFAULT_ARRIVAL_PER_MONTH,
    )


@pytest.mark.parametrize(
    ("name", "model_policy", "min_cycle_targets", "targets", "about"),
    [
        ("B", PRESET_B, 10, 1617, 1620),  # 93.10 / (0.052646 + 0.004923)
        ("A", PRESET_A, 10, 1119, 1120),  # 93.10 / (0.079255 + 0.003888)
        ("C", PRESET_C, 10, 4374, 4370),  # 93.10 / (0.019352 + 0.0019303)
        ("B, 20-target cycles", PRESET_B, 20, 1689, 1690),  # 93.10 / (0.052646 + 0.0024615); §21.5: about +70
    ],
)
def test_targets_per_month_per_policy(
    name: str, model_policy: ModelPolicy, min_cycle_targets: int, targets: int, about: int
) -> None:
    p = projected(model_policy, min_cycle_targets=min_cycle_targets)
    assert p.targets_per_month == targets and round(p.targets_per_month, -1) == about


def test_twenty_target_cycles_cut_the_cache_writes() -> None:
    # §21.5: "min_cycle_targets 10 → 20 | cache writes ≈ $7.96 → $4.16 (about +70 targets per month)"
    per_cycle = cache_writes_per_cycle(PRESET_B)
    ten, twenty = projected(PRESET_B), projected(PRESET_B, min_cycle_targets=20)
    assert round(ten.targets_per_month * per_cycle / 10, 2) == Decimal("7.96")
    assert round(twenty.targets_per_month * per_cycle / 20, 2) == Decimal("4.16")
    assert twenty.targets_per_month - ten.targets_per_month == 72
    # 1,689 targets x ($0.052646 + $0.0024615)
    assert twenty.projected_month_spend == Decimal("93.076568")


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
