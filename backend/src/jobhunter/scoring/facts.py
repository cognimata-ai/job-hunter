"""Job facts and their stable fact ids (spec §11.4, L4157-4174).

The generator emits the job facts (`llm.schemas.generator.JobFacts`) with evidence; they are CV-independent.
Every fact that can bind gets the **fact id** `code:subject`, derived deterministically here, so that a critic
review, a confirmation and a cap all name the same item:

- an asserted `onsite_only`: `onsite_required:onsite`;
- a mandatory `required_languages[]` item: `mandatory_language:<code>`, or
  `mandatory_language:other:<fold(name)>` for the code `other`;
- a mandatory `degree`: `degree:degree`;
- a mandatory `certifications[]` item: `certification:<fold(name)>`;
- a required `security_clearance`: `security_clearance:clearance`;
- a required `work_permit`: `work_permit:work_permit`;
- the generator's top-level `not_a_job_ad`: `not_a_job_ad:ad` (`NOT_A_JOB_AD`).

Names are folded with `normalize.text.fold` (A3 G11): NFKD with the marks removed, so `Französisch` is
`franzosisch`. Two facts with one id are a V4 issue, and the first is kept (L4174). `not_a_job_ad` is not a
`JobFacts` field: it is the draft's top-level flag, so its id is a constant the callers add
(`knockouts.required_facts`).
"""

from dataclasses import dataclass
from typing import Final, Literal

from jobhunter.llm.schemas.generator import JobFacts, RequiredLanguage
from jobhunter.normalize.text import fold

# The §11.4 fact table (L4157-4172), in its order: the `JobFacts` fields and the top-level flag.
FactCode = Literal[
    "work_mode",
    "onsite_only",
    "remote_region",
    "required_languages",
    "must_haves",
    "nice_to_haves",
    "seniority",
    "min_years_experience",
    "industry",
    "employment_types",
    "degree",
    "certifications",
    "security_clearance",
    "work_permit",
    "not_a_job_ad",
]
FactSource = Literal["text", "structured"]


def fact_id(code: str, subject: str) -> str:
    """The stable id `code:subject`."""
    return f"{code}:{subject}"


@dataclass(frozen=True)
class FactRef:
    """A fact id with its parts: `code` is the knockout code (or `not_a_job_ad`) and `subject` what it names;
    `source` is `text` for a generator fact, `structured` for a provider's field."""

    fact_id: str
    code: str
    subject: str
    source: FactSource


def fact_ref(code: str, subject: str, source: FactSource = "text") -> FactRef:
    return FactRef(fact_id(code, subject), code, subject, source)


NOT_A_JOB_AD: Final = fact_ref("not_a_job_ad", "ad")


def language_subject(language: RequiredLanguage) -> str:
    """`<code>`, or `other:<fold(name)>` for the code `other`."""
    if language.language == "other":
        return f"other:{fold(language.language_other or '')}"
    return language.language


def derive_fact_ids(facts: JobFacts) -> dict[str, FactRef]:
    """The ids of every asserted fact that binds, in the fact table's order; the first of an id is kept."""
    found = [
        *([("onsite_required", "onsite")] if facts.onsite_only.asserted else []),
        *(("mandatory_language", language_subject(x)) for x in facts.required_languages if x.mandatory),
        *([("degree", "degree")] if facts.degree.mandatory else []),
        *(("certification", fold(x.name)) for x in facts.certifications if x.mandatory),
        *([("security_clearance", "clearance")] if facts.security_clearance.required else []),
        *([("work_permit", "work_permit")] if facts.work_permit.required else []),
    ]
    refs: dict[str, FactRef] = {}
    for code, subject in found:
        ref = fact_ref(code, subject)
        refs.setdefault(ref.fact_id, ref)
    return refs
