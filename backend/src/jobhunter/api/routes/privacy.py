"""`/api/privacy`: the export, delete everything, the Adzuna purge and the deletion manifests (spec
§20.3-§20.5, §19.5, §14.2; plan M5 Task 18, M5-U4, D13, D18, Deviations 3, 21 and 22, Rulings T15-4 and
T18-1), for a logged-in session.

The edge lets every request under `/api/privacy/` through delete-everything's `global` fence
(`edge.fence_exempt`): each request refuses it itself where it must (the export's and the source purge's 409
`delete_everything_in_progress`, a second delete-everything's 409 `purge_in_progress`).

**The keyed requests** (`POST /export`, `/delete-everything`, `/purge-source`) take an `Idempotency-Key`
(§14.1) and share one key space, `PRIVACY_ROUTE`: a key is one user action, so a key that answered one of
them, sent with another, is 422 `idempotency_key_reused`. The two destructive ones run, in this order:

1. the body's validation (422 `validation_failed`; a password that is no valid Unicode text, a code that is
   not six digits, an unknown field), and the key's form (400 `idempotency_key_required`);
2. the phrase: `confirm` exactly `"DELETE EVERYTHING"` or `"PURGE ADZUNA"`, else 422 `confirmation_mismatch`
   (on `body.confirm`); nothing is recorded;
3. the action's fingerprint, `request_fingerprint` of the body without `password` and `totp`: a credential is
   no part of the action, so a retry with a new code is the same request;
4. `api_idempotency.stored_response`: a completed request with this key and fingerprint is answered at once
   with its stored 202, without a second re-authentication (the first one was authenticated, and this one
   carries a session too), so a lost response can be retried after its TOTP step was used, also after
   delete-everything logged the user out (the purge keeps its idempotency row); another fingerprint is 422
   `idempotency_key_reused`;
5. the re-authentication (`api.reauth.verify_reauth`): 403 `reauth_failed`, its failed attempt committed, or
   429 `throttled` while the address is locked out;
6. one transaction: `active_config FOR UPDATE` (A0's lock, before the key's row), then `run_idempotent` with
   the same fingerprint around A0 (`request_delete_everything`, which is given the id of the key's own row,
   claimed in this transaction, and keeps it; `request_source_purge`, whose cancelled collections are
   observed after the commit). A refusal rolls everything back, the key's row included. Of two first
   requests at once, the second waits for the first's key and gets its stored 202, or 409
   `idempotency_in_progress`.

The source purge's refusal when Adzuna is the only enabled source is the settings rule's 422
`validation_failed` on `payload.sources.enabled` (Ruling T15-4 (3)), which reaches the client as it is.

**The export** (`POST /export`, no body or `{}`): one transaction, `active_config FOR SHARE` (the request's
first lock) before the key's row, then `privacy.export.request_export` inside `run_idempotent`, so its
refusal (409 `export_running`, `delete_everything_in_progress`) rolls the new `running` row back with the
key's: no `running` export is left without its operation. `GET /exports/{id}` is the export's state; with
`?download=1` on a `ready` export, its zip (`application/zip`, `Content-Disposition: attachment;
filename="jobhunter-export-<id>.zip"`, `Content-Length` the zip's size): the stored file of the exports
directory (`cfg.exports_dir`, which the api reads from its configuration, never from the worker's
`export.configure`) is opened and its first segment decrypted (`export.iter_decrypted`) before the response
starts, so a file gone meanwhile (a purge's export op or the expiry, between the status read and the open)
is answered from the row read again: `expired` or `purged` is 410 `gone`, anything else (a `ready` row whose
file is missing) a 500. Once the 200 has started, the stream reads from the open file, so an unlink cannot
truncate a download under way. A download is logged `export_downloaded` with the id. `running` or `failed`
is 409 `conflict_state`, `expired` or `purged` 410 `gone`, an unknown id 404.

**The manifests** (D18): `GET /manifests?cursor&limit` → `{items, next_cursor}`, newest first, keyset-paged
with §14.3's signed cursor (sort `MANIFESTS_SORT`, `desc`, `last_key = [id]`, no filters), limit 1 to 100
(default 50); `GET /manifests/{id}` one of them. A deletion is reported done only at `shipped`, which the
`status` and `shipped_at` show; `deleted_golden_case_ids` lists the golden cases a source purge's recorded
content deletes (empty for another kind, and before the content is recorded). Every manifest survives
delete-everything.

`utcnow` is a name of this module, so a test injects the clock by patching
`jobhunter.api.routes.privacy.utcnow`. Ids, statuses and the client address only reach a log line.
"""

import asyncio
import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, BinaryIO, Final, Literal

from fastapi import APIRouter, Body, Depends, Header, Query, Request
from fastapi import Path as PathParam
from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from starlette.responses import JSONResponse, Response, StreamingResponse

from jobhunter.api.argon2_gate import Argon2Gate
from jobhunter.api.deps import get_argon2_gate, get_config, get_data_keys, get_engine, require_session
from jobhunter.api.errors import FieldError, Problem
from jobhunter.api.models import UtcDatetime, clean_text
from jobhunter.api.openapi import BODY_LIMIT_RESPONSES, CSRF_RESPONSES, SESSION_RESPONSES
from jobhunter.api.pagination import MANIFESTS_SORT, cursor_key, decode_id_cursor, list_page
from jobhunter.api.reauth import verify_reauth
from jobhunter.api.routes.auth import client_ip
from jobhunter.api_idempotency import (
    IDEMPOTENCY_HEADER,
    StoredResponse,
    check_idempotency_key,
    request_fingerprint,
    run_idempotent,
    stored_response,
)
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.db.sql import q
from jobhunter.observability.logging import get_logger
from jobhunter.privacy.export import iter_decrypted, request_export
from jobhunter.privacy.kinds.delete_everything import request_delete_everything
from jobhunter.privacy.kinds.source_purge import request_source_purge
from jobhunter.privacy.manifests import ManifestKind, ManifestStatus
from jobhunter.worker.runs import RunEnd, observe_run_ends
from jobhunter.worker.tasks import defer_in_tx

router = APIRouter(
    prefix="/api/privacy", dependencies=[Depends(require_session)], responses=SESSION_RESPONSES
)

# The one key space of the three keyed requests (module docstring).
PRIVACY_ROUTE: Final = "privacy"
DELETE_EVERYTHING_PHRASE: Final = "DELETE EVERYTHING"
PURGE_ADZUNA_PHRASE: Final = "PURGE ADZUNA"
# What no fingerprint holds: a credential is no part of the action.
CREDENTIALS: Final = frozenset({"password", "totp"})
DOWNLOAD_MEDIA_TYPE: Final = "application/zip"
GONE_STATES: Final = frozenset({"expired", "purged"})
DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 100
_BIGINT_MAX: Final = 2**63 - 1
_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
# The golden cases a source purge's content deletes: the ids of its `row_delete golden_cases` operations.
_DELETED_CASES_PATH: Final = '$.operations[*] ? (@.op == "row_delete" && @.table == "golden_cases").ids[*]'

_CONFIG_FOR_SHARE: Final = q("SELECT 1 FROM active_config WHERE id = 1 FOR SHARE")
_CONFIG_FOR_UPDATE: Final = q("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE")
_OWN_KEY_ROW: Final = q("SELECT id FROM api_idempotency WHERE route = :route AND idem_key = :key")
_EXPORT: Final = q(
    "SELECT id, status, created_at, expires_at, size_bytes, storage_path FROM exports WHERE id = :e"
)
_MANIFEST_COLUMNS: Final = (
    "SELECT id, kind, status, created_at, completed_at, shipped_at, CASE WHEN kind = 'source_purge' "
    "THEN jsonb_path_query_array(scope, CAST(:cases AS jsonpath)) ELSE CAST('[]' AS jsonb) END AS cases "
    "FROM deletion_manifests "
)
_MANIFEST: Final = q(_MANIFEST_COLUMNS + "WHERE id = :id")
_MANIFESTS: Final = q(
    _MANIFEST_COLUMNS
    + "WHERE (CAST(:before AS bigint) IS NULL OR id < :before) ORDER BY id DESC LIMIT :limit"
)

log = get_logger("jobhunter.api.privacy")

ExportStatus = Literal["running", "ready", "failed", "expired", "purged"]
ExportId = Annotated[int, PathParam(ge=1, le=_BIGINT_MAX, description="The id of an export")]
ManifestId = Annotated[int, PathParam(ge=1, le=_BIGINT_MAX, description="The id of a deletion manifest")]
Password = Annotated[
    str,
    StringConstraints(min_length=1, max_length=1024),
    AfterValidator(clean_text),
    Field(description="The user's password"),
]
Confirm = Annotated[str, StringConstraints(max_length=64)]


def _no_code(value: object) -> object:
    # Only the empty string: a code of any other shape than six digits stays a validation error (422).
    return None if value == "" else value


TotpCode = Annotated[
    Annotated[str, StringConstraints(pattern=r"^\d{6}$")] | None,
    BeforeValidator(_no_code),
    Field(
        description="The six-digit code of the authenticator app, needed when TOTP is on; an empty string "
        "counts as no code",
    ),
]


# ---- the models --------------------------------------------------------------------------------------------


class ExportIn(BaseModel):
    """An export request has nothing to say: no body, or an empty object."""

    model_config = ConfigDict(extra="forbid")


class ExportQueuedOut(BaseModel):
    export_id: int


class ExportOut(BaseModel):
    """An export: its state, when it was requested and until when it can be downloaded, and the size of its
    zip once it is ready."""

    id: int
    status: ExportStatus
    created_at: UtcDatetime
    expires_at: UtcDatetime
    size_bytes: int | None = Field(description="The size of the zip in bytes; null until the export is ready")


class DeleteEverythingIn(BaseModel):
    """Delete everything: the phrase `DELETE EVERYTHING`, the password and, with TOTP on, a code."""

    model_config = ConfigDict(extra="forbid")

    confirm: Confirm = Field(description="Exactly `DELETE EVERYTHING`")
    password: Password
    totp: TotpCode = None


class PurgeSourceIn(BaseModel):
    """Purge a source's data: the source, the phrase `PURGE ADZUNA`, the password and, with TOTP on, a
    code."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["adzuna"]
    confirm: Confirm = Field(description="Exactly `PURGE ADZUNA`")
    password: Password
    totp: TotpCode = None


class ManifestQueuedOut(BaseModel):
    """The deletion manifest the request opened; the worker carries the deletion out."""

    manifest_id: int


class ManifestOut(BaseModel):
    """A deletion: its kind and state (done only once `shipped`, its record confirmed offsite), when it was
    requested, completed and shipped, and the golden cases a source purge deletes."""

    id: int
    kind: ManifestKind
    status: ManifestStatus
    created_at: UtcDatetime
    completed_at: UtcDatetime | None
    shipped_at: UtcDatetime | None
    deleted_golden_case_ids: list[int] = Field(
        description="The golden cases a source purge deletes, once it has computed what it deletes; empty "
        "for any other kind"
    )


class ManifestsPageOut(BaseModel):
    items: list[ManifestOut]
    next_cursor: str | None


# ---- the shared steps --------------------------------------------------------------------------------------


def _json(response: StoredResponse) -> Response:
    return JSONResponse(response.body, status_code=response.status)


def _confirmed(confirm: str, phrase: str) -> None:
    """Step 2: 422 `confirmation_mismatch` unless `confirm` is exactly `phrase`."""
    if confirm != phrase:
        raise Problem(
            422,
            "confirmation_mismatch",
            "The confirmation phrase does not match",
            errors=[FieldError("body.confirm", "confirmation_mismatch", f"type exactly: {phrase}")],
        )


def _fingerprint(body: BaseModel) -> bytes:
    """Step 3: the request's fingerprint, without its credentials."""
    return request_fingerprint(body.model_dump(mode="json", exclude=set(CREDENTIALS)), {})


async def _answered(
    engine: AsyncEngine, key: str, fingerprint: bytes, now: datetime
) -> StoredResponse | None:
    """Step 4: the stored answer of a completed request with this key, or `None`."""
    async with engine.connect() as conn:  # a plain read, rolled back when the block ends
        return await stored_response(conn, route=PRIVACY_ROUTE, key=key, fingerprint=fingerprint, now=now)


_KEYED_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    400: {"description": "Idempotency-Key missing or malformed (`idempotency_key_required`)"},
    **BODY_LIMIT_RESPONSES,
}
_REAUTH_403: Final = (
    "Origin or `X-JH-Request` check failed (`csrf_failed`), or the password or the TOTP code is wrong "
    "(`reauth_failed`, counted as a failed login)"
)
_REAUTH_429: Final = (
    "Too many requests in this session, or too many failed logins from this address or in all "
    "(`throttled`, with `retry_after_s`)"
)


# ---- the export --------------------------------------------------------------------------------------------


@router.post(
    "/export",
    status_code=202,
    response_model=ExportQueuedOut,
    responses={
        **_KEYED_RESPONSES,
        **CSRF_RESPONSES,
        409: {
            "description": "An export is already running (`conflict_state`, detail `export_running`), "
            "everything is being deleted (`delete_everything_in_progress`), or a request with this key is "
            "still running (`idempotency_in_progress`)"
        },
        422: {
            "description": "A body other than an empty object (`validation_failed`), or the key was used for "
            "another request (`idempotency_key_reused`)"
        },
    },
)
async def post_export(
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    body: Annotated[ExportIn | None, Body()] = None,
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
) -> Response:
    """Export your data (202): a zip of your profile versions, CV files, settings, jobs, scores, feedback,
    golden cases and runs, encrypted at rest and available for 7 days."""
    key = check_idempotency_key(idempotency_key)
    now = utcnow()

    async def handler(conn: AsyncConnection) -> StoredResponse:
        export_id = await request_export(conn, now=now, defer=defer_in_tx)
        return StoredResponse(202, ExportQueuedOut(export_id=export_id).model_dump(mode="json"))

    async with engine.begin() as conn:
        await conn.execute(_CONFIG_FOR_SHARE)  # the request's first lock, before the key's row
        stored = await run_idempotent(
            conn,
            route=PRIVACY_ROUTE,
            key=key,
            fingerprint=request_fingerprint({}, {}),
            now=now,
            handler=handler,
        )
    return _json(stored)


@dataclass(frozen=True)
class ExportRow:
    """An `exports` row as the download reads it."""

    id: int
    status: ExportStatus
    created_at: datetime
    expires_at: datetime
    size_bytes: int | None
    storage_path: str | None


async def read_export(engine: AsyncEngine, export_id: int) -> ExportRow | None:
    """The export `export_id`, a plain read on a connection of its own; `None` when there is none."""
    async with engine.connect() as conn:
        row = (await conn.execute(_EXPORT, {"e": export_id})).first()
    if row is None:
        return None
    return ExportRow(
        id=int(row.id),
        status=row.status,
        created_at=row.created_at,
        expires_at=row.expires_at,
        size_bytes=None if row.size_bytes is None else int(row.size_bytes),
        storage_path=row.storage_path,
    )


def _gone(export_id: int, status: str) -> Problem:
    log.info("export_download_refused", export_id=export_id, status=status)
    return Problem(410, "gone", "The export is no longer available", detail=status)


def _stream(source: BinaryIO, first: bytes, rest: Iterator[bytes]) -> Iterator[bytes]:
    """The zip: the segment decrypted before the response started, then the others, read from the open file,
    which is closed at the end."""
    try:
        yield first
        yield from rest
    finally:
        source.close()


def _open_archive(path: Path, keys: Sequence[bytes], export_id: int) -> Iterator[bytes]:
    """The stored archive opened and its first segment decrypted (before the response starts, module
    docstring), as the stream of its plaintext. `FileNotFoundError` for a file that is gone."""
    source = os.fdopen(os.open(path, _READ_FLAGS), "rb")
    try:
        segments = iter_decrypted(source, keys, export_id=export_id)
        first = next(segments)
    except BaseException:
        source.close()
        raise
    return _stream(source, first, segments)


async def _download(
    engine: AsyncEngine, cfg: AppConfig, keys: Sequence[bytes], export: ExportRow
) -> Response:
    """The zip of a `ready` export (module docstring); 409 or 410 for any other state."""
    if export.status in GONE_STATES:
        raise _gone(export.id, export.status)
    if export.status != "ready" or export.storage_path is None:
        raise Problem(409, "conflict_state", "The export is not ready", detail=export.status)
    if Path(export.storage_path).name != export.storage_path:
        raise RuntimeError(f"export {export.id}: its storage path is no file name")
    try:
        stream = await asyncio.to_thread(
            _open_archive, cfg.exports_dir / export.storage_path, keys, export.id
        )
    except FileNotFoundError:
        again = await read_export(engine, export.id)
        if again is not None and again.status in GONE_STATES:  # a purge or the expiry took it meanwhile
            raise _gone(export.id, again.status) from None
        raise
    log.info("export_downloaded", export_id=export.id)
    headers = {"Content-Disposition": f'attachment; filename="jobhunter-export-{export.id}.zip"'}
    if export.size_bytes is not None:
        headers["Content-Length"] = str(export.size_bytes)
    return StreamingResponse(stream, media_type=DOWNLOAD_MEDIA_TYPE, headers=headers)


@router.get(
    "/exports/{export_id}",
    response_model=ExportOut,
    responses={
        200: {
            "description": "The export's state; with `download=1`, its zip",
            "content": {DOWNLOAD_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}},
        },
        404: {"description": "No such export (`not_found`)"},
        409: {"description": "A download of an export that is not ready, or that failed (`conflict_state`)"},
        410: {"description": "A download of an export that expired or was deleted by a purge (`gone`)"},
        422: {"description": "Invalid path or query (`validation_failed`)"},
    },
)
async def get_export(
    export_id: ExportId,
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    cfg: Annotated[AppConfig, Depends(get_config)],
    keys: Annotated[list[bytes], Depends(get_data_keys)],
    download: Annotated[bool, Query(description="Download the zip of a ready export")] = False,
) -> ExportOut | Response:
    """An export's state, or with `download=1` its zip, decrypted as it is sent."""
    export = await read_export(engine, export_id)
    if export is None:
        raise Problem(404, "not_found", "Export not found")
    if download:
        return await _download(engine, cfg, keys, export)
    return ExportOut(
        id=export.id,
        status=export.status,
        created_at=export.created_at,
        expires_at=export.expires_at,
        size_bytes=export.size_bytes,
    )


# ---- delete everything and the source purge ---------------------------------------------------------------


@router.post(
    "/delete-everything",
    status_code=202,
    response_model=ManifestQueuedOut,
    responses={
        **_KEYED_RESPONSES,
        403: {"description": _REAUTH_403},
        409: {
            "description": "Everything is already being deleted (`conflict_state`, detail "
            "`purge_in_progress`), or a request with this key is still running (`idempotency_in_progress`)"
        },
        422: {
            "description": "Invalid body (`validation_failed`), a phrase other than `DELETE EVERYTHING` "
            "(`confirmation_mismatch`), or the key was used for another request (`idempotency_key_reused`)"
        },
        429: {"description": _REAUTH_429},
    },
)
async def delete_everything(
    body: DeleteEverythingIn,
    request: Request,
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    cfg: Annotated[AppConfig, Depends(get_config)],
    gate: Annotated[Argon2Gate, Depends(get_argon2_gate)],
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
) -> Response:
    """Delete all your data but your account (202): the deletion runs in the background and logs you out when
    it completes; it is done once its record is confirmed offsite."""
    key = check_idempotency_key(idempotency_key)
    _confirmed(body.confirm, DELETE_EVERYTHING_PHRASE)
    fingerprint = _fingerprint(body)
    now = utcnow()
    if (answered := await _answered(engine, key, fingerprint, now)) is not None:
        return _json(answered)
    await verify_reauth(
        engine, cfg, gate, ip=client_ip(request), password=body.password, totp_code=body.totp, now=now
    )

    async def handler(conn: AsyncConnection) -> StoredResponse:
        row_id = int((await conn.execute(_OWN_KEY_ROW, {"route": PRIVACY_ROUTE, "key": key})).scalar_one())
        opened = await request_delete_everything(conn, now=now, idempotency_row_id=row_id, defer=defer_in_tx)
        return StoredResponse(202, ManifestQueuedOut(manifest_id=opened.manifest_id).model_dump(mode="json"))

    async with engine.begin() as conn:
        await conn.execute(_CONFIG_FOR_UPDATE)  # A0's lock, before the key's row
        stored = await run_idempotent(
            conn, route=PRIVACY_ROUTE, key=key, fingerprint=fingerprint, now=now, handler=handler
        )
    return _json(stored)


@router.post(
    "/purge-source",
    status_code=202,
    response_model=ManifestQueuedOut,
    responses={
        **_KEYED_RESPONSES,
        403: {"description": _REAUTH_403},
        409: {
            "description": "Everything is being deleted (`delete_everything_in_progress`), the source is "
            "already being purged (`conflict_state`, detail `purge_in_progress`), or a request with this key "
            "is still running (`idempotency_in_progress`)"
        },
        422: {
            "description": "Invalid body (`validation_failed`), the source is the only one enabled "
            "(`validation_failed` on `payload.sources.enabled`: enable another source first), a phrase other "
            "than `PURGE ADZUNA` (`confirmation_mismatch`), or the key was used for another request "
            "(`idempotency_key_reused`)"
        },
        429: {"description": _REAUTH_429},
    },
)
async def purge_source(
    body: PurgeSourceIn,
    request: Request,
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    cfg: Annotated[AppConfig, Depends(get_config)],
    gate: Annotated[Argon2Gate, Depends(get_argon2_gate)],
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
) -> Response:
    """Purge all data of the Adzuna source (202), as its agreement's end requires: the source is disabled at
    once, and the purge runs in the background; it is done once its record is confirmed offsite."""
    key = check_idempotency_key(idempotency_key)
    _confirmed(body.confirm, PURGE_ADZUNA_PHRASE)
    fingerprint = _fingerprint(body)
    now = utcnow()
    if (answered := await _answered(engine, key, fingerprint, now)) is not None:
        return _json(answered)
    await verify_reauth(
        engine, cfg, gate, ip=client_ip(request), password=body.password, totp_code=body.totp, now=now
    )
    ended: list[RunEnd] = []

    async def handler(conn: AsyncConnection) -> StoredResponse:
        opened = await request_source_purge(conn, source_id=body.source, now=now, defer=defer_in_tx)
        ended.extend(opened.run_ends)
        return StoredResponse(202, ManifestQueuedOut(manifest_id=opened.manifest_id).model_dump(mode="json"))

    async with engine.begin() as conn:
        await conn.execute(_CONFIG_FOR_UPDATE)  # A0's lock, before the key's row
        stored = await run_idempotent(
            conn, route=PRIVACY_ROUTE, key=key, fingerprint=fingerprint, now=now, handler=handler
        )
    observe_run_ends(ended)  # the collections A0 cancelled, after its commit
    return _json(stored)


# ---- the manifests -----------------------------------------------------------------------------------------


def _manifest(row: Any) -> ManifestOut:
    return ManifestOut(
        id=int(row.id),
        kind=row.kind,
        status=row.status,
        created_at=row.created_at,
        completed_at=row.completed_at,
        shipped_at=row.shipped_at,
        deleted_golden_case_ids=sorted(int(i) for i in row.cases),
    )


@router.get(
    "/manifests",
    response_model=ManifestsPageOut,
    responses={
        400: {"description": "A cursor this list did not issue (`invalid_cursor`)"},
        422: {"description": "Invalid query (`validation_failed`)"},
    },
)
async def list_manifests(
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    cfg: Annotated[AppConfig, Depends(get_config)],
    cursor: Annotated[
        str | None, Query(max_length=2048, description="`next_cursor` of the page before")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> ManifestsPageOut:
    """The deletions, newest first, keyset-paged."""
    key = cursor_key(cfg)
    before = (
        None
        if cursor is None
        else decode_id_cursor(cursor, key=key, sort=MANIFESTS_SORT, dir="desc", filters={})
    )
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                _MANIFESTS, {"before": before, "limit": limit + 1, "cases": _DELETED_CASES_PATH}
            )
        ).all()
    page, next_cursor = list_page(
        rows,
        limit=limit,
        last_key=lambda row: [int(row.id)],
        sort=MANIFESTS_SORT,
        dir="desc",
        filters={},
        key=key,
    )
    return ManifestsPageOut(items=[_manifest(row) for row in page], next_cursor=next_cursor)


@router.get(
    "/manifests/{manifest_id}",
    response_model=ManifestOut,
    responses={
        404: {"description": "No such deletion (`not_found`)"},
        422: {"description": "Invalid path (`validation_failed`)"},
    },
)
async def get_manifest(
    manifest_id: ManifestId, engine: Annotated[AsyncEngine, Depends(get_engine)]
) -> ManifestOut:
    """A deletion: its state, done only once `shipped`."""
    async with engine.connect() as conn:
        row = (await conn.execute(_MANIFEST, {"id": manifest_id, "cases": _DELETED_CASES_PATH})).first()
    if row is None:
        raise Problem(404, "not_found", "Deletion not found")
    return _manifest(row)
