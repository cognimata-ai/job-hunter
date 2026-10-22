"""The active settings version, locked (spec §9.1; plan Task 22 fix round 1).

`lock_active` takes `active_config` `FOR UPDATE`, the first lock of §9.1, and then reads the active settings
version's document. The two are separate statements on purpose. PostgreSQL (READ COMMITTED) rechecks a locked
row against its newest version when the lock had to wait; but a row it is joined to is looked up again only in
the snapshot the statement started with. A statement that locks `active_config` joined to `settings_versions`,
and that waited behind a settings save, therefore meets the pointer of the save and cannot see the version row
the save inserted: it yields no row, and its `.one()` raises (a 500). A statement of its own for the lock
returns the pointer as the save left it, and the next statement, with a fresh snapshot, sees that version.

`settings.save.save_settings` follows the same rule (its own lock statement, then the version read); callers
that edit the active document (the watchlist routes, `queries.authoring.edit_query`) and `POST
/api/settings/scope/retry-apply` use this helper. A caller that already holds the lock makes the first
statement a no-op.
"""

from dataclasses import dataclass
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.types import JSONValue

_LOCK: Final = q(
    "SELECT settings_version_id, scope_version_id, profile_version_id FROM active_config "
    "WHERE id = 1 FOR UPDATE"
)
_PAYLOAD: Final = q("SELECT payload FROM settings_versions WHERE id = :id")


@dataclass(frozen=True)
class LockedActive:
    """`active_config`'s pointers as they stand under the lock, and the active version's stored document."""

    settings_version_id: int
    scope_version_id: int
    profile_version_id: int | None
    payload: JSONValue


async def lock_active(conn: AsyncConnection) -> LockedActive:
    """Lock `active_config` and read the active settings version (module docstring)."""
    pointers = (await conn.execute(_LOCK)).one()
    payload = (await conn.execute(_PAYLOAD, {"id": pointers.settings_version_id})).scalar_one()
    return LockedActive(
        settings_version_id=int(pointers.settings_version_id),
        scope_version_id=int(pointers.scope_version_id),
        profile_version_id=None if pointers.profile_version_id is None else int(pointers.profile_version_id),
        payload=payload,
    )
