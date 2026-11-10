"""The write guard of a guarded api request (spec §20.2, §20.4; plan M5 Task 8, Deviation 15, P3, D15).

Delete-everything commits its `global` purge fence in its first transaction (A0) and then waits for every
transaction that began before that commit (the quiesce, Task 9). The api's edge refuses a state-changing
request that meets the committed fence (`api.edge`); a request it let through before the commit is guarded
here. While `WRITE_GUARD` is set, every transaction the request begins on an engine with the guard installed
reads the global fence as its first statement, and an active fence raises `delete_everything_in_progress()`
before anything is written. A transaction that began before the fence committed is one the quiesce waits
for, and one that begins after it reads the committed fence: no write of a guarded request lands after A0
unseen, those of the routes that take no `active_config` (the CV upload and paste) included.

- `WRITE_GUARD`: the context variable the edge sets for the rest of a guarded request (default `False`).
  A task the request starts inherits it; a transaction outside any request (the worker's, the lifespan's
  heartbeat watch) never sees it set.
- `install_fence_guard(engine)`: registers the `begin` listener on `engine.sync_engine`, once (a second call
  finds it with `event.contains`). SQLAlchemy dispatches `begin` inside `Connection._begin_impl`, before the
  driver's own begin and with autobegin off for that call, so the listener's statement (`db.guards`'
  `GLOBAL_FENCE_SQL`, a driver statement) is the transaction's first. The async engine runs the listener in
  a greenlet that carries the calling task's context (`sqlalchemy/util/concurrency.py`, SQLAlchemy 2.1.4),
  so it reads the request's `WRITE_GUARD`. A refused transaction never began on SQLAlchemy's side: the read
  stays in the driver's transaction, which the pool's reset rolls back when the connection is returned.
- `suspended()`: clears `WRITE_GUARD` for its block. Two transactions of a guarded request run without it:
  the alert of a 500 (`api.errors.persist_unhandled_alert`, written even under the fence) and a read
  snapshot (`db.snapshot.read_snapshot`), whose `SET TRANSACTION ISOLATION LEVEL …` must be its first
  statement and which refuses every write itself (SQLSTATE 25006).
- `delete_everything_in_progress()`: the refusal, 409 `delete_everything_in_progress`, which the edge answers
  too (through `api.errors.problem_of`) and `privacy.fences.refuse_under_global_fence` raises.

It lives in `db/` so that `db.snapshot` uses it without importing the api.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Final

from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.db.guards import GLOBAL_FENCE_SQL
from jobhunter.errors import Refusal
from jobhunter.observability.logging import get_logger

WRITE_GUARD: Final[ContextVar[bool]] = ContextVar("jobhunter_write_guard", default=False)

log = get_logger("jobhunter.db.fence_guard")


def delete_everything_in_progress() -> Refusal:
    """The refusal of a write while delete-everything's `global` fence stands (module docstring)."""
    return Refusal(
        409,
        "delete_everything_in_progress",
        "Everything is being deleted",
        detail="Delete everything is in progress; nothing can be changed until it has finished.",
    )


def _guard_begin(conn: Connection) -> None:
    """The `begin` listener: in a guarded request, the global fence read first, and an active one refused."""
    if WRITE_GUARD.get() and conn.exec_driver_sql(GLOBAL_FENCE_SQL).scalar_one():
        log.info("write_refused_global_fence")
        raise delete_everything_in_progress()


def install_fence_guard(engine: AsyncEngine) -> None:
    """Guard every transaction of `engine` that a guarded request begins (module docstring); idempotent."""
    target = engine.sync_engine
    if not event.contains(target, "begin", _guard_begin):
        event.listen(target, "begin", _guard_begin)


@contextmanager
def suspended() -> Iterator[None]:
    """The block without the guard (module docstring): `WRITE_GUARD` cleared, and set back afterwards."""
    token = WRITE_GUARD.set(False)
    try:
        yield
    finally:
        WRITE_GUARD.reset(token)
