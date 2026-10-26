"""The generator's structured output (spec §11.5): the structural mirrors only.

Its limits (V1) and its semantic rules (V2 to V6) are the validator's (M3 Task 5).

- `GeneratorOut` is prompt-schema version 1's wire mirror, frozen: the §11.5 schema, which the API rejects as
  too large a grammar (spike V4, M2 plan Deviation 33). It stays unchanged, so `json_schema(GeneratorOut)` is
  byte-identical to the schema version 1 stores. The `$defs` names are the spec's.
- `GeneratorWire` is version 2's wire mirror: candidate C of the pre-plan probe V4b
  (`docs/spikes/V4b-generator-v2.md`, A3 §3.6). It holds generic fact items (`FactItem`: a `code`, its
  `value` and `qualifier` as strings, the `required` flag and the quotes) and generic criterion items
  (`CriterionItem`, one per `CriterionKey`). Its `$defs` are `quotes`, `fact` and `criterion`, as in the
  candidate, and a profile reference (`ProfileRefWire`) carries any section string. The shared rubric template
  (`prompts/shared_rubric.md`) documents the value formats of the codes; the validator's normaliser applies
  them.
- `GeneratorDraft` is the internal draft, not a wire schema: v1's shape (`job_facts: JobFacts`, `criteria` by
  key), as `score_drafts.output_json` stores it and the critic and the reviser read it. Its profile references
  are `ProfileRefWire` too, so an unknown section reaches V3, which fails it, instead of being refused by the
  parse (Codex r1 #7); the strict publication model keeps the section enum (Task 6).
"""

from typing import Annotated, Literal

from jobhunter.llm.schemas._base import NOT_BOOL, Def, SchemaModel

Quotes = Annotated[list[str], Def("quotes")]
Level = Annotated[Annotated[Literal[0, 1, 2, 3, 4], NOT_BOOL] | None, Def("level")]
Cefr = Annotated[Literal["A1", "A2", "B1", "B2", "C1", "C2", "native", "unspecified"], Def("cefr")]
LangCode = Annotated[Literal["de", "en", "fr", "es", "it", "nl", "pl", "pt", "other"], Def("lang_code")]
NullableString = Annotated[str | None, Def("nullable_string")]
NullableInt = Annotated[int | None, Def("nullable_int")]


class ProfileRef(SchemaModel):
    section: Literal[
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
    quote: str


class Criterion(SchemaModel):
    level: Level
    basis: Literal["observed", "unknown", "unconstrained"]
    ad_evidence: Quotes
    profile_evidence: list[Annotated[ProfileRef, Def("profile_ref")]]
    rationale: str


CriterionRef = Annotated[Criterion, Def("criterion")]


class Requirement(SchemaModel):
    text: str
    evidence: Quotes


RequirementRef = Annotated[Requirement, Def("requirement")]


class WorkMode(SchemaModel):
    value: Literal["remote", "hybrid", "onsite", "unspecified"]
    evidence: Quotes


class OnsiteOnly(SchemaModel):
    asserted: bool
    evidence: Quotes


class RemoteRegion(SchemaModel):
    value: Literal["germany", "dach", "eu_eea", "europe", "worldwide", "other_country", "unspecified"]
    countries: list[str]
    evidence: Quotes


class RequiredLanguage(SchemaModel):
    language: LangCode
    language_other: NullableString
    level: Cefr
    mandatory: bool
    evidence: Quotes


class Seniority(SchemaModel):
    value: Literal[
        "intern",
        "working_student",
        "apprentice",
        "junior",
        "mid",
        "senior",
        "lead",
        "principal",
        "head",
        "unspecified",
    ]
    evidence: Quotes


class MinYearsExperience(SchemaModel):
    value: NullableInt
    evidence: Quotes


class Industry(SchemaModel):
    value: NullableString
    evidence: Quotes


class EmploymentTypes(SchemaModel):
    values: list[
        Literal[
            "full_time",
            "part_time",
            "contract",
            "temporary",
            "internship",
            "working_student",
            "apprenticeship",
            "freelance",
        ]
    ]
    evidence: Quotes


class Degree(SchemaModel):
    level: Literal["none_required", "vocational", "bachelor", "master", "phd", "unspecified"]
    mandatory: bool
    or_equivalent: bool
    field: NullableString
    evidence: Quotes


class Certification(SchemaModel):
    name: str
    mandatory: bool
    evidence: Quotes


class SecurityClearance(SchemaModel):
    required: bool
    level: NullableString
    evidence: Quotes


class WorkPermit(SchemaModel):
    required: bool
    evidence: Quotes


class JobFacts(SchemaModel):
    work_mode: WorkMode
    onsite_only: OnsiteOnly
    remote_region: RemoteRegion
    required_languages: list[RequiredLanguage]
    must_haves: list[RequirementRef]
    nice_to_haves: list[RequirementRef]
    seniority: Seniority
    min_years_experience: MinYearsExperience
    industry: Industry
    employment_types: EmploymentTypes
    degree: Degree
    certifications: list[Certification]
    security_clearance: SecurityClearance
    work_permit: WorkPermit


class Criteria(SchemaModel):
    role_fit: CriterionRef
    skills: CriterionRef
    experience_seniority: CriterionRef
    domain: CriterionRef
    languages: CriterionRef
    education_certifications: CriterionRef


class GeneratorOut(SchemaModel):
    """The generator's output (§11.5)."""

    not_a_job_ad: bool
    injection_observed: bool
    job_facts: Annotated[JobFacts, Def("job_facts")]
    criteria: Criteria
    summary_en: str
    summary_de: str


# ---- prompt-schema version 2: the wire schema (candidate C) and the internal draft -------------------------

# Candidate C's fact codes, in its order (A3 §3.6); a code absent from the output means unspecified.
FactItemCode = Literal[
    "work_mode",
    "onsite_only",
    "remote_region",
    "remote_country",
    "required_language",
    "must_have",
    "nice_to_have",
    "seniority",
    "min_years_experience",
    "industry",
    "employment_type",
    "degree",
    "degree_or_equivalent",
    "certification",
    "security_clearance",
    "work_permit",
]
# The six LLM-rated criteria of §11.2, in its order.
CriterionKey = Literal[
    "role_fit", "skills", "experience_seniority", "domain", "languages", "education_certifications"
]


class FactItem(SchemaModel):
    """One stated fact: its code, its value and qualifier in the code's format, v1's `mandatory`, `asserted`
    or `required` flag, and its quotes."""

    code: FactItemCode
    value: str
    qualifier: str
    required: bool
    evidence: Quotes


class ProfileRefWire(SchemaModel):
    """A profile reference as the wire and the draft carry it: any section string (V3 checks it)."""

    section: str
    quote: str


class CriterionItem(SchemaModel):
    """One rated criterion, named by its key; its level is an inline nullable integer enum, as in C."""

    criterion: CriterionKey
    level: Annotated[Literal[0, 1, 2, 3, 4], NOT_BOOL] | None
    basis: Literal["observed", "unknown", "unconstrained"]
    ad_evidence: Quotes
    profile_evidence: list[ProfileRefWire]
    rationale: str


class GeneratorWire(SchemaModel):
    """The generator's output in prompt-schema version 2 (candidate C)."""

    not_a_job_ad: bool
    injection_observed: bool
    facts: list[Annotated[FactItem, Def("fact")]]
    criteria: list[Annotated[CriterionItem, Def("criterion")]]
    summary_en: str
    summary_de: str


class DraftCriterion(SchemaModel):
    """v1's `Criterion` with `ProfileRefWire` references."""

    level: Level
    basis: Literal["observed", "unknown", "unconstrained"]
    ad_evidence: list[str]
    profile_evidence: list[ProfileRefWire]
    rationale: str


class DraftCriteria(SchemaModel):
    role_fit: DraftCriterion
    skills: DraftCriterion
    experience_seniority: DraftCriterion
    domain: DraftCriterion
    languages: DraftCriterion
    education_certifications: DraftCriterion


class GeneratorDraft(SchemaModel):
    """The internal draft (v1's shape): what the validator normalises a wire output into, stores, and renders
    to the critic and the reviser (`prompts.render.draft_block`)."""

    not_a_job_ad: bool
    injection_observed: bool
    job_facts: JobFacts
    criteria: DraftCriteria
    summary_en: str
    summary_de: str
