"""Rubric v1 and v2 and prompt-schema version 1 are seeded, not migrated (M2 Task 6, M3 Task 3; plan
Deviation 32).

No migration inserts them (§19.3 step 2). The seed step of `migrate` (§19.3 step 6) inserts each row when
absent, a rubric found by its version, and points a fresh `active_config` row at the shipped rubric v2 (R13)
and the prompt-schema version; a pointer already set is never changed, so an install M2 left on rubric v1
keeps it (a shipped version activates through Settings only, §13.3). Every path ends with exactly one row of
each rubric version and one prompt-schema row, stored as the pure functions give them. The two columns were
nullable in M2; M3's first migration (0008) restored NOT NULL, so an M1 install passes through M2's `migrate`
before M3's (plan Deviation 5).
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from jobhunter.db.alembic_runner import alembic_config, code_head
from jobhunter.db.migrate import run_migrate_async
from jobhunter.llm.prompts.render import prompt_schema_hash, prompt_schema_v1
from jobhunter.scoring.rubric import load_rubric

from tests.support.m3 import insert_m1_configuration, insert_m2_seed_rows
from tests.support.pg import PgServer, pg_one, pg_rows

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
M2_SCHEMA = "0006_m2_llm_profile_settings"
M2_HEAD = "0007_m2_draft_text_edits"  # the draft's stored text edit set (Task 14, R14-1)

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
COUNTS = "SELECT (SELECT count(*) FROM rubric_versions), (SELECT count(*) FROM prompt_schema_versions)"
RUBRICS = "SELECT version, content_hash, weights, anchors FROM rubric_versions ORDER BY version"
# Everything a second migrate could change or duplicate.
STATE = (
    "SELECT (SELECT count(*) FROM rubric_versions), (SELECT count(*) FROM prompt_schema_versions), "
    "a.rubric_version_id, a.prompt_schema_version_id, a.updated_at, "
    "(SELECT array_agg(created_at ORDER BY version) FROM rubric_versions), "
    "(SELECT created_at FROM prompt_schema_versions) "
    "FROM active_config a"
)
# The `active_config` columns other than the two pointers the seed fills.
OTHER_POINTERS = (
    "SELECT settings_version_id, scope_version_id, pending_scope_version_id, profile_version_id, "
    "model_policy_id, execution_config_id, evaluation_policy_id, llm_tuple_id FROM active_config"
)


def rubric_row(version: str) -> tuple[object, ...]:
    rubric = load_rubric(version)
    return (version, rubric.content_hash, dict(rubric.weights), dict(rubric.anchors))


def expected_rows(rubric_version: str) -> tuple[object, ...]:
    templates, schemas = prompt_schema_v1()
    return (*rubric_row(rubric_version), prompt_schema_hash(templates, schemas), templates, schemas)


def one(server: PgServer, statement: str) -> tuple[object, ...] | None:
    return pg_one(server, "migrator", statement)


def assert_points_at(server: PgServer, rubric_version: str) -> None:
    """`active_config` names rubric `rubric_version` and prompt-schema version 1; rubric v1 and v2 are both
    stored as `load_rubric` gives them, once each, next to the one prompt-schema row; and the two columns are
    NOT NULL (M3, migration 0008)."""
    assert one(server, POINTED) == expected_rows(rubric_version)
    assert pg_rows(server, "migrator", RUBRICS) == [rubric_row("v1"), rubric_row("v2")]
    assert one(server, COUNTS) == (2, 1)
    assert pg_rows(server, "migrator", NULLABLE) == [
        ("prompt_schema_version_id", "NO"),
        ("rubric_version_id", "NO"),
    ]


async def test_after_migrate_active_config_points_at_rubric_v2(fresh_pg_server, make_config) -> None:
    await run_migrate_async(make_config("migrate", fresh_pg_server), now=NOW)
    assert one(fresh_pg_server, "SELECT version_num FROM alembic_version") == (code_head(),)
    assert_points_at(fresh_pg_server, "v2")


async def test_a_second_migrate_is_a_noop(fresh_pg_server, make_config) -> None:
    cfg = make_config("migrate", fresh_pg_server)
    await run_migrate_async(cfg, now=NOW)
    before = one(fresh_pg_server, STATE)
    await run_migrate_async(cfg, now=NOW + timedelta(days=1))
    assert one(fresh_pg_server, STATE) == before
    assert_points_at(fresh_pg_server, "v2")


async def test_an_upgrade_from_0006_keeps_the_rubric_v1_pointer_m2_set(fresh_pg_server, make_config) -> None:
    """An install M1 left (NULL pointers, no rubric or prompt-schema row) passes through M2's `migrate`, whose
    seed set both pointers to rubric v1 and prompt-schema version 1, before M3's: 0008 then restores NOT NULL
    over them (plan Deviation 5), and M3's seed inserts rubric v2 next to v1 but changes neither pointer nor
    the other pointers. Both older states are put in place by direct SQL, so no seed of this code runs
    against an older schema (ruling T1-b)."""
    dsn = fresh_pg_server.dsn("migrator", driver="sqlalchemy")
    await asyncio.to_thread(command.upgrade, alembic_config(dsn), M2_SCHEMA)
    insert_m1_configuration(fresh_pg_server)  # a database as M1 left it
    assert one(fresh_pg_server, "SELECT rubric_version_id, prompt_schema_version_id FROM active_config") == (
        None,
        None,
    )
    assert one(fresh_pg_server, COUNTS) == (0, 0)
    others = one(fresh_pg_server, OTHER_POINTERS)

    # M2's `migrate`: its migrations, then its seed's rubric and prompt-schema rows and the two pointers
    await asyncio.to_thread(command.upgrade, alembic_config(dsn), M2_HEAD)
    seeded = insert_m2_seed_rows(fresh_pg_server, point=True)

    await run_migrate_async(make_config("migrate", fresh_pg_server), now=NOW)
    assert one(fresh_pg_server, "SELECT version_num FROM alembic_version") == (code_head(),)
    assert_points_at(fresh_pg_server, "v1")
    assert (
        one(fresh_pg_server, "SELECT rubric_version_id, prompt_schema_version_id FROM active_config")
        == seeded
    )
    assert one(fresh_pg_server, OTHER_POINTERS) == others  # the seed moved no pointer


async def test_every_migrate_leaves_both_pointers_set(migrated, make_config) -> None:
    """Whatever a migrate starts from, it ends with both pointers naming the seeded rows. Since 0008 the
    columns refuse NULL, so the damage left to repair is a missing `active_config` row, which the seed inserts
    with both pointers set, the rubric one to the shipped v2. (The autouse reset restores the shared database
    after.)"""
    one(migrated, "DELETE FROM active_config")
    await run_migrate_async(make_config("migrate"), now=NOW)
    assert one(
        migrated, "SELECT rubric_version_id IS NULL, prompt_schema_version_id IS NULL FROM active_config"
    ) == (
        False,
        False,
    )
    assert_points_at(migrated, "v2")
