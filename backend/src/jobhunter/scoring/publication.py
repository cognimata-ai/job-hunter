"""The publication object (spec §9.4.1 step 3, §11.6 L4380-4394, §11.10; plan Task 10, Codex r1 #10, r2 B17,
B18, r3 B10, B17, Controller carries T6-a, T6-b, T6-e, T9-g): `build`, the one pure function every publication
goes through. No connection, no clock.

The online publish and the batch `published` decision call it inside their persistence transaction
(`scoring.publish.publish_result`); the eval runner of Task 19 calls it alone and writes its case result from
the `Publication`, touching no target, result or ranking row (L4611).

`build(draft=…, validation=…, last_review=…, mode=…, scope=…, ctx=…, weights=…, revisions_used=…)`, in order:
1. **the facts and criteria**: for `mode = "contested"` the null-out (`contested.null_out`, §11.10 items 1-4);
   for `accepted` the facts the last review rejected are dropped with their quotes all the same (L4555 item
   3: under Ruling T9-f an accepted draft can carry the failures of a fact the review rejected, carry T9-g),
   and **the V6 guard** holds (Codex r1 #10): a draft that says it is no job ad reaches `accepted` only
   verified (a `fact_confirmed` review of `not_a_job_ad:ad` with a grounded quote), else it is a `Bug`
   (`PublicationInvalid`: the routing cannot produce it); in `contested` an unverified flag is published as
   a real ad with the badge (`contested.Contested`);
2. **the repairs** (`validator.repair_for_publication`, item 5), over the validation's issues and history
   moved to the published draft's places (`contested.Dropped.path`): `truncated_fields` and `repairs` start
   from `validation.history` (the normaliser's and the first validation's corrections, immutable) and add
   the publication's cuts (`PUBLICATION_CUT`), so a result published after a reclaim lists the same
   truncations as one published from the first validation (Codex r3 B10);
3. **the publication object**: `PublishedResult.model_validate` of the repaired draft with the status, the
   nulled criteria, the truncations, the facts' confirmations (each fact id of the published facts, and
   `not_a_job_ad:ad` when it is published, with the last review's confirmation after its round, else
   `unconfirmed`), the knockout candidates under `scope`, the critic's outcome (`contested`; else `accepted`
   when a review exists, `not_run` without one) and `revisions_used`. A failure is a `Bug`:
   `PublicationInvalid`, which names the error types and the paths of field names only, never a value (the
   `ValidationError`'s text holds the input: carry T6-b), and is raised outside the handler, so neither its
   `__cause__` nor its `__context__` holds it;
4. **the aggregation** under `scope` (item 6: the interval recomputed, the confidence downgraded for a
   contested result), from the published object as `det_results.compute_and_write` rebuilds it from the
   stored result: its nulled levels, its grounded region and industry, its confirmations, `not_a_job_ad`
   (verified only);
5. **`critic_injection`**, the `llm_results` column the strict model does not hold (carries T6-a, T6-f): the
   last valid review's assessment, `unavailable` when the critic ran and no review of it was valid (a
   contested result without one, L3557-3558), `None` when the critic did not run (`not_run`), as the CHECK
   `(critic_injection IS NULL) = (critic_outcome = 'not_run')` reads;
6. **the evidence** (`result_evidence`): every quote of the published draft with its grounding (`ctx`, as V3
   grounds it: a description span, a structured field or a profile section), its subject the criterion key
   or the fact's key (its fact id, else its pseudo key), then the last review's grounded quotes of a subject
   the result still holds (a criterion, a published fact, a verified `not_a_job_ad:ad`), each row once and
   each quote of 4 to 300 characters (the table's CHECK).

`ctx` is the scoring's `ValidationContext` (the ad, the profile with its sections, the job's `DetFacts` under
the lock the caller holds, the description's completeness) and `weights` the rubric's: the brief's `det`,
`profile` and `policy`.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final, Literal

from pydantic import ValidationError

from jobhunter.llm.schemas.generator import GeneratorDraft
from jobhunter.llm.schemas.published import PublishedResult
from jobhunter.run_settings import ScopeSettings
from jobhunter.scoring import contested
from jobhunter.scoring.aggregate import Aggregation, aggregate, industry_match
from jobhunter.scoring.det_criteria import employment_salary, location_work_mode
from jobhunter.scoring.facts import NOT_A_JOB_AD, derive_fact_ids
from jobhunter.scoring.knockouts import Confirmation, KnockoutCandidate, derive_knockouts
from jobhunter.scoring.rubric import LLM_CRITERIA
from jobhunter.scoring.validator import (
    QUOTE_MAX_CHARS,
    QUOTE_MIN_CHARS,
    CorrectionHistory,
    CriticValidation,
    DraftValidation,
    Grounding,
    Repair,
    ValidationContext,
    fact_places,
    ground_quote,
    repair_for_publication,
)
from jobhunter.types import JSONValue

PublishMode = Literal["accepted", "contested"]
CriticInjection = Literal["none", "present_no_influence", "present_influenced", "unavailable"]
UNAVAILABLE: Final = "unavailable"
PUBLICATION_CUT: Final = "publication_cut"  # the code of a repair publication made (`repairs`)
NOT_A_JOB_AD_UNVERIFIED: Final = "not_a_job_ad_unverified"
_UNCONFIRMED: Final = "unconfirmed"
_FIELD_NAME: Final = re.compile(r"[a-z_]{1,64}")
_KEY: Final = "<key>"


class PublicationInvalid(Exception):
    """The publication object failed the strict model or the V6 guard (module docstring, step 3): a `Bug`
    (`failed_invalid_output('publication_invalid')` and the critical `scoring.publication_invalid`, Task 10).
    `error_types` and `locations` are pydantic's error types and the paths of field names (a mapping key or
    any other value is `<key>`): never a value."""

    def __init__(self, error_types: Sequence[str], locations: Sequence[str]) -> None:
        self.error_types = tuple(error_types)
        self.locations = tuple(locations)
        super().__init__(f"the publication object is invalid: {', '.join(self.error_types)}")

    @classmethod
    def of(cls, error: ValidationError) -> "PublicationInvalid":
        types, places = [], []
        for item in error.errors(include_url=False, include_input=False, include_context=False):
            types.append(str(item["type"]))
            parts = [
                str(part) if isinstance(part, int) or _FIELD_NAME.fullmatch(str(part)) else _KEY
                for part in item["loc"]
            ]
            places.append(".".join(parts))
        return cls(list(dict.fromkeys(types)), list(dict.fromkeys(places)))


@dataclass(frozen=True)
class Review:
    """The last valid critic review a publication reads: its validation, its `injection` assessment, and the
    fact confirmations after its round (the chain's: `critic_notes.validation['fact_confirmations']`)."""

    validation: CriticValidation
    injection: Literal["none", "present_no_influence", "present_influenced"]
    confirmations: Mapping[str, Confirmation]


@dataclass(frozen=True)
class Evidence:
    """One `result_evidence` row: its subject (a criterion key or a fact's key), where its quote grounds (a
    description span, a structured field or a profile section, `field_name` naming the latter two) and the
    quote."""

    subject: str
    source_kind: Literal["description", "structured_field", "profile"]
    field_name: str | None
    quote: str
    span_start: int | None
    span_end: int | None


@dataclass(frozen=True)
class Publication:
    """What `build` gives (module docstring): the strict publication object, its aggregation under the scope
    given, its evidence, the `critic_injection` column, `truncated_fields` and the repairs behind them."""

    result: PublishedResult
    aggregation: Aggregation
    evidence: list[Evidence]
    critic_injection: CriticInjection | None
    truncated_fields: list[str]
    repairs: list[Repair]


def _moved(validation: DraftValidation, dropped: contested.Dropped) -> DraftValidation:
    """`validation` with its issues and its history at the published draft's places (module docstring)."""

    def repairs(found: Sequence[Repair]) -> list[Repair]:
        return [replace(r, path=path) for r in found if (path := dropped.path(r.path)) is not None]

    history = validation.history
    return replace(
        validation,
        issues=[
            replace(i, path=path) for i in validation.issues if (path := dropped.path(i.path)) is not None
        ],
        history=CorrectionHistory(
            replace(history.normalisation, repairs=repairs(history.normalisation.repairs)),
            repairs(history.corrections),
        ),
    )


def _knockout(candidate: KnockoutCandidate) -> dict[str, JSONValue]:
    return {
        "code": candidate.code,
        "fact_id": candidate.fact_id,
        "subject": candidate.subject,
        "requirement_source": candidate.requirement_source,
        "comparison": candidate.comparison,
        "binding_capable": candidate.binding_capable,
        "severity": candidate.severity,
        "inputs": dict(candidate.inputs),
    }


def _confirmations(
    draft: GeneratorDraft, not_a_job_ad: bool, review: Review | None
) -> dict[str, Confirmation]:
    names = list(derive_fact_ids(draft.job_facts)) + ([NOT_A_JOB_AD.fact_id] if not_a_job_ad else [])
    stored = {} if review is None else review.confirmations
    return {name: stored.get(name, _UNCONFIRMED) for name in names}


def _row(subject: str, quote: str, grounding: Grounding) -> Evidence | None:
    if not QUOTE_MIN_CHARS <= len(quote) <= QUOTE_MAX_CHARS:
        return None
    return Evidence(
        subject, grounding.source_kind, grounding.field_name, quote, grounding.span_start, grounding.span_end
    )


def _evidence(
    draft: GeneratorDraft, ctx: ValidationContext, review: CriticValidation | None, subjects: set[str]
) -> list[Evidence]:
    """The evidence of the published `draft` and of the review (module docstring, step 6)."""
    found: list[Evidence | None] = []
    for key in LLM_CRITERIA:
        criterion = getattr(draft.criteria, key)
        for quote in criterion.ad_evidence:
            grounding = ground_quote(quote, ctx.ad, ctx.profile_sections)
            found.append(None if grounding is None else _row(key, quote, grounding))
        for ref in criterion.profile_evidence:
            grounding = ground_quote(ref.quote, ctx.ad, ctx.profile_sections, section=ref.section)
            found.append(None if grounding is None else _row(key, ref.quote, grounding))
    for place in fact_places(draft.job_facts):
        for quote in place.evidence:
            grounding = ground_quote(quote, ctx.ad, ctx.profile_sections)
            found.append(None if grounding is None else _row(place.key, quote, grounding))
    for subject, grounded in ({} if review is None else review.evidence).items():
        if subject in subjects:
            found += [_row(subject, quote, grounding) for quote, grounding in grounded]
    return list(dict.fromkeys(row for row in found if row is not None))


def build(
    *,
    draft: GeneratorDraft,
    validation: DraftValidation,
    last_review: Review | None,
    mode: PublishMode,
    scope: ScopeSettings,
    ctx: ValidationContext,
    weights: Mapping[str, int],
    revisions_used: int,
) -> Publication:
    """The publication of `draft` (module docstring); `PublicationInvalid` for a `Bug`."""
    checked = None if last_review is None else last_review.validation
    verified = draft.not_a_job_ad and checked is not None and checked.not_a_job_ad_verified
    if mode == "contested":
        nulled = contested.null_out(draft, validation, checked)
        published, nulled_criteria, dropped = nulled.draft, nulled.nulled_criteria, nulled.dropped
        not_a_job_ad = nulled.not_a_job_ad
    else:
        if draft.not_a_job_ad and not verified:
            raise PublicationInvalid([NOT_A_JOB_AD_UNVERIFIED], ["not_a_job_ad"])
        published, _, dropped = contested.drop_facts(draft, contested.rejected_facts(checked))
        nulled_criteria, not_a_job_ad = [], draft.not_a_job_ad
    moved = _moved(validation, dropped)
    repaired, truncated = repair_for_publication(published, moved)
    confirmations = _confirmations(repaired, not_a_job_ad, last_review)
    facts = repaired.job_facts
    candidates = derive_knockouts(facts, ctx.det, ctx.profile, scope)
    critic_outcome = "contested" if mode == "contested" else "not_run" if last_review is None else "accepted"
    critic_injection: CriticInjection | None = (
        None if critic_outcome == "not_run" else UNAVAILABLE if last_review is None else last_review.injection
    )
    doc: dict[str, JSONValue] = {
        **repaired.model_dump(mode="json", exclude={"injection_observed", "not_a_job_ad"}),
        "status": mode,
        "not_a_job_ad": not_a_job_ad,
        "injection_suspected": validation.injection_suspected,
        "nulled_criteria": list(nulled_criteria),
        "truncated_fields": list(truncated),
        "fact_confirmations": dict(confirmations),
        "knockout_candidates": [_knockout(candidate) for candidate in candidates],
        "critic_outcome": critic_outcome,
        "revisions_used": revisions_used,
    }
    invalid: PublicationInvalid | None
    try:
        result = PublishedResult.model_validate(doc)
    except ValidationError as error:
        invalid = PublicationInvalid.of(error)
    else:
        invalid = None
    if invalid is not None:
        raise invalid  # outside the handler: neither its cause nor its context holds the input
    region = facts.remote_region if facts.remote_region.evidence else None
    industry = facts.industry.value if facts.industry.evidence else None
    aggregation = aggregate(
        criteria={key: criterion for key, criterion in result.criteria.by_key().items()},
        det_location=location_work_mode(ctx.det, scope, region),
        det_employment=employment_salary(ctx.det, scope),
        weights=weights,
        knockouts=candidates,
        confirmations=confirmations,
        industry=industry_match(industry, ctx.det, scope),
        completeness=ctx.completeness,
        quality=None,
        contested=mode == "contested",
        injection_suspected=result.injection_suspected,
        critic_injection=critic_injection,
        not_a_job_ad_verified=result.not_a_job_ad,
    )
    subjects = {*LLM_CRITERIA, *confirmations}
    history = [*moved.history.normalisation.repairs, *moved.history.corrections]
    corrected = {r.path for r in history}
    repairs = [*history, *(Repair(path, PUBLICATION_CUT) for path in truncated if path not in corrected)]
    return Publication(
        result=result,
        aggregation=aggregation,
        evidence=_evidence(repaired, ctx, checked, subjects),
        critic_injection=critic_injection,
        truncated_fields=list(truncated),
        repairs=repairs,
    )
