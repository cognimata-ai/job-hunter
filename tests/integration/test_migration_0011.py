"""Migration 0011 (M5 Task 27, Ruling T27-2): the partial index of the purge operations by their payload's
manifest id, an expand migration (floor 0010, not destructive), the one the upgrade-then-rollback drill
crossed until 0012 (`tests/stack/test_prod_drills.py`).
"""

import asyncio
import re
from collections.abc import AsyncIterator, Iterator
from typing import Final

import pytest
import sqlalchemy as sa
from alembic import command
from jobhunter.db.alembic_runner import alembic_config
from jobhunter.db.migrate import run_migrate_async
from jobhunter.privacy.kinds.delete_everything import _OWN_OPERATIONS
from jobhunter.privacy.reconcilers import _EXECUTING
from jobhunter.worker.operations import PURGE_KIND
from sqlalchemy import TextClause
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from tests.support.pg import PgServer, pg_one, pg_rows, remove_pg_server, start_pg_server

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
# The fill of `filled`: `OPERATIONS` operations of another kind and `PURGES` purge operations, each of a
# manifest of its own, every one finished (no transition: rows are inserted in a state their CHECKs allow).
OPERATIONS: Final = 5000
PURGES: Final = 50
# The readers the index serves, each with the number of its lookups of the purge operations by manifest: the
# purge-fences reconciler's two subqueries, and delete everything's lookup of its own manifest's operations.
READERS: Final = {
    "purge_fences_reconciler": (_EXECUTING, 2),
    "delete_everything_own_operations": (_OWN_OPERATIONS, 1),
}
# A reader's parameters (`:name`, never a `::` cast), and a value for each a reader may take.
_PARAMETER: Final = re.compile(r"(?<![:\w]):(\w+)")
VALUES: Final = {"kind": f"'{PURGE_KIND}'", "m": "1", "bound": str(OPERATIONS + PURGES)}
_FILL: Final = (
    "INSERT INTO operations (kind, payload, dedup_key, status, finished_at) "
    "SELECT 'eval_run', jsonb_build_object('eval_run_id', n), 'eval_run:' || n, 'succeeded', now() "
    "FROM generate_series(1, %s) n "
    "UNION ALL SELECT 'purge', jsonb_build_object('manifest_id', n), 'purge:' || n, 'succeeded', now() "
    "FROM generate_series(1, %s) n"
)


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


@pytest.fixture(scope="module")
def filled_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[PgServer]:
    """A server of this module's own at 0011, its `operations` filled at a production-like size and ANALYZEd
    (final fix B12): the planner's choice is then the data's, not that of whatever statistics earlier files
    left on the shared server (three operations of another kind make it take `operations_dedup_newest`)."""
    server = start_pg_server(tmp_path_factory.mktemp("filled-pg"))
    try:
        command.upgrade(alembic_config(server.dsn("migrator", driver="sqlalchemy")), M5_INDEX)
        pg_rows(server, "migrator", _FILL, OPERATIONS, PURGES)
        pg_rows(server, "migrator", "ANALYZE operations")
        yield server
    finally:
        remove_pg_server(server)


@pytest.fixture
async def filled(filled_server: PgServer) -> AsyncIterator[AsyncEngine]:
    """An engine on `filled_server` as the migrator, which owns the table."""
    filled_engine = create_async_engine(
        filled_server.dsn("migrator", driver="sqlalchemy"), poolclass=NullPool
    )
    try:
        yield filled_engine
    finally:
        await filled_engine.dispose()


def prepared(reader: TextClause) -> tuple[str, str]:
    """`reader` as `PREPARE reader AS …`, its parameters numbered `$1`, `$2`… in their order, and its `EXPLAIN
    EXECUTE` with a value (`VALUES`) for each."""
    names: list[str] = []

    def number(match: re.Match[str]) -> str:
        if match[1] not in names:
            names.append(match[1])
        return f"${names.index(match[1]) + 1}"

    body = _PARAMETER.sub(number, reader.text)
    values = f"({', '.join(VALUES[name] for name in names)})" if names else ""
    return f"PREPARE reader AS {body}", f"EXPLAIN EXECUTE reader{values}"


@pytest.mark.parametrize("mode", ["force_custom_plan", "force_generic_plan"])
@pytest.mark.parametrize(("reader", "uses"), list(READERS.values()), ids=list(READERS))
async def test_the_index_serves_the_purge_readers(
    filled: AsyncEngine, reader: TextClause, uses: int, mode: str
) -> None:
    """Each fixed-purpose purge reader uses the index on an ANALYZEd table of a production-like size, in each
    of its lookups, with a custom plan, which sees its parameters' values, and with a generic plan, which sees
    none (PostgreSQL may choose one for a statement psycopg prepares after its threshold): it writes `kind =
    'purge'` as a literal (final review, Codex's parked item 4), which a bound kind would hide from a generic
    plan. With sequential scans off, its plan names the index."""
    prepare, explain = prepared(reader)
    async with filled.connect() as conn:
        await conn.execute(sa.text("SET LOCAL enable_seqscan = off"))
        await conn.execute(sa.text(f"SET LOCAL plan_cache_mode = {mode}"))
        await conn.exec_driver_sql(prepare)
        lines = list((await conn.exec_driver_sql(explain)).scalars())
        await conn.rollback()
    assert sum(INDEX in line for line in lines) == uses, "\n".join(lines)
