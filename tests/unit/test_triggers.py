"""The critic triggers T0-T8 and the critic's effort (spec §11.7.1 L4402-4412, §11.7.4 L4470-4477; M3 Task 6).

Each trigger fires alone with the U14 defaults; T2's straddle fires only below the coverage 0.7; repairs
never trigger T1; T8 samples by the first 8 hex digits of the key's SHA-256. The critic runs at the light
effort only when a round's triggers are a non-empty subset of `critic_light_triggers`, and the settings model
refuses a light set that names a mandatory trigger, so no policy relaxes one. The policies seed
`t8_sample_pct = 0` unless a test exercises T8 (Controller Ruling P48), so no trigger depends on an id.
Every datum is invented.
"""

import hashlib
from dataclasses import replace
from decimal import Decimal
from typing import Any, Final

import pytest
from jobhunter.scoring.aggregate import Aggregation
from jobhunter.scoring.evaluation_policy import EvaluationPolicy
from jobhunter.scoring.facts import NOT_A_JOB_AD, FactRef, fact_ref
from jobhunter.scoring.triggers import (
    MANDATORY,
    Trigger,
    critic_effort,
    evaluate,
    golden_sample_key,
    sample_key,
    sampled,
)
from jobhunter.scoring.validator import (
    DraftValidation,
    Failure,
    Issue,
    ParsedGenerator,
    Repair,
    parse_generator,
    validate_draft,
)
from pydantic import ValidationError

from tests.support.scoring import evaluation_policy
from tests.support.validation import ctx_for, synthetic_ad, synthetic_wire_c

FRENCH: Final[FactRef] = fact_ref("mandatory_language", "fr")
NO_SAMPLE: Final = "1:1"  # bucket 52 (0xd6b5915c = 3602223452): sampled below 53 only
SAMPLED: Final = "2:1"  # bucket 3 (0x70a37d8f = 1889762703): sampled from 4 up, so at the default 5


def clean_validation() -> DraftValidation:
    parsed = parse_generator(synthetic_wire_c(), family="generator_v2_items")
    assert isinstance(parsed, ParsedGenerator)
    validation = validate_draft(parsed.draft, ctx_for(synthetic_ad()), normalisation=parsed.normalisation)
    assert not validation.t1 and not validation.injection_suspected and not validation.draft.not_a_job_ad
    return validation


CLEAN: Final = clean_validation()


def provisional(low: str, mid: str, high: str, coverage: str = "0.9") -> Aggregation:
    return Aggregation(
        low=Decimal(low),
        mid=Decimal(mid),
        high=Decimal(high),
        coverage=Decimal(coverage),
        confidence="high",
        caps_applied=[],
        det_criteria={},
        knockouts=[],
        industry_match="neutral",
    )


QUIET: Final = provisional("30", "40", "50")  # far from τ = 60, below 80, no straddle


def triggers(**overrides: Any) -> list[Trigger]:
    arguments: dict[str, Any] = {
        "validation": CLEAN,
        "provisional": QUIET,
        "policy": evaluation_policy(),
        "completeness": "full",
        "critic_rounds": 0,
        "required": [],
        "sample_key": SAMPLED,
        **overrides,
    }
    return evaluate(**arguments)


def test_a_clean_draft_far_from_the_threshold_triggers_nothing() -> None:
    assert triggers() == []


def test_t0_a_previous_critic_round() -> None:
    assert triggers(critic_rounds=1) == ["T0"]
    assert triggers(critic_rounds=2) == ["T0"]


def test_t1_a_limit_issue_or_a_deterministic_failure() -> None:
    limit = replace(CLEAN, issues=[Issue("too_many_words", "summary_en", {"max_words": 60})])
    assert triggers(validation=limit) == ["T1"]
    failure = replace(CLEAN, failures=[Failure("skills", "ad_evidence_missing")])
    assert triggers(validation=failure) == ["T1"]
    consistency = replace(CLEAN, failures=[Failure("languages", "languages_vs_unmet")])
    assert triggers(validation=consistency) == ["T1"]


def test_t1_never_for_a_repair_alone() -> None:
    repaired = replace(
        CLEAN,
        repairs=[
            Repair("job_facts.work_mode.value", "work_mode_structured", "remote"),
            Repair("criteria.experience_seniority.level", "seniority_recomputed", 2),
        ],
    )
    assert triggers(validation=repaired) == []


def test_t2_the_midpoint_within_the_band() -> None:
    assert triggers(provisional=provisional("50", "57", "64")) == ["T2"]
    assert triggers(provisional=provisional("60", "65", "70")) == ["T2"]  # |65 - 60| = 5 is within
    assert triggers(provisional=provisional("55", "55", "55")) == ["T2"]
    assert triggers(provisional=provisional("60.02", "65.01", "70")) == []
    assert triggers(provisional=provisional("50", "54.99", "59.98")) == []


def test_t2_a_straddle_only_below_the_coverage_bound() -> None:
    straddle = ("40", "66", "92")  # L < 60 <= U with M 6 above τ
    assert triggers(provisional=provisional(*straddle, coverage="0.69")) == ["T2"]
    assert triggers(provisional=provisional(*straddle, coverage="0.7")) == []
    assert triggers(provisional=provisional("46", "53", "60", coverage="0.5")) == [
        "T2"
    ]  # τ = U still straddles
    assert triggers(provisional=provisional("60", "70", "80", coverage="0.5")) == []  # L = τ does not


def test_t3_a_high_midpoint() -> None:
    assert triggers(provisional=provisional("75", "80", "85")) == ["T3"]
    assert triggers(provisional=provisional("90", "95", "100")) == ["T3"]
    assert triggers(provisional=provisional("75", "79.99", "84.98")) == []


def test_t4_a_partial_description_from_60() -> None:
    assert triggers(provisional=provisional("66", "70", "74"), completeness="partial") == ["T4"]
    assert triggers(provisional=provisional("66", "70", "74")) == []
    assert triggers(provisional=provisional("30", "40", "50"), completeness="partial") == []


def test_t4_uses_the_literal_60_not_the_relevance_threshold() -> None:
    policy = evaluation_policy(relevance_threshold=90, t2_band=0, t3_min_mid=100)
    partial = {"completeness": "partial", "policy": policy}
    assert triggers(provisional=provisional("60", "60", "60", coverage="1"), **partial) == ["T4"]
    assert triggers(provisional=provisional("59", "59.99", "60.98", coverage="1"), **partial) == []


def test_t5_a_binding_capable_fact() -> None:
    assert triggers(required=[FRENCH]) == ["T5"]
    assert triggers(required=[fact_ref("onsite_required", "onsite")]) == ["T5"]


def test_t6_an_injection_suspicion() -> None:
    assert triggers(validation=replace(CLEAN, injection_suspected=True)) == ["T6"]


def test_t7_not_a_job_ad_without_t5() -> None:
    flagged = replace(CLEAN, draft=CLEAN.draft.model_copy(update={"not_a_job_ad": True}))
    assert triggers(validation=flagged, required=[NOT_A_JOB_AD]) == ["T7"]


def test_t8_a_reproducible_sample() -> None:
    sampling = evaluation_policy(t8_sample_pct=5)
    assert triggers(policy=sampling, sample_key=SAMPLED) == ["T8"]
    assert triggers(policy=sampling, sample_key=NO_SAMPLE) == []
    assert triggers(sample_key=SAMPLED) == []  # the factory's policy samples nothing


def test_every_trigger_at_once_in_code_order() -> None:
    flagged = replace(
        CLEAN,
        draft=CLEAN.draft.model_copy(update={"not_a_job_ad": True}),
        failures=[Failure("skills", "ad_evidence_missing")],
        injection_suspected=True,
    )
    found = triggers(
        validation=flagged,
        provisional=provisional("40", "80", "100", coverage="0.5"),
        policy=evaluation_policy(relevance_threshold=80, t8_sample_pct=5),
        completeness="partial",
        critic_rounds=1,
        required=[FRENCH, NOT_A_JOB_AD],
        sample_key=SAMPLED,
    )
    assert found == ["T0", "T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8"]


def test_a_provisional_without_a_score_triggers_no_score_trigger() -> None:
    unscored = replace(QUIET, low=None, mid=None, high=None, confidence=None)
    policy = evaluation_policy(relevance_threshold=0, t3_min_mid=50)
    assert triggers(provisional=unscored, policy=policy, completeness="partial") == []


def test_the_policy_thresholds_drive_t2_and_t3() -> None:
    policy = evaluation_policy(relevance_threshold=70, t2_band=0, t3_min_mid=90)
    assert triggers(provisional=provisional("60", "70", "80"), policy=policy) == ["T2"]
    assert triggers(provisional=provisional("60", "69.99", "79.98"), policy=policy) == []
    assert triggers(provisional=provisional("85", "90", "95"), policy=policy) == ["T3"]
    assert triggers(provisional=provisional("60", "80", "100"), policy=policy) == []


# -- sampling ------------------------------------------------------------------------------------------------


def test_sampled_is_the_hand_computed_bucket() -> None:
    assert hashlib.sha256(b"1:1").hexdigest()[:8] == "d6b5915c"
    assert 0xD6B5915C == 3602223452 and 3602223452 % 100 == 52
    assert sampled("1:1", 5) is False
    assert (sampled("1:1", 52), sampled("1:1", 53)) == (False, True)
    assert (sampled("2:1", 3), sampled("2:1", 4), sampled("2:1", 5)) == (False, True, True)
    assert sampled("2:1", 0) is False and sampled("1:1", 100) is True


def test_the_sample_keys() -> None:
    assert sample_key(target_id=1, llm_tuple_id=1) == "1:1"
    assert golden_sample_key(golden_case_id=7, tuple_identity="abc") == "golden:7:abc"


# -- the critic's effort -------------------------------------------------------------------------------------


def effort(*codes: Trigger, policy: EvaluationPolicy | None = None) -> str:
    chosen = evaluation_policy() if policy is None else policy
    return critic_effort(list(codes), chosen, normal="medium", light="low")


def test_the_light_effort_for_a_subset_of_the_light_triggers() -> None:
    assert [effort("T2"), effort("T8"), effort("T2", "T8")] == ["low", "low", "low"]
    assert [effort("T2", "T0"), effort("T1"), effort("T3")] == ["medium", "medium", "medium"]


def test_no_trigger_is_the_normal_effort() -> None:
    assert effort() == "medium"


def test_the_light_set_is_the_policys() -> None:
    wide = evaluation_policy(critic_light_triggers=["T2", "T3", "T8"])
    assert effort("T3", policy=wide) == "low"
    assert effort("T3", "T5", policy=wide) == "medium"
    narrow = evaluation_policy(critic_light_triggers=[])
    assert effort("T2", policy=narrow) == "medium"


def test_the_mandatory_triggers() -> None:
    assert frozenset({"T0", "T1", "T4", "T5", "T6", "T7"}) == MANDATORY


@pytest.mark.parametrize("code", sorted(MANDATORY))
def test_no_policy_makes_a_mandatory_trigger_light(code: Trigger) -> None:
    with pytest.raises(ValidationError):
        evaluation_policy(critic_light_triggers=["T2", code])
    # every round with a mandatory trigger runs at the normal effort, whatever else it holds
    assert effort("T2", "T8", code, policy=evaluation_policy()) == "medium"
