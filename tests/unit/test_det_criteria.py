"""The deterministic criteria (spec §11.8, L4490-4510; M3 Task 3): `location_work_mode` from the distance
bands and `compat(m)` of each possible work mode, and `employment_salary` from the employment types and the
highest stated EUR figure. The `[20, 80]` interval examples of §11.8 are the aggregation's (Task 6)."""

from typing import Any

import pytest
from jobhunter.llm.schemas.generator import RemoteRegion
from jobhunter.run_settings import ScopeLocation, ScopeSettings
from jobhunter.scoring.det_criteria import DetCriterion, employment_salary, location_work_mode
from jobhunter.scoring.filters import DetFacts
from jobhunter.sources.parsing.salary import Salary, SalaryKind, parse_salary

from tests.support.facts import UNRESOLVED, facts, scope
from tests.support.scoring import CENTRE, km_east

ALL_TYPES = frozenset(
    {
        "full_time",
        "part_time",
        "contract",
        "temporary",
        "internship",
        "working_student",
        "apprenticeship",
        "freelance",
    }
)


def modes(*names: str) -> dict[str, Any]:
    return {"work_mode_possible": frozenset(names), "work_mode_source": "structured"}


def located(*locations: Any, possible: tuple[str, ...] = ("onsite",), **more: Any) -> DetFacts:
    return facts(locations=locations, **modes(*possible), **more)


def region(value: str, *countries: str) -> RemoteRegion:
    return RemoteRegion.model_validate(
        {"value": value, "countries": list(countries), "evidence": ["Remote innerhalb Europas"]}
    )


def level(det: DetFacts, where: ScopeSettings, remote: RemoteRegion | None = None) -> int | None:
    return location_work_mode(det, where, remote).level


AROUND_CENTRE = scope(locations=(CENTRE,))


# -- location_work_mode --------------------------------------------------------------------------------------


def test_the_onsite_band_at_1_4_radii_is_2() -> None:
    criterion = location_work_mode(located(km_east(28)), AROUND_CENTRE, None)
    assert criterion == DetCriterion(
        level=2,
        basis="observed",
        inputs={"compat": {"onsite": 2}, "onsite_band": 2, "hybrid_band": 2},
    )


@pytest.mark.parametrize(
    ("km", "onsite", "hybrid"),
    [(10, 4, 4), (19.99, 4, 4), (28, 2, 2), (35, 0, 2), (60, 0, 0)],  # x = 0.5, 1, 1.4, 1.75, 3
)
def test_the_onsite_and_hybrid_bands(km: float, onsite: int, hybrid: int) -> None:
    assert level(located(km_east(km), possible=("onsite",)), AROUND_CENTRE) == onsite
    assert level(located(km_east(km), possible=("hybrid",)), AROUND_CENTRE) == hybrid


def test_candidates_in_different_bands_make_the_band_unknown() -> None:
    criterion = location_work_mode(located(km_east(10, 35)), AROUND_CENTRE, None)
    assert (criterion.level, criterion.basis) == (None, "unknown")
    assert criterion.inputs["onsite_band"] is None


def test_the_jobs_band_is_4_if_any_location_has_4_else_unknown_if_any_is_unknown_else_the_highest() -> None:
    assert level(located(km_east(10), UNRESOLVED), AROUND_CENTRE) == 4
    assert level(located(km_east(28), UNRESOLVED), AROUND_CENTRE) is None
    assert level(located(km_east(28), km_east(60)), AROUND_CENTRE) == 2
    assert level(located(), AROUND_CENTRE) is None  # no job location: nothing to measure


def test_with_no_scope_location_the_band_is_0() -> None:
    assert level(located(km_east(10)), scope()) == 0
    assert level(located(km_east(10), possible=("hybrid",)), scope()) == 0


def test_the_distance_is_the_minimum_over_the_scope_locations_each_with_its_own_radius() -> None:
    def distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        return {(CENTRE.lat, CENTRE.lon): 30.0}.get((lat1, lon1), 5.0)  # 1.5 x 20 km; 5 km from the other

    other = ScopeLocation("Erfunden", "test:other", 50.0, 8.0, 10, "erfunden")
    det = located(km_east(99))
    assert location_work_mode(det, AROUND_CENTRE, None, distance_km=distance).level == 2  # x = 1.5
    assert location_work_mode(det, scope(locations=(CENTRE, other)), None, distance_km=distance).level == 4


def test_a_mode_outside_the_scope_is_0() -> None:
    assert level(located(km_east(10), possible=("remote",)), scope(work_modes=frozenset({"onsite"}))) == 0
    assert level(located(km_east(10)), scope(work_modes=frozenset({"remote"}), locations=(CENTRE,))) == 0


@pytest.mark.parametrize(
    ("structured", "remote", "expected"),
    [
        ((), region("germany"), 4),
        ((), region("dach"), 4),
        ((), region("eu_eea"), 4),
        ((), region("europe"), 4),
        ((), region("worldwide"), 4),
        ((), region("other_country"), 0),
        ((), region("unspecified"), None),
        ((), None, None),
        ((), region("unspecified", "DE", "AT"), 4),  # grounded countries with DE
        ((), region("europe", "FR", "ES"), 0),  # countries without DE are evidence against
        (("DE",), None, 4),  # structured remote countries
        (("de",), region("other_country"), 4),  # the structured value outranks the fact
        (("AT", "CH"), region("germany"), 0),
    ],
)
def test_remote_with_the_toggle_on_needs_positive_evidence_of_germany(
    structured: tuple[str, ...], remote: RemoteRegion | None, expected: int | None
) -> None:
    det = facts(**modes("remote"), remote_countries=structured)
    assert level(det, scope(remote_within_germany=True), remote) == expected


def test_remote_with_the_toggle_off_is_the_hybrid_band() -> None:
    det = located(km_east(35), possible=("remote",))
    where = scope(remote_within_germany=False, locations=(CENTRE,))
    assert level(det, where, region("germany")) == 2  # the hybrid band at 1.75 radii
    assert level(det, scope(remote_within_germany=False), region("other_country")) == 0  # no scope location


def test_the_level_exists_only_when_every_possible_mode_agrees() -> None:
    det = located(km_east(10), possible=("remote", "onsite"), remote_countries=("DE",))
    assert location_work_mode(det, AROUND_CENTRE, None) == DetCriterion(
        level=4,
        basis="observed",
        inputs={"compat": {"onsite": 4, "remote": 4}, "onsite_band": 4, "hybrid_band": 4},
    )
    assert level(
        located(km_east(28), possible=("remote", "onsite"), remote_countries=("DE",)), AROUND_CENTRE
    ) is (None)  # 4 and 2
    unknown_remote = located(km_east(10), possible=("remote", "hybrid", "onsite"))
    assert level(unknown_remote, AROUND_CENTRE) is None  # remote unspecified: null


# -- employment_salary ---------------------------------------------------------------------------------------


def salaried(salary: Salary | None, **more: Any) -> DetFacts:
    return facts(salary=salary, **more)


def stated(
    minimum: float | None, maximum: float | None, *, currency: str = "EUR", kind: SalaryKind = "stated"
) -> Salary:
    eur = currency == "EUR"
    return Salary(
        min=minimum,
        max=maximum,
        currency=currency,
        period="year",
        kind=kind,
        raw=None,
        annual_eur_min=minimum if eur else None,
        annual_eur_max=maximum if eur else None,
    )


def test_a_minimum_only_salary_is_compared_by_its_minimum() -> None:
    salary = parse_salary("ab 60.000 €")
    assert salary is not None and (salary.annual_eur_min, salary.annual_eur_max) == (60_000, None)
    where = scope(min_salary_eur_year=55_000)
    assert where.employment_types == ALL_TYPES  # the scope accepts every type: et = 4 with no stated type
    assert employment_salary(salaried(salary), where) == DetCriterion(
        level=4, basis="observed", inputs={"et": 4, "sal": 4}
    )


@pytest.mark.parametrize(
    ("salary", "sal"),
    [
        (stated(40_000, 55_000), 4),  # the highest figure meets the minimum
        (stated(40_000, 50_000), 2),  # >= 0.9 x 55,000 = 49,500
        (stated(40_000, 49_000), 0),
        (stated(49_500, None), 2),
        (stated(70_000, 80_000, kind="predicted"), None),
        (stated(70_000, 80_000, currency="CHF"), None),  # no EUR figure
        (None, None),
    ],
)
def test_the_salary_sub_rating(salary: Salary | None, sal: int | None) -> None:
    criterion = employment_salary(salaried(salary), scope(min_salary_eur_year=55_000))
    assert criterion.inputs == {"et": 4, "sal": sal}
    assert (criterion.level, criterion.basis) == (
        sal,
        "unknown" if sal is None else "observed",
    )  # min(4, sal)


def test_an_unset_minimum_is_4_whatever_the_salary() -> None:
    assert employment_salary(salaried(None), scope()).inputs == {"et": 4, "sal": 4}
    predicted = stated(10_000, None, kind="predicted")
    assert employment_salary(salaried(predicted), scope()).level == 4


@pytest.mark.parametrize(
    ("types", "accepted", "et"),
    [
        ({"full_time"}, {"full_time", "part_time"}, 4),
        ({"freelance", "full_time"}, {"full_time"}, 4),
        ({"freelance"}, {"full_time", "part_time"}, 0),
        (set(), {"full_time", "part_time"}, None),
        (set(), ALL_TYPES, 4),
    ],
)
def test_the_employment_type_sub_rating(types: set[str], accepted: set[str], et: int | None) -> None:
    det = facts(employment_types=frozenset(types), employment_source="structured" if types else "none")
    criterion = employment_salary(det, scope(employment_types=frozenset(accepted)))
    assert criterion.inputs == {"et": et, "sal": 4}
    assert criterion.level == et


def test_either_zero_is_0_else_either_null_is_null_else_the_minimum() -> None:
    disjoint = facts(employment_types=frozenset({"freelance"}), employment_source="structured")
    where = scope(employment_types=frozenset({"full_time"}), min_salary_eur_year=55_000)
    assert employment_salary(salaried(None, **_types(disjoint)), where).level == 0  # et 0, sal null
    no_type = scope(employment_types=frozenset({"full_time"}), min_salary_eur_year=55_000)
    assert employment_salary(salaried(stated(10_000, None)), no_type).level == 0  # et null, sal 0
    assert employment_salary(salaried(stated(60_000, None)), no_type) == DetCriterion(
        level=None, basis="unknown", inputs={"et": None, "sal": 4}
    )
    full_time = facts(salary=stated(50_000, None), employment_types=frozenset({"full_time"}))
    assert employment_salary(full_time, where).level == 2  # min(4, 2)


def _types(det: DetFacts) -> dict[str, Any]:
    return {"employment_types": det.employment_types, "employment_source": det.employment_source}
