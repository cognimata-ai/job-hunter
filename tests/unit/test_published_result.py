"""The strict publication model (spec §11.6 L4394, the §11.5 limits L4330-4347; M3 Task 6): the internal
draft's shape with every limit enforced, the quote rules, the strict profile sections and the publication
fields, with the `llm_results` CHECKs it mirrors. A clean validated draft repaired for publication validates;
each limit, one past it, is refused. Every datum is invented.
"""

import copy
from collections.abc import Callable
from typing import Any, Final, get_args

import pytest
from jobhunter.llm.schemas.generator import DraftCriteria, GeneratorDraft, JobFacts
from jobhunter.llm.schemas.published import (
    KnockoutCode,
    PublishedCriteria,
    PublishedJobFacts,
    PublishedResult,
)
from jobhunter.scoring.facts import FACT_ID_CODES, NOT_A_JOB_AD
from jobhunter.scoring.validator import (
    ParsedGenerator,
    parse_generator,
    repair_for_publication,
    validate_draft,
)
from jobhunter.settings.model import KnockoutSeverity
from pydantic import ValidationError

from tests.support.validation import ctx_for, publication_doc, synthetic_ad, synthetic_wire_c

Doc = dict[str, Any]


def clean_draft() -> GeneratorDraft:
    parsed = parse_generator(synthetic_wire_c(), family="generator_v2_items")
    assert isinstance(parsed, ParsedGenerator)
    validation = validate_draft(parsed.draft, ctx_for(synthetic_ad()), normalisation=parsed.normalisation)
    assert not validation.t1
    repaired, truncated = repair_for_publication(validation.draft, validation)
    assert truncated == []
    return repaired


DRAFT: Final = clean_draft()
QUOTE: Final = "Python, PostgreSQL"


def doc(**fields: Any) -> Doc:
    return copy.deepcopy(publication_doc(DRAFT, **fields))


def with_facts(**fields: Any) -> Doc:
    d = doc()
    d["job_facts"].update(fields)
    return d


def with_skills(**fields: Any) -> Doc:
    d = doc()
    d["criteria"]["skills"].update(fields)
    return d


def requirement(text: str = "Python") -> dict[str, Any]:
    return {"text": text, "evidence": [QUOTE]}


def language(other: str | None = None) -> dict[str, Any]:
    code = "de" if other is None else "other"
    return {"language": code, "language_other": other, "level": "C1", "mandatory": True, "evidence": [QUOTE]}


def certification(name: str = "AWS Solutions Architect") -> dict[str, Any]:
    return {"name": name, "mandatory": True, "evidence": [QUOTE]}


def region(*countries: str) -> dict[str, Any]:
    return {"value": "europe", "countries": list(countries), "evidence": [QUOTE]}


def knockout(**fields: Any) -> dict[str, Any]:
    return {
        "code": "degree",
        "fact_id": "degree:degree",
        "subject": "degree",
        "requirement_source": "text",
        "comparison": "unmet",
        "binding_capable": True,
        "severity": "soft",
        "inputs": {"required_level": "master", "profile_level": "bachelor"},
        **fields,
    }


STRUCTURED_KNOCKOUT: Final = knockout(
    code="contract_type",
    fact_id="contract_type:employment_types",
    subject="employment_types",
    requirement_source="structured",
    binding_capable=False,
    severity=None,
    inputs={"employment_types": ["full_time"], "scope_employment_types": []},
)


def nulled(d: Doc, key: str) -> Doc:
    d["criteria"][key].update({"level": None, "basis": "unknown", "ad_evidence": [], "profile_evidence": []})
    return d


def every_criterion_null(d: Doc) -> Doc:
    for key in d["criteria"]:
        nulled(d, key)
    return d


def test_a_clean_draft_repaired_for_publication_validates() -> None:
    published = PublishedResult.model_validate(doc())
    assert published.model_dump(mode="json") == doc()
    assert GeneratorDraft.model_validate(
        {
            **published.model_dump(mode="json", include=set(GeneratorDraft.model_fields)),
            "injection_observed": False,
        }
    ) == DRAFT.model_copy(update={"injection_observed": False})


def test_the_published_shape_is_the_drafts() -> None:
    assert set(PublishedResult.model_fields) >= set(GeneratorDraft.model_fields) - {"injection_observed"}
    assert set(PublishedJobFacts.model_fields) == set(JobFacts.model_fields)
    assert set(PublishedCriteria.model_fields) == set(DraftCriteria.model_fields)


def test_the_knockout_codes_are_the_knockout_tables() -> None:
    codes = set(get_args(KnockoutCode))
    assert codes == (set(FACT_ID_CODES) - {NOT_A_JOB_AD.code}) | {"contract_type"}
    assert codes == set(KnockoutSeverity.model_fields) | {"mandatory_language"}


@pytest.mark.parametrize(
    "valid",
    [
        doc(summary_en="w " * 59 + "w"),  # 60 words
        doc(summary_de="x" * 450),
        with_skills(rationale="w " * 24 + "w", ad_evidence=[QUOTE, "Berlin, hybrid"]),
        with_facts(remote_region=region(*["DE"] * 30)),
        with_facts(remote_region=region("de", " at ")),  # the validator's country check: trimmed, any case
        with_facts(must_haves=[requirement("x" * 200)] * 25),
        with_facts(required_languages=[language("x" * 40)] + [language()] * 5),
        with_facts(certifications=[certification("x" * 120)] * 10),
        with_facts(min_years_experience={"value": 40, "evidence": [QUOTE]}),
        with_facts(min_years_experience={"value": 0, "evidence": [QUOTE]}),
        with_facts(min_years_experience={"value": None, "evidence": []}),
        doc(knockout_candidates=[knockout(), STRUCTURED_KNOCKOUT]),
        doc(fact_confirmations={"degree:degree": "critic_confirmed", "mandatory_language:fr": "uncertain"}),
        nulled(doc(status="contested", critic_outcome="contested", nulled_criteria=["skills"]), "skills"),
        every_criterion_null(doc(not_a_job_ad=True, critic_outcome="accepted")),
        doc(revisions_used=2, truncated_fields=["summary_en", "criteria.skills.ad_evidence"]),
    ],
)
def test_values_at_their_limits_validate(valid: Doc) -> None:
    PublishedResult.model_validate(valid)


# Each case: its name, the dotted path its error names ("" for the result's own consistency checks), its doc.
OVER_THE_LIMIT: Final[list[tuple[str, str, Callable[[], Doc]]]] = [
    ("summary words", "summary_en", lambda: doc(summary_en="w " * 60 + "w")),
    ("summary characters", "summary_de", lambda: doc(summary_de="x" * 451)),
    ("rationale words", "criteria.skills.rationale", lambda: with_skills(rationale="w " * 25 + "w")),
    ("rationale characters", "criteria.skills.rationale", lambda: with_skills(rationale="x" * 201)),
    ("criterion quotes", "criteria.skills.ad_evidence", lambda: with_skills(ad_evidence=[QUOTE] * 3)),
    ("an ellipsis", "criteria.skills.ad_evidence.0", lambda: with_skills(ad_evidence=["Python … SQL"])),
    ("three dots", "criteria.skills.ad_evidence.0", lambda: with_skills(ad_evidence=["Python ... SQL"])),
    ("a short quote", "criteria.skills.ad_evidence.0", lambda: with_skills(ad_evidence=["ab"])),
    ("a long quote", "criteria.skills.ad_evidence.0", lambda: with_skills(ad_evidence=["x" * 301])),
    ("few letters", "criteria.skills.ad_evidence.0", lambda: with_skills(ad_evidence=["- - - -"])),
    (
        "a profile section",
        "criteria.skills.profile_evidence.0.section",
        lambda: with_skills(profile_evidence=[{"section": "hobbies", "quote": "Python"}]),
    ),
    (
        "a profile quote",
        "criteria.skills.profile_evidence",
        lambda: with_skills(profile_evidence=[{"section": "skills", "quote": "ab"}]),
    ),
    (
        "profile references",
        "criteria.skills.profile_evidence",
        lambda: with_skills(profile_evidence=[{"section": "skills", "quote": "Python"}] * 3),
    ),
    ("a level", "criteria.skills.level", lambda: with_skills(level=5)),
    ("a boolean level", "criteria.skills.level", lambda: with_skills(level=True)),
    ("a country", "job_facts.remote_region.countries.0", lambda: with_facts(remote_region=region("XX"))),
    (
        "countries",
        "job_facts.remote_region.countries",
        lambda: with_facts(remote_region=region(*["DE"] * 31)),
    ),
    ("must-haves", "job_facts.must_haves", lambda: with_facts(must_haves=[requirement()] * 26)),
    (
        "a requirement",
        "job_facts.nice_to_haves.0.text",
        lambda: with_facts(nice_to_haves=[requirement("x" * 201)]),
    ),
    ("languages", "job_facts.required_languages", lambda: with_facts(required_languages=[language()] * 7)),
    (
        "an other language",
        "job_facts.required_languages.0.language_other",
        lambda: with_facts(required_languages=[language("x" * 41)]),
    ),
    ("certifications", "job_facts.certifications", lambda: with_facts(certifications=[certification()] * 11)),
    (
        "a certification",
        "job_facts.certifications.0.name",
        lambda: with_facts(certifications=[certification("x" * 121)]),
    ),
    (
        "an industry",
        "job_facts.industry.value",
        lambda: with_facts(industry={"value": "x" * 121, "evidence": [QUOTE]}),
    ),
    (
        "a degree field",
        "job_facts.degree.field",
        lambda: with_facts(
            degree={
                "level": "master",
                "mandatory": True,
                "or_equivalent": False,
                "field": "x" * 121,
                "evidence": [QUOTE],
            }
        ),
    ),
    (
        "a clearance",
        "job_facts.security_clearance.level",
        lambda: with_facts(security_clearance={"required": True, "level": "x" * 61, "evidence": [QUOTE]}),
    ),
    (
        "too many years",
        "job_facts.min_years_experience.value",
        lambda: with_facts(min_years_experience={"value": 41, "evidence": [QUOTE]}),
    ),
    (
        "negative years",
        "job_facts.min_years_experience.value",
        lambda: with_facts(min_years_experience={"value": -1, "evidence": [QUOTE]}),
    ),
    (
        "fact quotes",
        "job_facts.work_permit.evidence",
        lambda: with_facts(work_permit={"required": True, "evidence": [QUOTE] * 3}),
    ),
    (
        "a fact quote",
        "job_facts.work_permit.evidence.0",
        lambda: with_facts(work_permit={"required": True, "evidence": ["…"]}),
    ),
    ("an extra key", "injection_observed", lambda: doc(injection_observed=False)),
    ("a status", "status", lambda: doc(status="published")),
    ("contested without the critic", "", lambda: doc(status="contested")),
    ("the critic contested an accepted result", "", lambda: doc(critic_outcome="contested")),
    ("nulled in an accepted result", "", lambda: nulled(doc(nulled_criteria=["skills"]), "skills")),
    (
        "a nulled criterion with a level",
        "",
        lambda: doc(status="contested", critic_outcome="contested", nulled_criteria=["skills"]),
    ),
    (
        "a nulled criterion twice",
        "",
        lambda: nulled(
            doc(status="contested", critic_outcome="contested", nulled_criteria=["skills", "skills"]),
            "skills",
        ),
    ),
    (
        "a nulled key",
        "nulled_criteria.0",
        lambda: doc(status="contested", critic_outcome="contested", nulled_criteria=["salary"]),
    ),
    ("not a job ad with a level", "", lambda: doc(not_a_job_ad=True)),
    ("revisions", "revisions_used", lambda: doc(revisions_used=3)),
    (
        "a confirmation",
        "fact_confirmations.degree:degree",
        lambda: doc(fact_confirmations={"degree:degree": "maybe"}),
    ),
    (
        "a comparison",
        "knockout_candidates.0.comparison",
        lambda: doc(knockout_candidates=[knockout(comparison="partial")]),
    ),
    (
        "a knockout code",
        "knockout_candidates.0.code",
        lambda: doc(knockout_candidates=[knockout(code="salary", fact_id="salary:degree")]),
    ),
    (
        "a knockout's fact id",
        "knockout_candidates.0",
        lambda: doc(knockout_candidates=[knockout(fact_id="degree:other")]),
    ),
    (
        "knockout inputs",
        "knockout_candidates.0.inputs",
        lambda: doc(knockout_candidates=[knockout(inputs={"x": {1, 2}})]),
    ),
]


@pytest.mark.parametrize(
    ("where", "build"), [(w, b) for _, w, b in OVER_THE_LIMIT], ids=[n for n, _, _ in OVER_THE_LIMIT]
)
def test_one_past_a_limit_is_refused(where: str, build: Callable[[], Doc]) -> None:
    with pytest.raises(ValidationError) as refused:
        PublishedResult.model_validate(build())
    places = {".".join(str(step) for step in error["loc"]) for error in refused.value.errors()}
    assert any(place.startswith(where) for place in places) if where else places == {""}, places
