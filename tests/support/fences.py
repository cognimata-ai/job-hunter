"""Purge fences for the tests, each over a deletion manifest of its own (M5 Task 1).

Since migration 0010 `purge_fences.manifest_id` references `deletion_manifests` (M0 Deviation 1), so a fence
names a manifest that exists. `insert_test_fence` writes one of the kind of purge that raises the fence, in
`executing` as a purge's manifest is while its fence stands, and then the fence:

- `TEST_MANIFEST_SQL`: the manifest insert, with a `:kind` bind and returning the id; the synchronous schema
  tests run it on their own connections;
- `insert_test_manifest(conn, *, kind)`: that insert on an async connection; the manifest's id;
- `insert_test_fence(conn, *, kind, source_id, profile_version_id, job_ids, released)`: a manifest of the
  matching kind (`delete_everything`, `source_purge`, `profile_purge`, `job_tombstone`), then the fence over
  it, released (`released_at = now()`) when `released`; the fence's id.

Neither commits: the caller owns the transaction.
"""

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Final, Literal

from jobhunter.db.sql import q
from sqlalchemy.ext.asyncio import AsyncConnection

FenceKind = Literal["global", "source", "profile", "jobs"]

TEST_MANIFEST_SQL: Final = (
    "INSERT INTO deletion_manifests (manifest_uuid, kind, status) "
    "VALUES (gen_random_uuid(), :kind, 'executing') RETURNING id"
)
# The manifest kind of the purge that raises each kind of fence (§7.14, §19.5).
MANIFEST_KIND: Final[Mapping[FenceKind, str]] = MappingProxyType(
    {
        "global": "delete_everything",
        "source": "source_purge",
        "profile": "profile_purge",
        "jobs": "job_tombstone",
    }
)
_INSERT_MANIFEST: Final = q(TEST_MANIFEST_SQL)
_INSERT_FENCE: Final = q(
    "INSERT INTO purge_fences (kind, source_id, profile_version_id, job_ids, manifest_id, released_at) "
    "VALUES (:kind, :source_id, :profile_version_id, CAST(:job_ids AS bigint[]), :manifest_id, "
    "CASE WHEN CAST(:released AS boolean) THEN now() END) RETURNING id"
)


async def insert_test_manifest(conn: AsyncConnection, *, kind: str) -> int:
    """An `executing` deletion manifest of `kind` with a fresh UUID; its id."""
    return int((await conn.execute(_INSERT_MANIFEST, {"kind": kind})).scalar_one())


async def insert_test_fence(
    conn: AsyncConnection,
    *,
    kind: FenceKind,
    source_id: str | None = None,
    profile_version_id: int | None = None,
    job_ids: Sequence[int] = (),
    released: bool = False,
) -> int:
    """A purge fence of `kind` over a manifest of its own (`MANIFEST_KIND`); the fence's id.

    The arguments are the fence's columns as they are: the table's CHECKs decide which a kind needs (a
    `source` fence its source, a `profile` fence its version, a `jobs` fence one job at least).
    """
    manifest_id = await insert_test_manifest(conn, kind=MANIFEST_KIND[kind])
    params = {
        "kind": kind,
        "source_id": source_id,
        "profile_version_id": profile_version_id,
        "job_ids": list(job_ids),
        "manifest_id": manifest_id,
        "released": released,
    }
    return int((await conn.execute(_INSERT_FENCE, params)).scalar_one())
