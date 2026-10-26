"""The strict publication model (spec §11.6 L4394; M3 Task 6): `PublishedResult`, the complete publication
object, validated after the null-out and the publication repairs. Not a wire schema: it is never sent to a
model, and `json_schema` refuses its limits.

It is the internal draft's shape (`generator.GeneratorDraft`: its job facts and criteria, its summaries; the
draft's `injection_observed` is folded into `injection_suspected`) with **every §11.5 limit enforced**, from
the validator's constants (`scoring.validator`, the `LIMITS` table's values), and the publication fields:
- the texts: a summary at most 450 characters and 60 words, a rationale 200 and 25 (the word count is
  `validator.cut_text`'s, so a text publication cut is within them), a requirement 200 characters, a
  certification name, `industry.value` and `degree.field` 120, `language_other` 40, a clearance level 60;
- the lists: at most 2 ad quotes and 2 profile references per criterion and 2 quotes per fact, 25
  must-haves and 25 nice-to-haves, 6 required languages, 10 certifications, 30 countries, each an ISO code
  (`validator.valid_country`); `min_years_experience` 0 to 40 or `null`;
- the quote rules: every quote passes `validator.quote_defect` (4 to 300 characters, no ellipsis, at least 3
  letters or digits); a profile reference names a section of the strict enum (`generator.ProfileRef`, which
  the draft's `ProfileRefWire` leaves open for V3);
- `status`, `nulled_criteria`, `truncated_fields`, `fact_confirmations`, `knockout_candidates`
  (`knockouts.KnockoutCandidate`'s fields, its fact id `code:subject`), `injection_suspected`,
  `not_a_job_ad`, `critic_outcome` and `revisions_used` (0 to 2);
- the `llm_results` CHECKs it mirrors, and the null-out's and V6's shape: only a contested result nulls
  criteria, each at most once and each `null`/`unknown`; the critic's outcome is `contested` exactly when the
  status is; a result that is no job ad (verified, V6) has every criterion `null`.

It does not check V2's level and basis rules or grounding: those are the validator's, before publication.

A failure to validate is a `Bug` (`failed_invalid_output('publication_invalid')` and a critical alert, Task
10). Its `ValidationError` text holds the input values (ad quotes, LLM text, fact ids with folded names), so
only its error types and paths of field names may be logged.
"""

from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from jobhunter.llm.schemas.generator import (
    Certification,
    Criterion,
    CriterionKey,
    Degree,
    EmploymentTypes,
    Industry,
    MinYearsExperience,
    OnsiteOnly,
    ProfileRef,
    RemoteRegion,
    RequiredLanguage,
    Requirement,
    SecurityClearance,
    Seniority,
    WorkMode,
    WorkPermit,
)
from jobhunter.scoring.det_criteria import MIN_YEARS_RANGE
from jobhunter.scoring.facts import fact_id
from jobhunter.scoring.knockouts import Comparison, Confirmation, RequirementSource, Severity
from jobhunter.scoring.validator import (
    CERTIFICATIONS_MAX,
    CLEARANCE_LEVEL_MAX_CHARS,
    COUNTRIES_MAX,
    LANGUAGE_OTHER_MAX_CHARS,
    LANGUAGES_MAX,
    NAME_MAX_CHARS,
    QUOTES_PER_CRITERION,
    QUOTES_PER_FACT,
    RATIONALE_MAX_CHARS,
    RATIONALE_MAX_WORDS,
    REQUIREMENT_MAX_CHARS,
    REQUIREMENTS_MAX,
    SUMMARY_MAX_CHARS,
    SUMMARY_MAX_WORDS,
    cut_text,
    quote_defect,
    valid_country,
)
from jobhunter.types import JSONValue

# The §11.4 knockout codes (`knockouts.derive_knockouts`).
KnockoutCode = Literal[
    "onsite_required",
    "mandatory_language",
    "security_clearance",
    "work_permit",
    "degree",
    "certification",
    "contract_type",
]


def _quote(quote: str) -> str:
    defect = quote_defect(quote)
    if defect is not None:
        raise ValueError(f"the quote is no valid evidence ({defect})")
    return quote


def _profile_quotes(refs: list[ProfileRef]) -> list[ProfileRef]:
    for ref in refs:
        _quote(ref.quote)
    return refs


def _country(code: str) -> str:
    if not valid_country(code):
        raise ValueError("not an ISO 3166-1 alpha-2 country code")
    return code


def _within(max_chars: int, max_words: int) -> AfterValidator:
    """A text within `max_chars` characters and `max_words` words (`validator.cut_text` cuts none)."""

    def check(text: str) -> str:
        if cut_text(text, max_chars=max_chars, max_words=max_words) is not None:
            raise ValueError(f"over {max_chars} characters or {max_words} words")
        return text

    return AfterValidator(check)


Quote = Annotated[str, AfterValidator(_quote)]
FactQuotes = Annotated[list[Quote], Field(max_length=QUOTES_PER_FACT)]
Summary = Annotated[str, _within(SUMMARY_MAX_CHARS, SUMMARY_MAX_WORDS)]
Name = Annotated[str, Field(max_length=NAME_MAX_CHARS)]


# ---- the job facts: the draft's fact models with their limits ---------------------------------------------


class PublishedWorkMode(WorkMode):
    evidence: FactQuotes


class PublishedOnsiteOnly(OnsiteOnly):
    evidence: FactQuotes


class PublishedRemoteRegion(RemoteRegion):
    countries: Annotated[list[Annotated[str, AfterValidator(_country)]], Field(max_length=COUNTRIES_MAX)]
    evidence: FactQuotes


class PublishedRequiredLanguage(RequiredLanguage):
    language_other: Annotated[str, Field(max_length=LANGUAGE_OTHER_MAX_CHARS)] | None
    evidence: FactQuotes


class PublishedRequirement(Requirement):
    text: Annotated[str, Field(max_length=REQUIREMENT_MAX_CHARS)]
    evidence: FactQuotes


class PublishedSeniority(Seniority):
    evidence: FactQuotes


class PublishedMinYearsExperience(MinYearsExperience):
    value: Annotated[int, Field(ge=MIN_YEARS_RANGE[0], le=MIN_YEARS_RANGE[1])] | None
    evidence: FactQuotes


class PublishedIndustry(Industry):
    value: Name | None
    evidence: FactQuotes


class PublishedEmploymentTypes(EmploymentTypes):
    evidence: FactQuotes


class PublishedDegree(Degree):
    field: Name | None
    evidence: FactQuotes


class PublishedCertification(Certification):
    name: Name
    evidence: FactQuotes


class PublishedSecurityClearance(SecurityClearance):
    level: Annotated[str, Field(max_length=CLEARANCE_LEVEL_MAX_CHARS)] | None
    evidence: FactQuotes


class PublishedWorkPermit(WorkPermit):
    evidence: FactQuotes


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PublishedJobFacts(_Strict):
    """`generator.JobFacts` with every limit."""

    work_mode: PublishedWorkMode
    onsite_only: PublishedOnsiteOnly
    remote_region: PublishedRemoteRegion
    required_languages: Annotated[list[PublishedRequiredLanguage], Field(max_length=LANGUAGES_MAX)]
    must_haves: Annotated[list[PublishedRequirement], Field(max_length=REQUIREMENTS_MAX)]
    nice_to_haves: Annotated[list[PublishedRequirement], Field(max_length=REQUIREMENTS_MAX)]
    seniority: PublishedSeniority
    min_years_experience: PublishedMinYearsExperience
    industry: PublishedIndustry
    employment_types: PublishedEmploymentTypes
    degree: PublishedDegree
    certifications: Annotated[list[PublishedCertification], Field(max_length=CERTIFICATIONS_MAX)]
    security_clearance: PublishedSecurityClearance
    work_permit: PublishedWorkPermit


# ---- the criteria ------------------------------------------------------------------------------------------


class PublishedCriterion(Criterion):
    """A criterion with its limits; its profile references keep the strict section enum."""

    ad_evidence: Annotated[list[Quote], Field(max_length=QUOTES_PER_CRITERION)]
    profile_evidence: Annotated[
        list[ProfileRef], Field(max_length=QUOTES_PER_CRITERION), AfterValidator(_profile_quotes)
    ]
    rationale: Annotated[str, _within(RATIONALE_MAX_CHARS, RATIONALE_MAX_WORDS)]


class PublishedCriteria(_Strict):
    role_fit: PublishedCriterion
    skills: PublishedCriterion
    experience_seniority: PublishedCriterion
    domain: PublishedCriterion
    languages: PublishedCriterion
    education_certifications: PublishedCriterion

    def by_key(self) -> dict[CriterionKey, PublishedCriterion]:
        return {
            "role_fit": self.role_fit,
            "skills": self.skills,
            "experience_seniority": self.experience_seniority,
            "domain": self.domain,
            "languages": self.languages,
            "education_certifications": self.education_certifications,
        }


# ---- the publication object --------------------------------------------------------------------------------


class PublishedKnockout(_Strict):
    """A knockout candidate as `llm_results.knockout_candidates` stores it (`knockouts.KnockoutCandidate`)."""

    code: KnockoutCode
    fact_id: str
    subject: str
    requirement_source: RequirementSource
    comparison: Comparison
    binding_capable: bool
    severity: Severity | None
    inputs: dict[str, JSONValue]

    @model_validator(mode="after")
    def _named(self) -> Self:
        if self.fact_id != fact_id(self.code, self.subject):
            raise ValueError("a knockout candidate's fact id is its code and subject")
        return self


class PublishedResult(_Strict):
    """The publication object (module docstring)."""

    status: Literal["accepted", "contested"]
    not_a_job_ad: bool
    injection_suspected: bool
    job_facts: PublishedJobFacts
    criteria: PublishedCriteria
    summary_en: Summary
    summary_de: Summary
    nulled_criteria: list[CriterionKey]
    truncated_fields: list[str]
    fact_confirmations: dict[str, Confirmation]
    knockout_candidates: list[PublishedKnockout]
    critic_outcome: Literal["not_run", "accepted", "contested"]
    revisions_used: Annotated[int, Field(ge=0, le=2)]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        criteria = self.criteria.by_key()
        nulled = self.nulled_criteria
        if nulled and self.status != "contested":
            raise ValueError("only a contested result nulls criteria")
        if (self.critic_outcome == "contested") != (self.status == "contested"):
            raise ValueError("the critic's outcome is contested exactly when the status is")
        if len(set(nulled)) != len(nulled):
            raise ValueError("a criterion is nulled twice")
        if any((criteria[key].level, criteria[key].basis) != (None, "unknown") for key in nulled):
            raise ValueError("a nulled criterion is null and unknown")
        if self.not_a_job_ad and any(c.level is not None for c in criteria.values()):
            raise ValueError("a result that is no job ad has every criterion null")
        return self
