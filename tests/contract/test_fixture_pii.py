# tests/contract/test_fixture_pii.py
import json
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

# validate_identifier(source, key, value) -> bool: the field's own shape check
from jobhunter.cli_fixtures import (
    IDENTIFIER_FIELDS,
    URL_IN_TEXT,
    contact_names,
    role_names,
    valid_url,
    validate_identifier,
)
from jobhunter.observability.redact import EMAIL, KEY, PHONE
from jobhunter.types import JSONValue

ROOT = Path("tests/contract/fixtures")
# bodies, headers, meta and the label file; `.html` and `.txt` hold the bodies that are not JSON (the derived
# HTML error page and the truncated feed page), so they are scanned like every other body
FILES = sorted(p for p in ROOT.rglob("*") if p.suffix in (".json", ".xml", ".tsv", ".html", ".txt"))
# Ruling T6-b: the plan's `\\s` (in a raw string: a backslash and an `s`) let `utm_source=s…` through.
UTM = re.compile(r"utm_source=([^&\"'\s]+)")
PHONE_DE = re.compile(
    r"\(0\d{2,5}\)\s?(?:\d{4,}|\d{2,}(?:[ /\-]\d{2,})+)(?:[ /\-]\d{2,})*(?!\w)"
)  # the scan's own pattern: "(030) 123456", which redact.PHONE misses


# More forms the scan checks on its own (6a review, finding 3): `(0)30 1234567`, a mobile number without
# separators, dotted forms, an `00` country prefix, spaced groups after a slash, a dotted area code before
# mixed groups (`030.12 34 56`; fix wave F1, ledger L78). Bounded: it stays linear.
PHONE_MORE = re.compile(
    r"(?<![\w+\-.])(?:"
    r"\(0\)\s?\d{2,5}(?:[ /\-.]?\d{2,8}){1,4}"
    r"|00[1-9]\d{0,2}(?:[ /\-.]{1,3}\d{2,8}){2,5}"
    r"|\+\d{1,3}(?:\.\d{2,8}){2,5}"
    r"|0\d{2,5}\.\d{2,8}(?:[ .]\d{2,8}){1,4}"
    r"|0\d{2,5}\.\d{3,8}(?:\.\d{2,8}){0,3}"
    r"|0\d{2,5}\s?/\s?\d{2,8}(?:\s\d{2,8}){0,5}"
    r"|01[5-7]\d{8,9}"
    r")(?!\w)"
)


def outside_links(
    text: str,
) -> str:  # a valid link keeps its key-shaped path (publisher keys): KEY is checked on everything else
    return URL_IN_TEXT.sub(lambda m: "" if valid_url(m.group(0)) else m.group(0), text)


SECRET_VARS = ("RAPIDAPI_KEY", "SERPAPI_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY")


def strings(
    doc: JSONValue, path: tuple[str | int, ...] = ()
) -> Iterator[tuple[tuple[str | int, ...], str]]:  # every (path, string value) of a JSON document
    if isinstance(doc, str):
        yield path, doc
    elif isinstance(doc, dict):
        for k, v in doc.items():
            yield from strings(v, (*path, k))
    elif isinstance(doc, list):
        for i, v in enumerate(doc):
            yield from strings(v, (*path, i))


@pytest.mark.parametrize("path", FILES, ids=[str(p.relative_to(ROOT)) for p in FILES])
def test_fixture_has_no_pii_secret_or_key_shaped_value(path) -> None:
    text, source = path.read_text(encoding="utf-8"), path.relative_to(ROOT).parts[0]
    assert not EMAIL.search(text), "an e-mail address survived the scrub"
    assert not PHONE.search(text) and not PHONE_DE.search(text) and not PHONE_MORE.search(text), (
        "a phone number survived the scrub"
    )
    assert {m.group(1) for m in UTM.finditer(text)} <= {"scrubbed"}
    # fix wave F1 (final review C1, ruling FR-f): a name next to a role word ("CEO und Gründer <name>",
    # "<name>, Founder"), introduced by a verb or an interview ("founded by <name>", "Interview mit <name>")
    assert not role_names(text), "a name next to a role word survived the scrub"
    for var in SECRET_VARS:  # the real keys of this machine, when present: never echoed in the message
        value = os.environ.get(var, "")
        assert len(value) < 8 or value not in text, f"the value of {var} is in the fixture"
    if path.suffix != ".json":
        assert not KEY.search(outside_links(text)), "a key-shaped value in a non-JSON fixture"
        # review Minor 6: the scrubber's contact window ("Your contact person: <name>"), on the text as
        # scrubbed
        assert not contact_names(text), "a name after a contact marker survived the scrub"
        return
    doc = json.loads(text)
    if path.name.endswith(".headers.json"):
        assert set(doc) == {"status", "headers"} and not {k.lower() for k in doc["headers"]} & {
            "set-cookie",
            "authorization",
            "x-rapidapi-key",
            "cookie",
        }
    # each JSON string decoded, as the scrubber reads it: line breaks are real, and a contact window never
    # reaches into the next field
    named = [p for p, v in strings(doc) if role_names(v) or contact_names(v)]
    assert not named, f"a name next to a role or contact word survived the scrub at {named}"
    strict = [
        (p, v) for p, v in strings(doc) if KEY.search(outside_links(v))
    ]  # a key-shaped value is allowed only in a valid link or a validated identifier field
    offending = [
        (p, v[:12] + "…")
        for p, v in strict
        if not (
            p
            and str(p[-1]) in IDENTIFIER_FIELDS.get(source, {})
            and validate_identifier(source, str(p[-1]), v)
        )
    ]
    assert not offending, f"key-shaped values outside the identifier allowlist: {[p for p, _ in offending]}"


def test_fixture_tree_is_not_empty() -> None:
    assert (
        len(FILES) >= 30
        and (ROOT / "jsearch/search_v2_page1.json").exists()
        and (ROOT / "location_labels.tsv").exists()
    )


# The scan's own patterns, proved on known values (6a review, findings 2 and 3).
def test_the_utm_check_flags_an_unscrubbed_value_and_passes_the_placeholder() -> None:
    assert {m.group(1) for m in UTM.finditer("x?utm_source=sitime.com&utm_source=scrubbed")} == {
        "sitime.com",
        "scrubbed",
    }
    assert {m.group(1) for m in UTM.finditer('"https://x?a=1&utm_source=scrubbed"')} <= {"scrubbed"}


@pytest.mark.parametrize(
    "phone",
    [
        "(0)30 1234567",
        "01711234567",
        "030.1234567",
        "+49.30.1234567",
        "0049 30 1234567",
        "030 / 12 34 56 78",
        "030.12 34 56",  # a dotted area code before mixed groups (fix wave F1, ledger L78)
        "089.12.34.56",
    ],
)
def test_the_scan_detects_every_phone_form(phone: str) -> None:
    text = f"Ruf an: {phone}."
    assert PHONE.search(text) or PHONE_DE.search(text) or PHONE_MORE.search(text), phone


@pytest.mark.parametrize(
    "value",
    [
        '"id": "744000156183719"',
        '"id": "5933514068"',
        '"created_at": 1792353656',
        '"createdAt": 1792353656000',
        '"id": "3fa85f64-5717-4562-b3fc-015712345678"',
        '"/jobs/0171123456789abc"',
        '"version": "0.10.2"',
    ],
)
def test_the_scan_leaves_ids_uuids_and_timestamps_alone(value: str) -> None:
    assert not (PHONE.search(value) or PHONE_DE.search(value) or PHONE_MORE.search(value)), value


# Fix wave F1 (final review C1, ruling FR-f): the narrative-name rules of the scrubber, proved on invented
# names.
@pytest.mark.parametrize(
    "text",
    [
        "gestaltest du gemeinsam mit unserem CEO und Gründer Max Mustermann unsere Vertriebsorganisation",
        "Max Mustermann, CEO",
        "Founder-Max Mustermann",
        "Hinter Acme steht Gründer Max.",
        "\u201eWe win together.\u201c \u2013 Erika C. Musterfrau, Founder of Acme",
        "Jane Doe (Co-Founder & CTO)",
        "Kontakt: Erika Musterfrau",
        "Ihre Ansprechpartnerin\nErika Musterfrau",
        "&lt;strong&gt;Geschäftsführerin&lt;/strong&gt; Erika Musterfrau",
        # ruling FR-f: the six introducing forms, and the contact forms of the scrubber's window (review Minor
        # 6)
        "Founded in Berlin in 2019 by Max Mustermann and Erika Musterfrau.",
        "Acme wurde 2019 gegründet von Erika Musterfrau.",
        "Watch the interview with Jane Doe on our blog.",
        "Ein Interview mit Max Mustermann über KI.",
        "This is a test job - created by Jane Doe, for testing candidate insights.",
        "Die Seite wurde erstellt von Erika Musterfrau.",
        "Your contact person: Jane Doe",
        "Ihr Ansprechpartner für Fragen: Max Mustermann",
        # F1 re-review, Important 1 (ruling FR-m): a gender marker later in the sentence hides no name
        "Unser CEO Max Mustermann sucht einen Developer (m/w/d)",
        "Max Mustermann, CEO, sucht einen Entwickler (m/w/d)",
        "Created by: Jane Doe",
        # F1 re-review 2, Important 1: a company in the next sentence, line or tag hides no name
        "Gegründet von Max Mustermann. Acme GmbH bietet Software.",
        "Kontakt: Max Mustermann\nAcme AG",
        "Kontakt: Max Mustermann<br>Acme GmbH is hiring",
        "Our CEO Jane Doe. Acme Inc. grew fast",
    ],
)
def test_the_scan_detects_a_name_next_to_a_role_word(text: str) -> None:
    assert role_names(text) or contact_names(text), text


@pytest.mark.parametrize(
    "text",
    [
        "gemeinsam mit unserem CEO und Gründer [person:10] unsere Vertriebsorganisation",  # the placeholder
        "Delivery Hero, Ernst & Young, Mercuri Urval und Acme GmbH",  # company names next to no role
        "Founders Associate - full-time (m/w/d)",
        "Founder-Led Sales",
        "Sparring mit dem CEO: Regelmäßiger Austausch",  # a heading after a colon
        "enterprise buying personas (CISO, CTO, Security, Risk teams)",  # a list after a comma
        "gemeinsam mit dem Geschäftsführer Strukturen für weiteres Wachstum schafft",
        "Dein Kontakt Du hast Fragen?",
        "Contact Us",
        "direct access to the Head of Finance and the founders",
        # ruling FR-f: a company after the introducing words, and the job titles of review Minor 1
        "founded by Acme Data Solutions GmbH in 2019",
        "created by Beispiel Ltd for you",
        "founded by Acme Inc.",
        "Kontakt: Muster Software GmbH",
        "founded by Google veterans",
        "Interview mit Hiring Manager und Team",
        "Ein Interview mit [person:3] über KI.",
        "Geschäftsführer Vertrieb und Marketing (m/w/d)",
        "Geschäftsführer Technik Deutschland (m/w/d). Wir suchen den Vorstand Technik.",
        "Managing Director Germany",
        "CTO Platform Engineering",
        "Kontakt Formular Seite",
        "Contact Customer Service",
    ],
)
def test_the_scan_passes_placeholders_companies_and_role_compounds(text: str) -> None:
    assert not role_names(text) and not contact_names(text), text
