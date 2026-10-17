"""The `Idempotency-Key` store (spec §14.1, §7.2 `api_idempotency`).

A POST that can start paid work, activate a version or delete data runs its handler through `run_idempotent`,
inside the caller's transaction. The row `(route, idem_key)` records the request's fingerprint and, once the
handler returned, its response, for `IDEMPOTENCY_TTL`:

- the same key with the same request returns the **stored response**, even after the work finished;
- the same key with another request → 422 `idempotency_key_reused`;
- a key whose first request is still running → 409 `idempotency_in_progress`. That request holds the row
  uncommitted, so the insert waits for it, for at most `IN_PROGRESS_LOCK_TIMEOUT`;
- a missing or malformed key → 400 `idempotency_key_required` (`check_idempotency_key`, pure, so a route can
  refuse it before it opens a transaction).

The row is inserted (`in_progress`), the handler runs and the row is completed in one transaction, so a
committed row is always `completed`. An exception from the handler rolls everything back, the row included:
a retry with the same key runs again (ruling T17-e). Only the handler's returned status and JSON body are
stored, and that body carries ids and states only, never a secret or PII. The first response is returned in
its stored (JSONB-decoded) form too, so it renders byte for byte like every replay.
"""

import hashlib
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import TextClause

from jobhunter.api.errors import Problem
from jobhunter.db.seed import canonical_json
from jobhunter.db.sql import LOCK_NOT_AVAILABLE, pg_error, q
from jobhunter.observability.logging import get_logger
from jobhunter.types import JSONValue, get_object

IDEMPOTENCY_TTL: Final = timedelta(days=7)
IDEM_KEY_RE: Final = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
# How long a request waits for another one that holds the same key before it answers 409. Read at call time,
# so a test can shorten it.
IN_PROGRESS_LOCK_TIMEOUT: Final = "5s"
IDEMPOTENCY_HEADER: Final = "Idempotency-Key"

log = get_logger("jobhunter.api.idempotency")

# `is_local`: for this transaction only; `'0'` turns the timeout off again (no server-wide `lock_timeout`).
_SET_LOCK_TIMEOUT: Final = q("SELECT set_config('lock_timeout', :timeout, true)")
_CLAIM: Final = q(
    "INSERT INTO api_idempotency (route, idem_key, request_sha256, status, expires_at) "
    "VALUES (:route, :key, :sha, 'in_progress', :expires_at) "
    "ON CONFLICT (route, idem_key) DO NOTHING RETURNING 1"
)
_STORED: Final = q(
    "SELECT request_sha256, status, response_status, response_body, expires_at FROM api_idempotency "
    "WHERE route = :route AND idem_key = :key"
)
# The expiry is checked again here, so a row another request has just put back is never deleted.
_DELETE_EXPIRED: Final = q(
    "DELETE FROM api_idempotency WHERE route = :route AND idem_key = :key AND expires_at <= :now RETURNING 1"
)
_COMPLETE: Final = q(
    "UPDATE api_idempotency SET status = 'completed', response_status = :status, response_body = :body "
    "WHERE route = :route AND idem_key = :key AND status = 'in_progress' RETURNING response_body",
    jsonb=["body"],
)


def request_fingerprint(body: JSONValue, path_params: Mapping[str, JSONValue]) -> bytes:
    """The SHA-256 of the request: its parsed JSON body and its path parameters, as canonical JSON."""
    return hashlib.sha256(canonical_json({"body": body, "path": dict(path_params)})).digest()


@dataclass(frozen=True)
class StoredResponse:
    """A response that a handler returned and a replay returns again: its status and JSON body."""

    status: int
    body: dict[str, JSONValue]


def check_idempotency_key(key: str | None) -> str:
    """`key`, when it is a well-formed `Idempotency-Key`; else 400 `idempotency_key_required`. No I/O."""
    if key is None or IDEM_KEY_RE.fullmatch(key) is None:
        raise Problem(
            400,
            "idempotency_key_required",
            "Idempotency-Key required",
            detail="send an Idempotency-Key header of 16 to 128 characters from A-Z, a-z, 0-9, '_' and '-'",
        )
    return key


def _in_progress() -> Problem:
    return Problem(409, "idempotency_in_progress", "A request with this Idempotency-Key is still running")


def _reused() -> Problem:
    return Problem(422, "idempotency_key_reused", "This Idempotency-Key was used for another request")


async def _under_lock_timeout(
    conn: AsyncConnection, statement: TextClause, params: Mapping[str, Any], *, route: str
) -> bool:
    """Run `statement` with `IN_PROGRESS_LOCK_TIMEOUT`; whether it touched a row. 409 when the wait timed out.

    The timeout is off again afterwards, so the handler's own waits are not bounded by it. A timeout aborts
    the transaction, which has nothing to keep yet: raising the 409 rolls it back.
    """
    await conn.execute(_SET_LOCK_TIMEOUT, {"timeout": IN_PROGRESS_LOCK_TIMEOUT})
    try:
        touched = (await conn.execute(statement, params)).first() is not None
    except OperationalError as exc:
        if pg_error(exc).sqlstate != LOCK_NOT_AVAILABLE:
            raise
        log.info("idempotency_in_progress", route=route)
        raise _in_progress() from None
    await conn.execute(_SET_LOCK_TIMEOUT, {"timeout": "0"})
    return touched


async def run_idempotent(
    conn: AsyncConnection,
    *,
    route: str,
    key: str | None,
    fingerprint: bytes,
    now: datetime,
    handler: Callable[[AsyncConnection], Awaitable[StoredResponse]],
) -> StoredResponse:
    """Run `handler` once per `(route, key)`, in the caller's transaction, and store what it returned.

    The caller has already taken any `active_config` lock its handler needs (global lock order). The key is
    claimed by inserting its `in_progress` row; then the handler runs and the row is completed with its
    response, which is returned as it was stored. When the key has a row already:
    - expired at `now` → it is deleted and the key claimed again, once;
    - another `fingerprint` → 422 `idempotency_key_reused`;
    - `in_progress` → 409 `idempotency_in_progress`;
    - else the stored response is returned and `handler` does not run.
    A wait on another request's uncommitted claim longer than `IN_PROGRESS_LOCK_TIMEOUT` is a 409 too.
    """
    ids = {"route": route, "key": check_idempotency_key(key)}
    claim = {**ids, "sha": fingerprint, "expires_at": now + IDEMPOTENCY_TTL}
    for _ in range(2):
        if await _under_lock_timeout(conn, _CLAIM, claim, route=route):
            response = await handler(conn)
            completed = (
                await conn.execute(_COMPLETE, {**ids, "status": response.status, "body": response.body})
            ).first()
            if completed is None:
                raise RuntimeError("api_idempotency: the claimed row vanished before it was completed")
            return StoredResponse(response.status, get_object(completed.response_body, ""))
        stored = (await conn.execute(_STORED, ids)).first()
        if stored is None:
            continue  # the row was deleted (expired) after the claim met it: claim again
        if stored.expires_at <= now:
            await _under_lock_timeout(conn, _DELETE_EXPIRED, {**ids, "now": now}, route=route)
            continue
        if bytes(stored.request_sha256) != fingerprint:
            log.info("idempotency_key_reused", route=route)
            raise _reused()
        if stored.status != "completed":
            log.info("idempotency_in_progress", route=route)
            raise _in_progress()
        log.info("idempotent_replay", route=route, status=int(stored.response_status))
        return StoredResponse(int(stored.response_status), get_object(stored.response_body, ""))
    # Twice the key had a row that went away (or expired) before it could be read: another request is busy.
    log.info("idempotency_in_progress", route=route)
    raise _in_progress()
