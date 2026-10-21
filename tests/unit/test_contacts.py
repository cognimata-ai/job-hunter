"""Contact stripping (spec §12.3, M2 Task 10): every rule of the table, the order the categories claim
text in, the edit set (`restore`, `remove_span`, `apply_edits`) and `scan_value`.

Every person and datum below is invented: Max Mustermann, Erika Musterfrau, example.org domains and phone
numbers in the ranges that regulators reserve for fiction (BNetzA 030 23125-xxx, 089 99998-xxx and
040 66969-xxx; Ofcom 020 7946 0xxx and 07700 900xxx; NANP 555-01xx).
"""

import subprocess
import sys
import time
from typing import get_args

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
        "0301234567",
        "+44 20 7946 0958",
        "+44 7700 900123",
        "+1 202 555 0143",
        "+1 (202) 555-0143",
        "+41 44 123 45 67",
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
        "Engineering 2020-2022",
        "Software Engineering",
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
    ],
)
def test_words_that_only_look_like_a_label_stay(text: str) -> None:
    assert strip_contacts(text).stripped_text == text


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
