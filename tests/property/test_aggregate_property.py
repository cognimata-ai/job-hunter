"""Aggregation and publication over random inputs (spec §11.8 L4512-4524, §7.11 L2496-2504, §11.6 L4394; M3
Task 6). For random levels, weights and cap sets:

- `0 ≤ L ≤ M ≤ U ≤ 100` and `M = round((L + U) / 2, 2)` (half up, as PostgreSQL rounds a non-negative
  numeric), and `coverage` in `[0, 1]`; a hard code caps `U` at 25, and `n` distinct soft codes lower it by
  `10 n` down to 0;
- `aggregate`'s output satisfies the `det_results` CHECKs, here in Python, the DDL's predicates one by one
  (and `numeric(5,2)`, `numeric(4,3)`): the integration tests write instances through the real ones;
- for random drafts and their `validate_draft` result, the draft `repair_for_publication(validation.draft,
  validation)` returns validates against the strict `PublishedResult` limits (Controller Ruling P6; the draft
  is the validation's own, whose paths the validation's issues name).

Every datum is generated or invented.
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final, get_args

from hypothesis import given, settings
from hypothesis import strategies as st
from jobhunter.llm.schemas.generator import DraftCriterion, GeneratorDraft
from jobhunter.llm.schemas.published import PublishedResult
from jobhunter.scoring.aggregate import (
    HARD_CAP,
    SOFT_PENALTY,
    Aggregation,
    IndustryMatch,
    Interval,
    aggregate,
    caps,
    interval,
)
from jobhunter.scoring.det_criteria import DetCriterion
from jobhunter.scoring.knockouts import Comparison, Confirmation, KnockoutCandidate
from jobhunter.scoring.rubric import LLM_CRITERIA, RUBRIC_KEYS, load_rubric
from jobhunter.scoring.validator import (
    Normalisation,
    ValidationContext,
    repair_for_publication,
    validate_draft,
)

from tests.property.test_validator_property import CONTEXTS, drafts
from tests.support.validation import publication_doc

CENT: Final = Decimal("0.01")
CODES: Final = (
    "onsite_required",
    "mandatory_language",
    "security_clearance",
    "work_permit",
    "degree",
    "certification",
    "contract_type",
    "industry",
)
CONFIRMATIONS: Final = get_args(Confirmation)
COMPARISONS: Final[tuple[Comparison, ...]] = get_args(Comparison)
INDUSTRY_MATCHES: Final = get_args(IndustryMatch)

levels_ = st.none() | st.integers(0, 4)
codes = st.lists(st.sampled_from(CODES), max_size=6)


@st.composite
def weights(draw: st.DrawFn) -> dict[str, int]:
    """Rubric v2's weights, or eight non-negative integers summing to 100."""
    if draw(st.booleans()):
        return dict(load_rubric("v2").weights)
    cuts = sorted(draw(st.lists(st.integers(0, 100), min_size=7, max_size=7)))
    parts = [b - a for a, b in zip([0, *cuts], [*cuts, 100], strict=True)]
    return dict(zip(RUBRIC_KEYS, parts, strict=True))


def representable(value: Decimal, places: str) -> bool:
    return value == value.quantize(Decimal(places))


def half_up_mid(low: Decimal, high: Decimal) -> Decimal:
    return ((low + high) / 2).quantize(CENT, ROUND_HALF_UP)


@settings(max_examples=400, deadline=None)
@given(
    levels=st.fixed_dictionaries({key: levels_ for key in RUBRIC_KEYS}),
    w=weights(),
    soft=codes,
    hard=codes,
)
def test_the_interval_and_its_caps_stay_ordered(
    levels: dict[str, int | None], w: dict[str, int], soft: list[str], hard: list[str]
) -> None:
    span = interval(levels, w)
    assert 0 <= span.coverage <= 1 and representable(span.coverage, "0.001")
    assert 0 <= span.low <= span.high <= 100
    capped = caps(span, soft_codes=soft, hard_codes=hard)
    assert 0 <= capped.low <= capped.mid <= capped.high <= 100
    assert capped.mid == half_up_mid(capped.low, capped.high)
    assert all(representable(x, "0.01") for x in (capped.low, capped.mid, capped.high))
    lowered = max(Decimal(0), span.high - SOFT_PENALTY * len(set(soft)))
    assert capped.high == (min(lowered, Decimal(HARD_CAP)) if hard else lowered)
    assert capped.low <= span.low and capped.high <= span.high
    assert [cap.code for cap in capped.applied] == [*dict.fromkeys(soft), *dict.fromkeys(hard)]


def test_interval_is_exact_for_the_v2_weights() -> None:
    span = interval(dict.fromkeys(RUBRIC_KEYS, 1), load_rubric("v2").weights)
    assert (span.low, span.high, span.coverage) == (Decimal(25), Decimal(25), Decimal(1))
    assert (
        caps(Interval(Decimal(0), Decimal("0.25"), Decimal(0)), soft_codes=(), hard_codes=()).mid == CENT * 13
    )


def rating(level: int | None) -> DraftCriterion:
    basis = "unknown" if level is None else "observed"
    return DraftCriterion.model_validate(
        {"level": level, "basis": basis, "ad_evidence": [], "profile_evidence": [], "rationale": ""}
    )


@st.composite
def candidates(draw: st.DrawFn) -> KnockoutCandidate:
    code = draw(st.sampled_from(CODES[:-1]))
    structured = draw(st.booleans())
    comparison: Comparison = draw(st.sampled_from(COMPARISONS))
    return KnockoutCandidate(
        code=code,
        fact_id=f"{code}:{draw(st.sampled_from(('a', 'b')))}",
        subject="a",
        requirement_source="structured" if structured else "text",
        comparison=comparison,
        binding_capable=not structured and comparison != "met",
        severity=draw(st.sampled_from(("hard", "soft"))),
        inputs={},
    )


def assert_the_det_results_checks(a: Aggregation) -> None:
    """The DDL's CHECKs of `det_results` (L2496-2504) over the instance's score columns."""
    scores = (a.low, a.mid, a.high)
    assert len({x is None for x in scores}) == 1  # (score_mid IS NULL) = (score_low IS NULL) = …high
    low, mid, high = scores
    if low is not None and mid is not None and high is not None:
        assert 0 <= low <= mid <= high <= 100
        assert mid == half_up_mid(low, high)  # score_mid = round((score_low + score_high) / 2, 2)
        assert all(representable(x, "0.01") and x < 1000 for x in (low, mid, high))  # numeric(5,2)
    assert (a.mid is None) == (a.confidence is None)
    assert a.confidence in (None, "high", "medium", "low")
    assert 0 <= a.coverage <= 1 and representable(a.coverage, "0.001")  # numeric(4,3)
    assert a.industry_match in INDUSTRY_MATCHES


@settings(max_examples=400, deadline=None)
@given(
    levels=st.fixed_dictionaries({key: levels_ for key in RUBRIC_KEYS}),
    w=weights(),
    knockouts=st.lists(candidates(), max_size=5),
    confirmations=st.dictionaries(
        st.sampled_from([f"{c}:{s}" for c in CODES for s in "ab"]), st.sampled_from(CONFIRMATIONS)
    ),
    industry=st.sampled_from(INDUSTRY_MATCHES),
    completeness=st.sampled_from(("full", "partial")),
    quality=st.sampled_from(("ok", "short", "garbage", None)),
    contested=st.booleans(),
    injection_suspected=st.booleans(),
    critic_injection=st.sampled_from(
        (None, "none", "present_no_influence", "present_influenced", "unavailable")
    ),
    verified=st.booleans(),
)
def test_aggregate_satisfies_the_det_results_checks(
    levels: dict[str, int | None],
    w: dict[str, int],
    knockouts: list[KnockoutCandidate],
    confirmations: dict[str, Confirmation],
    industry: IndustryMatch,
    completeness: str,
    quality: str | None,
    contested: bool,
    injection_suspected: bool,
    critic_injection: str | None,
    verified: bool,
) -> None:
    arguments: dict[str, Any] = {
        "criteria": {key: rating(levels[key]) for key in LLM_CRITERIA},
        "det_location": DetCriterion(levels["location_work_mode"], "observed", {}),
        "det_employment": DetCriterion(levels["employment_salary"], "observed", {}),
        "weights": w,
        "knockouts": knockouts,
        "confirmations": confirmations,
        "industry": industry,
        "completeness": completeness,
        "quality": quality,
        "contested": contested,
        "injection_suspected": injection_suspected,
        "critic_injection": critic_injection,
        "not_a_job_ad_verified": verified,
    }
    a = aggregate(**arguments)
    assert_the_det_results_checks(a)
    assert (a.mid is None) == verified
    binding = [k for k in a.knockouts if k["binding"]]
    assert all(
        k["comparison"] == "unmet" and k["source"] in ("structured", "critic_confirmed") for k in binding
    )
    assert all(k["source"] != "critic_rejected" for k in a.knockouts)
    if verified:
        assert a.caps_applied == []
    else:
        assert {cap["code"] for cap in a.caps_applied} == {k["code"] for k in binding} | (
            {"industry"} if industry in ("excluded", "outside_include") else set()
        )


@settings(max_examples=300, deadline=None)
@given(draft=drafts(), ctx=st.sampled_from(CONTEXTS))
def test_the_publication_repairs_meet_the_strict_limits(
    draft: GeneratorDraft, ctx: ValidationContext
) -> None:
    validation = validate_draft(draft, ctx, normalisation=Normalisation())
    repaired, truncated = repair_for_publication(validation.draft, validation)
    PublishedResult.model_validate(
        publication_doc(
            repaired, truncated_fields=truncated, injection_suspected=validation.injection_suspected
        )
    )
