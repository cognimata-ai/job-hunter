"""The profile (spec §12.4): the extraction output `ProfileOut`, and the confirmed profile with the
`user_entered` block.

`ProfileOut` mirrors the §12.4 schema structurally: types, enums, required keys and no extra keys, with no
limit. The limits, the date repairs (a month `YYYY-MM` becomes `YYYY-MM-01`, an unparsable date `null`), the
years recomputation, grounding and the contact scan are `profile/validate.py`'s (Task 12), so a role date is
any string here and the schema only asks the model for `format: date`. The schema has no field for any
protected attribute (§12.5).

The LLM never produces or modifies `user_entered` (§12.4): it is added in the review form. A `draft_json`
stored from the model therefore validates as a `ConfirmedProfile` with the defaults (`unspecified`, `[]`).
"""

from typing import Annotated, Literal

from pydantic import Field

from jobhunter.llm.schemas._base import Format, SchemaModel

DateString = Annotated[str, Format("date")]


class TargetTitle(SchemaModel):
    title: str
    lang: Literal["de", "en"]


class Role(SchemaModel):
    title: str
    employer: str | None
    start: DateString | None
    end: DateString | Literal["present"] | None
    seniority: Literal[
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
    domain: str | None
    highlights: list[str]


class Skill(SchemaModel):
    name: str
    category: Literal[
        "programming_language",
        "framework",
        "tool",
        "platform",
        "method",
        "domain_knowledge",
        "soft_skill",
        "other",
    ]
    level: Literal["basic", "intermediate", "advanced", "expert"]
    years: float | None
    evidence: str


class Language(SchemaModel):
    code: Literal["de", "en", "fr", "es", "it", "nl", "pl", "pt", "other"]
    code_other: str | None
    cefr: Literal["A1", "A2", "B1", "B2", "C1", "C2", "native", "unspecified"]
    evidence: str


class Education(SchemaModel):
    level: Literal["vocational", "bachelor", "master", "phd", "other"]
    field: str
    institution: str | None
    year: int | None


class Certification(SchemaModel):
    name: str
    issuer: str | None
    year: int | None


class ProfileOut(SchemaModel):
    """The `extract_profile` output (§12.4)."""

    summary: str
    target_titles: list[TargetTitle]
    seniority: Literal[
        "intern", "working_student", "apprentice", "junior", "mid", "senior", "lead", "principal", "head"
    ]
    total_years_experience: float | None
    roles: list[Role]
    skills: list[Skill]
    languages: list[Language]
    education: list[Education]
    certifications: list[Certification]
    domains: list[str]


class SecurityClearance(SchemaModel):
    name: str
    level: str | None


class UserEntered(SchemaModel):
    """Entered by the user in the review form, never by the LLM (§12.4, §12.5)."""

    work_authorization_de: Literal["authorized", "requires_sponsorship", "unspecified"] = "unspecified"
    security_clearances: list[SecurityClearance] = Field(default_factory=list)


# Stored as `profile_versions.profile_json` and as a draft's `draft_json`.
class ConfirmedProfile(ProfileOut):
    """A profile as a confirmed version or a draft holds it: the extraction plus `user_entered`."""

    user_entered: UserEntered = Field(default_factory=UserEntered)
