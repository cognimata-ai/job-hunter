"""The acquisition receipts (spec §7.7, §9.1; plan Deviation 23): fetch requests and their stored responses.

A `fetch_requests` row is the receipt of one physical attempt of a logical request: `request_key` (§5.1) names
the logical request and `attempt_seq` numbers its attempts across every run attempt (1-8). Its state moves
only through `transition()` (the acquisition protocol, `sources.acquire`); this module inserts the rows, reads
the attempts of a key back, and stores and loads the responses.

A `raw_responses` row keeps the body zstd-compressed (level 3): the whole body for a `succeeded` request, the
first `http.ERROR_BODY_BYTES` for every other outcome, whatever the HTTP status (an HTML page delivered with a
200 included). A request that did not succeed stores `item_count = 0` and `parsed_at = fetched_at`, so the I1
sum holds for an unparseable envelope and parsing never selects it (Deviation 23). Only the §5.2 header
allowlist is stored. A body equal (`body_sha256`) to an unexpired one stored earlier is not stored again: the
new row references that row (`body_ref_id`), whose `expires_at` is raised to the new row's in the same
transaction, so a referenced body never expires before the last row that relies on it.

Every function runs in the caller's transaction. A transaction that writes a receipt first holds the run row
`FOR SHARE` (`ledger.lock_run_owner`); that is the caller's part.
"""

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final, Literal

import httpx
import zstandard
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.sources.base import RawPage, RequestSpec
from jobhunter.sources.http import ERROR_BODY_BYTES, MAX_BODY_BYTES, stored_headers

if TYPE_CHECKING:
    # `worker.runs` imports `observability.health`, which imports the quota ledger: the owner type only.
    from jobhunter.worker.runs import RunOwner

RAW_BODY_TTL: Final = timedelta(days=30)
ZSTD_LEVEL: Final = 3

_INSERT = q(
    "INSERT INTO fetch_requests (run_id, run_attempt_no, cell_id, source_instance_id, phase, operation, "
    "detail_native_id, parent_fetch_request_id, request_key, attempt_seq, page_no, request_class, spec, "
    "status, skip_reason) VALUES (:run, :attempt, :cell, :instance, :phase, :operation, :detail, :parent, "
    ":key, :seq, :page_no, :class, :spec, :status, :skip_reason) RETURNING id",
    jsonb=["spec"],
)
_ATTEMPTS = q(
    "SELECT id, attempt_seq, status, raw_response_id, quota_ledger_id, run_id, run_attempt_no, http_status, "
    "request_class, skip_reason, error_code, new_native_ids FROM fetch_requests WHERE request_key = :key "
    "ORDER BY attempt_seq"
)
# An unexpired stored body with the same hash, locked so that retention cannot purge it under the reference.
_SAME_BODY = q(
    "SELECT id FROM raw_responses WHERE body_sha256 = :sha AND purged_at IS NULL AND body_zstd IS NOT NULL "
    "AND expires_at > :fetched_at ORDER BY id LIMIT 1 FOR UPDATE"
)
_EXTEND = q("UPDATE raw_responses SET expires_at = greatest(expires_at, :expires_at) WHERE id = :id")
_STORE = q(
    "INSERT INTO raw_responses (fetch_request_id, ledger_entry_id, source_instance_id, http_status, headers, "
    "body_zstd, body_sha256, body_ref_id, item_count, parsed_at, fetched_at, expires_at) VALUES (:fr, "
    ":entry, :instance, :status, :headers, :body, :sha, :ref, :items, :parsed_at, :fetched_at, "
    ":expires_at) RETURNING id",
    jsonb=["headers"],
)
_LINK = q("UPDATE fetch_requests SET raw_response_id = :rr WHERE id = :fr")
_LOAD = q(
    "SELECT http_status, headers, body_zstd, body_ref_id, purged_at, fetched_at FROM raw_responses "
    "WHERE id = :id"
)
_BODY = q("SELECT body_zstd FROM raw_responses WHERE id = :id AND purged_at IS NULL")


@dataclass(frozen=True)
class FetchAttempt:
    """One attempt of a request key, as `fetch_requests` records it. The fields after `http_status` are what
    the §6.2 rule-2 resolution reads besides the state (the attempt's class, its skip or error code and the
    persisted new-id count of a served page)."""

    id: int
    attempt_seq: int
    status: str
    raw_response_id: int | None
    quota_ledger_id: int | None
    run_id: int
    run_attempt_no: int
    http_status: int | None
    request_class: str
    skip_reason: str | None
    error_code: str | None
    new_native_ids: int | None


async def insert_fetch_request(
    conn: AsyncConnection,
    *,
    owner: "RunOwner",
    cell_id: int,
    source_instance_id: int,
    phase: Literal["primary", "fallback"],
    spec: RequestSpec,
    request_key: bytes,
    attempt_seq: int,
    status: Literal["planned", "skipped"],
    skip_reason: str | None = None,
) -> int:
    """Insert the receipt of attempt `attempt_seq` of `request_key` for `spec`, `planned` or (with its
    reason) `skipped`, recorded for `owner`'s attempt; returns its id. The caller holds the owner row."""
    if (spec.cell_id, spec.source_instance_id) != (cell_id, source_instance_id):
        raise ValueError(
            f"the spec is for cell {spec.cell_id} of instance {spec.source_instance_id}, "
            f"not cell {cell_id} of instance {source_instance_id}"
        )
    if (status == "skipped") != (skip_reason is not None):
        raise ValueError("a skipped request has a skip reason, and only a skipped one")
    params = {
        "run": owner.run_id,
        "attempt": owner.attempt_no,
        "cell": cell_id,
        "instance": source_instance_id,
        "phase": phase,
        "operation": spec.operation,
        "detail": spec.detail_native_id,
        "parent": spec.parent_fetch_request_id,
        "key": request_key,
        "seq": attempt_seq,
        "page_no": spec.page_no,
        "class": spec.request_class,
        "spec": spec.to_json(),
        "status": status,
        "skip_reason": skip_reason,
    }
    return int((await conn.execute(_INSERT, params)).scalar_one())


async def fetch_attempts(conn: AsyncConnection, request_key: bytes) -> list[FetchAttempt]:
    """Every attempt of `request_key`, across all run attempts, in `attempt_seq` order (at most 8)."""
    rows = await conn.execute(_ATTEMPTS, {"key": request_key})
    return [FetchAttempt(**row) for row in rows.mappings()]


async def store_response(
    conn: AsyncConnection,
    fetch_request_id: int,
    page: RawPage,
    *,
    source_instance_id: int,
    ledger_entry_id: int | None,
    succeeded: bool,
    items: int | None,
) -> int:
    """Store the response of the fetch request (see the module docstring) and link the request to it
    (`fetch_requests.raw_response_id`); returns the `raw_responses` id.

    `items` is the envelope's item count of a `succeeded` request (required then); any other request stores
    `item_count = 0` and `parsed_at = fetched_at`.
    """
    if succeeded and items is None:
        raise ValueError("a succeeded response has an item count")
    body = page.body if succeeded else page.body[:ERROR_BODY_BYTES]
    sha = hashlib.sha256(body).digest()
    expires_at = page.fetched_at + RAW_BODY_TTL
    ref = (await conn.execute(_SAME_BODY, {"sha": sha, "fetched_at": page.fetched_at})).scalar()
    if ref is not None:
        await conn.execute(_EXTEND, {"id": ref, "expires_at": expires_at})
    params = {
        "fr": fetch_request_id,
        "entry": ledger_entry_id,
        "instance": source_instance_id,
        "status": page.status,
        "headers": stored_headers(httpx.Headers(dict(page.headers))),
        "body": None if ref is not None else zstandard.ZstdCompressor(level=ZSTD_LEVEL).compress(body),
        "sha": sha,
        "ref": ref,
        "items": items if succeeded else 0,
        "parsed_at": None if succeeded else page.fetched_at,
        "fetched_at": page.fetched_at,
        "expires_at": expires_at,
    }
    raw_response_id = int((await conn.execute(_STORE, params)).scalar_one())
    await conn.execute(_LINK, {"rr": raw_response_id, "fr": fetch_request_id})
    return raw_response_id


def _decompress(data: bytes) -> bytes:
    return zstandard.ZstdDecompressor().decompress(data, max_output_size=MAX_BODY_BYTES)


async def load_response(conn: AsyncConnection, raw_response_id: int, spec: RequestSpec) -> RawPage:
    """The stored response as the page of `spec`, its body read through `body_ref_id` when it references an
    identical one. A purged body (its own or the referenced one) raises `LookupError`."""
    row = (await conn.execute(_LOAD, {"id": raw_response_id})).first()
    if row is None:
        raise LookupError(f"raw response {raw_response_id} does not exist")
    data: bytes | None = None
    if row.purged_at is None:
        data = row.body_zstd
        if data is None and row.body_ref_id is not None:
            data = (await conn.execute(_BODY, {"id": row.body_ref_id})).scalar()
    if data is None:
        raise LookupError(f"the body of raw response {raw_response_id} has been purged")
    return RawPage(spec, int(row.http_status), dict(row.headers), _decompress(bytes(data)), row.fetched_at)
