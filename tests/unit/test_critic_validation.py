"""The critic review validation and its derived outcome (spec §11.7.2 L4416-4430, §11.7.3 L4463-4464, V6
L4365; M3 Task 5).

- `accept` iff the review is complete, every required fact is confirmed or rejected with a grounded ad quote,
  no grounded dispute remains, the injection did not influence the draft, and the draft has no unresolved
  failure; each condition failing alone gives `revise`, except completeness, which gives `incomplete`.
- A review without a grounded quote is discarded: a discarded dispute never blocks, a discarded fact review
  leaves its fact `uncertain`, which does.
- `not_a_job_ad:ad` is special-cased: rejected gives `revise` whatever else holds, uncertain or missing
  `incomplete`, and only a grounded confirmation verifies it.
- The critic's limits are issues; disputes beyond six are ignored, required fact reviews never.
- `apply_fact_confirmations` takes the last round's reviews; a fact never reviewed stays `unconfirmed`.

Every datum is invented.
"""

from typing import Any

from jobhunter.llm.schemas.critic import CriticOut, Review
from jobhunter.llm.schemas.generator import GeneratorDraft
from jobhunter.scoring.facts import NOT_A_JOB_AD, FactRef, fact_ref
from jobhunter.scoring.knockouts import Confirmation
from jobhunter.scoring.validator import (
    CriticValidation,
    Dispute,
    Failure,
    Grounding,
    Issue,
    ParsedGenerator,
    apply_fact_confirmations,
    parse_generator,
    validate_review,
)

from tests.support.validation import CRITERIA, ctx_for, synthetic_ad, synthetic_wire_c

GROUNDED = "Python, PostgreSQL"
FRENCH: FactRef = fact_ref("mandatory_language", "fr")
PERMIT: FactRef = fact_ref("work_permit", "work_permit")
CTX = ctx_for(synthetic_ad())


def draft(*, not_a_job_ad: bool = False) -> GeneratorDraft:
    parsed = parse_generator(synthetic_wire_c(not_a_job_ad=not_a_job_ad), family="generator_v2_items")
    assert isinstance(parsed, ParsedGenerator)
    return parsed.draft


def review(
    target: str,
    assessment: str,
    *quotes: str,
    suggested_level: int | None = None,
    explanation: str = "As the ad states.",
) -> dict[str, Any]:
    return {
        "target": target,
        "assessment": assessment,
        "suggested_level": suggested_level,
        "ad_evidence": list(quotes),
        "explanation": explanation,
    }


def critic(
    *reviews: dict[str, Any], verdict: str = "accept", injection: str = "none", summary: str = "Reviewed."
) -> CriticOut:
    return CriticOut.model_validate(
        {"verdict": verdict, "reviews": list(reviews), "injection": injection, "summary": summary}
    )


def validate(
    out: CriticOut,
    *,
    required: list[FactRef] | None = None,
    failures: list[Failure] | None = None,
    not_a_job_ad: bool = False,
) -> CriticValidation:
    return validate_review(
        out,
        draft=draft(not_a_job_ad=not_a_job_ad),
        required=[FRENCH, PERMIT] if required is None else required,
        ctx=CTX,
        draft_failures=failures or [],
    )


CONFIRM_FRENCH = review(FRENCH.fact_id, "fact_confirmed", GROUNDED)
REJECT_PERMIT = review(PERMIT.fact_id, "fact_rejected", "Berlin, hybrid")


def test_a_complete_confirmed_review_with_nothing_disputed_is_accepted() -> None:
    v = validate(critic(CONFIRM_FRENCH, REJECT_PERMIT))
    assert v.derived_outcome == "accept"
    assert v.complete is True and v.verdict_mismatch is False
    assert v.fact_reviews == {FRENCH.fact_id: "critic_confirmed", PERMIT.fact_id: "critic_rejected"}
    assert v.grounded_disputes == [] and v.discarded == [] and v.issues == []
    assert v.not_a_job_ad_verified is False


def test_condition_1_an_incomplete_review_is_incomplete() -> None:
    unknown_target = review("salary", "level_too_high", GROUNDED, suggested_level=2)
    v = validate(critic(CONFIRM_FRENCH, REJECT_PERMIT, unknown_target))
    assert v.derived_outcome == "incomplete" and v.complete is False
    assert v.issues == [Issue("invalid_target", "reviews[2]", {})]
    wrong_kind = review("skills", "fact_confirmed", GROUNDED)  # a criterion with a fact assessment
    assert validate(critic(CONFIRM_FRENCH, REJECT_PERMIT, wrong_kind)).derived_outcome == "incomplete"
    not_required = review("degree:degree", "fact_confirmed", GROUNDED)
    assert validate(critic(CONFIRM_FRENCH, REJECT_PERMIT, not_required)).derived_outcome == "incomplete"
    twice = validate(critic(CONFIRM_FRENCH, REJECT_PERMIT, CONFIRM_FRENCH))
    assert twice.derived_outcome == "incomplete"
    assert twice.issues == [Issue("duplicate_target", "reviews[2]", {"target": FRENCH.fact_id})]
    assert twice.fact_reviews[FRENCH.fact_id] == "uncertain"  # two reviews of one fact settle nothing


def test_condition_2_a_fact_neither_confirmed_nor_rejected_is_revise() -> None:
    uncertain = review(PERMIT.fact_id, "fact_uncertain", "Berlin, hybrid")
    v = validate(critic(CONFIRM_FRENCH, uncertain))
    assert v.derived_outcome == "revise" and v.complete is True
    assert v.fact_reviews[PERMIT.fact_id] == "uncertain"


def test_condition_3_a_grounded_dispute_is_revise() -> None:
    dispute = review("skills", "level_too_high", GROUNDED, suggested_level=2, explanation="Only partly.")
    v = validate(critic(CONFIRM_FRENCH, REJECT_PERMIT, dispute))
    assert v.derived_outcome == "revise"
    assert v.grounded_disputes == [
        Dispute(
            target="skills",
            assessment="level_too_high",
            suggested_level=2,
            groundings=[Grounding("description", None, 51, 69)],
            explanation="Only partly.",
        )
    ]


def test_condition_4_an_influencing_injection_is_revise() -> None:
    v = validate(critic(CONFIRM_FRENCH, REJECT_PERMIT, injection="present_influenced"))
    assert v.derived_outcome == "revise"
    noticed = validate(critic(CONFIRM_FRENCH, REJECT_PERMIT, injection="present_no_influence"))
    assert noticed.derived_outcome == "accept"


def test_condition_5_an_unresolved_draft_failure_is_revise() -> None:
    v = validate(critic(CONFIRM_FRENCH, REJECT_PERMIT), failures=[Failure("skills", "quote_ungrounded")])
    assert v.derived_outcome == "revise" and v.complete is True


def test_a_missing_required_review_is_incomplete_and_counts_as_uncertain() -> None:
    v = validate(critic(CONFIRM_FRENCH))
    assert v.derived_outcome == "incomplete" and v.complete is False
    assert v.fact_reviews == {FRENCH.fact_id: "critic_confirmed", PERMIT.fact_id: "uncertain"}
    assert v.issues == [Issue("review_missing", "reviews", {"target": PERMIT.fact_id})]


def test_a_discarded_fact_review_leaves_the_fact_uncertain_and_blocks() -> None:
    ungrounded = review(PERMIT.fact_id, "fact_rejected", "nicht im Text")
    v = validate(critic(CONFIRM_FRENCH, ungrounded))
    assert v.discarded == [Issue("review_ungrounded", "reviews[1]", {"target": PERMIT.fact_id})]
    assert v.fact_reviews[PERMIT.fact_id] == "uncertain"
    assert v.complete is True and v.derived_outcome == "revise"
    assert Issue("quote_ungrounded", "reviews[1].ad_evidence[0]", {}) in v.issues


def test_a_discarded_dispute_does_not_block() -> None:
    ungrounded = review("domain", "level_too_low", "Java und Kotlin", suggested_level=4)
    no_quote = review("skills", "should_be_unknown")
    v = validate(critic(CONFIRM_FRENCH, REJECT_PERMIT, ungrounded, no_quote))
    assert v.derived_outcome == "accept"
    assert v.grounded_disputes == []
    assert v.discarded == [
        Issue("review_ungrounded", "reviews[2]", {"target": "domain"}),
        Issue("review_ungrounded", "reviews[3]", {"target": "skills"}),
    ]


def test_a_returned_accept_with_a_derived_revise_is_a_verdict_mismatch() -> None:
    dispute = review("skills", "level_too_high", GROUNDED, suggested_level=2)
    assert validate(critic(CONFIRM_FRENCH, REJECT_PERMIT, dispute, verdict="accept")).verdict_mismatch is True
    assert (
        validate(critic(CONFIRM_FRENCH, REJECT_PERMIT, dispute, verdict="revise")).verdict_mismatch is False
    )
    assert validate(critic(CONFIRM_FRENCH, REJECT_PERMIT, verdict="revise")).verdict_mismatch is True
    assert validate(critic(CONFIRM_FRENCH, verdict="revise")).verdict_mismatch is False  # incomplete


def _not_a_job_ad(assessment: str | None, *quotes: str, **kw: Any) -> CriticValidation:
    reviews = [] if assessment is None else [review(NOT_A_JOB_AD.fact_id, assessment, *quotes)]
    return validate(critic(*reviews, **kw), required=[NOT_A_JOB_AD], not_a_job_ad=True)


def test_a_grounded_confirmation_verifies_not_a_job_ad() -> None:
    v = _not_a_job_ad("fact_confirmed", GROUNDED)
    assert v.derived_outcome == "accept" and v.not_a_job_ad_verified is True


def test_a_rejected_not_a_job_ad_is_revise_whatever_else_holds() -> None:
    v = _not_a_job_ad("fact_rejected", GROUNDED, injection="present_influenced")
    assert v.derived_outcome == "revise" and v.not_a_job_ad_verified is False
    incomplete = validate(
        critic(review(NOT_A_JOB_AD.fact_id, "fact_rejected", GROUNDED), review("salary", "level_too_high")),
        required=[NOT_A_JOB_AD],
        not_a_job_ad=True,
    )
    assert incomplete.complete is False and incomplete.derived_outcome == "revise"


def test_an_uncertain_or_missing_not_a_job_ad_review_is_incomplete() -> None:
    assert _not_a_job_ad("fact_uncertain", GROUNDED).derived_outcome == "incomplete"
    assert _not_a_job_ad(None).derived_outcome == "incomplete"
    ungrounded = _not_a_job_ad("fact_confirmed", "nicht im Text")  # discarded: uncertain
    assert ungrounded.derived_outcome == "incomplete" and ungrounded.not_a_job_ad_verified is False
    # the draft's flag requires the review even when the caller's list leaves it out
    v = validate(critic(), required=[], not_a_job_ad=True)
    assert v.derived_outcome == "incomplete"
    assert v.issues == [Issue("review_missing", "reviews", {"target": NOT_A_JOB_AD.fact_id})]


def test_the_critic_limits_are_issues() -> None:
    long_explanation = review(FRENCH.fact_id, "fact_confirmed", GROUNDED, explanation=" ".join(["Wort"] * 41))
    wide = review(
        PERMIT.fact_id, "fact_rejected", "x" * 301, "Berlin, hybrid", GROUNDED, explanation="y" * 301
    )
    v = validate(critic(long_explanation, wide, summary=" ".join(["Wort"] * 61)))
    assert v.issues == [
        Issue("too_many_words", "summary", {"max_words": 60}),
        Issue("too_many_words", "reviews[0].explanation", {"max_words": 40}),
        Issue("too_many_chars", "reviews[1].explanation", {"max_chars": 300}),
        Issue("too_many_quotes", "reviews[1].ad_evidence", {"max_quotes": 2}),
        Issue("quote_too_long", "reviews[1].ad_evidence[0]", {}),
    ]
    assert v.derived_outcome == "accept"  # the limits alone never block


def test_disputes_beyond_six_are_ignored_and_required_fact_reviews_never_are() -> None:
    disputes = [review(key, "level_too_high", GROUNDED, suggested_level=2) for key in CRITERIA]
    extra = review("skills", "level_too_low", GROUNDED, suggested_level=4)
    v = validate(critic(*disputes, extra, CONFIRM_FRENCH, REJECT_PERMIT, verdict="revise"))
    assert [d.target for d in v.grounded_disputes] == list(CRITERIA)
    assert v.issues == [Issue("too_many_disputes", "reviews[6]", {"max_disputes": 6})]
    assert v.complete is True  # the ignored seventh is no duplicate
    assert v.fact_reviews == {FRENCH.fact_id: "critic_confirmed", PERMIT.fact_id: "critic_rejected"}
    assert v.derived_outcome == "revise"


def test_apply_fact_confirmations_takes_the_last_round() -> None:
    onsite = fact_ref("onsite_required", "onsite", "structured")
    previous: dict[str, Confirmation] = {
        FRENCH.fact_id: "critic_rejected",
        PERMIT.fact_id: "critic_confirmed",
        "degree:degree": "unconfirmed",
        "certification:scrum master": "critic_confirmed",
    }
    last = validate(critic(CONFIRM_FRENCH))  # the permit's review is missing: uncertain
    confirmations = apply_fact_confirmations(previous, last, [FRENCH, PERMIT, onsite])
    assert confirmations == {
        FRENCH.fact_id: "critic_confirmed",
        PERMIT.fact_id: "uncertain",
        "degree:degree": "unconfirmed",  # never reviewed: stays unconfirmed
        "certification:scrum master": "critic_confirmed",  # not reviewed in the last round: kept
        onsite.fact_id: "structured",
    }
    assert previous[FRENCH.fact_id] == "critic_rejected"  # the input is not changed


def test_a_review_needs_only_one_grounded_quote_and_keeps_the_first_two() -> None:
    dispute = review(
        "skills", "level_too_high", "nicht im Text", GROUNDED, "Berlin, hybrid", suggested_level=2
    )
    v = validate(critic(CONFIRM_FRENCH, REJECT_PERMIT, dispute, verdict="revise"))
    assert v.grounded_disputes[0].groundings == [
        Grounding("description", None, 51, 69),
        Grounding("description", None, 71, 85),
    ]
    assert Review.model_validate(dispute).ad_evidence[0] == "nicht im Text"
