"""The ranking generation (spec §7.1, §14.3): the committed counter that labels the pages of the Jobs list.

Every transaction that can change the order or the membership of the list (a job inserted, a job field that
a filter or sort reads, a current revision, an availability, a merge, a group, a target or user action, ...)
calls `bump` as its **last statement before commit**: the `ranking_state` row is the last lock of the §3.3.3
order and is held for microseconds. The list API reads the generation and the page rows in one snapshot.
"""

from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q

_BUMP: Final = q("UPDATE ranking_state SET generation = generation + 1, updated_at = now() WHERE id = 1")


async def bump(conn: AsyncConnection) -> None:
    """Advance the ranking generation by one in the caller's transaction (its last statement).

    Raises `LookupError` when the `ranking_state` row is missing (`migrate` seeds it).
    """
    if (await conn.execute(_BUMP)).rowcount != 1:
        raise LookupError("ranking_state has no row 1 (migrate seeds it)")
