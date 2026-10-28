# tests/contract/test_fixture_pii.py
from pathlib import Path

import pytest
from jobhunter.cli_fixtures import contact_names, role_names
from jobhunter.llm import fake
from jobhunter.observability.redact import PHONE

from tests.support.fixture_scan import (
    EVAL_FIXTURES,
    EVAL_ROOT,
    PHONE_DE,
    PHONE_MORE,
    UTM,
    scan_fixture,
    strings,
)

ROOT = Path("tests/contract/fixtures")
# bodies, headers, meta and the label file; `.html` and `.txt` hold the bodies that are not JSON (the derived
# HTML error page and the truncated feed page), so they are scanned like every other body; `.sse` holds the
# recorded and hand-authored Anthropic streams under `anthropic/` (M2 Task 4)
FILES = sorted(p for p in ROOT.rglob("*") if p.suffix in (".json", ".xml", ".tsv", ".html", ".txt", ".sse"))
# M2 Task 4: every file under `anthropic/` is scanned, whatever its suffix (a stream, a header or a sidecar)
ANTHROPIC_FILES = sorted(p for p in (ROOT / "anthropic").rglob("*") if p.is_file())


@pytest.mark.parametrize("path", FILES, ids=[str(p.relative_to(ROOT)) for p in FILES])
def test_fixture_has_no_pii_secret_or_key_shaped_value(path) -> None:
    scan_fixture(path, source=path.relative_to(ROOT).parts[0])


@pytest.mark.parametrize("path", EVAL_FIXTURES, ids=[str(p.relative_to(EVAL_ROOT)) for p in EVAL_FIXTURES])
def test_eval_fixture_has_no_pii_secret_or_key_shaped_value(path: Path) -> None:
    """M3 Task 19: the packaged eval fixtures pass the same scan (no identifier field is allowed there)."""
    scan_fixture(path, source="eval_fixtures")


def test_the_eval_fixture_tree_is_scanned() -> None:
    """The ten adversarial cases (M3 Task 19) and nothing of them escapes the scan by its suffix."""
    adversarial = [p for p in EVAL_FIXTURES if p.parent.name == "adversarial" and p.suffix == ".json"]
    assert len(adversarial) == 10
    assert all(p.suffix in (".json", ".py") for p in EVAL_FIXTURES)


def test_fixture_tree_is_not_empty() -> None:
    assert (
        len(FILES) >= 30
        and (ROOT / "jsearch/search_v2_page1.json").exists()
        and (ROOT / "location_labels.tsv").exists()
    )


def test_every_anthropic_fixture_is_scanned() -> None:
    """The scan gates `anthropic/**` whole (M2 Task 4): no file there escapes it by its suffix, and the tree
    holds at least the two V13 recordings with their sidecars."""
    assert set(ANTHROPIC_FILES) <= set(FILES)
    assert sum(1 for p in ANTHROPIC_FILES if p.suffix == ".sse") >= 2


@pytest.mark.parametrize(
    "path",
    [p for p in ANTHROPIC_FILES if p.suffix == ".sse"],
    ids=[str(p.relative_to(ROOT)) for p in ANTHROPIC_FILES if p.suffix == ".sse"],
)
def test_a_committed_anthropic_stream_stores_no_thinking_and_no_signature(path: Path) -> None:
    """Thinking content is never stored (G6): a committed stream keeps the thinking events' shape only."""
    for event in fake.parse_sse(path.read_bytes()):
        for key_path, value in strings(event.data):
            if key_path and key_path[-1] == "thinking":
                assert value in ("", fake.THINKING_OMITTED), f"thinking text at {key_path}"
            if key_path and key_path[-1] in ("signature", "data"):
                assert value == fake.SIGNATURE_OMITTED, f"a thinking signature at {key_path}"


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
        # final wave F5 (F1 re-review 3, Minor 1): an `&` before anything but a legal form ends the name
        "founded by Max Mustermann & Acme GmbH",
        "Kontakt: Max Mustermann & Acme GmbH",
        "Kontakt: Max Mustermann &amp; Acme GmbH",
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
        # final wave F5: a company joined by `&`, and an `&` right before a legal form, stay companies
        "founded by Müller & Schmidt GmbH",
        "Kontakt: Müller & Schmidt GmbH",
        "founded by Beispiel Muster & Co. KG",
        "Kontakt: Beispiel Muster &amp; Co. KG",
    ],
)
def test_the_scan_passes_placeholders_companies_and_role_compounds(text: str) -> None:
    assert not role_names(text) and not contact_names(text), text
