"""The validator's stage 2 (spec §11.6 V1-V6, as they apply to candidate C; M3 Task 5), the deterministic
seniority rating (§11.3 L4085-4103) and the repairs at publication (L4380-4392).

- V1: every limit is an issue (T1), never a failure.
- V2: the level/basis rules and the evidence of every asserted fact are failures of their criterion or fact
  (a fact without an id fails on its pseudo key).
- V3: a quote grounds in one segment of the rendered `<job_ad>` (folded on both sides), and a description
  quote maps back to its span in `job_revisions.description_text`, a tail quote included; an ellipsis, a
  quote under 4 or over 300 characters, one with under 3 letters or digits, one across the truncation marker,
  and a profile quote outside its named section fail.
- V4: the repairable corrections (the deterministic seniority, the structured work mode, the remote-region
  contradictions, duplicate fact ids) trigger nothing; the consistency checks are failures.
- `repair_for_publication` cuts what is over its limit and records it, with the history's corrections.

Every datum is invented.
"""

from typing import Any

import pytest
from jobhunter.llm.schemas.critic import CriticOut
from jobhunter.llm.schemas.generator import (
    DraftCriterion,
    GeneratorDraft,
    MinYearsExperience,
    ProfileRefWire,
    Seniority,
)
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.scoring.det_criteria import level_sub_rating, seniority_level, years_sub_rating
from jobhunter.scoring.facts import NOT_A_JOB_AD, fact_ref
from jobhunter.scoring.validator import (
    SENIORITY_EXEMPT,
    CorrectionHistory,
    DraftValidation,
    Failure,
    Grounding,
    Issue,
    Normalisation,
    ParsedGenerator,
    Repair,
    ValidationContext,
    ground_quote,
    parse_generator,
    quote_defect,
    repair_for_publication,
    validate_draft,
    validate_review,
)

from tests.support.llm import score_profile
from tests.support.scoring import profile
from tests.support.validation import (
    CRITERIA,
    SHARED,
    SyntheticAd,
    ctx_for,
    synthetic_ad,
    synthetic_wire_c,
)

GROUNDED = "Python, PostgreSQL"  # in the description of every synthetic ad


def base_draft() -> GeneratorDraft:
    parsed = parse_generator(synthetic_wire_c(), family="generator_v2_items")
    assert isinstance(parsed, ParsedGenerator)
    return parsed.draft


def draft_with(
    facts: dict[str, Any] | None = None, criteria: dict[str, dict[str, Any]] | None = None, **top: Any
) -> GeneratorDraft:
    """The clean synthetic draft with the given job facts, criterion fields and top-level fields replaced."""
    doc = base_draft().model_dump(mode="json")
    doc["job_facts"].update(facts or {})
    for key, changes in (criteria or {}).items():
        doc["criteria"][key].update(changes)
    doc.update(top)
    return GeneratorDraft.model_validate(doc)


def validated(
    draft: GeneratorDraft, ctx: ValidationContext | None = None, ad: SyntheticAd | None = None
) -> DraftValidation:
    context = ctx if ctx is not None else ctx_for(ad if ad is not None else synthetic_ad())
    return validate_draft(draft, context, normalisation=Normalisation())


def language(
    code: str, level: str = "B2", *, mandatory: bool = True, other: str | None = None
) -> dict[str, Any]:
    return {
        "language": code,
        "language_other": other,
        "level": level,
        "mandatory": mandatory,
        "evidence": [GROUNDED],
    }


def requirement(text: str) -> dict[str, Any]:
    return {"text": text, "evidence": [GROUNDED]}


def test_a_clean_c_output_validates_with_nothing_to_report() -> None:
    v = validated(base_draft())
    assert (v.issues, v.failures, v.repairs) == ([], [], [])
    assert v.t1 is False and v.injection_suspected is False
    assert v.draft == base_draft()
    assert v.history == CorrectionHistory(Normalisation(), [])
    assert v.fact_ids == {}
    assert v.groundings["criteria.skills.ad_evidence"] == [Grounding("description", None, 51, 69)]
    assert v.groundings["job_facts.employment_types.evidence"] == [
        Grounding("structured_field", "employment_types", None, None)
    ]
    assert v.groundings["criteria.skills.profile_evidence"] == [Grounding("profile", "skills", None, None)]


# ---- V1 ---------------------------------------------------------------------------------------------------

WORDS_61 = " ".join(["Wort"] * 61)
CHARS = "x" * 500


@pytest.mark.parametrize(
    ("changes", "issue"),
    [
        ({"summary_en": WORDS_61}, Issue("too_many_words", "summary_en", {"max_words": 60})),
        ({"summary_de": CHARS[:451]}, Issue("too_many_chars", "summary_de", {"max_chars": 450})),
        (
            {"criteria": {"domain": {"rationale": " ".join(["Wort"] * 26)}}},
            Issue("too_many_words", "criteria.domain.rationale", {"max_words": 25}),
        ),
        (
            {"criteria": {"domain": {"rationale": CHARS[:201]}}},
            Issue("too_many_chars", "criteria.domain.rationale", {"max_chars": 200}),
        ),
        (
            {"criteria": {"domain": {"ad_evidence": [GROUNDED, "Berlin, hybrid", "Backend Engineer"]}}},
            Issue("too_many_quotes", "criteria.domain.ad_evidence", {"max_quotes": 2}),
        ),
        (
            {
                "criteria": {
                    "domain": {
                        "profile_evidence": [
                            {"section": "skills", "quote": "Python"},
                            {"section": "skills", "quote": "PostgreSQL"},
                            {"section": "domains", "quote": "Logistik"},
                        ]
                    }
                }
            },
            Issue("too_many_quotes", "criteria.domain.profile_evidence", {"max_quotes": 2}),
        ),
        (
            {"facts": {"must_haves": [requirement("Python")] * 26}},
            Issue("too_many_items", "job_facts.must_haves", {"max_items": 25}),
        ),
        (
            {"facts": {"nice_to_haves": [requirement("Kafka")] * 26}},
            Issue("too_many_items", "job_facts.nice_to_haves", {"max_items": 25}),
        ),
        (
            {"facts": {"must_haves": [requirement("Python " + CHARS[:194])]}},
            Issue("too_many_chars", "job_facts.must_haves[0].text", {"max_chars": 200}),
        ),
        (
            {
                "facts": {
                    "certifications": [{"name": CHARS[:121], "mandatory": False, "evidence": [GROUNDED]}]
                }
            },
            Issue("too_many_chars", "job_facts.certifications[0].name", {"max_chars": 120}),
        ),
        (
            {"facts": {"certifications": [{"name": "AWS", "mandatory": False, "evidence": [GROUNDED]}] * 11}},
            Issue("too_many_items", "job_facts.certifications", {"max_items": 10}),
        ),
        (
            {"facts": {"industry": {"value": CHARS[:121], "evidence": [GROUNDED]}}},
            Issue("too_many_chars", "job_facts.industry.value", {"max_chars": 120}),
        ),
        (
            {
                "facts": {
                    "degree": {
                        "level": "bachelor",
                        "mandatory": False,
                        "or_equivalent": False,
                        "field": CHARS[:121],
                        "evidence": [GROUNDED],
                    }
                }
            },
            Issue("too_many_chars", "job_facts.degree.field", {"max_chars": 120}),
        ),
        (
            {"facts": {"required_languages": [language("other", mandatory=False, other=CHARS[:41])]}},
            Issue("too_many_chars", "job_facts.required_languages[0].language_other", {"max_chars": 40}),
        ),
        (
            {"facts": {"required_languages": [language("en", mandatory=False)] * 7}},
            Issue("too_many_items", "job_facts.required_languages", {"max_items": 6}),
        ),
        (
            {
                "facts": {
                    "security_clearance": {"required": False, "level": CHARS[:61], "evidence": [GROUNDED]}
                }
            },
            Issue("too_many_chars", "job_facts.security_clearance.level", {"max_chars": 60}),
        ),
        (
            {
                "facts": {
                    "remote_region": {
                        "value": "unspecified",
                        "countries": ["AT"] * 31,
                        "evidence": [GROUNDED],
                    }
                }
            },
            Issue("too_many_items", "job_facts.remote_region.countries", {"max_items": 30}),
        ),
        (
            {
                "facts": {
                    "remote_region": {
                        "value": "unspecified",
                        "countries": ["AT", "XX"],
                        "evidence": [GROUNDED],
                    }
                }
            },
            Issue("country_invalid", "job_facts.remote_region.countries[1]", {}),
        ),
        (
            {"facts": {"min_years_experience": {"value": 41, "evidence": [GROUNDED]}}},
            Issue("out_of_range", "job_facts.min_years_experience.value", {"min": 0, "max": 40}),
        ),
    ],
)
def test_each_limit_is_an_issue_never_a_failure(changes: dict[str, Any], issue: Issue) -> None:
    top = {k: v for k, v in changes.items() if k not in ("facts", "criteria")}
    v = validated(draft_with(changes.get("facts"), changes.get("criteria"), **top))
    assert v.issues == [issue]
    assert v.failures == []
    assert v.t1 is True  # a limit issue sends the draft to the critic


# ---- V2 ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"level": 2, "basis": "unknown"}, "unknown_with_level"),
        ({"level": None, "basis": "observed"}, "level_missing"),
        ({"level": None, "basis": "unconstrained"}, "level_missing"),
        ({"level": 3, "basis": "unconstrained"}, "unconstrained_not_4"),
        ({"level": 4, "basis": "unconstrained", "ad_evidence": []}, "ad_evidence_missing"),
        ({"level": 2, "basis": "observed", "ad_evidence": []}, "ad_evidence_missing"),
        ({"level": 1, "basis": "observed", "profile_evidence": []}, "profile_evidence_missing"),
    ],
)
def test_each_level_basis_rule_is_a_failure_of_its_criterion(changes: dict[str, Any], code: str) -> None:
    v = validated(draft_with(criteria={"domain": changes}))
    assert v.failures == [Failure("domain", code)]
    assert v.issues == []


def test_the_evidence_rules_that_hold() -> None:
    v = validated(
        draft_with(
            criteria={
                "domain": {"level": 0, "basis": "observed", "profile_evidence": []},  # level 0: an ad quote
                "role_fit": {"level": None, "basis": "unknown", "ad_evidence": [], "profile_evidence": []},
                "languages": {"level": 4, "basis": "unconstrained", "profile_evidence": []},
            }
        )
    )
    assert v.failures == []


def test_every_asserted_fact_needs_a_quote_and_fails_on_its_id_or_pseudo_key() -> None:
    v = validated(
        draft_with(
            {
                "work_mode": {"value": "hybrid", "evidence": []},
                "must_haves": [requirement("Python"), {"text": "PostgreSQL", "evidence": []}],
                "nice_to_haves": [{"text": "Kafka", "evidence": []}],
                "required_languages": [
                    {**language("de"), "evidence": []},
                    {**language("en", mandatory=False), "evidence": []},
                ],
                "certifications": [{"name": "Scrum Master", "mandatory": False, "evidence": []}],
                "onsite_only": {"asserted": False, "evidence": []},  # not asserted: no quote needed
                "seniority": {"value": "unspecified", "evidence": []},
            }
        )
    )
    assert v.failures == [
        Failure("work_mode", "fact_evidence_missing"),
        Failure("mandatory_language:de", "fact_evidence_missing"),
        Failure("required_language:1", "fact_evidence_missing"),
        Failure("must_have:1", "fact_evidence_missing"),
        Failure("nice_to_have:0", "fact_evidence_missing"),
        Failure("certification_item:0", "fact_evidence_missing"),
    ]


# ---- V3 ---------------------------------------------------------------------------------------------------

DESCRIPTION_UMLAUT = (
    "Wir suchen eine Entwicklerin für   die\nSendungsverfolgung in Hamburg. Python, PostgreSQL."
)


def test_a_quote_grounds_across_an_umlaut_and_collapsed_whitespace_to_its_description_span() -> None:
    ad = synthetic_ad(DESCRIPTION_UMLAUT)
    grounding = ground_quote("Entwicklerin fur die Sendungsverfolgung", ad.block, {})
    assert grounding is not None and grounding.source_kind == "description"
    assert grounding.span_start is not None and grounding.span_end is not None
    assert DESCRIPTION_UMLAUT[grounding.span_start : grounding.span_end] == (
        "Entwicklerin für   die\nSendungsverfolgung"
    )
    decomposed = synthetic_ad("Erfahrung mit Sendungsverfolgung für Kunden. Python, PostgreSQL.")
    grounding = ground_quote("Sendungsverfolgung für Kunden", decomposed.block, {})
    assert grounding is not None and grounding.span_start is not None and grounding.span_end is not None
    assert decomposed.description_text[grounding.span_start : grounding.span_end] == (
        "Sendungsverfolgung für Kunden"
    )


def test_a_structured_field_quote_names_its_field_and_has_no_description_span() -> None:
    assert ground_quote("hybrid (structured)", synthetic_ad().block, {}) == Grounding(
        "structured_field", "work_modes", None, None
    )
    assert ground_quote("Senior Backend Engineer", synthetic_ad().block, {}) == Grounding(
        "structured_field", "title", None, None
    )


@pytest.mark.parametrize(
    ("quote", "code"),
    [
        ("Python, … PostgreSQL", "quote_ellipsis"),
        ("Python... PostgreSQL", "quote_ellipsis"),
        ("SQL", "quote_too_short"),
        ("Python" + " PostgreSQL" * 26 + "!" * 9, "quote_too_long"),  # 301 characters
        ("- - -", "quote_too_few_alnum"),
        ("Java und Kotlin", "quote_ungrounded"),
    ],
)
def test_a_quote_that_cannot_ground_is_a_failure_of_its_criterion(quote: str, code: str) -> None:
    if code != "quote_ungrounded":
        assert quote_defect(quote) == code
    assert ground_quote(quote, synthetic_ad().block, {}) is None
    v = validated(draft_with(criteria={"domain": {"ad_evidence": [GROUNDED, quote]}}))
    assert v.failures == [Failure("domain", code)]
    assert v.issues == [Issue(code, "criteria.domain.ad_evidence[1]", {"key": "domain"})]
    assert v.groundings["criteria.domain.ad_evidence"] == [Grounding("description", None, 51, 69)]
    assert v.draft.criteria.domain.ad_evidence == [GROUNDED, quote]  # dropped at publication, kept here


def test_a_300_character_quote_is_still_a_quote() -> None:
    quote = "Python" + " PostgreSQL" * 26 + "!" * 8
    assert len(quote) == 300
    assert quote_defect(quote) is None


def _truncated_ad() -> SyntheticAd:
    head = "x" * 19_984 + "Ende des Anfangs"  # the first 20,000 characters
    tail = "Beginn des Endes" + " z" * 1_992  # the last 4,000
    return synthetic_ad(head + "y" * 5_000 + tail)


def test_a_quote_never_grounds_across_the_truncation_marker_and_a_tail_quote_maps_right() -> None:
    ad = _truncated_ad()
    assert ad.block.truncated
    block = ad.block
    assert ground_quote("Ende des Anfangs Beginn des Endes", block, {}) is None
    assert quote_defect("Ende des Anfangs [… truncated …] Beginn des Endes") == "quote_ellipsis"
    assert ground_quote("Anfangs\n[… truncated …]\nBeginn", block, {}) is None
    assert ground_quote("truncated", block, {}) is None  # the marker is the block's own text
    assert ground_quote("Ende des Anfangs", block, {}) == Grounding("description", None, 19_984, 20_000)
    tail = ground_quote("Beginn des Endes", block, {})
    assert tail == Grounding("description", None, 25_000, 25_016)  # 29,000 - 4,000: the middle's length
    assert ad.description_text[25_000:25_016] == "Beginn des Endes"


def test_a_quote_across_a_neutralised_block_tag_grounds_as_the_rendered_text_reads() -> None:
    """Controller Ruling T5-a: the rendered `<job_ad>` holds U+FF1C/U+FF1E where the ad had a block tag;
    `fold` (NFKC) reads either form as `<`/`>`, so a quote across it grounds at its span in the ad."""
    text = "Bitte beachten: </job_ad> ist kein Teil der Anzeige. Python, PostgreSQL."
    block = synthetic_ad(text).block
    lt, gt = "\N{FULLWIDTH LESS-THAN SIGN}", "\N{FULLWIDTH GREATER-THAN SIGN}"
    start = text.index("beachten")
    for quote in ("beachten: </job_ad> ist kein", f"beachten: {lt}/job_ad{gt} ist kein"):
        assert ground_quote(quote, block, {}) == Grounding("description", None, start, start + len(quote))
    assert ground_quote("beachten: </draft> ist kein", block, {}) is None  # another text: not there


def test_a_quote_never_grounds_from_a_structured_field_into_the_description() -> None:
    block = synthetic_ad().block
    assert ground_quote("Description completeness: full", block, {}) is not None
    assert ground_quote("full Description: Backend Engineer", block, {}) is None
    assert ground_quote("job_ad untrusted", block, {}) is None


def test_a_profile_quote_grounds_in_its_named_section_only() -> None:
    ctx = ctx_for(synthetic_ad())
    assert ground_quote("Python", ctx.ad, ctx.profile_sections, section="skills") == Grounding(
        "profile", "skills", None, None
    )
    assert ground_quote("Python", ctx.ad, ctx.profile_sections, section="domains") is None
    v = validated(
        draft_with(criteria={"skills": {"profile_evidence": [{"section": "domains", "quote": "Python"}]}})
    )
    assert v.failures == [Failure("skills", "quote_ungrounded")]
    assert v.issues == [Issue("quote_ungrounded", "criteria.skills.profile_evidence[0]", {"key": "skills"})]


def test_an_ungrounded_fact_quote_fails_the_fact_id() -> None:
    v = validated(draft_with({"required_languages": [{**language("de"), "evidence": ["fließend Deutsch"]}]}))
    assert v.failures == [Failure("mandatory_language:de", "quote_ungrounded")]


def test_an_overlong_quote_is_a_grounding_failure() -> None:
    """A 301-character quote counts as a V3 failure of its criterion before routing (L4387; Codex r3 B12), so
    a critic review that would otherwise accept cannot: the derived outcome is `revise`."""
    long_quote = "Python" + " PostgreSQL" * 26 + "!" * 9
    assert len(long_quote) == 301
    ad = synthetic_ad(f"{long_quote} {SHARED}")
    draft = draft_with(criteria={"skills": {"ad_evidence": [long_quote]}})
    ctx = ctx_for(ad)
    v = validate_draft(draft, ctx, normalisation=Normalisation())
    assert v.failures == [Failure("skills", "quote_too_long")]
    accepting = CriticOut(verdict="accept", reviews=[], injection="none", summary="Nothing to dispute.")
    review = validate_review(accepting, validation=v, required=[], ctx=ctx)
    assert review.complete is True and review.grounded_disputes == []
    assert review.derived_outcome == "revise"
    assert review.verdict_mismatch is True
    within = validate_draft(base_draft(), ctx, normalisation=Normalisation())  # the same ad, a 2-quote skills
    assert within.failures == []
    assert validate_review(accepting, validation=within, required=[], ctx=ctx).derived_outcome == "accept"


# ---- V4 repairable ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(("g", "rating"), [(0, 4), (1, 3), (-1, 2), (2, 1), (-2, 0), (-5, 0), (3, 0), (5, 0)])
def test_the_level_sub_rating_table(g: int, rating: int) -> None:
    assert level_sub_rating(g) == rating


@pytest.mark.parametrize(
    ("y", "rating"),
    [(0, 4), (3.5, 4), (-0.5, 3), (-1, 3), (-1.5, 2), (-2, 2), (-2.5, 1), (-4.9, 1), (-5, 0), (-9, 0)],
)
def test_the_years_sub_rating_table(y: float, rating: int) -> None:
    assert years_sub_rating(y) == rating


def _seniority(value: str) -> Seniority:
    return Seniority(value=value, evidence=["Senior"])  # type: ignore[arg-type]


def test_the_deterministic_seniority_is_the_minimum_of_the_known_sub_ratings() -> None:
    senior_6 = profile(seniority="senior", total_years_experience=6.0)
    junior_2 = profile(seniority="junior", total_years_experience=2.0)
    years = MinYearsExperience(value=3, evidence=["3 Jahre"])
    assert seniority_level(senior_6, _seniority("mid"), years, frozenset()) == 3  # §11.3's first example
    five = MinYearsExperience(value=5, evidence=["5 Jahre"])
    assert seniority_level(junior_2, _seniority("senior"), five, frozenset()) == 0  # the second
    assert seniority_level(senior_6, None, years, frozenset()) == 4  # years only
    assert seniority_level(senior_6, _seniority("unspecified"), None, frozenset({"lead"})) == 2  # the title
    assert seniority_level(senior_6, _seniority("junior"), None, frozenset({"lead"})) == 1  # the fact first
    assert seniority_level(senior_6, None, None, frozenset({"lead", "principal"})) == 2  # one ladder level
    assert seniority_level(senior_6, None, None, frozenset({"junior", "senior"})) is None  # ambiguous title
    assert seniority_level(senior_6, None, None, frozenset()) is None  # both unknown
    unknown_years = profile(seniority="senior", total_years_experience=None)
    assert seniority_level(unknown_years, None, years, frozenset()) is None


def _seniority_ad(description: str) -> SyntheticAd:
    return synthetic_ad(f"{description} {SHARED}", title="Backend Engineer (m/w/d)")


SENIORITY_PATH = "criteria.experience_seniority.level"


def recomputed(replaced: int | None) -> Repair:
    """The repair of a known deterministic seniority: exempt from V2's evidence rules and V1's counts."""
    return Repair(SENIORITY_PATH, "seniority_recomputed", replaced, SENIORITY_EXEMPT)


def test_the_deterministic_seniority_replaces_the_llm_level_and_records_the_repair() -> None:
    ad = _seniority_ad("Mid-Level Entwicklerin mit 3 Jahren Erfahrung.")
    draft = draft_with(
        {
            "seniority": {"value": "mid", "evidence": ["Mid-Level"]},
            "min_years_experience": {"value": 3, "evidence": ["3 Jahren Erfahrung"]},
        }
    )
    v = validated(draft, ad=ad)  # a senior profile with 6 years: level 3, years 4
    assert v.draft.criteria.experience_seniority == DraftCriterion(
        level=3,
        basis="observed",
        ad_evidence=["Mid-Level", "3 Jahren Erfahrung"],
        profile_evidence=[ProfileRefWire(section="skills", quote="Python")],
        rationale="Die Kernkenntnisse sind belegt.",
    )
    assert v.repairs == [recomputed(4)]
    assert SENIORITY_EXEMPT == ("ad_evidence_missing", "profile_evidence_missing", "too_many_quotes")
    assert v.history.corrections == v.repairs
    assert v.failures == [] and v.issues == [] and v.t1 is False  # a repair triggers nothing
    again = validate_draft(v.draft, ctx_for(ad), normalisation=Normalisation(), history=v.history)
    assert again.repairs == [] and again.draft == v.draft  # the corrected draft is a fixed point


def test_the_second_seniority_example_rates_zero() -> None:
    ad = _seniority_ad("Senior-Rolle, mindestens 5 Jahre Berufserfahrung.")
    junior = score_profile().model_copy(update={"seniority": "junior", "total_years_experience": 2.0})
    draft = draft_with(
        {
            "seniority": {"value": "senior", "evidence": ["Senior-Rolle"]},
            "min_years_experience": {"value": 5, "evidence": ["mindestens 5 Jahre Berufserfahrung"]},
        },
        criteria={"experience_seniority": {"level": 2}},
    )
    v = validated(draft, ctx_for(ad, junior))
    assert v.draft.criteria.experience_seniority.level == 0
    assert v.repairs == [recomputed(2)]


def test_the_recomputed_ad_evidence_is_the_first_grounded_quote_of_each_fact_then_the_rest_up_to_two() -> (
    None
):
    ad = _seniority_ad("Mid-Level, also mittlere Erfahrung, mit 3 Jahren Erfahrung oder drei Jahren Praxis.")
    both = draft_with(
        {
            "seniority": {"value": "mid", "evidence": ["Mid-Level", "nicht im Text", "mittlere Erfahrung"]},
            "min_years_experience": {"value": 3, "evidence": ["3 Jahren Erfahrung", "drei Jahren Praxis"]},
        }
    )
    v = validated(both, ad=ad)
    assert v.draft.criteria.experience_seniority.ad_evidence == ["Mid-Level", "3 Jahren Erfahrung"]
    assert [i for i in v.issues if i.code == "too_many_quotes"] == []  # never a limit issue of its own
    only = draft_with(
        {"seniority": {"value": "mid", "evidence": ["Mid-Level", "mittlere Erfahrung", "Mid-Level"]}}
    )
    v = validated(only, ad=ad)
    assert v.draft.criteria.experience_seniority.ad_evidence == ["Mid-Level", "mittlere Erfahrung"]


def test_a_known_deterministic_level_without_a_quote_raises_nothing() -> None:
    """The generator said `unknown`, and the title's token rates the level: the correction has no quote and
    no profile reference, yet raises no V2 failure, no T1 and nothing to null at a contested publication;
    a revision that carries the corrected criterion (validated afresh, with no history) stays clean too."""
    unknown: dict[str, Any] = {"level": None, "basis": "unknown", "ad_evidence": [], "profile_evidence": []}
    v = validated(draft_with(criteria={"experience_seniority": unknown}))  # the title "Senior …"
    assert v.draft.criteria.experience_seniority == DraftCriterion(
        level=4,
        basis="observed",
        ad_evidence=[],
        profile_evidence=[],
        rationale="Die Kernkenntnisse sind belegt.",
    )
    assert v.repairs == [recomputed(None)]
    assert (v.failures, v.issues, v.t1) == ([], [], False)
    revision = validated(v.draft)
    assert (revision.repairs, revision.failures, revision.issues) == ([], [], [])


def test_the_evidence_counts_of_a_deterministic_level_are_no_limit_issue() -> None:
    refs = [{"section": "skills", "quote": q} for q in ("Python", "PostgreSQL", "Python")]
    quotes = [GROUNDED, "Berlin, hybrid", "Backend Engineer"]
    agreeing = draft_with(
        criteria={"experience_seniority": {"ad_evidence": quotes, "profile_evidence": refs}}
    )
    v = validated(agreeing)  # level 4, the deterministic one: no repair, and still exempt
    assert v.repairs == [] and v.issues == [] and v.failures == []
    repaired, truncated = repair_for_publication(v.draft, v)
    assert len(repaired.criteria.experience_seniority.profile_evidence) == 2  # publication still cuts
    assert truncated == [
        "criteria.experience_seniority.ad_evidence",
        "criteria.experience_seniority.profile_evidence",
    ]
    other = validated(draft_with(criteria={"domain": {"ad_evidence": quotes}}))
    assert other.issues == [Issue("too_many_quotes", "criteria.domain.ad_evidence", {"max_quotes": 2})]


def test_an_unknown_deterministic_seniority_makes_the_rating_unknown() -> None:
    ad = _seniority_ad("Wir suchen Verstärkung.")  # no level, no years, no seniority token in the title
    v = validated(draft_with(criteria={"experience_seniority": {"level": 3}}), ad=ad)
    assert v.draft.criteria.experience_seniority == DraftCriterion(
        level=None,
        basis="unknown",
        ad_evidence=[],
        profile_evidence=[],
        rationale="Die Kernkenntnisse sind belegt.",
    )
    assert v.repairs == [Repair(SENIORITY_PATH, "seniority_recomputed", 3)]  # nothing to exempt
    assert v.failures == []


def test_an_ungrounded_seniority_fact_is_not_used() -> None:
    ad = _seniority_ad("Wir suchen Verstärkung.")
    draft = draft_with(
        {"seniority": {"value": "junior", "evidence": ["Junior-Position"]}},  # not in the ad
        criteria={"experience_seniority": {"level": None, "basis": "unknown", "ad_evidence": []}},
    )
    v = validated(draft, ad=ad)
    assert v.draft.criteria.experience_seniority.level is None  # not rated from a fact that does not ground
    assert v.failures == [Failure("seniority", "quote_ungrounded")]
    assert v.repairs == []


def test_the_title_rates_the_level_when_the_ad_states_none() -> None:
    v = validated(draft_with(criteria={"experience_seniority": {"level": 2}}))  # title "Senior …"
    assert v.draft.criteria.experience_seniority.level == 4
    assert v.draft.criteria.experience_seniority.ad_evidence == []  # no fact quote: no ad evidence
    assert v.repairs == [recomputed(2)]
    assert v.failures == []


def test_a_deterministic_level_with_another_basis_is_corrected() -> None:
    v = validated(draft_with(criteria={"experience_seniority": {"level": 4, "basis": "unconstrained"}}))
    assert v.draft.criteria.experience_seniority.basis == "observed"
    assert v.repairs == [recomputed(4)] and v.failures == []


def test_a_work_mode_that_contradicts_the_structured_one_is_replaced() -> None:
    draft = draft_with({"work_mode": {"value": "remote", "evidence": [GROUNDED]}})
    v = validated(draft)  # the structured work mode is hybrid
    assert v.draft.job_facts.work_mode.value == "hybrid"
    assert v.repairs == [Repair("job_facts.work_mode.value", "work_mode_structured", "remote")]
    several = validated(draft, ctx_for(synthetic_ad(), structured_work_modes=("onsite", "hybrid")))
    assert several.draft.job_facts.work_mode.value == "unspecified"  # no one structured value to take
    agreeing = validated(draft, ctx_for(synthetic_ad(), structured_work_modes=("remote", "hybrid")))
    assert agreeing.repairs == [] and agreeing.draft.job_facts.work_mode.value == "remote"
    none = validated(draft, ctx_for(synthetic_ad(), structured_work_modes=()))
    assert none.repairs == []


@pytest.mark.parametrize(
    ("value", "countries", "repaired"),
    [
        ("other_country", ["DE"], True),
        ("other_country", ["AT"], False),
        ("germany", ["AT"], True),
        ("dach", ["AT", "CH"], True),
        ("dach", ["AT", "DE"], False),
        ("germany", [], False),
        ("europe", ["AT"], False),
    ],
)
def test_a_remote_region_that_contradicts_its_countries_becomes_unspecified(
    value: str, countries: list[str], repaired: bool
) -> None:
    region = {"value": value, "countries": countries, "evidence": [GROUNDED]}
    v = validated(draft_with({"remote_region": region}))
    assert v.draft.job_facts.remote_region.value == ("unspecified" if repaired else value)
    expected = [Repair("job_facts.remote_region.value", "remote_region_contradiction", value)]
    assert v.repairs == (expected if repaired else [])


def test_two_facts_with_one_fact_id_keep_the_first() -> None:
    draft = draft_with(
        {
            "required_languages": [
                language("de", "C1"),
                language("en", mandatory=False),
                language("de", "B1"),
                language("other", other="Französisch"),
                language("other", other="FRANZÖSISCH"),
            ],
            "certifications": [
                {"name": "Scrum Master", "mandatory": True, "evidence": [GROUNDED]},
                {"name": "scrum  master", "mandatory": True, "evidence": [GROUNDED]},
                {"name": "Scrum Master", "mandatory": False, "evidence": [GROUNDED]},
            ],
        },
        criteria={"languages": {"level": 2}},
    )
    v = validated(draft)
    languages = v.draft.job_facts.required_languages
    assert [(x.language, x.level) for x in languages] == [("de", "C1"), ("en", "B2"), ("other", "B2")]
    assert [x.mandatory for x in v.draft.job_facts.certifications] == [True, False]
    assert v.repairs == [
        Repair("job_facts.required_languages[2]", "duplicate_fact_id"),
        Repair("job_facts.required_languages[4]", "duplicate_fact_id"),
        Repair("job_facts.certifications[1]", "duplicate_fact_id"),
    ]
    assert list(v.fact_ids) == [
        "mandatory_language:de",
        "mandatory_language:other:franzosisch",
        "certification:scrum master",
    ]


# ---- V4 consistency ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("languages", "level", "failure"),
    [
        ([language("fr", "B1")], 3, "languages_vs_unmet"),  # the profile has no French
        ([language("fr", "B1")], 2, None),
        ([language("de", "B2")], 1, "languages_vs_met"),  # the profile is native in German
        ([language("de", "B2"), language("en", "C1")], 0, "languages_vs_met"),
        ([language("de", "B2")], 2, None),
        ([language("de", "B2"), language("fr", "B1")], 1, None),  # not every comparison is met
        ([language("en", "B2", mandatory=False)], 1, None),  # no mandatory language
        ([language("other", "B2", other="Klingonisch")], 4, None),  # uncomparable, never unmet
    ],
)
def test_the_languages_rating_against_the_mandatory_language_comparisons(
    languages: list[dict[str, Any]], level: int, failure: str | None
) -> None:
    v = validated(draft_with({"required_languages": languages}, criteria={"languages": {"level": level}}))
    assert v.failures == ([Failure("languages", failure)] if failure else [])


def test_full_skills_while_a_must_have_matches_no_profile_skill() -> None:
    must_haves = [requirement("Python"), requirement("Kubernetes")]
    v = validated(draft_with({"must_haves": must_haves}))
    assert v.failures == [Failure("skills", "skills_vs_must_have")]
    assert validated(draft_with({"must_haves": must_haves}, criteria={"skills": {"level": 3}})).failures == []
    assert validated(draft_with({"must_haves": [requirement("Python 3 Kenntnisse")]})).failures == []


def test_unconstrained_languages_while_the_ad_states_a_language() -> None:
    v = validated(
        draft_with(
            {"required_languages": [language("en", mandatory=False)]},
            criteria={"languages": {"level": 4, "basis": "unconstrained"}},
        )
    )
    assert v.failures == [Failure("languages", "languages_unconstrained_with_requirements")]


def test_not_a_job_ad_with_a_rated_criterion() -> None:
    unknown: dict[str, Any] = {"level": None, "basis": "unknown", "ad_evidence": [], "profile_evidence": []}
    criteria: dict[str, dict[str, Any]] = {key: unknown for key in CRITERIA}
    criteria["role_fit"] = {"level": 2}
    v = validated(
        draft_with(
            {"must_haves": [], "work_mode": {"value": "unspecified", "evidence": []}},
            criteria=criteria,
            not_a_job_ad=True,
        )
    )
    assert v.failures == [Failure("role_fit", "not_a_job_ad_with_levels")]
    assert v.issues == [Issue("not_a_job_ad", "not_a_job_ad", {"required_review": "not_a_job_ad:ad"})]
    assert v.fact_ids == {NOT_A_JOB_AD.fact_id: NOT_A_JOB_AD}
    assert v.draft.criteria.experience_seniority.level is None  # never recomputed for a non-ad


# ---- V5 and V6 --------------------------------------------------------------------------------------------


def test_the_injection_flag_alone_sets_the_suspicion() -> None:
    v = validated(draft_with(injection_observed=True))
    assert v.injection_suspected is True
    assert v.issues == [
        Issue("injection_suspected", "injection_observed", {"patterns": [], "observed": True})
    ]
    assert v.failures == [] and v.t1 is False  # V5 routes through T6, not T1


# ---- repairs at publication -------------------------------------------------------------------------------


def test_repair_for_publication_cuts_a_long_summary_and_keeps_the_first_25_must_haves() -> None:
    summary = " ".join(f"Wort{i}" for i in range(70))
    must_haves = [requirement(f"Python {i}") for i in range(30)]
    draft = draft_with({"must_haves": must_haves}, summary_en=summary)
    v = validated(draft)
    repaired, truncated = repair_for_publication(v.draft, v)
    assert repaired.summary_en.endswith(" […]")
    assert repaired.summary_en.startswith("Wort0 Wort1 ")
    assert len(repaired.summary_en.split()) <= 60 and len(repaired.summary_en) <= 450
    assert repaired.job_facts.must_haves == v.draft.job_facts.must_haves[:25]
    assert truncated == ["summary_en", "job_facts.must_haves"]
    assert repaired.summary_de == draft.summary_de  # within its limits: unchanged


def test_repair_for_publication_cuts_every_limited_string_at_its_limit() -> None:
    long = "Wort " * 100
    draft = draft_with(
        {
            "must_haves": [requirement(long)],
            "industry": {"value": long, "evidence": [GROUNDED]},
            "security_clearance": {"required": False, "level": long, "evidence": [GROUNDED]},
            "required_languages": [language("other", mandatory=False, other=long)],
        },
        criteria={"domain": {"rationale": long}},
    )
    repaired, truncated = repair_for_publication(draft, validated(draft))
    for text, limit in [
        (repaired.job_facts.must_haves[0].text, 200),
        (repaired.job_facts.industry.value, 120),
        (repaired.job_facts.security_clearance.level, 60),
        (repaired.job_facts.required_languages[0].language_other, 40),
        (repaired.criteria.domain.rationale, 200),
    ]:
        assert text is not None and text.endswith("[…]") and len(text) <= limit
    assert len(repaired.criteria.domain.rationale.split()) <= 25
    assert truncated == [
        "criteria.domain.rationale",
        "job_facts.required_languages[0].language_other",
        "job_facts.must_haves[0].text",
        "job_facts.industry.value",
        "job_facts.security_clearance.level",
    ]


def test_repair_for_publication_drops_invalid_quotes_and_records_the_history() -> None:
    ad = _seniority_ad("Mid-Level Entwicklerin.")
    draft = draft_with(
        {
            "seniority": {"value": "mid", "evidence": ["Mid-Level"]},
            "remote_region": {"value": "unspecified", "countries": ["AT", "XX"], "evidence": [GROUNDED]},
            "min_years_experience": {"value": 50, "evidence": [GROUNDED]},
        },
        criteria={
            "domain": {
                "ad_evidence": ["nicht im Text", GROUNDED, "Berlin, hybrid"],
                "profile_evidence": [{"section": "hobbies", "quote": "Python"}],
            }
        },
    )
    v = validated(draft, ad=ad)
    repaired, truncated = repair_for_publication(v.draft, v)
    assert repaired.criteria.domain.ad_evidence == [GROUNDED, "Berlin, hybrid"]
    assert repaired.criteria.domain.profile_evidence == []
    assert repaired.job_facts.remote_region.countries == ["AT"]
    assert repaired.job_facts.min_years_experience.value is None
    assert truncated == [
        "criteria.experience_seniority.level",  # the history's correction, first
        "criteria.domain.ad_evidence",
        "criteria.domain.profile_evidence",
        "job_facts.remote_region.countries",
        "job_facts.min_years_experience.value",
    ]
    # after a re-validation (which repairs nothing) the publication still carries the stored history
    later = validate_draft(v.draft, ctx_for(ad), normalisation=Normalisation(), history=v.history)
    assert later.repairs == [] and later.history == v.history
    assert repair_for_publication(later.draft, later)[1] == truncated


def test_repair_for_publication_keeps_the_first_two_grounded_quotes() -> None:
    quotes = ["nicht im Text", GROUNDED, "Berlin, hybrid", "Backend Engineer"]
    draft = draft_with({"work_mode": {"value": "hybrid", "evidence": quotes}})
    v = validated(draft)
    repaired, truncated = repair_for_publication(v.draft, v)
    assert repaired.job_facts.work_mode.evidence == [GROUNDED, "Berlin, hybrid"]
    assert truncated == ["job_facts.work_mode.evidence"]
    assert v.groundings["job_facts.work_mode.evidence"][:2] == [
        Grounding("description", None, 51, 69),
        Grounding("description", None, 71, 85),
    ]


def test_the_normalisation_and_the_history_round_trip_through_json() -> None:
    history = CorrectionHistory(
        Normalisation(
            issues=[Issue("criterion_count", "criteria.languages", {"count": 0})],
            failures=[Failure("languages", "criterion_missing")],
            repairs=[Repair("job_facts.seniority", "duplicate_fact")],
        ),
        [Repair("job_facts.work_mode.value", "work_mode_structured", "remote")],
    )
    assert CorrectionHistory.from_json(history.to_json()) == history
    with pytest.raises(ValueError, match="normalisation"):
        Normalisation.from_json({"issues": [{"code": 1}], "failures": [], "repairs": []})


def test_the_fact_ids_and_the_draft_block_agree() -> None:
    from jobhunter.llm.prompts.render import draft_block

    draft = draft_with({"required_languages": [language("de"), language("de", "C1")]})
    v = validated(draft)
    assert v.fact_ids == {"mandatory_language:de": fact_ref("mandatory_language", "de")}
    assert '"fact_id":"mandatory_language:de"' in draft_block(v.draft, v.fact_ids)


def test_profile_sections_are_the_canonical_serialisation_of_each_section() -> None:
    from jobhunter.scoring.validator import profile_sections

    sections = profile_sections(score_profile())
    assert list(sections) == [
        "summary",
        "target_titles",
        "roles",
        "skills",
        "languages",
        "education",
        "certifications",
        "domains",
        "seniority",
    ]
    assert sections["domains"] == '["Logistik"]'
    assert sections["seniority"] == '"senior"'
    assert '"name":"Python"' in sections["skills"]
    assert isinstance(score_profile(), ConfirmedProfile)
