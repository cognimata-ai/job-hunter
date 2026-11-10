"""The read snapshot of a list page (spec §14.3 L5079, §7.1 L1255; plan B13).

A page and the ranking generation it carries are read in one read-only `REPEATABLE READ` transaction: every
statement in it sees the database as of its first query, so the generation returned with a page always
describes exactly the rows on it, whatever commits meanwhile. `SET TRANSACTION` is the transaction's first
statement (PostgreSQL takes the snapshot at the first query after it, and refuses the setting later); the
transaction is read-only, so PostgreSQL refuses any write and any `SELECT … FOR UPDATE | SHARE` in it
(SQLSTATE 25006) and it takes no row lock. It is rolled back when the block ends, also when the block raises.

It begins outside the write guard of a guarded api request (`db.fence_guard.suspended`, M5 Task 8): the
guard's read of delete-everything's fence would be the transaction's first statement, after which PostgreSQL
refuses the isolation level, and a read-only transaction writes nothing for the guard to stop. So a write
route that answers from a snapshot after its commit (the group split, Score individually) answers it even
when the fence committed in between.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.db.fence_guard import suspended
from jobhunter.db.sql import q

_SNAPSHOT: Final = q("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")


@asynccontextmanager
async def read_snapshot(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A connection of `engine` in a read-only `REPEATABLE READ` transaction, rolled back at exit."""
    async with engine.connect() as conn:
        with suspended():  # the guard's read would come first (module docstring)
            await conn.execute(_SNAPSHOT)  # begins the transaction: its first statement
        try:
            yield conn
        finally:
            await conn.rollback()
