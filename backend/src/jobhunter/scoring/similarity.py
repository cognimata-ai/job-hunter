"""The similarity of a revision to a profile (spec §9.2 node 9, §9.6.2, §12.6 step 3, §17.4
`awaiting_embedding`; plan Task 14, L3431, L5514): the admission's similarity lane ranks by it.

`similarity_for(conn, *, job_revision_id, profile_version_id) -> float | None` is the best cosine similarity
of the revision's embedding against the profile's vectors (`profile_vectors`, the summary and each target
title as `query:` texts), `max(1 - (e.vector <=> v.vector))` with pgvector's cosine distance `<=>`, clamped to
`[-1, 1]` (the column's CHECK: two `halfvec` unit vectors can round a hair past it). It is `None` while the
revision awaits its embedding and while the profile has no vectors (the embedder was down at its confirm).

`fill_missing_similarity(conn, *, llm_tuple_id, now, limit=500) -> int` sets `similarity` (and `updated_at` to
the caller's `now`) on up to `limit` of the tuple's unpublished, uncancelled targets that have none, once
their revision is embedded and the tuple's profile has vectors; how many it set. The rows are taken in id
order `FOR UPDATE SKIP LOCKED` (other domain rows: a target another transaction holds is left for the next
pass), in the caller's transaction. The `awaiting_embedding` reconciler calls it (Task 15).

Both read vectors only; nothing here logs.
"""

from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q

# The best cosine of revision `{revision}` against profile `{profile}`'s vectors, each clamped before the
# aggregate (`greatest` ignores a NULL, so the clamp must not see the aggregate of no rows): NULL without an
# embedding (the join to `embeddings` finds none) or without a vector. Real, as the column.
SIMILARITY_SQL: Final = (
    "(SELECT CAST(max(least(1, greatest(-1, 1 - (e.vector <=> pe.vector)))) AS real) "
    "FROM job_revisions r JOIN embeddings e ON e.id = r.embedding_id "
    "JOIN profile_vectors v ON v.profile_version_id = {profile} JOIN embeddings pe ON pe.id = v.embedding_id "
    "WHERE r.id = {revision})"
)
_SIMILARITY: Final = q("SELECT " + SIMILARITY_SQL.format(profile=":profile", revision=":revision"))
_PROFILE_HAS_VECTORS: Final = q(
    "SELECT u.profile_version_id FROM llm_tuples u WHERE u.id = :tuple "
    "AND EXISTS (SELECT 1 FROM profile_vectors v WHERE v.profile_version_id = u.profile_version_id)"
)
# The candidates are locked in a materialised CTE, evaluated once: inside `t.id IN (…)` the planner may
# rescan a `LIMIT … FOR UPDATE SKIP LOCKED` subquery for each target row, and each rescan skips the rows the
# statement has set already and takes the next ones, so more than `limit` rows would be set.
_FILL: Final = q(
    "WITH picked AS MATERIALIZED (SELECT s.id FROM score_targets s "  # noqa: S608 (the module's constant)
    "JOIN job_revisions r ON r.id = s.job_revision_id WHERE s.llm_tuple_id = :tuple AND s.similarity IS NULL "
    "AND r.embedding_id IS NOT NULL "
    "AND s.state NOT IN ('published_accepted', 'published_contested', 'cancelled') "
    "ORDER BY s.id LIMIT :limit FOR UPDATE OF s SKIP LOCKED) "
    "UPDATE score_targets t SET similarity = "
    + SIMILARITY_SQL.format(profile=":profile", revision="t.job_revision_id")
    + ", updated_at = :now FROM picked WHERE t.id = picked.id RETURNING t.id"
)


async def similarity_for(
    conn: AsyncConnection, *, job_revision_id: int, profile_version_id: int
) -> float | None:
    """The revision's best cosine against the profile's vectors (module docstring); `None` while it awaits its
    embedding or the profile has no vectors."""
    found = (
        await conn.execute(_SIMILARITY, {"profile": profile_version_id, "revision": job_revision_id})
    ).scalar_one()
    return None if found is None else float(found)


async def fill_missing_similarity(
    conn: AsyncConnection, *, llm_tuple_id: int, now: datetime, limit: int = 500
) -> int:
    """Set the missing similarity of up to `limit` targets of the tuple whose revision is embedded (module
    docstring); how many it set (0 while the tuple's profile has no vectors)."""
    profile = (await conn.execute(_PROFILE_HAS_VECTORS, {"tuple": llm_tuple_id})).scalar_one_or_none()
    if profile is None:
        return 0
    params = {"profile": int(profile), "tuple": llm_tuple_id, "limit": limit, "now": now}
    return len((await conn.execute(_FILL, params)).all())
