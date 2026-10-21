"""The hand-authored synthetic `extract_profile` stream (plan Task 13). The paid budget records no
extraction, so the committed fixture is built from `SYNTHETIC_PROFILE`, a body for the invented
`SYNTHETIC_CV`, by `write_synthetic_recording`. It is labelled synthetic (the recorded-bounds check skips it)
and passes the fixture PII gate (`test_fixture_pii.py`, which scans it with every other file). Offline, no
database."""

import json
from pathlib import Path

import anthropic
from jobhunter.llm import fake
from jobhunter.llm.schemas import transformed
from jobhunter.llm.schemas.profile import ProfileOut
from jobhunter.normalize.text import fold
from jobhunter.profile.contacts import strip_contacts

from tests.support.llm import FAKE_LLM_DIR, SYNTHETIC_HEADERS
from tests.support.profile import (
    EXTRACT_MODEL,
    EXTRACT_PURPOSE,
    SYNTHETIC_CV,
    SYNTHETIC_PROFILE,
    write_extract_profile_fixture,
)

COMMITTED = fake.recording_path(FAKE_LLM_DIR, EXTRACT_PURPOSE, EXTRACT_MODEL, transformed(ProfileOut))


def evidences() -> list[str]:
    return [item["evidence"] for key in ("skills", "languages") for item in SYNTHETIC_PROFILE[key]]


def test_the_fixture_is_stored_under_the_profile_schema_key() -> None:
    """Task 6's naming: the purpose, the model and the first 12 hex digits of the sent schema's sha256."""
    assert transformed(ProfileOut) == anthropic.transform_schema(ProfileOut)  # the schema a request sends
    assert COMMITTED == (
        FAKE_LLM_DIR / "extract_profile" / "claude-opus-5-5" / f"{fake.schema12(transformed(ProfileOut))}.sse"
    )
    assert COMMITTED.is_file()


def test_the_committed_fixture_is_what_the_builder_writes(tmp_path: Path) -> None:
    built = write_extract_profile_fixture(tmp_path)
    for suffix in (".sse", ".headers.json", ".components.json"):
        assert built.with_suffix(suffix).read_bytes() == COMMITTED.with_suffix(suffix).read_bytes(), suffix


def test_the_stream_carries_the_body_as_a_parsable_profile() -> None:
    events = fake.parse_sse(COMMITTED.read_bytes())
    assert events[0].name == "message_start" and events[-1].name == "message_stop"
    assert events[0].data["message"]["model"] == EXTRACT_MODEL
    body = json.loads(fake.text_of(events))
    assert body == SYNTHETIC_PROFILE
    ProfileOut.model_validate(body)


def test_every_evidence_is_a_passage_of_the_cv_that_contact_stripping_keeps() -> None:
    stripped = strip_contacts(SYNTHETIC_CV).stripped_text
    assert evidences()
    for evidence in evidences():
        assert evidence in SYNTHETIC_CV and fold(evidence) in fold(stripped), evidence


def test_the_fixture_is_labelled_synthetic_with_the_gate_allowed_headers_only() -> None:
    components = json.loads(COMMITTED.with_suffix(".components.json").read_text("utf-8"))
    assert components["source"] == "synthetic"  # the label tests/payload/test_recorded_bounds.py skips
    headers = json.loads(COMMITTED.with_suffix(".headers.json").read_text("utf-8"))
    assert headers == {"status": 200, "headers": SYNTHETIC_HEADERS}
