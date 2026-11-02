"""`derive_knockouts` over random drafts, profiles, job facts and scopes (spec §11.4, L4179; M3 Task 3):
`unmet` is a *provable* incompatibility, "every input known". For every `unmet` candidate no input it used is
null, and the incompatibility is recomputed here from the raw values, independently of the module. The review
need follows layer 3: a text requirement is binding-capable iff it is unmet or uncomparable, or it is the
on-site requirement; a structured one never is. Nothing raises.

Every datum is generated or invented; no real ad or person.
"""

import re
from typing import Any, get_args

from hypothesis import given, settings
from hypothesis import strategies as st
from jobhunter.geo.gazetteer import distance_km
from jobhunter.llm.schemas.generator import Cefr, JobFacts, LangCode
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.normalize.text import fold
from jobhunter.run_settings import ScopeLocation, ScopeSettings
from jobhunter.scoring.filters import DetFacts
from jobhunter.scoring.knockouts import KnockoutCandidate, binding, derive_knockouts
from jobhunter.sources.parsing.locations import Location

from tests.support.facts import UNRESOLVED, facts, scope
from tests.support.scoring import CENTRE, job_facts, km_east, language, profile, required_language

CEFR = get_args(get_args(Cefr)[0])  # A1 ... C2, native, unspecified
CODES = get_args(get_args(LangCode)[0])
FEW_CODES = ("de", "en", "other")  # the languages of the profiles and most required ones
RANK = {"A1": 1, "A2": 2, "B1": 3, "B2": 4, "C1": 5, "C2": 6, "native": 7}
DEGREES = {"none": 0, "vocational": 1, "bachelor": 2, "master": 3, "phd": 4}
NAMES = (
    "Französisch",
    "FRANZOSISCH",
    "Klingonisch",
    "Scrum Master",
    "scrum  master",
    "Kubernetes Administrator",
)
WORK_MODES = ("remote", "hybrid", "onsite")
TYPES = ("full_time", "part_time", "freelance", "contract")
OTHER = ScopeLocation("Erfunden", "test:other", 52.4, 13.9, 15, "erfunden")
LOCATIONS: tuple[Location, ...] = (
    km_east(10),
    km_east(28),
    km_east(35),
    km_east(60),
    km_east(10, 35),
    km_east(35, 60),
    UNRESOLVED,
)

names = st.sampled_from(NAMES)
optional_names = st.none() | names


@st.composite
def drafts(draw: st.DrawFn) -> JobFacts:
    languages = draw(
        st.lists(
            st.builds(
                lambda code, other, level, mandatory: required_language(
                    code, level=level, mandatory=mandatory, other=other if code == "other" else None
                ),
                st.sampled_from(FEW_CODES) | st.sampled_from(CODES),
                optional_names,
                st.sampled_from(CEFR),
                st.booleans(),
            ),
            max_size=4,
        )
    )
    return job_facts(
        onsite_only={"asserted": draw(st.booleans()), "evidence": ["vor Ort"]},
        required_languages=languages,
        degree={
            "level": draw(
                st.sampled_from(("none_required", "vocational", "bachelor", "master", "phd", "unspecified"))
            ),
            "mandatory": draw(st.booleans()),
            "or_equivalent": draw(st.booleans()),
            "field": None,
            "evidence": ["Studium"],
        },
        certifications=draw(
            st.lists(
                st.builds(
                    lambda n, m: {"name": n, "mandatory": m, "evidence": ["Zertifikat"]}, names, st.booleans()
                ),
                max_size=3,
            )
        ),
        security_clearance={
            "required": draw(st.booleans()),
            "level": draw(optional_names),
            "evidence": ["Ü2"],
        },
        work_permit={"required": draw(st.booleans()), "evidence": ["Arbeitserlaubnis"]},
    )


@st.composite
def profiles(draw: st.DrawFn) -> ConfirmedProfile:
    languages = [  # up to three entries of each code, so one language often has entries of several levels
        language(code, cefr, other=draw(optional_names) if code == "other" else None)
        for code in FEW_CODES
        for cefr in draw(st.lists(st.sampled_from(CEFR), max_size=3))
    ]
    education = draw(
        st.lists(st.sampled_from(("vocational", "bachelor", "master", "phd", "other")), max_size=3)
    )
    return profile(
        languages=languages,
        education=[{"level": x, "field": "Informatik", "institution": None, "year": None} for x in education],
        certifications=[{"name": n, "issuer": None, "year": None} for n in draw(st.lists(names, max_size=2))],
        user_entered={
            "work_authorization_de": draw(
                st.sampled_from(("authorized", "requires_sponsorship", "unspecified"))
            ),
            "security_clearances": [
                {"name": "Sicherheitsüberprüfung", "level": level}
                for level in draw(st.lists(optional_names, max_size=2))
            ],
        },
    )


@st.composite
def det_facts(draw: st.DrawFn) -> DetFacts:
    return facts(
        locations=tuple(draw(st.lists(st.sampled_from(LOCATIONS), max_size=3))),
        work_mode_possible=frozenset(draw(st.sets(st.sampled_from(WORK_MODES), min_size=1))),
        work_mode_source=draw(st.sampled_from(("structured", "text", "none"))),
        employment_types=frozenset(draw(st.sets(st.sampled_from(TYPES)))),
        employment_source=draw(st.sampled_from(("structured", "text", "none"))),
    )


@st.composite
def scopes(draw: st.DrawFn) -> ScopeSettings:
    return scope(
        locations=tuple(draw(st.lists(st.sampled_from((CENTRE, OTHER)), max_size=2, unique=True))),
        work_modes=frozenset(draw(st.sets(st.sampled_from(WORK_MODES), min_size=1))),
        employment_types=frozenset(draw(st.sets(st.sampled_from(TYPES), min_size=1))),
        language_policy=draw(st.dictionaries(st.sampled_from(CODES), st.sampled_from(("hard", "soft")))),
    )


def nulls(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, dict):
        return any(nulls(item) for item in value.values())
    if isinstance(value, list):
        return any(nulls(item) for item in value)
    return False


def words(text: str) -> set[str]:
    return set(re.findall(r"\w+", fold(text)))


def provably_incompatible(
    candidate: KnockoutCandidate, job: JobFacts, det: DetFacts, person: ConfirmedProfile, where: ScopeSettings
) -> bool:
    """The `unmet` condition of the §11.4 table, recomputed from the raw values."""
    match candidate.code:
        case "onsite_required":
            if "onsite" not in where.work_modes:
                return True
            return bool(where.locations and det.locations) and all(
                location.resolution != "unresolved"
                and location.candidates
                and all(
                    min(distance_km(w.lat, w.lon, c.lat, c.lon) / w.radius_km for w in where.locations) > 1.5
                    for c in location.candidates
                )
                for location in det.locations
            )
        case "mandatory_language":
            fact = next(
                x
                for x in job.required_languages
                if x.mandatory
                and (x.language if x.language != "other" else f"other:{fold(x.language_other or '')}")
                == candidate.subject
            )
            if fact.language == "other":
                name = fold(fact.language_other or "")
                entries = [
                    x
                    for x in person.languages
                    if x.code == "other" and x.code_other is not None and fold(x.code_other) == name
                ]
                if not entries:
                    return False  # an unmatched `other` name is never provably absent
            else:
                entries = [x for x in person.languages if x.code == fact.language]
            if not entries:
                return True
            return fact.level in RANK and all(
                x.cefr in RANK and RANK[x.cefr] < RANK[fact.level] for x in entries
            )
        case "security_clearance":
            return not person.user_entered.security_clearances
        case "work_permit":
            return person.user_entered.work_authorization_de == "requires_sponsorship"
        case "degree":
            levels = [x.level for x in person.education]
            ordered = [DEGREES[x] for x in levels if x in DEGREES]
            if levels and not ordered:
                return False
            return job.degree.level in DEGREES and DEGREES[job.degree.level] > max(ordered, default=0)
        case "certification":
            required = words(candidate.subject)
            return all(  # the overlap coefficient of the word sets (Controller Ruling T3-b)
                not required
                or not words(x.name)
                or len(required & words(x.name)) / min(len(required), len(words(x.name))) < 0.8
                for x in person.certifications
            )
        case "contract_type":
            return not det.employment_types & where.employment_types
    raise AssertionError(candidate.code)


@settings(max_examples=400, deadline=None)
@given(drafts(), det_facts(), profiles(), scopes())
def test_unmet_is_provable_from_known_inputs(
    job: JobFacts, det: DetFacts, person: ConfirmedProfile, where: ScopeSettings
) -> None:
    found = derive_knockouts(job, det, person, where)
    assert len({c.fact_id for c in found}) == len(found)  # one candidate per fact id
    for candidate in found:
        if candidate.comparison == "unmet":
            assert not nulls(candidate.inputs), candidate  # every input it used is known (L4179)
            assert provably_incompatible(candidate, job, det, person, where), candidate
        text = candidate.requirement_source == "text"
        needs_review = candidate.comparison != "met" or candidate.code == "onsite_required"
        assert candidate.binding_capable is (text and needs_review), candidate
        assert candidate.fact_id == f"{candidate.code}:{candidate.subject}"
        assert candidate.severity in ("hard", "soft")
        if candidate.comparison != "unmet":
            assert not binding(candidate, "critic_confirmed") and not binding(candidate, "structured")
