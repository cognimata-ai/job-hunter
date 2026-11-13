"""Migration 0012 (the M5 final review's fix E2, Ruling F-6): `unship_manifest(manifest_id)`, the backup
role's one way to return a shipped deletion manifest to `completed`, an expand migration (floor 0011, not
destructive), the one the upgrade-then-rollback drill crosses (`tests/stack/test_prod_drills.py`).

The backup role holds no UPDATE on `deletion_manifests` (the final re-review's R3-I1: with one, a compromised
backup container could move an `executing` manifest to `completed` and so end an unfinished purge as done).
The function, `SECURITY DEFINER` as its owner, the migrator, with its `search_path` pinned, moves only a
`shipped` manifest, clearing its shipment and keeping its completion time; on a manifest in any other state it
returns false and changes nothing. Only the backup role may execute it.
"""

import asyncio
from typing import Final

import psycopg
import pytest
from alembic import command
from jobhunter.db.alembic_runner import alembic_config
from jobhunter.db.migrate import run_migrate_async

from tests.support.pg import PgServer, pg_one, pg_rows

pytestmark = pytest.mark.integration
M5_INDEX: Final = "0011_m5_purge_operation_index"
M5_UNSHIP: Final = "0012_m5_unship_manifest"
_VERSION: Final = "SELECT version_num FROM alembic_version"
# Every function of the application schema: its signature, whether it runs as its owner, its settings, its
# owner and who may execute it (`PUBLIC` for every role).
_FUNCTIONS: Final = (
    "SELECT p.proname, pg_get_function_identity_arguments(p.oid), pg_get_function_result(p.oid), "
    "p.prosecdef, p.proconfig, pg_get_userbyid(p.proowner), "
    "ARRAY(SELECT (CASE a.grantee WHEN 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END) || ':' || "
    "a.privilege_type FROM aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a ORDER BY 1) "
    "FROM pg_proc p WHERE p.pronamespace = CAST('public' AS regnamespace) ORDER BY 1, 2"
)
UNSHIP: Final = (
    "unship_manifest",
    "manifest_id bigint",
    "boolean",
    True,
    ("search_path=public, pg_temp",),
    "jobhunter_migrator",
    ("jobhunter_backup:EXECUTE", "jobhunter_migrator:EXECUTE"),  # PUBLIC may not
)
_EXECUTE: Final = "SELECT has_function_privilege(%s, 'public.unship_manifest(bigint)', 'EXECUTE')"


def functions(server: PgServer) -> list[tuple[object, ...]]:
    """`_FUNCTIONS`' rows, each array a tuple."""
    return [
        tuple(tuple(value) if isinstance(value, list) else value for value in found)
        for found in pg_rows(server, "migrator", _FUNCTIONS)
    ]


async def upgrade(server: PgServer, revision: str) -> None:
    await asyncio.to_thread(
        command.upgrade, alembic_config(server.dsn("migrator", driver="sqlalchemy")), revision
    )


async def downgrade(server: PgServer, revision: str) -> None:
    await asyncio.to_thread(
        command.downgrade, alembic_config(server.dsn("migrator", driver="sqlalchemy")), revision
    )


async def test_the_function_exists_and_downgrades(fresh_pg_server: PgServer, make_config) -> None:
    """0012 adds exactly the one function, owned by the migrator, `SECURITY DEFINER` with its `search_path`
    pinned, executable by the backup role alone; its downgrade leaves the 0011 functions, and upgrading again
    gives the 0012 ones. `migrate` records its floor, 0011, not destructive."""
    server = fresh_pg_server
    await upgrade(server, M5_INDEX)
    at_0011 = functions(server)
    await upgrade(server, M5_UNSHIP)
    assert pg_one(server, "migrator", _VERSION) == (M5_UNSHIP,)
    at_0012 = functions(server)
    assert set(at_0012) - set(at_0011) == {UNSHIP} and set(at_0011) < set(at_0012)
    for role, may in (("jobhunter_backup", True), ("jobhunter_app", False), ("jobhunter_restore", False)):
        assert pg_one(server, "migrator", _EXECUTE, role) == (may,), role

    await downgrade(server, M5_INDEX)
    assert pg_one(server, "migrator", _VERSION) == (M5_INDEX,)
    assert functions(server) == at_0011
    await upgrade(server, M5_UNSHIP)
    assert functions(server) == at_0012

    await run_migrate_async(make_config("migrate", server))
    record = "SELECT compat_floor, destructive FROM schema_compat WHERE revision = %s"
    assert pg_one(server, "migrator", record, M5_UNSHIP) == (M5_INDEX, False)
    # `migrate`'s grants step keeps it so (the queue's own functions are new beside it)
    assert [found for found in functions(server) if found[0] == UNSHIP[0]] == [UNSHIP]


# A manifest in each state, inserted as the migrator in a state its CHECKs allow (no transition): the
# `executing` one with its file's hash recorded, as a purge interrupted after A3 leaves it (R3-I1).
_MANIFEST: Final = (
    "INSERT INTO deletion_manifests (manifest_uuid, kind, status, scope, file_sha256, completed_at, "
    "shipped_at, shipment_id) VALUES (gen_random_uuid(), 'job_tombstone', %s, "
    "'{\"max_ids\": {\"jobs\": 1}}', '\\x01', %s, %s, %s) RETURNING id"
)
_ROW: Final = (
    "SELECT status, scope, file_sha256, completed_at, shipped_at, shipment_id, replay_log, request "
    "FROM deletion_manifests WHERE id = %s"
)
COMPLETED_AT: Final = "2026-11-10 08:00:00+00"
SHIPPED_AT: Final = "2026-11-10 08:05:00+00"


def manifest(server: PgServer, status: str, shipment: int | None = None) -> int:
    completed = None if status == "executing" else COMPLETED_AT
    shipped = SHIPPED_AT if status == "shipped" else None
    row = pg_one(server, "migrator", _MANIFEST, status, completed, shipped, shipment)
    assert row is not None
    return int(str(row[0]))


def row(server: PgServer, manifest_id: int) -> tuple[object, ...] | None:
    return pg_one(server, "migrator", _ROW, manifest_id)


def test_the_backup_role_can_only_unship_a_shipped_manifest(migrated: PgServer) -> None:
    """As the backup role, every UPDATE of `deletion_manifests` is refused: completing an `executing`
    manifest (R3-I1), editing a completion time, editing a manifest's content. `unship_manifest` changes
    nothing on an `executing` or a `completed` manifest, nor on an id no manifest has, and returns false; on a
    `shipped` one it returns true and leaves it `completed`, its shipment cleared and its completion time
    kept, every other column as it was. The application role may not execute it."""
    receipt = pg_one(
        migrated, "migrator", "INSERT INTO manifest_shipments (restic_snapshot_id) VALUES ('s') RETURNING id"
    )
    assert receipt is not None
    executing, completed = manifest(migrated, "executing"), manifest(migrated, "completed")
    shipped = manifest(migrated, "shipped", int(str(receipt[0])))
    before = {each: row(migrated, each) for each in (executing, completed, shipped)}

    for denied, target in (
        ("UPDATE deletion_manifests SET status = 'completed', completed_at = now() WHERE id = %s", executing),
        ("UPDATE deletion_manifests SET completed_at = now() - interval '1 day' WHERE id = %s", completed),
        (
            "UPDATE deletion_manifests SET shipped_at = NULL, shipment_id = NULL, status = 'completed' "
            "WHERE id = %s",
            shipped,
        ),
        ("UPDATE deletion_manifests SET scope = '{}' WHERE id = %s", completed),
        ("UPDATE deletion_manifests SET file_sha256 = '\\x02' WHERE id = %s", completed),
    ):
        with pytest.raises(psycopg.Error) as refusal:
            pg_one(migrated, "backup", denied, target)
        assert refusal.value.sqlstate == "42501", denied  # insufficient_privilege
    for unchanged in (executing, completed, 1 << 40):
        assert pg_one(migrated, "backup", "SELECT unship_manifest(%s)", unchanged) == (False,), unchanged
    assert {each: row(migrated, each) for each in (executing, completed, shipped)} == before

    assert pg_one(migrated, "backup", "SELECT unship_manifest(%s)", shipped) == (True,)
    status, *kept, completed_at, shipped_at, shipment_id, replay_log, request = before[shipped] or ()
    assert row(migrated, shipped) == ("completed", *kept, completed_at, None, None, replay_log, request)
    assert (shipped_at, shipment_id) != (None, None) and status == "shipped"
    assert pg_one(migrated, "backup", "SELECT unship_manifest(%s)", shipped) == (False,)  # once only

    with pytest.raises(psycopg.Error) as refusal:
        pg_one(migrated, "app", "SELECT unship_manifest(%s)", executing)
    assert refusal.value.sqlstate == "42501"
