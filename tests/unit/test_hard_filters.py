from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from jobhunter.geo.gazetteer import distance_km
from jobhunter.normalize.titles import normalize_title
from jobhunter.run_settings import ScopeLocation, SeniorityRange
from jobhunter.scoring.filters import DetFacts, det_facts_hash, evaluate
from jobhunter.sources.parsing.locations import Candidate, Location
from jobhunter.sources.parsing.salary import Salary

from tests.support.facts import (
    AMBIGUOUS_WITH_ONE_DE_CANDIDATE,
    BERLIN,
    HAMBURG,
    LONDON,
    POTSDAM_40KM,
    UNRESOLVED,
    facts,
    scope,
)

ONSITE = {"work_mode_possible": frozenset({"onsite"}), "work_mode_source": "structured"}
BERLIN_25 = ScopeLocation("Berlin", "geonames:2950159", 52.52, 13.405, 25, "berlin")
COLOGNE_40 = ScopeLocation("Köln", "geonames:2886242", 50.94, 6.96, 40, "koln")


def test_unknown_passes_and_positive_evidence_fails() -> None:
    assert evaluate(facts(), "", scope()).passed and evaluate(facts(locations=()), "", scope()).passed
    assert evaluate(facts(locations=(LONDON,)), "", scope(remote_within_germany=False)).reasons == (
        "outside_germany",
    )
    assert evaluate(
        facts(locations=(LONDON,), work_mode_possible=frozenset({"remote"}), work_mode_source="structured"),
        "",
        scope(remote_within_germany=True),
    ).passed
    assert evaluate(
        facts(
            locations=(LONDON,),
            work_mode_possible=frozenset({"remote"}),
            work_mode_source="structured",
            remote_countries=("GB",),
        ),
        "",
        scope(),
    ).reasons == ("outside_germany",)
    assert evaluate(facts(locations=(AMBIGUOUS_WITH_ONE_DE_CANDIDATE,)), "", scope()).passed


def test_outside_germany_needs_every_location_resolved_and_foreign() -> None:
    off = scope(remote_within_germany=False)
    assert evaluate(facts(locations=(LONDON, BERLIN)), "", off).passed  # one German location
    assert evaluate(facts(locations=(LONDON, UNRESOLVED)), "", off).passed  # one unresolved: unknown
    assert evaluate(facts(locations=(UNRESOLVED,)), "", off).passed
    assert evaluate(facts(locations=(AMBIGUOUS_WITH_ONE_DE_CANDIDATE, LONDON)), "", off).passed
    assert evaluate(facts(locations=(LONDON, LONDON)), "", off).reasons == ("outside_germany",)
    # Unknown work modes (all three) include remote: the toggle exempts the job, unless structured
    # countries exclude DE.
    assert evaluate(facts(locations=(LONDON,)), "", scope(remote_within_germany=True)).passed
    # A structured remote job that names Germany among its countries is workable from Germany.
    reachable = facts(
        locations=(LONDON,),
        work_mode_possible=frozenset({"remote"}),
        work_mode_source="structured",
        remote_countries=("gb", "de"),
    )
    assert evaluate(reachable, "", scope()).passed
    # A job that cannot be remote is not exempt, whatever the toggle.
    hybrid = facts(
        locations=(LONDON,), work_mode_possible=frozenset({"hybrid", "onsite"}), work_mode_source="text"
    )
    assert evaluate(hybrid, "", scope(remote_within_germany=True)).reasons == ("outside_germany",)


def test_radius_bands_per_scope_location() -> None:
    berlin_25 = scope(locations=(BERLIN_25,))
    assert evaluate(facts(locations=(HAMBURG,), **ONSITE), "", berlin_25).reasons == ("beyond_radius",)
    assert evaluate(facts(locations=(HAMBURG,)), "", berlin_25).passed  # all three work modes: unknown
    assert evaluate(
        facts(locations=(POTSDAM_40KM,), work_mode_possible=frozenset({"hybrid"}), work_mode_source="text"),
        "",
        berlin_25,
    ).passed  # between 1x and 2x the radius
    two = scope(
        locations=(
            *berlin_25.locations,
            ScopeLocation("Hamburg", "geonames:2911298", 53.55, 9.99, 25, "hamburg"),
        )
    )
    assert evaluate(facts(locations=(HAMBURG,), **ONSITE), "", two).passed


def test_the_potsdam_fixture_lies_between_one_and_two_radii_of_berlin() -> None:
    candidate = POTSDAM_40KM.candidates[0]
    assert distance_km(BERLIN_25.lat, BERLIN_25.lon, candidate.lat, candidate.lon) == pytest.approx(
        40, abs=0.1
    )


def test_beyond_radius_needs_a_scope_location_a_job_location_and_no_remote_possibility() -> None:
    assert evaluate(facts(locations=(HAMBURG,), **ONSITE), "", scope()).passed  # the scope has no location
    berlin_25 = scope(locations=(BERLIN_25,))
    assert evaluate(facts(**ONSITE), "", berlin_25).passed  # the job has no location
    hybrid = {"work_mode_possible": frozenset({"onsite", "hybrid"}), "work_mode_source": "text"}
    assert evaluate(facts(locations=(HAMBURG,), **hybrid), "", berlin_25).reasons == ("beyond_radius",)
    remote_too = {"work_mode_possible": frozenset({"remote", "onsite"}), "work_mode_source": "text"}
    assert evaluate(facts(locations=(HAMBURG,), **remote_too), "", berlin_25).passed
    assert evaluate(facts(locations=(UNRESOLVED,), **ONSITE), "", berlin_25).passed  # unresolved: unknown
    assert evaluate(facts(locations=(HAMBURG, UNRESOLVED), **ONSITE), "", berlin_25).passed
    # Every location must be beyond: Hamburg and London are, Potsdam is not.
    assert evaluate(facts(locations=(HAMBURG, LONDON), **ONSITE), "", berlin_25).reasons == ("beyond_radius",)
    assert evaluate(facts(locations=(HAMBURG, POTSDAM_40KM), **ONSITE), "", berlin_25).passed


def test_a_location_whose_candidates_fall_into_different_bands_is_unknown() -> None:
    # Linz am Rhein is 47 km from Köln (between 1x and 2x of 40 km), Linz in Austria 603 km (beyond).
    cologne = scope(locations=(COLOGNE_40,))
    assert evaluate(facts(locations=(AMBIGUOUS_WITH_ONE_DE_CANDIDATE,), **ONSITE), "", cologne).passed
    # Both candidates beyond the same radius: the band is known.
    far = Location(
        label="Linz",
        resolution="ambiguous",
        candidates=[
            Candidate(gazetteer_id="x:1", country_code="DE", lat=48.1, lon=11.6),
            Candidate(gazetteer_id="x:2", country_code="AT", lat=48.3, lon=14.3),
        ],
        postal_code=None,
        source="test",
    )
    assert evaluate(facts(locations=(far,), **ONSITE), "", cologne).reasons == ("beyond_radius",)


def test_structured_only_filters_and_keywords() -> None:
    assert evaluate(
        facts(work_mode_possible=frozenset({"onsite"}), work_mode_source="structured"),
        "",
        scope(work_modes=frozenset({"remote"})),
    ).reasons == ("work_mode_excluded",)
    assert evaluate(
        facts(work_mode_possible=frozenset({"onsite"}), work_mode_source="text"),
        "",
        scope(work_modes=frozenset({"remote"})),
    ).passed
    assert evaluate(
        facts(employment_types=frozenset({"internship"}), employment_source="structured"),
        "",
        scope(employment_types=frozenset({"full_time"})),
    ).reasons == ("employment_type_excluded",)
    assert evaluate(facts(company_key="acme"), "", scope(excluded_companies=frozenset({"acme"}))).reasons == (
        "excluded_company",
    )
    assert evaluate(
        facts(title_norm="senior sap consultant"), "we use SAP daily", scope(excluded_keywords=("sap",))
    ).reasons == ("excluded_keyword",)
    assert evaluate(
        facts(title_norm="senior saperlot engineer"), "we use SAP daily", scope(excluded_keywords=("sap",))
    ).passed  # whole word only, description off by default
    assert evaluate(
        facts(title_norm="engineer"),
        "we use SAP daily",
        scope(excluded_keywords=("sap",), exclude_keywords_in_description=True),
    ).reasons == ("excluded_keyword",)
    assert (
        evaluate(facts(ad_language="other"), "", scope()).reasons == ("ad_language_excluded",)
        and evaluate(facts(ad_language="unknown"), "", scope()).passed
    )
    assert evaluate(
        facts(industry_structured="gaming"), "", scope(industries_exclude=frozenset({"gaming"}))
    ).reasons == ("excluded_industry",)


def test_structured_values_that_say_nothing_are_unknown() -> None:
    nothing = scope(work_modes=frozenset({"remote"}), employment_types=frozenset({"full_time"}))
    assert evaluate(facts(work_mode_possible=frozenset(), work_mode_source="structured"), "", nothing).passed
    assert evaluate(facts(employment_types=frozenset(), employment_source="structured"), "", nothing).passed
    assert evaluate(
        facts(employment_types=frozenset({"internship"}), employment_source="text"), "", nothing
    ).passed
    assert evaluate(
        facts(industry_structured=None), "", scope(industries_exclude=frozenset({"gaming"}))
    ).passed
    assert evaluate(facts(ad_language="de"), "", scope(accepted_ad_languages=frozenset({"de"}))).passed
    assert evaluate(facts(ad_language="en"), "", scope(accepted_ad_languages=frozenset({"de"}))).reasons == (
        "ad_language_excluded",
    )
    # The scope's industries are case-folded; the job's structured industry is folded to meet them.
    assert evaluate(
        facts(industry_structured="Gaming"), "", scope(industries_exclude=frozenset({"gaming"}))
    ).reasons == ("excluded_industry",)


def test_excluded_keywords_go_through_the_normalisation_of_title_norm() -> None:
    """Carry 1: `title_norm` collapses in-word hyphens to spaces, so a hyphenated keyword is normalised the
    same way before the whole-word match, on the title and on the description."""
    title = normalize_title("Front-End Developer (m/w/d)")
    assert title.title_norm == "front end developer"
    hyphenated = facts(title_norm=title.title_norm)
    assert evaluate(hyphenated, "", scope(excluded_keywords=("front-end",))).reasons == ("excluded_keyword",)
    assert evaluate(hyphenated, "", scope(excluded_keywords=("Front-End",))).reasons == ("excluded_keyword",)
    assert evaluate(hyphenated, "", scope(excluded_keywords=("front end",))).reasons == ("excluded_keyword",)
    assert evaluate(hyphenated, "", scope(excluded_keywords=("frontend",))).passed  # another word
    assert evaluate(hyphenated, "", scope(excluded_keywords=("front-ends",))).passed  # whole words only
    in_description = scope(excluded_keywords=("front-end",), exclude_keywords_in_description=True)
    assert evaluate(
        facts(title_norm="engineer"), "Wir suchen einen Front-End Entwickler.", in_description
    ).reasons == ("excluded_keyword",)
    assert evaluate(
        facts(title_norm="engineer"), "Wir suchen einen Backend Entwickler.", in_description
    ).passed
    # The description is not read unless the scope says so.
    assert evaluate(
        facts(title_norm="engineer"), "Front-End Entwickler", scope(excluded_keywords=("front-end",))
    ).passed


def test_excluded_keywords_match_whole_words_and_phrases_case_and_accent_insensitively() -> None:
    engineer = facts(title_norm="machine learning engineer")
    assert evaluate(engineer, "", scope(excluded_keywords=("Machine Learning",))).reasons == (
        "excluded_keyword",
    )
    assert evaluate(engineer, "", scope(excluded_keywords=("learn",))).passed
    assert evaluate(engineer, "", scope(excluded_keywords=("engineers",))).passed
    assert evaluate(engineer, "", scope(excluded_keywords=("php", "ENGINEER"))).reasons == (
        "excluded_keyword",
    )
    assert evaluate(engineer, "", scope(excluded_keywords=("",))).passed  # an empty keyword matches nothing
    assert evaluate(engineer, "", scope(excluded_keywords=(" - ",))).passed
    assert evaluate(
        facts(title_norm="berater c++ entwickler"), "", scope(excluded_keywords=("c++",))
    ).reasons == ("excluded_keyword",)
    assert evaluate(facts(title_norm="consultant"), "", scope(excluded_keywords=("c++",))).passed
    assert evaluate(
        facts(title_norm="fuhrungskraft"), "", scope(excluded_keywords=("Führungskraft",))
    ).reasons == ("excluded_keyword",)
    in_description = scope(excluded_keywords=("sap",), exclude_keywords_in_description=True)
    assert evaluate(facts(), "Erfahrung mit SAP, Oracle", in_description).reasons == ("excluded_keyword",)
    assert evaluate(facts(), "Experience with SAPUI5", in_description).passed
    assert evaluate(facts(), "", in_description).passed


def test_seniority_explicit_range_only() -> None:
    explicit = scope(seniority_range=SeniorityRange("explicit", 0, 0, "junior", "senior"))
    assert evaluate(facts(seniority_tokens=frozenset({"head"})), "", explicit).reasons == (
        "seniority_out_of_range",
    )
    assert (
        evaluate(facts(seniority_tokens=frozenset({"senior", "lead"})), "", explicit).passed
        and evaluate(facts(seniority_tokens=frozenset()), "", explicit).passed
    )
    assert evaluate(
        facts(seniority_tokens=frozenset({"head"})), "", scope()
    ).passed  # profile_relative: unknown in M1


def test_seniority_bounds_and_the_structured_level() -> None:
    only_min = scope(seniority_range=SeniorityRange("explicit", 0, 0, "senior", None))
    assert evaluate(facts(seniority_tokens=frozenset({"junior"})), "", only_min).reasons == (
        "seniority_out_of_range",
    )
    assert evaluate(facts(seniority_tokens=frozenset({"head"})), "", only_min).passed
    only_max = scope(seniority_range=SeniorityRange("explicit", 0, 0, None, "mid"))
    assert evaluate(facts(seniority_tokens=frozenset({"intern"})), "", only_max).passed
    assert evaluate(facts(seniority_tokens=frozenset({"principal"})), "", only_max).reasons == (
        "seniority_out_of_range",
    )
    senior_only = scope(seniority_range=SeniorityRange("explicit", 0, 0, "senior", "senior"))
    # `mid` has no title token: only the structured level can put a job there.
    assert evaluate(facts(structured_seniority="mid"), "", senior_only).reasons == ("seniority_out_of_range",)
    # SmartRecruiters and Personio write their own levels; each maps onto the ladder, possibly onto several.
    assert evaluate(facts(structured_seniority="entry_level"), "", senior_only).reasons == (
        "seniority_out_of_range",
    )
    assert evaluate(facts(structured_seniority="entry-level"), "", senior_only).reasons == (
        "seniority_out_of_range",
    )
    assert evaluate(facts(structured_seniority="mid_senior_level"), "", senior_only).passed  # may be senior
    assert evaluate(facts(structured_seniority="experienced"), "", senior_only).passed
    assert evaluate(facts(structured_seniority="Director"), "", senior_only).reasons == (
        "seniority_out_of_range",
    )
    assert evaluate(facts(structured_seniority="not_applicable"), "", senior_only).passed
    assert evaluate(facts(structured_seniority="something new"), "", senior_only).passed  # unknown value
    # The levels of the title and of the structured field are one body of evidence: one inside is enough.
    assert evaluate(
        facts(seniority_tokens=frozenset({"head"}), structured_seniority="mid_senior_level"), "", senior_only
    ).passed
    assert evaluate(
        facts(seniority_tokens=frozenset({"head"}), structured_seniority="entry_level"), "", senior_only
    ).reasons == ("seniority_out_of_range",)


def test_an_unknown_bound_is_open() -> None:
    odd = scope(seniority_range=SeniorityRange("explicit", 0, 0, "wizard", "junior"))
    assert evaluate(facts(seniority_tokens=frozenset({"intern"})), "", odd).passed
    assert evaluate(facts(seniority_tokens=frozenset({"senior"})), "", odd).reasons == (
        "seniority_out_of_range",
    )


def test_every_failed_filter_is_a_reason_in_the_order_of_the_table() -> None:
    everything = scope(
        remote_within_germany=False,
        locations=(BERLIN_25,),
        work_modes=frozenset({"remote"}),
        employment_types=frozenset({"full_time"}),
        seniority_range=SeniorityRange("explicit", 0, 0, "junior", "senior"),
        excluded_companies=frozenset({"acme"}),
        excluded_keywords=("python",),
        industries_exclude=frozenset({"gaming"}),
    )
    failing = facts(
        locations=(LONDON,),
        **ONSITE,
        employment_types=frozenset({"internship"}),
        employment_source="structured",
        seniority_tokens=frozenset({"head"}),
        ad_language="other",
        industry_structured="gaming",
    )
    result = evaluate(failing, "", everything)
    assert not result.passed
    assert result.reasons == (
        "outside_germany",
        "beyond_radius",
        "work_mode_excluded",
        "employment_type_excluded",
        "seniority_out_of_range",
        "excluded_company",
        "excluded_keyword",
        "ad_language_excluded",
        "excluded_industry",
    )


def test_a_passing_result_has_no_reasons() -> None:
    result = evaluate(facts(), "", scope())
    assert (result.passed, result.reasons) == (True, ())


SALARY = Salary(
    min=60000,
    max=80000,
    currency="EUR",
    period="year",
    kind="stated",
    raw="60-80k",
    annual_eur_min=60000,
    annual_eur_max=80000,
)


def test_det_facts_hash_is_a_stable_sha256_of_the_canonical_json() -> None:
    a = facts(
        seniority_tokens=frozenset({"senior", "lead"}),
        remote_countries=("DE", "AT"),
        locations=(BERLIN, HAMBURG),
        salary=SALARY,
        posted_at_effective=datetime(2026, 10, 1, 8, tzinfo=UTC),
    )
    same = replace(
        a,
        seniority_tokens=frozenset({"lead", "senior"}),
        remote_countries=("AT", "DE"),
        locations=(HAMBURG, BERLIN),
    )
    digest = det_facts_hash(a)
    assert isinstance(digest, bytes) and len(digest) == 32
    assert det_facts_hash(same) == digest  # sets and the order of unordered lists do not matter
    assert det_facts_hash(a) == digest
    assert det_facts_hash(facts()) != digest


@pytest.mark.parametrize(
    "change",
    [
        {"company_key": "other"},
        {"title_norm": "other"},
        {"seniority_tokens": frozenset({"head"})},
        {"structured_seniority": "entry_level"},
        {"locations": (BERLIN,)},
        {"location_kind": "city"},
        {"work_mode_possible": frozenset({"remote"})},
        {"work_mode_source": "text"},
        {"remote_countries": ("DE",)},
        {"employment_types": frozenset({"full_time"})},
        {"employment_source": "structured"},
        {"salary": SALARY},
        {"industry_structured": "gaming"},
        {"ad_language": "de"},
        {"posted_at_effective": datetime(2026, 10, 1, tzinfo=UTC)},
    ],
)
def test_every_det_fact_is_in_the_hash(change: dict[str, Any]) -> None:
    assert det_facts_hash(replace(facts(), **change)) != det_facts_hash(facts())


def test_the_hash_reads_the_candidates_of_a_location() -> None:
    moved = Location(
        label="Berlin",
        resolution="unique",
        candidates=[Candidate(gazetteer_id="geonames:2950159", country_code="DE", lat=52.6, lon=13.4)],
        postal_code=None,
        source="test",
    )
    assert det_facts_hash(facts(locations=(BERLIN,))) != det_facts_hash(facts(locations=(moved,)))


def test_the_facts_are_the_det_facts_of_the_spec() -> None:
    assert tuple(DetFacts.__dataclass_fields__) == (
        "company_key",
        "title_norm",
        "seniority_tokens",
        "structured_seniority",
        "locations",
        "location_kind",
        "work_mode_possible",
        "work_mode_source",
        "remote_countries",
        "employment_types",
        "employment_source",
        "salary",
        "industry_structured",
        "ad_language",
        "posted_at_effective",
    )
