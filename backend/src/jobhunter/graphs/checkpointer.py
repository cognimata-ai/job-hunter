"""The LangGraph PostgreSQL checkpointer: `migrate` sets up its tables, the worker opens its saver."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

# Every connection of the saver is autocommit (`CREATE INDEX CONCURRENTLY` cannot run in a transaction and
# the saver manages its own), does not prepare statements server-side and returns rows as dicts.
_POOL_MIN_SIZE: Final = 1
_POOL_MAX_SIZE: Final = 4


async def setup_checkpointer(dsn: str) -> None:
    """Create or upgrade the checkpoint tables (idempotent), on one dedicated autocommit connection."""
    async with await AsyncConnection.connect(
        dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row
    ) as conn:
        await AsyncPostgresSaver(conn).setup()


@asynccontextmanager
async def open_saver(dsn: str) -> AsyncIterator[AsyncPostgresSaver]:
    """A saver over a small pool (1 to 4 connections) that is closed when the block ends."""
    pool: AsyncConnectionPool[AsyncConnection[DictRow]] = AsyncConnectionPool(
        conninfo=dsn,
        min_size=_POOL_MIN_SIZE,
        max_size=_POOL_MAX_SIZE,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=False,
    )
    await pool.open(wait=True)
    try:
        yield AsyncPostgresSaver(pool)
    finally:
        await pool.close()
