"""The generator's structured output (spec §11.5): the structural mirror only.

Its limits (V1) and its semantic rules (V2 to V6) are the validator's, which M3 builds; M2 holds the schema as
data for prompt-schema version 1 and the schema dry run (§10.11). The `$defs` names are the spec's.
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
