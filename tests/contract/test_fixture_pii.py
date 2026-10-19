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
# separators, dotted forms, an `00` country prefix, spaced groups after a slash. Bounded: it stays linear.
PHONE_MORE = re.compile(
    r"(?<![\w+\-.])(?:"
    r"\(0\)\s?\d{2,5}(?:[ /\-.]?\d{2,8}){1,4}"
    r"|00[1-9]\d{0,2}(?:[ /\-.]{1,3}\d{2,8}){2,5}"
    r"|\+\d{1,3}(?:\.\d{2,8}){2,5}"
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
    for var in SECRET_VARS:  # the real keys of this machine, when present: never echoed in the message
        value = os.environ.get(var, "")
        assert len(value) < 8 or value not in text, f"the value of {var} is in the fixture"
    if path.suffix != ".json":
        assert not KEY.search(outside_links(text)), "a key-shaped value in a non-JSON fixture"
        return
    doc = json.loads(text)
    if path.name.endswith(".headers.json"):
        assert set(doc) == {"status", "headers"} and not {k.lower() for k in doc["headers"]} & {
            "set-cookie",
            "authorization",
            "x-rapidapi-key",
            "cookie",
        }
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
    ["(0)30 1234567", "01711234567", "030.1234567", "+49.30.1234567", "0049 30 1234567", "030 / 12 34 56 78"],
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
