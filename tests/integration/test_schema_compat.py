"""An older release on a newer schema (plan M5 Task 27, M5-R4 R2, Deviation 32): `schema_state`, `migrate` and
`/readyz` accept a newer head whose compatibility floor the code knows and refuse a destructive head, a floor
the code does not know and a head without a `schema_compat` row.

The code under test is the older release; the newer one is this package's chain with the test-only revision
`tests/support/fake_revision` appended (`FAKE_HEAD`, an expand migration whose floor is this package's head).
Each test has a database of its own.
"""

import asyncio
import json
import subprocess
import sys
from typing import Final

import pytest
from jobhunter.clock import utcnow
from jobhunter.db.alembic_runner import code_head
from jobhunter.db.compat import schema_state
from jobhunter.db.migrate import MIGRATE_ORDER, SchemaAheadIncompatible, run_migrate_async
from jobhunter.db.session import make_engine, make_null_engine
from jobhunter.observability.health import ReadinessReport, check_readiness
from structlog.testing import capture_logs

from tests.support.fake_revision import FAKE_HEAD, FAKE_TABLE, migrate_newer_release
from tests.support.fixtures import fresh_heartbeat
from tests.support.pg import PgServer, pg_one, pg_rows

pytestmark = pytest.mark.integration

AFTER_ALEMBIC: Final = [step for step in MIGRATE_ORDER if step != "alembic"]
_TABLES: Final = (
    "SELECT schemaname, tablename FROM pg_tables "
    "WHERE schemaname IN ('public', 'procrastinate') ORDER BY 1, 2"
)
_SEQUENCES: Final = (
    "SELECT schemaname, sequencename, last_value FROM pg_sequences "
    "WHERE schemaname IN ('public', 'procrastinate') ORDER BY 1, 2"
)


def row_versions(server: PgServer) -> dict[str, list[object]]:
    """Every row of every table by its version and place (`xmin`, `ctid`), and every sequence's last value:
    any write of a row (an UPDATE, or a DELETE and INSERT of the same values) or of a sequence changes it."""
    found: dict[str, list[object]] = {}
    for schema, table in pg_rows(server, "superuser", _TABLES):
        found[f"{schema}.{table}"] = [
            row[0]
            for row in pg_rows(
                server,
                "superuser",
                f'SELECT CAST(xmin AS text) || CAST(ctid AS text) FROM "{schema}"."{table}" ORDER BY ctid',  # noqa: S608
            )
        ]
    found["sequences"] = list(pg_rows(server, "superuser", _SEQUENCES))
    return found


async def state_of(server: PgServer) -> str:
    """`schema_state` of `server`'s database to this code."""
    engine = make_null_engine(server.dsn("migrator", driver="sqlalchemy"))
    try:
        async with engine.connect() as conn:
            return await schema_state(conn, code_head=code_head())
    finally:
        await engine.dispose()


def mark(server: PgServer, statement: str) -> None:
    """A change to the newer release's `schema_compat` row (`FAKE_HEAD`), as the migrator."""
    pg_one(server, "migrator", statement, FAKE_HEAD)


async def test_older_code_accepts_a_newer_compatible_head(fresh_pg_server, make_config) -> None:
    """Empty, the database is `behind`; migrated by this code, `current`; at the newer release's head, whose
    floor is this code's head, `ahead_compatible`: `migrate` skips the alembic step, says so with both heads,
    runs every other step and leaves the newer schema as it is."""
    server = fresh_pg_server
    assert await state_of(server) == "behind"
    await run_migrate_async(make_config("migrate", server))
    assert await state_of(server) == "current"
    await migrate_newer_release(server)
    assert await state_of(server) == "ahead_compatible"

    with capture_logs() as logs:
        ran = await run_migrate_async(make_config("migrate", server))
    assert ran == AFTER_ALEMBIC
    assert [e["step"] for e in logs if e["event"] == "migrate_step"] == AFTER_ALEMBIC
    (ahead,) = [e for e in logs if e["event"] == "migrate_schema_ahead_compatible"]
    assert (ahead["database_heads"], ahead["code_head"]) == ([FAKE_HEAD], code_head())
    assert not [e for e in logs if e["event"] in ("migrate_failed", "migrate_compat_recorded")]
    assert pg_one(server, "migrator", "SELECT version_num FROM alembic_version") == (FAKE_HEAD,)
    assert pg_one(server, "migrator", f"SELECT to_regclass('{FAKE_TABLE}') IS NOT NULL") == (True,)
    assert await state_of(server) == "ahead_compatible"


async def test_older_code_refuses_a_destructive_head(fresh_pg_server, make_config, proc_env) -> None:
    """The newer head's record flagged destructive, its floor still one this code knows: `ahead_incompatible`.
    `migrate` refuses before any step (`migrate_schema_ahead_incompatible` with both heads) and writes
    nothing; the CLI exits 1 without a traceback."""
    server = fresh_pg_server
    await migrate_newer_release(server)
    mark(server, "UPDATE schema_compat SET destructive = true WHERE revision = %s")
    assert await state_of(server) == "ahead_incompatible"
    before = row_versions(server)

    with capture_logs() as logs, pytest.raises(SchemaAheadIncompatible):
        await run_migrate_async(make_config("migrate", server))
    (refused,) = [e for e in logs if e["event"] == "migrate_schema_ahead_incompatible"]
    assert (refused["database_heads"], refused["code_head"]) == ([FAKE_HEAD], code_head())
    assert [e for e in logs if e["event"] == "migrate_step"] == []
    assert row_versions(server) == before

    done = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "jobhunter.cli", "migrate"],
        env=proc_env("migrate", server),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert done.returncode == 1, done.stdout + done.stderr
    events = [json.loads(line) for line in done.stdout.splitlines() if line.startswith("{")]
    assert [e["event"] for e in events if e["event"].startswith("migrate_schema_")] == [
        "migrate_schema_ahead_incompatible"
    ]
    assert [e for e in events if e["event"] == "migrate_step"] == []
    assert "Traceback" not in done.stderr and server.passwords["migrator"] not in done.stdout + done.stderr
    assert row_versions(server) == before


async def test_readiness_accepts_a_compatible_head_and_refuses_an_incompatible_one(
    fresh_pg_server, make_config, log_events
) -> None:
    """`/readyz`'s `migrations` passes on the newer compatible head and fails, with the state as its reason
    and no SQLSTATE, once that head's record is destructive."""
    server = fresh_pg_server
    await migrate_newer_release(server)
    engine = make_engine(make_config("api", server))
    try:
        await fresh_heartbeat(engine)
        ready = await check_readiness(engine, code_head=code_head(), now=utcnow(), embedder=None)
        assert ready == ReadinessReport(ok=True, failing=[])
        mark(server, "UPDATE schema_compat SET destructive = true WHERE revision = %s")
        refused = await check_readiness(engine, code_head=code_head(), now=utcnow(), embedder=None)
        assert refused == ReadinessReport(ok=False, failing=["migrations"])
    finally:
        await engine.dispose()
    failed = [e for e in log_events() if e["event"] == "readiness_probe_failed"]
    assert [(e["probe"], e["reason"], e["sqlstate"]) for e in failed] == [
        ("migrations", "ahead_incompatible", None)
    ]


async def test_a_head_without_a_compat_row_is_refused(fresh_pg_server, make_config) -> None:
    """A newer head whose `schema_compat` row names a floor this code does not know, or that has no row at
    all, is `ahead_incompatible`: `migrate` refuses it before any step."""
    server = fresh_pg_server
    await migrate_newer_release(server)
    mark(server, "UPDATE schema_compat SET compat_floor = '9998_unknown_to_this_code' WHERE revision = %s")
    assert await state_of(server) == "ahead_incompatible"
    mark(server, "DELETE FROM schema_compat WHERE revision = %s")
    assert await state_of(server) == "ahead_incompatible"
    with capture_logs() as logs, pytest.raises(SchemaAheadIncompatible):
        await run_migrate_async(make_config("migrate", server))
    assert [e["event"] for e in logs if e["event"].startswith("migrate_schema_")] == [
        "migrate_schema_ahead_incompatible"
    ]
    assert [e for e in logs if e["event"] == "migrate_step"] == []
    assert pg_one(server, "migrator", "SELECT version_num FROM alembic_version") == (FAKE_HEAD,)


async def test_the_seed_of_an_older_release_changes_nothing(fresh_pg_server, make_config) -> None:
    """On the newer schema, whose transition matrix holds an edge of the newer release (its new table's), the
    older release's `migrate` (every step but alembic, the seed among them) writes no row and moves no
    sequence: the matrix stays the newer release's."""
    server = fresh_pg_server
    await migrate_newer_release(server)
    pg_one(
        server,
        "migrator",
        "INSERT INTO transition_matrix (table_name, from_state, to_state) VALUES (%s, 'new', 'done')",
        FAKE_TABLE,
    )
    before = row_versions(server)
    assert await run_migrate_async(make_config("migrate", server)) == AFTER_ALEMBIC
    assert row_versions(server) == before
    assert pg_one(
        server, "migrator", "SELECT count(*) FROM transition_matrix WHERE table_name = %s", FAKE_TABLE
    ) == (1,)
