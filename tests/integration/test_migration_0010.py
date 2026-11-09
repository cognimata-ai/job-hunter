"""Migration 0010 (M5 Task 1): the manifest, shipment, backup and export tables of §7.14, the fence's foreign
key to its manifest (M0 Deviation 1), the purged batch items, the dead-man state, the schema-compat record,
the new columns of existing tables (`api_idempotency.id`, `login_lockouts.generation`,
`llm_calls.content_purged_at`, `worker_heartbeat.tracing`), the transition triggers of the four new machines,
the backup role's grants, and the exact downgrade.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Final

import psycopg
import pytest
import sqlalchemy as sa
from alembic import command
from jobhunter.auth import throttle
from jobhunter.db.alembic_runner import alembic_config
from jobhunter.db.sql import FOREIGN_KEY_VIOLATION, UNIQUE_VIOLATION, PgErrorInfo, pg_error
from jobhunter.db.transitions import transition
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.fences import MANIFEST_KIND, insert_test_fence, insert_test_manifest
from tests.support.m2 import insert_profile_chain, insert_proposal
from tests.support.m3 import insert_bulk_operation, insert_id, insert_submission
from tests.support.pg import PgServer, pg_one, pg_rows
from tests.support.schema import refused, refused_check

pytestmark = pytest.mark.integration
M4_SCHEMA: Final = "0009_m4_ui"
M5_SCHEMA: Final = "0010_m5_operations"
TZ: Final = "timestamp with time zone"
NOW: Final = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)

# Every column as (name, type, NOT NULL, default, identity: 'a' ALWAYS, 'd' BY DEFAULT, '' none), by name.
_COLUMNS: Final = (
    "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, pg_get_expr(d.adbin, d.adrelid), "
    "CAST(a.attidentity AS text) FROM pg_attribute a "
    "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
    "WHERE a.attrelid = CAST(%s AS regclass) AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attname"
)
# §7.14 verbatim, plus the plan's columns: `deletion_manifests.request` (Deviation 53), `exports.size_bytes`
# and `exports.purge_generation` (Deviation 19), and the M5 tables of the brief.
TABLES: Final = {
    "deletion_manifests": [
        ("completed_at", TZ, False, None, ""),
        ("created_at", TZ, True, "now()", ""),
        ("file_sha256", "bytea", False, None, ""),
        ("id", "bigint", True, None, "a"),
        ("kind", "text", True, None, ""),
        ("manifest_uuid", "uuid", True, None, ""),
        ("replay_log", "jsonb", True, "'[]'::jsonb", ""),
        ("request", "jsonb", True, "'{}'::jsonb", ""),
        ("scope", "jsonb", True, "'{}'::jsonb", ""),
        ("shipment_id", "bigint", False, None, ""),
        ("shipped_at", TZ, False, None, ""),
        ("status", "text", True, None, ""),
    ],
    "manifest_shipments": [
        ("id", "bigint", True, None, "a"),
        ("restic_snapshot_id", "text", True, None, ""),
        ("shipped_at", TZ, True, "now()", ""),
    ],
    "manifest_shipment_items": [
        ("file_sha256", "bytea", True, None, ""),
        ("manifest_uuid", "uuid", True, None, ""),
        ("shipment_id", "bigint", True, None, ""),
    ],
    "backup_sets": [
        ("cv_file_count", "integer", False, None, ""),
        ("db_snapshot_at", TZ, False, None, ""),
        ("dump_bytes", "bigint", False, None, ""),
        ("dump_sha256", "bytea", False, None, ""),
        ("error", "text", False, None, ""),
        ("finished_at", TZ, False, None, ""),
        ("id", "bigint", True, None, "a"),
        ("manifest_count", "integer", False, None, ""),
        ("restic_snapshot_id", "text", False, None, ""),
        ("row_counts", "jsonb", False, None, ""),
        ("started_at", TZ, True, "now()", ""),
        ("status", "text", True, None, ""),
    ],
    "backup_runs": [
        ("backup_set_id", "bigint", False, None, ""),
        ("error", "text", False, None, ""),
        ("finished_at", TZ, False, None, ""),
        ("id", "bigint", True, None, "a"),
        ("kind", "text", True, None, ""),
        ("report", "jsonb", False, None, ""),
        ("started_at", TZ, True, "now()", ""),
        ("status", "text", True, None, ""),
    ],
    "exports": [
        ("created_at", TZ, True, "now()", ""),
        ("expires_at", TZ, True, None, ""),
        ("id", "bigint", True, None, "a"),
        ("purge_generation", "bigint", False, None, ""),
        ("sha256", "bytea", False, None, ""),
        ("size_bytes", "bigint", False, None, ""),
        ("status", "text", True, None, ""),
        ("storage_path", "text", False, None, ""),
    ],
    "purged_batch_items": [
        ("custom_id", "text", True, None, ""),
        ("manifest_id", "bigint", True, None, ""),
        ("purged_at", TZ, True, "now()", ""),
        ("submission_id", "bigint", True, None, ""),
    ],
    "deadman_state": [
        ("applied_grace_s", "integer", False, None, ""),
        ("applied_interval_s", "integer", False, None, ""),
        ("applied_paused", "boolean", False, None, ""),
        ("applied_timeout_s", "integer", False, None, ""),
        ("id", "smallint", True, None, ""),
        ("last_error_code", "text", False, None, ""),
        ("last_pinged_run_id", "bigint", False, None, ""),
        ("next_try_at", TZ, False, None, ""),
        ("synced_at", TZ, False, None, ""),
    ],
    "schema_compat": [
        ("compat_floor", "text", True, None, ""),
        ("destructive", "boolean", True, None, ""),
        ("recorded_at", TZ, True, "now()", ""),
        ("revision", "text", True, None, ""),
    ],
}
# The columns 0010 adds to tables of earlier milestones.
ADDED_COLUMNS: Final = {
    "api_idempotency": ("id", "bigint", True, None, "a"),
    "login_lockouts": ("generation", "bigint", True, None, "d"),
    "llm_calls": ("content_purged_at", TZ, False, None, ""),
    "worker_heartbeat": ("tracing", "jsonb", False, None, ""),
}
INDEXES: Final = {
    "deletion_manifests_open": (
        "CREATE INDEX deletion_manifests_open ON public.deletion_manifests USING btree "
        "(status, completed_at) WHERE (status <> 'shipped'::text)"
    ),
    "manifest_shipment_items_uuid": (
        "CREATE INDEX manifest_shipment_items_uuid ON public.manifest_shipment_items USING btree "
        "(manifest_uuid, file_sha256)"
    ),
    "backup_sets_latest": (
        "CREATE INDEX backup_sets_latest ON public.backup_sets USING btree (finished_at DESC) "
        "WHERE (status = 'succeeded'::text)"
    ),
    "backup_runs_latest": (
        "CREATE INDEX backup_runs_latest ON public.backup_runs USING btree (kind, finished_at DESC)"
    ),
    "exports_one_running": (
        "CREATE UNIQUE INDEX exports_one_running ON public.exports USING btree ((true)) "
        "WHERE (status = 'running'::text)"
    ),
}
_INDEXES: Final = "SELECT indexname, indexdef FROM pg_indexes WHERE indexname = ANY (%s) ORDER BY indexname"
# Every foreign key of a table 0010 creates or of `purge_fences`: (column, referenced table, ON DELETE).
_FOREIGN_KEYS: Final = (
    "SELECT CAST(c.conrelid AS regclass) || '.' || a.attname, CAST(CAST(c.confrelid AS regclass) AS text), "
    "CAST(c.confdeltype AS text) FROM pg_constraint c "
    "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey) "
    "WHERE c.contype = 'f' AND CAST(c.conrelid AS regclass) = ANY (CAST(%s AS regclass[])) ORDER BY 1"
)
FOREIGN_KEYS: Final = [
    ("backup_runs.backup_set_id", "backup_sets", "a"),
    ("deletion_manifests.shipment_id", "manifest_shipments", "a"),
    ("manifest_shipment_items.shipment_id", "manifest_shipments", "a"),
    ("purge_fences.manifest_id", "deletion_manifests", "a"),
    ("purge_fences.source_id", "sources", "a"),
    ("purged_batch_items.manifest_id", "deletion_manifests", "a"),
    ("purged_batch_items.submission_id", "batch_submissions", "c"),
]
# One assert_transition trigger per M5 machine, on its state column.
MACHINES: Final = ("deletion_manifests", "exports", "backup_sets", "backup_runs")
_TRIGGERS: Final = (
    "SELECT CAST(CAST(tgrelid AS regclass) AS text), pg_get_triggerdef(oid) FROM pg_trigger "
    "WHERE tgfoid = CAST('assert_transition' AS regproc) AND NOT tgisinternal "
    "AND CAST(tgrelid AS regclass) = ANY (CAST(%s AS regclass[])) ORDER BY 1"
)
_KEYS: Final = (
    "SELECT conname, CAST(contype AS text), pg_get_constraintdef(oid) FROM pg_constraint "
    "WHERE conrelid = CAST(%s AS regclass) AND contype IN ('p', 'u') ORDER BY conname"
)
_CONSTRAINT: Final = (
    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
    "WHERE conrelid = CAST(%s AS regclass) AND conname = %s"
)


async def upgrade(server: PgServer, revision: str) -> None:
    await asyncio.to_thread(
        command.upgrade, alembic_config(server.dsn("migrator", driver="sqlalchemy")), revision
    )


async def downgrade(server: PgServer, revision: str) -> None:
    dsn = server.dsn("migrator", driver="sqlalchemy")
    await asyncio.to_thread(command.downgrade, alembic_config(dsn), revision)


def refused_by_check(server: PgServer, table: str, statement: str, fragment: str) -> None:
    """`statement` (run as the migrator) violates a CHECK of `table` whose definition holds `fragment`."""
    with pytest.raises(psycopg.Error) as bad:
        pg_one(server, "migrator", statement)
    assert bad.value.sqlstate == "23514", bad.value
    definition = pg_one(server, "migrator", _CONSTRAINT, table, bad.value.diag.constraint_name)
    assert definition is not None and fragment in str(definition[0]), definition


async def test_the_m5_tables_match_the_spec_ddl(fresh_pg_server: PgServer) -> None:
    server = fresh_pg_server
    await upgrade(server, M4_SCHEMA)
    # rows 0010 meets: two idempotency records and two lockouts, which its identity columns number
    for key in ("aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"):
        pg_one(
            server,
            "migrator",
            "INSERT INTO api_idempotency (route, idem_key, request_sha256, status, expires_at) "
            "VALUES ('POST /api/runs', %s, '\\x00', 'in_progress', now() + interval '7 days')",
            key,
        )
    pg_one(
        server,
        "migrator",
        "INSERT INTO login_lockouts VALUES ('global', now(), 'r'), ('ip:192.0.2.1', now(), 'r')",
    )
    await upgrade(server, M5_SCHEMA)

    for table, columns in TABLES.items():
        assert pg_rows(server, "migrator", _COLUMNS, table) == columns, table
    for table, column in ADDED_COLUMNS.items():
        assert column in pg_rows(server, "migrator", _COLUMNS, table), table
    assert pg_rows(server, "migrator", _INDEXES, list(INDEXES)) == sorted(INDEXES.items())
    tables = [*TABLES, "purge_fences"]
    assert pg_rows(server, "migrator", _FOREIGN_KEYS, tables) == FOREIGN_KEYS
    assert pg_rows(server, "migrator", _TRIGGERS, list(MACHINES)) == sorted(
        (
            table,
            f"CREATE TRIGGER {table}_assert_transition BEFORE UPDATE OF status ON public.{table} "
            "FOR EACH ROW EXECUTE FUNCTION assert_transition('status')",
        )
        for table in MACHINES
    )

    # api_idempotency keeps its key; the surrogate is unique, set on the rows that were there, and generated
    assert pg_rows(server, "migrator", _KEYS, "api_idempotency") == [
        ("api_idempotency_id_key", "u", "UNIQUE (id)"),
        ("api_idempotency_pkey", "p", "PRIMARY KEY (route, idem_key)"),
    ]
    assert pg_rows(server, "migrator", "SELECT id FROM api_idempotency ORDER BY id") == [(1,), (2,)]
    with pytest.raises(psycopg.Error) as explicit:
        pg_one(
            server,
            "migrator",
            "INSERT INTO api_idempotency (id, route, idem_key, request_sha256, status, expires_at) "
            "VALUES (9, 'r', 'cccccccccccccccc', '\\x00', 'in_progress', now())",
        )
    assert explicit.value.sqlstate == "428C9"  # GENERATED ALWAYS
    assert pg_rows(server, "migrator", "SELECT generation FROM login_lockouts ORDER BY generation") == [
        (1,),
        (2,),
    ]

    # the CHECKs of §7.14, each against the column its state needs; the same row with it is accepted
    refused_by_check(
        server,
        "backup_sets",
        "INSERT INTO backup_sets (status, restic_snapshot_id, db_snapshot_at, row_counts, finished_at) "
        "VALUES ('succeeded', 'snap', now(), '{}', now())",
        "dump_sha256 IS NOT NULL",
    )
    pg_one(
        server,
        "migrator",
        "INSERT INTO backup_sets (status, restic_snapshot_id, dump_sha256, db_snapshot_at, row_counts, "
        "finished_at) VALUES ('succeeded', 'snap', '\\x00', now(), '{}', now())",
    )
    refused_by_check(
        server,
        "deletion_manifests",
        "INSERT INTO deletion_manifests (manifest_uuid, kind, status, completed_at) "
        "VALUES (gen_random_uuid(), 'source_purge', 'completed', now())",
        "file_sha256 IS NOT NULL",
    )
    pg_one(
        server,
        "migrator",
        "INSERT INTO deletion_manifests (manifest_uuid, kind, status, completed_at, file_sha256) "
        "VALUES (gen_random_uuid(), 'source_purge', 'completed', now(), '\\x00')",
    )
    refused_by_check(
        server,
        "deletion_manifests",
        "INSERT INTO deletion_manifests (manifest_uuid, kind, status, completed_at, file_sha256, shipped_at) "
        "VALUES (gen_random_uuid(), 'source_purge', 'shipped', now(), '\\x00', now())",
        "shipment_id IS NOT NULL",
    )
    pg_one(
        server,
        "migrator",
        "WITH s AS (INSERT INTO manifest_shipments (restic_snapshot_id) VALUES ('snap') RETURNING id) "
        "INSERT INTO deletion_manifests (manifest_uuid, kind, status, completed_at, file_sha256, shipped_at, "
        "shipment_id) SELECT gen_random_uuid(), 'source_purge', 'shipped', now(), '\\x00', now(), id FROM s",
    )
    refused_by_check(
        server,
        "exports",
        "INSERT INTO exports (status, expires_at, size_bytes) VALUES ('ready', now(), -1)",
        "size_bytes >= 0",
    )


async def test_purge_fences_manifest_fk_is_enforced(engine: AsyncEngine) -> None:
    async with engine.connect() as c:
        missing = (
            await c.execute(sa.text("SELECT coalesce(max(id), 0) + 1000 FROM deletion_manifests"))
        ).scalar_one()
        info = await refused(
            c, "INSERT INTO purge_fences (kind, manifest_id) VALUES ('global', :m)", {"m": missing}
        )
        assert info == PgErrorInfo(FOREIGN_KEY_VIOLATION, "purge_fences_manifest_id_fkey")

        # the test fences: each over an executing manifest of the purge that raises its kind
        fences = {
            "global": await insert_test_fence(c, kind="global"),
            "source": await insert_test_fence(c, kind="source", source_id="adzuna"),
            "profile": await insert_test_fence(c, kind="profile", profile_version_id=999_999),
            "jobs": await insert_test_fence(c, kind="jobs", job_ids=[5, 6], released=True),
        }
        manifests = (
            await c.execute(
                sa.text(
                    "SELECT f.kind, m.kind, m.status, f.job_ids, f.released_at IS NOT NULL "
                    "FROM purge_fences f JOIN deletion_manifests m ON m.id = f.manifest_id "
                    "WHERE f.id = ANY (:f) ORDER BY f.id"
                ),
                {"f": list(fences.values())},
            )
        ).all()
        assert [tuple(row) for row in manifests] == [
            ("global", MANIFEST_KIND["global"], "executing", [], False),
            ("source", MANIFEST_KIND["source"], "executing", [], False),
            ("profile", MANIFEST_KIND["profile"], "executing", [], False),
            ("jobs", MANIFEST_KIND["jobs"], "executing", [5, 6], True),
        ]

        # and the manifest cannot go while a fence names it
        info = await refused(
            c,
            "DELETE FROM deletion_manifests WHERE id = (SELECT manifest_id FROM purge_fences WHERE id = :f)",
            {"f": fences["global"]},
        )
        assert info == PgErrorInfo(FOREIGN_KEY_VIOLATION, "purge_fences_manifest_id_fkey")
        await c.rollback()


async def refused_edge(c: AsyncConnection, statement: str, params: dict[str, object], edge: str) -> None:
    """The trigger refuses `statement`'s state change `edge` (`table: from -> to`) with SQLSTATE 23514."""
    with pytest.raises(sa.exc.IntegrityError, match=f"disallowed transition {edge}") as bad:
        async with c.begin_nested():
            await c.execute(sa.text(statement), params)
    assert pg_error(bad.value).sqlstate == "23514"


SUCCEEDED_SET: Final = (
    "INSERT INTO backup_sets (status, restic_snapshot_id, dump_sha256, db_snapshot_at, row_counts, "
    "finished_at) VALUES ('succeeded', 'snap', '\\x00', now(), '{}', now()) RETURNING id"
)


async def test_illegal_edges_are_refused_by_the_trigger(engine: AsyncEngine) -> None:
    """Each refused UPDATE satisfies every CHECK, so the edge alone is wrong; the registry's edges pass."""
    async with engine.connect() as c:
        shipment = await insert_id(c, "manifest_shipments", {"restic_snapshot_id": "snap"})
        manifest = await insert_test_manifest(c, kind="source_purge")
        await refused_edge(
            c,
            "UPDATE deletion_manifests SET status = 'shipped', file_sha256 = '\\x00', completed_at = now(), "
            "shipped_at = now(), shipment_id = :s WHERE id = :m",
            {"s": shipment, "m": manifest},
            "deletion_manifests: executing -> shipped",
        )
        await transition(
            c, "deletion_manifests", manifest, ["executing"], "completed", set_={"file_sha256": b"\x01"}
        )
        await transition(
            c, "deletion_manifests", manifest, ["completed"], "shipped", set_={"shipment_id": shipment}
        )
        shipped = (
            await c.execute(
                sa.text(
                    "SELECT status, completed_at IS NOT NULL, shipped_at IS NOT NULL, shipment_id "
                    "FROM deletion_manifests WHERE id = :m"
                ),
                {"m": manifest},
            )
        ).one()
        assert tuple(shipped) == ("shipped", True, True, shipment)

        ready = await insert_id(c, "exports", {"status": "ready", "expires_at": NOW + timedelta(days=7)})
        await refused_edge(
            c,
            "UPDATE exports SET status = 'running' WHERE id = :e",
            {"e": ready},
            "exports: ready -> running",
        )
        await transition(c, "exports", ready, ["ready"], "expired")

        succeeded = (await c.execute(sa.text(SUCCEEDED_SET))).scalar_one()
        await refused_edge(
            c,
            "UPDATE backup_sets SET status = 'failed' WHERE id = :b",
            {"b": succeeded},
            "backup_sets: succeeded -> failed",
        )
        run = await insert_id(c, "backup_runs", {"kind": "prune", "status": "running"})
        await transition(c, "backup_runs", run, ["running"], "succeeded", set_={"report": {"pruned": 1}})
        await refused_edge(
            c,
            "UPDATE backup_runs SET status = 'failed' WHERE id = :r",
            {"r": run},
            "backup_runs: succeeded -> failed",
        )
        await c.rollback()


async def test_a_proposal_moves_to_purged_with_its_items_nulled_in_one_update(engine: AsyncEngine) -> None:
    async with engine.connect() as c:
        chain = await insert_profile_chain(c)
        proposals = {
            "running": await insert_proposal(c, chain.profile_version_id),
            "ready": await insert_proposal(
                c, chain.profile_version_id, "ready", items=[{"query": "Testrolle"}]
            ),
            "failed": await insert_proposal(c, chain.profile_version_id, "failed"),
        }
        # a ready proposal holds its items: removing them first is refused, so the purge does both at once
        await refused_check(
            c,
            "query_proposals",
            "UPDATE query_proposals SET items = NULL WHERE id = :p",
            {"p": proposals["ready"]},
            "items IS NOT NULL",
        )
        for state, proposal in proposals.items():
            await transition(c, "query_proposals", proposal, [state], "purged", set_={"items": None})
        rows = await c.execute(
            sa.text("SELECT status, items FROM query_proposals WHERE id = ANY (:p) ORDER BY id"),
            {"p": list(proposals.values())},
        )
        assert [tuple(row) for row in rows] == [("purged", None)] * 3
        # nothing leaves `purged`
        await refused_edge(
            c,
            "UPDATE query_proposals SET status = 'ready', items = '[]' WHERE id = :p",
            {"p": proposals["ready"]},
            "query_proposals: purged -> ready",
        )
        await c.rollback()


EXPORT: Final = "INSERT INTO exports (status, expires_at) VALUES (:s, now() + interval '7 days') RETURNING id"


async def test_one_running_export(engine: AsyncEngine) -> None:
    async with engine.connect() as c:
        first = (await c.execute(sa.text(EXPORT), {"s": "running"})).scalar_one()
        assert await refused(c, EXPORT, {"s": "running"}) == PgErrorInfo(
            UNIQUE_VIOLATION, "exports_one_running"
        )
        # exports that are not running are not limited
        for status in ("ready", "ready", "failed", "expired", "purged"):
            await c.execute(sa.text(EXPORT), {"s": status})
        # once the first one is ready, the next one may run
        await transition(
            c,
            "exports",
            first,
            ["running"],
            "ready",
            set_={
                "storage_path": "exports/1.age",
                "sha256": b"\x01",
                "size_bytes": 10,
                "purge_generation": 1,
            },
        )
        await c.execute(sa.text(EXPORT), {"s": "running"})
        counts = await c.execute(
            sa.text("SELECT status, count(*) FROM exports GROUP BY status ORDER BY status")
        )
        assert [tuple(row) for row in counts] == [
            ("expired", 1),
            ("failed", 1),
            ("purged", 1),
            ("ready", 3),
            ("running", 1),
        ]
        await c.rollback()


IP: Final = "192.0.2.7"
LOCKOUT: Final = sa.text("SELECT generation, locked_until FROM login_lockouts WHERE scope = :s")


async def fail_login(engine: AsyncEngine, ip: str, now: datetime) -> None:
    async with engine.begin() as c:
        await throttle.serialize(c)
        await throttle.record_attempt(c, ip=ip, username_hmac=b"\x00", success=False, now=now)


async def lockout(engine: AsyncEngine, ip: str) -> tuple[int, datetime]:
    async with engine.connect() as c:
        generation, locked_until = (await c.execute(LOCKOUT, {"s": throttle.ip_scope(ip)})).one()
    return int(generation), locked_until


async def test_a_renewed_lockout_takes_a_new_generation(engine: AsyncEngine) -> None:
    for _ in range(throttle.IP_MAX_FAILURES):
        await fail_login(engine, IP, NOW)
    locked = await lockout(engine, IP)
    assert locked[1] == NOW + throttle.IP_LOCK
    # a renewal that does not extend the lockout still takes a new generation
    await fail_login(engine, IP, NOW)
    same_end = await lockout(engine, IP)
    assert same_end[1] == locked[1] and same_end[0] > locked[0]
    # and one that extends it, again
    later = NOW + timedelta(minutes=1)
    await fail_login(engine, IP, later)
    extended = await lockout(engine, IP)
    assert extended[1] == later + throttle.IP_LOCK and extended[0] > same_end[0]
    # another address's lockout has a generation of its own
    for _ in range(throttle.IP_MAX_FAILURES):
        await fail_login(engine, "192.0.2.8", later)
    assert (await lockout(engine, "192.0.2.8"))[0] > extended[0]


PURGED_ITEM: Final = (
    "INSERT INTO purged_batch_items (submission_id, custom_id, manifest_id) VALUES (:s, :c, :m)"
)


async def test_purged_batch_items_go_with_their_submission(engine: AsyncEngine) -> None:
    insert = sa.text(PURGED_ITEM)
    count = sa.text("SELECT count(*) FROM purged_batch_items WHERE submission_id = :s")
    async with engine.connect() as c:
        chain = await insert_profile_chain(c)
        submission = await insert_submission(
            c, chain.llm_tuple_id, await insert_bulk_operation(c, chain.llm_tuple_id)
        )
        manifest = await insert_test_manifest(c, kind="source_purge")
        for custom_id in ("t1_a1", "t2_a1"):
            await c.execute(insert, {"s": submission, "c": custom_id, "m": manifest})
        assert await refused(c, PURGED_ITEM, {"s": submission, "c": "t1_a1", "m": manifest}) == PgErrorInfo(
            UNIQUE_VIOLATION, "purged_batch_items_pkey"
        )
        assert await refused(
            c, PURGED_ITEM, {"s": submission, "c": "t3_a1", "m": manifest + 1000}
        ) == PgErrorInfo(FOREIGN_KEY_VIOLATION, "purged_batch_items_manifest_id_fkey")
        assert (await c.execute(count, {"s": submission})).scalar_one() == 2

        await c.execute(sa.text("DELETE FROM batch_submissions WHERE id = :s"), {"s": submission})
        assert (await c.execute(count, {"s": submission})).scalar_one() == 0
        # the manifest that recorded them stays
        kept = sa.text("SELECT count(*) FROM deletion_manifests WHERE id = :m")
        assert (await c.execute(kept, {"m": manifest})).scalar_one() == 1
        await c.rollback()


# Every table on which the backup role holds a write privilege: (table, INSERT, UPDATE, DELETE, TRUNCATE).
_BACKUP_WRITES: Final = (
    "SELECT c.relname, has_table_privilege('jobhunter_backup', c.oid, 'INSERT'), "
    "has_table_privilege('jobhunter_backup', c.oid, 'UPDATE'), "
    "has_table_privilege('jobhunter_backup', c.oid, 'DELETE'), "
    "has_table_privilege('jobhunter_backup', c.oid, 'TRUNCATE') "
    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
    "AND (has_table_privilege('jobhunter_backup', c.oid, 'INSERT') "
    "OR has_table_privilege('jobhunter_backup', c.oid, 'UPDATE') "
    "OR has_table_privilege('jobhunter_backup', c.oid, 'DELETE') "
    "OR has_table_privilege('jobhunter_backup', c.oid, 'TRUNCATE')) ORDER BY 1"
)


def test_the_backup_role_writes_its_four_tables_and_nothing_else(migrated: PgServer) -> None:
    row = pg_one(migrated, "backup", "INSERT INTO backup_sets (status) VALUES ('running') RETURNING id")
    assert row is not None
    backup_set = row[0]
    pg_one(
        migrated,
        "backup",
        "UPDATE backup_sets SET status = 'succeeded', finished_at = now(), restic_snapshot_id = 'snap', "
        "dump_sha256 = '\\x00', db_snapshot_at = now(), row_counts = '{}', manifest_count = 0 WHERE id = %s",
        backup_set,
    )
    pg_one(
        migrated,
        "backup",
        "INSERT INTO backup_runs (kind, backup_set_id, status) VALUES ('restore_test', %s, 'running')",
        backup_set,
    )
    shipment = pg_one(
        migrated, "backup", "INSERT INTO manifest_shipments (restic_snapshot_id) VALUES ('m') RETURNING id"
    )
    assert shipment is not None
    pg_one(
        migrated,
        "backup",
        "INSERT INTO manifest_shipment_items (shipment_id, manifest_uuid, file_sha256) "
        "VALUES (%s, gen_random_uuid(), '\\x00')",
        shipment[0],
    )
    assert pg_one(migrated, "migrator", "SELECT status FROM backup_sets WHERE id = %s", backup_set) == (
        "succeeded",
    )

    for denied in (
        "INSERT INTO alerts (code, severity, dedup_key, message) VALUES ('a', 'warn', 'k', 'm')",
        "INSERT INTO deletion_manifests (manifest_uuid, kind, status) "
        "VALUES (gen_random_uuid(), 'source_purge', 'executing')",
        "DELETE FROM backup_sets",
    ):
        with pytest.raises(psycopg.Error) as refusal:
            pg_one(migrated, "backup", denied)
        assert refusal.value.sqlstate == "42501", denied  # insufficient_privilege

    assert pg_rows(migrated, "migrator", _BACKUP_WRITES) == [
        ("backup_runs", True, True, False, False),
        ("backup_sets", True, True, False, False),
        ("manifest_shipment_items", True, True, False, False),
        ("manifest_shipments", True, True, False, False),
    ]


# The schema as the catalogue holds it: what an exact downgrade must give back.
_CATALOGUE: Final = (
    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1",
    "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
    "pg_get_expr(d.adbin, d.adrelid), CAST(a.attidentity AS text) FROM pg_attribute a "
    "JOIN pg_class c ON c.oid = a.attrelid "
    "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
    "WHERE c.relnamespace = CAST('public' AS regnamespace) AND c.relkind IN ('r', 'p', 'v') "
    "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY 1, 2",
    "SELECT CAST(CAST(conrelid AS regclass) AS text), conname, pg_get_constraintdef(oid) FROM pg_constraint "
    "WHERE connamespace = CAST('public' AS regnamespace) ORDER BY 1, 2",
    "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' ORDER BY 1",
    "SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger WHERE NOT tgisinternal ORDER BY 1",
    "SELECT sequencename FROM pg_sequences WHERE schemaname = 'public' ORDER BY 1",
    "SELECT proname, pg_get_function_identity_arguments(oid) FROM pg_proc "
    "WHERE pronamespace = CAST('public' AS regnamespace) ORDER BY 1, 2",
)


Catalogue = tuple[tuple[object, ...] | None, list[list[tuple[object, ...]]]]


def catalogue(server: PgServer) -> Catalogue:
    """The Alembic revision, and the rows of each `_CATALOGUE` statement (the tables first)."""
    version = pg_one(server, "migrator", "SELECT version_num FROM alembic_version")
    return version, [pg_rows(server, "migrator", statement) for statement in _CATALOGUE]


async def test_downgrade_to_0009_and_upgrade_again(fresh_pg_server: PgServer) -> None:
    """The downgrade leaves exactly the 0009 schema, the rows of the tables it alters kept; upgrading again
    gives the 0010 schema. With a manifest stored the downgrade is refused: it would delete the record of a
    purge, so the database stays at 0010 with it."""
    server = fresh_pg_server
    await upgrade(server, M4_SCHEMA)
    pg_one(
        server,
        "migrator",
        "INSERT INTO api_idempotency (route, idem_key, request_sha256, status, expires_at) "
        "VALUES ('POST /api/runs', 'aaaaaaaaaaaaaaaa', '\\x00', 'in_progress', now())",
    )
    pg_one(server, "migrator", "INSERT INTO login_lockouts VALUES ('global', now(), 'r')")
    at_0009 = catalogue(server)
    assert at_0009[0] == (M4_SCHEMA,) and ("deletion_manifests",) not in at_0009[1][0]

    await upgrade(server, M5_SCHEMA)
    at_0010 = catalogue(server)
    assert at_0010[0] == (M5_SCHEMA,)
    assert {(t,) for t in TABLES} <= set(at_0010[1][0]) - set(at_0009[1][0])

    await downgrade(server, M4_SCHEMA)
    assert catalogue(server) == at_0009
    kept = "SELECT (SELECT count(*) FROM api_idempotency), (SELECT count(*) FROM login_lockouts)"
    assert pg_one(server, "migrator", kept) == (1, 1)

    await upgrade(server, M5_SCHEMA)
    assert catalogue(server) == at_0010
    assert pg_one(server, "migrator", kept) == (1, 1)

    pg_one(
        server,
        "migrator",
        "INSERT INTO deletion_manifests (manifest_uuid, kind, status) "
        "VALUES (gen_random_uuid(), 'source_purge', 'executing')",
    )
    with pytest.raises(sa.exc.DBAPIError, match="deletion_manifests holds rows"):
        await downgrade(server, M4_SCHEMA)
    assert catalogue(server) == at_0010
    assert pg_one(server, "migrator", "SELECT count(*) FROM deletion_manifests") == (1,)
