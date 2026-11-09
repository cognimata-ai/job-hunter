"""The active settings version, locked (spec §9.1; plan Task 22 fix rounds 1 and 2).

`lock_active` takes `active_config` `FOR UPDATE` (`FOR SHARE` with `share=True`, for the readers: the spend
ledger's reservation path), the first lock of §9.1, and then reads the active settings version's document.
The two are separate statements on purpose. PostgreSQL (READ COMMITTED) rechecks a locked row against its
newest version when the lock had to wait; but a row it is joined to is looked up again only in the snapshot
the statement started with. A statement that locks `active_config` joined to `settings_versions`, and that
waited behind a settings save, therefore meets the pointer of the save and cannot see the version row the
save inserted: it yields no row, and its `.one()` raises (a 500). A statement of its own for the lock returns
the pointer as the save left it, and the next statement, with a fresh snapshot, sees that version.

`settings.save.save_settings` follows the same rule (its own lock statement, then the version read); callers
that edit the active document (the watchlist routes, `queries.authoring.edit_query`), `POST
/api/settings/scope/retry-apply` and `llm.spend_ledger.current_params` (`share=True`) use this helper. A
caller that already holds the lock makes the first statement a no-op. The rule holds for any statement that
locks `active_config` and follows one of its pointers into another table:
`scoring.filters.lock_active_scopes` reads its scope rows in a second statement for it.
"""

from dataclasses import dataclass
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.types import JSONValue

_POINTERS: Final = (
    "SELECT settings_version_id, scope_version_id, profile_version_id FROM active_config WHERE id = 1"
)
_LOCK: Final = q(_POINTERS + " FOR UPDATE")
_LOCK_SHARED: Final = q(_POINTERS + " FOR SHARE")
_PAYLOAD: Final = q("SELECT payload FROM settings_versions WHERE id = :id")


@dataclass(frozen=True)
class LockedActive:
    """`active_config`'s pointers as they stand under the lock, and the active version's stored document."""

    settings_version_id: int
    scope_version_id: int
    profile_version_id: int | None
    payload: JSONValue


async def lock_active(conn: AsyncConnection, *, share: bool = False) -> LockedActive:
    """Lock `active_config` (`FOR SHARE` when `share`, else `FOR UPDATE`) and read the active settings version
    (module docstring); `LookupError` when the database has no active configuration."""
    pointers = (await conn.execute(_LOCK_SHARED if share else _LOCK)).first()
    if pointers is None:
        raise LookupError("there is no active configuration (migrate seeds it)")
    payload = (await conn.execute(_PAYLOAD, {"id": pointers.settings_version_id})).scalar_one()
    return LockedActive(
        settings_version_id=int(pointers.settings_version_id),
        scope_version_id=int(pointers.scope_version_id),
        profile_version_id=None if pointers.profile_version_id is None else int(pointers.profile_version_id),
        payload=payload,
    )
