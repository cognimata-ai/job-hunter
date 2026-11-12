"""The rollback compatibility floor of every schema revision (plan M5 Task 1; Task 27 reads it).

A migration module may declare two constants beside its revision:

    compat_floor: Final[str]   the oldest revision whose code still runs on the schema this one leaves
    destructive: Final[bool]   whether its upgrade removes or rewrites what an older revision's code reads

so that a release can be rolled back to the code of any revision from the floor on without a downgrade. A
module that declares neither (0001-0009, written before the record existed) is its own floor and not
destructive: no older code is promised to run on it.

`chain_compat()` reads the declarations of every revision of the package, base first, and refuses
(`ValueError`) a set the rollback could not rely on:

- a floor that is not the revision itself or one of its ancestors;
- a destructive revision whose floor is not itself: older code reads what it removed or rewrote;
- a floor older than a parent's floor. Floors only move forward along the chain, so no floor reaches past an
  earlier destructive revision, and the code of any revision from a head's floor on runs on that head.

`migrate` validates the chain before Alembic moves the schema, and records it in `schema_compat`
(`record_compat`) right after Alembic has upgraded to the head.

**An older release on a newer schema** (Task 27, M5-R4 R2, Deviation 32). `schema_state` tells the code of a
release where the database stands to it, from `alembic_version` and, for a head the code does not know, that
head's `schema_compat` row, which the newer release's `migrate` recorded:

- `current`: the database's head is the code's;
- `behind`: every head is in the code's chain (an empty database has none): `migrate` upgrades it;
- `ahead_compatible`: one head the code does not know, whose row names a floor in the code's chain (at or
  before the code's head) and is not destructive. The code runs on it: `migrate` skips the alembic step
  and `/readyz` passes, so a rollback is a redeploy of the previous release's images;
- `ahead_incompatible`: anything else, a destructive head, a floor the code does not know, a head without
  a row (or several heads): `migrate` refuses before any step and `/readyz` fails; the RUNBOOK rolls back
  by restore.

`classify_schema` is that rule without the database. The floors only move forward along a validated chain,
so a floor the older code knows also means that no destructive revision lies between its head and the
database's. `schema_compat` versions the schema only, not the transition matrix (an older release on a newer
schema keeps the newer one, `seed_defaults(sync_matrix=False)`) nor the manifest stream (step 1 and step 7 of
`migrate` parse it). A release whose revision narrows a transition edge of an existing table declares that
revision `destructive`, so its rollback is a restore: with a compatible floor the older release would pass
`/readyz` and fail later, at a refused `transition()`. A change of the manifest format ships expand/contract
instead (spec §19.6): release N teaches the parser the new format while it still writes the old one, and
release N+1 writes the new format. A rollback from N+1 therefore lands on N, whose restore reads both, so no
release ever needs a restore its own parser cannot replay (one that skipped release N would stop its restore
at a `ManifestInvalid`).
"""

import functools
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from types import ModuleType
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.alembic_runner import chain_modules
from jobhunter.db.sql import q

SchemaState = Literal["current", "behind", "ahead_compatible", "ahead_incompatible"]


@dataclass(frozen=True)
class CompatInfo:
    """One revision's record: its floor and whether its upgrade is destructive."""

    revision: str
    compat_floor: str
    destructive: bool


def _parents(module: ModuleType) -> tuple[str, ...]:
    """The revisions `module` revises: none for the base, one on a linear chain, several for a merge."""
    down = module.down_revision
    if down is None:
        return ()
    if isinstance(down, str):
        return (down,)
    return tuple(down)


def _ancestors_or_self(revision: str, parents: dict[str, tuple[str, ...]]) -> set[str]:
    """`revision` and every revision it descends from, along `parents`."""
    found: set[str] = set()
    pending = [revision]
    while pending:
        current = pending.pop()
        if current not in found:
            found.add(current)
            pending.extend(parents.get(current, ()))
    return found


def compat_of(modules: Sequence[ModuleType]) -> tuple[CompatInfo, ...]:
    """The record of each of `modules` (migration modules, base first), in their order.

    A module without `compat_floor` is its own floor; one without `destructive` is not destructive. Raises
    `ValueError` for a declaration of the wrong type, and for a floor that:

    - is not the revision itself or one of its ancestors among `modules`: the code of a later revision, or of
      another branch, cannot be promised to run on a schema it never saw;
    - is not the revision itself while the revision is destructive;
    - is older than the floor of a parent among `modules` (not a descendant-or-self of it): the parent's
      schema change, which code before the parent's floor cannot read, is still in this revision's schema.
    """
    parents = {str(module.revision): _parents(module) for module in modules}
    infos: dict[str, CompatInfo] = {}
    for module in modules:
        revision = str(module.revision)
        floor = getattr(module, "compat_floor", revision)
        destructive = getattr(module, "destructive", False)
        if not isinstance(floor, str):
            raise ValueError(f"{revision}: compat_floor must be a str, got {type(floor).__name__}")
        if not isinstance(destructive, bool):
            raise ValueError(f"{revision}: destructive must be a bool, got {type(destructive).__name__}")
        infos[revision] = CompatInfo(revision, floor, destructive)
    for info in infos.values():
        revision, floor = info.revision, info.compat_floor
        floor_ancestry = _ancestors_or_self(floor, parents)
        if floor not in _ancestors_or_self(revision, parents):
            raise ValueError(
                f"{revision}: compat_floor {floor!r} is neither the revision nor an ancestor of it"
            )
        if info.destructive and floor != revision:
            raise ValueError(
                f"{revision}: destructive, so its compat_floor must be {revision!r}, not {floor!r}"
            )
        for parent in parents[revision]:
            parent_info = infos.get(parent)
            if parent_info is not None and parent_info.compat_floor not in floor_ancestry:
                raise ValueError(
                    f"{revision}: compat_floor {floor!r} is older than the floor "
                    f"{parent_info.compat_floor!r} of its parent {parent!r}"
                )
    return tuple(infos.values())


def chain_compat() -> tuple[CompatInfo, ...]:
    """The record of every revision of this package's migration chain, base first."""
    return compat_of(chain_modules())


# One statement for the whole chain. A row that already holds the chain's values is not written again, so a
# second `migrate` changes nothing (`recorded_at` and the row version included); a changed one is rewritten
# with a new `recorded_at`. Rows of revisions this code does not know (a newer release's, after a rollback)
# are kept.
_UPSERT: Final = q(
    "INSERT INTO schema_compat (revision, compat_floor, destructive) "
    "SELECT * FROM unnest(CAST(:revisions AS text[]), CAST(:floors AS text[]), "
    "CAST(:destructive AS boolean[])) "
    "ON CONFLICT (revision) DO UPDATE SET compat_floor = EXCLUDED.compat_floor, "
    "destructive = EXCLUDED.destructive, recorded_at = now() "
    "WHERE (schema_compat.compat_floor, schema_compat.destructive) "
    "IS DISTINCT FROM (EXCLUDED.compat_floor, EXCLUDED.destructive) "
    "RETURNING revision"
)


async def record_compat(conn: AsyncConnection, *, chain: Sequence[CompatInfo] | None = None) -> int:
    """Upsert the record of every revision of the chain into `schema_compat`; return how many rows it
    inserted or changed (0 when the table already held them all). Idempotent; the caller owns the
    transaction.

    `chain` is a result of `chain_compat()` the caller already validated (`migrate` validates before Alembic
    runs); without it the chain is read here.
    """
    if chain is None:
        chain = chain_compat()
    params = {
        "revisions": [info.revision for info in chain],
        "floors": [info.compat_floor for info in chain],
        "destructive": [info.destructive for info in chain],
    }
    return len((await conn.execute(_UPSERT, params)).all())


def known_revisions(code_head: str, modules: Sequence[ModuleType]) -> frozenset[str]:
    """`code_head` and every revision it descends from among `modules`: the chain of the code whose head is
    `code_head`, the schemas that code knows. A head outside `modules` knows itself alone."""
    return frozenset(_ancestors_or_self(code_head, {str(m.revision): _parents(m) for m in modules}))


@functools.cache
def _code_known(code_head: str) -> frozenset[str]:
    """`known_revisions` over this package's chain, read once per process (the migrations do not change while
    it runs, and `/readyz` asks on every probe of a schema that is not current)."""
    return known_revisions(code_head, chain_modules())


def classify_schema(
    heads: Sequence[str], records: Mapping[str, CompatInfo], *, code_head: str, known: Set[str]
) -> SchemaState:
    """The state of a database whose `alembic_version` holds `heads` to the code of `code_head`, whose chain
    is `known` (`known_revisions`); `records` holds the database's `schema_compat` rows by revision (the
    module docstring has the rule)."""
    if tuple(heads) == (code_head,):
        return "current"
    if all(head in known for head in heads):
        return "behind"
    if len(heads) == 1 and (record := records.get(heads[0])) is not None:
        if not record.destructive and record.compat_floor in known:
            return "ahead_compatible"
    return "ahead_incompatible"


_HAS_VERSIONS: Final = q("SELECT to_regclass('public.alembic_version') IS NOT NULL")
_HEADS: Final = q("SELECT version_num FROM alembic_version ORDER BY version_num")
_HAS_COMPAT: Final = q("SELECT to_regclass('public.schema_compat') IS NOT NULL")
_RECORDS: Final = q(
    "SELECT revision, compat_floor, destructive FROM schema_compat "
    "WHERE revision = ANY(CAST(:heads AS text[]))"
)


async def database_heads(conn: AsyncConnection) -> tuple[str, ...]:
    """The revisions `alembic_version` holds, sorted; none for a database without that table (an empty
    one)."""
    if not (await conn.execute(_HAS_VERSIONS)).scalar_one():
        return ()
    return tuple(str(head) for head in (await conn.execute(_HEADS)).scalars())


async def heads_state(conn: AsyncConnection, heads: Sequence[str], *, code_head: str) -> SchemaState:
    """Where a database whose heads are `heads` (`database_heads` of `conn`) stands to the code whose head is
    `code_head`. Reads only the `schema_compat` rows of the heads the code does not know."""
    if tuple(heads) == (code_head,):
        return "current"
    known = _code_known(code_head)
    unknown = [head for head in heads if head not in known]
    records: dict[str, CompatInfo] = {}
    if unknown and (await conn.execute(_HAS_COMPAT)).scalar_one():
        rows = await conn.execute(_RECORDS, {"heads": unknown})
        records = {row.revision: CompatInfo(row.revision, row.compat_floor, row.destructive) for row in rows}
    return classify_schema(heads, records, code_head=code_head, known=known)


async def schema_state(conn: AsyncConnection, *, code_head: str) -> SchemaState:
    """Where the database of `conn` stands to the code whose head is `code_head` (module docstring). Reads
    only."""
    return await heads_state(conn, await database_heads(conn), code_head=code_head)
