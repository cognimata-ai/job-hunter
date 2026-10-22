"""The pure rules of the profile review (spec §12.3, §12.7, §14.2; M2 Task 14): the repairs of a failed draft
and the retry route's admission, which are one predicate (Deviation 3), and the complete text edit set
(Deviation 29, rulings R14-1 and R14-2).

Every name and datum is invented (Max Mustermann, Erika Musterfrau, example.org).
"""

from dataclasses import replace

import pytest
from jobhunter.llm.gateway import INVALID_OUTPUT, MAX_TOKENS, MODEL_MISMATCH, REFUSED, UNEXPECTED_STOP_REASON
from jobhunter.profile.contacts import Edits, apply_edits, restore, strip_contacts
from jobhunter.profile.review import (
    MODEL_OUTCOME_CODES,
    PURGED_DETAIL,
    FailedDraft,
    InvalidEditSet,
    RetryMode,
    SpanEdit,
    rebuild,
    repairs,
    retry_refusal,
)

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TEXT = "text/plain"

# A PDF upload that failed in the text phase: never approved, no reviewed text, its file still there.
TEXT_PHASE = FailedDraft(
    failure_code="low_text",
    native_pdf=False,
    approved=False,
    has_reviewed_text=False,
    content_purged=False,
    mime=PDF,
    has_file=True,
)
# A paste whose approved text the model refused.
REFUSED_PASTE = FailedDraft(
    failure_code="refused",
    native_pdf=False,
    approved=True,
    has_reviewed_text=True,
    content_purged=False,
    mime=TEXT,
    has_file=False,
)
REFUSED_UPLOAD = replace(REFUSED_PASTE, mime=PDF, has_file=True)
NATIVE = replace(REFUSED_UPLOAD, native_pdf=True, approved=False, has_reviewed_text=False)

CASES = [
    pytest.param(TEXT_PHASE, True, ["paste_text", "native_pdf"], id="low_text"),
    pytest.param(TEXT_PHASE, False, ["paste_text"], id="low_text-native-disabled"),
    pytest.param(replace(TEXT_PHASE, failure_code="parse_error"), True, ["paste_text"], id="parse_error"),
    pytest.param(replace(TEXT_PHASE, failure_code="encrypted"), True, ["paste_text"], id="encrypted"),
    pytest.param(
        replace(TEXT_PHASE, failure_code="too_many_pages"), True, ["paste_text"], id="too_many_pages"
    ),
    pytest.param(replace(TEXT_PHASE, failure_code="timeout"), True, ["paste_text"], id="timeout"),
    pytest.param(
        replace(TEXT_PHASE, failure_code="attempts_exhausted"), True, ["paste_text"], id="text-hook-exhausted"
    ),
    pytest.param(replace(TEXT_PHASE, mime=DOCX), True, ["paste_text"], id="low_text-docx"),
    pytest.param(replace(TEXT_PHASE, has_file=False), True, ["paste_text"], id="low_text-file-purged"),
    pytest.param(REFUSED_PASTE, True, ["retry_llm"], id="refused-paste"),
    pytest.param(
        replace(REFUSED_PASTE, has_reviewed_text=False, content_purged=True),
        True,
        [],
        id="refused-paste-purged",
    ),
    pytest.param(
        replace(REFUSED_PASTE, failure_code="attempts_exhausted"), True, ["retry_llm"], id="extract-exhausted"
    ),
    pytest.param(REFUSED_UPLOAD, True, ["native_pdf", "retry_llm"], id="refused-upload"),
    pytest.param(REFUSED_UPLOAD, False, ["retry_llm"], id="refused-upload-native-disabled"),
    pytest.param(
        replace(REFUSED_UPLOAD, has_reviewed_text=False, content_purged=True),
        True,
        ["native_pdf"],
        id="refused-upload-content-purged",
    ),
    pytest.param(
        replace(REFUSED_UPLOAD, has_reviewed_text=False, content_purged=True, has_file=False),
        True,
        [],
        id="refused-upload-all-purged",
    ),
    pytest.param(NATIVE, True, ["native_pdf", "retry_llm"], id="native-refused"),
    pytest.param(NATIVE, False, [], id="native-refused-native-disabled"),  # its retry sends the PDF again
    pytest.param(replace(NATIVE, has_file=False), True, [], id="native-file-purged"),
    # A docparse timeout may pass the next time; the flag decides.
    pytest.param(
        replace(NATIVE, failure_code="timeout"), True, ["paste_text", "retry_llm"], id="native-timeout"
    ),
    pytest.param(
        replace(NATIVE, failure_code="timeout"), False, ["paste_text"], id="native-timeout-disabled"
    ),
    pytest.param(
        replace(NATIVE, failure_code="native_pdf_disabled"), False, ["paste_text"], id="native-disabled-code"
    ),
    pytest.param(
        replace(NATIVE, failure_code="native_pdf_disabled"),
        True,
        ["paste_text", "retry_llm"],
        id="native-disabled-code-flag-back",
    ),
    pytest.param(replace(NATIVE, failure_code="budget_exhausted"), True, ["retry_llm"], id="native-budget"),
    # The model's outcomes: the original PDF may change them.
    *(
        pytest.param(
            replace(REFUSED_UPLOAD, failure_code=code), True, ["native_pdf", "retry_llm"], id=f"upload-{code}"
        )
        for code in ("invalid_output", "refused", "max_tokens", "unexpected_stop_reason", "model_mismatch")
    ),
    # Every other extract-phase failure: the PDF cures none of them, and it sends the contacts (fix round 1).
    *(
        pytest.param(replace(REFUSED_UPLOAD, failure_code=code), True, ["retry_llm"], id=f"upload-{code}")
        for code in (
            "budget_exhausted",
            "bug",
            "llm_transient",
            "attempts_exhausted",
            "input_unbounded",
            "llm_request_401",
            "llm_request_500",
        )
    ),
    # What `check_pdf` gives again for the same file: never a retry of the native path (fix round 1).
    *(
        pytest.param(replace(NATIVE, failure_code=code), True, ["paste_text"], id=f"native-{code}")
        for code in (
            "encrypted",
            "too_many_pages",
            "parse_error",
            "parse_limit",
            "unsupported",
            "cv_file_unreadable",
        )
    ),
]


@pytest.mark.parametrize(("draft", "enabled", "expected"), CASES)
def test_the_repairs_of_a_failed_draft(draft: FailedDraft, enabled: bool, expected: list[str]) -> None:
    assert repairs(draft, native_enabled=enabled) == expected


@pytest.mark.parametrize(("draft", "enabled", "expected"), CASES)
def test_a_repair_is_offered_exactly_when_the_retry_route_admits_its_mode(
    draft: FailedDraft, enabled: bool, expected: list[str]
) -> None:
    offered = repairs(draft, native_enabled=enabled)
    modes: tuple[tuple[RetryMode, str], ...] = (("llm", "retry_llm"), ("native_pdf", "native_pdf"))
    for mode, repair in modes:
        refusal = retry_refusal(draft, mode, native_enabled=enabled)
        assert (refusal is None) == (repair in offered), (mode, refusal)


def test_the_refusals_name_their_reason() -> None:
    def reason(draft: FailedDraft, mode: RetryMode, enabled: bool = True) -> str | None:
        refusal = retry_refusal(draft, mode, native_enabled=enabled)
        return None if refusal is None else refusal.reason

    assert reason(TEXT_PHASE, "llm") == "text_not_approved"
    assert reason(TEXT_PHASE, "native_pdf", enabled=False) == "native_disabled"
    assert reason(replace(TEXT_PHASE, failure_code="encrypted"), "native_pdf") == "failure_not_admitted"
    assert reason(replace(TEXT_PHASE, mime=DOCX), "native_pdf") == "not_a_pdf"
    assert reason(REFUSED_PASTE, "native_pdf") == "not_a_pdf"
    assert reason(replace(NATIVE, failure_code="too_many_pages"), "llm") == "pdf_checks_failed"
    assert reason(replace(NATIVE, failure_code="parse_error"), "llm") == "pdf_checks_failed"
    assert (
        reason(replace(REFUSED_UPLOAD, failure_code="budget_exhausted"), "native_pdf")
        == "failure_not_admitted"
    )
    assert reason(replace(NATIVE, failure_code="encrypted"), "native_pdf") == "failure_not_admitted"
    assert reason(NATIVE, "llm", enabled=False) == "native_disabled"
    purged = [
        retry_refusal(
            replace(REFUSED_PASTE, has_reviewed_text=False, content_purged=True), "llm", native_enabled=True
        ),
        retry_refusal(replace(TEXT_PHASE, has_file=False), "native_pdf", native_enabled=True),
        retry_refusal(replace(NATIVE, has_file=False), "llm", native_enabled=True),
    ]
    for refusal in purged:
        assert refusal is not None and (refusal.reason, refusal.detail) == ("input_purged", PURGED_DETAIL)
    assert PURGED_DETAIL == "The CV was deleted after 30 days; upload or paste it again"


def test_the_model_outcome_codes_are_the_gateways() -> None:
    assert frozenset({INVALID_OUTPUT, REFUSED, MAX_TOKENS, UNEXPECTED_STOP_REASON, MODEL_MISMATCH}) == (
        MODEL_OUTCOME_CODES
    )


# --- the complete edit set -------------------------------------------------------------------------------

CV = (
    "Max Mustermann\nE-Mail: max@example.org\nReligion und Ethik, Kontakt: erika@example.org\n"
    "Python, FastAPI\n"
)
NAME, EMAIL, PERSONAL = 0, 1, 2  # the replacements of `strip_contacts(CV)`
INNER = (CV.index("erika@"), CV.index("erika@") + len("erika@example.org"))  # inside the personal span
PYTHON = (CV.index("Python"), CV.index("Python") + len("Python"))


def remove(start: int, end: int) -> SpanEdit:
    return SpanEdit(op="remove", start=start, end=end)


def restore_index(index: int) -> SpanEdit:
    return SpanEdit(op="restore", replacement_index=index)


def restore_span(start: int, end: int) -> SpanEdit:
    return SpanEdit(op="restore", start=start, end=end)


def test_the_fresh_strip_of_the_cv() -> None:
    base = strip_contacts(CV)
    assert base.stripped_text == "[NAME]\nE-Mail: [EMAIL]\n[PERSONAL]\nPython, FastAPI\n"
    assert [r.category for r in base.replacements] == ["name", "email", "personal"]


def test_an_empty_set_is_the_fresh_strip() -> None:
    assert rebuild(CV, []) == strip_contacts(CV)


def test_the_set_is_rebuilt_from_the_original_and_composes() -> None:
    first = rebuild(CV, [remove(*PYTHON)])
    assert first.stripped_text == "[NAME]\nE-Mail: [EMAIL]\n[PERSONAL]\n[REMOVED], FastAPI\n"
    both = rebuild(CV, [remove(*PYTHON), restore_index(EMAIL)])
    assert both.stripped_text == "[NAME]\nE-Mail: max@example.org\n[PERSONAL]\n[REMOVED], FastAPI\n"
    # The order of the set does not matter, and a restore by index equals one by its span.
    email = strip_contacts(CV).replacements[EMAIL]
    assert rebuild(CV, [restore_span(email.start, email.end), remove(*PYTHON)]) == both
    assert both.edits == Edits(restored=frozenset({(email.start, email.end)}), removed=frozenset({PYTHON}))
    assert apply_edits(CV, both.edits) == both


def test_a_contact_inside_a_restored_personal_span_is_restored_by_its_span() -> None:
    personal = rebuild(CV, [restore_index(PERSONAL)])
    assert (
        personal.stripped_text
        == "[NAME]\nE-Mail: [EMAIL]\nReligion und Ethik, Kontakt: [EMAIL]\nPython, FastAPI\n"
    )
    assert personal == restore(CV, strip_contacts(CV), PERSONAL)
    both = rebuild(CV, [restore_index(PERSONAL), restore_span(*INNER)])
    assert both.stripped_text == (
        "[NAME]\nE-Mail: [EMAIL]\nReligion und Ethik, Kontakt: erika@example.org\nPython, FastAPI\n"
    )
    # The same span without its personal span names no replacement: the inner contact exists only then.
    with pytest.raises(InvalidEditSet) as bad:
        rebuild(CV, [restore_span(*INNER)])
    assert [(e.index, e.member, e.code) for e in bad.value.errors] == [(0, "start", "no_replacement")]


@pytest.mark.parametrize(
    ("edits", "errors"),
    [
        pytest.param([remove(1, 5), remove(4, 9)], [(1, "start", "overlap")], id="overlapping-removals"),
        pytest.param([remove(4, 9), remove(4, 9)], [(1, "start", "duplicate")], id="duplicate-removal"),
        pytest.param(
            [restore_index(EMAIL), restore_index(EMAIL)],
            [(1, "replacement_index", "duplicate")],
            id="same-index-twice",
        ),
        pytest.param(
            [remove(len(CV) - 1, len(CV) + 1)], [(0, "end", "out_of_range")], id="end-past-the-text"
        ),
        pytest.param(
            [remove(len(CV), len(CV) + 3)], [(0, "start", "out_of_range")], id="start-past-the-text"
        ),
        pytest.param([remove(-1, 3)], [(0, "start", "out_of_range")], id="negative-start"),
        pytest.param([remove(5, 5)], [(0, "end", "out_of_range")], id="empty-span"),
        pytest.param([remove(7, 3)], [(0, "end", "out_of_range")], id="reversed-span"),
        pytest.param(
            [restore_index(3)], [(0, "replacement_index", "unknown_replacement")], id="unknown-index"
        ),
        pytest.param(
            [restore_index(-1)], [(0, "replacement_index", "unknown_replacement")], id="negative-index"
        ),
        pytest.param([restore_span(1, 5)], [(0, "start", "no_replacement")], id="span-of-no-replacement"),
        pytest.param(
            [restore_span(0, 10_000)], [(0, "end", "out_of_range")], id="restore-span-past-the-text"
        ),
        pytest.param([SpanEdit(op="remove", start=3)], [(0, "end", "missing")], id="removal-without-end"),
        pytest.param(
            [SpanEdit(op="remove", start=1, end=3, replacement_index=0)],
            [(0, "replacement_index", "unexpected")],
            id="removal-with-index",
        ),
        pytest.param(
            [SpanEdit(op="restore")], [(0, "replacement_index", "missing")], id="restore-of-nothing"
        ),
        pytest.param([SpanEdit(op="restore", end=14)], [(0, "start", "missing")], id="restore-without-start"),
        pytest.param(
            [SpanEdit(op="restore", start=0, end=14, replacement_index=0)],
            [(0, "replacement_index", "unexpected")],
            id="restore-by-both",
        ),
        pytest.param(
            [restore_index(EMAIL), remove(1, 5), restore_index(9), remove(2, 3)],
            [(2, "replacement_index", "unknown_replacement"), (3, "start", "overlap")],
            id="every-error-of-the-set",
        ),
    ],
)
def test_an_invalid_edit_set_names_each_invalid_edit(
    edits: list[SpanEdit], errors: list[tuple[int, str, str]]
) -> None:
    with pytest.raises(InvalidEditSet) as bad:
        rebuild(CV, edits)
    assert [(e.index, e.member, e.code) for e in bad.value.errors] == errors
    for error in bad.value.errors:  # fixed texts: no value of the CV
        assert "max@" not in error.message and "Mustermann" not in error.message and error.message


def test_a_replacement_or_a_span_named_twice_is_a_duplicate_on_the_later_entry() -> None:
    email = strip_contacts(CV).replacements[EMAIL]
    cases = [
        ([restore_index(EMAIL), restore_span(email.start, email.end)], [(1, "start", "duplicate")]),
        (
            [restore_span(email.start, email.end), restore_index(EMAIL)],
            [(1, "replacement_index", "duplicate")],
        ),
        ([restore_index(PERSONAL), restore_span(*INNER), restore_span(*INNER)], [(2, "start", "duplicate")]),
        (
            [remove(*PYTHON), remove(PYTHON[0], PYTHON[1] + 2), remove(*PYTHON)],
            [(1, "start", "overlap"), (2, "start", "duplicate")],
        ),
    ]
    for edits, errors in cases:
        with pytest.raises(InvalidEditSet) as bad:
            rebuild(CV, edits)
        assert [(e.index, e.member, e.code) for e in bad.value.errors] == errors, edits
    # Without its duplicate the same set is valid.
    assert rebuild(CV, [restore_index(PERSONAL), restore_span(*INNER)]).stripped_text.count("erika@") == 1


def test_touching_removals_and_a_removal_over_a_replacement_are_valid() -> None:
    touching = rebuild(CV, [remove(PYTHON[0], PYTHON[0] + 3), remove(PYTHON[0] + 3, PYTHON[1])])
    assert "[REMOVED][REMOVED], FastAPI" in touching.stripped_text
    # Over the end of the e-mail address and its line break: one `[REMOVED]` over the union, no half address.
    email = strip_contacts(CV).replacements[EMAIL]
    over = rebuild(CV, [remove(email.start + 2, email.end + 1)])
    assert over.stripped_text == "[NAME]\nE-Mail: [REMOVED][PERSONAL]\nPython, FastAPI\n"
