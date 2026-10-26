"""The score validator (spec §11.6, adapted to candidate C; §11.7.2-11.7.3 for the critic). Pure functions of
the output and a `ValidationContext`: no connection, no clock, no logging (an issue holds codes and paths,
never ad text).

**Stage 1** (`parse_generator`) is the structural parse with the version's stored parser family (Task 4):
`GeneratorWire` checks types, enums, required keys and that no key is extra, with no length or count limit;
its failure (`StructuralFailure`) is the only fatal outcome (§9.4.2: one structural repair per attempt). A
`generator_v1` version raises `PromptVersionUnscorable` before the output is read. The normaliser then builds
the internal draft (`GeneratorDraft`, v1's shape) and returns what it found as a `Normalisation`, which Task 9
stores beside the draft and merges into every later stage 2:
- a single-valued code counts once (the first item; a later one is the repair `duplicate_fact`);
  `remote_country` items are `remote_region.countries`, a `degree_or_equivalent` item sets `or_equivalent`,
  `employment_type` items are `employment_types.values`, and their quotes join the fact's evidence;
- `required_language` values are a language code or `other:<name>`, the qualifier a CEFR level (an invalid
  level is `unspecified` with the issue `qualifier_invalid`); `min_years_experience` is a whole number from
  0 to 40, else `null` with the repair and the issue `out_of_range` (L4390, L4347);
- a value outside its code's vocabulary is `unspecified` (a single-valued code) or dropped (a list item) with
  the issue `value_invalid`, as is a certification without a name (its fact id would be empty);
- more than 2 quotes on one fact item is the V1 issue `too_many_quotes` (a merged fact's evidence may hold
  more: the limit is per item, as C states facts);
- `criteria` are keyed by `criterion`, in §11.2's order: a missing key is `unknown`/`null` with the
  deterministic failure `criterion_missing` (A3 G3: never a parse failure), a duplicate keeps the first (the
  repair `duplicate_criterion`), and either is the V1 issue `criterion_count` ("exactly one item per
  criterion key"); a profile reference keeps any section string, which V3 fails when it is unknown.
`parse_critic` is the critic's mirror alone.

**Stage 2** (`validate_draft`) never raises. In order:
1. V4's repairable corrections of the facts (each a `Repair` with the value it replaced when that is a code
   or a number, never ad text; no trigger): a second fact of one fact id is dropped (`duplicate_fact_id`), a
   `work_mode` that contradicts the structured work modes becomes the single structured one, or
   `unspecified` when there are several (`work_mode_structured`), and a `remote_region` value that
   contradicts its countries (L4370) becomes `unspecified` (`remote_region_contradiction`);
2. V3 over the facts' quotes; then V4's deterministic `experience_seniority` (`det_criteria.seniority_level`
   over the facts whose quotes ground, and the title's seniority tokens): a different level is replaced
   (`seniority_recomputed`), with basis `observed` and the grounded fact quotes as its ad evidence (its own
   when the facts give none), or `unknown`/`null` with no evidence when both sub-ratings are unknown (A3 G13).
   A draft that says it is no job ad is never rated;
3. V3 over the criteria's quotes. A quote grounds in one segment of the rendered `<job_ad>` (`ground_quote`:
   fold on both sides, a structured field's line or a description part, never a `marker` segment, so never
   across the truncation marker or from a field into the description); a description match maps back to its
   span in `job_revisions.description_text`. A profile quote grounds in the canonical serialisation of its
   named section (`profile_sections`). A quote with an ellipsis, under 4 or over 300 characters, or with
   fewer than 3 letters or digits fails (`quote_defect`, L4337, L4387), as does an unknown section
   (`profile_section_unknown`). A failing quote is a `Failure` of its criterion or fact and an `Issue` at its
   path; it stays in the draft (so a re-validation finds it again) and `repair_for_publication` drops it;
4. V2: `unknown ⇔ level = null`, `unconstrained ⇒ level = 4` with an ad quote, `observed ⇒` a level and an
   ad quote (and a profile reference from level 1), every asserted fact with a quote;
5. V4's consistency failures (L4373-4378): `languages_vs_unmet`, `languages_vs_met` (with at least one
   mandatory language), `skills_vs_must_have` (the overlap coefficient below 0.5 against every profile
   skill, Controller Ruling T3-b), `languages_unconstrained_with_requirements`, and one
   `not_a_job_ad_with_levels` per rated criterion of a draft that says it is no job ad;
6. V1, the §11.5 limits of the draft (each an `Issue`, never a failure);
7. V5 (`injection.detect` over the rendered block, so the title too) and V6 (`not_a_job_ad` adds the review
   `not_a_job_ad:ad`), each an issue.
The stage-1 normalisation merges first: its issues before V1's, its failures before V2's, its repairs before
V4's. `DraftValidation.t1` is T1's condition (a limit issue or any failure; a repair is none).

**Keys.** A failure names a criterion key, a fact id (`facts.derive_fact_ids`: the binding facts), or the
pseudo key of an id-less fact (A3 G12): its C code (`work_mode`, `remote_region`, `employment_type`, …), or
`<code>:<n>` for the `n`-th item (0-based) of `must_haves`, `nice_to_haves`, `required_languages` and
`certifications` (`must_have:0`, `required_language:2`). A pseudo key is no `render.Issue` target.

**History.** `CorrectionHistory` is the normalisation and the first validation's corrections, immutable: a
later validation of the stored (corrected) draft repairs nothing, so the caller passes the stored history
and it is carried into publication (Codex r3 B10).

**Publication** (`repair_for_publication`, L4380-4392): every length-limited string cut at its limit ending
in `[…]`, every count-limited list cut to its first N, a number out of range `null`, every failing quote and
invalid country code dropped and each evidence list cut to its first 2 remaining quotes; `truncated_fields`
lists the history's repairs, then what publication cut.

**The critic** (`validate_review`, §11.7.2): a target is an LLM criterion key with a criterion assessment,
or a required fact id with a `fact_*` assessment; a review needs a grounded ad quote (the first 2 grounded
count), else it is discarded; the derived outcome follows §11.7.2 with V6 special-cased first (L4365).
"""

import functools
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from typing import Final, Literal, Self, get_args, get_origin

from pydantic import BaseModel, ValidationError

from jobhunter.db.seed import canonical_json
from jobhunter.llm.prompts.render import JobAdBlock, PromptVersionUnscorable, Segment
from jobhunter.llm.schemas import ParserFamily, mirror_for
from jobhunter.llm.schemas.critic import CriticOut, Review
from jobhunter.llm.schemas.generator import (
    Certification,
    CriterionItem,
    Degree,
    DraftCriteria,
    DraftCriterion,
    EmploymentTypes,
    FactItem,
    GeneratorDraft,
    GeneratorWire,
    Industry,
    JobFacts,
    MinYearsExperience,
    OnsiteOnly,
    ProfileRef,
    ProfileRefWire,
    RemoteRegion,
    RequiredLanguage,
    Requirement,
    SecurityClearance,
    Seniority,
    WorkMode,
    WorkPermit,
)
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.normalize.text import fold, fold_with_offsets
from jobhunter.scoring import injection
from jobhunter.scoring.det_criteria import MIN_YEARS_RANGE, seniority_level
from jobhunter.scoring.facts import NOT_A_JOB_AD, FactRef, binding_facts, derive_fact_ids, fact_id
from jobhunter.scoring.filters import DetFacts
from jobhunter.scoring.knockouts import Confirmation, compare_language, token_set_similarity
from jobhunter.scoring.rubric import LLM_CRITERIA
from jobhunter.types import JSONValue, get_list, get_object, get_str

# ---- the limits (§11.5 L4330-4347, §11.7.3 L4463-4464) -----------------------------------------------------

SUMMARY_MAX_WORDS: Final = 60
SUMMARY_MAX_CHARS: Final = 450
RATIONALE_MAX_WORDS: Final = 25
RATIONALE_MAX_CHARS: Final = 200
QUOTES_PER_CRITERION: Final = 2  # ad quotes, and profile references, per criterion
QUOTES_PER_FACT: Final = 2
QUOTE_MIN_CHARS: Final = 4
QUOTE_MAX_CHARS: Final = 300
QUOTE_MIN_ALNUM: Final = 3
REQUIREMENTS_MAX: Final = 25  # must_haves, nice_to_haves each
REQUIREMENT_MAX_CHARS: Final = 200
NAME_MAX_CHARS: Final = 120  # a certification name, industry.value, degree.field
LANGUAGE_OTHER_MAX_CHARS: Final = 40
CLEARANCE_LEVEL_MAX_CHARS: Final = 60
LANGUAGES_MAX: Final = 6
CERTIFICATIONS_MAX: Final = 10
COUNTRIES_MAX: Final = 30
EXPLANATION_MAX_WORDS: Final = 40
EXPLANATION_MAX_CHARS: Final = 300
CRITIC_SUMMARY_MAX_WORDS: Final = 60
QUOTES_PER_REVIEW: Final = 2
CRITERION_DISPUTES_MAX: Final = 6
SKILL_MATCH: Final = 0.5  # V4: a must-have matches a profile skill from this overlap coefficient
TRUNCATION_MARK: Final = "[…]"

# The V1 issue codes (T1); every other issue (V3's per quote, V5, V6) routes otherwise.
LIMIT_ISSUE_CODES: Final = frozenset(
    {
        "too_many_words",
        "too_many_chars",
        "too_many_items",
        "too_many_quotes",
        "out_of_range",
        "value_invalid",
        "qualifier_invalid",
        "country_invalid",
        "criterion_count",
    }
)
# The V3 failure codes of one quote (each is also the issue code at the quote's path).
QUOTE_FAILURE_CODES: Final = frozenset(
    {
        "quote_ellipsis",
        "quote_too_long",
        "quote_too_short",
        "quote_too_few_alnum",
        "quote_ungrounded",
        "profile_section_unknown",
    }
)
PROFILE_SECTIONS: Final[tuple[str, ...]] = get_args(ProfileRef.model_fields["section"].annotation)
CRITERION_ASSESSMENTS: Final = frozenset(
    {
        "level_too_high",
        "level_too_low",
        "evidence_ungrounded",
        "should_be_unknown",
        "evidence_overlooked",
        "anchor_misapplied",
    }
)
FACT_ASSESSMENTS: Final = frozenset({"fact_confirmed", "fact_rejected", "fact_uncertain"})


# ---- the records -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    """A finding at a place of the draft (`path`, as `criteria.skills.ad_evidence[1]`): a limit (V1), a
    failing quote (V3), the injection suspicion (V5) or the not-a-job-ad review (V6). `detail` holds codes,
    keys and numbers only."""

    code: str
    path: str
    detail: dict[str, JSONValue] = field(default_factory=dict)


@dataclass(frozen=True)
class Failure:
    """A deterministic failure (V2, V3, a V4 consistency failure, or the stage-1 `criterion_missing`) of a
    criterion or fact (module docstring, Keys). It blocks acceptance."""

    key: str
    code: str


@dataclass(frozen=True)
class Repair:
    """A correction (the normaliser's, or V4's repairable ones), with the value it replaced when that is a
    code or a number (`None` otherwise: never ad text)."""

    path: str
    code: str
    replaced: JSONValue = None


def _issue_json(issue: Issue) -> dict[str, JSONValue]:
    return {"code": issue.code, "path": issue.path, "detail": dict(issue.detail)}


def _failure_json(failure: Failure) -> dict[str, JSONValue]:
    return {"key": failure.key, "code": failure.code}


def _repair_json(repair: Repair) -> dict[str, JSONValue]:
    return {"path": repair.path, "code": repair.code, "replaced": repair.replaced}


def _issue_of(doc: JSONValue) -> Issue:
    return Issue(get_str(doc, "code"), get_str(doc, "path"), get_object(doc, "detail"))


def _failure_of(doc: JSONValue) -> Failure:
    return Failure(get_str(doc, "key"), get_str(doc, "code"))


def _repair_of(doc: JSONValue) -> Repair:
    replaced = get_object(doc, "").get("replaced")
    return Repair(get_str(doc, "path"), get_str(doc, "code"), replaced)


@dataclass(frozen=True)
class Normalisation:
    """What stage 1 found: its issues (merged into V1's), failures (into V2's) and repairs (into V4's)."""

    issues: list[Issue] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    repairs: list[Repair] = field(default_factory=list)

    def to_json(self) -> dict[str, JSONValue]:
        """The stored form: codes, keys and paths only."""
        return {
            "issues": [_issue_json(x) for x in self.issues],
            "failures": [_failure_json(x) for x in self.failures],
            "repairs": [_repair_json(x) for x in self.repairs],
        }

    @classmethod
    def from_json(cls, doc: JSONValue) -> Self:
        """The normalisation `to_json` stored; `ValueError` for any other shape."""
        try:
            return cls(
                issues=[_issue_of(x) for x in get_list(doc, "issues")],
                failures=[_failure_of(x) for x in get_list(doc, "failures")],
                repairs=[_repair_of(x) for x in get_list(doc, "repairs")],
            )
        except (KeyError, TypeError) as error:
            raise ValueError(f"normalisation: not a stored normalisation ({error})") from error


@dataclass(frozen=True)
class CorrectionHistory:
    """The normaliser's and the first validation's repairs, immutable (module docstring, History)."""

    normalisation: Normalisation
    corrections: list[Repair]

    def to_json(self) -> dict[str, JSONValue]:
        return {
            "normalisation": self.normalisation.to_json(),
            "corrections": [_repair_json(x) for x in self.corrections],
        }

    @classmethod
    def from_json(cls, doc: JSONValue) -> Self:
        """The history `to_json` stored; `ValueError` for any other shape."""
        try:
            corrections = [_repair_of(x) for x in get_list(doc, "corrections")]
            return cls(Normalisation.from_json(get_object(doc, "normalisation")), corrections)
        except (KeyError, TypeError) as error:
            raise ValueError(f"correction history: not a stored history ({error})") from error


@dataclass(frozen=True)
class StructuralFailure:
    """The structural parse failed: pydantic's error type and the dotted path of the first error."""

    error_class: str
    path: str


@dataclass(frozen=True)
class ParsedGenerator:
    draft: GeneratorDraft
    normalisation: Normalisation


@dataclass(frozen=True)
class Grounding:
    """Where a quote grounds: a description span of `job_revisions.description_text`, a structured field
    (by name, no span), or a profile section (`field_name` is the section)."""

    source_kind: Literal["description", "structured_field", "profile"]
    field_name: str | None
    span_start: int | None
    span_end: int | None


@dataclass(frozen=True)
class ValidationContext:
    """What stage 2 validates against: the rendered `<job_ad>`, the confirmed profile with the canonical
    serialisation of each named section (`profile_sections`), the job's `DetFacts`, its structured work modes
    and its description completeness."""

    ad: JobAdBlock
    profile: ConfirmedProfile
    profile_sections: Mapping[str, str]
    det: DetFacts
    structured_work_modes: Sequence[str]
    completeness: str


@dataclass(frozen=True)
class DraftValidation:
    """The result of stage 2. `draft` is the corrected draft (the caller writes it back); `groundings` holds,
    per evidence list (`criteria.skills.ad_evidence`, `job_facts.must_haves[0].evidence`, …), the groundings
    of its quotes that ground, in order, which are the quotes publication keeps; `fact_ids` are the draft's
    (with `not_a_job_ad:ad` when it says it is no job ad)."""

    draft: GeneratorDraft
    issues: list[Issue]
    failures: list[Failure]
    repairs: list[Repair]
    groundings: dict[str, list[Grounding]]
    fact_ids: dict[str, FactRef]
    injection_suspected: bool
    history: CorrectionHistory

    @property
    def t1(self) -> bool:
        """T1's condition (§11.7.1): a V1 limit issue, or a V2/V3 or V4 consistency failure."""
        return bool(self.failures) or any(issue.code in LIMIT_ISSUE_CODES for issue in self.issues)


@dataclass(frozen=True)
class Dispute:
    """A grounded criterion dispute of the critic."""

    target: str
    assessment: str
    suggested_level: int | None
    groundings: list[Grounding]
    explanation: str


FactReview = Literal["critic_confirmed", "critic_rejected", "uncertain"]


@dataclass(frozen=True)
class CriticValidation:
    """The validated critic review (§11.7.2): the derived outcome, the grounded disputes, each required fact's
    review, the reviews discarded for want of a grounded quote, the other issues (limits, targets, quotes)."""

    derived_outcome: Literal["accept", "revise", "incomplete"]
    grounded_disputes: list[Dispute]
    fact_reviews: dict[str, FactReview]
    discarded: list[Issue]
    issues: list[Issue]
    complete: bool
    verdict_mismatch: bool
    not_a_job_ad_verified: bool


# ---- stage 1 -----------------------------------------------------------------------------------------------


def _structural(error: ValidationError) -> StructuralFailure:
    first = error.errors()[0]
    return StructuralFailure(first["type"], ".".join(str(step) for step in first["loc"]))


def _model(wire: JSONValue, model: type[BaseModel]) -> BaseModel | StructuralFailure:
    try:
        return model.model_validate(wire)
    except ValidationError as error:
        return _structural(error)


def parse_generator(wire: JSONValue, *, family: ParserFamily) -> ParsedGenerator | StructuralFailure:
    """Stage 1 of a generator output of parser family `family` (module docstring): the structural mirror,
    then the normaliser. `generator_v1` raises `PromptVersionUnscorable` before `wire` is read; a family this
    module has no normaliser for is a `ValueError`."""
    if family == "generator_v1":
        raise PromptVersionUnscorable(None, family)
    if mirror_for(family) is not GeneratorWire:
        raise ValueError(f"no normaliser for the parser family {family!r}")
    parsed = _model(wire, GeneratorWire)
    if isinstance(parsed, StructuralFailure):
        return parsed
    if not isinstance(parsed, GeneratorWire):  # pragma: no cover - the mirror's type
        raise TypeError("the generator mirror returned another model")
    return _normalise(parsed)


def parse_critic(wire: JSONValue) -> CriticOut | StructuralFailure:
    """The critic's structural parse (§11.7.3): the mirror alone."""
    parsed = _model(wire, CriticOut)
    if isinstance(parsed, StructuralFailure | CriticOut):
        return parsed
    raise TypeError("the critic mirror returned another model")  # pragma: no cover


_SINGLE_CODES: Final = frozenset(
    {
        "work_mode",
        "onsite_only",
        "remote_region",
        "seniority",
        "min_years_experience",
        "industry",
        "degree",
        "degree_or_equivalent",
        "security_clearance",
        "work_permit",
    }
)
_CODE_PATH: Final = {
    "work_mode": "job_facts.work_mode",
    "onsite_only": "job_facts.onsite_only",
    "remote_region": "job_facts.remote_region",
    "seniority": "job_facts.seniority",
    "min_years_experience": "job_facts.min_years_experience",
    "industry": "job_facts.industry",
    "degree": "job_facts.degree",
    "degree_or_equivalent": "job_facts.degree",
    "security_clearance": "job_facts.security_clearance",
    "work_permit": "job_facts.work_permit",
}


def _vocabulary(model: type[BaseModel], name: str) -> dict[str, str]:
    """The values of a mirror's enum field (or of its list's items), by their lower-case form."""
    annotation = model.model_fields[name].annotation
    values = get_args(get_args(annotation)[0]) if get_origin(annotation) is list else get_args(annotation)
    return {str(value).lower(): str(value) for value in values}


# The single-valued codes with a vocabulary: the draft field their value fills, and the vocabulary.
_SINGLE_VOCABULARIES: Final[Mapping[str, tuple[str, Mapping[str, str]]]] = {
    "work_mode": ("value", _vocabulary(WorkMode, "value")),
    "remote_region": ("value", _vocabulary(RemoteRegion, "value")),
    "seniority": ("value", _vocabulary(Seniority, "value")),
    "degree": ("level", _vocabulary(Degree, "level")),
}
_EMPLOYMENT_TYPES: Final = _vocabulary(EmploymentTypes, "values")
_CEFR: Final = _vocabulary(RequiredLanguage, "level")
_LANGUAGES: Final = frozenset(_vocabulary(RequiredLanguage, "language")) - {"other"}
_OTHER_PREFIX: Final = "other:"
_WHOLE_NUMBER: Final = re.compile(r"-?[0-9]{1,9}")


def _blank_to_none(text: str) -> str | None:
    return text if text.strip() else None


class _Normaliser:
    """The state of one normalisation: the draft's facts as they are built, and what was found."""

    def __init__(self) -> None:
        self.issues: list[Issue] = []
        self.failures: list[Failure] = []
        self.repairs: list[Repair] = []
        self.single: dict[str, FactItem] = {}
        self.read: dict[str, str] = {}  # the vocabulary value of each single-valued code read so far
        self.years: int | None = None
        self.countries: list[FactItem] = []
        self.languages: list[RequiredLanguage] = []
        self.must_haves: list[Requirement] = []
        self.nice_to_haves: list[Requirement] = []
        self.employment: list[FactItem] = []
        self.employment_values: list[str] = []
        self.certifications: list[Certification] = []

    def invalid(self, path: str, code: str, *, qualifier: bool = False) -> None:
        self.issues.append(Issue("qualifier_invalid" if qualifier else "value_invalid", path, {"code": code}))

    def quotes(self, item: FactItem, path: str) -> None:
        if len(item.evidence) > QUOTES_PER_FACT:
            self.issues.append(Issue("too_many_quotes", f"{path}.evidence", {"max_quotes": QUOTES_PER_FACT}))

    def add(self, item: FactItem) -> None:
        code = item.code
        if code in _SINGLE_CODES:
            if code in self.single:
                self.repairs.append(Repair(_CODE_PATH[code], "duplicate_fact"))
                return
            self.single[code] = item
            self.read_single(code, item)
            self.quotes(item, _CODE_PATH[code])
        elif code == "remote_country":
            self.countries.append(item)
            self.quotes(item, "job_facts.remote_region")
        elif code == "required_language":
            self.language(item)
        elif code in ("must_have", "nice_to_have"):
            target = self.must_haves if code == "must_have" else self.nice_to_haves
            self.quotes(item, f"job_facts.{code}s[{len(target)}]")
            target.append(Requirement(text=item.value, evidence=list(item.evidence)))
        elif code == "employment_type":
            self.employment.append(item)
            self.quotes(item, "job_facts.employment_types")
            value = _EMPLOYMENT_TYPES.get(item.value.strip().lower())
            if value is None:
                self.invalid("job_facts.employment_types.values", code)
            elif value not in self.employment_values:
                self.employment_values.append(value)
        elif code == "certification":
            if not fold(item.value):
                self.invalid("job_facts.certifications", code)
                return
            self.quotes(item, f"job_facts.certifications[{len(self.certifications)}]")
            self.certifications.append(
                Certification(name=item.value, mandatory=item.required, evidence=list(item.evidence))
            )
        else:  # pragma: no cover - the mirror's enum
            raise ValueError(f"no normalisation of the code {code!r}")

    def language(self, item: FactItem) -> None:
        text = item.value.strip()
        other: str | None = None
        if text.lower().startswith(_OTHER_PREFIX):
            other = text[len(_OTHER_PREFIX) :].strip()
            code = "other" if fold(other) else ""
        else:
            code = text.lower() if text.lower() in _LANGUAGES else ""
        if not code:
            self.invalid("job_facts.required_languages", "required_language")
            return
        path = f"job_facts.required_languages[{len(self.languages)}]"
        level = _CEFR.get(item.qualifier.strip().lower())
        if level is None:
            self.invalid(f"{path}.level", "required_language", qualifier=True)
        self.quotes(item, path)
        self.languages.append(
            RequiredLanguage.model_validate(
                {
                    "language": code,
                    "language_other": other,
                    "level": level or "unspecified",
                    "mandatory": item.required,
                    "evidence": list(item.evidence),
                }
            )
        )

    def read_single(self, code: str, item: FactItem) -> None:
        """The value of a single-valued code, read as its item comes (so every issue is in the output's
        order): a vocabulary value, or the minimum years."""
        if code == "min_years_experience":
            text = item.value.strip()
            low, high = MIN_YEARS_RANGE
            if _WHOLE_NUMBER.fullmatch(text) and low <= (years := int(text)) <= high:
                self.years = years
                return
            path = "job_facts.min_years_experience.value"
            self.repairs.append(Repair(path, "out_of_range"))
            self.issues.append(Issue("out_of_range", path, {"min": low, "max": high}))
            return
        vocabulary = _SINGLE_VOCABULARIES.get(code)
        if vocabulary is None:
            return
        field_name, values = vocabulary
        value = values.get(item.value.strip().lower())
        if value is None:
            self.invalid(f"{_CODE_PATH[code]}.{field_name}", code)
        self.read[code] = value or "unspecified"

    def evidence(self, code: str) -> list[str]:
        item = self.single.get(code)
        return [] if item is None else list(item.evidence)

    def job_facts(self) -> JobFacts:
        single = self.single
        work_mode = self.read.get("work_mode", "unspecified")
        region = self.read.get("remote_region", "unspecified")
        seniority = self.read.get("seniority", "unspecified")
        degree_level = self.read.get("degree", "unspecified")
        degree = single.get("degree")
        equivalent = single.get("degree_or_equivalent")
        clearance = single.get("security_clearance")
        industry = single.get("industry")
        return JobFacts.model_validate(
            {
                "work_mode": WorkMode.model_validate(
                    {"value": work_mode, "evidence": self.evidence("work_mode")}
                ),
                "onsite_only": OnsiteOnly(
                    asserted=single["onsite_only"].required if "onsite_only" in single else False,
                    evidence=self.evidence("onsite_only"),
                ),
                "remote_region": RemoteRegion.model_validate(
                    {
                        "value": region,
                        "countries": [item.value.strip().upper() for item in self.countries],
                        "evidence": self.evidence("remote_region")
                        + [quote for item in self.countries for quote in item.evidence],
                    }
                ),
                "required_languages": self.languages,
                "must_haves": self.must_haves,
                "nice_to_haves": self.nice_to_haves,
                "seniority": Seniority.model_validate(
                    {"value": seniority, "evidence": self.evidence("seniority")}
                ),
                "min_years_experience": MinYearsExperience(
                    value=self.years, evidence=self.evidence("min_years_experience")
                ),
                "industry": Industry(
                    value=None if industry is None else _blank_to_none(industry.value),
                    evidence=self.evidence("industry"),
                ),
                "employment_types": EmploymentTypes.model_validate(
                    {
                        "values": self.employment_values,
                        "evidence": [quote for item in self.employment for quote in item.evidence],
                    }
                ),
                "degree": Degree.model_validate(
                    {
                        "level": degree_level,
                        "mandatory": degree.required if degree is not None else False,
                        "or_equivalent": equivalent is not None,
                        "field": None if degree is None else _blank_to_none(degree.qualifier),
                        "evidence": self.evidence("degree") + self.evidence("degree_or_equivalent"),
                    }
                ),
                "certifications": self.certifications,
                "security_clearance": SecurityClearance(
                    required=clearance.required if clearance is not None else False,
                    level=None if clearance is None else _blank_to_none(clearance.qualifier),
                    evidence=self.evidence("security_clearance"),
                ),
                "work_permit": WorkPermit(
                    required=single["work_permit"].required if "work_permit" in single else False,
                    evidence=self.evidence("work_permit"),
                ),
            }
        )

    def criteria(self, items: Sequence[CriterionItem]) -> DraftCriteria:
        first: dict[str, CriterionItem] = {}
        count: dict[str, int] = dict.fromkeys(LLM_CRITERIA, 0)
        for item in items:
            count[item.criterion] += 1
            if item.criterion in first:
                self.repairs.append(Repair(f"criteria.{item.criterion}", "duplicate_criterion"))
            else:
                first[item.criterion] = item
        rated: dict[str, DraftCriterion] = {}
        for key in LLM_CRITERIA:
            if count[key] != 1:
                self.issues.append(Issue("criterion_count", f"criteria.{key}", {"count": count[key]}))
            chosen = first.get(key)
            if chosen is None:
                self.failures.append(Failure(key, "criterion_missing"))
                rated[key] = DraftCriterion(
                    level=None, basis="unknown", ad_evidence=[], profile_evidence=[], rationale=""
                )
            else:
                rated[key] = DraftCriterion(
                    level=chosen.level,
                    basis=chosen.basis,
                    ad_evidence=list(chosen.ad_evidence),
                    profile_evidence=list(chosen.profile_evidence),
                    rationale=chosen.rationale,
                )
        return DraftCriteria.model_validate(rated)


def _normalise(wire: GeneratorWire) -> ParsedGenerator:
    run = _Normaliser()
    for item in wire.facts:
        run.add(item)
    job_facts = run.job_facts()
    criteria = run.criteria(wire.criteria)
    draft = GeneratorDraft(
        not_a_job_ad=wire.not_a_job_ad,
        injection_observed=wire.injection_observed,
        job_facts=job_facts,
        criteria=criteria,
        summary_en=wire.summary_en,
        summary_de=wire.summary_de,
    )
    return ParsedGenerator(draft, Normalisation(run.issues, run.failures, run.repairs))


# ---- grounding (V3) ----------------------------------------------------------------------------------------

_INVISIBLE: Final = frozenset(
    "\N{SOFT HYPHEN}\N{ZERO WIDTH SPACE}\N{ZERO WIDTH NON-JOINER}\N{ZERO WIDTH JOINER}"
    "\N{ZERO WIDTH NO-BREAK SPACE}"
)  # removed by `fold`


def quote_defect(quote: str) -> str | None:
    """Why `quote` can never ground (L4337, L4387), or `None`: `quote_ellipsis` (`…` or `...`),
    `quote_too_long` (over 300 characters), `quote_too_short` (under 4 characters, folded) or
    `quote_too_few_alnum` (under 3 letters or digits)."""
    folded = fold(quote)
    if "…" in quote or "..." in folded:
        return "quote_ellipsis"
    if len(quote) > QUOTE_MAX_CHARS:
        return "quote_too_long"
    if len(folded) < QUOTE_MIN_CHARS:
        return "quote_too_short"
    if sum(char.isalnum() for char in folded) < QUOTE_MIN_ALNUM:
        return "quote_too_few_alnum"
    return None


@dataclass(frozen=True)
class _FoldedSegment:
    segment: Segment
    text: str
    folded: str
    offsets: list[int]


@functools.lru_cache(maxsize=16)
def _folded_segments(ad: JobAdBlock) -> tuple[_FoldedSegment, ...]:
    """The block's quotable segments (every one but its `marker` segments), each folded with its offsets."""
    segments = []
    for segment in ad.segments:
        if segment.kind == "marker":
            continue
        text = ad.text[segment.block_start : segment.block_end]
        folded, offsets = fold_with_offsets(text)
        segments.append(_FoldedSegment(segment, text, folded, offsets))
    return tuple(segments)


def _span(part: _FoldedSegment, at: int, length: int) -> tuple[int, int]:
    """The span of the segment's text that the folded characters `[at, at + length)` came from."""
    last = at + length - 1
    start = part.offsets[at]
    end = part.offsets[last + 1] if last + 1 < len(part.offsets) else len(part.text)
    end = max(end, part.offsets[last] + 1)  # a match ending inside one character's folded form takes it all
    while end > part.offsets[last] + 1 and (part.text[end - 1].isspace() or part.text[end - 1] in _INVISIBLE):
        end -= 1
    return start, end


def _ground_ad(folded_quote: str, ad: JobAdBlock) -> Grounding | None:
    for part in _folded_segments(ad):
        at = part.folded.find(folded_quote)
        if at < 0:
            continue
        segment = part.segment
        if segment.kind == "structured_field":
            return Grounding("structured_field", segment.field_name, None, None)
        start, end = _span(part, at, len(folded_quote))
        source = segment.source_start or 0
        return Grounding("description", None, source + start, source + end)
    return None


def ground_quote(
    quote: str, ad: JobAdBlock, profile_sections: Mapping[str, str], *, section: str | None = None
) -> Grounding | None:
    """Where `quote` grounds (module docstring, stage 2 step 3): in the `<job_ad>` block, or with `section`
    in that profile section's canonical serialisation; `None` when it does not, or cannot (`quote_defect`,
    an unknown section)."""
    if quote_defect(quote) is not None:
        return None
    folded = fold(quote)
    if section is None:
        return _ground_ad(folded, ad)
    text = profile_sections.get(section)
    if text is None or folded not in fold(text):
        return None
    return Grounding("profile", section, None, None)


def profile_sections(profile: ConfirmedProfile) -> dict[str, str]:
    """The canonical serialisation (`db.seed.canonical_json`, as `<candidate_profile>` holds the profile) of
    each section a profile reference can name, in the schema's order."""
    doc = profile.model_dump(mode="json", exclude={"user_entered"})
    return {name: canonical_json(doc[name]).decode("utf-8") for name in PROFILE_SECTIONS}


# ---- stage 2 -----------------------------------------------------------------------------------------------


def _words(text: str) -> int:
    return len(text.split())


@functools.cache
def _iso_countries() -> frozenset[str]:
    """The ISO 3166-1 alpha-2 codes: those of the packaged country names (every code but DE) and DE."""
    text = resources.files("jobhunter.geo").joinpath("data", "country_names_de.tsv").read_text("utf-8")
    codes = {line.split("\t", 1)[0] for line in text.splitlines() if line and not line.startswith("#")}
    return frozenset(codes | {"DE"})


@dataclass(frozen=True)
class _Fact:
    """One fact of the draft for V2 and V3: its key, the path of its evidence list, its quotes, and whether it
    is asserted."""

    key: str
    path: str
    evidence: list[str]
    asserted: bool


def _facts(facts: JobFacts) -> Iterator[_Fact]:
    """The draft's facts in the fact table's order, each with its fact id or pseudo key."""
    ids = {fact.path: fact_id(fact.code, fact.subject) for fact in binding_facts(facts)}

    def fact(path: tuple[str | int, ...], pseudo: str, evidence: list[str], asserted: bool) -> _Fact:
        dotted = "job_facts." + "".join(f"[{step}]" if isinstance(step, int) else step for step in path)
        return _Fact(ids.get(path, pseudo), f"{dotted}.evidence", evidence, asserted)

    def stated(value: str | None) -> bool:
        return value is not None and bool(value.strip())

    yield fact(("work_mode",), "work_mode", facts.work_mode.evidence, facts.work_mode.value != "unspecified")
    yield fact(("onsite_only",), "onsite_only", facts.onsite_only.evidence, facts.onsite_only.asserted)
    region = facts.remote_region
    asserted = region.value != "unspecified" or bool(region.countries)
    yield fact(("remote_region",), "remote_region", region.evidence, asserted)
    for i, language in enumerate(facts.required_languages):
        yield fact(("required_languages", i), f"required_language:{i}", language.evidence, True)
    for i, must in enumerate(facts.must_haves):
        yield fact(("must_haves", i), f"must_have:{i}", must.evidence, True)
    for i, nice in enumerate(facts.nice_to_haves):
        yield fact(("nice_to_haves", i), f"nice_to_have:{i}", nice.evidence, True)
    seniority = facts.seniority
    yield fact(("seniority",), "seniority", seniority.evidence, seniority.value != "unspecified")
    years = facts.min_years_experience
    yield fact(("min_years_experience",), "min_years_experience", years.evidence, years.value is not None)
    industry = facts.industry
    yield fact(("industry",), "industry", industry.evidence, stated(industry.value))
    employment = facts.employment_types
    yield fact(("employment_types",), "employment_type", employment.evidence, bool(employment.values))
    degree = facts.degree
    asserted = (
        degree.mandatory or degree.level != "unspecified" or stated(degree.field) or degree.or_equivalent
    )
    yield fact(("degree",), "degree", degree.evidence, asserted)
    for i, certification in enumerate(facts.certifications):
        yield fact(("certifications", i), f"certification:{i}", certification.evidence, True)
    clearance = facts.security_clearance
    asserted = clearance.required or stated(clearance.level)
    yield fact(("security_clearance",), "security_clearance", clearance.evidence, asserted)
    yield fact(("work_permit",), "work_permit", facts.work_permit.evidence, facts.work_permit.required)


class _Validation:
    """The state of one stage-2 run."""

    def __init__(self, ctx: ValidationContext) -> None:
        self.ctx = ctx
        self.repairs: list[Repair] = []
        self.quote_issues: list[Issue] = []
        self.quote_failures: list[Failure] = []
        self.groundings: dict[str, list[Grounding]] = {}
        self.grounded: dict[str, list[str]] = {}  # the grounded quotes of each evidence list

    def fail_quote(self, key: str, path: str, code: str) -> None:
        self.quote_issues.append(Issue(code, path, {"key": key}))
        if Failure(key, code) not in self.quote_failures:
            self.quote_failures.append(Failure(key, code))

    def ground(self, key: str, path: str, quotes: Sequence[str]) -> None:
        """V3 over the ad quotes of one evidence list."""
        groundings = self.groundings.setdefault(path, [])
        grounded = self.grounded.setdefault(path, [])
        for i, quote in enumerate(quotes):
            defect = quote_defect(quote)
            found = None if defect else ground_quote(quote, self.ctx.ad, self.ctx.profile_sections)
            if found is None:
                self.fail_quote(key, f"{path}[{i}]", defect or "quote_ungrounded")
            else:
                groundings.append(found)
                grounded.append(quote)

    def ground_profile(self, key: str, path: str, refs: Sequence[ProfileRefWire]) -> None:
        """V3 over the profile references of one criterion."""
        groundings = self.groundings.setdefault(path, [])
        for i, ref in enumerate(refs):
            if ref.section not in self.ctx.profile_sections:
                self.fail_quote(key, f"{path}[{i}]", "profile_section_unknown")
                continue
            defect = quote_defect(ref.quote)
            sections = self.ctx.profile_sections
            found = None if defect else ground_quote(ref.quote, self.ctx.ad, sections, section=ref.section)
            if found is None:
                self.fail_quote(key, f"{path}[{i}]", defect or "quote_ungrounded")
            else:
                groundings.append(found)


def _dedupe_fact_ids(facts: JobFacts, run: _Validation) -> JobFacts:
    """V4: a second fact of one fact id is dropped (the first is kept, L4174)."""
    dropped: dict[str, set[int]] = {"required_languages": set(), "certifications": set()}
    seen: set[str] = set()
    for fact in binding_facts(facts):
        name = fact_id(fact.code, fact.subject)
        if name not in seen:
            seen.add(name)
        elif len(fact.path) == 2 and isinstance(fact.path[1], int) and str(fact.path[0]) in dropped:
            list_name, index = str(fact.path[0]), fact.path[1]
            dropped[list_name].add(index)
            run.repairs.append(Repair(f"job_facts.{list_name}[{index}]", "duplicate_fact_id"))
    if not any(dropped.values()):
        return facts
    return facts.model_copy(
        update={
            "required_languages": [
                x for i, x in enumerate(facts.required_languages) if i not in dropped["required_languages"]
            ],
            "certifications": [
                x for i, x in enumerate(facts.certifications) if i not in dropped["certifications"]
            ],
        }
    )


_REMOTE_MODES: Final = ("remote", "hybrid", "onsite")


def _work_mode(facts: JobFacts, run: _Validation) -> JobFacts:
    """V4: a work mode the structured work modes do not include becomes the structured one (L4369)."""
    structured = [mode for mode in dict.fromkeys(run.ctx.structured_work_modes) if mode in _REMOTE_MODES]
    value = facts.work_mode.value
    if not structured or value == "unspecified" or value in structured:
        return facts
    replacement = structured[0] if len(structured) == 1 else "unspecified"
    run.repairs.append(Repair("job_facts.work_mode.value", "work_mode_structured", value))
    work_mode = facts.work_mode.model_copy(update={"value": replacement})
    return facts.model_copy(update={"work_mode": work_mode})


def _remote_region(facts: JobFacts, run: _Validation) -> JobFacts:
    """V4: `other_country` with DE among the countries, or `germany`/`dach` with countries that exclude DE,
    becomes `unspecified` (L4370)."""
    region = facts.remote_region
    countries = {country.strip().upper() for country in region.countries}
    contradicts = (region.value == "other_country" and "DE" in countries) or (
        region.value in ("germany", "dach") and bool(countries) and "DE" not in countries
    )
    if not contradicts:
        return facts
    run.repairs.append(Repair("job_facts.remote_region.value", "remote_region_contradiction", region.value))
    return facts.model_copy(update={"remote_region": region.model_copy(update={"value": "unspecified"})})


def _seniority(draft: GeneratorDraft, facts: JobFacts, run: _Validation) -> DraftCriteria:
    """V4: the deterministic `experience_seniority` replaces a different level (module docstring, step 2)."""
    criteria = draft.criteria
    if draft.not_a_job_ad:
        return criteria
    seniority = facts.seniority
    seniority_quotes = run.grounded.get("job_facts.seniority.evidence", [])
    years = facts.min_years_experience
    years_quotes = run.grounded.get("job_facts.min_years_experience.evidence", [])
    use_seniority = seniority.value != "unspecified" and bool(seniority_quotes)
    low, high = MIN_YEARS_RANGE
    use_years = years.value is not None and low <= years.value <= high and bool(years_quotes)
    level = seniority_level(
        run.ctx.profile,
        seniority if use_seniority else None,
        years if use_years else None,
        run.ctx.det.seniority_tokens,
    )
    current = criteria.experience_seniority
    if level == current.level:
        return criteria
    run.repairs.append(Repair("criteria.experience_seniority.level", "seniority_recomputed", current.level))
    if level is None:
        replaced = DraftCriterion(
            level=None, basis="unknown", ad_evidence=[], profile_evidence=[], rationale=current.rationale
        )
    else:
        quotes = (seniority_quotes if use_seniority else []) + (years_quotes if use_years else [])
        evidence = list(dict.fromkeys(quotes)) or list(current.ad_evidence)
        replaced = current.model_copy(update={"level": level, "basis": "observed", "ad_evidence": evidence})
    return criteria.model_copy(update={"experience_seniority": replaced})


def _criteria(criteria: DraftCriteria) -> Iterator[tuple[str, DraftCriterion]]:
    for key in LLM_CRITERIA:
        yield key, getattr(criteria, key)


def _level_basis(criteria: DraftCriteria) -> Iterator[Failure]:
    """V2 over the criteria (L4361)."""
    for key, c in _criteria(criteria):
        if c.basis == "unknown":
            if c.level is not None:
                yield Failure(key, "unknown_with_level")
            continue
        if c.level is None:
            yield Failure(key, "level_missing")
        elif c.basis == "unconstrained" and c.level != 4:
            yield Failure(key, "unconstrained_not_4")
        if not c.ad_evidence:
            yield Failure(key, "ad_evidence_missing")
        if c.basis == "observed" and c.level is not None and c.level >= 1 and not c.profile_evidence:
            yield Failure(key, "profile_evidence_missing")


def _consistency(draft: GeneratorDraft, ctx: ValidationContext) -> Iterator[Failure]:
    """V4's consistency failures (L4373-4378)."""
    facts, criteria = draft.job_facts, draft.criteria
    languages = criteria.languages
    comparisons = [compare_language(x, ctx.profile)[0] for x in facts.required_languages if x.mandatory]
    if languages.level is not None and languages.level >= 3 and "unmet" in comparisons:
        yield Failure("languages", "languages_vs_unmet")
    if languages.level is not None and languages.level <= 1 and comparisons and set(comparisons) == {"met"}:
        yield Failure("languages", "languages_vs_met")
    skills = [skill.name for skill in ctx.profile.skills]
    if criteria.skills.level == 4 and any(
        all(token_set_similarity(must.text, skill) < SKILL_MATCH for skill in skills)
        for must in facts.must_haves
    ):
        yield Failure("skills", "skills_vs_must_have")
    if languages.basis == "unconstrained" and facts.required_languages:
        yield Failure("languages", "languages_unconstrained_with_requirements")
    if draft.not_a_job_ad:
        for key, c in _criteria(criteria):
            if c.level is not None:
                yield Failure(key, "not_a_job_ad_with_levels")


def _text_limit(path: str, text: str | None, max_chars: int, max_words: int | None = None) -> Iterator[Issue]:
    if text is None:
        return
    if max_words is not None and _words(text) > max_words:
        yield Issue("too_many_words", path, {"max_words": max_words})
    if len(text) > max_chars:
        yield Issue("too_many_chars", path, {"max_chars": max_chars})


def _count_limit(path: str, items: Sequence[object], max_items: int) -> Iterator[Issue]:
    if len(items) > max_items:
        yield Issue("too_many_items", path, {"max_items": max_items})


def _limits(draft: GeneratorDraft) -> Iterator[Issue]:
    """V1 over the draft (§11.5 L4330-4347; the quotes per fact item are stage 1's)."""
    yield from _text_limit("summary_en", draft.summary_en, SUMMARY_MAX_CHARS, SUMMARY_MAX_WORDS)
    yield from _text_limit("summary_de", draft.summary_de, SUMMARY_MAX_CHARS, SUMMARY_MAX_WORDS)
    for key, c in _criteria(draft.criteria):
        path = f"criteria.{key}"
        yield from _text_limit(f"{path}.rationale", c.rationale, RATIONALE_MAX_CHARS, RATIONALE_MAX_WORDS)
        for name, items in (("ad_evidence", c.ad_evidence), ("profile_evidence", c.profile_evidence)):
            if len(items) > QUOTES_PER_CRITERION:
                yield Issue("too_many_quotes", f"{path}.{name}", {"max_quotes": QUOTES_PER_CRITERION})
    facts = draft.job_facts
    region = facts.remote_region
    yield from _count_limit("job_facts.remote_region.countries", region.countries, COUNTRIES_MAX)
    for i, country in enumerate(region.countries):
        if country.strip().upper() not in _iso_countries():
            yield Issue("country_invalid", f"job_facts.remote_region.countries[{i}]", {})
    yield from _count_limit("job_facts.required_languages", facts.required_languages, LANGUAGES_MAX)
    for i, language in enumerate(facts.required_languages):
        path = f"job_facts.required_languages[{i}].language_other"
        yield from _text_limit(path, language.language_other, LANGUAGE_OTHER_MAX_CHARS)
    for name, requirements in (("must_haves", facts.must_haves), ("nice_to_haves", facts.nice_to_haves)):
        yield from _count_limit(f"job_facts.{name}", requirements, REQUIREMENTS_MAX)
        for i, requirement in enumerate(requirements):
            yield from _text_limit(f"job_facts.{name}[{i}].text", requirement.text, REQUIREMENT_MAX_CHARS)
    years = facts.min_years_experience.value
    low, high = MIN_YEARS_RANGE
    if years is not None and not low <= years <= high:
        yield Issue("out_of_range", "job_facts.min_years_experience.value", {"min": low, "max": high})
    yield from _text_limit("job_facts.industry.value", facts.industry.value, NAME_MAX_CHARS)
    yield from _text_limit("job_facts.degree.field", facts.degree.field, NAME_MAX_CHARS)
    yield from _count_limit("job_facts.certifications", facts.certifications, CERTIFICATIONS_MAX)
    for i, certification in enumerate(facts.certifications):
        yield from _text_limit(f"job_facts.certifications[{i}].name", certification.name, NAME_MAX_CHARS)
    level = facts.security_clearance.level
    yield from _text_limit("job_facts.security_clearance.level", level, CLEARANCE_LEVEL_MAX_CHARS)


def validate_draft(
    draft: GeneratorDraft,
    ctx: ValidationContext,
    *,
    normalisation: Normalisation,
    history: CorrectionHistory | None = None,
) -> DraftValidation:
    """Stage 2 of `draft` (module docstring): never raises. `normalisation` is the draft's stage-1 record (as
    stored); `history` the stored history of an earlier validation of the same draft, which the result
    carries instead of a new one."""
    run = _Validation(ctx)
    facts = _remote_region(_work_mode(_dedupe_fact_ids(draft.job_facts, run), run), run)
    fact_list = list(_facts(facts))
    for fact in fact_list:
        run.ground(fact.key, fact.path, fact.evidence)
    criteria = _seniority(draft.model_copy(update={"job_facts": facts}), facts, run)
    corrected = draft.model_copy(update={"job_facts": facts, "criteria": criteria})
    for key, c in _criteria(criteria):
        run.ground(key, f"criteria.{key}.ad_evidence", c.ad_evidence)
        run.ground_profile(key, f"criteria.{key}.profile_evidence", c.profile_evidence)
    failures = [
        *normalisation.failures,
        *_level_basis(criteria),
        *(
            Failure(fact.key, "fact_evidence_missing")
            for fact in fact_list
            if fact.asserted and not fact.evidence
        ),
        *run.quote_failures,
        *_consistency(corrected, ctx),
    ]
    issues = [*normalisation.issues, *_limits(corrected), *run.quote_issues]
    hits = injection.detect(ctx.ad.text)
    suspected = bool(hits) or draft.injection_observed
    if suspected:
        names: list[JSONValue] = [name for name in sorted({hit.name for hit in hits})]
        path = "job_ad" if hits else "injection_observed"
        issues.append(
            Issue("injection_suspected", path, {"patterns": names, "observed": draft.injection_observed})
        )
    fact_ids = derive_fact_ids(facts)
    if draft.not_a_job_ad:
        issues.append(Issue("not_a_job_ad", "not_a_job_ad", {"required_review": NOT_A_JOB_AD.fact_id}))
        fact_ids[NOT_A_JOB_AD.fact_id] = NOT_A_JOB_AD
    repairs = [*normalisation.repairs, *run.repairs]
    return DraftValidation(
        draft=corrected,
        issues=issues,
        failures=failures,
        repairs=repairs,
        groundings=run.groundings,
        fact_ids=fact_ids,
        injection_suspected=suspected,
        history=history if history is not None else CorrectionHistory(normalisation, list(run.repairs)),
    )


# ---- repairs at publication --------------------------------------------------------------------------------

_WORD: Final = re.compile(r"\S+")


def cut_text(text: str, *, max_chars: int, max_words: int | None = None) -> str | None:
    """`text` cut at its limit and ending in `[…]` (L4382), or `None` when it is within both limits. The cut
    text with its marker is within both limits (the marker counts as a word)."""
    if len(text) <= max_chars and (max_words is None or _words(text) <= max_words):
        return None
    prefix = text
    if max_words is not None:  # the marker is a word: keep one fewer
        words = list(_WORD.finditer(text))
        keep = max_words - 1
        if len(words) > keep:
            prefix = text[: words[keep - 1].end()] if keep >= 1 else ""
    prefix = prefix[: max_chars - len(TRUNCATION_MARK) - 1].rstrip()
    return f"{prefix} {TRUNCATION_MARK}" if prefix else TRUNCATION_MARK


class _Publication:
    """The state of one `repair_for_publication`: the quote paths to drop and the paths it cut."""

    def __init__(self, validation: DraftValidation) -> None:
        self.dropped = {issue.path for issue in validation.issues if issue.code in QUOTE_FAILURE_CODES}
        self.cut: list[str] = []

    def text(self, path: str, text: str, max_chars: int, max_words: int | None = None) -> str:
        repaired = cut_text(text, max_chars=max_chars, max_words=max_words)
        if repaired is None:
            return text
        self.cut.append(path)
        return repaired

    def optional(self, path: str, text: str | None, max_chars: int) -> str | None:
        return None if text is None else self.text(path, text, max_chars)

    def items[T](self, path: str, items: Sequence[T], max_items: int) -> list[T]:
        kept = [x for i, x in enumerate(items) if f"{path}[{i}]" not in self.dropped]
        if len(kept) > max_items:
            kept = kept[:max_items]
        if len(kept) != len(items):
            self.cut.append(path)
        return kept


def _publish_facts(facts: JobFacts, run: _Publication) -> JobFacts:
    def evidence(path: str, quotes: Sequence[str]) -> list[str]:
        return run.items(f"{path}.evidence", quotes, QUOTES_PER_FACT)

    region = facts.remote_region
    countries = [c for c in region.countries if c.strip().upper() in _iso_countries()][:COUNTRIES_MAX]
    if len(countries) != len(region.countries):  # an invalid code dropped, or the list cut (L4389)
        run.cut.append("job_facts.remote_region.countries")
    languages = []
    for i, x in enumerate(run.items("job_facts.required_languages", facts.required_languages, LANGUAGES_MAX)):
        path = f"job_facts.required_languages[{i}]"
        other = run.optional(f"{path}.language_other", x.language_other, LANGUAGE_OTHER_MAX_CHARS)
        languages.append(
            x.model_copy(update={"language_other": other, "evidence": evidence(path, x.evidence)})
        )
    requirements: dict[str, list[Requirement]] = {}
    for name in ("must_haves", "nice_to_haves"):
        kept = run.items(f"job_facts.{name}", getattr(facts, name), REQUIREMENTS_MAX)
        requirements[name] = [
            Requirement(
                text=run.text(f"job_facts.{name}[{i}].text", x.text, REQUIREMENT_MAX_CHARS),
                evidence=evidence(f"job_facts.{name}[{i}]", x.evidence),
            )
            for i, x in enumerate(kept)
        ]
    years = facts.min_years_experience
    low, high = MIN_YEARS_RANGE
    years_value = years.value
    if years_value is not None and not low <= years_value <= high:
        run.cut.append("job_facts.min_years_experience.value")
        years_value = None
    certifications = [
        x.model_copy(
            update={
                "name": run.text(f"job_facts.certifications[{i}].name", x.name, NAME_MAX_CHARS),
                "evidence": evidence(f"job_facts.certifications[{i}]", x.evidence),
            }
        )
        for i, x in enumerate(run.items("job_facts.certifications", facts.certifications, CERTIFICATIONS_MAX))
    ]
    clearance = facts.security_clearance
    return JobFacts(
        work_mode=facts.work_mode.model_copy(
            update={"evidence": evidence("job_facts.work_mode", facts.work_mode.evidence)}
        ),
        onsite_only=facts.onsite_only.model_copy(
            update={"evidence": evidence("job_facts.onsite_only", facts.onsite_only.evidence)}
        ),
        remote_region=region.model_copy(
            update={"countries": countries, "evidence": evidence("job_facts.remote_region", region.evidence)}
        ),
        required_languages=languages,
        must_haves=requirements["must_haves"],
        nice_to_haves=requirements["nice_to_haves"],
        seniority=facts.seniority.model_copy(
            update={"evidence": evidence("job_facts.seniority", facts.seniority.evidence)}
        ),
        min_years_experience=MinYearsExperience(
            value=years_value, evidence=evidence("job_facts.min_years_experience", years.evidence)
        ),
        industry=Industry(
            value=run.optional("job_facts.industry.value", facts.industry.value, NAME_MAX_CHARS),
            evidence=evidence("job_facts.industry", facts.industry.evidence),
        ),
        employment_types=facts.employment_types.model_copy(
            update={"evidence": evidence("job_facts.employment_types", facts.employment_types.evidence)}
        ),
        degree=facts.degree.model_copy(
            update={
                "field": run.optional("job_facts.degree.field", facts.degree.field, NAME_MAX_CHARS),
                "evidence": evidence("job_facts.degree", facts.degree.evidence),
            }
        ),
        certifications=certifications,
        security_clearance=clearance.model_copy(
            update={
                "level": run.optional(
                    "job_facts.security_clearance.level", clearance.level, CLEARANCE_LEVEL_MAX_CHARS
                ),
                "evidence": evidence("job_facts.security_clearance", clearance.evidence),
            }
        ),
        work_permit=facts.work_permit.model_copy(
            update={"evidence": evidence("job_facts.work_permit", facts.work_permit.evidence)}
        ),
    )


def repair_for_publication(
    draft: GeneratorDraft, validation: DraftValidation
) -> tuple[GeneratorDraft, list[str]]:
    """The publication repairs of `draft` (module docstring, Publication; `validation` is its last
    validation): the repaired draft and `truncated_fields`, the history's repair paths, then each path that
    publication cut, once each."""
    run = _Publication(validation)
    summary_en = run.text("summary_en", draft.summary_en, SUMMARY_MAX_CHARS, SUMMARY_MAX_WORDS)
    summary_de = run.text("summary_de", draft.summary_de, SUMMARY_MAX_CHARS, SUMMARY_MAX_WORDS)
    criteria: dict[str, DraftCriterion] = {}
    for key, c in _criteria(draft.criteria):
        path = f"criteria.{key}"
        criteria[key] = DraftCriterion(
            level=c.level,
            basis=c.basis,
            rationale=run.text(f"{path}.rationale", c.rationale, RATIONALE_MAX_CHARS, RATIONALE_MAX_WORDS),
            ad_evidence=run.items(f"{path}.ad_evidence", c.ad_evidence, QUOTES_PER_CRITERION),
            profile_evidence=run.items(f"{path}.profile_evidence", c.profile_evidence, QUOTES_PER_CRITERION),
        )
    repaired = draft.model_copy(
        update={
            "summary_en": summary_en,
            "summary_de": summary_de,
            "criteria": DraftCriteria.model_validate(criteria),
            "job_facts": _publish_facts(draft.job_facts, run),
        }
    )
    history = validation.history
    paths = [r.path for r in (*history.normalisation.repairs, *history.corrections)] + run.cut
    return repaired, list(dict.fromkeys(paths))


# ---- the critic (§11.7.2, §11.7.3) -------------------------------------------------------------------------

_FACT_REVIEW: Final[Mapping[str, FactReview]] = {
    "fact_confirmed": "critic_confirmed",
    "fact_rejected": "critic_rejected",
}
_DECIDED: Final = frozenset({"critic_confirmed", "critic_rejected"})


def _review_limits(i: int, review: Review) -> Iterator[Issue]:
    path = f"reviews[{i}]"
    yield from _text_limit(
        f"{path}.explanation", review.explanation, EXPLANATION_MAX_CHARS, EXPLANATION_MAX_WORDS
    )
    if len(review.ad_evidence) > QUOTES_PER_REVIEW:
        yield Issue("too_many_quotes", f"{path}.ad_evidence", {"max_quotes": QUOTES_PER_REVIEW})


def _review_groundings(
    i: int, review: Review, ctx: ValidationContext, issues: list[Issue]
) -> list[Grounding]:
    """The groundings of a review's first `QUOTES_PER_REVIEW` grounded quotes; each failing quote an issue."""
    found: list[Grounding] = []
    for j, quote in enumerate(review.ad_evidence):
        defect = quote_defect(quote)
        grounding = None if defect else ground_quote(quote, ctx.ad, ctx.profile_sections)
        if grounding is None:
            issues.append(Issue(defect or "quote_ungrounded", f"reviews[{i}].ad_evidence[{j}]", {}))
        elif len(found) < QUOTES_PER_REVIEW:
            found.append(grounding)
    return found


def validate_review(
    review: CriticOut,
    *,
    draft: GeneratorDraft,
    required: Sequence[FactRef],
    ctx: ValidationContext,
    draft_failures: Sequence[Failure],
) -> CriticValidation:
    """The critic review of `draft` validated, with its derived outcome (§11.7.2; module docstring).
    `required` are the facts the critic had to review (`knockouts.required_facts`; `not_a_job_ad:ad` is
    required whenever the draft says it is no job ad), `draft_failures` the draft's unresolved failures
    (`DraftValidation.failures`, the stored normalisation's included)."""
    required_ids = list(dict.fromkeys(ref.fact_id for ref in required))
    if draft.not_a_job_ad and NOT_A_JOB_AD.fact_id not in required_ids:
        required_ids.append(NOT_A_JOB_AD.fact_id)
    issues: list[Issue] = []
    if _words(review.summary) > CRITIC_SUMMARY_MAX_WORDS:
        issues.append(Issue("too_many_words", "summary", {"max_words": CRITIC_SUMMARY_MAX_WORDS}))
    discarded: list[Issue] = []
    disputes: list[Dispute] = []
    fact_reviews: dict[str, FactReview] = {}
    seen: set[str] = set()
    complete = True
    criterion_reviews = 0
    for i, r in enumerate(review.reviews):
        path = f"reviews[{i}]"
        issues.extend(_review_limits(i, r))
        is_criterion = r.target in LLM_CRITERIA and r.assessment in CRITERION_ASSESSMENTS
        is_fact = r.target in required_ids and r.assessment in FACT_ASSESSMENTS
        if not (is_criterion or is_fact):
            complete = False
            issues.append(Issue("invalid_target", path, {}))  # the target may be any text: never stored
            continue
        if is_criterion:
            criterion_reviews += 1
            if criterion_reviews > CRITERION_DISPUTES_MAX:  # ignored (required fact reviews never are)
                issues.append(Issue("too_many_disputes", path, {"max_disputes": CRITERION_DISPUTES_MAX}))
                continue
        if r.target in seen:
            complete = False
            issues.append(Issue("duplicate_target", path, {"target": r.target}))
            if is_fact:
                fact_reviews[r.target] = "uncertain"  # two reviews of one fact settle nothing
            continue
        seen.add(r.target)
        groundings = _review_groundings(i, r, ctx, issues)
        if not groundings:
            discarded.append(Issue("review_ungrounded", path, {"target": r.target}))
            if is_fact:
                fact_reviews[r.target] = "uncertain"
            continue
        if is_fact:
            fact_reviews[r.target] = _FACT_REVIEW.get(r.assessment, "uncertain")
        else:
            disputes.append(Dispute(r.target, r.assessment, r.suggested_level, groundings, r.explanation))
    for name in required_ids:
        if name not in seen:
            complete = False
            issues.append(Issue("review_missing", "reviews", {"target": name}))
            fact_reviews[name] = "uncertain"
    ordered: dict[str, FactReview] = {name: fact_reviews[name] for name in required_ids}
    verified = ordered.get(NOT_A_JOB_AD.fact_id) == "critic_confirmed"
    outcome: Literal["accept", "revise", "incomplete"]
    not_a_job_ad = ordered.get(NOT_A_JOB_AD.fact_id) if draft.not_a_job_ad else None
    if not_a_job_ad == "critic_rejected":
        outcome = "revise"  # a real job ad: the reviser must rate it (L4365)
    elif draft.not_a_job_ad and not verified:
        outcome = "incomplete"  # an uncertain or missing review never yields the label
    elif (
        complete
        and all(status in _DECIDED for status in ordered.values())
        and not disputes
        and review.injection != "present_influenced"
        and not draft_failures
    ):
        outcome = "accept"
    else:
        outcome = "revise" if complete else "incomplete"
    return CriticValidation(
        derived_outcome=outcome,
        grounded_disputes=disputes,
        fact_reviews=ordered,
        discarded=discarded,
        issues=issues,
        complete=complete,
        verdict_mismatch=(review.verdict == "accept") != (outcome == "accept"),
        not_a_job_ad_verified=verified,
    )


def apply_fact_confirmations(
    previous: Mapping[str, Confirmation], validation: CriticValidation, required: Iterable[FactRef]
) -> dict[str, Confirmation]:
    """The fact confirmations after a critic round (L4430): each required text fact takes the round's review
    (`critic_confirmed`, `critic_rejected`, or `uncertain`, a missing review included), a structured
    requirement is `structured`, and every other fact keeps its previous value (`unconfirmed` for one never
    reviewed). `previous` is not changed."""
    confirmations = dict(previous)
    for ref in required:
        if ref.source == "structured":
            confirmations[ref.fact_id] = "structured"
        else:
            confirmations[ref.fact_id] = validation.fact_reviews.get(ref.fact_id, "uncertain")
    return confirmations
