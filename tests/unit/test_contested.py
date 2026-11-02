"""The contested null-out and the publication object (spec §11.10 L4549-4561, §11.6 L4380-4394; plan Task 10,
Codex r1 #10, r2 B17, r3 B10, B12; Controller carries T9-a, T9-g, Ruling P24): `contested.null_out` and the
pure `publication.build`, on synthetic drafts validated against the synthetic ad and reviews of them.

The eight rules of L4553-4561, one test each (rule 3 and 3a in two): a criterion with an unresolved V2/V3 or
V4 consistency failure is nulled and listed (1); a fact with failed grounding is not asserted and its knockout
dropped (2); a rejected fact is dropped, an uncertain or unconfirmed one kept as a badge without a cap (3);
`not_a_job_ad` only when verified, else a real ad with the badge (3a); a semantic dispute keeps the validated
level (4); the publication repairs apply and the object is revalidated (5); the interval is recomputed and the
confidence downgraded (6); the drafts are left as they are (7); a contested result is published (8). Then the
overlong quote of Ruling P24, the accepted publication's rejected facts (T9-g), the V6 guard, the critic's
injection column, the evidence and the correction history.

Every datum is invented.
"""

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest
from jobhunter.llm.schemas.critic import CriticOut
from jobhunter.llm.schemas.generator import GeneratorDraft, JobFacts
from jobhunter.scoring import contested, publication
from jobhunter.scoring.contested import null_out
from jobhunter.scoring.facts import FactRef, derive_fact_ids, name_subject
from jobhunter.scoring.knockouts import Confirmation, derive_knockouts, required_facts
from jobhunter.scoring.publication import Publication, PublicationInvalid, Review, build
from jobhunter.scoring.rubric import load_rubric
from jobhunter.scoring.validator import (
    CriticValidation,
    DraftValidation,
    ParsedGenerator,
    Repair,
    apply_fact_confirmations,
    parse_generator,
    validate_draft,
    validate_review,
)

from tests.support.facts import scope
from tests.support.scoring import review_item, synthetic_review
from tests.support.validation import (
    CRITERIA,
    DESCRIPTION,
    criterion_item,
    ctx_for,
    fact_item,
    synthetic_ad,
    synthetic_wire_c,
)

AD = synthetic_ad(
    description=f"{DESCRIPTION} Zertifizierung AWS Solutions Architect erforderlich. Französisch B2 "
    "erwünscht. Kubernetes im Team."
)
CTX = ctx_for(AD)
SCOPE = scope()
WEIGHTS = load_rubric("v2").weights
GROUNDED = "Python, PostgreSQL"
CERTIFICATION = "certification:aws solutions architect"
FRENCH = "mandatory_language:fr"
DEFAULT_FACTS = [
    fact_item("work_mode", "hybrid", "Berlin, hybrid"),
    fact_item("must_have", "Python", GROUNDED),
    fact_item("must_have", "PostgreSQL", GROUNDED),
    fact_item("employment_type", "full_time", "full_time (structured)"),
]
CERTIFIED = fact_item(
    "certification", "AWS Solutions Architect", "AWS Solutions Architect erforderlich", required=True
)
UNGROUNDED_CERTIFIED = fact_item(
    "certification",
    "AWS Solutions Architect",
    "AWS Certified Solutions Architect Professional",
    required=True,
)
FRENCH_FACT = fact_item("required_language", "fr", "Französisch B2", qualifier="B2", required=True)


def validated(wire: dict[str, Any]) -> DraftValidation:
    """`wire` parsed and validated against the synthetic ad (`CTX`)."""
    parsed = parse_generator(wire, family="generator_v2_items")
    assert isinstance(parsed, ParsedGenerator)
    return validate_draft(parsed.draft, CTX, normalisation=parsed.normalisation)


def wire(*, facts: list[dict[str, Any]] | None = None, **criteria: dict[str, Any]) -> dict[str, Any]:
    """The synthetic C answer at level 4 with `facts` and the given criterion items replaced."""
    items = [criteria.get(key, criterion_item(key, level=4)) for key in CRITERIA]
    return synthetic_wire_c(facts=DEFAULT_FACTS if facts is None else facts, criteria=items)


def required_of(validation: DraftValidation) -> list[FactRef]:
    """The facts the critic reviews (`scoring.steps.validate_step`'s rule)."""
    facts = validation.draft.job_facts
    candidates = derive_knockouts(facts, CTX.det, CTX.profile, SCOPE)
    return required_facts(facts, candidates, not_a_job_ad=validation.draft.not_a_job_ad)


def reviewed(
    validation: DraftValidation,
    *items: dict[str, Any],
    injection: str = "none",
    previous: Mapping[str, Confirmation] | None = None,
) -> Review:
    """The critic's review of `validation`'s draft with `items`, validated, and the confirmations after it."""
    out = CriticOut.model_validate(synthetic_review(*items, injection=injection))
    required = required_of(validation)
    checked = validate_review(out, validation=validation, required=required, ctx=CTX)
    confirmations = apply_fact_confirmations(previous or {}, checked, required)
    return Review(validation=checked, injection=out.injection, confirmations=confirmations)


def publish(
    validation: DraftValidation,
    review: Review | None,
    *,
    mode: str = "contested",
    revisions_used: int = 2,
) -> Publication:
    return build(
        draft=validation.draft,
        validation=validation,
        last_review=review,
        mode=mode,  # type: ignore[arg-type]
        scope=SCOPE,
        ctx=CTX,
        weights=WEIGHTS,
        revisions_used=revisions_used,
    )


def checked(review: Review | None) -> CriticValidation | None:
    return None if review is None else review.validation


def levels(draft: GeneratorDraft) -> dict[str, int | None]:
    return {key: getattr(draft.criteria, key).level for key in CRITERIA}


def published_levels(published: Publication) -> dict[str, int | None]:
    return {key: criterion.level for key, criterion in published.result.criteria.by_key().items()}


# ---- the eight rules ------------------------------------------------------------------------------------


def test_rule_1_a_criterion_with_an_unresolved_failure_is_nulled_and_listed() -> None:
    ungrounded = criterion_item("skills", level=4, ad_evidence=("Java und Kotlin",))  # V3
    inconsistent = criterion_item("languages", level=4, basis="unconstrained")  # V4 with a requirement
    v = validated(wire(facts=[*DEFAULT_FACTS, FRENCH_FACT], skills=ungrounded, languages=inconsistent))
    assert {f.key for f in v.failures} >= {"skills", "languages"}
    result = null_out(v.draft, v, None)
    assert result.nulled_criteria == ["skills", "languages"]
    for key in ("skills", "languages"):
        criterion = getattr(result.draft.criteria, key)
        assert (criterion.level, criterion.basis) == (None, "unknown")
    assert levels(result.draft) == {**levels(v.draft), "skills": None, "languages": None}
    published = publish(v, None)
    assert published.result.nulled_criteria == ["skills", "languages"]
    assert published.result.criteria.skills.level is None


def test_rule_2_a_fact_with_failed_grounding_is_not_asserted_and_its_knockout_is_dropped() -> None:
    onsite = fact_item("onsite_only", "", "nur vor Ort", required=True)  # not in the ad
    v = validated(wire(facts=[*DEFAULT_FACTS, UNGROUNDED_CERTIFIED, onsite]))
    assert {f.key for f in v.failures} == {CERTIFICATION, "onsite_required:onsite"}
    result = null_out(v.draft, v, None)
    assert set(result.dropped_facts) == {CERTIFICATION, "onsite_required:onsite"}
    assert result.draft.job_facts.certifications == []
    assert result.draft.job_facts.onsite_only.asserted is False
    assert result.nulled_criteria == []  # a fact's failure nulls no criterion
    published = publish(v, None)
    assert published.result.knockout_candidates == []  # neither binds nor is even a candidate
    assert CERTIFICATION not in published.result.fact_confirmations
    assert all(e.subject not in result.dropped_facts for e in published.evidence)


def test_rule_3_a_rejected_fact_is_dropped_an_uncertain_one_kept_as_a_badge_without_a_cap() -> None:
    v = validated(wire(facts=[*DEFAULT_FACTS, CERTIFIED, FRENCH_FACT]))
    assert [ref.fact_id for ref in required_of(v)] == [CERTIFICATION, FRENCH]  # both unmet: binding-capable
    review = reviewed(
        v,
        review_item(CERTIFICATION, "fact_rejected", GROUNDED),
        review_item(FRENCH, "fact_uncertain", "Französisch B2"),
    )
    result = null_out(v.draft, v, checked(review))
    assert result.dropped_facts == [CERTIFICATION]
    assert result.draft.job_facts.certifications == []
    assert [x.language for x in result.draft.job_facts.required_languages] == ["fr"]
    published = publish(v, review)
    assert published.result.fact_confirmations == {FRENCH: "uncertain"}
    (french,) = published.result.knockout_candidates
    assert (french.fact_id, french.comparison) == (FRENCH, "unmet")
    assert [k["binding"] for k in published.aggregation.knockouts] == [False]  # a badge, no cap
    assert published.aggregation.caps_applied == []


def test_rule_3a_a_verified_not_a_job_ad_is_kept_with_every_criterion_null() -> None:
    unknown = {
        key: criterion_item(key, level=None, basis="unknown", ad_evidence=(), profile_evidence=())
        for key in CRITERIA
    }
    v = validated(synthetic_wire_c(not_a_job_ad=True, facts=[], criteria=list(unknown.values())))
    review = reviewed(v, review_item("not_a_job_ad:ad", "fact_confirmed", "Du entwickelst Services"))
    assert review.validation.not_a_job_ad_verified
    result = null_out(v.draft, v, checked(review))
    assert (result.not_a_job_ad, result.badge_possibly_not_a_job_ad) == (True, False)
    published = publish(v, review)
    assert published.result.not_a_job_ad is True
    assert published_levels(published) == dict.fromkeys(CRITERIA)
    assert (published.aggregation.low, published.aggregation.mid, published.aggregation.high) == (None,) * 3
    assert published.result.fact_confirmations == {"not_a_job_ad:ad": "critic_confirmed"}
    assert published.badge_possibly_not_a_job_ad is False


def test_rule_3a_an_unverified_not_a_job_ad_is_a_real_ad_with_the_badge_and_its_levels() -> None:
    v = validated(synthetic_wire_c(not_a_job_ad=True))
    assert {f.code for f in v.failures} == {"not_a_job_ad_with_levels"}
    review = reviewed(v, review_item("not_a_job_ad:ad", "fact_uncertain", "Du entwickelst Services"))
    result = null_out(v.draft, v, checked(review))
    assert (result.not_a_job_ad, result.badge_possibly_not_a_job_ad) == (False, True)
    assert result.nulled_criteria == []  # its criteria as validated (§11.10 3a)
    assert levels(result.draft) == levels(v.draft) == dict.fromkeys(CRITERIA, 4)
    published = publish(v, review)
    assert published.result.not_a_job_ad is False and published.aggregation.mid is not None


@pytest.mark.parametrize(
    ("assessment", "stored"),
    [
        (None, "unconfirmed"),  # no valid review
        ("fact_uncertain", "uncertain"),
        ("fact_rejected", "critic_rejected"),
        ("chain_confirmed", "unconfirmed"),  # a chain status that says more than the last review verified
    ],
)
def test_rule_3a_the_badge_is_published_with_the_flag_s_unconfirmed_status(
    assessment: str | None, stored: str
) -> None:
    # §11.10 3a: the badge reaches the stored result (M4's list, Task 19's eval runner), no llm_results column
    v = validated(synthetic_wire_c(not_a_job_ad=True))
    review = None
    if assessment == "chain_confirmed":
        uncertain = reviewed(v, review_item("not_a_job_ad:ad", "fact_uncertain", "Du entwickelst Services"))
        review = Review(uncertain.validation, "none", {"not_a_job_ad:ad": "critic_confirmed"})
    elif assessment is not None:
        review = reviewed(v, review_item("not_a_job_ad:ad", assessment, "Du entwickelst Services"))
    published = publish(v, review)
    assert published.badge_possibly_not_a_job_ad is True
    assert published.result.not_a_job_ad is False
    assert published.result.fact_confirmations["not_a_job_ad:ad"] == stored
    assert all(e.subject != "not_a_job_ad:ad" for e in published.evidence)  # evidence of a verified flag only


def test_no_badge_and_no_flag_key_for_a_draft_that_is_an_ad() -> None:
    v = validated(wire())
    for mode in ("contested", "accepted"):
        published = publish(v, None, mode=mode, revisions_used=0)
        assert published.badge_possibly_not_a_job_ad is False
        assert "not_a_job_ad:ad" not in published.result.fact_confirmations


def test_rule_4_a_semantic_dispute_keeps_the_validated_level() -> None:
    v = validated(wire())
    dispute = review_item("role_fit", "level_too_high", "Backend Engineer (m/w/d)", suggested_level=2)
    review = reviewed(v, dispute)
    assert [d.target for d in review.validation.grounded_disputes] == ["role_fit"]
    result = null_out(v.draft, v, checked(review))
    assert result.draft.criteria.role_fit.level == 4 and result.nulled_criteria == []
    assert publish(v, review).result.criteria.role_fit.level == 4  # the suggestion stays in critic_notes


def test_rule_5_the_publication_repairs_apply_and_the_object_is_revalidated() -> None:
    long_summary = " ".join(["Passung"] * 70)  # over 60 words
    doc = wire()
    doc["summary_en"] = long_summary
    v = validated(doc)
    published = publish(v, None)
    assert published.result.summary_en.endswith("[…]") and len(published.result.summary_en.split()) <= 60
    assert "summary_en" in published.result.truncated_fields
    assert "summary_en" in published.truncated_fields
    assert Repair("summary_en", publication.PUBLICATION_CUT) in published.repairs


def test_rule_6_the_interval_is_recomputed_and_the_confidence_downgraded() -> None:
    v = validated(wire(skills=criterion_item("skills", level=4, ad_evidence=("Java und Kotlin",))))
    accepted_like = publish(validated(wire()), None, mode="accepted", revisions_used=0).aggregation
    contested_result = publish(v, None).aggregation
    assert contested_result.high is not None and contested_result.low is not None
    assert contested_result.high - contested_result.low == Decimal(WEIGHTS["skills"]) + (
        (accepted_like.high or 0) - (accepted_like.low or 0)
    )  # the nulled skills widen the interval by their weight
    assert accepted_like.confidence == "high"
    assert contested_result.coverage == accepted_like.coverage - Decimal(WEIGHTS["skills"]) / 100
    assert contested_result.confidence == "low"  # coverage below 0.80 is medium, contested one step down


def test_rule_7_the_validated_draft_is_left_as_it_was() -> None:
    v = validated(wire(facts=[*DEFAULT_FACTS, UNGROUNDED_CERTIFIED]))
    before = v.draft.model_dump(mode="json")
    result = null_out(v.draft, v, None)
    assert result.draft.job_facts.certifications == []
    assert v.draft.model_dump(mode="json") == before  # the draft row's content is never changed here


def test_rule_8_a_contested_result_is_published_never_withheld() -> None:
    v = validated(wire(skills=criterion_item("skills", level=4, ad_evidence=("Java und Kotlin",))))
    published = publish(v, None)
    assert (published.result.status, published.result.critic_outcome) == ("contested", "contested")
    assert published.critic_injection == publication.UNAVAILABLE  # the critic ran, no review was valid


# ---- Ruling P24, T9-g, the V6 guard, the critic's injection --------------------------------------------


def test_an_unresolved_overlong_quote_nulls_its_criterion_on_the_exhausted_revision_path() -> None:
    overlong = "Python, PostgreSQL " + "und mehr " * 40  # over 300 characters: V3's quote_too_long
    assert len(overlong) > 300
    v = validated(wire(domain=criterion_item("domain", level=4, ad_evidence=(overlong,))))
    assert ("domain", "quote_too_long") in {(f.key, f.code) for f in v.failures}
    published = publish(v, None, revisions_used=2)  # after the second revision: contested
    assert published.result.nulled_criteria == ["domain"]
    assert published.result.criteria.domain.level is None
    assert published.result.criteria.domain.ad_evidence == []  # the quote is dropped as evidence
    assert all(len(e.quote) <= 300 for e in published.evidence)


def test_an_accepted_publication_drops_a_rejected_fact_and_its_knockout() -> None:
    v = validated(wire(facts=[*DEFAULT_FACTS, UNGROUNDED_CERTIFIED]))
    review = reviewed(v, review_item(CERTIFICATION, "fact_rejected", GROUNDED))
    assert review.validation.derived_outcome == "accept"  # the rejection resolves its failure (T9-f)
    published = publish(v, review, mode="accepted", revisions_used=0)
    assert published.result.status == "accepted" and published.result.critic_outcome == "accepted"
    assert published.result.job_facts.certifications == []
    assert published.result.knockout_candidates == []
    assert published.aggregation.knockouts == [] and published.aggregation.caps_applied == []
    assert CERTIFICATION not in published.result.fact_confirmations
    assert all(e.subject != CERTIFICATION for e in published.evidence)  # the review's quote too


def test_an_unverified_not_a_job_ad_reaching_accepted_is_a_bug() -> None:
    v = validated(synthetic_wire_c(not_a_job_ad=True))
    with pytest.raises(PublicationInvalid) as raised:
        publish(v, None, mode="accepted", revisions_used=0)
    assert raised.value.error_types == ("not_a_job_ad_unverified",)


def test_a_failure_of_the_strict_model_is_a_bug_that_names_types_and_field_paths_only() -> None:
    # a verified not-a-job-ad whose criteria are rated reaches `accepted` (the routing cannot produce it)
    v = validated(synthetic_wire_c(not_a_job_ad=True))
    review = reviewed(v, review_item("not_a_job_ad:ad", "fact_confirmed", "Du entwickelst Services"))
    with pytest.raises(PublicationInvalid) as raised:
        publish(v, review, mode="accepted", revisions_used=0)
    assert raised.value.error_types == ("value_error",)
    text = str(raised.value)
    assert "Python" not in text and "Services" not in text and "Passung" not in text
    assert raised.value.__cause__ is None and raised.value.__context__ is None


@pytest.mark.parametrize(
    ("mode", "review", "outcome", "column"),
    [
        ("accepted", None, "not_run", None),
        ("accepted", "present_no_influence", "accepted", "present_no_influence"),
        ("contested", "none", "contested", "none"),
        ("contested", None, "contested", "unavailable"),
    ],
)
def test_the_critic_s_outcome_and_injection_follow_the_last_valid_review(
    mode: str, review: str | None, outcome: str, column: str | None
) -> None:
    v = validated(wire())
    last = None if review is None else reviewed(v, injection=review)
    published = publish(v, last, mode=mode, revisions_used=0)
    assert (published.result.critic_outcome, published.critic_injection) == (outcome, column)


def test_an_injection_suspected_contested_result_without_a_valid_review_is_low() -> None:
    v = validated(synthetic_wire_c(injection_observed=True))
    assert v.injection_suspected
    published = publish(v, None)
    assert published.result.injection_suspected is True
    assert (published.critic_injection, published.aggregation.confidence) == ("unavailable", "low")


# ---- the evidence and the history ------------------------------------------------------------------------


def test_the_evidence_is_the_final_draft_s_and_the_review_s_groundings() -> None:
    v = validated(wire(facts=[*DEFAULT_FACTS, CERTIFIED]))
    confirmation = "Zertifizierung AWS Solutions Architect"  # the critic's own quote, not the fact's
    review = reviewed(v, review_item(CERTIFICATION, "fact_confirmed", confirmation, "Python, PostgreSQL"))
    published = publish(v, review, mode="accepted", revisions_used=0)
    rows = {
        (e.subject, e.source_kind, e.field_name, e.quote, e.span_start, e.span_end)
        for e in published.evidence
    }
    start = AD.description_text.index(GROUNDED)
    assert ("skills", "description", None, GROUNDED, start, start + len(GROUNDED)) in rows
    assert ("skills", "profile", "skills", "Python", None, None) in rows
    structured = ("employment_type", "structured_field", "employment_types", "full_time (structured)")
    assert (*structured, None, None) in rows
    spans = {
        quote: (AD.description_text.index(quote), AD.description_text.index(quote) + len(quote))
        for quote in ("AWS Solutions Architect erforderlich", confirmation, GROUNDED)
    }
    assert sorted(
        (e.quote, e.span_start, e.span_end) for e in published.evidence if e.subject == CERTIFICATION
    ) == [
        (quote, *spans[quote]) for quote in sorted(spans)
    ]  # the fact's quote and the review's two, each once
    assert all(4 <= len(e.quote) <= 300 for e in published.evidence)
    assert len(published.evidence) == len(set(published.evidence))


def test_a_removed_list_fact_leaves_the_kept_items_at_their_published_places() -> None:
    long_text = "PostgreSQL " * 25  # over 200 characters: cut at publication
    facts = [
        fact_item("must_have", "Rust", "Rust und Go"),  # ungrounded: removed
        fact_item("must_have", long_text.strip(), GROUNDED),
        *DEFAULT_FACTS[:1],
    ]
    v = validated(wire(facts=facts))
    assert "must_have:0" in {f.key for f in v.failures}
    published = publish(v, None)
    assert [m.text.endswith("[…]") for m in published.result.job_facts.must_haves] == [True]
    assert "job_facts.must_haves[0].text" in published.truncated_fields  # the kept item's new place
    assert not any(path.startswith("job_facts.must_haves[1]") for path in published.truncated_fields)


def test_a_dropped_fact_takes_its_correction_out_of_the_truncated_fields() -> None:
    # the generator's work mode contradicts the structured one (V4 corrects it) and its quote fails (V3)
    v = validated(wire(facts=[fact_item("work_mode", "remote", "nur remote"), *DEFAULT_FACTS[1:]]))
    assert [r.path for r in v.history.corrections] == ["job_facts.work_mode.value"]
    assert "work_mode" in {f.key for f in v.failures}
    published = publish(v, None)
    assert published.result.job_facts.work_mode.value == "unspecified"  # not asserted
    assert not any(path.startswith("job_facts.work_mode") for path in published.truncated_fields)
    assert all(r.path != "job_facts.work_mode.value" for r in published.repairs)
    kept = publish(v, None, mode="accepted", revisions_used=0)  # the same draft accepted keeps the fact
    assert kept.truncated_fields == ["job_facts.work_mode.value", "job_facts.work_mode.evidence"]


def test_the_dropped_places_move_the_later_items_up() -> None:
    dropped = contested.Dropped(
        frozenset({"job_facts.work_mode"}),
        {"must_haves": frozenset({0, 2}), "certifications": frozenset({1})},
    )
    assert dropped.path("job_facts.must_haves[3].evidence[1]") == "job_facts.must_haves[1].evidence[1]"
    assert dropped.path("job_facts.must_haves[1].text") == "job_facts.must_haves[0].text"
    assert dropped.path("job_facts.must_haves[2].text") is None  # removed
    assert dropped.path("job_facts.certifications[0].name") == "job_facts.certifications[0].name"
    assert dropped.path("job_facts.certifications[4]") == "job_facts.certifications[3]"
    assert dropped.path("job_facts.nice_to_haves[2].text") == "job_facts.nice_to_haves[2].text"
    assert dropped.path("job_facts.work_mode.value") is None and dropped.path("job_facts.work_mode") is None
    assert dropped.path("job_facts.work_modes") == "job_facts.work_modes"  # another name, not under it
    assert dropped.path("summary_en") == "summary_en"


def test_the_truncated_fields_and_repairs_start_from_the_correction_history() -> None:
    corrected = criterion_item("experience_seniority", level=2)  # V4 recomputes the deterministic 4
    v = validated(wire(experience_seniority=corrected))
    (repair,) = v.history.corrections
    assert (repair.path, repair.code) == ("criteria.experience_seniority.level", "seniority_recomputed")
    # a re-validation of the corrected draft with its stored history repairs nothing new
    again = validate_draft(v.draft, CTX, normalisation=v.history.normalisation, history=v.history)
    assert again.history == v.history and again.repairs == []
    first, later = publish(v, None, mode="accepted"), publish(again, None, mode="accepted")
    assert first.truncated_fields == later.truncated_fields == ["criteria.experience_seniority.level"]
    assert first.repairs == later.repairs == [repair]
    assert first.result.truncated_fields == first.truncated_fields


def test_null_out_needs_a_review_only_for_rejections_and_the_flag() -> None:
    v = validated(wire())
    result = null_out(v.draft, v, None)
    assert (result.nulled_criteria, result.dropped_facts) == ([], [])
    assert result.draft == v.draft
    assert contested.rejected_facts(None) == []


# ---- a fact's review across the publication's repairs (final review, Codex I2) ----------------------------

# A mandatory certification over its 120-character limit: publication cuts it to 116 characters and `[…]`,
# which gives the fact another id than the one the critic reviewed (`facts.name_subject`).
LONG_PREFIX = "AWS Solutions Architect " + "mit Praxis in Cloud-Sicherheit und Netzwerken, " * 2
LONG_NAME = f"{LONG_PREFIX}inklusive Nachweis der Rezertifizierung"
OTHER_LANGUAGE = "Plattdeutsch aus dem nordlichen Emsland, fliessend"  # over the 40 of `language_other`
CONFIRMING_QUOTE = "AWS Solutions Architect erforderlich"


def long_certification(name: str = LONG_NAME) -> dict[str, Any]:
    return fact_item("certification", name, CONFIRMING_QUOTE, required=True)


def name_fact_id(code: str, name: str) -> str:
    return f"{code}:{name_subject(name)}"


def published_fact_id(code: str, published: Publication) -> str:
    """The one fact id of `code` the published facts hold (`facts.derive_fact_ids` over them, read back as
    `det_results` reads the stored facts)."""
    stored = JobFacts.model_validate(published.result.job_facts.model_dump(mode="json"))
    (found,) = [name for name in derive_fact_ids(stored) if name.startswith(f"{code}:")]
    return found


def test_a_confirmed_overlong_certification_keeps_its_confirmation_and_its_penalty() -> None:
    v = validated(wire(facts=[*DEFAULT_FACTS, long_certification()]))
    reviewed_id = name_fact_id("certification", LONG_NAME)
    assert [ref.fact_id for ref in required_of(v)] == [reviewed_id]  # unmet: the profile lacks it
    review = reviewed(v, review_item(reviewed_id, "fact_confirmed", CONFIRMING_QUOTE))
    published = publish(v, review, mode="accepted", revisions_used=0)
    (certification,) = published.result.job_facts.certifications
    assert certification.name.endswith("[…]") and len(certification.name) == 120  # the publication's cut
    repaired_id = published_fact_id("certification", published)
    assert repaired_id != reviewed_id
    assert published.result.fact_confirmations == {repaired_id: "critic_confirmed"}
    (candidate,) = published.result.knockout_candidates
    assert (candidate.fact_id, candidate.comparison, candidate.severity) == (repaired_id, "unmet", "soft")
    assert [k["binding"] for k in published.aggregation.knockouts] == [True]
    without = publish(validated(wire()), None, mode="accepted", revisions_used=0)
    assert without.aggregation.mid is not None and published.aggregation.mid is not None
    assert published.aggregation.mid == without.aggregation.mid - 10  # the soft penalty, not lost
    # the critic's quote is the evidence of the fact under its published id
    assert ("AWS Solutions Architect erforderlich", repaired_id) in {
        (e.quote, e.subject) for e in published.evidence
    }
    assert all(e.subject != reviewed_id for e in published.evidence)


def test_a_confirmed_overlong_other_language_keeps_its_confirmation() -> None:
    other = fact_item(
        "required_language", f"other:{OTHER_LANGUAGE}", "Französisch B2", qualifier="B2", required=True
    )
    v = validated(wire(facts=[*DEFAULT_FACTS, other]))
    reviewed_id = f"mandatory_language:other:{name_subject(OTHER_LANGUAGE)}"
    assert [ref.fact_id for ref in required_of(v)] == [reviewed_id]  # uncomparable: binding-capable
    review = reviewed(v, review_item(reviewed_id, "fact_confirmed", "Französisch B2"))
    published = publish(v, review, mode="contested")
    (language,) = published.result.job_facts.required_languages
    assert language.language_other is not None and language.language_other.endswith("[…]")
    repaired_id = published_fact_id("mandatory_language", published)
    assert repaired_id != reviewed_id
    assert published.result.fact_confirmations == {repaired_id: "critic_confirmed"}
    (candidate,) = published.result.knockout_candidates
    assert (candidate.fact_id, candidate.comparison) == (repaired_id, "uncomparable")


@pytest.mark.parametrize(
    ("second", "expected"),
    [("fact_confirmed", "critic_confirmed"), ("fact_uncertain", "unconfirmed")],
    ids=["both_confirmed", "one_uncertain"],
)
def test_two_certifications_cut_to_one_id_share_a_confirmation_only_when_both_have_it(
    second: str, expected: str
) -> None:
    shared = LONG_PREFIX[:100] + "x" * (116 - 100)  # the 116 characters the cut keeps
    first_name, second_name = f"{shared}Alpha-Stufe mit Nachweis", f"{shared}Beta-Stufe mit Nachweis"
    v = validated(
        wire(facts=[*DEFAULT_FACTS, long_certification(first_name), long_certification(second_name)])
    )
    first_id, second_id = (
        name_fact_id("certification", first_name),
        name_fact_id("certification", second_name),
    )
    assert [ref.fact_id for ref in required_of(v)] == sorted([first_id, second_id])  # two ids reviewed
    review = reviewed(
        v,
        review_item(first_id, "fact_confirmed", CONFIRMING_QUOTE),
        review_item(second_id, second, CONFIRMING_QUOTE),
    )
    published = publish(v, review, mode="contested")
    assert len(published.result.job_facts.certifications) == 2  # both published, cut to one name
    repaired_id = published_fact_id("certification", published)
    assert published.result.fact_confirmations == {repaired_id: expected}
    (candidate,) = published.result.knockout_candidates  # one candidate per fact id
    assert candidate.comparison == "unmet"
    assert [k["binding"] for k in published.aggregation.knockouts] == [expected == "critic_confirmed"]
