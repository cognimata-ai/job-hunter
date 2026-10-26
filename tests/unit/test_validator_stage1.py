"""The validator's stage 1 (spec §11.6 stage 1, adapted to candidate C; M3 Task 5): the structural mirror
(`GeneratorWire`: types, enums, required keys, no extra keys, no limits) and the normaliser to the internal
draft (v1's shape), with its issues, deterministic failures and repairs returned as a `Normalisation`.

- Every code once gives v1's facts, fact by fact; a single-valued code twice keeps the first (a repair).
- A missing criterion item is `unknown`/`null` plus the failure `criterion_missing`, never a parse failure,
  and that record survives a JSON round trip into stage 2, where it still yields T1.
- The value formats of the codes (`min_years_experience` a whole number from 0 to 40, `other:<name>`, the
  CEFR qualifier, the vocabularies) are applied, a bad value becoming `unspecified`/`null` or being dropped.
- An unknown profile section parses (V3 fails it in stage 2); an unknown code or an extra key does not.
- The parser is the version's stored family: two `generator_v2_items` versions whose generator schemas differ
  only in a description and an enum order parse the same output to the same result.

Every datum is invented.
"""

import copy
import json
from typing import Any, cast

import pytest
from jobhunter.llm.prompts.render import PromptVersionUnscorable, prompt_schema_v2
from jobhunter.llm.schema_dry_run import schema_violations
from jobhunter.llm.schemas.critic import CriticOut
from jobhunter.llm.schemas.generator import (
    Certification,
    Degree,
    DraftCriterion,
    EmploymentTypes,
    Industry,
    JobFacts,
    MinYearsExperience,
    OnsiteOnly,
    RemoteRegion,
    RequiredLanguage,
    Requirement,
    SecurityClearance,
    Seniority,
    WorkMode,
    WorkPermit,
)
from jobhunter.scoring.validator import (
    Failure,
    Issue,
    Normalisation,
    ParsedGenerator,
    Repair,
    StructuralFailure,
    parse_critic,
    parse_generator,
    validate_draft,
)

from tests.support.llm import critic_body
from tests.support.scoring import job_facts
from tests.support.validation import (
    CRITERIA,
    criterion_item,
    ctx_for,
    fact_item,
    synthetic_ad,
    synthetic_wire_c,
)


def parse(wire: Any) -> ParsedGenerator:
    parsed = parse_generator(wire, family="generator_v2_items")
    assert isinstance(parsed, ParsedGenerator), parsed
    return parsed


def with_facts(*items: dict[str, Any]) -> dict[str, Any]:
    return synthetic_wire_c(facts=list(items))


def test_a_c_output_with_every_code_once_normalises_to_the_v1_shape() -> None:
    wire = with_facts(
        fact_item("work_mode", "hybrid", "Berlin, hybrid"),
        fact_item("onsite_only", "", "nur vor Ort", required=True),
        fact_item("remote_region", "dach", "remote aus der DACH-Region"),
        fact_item("remote_country", "AT", "aus Österreich"),
        fact_item("required_language", "de", "Deutsch verhandlungssicher", qualifier="C1", required=True),
        fact_item("must_have", "Python", "Python"),
        fact_item("nice_to_have", "Kafka", "Kafka von Vorteil"),
        fact_item("seniority", "senior", "Senior"),
        fact_item("min_years_experience", "5", "5 Jahre Erfahrung"),
        fact_item("industry", "Logistik", "in der Logistik"),
        fact_item("employment_type", "full_time", "Vollzeit"),
        fact_item("degree", "bachelor", "Bachelor in Informatik", qualifier="Informatik", required=True),
        fact_item("degree_or_equivalent", "", "oder vergleichbare Erfahrung"),
        fact_item("certification", "AWS Solutions Architect", "AWS-Zertifizierung"),
        fact_item("security_clearance", "", "Sicherheitsüberprüfung Ü2", qualifier="Ü2", required=True),
        fact_item("work_permit", "", "Arbeitserlaubnis für Deutschland", required=True),
    )
    expected = JobFacts(
        work_mode=WorkMode(value="hybrid", evidence=["Berlin, hybrid"]),
        onsite_only=OnsiteOnly(asserted=True, evidence=["nur vor Ort"]),
        remote_region=RemoteRegion(
            value="dach", countries=["AT"], evidence=["remote aus der DACH-Region", "aus Österreich"]
        ),
        required_languages=[
            RequiredLanguage(
                language="de",
                language_other=None,
                level="C1",
                mandatory=True,
                evidence=["Deutsch verhandlungssicher"],
            )
        ],
        must_haves=[Requirement(text="Python", evidence=["Python"])],
        nice_to_haves=[Requirement(text="Kafka", evidence=["Kafka von Vorteil"])],
        seniority=Seniority(value="senior", evidence=["Senior"]),
        min_years_experience=MinYearsExperience(value=5, evidence=["5 Jahre Erfahrung"]),
        industry=Industry(value="Logistik", evidence=["in der Logistik"]),
        employment_types=EmploymentTypes(values=["full_time"], evidence=["Vollzeit"]),
        degree=Degree(
            level="bachelor",
            mandatory=True,
            or_equivalent=True,
            field="Informatik",
            evidence=["Bachelor in Informatik", "oder vergleichbare Erfahrung"],
        ),
        certifications=[
            Certification(name="AWS Solutions Architect", mandatory=False, evidence=["AWS-Zertifizierung"])
        ],
        security_clearance=SecurityClearance(
            required=True, level="Ü2", evidence=["Sicherheitsüberprüfung Ü2"]
        ),
        work_permit=WorkPermit(required=True, evidence=["Arbeitserlaubnis für Deutschland"]),
    )
    parsed = parse(wire)
    for name in JobFacts.model_fields:  # fact by fact
        assert getattr(parsed.draft.job_facts, name) == getattr(expected, name), name
    assert parsed.normalisation == Normalisation()
    assert parsed.draft.summary_en == wire["summary_en"] and parsed.draft.summary_de == wire["summary_de"]
    assert (parsed.draft.not_a_job_ad, parsed.draft.injection_observed) == (False, False)


def test_a_code_the_output_leaves_out_is_unspecified_or_not_asserted() -> None:
    assert parse(with_facts()).draft.job_facts == job_facts()


def test_the_criteria_are_keyed_by_criterion_in_the_order_of_the_rubric() -> None:
    items = [criterion_item(key, rationale=f"Rated {key}.") for key in reversed(CRITERIA)]
    criteria = parse(synthetic_wire_c(criteria=items)).draft.criteria
    assert list(criteria.model_dump()) == list(CRITERIA)
    assert all(getattr(criteria, key).rationale == f"Rated {key}." for key in CRITERIA)


def test_a_single_valued_code_twice_keeps_the_first_with_a_repair() -> None:
    parsed = parse(
        with_facts(
            fact_item("seniority", "senior", "Senior"),
            fact_item("seniority", "junior", "Junior"),
            fact_item("work_mode", "remote", "remote"),
            fact_item("work_mode", "onsite", "vor Ort"),
            fact_item("must_have", "Python", "Python"),
            fact_item("must_have", "Python", "Python"),  # a multi-valued code may repeat
        )
    )
    facts = parsed.draft.job_facts
    assert facts.seniority == Seniority(value="senior", evidence=["Senior"])
    assert facts.work_mode == WorkMode(value="remote", evidence=["remote"])
    assert [r.text for r in facts.must_haves] == ["Python", "Python"]
    assert parsed.normalisation.repairs == [
        Repair("job_facts.seniority", "duplicate_fact"),
        Repair("job_facts.work_mode", "duplicate_fact"),
    ]
    assert parsed.normalisation.issues == [] and parsed.normalisation.failures == []


def test_a_duplicate_criterion_keeps_the_first_with_a_repair_and_a_limit_issue() -> None:
    items = [criterion_item(key) for key in CRITERIA] + [criterion_item("skills", level=0)]
    parsed = parse(synthetic_wire_c(criteria=items))
    assert parsed.draft.criteria.skills.level == 3
    assert parsed.normalisation.repairs == [Repair("criteria.skills", "duplicate_criterion")]
    assert parsed.normalisation.issues == [Issue("criterion_count", "criteria.skills", {"count": 2})]
    assert parsed.normalisation.failures == []


def test_a_missing_criterion_is_unknown_with_a_deterministic_failure_never_a_parse_failure() -> None:
    items = [criterion_item(key) for key in CRITERIA if key != "languages"]
    parsed = parse(synthetic_wire_c(criteria=items))
    assert parsed.draft.criteria.languages == DraftCriterion(
        level=None, basis="unknown", ad_evidence=[], profile_evidence=[], rationale=""
    )
    assert parsed.normalisation.failures == [Failure("languages", "criterion_missing")]
    assert parsed.normalisation.issues == [Issue("criterion_count", "criteria.languages", {"count": 0})]


def test_the_normalisation_of_a_missing_criterion_survives_json_and_still_triggers_t1() -> None:
    items = [criterion_item(key, level=4) for key in CRITERIA if key != "languages"]
    parsed = parse(synthetic_wire_c(criteria=items))
    stored = json.loads(json.dumps(parsed.normalisation.to_json()))  # as score_drafts.validation holds it
    restored = Normalisation.from_json(stored)
    assert restored == parsed.normalisation
    ctx = ctx_for(synthetic_ad())
    validation = validate_draft(parsed.draft, ctx, normalisation=restored)
    assert Failure("languages", "criterion_missing") in validation.failures
    assert validation.t1 is True
    # without the stored record the same draft is clean: the record is what carries the failure
    assert validate_draft(parsed.draft, ctx, normalisation=Normalisation()).t1 is False


@pytest.mark.parametrize(
    ("value", "expected"), [("5", 5), (" 0 ", 0), ("40", 40), ("41", None), ("-1", None), ("fifty", None)]
)
def test_min_years_is_a_whole_number_from_0_to_40_else_null_with_a_repair(
    value: str, expected: int | None
) -> None:
    parsed = parse(with_facts(fact_item("min_years_experience", value, "Jahre Erfahrung")))
    assert parsed.draft.job_facts.min_years_experience == MinYearsExperience(
        value=expected, evidence=["Jahre Erfahrung"]
    )
    path = "job_facts.min_years_experience.value"
    if expected is None:
        assert parsed.normalisation.repairs == [Repair(path, "out_of_range")]
        assert parsed.normalisation.issues == [Issue("out_of_range", path, {"min": 0, "max": 40})]
    else:
        assert parsed.normalisation == Normalisation()


def test_an_other_language_is_split_at_other_and_keeps_its_cefr_qualifier() -> None:
    parsed = parse(
        with_facts(
            fact_item(
                "required_language", "other:Französisch", "Französisch B2", qualifier="B2", required=True
            )
        )
    )
    assert parsed.draft.job_facts.required_languages == [
        RequiredLanguage(
            language="other",
            language_other="Französisch",
            level="B2",
            mandatory=True,
            evidence=["Französisch B2"],
        )
    ]
    assert parsed.normalisation == Normalisation()


def test_a_value_outside_its_vocabulary_is_unspecified_or_dropped_with_an_issue() -> None:
    parsed = parse(
        with_facts(
            fact_item("work_mode", "teilweise remote", "teilweise remote"),
            fact_item("seniority", "Senior", "Senior"),  # the case of a vocabulary value does not matter
            fact_item("employment_type", "minijob", "Minijob"),
            fact_item("employment_type", "part_time", "Teilzeit"),
            fact_item("required_language", "Deutsch", "Deutsch", qualifier="B2", required=True),
            fact_item("required_language", "en", "fließendes Englisch", qualifier="fließend", required=True),
            fact_item("certification", "  ", "Zertifikat", required=True),
        )
    )
    facts = parsed.draft.job_facts
    assert facts.work_mode.value == "unspecified"
    assert facts.seniority.value == "senior"
    assert facts.employment_types == EmploymentTypes(values=["part_time"], evidence=["Minijob", "Teilzeit"])
    assert facts.required_languages == [
        RequiredLanguage(
            language="en",
            language_other=None,
            level="unspecified",
            mandatory=True,
            evidence=["fließendes Englisch"],
        )
    ]
    assert facts.certifications == []
    assert parsed.normalisation.issues == [
        Issue("value_invalid", "job_facts.work_mode.value", {"code": "work_mode"}),
        Issue("value_invalid", "job_facts.employment_types.values", {"code": "employment_type"}),
        Issue("value_invalid", "job_facts.required_languages", {"code": "required_language"}),
        Issue("qualifier_invalid", "job_facts.required_languages[0].level", {"code": "required_language"}),
        Issue("value_invalid", "job_facts.certifications", {"code": "certification"}),
    ]
    assert parsed.normalisation.failures == [] and parsed.normalisation.repairs == []


def test_more_than_two_quotes_on_one_fact_item_is_a_limit_issue_at_its_place_in_the_draft() -> None:
    parsed = parse(
        with_facts(
            fact_item("must_have", "Python", "Python", "Python 3", "Python-Kenntnisse"),
            fact_item("employment_type", "full_time", "Vollzeit"),
            fact_item("employment_type", "part_time", "Teilzeit", "auch Teilzeit", "oder Teilzeit"),
        )
    )
    assert parsed.normalisation.issues == [
        Issue("too_many_quotes", "job_facts.must_haves[0].evidence", {"max_quotes": 2}),
        Issue("too_many_quotes", "job_facts.employment_types.evidence", {"max_quotes": 2}),
    ]


def test_an_unknown_profile_section_parses_and_fails_its_quote_in_stage_2() -> None:
    items = [criterion_item(key, level=4) for key in CRITERIA if key != "domain"]
    items.insert(3, criterion_item("domain", level=4, profile_evidence=(("hobbies", "Logistik"),)))
    parsed = parse_generator(synthetic_wire_c(criteria=items), family="generator_v2_items")
    assert isinstance(parsed, ParsedGenerator)  # no StructuralFailure, so no structural repair call
    assert parsed.draft.criteria.domain.profile_evidence[0].section == "hobbies"
    assert parsed.normalisation == Normalisation()
    validation = validate_draft(parsed.draft, ctx_for(synthetic_ad()), normalisation=parsed.normalisation)
    assert validation.failures == [Failure("domain", "profile_section_unknown")]


def test_an_unknown_code_is_a_structural_failure() -> None:
    wire = with_facts(fact_item("salary", "60000", "60.000 EUR"))
    assert parse_generator(wire, family="generator_v2_items") == StructuralFailure(
        "literal_error", "facts.0.code"
    )


def test_an_extra_key_is_a_structural_failure() -> None:
    wire = synthetic_wire_c()
    wire["criteria"][1]["confidence"] = "high"
    assert parse_generator(wire, family="generator_v2_items") == StructuralFailure(
        "extra_forbidden", "criteria.1.confidence"
    )
    assert parse_generator({**synthetic_wire_c(), "score": 80}, family="generator_v2_items") == (
        StructuralFailure("extra_forbidden", "score")
    )


def test_a_missing_key_or_a_wrong_type_is_a_structural_failure() -> None:
    wire = synthetic_wire_c()
    del wire["summary_de"]
    assert parse_generator(wire, family="generator_v2_items") == StructuralFailure("missing", "summary_de")
    wire = synthetic_wire_c()
    wire["criteria"][0]["level"] = "3"  # strict: a JSON string is no number
    failure = parse_generator(wire, family="generator_v2_items")
    assert isinstance(failure, StructuralFailure) and failure.path.startswith("criteria.0.level")
    assert isinstance(parse_generator("not an object", family="generator_v2_items"), StructuralFailure)


def test_generator_v1_cannot_score_and_raises_before_the_wire_is_read() -> None:
    with pytest.raises(PromptVersionUnscorable):
        parse_generator(object(), family="generator_v1")  # type: ignore[arg-type]  # never looked at
    with pytest.raises(ValueError, match="no parser family"):
        parse_generator(synthetic_wire_c(), family="generator_v3")  # type: ignore[arg-type]


def _variant(marker: str, *, reversed_basis: bool) -> dict[str, Any]:
    """Version 2's generator schema with its own `description` string and, for one of the two, the `basis`
    enum reversed (as Task 4's `test_two_stored_versions_render_their_own_content` stores them)."""
    _, schemas = prompt_schema_v2()
    generator: dict[str, Any] = copy.deepcopy(cast(dict[str, Any], schemas["generator"]))
    generator["properties"]["summary_en"]["description"] = f"The English summary ({marker})."
    if reversed_basis:
        basis = generator["$defs"]["criterion"]["properties"]["basis"]
        basis["enum"] = list(reversed(basis["enum"]))
    return generator


def _reordered(doc: Any) -> Any:
    """`doc` with every object's keys in reverse order: the same JSON value, emitted another way."""
    if isinstance(doc, dict):
        return {key: _reordered(doc[key]) for key in reversed(list(doc))}
    if isinstance(doc, list):
        return [_reordered(item) for item in doc]
    return doc


def test_both_versions_parse_through_the_same_normaliser() -> None:
    schema_a, schema_b = _variant("A", reversed_basis=False), _variant("B", reversed_basis=True)
    assert schema_a != schema_b
    output_a = synthetic_wire_c(
        facts=[
            fact_item("work_mode", "hybrid", "Berlin, hybrid"),
            fact_item("required_language", "other:Dänisch", "Dänisch", qualifier="A2", required=True),
            fact_item("seniority", "senior", "Senior"),
            fact_item("seniority", "lead", "Lead"),
        ],
        criteria=[criterion_item(key, basis="observed") for key in CRITERIA if key != "domain"],
    )
    output_b = json.loads(json.dumps(_reordered(output_a)))
    assert schema_violations(schema_a, output_a) == [] and schema_violations(schema_b, output_b) == []
    parsed_a = parse_generator(output_a, family="generator_v2_items")
    parsed_b = parse_generator(output_b, family="generator_v2_items")
    assert isinstance(parsed_a, ParsedGenerator)
    assert parsed_a == parsed_b
    assert parsed_a.normalisation.failures == [Failure("domain", "criterion_missing")]  # the normaliser ran
    assert parsed_a.normalisation.repairs == [Repair("job_facts.seniority", "duplicate_fact")]


def test_the_critic_is_the_mirror_alone() -> None:
    review = parse_critic(critic_body())
    assert isinstance(review, CriticOut) and review.reviews[0].target == "languages"
    body = critic_body()
    body["reviews"][0]["assessment"] = "looks_fine"
    assert parse_critic(body) == StructuralFailure("literal_error", "reviews.0.assessment")
    assert parse_critic({**critic_body(), "confidence": 1}) == StructuralFailure(
        "extra_forbidden", "confidence"
    )
