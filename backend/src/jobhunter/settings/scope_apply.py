"""Scope apply: precompute, then flip (spec §13.4; plan Tasks 17 and 18, Deviation 12).

A save of class `scope` points `active_config.pending_scope_version_id` at the desired scope and records an
`apply_scope` operation in the same transaction; the operation precomputes the scope's rows and flips the
active scope once they are complete. This first part (Task 17) is what the writers of the pending pointer
need:

- `KIND` and `dedup_key(scope_version_id)` (`"apply_scope:<id>"`): one live operation per scope version;
- `request_apply(conn, scope_version_id, *, defer)`: the `create_operation` wrapper, payload
  `{"scope_version_id": id}`. The live operation of that scope, when there is one, is returned (`created`
  false) and its row is locked `FOR UPDATE` first, so it cannot end in between: an apply ends `succeeded` as
  obsolete (§13.4 step 3) only in a transaction that holds its own row and has then read the pending pointer
  (Task 18's handler), so one this function returns sees a pointer that names its scope;
- `set_desired_scope(conn, desired_id, *, defer)`: the one place that moves the pending pointer towards a
  desired scope (the settings save, Task 21's profile confirm and Task 18's retry-apply). The caller holds
  `active_config FOR UPDATE` (§9.1: the first lock; it is taken again here, a no-op, before the pointers are
  read). It returns the `apply_scope` operation's id, or `None` when no apply is needed:
  - the desired scope is the active one (a revert): `pending := NULL`, as the CHECK forbids pending = active;
  - it is the pending one: no change, and the live operation's id (`request_apply`; when that apply has just
    failed for good and the reconciler has not cleared the pointer yet, this requests a new one, as
    retry-apply would);
  - otherwise: `pending := desired` and `request_apply`.
  A pending apply of another scope is left as it is: it ends obsolete at its own final check (§13.4 step 3).

Task 18 adds the handler, `incomplete_jobs`, the reconciler and the route, and registers the module with the
worker; until then an `apply_scope` operation waits `pending` without a handler. Lock order (§9.1):
`active_config`, then the operation row (the owner row), then the new operation and its job. Logs carry ids
only.
"""

from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.observability.logging import get_logger
from jobhunter.worker.operations import create_operation
from jobhunter.worker.tasks import Deferrer

KIND: Final = "apply_scope"

log = get_logger("jobhunter.settings.scope_apply")

_POINTERS: Final = q(
    "SELECT scope_version_id, pending_scope_version_id FROM active_config WHERE id = 1 FOR UPDATE"
)
_SET_PENDING: Final = q(
    "UPDATE active_config SET pending_scope_version_id = :pending, updated_at = now() WHERE id = 1"
)
_LOCK_LIVE: Final = q(
    "SELECT id FROM operations WHERE kind = :kind AND dedup_key = :dedup_key "
    "AND status IN ('pending', 'running') FOR UPDATE"
)


def dedup_key(scope_version_id: int) -> str:
    """The dedup key of the apply of `scope_version_id`: `apply_scope:<id>` (§13.4 step 1)."""
    return f"{KIND}:{scope_version_id}"


async def request_apply(conn: AsyncConnection, scope_version_id: int, *, defer: Deferrer) -> tuple[int, bool]:
    """The live `apply_scope` operation of `scope_version_id`, created (and its job deferred) when there is
    none: `(id, created)` (module docstring)."""
    key = dedup_key(scope_version_id)
    await conn.execute(_LOCK_LIVE, {"kind": KIND, "dedup_key": key})
    operation_id, created = await create_operation(
        conn, kind=KIND, payload={"scope_version_id": scope_version_id}, dedup_key=key, defer=defer
    )
    log.info(
        "scope_apply_requested", scope_version_id=scope_version_id, operation_id=operation_id, created=created
    )
    return operation_id, created


async def set_desired_scope(conn: AsyncConnection, desired_id: int, *, defer: Deferrer) -> int | None:
    """Move the pending scope pointer towards `desired_id`; the `apply_scope` operation's id, or `None` when
    no apply is needed (module docstring)."""
    pointers = (await conn.execute(_POINTERS)).one()
    if desired_id == pointers.scope_version_id:
        if pointers.pending_scope_version_id is not None:
            await conn.execute(_SET_PENDING, {"pending": None})
            log.info(
                "pending_scope_cleared",
                scope_version_id=desired_id,
                replaced_scope_version_id=pointers.pending_scope_version_id,
            )
        return None
    if desired_id != pointers.pending_scope_version_id:
        await conn.execute(_SET_PENDING, {"pending": desired_id})
    operation_id, _ = await request_apply(conn, desired_id, defer=defer)
    return operation_id
