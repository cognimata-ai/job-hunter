"""The guard of a transaction (spec §3.3.3): the step that proves the caller still owns its work.

A `Guard` runs inside the caller's transaction, after the job rows and before any write (Ruling T19-a): the
node passes `ledger.lock_run_owner` for its run, a reconciler `epoch.fence_epoch` for its worker epoch. It
raises `FencedOut`, having written nothing, when the work is no longer the caller's.

`global_fence_active(conn)` is the one statement of "a `global` purge fence is active" (§20.2: a
`purge_fences` row of kind `global` not yet released), which the owner checks and the pause guards read.
"""

from collections.abc import Awaitable, Callable
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q

type Guard = Callable[[AsyncConnection], Awaitable[None]]

_GLOBAL_FENCE: Final = q(
    "SELECT EXISTS (SELECT 1 FROM purge_fences WHERE kind = 'global' AND released_at IS NULL)"
)


async def global_fence_active(conn: AsyncConnection) -> bool:
    """Whether a `global` purge fence is active (module docstring), read in the caller's transaction."""
    return bool((await conn.execute(_GLOBAL_FENCE)).scalar_one())
