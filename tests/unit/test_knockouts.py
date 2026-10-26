"""Knockout candidates (spec §11.4, L4176-4202; M3 Task 3): the three layers (requirement, comparison,
review need), the comparison table verbatim, the severities from the scope, `binding` (L4192) and the facts
the critic must review (`required_facts`, §11.7.2). The scope's scoring fields come from
`ScopeSettings.from_model`.
"""

import json
from dataclasses import replace
from typing import Any

import pytest
from jobhunter.geo.gazetteer import distance_km
from jobhunter.llm.schemas.generator import (
    Certification,
    Degree,
    JobFacts,
    OnsiteOnly,
    RequiredLanguage,
    SecurityClearance,
    WorkPermit,
)
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.run_settings import ScopeSettings
from jobhunter.scoring.facts import FactRef
from jobhunter.scoring.filters import DetFacts
from jobhunter.scoring.knockouts import (
    KnockoutCandidate,
    binding,
    derive_knockouts,
    required_facts,
    token_set_similarity,
)
from jobhunter.settings.model import ScopeVersionPayload

from tests.support.facts import UNRESOLVED, facts, scope
from tests.support.scoring import (
    CENTRE,
    job_facts,
    km_east,
    language,
    profile,
    profile_with_language,
    profile_without,
    required_language,
)

ONSITE_TEXT = {"onsite_only": OnsiteOnly(asserted=True, evidence=["Arbeit ausschließlich vor Ort"])}
STRUCTURED_ONSITE = {"work_mode_possible": frozenset({"onsite"}), "work_mode_source": "structured"}


def candidates(
    job: JobFacts | None = None,
    det: DetFacts | None = None,
    person: ConfirmedProfile | None = None,
    where: ScopeSettings | None = None,
) -> list[KnockoutCandidate]:
    return derive_knockouts(
        job_facts() if job is None else job,
        facts() if det is None else det,
        profile() if person is None else person,
        scope() if where is None else where,
    )


def only(found: list[KnockoutCandidate]) -> KnockoutCandidate:
    assert len(found) == 1, found
    return found[0]


def comparison(fact: RequiredLanguage, person: ConfirmedProfile) -> str:
    return only(candidates(job_facts(required_languages=[fact]), person=person)).comparison


def onsite(det: DetFacts | None = None, where: ScopeSettings | None = None, **job: Any) -> KnockoutCandidate:
    return only(candidates(job_facts(**job), det, where=where))


# -- the scope's scoring fields ------------------------------------------------------------------------------


def test_the_scope_view_holds_the_scoring_policies_of_the_payload() -> None:
    payload = ScopeVersionPayload.model_validate(
        {
            "industries_include": ["Software", "FinTech"],
            "language_policy_default": "soft",
            "language_policy": {"de": "hard", "fr": "soft"},
            "min_salary_eur_year": 55_000,
            "knockout_severity": {"degree": "hard", "work_permit": "soft"},
        }
    )
    view = ScopeSettings.from_model(payload)
    assert view.industries_include == frozenset({"software", "fintech"})  # case-folded
    assert view.language_policy_default == "soft"
    assert dict(view.language_policy) == {"de": "hard", "fr": "soft"}
    assert view.min_salary_eur_year == 55_000
    assert dict(view.knockout_severity) == {
        "degree": "hard",
        "certification": "soft",
        "security_clearance": "hard",
        "work_permit": "soft",
        "onsite_required": "hard",
        "contract_type": "hard",
    }
    seeded = scope()  # the defaults of settings version 1
    assert (seeded.industries_include, seeded.language_policy_default, seeded.min_salary_eur_year) == (
        frozenset(),
        "hard",
        None,
    )
    assert dict(seeded.language_policy) == {} and dict(seeded.knockout_severity)["degree"] == "soft"


# -- mandatory_language --------------------------------------------------------------------------------------


def test_mandatory_language_unspecified_level_follows_the_11_4_table() -> None:  # R13, G9
    profile_b2 = profile_with_language("de", "B2")
    profile_b1 = profile_with_language("de", "B1")
    fact = required_language("de", level="unspecified", mandatory=True, evidence=["Deutsch erforderlich"])
    assert comparison(fact, profile_b2) == "met" and comparison(fact, profile_b1) == "uncomparable"
    assert comparison(fact, profile_without("de")) == "unmet"
    assert comparison(required_language("de", level="C1", mandatory=True), profile_b2) == "unmet"  # below


def test_a_specified_level_is_met_at_or_above_it_and_native_counts_above_c2() -> None:
    assert comparison(required_language("de", level="B2"), profile_with_language("de", "B2")) == "met"
    assert comparison(required_language("de", level="C2"), profile_with_language("de", "native")) == "met"
    assert comparison(required_language("de", level="native"), profile_with_language("de", "C2")) == "unmet"
    assert comparison(
        required_language("de", level="unspecified"), profile_with_language("de", "native")
    ) == ("met")


def test_an_unknown_profile_level_is_uncomparable_unless_a_known_one_meets() -> None:
    fact = required_language("de", level="C1")
    assert comparison(fact, profile_with_language("de", "unspecified")) == "uncomparable"
    two_entries = profile(languages=[language("de", "unspecified"), language("de", "B1")])
    assert comparison(fact, two_entries) == "uncomparable"  # the unspecified entry may be C1
    assert comparison(fact, profile(languages=[language("de", "unspecified"), language("de", "C2")])) == "met"
    assert comparison(fact, profile(languages=[language("de", "A2"), language("de", "B1")])) == "unmet"


def test_an_other_language_is_matched_by_its_folded_name() -> None:
    fact = required_language("other", level="B2", other="Französisch")
    assert comparison(fact, profile(languages=[language("other", "C1", other="FRANZOSISCH")])) == "met"
    assert comparison(fact, profile(languages=[language("other", "A2", other="französisch")])) == "unmet"
    assert (
        comparison(fact, profile(languages=[language("other", "C1", other="Italienisch")])) == "uncomparable"
    )
    assert comparison(fact, profile_without("other")) == "uncomparable"  # a name no `code_other` has


def test_the_language_severity_is_the_policy_of_the_language_or_the_default() -> None:
    job = job_facts(
        required_languages=[
            required_language("de", level="C1"),
            required_language("en", level="C1"),
            required_language("other", level="B2", other="Französisch"),
        ]
    )
    where = scope(language_policy={"de": "soft", "other": "hard"}, language_policy_default="soft")
    assert [(c.subject, c.severity) for c in candidates(job, where=where)] == [
        ("de", "soft"),
        ("en", "soft"),
        ("other:franzosisch", "hard"),
    ]
    assert {c.severity for c in candidates(job)} == {"hard"}  # the seeded default


def test_a_preferred_language_is_no_requirement_and_a_repeated_id_keeps_the_first() -> None:
    assert (
        candidates(job_facts(required_languages=[required_language("de", level="C2", mandatory=False)])) == []
    )
    job = job_facts(
        required_languages=[required_language("de", level="C1"), required_language("de", level="A1")]
    )
    found = only(candidates(job, person=profile_with_language("de", "B2")))
    assert (found.fact_id, found.comparison) == ("mandatory_language:de", "unmet")  # C1, the first


# -- onsite_required -----------------------------------------------------------------------------------------


def test_text_asserted_onsite_is_binding_capable_even_when_met() -> None:  # L4180
    candidate = onsite(facts(locations=(km_east(10),)), scope(locations=(CENTRE,)), **ONSITE_TEXT)
    assert (candidate.fact_id, candidate.requirement_source, candidate.comparison) == (
        "onsite_required:onsite",
        "text",
        "met",
    )
    assert candidate.binding_capable is True and candidate.severity == "hard"


def test_onsite_is_unmet_when_onsite_is_refused_or_every_location_is_beyond_1_5_radii() -> None:
    remote_only = scope(work_modes=frozenset({"remote"}), locations=(CENTRE,))
    assert onsite(facts(locations=(km_east(10),)), remote_only, **ONSITE_TEXT).comparison == "unmet"
    assert 1.5 < distance_km(CENTRE.lat, CENTRE.lon, *latlon(km_east(35))) / CENTRE.radius_km
    far = facts(locations=(km_east(35), km_east(60)))
    assert onsite(far, scope(locations=(CENTRE,)), **ONSITE_TEXT).comparison == "unmet"
    near_and_far = facts(locations=(km_east(28), km_east(60)))  # 1.4 x the radius is the on-site band 2
    assert onsite(near_and_far, scope(locations=(CENTRE,)), **ONSITE_TEXT).comparison == "met"


def test_onsite_is_uncomparable_with_an_unknown_location_band_or_no_scope_location() -> None:
    where = scope(locations=(CENTRE,))
    assert (
        onsite(facts(locations=(km_east(35), UNRESOLVED)), where, **ONSITE_TEXT).comparison == "uncomparable"
    )
    across_bands = facts(locations=(km_east(10, 35),))  # one location, candidates in bands 4 and 0
    assert onsite(across_bands, where, **ONSITE_TEXT).comparison == "uncomparable"
    assert onsite(facts(locations=()), where, **ONSITE_TEXT).comparison == "uncomparable"
    assert onsite(facts(locations=(km_east(35),)), scope(), **ONSITE_TEXT).comparison == "uncomparable"


def test_a_structured_onsite_mode_is_a_structured_requirement_and_never_binding_capable() -> None:
    det = facts(locations=(km_east(60),), **STRUCTURED_ONSITE)
    for job in (job_facts(), job_facts(**ONSITE_TEXT)):  # the structured mode outranks the asserted fact
        candidate = only(candidates(job, det, where=scope(locations=(CENTRE,))))
        assert (candidate.requirement_source, candidate.comparison, candidate.binding_capable) == (
            "structured",
            "unmet",
            False,
        )
    text_derived = facts(
        locations=(km_east(60),), work_mode_possible=frozenset({"onsite"}), work_mode_source="text"
    )
    assert (
        candidates(det=text_derived) == []
    )  # a mode derived from the ad's text is no structured requirement


# -- security_clearance, work_permit, degree, certification, contract_type -----------------------------------


def clearance(level: str | None) -> JobFacts:
    return job_facts(security_clearance=SecurityClearance(required=True, level=level, evidence=["Ü2"]))


def held(*levels: str | None) -> ConfirmedProfile:
    return profile(
        user_entered={"security_clearances": [{"name": "Sicherheitsüberprüfung", "level": x} for x in levels]}
    )


def test_security_clearance() -> None:
    assert only(candidates(clearance("Ü2"))).comparison == "unmet"  # no user-entered clearance
    assert only(candidates(clearance("Ü2"), person=held("ü2 "))).comparison == "met"  # folded equality
    assert only(candidates(clearance("Ü3"), person=held("Ü2", None))).comparison == "uncomparable"
    assert only(candidates(clearance(None), person=held("Ü2"))).comparison == "met"  # no level to compare
    assert only(candidates(clearance("Ü2"))).fact_id == "security_clearance:clearance"


@pytest.mark.parametrize(
    ("authorization", "expected"),
    [("requires_sponsorship", "unmet"), ("unspecified", "uncomparable"), ("authorized", "met")],
)
def test_work_permit(authorization: str, expected: str) -> None:
    job = job_facts(work_permit=WorkPermit(required=True, evidence=["Arbeitserlaubnis"]))
    person = profile(user_entered={"work_authorization_de": authorization})
    found = only(candidates(job, person=person))
    assert (found.fact_id, found.comparison, found.severity) == ("work_permit:work_permit", expected, "hard")


def degree(level: str, *, mandatory: bool = True, or_equivalent: bool = False) -> JobFacts:
    return job_facts(
        degree={
            "level": level,
            "mandatory": mandatory,
            "or_equivalent": or_equivalent,
            "field": None,
            "evidence": ["x"],
        }
    )


def educated(*levels: str) -> ConfirmedProfile:
    return profile(
        education=[{"level": x, "field": "Informatik", "institution": None, "year": None} for x in levels]
    )


@pytest.mark.parametrize(
    ("required", "levels", "expected"),
    [
        ("master", ("bachelor",), "unmet"),
        ("master", ("master",), "met"),
        ("master", ("vocational", "phd"), "met"),  # the highest ordered degree
        ("vocational", (), "unmet"),  # no education is `none`
        ("bachelor", ("other",), "uncomparable"),  # only `other` levels
        ("master", ("other", "bachelor"), "unmet"),  # the highest ordered degree is bachelor
        ("unspecified", ("master",), "uncomparable"),
    ],
)
def test_degree(required: str, levels: tuple[str, ...], expected: str) -> None:
    found = only(candidates(degree(required), person=educated(*levels)))
    assert (found.fact_id, found.comparison, found.severity) == ("degree:degree", expected, "soft")


def test_a_degree_with_an_equivalent_or_none_required_or_preferred_is_no_requirement() -> None:
    assert candidates(degree("master", or_equivalent=True)) == []
    assert candidates(degree("none_required")) == []
    assert candidates(degree("master", mandatory=False)) == []


def test_token_set_similarity_is_the_jaccard_index_of_the_folded_word_sets() -> None:
    assert (
        token_set_similarity("AWS Certified Solutions Architect", "aws  certified solutions ARCHITECT") == 1.0
    )
    assert token_set_similarity("Zertifizierter Scrum-Master", "zertifizierter scrum master") == 1.0
    assert token_set_similarity("AWS Solutions Architect", "AWS Certified Solutions Architect") == 0.75
    assert token_set_similarity("", "") == 0.0


def test_certification() -> None:
    job = job_facts(
        certifications=[
            Certification(
                name="AWS Certified Solutions Architect Associate", mandatory=True, evidence=["AWS"]
            ),
            Certification(name="Certified Kubernetes Administrator", mandatory=True, evidence=["CKA"]),
            Certification(name="ITIL Foundation", mandatory=False, evidence=["ITIL"]),
        ]
    )
    person = profile(
        certifications=[
            {"name": "AWS Certified Solutions Architect \N{EN DASH} Associate", "issuer": None, "year": 2024},
            {"name": "Kubernetes Administrator", "issuer": None, "year": 2023},  # 2 of 3 words: 0.67
        ]
    )
    assert [(c.subject, c.comparison, c.severity) for c in candidates(job, person=person)] == [
        ("aws certified solutions architect associate", "met", "soft"),
        ("certified kubernetes administrator", "unmet", "soft"),
    ]


def test_contract_type_is_the_filters_domain_and_never_binding_capable() -> None:
    structured = facts(employment_types=frozenset({"freelance"}), employment_source="structured")
    where = scope(employment_types=frozenset({"full_time", "part_time"}))
    found = only(candidates(det=structured, where=where))
    assert (
        found.code,
        found.requirement_source,
        found.comparison,
        found.binding_capable,
        found.severity,
    ) == (
        "contract_type",
        "structured",
        "unmet",
        False,
        "hard",
    )
    both = replace(structured, employment_types=frozenset({"freelance", "full_time"}))
    assert only(candidates(det=both, where=where)).comparison == "met"
    text = replace(structured, employment_source="text")
    assert candidates(det=text, where=where) == []


# -- review need, binding and the required facts -------------------------------------------------------------


def test_a_text_requirement_needs_review_when_unmet_or_uncomparable_only() -> None:
    job = job_facts(
        required_languages=[
            required_language("de", level="C1"),  # B2 in the profile: unmet
            required_language("en", level="unspecified"),  # B1: uncomparable
            required_language("fr", level="A2"),  # B1: met
        ]
    )
    person = profile(languages=[language("de", "B2"), language("en", "B1"), language("fr", "B1")])
    assert [(c.subject, c.comparison, c.binding_capable) for c in candidates(job, person=person)] == [
        ("de", "unmet", True),
        ("en", "uncomparable", True),
        ("fr", "met", False),
    ]


def test_uncomparable_never_binds_and_rejected_drops() -> None:
    c = KnockoutCandidate(
        code="mandatory_language",
        fact_id="mandatory_language:de",
        subject="de",
        requirement_source="text",
        comparison="uncomparable",
        binding_capable=True,
        severity="hard",
        inputs={},
    )
    assert binding(c, "critic_confirmed") is False
    assert binding(replace(c, comparison="unmet"), "critic_rejected") is False
    assert binding(replace(c, comparison="unmet"), "critic_confirmed") is True


@pytest.mark.parametrize(
    ("confirmation", "binds"),
    [
        ("structured", True),
        ("critic_confirmed", True),
        ("critic_rejected", False),
        ("uncertain", False),
        ("unconfirmed", False),
    ],
)
def test_only_an_unmet_structured_or_confirmed_requirement_binds(confirmation: Any, binds: bool) -> None:
    c = only(candidates(degree("master"), person=educated("bachelor")))
    assert binding(c, confirmation) is binds
    assert binding(replace(c, comparison="met"), confirmation) is False


def test_required_facts_are_the_binding_capable_ones_and_the_ad_flag_in_fact_id_order() -> None:
    job = job_facts(
        **ONSITE_TEXT,
        required_languages=[required_language("en", level="C1"), required_language("de", level="A1")],
        work_permit=WorkPermit(required=True, evidence=["Arbeitserlaubnis"]),
        degree=Degree(level="master", mandatory=True, or_equivalent=False, field=None, evidence=["Master"]),
    )
    person = profile(
        languages=[language("de", "C1")], education=[], user_entered={"work_authorization_de": "authorized"}
    )
    det = facts(employment_types=frozenset({"freelance"}), employment_source="structured")
    found = derive_knockouts(job, det, person, scope(employment_types=frozenset({"full_time"})))
    assert {c.fact_id: c.comparison for c in found} == {
        "onsite_required:onsite": "uncomparable",  # no scope location
        "mandatory_language:en": "unmet",
        "mandatory_language:de": "met",
        "work_permit:work_permit": "met",
        "degree:degree": "unmet",
        "contract_type:employment_types": "unmet",  # structured: no review
    }
    expected = ["degree:degree", "mandatory_language:en", "onsite_required:onsite"]
    assert [ref.fact_id for ref in required_facts(job, found, not_a_job_ad=False)] == expected
    with_flag = required_facts(job, found, not_a_job_ad=True)
    assert [ref.fact_id for ref in with_flag] == [*expected[:2], "not_a_job_ad:ad", expected[2]]
    assert with_flag[2] == FactRef("not_a_job_ad:ad", "not_a_job_ad", "ad", "text")
    assert required_facts(job_facts(), [], not_a_job_ad=False) == []


def test_a_binding_capable_candidate_must_name_a_fact_of_the_draft() -> None:
    found = candidates(job_facts(**ONSITE_TEXT))
    with pytest.raises(ValueError, match="onsite_required:onsite"):
        required_facts(job_facts(), found, not_a_job_ad=False)


def test_inputs_hold_ids_and_enums_never_ad_text() -> None:
    job = job_facts(
        **ONSITE_TEXT,
        required_languages=[required_language("other", level="B2", other="Klingonisch")],
        security_clearance=SecurityClearance(required=True, level="Geheimschutzstufe Omega", evidence=["x"]),
        certifications=[Certification(name="Zertifikat Sternenflotte", mandatory=True, evidence=["x"])],
    )
    found = candidates(
        job, facts(locations=(km_east(10),)), held("Geheimschutzstufe Alpha"), scope(locations=(CENTRE,))
    )
    assert len(found) == 4
    text = json.dumps([c.inputs for c in found])
    for ad_text in (
        "Klingon",
        "klingon",
        "Omega",
        "omega",
        "Sternenflotte",
        "sternenflotte",
        "Erfundenes",
        "Alpha",
    ):
        assert ad_text not in text


def latlon(location: Any) -> tuple[float, float]:
    return location.candidates[0].lat, location.candidates[0].lon
