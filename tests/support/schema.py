"""Helpers of the schema tests: the foreign-key catalogue query and the refusal assertions.

The spec leaves its CHECKs unnamed, so `refused_check` identifies the CHECK that fired by what its definition
says, which shows that the intended CHECK refused the row and not another one.
"""

from typing import Final

import pytest
import sqlalchemy as sa
from jobhunter.db.sql import CHECK_VIOLATION, PgErrorInfo, pg_error
from sqlalchemy.ext.asyncio import AsyncConnection

# Every foreign key column with its ON DELETE action: 'a' NO ACTION, 'n' SET NULL, 'c' CASCADE.
FOREIGN_KEY_COLUMNS: Final = (
    "SELECT CAST(c.conrelid AS regclass) || '.' || a.attname, c.confdeltype FROM pg_constraint c "
    "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey) WHERE c.contype = 'f'"
)


async def refused(c: AsyncConnection, statement: str, params: dict[str, object] | None = None) -> PgErrorInfo:
    """Run `statement` in a savepoint that the database must refuse; return the SQLSTATE and constraint."""
    with pytest.raises(sa.exc.IntegrityError) as bad:
        async with c.begin_nested():
            await c.execute(sa.text(statement), params or {})
    return pg_error(bad.value)


async def refused_check(
    c: AsyncConnection, table: str, statement: str, params: dict[str, object], *fragments: str
) -> None:
    """`statement` violates a CHECK of `table` whose definition holds every one of `fragments`."""
    info = await refused(c, statement, params)
    assert info.sqlstate == CHECK_VIOLATION
    definition = (
        await c.execute(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = CAST(:t AS regclass) AND conname = :n"
            ),
            {"t": table, "n": info.constraint},
        )
    ).scalar_one()
    for fragment in fragments:
        assert fragment in definition, f"{info.constraint}: {definition}"
