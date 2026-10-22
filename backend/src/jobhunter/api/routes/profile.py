"""`/api/profile`: CV upload and paste (spec §12.1, §14.2; M2 Task 9). The review of a draft is
`api/routes/profile_review.py`'s (Task 14).

`POST /api/profile/cv` (multipart: `file`, `native_pdf_opt_in?`) → 202 `{draft_id}`:
1. the form is read while streaming (`upload.read_multipart_limited`): a file over 10 MiB is a 413
   `upload_rejected` before anything past the limit is read; a malformed form is a 422 `validation_failed`;
2. a native opt-in while `bounds.NATIVE_PDF_ENABLED` is false is a 422 `validation_failed` on
   `native_pdf_opt_in` (plan Deviation 9; the flag is read per request); the file is checked in a worker
   thread, so a hostile archive's parse never holds the event loop (`upload.check_file`: 413 or 422
   `upload_rejected` with `errors=[{field: "file", code: <reason>}]`); an opt-in for a DOCX is a 422 on
   `native_pdf_opt_in`. Nothing is stored for any of them;
3. the file is written encrypted to the `cv_store` (`CvStore.write`), then one transaction opens the draft
   (`upload.open_new_draft`: 409 `conflict_state` while a draft is being extracted). An error before the
   transaction's body ended (it rolls back) deletes the file again before the error goes on. An error of the
   COMMIT itself leaves it: the commit may have happened, and a row would then name a missing file. A file no
   committed row names, as after a crash in between, is an orphan that Task 23's `cv_store_orphans` rule
   removes; so is one whose deletion fails (`cv_file_delete_failed`), which never hides the first error.

`POST /api/profile/cv-text` (`{text}`) → 202 `{draft_id}`: its body is read with a 1 MiB cap before FastAPI
parses it (`_CappedBodyRoute`: 413 `upload_rejected`, `too_large` on `text`); the text is checked
(`upload.check_paste`: 422 `upload_rejected` on `text`) and stored sealed in its `cv_files` row
(`raw_text_enc`, `extraction_method = 'pasted'`), never as a file; the same transaction opens the draft.

Both need a session and pass the edge's CSRF check. Logs carry ids, sizes and reason codes only: never a file
name, a file's content or a pasted text.
"""

import asyncio
import hashlib
from collections.abc import Callable, Coroutine
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Request
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.responses import Response
from starlette.types import Message

from jobhunter.api.deps import get_config, get_data_keys, get_engine, require_session
from jobhunter.api.errors import FieldError, Problem
from jobhunter.api.openapi import CSRF_RESPONSES, SESSION_RESPONSES
from jobhunter.config import AppConfig
from jobhunter.llm import bounds
from jobhunter.observability.logging import get_logger
from jobhunter.profile import upload
from jobhunter.profile.storage import CvStore
from jobhunter.profile.text import drop_nul
from jobhunter.worker.tasks import defer_in_tx

router = APIRouter(
    prefix="/api/profile", dependencies=[Depends(require_session)], responses=SESSION_RESPONSES
)
log = get_logger("jobhunter.api.profile")

_TEXT_FIELD: Final = "text"
_CONFLICT_RESPONSE: Final[dict[int | str, dict[str, Any]]] = {
    409: {"description": "A draft is being extracted (`conflict_state`)"}
}
# The form, which the route reads itself (streaming), described for the OpenAPI document and the frontend.
_CV_FORM: Final[dict[str, Any]] = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {
                        "file": {
                            "type": "string",
                            "format": "binary",
                            "description": "A PDF or DOCX, ≤ 10 MiB",
                        },
                        "native_pdf_opt_in": {
                            "type": "boolean",
                            "default": False,
                            "description": "Send the original PDF to the model instead of its text (§12.7)",
                        },
                    },
                }
            }
        },
    }
}


class DraftCreatedOut(BaseModel):
    draft_id: int


class CvTextIn(BaseModel):
    text: str


def _native_refused(code: str, message: str) -> Problem:
    return Problem(
        422,
        "validation_failed",
        "Request validation failed",
        errors=[FieldError(field=upload.NATIVE_FIELD, code=code, message=message)],
    )


def _delete_quietly(store: CvStore, storage_path: str) -> None:
    """Delete a file no row names; a failure is logged (its class only) and leaves an orphan for Task 23's
    `cv_store_orphans` rule, so the error that led here is the one that goes on."""
    try:
        store.delete(storage_path)
    except OSError as exc:
        log.error("cv_file_delete_failed", error_class=type(exc).__name__)


def _replayed(request: Request, body: bytes) -> Request:
    """A request like `request` whose body, already read, is `body`; later receives (a disconnect) pass on."""
    delivered = False

    async def receive() -> Message:
        nonlocal delivered
        if delivered:
            return await request.receive()
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(request.scope, receive)


class _CappedBodyRoute(APIRoute):
    """A route whose body is read with the paste's cap (`upload.read_body_limited`) before FastAPI parses it.

    FastAPI reads a JSON body whole, and turns any error of that read into a 400, so the cap runs first and
    the body read under it is replayed to FastAPI's own handler.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def capped(request: Request) -> Response:
            try:
                body = await upload.read_body_limited(
                    request, limit=upload.PASTE_BODY_MAX_BYTES, field_name=_TEXT_FIELD
                )
            except Problem as problem:
                log.info("cv_upload_refused", status=problem.status, code=problem.code)
                raise
            return await handler(_replayed(request, body))

        return capped


def _rejected(exc: upload.UploadRejected, *, field_name: str, **measures: int) -> Problem:
    """The problem of a refused file or text, logged with its reason and size (`size_bytes` or `chars`)."""
    log.info("cv_upload_rejected", reason=exc.reason, field=field_name, **measures)
    return upload.rejected_problem(exc, field_name=field_name)


async def _accepted_form(request: Request) -> tuple[upload.UploadedFile, upload.Detected]:
    """The form of an upload whose file passed every check, and its type; a `Problem` for any other."""
    try:
        form = await upload.read_multipart_limited(request, limit=upload.MAX_UPLOAD_BYTES)
    except Problem as problem:
        log.info("cv_upload_refused", status=problem.status, code=problem.code)
        raise
    if form.native_pdf_opt_in and not bounds.NATIVE_PDF_ENABLED:
        raise _native_refused("native_pdf_disabled", "Sending the original PDF is not enabled")
    try:
        # Pure, and seconds of CPU for a hostile central directory: off the event loop.
        detected = await asyncio.to_thread(
            upload.check_file, form.data[: upload.PDF_MAGIC_WITHIN], form.data, form.filename
        )
    except upload.UploadRejected as exc:
        raise _rejected(exc, field_name=upload.FILE_FIELD, size_bytes=len(form.data)) from None
    if form.native_pdf_opt_in and detected != upload.PDF_MIME:
        raise _native_refused("pdf_only", "Only a PDF can be sent as the original file")
    return form, detected


@router.post(
    "/cv",
    status_code=202,
    response_model=DraftCreatedOut,
    openapi_extra=_CV_FORM,
    responses={
        **_CONFLICT_RESPONSE,
        413: {"description": "The file is larger than 10 MiB (`upload_rejected`, `too_large`)"},
        422: {"description": "A malformed form (`validation_failed`) or a refused file (`upload_rejected`)"},
        **CSRF_RESPONSES,
    },
)
async def upload_cv(
    request: Request,
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    cfg: Annotated[AppConfig, Depends(get_config)],
    keys: Annotated[list[bytes], Depends(get_data_keys)],
) -> DraftCreatedOut:
    """Upload a CV file: 202 with the new draft, whose text extraction (or native extraction) is queued."""
    form, detected = await _accepted_form(request)
    store = CvStore(cfg.cv_store_dir)
    stored = store.write(form.data, keys)
    cv = upload.NewCvFile(
        mime=detected, sha256=stored.sha256, size_bytes=len(form.data), storage_path=stored.storage_path
    )
    committing = False
    try:
        async with engine.begin() as conn:
            draft_id = await upload.open_new_draft(
                conn, cv=cv, native_pdf=form.native_pdf_opt_in, defer=defer_in_tx
            )
            committing = True  # the last statement: what fails from here on is the COMMIT
    except BaseException as exc:
        if committing:
            # The outcome is unknown: a committed row may name the file, so it stays (an orphan otherwise).
            log.warning("cv_upload_commit_failed", error_class=type(exc).__name__)
        else:
            _delete_quietly(store, stored.storage_path)  # rolled back: nothing names it
        raise
    log.info(
        "cv_uploaded",
        draft_id=draft_id,
        mime=detected,
        size_bytes=len(form.data),
        native_pdf=form.native_pdf_opt_in,
    )
    return DraftCreatedOut(draft_id=draft_id)


async def paste_cv(
    body: CvTextIn,
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    cfg: Annotated[AppConfig, Depends(get_config)],
    keys: Annotated[list[bytes], Depends(get_data_keys)],
) -> DraftCreatedOut:
    """Paste a CV's text: 202 with the new draft; the text is stored sealed in its row, never as a file."""
    try:
        text = upload.check_paste(body.text)
    except upload.UploadRejected as exc:
        # the length `check_paste` measured: without the NUL characters it drops
        raise _rejected(exc, field_name=_TEXT_FIELD, chars=len(drop_nul(body.text))) from None
    utf8 = text.encode("utf-8")
    store = CvStore(cfg.cv_store_dir)
    cv = upload.NewCvFile(
        mime=upload.TEXT_MIME,
        sha256=hashlib.sha256(utf8).digest(),
        size_bytes=len(utf8),
        seal_text=lambda cv_file_id: store.encrypt_text(text, keys, cv_file_id=cv_file_id),
    )
    async with engine.begin() as conn:
        draft_id = await upload.open_new_draft(conn, cv=cv, native_pdf=False, defer=defer_in_tx)
    log.info("cv_pasted", draft_id=draft_id, size_bytes=len(utf8))
    return DraftCreatedOut(draft_id=draft_id)


# `router.post` takes no route class, so the paste route is added with the capped one.
router.add_api_route(
    "/cv-text",
    paste_cv,
    methods=["POST"],
    status_code=202,
    response_model=DraftCreatedOut,
    responses={
        **_CONFLICT_RESPONSE,
        413: {"description": "The body is larger than 1 MiB (`upload_rejected`, `too_large`)"},
        422: {"description": "An invalid body (`validation_failed`) or a refused text (`upload_rejected`)"},
        **CSRF_RESPONSES,
    },
    route_class_override=_CappedBodyRoute,
)
