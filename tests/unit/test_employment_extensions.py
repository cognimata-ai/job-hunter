from datetime import UTC, datetime

import pytest
from jobhunter.sources.parsing.dates import ParsedDate
from jobhunter.sources.parsing.employment import (
    JSEARCH_TYPES,
    EmploymentType,
    adzuna_employment,
    parse_employment,
)
from jobhunter.sources.parsing.extensions import Extensions, parse_extensions

REF = datetime(2026, 10, 16, 9, 47, tzinfo=UTC)


def test_employment_strings() -> None:
    assert parse_employment("Vollzeit und Teilzeit") == ["full_time", "part_time"]
    assert parse_employment("Praktikum") == ["internship"]
    assert adzuna_employment("full_time", "permanent") == ["full_time"]
    assert adzuna_employment(None, "contract") == ["contract"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Vollzeit", ["full_time"]),
        ("Full-time", ["full_time"]),
        ("full time", ["full_time"]),
        ("FULLTIME", ["full_time"]),
        ("Teilzeit", ["part_time"]),
        ("Part-time", ["part_time"]),
        ("part time", ["part_time"]),
        ("Vertrag", ["contract"]),
        ("Contractor", ["contract"]),
        ("Praktikum", ["internship"]),
        ("Pflichtpraktikum", ["internship"]),
        ("Internship", ["internship"]),
        ("Werkstudent", ["working_student"]),
        ("Werkstudentin (m/w/d)", ["working_student"]),
        ("Working student", ["working_student"]),
        ("Ausbildung", ["apprenticeship"]),
        ("Apprenticeship", ["apprenticeship"]),
        ("befristet", ["temporary"]),
        ("Befristete Stelle", ["temporary"]),
        ("Temporary", ["temporary"]),
        ("Freelance", ["freelance"]),
        ("Freiberuflich", ["freelance"]),
        ("Freiberufler", ["freelance"]),
        ("Teilzeit, Vollzeit", ["part_time", "full_time"]),
        ("Vollzeit / Vollzeit / Full-time", ["full_time"]),
        ("Full-time, Part-time & Internship", ["full_time", "part_time", "internship"]),
        ("Werkstudent oder Praktikum", ["working_student", "internship"]),
    ],
)
def test_parse_employment(text, expected) -> None:
    assert parse_employment(text) == expected


@pytest.mark.parametrize(
    "text", ["", "Homeoffice", "unbefristet", "Unbefristete Festanstellung", "Berufserfahrung", "intern"]
)
def test_parse_employment_ignores_other_text(text) -> None:
    assert parse_employment(text) == []


def test_jsearch_types() -> None:
    assert dict(JSEARCH_TYPES) == {
        "FULLTIME": "full_time",
        "PARTTIME": "part_time",
        "CONTRACTOR": "contract",
        "INTERN": "internship",
    }
    with pytest.raises(TypeError):
        JSEARCH_TYPES["TEMP"] = "temporary"  # type: ignore[index]


@pytest.mark.parametrize(
    ("contract_time", "contract_type", "expected"),
    [
        ("full_time", "permanent", ["full_time"]),
        ("part_time", "permanent", ["part_time"]),
        (None, "contract", ["contract"]),
        ("full_time", "contract", ["full_time", "contract"]),
        ("part_time", "contract", ["part_time", "contract"]),
        ("FULL_TIME", "Contract", ["full_time", "contract"]),
        (None, "permanent", []),
        (None, None, []),
        ("full_time", None, ["full_time"]),
        ("unknown", "other", []),
        ("", "", []),
    ],
)
def test_adzuna_employment(contract_time, contract_type, expected) -> None:
    assert adzuna_employment(contract_time, contract_type) == expected


def test_every_employment_type_is_reachable() -> None:
    reached: set[EmploymentType] = set()
    for text in (
        "Vollzeit",
        "Teilzeit",
        "Vertrag",
        "Praktikum",
        "Werkstudent",
        "Ausbildung",
        "befristet",
        "Freelance",
    ):
        reached.update(parse_employment(text))
    assert reached == {
        "full_time",
        "part_time",
        "contract",
        "temporary",
        "internship",
        "working_student",
        "apprenticeship",
        "freelance",
    }


def test_serpapi_extensions_bilingual() -> None:
    ref = datetime(2026, 10, 16, 9, 47, tzinfo=UTC)
    e = parse_extensions(
        ["vor 4 Tagen", "Vollzeit", "Homeoffice", "60.000 € bis 80.000 € pro Jahr"], None, reference=ref
    )
    assert e.posted is not None and e.salary is not None
    assert (e.posted.precision, e.work_modes, e.employment, e.salary.max) == (
        "relative_day",
        frozenset({"remote", "hybrid"}),
        ("full_time",),
        80000,
    )
    d = parse_extensions(
        ["3 days ago", "Full-time"],
        {"posted_at": "3 days ago", "schedule_type": "Full-time", "work_from_home": True},
        reference=ref,
    )
    assert d.work_modes == frozenset({"remote", "hybrid"})
    assert d.employment == ("full_time",)


def test_a_german_chip_list_resolves_against_the_reference_in_berlin() -> None:
    e = parse_extensions(["vor 4 Tagen", "Vollzeit"], None, reference=REF)
    assert e.posted == ParsedDate(
        at=datetime(2026, 10, 11, 22, 0, tzinfo=UTC), precision="relative_day", raw="vor 4 Tagen"
    )
    hours = parse_extensions(["vor 15 Stunden"], None, reference=REF)
    assert hours.posted is not None and hours.posted.precision == "hour"
    assert hours.posted.at == datetime(2026, 10, 15, 18, 47, tzinfo=UTC)


def test_english_chips() -> None:
    e = parse_extensions(
        ["2 days ago", "Work from home", "Part-time", "Contractor", "$80,000 a year"], None, reference=REF
    )
    assert e.posted is not None and e.posted.at == datetime(2026, 10, 13, 22, 0, tzinfo=UTC)
    assert e.work_modes == frozenset({"remote", "hybrid"})
    assert e.employment == ("part_time", "contract")
    assert e.salary is not None and e.salary.currency == "USD" and e.salary.annual_eur_min is None


@pytest.mark.parametrize(
    "chip",
    ["Homeoffice", "Home Office", "home-office", "Work from home", "WORK FROM HOME", "Remote", "100% Remote"],
)
def test_remote_chips_give_remote_and_hybrid(chip) -> None:
    assert parse_extensions([chip], None, reference=REF).work_modes == frozenset({"remote", "hybrid"})


@pytest.mark.parametrize(
    "chip", ["Vollzeit", "Hybrid", "Vor Ort", "Remotely sensed", "Krankenversicherung", "vor 4 Tagen"]
)
def test_other_chips_give_no_work_mode(chip) -> None:
    assert parse_extensions([chip], None, reference=REF).work_modes is None


def test_nothing_recognised_is_all_unknown() -> None:
    e = parse_extensions(["Krankenversicherung", "Zahnversicherung", "30 Tage Urlaub"], None, reference=REF)
    assert e == Extensions(posted=None, work_modes=None, employment=(), salary=None)
    assert parse_extensions([], None, reference=REF) == e
    assert parse_extensions([], {}, reference=REF) == e


def test_detected_extensions_alone() -> None:
    e = parse_extensions(
        [],
        {
            "posted_at": "vor 2 Tagen",
            "schedule_type": "Teilzeit",
            "work_from_home": True,
            "salary": "5.000 € pro Monat",
            "health_insurance": True,
        },
        reference=REF,
    )
    assert e.posted is not None and e.posted.at == datetime(2026, 10, 13, 22, 0, tzinfo=UTC)
    assert e.work_modes == frozenset({"remote", "hybrid"})
    assert e.employment == ("part_time",)
    assert e.salary is not None and e.salary.annual_eur_min == 60000


def test_chips_and_detected_extensions_are_merged_without_duplicates() -> None:
    e = parse_extensions(
        ["Vollzeit", "Praktikum", "Full-time"],
        {"schedule_type": "Full-time", "salary": "60K EUR a year"},
        reference=REF,
    )
    assert e.employment == ("full_time", "internship")
    assert e.salary is not None and e.salary.min == 60000


def test_detected_values_take_precedence_over_chips() -> None:
    e = parse_extensions(
        ["vor 4 Tagen", "30.000 € pro Jahr"],
        {"posted_at": "3 days ago", "salary": "40.000 € pro Jahr"},
        reference=REF,
    )
    assert e.posted is not None and e.posted.raw == "3 days ago"
    assert e.salary is not None and e.salary.min == 40000


@pytest.mark.parametrize(
    "detected",
    [
        {"posted_at": 5, "schedule_type": ["Vollzeit"], "work_from_home": "yes", "salary": None},
        {"posted_at": None, "schedule_type": None, "work_from_home": False, "salary": 60000},
        {
            "posted_at": "irgendwann",
            "schedule_type": "",
            "work_from_home": 1,
            "salary": "Gehalt nach Vereinbarung",
        },
    ],
)
def test_malformed_detected_values_are_ignored(detected) -> None:
    assert parse_extensions([], detected, reference=REF) == Extensions(None, None, (), None)


def test_work_from_home_false_is_unknown_not_on_site() -> None:
    assert parse_extensions([], {"work_from_home": False}, reference=REF).work_modes is None


def test_the_first_recognised_date_and_salary_win() -> None:
    e = parse_extensions(
        ["vor 4 Tagen", "Gestern", "50.000 € pro Jahr", "70.000 € pro Jahr"], None, reference=REF
    )
    assert e.posted is not None and e.posted.raw == "vor 4 Tagen"
    assert e.salary is not None and e.salary.min == 50000


def test_extensions_is_frozen() -> None:
    e = parse_extensions([], None, reference=REF)
    with pytest.raises(AttributeError):
        e.posted = None  # type: ignore[misc]
