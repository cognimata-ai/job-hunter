"""Rubric v1 and v2 and prompt-schema versions 1 and 2 are seeded, not migrated (M2 Task 6, M3 Tasks 3 and 4;
plan Deviations 32 and 40).

No migration inserts them (§19.3 step 2). The seed step of `migrate` (§19.3 step 6) inserts each row when
absent, a rubric found by its version and a prompt-schema version by its hash with its parser family, and
points a fresh `active_config` row at the shipped rubric v2 (R13) and prompt-schema version 2; a pointer
already set is never changed, so an install M2 left on rubric v1 and prompt-schema version 1 keeps both (a
shipped version activates through Settings only, §13.3). Every path ends with exactly one row of each rubric
version and of each prompt-schema version, stored as the pure functions give them. The two columns were
nullable in M2; M3's first migration (0008) restored NOT NULL, so an M1 install passes through M2's `migrate`
before M3's (plan Deviation 5).
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from jobhunter.db.alembic_runner import alembic_config, code_head
from jobhunter.db.migrate import run_migrate_async
from jobhunter.llm.prompts.render import prompt_schema_hash, prompt_schema_v1, prompt_schema_v2
from jobhunter.scoring.rubric import load_rubric

from tests.support.fixtures import _SEEDED_PROMPT_SCHEMA_HASH_V1
from tests.support.m3 import insert_m1_configuration, insert_m2_seed_rows
from tests.support.pg import PgServer, pg_one, pg_rows

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
M2_SCHEMA = "0006_m2_llm_profile_settings"
M2_HEAD = "0007_m2_draft_text_edits"  # the draft's stored text edit set (Task 14, R14-1)

# What `active_config` points at, as stored.
POINTED = (
    "SELECT r.version, r.content_hash, r.weights, r.anchors, p.prompt_schema_hash, p.templates, p.schemas, "
    "p.parser_family FROM active_config a JOIN rubric_versions r ON r.id = a.rubric_version_id "
    "JOIN prompt_schema_versions p ON p.id = a.prompt_schema_version_id WHERE a.id = 1"
)
NULLABLE = (
    "SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name = 'active_config' "
    "AND column_name IN ('rubric_version_id', 'prompt_schema_version_id') ORDER BY 1"
)
COUNTS = "SELECT (SELECT count(*) FROM rubric_versions), (SELECT count(*) FROM prompt_schema_versions)"
RUBRICS = "SELECT version, content_hash, weights, anchors FROM rubric_versions ORDER BY version"
PROMPT_SCHEMAS = (
    "SELECT prompt_schema_hash, templates, schemas, parser_family FROM prompt_schema_versions ORDER BY id"
)
# Everything a second migrate could change or duplicate.
STATE = (
    "SELECT (SELECT count(*) FROM rubric_versions), (SELECT count(*) FROM prompt_schema_versions), "
    "a.rubric_version_id, a.prompt_schema_version_id, a.updated_at, "
    "(SELECT array_agg(created_at ORDER BY version) FROM rubric_versions), "
    "(SELECT array_agg(created_at ORDER BY id) FROM prompt_schema_versions) "
    "FROM active_config a"
)
# Each shipped prompt-schema version and the parser family the seed names it with (both literals here, so a
# changed seed fails the test).
FAMILIES = {1: "generator_v1", 2: "generator_v2_items"}
# The `active_config` columns other than the two pointers the seed fills.
OTHER_POINTERS = (
    "SELECT settings_version_id, scope_version_id, pending_scope_version_id, profile_version_id, "
    "model_policy_id, execution_config_id, evaluation_policy_id, llm_tuple_id FROM active_config"
)


def rubric_row(version: str) -> tuple[object, ...]:
    rubric = load_rubric(version)
    return (version, rubric.content_hash, dict(rubric.weights), dict(rubric.anchors))


def prompt_schema_row(version: int) -> tuple[object, ...]:
    templates, schemas = prompt_schema_v1() if version == 1 else prompt_schema_v2()
    return (prompt_schema_hash(templates, schemas), templates, schemas, FAMILIES[version])


def expected_rows(rubric_version: str, prompt_schema_version: int) -> tuple[object, ...]:
    return (*rubric_row(rubric_version), *prompt_schema_row(prompt_schema_version))


def one(server: PgServer, statement: str) -> tuple[object, ...] | None:
    return pg_one(server, "migrator", statement)


def assert_points_at(server: PgServer, rubric_version: str, prompt_schema_version: int) -> None:
    """`active_config` names rubric `rubric_version` and prompt-schema version `prompt_schema_version`;
    rubric v1 and v2 are both stored as `load_rubric` gives them, once each, and prompt-schema versions 1 and
    2 as `prompt_schema_v1()` and `prompt_schema_v2()` give them with their families, once each (version 1's
    hash is the one the M2 seed wrote); and the two columns are NOT NULL (M3, migration 0008)."""
    assert one(server, POINTED) == expected_rows(rubric_version, prompt_schema_version)
    assert pg_rows(server, "migrator", RUBRICS) == [rubric_row("v1"), rubric_row("v2")]
    assert pg_rows(server, "migrator", PROMPT_SCHEMAS) == [prompt_schema_row(1), prompt_schema_row(2)]
    assert prompt_schema_row(1)[0] == _SEEDED_PROMPT_SCHEMA_HASH_V1
    assert one(server, COUNTS) == (2, 2)
    assert pg_rows(server, "migrator", NULLABLE) == [
        ("prompt_schema_version_id", "NO"),
        ("rubric_version_id", "NO"),
    ]


async def test_after_migrate_active_config_points_at_rubric_v2_and_prompt_schema_v2(
    fresh_pg_server, make_config
) -> None:
    await run_migrate_async(make_config("migrate", fresh_pg_server), now=NOW)
    assert one(fresh_pg_server, "SELECT version_num FROM alembic_version") == (code_head(),)
    assert_points_at(fresh_pg_server, "v2", 2)


async def test_a_second_migrate_is_a_noop(fresh_pg_server, make_config) -> None:
    cfg = make_config("migrate", fresh_pg_server)
    await run_migrate_async(cfg, now=NOW)
    before = one(fresh_pg_server, STATE)
    await run_migrate_async(cfg, now=NOW + timedelta(days=1))
    assert one(fresh_pg_server, STATE) == before
    assert_points_at(fresh_pg_server, "v2", 2)


async def test_an_upgrade_from_0006_keeps_the_v1_pointers_m2_set(fresh_pg_server, make_config) -> None:
    """An install M1 left (NULL pointers, no rubric or prompt-schema row) passes through M2's `migrate`, whose
    seed set both pointers to rubric v1 and prompt-schema version 1, before M3's: 0008 then restores NOT NULL
    over them (plan Deviation 5) and gives the version 1 row its default family `generator_v1`, and M3's seed
    inserts rubric v2 and prompt-schema version 2 next to them but changes neither pointer nor the other
    pointers. Both older states are put in place by direct SQL, so no seed of this code runs against an older
    schema (ruling T1-b)."""
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
    assert_points_at(fresh_pg_server, "v1", 1)
    assert (
        one(fresh_pg_server, "SELECT rubric_version_id, prompt_schema_version_id FROM active_config")
        == seeded
    )
    assert one(fresh_pg_server, OTHER_POINTERS) == others  # the seed moved no pointer


async def test_every_migrate_leaves_both_pointers_set(migrated, make_config, tmp_path) -> None:
    """Whatever a migrate starts from, it ends with both pointers naming the seeded rows. Since 0008 the
    columns refuse NULL, so the damage left to repair is a missing `active_config` row, which the seed inserts
    with both pointers set, to the shipped rubric v2 and prompt-schema version 2. (The autouse reset restores
    the shared database after.)"""
    one(migrated, "DELETE FROM active_config")
    # A manifest stream of its own: the shared server's holds whatever other tests left there.
    await run_migrate_async(make_config("migrate"), now=NOW, manifest_dir=tmp_path)
    assert one(
        migrated, "SELECT rubric_version_id IS NULL, prompt_schema_version_id IS NULL FROM active_config"
    ) == (
        False,
        False,
    )
    assert_points_at(migrated, "v2", 2)
