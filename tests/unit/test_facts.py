"""Job facts and their stable fact ids (spec §11.4, L4157-4174; M3 Task 3).

Every asserted fact that can bind gets the id `code:subject`, derived from the draft alone: an asserted
`onsite_only`, each mandatory language, a mandatory degree, each mandatory certification, a required
clearance and a required work permit. Names are folded with `normalize.text.fold` (NFKD with the marks
removed, so `ö` is `o`, never `oe`). Two facts with the same id keep the first.
"""

from typing import get_args

from jobhunter.llm.schemas.generator import (
    Certification,
    Degree,
    JobFacts,
    OnsiteOnly,
    SecurityClearance,
    WorkPermit,
)
from jobhunter.scoring.facts import FactCode, FactRef, derive_fact_ids, fact_id

from tests.support.scoring import job_facts, required_language


def every_fact_asserted() -> JobFacts:
    return job_facts(
        onsite_only=OnsiteOnly(asserted=True, evidence=["Arbeit ausschließlich vor Ort"]),
        required_languages=[
            required_language("de", level="C1", evidence=["Deutsch verhandlungssicher"]),
            required_language("other", level="B2", other="Französisch", evidence=["Französisch fließend"]),
        ],
        degree=Degree(
            level="master", mandatory=True, or_equivalent=False, field="Informatik", evidence=["Master"]
        ),
        certifications=[
            Certification(name="Zertifizierter Scrum Master", mandatory=True, evidence=["Scrum-Zertifikat"])
        ],
        security_clearance=SecurityClearance(required=True, level="Ü2", evidence=["Sicherheitsüberprüfung"]),
        work_permit=WorkPermit(required=True, evidence=["Arbeitserlaubnis für Deutschland"]),
    )


def test_the_fact_codes_are_the_job_facts_and_the_top_level_flag() -> None:
    assert get_args(FactCode) == (*JobFacts.model_fields, "not_a_job_ad")  # the §11.4 table, in its order


def test_a_fact_id_is_code_colon_subject() -> None:
    assert fact_id("mandatory_language", "de") == "mandatory_language:de"
    assert fact_id("not_a_job_ad", "ad") == "not_a_job_ad:ad"


def test_every_asserted_fact_gets_the_id_of_the_table() -> None:
    refs = derive_fact_ids(every_fact_asserted())
    assert list(refs) == [
        "onsite_required:onsite",
        "mandatory_language:de",
        "mandatory_language:other:franzosisch",
        "degree:degree",
        "certification:zertifizierter scrum master",
        "security_clearance:clearance",
        "work_permit:work_permit",
    ]
    assert all(ref.fact_id == key and ref.source == "text" for key, ref in refs.items())
    assert refs["mandatory_language:other:franzosisch"] == FactRef(
        fact_id="mandatory_language:other:franzosisch",
        code="mandatory_language",
        subject="other:franzosisch",
        source="text",
    )
    assert refs["onsite_required:onsite"] == FactRef(
        "onsite_required:onsite", "onsite_required", "onsite", "text"
    )


def test_a_fact_that_does_not_bind_gets_no_id() -> None:
    facts = job_facts(
        onsite_only=OnsiteOnly(asserted=False, evidence=[]),
        required_languages=[required_language("en", level="B2", mandatory=False)],
        degree=Degree(
            level="bachelor", mandatory=False, or_equivalent=False, field=None, evidence=["Bachelor"]
        ),
        certifications=[Certification(name="AWS Solutions Architect", mandatory=False, evidence=["AWS"])],
        security_clearance=SecurityClearance(required=False, level=None, evidence=[]),
        work_permit=WorkPermit(required=False, evidence=[]),
    )
    assert derive_fact_ids(facts) == {}


def test_names_are_folded_by_fold_not_transliterated() -> None:
    facts = job_facts(
        required_languages=[required_language("other", level="B1", other="  Französisch ")],
        certifications=[Certification(name="Größe-Zertifikat", mandatory=True, evidence=["Zertifikat"])],
    )
    assert list(derive_fact_ids(facts)) == [
        "mandatory_language:other:franzosisch",  # ö -> o (Codex r2 B25), trimmed
        "certification:grosse-zertifikat",  # ß -> ss
    ]


def test_two_facts_with_one_id_keep_the_first() -> None:
    facts = job_facts(
        required_languages=[
            required_language("de", level="C1", evidence=["Deutsch C1"]),
            required_language("de", level="B1", evidence=["Deutsch B1"]),
            required_language("other", level="B2", other="Französisch"),
            required_language("other", level="C1", other="FRANZÖSISCH"),
        ],
        certifications=[
            Certification(name="Scrum Master", mandatory=True, evidence=["first"]),
            Certification(name="scrum  master", mandatory=True, evidence=["second"]),
        ],
    )
    assert list(derive_fact_ids(facts)) == [
        "mandatory_language:de",
        "mandatory_language:other:franzosisch",
        "certification:scrum master",
    ]
