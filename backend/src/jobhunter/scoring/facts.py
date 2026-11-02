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
`franzosisch`. A name's subject (`name_subject`) is that folded form without control characters, cut to
`NAME_SUBJECT_MAX` characters (the §11.5 limit of a certification name), so a fact id is always one line of
at most 160 characters, as a prompt's issue target must be (`prompts.render.Issue`; Controller carry T4-d):
a name beyond its V1 limit is an issue, and its id is the cut name's. Every derivation of a name's id
(`binding_facts`, `knockouts.derive_knockouts`) goes through `name_subject`. Two facts with one id are a V4
issue, and the first is kept (L4174). `not_a_job_ad` is not a `JobFacts` field: it is the draft's top-level
flag, so its id is a constant the callers add (`knockouts.required_facts`).

`binding_facts(facts)` is the one walk of the asserted facts: each one's code, subject and path in the
`job_facts` document (the field name, and the index in a list), so `derive_fact_ids` and the critic's
rendering of a draft (`prompts.render.draft_block`) name the same facts. `fact_ids_by_place` keys the ids by
that path: a publication repair that cuts a name gives its fact another id at the same place, and the
publication carries the critic's review of the fact across it by the place (final review, Codex I2).
`FACT_ID_CODES` are the codes a fact id of a draft can have.
"""

import re
from collections.abc import Iterator
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


# The folded name of a subject is cut to this many characters (§11.5: a certification name is at most 120).
NAME_SUBJECT_MAX: Final = 120
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")


def name_subject(name: str) -> str:
    """The subject a name gives a fact id: `fold(name)` without control characters, cut to
    `NAME_SUBJECT_MAX` characters (and trimmed again after the cut)."""
    return _CONTROL.sub("", fold(name))[:NAME_SUBJECT_MAX].rstrip()


def language_subject(language: RequiredLanguage) -> str:
    """`<code>`, or `other:<name_subject(name)>` for the code `other`."""
    if language.language == "other":
        return f"other:{name_subject(language.language_other or '')}"
    return language.language


# The codes of the fact ids a draft can give: those of `binding_facts`, then the top-level flag's.
FACT_ID_CODES: Final = (
    "onsite_required",
    "mandatory_language",
    "degree",
    "certification",
    "security_clearance",
    "work_permit",
    NOT_A_JOB_AD.code,
)


@dataclass(frozen=True)
class BindingFact:
    """An asserted fact that binds: its fact-id code and subject, and its path in the `job_facts` document
    (`("onsite_only",)`, `("required_languages", 2)`, …)."""

    code: str
    subject: str
    path: tuple[str | int, ...]


def binding_facts(facts: JobFacts) -> Iterator[BindingFact]:
    """Every asserted fact that binds, in the fact table's order (a fact id may repeat; callers keep the
    first): an asserted `onsite_only`, a mandatory language, a mandatory degree, a mandatory certification, a
    required clearance, a required work permit."""
    if facts.onsite_only.asserted:
        yield BindingFact("onsite_required", "onsite", ("onsite_only",))
    for i, language in enumerate(facts.required_languages):
        if language.mandatory:
            yield BindingFact("mandatory_language", language_subject(language), ("required_languages", i))
    if facts.degree.mandatory:
        yield BindingFact("degree", "degree", ("degree",))
    for i, certification in enumerate(facts.certifications):
        if certification.mandatory:
            yield BindingFact("certification", name_subject(certification.name), ("certifications", i))
    if facts.security_clearance.required:
        yield BindingFact("security_clearance", "clearance", ("security_clearance",))
    if facts.work_permit.required:
        yield BindingFact("work_permit", "work_permit", ("work_permit",))


def fact_ids_by_place(facts: JobFacts) -> dict[tuple[str | int, ...], str]:
    """The fact id of every asserted fact that binds, by its place in the `job_facts` document
    (`BindingFact.path`): what a repair that cuts a name renames, the place stays (`publication.build`)."""
    return {fact.path: fact_id(fact.code, fact.subject) for fact in binding_facts(facts)}


def derive_fact_ids(facts: JobFacts) -> dict[str, FactRef]:
    """The ids of every asserted fact that binds, in the fact table's order; the first of an id is kept."""
    refs: dict[str, FactRef] = {}
    for fact in binding_facts(facts):
        ref = fact_ref(fact.code, fact.subject)
        refs.setdefault(ref.fact_id, ref)
    return refs
