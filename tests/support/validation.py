"""Validator test data (M3 Task 5): synthetic ads, candidate C wire outputs and validation contexts. Every
value is invented: no real ad, company or person.

- `synthetic_ad(description=…, **fields)` is a `SyntheticAd`: the fields `render.job_ad_block` takes, with an
  invented title (`Senior Backend Engineer (m/w/d)`, so the title has the seniority token `senior`), company,
  location, the structured work mode `hybrid` and employment type `full_time`, and the given description;
  `.block` is its rendered `<job_ad>` block.
- `ctx_for(ad, profile=…, structured_work_modes=…)` is the `ValidationContext` of an ad and a profile
  (`tests.support.llm.score_profile` unless given): the profile's canonical sections, the job's `DetFacts`
  with the title's normalised form and seniority tokens, and the ad's structured work modes.
- `synthetic_wire_c(level_all=…, injection_observed=…, facts=…, criteria=…)` is a candidate C generator
  output whose ad quotes all lie in the text `SHARED` (which `DESCRIPTION` contains) or in a structured
  field, and whose profile references quote `score_profile()`: each criterion is `observed` at `level_all`
  (4 by default, with an ad quote and a profile reference), and the facts are a hybrid work mode, two
  must-haves and full-time employment. With the default ad and profile it validates with nothing to report.
  A test replaces the facts or the criteria it is about.
- `fact_item(code, value, quote, …)` and `criterion_item(key, …)` are one item of each list.
"""

from dataclasses import dataclass
from typing import Any, Final

from jobhunter.llm.prompts.render import JobAdBlock, job_ad_block
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.normalize.titles import normalize_title
from jobhunter.scoring.validator import ValidationContext, profile_sections

from tests.support.facts import facts as det_facts
from tests.support.llm import score_profile

CRITERIA: Final = (
    "role_fit",
    "skills",
    "experience_seniority",
    "domain",
    "languages",
    "education_certifications",
)
TITLE: Final = "Senior Backend Engineer (m/w/d)"
SHARED: Final = "Python, PostgreSQL. Berlin, hybrid."
DESCRIPTION: Final = f"Backend Engineer (m/w/d). Du entwickelst Services. {SHARED}"


@dataclass(frozen=True)
class SyntheticAd:
    title: str
    company: str | None
    locations: tuple[str, ...]
    work_modes: tuple[tuple[str, str], ...]
    employment_types: tuple[tuple[str, str], ...]
    completeness: str
    description_text: str

    @property
    def block(self) -> JobAdBlock:
        return job_ad_block(
            title=self.title,
            company=self.company,
            locations=self.locations,
            work_modes=self.work_modes,
            employment_types=self.employment_types,
            completeness=self.completeness,
            description_text=self.description_text,
        )


def synthetic_ad(description: str = DESCRIPTION, **fields: Any) -> SyntheticAd:
    values: dict[str, Any] = {
        "title": TITLE,
        "company": "Beispiel Logistik GmbH",
        "locations": ("Berlin",),
        "work_modes": (("hybrid", "structured"),),
        "employment_types": (("full_time", "structured"),),
        "completeness": "full",
        "description_text": description,
    }
    return SyntheticAd(**{**values, **fields})


def ctx_for(
    ad: SyntheticAd,
    profile: ConfirmedProfile | None = None,
    *,
    structured_work_modes: tuple[str, ...] | None = None,
) -> ValidationContext:
    person = score_profile() if profile is None else profile
    title = normalize_title(ad.title)
    modes = (
        tuple(mode for mode, source in ad.work_modes if source == "structured")
        if structured_work_modes is None
        else structured_work_modes
    )
    return ValidationContext(
        ad=ad.block,
        profile=person,
        profile_sections=profile_sections(person),
        det=det_facts(title_norm=title.title_norm, seniority_tokens=title.seniority_tokens),
        structured_work_modes=modes,
        completeness=ad.completeness,
    )


def fact_item(
    code: str, value: str, *quotes: str, qualifier: str = "", required: bool = False
) -> dict[str, Any]:
    return {
        "code": code,
        "value": value,
        "qualifier": qualifier,
        "required": required,
        "evidence": list(quotes),
    }


def criterion_item(
    key: str,
    *,
    level: int | None = 3,
    basis: str = "observed",
    ad_evidence: tuple[str, ...] = ("Python, PostgreSQL",),
    profile_evidence: tuple[tuple[str, str], ...] = (("skills", "Python"),),
    rationale: str = "Die Kernkenntnisse sind belegt.",
) -> dict[str, Any]:
    return {
        "criterion": key,
        "level": level,
        "basis": basis,
        "ad_evidence": list(ad_evidence),
        "profile_evidence": [{"section": section, "quote": quote} for section, quote in profile_evidence],
        "rationale": rationale,
    }


def synthetic_wire_c(
    *,
    level_all: int = 4,
    injection_observed: bool = False,
    not_a_job_ad: bool = False,
    facts: list[dict[str, Any]] | None = None,
    criteria: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    default_facts = [
        fact_item("work_mode", "hybrid", "Berlin, hybrid"),
        fact_item("must_have", "Python", "Python, PostgreSQL"),
        fact_item("must_have", "PostgreSQL", "Python, PostgreSQL"),
        fact_item("employment_type", "full_time", "full_time (structured)"),
    ]
    return {
        "not_a_job_ad": not_a_job_ad,
        "injection_observed": injection_observed,
        "facts": default_facts if facts is None else facts,
        "criteria": (
            [criterion_item(key, level=level_all) for key in CRITERIA] if criteria is None else criteria
        ),
        "summary_en": "A good fit for the backend role.",
        "summary_de": "Eine gute Passung für die Backend-Rolle.",
    }
