"""The embedder client and the `embeddings` writes (spec §2.1 U3, §7.10, §9.2 node 7, §17.4, §17.5).

**The service** is Task 21's `embedder` (`embedder/app.py`, never imported here: the app image does not
contain it). `POST /v1/embed {input_kind, texts}` answers `{model_id, vectors, truncated, tokens}`, at most 16
texts of at most 64 KiB of UTF-8 each; `GET /health` answers `{status, model_id, checksum_ok, dim}`, 503 when
the startup checksum failed. The model is the fp32 export that `embedder/model.lock` pins (ruling T21-a):
`EMBEDDING_MODEL_ID` is that lock's `model_id`, and a unit test keeps the two equal.

**`EmbedderClient`** wraps an `httpx.AsyncClient` its caller owns and closes (`make_http_client`: no proxy
from the environment, since the embedder is on the internal network). `embed` sends one batch and checks the
answer; it raises `EmbedderError` with a kind and a short code, never content:

- `unavailable`: a connect error, a timeout (each batch has `BATCH_TIMEOUT_S` in all, §9.2) or a 5xx;
- `bad_response`: a 4xx, a body that is not the contract's JSON, a wrong count, a wrong dimension, a value
  that is no finite number, or a `model_id` other than `EMBEDDING_MODEL_ID`;
- `checksum`: a 5xx while `/health` answers 503 with `checksum_ok: false`. **Known limitation (ruling
  T21-b):** the real embedder exits 78 on a checksum mismatch (§16.8), so the worker and the api see
  `unavailable` (and the container's restart loop); the critical `embedder.checksum_mismatch` alert is not
  raised from the worker in M1. The kind stays for the fake embedder and any serving path that reports it.

`health` never raises: an unreachable or malformed answer is `EmbedderHealth(False, None, None)`. `ok` means
usable by this code: a 200 with status `ok`, the checksum verified, `EMBEDDING_MODEL_ID` and the dimension.

**The text** of a revision is its `llm_input` title, company and description joined by newlines
(`embedding_text`), cut to `MAX_TEXT_BYTES` of UTF-8 on a character boundary, so the service never answers
413 and no revision stays awaiting for its length. The service flags its own 1,024-token truncation.

**`embed_awaiting`** embeds the revisions `awaiting_embedding`, oldest first (`created_at`, then `id`), in
batches of `BATCH`, as passages. Per batch: one `/v1/embed` call (its duration observed in `EMBED_SECONDS`),
the fault point `collect.embed.after_http`, then one transaction that **first** runs `guard(conn)`, the
post-I/O ownership check (the node: the run's owner row, `ledger.lock_run_owner`; the reconciler: the worker
epoch, `fence_epoch`). `FencedOut` propagates and the attempt is lost, so no vector of a superseded owner is
committed. Then each vector is upserted (`upsert_embedding`: two revisions with one text share one row) and
its revision marked `embedded` if it still awaits (`mark_embedded`: one embedded meanwhile is left alone,
and its text's row is the one the other caller wrote, or one `retention_daily` removes unreferenced, §7.10).
The transaction locks no job row, so the guard is its first lock (§3.3.3). `unavailable` stops the loop and
the rest stays awaiting; any other `EmbedderError` propagates, with nothing of its batch written. `stop` is
asked before each batch (the node's deadline and the worker's shutdown).

**In flight.** The service embeds one batch at a time (Task 21), about 28 s for a batch of 16 on the V7 CPU,
so a fourth queued batch would pass `BATCH_TIMEOUT_S`. Each caller runs one sequential loop, one batch in
flight: the `embed` node (the collect queue runs one run at a time) and the `awaiting_embedding` reconciler
(the tick queue's one worker), at most two batches together in M1; with M2's profile embedding at most
three.

**The reconciler** `awaiting_embedding` (§17.4, cadence `heavy`) runs only while the embedder is healthy: it
embeds the revisions awaiting for more than `AWAITING_GRACE` (at most `RECONCILE_LIMIT` per pass, so the tick
queue is not held for long), fenced by the worker epoch, and resolves `embedder.unavailable` once no revision
awaits at all ("embedder healthy and backlog cleared", §17.6). M1 has no score targets, so there is no
similarity to set yet. The `embed` node raises that alert (`raise_unavailable`).
"""

import asyncio
import hashlib
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

import httpx
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.db.sql import q
from jobhunter.errors import DomainError, FencedOut
from jobhunter.faults import afault_point
from jobhunter.observability.alerts import raise_alert, resolve_alert
from jobhunter.observability.logging import get_logger
from jobhunter.observability.metrics import EMBED_SECONDS
from jobhunter.types import JSONValue
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.reconcilers import ReconcileContext, reconciler

EMBEDDING_MODEL_ID: Final = "snowflake-arctic-embed-l-v2.0@ac6544c8a46e00af67e330e85a9028c66b8cfd9a/fp32"
EMBEDDING_DIM: Final = 1024  # `embeddings.vector halfvec(1024)`
BATCH: Final = 16
BATCH_TIMEOUT_S: Final = 120.0
HEALTH_TIMEOUT_S: Final = 2.0
MAX_TEXT_BYTES: Final = 65_536  # the service's limit (Task 21)
# The `llm_input` fields of a revision's text, in order.
TEXT_FIELDS: Final = ("title", "company_display", "description_text")
AFTER_HTTP: Final = "collect.embed.after_http"
# §17.4: the reconciler embeds revisions awaiting for longer than this.
AWAITING_GRACE: Final = timedelta(minutes=10)
# The most revisions one reconciler pass embeds: 4 batches, about 2 min on the V7 CPU.
RECONCILE_LIMIT: Final = 4 * BATCH
UNAVAILABLE_ALERT: Final = "embedder.unavailable"
ALERT_SUBJECT: Final = "embedder"

EmbedderErrorKind = Literal["unavailable", "bad_response", "checksum"]
InputKind = Literal["query", "passage"]

log = get_logger("jobhunter.embeddings")


class EmbedderError(DomainError):
    """An embed call failed (§17.1); the message names the kind and the code, nothing else."""

    def __init__(self, kind: EmbedderErrorKind, *, code: str | None = None) -> None:
        self.kind: EmbedderErrorKind = kind
        self.code = kind if code is None else code
        super().__init__(f"{kind}: {self.code}")


@dataclass(frozen=True)
class Embedded:
    vector: tuple[float, ...]
    truncated: bool
    tokens: int


@dataclass(frozen=True)
class EmbedderHealth:
    ok: bool
    model_id: str | None
    checksum_ok: bool | None


# The health of an embedder that gave no usable answer.
NO_HEALTH: Final = EmbedderHealth(ok=False, model_id=None, checksum_ok=None)


def make_http_client() -> httpx.AsyncClient:
    """The HTTP client of an `EmbedderClient`: no proxy or certificates from the environment, no redirects."""
    return httpx.AsyncClient(trust_env=False, follow_redirects=False)


def _bad(code: str) -> EmbedderError:
    return EmbedderError("bad_response", code=code)


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _bad("vector")
    number = float(value)
    if not math.isfinite(number):
        raise _bad("non_finite")
    return number


def _embedded(body: object, n: int) -> list[Embedded]:
    """The contract's answer for `n` texts, checked; a `bad_response` otherwise."""
    if not isinstance(body, dict):
        raise _bad("not_an_object")
    if body.get("model_id") != EMBEDDING_MODEL_ID:
        raise _bad("model_id")
    vectors, truncated, tokens = body.get("vectors"), body.get("truncated"), body.get("tokens")
    if not (isinstance(vectors, list) and isinstance(truncated, list) and isinstance(tokens, list)):
        raise _bad("count")
    if not len(vectors) == len(truncated) == len(tokens) == n:
        raise _bad("count")
    out: list[Embedded] = []
    for vector, flag, count in zip(vectors, truncated, tokens, strict=True):
        if not isinstance(vector, list):
            raise _bad("vector")
        if len(vector) != EMBEDDING_DIM:
            raise _bad("dimension")
        if not isinstance(flag, bool):
            raise _bad("truncated")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise _bad("tokens")
        out.append(Embedded(vector=tuple(_number(x) for x in vector), truncated=flag, tokens=count))
    return out


class EmbedderClient:
    """The embedder at `base_url` over `client`, which the caller owns (see the module docstring)."""

    def __init__(self, base_url: str, client: httpx.AsyncClient) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = client

    async def embed(
        self, texts: Sequence[str], *, input_kind: InputKind, timeout_s: float = BATCH_TIMEOUT_S
    ) -> list[Embedded]:
        """One batch: the vector, truncation flag and token count of each text, in order.

        At most `BATCH` texts (a `ValueError` before any request otherwise); none makes no request. Raises
        `EmbedderError` as the module docstring says.
        """
        if len(texts) > BATCH:
            raise ValueError(f"at most {BATCH} texts per embed request, got {len(texts)}")
        if not texts:
            return []
        payload = {"input_kind": input_kind, "texts": list(texts)}
        try:
            async with asyncio.timeout(timeout_s):
                response = await self.http.post(f"{self.base_url}/v1/embed", json=payload, timeout=timeout_s)
        except TimeoutError:
            raise EmbedderError("unavailable", code="deadline") from None
        except httpx.ConnectError:
            raise EmbedderError("unavailable", code="connect") from None
        except httpx.TimeoutException:
            raise EmbedderError("unavailable", code="timeout") from None
        except httpx.TransportError:
            raise EmbedderError("unavailable", code="transport") from None
        except httpx.DecodingError:
            raise _bad("decoding") from None
        status = response.status_code
        if status >= 500:
            if (await self.health()).checksum_ok is False:
                raise EmbedderError("checksum", code="health_503")
            raise EmbedderError("unavailable", code=f"http_{status}")
        if status != 200:
            raise _bad(f"http_{status}")
        try:
            body: object = response.json()
        except ValueError:  # not JSON, or not UTF-8
            raise _bad("not_json") from None
        return _embedded(body, len(texts))

    async def health(self, timeout_s: float = HEALTH_TIMEOUT_S) -> EmbedderHealth:
        """The service's `/health`, within `timeout_s`; never raises (see the module docstring)."""
        try:
            async with asyncio.timeout(timeout_s):
                response = await self.http.get(f"{self.base_url}/health", timeout=timeout_s)
            body: object = response.json()
        except (TimeoutError, ValueError, httpx.HTTPError, httpx.InvalidURL):
            return NO_HEALTH
        if response.status_code not in (200, 503) or not isinstance(body, dict):
            return NO_HEALTH
        model_id, checksum_ok = body.get("model_id"), body.get("checksum_ok")
        if not isinstance(model_id, str) or not isinstance(checksum_ok, bool):
            return NO_HEALTH
        dim = body.get("dim")
        ok = (
            response.status_code == 200
            and body.get("status") == "ok"
            and checksum_ok
            and model_id == EMBEDDING_MODEL_ID
            and isinstance(dim, int)
            and not isinstance(dim, bool)
            and dim == EMBEDDING_DIM
        )
        return EmbedderHealth(ok=ok, model_id=model_id, checksum_ok=checksum_ok)


def embedding_text(llm_input: Mapping[str, JSONValue]) -> str:
    """The text a revision is embedded as: its `TEXT_FIELDS` joined by newlines, at most `MAX_TEXT_BYTES`.

    The cut drops at most one character's incomplete bytes at the end, so the text stays valid UTF-8. A
    field that is not a string is a `TypeError`: the material projection always writes strings (§8.8).
    """
    parts: list[str] = []
    for name in TEXT_FIELDS:
        value = llm_input.get(name)
        if not isinstance(value, str):
            raise TypeError(f"llm_input.{name} is not a string")
        parts.append(value)
    encoded = "\n".join(parts).encode("utf-8")
    return encoded[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore")


def text_sha256(text: str) -> bytes:
    """The sha256 of `text` as UTF-8: `embeddings.text_sha256`."""
    return hashlib.sha256(text.encode("utf-8")).digest()


_INSERT_EMBEDDING: Final = q(
    "INSERT INTO embeddings (embedding_model_id, text_sha256, input_kind, vector) "
    "VALUES (:model, :sha, :kind, CAST(:vector AS halfvec)) "
    "ON CONFLICT (embedding_model_id, text_sha256, input_kind) DO NOTHING RETURNING id"
)
# `FOR KEY SHARE`: the row the caller is about to reference cannot be deleted before its transaction ends.
_EMBEDDING_ID: Final = q(
    "SELECT id FROM embeddings WHERE embedding_model_id = :model AND text_sha256 = :sha "
    "AND input_kind = :kind FOR KEY SHARE"
)
_MARK_EMBEDDED: Final = q(
    "UPDATE job_revisions SET embedding_status = 'embedded', embedding_id = :embedding, "
    "embedding_truncated = :truncated WHERE id = :id AND embedding_status = 'awaiting_embedding' RETURNING id"
)


async def upsert_embedding(
    conn: AsyncConnection, *, model_id: str, text_sha256: bytes, input_kind: str, vector: Sequence[float]
) -> int:
    """The id of the `embeddings` row of `(model_id, text_sha256, input_kind)`, inserted with `vector` when
    there is none (`ON CONFLICT … DO NOTHING`, then the existing row's id, in a statement of its own so that
    it sees a row a concurrent transaction committed meanwhile). The vector is bound as the `halfvec`
    literal `[v1,…]`. A row deleted between the two statements is inserted again, once."""
    params = {
        "model": model_id,
        "sha": text_sha256,
        "kind": input_kind,
        "vector": "[" + ",".join(repr(float(x)) for x in vector) + "]",
    }
    for _ in range(2):
        inserted = (await conn.execute(_INSERT_EMBEDDING, params)).scalar_one_or_none()
        if inserted is not None:
            return int(inserted)
        existing = (await conn.execute(_EMBEDDING_ID, params)).scalar_one_or_none()
        if existing is not None:
            return int(existing)
    raise LookupError("upsert_embedding: the conflicting embeddings row vanished twice")


async def mark_embedded(
    conn: AsyncConnection, revision_id: int, *, embedding_id: int, truncated: bool
) -> bool:
    """Mark the revision `embedded` with `embedding_id` if it still awaits; whether it moved (`False`: it was
    embedded meanwhile, and is left alone)."""
    params = {"id": revision_id, "embedding": embedding_id, "truncated": truncated}
    return (await conn.execute(_MARK_EMBEDDED, params)).first() is not None


@dataclass(frozen=True)
class EmbedReport:
    """What one `embed_awaiting` call did: the revisions it marked `embedded` (`truncated` of them flagged),
    the revisions of its selection still awaiting when it ended, and whether the embedder was unavailable."""

    embedded: int
    awaiting: int
    truncated: int
    unavailable: bool


# The keyset bounds of a selection without a time bound, and its start.
_NO_TIME_BOUND: Final = datetime.max.replace(tzinfo=UTC)
_START: Final = (datetime.min.replace(tzinfo=UTC), 0)
_NEXT_BATCH: Final = q(
    "SELECT id, created_at, llm_input FROM job_revisions WHERE embedding_status = 'awaiting_embedding' "
    "AND created_at < :before AND (created_at, id) > (:after_at, :after_id) ORDER BY created_at, id LIMIT :n"
)
_COUNT_AWAITING: Final = q(
    "SELECT count(*) FROM job_revisions WHERE embedding_status = 'awaiting_embedding' "
    "AND created_at < :before"
)

Guard = Callable[[AsyncConnection], Awaitable[None]]


async def count_awaiting(conn: AsyncConnection, *, created_before: datetime | None = None) -> int:
    """How many revisions are `awaiting_embedding` (created before `created_before`, when given)."""
    before = _NO_TIME_BOUND if created_before is None else created_before
    return int((await conn.execute(_COUNT_AWAITING, {"before": before})).scalar_one())


async def _write_batch(
    engine: AsyncEngine, guard: Guard, rows: Sequence[Any], texts: Sequence[str], out: Sequence[Embedded]
) -> tuple[int, int]:
    """The batch's transaction: the guard first, then the upserts and the marks; (embedded, truncated)."""
    embedded = truncated = 0
    async with engine.begin() as conn:
        await guard(conn)
        for row, text, item in zip(rows, texts, out, strict=True):
            embedding_id = await upsert_embedding(
                conn,
                model_id=EMBEDDING_MODEL_ID,
                text_sha256=text_sha256(text),
                input_kind="passage",
                vector=item.vector,
            )
            if await mark_embedded(conn, int(row.id), embedding_id=embedding_id, truncated=item.truncated):
                embedded += 1
                truncated += item.truncated
    return embedded, truncated


async def embed_awaiting(
    engine: AsyncEngine,
    client: EmbedderClient,
    *,
    guard: Guard,
    limit: int | None = None,
    batch: int = BATCH,
    fault: str | None = None,
    created_before: datetime | None = None,
    stop: Callable[[], bool] | None = None,
) -> EmbedReport:
    """Embed the revisions awaiting embedding (created before `created_before`, when given), at most `limit`
    of them, `batch` per call, as the module docstring says; `afault_point(fault)` after each batch when
    `fault` is given, and no batch started once `stop()` is true."""
    if not 1 <= batch <= BATCH:
        raise ValueError(f"batch must be 1 to {BATCH}")
    before = _NO_TIME_BOUND if created_before is None else created_before
    after_at, after_id = _START
    embedded = truncated = taken = 0
    unavailable = False
    while limit is None or taken < limit:
        if stop is not None and stop():
            break
        size = batch if limit is None else min(batch, limit - taken)
        params = {"before": before, "after_at": after_at, "after_id": after_id, "n": size}
        async with engine.connect() as conn:
            rows = (await conn.execute(_NEXT_BATCH, params)).all()
        if not rows:
            break
        after_at, after_id = rows[-1].created_at, int(rows[-1].id)
        taken += len(rows)
        texts = [embedding_text(row.llm_input) for row in rows]
        started = time.monotonic()
        try:
            out = await client.embed(texts, input_kind="passage")
        except EmbedderError as err:
            if err.kind != "unavailable":
                raise
            log.warning("embed_unavailable", error_code=err.code, batch=len(rows))
            unavailable = True
            break
        EMBED_SECONDS.observe(time.monotonic() - started)
        await afault_point(AFTER_HTTP)
        done, flagged = await _write_batch(engine, guard, rows, texts, out)
        embedded += done
        truncated += flagged
        if fault is not None:
            await afault_point(fault)
    async with engine.connect() as conn:
        awaiting = await count_awaiting(conn, created_before=created_before)
    return EmbedReport(embedded=embedded, awaiting=awaiting, truncated=truncated, unavailable=unavailable)


async def raise_unavailable(conn: AsyncConnection, *, kind: EmbedderErrorKind, run_id: int | None) -> None:
    """Raise `embedder.unavailable` (warn, subject `embedder`): embedding failed in a run (§17.6)."""
    await raise_alert(
        conn,
        code=UNAVAILABLE_ALERT,
        severity="warn",
        subject=ALERT_SUBJECT,
        message="embedding failed in a run: its revisions stay awaiting_embedding",
        context={"kind": kind, "run_id": run_id},
    )


@reconciler("awaiting_embedding", "heavy")
async def reconcile_awaiting_embedding(ctx: ReconcileContext) -> int:
    """§17.4 `awaiting_embedding` (see the module docstring); how many revisions it embedded.

    0 without a client (`ctx.embedder`), while the embedder is not healthy, and when `ctx.epoch` is no
    longer the current worker epoch (`fence_epoch` logged it; the fenced transaction wrote nothing).
    """
    client = ctx.embedder
    if client is None or not (await client.health()).ok:
        return 0

    async def fenced(conn: AsyncConnection) -> None:
        await fence_epoch(conn, ctx.epoch)

    try:
        report = await embed_awaiting(
            ctx.engine, client, guard=fenced, limit=RECONCILE_LIMIT, created_before=ctx.now - AWAITING_GRACE
        )
        if not report.unavailable:
            async with ctx.engine.begin() as conn:
                await fence_epoch(conn, ctx.epoch)
                if await count_awaiting(conn) == 0:
                    await resolve_alert(conn, code=UNAVAILABLE_ALERT, subject=ALERT_SUBJECT)
    except FencedOut:
        return 0
    return report.embedded
