"""Scope apply: precompute, then flip (spec §13.4; plan Tasks 17 and 18, Deviation 12).

A save of class `scope` points `active_config.pending_scope_version_id` at the desired scope and records an
`apply_scope` operation in the same transaction; the operation precomputes the scope's rows and flips the
active scope once they are complete. This first part (Task 17) is what the writers of the pending pointer
need:

- `KIND` and `dedup_key(scope_version_id)` (`"apply_scope:<id>"`): at most one live operation per scope
  version, which the partial unique index `operations_live_dedup` enforces;
- `request_apply(conn, scope_version_id, *, defer)`: the `create_operation` wrapper, payload
  `{"scope_version_id": id}`. It inserts a `pending` operation and defers its job, or, when the scope has a
  live (`pending` or `running`) operation, returns that one's id (`created` false) through the dedup key;
- `set_desired_scope(conn, desired_id, *, now, defer)`: the one place that moves the pending pointer towards
  a desired scope (the settings save, Task 21's profile confirm and Task 18's retry-apply). The caller holds
  `active_config FOR UPDATE` (§9.1: the first lock; it is taken again here, a no-op, before the pointers are
  read). It returns the `apply_scope` operation's id, or `None` when no apply is needed:
  - the desired scope is the active one (a revert): `pending := NULL`, as the CHECK forbids pending = active;
  - it is the pending one: no change, and the live operation's id (`request_apply`; when that apply has just
    failed for good and the reconciler has not cleared the pointer yet, this requests a new one, as
    retry-apply would);
  - otherwise: `pending := desired` and `request_apply`.
  The pointer's `updated_at` is the caller's `now`. A pending apply of another scope is left as it is.

**The contract Task 18's handler keeps** (§13.4 step 3, Ruling R18-1): an apply decides that it is obsolete
only in its final transaction, which takes `active_config FOR UPDATE`, then its own operation row, then reads
the pending pointer again. A chunk's plain read of the pointer is only a hint to go to that final
transaction. Every writer of the pointer holds `active_config FOR UPDATE` too, so a pointer that a writer
moved to a scope whose live apply it reused is the pointer that apply's final transaction reads: such an apply
never ends obsolete for a pointer that names its scope. No operation row is locked here, so a save never waits
for a running chunk while it holds `active_config`.

Task 18 adds the handler, `incomplete_jobs`, the reconciler and the route, and registers the module with the
worker; until then an `apply_scope` operation waits `pending` without a handler. Lock order (§9.1):
`active_config`, then the new operation and its job. Nothing here logs: the caller logs the ids after its
commit, so no log line names an operation that was rolled back.
"""

from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.worker.operations import create_operation
from jobhunter.worker.tasks import Deferrer

KIND: Final = "apply_scope"

_POINTERS: Final = q(
    "SELECT scope_version_id, pending_scope_version_id FROM active_config WHERE id = 1 FOR UPDATE"
)
_SET_PENDING: Final = q(
    "UPDATE active_config SET pending_scope_version_id = :pending, updated_at = :now WHERE id = 1"
)


def dedup_key(scope_version_id: int) -> str:
    """The dedup key of the apply of `scope_version_id`: `apply_scope:<id>` (§13.4 step 1)."""
    return f"{KIND}:{scope_version_id}"


async def request_apply(conn: AsyncConnection, scope_version_id: int, *, defer: Deferrer) -> tuple[int, bool]:
    """The live `apply_scope` operation of `scope_version_id`, created (and its job deferred) when there is
    none: `(id, created)` (module docstring)."""
    return await create_operation(
        conn,
        kind=KIND,
        payload={"scope_version_id": scope_version_id},
        dedup_key=dedup_key(scope_version_id),
        defer=defer,
    )


async def set_desired_scope(
    conn: AsyncConnection, desired_id: int, *, now: datetime, defer: Deferrer
) -> int | None:
    """Move the pending scope pointer towards `desired_id`; the `apply_scope` operation's id, or `None` when
    no apply is needed (module docstring)."""
    pointers = (await conn.execute(_POINTERS)).one()
    if desired_id == pointers.scope_version_id:
        if pointers.pending_scope_version_id is not None:
            await conn.execute(_SET_PENDING, {"pending": None, "now": now})
        return None
    if desired_id != pointers.pending_scope_version_id:
        await conn.execute(_SET_PENDING, {"pending": desired_id, "now": now})
    operation_id, _ = await request_apply(conn, desired_id, defer=defer)
    return operation_id
