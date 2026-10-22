"""The pure rules of the profile review (spec §12.3, §12.7, §14.2; M2 Task 14): what a failed draft offers,
what the retry route admits, and the complete text edit set. No I/O, no clock, no logging, and no value of a
CV in any message: the api routes (`api/routes/profile_review.py`) read the rows, call these and write them.

**Repairs and the retry** are one predicate (plan Deviation 3): `repairs(draft)` lists a mode exactly when
`retry_refusal(draft, mode)` admits it, both from the stored state (`FailedDraft`), never from the failure
code alone. The phase a draft failed in is in that state: a draft whose text was never approved
(`reviewed_text_sha256 IS NULL`) and that was not sent as a PDF failed in the text phase; any other failed in
the extract phase, where `attempts_exhausted` (the extract hook's) or a docparse code of `check_pdf` can
appear as well. So:
- `"paste_text"` for a failure to read the document (`document_failure`): every text-phase failure
  (`low_text`, `encrypted`, `too_many_pages`, `parse_error`, `parse_limit`, `timeout`, `unsupported`,
  `cv_file_unreadable`, the text hook's `attempts_exhausted`), and a native PDF that `check_pdf` refused
  (`DOCUMENT_CODES`; plan Deviation 7: after a deterministic code that is all it offers, after a `timeout`
  `retry_llm` too). §12.7: paste text is the alternative. A new paste or upload discards the failed draft
  (Task 9), so the route of this repair is `POST /api/profile/cv-text`, not the retry;
- `"native_pdf"` when `mode="native_pdf"` is admitted: `bounds.NATIVE_PDF_ENABLED` (the caller reads it per
  request), a PDF whose file remains (not purged), and a draft that failed with `low_text` in the text phase
  or with a model outcome in the extract phase (`MODEL_OUTCOME_CODES`: `invalid_output`, `refused`,
  `max_tokens`, `unexpected_stop_reason`, `model_mismatch`), the failures the original PDF can change.
  `budget_exhausted`, `input_unbounded`, `llm_request_<status>`, `llm_transient`, `attempts_exhausted` and an
  operation's `bug` never offer it: sending the PDF does not cure them, and it sends the contacts. `encrypted`
  and `too_many_pages` fail §12.1's PDF checks, which the native path must pass as well;
- `"retry_llm"` when `mode="llm"` is admitted: the same input again. A text draft needs its approved text,
  stored and not purged (`reviewed_text IS NOT NULL AND content_purged_at IS NULL`), whatever its code. A
  native draft is sent as its PDF again, so it needs the native path enabled (plan Deviation 9), its PDF not
  purged, and a failure that the same file does not give again: never one of `check_pdf`'s deterministic
  document codes (`DETERMINISTIC_PDF_CODES`: `encrypted`, `too_many_pages`, `parse_error`, `parse_limit`,
  `unsupported`, `cv_file_unreadable`; plan Deviation 7); a docparse `timeout` may pass the next time. A
  text-phase failure has no approved text, so it is refused here, and never reaches §7.4's CHECK and a 500.
A refusal names its reason and a fixed detail; input that §20.1's purge removed has the detail
`PURGED_DETAIL`.

**The edit set** (plan Deviation 29, rulings R14-1 and R14-2): `rebuild(raw, edits)` checks the draft's
complete set against the raw text and builds its stripped text with `contacts.apply_edits`; an invalid set
raises `InvalidEditSet` with every invalid edit (its position, the member and a code), and nothing is built:
- a removal has `start` and `end`, offsets into the raw text with `0 <= start < end <= len(raw)`, and no
  `replacement_index`; removals may not overlap each other (`overlap`; touching is fine), and one span twice
  is `duplicate`. A removal over a replacement is valid: it merges into one `[REMOVED]` (Task 10);
- a restore names a replacement either by `replacement_index`, into the fresh `strip_contacts(raw)` (the
  index `contacts.restore` takes), or by its raw span `start`, `end`, never by both. A span names a
  replacement of the fresh strip, or a contact inside a personal span the same set restores (Deviation 35:
  those contacts have no index). A span that names neither is `no_replacement`. A replacement named twice
  (the same index twice, or an index and its own span) is `duplicate`, on the later entry.
The set's own order does not matter, and the result's `edits` is what the draft stores: spans only. Offsets
are Unicode code points (Python `str` indices), not UTF-16 code units.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from jobhunter.profile.contacts import Edits, Span, StripResult, apply_edits, strip_contacts

Repair = Literal["paste_text", "native_pdf", "retry_llm"]
RetryMode = Literal["llm", "native_pdf"]
RefusalReason = Literal[
    "native_disabled",
    "not_a_pdf",
    "input_purged",
    "failure_not_admitted",
    "text_not_approved",
    "pdf_checks_failed",
]

PDF_MIME: Final = "application/pdf"
LOW_TEXT: Final = "low_text"
# The codes of a model outcome (Task 5's gateway codes, `graphs.profile.nodes.llm_outcome`): what the model
# made of the text. The original PDF may change them; nothing else that fails an extraction is cured by it.
MODEL_OUTCOME_CODES: Final = frozenset(
    {"invalid_output", "refused", "max_tokens", "unexpected_stop_reason", "model_mismatch"}
)
# What docparse answers again for the same file (plan Deviation 7; `timeout` is not among them).
DETERMINISTIC_PDF_CODES: Final = frozenset(
    {"encrypted", "too_many_pages", "parse_error", "parse_limit", "unsupported", "cv_file_unreadable"}
)
# The codes of a document that could not be read: docparse's, an unreadable stored file, and a native draft
# that met a disabled native path (`check_pdf`).
DOCUMENT_CODES: Final = DETERMINISTIC_PDF_CODES | frozenset({"timeout", "native_pdf_disabled"})
PURGED_DETAIL: Final = "The CV was deleted after 30 days; upload or paste it again"
_DETAILS: Final[dict[RefusalReason, str]] = {
    "native_disabled": "Sending the original PDF is not enabled.",
    "not_a_pdf": "Only an uploaded PDF can be sent as the original file.",
    "input_purged": PURGED_DETAIL,
    "failure_not_admitted": "The original PDF cannot be sent after this failure; paste the CV text instead.",
    "text_not_approved": "This CV failed before its text was approved; paste the text or upload it again.",
    "pdf_checks_failed": "The original PDF fails the same checks again; paste the CV text instead.",
}


@dataclass(frozen=True)
class FailedDraft:
    """What the repairs and the retry read of a `failed` draft and its `cv_files` row: no content.

    `approved` is `reviewed_text_sha256 IS NOT NULL`, `has_reviewed_text` is `reviewed_text IS NOT NULL`,
    `content_purged` is `content_purged_at IS NOT NULL`, and `has_file` is a stored file that §20.1 has not
    purged (`storage_path IS NOT NULL AND purged_at IS NULL`)."""

    failure_code: str
    native_pdf: bool
    approved: bool
    has_reviewed_text: bool
    content_purged: bool
    mime: str
    has_file: bool

    @property
    def text_phase(self) -> bool:
        """It failed before its text was approved, and not on the native path."""
        return not self.native_pdf and not self.approved

    @property
    def document_failure(self) -> bool:
        return self.text_phase or self.failure_code in DOCUMENT_CODES

    @property
    def native_admissible(self) -> bool:
        """It failed in a way the original PDF may change: `low_text`, or a model outcome."""
        if self.text_phase:
            return self.failure_code == LOW_TEXT
        return self.failure_code in MODEL_OUTCOME_CODES


@dataclass(frozen=True)
class Refusal:
    reason: RefusalReason
    detail: str


def _refused(reason: RefusalReason) -> Refusal:
    return Refusal(reason, _DETAILS[reason])


def retry_refusal(draft: FailedDraft, mode: RetryMode, *, native_enabled: bool) -> Refusal | None:
    """Why the retry route refuses `mode` for `draft` (a 409 `conflict_state`), or `None` when it admits it
    (module docstring)."""
    if mode == "native_pdf":
        if not native_enabled:
            return _refused("native_disabled")
        if not draft.native_admissible:
            return _refused("failure_not_admitted")
        if draft.mime != PDF_MIME:
            return _refused("not_a_pdf")
        if not draft.has_file:
            return _refused("input_purged")
        return None
    if draft.native_pdf:  # the same input is the PDF: the native path again
        if not native_enabled:
            return _refused("native_disabled")
        if not draft.has_file:
            return _refused("input_purged")
        if draft.failure_code in DETERMINISTIC_PDF_CODES:
            return _refused("pdf_checks_failed")
        return None
    if draft.has_reviewed_text and not draft.content_purged:
        return None
    if draft.content_purged:
        return _refused("input_purged")
    return _refused("text_not_approved")


def repairs(draft: FailedDraft, *, native_enabled: bool) -> list[Repair]:
    """The §12.7 repairs `draft` offers, in the order `paste_text`, `native_pdf`, `retry_llm`."""
    offered: list[Repair] = ["paste_text"] if draft.document_failure else []
    if retry_refusal(draft, "native_pdf", native_enabled=native_enabled) is None:
        offered.append("native_pdf")
    if retry_refusal(draft, "llm", native_enabled=native_enabled) is None:
        offered.append("retry_llm")
    return offered


# --- the complete edit set -------------------------------------------------------------------------------

EditMember = Literal["start", "end", "replacement_index"]
EditErrorCode = Literal[
    "missing", "unexpected", "out_of_range", "overlap", "duplicate", "unknown_replacement", "no_replacement"
]


@dataclass(frozen=True)
class SpanEdit:
    """One edit of the set as the request sends it: `remove` with a raw span, or `restore` with an index or a
    raw span."""

    op: Literal["remove", "restore"]
    start: int | None = None
    end: int | None = None
    replacement_index: int | None = None


@dataclass(frozen=True)
class EditError:
    """Edit `index` of the set is invalid at `member`: a code and a fixed message (no value of the CV)."""

    index: int
    member: EditMember
    code: EditErrorCode
    message: str


class InvalidEditSet(Exception):
    """The set holds invalid edits; `errors` names each, ordered by position."""

    def __init__(self, errors: Sequence[EditError]) -> None:
        super().__init__(f"{len(errors)} invalid edits")
        self.errors = tuple(sorted(errors, key=lambda e: e.index))


def _span(index: int, edit: SpanEdit, length: int) -> Span | EditError:
    """The raw span of `edit`, or why it is none: a member missing, or not `0 <= start < end <= length`."""
    if edit.start is None:
        return EditError(index, "start", "missing", "A span needs its start offset.")
    if edit.end is None:
        return EditError(index, "end", "missing", "A span needs its end offset.")
    if not 0 <= edit.start < length:
        return EditError(
            index, "start", "out_of_range", f"The start must lie in the CV text (0 to {length - 1})."
        )
    if not edit.start < edit.end <= length:
        message = f"The end must lie after the start and at most at the end of the CV text ({length})."
        return EditError(index, "end", "out_of_range", message)
    return edit.start, edit.end


def rebuild(raw: str, edits: Sequence[SpanEdit]) -> StripResult:
    """The stripped text of `raw` with the complete edit set `edits` (module docstring); `InvalidEditSet`."""
    base = strip_contacts(raw).replacements
    base_spans = {(r.start, r.end) for r in base}
    errors: list[EditError] = []
    named: set[Span] = set()  # every span a restore of the set has named so far, by index or by span
    restored: set[Span] = set()  # the replacements of the fresh strip among them
    # Restore spans that name no replacement of the fresh strip: contacts inside a restored personal span?
    span_restores: list[tuple[int, Span]] = []
    removals: list[tuple[Span, int]] = []
    removed: set[Span] = set()

    def name(index: int, member: EditMember, span: Span) -> None:
        if span in named:
            message = "An earlier edit of the set restores this replacement already."
            errors.append(EditError(index, member, "duplicate", message))
        elif span in base_spans:
            named.add(span)
            restored.add(span)
        else:
            named.add(span)
            span_restores.append((index, span))

    for index, edit in enumerate(edits):
        if edit.op == "restore" and edit.replacement_index is not None:
            if edit.start is not None or edit.end is not None:
                message = "Name a replacement by its index or by its span, not both."
                errors.append(EditError(index, "replacement_index", "unexpected", message))
            elif not 0 <= edit.replacement_index < len(base):
                message = f"The CV text has {len(base)} replacements, numbered from 0."
                errors.append(EditError(index, "replacement_index", "unknown_replacement", message))
            else:
                target = base[edit.replacement_index]
                name(index, "replacement_index", (target.start, target.end))
            continue
        if edit.op == "restore" and edit.start is None and edit.end is None:
            message = "Name the replacement to restore."
            errors.append(EditError(index, "replacement_index", "missing", message))
            continue
        if edit.op == "remove" and edit.replacement_index is not None:
            message = "A removal is a span of the CV text, not a replacement."
            errors.append(EditError(index, "replacement_index", "unexpected", message))
            continue
        span = _span(index, edit, len(raw))
        if isinstance(span, EditError):
            errors.append(span)
        elif edit.op == "restore":
            name(index, "start", span)
        elif span in removed:
            errors.append(
                EditError(index, "start", "duplicate", "An earlier edit of the set removes this span.")
            )
        else:
            removed.add(span)
            removals.append((span, index))
    removals.sort()
    reach = 0  # the furthest end of the removals before this one, in the order of their starts
    for (start, end), index in removals:
        if start < reach:
            errors.append(EditError(index, "start", "overlap", "Removed spans may not overlap each other."))
        reach = max(reach, end)
    if span_restores:
        # The contacts inside the personal spans the set restores: replacements of their own, with no index.
        with_personal = apply_edits(raw, Edits(restored=frozenset(restored)))
        inner = {(r.start, r.end) for r in with_personal.replacements} - base_spans
        for index, span in span_restores:
            if span in inner:
                restored.add(span)
            else:
                message = "The span names no replacement of the CV text."
                errors.append(EditError(index, "start", "no_replacement", message))
    if errors:
        raise InvalidEditSet(errors)
    return apply_edits(raw, Edits(restored=frozenset(restored), removed=frozenset(removed)))
