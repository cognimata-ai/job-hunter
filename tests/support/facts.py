"""Hard-filter test data (Task 16): job det-facts and scopes over the seeded defaults, and locations.

`facts(**overrides)` is a `DetFacts` with nothing known (unknown work mode, no location, no structured
value, language `unknown`): every filter passes it, so a test overrides exactly the facts it exercises.
`scope(**overrides)` is the seeded scope (`settings_v1.json`: no location, remote within Germany, every work
mode and employment type, a profile-relative seniority range, German and English ads, nothing excluded)
with the given fields replaced.

The locations are what the parser stores in `jobs.locations`, with gazetteer ids. `POTSDAM_40KM` is labelled
Potsdam but lies 40 km from the centre of Berlin (52.52, 13.405), which puts it between 1x and 2x a 25 km
radius around it; the real Potsdam is 27 km out. `AMBIGUOUS_WITH_ONE_DE_CANDIDATE` is `Linz`: Linz am Rhein
and Linz in Austria.
"""

from dataclasses import replace
from typing import Any, Final

from jobhunter.run_settings import ScopeSettings, scope_from_payload
from jobhunter.scoring.filters import DetFacts
from jobhunter.sources.parsing.locations import Candidate, Location

from tests.support.quota import seeded_payload


def _place(label: str, gazetteer_id: str, country: str, lat: float, lon: float) -> Location:
    candidate = Candidate(gazetteer_id=gazetteer_id, country_code=country, lat=lat, lon=lon)
    return Location(label=label, resolution="unique", candidates=[candidate], postal_code=None, source="test")


BERLIN: Final = _place("Berlin", "geonames:2950159", "DE", 52.52437, 13.41053)
HAMBURG: Final = _place("Hamburg", "geonames:2911298", "DE", 53.55073, 9.99302)
LONDON: Final = _place("London", "geonames:2643743", "GB", 51.50853, -0.12574)
POTSDAM_40KM: Final = _place("Potsdam", "geonames:2852458", "DE", 52.338, 12.896)
AMBIGUOUS_WITH_ONE_DE_CANDIDATE: Final = Location(
    label="Linz",
    resolution="ambiguous",
    candidates=[
        Candidate(gazetteer_id="geonames:2876952", country_code="DE", lat=50.56884, lon=7.28445),
        Candidate(gazetteer_id="geonames:2772400", country_code="AT", lat=48.30639, lon=14.28611),
    ],
    postal_code=None,
    source="test",
)
UNRESOLVED: Final = Location(
    label="Somewhere", resolution="unresolved", candidates=[], postal_code=None, source="test"
)

_UNKNOWN_FACTS: Final = DetFacts(
    company_key="acme",
    title_norm="python developer",
    seniority_tokens=frozenset(),
    structured_seniority=None,
    locations=(),
    location_kind="unknown",
    work_mode_possible=frozenset({"remote", "hybrid", "onsite"}),
    work_mode_source="none",
    remote_countries=(),
    employment_types=frozenset(),
    employment_source="none",
    salary=None,
    industry_structured=None,
    ad_language="unknown",
    posted_at_effective=None,
)


def facts(**overrides: Any) -> DetFacts:
    """`_UNKNOWN_FACTS` with the given fields replaced."""
    return replace(_UNKNOWN_FACTS, **overrides)


def seeded_scope() -> ScopeSettings:
    """The scope of the seeded settings version 1 (with its `max_ad_age_days`)."""
    return scope_from_payload(seeded_payload()["scope"])


def scope(**overrides: Any) -> ScopeSettings:
    """The seeded scope with the given fields replaced."""
    return replace(seeded_scope(), **overrides)
