"""Contact stripping (spec §12.3, M2 Task 10): every rule of the table, the order the categories claim
text in, the edit set (`restore`, `remove_span`, `apply_edits`) and `scan_value`.

Every person and datum below is invented: Max Mustermann, Erika Musterfrau, example.org domains and phone
numbers in the ranges that regulators reserve for fiction (BNetzA 030 23125-xxx, 089 99998-xxx and
040 66969-xxx; Ofcom 020 7946 0xxx and 07700 900xxx; NANP 555-01xx). The exceptions are the brief's pin
(`Tel. +49 30` / `1234 5678`) and digit strings that only test a boundary (`0171 123`, `010670049www.Max`).
"""

import importlib
import re
import subprocess
import sys
import time
from dataclasses import replace
from typing import Any, get_args

import pytest
from jobhunter.profile import contacts
from jobhunter.profile.contacts import (
    APPLY_ORDER,
    REPLACEMENT,
    ROLE_KEYWORDS,
    Category,
    Edits,
    Replacement,
    StripResult,
    apply_edits,
    remove_span,
    restore,
    scan_value,
    strip_contacts,
)

CV = (
    "Max Mustermann\nSenior Backend Engineer\n\n"
    "Erfahrung\n| Firma | Kontakt |\n| Beispiel GmbH | max.mustermann@example.org |\n"
    "Tel. +49 30\n1234 5678\nMusterstraße 12, 10115 Berlin\nGeburtsdatum: 01.02.1990\n"
    "Familienstand: ledig\n\nMit freundlichen Grüßen\nmax mustermann\n"
)
CATEGORIES: tuple[Category, ...] = get_args(Category)

_sre_parse = importlib.import_module("re._parser")
_MAXREPEAT = importlib.import_module("re._constants").MAXREPEAT


def repeat_bounds(source: str, flags: int = 0) -> list[tuple[int, int]]:
    """The (min, max) of every repeat in a pattern, from the regular expression parser."""
    found: list[tuple[int, int]] = []

    def walk(node: Any) -> None:
        for op, av in node:
            name = str(op)
            if name in {"MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"}:
                found.append((av[0], av[1]))
                walk(av[2])
            elif name == "SUBPATTERN":
                walk(av[3])
            elif name == "BRANCH":
                for alternative in av[1]:
                    walk(alternative)
            elif name in {"ASSERT", "ASSERT_NOT"}:
                walk(av[1])
            elif name == "ATOMIC_GROUP":
                walk(av)
            elif name == "GROUPREF_EXISTS":
                for part in av[1:]:
                    if part is not None:
                        walk(part)

    walk(_sre_parse.parse(source, flags))
    return found


def test_contacts_in_footer_table_and_signature_are_stripped() -> None:
    r = strip_contacts(CV)
    for value in (
        "max.mustermann@example.org",
        "1234 5678",
        "Musterstraße 12",
        "10115 Berlin",
        "01.02.1990",
        "ledig",
        "Mustermann",
    ):
        assert value.casefold() not in r.stripped_text.casefold(), value
    assert dict(r.report) == {"email": 1, "phone": 1, "address": 2, "personal": 2, "name": 2, "url": 0}
    assert "Senior Backend Engineer" in r.stripped_text  # a role line is not a name


def test_the_pin_text_after_stripping_is_exact() -> None:
    assert strip_contacts(CV).stripped_text == (
        "[NAME]\nSenior Backend Engineer\n\n"
        "Erfahrung\n| Firma | Kontakt |\n| Beispiel GmbH | [EMAIL] |\n"
        "Tel. [PHONE]\n[ADDRESS], [ADDRESS]\n[PERSONAL]\n"
        "[PERSONAL]\n\nMit freundlichen Grüßen\n[NAME]\n"
    )


def found(original: str, result: StripResult) -> list[tuple[str, str]]:
    """(category, the original text it replaced) in document order."""
    return [(r.category, original[r.start : r.end]) for r in result.replacements]


def test_offsets_refer_to_the_original_text_and_the_replacements_are_ordered() -> None:
    r = strip_contacts(CV)
    assert found(CV, r) == [
        ("name", "Max Mustermann"),
        ("email", "max.mustermann@example.org"),
        ("phone", "+49 30\n1234 5678"),
        ("address", "Musterstraße 12"),
        ("address", "10115 Berlin"),
        ("personal", "Geburtsdatum: 01.02.1990"),
        ("personal", "Familienstand: ledig"),
        ("name", "max mustermann"),
    ]
    ends = [0, *(x for rep in r.replacements for x in (rep.start, rep.end))]
    assert ends == sorted(ends), "sorted and not overlapping"
    assert isinstance(r.replacements, tuple)


def test_the_report_holds_a_count_for_every_category_and_never_a_value() -> None:
    r = strip_contacts(CV)
    assert set(r.report) == set(CATEGORIES)
    assert all(type(count) is int for count in r.report.values())
    assert dict(strip_contacts("").report) == dict.fromkeys(CATEGORIES, 0)
    with pytest.raises(TypeError):
        r.report["email"] = 5  # type: ignore[index]


def test_the_replacement_tokens_are_the_spec_tokens() -> None:
    assert dict(REPLACEMENT) == {
        "email": "[EMAIL]",
        "phone": "[PHONE]",
        "url": "[URL]",
        "address": "[ADDRESS]",
        "personal": "[PERSONAL]",
        "name": "[NAME]",
    }


def test_the_categories_are_applied_in_one_documented_order() -> None:
    assert sorted(APPLY_ORDER) == sorted(CATEGORIES)
    # A birth line claims a date that is also phone-shaped; an e-mail address in a URL stays in the URL.
    assert APPLY_ORDER.index("personal") < APPLY_ORDER.index("phone")
    assert APPLY_ORDER.index("url") < APPLY_ORDER.index("email")


def test_empty_and_plain_texts_are_unchanged() -> None:
    r = strip_contacts("")
    assert (r.stripped_text, r.replacements) == ("", ())
    plain = (
        "Entwicklung von Python-Diensten.\nKubernetes, Docker, PostgreSQL.\n2019 - 2022 bei Beispiel GmbH\n"
    )
    r = strip_contacts(plain)
    assert r.stripped_text == plain
    assert r.replacements == ()


@pytest.mark.parametrize(
    "value",
    [
        "max.mustermann@example.org",
        "erika_musterfrau+cv@mail.example.org",
        "m@x.de",
        "e.musterfrau@uni-musterstadt.example.org",
        "max%mustermann@example.org",
        "ÄrztinMuster@example.org",
    ],
)
@pytest.mark.parametrize("template", ["Mail: {}", "<{}>", "({}).", "E-Mail:{}, Tel", "mailto:{}"])
def test_an_email_address_is_replaced(value: str, template: str) -> None:
    text = template.format(value)
    r = strip_contacts(text)
    assert value not in r.stripped_text
    assert r.report["email"] == 1
    assert r.stripped_text == template.format("[EMAIL]")


@pytest.mark.parametrize(
    "text", ["user@localhost", "a @ b.org", "@example.org", "example.org", "max@", "a@b.c"]
)
def test_text_that_is_no_email_address_stays(text: str) -> None:
    assert strip_contacts(text).stripped_text == text


@pytest.mark.parametrize(
    "value",
    [
        "+49 30 23125456",
        "+49 30 2312 5456",
        "+49 (0)30 23125456",
        "+49 (0) 30 / 23125-456",
        "+49-30-23125456",
        "0049 30 23125456",
        "0049-30-23125456",
        "030 23125456",
        "030/23125456",
        "030-23125456",
        "030.23125456",
        "(030) 23125456",
        "030 23125 456",
        "089 99998-123",
        "040 / 66969 123",
        "030 2312545",
        "03023125456",
        "+44 20 7946 0958",
        "+44 7700 900123",
        "+1 202 555 0143",
        "+1 (202) 555-0143",
        "+44 20 7946 01 23",
        "(0)30 23125456",
        "0 30 / 231 254 56",
        "0 30 231 25456",
        "+49\xa030\xa023125456",
        "030 \u2013 23125456",
        "+49 30 23125\u202f456",
    ],
)
def test_a_phone_number_is_replaced(value: str) -> None:
    for template in ("Tel: {}", "{}", "Mobil {}, Mail", "({})"):
        r = strip_contacts(template.format(value))
        assert r.stripped_text == template.format("[PHONE]"), template
        assert r.report["phone"] == 1


def test_a_phone_number_spans_one_line_break_and_no_more() -> None:
    assert strip_contacts("Tel. +49 30\n1234 5678").stripped_text == "Tel. [PHONE]"
    assert strip_contacts("Tel. +49 30 \r\n 1234 5678").stripped_text == "Tel. [PHONE]"
    assert strip_contacts("Tel. 030 2312\n5456 Berlin").stripped_text == "Tel. [PHONE] Berlin"
    # A blank line ends it; a number that is long enough does not take in the next line.
    blank = "Tel. +49 30\n\n1234 5678"
    assert strip_contacts(blank).stripped_text == blank
    assert strip_contacts("Tel. 030 23125456\n2019 Erfahrung").stripped_text == "Tel. [PHONE]\n2019 Erfahrung"
    assert (
        strip_contacts("Tel. 030 23125456\n\n2019 Erfahrung").stripped_text
        == "Tel. [PHONE]\n\n2019 Erfahrung"
    )


@pytest.mark.parametrize(
    "text",
    [
        "2019 - 2021",
        "01.2019 - 03.2021",
        "03.2021",
        "Seit 01.02.1990 dabei",
        "31.12.1999 - 01.01.2000",
        "2019-03-01",
        "Version 3.12.4",
        "Gehalt: 65.000 - 75.000 EUR",
        "0,5 FTE",
        "1234567",
        "+49 30",
        "0171 123",
        "Postleitzahl 01067",
        "Kennzahl 0815",
        "0-306-40615-2",
        "ISBN 0-306-40615-2",
        "0 - 2019 - 2022",
        "0 1 2 3 4 5 6 7",
        "0 5 9 12 34 56 78",
    ],
)
def test_digits_that_are_no_phone_number_stay(text: str) -> None:
    assert strip_contacts(text).stripped_text == text


@pytest.mark.parametrize(
    "value",
    [
        "https://example.org/cv",
        "http://example.org",
        "https://www.example.org/ueber-mich?lang=de#top",
        "www.example.org",
        "www.example.org/ueber-mich",
        "linkedin.com/in/max-mustermann",
        "www.linkedin.com/in/max-mustermann/",
        "https://de.linkedin.com/in/max-mustermann-12345",
        "xing.com/profile/Max_Mustermann",
        "www.xing.com/profile/Max_Mustermann2",
        "github.com/maxmustermann",
        "github.com/maxmustermann/projekt",
    ],
)
def test_a_url_is_replaced_and_trailing_punctuation_stays(value: str) -> None:
    for template in ("{}", "Siehe {}.", "Web: {}, Mail", "({})"):
        r = strip_contacts(template.format(value))
        assert r.stripped_text == template.format("[URL]"), template
        assert r.report["url"] == 1


@pytest.mark.parametrize(
    "text",
    [
        "example.org",
        "node.js und e.g. foo.bar",
        "Microsoft.NET",
        "github.com",
        "linkedin.com/company/example",
        "awww.example.org",
    ],
)
def test_text_that_is_no_url_of_the_table_stays(text: str) -> None:
    assert strip_contacts(text).stripped_text == text


def test_an_email_address_inside_a_url_belongs_to_the_url() -> None:
    r = strip_contacts("Kontakt: https://example.org/kontakt?mail=max.mustermann@example.org.")
    assert r.stripped_text == "Kontakt: [URL]."
    assert (r.report["url"], r.report["email"]) == (1, 0)
    assert strip_contacts("https://max.mustermann@example.org/x").stripped_text == "[URL]"


def test_an_email_address_on_a_www_host_is_an_email_address() -> None:
    r = strip_contacts("max.mustermann@www.example.org")
    assert r.stripped_text == "[EMAIL]"
    assert (r.report["email"], r.report["url"]) == (1, 0)


@pytest.mark.parametrize(
    "value",
    [
        "Musterstraße 12",
        "Musterstrasse 12",
        "Hauptstr. 5",
        "Hauptstr.5",
        "Waldweg 3a",
        "Berliner Allee 14-16",
        "Karl-Marx-Straße 7",
        "Musterplatz 1",
        "Münchener Straße 7",
        "Beispielgasse 9",
        "Rheinufer 3",
        "Kaiserdamm 20",
        "Königsring 5",
        "Alter Weg 4/2",
        "10115 Berlin",
        "01067 Dresden",
        "80331 München",
        "20095 Hamburg-Mitte",
        "50667 Köln",
        "Potsdamer Platz 1",
        "Pariser Platz 3",
        "Stadtring 12",
        "Kaiserring 3",
        "Hansaring 5",
        "Hansering 12",
        "Kaiser-Wilhelm-Ring 3",
        "Hohenzollernring 48",
        "Ostring 7",
        "Mittlerer Ring 2",
    ],
)
def test_a_street_address_or_postal_code_with_a_city_is_replaced(value: str) -> None:
    for template in ("Adresse: {}", "{}, Deutschland", "{}\n"):
        r = strip_contacts(template.format(value))
        assert r.stripped_text == template.format("[ADDRESS]"), template
        assert r.report["address"] == 1


def test_a_street_and_a_postal_code_are_two_addresses() -> None:
    r = strip_contacts("Musterstraße 12, 10115 Berlin")
    assert r.stripped_text == "[ADDRESS], [ADDRESS]"
    assert r.report["address"] == 2


@pytest.mark.parametrize(
    "text",
    [
        "Platz 1 beim Wettbewerb",
        "Auf Platz 1 im Ranking",
        "Spring 5 und Spring Boot 3",
        "Die String 2 Klasse",
        "Monitoring 4 Dashboards",
        "Erster Platz 1 im Ranking",
        "Zweiter Platz 2",
        "Arbeitsplatz 4.0",
        "Manufacturing 4.0 Projekt",
        "Clustering 3",
        "Rendering 2",
        "Knowledge Sharing 3",
        "Catering 5",
        "Docstring 2",
        "Bildungsweg 2",
        "Ausweg 3",
        "Hiring 2",
        "Engineering 2020-2022",
        "Software Engineering",
        "Software Engineering 2",
        "Musterstraße",  # no house number
        "123456 Berlin",  # a postal code that is part of a longer number
        "1234567 Berlin",
        "A10115 Berlin",
        "10115 berlin",
        "2020510115Berlin",
        "12345 users",
        "1234 Berlin",
    ],
)
def test_text_that_is_no_address_stays(text: str) -> None:
    assert strip_contacts(text).stripped_text == text


@pytest.mark.parametrize(
    "line",
    [
        "Geburtsdatum: 01.02.1990",
        "Geburtsdatum 01.02.1990",
        "Geburtsort: Musterstadt",
        "Geboren am 01.02.1990 in Musterstadt",
        "Max Mustermann, geb. 01.02.1990 in Musterstadt",
        "Date of birth: 1 Feb 1990",
        "Place of birth: Musterstadt",
        "DOB: 01/02/1990",
        "Familienstand: ledig",
        "Marital status: single",
        "Staatsangehörigkeit: deutsch",
        "Nationalität: deutsch",
        "Nationality: German",
        "Konfession: evangelisch",
        "Religion: none",
        "Geschlecht: männlich",
        "Gender: female",
        "Kinder: 2",
        "Children: 2",
        "- Familienstand: ledig",
        "| Familienstand | ledig |",
        "\u2022 Nationalität deutsch",
        "Familienstand ledig",
        "Kinder | 2",
        "Kinder\t2",
    ],
)
def test_a_labelled_personal_datum_is_replaced_with_the_rest_of_the_line(line: str) -> None:
    text = f"Berufserfahrung\n{line}\nSkills: Python\n"
    r = strip_contacts(text)
    assert r.stripped_text.startswith("Berufserfahrung\n"), line
    assert r.stripped_text.endswith("]\nSkills: Python\n"), line
    assert r.report["personal"] == 1, line
    assert r.stripped_text.count("[") == 1, line
    # The whole of the line from the label on is gone (a leading bullet or pipe is not part of the label).
    assert "ledig" not in r.stripped_text
    assert "1990" not in r.stripped_text


def test_a_label_in_the_middle_of_a_line_takes_the_rest_of_that_line() -> None:
    r = strip_contacts("Max, Familienstand: ledig, Nationalität: deutsch\nSkills: Python")
    assert r.stripped_text == "Max, [PERSONAL]\nSkills: Python"
    assert r.report["personal"] == 1


def test_a_birth_date_is_personal_and_not_also_a_phone_number() -> None:
    for line in (
        "Geburtsdatum: 01.02.1990",
        "Geburtsdatum: 01021990",
        "geboren: 01 02 1990",
        "DOB 01-02-1990",
    ):
        r = strip_contacts(line)
        assert r.stripped_text == "[PERSONAL]", line
        assert (r.report["personal"], r.report["phone"]) == (1, 0), line
    # Whatever stands in the rest of the line belongs to the label: nothing is counted twice.
    r = strip_contacts("Geboren 01.02.1990 in Musterstadt, Tel. 030 23125456, max@example.org")
    assert r.stripped_text == "[PERSONAL]"
    assert dict(r.report) == {**dict.fromkeys(CATEGORIES, 0), "personal": 1}


@pytest.mark.parametrize(
    "text",
    [
        "Kinder- und Jugendarbeit",
        "Kinder und Jugendliche betreut",
        "Children's hospital volunteering",
        "Gender Pay Gap analysis",
        "Betreuung von Kindern: ehrenamtlich",
        "Ich bin 1990 geboren.",
        "Ihre Nationalität ist kein Kriterium",
        "Kinder:",
        "Geburtsdatum:",
        "Nationality-neutral hiring",
        "Gender-neutral language",
        "Religion-based hiring",
    ],
)
def test_words_that_only_look_like_a_label_stay(text: str) -> None:
    assert strip_contacts(text).stripped_text == text


BIRTH_LABELS = ["Geburtsdatum", "Geburtsort", "Date of birth", "Place of birth", "DOB", "geboren", "geb."]
PROTECTED_LABELS = [
    "Familienstand",
    "Marital status",
    "Staatsangehörigkeit",
    "Nationalität",
    "Nationality",
    "Konfession",
    "Religion",
    "Geschlecht",
    "Gender",
    "Kinder",
    "Children",
]


@pytest.mark.parametrize("label", [*BIRTH_LABELS, *PROTECTED_LABELS])
@pytest.mark.parametrize("separator", ["", ":", " :", " | ", "\t"])
def test_a_label_cell_with_its_value_cell_on_the_next_line_is_one_personal_replacement(
    label: str, separator: str
) -> None:
    # `docparse` writes every table cell on a line of its own: `Geburtsdatum` / `01.02.1990`.
    text = f"Berufserfahrung\n{label}{separator}\n01.02.1990\nSkills: Python\n"
    r = strip_contacts(text)
    assert r.stripped_text == "Berufserfahrung\n[PERSONAL]\nSkills: Python\n", (label, separator)
    assert r.report["personal"] == 1
    assert dict(r.report) == {**dict.fromkeys(CATEGORIES, 0), "personal": 1}
    assert found(text, r) == [("personal", f"{label}{separator}\n01.02.1990")]


def test_a_personal_data_table_in_cell_order_is_stripped_row_by_row() -> None:
    text = (
        "Persönliche Daten\nGeburtsdatum\n01.02.1990\nGeburtsort\nMusterstadt\nFamilienstand\nledig\n"
        "Staatsangehörigkeit\ndeutsch\nKinder\n2\nBerufserfahrung\n2019 - 2022\n"
    )
    r = strip_contacts(text)
    assert r.stripped_text == "Persönliche Daten\n" + "[PERSONAL]\n" * 5 + "Berufserfahrung\n2019 - 2022\n"
    assert (r.report["personal"], r.report["name"], r.report["phone"]) == (5, 0, 0)
    for i in range(5):
        assert restore(text, r, i).stripped_text.count("[PERSONAL]") == 4
    everything = r
    for i in range(5):
        everything = restore(text, everything, i)
    assert everything.stripped_text == text


def test_the_value_cell_may_follow_after_blank_lines_but_not_after_three() -> None:
    assert strip_contacts("Familienstand:\n\nledig\nSkills").stripped_text == "[PERSONAL]\nSkills"
    assert strip_contacts("Familienstand:\n \n\t\nledig\nSkills").stripped_text == "[PERSONAL]\nSkills"
    far = "Familienstand:\n\n\n\nledig\nSkills"
    assert strip_contacts(far).stripped_text == far  # three blank lines: the label stays alone
    assert strip_contacts("Familienstand:\r\n\r\nledig\r\nSkills").stripped_text == "[PERSONAL]\r\nSkills"


def test_only_the_single_next_non_empty_line_is_taken() -> None:
    assert strip_contacts("Geburtsdatum\n01.02.1990\nBerlin\n").stripped_text == "[PERSONAL]\nBerlin\n"
    # A label that has a value on its line does not take the next line.
    r = strip_contacts("Familienstand: ledig\nBerlin\nSkills\n")
    assert r.stripped_text == "[PERSONAL]\nBerlin\nSkills\n"
    r = strip_contacts("Geburtsdatum: 01.02.1990\nGeburtsort\nMusterstadt\n")
    assert r.stripped_text == "[PERSONAL]\n[PERSONAL]\n"
    assert r.report["personal"] == 2


def test_a_label_followed_by_another_label_line_is_not_given_that_line_as_its_value() -> None:
    text = "Geburtsdatum\nFamilienstand\nledig\nSkills\n"
    r = strip_contacts(text)
    assert r.stripped_text == "Geburtsdatum\n[PERSONAL]\nSkills\n"
    assert r.report["personal"] == 1
    assert strip_contacts(r.stripped_text).stripped_text == r.stripped_text
    # The same for a label with a value on the next line and a line that is already a token.
    assert strip_contacts("Geburtsdatum\n[PERSONAL]\n").stripped_text == "Geburtsdatum\n[PERSONAL]\n"


@pytest.mark.parametrize(
    "text",
    [
        "Geburtsdatum",  # nothing follows
        "Geburtsdatum:\n",
        "Ich bin 1990 geboren.\nIch arbeite bei Beispiel GmbH\n",  # not a label line
        "Max, geb.\n01.02.1990\n",  # a label in the middle of a line has no value cell
        "Kinder und Jugendliche\n2019 - 2022\n",  # a look-alike is no label
        "Children's hospital\nVolunteer\n",
        "Skills\nGender Pay Gap\nAnalysis\n",
    ],
)
def test_a_label_without_a_value_cell_or_a_label_that_is_none_takes_no_next_line(text: str) -> None:
    assert strip_contacts(text).stripped_text == text


def test_a_cell_per_line_value_is_taken_whatever_it_looks_like() -> None:
    for value in (
        "max@example.org",
        "+49 30 23125456",
        "https://example.org",
        "Musterstraße 12",
        "10115 Berlin",
    ):
        r = strip_contacts(f"Geburtsort\n{value}\nSkills\n")
        assert r.stripped_text == "[PERSONAL]\nSkills\n", value
        assert dict(r.report) == {**dict.fromkeys(CATEGORIES, 0), "personal": 1}, value


def test_a_label_inside_a_url_does_not_start_a_personal_line() -> None:
    r = strip_contacts("Blog: https://example.org/geboren-in-berlin und mehr")
    assert r.stripped_text == "Blog: [URL] und mehr"


@pytest.mark.parametrize(
    ("text", "stripped"),
    [
        ("Max Mustermann\nSkills: Python\n", "[NAME]\nSkills: Python\n"),
        ("  Max Mustermann  \nSkills", "  [NAME]  \nSkills"),
        ("MAX MUSTERMANN\nSkills", "[NAME]\nSkills"),
        ("Dr. Erika Musterfrau\nSkills", "[NAME]\nSkills"),
        ("Dr.-Ing. Max Mustermann\nSkills", "[NAME]\nSkills"),
        ("Prof. Dr. med. Erika Musterfrau\nSkills", "[NAME]\nSkills"),
        ("Max J. Mustermann M.Sc.\nSkills", "[NAME]\nSkills"),
        ("Anna-Lena Müller-Schmidt\nSkills", "[NAME]\nSkills"),
        ("Max von Mustermann\nSkills", "[NAME]\nSkills"),
        ("Zoë Åberg O'Neil\nSkills", "[NAME]\nSkills"),
        ("Max Jonas Peter Mustermann\nSkills", "[NAME]\nSkills"),
        ("\n\nMax Mustermann\nSkills", "\n\n[NAME]\nSkills"),
        ("Max Mustermann", "[NAME]"),
        ("Max Kaufmann\nSkills", "[NAME]\nSkills"),  # a surname that is also a title word
        ("Erika Leiter\nSkills", "[NAME]\nSkills"),
        ("Persönliche Daten\nName\nMax Mustermann\nSkills", "Persönliche Daten\nName\n[NAME]\nSkills"),
        ("Personal details\nName:\nMax Mustermann\nSkills", "Personal details\nName:\n[NAME]\nSkills"),
        ("Name: Max Mustermann\nSkills", "Name: [NAME]\nSkills"),
        ("Vorname/Nachname: Erika Musterfrau\nSkills", "Vorname/Nachname: [NAME]\nSkills"),
        ("Name : Dr. Max Mustermann\nSkills", "Name : [NAME]\nSkills"),
        ("Leiter Controlling\nMax Mustermann\nSkills", "Leiter Controlling\n[NAME]\nSkills"),
        ("Leiterin Finanzen\nErika Musterfrau\nSkills", "Leiterin Finanzen\n[NAME]\nSkills"),
        (
            "Kaufmann für Büromanagement\nMax Mustermann\nSkills",
            "Kaufmann für Büromanagement\n[NAME]\nSkills",
        ),
        (
            "Kauffrau für Büromanagement\nErika Musterfrau\nSkills",
            "Kauffrau für Büromanagement\n[NAME]\nSkills",
        ),
        ("Architektin\nErika Musterfrau\nSkills", "Architektin\n[NAME]\nSkills"),
        ("Softwareentwicklerin\nErika Musterfrau\nSkills", "Softwareentwicklerin\n[NAME]\nSkills"),
    ],
)
def test_the_header_name_is_replaced(text: str, stripped: str) -> None:
    r = strip_contacts(text)
    assert r.stripped_text == stripped
    assert r.report["name"] == 1


@pytest.mark.parametrize(
    "text",
    [
        "Senior Backend Engineer\nBerlin\n",
        "Curriculum Vitae\nBerufserfahrung\n",
        "Lebenslauf\n\nBerufserfahrung\n2019 - 2022 Entwickler\n",
        "Max\nBerufserfahrung\n",  # one token
        "Max Jonas Peter Hans Mustermann\nSkills",  # five tokens
        "max mustermann\nSkills",  # not capitalised
        "Max Mustermann 2\nSkills",  # a digit
        "Max Mustermann, Berlin\nSkills",  # punctuation inside the line
        "Berufserfahrung\n\nMax Mustermann\n",  # not the header
        "Persönliche Daten\nBerlin\n",
        "Entwicklung von Web.\nSkills",  # a period ends a sentence, not a name
        "Leitung der Abteilung\nSkills",
        "Dr. Müller\nSkills",  # one name word
        "Projektleiter Softwareentwicklung\n",
        "Backend Developer\n",
        "[NAME]\nBerlin Germany\n",
        "Industriekaufmann Hamburg\nSkills",  # a title word in a compound is a title
        "Teamleiter Entwicklung\nSkills",
        "Name: berlin\nSkills",  # a name label whose value is no name
        "Name\nSkills\n",
    ],
)
def test_text_without_a_header_name_has_no_name_replaced(text: str) -> None:
    r = strip_contacts(text)
    assert r.report["name"] == 0
    assert r.stripped_text == text


def test_a_role_line_is_not_a_name_but_the_name_after_it_is() -> None:
    text = "Senior Backend Engineer\nMax Mustermann\nBerlin\nIch bin Max Mustermann.\n"
    r = strip_contacts(text)
    assert r.stripped_text == "Senior Backend Engineer\n[NAME]\nBerlin\nIch bin [NAME].\n"
    text = "Lebenslauf\nCurriculum Vitae\nProfil\nErika Musterfrau\nSkills\n"
    assert strip_contacts(text).stripped_text == "Lebenslauf\nCurriculum Vitae\nProfil\n[NAME]\nSkills\n"
    # At most three heading lines are skipped.
    text = "Lebenslauf\nCurriculum Vitae\nProfil\nResume\nErika Musterfrau\n"
    assert strip_contacts(text).report["name"] == 0


def test_later_occurrences_of_the_full_name_are_replaced_case_insensitively() -> None:
    text = (
        "Anna-Lena Müller-Schmidt\nSkills\n"
        "Referenz: ANNA-LENA MÜLLER-SCHMIDT, später anna-lena müller-schmidt und Anna-Lena  Müller-Schmidt.\n"
        "Frau Müller-Schmidt blieb übrig, Anna-Lena Müller-Schmidtke auch, ebenso Anna-Lena allein.\n"
    )
    r = strip_contacts(text)
    assert r.stripped_text == (
        "[NAME]\nSkills\n"
        "Referenz: [NAME], später [NAME] und [NAME].\n"
        "Frau Müller-Schmidt blieb übrig, Anna-Lena Müller-Schmidtke auch, ebenso Anna-Lena allein.\n"
    )
    assert r.report["name"] == 4


def test_a_title_in_the_header_is_not_needed_for_a_later_occurrence() -> None:
    r = strip_contacts("Dr. Erika Musterfrau\nSkills\nMit Grüßen\nErika Musterfrau\n")
    assert r.stripped_text == "[NAME]\nSkills\nMit Grüßen\n[NAME]\n"


def test_a_name_inside_a_claimed_span_is_not_claimed_again() -> None:
    text = (
        "Max Mustermann\nKontakt: Geboren 01.02.1990 in Musterstadt, Max Mustermann\n"
        "max.mustermann@example.org\n"
    )
    r = strip_contacts(text)
    assert r.stripped_text == "[NAME]\nKontakt: [PERSONAL]\n[EMAIL]\n"
    assert (r.report["name"], r.report["personal"], r.report["email"]) == (1, 1, 1)


def test_role_keywords_is_one_documented_lowercase_frozenset() -> None:
    assert isinstance(ROLE_KEYWORDS, frozenset)
    assert all(k == k.casefold() and k.strip() == k and k for k in ROLE_KEYWORDS)
    required = {"lebenslauf", "curriculum", "vitae", "resume", "cv", "profil", "profile"}
    titles = {"engineer", "developer", "entwickler", "manager", "berater", "consultant", "ingenieur"}
    titles |= {"architekt", "analyst", "designer", "leiter", "senior", "junior", "lead", "head"}
    assert required | titles <= ROLE_KEYWORDS
    assert contacts.__doc__ is not None
    assert "ROLE_KEYWORDS" in contacts.__doc__


def test_scan_value_finds_each_category_once_in_the_category_order() -> None:
    assert scan_value("max.mustermann@example.org") == ["email"]
    assert scan_value("+49 30 23125456") == ["phone"]
    assert scan_value("https://example.org/cv") == ["url"]
    assert scan_value("Musterstraße 12") == ["address"]
    assert scan_value("10115 Berlin") == ["address"]
    assert scan_value("Geburtsdatum: 01.02.1990") == ["personal"]
    assert scan_value("Familienstand: ledig") == ["personal"]
    everything = (
        "Geburtsdatum: 01.02.1990\nMusterstraße 12\nhttps://example.org\n"
        "max@example.org\n+49 30 23125456\nMusterstraße 14\nFamilienstand: ledig"
    )
    assert scan_value(everything) == ["email", "phone", "url", "address", "personal"]


def test_scan_value_has_no_name_rule_and_no_false_alarm_on_profile_values() -> None:
    assert scan_value("Max Mustermann") == []
    assert scan_value("") == []
    for value in (
        "Senior Backend Engineer",
        "Python, Docker, PostgreSQL",
        "01.2019 - 03.2021",
        "2019-03-01",
        "Led a team of 5 engineers; shipped v3.12.4",
        "[EMAIL] [PHONE] [URL] [ADDRESS] [PERSONAL] [NAME] [REMOVED]",
    ):
        assert scan_value(value) == [], value


def test_replacement_tokens_match_no_rule() -> None:
    tokens = "\n".join(f"{t} {t}" for t in (*REPLACEMENT.values(), "[REMOVED]"))
    assert strip_contacts(tokens).stripped_text == tokens


def test_stripping_the_pin_text_twice_changes_nothing() -> None:
    once = strip_contacts(CV)
    assert strip_contacts(once.stripped_text).stripped_text == once.stripped_text
    assert strip_contacts(once.stripped_text).replacements == ()


def test_a_claim_that_ends_in_a_word_does_not_hide_what_follows_it() -> None:
    # The phone number ends in the middle of a word, so a first look cannot see the `www.` or the label that
    # follows it; a second `strip_contacts` would find them. The rounds of `_detect` make one pass enough.
    assert strip_contacts("010670049www.Max").stripped_text == "[PHONE][URL]"
    text = "max.mustermann@example.org12erika@mail.example.org"
    assert strip_contacts(text).stripped_text == "[EMAIL][EMAIL]"
    glued = strip_contacts("01710049Geburtsdatum:03010115")
    assert glued.stripped_text == "[PHONE][PERSONAL]"
    assert (glued.report["phone"], glued.report["personal"]) == (1, 1)
    for text in ("010670049www.Max", "01710049Geburtsdatum:03010115", "Familienstand|Geburtsdatum[EMAIL]"):
        once = strip_contacts(text).stripped_text
        assert strip_contacts(once).stripped_text == once


def test_a_text_with_windows_line_breaks_is_stripped_as_well() -> None:
    crlf = CV.replace("\n", "\r\n")
    r = strip_contacts(crlf)
    assert dict(r.report) == dict(strip_contacts(CV).report)
    assert r.stripped_text == strip_contacts(CV).stripped_text.replace("\n", "\r\n")


def test_the_module_is_pure_and_imports_nothing_of_the_application() -> None:
    """No database, no logging, no I/O layer: the module reads only the standard library."""
    code = (
        "import sys, jobhunter.profile.contacts\n"
        "own = {'jobhunter', 'jobhunter.profile', 'jobhunter.profile.contacts'}\n"
        "app = sorted(m for m in sys.modules if m.startswith('jobhunter') and m not in own)\n"
        "heavy = sorted(m for m in sys.modules if m.split('.')[0] in {'sqlalchemy', 'psycopg'})\n"
        "assert not app + heavy, (app, heavy)\n"
    )
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


def test_the_name_label_value_is_the_header_name_for_later_occurrences() -> None:
    text = "Persönliche Daten\nName\nMax Mustermann\nSkills\nGruß, MAX MUSTERMANN\n"
    r = strip_contacts(text)
    assert r.stripped_text == "Persönliche Daten\nName\n[NAME]\nSkills\nGruß, [NAME]\n"
    assert r.report["name"] == 2
    text = "Name: Erika Musterfrau\nSkills\nerika musterfrau\n"
    assert strip_contacts(text).stripped_text == "Name: [NAME]\nSkills\n[NAME]\n"
    for text in ("Name: Max Mustermann\nSkills", "Persönliche Daten\nName\nMax Mustermann\nSkills"):
        once = strip_contacts(text).stripped_text
        assert strip_contacts(once).stripped_text == once


def test_every_compiled_pattern_is_bounded() -> None:
    """No `*`, `+` or `{n,}` in any pattern of the module (the M1 F1 lesson): the allow-list is empty."""
    unbounded_allowed: set[str] = set()  # patterns by name that may hold an unbounded repeat; none does
    patterns = {n: v for n, v in vars(contacts).items() if isinstance(v, re.Pattern)}
    patterns["_name_pattern"] = contacts._name_pattern(["Max", "von", "Mustermann"])
    assert len(patterns) >= 15
    for name, pattern in patterns.items():
        if name in unbounded_allowed:
            continue
        unbounded = [b for b in repeat_bounds(pattern.pattern, pattern.flags) if b[1] == _MAXREPEAT]
        assert not unbounded, (name, pattern.pattern)


def test_the_bound_check_sees_an_unbounded_repeat() -> None:
    for source in ("a*", "a+", "a{2,}", "(?:ab|c*)d", "(?<=x)y[0-9]+", "(?=a+)b", "a{1,3}?b+?"):
        assert any(b[1] == _MAXREPEAT for b in repeat_bounds(source)), source
    for source in ("a?", "a{0,5}", "[ \\t]{0,200}(?:b|c){1,3}", "(?!x{1,2})y", "\\*\\+[*+]"):
        assert not any(b[1] == _MAXREPEAT for b in repeat_bounds(source)), source


@pytest.mark.parametrize(
    "label",
    [
        "Geburtsdatum/-ort",
        "Geburtsdatum und -ort",
        "Geburtsdatum (TT.MM.JJJJ)",
        "Familienstand / Kinder",
        "Staatsangehörigkeit / Nationality",
        "Geburtsdatum / Geburtsort",
        "Date of birth (DD/MM/YYYY)",
        "Geburtsdatum, Geburtsort:",
        "Marital status / Children:",
    ],
)
def test_a_compound_label_cell_takes_the_value_cell_below_it(label: str) -> None:
    text = f"Berufserfahrung\n{label}\n01.02.1990 in Musterstadt\nSkills: Python\n"
    r = strip_contacts(text)
    assert r.stripped_text == "Berufserfahrung\n[PERSONAL]\nSkills: Python\n", label
    assert r.report["personal"] == 1
    assert "1990" not in r.stripped_text
    assert "Musterstadt" not in r.stripped_text


def test_a_label_with_words_of_its_own_after_it_has_a_value_and_takes_no_line_below() -> None:
    r = strip_contacts("Religion und Ethik unterrichtet\nBerlin\n")
    assert r.stripped_text == "[PERSONAL]\nBerlin\n"
    r = strip_contacts("Familienstand: ledig (seit 2019)\nBerlin\n")
    assert r.stripped_text == "[PERSONAL]\nBerlin\n"


def test_a_phone_number_with_a_leading_zero_in_its_own_group_is_found_and_dates_are_not() -> None:
    for value in (
        "(0)30 23125456",
        "0 30 / 231 254 56",
        "0 30 231 25456",
        "0 89 99998 123",
        "(0) 30 23125456",
    ):
        r = strip_contacts(f"Tel: {value}.")
        assert r.stripped_text == "Tel: [PHONE].", value
    for text in ("0.5 FTE", "0 / 2019", "Stand 0 3 2019", "01.02.1990", "0 5 9"):
        assert strip_contacts(text).stripped_text == text, text


def test_a_year_range_on_the_next_line_is_no_part_of_a_short_number() -> None:
    for text in (
        "Projekt 0815\n2019 - 2022 Senior Engineer\n",
        "Projekt 0815\n1999 bis 2004 Entwickler\n",
        "Tel. 030 231\n2022 - heute\n",
    ):
        assert strip_contacts(text).stripped_text == text, text
    # Digits that are no year still join.
    assert strip_contacts("Tel. 030 231\n25456").stripped_text == "Tel. [PHONE]"
    assert strip_contacts("Tel. 030 23\n1254 56").stripped_text == "Tel. [PHONE]"


def test_a_house_number_is_not_the_start_of_a_version_or_a_decimal() -> None:
    for text in ("Arbeitsplatz 4.0", "Musterweg 4,5 Stunden", "Hauptstraße 5.1 Kapitel"):
        assert strip_contacts(text).stripped_text == text, text
    assert strip_contacts("Hauptstraße 5. Etage").stripped_text == "[ADDRESS]. Etage"
    assert strip_contacts("Hauptstraße 5, 10115 Berlin").stripped_text == "[ADDRESS], [ADDRESS]"


def test_an_english_ring_word_of_the_list_is_no_street_and_every_other_ring_word_is() -> None:
    english = (
        "Clustering Rendering Sharing Catering Hiring Scoring Manufacturing Engineering Monitoring".split()
    )
    english += "Mentoring Tutoring Refactoring Offering Gathering Ordering Filtering Wiring Tiering".split()
    english += "Recovering Delivering Steering Covering Docstring Spring String".split()
    for word in english:
        for form in (word, word.lower(), word.upper()):  # the whole word, in any case
            assert strip_contacts(f"{form} 3").stripped_text == f"{form} 3", form
        assert strip_contacts(f"Cloud-{word} 3").stripped_text == f"Cloud-{word} 3", (
            word
        )  # the last part decides
    # A German street that ends in `ring` is stripped whatever letter stands before it; so is a word of the
    # list with something in front of it (the list is of whole words, and stripping is the safe default).
    for street in (
        "Königsring",
        "Stadtring",
        "Kaiserring",
        "Hauptring",
        "Gürtelring",
        "Hansaring",
        "Hansering",
    ):
        assert strip_contacts(f"{street} 3").stripped_text == "[ADDRESS]", street
    for street in (
        "Ostring",
        "Hohenzollernring",
        "Kaiser-Wilhelm-Ring",
        "Luisenring",
        "Ottoring",
        "Maiering",
    ):
        assert strip_contacts(f"{street} 3").stripped_text == "[ADDRESS]", street
    assert strip_contacts("Mittlerer Ring 2").stripped_text == "[ADDRESS]"
    assert strip_contacts("Hauptclustering 3").stripped_text == "[ADDRESS]"


def test_a_pass_over_a_text_longer_than_the_cap_leaves_no_half_token() -> None:
    cut = "Familienstand: " + "x" * 190 + " max@example.org"
    assert strip_contacts(cut).stripped_text == "[PERSONAL]"
    cell = "Geburtsdatum\n" + "x" * 190 + " max@example.org\nSkills"
    assert strip_contacts(cell).stripped_text == "[PERSONAL]\nSkills"
    # A cut at a blank leaves the rest to the other categories.
    exact = "Familienstand: " + "x" * 198 + " max@example.org"
    assert strip_contacts(exact).stripped_text == "[PERSONAL] [EMAIL]"
    # A token longer than any bound is cut, and what is left of it is a fragment, never a whole address.
    huge = strip_contacts("Familienstand: " + "x" * 190 + " " + "y" * 500 + "@example.org").stripped_text
    assert huge.startswith("[PERSONAL]y") and huge.endswith("y[EMAIL]")


@pytest.mark.parametrize(
    "text",
    [
        "geb." * 310,  # all label words: no value, nothing to strip, and a second pass agrees
        ("geb." + "1" * 97)
        * 12,  # a cut lands exactly on the next label, which a second pass reads after a token
        ("Religion:" + "1" * 399) * 4,
        ("geboren " + "1" * 394) * 4,
        "geb. 1 " * 400,
        "Kinder: 2 " * 200,
        "x " + "Gender: 1" * 200,
        "DOB 1 " * 500,
    ],
)
def test_a_long_run_of_labels_is_stripped_in_one_pass(text: str) -> None:
    once = strip_contacts(text).stripped_text
    assert strip_contacts(once).stripped_text == once
    if any(c.isdigit() for c in text):  # a value after the label: something is stripped
        assert "[PERSONAL]" in once


def test_a_label_that_starts_where_the_cut_of_the_previous_one_ended_is_a_label() -> None:
    text = ("geb." + "1" * 97) * 12
    once = strip_contacts(text).stripped_text
    assert once == "[PERSONAL]" * 3  # three cuts of 404 characters, each one a label of its own


def test_restoring_a_personal_false_positive_keeps_the_contacts_inside_it_replaced() -> None:
    text = "Religion und Ethik, Kontakt: max@example.org, Tel. 030 23125456\nSkills\n"
    base = strip_contacts(text)
    assert base.stripped_text == "[PERSONAL]\nSkills\n"
    back = restore(text, base, 0)
    assert back.stripped_text == "Religion und Ethik, Kontakt: [EMAIL], Tel. [PHONE]\nSkills\n"
    assert (back.report["email"], back.report["phone"], back.report["personal"]) == (1, 1, 0)
    for value in ("max@example.org", "030 23125456"):
        assert value not in back.stripped_text
    # The contacts inside are replacements of their own: restored by their span, never by an index.
    email = next(r for r in back.replacements if r.category == "email")
    assert text[email.start : email.end] == "max@example.org"
    only_phone = apply_edits(
        text, replace(back.edits, restored=back.edits.restored | {(email.start, email.end)})
    )
    assert only_phone.stripped_text == "Religion und Ethik, Kontakt: max@example.org, Tel. [PHONE]\nSkills\n"
    assert restore(text, back, 0) == back  # index 0 of the fresh strip is the personal span again
    # The value cell below a lone label.
    cell = "Kinder\nmax@example.org\nSkills\n"
    assert strip_contacts(cell).stripped_text == "[PERSONAL]\nSkills\n"
    assert restore(cell, strip_contacts(cell), 0).stripped_text == "Kinder\n[EMAIL]\nSkills\n"
    # Every category applies inside it, the header name too.
    mixed = "Max Mustermann\nReligion: Musterstraße 12, 10115 Berlin, www.example.org, Max Mustermann\n"
    base = strip_contacts(mixed)
    assert base.stripped_text == "[NAME]\n[PERSONAL]\n"
    back = restore(mixed, base, 1)
    assert back.stripped_text == "[NAME]\nReligion: [ADDRESS], [ADDRESS], [URL], [NAME]\n"
    assert dict(back.report) == {**dict.fromkeys(CATEGORIES, 0), "name": 2, "address": 2, "url": 1}


def test_a_restored_personal_span_and_a_removal_commute_and_the_edit_set_rebuilds_the_result() -> None:
    text = "Religion: max@example.org, Tel. 030 23125456\nSkills: Python\n"
    base = strip_contacts(text)
    start = text.index("Skills")
    a = remove_span(text, restore(text, base, 0), start, start + 6)
    b = restore(text, remove_span(text, base, start, start + 6), 0)
    assert a == b
    assert a.stripped_text == "Religion: [EMAIL], Tel. [PHONE]\n[REMOVED]: Python\n"
    assert apply_edits(text, a.edits) == a
    # The contacts inside a restored span are claims of their own: a removal over one of them merges with it.
    email = text.index("max@")
    c = remove_span(text, restore(text, base, 0), email + 2, email + 6)
    assert c.stripped_text == "Religion: [REMOVED], Tel. [PHONE]\nSkills: Python\n"


def test_restore_resolves_the_index_against_the_fresh_strip_after_a_prior_edit() -> None:
    base = strip_contacts(CV)
    start = CV.index("Beispiel GmbH")
    edited = remove_span(CV, base, start, start + 13)
    email = index_of(base, "email")
    assert restore(CV, edited, email).stripped_text == edited.stripped_text.replace(
        "[EMAIL]", "max.mustermann@example.org"
    )
    # Index 0 is the header name of the fresh strip, restored once; asking again names the same replacement,
    # not the first one that is left in `result.replacements` (which is now the e-mail address).
    once = restore(CV, base, 0)
    assert once.replacements[0].category == "email"
    assert restore(CV, once, 0) == once
    assert restore(CV, once, 0).stripped_text.count("Max Mustermann") == 1
    # The replacements of an edited result are not a list of indexes: the fresh strip's list is.
    assert [r.category for r in strip_contacts(CV).replacements] == [r.category for r in base.replacements]


BRACKET_VALUES = [
    ("Geburtsdatum (01.02.1990)\nFamilienstand: ledig\n", "[PERSONAL]\n[PERSONAL]\n"),
    ("Geburtsdatum (01.02.1990)", "[PERSONAL]"),
    ("Geburtsdatum (01.02.1990)\n", "[PERSONAL]\n"),
    ("Geburtsdatum (01.02.1990)\n\n\n\nSkills", "[PERSONAL]\n\n\n\nSkills"),
    ("Familienstand (ledig)\nGeburtsdatum: 01.02.1990", "[PERSONAL]\n[PERSONAL]"),
    ("Religion (evangelisch)\nFamilienstand: ledig", "[PERSONAL]\n[PERSONAL]"),
    ("Staatsangehörigkeit (deutsch)", "[PERSONAL]"),
    ("Staatsangehörigkeit (deutsch)\n\n\n\nSkills", "[PERSONAL]\n\n\n\nSkills"),
    ("Max, geboren (1990)\nSkills", "Max, [PERSONAL]\nSkills"),
    ("Max, Geburtsdatum: (01.02.1990)\nSkills", "Max, [PERSONAL]\nSkills"),
    ("Geburtsdatum (01.02.1990)\nSkills\n", "[PERSONAL]\nSkills\n"),  # the next line is no value cell
    ("Nationality (USA)\nSkills\n", "[PERSONAL]\nSkills\n"),  # letters that are no format mask
    ("Geschlecht (M/F)\nSkills\n", "[PERSONAL]\nSkills\n"),
    ("Familienstand (ledig)\nSkills\n", "[PERSONAL]\nSkills\n"),
]


@pytest.mark.parametrize(("text", "stripped"), BRACKET_VALUES)
def test_a_value_in_brackets_after_a_label_is_a_value_and_takes_no_line_below(
    text: str, stripped: str
) -> None:
    assert strip_contacts(text).stripped_text == stripped
    for secret in ("01.02.1990", "ledig", "evangelisch", "deutsch", "1990"):
        assert secret not in strip_contacts(text).stripped_text, secret


@pytest.mark.parametrize("mask", ["(TT.MM.JJJJ)", "(DD/MM/YYYY)", "(TT/MM/JJ)", "(JJJJ-MM-TT)", "(D.M.YYYY)"])
def test_only_a_format_mask_is_a_hint_and_the_cell_below_it_is_the_value(mask: str) -> None:
    assert strip_contacts(f"Geburtsdatum {mask}\n01.02.1990\nSkills").stripped_text == "[PERSONAL]\nSkills"
    # No cell below: the label line is claimed, as it was before the hint rule.
    assert strip_contacts(f"Geburtsdatum {mask}").stripped_text == "[PERSONAL]"
    assert (
        strip_contacts(f"Geburtsdatum {mask}\nFamilienstand: ledig").stripped_text == "[PERSONAL]\n[PERSONAL]"
    )
    assert strip_contacts(f"Geburtsdatum {mask}\n\n\n\nSkills").stripped_text == "[PERSONAL]\n\n\n\nSkills"


def test_a_label_of_label_words_without_a_value_cell_is_still_claimed() -> None:
    for text in ("Familienstand / Kinder", "Geburtsdatum/-ort", "Geburtsdatum und -ort", "Geburtsort-Datum"):
        assert strip_contacts(text).stripped_text == "[PERSONAL]", text
        assert strip_contacts(f"{text}\n\n\n\nSkills").stripped_text == "[PERSONAL]\n\n\n\nSkills", text
        assert (
            strip_contacts(f"{text}\nGeburtsdatum: 01.02.1990").stripped_text == "[PERSONAL]\n[PERSONAL]"
        ), text
    # A label that is alone on its line with nothing after it is still no hit.
    assert strip_contacts("Geburtsdatum\n\n\n\nSkills").stripped_text == "Geburtsdatum\n\n\n\nSkills"


@pytest.mark.parametrize(
    "label", ["Geburtsdatum-Ort", "Geburtsort-Datum", "GEBURTSDATUM-ORT", "geburtsort-datum"]
)
def test_a_label_joined_to_ort_or_datum_by_a_hyphen_is_a_label(label: str) -> None:
    for text in (f"{label}: 01.02.1990, Musterstadt", f"Max, {label}: 01.02.1990, Musterstadt"):
        r = strip_contacts(text)
        assert "1990" not in r.stripped_text and "Musterstadt" not in r.stripped_text, text
        assert r.report["personal"] == 1, text
    assert strip_contacts(f"{label}\n01.02.1990 in Musterstadt\nSkills").stripped_text == "[PERSONAL]\nSkills"


def test_a_title_word_that_is_also_a_surname_starts_a_title_line_and_ends_a_name() -> None:
    for title in ("Leiter Controlling", "Leiterin Finanzen", "Kaufmann für Büromanagement"):
        r = strip_contacts(f"{title}\nMax Mustermann\nSkills")
        assert r.stripped_text == f"{title}\n[NAME]\nSkills", title
        # The title is not turned into a name for later occurrences either.
        r = strip_contacts(f"{title}\nMax Mustermann\nSkills\n{title}\n")
        assert r.stripped_text == f"{title}\n[NAME]\nSkills\n{title}\n", title
    for name in ("Max Kaufmann", "Erika Leiter", "Erika Leiterin"):
        assert strip_contacts(f"{name}\nSkills").stripped_text == "[NAME]\nSkills", name


def test_spring_is_a_framework_unless_a_german_preposition_makes_it_a_street() -> None:
    for text in ("Spring 5", "Spring 5 und Spring Boot 3", "Mit Spring 5 gebaut", "Kenntnisse: Spring 5"):
        assert strip_contacts(text).stripped_text == text, text
    for text in ("Am Spring 5", "Im Spring 5", "Zum Spring 5", "Zur Spring 5", "am Spring 5"):
        r = strip_contacts(text)
        assert r.stripped_text == "[ADDRESS]", text
        assert r.report["address"] == 1
    assert (
        strip_contacts("Wohnhaft: Im Spring 5, 10115 Berlin").stripped_text
        == "Wohnhaft: [ADDRESS], [ADDRESS]"
    )
    assert strip_contacts("Im Spring 5").replacements == (Replacement("address", 0, 11),)


def test_a_restored_personal_span_that_is_one_contact_keeps_that_contact_replaced() -> None:
    for text, token in (
        ("dob@example.org", "[EMAIL]"),
        ("geb.a@b.de", "[EMAIL]"),
        ("DOB@example.org\n", "[EMAIL]\n"),
    ):
        base = strip_contacts(text)
        assert base.stripped_text.startswith("[PERSONAL]"), text
        assert restore(text, base, 0).stripped_text == token, text


def test_a_restored_personal_span_takes_the_contact_that_runs_past_its_end() -> None:
    text = "Kinder\n030 2312\n5456\nSkills\n"
    base = strip_contacts(text)
    assert base.stripped_text == "[PERSONAL]\n5456\nSkills\n"
    back = restore(text, base, 0)
    assert back.stripped_text == "Kinder\n[PHONE]\nSkills\n"
    assert (back.report["phone"], back.report["personal"]) == (1, 0)
    assert "2312" not in back.stripped_text and "5456" not in back.stripped_text
    # Commutation and the edit set.
    start = text.index("Skills")
    a = remove_span(text, back, start, start + 6)
    b = restore(text, remove_span(text, base, start, start + 6), 0)
    assert a == b and apply_edits(text, a.edits) == a


REGRESSION_CASES = [
    # (text, what must be gone, the category that must be counted)
    # round 1 (the rest of the label, a value cell below a label, compound labels)
    ("Geburtsdatum\n01.02.1990", "01.02.1990", "personal"),
    ("Familienstand\nledig", "ledig", "personal"),
    ("Geburtsdatum/-ort\n01.02.1990 in Musterstadt", "1990", "personal"),
    ("Geburtsdatum und -ort\n01.02.1990 in Musterstadt", "Musterstadt", "personal"),
    ("Geburtsdatum (TT.MM.JJJJ)\n01.02.1990", "1990", "personal"),
    ("Familienstand / Kinder\nledig, 2", "ledig", "personal"),
    ("Staatsangehörigkeit / Nationality\ndeutsch", "deutsch", "personal"),
    ("Kinder\nmax@example.org", "max@example.org", "personal"),
    ("Familienstand: " + "x" * 190 + " max@example.org", "mple.org", "personal"),
    # N1: a value in brackets is a value
    ("Geburtsdatum (01.02.1990)\nFamilienstand: ledig", "01.02.1990", "personal"),
    ("Geburtsdatum (01.02.1990)", "01.02.1990", "personal"),
    ("Geburtsdatum (01.02.1990)\n\n\n\nSkills", "01.02.1990", "personal"),
    ("Familienstand (ledig)\nGeburtsdatum: 01.02.1990", "ledig", "personal"),
    ("Religion (evangelisch)\nFamilienstand: ledig", "evangelisch", "personal"),
    ("Staatsangehörigkeit (deutsch)", "deutsch", "personal"),
    ("Max, geboren (1990)", "1990", "personal"),
    ("Max, Geburtsdatum: (01.02.1990)", "01.02.1990", "personal"),
    # N3: a label joined to ort or datum
    ("Geburtsdatum-Ort: 01.02.1990, Musterstadt", "Musterstadt", "personal"),
    ("Geburtsort-Datum: 01.02.1990, Musterstadt", "01.02.1990", "personal"),
    # N2: a title line above the name
    ("Leiter Controlling\nMax Mustermann", "Mustermann", "name"),
    ("Leiterin Finanzen\nErika Musterfrau", "Musterfrau", "name"),
    ("Kaufmann für Büromanagement\nMax Mustermann", "Mustermann", "name"),
    ("Max Kaufmann\nSkills", "Kaufmann", "name"),
    ("Erika Leiter\nSkills", "Leiter", "name"),
    ("Persönliche Daten\nName\nMax Mustermann", "Mustermann", "name"),
    ("Name: Max Mustermann", "Mustermann", "name"),
    ("Vorname/Nachname: Erika Musterfrau", "Musterfrau", "name"),
    # phones (round 1)
    ("Tel: (0)30 23125456", "23125456", "phone"),
    ("Tel: +49 (0)30 23125456", "23125456", "phone"),
    ("Tel: 0 30 / 231 254 56", "231 254 56", "phone"),
    ("Tel: 030 23125456", "23125456", "phone"),
    ("Tel. +49 30\n1234 5678", "1234 5678", "phone"),
    # addresses (rounds 1 and 2, the spring ruling)
    ("Potsdamer Platz 1", "Potsdamer", "address"),
    ("Pariser Platz 3", "Pariser", "address"),
    ("Hansaring 5", "Hansaring", "address"),
    ("Hansering 12", "Hansering", "address"),
    ("Kaiser-Wilhelm-Ring 3", "Wilhelm", "address"),
    ("Hohenzollernring 48", "Hohenzollernring", "address"),
    ("Ostring 7", "Ostring", "address"),
    ("Mittlerer Ring 2", "Mittlerer", "address"),
    ("Königsring 5", "Königsring", "address"),
    ("Am Spring 5", "Spring", "address"),
    ("Im Spring 5", "Spring", "address"),
    ("Zum Spring 5", "Spring", "address"),
    ("Musterstraße 12, 10115 Berlin", "Musterstraße", "address"),
]


@pytest.mark.parametrize(("text", "secret", "category"), REGRESSION_CASES)
def test_everything_the_reviews_named_as_stripped_is_stripped(text: str, secret: str, category: str) -> None:
    r = strip_contacts(text)
    assert secret not in r.stripped_text, r.stripped_text
    assert r.report[category] >= 1, dict(r.report)  # type: ignore[index]
    once = r.stripped_text
    assert strip_contacts(once).stripped_text == once


# --- the edit set --------------------------------------------------------------------------------


def index_of(result: StripResult, category: str, nth: int = 0) -> int:
    return [i for i, r in enumerate(result.replacements) if r.category == category][nth]


def test_strip_contacts_carries_an_empty_edit_set() -> None:
    assert strip_contacts(CV).edits == Edits()
    assert Edits().restored == frozenset()
    assert Edits().removed == frozenset()
    hash(Edits(restored=frozenset({(0, 1)}), removed=frozenset({(2, 3)})))  # frozen, so hashable


def test_restore_puts_one_replacement_back() -> None:
    base = strip_contacts(CV)
    email = restore(CV, base, index_of(base, "email"))
    assert email.stripped_text == base.stripped_text.replace("[EMAIL]", "max.mustermann@example.org")
    assert email.report["email"] == 0
    assert email.report["phone"] == 1
    assert all(r.category != "email" for r in email.replacements)
    assert len(email.replacements) == len(base.replacements) - 1
    # An address of two: the second one only.
    second = restore(CV, base, index_of(base, "address", 1))
    assert second.stripped_text == base.stripped_text.replace("[ADDRESS]\n", "10115 Berlin\n")
    assert second.report["address"] == 1
    # The input is untouched.
    assert base == strip_contacts(CV)


def test_restoring_every_replacement_gives_the_original_text() -> None:
    result = strip_contacts(CV)
    for i in range(len(result.replacements)):
        result = restore(CV, result, i)
    assert result.stripped_text == CV
    assert result.replacements == ()
    assert dict(result.report) == dict.fromkeys(CATEGORIES, 0)


def test_restore_refers_to_the_replacements_of_the_stripped_original_whatever_was_restored_before() -> None:
    base = strip_contacts(CV)
    name, phone = 0, index_of(base, "phone")
    a = restore(CV, restore(CV, base, name), phone)
    b = restore(CV, restore(CV, base, phone), name)
    assert a.stripped_text == b.stripped_text
    assert a.stripped_text == base.stripped_text.replace("[PHONE]", "+49 30\n1234 5678").replace(
        "[NAME]\nSenior", "Max Mustermann\nSenior"
    )
    assert a.edits == b.edits


def test_restore_rejects_an_index_that_names_no_replacement() -> None:
    base = strip_contacts(CV)
    for bad in (len(base.replacements), 99, -1):
        with pytest.raises(IndexError):
            restore(CV, base, bad)


def test_remove_span_marks_an_extra_span_removed_with_offsets_into_the_original() -> None:
    base = strip_contacts(CV)
    start = CV.index("Beispiel GmbH")
    r = remove_span(CV, base, start, start + len("Beispiel GmbH"))
    assert r.stripped_text == base.stripped_text.replace("Beispiel GmbH", "[REMOVED]")
    assert dict(r.report) == dict(base.report)  # the report counts the six categories only
    assert r.replacements == base.replacements
    assert r.edits.removed == frozenset({(start, start + 13)})
    # An offset into the stripped string would not do: the stripped text is shorter than the original here.
    assert len(base.stripped_text) < len(CV)


@pytest.mark.parametrize("start_end", [(-1, 3), (3, 3), (5, 3), (0, 10_000), (len(CV), len(CV) + 1)])
def test_remove_span_rejects_a_span_outside_the_original(start_end: tuple[int, int]) -> None:
    with pytest.raises(ValueError, match="span"):
        remove_span(CV, strip_contacts(CV), *start_end)


def test_a_removal_and_a_restore_give_the_same_text_whichever_comes_first() -> None:
    base = strip_contacts(CV)
    email = index_of(base, "email")
    start = CV.index("Beispiel GmbH")
    end = start + len("Beispiel GmbH")
    a = remove_span(CV, restore(CV, base, email), start, end)
    b = restore(CV, remove_span(CV, base, start, end), email)
    expected = base.stripped_text.replace("[EMAIL]", "max.mustermann@example.org").replace(
        "Beispiel GmbH", "[REMOVED]"
    )
    assert a.stripped_text == b.stripped_text == expected
    assert a == b
    # Re-applying the same edit set from scratch gives the same result.
    assert apply_edits(CV, a.edits) == a
    assert (
        apply_edits(CV, Edits(restored=b.edits.restored, removed=b.edits.removed)).stripped_text == expected
    )


def test_applying_the_same_edit_twice_changes_nothing() -> None:
    base = strip_contacts(CV)
    once = restore(CV, base, 1)
    assert restore(CV, once, 1) == once
    start = CV.index("Erfahrung")
    removed = remove_span(CV, base, start, start + 9)
    assert remove_span(CV, removed, start, start + 9) == removed


def test_a_removal_that_overlaps_a_replacement_covers_all_of_it() -> None:
    base = strip_contacts(CV)
    start = CV.index("mustermann@exam")
    r = remove_span(CV, base, start, start + len("mustermann@exam"))
    assert r.stripped_text == base.stripped_text.replace("[EMAIL]", "[REMOVED]")  # no half-address is left
    assert r.report["email"] == 0
    assert all(rep.category != "email" for rep in r.replacements)
    # Restoring the replacement afterwards leaves only the user's own span removed.
    back = restore(CV, r, index_of(base, "email"))
    assert back.stripped_text == base.stripped_text.replace(
        "[EMAIL]", "max.mustermann@example.org".replace("mustermann@exam", "[REMOVED]")
    )
    # A span over two replacements and what lies between them is one removal.
    start = CV.index("Musterstraße")
    end = CV.index("Berlin")
    both = remove_span(CV, base, start + 3, end + 2)
    assert both.stripped_text == base.stripped_text.replace("[ADDRESS], [ADDRESS]", "[REMOVED]")
    assert both.report["address"] == 0


def test_overlapping_removals_merge_and_touching_ones_stay_apart() -> None:
    base = strip_contacts(CV)
    s = CV.index("Erfahrung")
    one = remove_span(CV, remove_span(CV, base, s, s + 5), s + 3, s + 9)
    union = remove_span(CV, base, s, s + 9)
    assert one.stripped_text == union.stripped_text == base.stripped_text.replace("Erfahrung", "[REMOVED]")
    touching = remove_span(CV, remove_span(CV, base, s, s + 5), s + 5, s + 9)
    assert touching.stripped_text == base.stripped_text.replace("Erfahrung", "[REMOVED][REMOVED]")


def test_apply_edits_ignores_a_restored_span_that_names_no_replacement() -> None:
    base = strip_contacts(CV)
    edited = apply_edits(CV, Edits(restored=frozenset({(1, 2), (500, 600)})))
    assert (edited.stripped_text, edited.report, edited.replacements) == (
        base.stripped_text,
        base.report,
        base.replacements,
    )


def test_apply_edits_with_no_edits_is_strip_contacts() -> None:
    assert apply_edits(CV, Edits()) == strip_contacts(CV)
    assert apply_edits("", Edits()) == strip_contacts("")


def test_apply_edits_rejects_a_removed_span_outside_the_original() -> None:
    with pytest.raises(ValueError, match="span"):
        apply_edits("abc", Edits(removed=frozenset({(1, 9)})))


def test_the_dataclasses_are_frozen() -> None:
    r = strip_contacts(CV)
    with pytest.raises(AttributeError):
        r.stripped_text = "x"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        r.replacements[0].start = 1  # type: ignore[misc]
    assert Replacement("email", 1, 2) == Replacement("email", 1, 2)


# --- bounded, linear patterns (the M1 F1 lesson) --------------------------------------------------

_ADVERSARIAL = [
    "a" * 200_000,
    "a." * 100_000,
    "a@" * 100_000,
    "a@b." * 50_000,
    "+1 " * 100_000,
    "0" * 200_000,
    "0171 " * 40_000,
    "0171 1" * 40_000,
    " " * 200_000,
    "\n" * 200_000,
    "+49 30\n" * 30_000,
    "12345 " * 40_000,
    "12345 A" * 30_000,
    "Straße 1" * 25_000,
    "straße " * 30_000,
    "Aa-" * 60_000,
    "Aa Bb " * 30_000,
    "www." * 50_000,
    "www.a" * 40_000,
    "http://" + "a" * 200_000,
    "https://a.b/" * 15_000,
    "github.com/" * 20_000,
    "linkedin.com/in/" * 12_000,
    "Geburtsdatum: " + "x" * 200_000,
    "Geburtsdatum: x\n" * 12_000,
    "Kinder " * 30_000,
    "Kinder\n" * 100_000,
    "Kinder" + " " * 200_000 + "x",
    "Geburtsdatum\n" + " " * 200_000 + "x",
    ("Geburtsdatum\n \n \n" + " " * 50 + "x\n") * 15_000,
    "Familienstand: " + "(" * 100_000,
    "Familienstand " + "(a)" * 60_000,
    "Name: " * 30_000,
    "Name\n" * 60_000,
    "0 " * 100_000,
    "0/" * 100_000,
    "(0)" * 70_000,
    "0 3 " * 50_000,
    "Platz " * 40_000,
    "Potsdamer Platz " * 12_000,
    "Aer Platz 1" * 20_000,
    "Hauptstraße 1.0" * 15_000,
    "0815\n2019\n" * 30_000,
    "Geburtsdatum\n" * 40_000,
    "Familienstand\n\n\n" * 30_000,
    "Geburtsdatum\n \t \n" * 30_000,
    "| Familienstand " * 12_000,
    "geb." * 50_000,
    ("Max Mustermann\n" + "max mustermann " * 15_000),
    "a" * 100_000 + "@" + "b." * 50_000,
    "-" * 200_000,
    ". " * 100_000,
    "(((" * 60_000,
]


@pytest.mark.parametrize("text", _ADVERSARIAL, ids=lambda t: f"{t[:14]!r}x{len(t)}")
def test_the_patterns_stay_linear_on_adversarial_input(text: str) -> None:
    started = time.perf_counter()
    result = strip_contacts(text)
    scan_value(text)
    assert time.perf_counter() - started < 5.0
    assert isinstance(result.stripped_text, str)
