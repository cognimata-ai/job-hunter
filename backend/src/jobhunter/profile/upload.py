"""CV upload and paste: the checks, the streaming multipart reader and the draft opener (§12.1; M2 Task 9).

**Checks** (pure, before anything is stored):
- `check_file(head, data, filename)` detects the type from the content and answers it, or raises
  `UploadRejected(reason)`. In this order: `too_large` (over `MAX_UPLOAD_BYTES`); `unknown_type` (no
  `%PDF-` within the first 1,024 bytes of `head`, and no ZIP signature `PK\\x03\\x04` at its start; a ZIP
  signature is looked at first); `extension_mismatch` (the name does not end in `.pdf` or `.docx` for the
  detected type, in any case). A DOCX whose bytes hold more central-directory signatures than
  `CENTRAL_RECORDS_MAX` is `docx_expansion` before any parsing (`_check_docx`); it is then read through the
  stdlib `zipfile`, which reads the ZIP central directory only and inflates nothing: an archive it cannot read
  is `docx_structure`; more than `MAX_DOCX_ENTRIES` entries, or declared expanded sizes over
  `MAX_DOCX_EXPANDED` in all, is `docx_expansion`; a `vbaProject.bin` anywhere (in any case) is
  `docx_macro`; no `[Content_Types].xml` or no `word/document.xml` (part names compared without case, as
  OPC does) is `docx_structure`. A PDF's
  structure is never parsed here: its encryption and page count are docparse's (plan Deviation 7), so no PDF
  parser runs in the networked api. The route runs `check_file` in a worker thread: it is pure, and the
  parse of a hostile central directory costs seconds of CPU that the event loop must not wait for;
- `check_paste(text)` answers a pasted text of `PASTE_MIN` to `PASTE_MAX` characters without its NUL
  characters (`profile.text.drop_nul`; counted without them), or raises `UploadRejected` with `too_short`,
  `too_long`, or `not_utf8` (a lone surrogate, which JSON can carry). `UploadRejected` lives in
  `profile.errors`, which the worker imports without FastAPI, and is re-exported here.

**Streaming** (`read_multipart_limited`): the request body goes from `request.stream()` straight into
`python_multipart.MultipartParser`, never through `request.form()` or `request.body()`, so the body is never
buffered whole. The file part's bytes are counted as they arrive; the part that would pass `limit` raises the
413 at once, so at most `limit` bytes of the file and the one message being parsed are ever held, and no
further message is read. The whole body is bounded too, at `limit + MULTIPART_OVERHEAD_BYTES` (413 again): an
ignored part is not kept, but neither is it read without end. A body that is no multipart form, has no
boundary, has no `file` part, more than one, or more than `MULTIPART_MAX_PARTS` parts, or ends early is a 422
`validation_failed` naming the field. `read_body_limited` is the same bound for a body that FastAPI parses
afterwards (the paste's JSON): a declared `Content-Length` over the limit is refused before any read, and a
body sent without one is counted as it streams.

**The draft** (`open_new_draft`, in the caller's transaction): the open draft is locked, refused (409) while
it is being extracted, otherwise discarded with every `failed` draft; then the `cv_files` row, the
`uploaded` draft and its operation (`request_draft_operation`). Two first uploads at once both see no open
draft, so the second meets the first's row in the `profile_drafts_one_open` index: that is the same 409.

The file is stored before this transaction (the route deletes it when the transaction fails; Task 23's
`cv_store_orphans` rule removes what a crash in between leaves). Nothing here logs; a name, a content or a
text never reaches a problem's message either.
"""

import zipfile
from collections.abc import Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from io import BytesIO
from typing import TYPE_CHECKING, Final, Literal

import sqlalchemy as sa
from pydantic import TypeAdapter, ValidationError
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.requests import Request

from jobhunter.api.errors import FieldError, Problem
from jobhunter.db.sql import is_unique_violation, q
from jobhunter.db.transitions import transition
from jobhunter.profile.errors import RejectReason as RejectReason
from jobhunter.profile.errors import UploadRejected as UploadRejected
from jobhunter.profile.text import drop_nul
from jobhunter.worker.operations import create_operation
from jobhunter.worker.tasks import Deferrer

if TYPE_CHECKING:
    # The parser's callback table is a TypedDict that the library defines for type checkers only.
    from python_multipart.multipart import MultipartCallbacks

MAX_UPLOAD_BYTES: Final = 10_485_760
MAX_DOCX_EXPANDED: Final = 50 * 2**20
MAX_DOCX_ENTRIES: Final = 1000
PASTE_MIN: Final = 300
PASTE_MAX: Final = 50_000
# Beyond the file's own bytes, a form holds the boundaries, the part headers and the opt-in's value.
MULTIPART_OVERHEAD_BYTES: Final = 64 * 1024
MULTIPART_MAX_PARTS: Final = 8
# The opt-in is a boolean: "true", "false", "1", "0" and their kin; longer is no boolean.
_FLAG_MAX_BYTES: Final = 16
# §12.1: `%PDF-` within the first 1,024 bytes; the route hands `check_file` that much as `head`.
PDF_MAGIC_WITHIN: Final = 1024
# The JSON body of a paste: 50,000 characters, each at most 12 bytes JSON-escaped (a character outside the
# BMP is two `\uXXXX` escapes), is about 600 KB; 1 MiB leaves room for whitespace and the member name.
PASTE_BODY_MAX_BYTES: Final = 2**20

PDF_MIME: Final = "application/pdf"
DOCX_MIME: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TEXT_MIME: Final = "text/plain"
Detected = Literal[
    "application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
]
CvFileMime = Detected | Literal["text/plain"]
DraftKind = Literal["extract_profile_text", "extract_profile"]

FILE_FIELD: Final = "file"
NATIVE_FIELD: Final = "native_pdf_opt_in"

_EXTENSIONS: Final[dict[Detected, str]] = {PDF_MIME: ".pdf", DOCX_MIME: ".docx"}
_REQUIRED_PARTS: Final = frozenset({"[content_types].xml", "word/document.xml"})
_MACRO_PART: Final = "vbaproject.bin"
# Every record `zipfile` parses starts with this signature, so the count of its occurrences bounds the records
# it would build before the entry check could refuse them: 200,000 minimal records fit in 10 MiB. The bound is
# four times the entry cap: a DOCX may hold embedded packages (a chart's workbook, an OLE object) stored
# uncompressed, whose own central directories count here too, and compressed data can hold the four bytes by
# chance (about once in 4 GiB). A document under the entry cap is refused by the count only when its stored
# embedded packages hold some 3,000 entries more, and the parse the count lets through stays at a few
# thousand records.
_CENTRAL_HEADER: Final = b"PK\x01\x02"
CENTRAL_RECORDS_MAX: Final = 4 * MAX_DOCX_ENTRIES
# What `zipfile` raises for an archive it cannot read: a damaged one (`BadZipFile`), a version or feature it
# does not support (`NotImplementedError`), a name that is not the UTF-8 its flag claims (`ValueError`).
_UNREADABLE_ZIP: Final = (zipfile.BadZipFile, NotImplementedError, ValueError)
_BOOL: Final = TypeAdapter(bool)

# The open states of §7.4's `profile_drafts_one_open`; the first three are being extracted.
_EXTRACTING: Final = ("uploaded", "extracting_text", "extracting_profile")
_REVIEWABLE: Final = ("text_review", "ready")
_ONE_OPEN_INDEX: Final = "profile_drafts_one_open"
_DISCARD_REASON: Final = "superseded"

_LOCK_OPEN = q(
    "SELECT id, status FROM profile_drafts "
    "WHERE status IN ('uploaded','extracting_text','text_review','extracting_profile','ready') FOR UPDATE"
)
_LOCK_FAILED = q("SELECT id FROM profile_drafts WHERE status = 'failed' ORDER BY id FOR UPDATE")
_INSERT_CV = q(
    "INSERT INTO cv_files (sha256, mime, size_bytes, storage_path, extraction_method, native_pdf_opt_in) "
    "VALUES (:sha256, :mime, :size_bytes, :storage_path, :extraction_method, :native) RETURNING id"
)
_SEAL_TEXT = q("UPDATE cv_files SET raw_text_enc = :enc WHERE id = :id")
_INSERT_DRAFT = q("INSERT INTO profile_drafts (cv_file_id, status) VALUES (:cv, 'uploaded') RETURNING id")


def _detect(head: bytes) -> Detected | None:
    if head.startswith(b"PK\x03\x04"):
        return DOCX_MIME
    if b"%PDF-" in head[:PDF_MAGIC_WITHIN]:
        return PDF_MIME
    return None


def _check_docx(data: bytes) -> None:
    if data.count(_CENTRAL_HEADER) > CENTRAL_RECORDS_MAX:
        raise UploadRejected("docx_expansion")
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
    except _UNREADABLE_ZIP:
        raise UploadRejected("docx_structure") from None
    if len(entries) > MAX_DOCX_ENTRIES or sum(entry.file_size for entry in entries) > MAX_DOCX_EXPANDED:
        raise UploadRejected("docx_expansion")
    names = {entry.filename.replace("\\", "/").lower() for entry in entries}
    if any(name.rsplit("/", 1)[-1] == _MACRO_PART for name in names):
        raise UploadRejected("docx_macro")
    if not _REQUIRED_PARTS <= names:
        raise UploadRejected("docx_structure")


def check_file(head: bytes, data: bytes, filename: str) -> Detected:
    """The type of an uploaded file, from `head` (its first bytes, at least 1,024 when it has them), the
    whole file `data` and the name the client gave; `UploadRejected` for a file §12.1 refuses."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadRejected("too_large")
    detected = _detect(head)
    if detected is None:
        raise UploadRejected("unknown_type")
    if not filename.lower().endswith(_EXTENSIONS[detected]):
        raise UploadRejected("extension_mismatch")
    if detected == DOCX_MIME:
        _check_docx(data)
    return detected


def check_paste(text: str) -> str:
    """`text` without its NUL characters (`profile.text.drop_nul`: PostgreSQL's `text` refuses them, Task 11)
    when that is a pasted CV §12.1 accepts: 300 to 50,000 characters of UTF-8."""
    text = drop_nul(text)
    if len(text) < PASTE_MIN:
        raise UploadRejected("too_short")
    if len(text) > PASTE_MAX:
        raise UploadRejected("too_long")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise UploadRejected("not_utf8") from None
    return text


def rejected_problem(
    exc: UploadRejected, *, field_name: str = FILE_FIELD, message: str | None = None
) -> Problem:
    """The problem of a rejection: 413 for `too_large`, else 422, `upload_rejected` with the reason; the
    field error's message is the reason's fixed text unless `message` (also fixed) replaces it."""
    status = 413 if exc.reason == "too_large" else 422
    text = REJECT_MESSAGES[exc.reason] if message is None else message
    return Problem(
        status,
        "upload_rejected",
        "Upload rejected",
        errors=[FieldError(field=field_name, code=exc.reason, message=text)],
    )


# The `message` of each reason's field error: fixed texts, never the file's name or content.
REJECT_MESSAGES: Final[dict[RejectReason, str]] = {
    "too_large": "The file is larger than 10 MiB",
    "unknown_type": "Only PDF and DOCX files are accepted",
    "extension_mismatch": "The file name's extension does not match its content",
    "docx_structure": "The DOCX file is damaged or incomplete",
    "docx_macro": "Macro-enabled documents are not accepted",
    "docx_expansion": "The DOCX file expands to more than 50 MiB or holds more than 1,000 entries",
    "too_short": "The text needs at least 300 characters",
    "too_long": "The text may have at most 50,000 characters",
    "not_utf8": "The text is not valid UTF-8",
}


# --- the streaming multipart reader ----------------------------------------------------------------------


@dataclass(frozen=True)
class UploadedFile:
    """The form of an upload: the file and the opt-in. The name is the client's and may name a person."""

    filename: str = field(repr=False)
    data: bytes = field(repr=False)
    native_pdf_opt_in: bool


def _invalid(field_name: str, code: str, message: str) -> Problem:
    return Problem(
        422,
        "validation_failed",
        "Request validation failed",
        errors=[FieldError(field=field_name, code=code, message=message)],
    )


def _too_large() -> Problem:
    return rejected_problem(UploadRejected("too_large"))


class _FormReader:
    """The parser's callbacks: they keep the file and the opt-in, and drop every other part's data."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.parts = 0
        self.ended = False
        self.file: bytearray | None = None
        self.filename = ""
        self.flag: bytearray | None = None
        self._target: bytearray | None = None
        self._header_name = bytearray()
        self._header_value = bytearray()
        self._disposition = b""

    def callbacks(self) -> "MultipartCallbacks":
        return {
            "on_part_begin": self.on_part_begin,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_headers_finished": self.on_headers_finished,
            "on_part_data": self.on_part_data,
            "on_part_end": self.on_part_end,
            "on_end": self.on_end,
        }

    def on_part_begin(self) -> None:
        self.parts += 1
        if self.parts > MULTIPART_MAX_PARTS:
            raise _invalid("body", "too_many_parts", f"A form has at most {MULTIPART_MAX_PARTS} parts")
        self._disposition = b""
        self._target = None

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_name += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def on_header_end(self) -> None:
        if self._header_name.strip().lower() == b"content-disposition":
            self._disposition = bytes(self._header_value)
        self._header_name.clear()
        self._header_value.clear()

    def on_headers_finished(self) -> None:
        kind, options = parse_options_header(self._disposition)
        name = options.get(b"name")
        if kind != b"form-data" or name is None:
            raise _invalid("body", "malformed_multipart", "Every part needs a form-data name")
        if name == FILE_FIELD.encode():
            if self.file is not None:
                raise _invalid(FILE_FIELD, "duplicate", "Send exactly one file")
            self.file = bytearray()
            # The header's bytes as sent (browsers send UTF-8); only its extension is ever looked at.
            self.filename = options.get(b"filename", b"").decode("utf-8", "replace")
            self._target = self.file
        elif name == NATIVE_FIELD.encode():
            if self.flag is not None:
                raise _invalid(NATIVE_FIELD, "duplicate", "Send the opt-in once")
            self.flag = bytearray()
            self._target = self.flag

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        target = self._target
        if target is None:
            return
        if target is self.file:
            if len(target) + (end - start) > self.limit:
                raise _too_large()
        elif len(target) + (end - start) > _FLAG_MAX_BYTES:
            raise _invalid(NATIVE_FIELD, "bool_parsing", "The opt-in must be true or false")
        target += data[start:end]

    def on_part_end(self) -> None:
        self._target = None

    def on_end(self) -> None:
        self.ended = True


def _opt_in(flag: bytearray | None) -> bool:
    if flag is None:
        return False
    try:
        return _BOOL.validate_python(bytes(flag).decode("utf-8"))
    except (UnicodeDecodeError, ValidationError):
        raise _invalid(NATIVE_FIELD, "bool_parsing", "The opt-in must be true or false") from None


async def read_multipart_limited(request: Request, *, limit: int) -> UploadedFile:
    """The `file` part and the `native_pdf_opt_in` field of a multipart form, read while streaming: the 413
    `upload_rejected` of a file over `limit` is raised as soon as its bytes pass it (module docstring)."""
    media_type, params = parse_options_header(request.headers.get("content-type"))
    if media_type != b"multipart/form-data":
        raise _invalid("body", "multipart_required", "The body must be multipart/form-data")
    boundary = params.get(b"boundary")
    if not boundary:
        raise _invalid("body", "boundary_missing", "The multipart body has no boundary")
    reader = _FormReader(limit)
    try:
        parser = MultipartParser(boundary, reader.callbacks())
        received, max_body = 0, limit + MULTIPART_OVERHEAD_BYTES
        async with aclosing(request.stream()) as chunks:
            async for chunk in chunks:
                received += len(chunk)
                if received > max_body:
                    raise _too_large()
                parser.write(chunk)
                if reader.ended:  # an epilogue after the closing boundary is not read
                    break
    except FormParserError:  # a malformed boundary, header or part (`MultipartParseError` among them)
        raise _invalid("body", "malformed_multipart", "The multipart body is malformed") from None
    if not reader.ended:
        raise _invalid("body", "malformed_multipart", "The multipart body ended before its closing boundary")
    if reader.file is None:
        raise _invalid(FILE_FIELD, "missing", "Field required")
    return UploadedFile(
        filename=reader.filename, data=bytes(reader.file), native_pdf_opt_in=_opt_in(reader.flag)
    )


def _body_too_large(field_name: str, limit: int) -> Problem:
    return rejected_problem(
        UploadRejected("too_large"),
        field_name=field_name,
        message=f"The request body is larger than {limit} bytes",
    )


async def read_body_limited(request: Request, *, limit: int, field_name: str) -> bytes:
    """The whole body of `request`, at most `limit` bytes, or the 413 `upload_rejected` (`too_large` on
    `field_name`): at once for a declared `Content-Length` over the limit, else as soon as the streamed bytes
    pass it, without reading further. A `Content-Length` that is no number is left to the stream count."""
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > limit:
        raise _body_too_large(field_name, limit)
    body = bytearray()
    async with aclosing(request.stream()) as chunks:
        async for chunk in chunks:
            if len(body) + len(chunk) > limit:
                raise _body_too_large(field_name, limit)
            body += chunk
    return bytes(body)


# --- the draft and its operation -------------------------------------------------------------------------


@dataclass(frozen=True)
class NewCvFile:
    """The `cv_files` row of an upload or a paste, its content already stored or ready to seal.

    An upload names its encrypted file (`storage_path`, `CvStore.write`). A paste (`text/plain`) has no file:
    `seal_text(cv_file_id)` gives its `raw_text_enc`, sealed for the id the insert assigns (its associated
    data names the row, `CvStore.encrypt_text`).
    """

    mime: CvFileMime
    sha256: bytes = field(repr=False)
    size_bytes: int
    storage_path: str | None = None
    seal_text: Callable[[int], bytes] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        pasted = self.mime == TEXT_MIME
        if pasted != (self.seal_text is not None) or pasted == (self.storage_path is not None):
            raise ValueError("a file has a storage path and no text to seal, a paste the reverse")


async def request_draft_operation(
    conn: AsyncConnection, kind: DraftKind, draft_id: int, *, defer: Deferrer | None
) -> tuple[int, bool]:
    """The live `kind` operation of draft `draft_id`, created with its job when there is none: payload
    `{"draft_id": <id>}`, dedup key `<kind>:<id>`. Returns `(operation_id, created)`."""
    return await create_operation(
        conn, kind=kind, payload={"draft_id": draft_id}, dedup_key=f"{kind}:{draft_id}", defer=defer
    )


def _conflict() -> Problem:
    return Problem(
        409,
        "conflict_state",
        "A CV is being read",
        detail="The previous upload is still being extracted; try again when it is done.",
    )


async def open_new_draft(
    conn: AsyncConnection, *, cv: NewCvFile, native_pdf: bool, defer: Deferrer | None
) -> int:
    """Open the draft of a new CV in the caller's transaction and return its id (module docstring).

    1. The open draft, if any, is locked `FOR UPDATE`; one in `uploaded`, `extracting_text` or
       `extracting_profile` is a 409 `conflict_state`.
    2. One in `text_review` or `ready` is discarded, then every `failed` draft (§7.4's one-open index does not
       cover `failed`, so there may be several), each with the reason `superseded`.
    3. The `cv_files` row: a paste is `extraction_method = 'pasted'` with its sealed text; a PDF opted into
       the native path is `native_pdf_opt_in` and `extraction_method = 'native_pdf'` (§7.4's CHECK).
    4. The draft, `uploaded`, and its operation: `extract_profile_text`, or for the native path
       `uploaded → extracting_profile` with `native_pdf := true` and `extract_profile` (whose `check_pdf` node
       validates the PDF in docparse before any token count or LLM call, Task 13).
    """
    if native_pdf and cv.mime != PDF_MIME:
        raise ValueError("only a PDF can be sent natively")
    open_draft = (await conn.execute(_LOCK_OPEN)).first()
    if open_draft is not None:
        if open_draft.status in _EXTRACTING:
            raise _conflict()
        await transition(
            conn, "profile_drafts", open_draft.id, _REVIEWABLE, "discarded", reason=_DISCARD_REASON
        )
    for failed in (await conn.execute(_LOCK_FAILED)).all():
        await transition(conn, "profile_drafts", failed.id, ["failed"], "discarded", reason=_DISCARD_REASON)
    pasted = cv.mime == TEXT_MIME
    cv_file_id = (
        await conn.execute(
            _INSERT_CV,
            {
                "sha256": cv.sha256,
                "mime": cv.mime,
                "size_bytes": cv.size_bytes,
                "storage_path": cv.storage_path,
                "extraction_method": "pasted" if pasted else "native_pdf" if native_pdf else None,
                "native": native_pdf,
            },
        )
    ).scalar_one()
    if cv.seal_text is not None:
        await conn.execute(_SEAL_TEXT, {"id": cv_file_id, "enc": cv.seal_text(cv_file_id)})
    try:
        draft_id = int((await conn.execute(_INSERT_DRAFT, {"cv": cv_file_id})).scalar_one())
    except sa.exc.IntegrityError as exc:
        # A concurrent first upload opened its draft first; the caller's transaction is rolled back.
        if is_unique_violation(exc, _ONE_OPEN_INDEX):
            raise _conflict() from None
        raise
    if native_pdf:
        await transition(
            conn, "profile_drafts", draft_id, ["uploaded"], "extracting_profile", set_={"native_pdf": True}
        )
    await request_draft_operation(
        conn, "extract_profile" if native_pdf else "extract_profile_text", draft_id, defer=defer
    )
    return draft_id
