"""Scoring test data (M3 Task 3): generator job facts and confirmed profiles for the fact-id, knockout and
deterministic-criteria tests. Every value is invented: no real ad, company or person.

- `job_facts(**overrides)` is a `JobFacts` that asserts nothing (no work mode, no language, no degree, no
  certification, no clearance, no work permit), with the given fields replaced, so a test names only the
  facts it is about;
- `required_language(code, *, level, mandatory=True, evidence=("Sprachkenntnisse",), other=None)` is one
  `required_languages` item (`other` is the `language_other` name of the code `other`);
- `profile(**overrides)` is a `ConfirmedProfile` of an invented senior developer with no language, education,
  certification or clearance and an unspecified work authorisation, with the given fields replaced;
  `language(code, cefr, *, other=None)` is one of its `languages`, `profile_with_language(code, cefr)` a
  profile speaking that language only, and `profile_without(code)` one that speaks English at C1 (German when
  `code` is English) and not `code`;
- `CENTRE` is a scope location (Berlin's centre, radius 20 km) and `km_east(*km)` a resolved location with one
  candidate that many kilometres east of it for each distance (`ambiguous` when there are several), so a
  test sets `x = d / r` directly: `km_east(28)` lies at 1.4 times the radius.
"""

import math
from collections.abc import Sequence
from typing import Any, Final

from jobhunter.llm.schemas.generator import JobFacts, RequiredLanguage
from jobhunter.llm.schemas.profile import ConfirmedProfile, Language
from jobhunter.run_settings import ScopeLocation
from jobhunter.sources.parsing.locations import Candidate, Location

_NOTHING_ASSERTED: Final[dict[str, Any]] = {
    "work_mode": {"value": "unspecified", "evidence": []},
    "onsite_only": {"asserted": False, "evidence": []},
    "remote_region": {"value": "unspecified", "countries": [], "evidence": []},
    "required_languages": [],
    "must_haves": [],
    "nice_to_haves": [],
    "seniority": {"value": "unspecified", "evidence": []},
    "min_years_experience": {"value": None, "evidence": []},
    "industry": {"value": None, "evidence": []},
    "employment_types": {"values": [], "evidence": []},
    "degree": {
        "level": "unspecified",
        "mandatory": False,
        "or_equivalent": False,
        "field": None,
        "evidence": [],
    },
    "certifications": [],
    "security_clearance": {"required": False, "level": None, "evidence": []},
    "work_permit": {"required": False, "evidence": []},
}


def job_facts(**overrides: Any) -> JobFacts:
    """`JobFacts` asserting nothing, with the given fields (models or their dicts) replaced."""
    return JobFacts.model_validate({**_NOTHING_ASSERTED, **overrides})


def required_language(
    code: str,
    *,
    level: str,
    mandatory: bool = True,
    evidence: Sequence[str] = ("Sprachkenntnisse",),
    other: str | None = None,
) -> RequiredLanguage:
    return RequiredLanguage.model_validate(
        {
            "language": code,
            "language_other": other,
            "level": level,
            "mandatory": mandatory,
            "evidence": list(evidence),
        }
    )


_PROFILE: Final[dict[str, Any]] = {
    "summary": "Erfundener Backend-Entwickler",
    "target_titles": [{"title": "Backend Developer", "lang": "en"}],
    "seniority": "senior",
    "total_years_experience": 6.0,
    "roles": [],
    "skills": [],
    "languages": [],
    "education": [],
    "certifications": [],
    "domains": [],
}


def profile(**overrides: Any) -> ConfirmedProfile:
    """The invented profile with the given fields (models or their dicts) replaced."""
    return ConfirmedProfile.model_validate({**_PROFILE, **overrides})


def language(code: str, cefr: str, *, other: str | None = None) -> Language:
    return Language.model_validate({"code": code, "code_other": other, "cefr": cefr, "evidence": "Sprachen"})


def profile_with_language(code: str, cefr: str) -> ConfirmedProfile:
    return profile(languages=[language(code, cefr)])


def profile_without(code: str) -> ConfirmedProfile:
    return profile(languages=[language("de" if code == "en" else "en", "C1")])


CENTRE: Final = ScopeLocation("Berlin", "geonames:2950159", 52.52, 13.405, 20, "berlin")
_KM_PER_DEGREE: Final = 6371.0088 * math.pi / 180  # the gazetteer's haversine radius


def _east(km: float) -> Candidate:
    degrees = km / (_KM_PER_DEGREE * math.cos(math.radians(CENTRE.lat)))
    return Candidate(gazetteer_id=f"test:{km}", country_code="DE", lat=CENTRE.lat, lon=CENTRE.lon + degrees)


def km_east(*km: float) -> Location:
    """A resolved location with one candidate `km` east of `CENTRE` per distance given."""
    return Location(
        label="Erfundenes Dorf",
        resolution="unique" if len(km) == 1 else "ambiguous",
        candidates=[_east(distance) for distance in km],
        postal_code=None,
        source="test",
    )
