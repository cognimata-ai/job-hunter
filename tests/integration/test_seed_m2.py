"""Rubric v1, prompt-schema version 1 and the restored NOT NULL pointers (M2 Task 6; M0 Deviation 1).

Migration 0007 inserts both rows and points an existing `active_config` row at them, then restores NOT NULL;
on a fresh install `active_config` does not exist yet when 0007 runs, and `seed_defaults` inserts it with
both pointers. Every path ends with exactly one row each, stored as the pure functions give them.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from alembic import command
from jobhunter.db.alembic_runner import alembic_config, upgrade_head
from jobhunter.db.migrate import run_migrate_async
from jobhunter.db.seed import seed_defaults
from jobhunter.db.session import make_null_engine
from jobhunter.llm.prompts.render import prompt_schema_hash, prompt_schema_v1
from jobhunter.scoring.rubric import load_rubric

from tests.support.pg import PgServer, pg_one, pg_rows

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
M2_SCHEMA = "0006_m2_llm_profile_settings"
NOT_NULL_VIOLATION = "23502"

# What `active_config` points at, as stored.
POINTED = (
    "SELECT r.version, r.content_hash, r.weights, r.anchors, p.prompt_schema_hash, p.templates, p.schemas "
    "FROM active_config a JOIN rubric_versions r ON r.id = a.rubric_version_id "
    "JOIN prompt_schema_versions p ON p.id = a.prompt_schema_version_id WHERE a.id = 1"
)
NULLABLE = (
    "SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name = 'active_config' "
    "AND column_name IN ('rubric_version_id', 'prompt_schema_version_id') ORDER BY 1"
)
# Everything a second migrate could change or duplicate.
STATE = (
    "SELECT (SELECT count(*) FROM rubric_versions), (SELECT count(*) FROM prompt_schema_versions), "
    "a.rubric_version_id, a.prompt_schema_version_id, a.updated_at, "
    "(SELECT created_at FROM rubric_versions), (SELECT created_at FROM prompt_schema_versions) "
    "FROM active_config a"
)


def expected_rows() -> tuple[object, ...]:
    rubric = load_rubric("v1")
    templates, schemas = prompt_schema_v1()
    return (
        "v1",
        rubric.content_hash,
        dict(rubric.weights),
        dict(rubric.anchors),
        prompt_schema_hash(templates, schemas),
        templates,
        schemas,
    )


def one(server: PgServer, statement: str) -> tuple[object, ...] | None:
    return pg_one(server, "migrator", statement)


def assert_points_at_v1_with_not_null(server: PgServer) -> None:
    assert one(server, POINTED) == expected_rows()
    assert one(
        server, "SELECT (SELECT count(*) FROM rubric_versions), (SELECT count(*) FROM prompt_schema_versions)"
    ) == (1, 1)
    assert pg_rows(server, "migrator", NULLABLE) == [
        ("prompt_schema_version_id", "NO"),
        ("rubric_version_id", "NO"),
    ]
    for statement in (
        "UPDATE active_config SET rubric_version_id = NULL",
        "UPDATE active_config SET prompt_schema_version_id = NULL",
    ):
        with pytest.raises(psycopg.Error) as refused:
            one(server, statement)
        assert refused.value.sqlstate == NOT_NULL_VIOLATION, statement


async def test_after_migrate_active_config_points_at_the_v1_rows_and_both_are_not_null(
    fresh_pg_server, make_config
) -> None:
    await run_migrate_async(make_config("migrate", fresh_pg_server), now=NOW)
    assert_points_at_v1_with_not_null(fresh_pg_server)


async def test_a_second_migrate_is_a_noop(fresh_pg_server, make_config) -> None:
    cfg = make_config("migrate", fresh_pg_server)
    await run_migrate_async(cfg, now=NOW)
    before = one(fresh_pg_server, STATE)
    await run_migrate_async(cfg, now=NOW + timedelta(days=1))
    assert one(fresh_pg_server, STATE) == before
    assert_points_at_v1_with_not_null(fresh_pg_server)


async def test_an_upgrade_from_0006_with_an_active_config_row_sets_the_pointers(fresh_pg_server) -> None:
    dsn = fresh_pg_server.dsn("migrator", driver="sqlalchemy")
    await asyncio.to_thread(command.upgrade, alembic_config(dsn), M2_SCHEMA)
    # A database as M1 left it: settings version 1 and an `active_config` row with NULL rubric and prompt
    # pointers, and both tables empty (the seed of this code writes them, so they are taken back out).
    engine = make_null_engine(dsn)
    try:
        async with engine.begin() as conn:
            await seed_defaults(conn, now=NOW)
    finally:
        await engine.dispose()
    one(fresh_pg_server, "UPDATE active_config SET rubric_version_id = NULL, prompt_schema_version_id = NULL")
    one(fresh_pg_server, "DELETE FROM rubric_versions")
    one(fresh_pg_server, "DELETE FROM prompt_schema_versions")
    assert one(fresh_pg_server, "SELECT version_num FROM alembic_version") == (M2_SCHEMA,)
    assert pg_rows(fresh_pg_server, "migrator", NULLABLE) == [
        ("prompt_schema_version_id", "YES"),
        ("rubric_version_id", "YES"),
    ]
    settings_before = one(
        fresh_pg_server,
        "SELECT settings_version_id, scope_version_id, model_policy_id, execution_config_id, "
        "evaluation_policy_id FROM active_config",
    )

    await asyncio.to_thread(upgrade_head, dsn)  # 0007
    assert_points_at_v1_with_not_null(fresh_pg_server)
    # Only the two pointers moved.
    assert (
        one(
            fresh_pg_server,
            "SELECT settings_version_id, scope_version_id, model_policy_id, execution_config_id, "
            "evaluation_policy_id FROM active_config",
        )
        == settings_before
    )
