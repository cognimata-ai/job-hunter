"""Knockout candidates (spec §11.4, L4176-4204), derived deterministically: the generator never outputs a
knockout. Pure functions of the draft's job facts, the job's `DetFacts`, the confirmed profile and the scope
the caller has loaded; no connection, no clock.

Each candidate goes through three layers (L4176-4180):
1. **Requirement**: a fact the draft asserts (`requirement_source = "text"`) or a structured field
   (`"structured"`). A structured on-site work mode outranks an asserted `onsite_only`: both name
   `onsite_required:onsite`, and one candidate exists per fact id (the first fact of an id is kept).
2. **Comparison** against the profile and the scope, exactly one of `met`, `unmet` (a provable
   incompatibility, every input known) or `uncomparable` (an input is unknown or cannot be compared), by
   code (the table L4194-4202):
   - `onsite_required` (`onsite_only` asserted, or the structured work mode `{onsite}`): `unmet` when
     `onsite ∉ scope.work_modes`, or every job location has the on-site band 0 (every candidate beyond 1.5 x
     each scope location's radius, §11.8); `uncomparable` when onsite is accepted and a job location is
     unresolved or ambiguous across bands, or there is no job location or no scope location;
   - `mandatory_language` (a mandatory language): `unmet` when it is absent from the profile, or its level is
     specified and every profile entry of the language is known and below it; `uncomparable` when the level
     is `unspecified` and the profile has the language below B2, when a profile entry of unknown level could
     meet it, or when the language is `other` and its folded name matches no profile `code_other`. With an
     `unspecified` level, B2 or higher is `met` (R13); `native` counts above C2;
   - `security_clearance` (required): `unmet` when the profile has no user-entered clearance; `uncomparable`
     when the ad's level is specified and no profile clearance has it (folded equality);
   - `work_permit` (required): `unmet` iff `work_authorization_de = requires_sponsorship`, `uncomparable`
     iff it is `unspecified`;
   - `degree` (mandatory, with no "or equivalent" clause; `none_required` is no requirement): `unmet` when
     the level is above the profile's highest ordered degree, `none < vocational < bachelor < master < phd`
     (an empty education list is `none`); `uncomparable` when the level is `unspecified`, or the education
     holds only `other` levels (A3 G10);
   - `certification` (mandatory): `unmet` when no profile certification has a folded token-set similarity of
     at least 0.8;
   - `contract_type` (the structured employment types): `unmet` when all lie outside the scope, the
     `employment_type_excluded` filter's domain.
3. **Review need**: a text requirement that is `unmet` or `uncomparable` is **binding-capable** (it triggers
   the critic, T5, for its fact id), and so is every text-asserted `onsite_required`, even when met: a later
   scope can make it unmet, and scope changes never call the LLM (L4180). A structured requirement never is.

The severity is `scope.knockout_severity[code]`, and for a language
`scope.language_policy.get(code, scope.language_policy_default)` (§11.8). `binding` is L4192: only an `unmet`
comparison whose fact is structured or critic-confirmed caps or penalises; `uncomparable` never does.

`inputs` holds the compared values, ids and enums, never ad text; an unknown input is `null`, so an `unmet`
candidate's inputs hold no `null`. The token-set similarity of two names is the Jaccard index of their sets of
folded words (the `\\w+` runs of `fold`).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from jobhunter.geo import gazetteer
from jobhunter.llm.schemas.generator import JobFacts, RequiredLanguage
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.normalize.text import fold
from jobhunter.run_settings import ScopeSettings
from jobhunter.scoring.det_criteria import ONSITE_BOUNDS, location_band
from jobhunter.scoring.facts import NOT_A_JOB_AD, FactRef, derive_fact_ids, fact_id, language_subject
from jobhunter.scoring.filters import DetFacts
from jobhunter.types import JSONValue

Comparison = Literal["met", "unmet", "uncomparable"]
Severity = Literal["hard", "soft"]
Confirmation = Literal["structured", "critic_confirmed", "critic_rejected", "uncertain", "unconfirmed"]
RequirementSource = Literal["structured", "text"]

CEFR_RANK: Final = {"A1": 1, "A2": 2, "B1": 3, "B2": 4, "C1": 5, "C2": 6, "native": 7}
UNSPECIFIED_LEVEL_MET_FROM: Final = "B2"  # R13: an `unspecified` mandatory level is met from B2 up
DEGREE_RANK: Final = {"none": 0, "vocational": 1, "bachelor": 2, "master": 3, "phd": 4}
_CEFR_LEVEL: Final = {rank: level for level, rank in CEFR_RANK.items()}
_DEGREE_LEVEL: Final = {rank: level for level, rank in DEGREE_RANK.items()}
CERTIFICATION_MATCH: Final = 0.8
CONTRACT_TYPE_SUBJECT: Final = "employment_types"  # the structured field; no generator fact names it
_BINDING_CONFIRMATIONS: Final = frozenset({"structured", "critic_confirmed"})
_WORD: Final = re.compile(r"\w+")

_Outcome = tuple[Comparison, dict[str, JSONValue]]


@dataclass(frozen=True)
class KnockoutCandidate:
    code: str
    fact_id: str
    subject: str
    requirement_source: RequirementSource
    comparison: Comparison
    binding_capable: bool
    severity: Severity | None
    inputs: dict[str, JSONValue]


def token_set_similarity(a: str, b: str) -> float:
    """The Jaccard index of the folded word sets of `a` and `b`; 0.0 when both have no word."""
    left, right = set(_WORD.findall(fold(a))), set(_WORD.findall(fold(b)))
    union = left | right
    return len(left & right) / len(union) if union else 0.0


# -- the comparisons -----------------------------------------------------------------------------------------


def _onsite(det: DetFacts, scope: ScopeSettings) -> _Outcome:
    if "onsite" not in scope.work_modes:
        return "unmet", {"onsite_accepted": False}
    scope_locations: list[JSONValue] = [location.gazetteer_id for location in scope.locations]
    if not scope.locations:
        return "uncomparable", {"onsite_accepted": True, "scope_locations": scope_locations}
    bands: list[JSONValue] = [
        location_band(x, scope.locations, ONSITE_BOUNDS, gazetteer.distance_km) for x in det.locations
    ]
    inputs: dict[str, JSONValue] = {
        "onsite_accepted": True,
        "scope_locations": scope_locations,
        "location_bands": bands,
    }
    if not bands or None in bands:
        return "uncomparable", inputs
    return ("unmet" if all(band == 0 for band in bands) else "met"), inputs


def _language(required: RequiredLanguage, profile: ConfirmedProfile) -> _Outcome:
    if required.language == "other":
        name = fold(required.language_other or "")
        entries = [
            x
            for x in profile.languages
            if x.code == "other" and x.code_other is not None and fold(x.code_other) == name
        ]
        if not entries:  # the name may be the profile's under another spelling: never provably absent
            return "uncomparable", {"in_profile": None}
    else:
        entries = [x for x in profile.languages if x.code == required.language]
        if not entries:
            return "unmet", {"in_profile": False}
    known = [CEFR_RANK[x.cefr] for x in entries if x.cefr in CEFR_RANK]
    best = max(known, default=None)
    required_rank = CEFR_RANK.get(required.level)
    required_level = required.level if required_rank is not None else None
    threshold = CEFR_RANK[UNSPECIFIED_LEVEL_MET_FROM] if required_rank is None else required_rank
    if best is not None and best >= threshold:
        return "met", {
            "in_profile": True,
            "required_level": required_level,
            "profile_level": _CEFR_LEVEL[best],
        }
    # below the threshold, an entry of unknown level could still meet it
    profile_level = _CEFR_LEVEL[best] if best is not None and len(known) == len(entries) else None
    inputs: dict[str, JSONValue] = {
        "in_profile": True,
        "required_level": required_level,
        "profile_level": profile_level,
    }
    return ("uncomparable" if required_level is None or profile_level is None else "unmet"), inputs


def _clearance(facts: JobFacts, profile: ConfirmedProfile) -> _Outcome:
    held = profile.user_entered.security_clearances
    if not held:
        return "unmet", {"profile_clearance": False}
    level = fold(facts.security_clearance.level or "")
    if not level:
        return "met", {"profile_clearance": True, "level_specified": False}
    match = any(x.level is not None and fold(x.level) == level for x in held)
    inputs: dict[str, JSONValue] = {"profile_clearance": True, "level_specified": True, "level_match": match}
    return ("met" if match else "uncomparable"), inputs


def _work_permit(profile: ConfirmedProfile) -> _Outcome:
    authorization = profile.user_entered.work_authorization_de
    if authorization == "unspecified":
        return "uncomparable", {"work_authorization_de": None}
    comparison: Comparison = "unmet" if authorization == "requires_sponsorship" else "met"
    return comparison, {"work_authorization_de": authorization}


def _degree(facts: JobFacts, profile: ConfirmedProfile) -> _Outcome:
    required = DEGREE_RANK.get(facts.degree.level)  # None for `unspecified`
    levels = [x.level for x in profile.education]
    ordered = [DEGREE_RANK[level] for level in levels if level in DEGREE_RANK]
    highest = None if levels and not ordered else max(ordered, default=DEGREE_RANK["none"])
    inputs: dict[str, JSONValue] = {
        "required_level": None if required is None else facts.degree.level,
        "profile_level": None if highest is None else _DEGREE_LEVEL[highest],
    }
    if required is None or highest is None:
        return "uncomparable", inputs
    return ("unmet" if required > highest else "met"), inputs


def _certification(name: str, profile: ConfirmedProfile) -> _Outcome:
    matched = any(token_set_similarity(name, x.name) >= CERTIFICATION_MATCH for x in profile.certifications)
    return ("met" if matched else "unmet"), {"matched": matched}


def _contract_type(det: DetFacts, scope: ScopeSettings) -> _Outcome:
    types: list[JSONValue] = [x for x in sorted(det.employment_types)]
    accepted: list[JSONValue] = [x for x in sorted(scope.employment_types)]
    inputs: dict[str, JSONValue] = {"employment_types": types, "scope_employment_types": accepted}
    return ("met" if det.employment_types & scope.employment_types else "unmet"), inputs


# -- the candidates ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Requirement:
    code: str
    subject: str
    source: RequirementSource
    outcome: _Outcome


def _requirements(
    facts: JobFacts, det: DetFacts, profile: ConfirmedProfile, scope: ScopeSettings
) -> list[_Requirement]:
    """Layers 1 and 2, in the order of the table; a fact id may repeat (the caller keeps the first)."""
    found: list[_Requirement] = []
    structured_onsite = det.work_mode_source == "structured" and det.work_mode_possible == {"onsite"}
    if structured_onsite or facts.onsite_only.asserted:
        source: RequirementSource = "structured" if structured_onsite else "text"
        found.append(_Requirement("onsite_required", "onsite", source, _onsite(det, scope)))
    found += [
        _Requirement("mandatory_language", language_subject(x), "text", _language(x, profile))
        for x in facts.required_languages
        if x.mandatory
    ]
    if facts.security_clearance.required:
        found.append(_Requirement("security_clearance", "clearance", "text", _clearance(facts, profile)))
    if facts.work_permit.required:
        found.append(_Requirement("work_permit", "work_permit", "text", _work_permit(profile)))
    degree = facts.degree
    if degree.mandatory and not degree.or_equivalent and degree.level != "none_required":
        found.append(_Requirement("degree", "degree", "text", _degree(facts, profile)))
    found += [
        _Requirement("certification", fold(x.name), "text", _certification(x.name, profile))
        for x in facts.certifications
        if x.mandatory
    ]
    if det.employment_source == "structured" and det.employment_types:
        found.append(
            _Requirement("contract_type", CONTRACT_TYPE_SUBJECT, "structured", _contract_type(det, scope))
        )
    return found


def _severity(code: str, subject: str, scope: ScopeSettings) -> Severity:
    if code == "mandatory_language":
        language = subject.split(":", 1)[0]  # `other:<name>` is the policy of `other`
        return scope.language_policy.get(language, scope.language_policy_default)
    return scope.knockout_severity[code]


def derive_knockouts(
    facts: JobFacts, det: DetFacts, profile: ConfirmedProfile, scope: ScopeSettings
) -> list[KnockoutCandidate]:
    """The knockout candidates of a draft under `scope` (see the module docstring), one per fact id."""
    candidates: dict[str, KnockoutCandidate] = {}
    for requirement in _requirements(facts, det, profile, scope):
        key = fact_id(requirement.code, requirement.subject)
        if key in candidates:
            continue
        comparison, inputs = requirement.outcome
        text = requirement.source == "text"
        candidates[key] = KnockoutCandidate(
            code=requirement.code,
            fact_id=key,
            subject=requirement.subject,
            requirement_source=requirement.source,
            comparison=comparison,
            binding_capable=text and (comparison != "met" or requirement.code == "onsite_required"),
            severity=_severity(requirement.code, requirement.subject, scope),
            inputs=inputs,
        )
    return list(candidates.values())


def binding(candidate: KnockoutCandidate, confirmation: Confirmation) -> bool:
    """L4192: a cap or penalty applies only to an `unmet` comparison whose fact is structured or
    critic-confirmed."""
    return candidate.comparison == "unmet" and confirmation in _BINDING_CONFIRMATIONS


def required_facts(
    facts: JobFacts, candidates: Sequence[KnockoutCandidate], *, not_a_job_ad: bool
) -> list[FactRef]:
    """The facts the critic must review (§11.7.2): those of the binding-capable candidates, and
    `not_a_job_ad:ad` when the draft's top-level `not_a_job_ad` is true, in fact-id order. A binding-capable
    candidate is a text requirement, so its fact is one `derive_fact_ids(facts)` gives; one that is not raises
    `ValueError` (the candidates were derived from other facts)."""
    asserted = derive_fact_ids(facts)
    refs: dict[str, FactRef] = {}
    for candidate in candidates:
        if not candidate.binding_capable:
            continue
        ref = asserted.get(candidate.fact_id)
        if ref is None:
            raise ValueError(
                f"the binding-capable candidate {candidate.fact_id!r} names no fact of the draft"
            )
        refs[ref.fact_id] = ref
    if not_a_job_ad:
        refs[NOT_A_JOB_AD.fact_id] = NOT_A_JOB_AD
    return [refs[key] for key in sorted(refs)]
