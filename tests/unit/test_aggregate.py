"""Aggregation (spec §11.8, L4512-4537; M3 Task 6): the interval and its coverage, the caps and penalties in
their order (soft once per code, then hard, then the midpoint), the industry preference match, the confidence
table, and `aggregate`, which applies only binding knockouts and nulls everything for a verified
not-a-job-ad. Every datum is invented.
"""

from dataclasses import replace
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

import pytest
from jobhunter.scoring.aggregate import (
    HARD_CAP,
    SOFT_PENALTY,
    Aggregation,
    Cap,
    Interval,
    aggregate,
    caps,
    confidence,
    industry_match,
    interval,
)
from jobhunter.scoring.det_criteria import DetCriterion
from jobhunter.scoring.knockouts import Comparison, Confirmation, KnockoutCandidate, RequirementSource
from jobhunter.scoring.rubric import LLM_CRITERIA, load_rubric

from tests.support.facts import facts, scope
from tests.support.scoring import rating

WEIGHTS_V2: Final = load_rubric("v2").weights


def det(level: int | None) -> DetCriterion:
    return DetCriterion(level, "unknown" if level is None else "observed", {"et": level, "sal": 4})


def candidate(
    code: str,
    subject: str,
    *,
    comparison: Comparison = "unmet",
    source: RequirementSource = "text",
    severity: str = "soft",
) -> KnockoutCandidate:
    return KnockoutCandidate(
        code=code,
        fact_id=f"{code}:{subject}",
        subject=subject,
        requirement_source=source,
        comparison=comparison,
        binding_capable=source == "text" and comparison != "met",
        severity="hard" if severity == "hard" else "soft",
        inputs={},
    )


# role_fit 20, skills 25, experience 15 x 3/4, domain 10 x 2/4, languages 10, education unknown (5); the
# location 10, employment unknown (5): L0 = 81.25, U0 = 91.25, coverage 0.9.
LEVELS: Final = {
    "role_fit": 4,
    "skills": 4,
    "experience_seniority": 3,
    "domain": 2,
    "languages": 4,
    "education_certifications": None,
}


def run(**overrides: Any) -> Aggregation:
    arguments: dict[str, Any] = {
        "criteria": {key: rating(level) for key, level in LEVELS.items()},
        "det_location": det(4),
        "det_employment": det(None),
        "weights": WEIGHTS_V2,
        "knockouts": [],
        "confirmations": {},
        "industry": "neutral",
        "completeness": "full",
        "quality": "ok",
        "contested": False,
        "injection_suspected": False,
        "critic_injection": None,
        "not_a_job_ad_verified": False,
        **overrides,
    }
    return aggregate(**arguments)


# -- the interval and the caps -------------------------------------------------------------------------------


def test_codex_30_example_and_the_cap_order() -> None:
    capped = caps(
        Interval(Decimal(20), Decimal(80), Decimal("0.6")), soft_codes=(), hard_codes=("mandatory_language",)
    )
    assert (capped.low, capped.mid, capped.high) == (Decimal(20), Decimal("22.50"), Decimal(25))
    capped = caps(
        Interval(Decimal(30), Decimal(90), Decimal("0.6")),
        soft_codes=("degree", "certification", "mandatory_language", "mandatory_language"),
        hard_codes=("onsite_required",),
    )
    # three soft codes once each (-30), then the cap
    assert (capped.low, capped.high) == (Decimal(0), Decimal(25))


def test_interval_and_coverage() -> None:
    levels = {
        "role_fit": 4,
        "skills": 2,
        "experience_seniority": None,
        "domain": 3,
        "languages": 4,
        "education_certifications": None,
        "location_work_mode": 4,
        "employment_salary": None,
    }
    i = interval(levels, WEIGHTS_V2)
    # 20+12.5+7.5+10+10; +15+5+5
    assert (i.low, i.high, i.coverage) == (Decimal("60.0"), Decimal("85.0"), Decimal("0.75"))


def test_the_constants() -> None:
    assert (HARD_CAP, SOFT_PENALTY) == (25, 10)


def test_an_interval_of_no_known_criterion_spans_everything() -> None:
    i = interval(dict.fromkeys(WEIGHTS_V2), WEIGHTS_V2)
    assert (i.low, i.high, i.coverage) == (Decimal(0), Decimal(100), Decimal(0))


def test_an_interval_of_every_criterion_at_4_is_100() -> None:
    i = interval(dict.fromkeys(WEIGHTS_V2, 4), WEIGHTS_V2)
    assert (i.low, i.high, i.coverage) == (Decimal(100), Decimal(100), Decimal(1))


@pytest.mark.parametrize(
    ("levels", "message"),
    [
        ({key: 4 for key in list(WEIGHTS_V2)[:-1]}, "the criteria"),
        ({**dict.fromkeys(WEIGHTS_V2, 4), "role_fit": 5}, "level"),
        ({**dict.fromkeys(WEIGHTS_V2, 4), "role_fit": True}, "level"),
    ],
)
def test_an_interval_refuses_other_criteria_or_levels(levels: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        interval(levels, WEIGHTS_V2)


def test_soft_penalties_floor_at_zero_and_hard_caps_follow() -> None:
    capped = caps(Interval(Decimal(5), Decimal(40), Decimal("0.5")), soft_codes=("degree",), hard_codes=())
    assert (capped.low, capped.mid, capped.high) == (Decimal(0), Decimal(15), Decimal(30))
    # soft first: 30..90 becomes 20..80, then the cap 20..25; the other order would give 15..15
    capped = caps(
        Interval(Decimal(30), Decimal(90), Decimal("0.6")),
        soft_codes=("degree",),
        hard_codes=("work_permit",),
    )
    assert (capped.low, capped.mid, capped.high) == (Decimal(20), Decimal("22.50"), Decimal(25))
    assert capped.applied == [Cap("degree", "soft"), Cap("work_permit", "hard")]


def test_caps_record_each_code_once_in_their_order() -> None:
    capped = caps(
        Interval(Decimal(50), Decimal(70), Decimal(1)),
        soft_codes=("industry", "degree", "industry"),
        hard_codes=("onsite_required", "work_permit", "onsite_required"),
    )
    assert capped.applied == [
        Cap("industry", "soft"),
        Cap("degree", "soft"),
        Cap("onsite_required", "hard"),
        Cap("work_permit", "hard"),
    ]
    assert (capped.low, capped.high) == (Decimal(25), Decimal(25))


def test_the_midpoint_rounds_half_up_to_two_places() -> None:
    capped = caps(Interval(Decimal(0), Decimal("0.25"), Decimal(0)), soft_codes=(), hard_codes=())
    assert capped.mid == Decimal("0.13")  # 0.125: PostgreSQL's round(numeric, 2) gives 0.13 too
    capped = caps(Interval(Decimal("61.25"), Decimal("71.50"), Decimal(1)), soft_codes=(), hard_codes=())
    assert capped.mid == Decimal("66.38")  # 66.375


def test_the_midpoint_is_of_the_rounded_endpoints() -> None:
    # an interval of three places: L and U round half up to their numeric(5,2) values first, and M is the
    # rounded midpoint of those (15.005 -> 15.01); of the unrounded ones it would be 15.0045 -> 15.00
    capped = caps(Interval(Decimal("10.005"), Decimal("20.004"), Decimal(1)), soft_codes=(), hard_codes=())
    assert (capped.low, capped.mid, capped.high) == (Decimal("10.01"), Decimal("15.01"), Decimal("20.00"))
    assert capped.mid == ((capped.low + capped.high) / 2).quantize(Decimal("0.01"), ROUND_HALF_UP)


def test_no_cap_keeps_the_interval() -> None:
    capped = caps(Interval(Decimal("60.5"), Decimal(85), Decimal("0.75")), soft_codes=(), hard_codes=())
    assert (capped.low, capped.mid, capped.high, capped.applied) == (
        Decimal("60.5"),
        Decimal("72.75"),
        Decimal(85),
        [],
    )


# -- the industry preference ---------------------------------------------------------------------------------


def test_industry_match_without_a_fact_is_unknown() -> None:
    where = scope(industries_include=frozenset({"logistik"}))
    assert industry_match(None, facts(), where) == "unknown"
    assert industry_match("   ", facts(), where) == "unknown"


def test_industry_match_compares_the_folded_fixed_point() -> None:
    where = scope(industries_exclude=frozenset({"rustungsindustrie"}))
    assert industry_match("Rüstungsindustrie", facts(), where) == "excluded"
    assert industry_match("RÜSTUNGSINDUSTRIE", facts(), where) == "excluded"
    assert industry_match("Logistik", facts(), where) == "neutral"  # an empty include list prefers nothing


def test_an_excluded_industry_takes_precedence_over_the_include_list() -> None:
    where = scope(
        industries_include=frozenset({"logistik"}), industries_exclude=frozenset({"rustungsindustrie"})
    )
    assert industry_match("Rüstungsindustrie", facts(), where) == "excluded"
    assert industry_match("Gesundheitswesen", facts(), where) == "outside_include"
    assert industry_match("Logistik", facts(), where) == "neutral"


def test_a_structured_industry_is_the_filters_and_never_penalised() -> None:
    where = scope(
        industries_include=frozenset({"logistik"}), industries_exclude=frozenset({"rustungsindustrie"})
    )
    structured = facts(industry_structured="Rüstungsindustrie")
    assert industry_match("Rüstungsindustrie", structured, where) == "neutral"
    assert industry_match(None, structured, where) == "neutral"


# -- confidence (L4534-4537) ---------------------------------------------------------------------------------


def conf(coverage: str, **overrides: Any) -> str:
    arguments: dict[str, Any] = {
        "coverage": Decimal(coverage),
        "completeness": "full",
        "quality": "ok",
        "contested": False,
        "injection_suspected": False,
        "critic_injection": None,
        **overrides,
    }
    return confidence(**arguments)


def test_the_base_confidence_follows_the_coverage() -> None:
    assert [conf(x) for x in ("1", "0.8", "0.79", "0.6", "0.59", "0")] == [
        "high",
        "high",
        "medium",
        "medium",
        "low",
        "low",
    ]


def test_a_partial_or_garbage_description_is_low() -> None:
    assert conf("1", completeness="partial") == "low"
    assert conf("1", quality="garbage") == "low"
    assert conf("1", quality=None) == "high"


def test_a_contested_result_is_downgraded_once() -> None:
    assert [conf(x, contested=True) for x in ("0.9", "0.7", "0.1")] == ["medium", "low", "low"]
    assert conf("0.9", contested=True, completeness="partial") == "low"


@pytest.mark.parametrize(
    ("critic_injection", "expected"),
    [
        ("none", "high"),
        ("present_no_influence", "low"),
        ("present_influenced", "low"),
        ("unavailable", "low"),
    ],
)
def test_an_injection_suspicion_is_low_unless_the_critic_found_none(
    critic_injection: str, expected: str
) -> None:
    assert conf("0.9", injection_suspected=True, critic_injection=critic_injection) == expected
    assert conf("0.9", injection_suspected=False, critic_injection=critic_injection) == "high"


def test_an_injection_suspicion_without_a_critic_assessment_is_low() -> None:
    assert conf("0.9", injection_suspected=True, critic_injection=None) == "low"


# -- aggregate -----------------------------------------------------------------------------------------------


def test_aggregate_without_a_knockout_is_the_plain_interval() -> None:
    result = run()
    assert (result.low, result.mid, result.high, result.coverage) == (
        Decimal("81.25"),
        Decimal("86.25"),
        Decimal("91.25"),
        Decimal("0.9"),
    )
    assert (result.confidence, result.caps_applied, result.knockouts, result.industry_match) == (
        "high",
        [],
        [],
        "neutral",
    )
    assert result.det_criteria == {
        "location_work_mode": {"level": 4, "basis": "observed", "inputs": {"et": 4, "sal": 4}},
        "employment_salary": {"level": None, "basis": "unknown", "inputs": {"et": None, "sal": 4}},
    }


def test_aggregate_applies_only_binding_knockouts() -> None:
    knockouts = [
        candidate("degree", "degree"),  # confirmed, unmet: -10
        candidate("certification", "aws"),  # uncertain: a badge, no penalty
        candidate("mandatory_language", "fr", comparison="uncomparable", severity="hard"),  # never binds
        candidate("security_clearance", "clearance", severity="hard"),  # unconfirmed: no cap
        candidate("onsite_required", "onsite", comparison="met", severity="hard"),
    ]
    confirmations: dict[str, Confirmation] = {
        "degree:degree": "critic_confirmed",
        "certification:aws": "uncertain",
        "mandatory_language:fr": "critic_confirmed",
        "onsite_required:onsite": "critic_confirmed",
    }
    result = run(knockouts=knockouts, confirmations=confirmations, industry="outside_include")
    # degree and the industry preference, each -10: 61.25..71.25
    assert (result.low, result.mid, result.high) == (Decimal("61.25"), Decimal("66.25"), Decimal("71.25"))
    assert result.caps_applied == [{"code": "degree", "kind": "soft"}, {"code": "industry", "kind": "soft"}]
    assert result.knockouts == [
        {
            "code": "degree",
            "fact_id": "degree:degree",
            "comparison": "unmet",
            "severity": "soft",
            "binding": True,
            "source": "critic_confirmed",
        },
        {
            "code": "certification",
            "fact_id": "certification:aws",
            "comparison": "unmet",
            "severity": "soft",
            "binding": False,
            "source": "uncertain",
        },
        {
            "code": "mandatory_language",
            "fact_id": "mandatory_language:fr",
            "comparison": "uncomparable",
            "severity": "hard",
            "binding": False,
            "source": "critic_confirmed",
        },
        {
            "code": "security_clearance",
            "fact_id": "security_clearance:clearance",
            "comparison": "unmet",
            "severity": "hard",
            "binding": False,
            "source": "unconfirmed",
        },
        {
            "code": "onsite_required",
            "fact_id": "onsite_required:onsite",
            "comparison": "met",
            "severity": "hard",
            "binding": False,
            "source": "critic_confirmed",
        },
    ]


def test_a_structured_knockout_binds_and_a_rejected_one_is_dropped() -> None:
    knockouts = [
        candidate("contract_type", "employment_types", source="structured", severity="hard"),
        candidate("work_permit", "work_permit", severity="hard"),
    ]
    result = run(knockouts=knockouts, confirmations={"work_permit:work_permit": "critic_rejected"})
    assert (result.low, result.mid, result.high) == (Decimal(25), Decimal(25), Decimal(25))
    assert result.caps_applied == [{"code": "contract_type", "kind": "hard"}]
    assert [(k["fact_id"], k["binding"], k["source"]) for k in result.knockouts] == [
        ("contract_type:employment_types", True, "structured")
    ]


def test_an_excluded_industry_is_one_soft_penalty() -> None:
    result = run(industry="excluded")
    assert (result.low, result.high) == (Decimal("71.25"), Decimal("81.25"))
    assert result.caps_applied == [{"code": "industry", "kind": "soft"}]
    assert run(industry="unknown").caps_applied == []


def test_aggregate_confidence_reads_its_inputs() -> None:
    assert run(contested=True).confidence == "medium"
    assert run(injection_suspected=True, critic_injection="none").confidence == "high"
    assert run(injection_suspected=True, critic_injection="unavailable").confidence == "low"
    assert run(completeness="partial").confidence == "low"


def test_a_verified_not_a_job_ad_has_no_score_and_no_criterion() -> None:
    knockouts = [candidate("contract_type", "employment_types", source="structured", severity="hard")]
    result = run(not_a_job_ad_verified=True, knockouts=knockouts)
    assert (result.low, result.mid, result.high, result.confidence) == (None, None, None, None)
    assert result.coverage == Decimal(0)
    assert result.caps_applied == []
    assert result.det_criteria == {
        "location_work_mode": {"level": None, "basis": "unknown", "inputs": {"et": 4, "sal": 4}},
        "employment_salary": {"level": None, "basis": "unknown", "inputs": {"et": None, "sal": 4}},
    }


def test_a_binding_knockout_without_a_severity_is_refused() -> None:
    unsevered = replace(candidate("contract_type", "employment_types", source="structured"), severity=None)
    with pytest.raises(ValueError, match="no severity"):
        run(knockouts=[unsevered])
    # a candidate that does not bind needs none
    met = replace(candidate("degree", "degree", comparison="met"), severity=None)
    assert run(knockouts=[met]).knockouts[0]["severity"] is None


def test_aggregate_needs_every_llm_criterion() -> None:
    criteria = {key: rating(4) for key in LLM_CRITERIA[:-1]}
    with pytest.raises(ValueError, match="education_certifications"):
        run(criteria=criteria)
