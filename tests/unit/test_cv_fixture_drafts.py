"""The SPA's text-review fixtures (`frontend/src/test/fixtures/drafts.json`) are the server's real answers
(plan M4 Task 27, Ruling T27-d).

Each fixture is a synthetic raw CV text and the states its review goes through: per state the complete edit
set the client sends (`PUT /drafts/{id}/text`) and what the server then answers, its stripped text, strip
counts and stored edit set, and the review map that `GET /drafts/{id}` rebuilds from the stored set. Each
state is rebuilt here through the backend's own functions (`review.rebuild`, the stored set's round trip and
`profile_review.review_segments`), so a change of the stripping rules or of the map fails here, before the
client's offset mapping is tested against a map the server no longer gives. A first state without edits is
also the strip of the text phase (`strip_contacts`).

A `stale` state is a stored set that the rules no longer apply (the route module's last paragraph): its
stored text is not the rebuild's (so the GET's map is `[]`), and resending its set is refused with
`no_replacement` at the entries the client must drop.
"""

import json
from pathlib import Path
from typing import Any, Final

import pytest
from jobhunter.api.routes import profile_review
from jobhunter.profile import review
from jobhunter.profile.contacts import Edits, apply_edits, strip_contacts

FIXTURES: Final = Path(__file__).resolve().parents[2] / "frontend/src/test/fixtures/drafts.json"
LOADED: Final[dict[str, Any]] = json.loads(FIXTURES.read_text(encoding="utf-8"))
# What the client tests read of a state: `DraftOut`'s text-review members.
DRAFT_PARTS: Final = ("stripped_text_preview", "strip_report", "edits", "segments")


def span_edits(stored: dict[str, Any]) -> list[review.SpanEdit]:
    """A stored set as the client sends it back: each restore and each removal by its span."""
    return [
        *(review.SpanEdit(op="restore", start=s["start"], end=s["end"]) for s in stored["restored"]),
        *(review.SpanEdit(op="remove", start=s["start"], end=s["end"]) for s in stored["removed"]),
    ]


def answered(raw: str, edits: list[dict[str, Any]]) -> dict[str, Any]:
    """What the server answers after `PUT …/text {span_edits: edits}`, read back with `GET /drafts/{id}`."""
    result = review.rebuild(raw, [review.SpanEdit(**edit) for edit in edits])
    stored = profile_review._stored_edits(profile_review._edits_doc(result.edits))
    segments = profile_review.review_segments(apply_edits(raw, stored))
    assert segments == profile_review.review_segments(result)  # the PUT's rebuild and the GET's agree
    return {
        "stripped_text_preview": result.stripped_text,
        "strip_report": dict(result.report),
        "edits": profile_review._edits_out(stored).model_dump(mode="json"),
        "segments": [segment.model_dump(mode="json") for segment in segments],
    }


def states() -> list[tuple[str, int]]:
    """Every state but a stale one (`test_a_stale_state_is_refused_where_it_says`)."""
    return [
        (name, i)
        for name, fixture in LOADED.items()
        for i, state in enumerate(fixture["states"])
        if not state.get("stale")
    ]


def stale_fixtures() -> list[str]:
    return [name for name, fixture in LOADED.items() if any(s.get("stale") for s in fixture["states"])]


def test_the_fixtures_cover_the_review_shapes() -> None:
    assert sorted(LOADED) == [
        "berlin_germany",
        "emoji",
        "glued_dob",
        "header_name_title",
        "hyphen_phone",
        "kaufmann_surname",
        "name_title_line",
        "stale",
        "successive",
    ]
    assert stale_fixtures() == ["stale"]


@pytest.mark.parametrize(("name", "index"), states())
def test_each_state_is_the_server_s_answer(name: str, index: int) -> None:
    fixture = LOADED[name]
    state = fixture["states"][index]
    expected = answered(fixture["raw"], state["span_edits"])
    assert {part: state[part] for part in DRAFT_PARTS} == expected
    if not state["span_edits"]:
        assert state["stripped_text_preview"] == strip_contacts(fixture["raw"]).stripped_text


@pytest.mark.parametrize("name", stale_fixtures())
def test_a_stale_state_is_refused_where_it_says(name: str) -> None:
    fixture = LOADED[name]
    raw = fixture["raw"]
    stale = next(s for s in fixture["states"] if s.get("stale"))
    assert stale["segments"] == []
    stored = Edits(
        restored=frozenset((s["start"], s["end"]) for s in stale["edits"]["restored"]),
        removed=frozenset((s["start"], s["end"]) for s in stale["edits"]["removed"]),
    )
    # the GET rebuilds the stored set and finds another text: no map
    assert apply_edits(raw, stored).stripped_text != stale["stripped_text_preview"]
    with pytest.raises(review.InvalidEditSet) as refused:
        review.rebuild(raw, span_edits(stale["edits"]))
    errors = [(e.index, e.code) for e in refused.value.errors]
    assert errors == [(i, "no_replacement") for i in stale["refused"]]
    # the rest of the set is the next state's
    rest = [e for i, e in enumerate(span_edits(stale["edits"])) if i not in stale["refused"]]
    following = fixture["states"][fixture["states"].index(stale) + 1]
    assert [review.SpanEdit(**e) for e in following["span_edits"]] == rest
