"""The inputs of a score instance (spec §11.11 L4581, §7.11 L2496; M3 Task 6): the job det-facts that the
deterministic criteria, the knockouts and the industry match read, each with its source ids, and the scope
fields they read, serialised explicitly (never `dataclasses.asdict` of a `ScopeSettings`, whose mapping
fields are `MappingProxyType`s: Controller carry T3-c), with the content hash of their canonical JSON. A
field no instance reads changes neither the document nor the hash; a reordering of a set, a location list or
a mapping changes nothing. Every datum is invented.
"""

from types import MappingProxyType
from typing import Final

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
    assert list(job) == list(JOB_INPUT_FIELDS) and list(where) == list(SCOPE_INPUT_FIELDS)
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


def test_a_read_field_changes_the_hash() -> None:
    _, base = det_inputs(facts(), SOURCES, scope())
    changed = [
        det_inputs(facts(work_mode_possible=frozenset({"remote"})), SOURCES, scope()),
        det_inputs(facts(salary=SALARY), SOURCES, scope()),
        det_inputs(facts(industry_structured="Handel"), SOURCES, scope()),
        det_inputs(facts(), {**SOURCES, "salary": ["jsearch"]}, scope()),
        det_inputs(facts(), SOURCES, scope(remote_within_germany=False)),
        det_inputs(facts(), SOURCES, scope(min_salary_eur_year=1)),
        det_inputs(facts(), SOURCES, scope(industries_exclude=frozenset({"handel"}))),
        det_inputs(facts(), SOURCES, scope(knockout_severity=MappingProxyType({"degree": "hard"}))),
        det_inputs(facts(), SOURCES, scope(locations=(CENTRE,))),
    ]
    assert all(digest != base for _, digest in changed)
