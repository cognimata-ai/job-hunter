"""Migration 0011 (M5 Task 27, Ruling T27-2): the partial index of the purge operations by their payload's
manifest id, an expand migration (floor 0010, not destructive), the one the upgrade-then-rollback drill
crosses (`tests/stack/test_prod_drills.py`).
"""

import asyncio
from typing import Final

import pytest
import sqlalchemy as sa
from alembic import command
from jobhunter.db.alembic_runner import alembic_config
from jobhunter.db.migrate import run_migrate_async
from jobhunter.privacy.reconcilers import _EXECUTING
from jobhunter.worker.operations import PURGE_KIND
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.pg import PgServer, pg_one, pg_rows

pytestmark = pytest.mark.integration
M5_SCHEMA: Final = "0010_m5_operations"
M5_INDEX: Final = "0011_m5_purge_operation_index"
INDEX: Final = "operations_purge_manifest"
INDEX_DEF: Final = (
    "CREATE INDEX operations_purge_manifest ON public.operations USING btree "
    "(((payload -> 'manifest_id'::text))) WHERE (kind = 'purge'::text)"
)
_INDEXES: Final = "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' ORDER BY 1"
_VERSION: Final = "SELECT version_num FROM alembic_version"


async def upgrade(server: PgServer, revision: str) -> None:
    await asyncio.to_thread(
        command.upgrade, alembic_config(server.dsn("migrator", driver="sqlalchemy")), revision
    )


async def downgrade(server: PgServer, revision: str) -> None:
    await asyncio.to_thread(
        command.downgrade, alembic_config(server.dsn("migrator", driver="sqlalchemy")), revision
    )


async def test_the_index_exists_and_downgrades(fresh_pg_server: PgServer, make_config) -> None:
    """0011 adds exactly the one index; its downgrade leaves the 0010 indexes, and upgrading again gives the
    0011 ones. `migrate` records its floor, 0010, not destructive."""
    server = fresh_pg_server
    await upgrade(server, M5_SCHEMA)
    at_0010 = pg_rows(server, "migrator", _INDEXES)
    await upgrade(server, M5_INDEX)
    assert pg_one(server, "migrator", _VERSION) == (M5_INDEX,)
    at_0011 = pg_rows(server, "migrator", _INDEXES)
    assert set(at_0011) - set(at_0010) == {(INDEX, INDEX_DEF)} and set(at_0010) < set(at_0011)

    await downgrade(server, M5_SCHEMA)
    assert pg_one(server, "migrator", _VERSION) == (M5_SCHEMA,)
    assert pg_rows(server, "migrator", _INDEXES) == at_0010
    await upgrade(server, M5_INDEX)
    assert pg_rows(server, "migrator", _INDEXES) == at_0011

    await run_migrate_async(make_config("migrate", server))
    record = "SELECT compat_floor, destructive FROM schema_compat WHERE revision = %s"
    assert pg_one(server, "migrator", record, M5_INDEX) == (M5_SCHEMA, False)


async def test_the_index_serves_the_purge_readers(engine: AsyncEngine) -> None:
    """The purge-fences reconciler's own lookup (`_EXECUTING`, its kind bound to `purge`) can use the index:
    with sequential scans off, its plan names it."""
    async with engine.connect() as conn:
        await conn.execute(sa.text("SET LOCAL enable_seqscan = off"))
        plan = (await conn.execute(sa.text(f"EXPLAIN {_EXECUTING.text}"), {"kind": PURGE_KIND})).scalars()
        lines = list(plan)
        await conn.rollback()
    assert any(INDEX in line for line in lines), "\n".join(lines)
