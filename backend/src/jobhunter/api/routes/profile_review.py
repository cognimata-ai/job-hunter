"""`/api/profile`: the review of a draft (spec §12.3, §12.6, §12.7, §14.2, §15.7; M2 Task 14), beside the
upload and paste of `api/routes/profile.py` under the same prefix.

Every route needs a session, and the state-changing ones pass the edge's CSRF check; an unknown id is a 404
`not_found`, a draft in a state the route does not serve a 409 `conflict_state`. The pure rules are
`profile.review`'s; a draft's row is taken `FOR UPDATE` before its `cv_files` row, the order of the retention
rules.
- `GET /drafts/{id}` → `DraftOut`: the status, the profile (`draft`, a `ConfirmedProfile`), its issues, the
  strip counts, the stripped text (`stripped_text_preview`, never the raw text), the approved text's sha256
  (hex), the failure code, the §12.7 `repairs` of a failed draft (`review.repairs`, the retry route's own
  predicate; beyond the spec's summary, plan Deviation 3), and, beyond it too: the stored edit set (`edits`,
  ruling R14-1), the review map (`segments`, R14-2: in `text_review` and `ready`, rebuilt from the decrypted
  raw text with the stored set, offsets and categories only) and the years the roles give
  (`recomputed_total_years`, R14-3, with the injected clock). A stored profile that the current model refuses
  (a later release tightened it; final review FR-3) is shown as `draft: null` with one issue
  `schema_mismatch` at the path `""` and no value, `recomputed_total_years: null`, and `repairs` with
  `paste_text` (a new draft, which discards this one), so the client can discard it; a stored issue the
  current model refuses is left out. Neither quotes a stored value or the validation error.
- `PUT /drafts/{id}` (`{draft}`), in `ready` only: a string of the profile that holds a NUL character or a
  lone surrogate (a JSON body can escape both; no JSONB value or UTF-8 text holds them) is a 422
  `validation_failed` with a `FieldError` (`invalid`) at its path (`body.draft.roles[0].highlights[1]`),
  before anything is read; otherwise the profile is validated again (`validate_profile` against the approved
  text, against nothing for a native PDF, and `validate_user_entered`) and stored repaired with its issues by
  a plain `UPDATE`; → `{validation_issues}`.
- `PUT /drafts/{id}/text` (`{span_edits}`): the complete edit set (plan Deviation 29, `review.rebuild`; an
  invalid set is a 422 `validation_failed` naming each invalid edit), in `text_review` or `ready` (which moves
  `ready → text_review` first, `draft_json := NULL` and its issues emptied, §12.3). One transaction stores the
  rebuilt `stripped_text` with its counts and the set itself (`profile_drafts.text_edits`, offsets only);
  → `{stripped_text_preview}`. A draft without a raw text (a native PDF) has no text to edit: 409.
- `POST /drafts/{id}/text-review` (Idempotency-Key), in `text_review` only: `reviewed_text` := the stored
  stripped text and its sha256, `native_pdf := false` (the user approved text), `text_review →
  extracting_profile`, and the `extract_profile` operation (`upload.request_draft_operation`); → 202
  `{status: "extracting_profile"}`.
- `POST /drafts/{id}/retry` (Idempotency-Key, `{mode, ack_native_pdf?}`), from `failed` only: `native_pdf`
  without the acknowledgement is a 422 before anything is read; the draft `FOR UPDATE`, then its `cv_files`
  row `FOR SHARE`; a mode `review.retry_refusal` refuses is a 409 `conflict_state` with its detail, and
  nothing changes. Otherwise a native retry records the opt-in on the file (`native_pdf_opt_in`,
  `extraction_method = 'native_pdf'`) and the draft (`native_pdf := true`); `failed → extracting_profile`
  clears the failure and `content_purged_at`, and the operation is created; → 202.
- `POST /drafts/{id}/discard` → 204, from `text_review`, `ready` or `failed` (reason `user_discard`).
- `GET /api/profile` → `{active, versions}` (`ProfileVersionOut`, newest first): the versions the confirm
  (`api/routes/profile.py`, Task 21) writes, each with its profile, `null` once purged, and `null` with
  `purged: false` for a stored profile that the current model refuses (FR-3).
A repeated `Idempotency-Key` returns the stored response (`run_idempotent`); a refused request stores
nothing. Logs carry ids, codes and counts only; no response holds the raw text or a value that was stripped
or removed (R14-5).

**Offsets** (`SpanOut`, `SegmentOut`, `SpanEditIn`) are Unicode code points of the raw text, or of
`stripped_text_preview` for a segment's `stripped_*` pair, as Python indexes a `str`: not UTF-16 code units. A
JavaScript client converts them, for example by indexing `Array.from(text)` instead of `text`.

**A stored set that no longer applies.** The stored set's restore spans name replacements of the stripping
rules in force when it was saved (its removals are raw spans and always apply). Should the rules change, a
restore span may name no replacement any more: GET then gives `segments: []` (the rebuild no longer gives the
stored text, and the map would not index it), and resending the stored set answers 422 `no_replacement` on
those entries. GET still returns the stored `edits`; a client drops the entries the 422 names
(`body.span_edits.<i>`) and sends the rest again, which rebuilds the text and its map under the new rules.
"""

import asyncio
import hashlib
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date, datetime
from typing import Annotated, Any, Final, Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Header, Path
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from starlette.responses import JSONResponse, Response

from jobhunter.api.deps import get_config, get_data_keys, get_engine, require_session
from jobhunter.api.errors import FieldError, Problem
from jobhunter.api.openapi import BODY_LIMIT_RESPONSES, CSRF_RESPONSES, SESSION_RESPONSES
from jobhunter.api_idempotency import (
    IDEMPOTENCY_HEADER,
    StoredResponse,
    check_idempotency_key,
    request_fingerprint,
    run_idempotent,
)
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.crypto import DecryptError
from jobhunter.db.sql import q
from jobhunter.db.transitions import CLEAR_REASON, transition
from jobhunter.errors import InvariantViolation
from jobhunter.llm import bounds
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.observability.logging import get_logger
from jobhunter.profile import review, upload
from jobhunter.profile.contacts import Category, Edits, SegmentKind, StripResult, apply_edits
from jobhunter.profile.storage import CvStore
from jobhunter.profile.validate import (
    Issue,
    IssueCode,
    recompute_total_years,
    validate_profile,
    validate_user_entered,
)
from jobhunter.types import json_strings, unstorable
from jobhunter.worker.tasks import defer_in_tx

router = APIRouter(
    prefix="/api/profile", dependencies=[Depends(require_session)], responses=SESSION_RESPONSES
)
log = get_logger("jobhunter.api.profile_review")

APPROVE_ROUTE: Final = "profile.text_review"
RETRY_ROUTE: Final = "profile.retry"
DISCARD_REASON: Final = "user_discard"
# A complete edit set holds one entry per restored replacement and per removed span: a CV has some hundreds of
# replacements at the most.
MAX_SPAN_EDITS: Final = 1000
_BIGINT_MAX: Final = 2**63 - 1
_DRAFTS: Final = "profile_drafts"
_REVIEWABLE: Final = ("text_review", "ready")
SCHEMA_MISMATCH: Final = "schema_mismatch"
SCHEMA_MISMATCH_MESSAGE: Final = (
    "The stored profile no longer matches the profile schema; discard this draft and paste or upload the CV "
    "again."
)
_DISCARDABLE: Final = ("text_review", "ready", "failed")

DraftId = Annotated[int, Path(ge=1, le=_BIGINT_MAX, description="The id of a profile draft")]
DraftStatus = Literal[
    "uploaded",
    "extracting_text",
    "text_review",
    "extracting_profile",
    "ready",
    "failed",
    "confirmed",
    "discarded",
]

_NOT_FOUND_RESPONSE: Final[dict[int | str, dict[str, Any]]] = {
    404: {"description": "No such draft (`not_found`)"}
}
_IDEMPOTENT_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    400: {"description": "Idempotency-Key missing or malformed (`idempotency_key_required`)"},
    **_NOT_FOUND_RESPONSE,
    422: {
        "description": "Invalid body (`validation_failed`), or the key was used for another request "
        "(`idempotency_key_reused`)"
    },
    **CSRF_RESPONSES,
}

# The draft and its CV file as the status route shows them: the reviewed text's presence, never the text; the
# sealed raw text only where the review map needs it.
_DRAFT_VIEW = q(
    "SELECT d.status, d.draft_json, d.validation_issues, d.reviewed_text_sha256, d.failure_code, "
    "d.native_pdf, d.reviewed_text IS NOT NULL AS has_reviewed_text, "
    "d.content_purged_at IS NOT NULL AS content_purged, "
    "d.text_edits, d.cv_file_id, c.mime, (c.storage_path IS NOT NULL AND c.purged_at IS NULL) AS has_file, "
    "c.stripped_text, c.strip_report, "
    "CASE WHEN d.status IN ('text_review', 'ready') THEN c.raw_text_enc END AS raw_text_enc "
    "FROM profile_drafts d JOIN cv_files c ON c.id = d.cv_file_id WHERE d.id = :d"
)
_LOCK_DRAFT = q(
    "SELECT status, cv_file_id, failure_code, native_pdf, reviewed_text_sha256, "
    "reviewed_text IS NOT NULL AS has_reviewed_text, content_purged_at IS NOT NULL AS content_purged "
    "FROM profile_drafts WHERE id = :d FOR UPDATE"
)
_LOCK_DRAFT_TEXT = q("SELECT status, native_pdf, reviewed_text FROM profile_drafts WHERE id = :d FOR UPDATE")
_SHARE_CV = q(
    "SELECT mime, (storage_path IS NOT NULL AND purged_at IS NULL) AS has_file, stripped_text "
    "FROM cv_files WHERE id = :cv FOR SHARE"
)
_LOCK_CV_TEXT = q("SELECT raw_text_enc FROM cv_files WHERE id = :cv FOR UPDATE")
_STORE_TEXT = q(
    "UPDATE cv_files SET stripped_text = :stripped, strip_report = :report WHERE id = :cv", jsonb=["report"]
)
_STORE_EDITS = q(
    "UPDATE profile_drafts SET text_edits = :edits, updated_at = now() WHERE id = :d", jsonb=["edits"]
)
_STORE_DRAFT = q(
    "UPDATE profile_drafts SET draft_json = :draft, validation_issues = :issues, updated_at = now() "
    "WHERE id = :d AND status = 'ready'",
    jsonb=["draft", "issues"],
)
_OPT_IN = q("UPDATE cv_files SET native_pdf_opt_in = true, extraction_method = 'native_pdf' WHERE id = :cv")
_ACTIVE_VERSION = q("SELECT profile_version_id FROM active_config WHERE id = 1")
_VERSIONS = q(
    "SELECT id, version_no, confirmed_at, purged_at IS NOT NULL AS purged, profile_json "
    "FROM profile_versions ORDER BY version_no DESC"
)


# Said in every model that carries offsets, so that the OpenAPI document and the frontend types say it too.
_OFFSETS_NOTE: Final = (
    "Offsets are Unicode code points, not UTF-16 code units: a JavaScript client must convert them, for "
    "example with Array.from(text)."
)


class SpanOut(BaseModel):
    """A span of the raw text: `start` and `end` are offsets into it. Offsets are Unicode code points, not
    UTF-16 code units: a JavaScript client must convert them, for example with `Array.from(text)`."""

    start: int
    end: int


class TextEditsOut(BaseModel):
    """The draft's stored edit set (ruling R14-1): the replacements put back and the spans removed, as spans
    of the raw text. A client sends it back as `span_edits` (`restore` and `remove` by span) to keep it."""

    restored: list[SpanOut]
    removed: list[SpanOut]


class SegmentOut(BaseModel):
    """One run of `stripped_text_preview` and the raw span it stands for (ruling R14-2): `text` (the same
    characters), `replacement` (`category`; `replacement_index` into the fresh strip, `null` for a contact
    inside a restored personal span, which is restored by its span; `restorable` false for one that the edit
    set cannot name), or `removed`. Offsets are Unicode code points, not UTF-16 code units: a JavaScript
    client must convert them, for example with `Array.from(text)`."""

    kind: SegmentKind
    category: Category | None
    replacement_index: int | None
    restorable: bool
    stripped_start: int
    stripped_end: int
    raw_start: int
    raw_end: int


class ValidationIssueOut(BaseModel):
    """An issue of the profile (`profile.validate.Issue`): where, what kind, a fixed message, a proposal. The
    code `schema_mismatch` (path `""`, no proposal) says that the stored profile no longer matches the profile
    schema and is not shown."""

    path: str
    code: IssueCode | Literal["schema_mismatch"]
    message: str
    proposal: JsonValue = None


class DraftOut(BaseModel):
    status: DraftStatus
    draft: ConfirmedProfile | None
    validation_issues: list[ValidationIssueOut]
    strip_report: dict[str, int] | None
    stripped_text_preview: str | None = Field(
        description="The contact-stripped text, never the raw text; `segments` index it. " + _OFFSETS_NOTE
    )
    reviewed_text_sha256: str | None
    failure_code: str | None
    repairs: list[review.Repair]
    edits: TextEditsOut
    segments: list[SegmentOut]
    recomputed_total_years: float | None


class DraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft: ConfirmedProfile


class ValidationIssuesOut(BaseModel):
    validation_issues: list[ValidationIssueOut]


class SpanEditIn(BaseModel):
    """One edit of the set: `remove` with `start` and `end`, or `restore` with `replacement_index` (into the
    fresh strip) or with the replacement's `start` and `end`, offsets into the raw text. Offsets are Unicode
    code points, not UTF-16 code units: a JavaScript client must convert them, for example with
    `Array.from(text)`."""

    model_config = ConfigDict(extra="forbid", strict=True)

    op: Literal["remove", "restore"]
    start: int | None = None
    end: int | None = None
    replacement_index: int | None = None


class TextEditsIn(BaseModel):
    """The draft's complete edit set, never a delta (plan Deviation 29): an edit left out is undone."""

    model_config = ConfigDict(extra="forbid")

    span_edits: list[SpanEditIn] = Field(max_length=MAX_SPAN_EDITS)


class StrippedTextOut(BaseModel):
    stripped_text_preview: str = Field(
        description="The rebuilt contact-stripped text, never the raw text. " + _OFFSETS_NOTE
    )


class ExtractingOut(BaseModel):
    status: Literal["extracting_profile"]


class RetryIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mode: review.RetryMode
    ack_native_pdf: bool = False


class ProfileVersionOut(BaseModel):
    id: int
    version_no: int
    confirmed_at: datetime
    purged: bool
    profile: ConfirmedProfile | None


class ProfileListOut(BaseModel):
    active: ProfileVersionOut | None
    versions: list[ProfileVersionOut]


def _not_found() -> Problem:
    return Problem(404, "not_found", "Profile draft not found")


def _wrong_state(status: str, allowed: Sequence[str]) -> Problem:
    return Problem(
        409,
        "conflict_state",
        "The draft is not in a state that allows this",
        detail=f"The draft is {status}; this needs it {' or '.join(allowed)}.",
    )


def _no_text() -> Problem:
    return Problem(
        409,
        "conflict_state",
        "The draft has no CV text",
        detail="The CV text of this draft cannot be read (a PDF sent as it is has none); "
        "upload or paste it again.",
    )


def _stored_edits(doc: Any) -> Edits:
    """The `Edits` of a stored `text_edits` value (offset pairs)."""
    return Edits(
        restored=frozenset((int(s), int(e)) for s, e in doc["restored"]),
        removed=frozenset((int(s), int(e)) for s, e in doc["removed"]),
    )


def _edits_doc(edits: Edits) -> dict[str, list[list[int]]]:
    return {
        "restored": [[s, e] for s, e in sorted(edits.restored)],
        "removed": [[s, e] for s, e in sorted(edits.removed)],
    }


def _edits_out(edits: Edits) -> TextEditsOut:
    return TextEditsOut(
        restored=[SpanOut(start=s, end=e) for s, e in sorted(edits.restored)],
        removed=[SpanOut(start=s, end=e) for s, e in sorted(edits.removed)],
    )


def _failed(draft: Row[Any], cv: Row[Any]) -> review.FailedDraft:
    """The stored state the repairs and the retry read (`review.FailedDraft`): the draft's columns from
    `draft` and its file's from `cv`, the same names in `_DRAFT_VIEW` (one row for both) as in `_LOCK_DRAFT`
    and `_SHARE_CV`, so the status route and the retry route decide from the same columns."""
    return review.FailedDraft(
        failure_code=str(draft.failure_code),
        native_pdf=bool(draft.native_pdf),
        approved=draft.reviewed_text_sha256 is not None,
        has_reviewed_text=bool(draft.has_reviewed_text),
        content_purged=bool(draft.content_purged),
        mime=str(cv.mime),
        has_file=bool(cv.has_file),
    )


def _rebuild_stored(
    store: CvStore, sealed: bytes, keys: Sequence[bytes], cv_file_id: int, edits: Edits
) -> StripResult:
    """The stored text's rebuild for the review map: the raw text opened and stripped with the stored set
    (in a worker thread, so neither the decryption nor the patterns hold the event loop; the raw text stays
    here)."""
    return apply_edits(store.decrypt_text(sealed, keys, cv_file_id=cv_file_id), edits)


def _rebuild_edited(
    store: CvStore, sealed: bytes, keys: Sequence[bytes], cv_file_id: int, edits: Sequence[review.SpanEdit]
) -> StripResult:
    """The raw text opened and rebuilt with a requested set (`review.rebuild`), in a worker thread."""
    return review.rebuild(store.decrypt_text(sealed, keys, cv_file_id=cv_file_id), edits)


async def _review_map(
    row: Row[Any], edits: Edits, store: CvStore, keys: Sequence[bytes], draft_id: int
) -> list[SegmentOut]:
    """The segments of a draft in review, or `[]` (no raw text, a text that does not open, or a stored text
    the rebuild does not give, as after a change of the stripping rules: the map would not index it)."""
    if row.raw_text_enc is None or row.stripped_text is None:
        return []
    try:
        rebuilt = await asyncio.to_thread(
            _rebuild_stored, store, bytes(row.raw_text_enc), keys, int(row.cv_file_id), edits
        )
    except DecryptError as exc:
        log.warning("profile_cv_unreadable", draft_id=draft_id, error_class=type(exc).__name__)
        return []
    if rebuilt.stripped_text != row.stripped_text:
        log.warning("profile_review_map_stale", draft_id=draft_id)
        return []
    return [
        SegmentOut(
            kind=s.kind,
            category=s.category,
            replacement_index=s.replacement_index,
            restorable=s.restorable,
            stripped_start=s.stripped_start,
            stripped_end=s.stripped_end,
            raw_start=s.raw_start,
            raw_end=s.raw_end,
        )
        for s in rebuilt.segments
    ]


def _profile_or_none(stored: Any) -> ConfirmedProfile | None:
    """`stored` as a `ConfirmedProfile`, or `None` when the current model refuses it (FR-3). The validation
    error quotes the stored values, so it is dropped here and never logged or answered."""
    try:
        return ConfirmedProfile.model_validate(stored)
    except ValidationError:
        return None


def _stored_profile(
    stored: Any, stored_issues: Sequence[Any], draft_id: int
) -> tuple[ConfirmedProfile | None, list[ValidationIssueOut]]:
    """The draft's profile and issues as `GET` shows them (module docstring): a profile the current model
    refuses is `None` with the one `schema_mismatch` issue, and a stored issue it refuses is left out."""
    if stored is None:
        draft = None
    elif (draft := _profile_or_none(stored)) is None:
        log.warning("profile_draft_schema_mismatch", draft_id=draft_id)
        mismatch = ValidationIssueOut(path="", code=SCHEMA_MISMATCH, message=SCHEMA_MISMATCH_MESSAGE)
        return None, [mismatch]
    issues: list[ValidationIssueOut] = []
    for stored_issue in stored_issues:
        try:
            issues.append(ValidationIssueOut.model_validate(stored_issue))
        except ValidationError:
            continue
    if left_out := len(stored_issues) - len(issues):
        log.warning("profile_draft_issues_left_out", draft_id=draft_id, issues=left_out)
    return draft, issues


@router.get("/drafts/{draft_id}", response_model=DraftOut, responses=_NOT_FOUND_RESPONSE)
async def get_draft(
    draft_id: DraftId,
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    cfg: Annotated[AppConfig, Depends(get_config)],
    keys: Annotated[list[bytes], Depends(get_data_keys)],
) -> DraftOut:
    """The draft's status, its profile and issues, its stripped text with the edit set and the review map,
    and the repairs of a failed draft."""
    async with engine.connect() as conn:
        row = (await conn.execute(_DRAFT_VIEW, {"d": draft_id})).first()
    if row is None:
        raise _not_found()
    edits = _stored_edits(row.text_edits)
    segments = await _review_map(row, edits, CvStore(cfg.cv_store_dir), keys, draft_id)
    draft, issues = _stored_profile(row.draft_json, row.validation_issues, draft_id)
    repairs = (
        review.repairs(_failed(row, row), native_enabled=bounds.NATIVE_PDF_ENABLED)
        if row.status == "failed"
        else []
    )
    if draft is None and row.draft_json is not None and "paste_text" not in repairs:
        repairs = ["paste_text", *repairs]  # the schema mismatch: a new draft replaces this one
    sha = None if row.reviewed_text_sha256 is None else bytes(row.reviewed_text_sha256).hex()
    return DraftOut(
        status=row.status,
        draft=draft,
        validation_issues=issues,
        strip_report=row.strip_report,
        stripped_text_preview=row.stripped_text,
        reviewed_text_sha256=sha,
        failure_code=row.failure_code,
        repairs=repairs,
        edits=_edits_out(edits),
        segments=segments,
        recomputed_total_years=None if draft is None else recompute_total_years(draft.roles, utcnow().date()),
    )


def _unstorable(draft: ConfirmedProfile) -> list[FieldError]:
    """An `invalid` error at each string of `draft` that no JSONB value can hold (module docstring)."""
    return [
        FieldError(field=path, code="invalid", message=message)
        for path, text in json_strings(draft.model_dump(mode="json"), "body.draft")
        if (message := unstorable(text)) is not None
    ]


def _validated(
    draft: ConfirmedProfile, transmitted_text: str | None, today: date
) -> tuple[ConfirmedProfile, list[Issue]]:
    profile, issues = validate_profile(draft, transmitted_text=transmitted_text, today=today)
    return profile, [*issues, *validate_user_entered(profile.user_entered)]


@router.put(
    "/drafts/{draft_id}",
    response_model=ValidationIssuesOut,
    responses={
        **_NOT_FOUND_RESPONSE,
        409: {"description": "The draft is not ready (`conflict_state`)"},
        422: {"description": "Invalid body (`validation_failed`)"},
        **CSRF_RESPONSES,
        **BODY_LIMIT_RESPONSES,
    },
)
async def save_draft(
    draft_id: DraftId, body: DraftIn, engine: Annotated[AsyncEngine, Depends(get_engine)]
) -> ValidationIssuesOut:
    """Save the edited profile of a `ready` draft: validated again and stored repaired, with its issues."""
    if errors := _unstorable(body.draft):
        raise Problem(422, "validation_failed", "Request validation failed", errors=errors)
    today = utcnow().date()
    async with engine.begin() as conn:
        row = (await conn.execute(_LOCK_DRAFT_TEXT, {"d": draft_id})).first()
        if row is None:
            raise _not_found()
        if row.status != "ready":
            raise _wrong_state(row.status, ["ready"])
        transmitted = None if row.native_pdf else row.reviewed_text
        profile, issues = await asyncio.to_thread(_validated, body.draft, transmitted, today)
        stored = [asdict(issue) for issue in issues]
        await conn.execute(
            _STORE_DRAFT, {"d": draft_id, "draft": profile.model_dump(mode="json"), "issues": stored}
        )
    log.info("profile_draft_saved", draft_id=draft_id, issues=len(issues))
    return ValidationIssuesOut(validation_issues=[ValidationIssueOut.model_validate(i) for i in stored])


def _invalid_edits(exc: review.InvalidEditSet) -> Problem:
    return Problem(
        422,
        "validation_failed",
        "Request validation failed",
        errors=[
            FieldError(field=f"body.span_edits.{e.index}.{e.member}", code=e.code, message=e.message)
            for e in exc.errors
        ],
    )


@router.put(
    "/drafts/{draft_id}/text",
    response_model=StrippedTextOut,
    responses={
        **_NOT_FOUND_RESPONSE,
        409: {"description": "The draft is not in text review or ready, or has no text (`conflict_state`)"},
        422: {"description": "Invalid body or edit set (`validation_failed`)"},
        **CSRF_RESPONSES,
        **BODY_LIMIT_RESPONSES,
    },
)
async def edit_text(
    draft_id: DraftId,
    body: TextEditsIn,
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    cfg: Annotated[AppConfig, Depends(get_config)],
    keys: Annotated[list[bytes], Depends(get_data_keys)],
) -> StrippedTextOut:
    """Replace the draft's edit set and rebuild its stripped text; from `ready`, back to `text_review`."""
    edits = [
        review.SpanEdit(op=e.op, start=e.start, end=e.end, replacement_index=e.replacement_index)
        for e in body.span_edits
    ]
    store = CvStore(cfg.cv_store_dir)
    async with engine.begin() as conn:
        draft = (await conn.execute(_LOCK_DRAFT, {"d": draft_id})).first()
        if draft is None:
            raise _not_found()
        if draft.status not in _REVIEWABLE:
            raise _wrong_state(draft.status, _REVIEWABLE)
        cv_file_id = int(draft.cv_file_id)
        sealed = (await conn.execute(_LOCK_CV_TEXT, {"cv": cv_file_id})).scalar_one()
        if sealed is None:
            raise _no_text()
        try:
            result = await asyncio.to_thread(_rebuild_edited, store, bytes(sealed), keys, cv_file_id, edits)
        except DecryptError as exc:
            log.warning("profile_cv_unreadable", draft_id=draft_id, error_class=type(exc).__name__)
            raise _no_text() from None
        except review.InvalidEditSet as exc:
            codes = sorted({e.code for e in exc.errors})
            log.info("profile_text_edits_refused", draft_id=draft_id, errors=len(exc.errors), codes=codes)
            raise _invalid_edits(exc) from None
        if draft.status == "ready":  # §12.3: the extracted profile goes, and approving extracts again
            await transition(
                conn,
                _DRAFTS,
                draft_id,
                ["ready"],
                "text_review",
                reason=CLEAR_REASON,
                set_={"draft_json": None, "validation_issues": [], "updated_at": sa.func.now()},
            )
        report = dict(result.report)
        await conn.execute(
            _STORE_TEXT, {"cv": cv_file_id, "stripped": result.stripped_text, "report": report}
        )
        await conn.execute(_STORE_EDITS, {"d": draft_id, "edits": _edits_doc(result.edits)})
    log.info(
        "profile_text_edited",
        draft_id=draft_id,
        restored=len(result.edits.restored),
        removed=len(result.edits.removed),
        replacements=len(result.replacements),
        reopened=draft.status == "ready",
    )
    return StrippedTextOut(stripped_text_preview=result.stripped_text)


async def _new_extraction(conn: AsyncConnection, draft_id: int) -> int:
    """The `extract_profile` operation of the generation this transaction opens: always a new one, since the
    previous operation of the draft ended with its last state change (`OperationContext.finish_in`)."""
    op_id, created = await upload.request_draft_operation(
        conn, "extract_profile", draft_id, defer=defer_in_tx
    )
    if not created:
        raise InvariantViolation(f"profile draft {draft_id}: a live extract_profile outlived its generation")
    return op_id


def _extracting() -> StoredResponse:
    return StoredResponse(202, ExtractingOut(status="extracting_profile").model_dump(mode="json"))


@router.post(
    "/drafts/{draft_id}/text-review",
    status_code=202,
    response_model=ExtractingOut,
    responses={
        **_IDEMPOTENT_RESPONSES,
        409: {
            "description": "The draft is not in text review (`conflict_state`), or a request with this key "
            "is still running (`idempotency_in_progress`)"
        },
    },
)
async def approve_text(
    draft_id: DraftId,
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
) -> Response:
    """Approve the reviewed text: it is stored with its sha256 and the profile extraction starts (§12.3)."""
    key = check_idempotency_key(idempotency_key)
    started: list[tuple[int, int]] = []  # (operation id, characters) of a first request, logged after commit

    async def handler(conn: AsyncConnection) -> StoredResponse:
        draft = (await conn.execute(_LOCK_DRAFT, {"d": draft_id})).first()
        if draft is None:
            raise _not_found()
        if draft.status != "text_review":
            raise _wrong_state(draft.status, ["text_review"])
        reviewed = (await conn.execute(_SHARE_CV, {"cv": draft.cv_file_id})).one().stripped_text
        if reviewed is None:
            raise _no_text()
        await transition(
            conn,
            _DRAFTS,
            draft_id,
            ["text_review"],
            "extracting_profile",
            reason=CLEAR_REASON,
            set_={
                "reviewed_text": reviewed,
                "reviewed_text_sha256": hashlib.sha256(reviewed.encode("utf-8")).digest(),
                "native_pdf": False,
                "failure_code": None,
                "updated_at": sa.func.now(),
            },
        )
        started.append((await _new_extraction(conn, draft_id), len(reviewed)))
        return _extracting()

    async with engine.begin() as conn:
        stored = await run_idempotent(
            conn,
            route=APPROVE_ROUTE,
            key=key,
            fingerprint=request_fingerprint({}, {"draft_id": draft_id}),
            now=utcnow(),
            handler=handler,
        )
    for op_id, chars in started:
        log.info("profile_text_approved", draft_id=draft_id, operation_id=op_id, chars=chars)
    return JSONResponse(stored.body, status_code=stored.status)


@router.post(
    "/drafts/{draft_id}/retry",
    status_code=202,
    response_model=ExtractingOut,
    responses={
        **_IDEMPOTENT_RESPONSES,
        409: {
            "description": "The draft is not failed, the mode is not admitted for it or its input was purged "
            "(`conflict_state`), or a request with this key is still running (`idempotency_in_progress`)"
        },
        **BODY_LIMIT_RESPONSES,
    },
)
async def retry_draft(
    draft_id: DraftId,
    body: RetryIn,
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
) -> Response:
    """Repair a failed draft (§12.7): extract again from the same input (`llm`) or from the original PDF
    (`native_pdf`, with the disclosure acknowledged)."""
    key = check_idempotency_key(idempotency_key)
    if body.mode == "native_pdf" and not body.ack_native_pdf:
        raise Problem(
            422,
            "validation_failed",
            "Request validation failed",
            errors=[
                FieldError(
                    field="body.ack_native_pdf",
                    code="ack_required",
                    message="Sending the original PDF needs the acknowledgement of its disclosure",
                )
            ],
        )
    started: list[int] = []

    async def handler(conn: AsyncConnection) -> StoredResponse:
        draft = (await conn.execute(_LOCK_DRAFT, {"d": draft_id})).first()
        if draft is None:
            raise _not_found()
        if draft.status != "failed":
            raise _wrong_state(draft.status, ["failed"])
        cv = (await conn.execute(_SHARE_CV, {"cv": draft.cv_file_id})).one()
        state = _failed(draft, cv)
        refusal = review.retry_refusal(state, body.mode, native_enabled=bounds.NATIVE_PDF_ENABLED)
        if refusal is not None:
            log.info("profile_retry_refused", draft_id=draft_id, mode=body.mode, reason=refusal.reason)
            raise Problem(
                409, "conflict_state", "The draft cannot be retried this way", detail=refusal.detail
            )
        set_: dict[str, object] = {
            "failure_code": None,
            "content_purged_at": None,
            "updated_at": sa.func.now(),
        }
        if body.mode == "native_pdf":
            await conn.execute(_OPT_IN, {"cv": draft.cv_file_id})
            set_["native_pdf"] = True
        await transition(
            conn, _DRAFTS, draft_id, ["failed"], "extracting_profile", reason=CLEAR_REASON, set_=set_
        )
        started.append(await _new_extraction(conn, draft_id))
        return _extracting()

    async with engine.begin() as conn:
        stored = await run_idempotent(
            conn,
            route=RETRY_ROUTE,
            key=key,
            fingerprint=request_fingerprint(body.model_dump(mode="json"), {"draft_id": draft_id}),
            now=utcnow(),
            handler=handler,
        )
    for op_id in started:
        log.info("profile_draft_retried", draft_id=draft_id, operation_id=op_id, mode=body.mode)
    return JSONResponse(stored.body, status_code=stored.status)


@router.post(
    "/drafts/{draft_id}/discard",
    status_code=204,
    response_class=Response,
    responses={
        **_NOT_FOUND_RESPONSE,
        409: {"description": "The draft is not in text review, ready or failed (`conflict_state`)"},
        **CSRF_RESPONSES,
    },
)
async def discard_draft(draft_id: DraftId, engine: Annotated[AsyncEngine, Depends(get_engine)]) -> Response:
    """Discard a draft in text review, ready or failed."""
    async with engine.begin() as conn:
        draft = (await conn.execute(_LOCK_DRAFT, {"d": draft_id})).first()
        if draft is None:
            raise _not_found()
        if draft.status not in _DISCARDABLE:
            raise _wrong_state(draft.status, _DISCARDABLE)
        await transition(
            conn,
            _DRAFTS,
            draft_id,
            [draft.status],
            "discarded",
            reason=DISCARD_REASON,
            set_={"updated_at": sa.func.now()},
        )
    log.info("profile_draft_discarded", draft_id=draft_id, from_status=draft.status)
    return Response(status_code=204)


def _version_profile(version_id: int, stored: Any) -> ConfirmedProfile | None:
    """A version's profile, `None` once purged or when the current model refuses it (FR-3)."""
    if stored is None:
        return None
    profile = _profile_or_none(stored)
    if profile is None:
        log.warning("profile_version_schema_mismatch", version_id=version_id)
    return profile


@router.get("", response_model=ProfileListOut)
async def get_profile(engine: Annotated[AsyncEngine, Depends(get_engine)]) -> ProfileListOut:
    """The active profile version and every version, newest first (empty until a draft is confirmed)."""
    async with engine.connect() as conn:
        active_id = (await conn.execute(_ACTIVE_VERSION)).scalar_one_or_none()
        rows = (await conn.execute(_VERSIONS)).all()
    versions = [
        ProfileVersionOut(
            id=row.id,
            version_no=row.version_no,
            confirmed_at=row.confirmed_at,
            purged=row.purged,
            profile=_version_profile(row.id, row.profile_json),
        )
        for row in rows
    ]
    return ProfileListOut(active=next((v for v in versions if v.id == active_id), None), versions=versions)
