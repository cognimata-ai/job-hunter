"""The inputs of a score instance (spec §11.11 L4581, §7.11 L2496; M3 Task 6): the job det-facts that the
deterministic criteria, the knockouts and the industry match read, each with its source ids, and the scope
fields they read, serialised explicitly (never `dataclasses.asdict` of a `ScopeSettings`, whose mapping
fields are `MappingProxyType`s: Controller carry T3-c), with the content hash of their canonical JSON. Each
read field, and each job field's source ids, changes the hash; a field no instance reads changes neither the
document nor the hash; a reordering of a set, a location list or a mapping changes nothing. Every datum is
invented.
"""

from types import MappingProxyType
from typing import Any, Final

import pytest
from jobhunter.db.seed import canonical_json, content_hash
from jobhunter.run_settings import ScopeLocation
from jobhunter.scoring.det_results import JOB_INPUT_FIELDS, SCOPE_INPUT_FIELDS, det_inputs
from jobhunter.sources.parsing.salary import Salary

from tests.support.facts import BERLIN, HAMBURG, facts, scope
from tests.support.scoring import CENTRE

SOURCES: Final = {
    "locations": ["greenhouse", "jsearch", "greenhouse"],
    "salary": ["adzuna"],
    "employment_types": ["jsearch"],
    "title_norm": ["jsearch"],  # read by no instance
}
OTHER: Final = ScopeLocation("Hamburg", "geonames:2911298", 53.55, 9.99, 30, "hamburg")
SALARY: Final = Salary.model_validate(
    {
        "kind": "stated",
        "currency": "EUR",
        "period": "year",
        "min": 60000,
        "max": 70000,
        "annual_eur_min": 60000,
        "annual_eur_max": 70000,
        "raw": "60.000 - 70.000 EUR",
    }
)


def test_the_document_holds_the_read_fields_with_their_sources() -> None:
    det = facts(
        locations=(BERLIN, HAMBURG),
        work_mode_possible=frozenset({"onsite", "hybrid"}),
        work_mode_source="structured",
        remote_countries=("DE", "AT"),
        employment_types=frozenset({"part_time", "full_time"}),
        employment_source="structured",
        salary=SALARY,
        industry_structured="Logistik",
    )
    doc, digest = det_inputs(det, SOURCES, scope(locations=(CENTRE,), min_salary_eur_year=50_000))
    job, where = doc["job"], doc["scope"]
    assert isinstance(job, dict) and isinstance(where, dict)
    assert list(job) == [
        "locations",
        "work_mode_possible",
        "work_mode_source",
        "remote_countries",
        "employment_types",
        "employment_source",
        "salary",
        "industry_structured",
    ]
    assert list(where) == [
        "locations",
        "remote_within_germany",
        "work_modes",
        "employment_types",
        "min_salary_eur_year",
        "industries_include",
        "industries_exclude",
        "language_policy_default",
        "language_policy",
        "knockout_severity",
    ]
    assert job["locations"] == {  # by canonical JSON: Hamburg's candidate id sorts first
        "value": [HAMBURG.model_dump(mode="json"), BERLIN.model_dump(mode="json")],
        "sources": ["greenhouse", "jsearch"],
    }
    assert job["work_mode_possible"] == {"value": ["hybrid", "onsite"], "sources": []}
    assert job["remote_countries"] == {"value": ["AT", "DE"], "sources": []}
    assert job["employment_types"] == {"value": ["full_time", "part_time"], "sources": ["jsearch"]}
    assert job["salary"] == {"value": SALARY.model_dump(mode="json"), "sources": ["adzuna"]}
    assert job["industry_structured"] == {"value": "Logistik", "sources": []}
    assert where["locations"] == [
        {"gazetteer_id": "geonames:2950159", "lat": 52.52, "lon": 13.405, "radius_km": 20}
    ]
    assert where["min_salary_eur_year"] == 50_000
    assert digest == content_hash(doc)
    assert canonical_json(doc)  # every value is JSON


def test_the_scope_document_reads_its_mappings_explicitly() -> None:
    where = scope(
        language_policy=MappingProxyType({"fr": "soft", "de": "hard"}),
        industries_include=frozenset({"logistik", "handel"}),
    )
    doc, _ = det_inputs(facts(), {}, where)
    assert doc["scope"] == {
        "locations": [],
        "remote_within_germany": True,
        "work_modes": ["hybrid", "onsite", "remote"],
        "employment_types": sorted(where.employment_types),
        "min_salary_eur_year": None,
        "industries_include": ["handel", "logistik"],
        "industries_exclude": [],
        "language_policy_default": "hard",
        "language_policy": {"de": "hard", "fr": "soft"},
        "knockout_severity": dict(where.knockout_severity),
    }


def test_a_field_no_instance_reads_changes_nothing() -> None:
    base = det_inputs(facts(), SOURCES, scope())
    assert det_inputs(facts(title_norm="etwas anderes", ad_language="de"), SOURCES, scope()) == base
    assert det_inputs(facts(), {**SOURCES, "title_norm": ["adzuna"]}, scope()) == base
    unread = scope(
        excluded_keywords=("praktikum",),
        excluded_companies=frozenset({"acme"}),
        accepted_ad_languages=frozenset({"de"}),
        max_ad_age_days=7,
    )
    assert det_inputs(facts(), SOURCES, unread) == base


def test_an_order_changes_nothing() -> None:
    one = det_inputs(
        facts(locations=(BERLIN, HAMBURG), remote_countries=("DE", "AT")),
        {"locations": ["jsearch", "greenhouse"]},
        scope(locations=(CENTRE, OTHER), language_policy=MappingProxyType({"de": "hard", "fr": "soft"})),
    )
    two = det_inputs(
        facts(locations=(HAMBURG, BERLIN), remote_countries=("AT", "DE")),
        {"locations": ["greenhouse", "jsearch"]},
        scope(locations=(OTHER, CENTRE), language_policy=MappingProxyType({"fr": "soft", "de": "hard"})),
    )
    assert one == two


# One change of each read field, from `facts()` and the seeded `scope()`: each must change the hash.
JOB_CHANGES: Final[dict[str, dict[str, Any]]] = {
    "locations": {"locations": (BERLIN,)},
    "work_mode_possible": {"work_mode_possible": frozenset({"remote"})},
    "work_mode_source": {"work_mode_source": "structured"},
    "remote_countries": {"remote_countries": ("DE",)},
    "employment_types": {"employment_types": frozenset({"full_time"})},
    "employment_source": {"employment_source": "structured"},
    "salary": {"salary": SALARY},
    "industry_structured": {"industry_structured": "Logistik"},
}
SCOPE_CHANGES: Final[dict[str, dict[str, Any]]] = {
    "locations": {"locations": (CENTRE,)},
    "remote_within_germany": {"remote_within_germany": False},
    "work_modes": {"work_modes": frozenset({"remote"})},
    "employment_types": {"employment_types": frozenset({"full_time"})},
    "min_salary_eur_year": {"min_salary_eur_year": 1},
    "industries_include": {"industries_include": frozenset({"logistik"})},
    "industries_exclude": {"industries_exclude": frozenset({"handel"})},
    "language_policy_default": {"language_policy_default": "soft"},
    "language_policy": {"language_policy": MappingProxyType({"fr": "soft"})},
    "knockout_severity": {
        "knockout_severity": MappingProxyType({**scope().knockout_severity, "degree": "hard"})
    },
}
_, BASE = det_inputs(facts(), SOURCES, scope())


def test_every_read_field_has_a_change() -> None:
    assert list(JOB_CHANGES) == list(JOB_INPUT_FIELDS)
    assert list(SCOPE_CHANGES) == list(SCOPE_INPUT_FIELDS)


@pytest.mark.parametrize("field", JOB_INPUT_FIELDS)
def test_a_read_job_field_changes_the_hash(field: str) -> None:
    assert det_inputs(facts(**JOB_CHANGES[field]), SOURCES, scope())[1] != BASE
    assert det_inputs(facts(), {**SOURCES, field: ["ashby"]}, scope())[1] != BASE  # and its sources


@pytest.mark.parametrize("field", SCOPE_INPUT_FIELDS)
def test_a_read_scope_field_changes_the_hash(field: str) -> None:
    assert det_inputs(facts(), SOURCES, scope(**SCOPE_CHANGES[field]))[1] != BASE
