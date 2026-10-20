"""The guard of a transaction (spec §3.3.3): the step that proves the caller still owns its work.

A `Guard` runs inside the caller's transaction, after the job rows and before any write (Ruling T19-a): the
node passes `ledger.lock_run_owner` for its run, a reconciler `epoch.fence_epoch` for its worker epoch. It
raises `FencedOut`, having written nothing, when the work is no longer the caller's.
"""

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncConnection

type Guard = Callable[[AsyncConnection], Awaitable[None]]
