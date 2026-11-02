"""The node modules of the collection graph (spec §9.2); each `make_*(deps)` builds its node.

`Node` is the type of a node over the graph's state: LangGraph's node protocols name the parameter `state`,
which `mypy --strict` matches, so a plain `Callable` does not fit.

`run_started_at(conn, run_id)` is the run's `started_at` (its first attempt's start), from which nodes 8 and 9
and the run report take "of the run" for the rows without a run column; `LookupError` for a run that has not
started.
"""

from datetime import datetime
from typing import Final, Protocol

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.graphs.collect.state import CollectState, CollectUpdate

_STARTED_AT: Final = q("SELECT started_at FROM runs WHERE id = :run")


class Node(Protocol):
    async def __call__(self, state: CollectState) -> CollectUpdate: ...


async def run_started_at(conn: AsyncConnection, run_id: int) -> datetime:
    """The run's `started_at` (module docstring)."""
    started: datetime | None = (await conn.execute(_STARTED_AT, {"run": run_id})).scalar_one_or_none()
    if started is None:
        raise LookupError(f"run {run_id} has not started")
    return started
