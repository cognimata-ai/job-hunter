"""The PII, secret and key-shape scan of a committed fixture file (spec §18.4; M1 Task 6, M2 Task 4, M3
Task 19): `scan_fixture(path, source=…)`, shared by the contract fixtures' gate
(`tests/contract/test_fixture_pii.py`) and the adversarial eval fixtures' offline checks
(`tests/eval/test_adversarial_offline.py`).
"""

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path

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

# M3 Task 19 (Ruling P39): the packaged adversarial golden-case fixtures, synthetic ads and profiles of
# invented people, ship in the wheel and are scanned like the contract fixtures (every file, any suffix)
EVAL_ROOT = Path("backend/src/jobhunter/scoring/eval_fixtures")
EVAL_FIXTURES = sorted(p for p in EVAL_ROOT.rglob("*") if p.is_file() and "__pycache__" not in p.parts)

# The response headers a committed Anthropic recording never keeps (spec §18.4; plan Task 4)
ANTHROPIC_DROPPED_HEADERS = frozenset({"request-id", "anthropic-organization-id", "cf-ray", "set-cookie"})

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


SECRET_VARS = (
    "RAPIDAPI_KEY",
    "SERPAPI_API_KEY",
    "ADZUNA_APP_ID",
    "ADZUNA_APP_KEY",
    "JOBHUNTER_ANTHROPIC_API_KEY",
)


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


def scan_fixture(path: Path, *, source: str) -> None:
    """The scan of one fixture file (`source`: its tree, which names its identifier allowlist)."""
    text = path.read_text(encoding="utf-8")
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
            "x-api-key",
            "cookie",
        }
        if source == "anthropic":
            assert not {k.lower() for k in doc["headers"]} & ANTHROPIC_DROPPED_HEADERS
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
