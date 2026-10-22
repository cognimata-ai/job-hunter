"""The profile vectors (spec §7.10, §12.6 step 3, §20.1; M2 plan Task 19, Deviation 23).

A confirmed profile version has one vector per text that stands for it: its `summary` and each of its target
titles. M3's ranking compares the ads' embeddings with these. `embed_profile` (the handler,
`profile.operations`) embeds the texts through M1's embedder as queries (`input_kind="query"`; the service
adds the `query: ` prefix itself) and stores them in `profile_vectors (profile_version_id, kind,
embedding_id)`.

**The texts** (`vector_texts`): `("summary", summary)` and `("title:<i>", target_titles[i].title)`, `i`
counted from 0 in the stored list. A `target_titles` item is a `{title, lang}` object and the text is its
`title`.
Empty texts (blank ones included) are skipped, and every other kind keeps its index. A validated profile has
at most `1 + 12` texts (§12.4), so the handler's one request is always within the service's batch.

**The operation** is created with the version by the confirm (Task 21) and by the reconciler below:
`request_embedding` is `create_operation` with `KIND`, the payload `{"profile_version_id": id}` and the dedup
key `embed_profile:<id>`, so a version has at most one live operation.

**A run** (the handler's three steps; this module holds their pieces):
1. `read_version`: the version's state. A version that is gone or purged (`purged_at`), or under an active
   purge fence, is `skipped` (the handler finishes the operation `succeeded` with the progress
   `{"skipped": "purged" | "fenced"}`). A fence is a `purge_fences` row with `released_at IS NULL` that is
   `global` (delete-everything) or of kind `profile` for this version (`profile_version_id`; `is_fenced`
   cannot be asked without a tuple). `vectors_complete` says whether the version already has exactly the
   vectors of its texts, as the current model embedded them; then there is nothing to embed.
2. The embedder call, outside any transaction (`AFTER_EMBED_POINT` follows it).
3. `write_vectors`, in the one transaction that also ends the operation (`finish_in`), after the owner lock
   (§3.3.3: the owner row, then the other domain rows). The transaction first takes the version row
   `FOR SHARE` and reads the state again: the profile purge fences the version, then locks its row
   `FOR UPDATE` before it deletes anything (§20.2 step 1). A version purged or fenced since step 1 writes
   nothing, not an embedding row either (a vector must never outlive its purge). Otherwise the embeddings are
   upserted (`upsert_embedding`: a row another version or an earlier run left is reused, and held
   `FOR KEY SHARE` against the retention rule until the commit), the `profile_vectors` rows upserted
   (`ON CONFLICT (profile_version_id, kind) DO UPDATE`), and the kinds the version no longer has deleted.

**The reconciler** `profile_vectors` (heavy) implements §12.6 step 3's "recomputed when it returns" for the
profile side, as `awaiting_embedding` does for revisions. A pass reads, fenced by the worker epoch, the
active profile version, and creates an `embed_profile` operation for it when it is usable, not fenced, its
vectors are not complete and it has no live operation (the dedup key). Only then does it ask the embedder
for its health (`/health`): with the embedder unhealthy, or this process without one, it creates nothing, so
the operation that exhausted its attempts while the embedder was down is re-created once it is up.

Logs carry ids, counts and short codes, never a text. The version's id is logged as `version_id`: a key that
starts with `profile` is redacted (`observability.redact.REDACT_PREFIXES`).
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.embeddings import EMBEDDING_MODEL_ID, Embedded, text_sha256, upsert_embedding
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.observability.logging import get_logger
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.operations import create_operation
from jobhunter.worker.reconcilers import ReconcileContext, reconciler
from jobhunter.worker.tasks import Deferrer

KIND: Final = "embed_profile"
SUMMARY_KIND: Final = "summary"
INPUT_KIND: Final = "query"
# The fault point after the embedder call, before the write's transaction (the race tests hold it there).
AFTER_EMBED_POINT: Final = "embed_profile.after_embed"
# The operation's error code and wait when the embedder cannot be used (§12.6 step 3: "recomputed when it
# returns").
REQUEUE_CODE: Final = "embedder_unavailable"
REQUEUE_AFTER: Final = timedelta(minutes=10)

Skipped = Literal["purged", "fenced"]

# The profile version's state; `FOR SHARE` in the write's transaction, where it is the lock the profile purge
# (which locks the version row `FOR UPDATE`, §20.2 step 1) is ordered against.
_VERSION: Final = "SELECT purged_at, profile_json FROM profile_versions WHERE id = :pv"
_READ_VERSION: Final = q(_VERSION)
_LOCK_VERSION: Final = q(_VERSION + " FOR SHARE")
# A fence that stops the work on this version: a purge of exactly this version, or delete-everything (§20.2).
_FENCE_PREDICATE: Final = (
    "f.released_at IS NULL "
    "AND (f.kind = 'global' OR (f.kind = 'profile' AND f.profile_version_id = {version}))"
)
_FENCED: Final = q(
    f"SELECT EXISTS (SELECT 1 FROM purge_fences f WHERE {_FENCE_PREDICATE.format(version=':pv')})"  # noqa: S608
)
_STORED: Final = q(
    "SELECT v.kind, e.embedding_model_id, e.text_sha256, e.input_kind FROM profile_vectors v "
    "JOIN embeddings e ON e.id = v.embedding_id WHERE v.profile_version_id = :pv"
)
_UPSERT_VECTOR: Final = q(
    "INSERT INTO profile_vectors (profile_version_id, kind, embedding_id) VALUES (:pv, :kind, :embedding) "
    "ON CONFLICT (profile_version_id, kind) DO UPDATE SET embedding_id = EXCLUDED.embedding_id"
)
_DELETE_OTHER_KINDS: Final = q(
    "DELETE FROM profile_vectors WHERE profile_version_id = :pv AND kind <> ALL (CAST(:kinds AS text[]))"
)
# The active profile version, if it is usable: not purged and not under a fence.
_ACTIVE: Final = q(
    "SELECT v.id, v.profile_json FROM active_config a JOIN profile_versions v ON v.id = a.profile_version_id "  # noqa: S608
    "WHERE a.id = 1 AND v.purged_at IS NULL "
    f"AND NOT EXISTS (SELECT 1 FROM purge_fences f WHERE {_FENCE_PREDICATE.format(version='v.id')})"
)

log = get_logger("jobhunter.profile.vectors")


def dedup_key(profile_version_id: int) -> str:
    """The dedup key of the `embed_profile` operation of a version: `embed_profile:<id>`."""
    return f"{KIND}:{profile_version_id}"


async def request_embedding(
    conn: AsyncConnection, profile_version_id: int, *, defer: Deferrer | None
) -> tuple[int, bool]:
    """The live `embed_profile` operation of the version, created (and its job deferred, in the caller's
    transaction) when there is none: `(id, created)`. The payload holds the version id only."""
    return await create_operation(
        conn,
        kind=KIND,
        payload={"profile_version_id": profile_version_id},
        dedup_key=dedup_key(profile_version_id),
        defer=defer,
    )


def vector_texts(profile: ConfirmedProfile) -> list[tuple[str, str]]:
    """The `(kind, text)` pairs a profile is embedded as, in order (module docstring)."""
    texts = [(SUMMARY_KIND, profile.summary)]
    texts.extend((f"title:{i}", title.title) for i, title in enumerate(profile.target_titles))
    return [(kind, text) for kind, text in texts if text.strip()]


@dataclass(frozen=True)
class VersionState:
    """What a run sees of its version: `skipped` when there is nothing to write for it, else its `profile`."""

    skipped: Skipped | None
    profile: ConfirmedProfile | None


async def read_version(conn: AsyncConnection, profile_version_id: int, *, lock: bool = False) -> VersionState:
    """The state of the version; `lock` takes its row `FOR SHARE` first (the write's transaction)."""
    params = {"pv": profile_version_id}
    row = (await conn.execute(_LOCK_VERSION if lock else _READ_VERSION, params)).first()
    if row is None or row.purged_at is not None or row.profile_json is None:
        # a version that delete-everything removed is as gone as a purged one
        return VersionState("purged", None)
    if (await conn.execute(_FENCED, params)).scalar_one():
        return VersionState("fenced", None)
    return VersionState(None, ConfirmedProfile.model_validate(row.profile_json))


def _wanted(texts: list[tuple[str, str]]) -> set[tuple[str, str, bytes, str]]:
    return {(kind, EMBEDDING_MODEL_ID, text_sha256(text), INPUT_KIND) for kind, text in texts}


async def vectors_complete(
    conn: AsyncConnection, profile_version_id: int, texts: list[tuple[str, str]]
) -> bool:
    """Whether the version has exactly the vectors of `texts`, each over the embedding of its text as the
    current model embeds a query: then a run has nothing to embed or write."""
    rows = (await conn.execute(_STORED, {"pv": profile_version_id})).all()
    stored = {(row.kind, row.embedding_model_id, bytes(row.text_sha256), row.input_kind) for row in rows}
    return stored == _wanted(texts)


async def write_vectors(
    conn: AsyncConnection, profile_version_id: int, texts: list[tuple[str, str]], embedded: list[Embedded]
) -> int:
    """Upsert the embeddings and the version's `profile_vectors` of `texts` (`embedded[i]` is the answer for
    `texts[i]`) and delete the kinds it no longer has; the number of vectors. The caller's transaction holds
    the owner row and the version row (`read_version(…, lock=True)`) and ends the operation."""
    kinds: list[str] = []
    for (kind, text), item in zip(texts, embedded, strict=True):
        embedding_id = await upsert_embedding(
            conn,
            model_id=EMBEDDING_MODEL_ID,
            text_sha256=text_sha256(text),
            input_kind=INPUT_KIND,
            vector=item.vector,
        )
        await conn.execute(
            _UPSERT_VECTOR, {"pv": profile_version_id, "kind": kind, "embedding": embedding_id}
        )
        kinds.append(kind)
    await conn.execute(_DELETE_OTHER_KINDS, {"pv": profile_version_id, "kinds": kinds})
    return len(kinds)


# --- the reconciler -----------------------------------------------------------------------------------------


async def _incomplete_active(conn: AsyncConnection) -> int | None:
    """The id of the active profile version when it is usable and its vectors are incomplete, else `None`."""
    row = (await conn.execute(_ACTIVE)).first()
    if row is None:
        return None
    texts = vector_texts(ConfirmedProfile.model_validate(row.profile_json))
    if await vectors_complete(conn, int(row.id), texts):
        return None
    return int(row.id)


@reconciler("profile_vectors", "heavy")
async def reconcile_profile_vectors(ctx: ReconcileContext) -> int:
    """§12.6 step 3 and Deviation 23 (module docstring): 1 when it created the active version's
    `embed_profile` operation, else 0. Fenced by the worker epoch before it reads anything: a superseded
    worker raises `FencedOut` (`fence_epoch` logged it), which `run_reconcilers` takes as the end of its
    cadence."""
    async with ctx.engine.begin() as conn:
        await fence_epoch(conn, ctx.epoch)
        version_id = await _incomplete_active(conn)
    if version_id is None:
        return 0
    client = ctx.embedder
    if client is None:
        log.warning("profile_vectors_skipped", reason="no_embedder", version_id=version_id)
        return 0
    health = await client.health()
    if not health.ok:
        log.info(
            "profile_vectors_skipped",
            reason="embedder_unhealthy",
            version_id=version_id,
            checksum_ok=health.checksum_ok,
        )
        return 0
    async with ctx.engine.begin() as conn:
        await fence_epoch(conn, ctx.epoch)
        operation_id, created = await request_embedding(conn, version_id, defer=ctx.defer)
    if created:
        log.info("profile_vectors_requested", version_id=version_id, operation_id=operation_id)
    return int(created)
