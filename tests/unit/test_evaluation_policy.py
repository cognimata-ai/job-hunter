"""The evaluation policy (spec §11.7.4 L4466-4477; M3 Task 6): the six routing fields of an
`evaluation_policies.payload`, validated by the settings model's `Evaluation` group (§13.1), and T4's literal
60 (L4408). Every datum is invented or the packaged seed.
"""

from decimal import Decimal
from typing import Any

import pytest
from jobhunter.db.seed import load_settings_v1
from jobhunter.scoring.evaluation_policy import T4_MIN_MID, EvaluationPolicy
from jobhunter.types import get_object
from pydantic import ValidationError


def seeded() -> dict[str, Any]:
    return dict(get_object(load_settings_v1(), "evaluation"))


def test_from_payload_of_the_seeded_evaluation_block() -> None:
    assert EvaluationPolicy.from_payload(seeded()) == EvaluationPolicy(
        relevance_threshold=60,
        t2_band=5,
        t2_straddle_max_coverage=Decimal("0.7"),
        t3_min_mid=80,
        t8_sample_pct=5,
        critic_light_triggers=frozenset({"T2", "T8"}),
    )


def test_t4_is_the_literal_60() -> None:
    assert T4_MIN_MID == 60


def test_a_partial_payload_takes_the_defaults() -> None:
    policy = EvaluationPolicy.from_payload({"t2_band": 0, "critic_light_triggers": ["T8", "T3"]})
    assert (policy.relevance_threshold, policy.t2_band, policy.critic_light_triggers) == (
        60,
        0,
        frozenset({"T3", "T8"}),
    )


@pytest.mark.parametrize(
    "override",
    [
        {"t8_sample_pct": 101},
        {"t2_band": 21},
        {"t3_min_mid": 49},
        {"t2_straddle_max_coverage": 0.7},  # a rate is a decimal string, never a float
        {"t2_straddle_max_coverage": "0.705"},
        {"critic_light_triggers": ["T2", "T2"]},
        {"critic_light_triggers": ["T0"]},
        {"critic_light_threshold": 1},
    ],
)
def test_from_payload_refuses_what_the_settings_model_refuses(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        EvaluationPolicy.from_payload({**seeded(), **override})
