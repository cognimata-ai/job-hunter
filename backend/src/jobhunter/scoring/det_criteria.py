"""The two deterministic criteria (spec §11.8, L4490-4510), as pure functions of the job's `DetFacts`, the
scope and, for remote work, the draft's grounded `remote_region` fact: no connection, no clock.

**`location_work_mode`.** `P = work_mode_possible`, `A = scope.work_modes`, the scope locations `i` with their
radii `r_i ≥ 1 km` (Settings refuses smaller radii, so `d / r_i` is always defined), and the "remote within
Germany" toggle.
- *Distance bands*, per job location and work mode, from the candidates `c` of the location (§8.12), with
  `x = min_i d(c, i) / r_i`: on-site `x ≤ 1` is 4, `x ≤ 1.5` is 2, else 0; hybrid `x ≤ 1` is 4, `x ≤ 2` is 2,
  else 0. A location whose candidates all give one band has that band; one whose candidates disagree, or an
  unresolved one, has an unknown band. The job's band is 4 when any location has 4, else unknown when any is
  unknown, else the highest. With no scope location the band is 0. A job with no location has an unknown
  band: there is no distance to measure (the spec names no rule for it).
- *`compat(m)`* for each `m ∈ P`: 0 when `m ∉ A`. `hybrid` is the hybrid band and `onsite` the on-site band.
  `remote`, with the toggle off, is the hybrid band; with the toggle on it is 4 only on positive evidence that
  Germany is eligible and 0 on positive evidence that it is not, else `null` (`unspecified` is unknown). The
  evidence, most specific first: the structured `remote_countries` when there are any (DE among them or not);
  else the grounded fact's `countries` when it lists any; else its value (`germany`, `dach`, `eu_eea`,
  `europe` or `worldwide` for, `other_country` against). The caller passes the fact only when its evidence is
  grounded (V3), else `None`.
- `level = v` iff every `compat(m)`, `m ∈ P`, is the same non-null `v`; else `null` (A3 G32, as the spec has
  it). The basis is `observed` with a level and `unknown` without.

**`employment_salary`.** `et` is 4 when the job's employment types meet the scope's, 0 when they are
non-empty and disjoint, and `null` when empty, except 4 when the scope accepts every type. `sal` is 4 when
`min_salary_eur_year` is unset; otherwise it compares the highest stated EUR figure (`annual_eur_max`, else
`annual_eur_min` for a minimum-only salary; A3 G31): `≥ min` is 4, `≥ 0.9 x min` is 2, below 0; a predicted
or unknown salary (or one in another currency) is `null`. `level` is 0 when either is 0, `null` when either
is `null`, else `min(et, sal)`.

`inputs` holds the compared values (bands, sub-ratings, compat by mode), never ad text. The on-site band
helpers are shared with the `onsite_required` knockout (`scoring.knockouts`).

**The deterministic `experience_seniority` rating** (§11.3 L4085-4103), which the validator puts in place of
the generator's level (V4, §11.6): `seniority_level` is the minimum of the two sub-ratings whose inputs are
known, `None` when neither is.
- *Level.* The ordinal scale `SENIORITY_ORDINAL` (intern, working student, apprentice 0; junior 1; mid 2;
  senior 3; lead, principal 4; head 5; not `normalize.titles.SENIORITY_LADDER`, the order of the levels) and
  `g = profile level - ad level`: 0 is 4, +1 is 3, -1 is 2, +2 is 1, and ≤ -2 or ≥ +3 is 0. The ad's level
  is the `seniority` fact's value; when the ad states none (`unspecified`, or the caller passes no fact), the
  title's seniority tokens give it when they name one ordinal level (a title with tokens of two levels,
  `Junior/Senior`, says nothing).
- *Years.* `y = profile total years - the ad's minimum years`: `y ≥ 0` is 4, `-1 ≤ y < 0` is 3,
  `-2 ≤ y < -1` is 2, `-5 < y < -2` is 1, `y ≤ -5` is 0. A minimum outside 0-40 is unknown (§11.6 L4390).
The caller passes a fact only when its evidence is grounded (V3), else `None`, as for `location_work_mode`.
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Literal, get_args

from jobhunter.geo import gazetteer
from jobhunter.llm.schemas.generator import MinYearsExperience, RemoteRegion, Seniority
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.run_settings import ScopeLocation, ScopeSettings
from jobhunter.scoring.filters import DetFacts
from jobhunter.settings.model import EmploymentType
from jobhunter.sources.parsing.locations import GERMANY, Location
from jobhunter.types import JSONValue

DistanceKm = Callable[[float, float, float, float], float]
Bounds = tuple[float, float]  # the largest `x` of band 4, then of band 2

ONSITE_BOUNDS: Final[Bounds] = (1.0, 1.5)
HYBRID_BOUNDS: Final[Bounds] = (1.0, 2.0)
ALL_EMPLOYMENT_TYPES: Final = frozenset(get_args(EmploymentType))
_REMOTE_GERMANY: Final = frozenset({"germany", "dach", "eu_eea", "europe", "worldwide"})
_SALARY_PARTIAL: Final = (9, 10)  # sal 2 at 0.9 x the minimum, compared as 10 x figure >= 9 x minimum


@dataclass(frozen=True)
class DetCriterion:
    level: int | None
    basis: Literal["observed", "unknown"]
    inputs: dict[str, JSONValue]


def _criterion(level: int | None, inputs: dict[str, JSONValue]) -> DetCriterion:
    return DetCriterion(level, "unknown" if level is None else "observed", inputs)


def _band(x: float, bounds: Bounds) -> int:
    full, partial = bounds
    if x <= full:
        return 4
    return 2 if x <= partial else 0


def location_band(
    location: Location, scope_locations: Sequence[ScopeLocation], bounds: Bounds, distance_km: DistanceKm
) -> int | None:
    """The band of one job location around the (non-empty) scope locations; `None` when it is unresolved or
    its candidates fall into different bands."""
    if location.resolution == "unresolved" or not location.candidates:
        return None
    bands = {
        _band(min(distance_km(i.lat, i.lon, c.lat, c.lon) / i.radius_km for i in scope_locations), bounds)
        for c in location.candidates
    }
    return next(iter(bands)) if len(bands) == 1 else None


def job_band(
    locations: Sequence[Location],
    scope_locations: Sequence[ScopeLocation],
    bounds: Bounds,
    distance_km: DistanceKm,
) -> int | None:
    """The job's band: 0 with no scope location; else 4 if a location has 4, else unknown if one is unknown
    (or there is no job location), else the highest."""
    if not scope_locations:
        return 0
    bands = [location_band(location, scope_locations, bounds, distance_km) for location in locations]
    known = [band for band in bands if band is not None]
    if 4 in known:
        return 4
    if not known or len(known) < len(bands):
        return None
    return max(known)


def _remote_germany(det: DetFacts, remote_region: RemoteRegion | None) -> int | None:
    """`compat(remote)` with the toggle on: 4 or 0 on evidence for or against Germany, else `None`."""
    structured = {country.upper() for country in det.remote_countries}
    if structured:
        return 4 if GERMANY in structured else 0
    if remote_region is None:
        return None
    countries = {country.upper() for country in remote_region.countries}
    if countries:
        return 4 if GERMANY in countries else 0
    if remote_region.value in _REMOTE_GERMANY:
        return 4
    return 0 if remote_region.value == "other_country" else None


def location_work_mode(
    det: DetFacts,
    scope: ScopeSettings,
    remote_region: RemoteRegion | None,
    *,
    distance_km: DistanceKm = gazetteer.distance_km,
) -> DetCriterion:
    """The `location_work_mode` criterion (see the module docstring); `remote_region` is the draft's grounded
    fact, or `None`."""
    onsite = job_band(det.locations, scope.locations, ONSITE_BOUNDS, distance_km)
    hybrid = job_band(det.locations, scope.locations, HYBRID_BOUNDS, distance_km)
    compat: dict[str, int | None] = {}
    for mode in sorted(det.work_mode_possible):
        if mode not in scope.work_modes:
            compat[mode] = 0
        elif mode == "remote":
            compat[mode] = _remote_germany(det, remote_region) if scope.remote_within_germany else hybrid
        elif mode == "hybrid":
            compat[mode] = hybrid
        elif mode == "onsite":
            compat[mode] = onsite
        else:
            raise ValueError(f"unknown work mode {mode!r}")
    values = set(compat.values())
    level = values.pop() if len(values) == 1 else None
    by_mode: dict[str, JSONValue] = {mode: value for mode, value in compat.items()}
    return _criterion(level, {"compat": by_mode, "onsite_band": onsite, "hybrid_band": hybrid})


def _salary_rating(det: DetFacts, minimum: int | None) -> int | None:
    if minimum is None:
        return 4
    salary = det.salary
    if salary is None or salary.kind != "stated":
        return None
    figure = salary.annual_eur_max if salary.annual_eur_max is not None else salary.annual_eur_min
    if figure is None:
        return None
    if figure >= minimum:
        return 4
    numerator, denominator = _SALARY_PARTIAL
    return 2 if denominator * figure >= numerator * minimum else 0


def employment_salary(det: DetFacts, scope: ScopeSettings) -> DetCriterion:
    """The `employment_salary` criterion (see the module docstring)."""
    types = det.employment_types
    et: int | None
    if types & scope.employment_types:
        et = 4
    elif types:
        et = 0
    else:
        et = 4 if scope.employment_types >= ALL_EMPLOYMENT_TYPES else None
    sal = _salary_rating(det, scope.min_salary_eur_year)
    level: int | None
    if et == 0 or sal == 0:
        level = 0
    elif et is None or sal is None:
        level = None
    else:
        level = min(et, sal)
    return _criterion(level, {"et": et, "sal": sal})


# ---- experience_seniority (§11.3) -------------------------------------------------------------------------

SENIORITY_ORDINAL: Final = {
    "intern": 0,
    "working_student": 0,
    "apprentice": 0,
    "junior": 1,
    "mid": 2,
    "senior": 3,
    "lead": 4,
    "principal": 4,
    "head": 5,
}
MIN_YEARS_RANGE: Final = (0, 40)
_LEVEL_SUB_RATING: Final = {0: 4, 1: 3, -1: 2, 2: 1}  # every other g is 0


def level_sub_rating(g: int) -> int:
    """The level sub-rating of `g = profile level - ad level`."""
    return _LEVEL_SUB_RATING.get(g, 0)


def years_sub_rating(y: float) -> int:
    """The years sub-rating of `y = profile total years - required minimum years`."""
    if y >= 0:
        return 4
    if y >= -1:
        return 3
    if y >= -2:
        return 2
    return 1 if y > -5 else 0


def min_years_in_range(years: int) -> bool:
    """Whether a minimum of years is within `MIN_YEARS_RANGE` (0-40, §11.5); outside it, it is unknown."""
    low, high = MIN_YEARS_RANGE
    return low <= years <= high


def _ad_level(seniority_fact: Seniority | None, title_tokens: Iterable[str]) -> int | None:
    if seniority_fact is not None and seniority_fact.value != "unspecified":
        return SENIORITY_ORDINAL[seniority_fact.value]
    levels = {SENIORITY_ORDINAL[token] for token in title_tokens if token in SENIORITY_ORDINAL}
    return levels.pop() if len(levels) == 1 else None


def seniority_level(
    profile: ConfirmedProfile,
    seniority_fact: Seniority | None,
    min_years_fact: MinYearsExperience | None,
    title_tokens: Iterable[str],
) -> int | None:
    """The deterministic `experience_seniority` level (see the module docstring)."""
    ratings: list[int] = []
    ad_level = _ad_level(seniority_fact, title_tokens)
    if ad_level is not None:
        ratings.append(level_sub_rating(SENIORITY_ORDINAL[profile.seniority] - ad_level))
    years = None if min_years_fact is None else min_years_fact.value
    if years is not None and min_years_in_range(years) and profile.total_years_experience is not None:
        ratings.append(years_sub_rating(profile.total_years_experience - years))
    return min(ratings, default=None)
