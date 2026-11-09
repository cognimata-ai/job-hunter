"""The rollback compatibility floor of every schema revision (plan M5 Task 1; Task 27 reads it).

A migration module may declare two constants beside its revision:

    compat_floor: Final[str]   the oldest revision whose code still runs on the schema this one leaves
    destructive: Final[bool]   whether its upgrade removes or rewrites what an older revision's code reads

so that a release can be rolled back to the code of any revision from the floor on without a downgrade. A
module that declares neither (0001-0009, written before the record existed) is its own floor and not
destructive: no older code is promised to run on it.

`chain_compat()` reads the declarations of every revision of the package, base first, and refuses a floor that
is not the revision itself or one of its ancestors (`ValueError`). `migrate` records them in `schema_compat`
(`record_compat`) right after Alembic has upgraded to the head.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from types import ModuleType
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.alembic_runner import chain_modules
from jobhunter.db.sql import q


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
    `ValueError` for a declaration of the wrong type, and for a floor that is not the revision itself or one
    of its ancestors among `modules`: the code of a later revision, or of another branch, cannot be promised
    to run on a schema it never saw.
    """
    parents = {str(module.revision): _parents(module) for module in modules}
    infos: list[CompatInfo] = []
    for module in modules:
        revision = str(module.revision)
        floor = getattr(module, "compat_floor", revision)
        destructive = getattr(module, "destructive", False)
        if not isinstance(floor, str):
            raise ValueError(f"{revision}: compat_floor must be a str, got {type(floor).__name__}")
        if not isinstance(destructive, bool):
            raise ValueError(f"{revision}: destructive must be a bool, got {type(destructive).__name__}")
        if floor not in _ancestors_or_self(revision, parents):
            raise ValueError(
                f"{revision}: compat_floor {floor!r} is neither the revision nor an ancestor of it"
            )
        infos.append(CompatInfo(revision, floor, destructive))
    return tuple(infos)


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


async def record_compat(conn: AsyncConnection) -> int:
    """Upsert the record of every revision of the chain into `schema_compat`; return how many rows it
    inserted or changed (0 when the table already held them all). Idempotent; the caller owns the
    transaction."""
    chain = chain_compat()
    params = {
        "revisions": [info.revision for info in chain],
        "floors": [info.compat_floor for info in chain],
        "destructive": [info.destructive for info in chain],
    }
    return len((await conn.execute(_UPSERT, params)).all())
