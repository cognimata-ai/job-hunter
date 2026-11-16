"""Identity advancement (spec §19.3 steps 1, 3 and 5, §19.5 "Identity advancement"; plan M5 Task 23, R25,
Deviation 38).

`rewrite_missing_files` writes the file of each manifest the database holds with its content (its hash
recorded) but the stream lacks, from its `scope`, and stops on a scope that is not its file's content;
`manifest_bounds` takes, per table, the largest `max_ids`, watermark and listed id over every file and every
recorded scope; `advance_identities` moves each sequence whose next value is at or below its table's target
(its bound, or its column's maximum) to restart just above it with `setval`: never lower, nothing for a table
already above, and no lock that a reader waits for.

The shared migrated database's sequences stand wherever earlier tests left them, so each bound on one of its
tables is set relative to that table's next value; the probe table of this module (`probe`, created and
dropped around a test) gives exact sequence states. Every manifest and row is invented.
"""

import hashlib
import os
import stat
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

import procrastinate
import psycopg
import pytest
import sqlalchemy as sa
from jobhunter.db.migrate import run_migrate_async
from jobhunter.db.session import make_null_engine
from jobhunter.db.transitions import transition
from jobhunter.ids import uuid7
from jobhunter.privacy.identities import advance_identities, manifest_bounds, rewrite_missing_files
from jobhunter.privacy.manifests import (
    ManifestConflict,
    ManifestContent,
    ManifestInvalid,
    ManifestKind,
    ManifestRow,
    Operation,
    canonical_bytes,
    complete_manifest,
    insert_manifest,
    manifest_file_name,
    record_manifest,
    write_manifest_file,
)
from psycopg.types.json import Jsonb
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from tests.support.fixtures import ConfigFactory
from tests.support.pg import PgServer, pg_one, pg_rows

pytestmark = pytest.mark.integration

NOW: Final = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
PROBE: Final = "identity_probe"
_RELATION_LOCKS: Final = (
    "SELECT CAST(CAST(relation AS regclass) AS text), mode FROM pg_locks "
    "WHERE pid = %s AND locktype = 'relation' "
    "AND mode NOT IN ('AccessShareLock', 'RowShareLock', 'RowExclusiveLock') ORDER BY 1, 2"
)


def content(
    manifest_uuid: str | None = None,
    *,
    kind: ManifestKind = "source_purge",
    max_ids: Mapping[str, int] | None = None,
    operations: Iterable[Operation] = (),
    **extra: Any,
) -> ManifestContent:
    """A manifest's content of format 1, with the bounds and operations given."""
    stamp = NOW.isoformat()
    built: dict[str, Any] = {
        "format": 1,
        "manifest_uuid": manifest_uuid or str(uuid7()),
        "kind": kind,
        "admitted_at": stamp,
        "fenced_at": stamp,
        "max_ids": dict(max_ids or {}),
        "operations": list(operations),
        **extra,
    }
    return built  # type: ignore[return-value]  # the TypedDict's keys are built above


def sequence_state(server: PgServer, table: str) -> tuple[int, bool]:
    """`(last_value, is_called)` of the sequence of `table`'s `id`."""
    found = pg_one(server, "migrator", "SELECT pg_get_serial_sequence(%s, 'id')", f"public.{table}")
    assert found is not None
    row = pg_one(server, "migrator", f"SELECT last_value, is_called FROM {found[0]}")  # noqa: S608 (catalogue)
    assert row is not None
    return int(str(row[0])), bool(row[1])


def next_id(server: PgServer, table: str) -> int:
    """The value the next insert into `table` gets: `last_value + 1` once called, else `last_value`."""
    last_value, is_called = sequence_state(server, table)
    return last_value + 1 if is_called else last_value


def names(directory: Path) -> list[str]:
    """The entries of `directory`, by name."""
    return sorted(os.listdir(directory))


def sequences(server: PgServer) -> list[tuple[object, ...]]:
    return pg_rows(
        server,
        "migrator",
        "SELECT sequencename, last_value FROM pg_sequences WHERE schemaname = 'public' ORDER BY 1",
    )


def insert_probe(server: PgServer) -> int:
    row = pg_one(server, "migrator", f"INSERT INTO {PROBE} DEFAULT VALUES RETURNING id")  # noqa: S608
    assert row is not None
    return int(str(row[0]))


def insert_alert(server: PgServer) -> int:
    row = pg_one(
        server,
        "migrator",
        "INSERT INTO alerts (code, severity, dedup_key, message) "
        "VALUES ('test.identity', 'info', %s, 'an invented alert') RETURNING id",
        f"identity:{uuid7()}",
    )
    assert row is not None
    return int(str(row[0]))


async def recorded(
    engine: AsyncEngine,
    *,
    status: str,
    max_ids: Mapping[str, int],
    operations: Iterable[Operation] = (),
) -> tuple[ManifestRow, ManifestContent]:
    """A source purge's manifest whose content is recorded with its file's hash (A3), then completed or
    shipped as `status` says; its file is written nowhere."""
    async with engine.begin() as conn:
        row = await insert_manifest(conn, kind="source_purge", request={}, now=NOW)
        body = content(str(row.manifest_uuid), max_ids=max_ids, operations=operations)
        await record_manifest(conn, row, body, hashlib.sha256(canonical_bytes(body)).digest())
        if status in ("completed", "shipped"):
            await complete_manifest(conn, row.id)
        if status == "shipped":
            shipment = await conn.execute(
                sa.text("INSERT INTO manifest_shipments (restic_snapshot_id) VALUES ('snap') RETURNING id")
            )
            set_ = {"shipment_id": shipment.scalar_one()}
            await transition(conn, "deletion_manifests", row.id, ["completed"], "shipped", set_=set_)
    return row, body


async def advance(engine: AsyncEngine, stream: Path) -> dict[str, int]:
    async with engine.begin() as conn:
        return await advance_identities(conn, await manifest_bounds(conn, stream))


async def bounds_of(engine: AsyncEngine, stream: Path) -> dict[str, int]:
    async with engine.connect() as conn:
        return await manifest_bounds(conn, stream)


@pytest.fixture
async def migrator(migrated: PgServer) -> AsyncIterator[AsyncEngine]:
    engine = make_null_engine(migrated.dsn("migrator", driver="sqlalchemy"))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def stream(tmp_path: Path) -> Path:
    """An empty manifest stream of the test's own (mode 0700, as the image makes the volume's mount point)."""
    directory = tmp_path / "manifests"
    directory.mkdir(mode=0o700)
    return directory


@pytest.fixture
def probe(migrated: PgServer) -> Iterator[str]:
    """A table of `public` with a fresh identity sequence (`last_value` 1, not called), dropped after."""
    pg_rows(
        migrated, "migrator", f"CREATE TABLE {PROBE} (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY)"
    )
    try:
        yield PROBE
    finally:
        pg_rows(migrated, "migrator", f"DROP TABLE IF EXISTS {PROBE}")


async def test_identities_restart_above_every_manifest_bound(
    migrated: PgServer, migrator: AsyncEngine, stream: Path
) -> None:
    tables = ("alerts", "login_attempts", "posting_observations", "job_revisions", "operations", "exports")
    first = {table: next_id(migrated, table) for table in tables}
    # A file of the stream: its max_ids, its watermark, an id it lists under a parent bound (a `job_revisions`
    # id, which `job_revision_sources.job_revision_id` holds) and an id it keeps.
    filed = content(
        max_ids={"alerts": first["alerts"] + 100, "login_attempts": 0},
        watermark={
            "fenced_at": NOW.isoformat(),
            "max_ids": {"posting_observations": first["posting_observations"] + 120},
        },
        operations=[
            {
                "op": "row_delete",
                "table": "job_revision_sources",
                "ids": [first["job_revisions"] + 140],
                "bound": {"by": "parent", "table": "job_revisions", "column": "job_revision_id"},
            },
            {
                "op": "row_delete",
                "table": "operations",
                "selector": "all",
                "keep": [first["operations"] + 130],
                "bound": {"by": "id"},
            },
        ],
    )
    write_manifest_file(stream, filed)
    # A manifest the database holds with its content and whose file is not in the stream: its scope's max_ids
    # (above the file's 0 for the same table) and the ids it redacts.
    await recorded(
        migrator,
        status="executing",
        max_ids={"login_attempts": first["login_attempts"] + 150},
        operations=[
            {"op": "field_redact", "table": "exports", "ids": [first["exports"] + 160], "columns": ["error"]}
        ],
    )
    expected = {
        "alerts": first["alerts"] + 100,
        "login_attempts": first["login_attempts"] + 150,
        "posting_observations": first["posting_observations"] + 120,
        "job_revisions": first["job_revisions"] + 140,
        "operations": first["operations"] + 130,
        "exports": first["exports"] + 160,
    }
    assert await bounds_of(migrator, stream) == expected

    advanced = await advance(migrator, stream)
    assert {table: advanced[table] for table in tables} == expected
    assert {table: next_id(migrated, table) for table in tables} == {t: b + 1 for t, b in expected.items()}
    assert insert_alert(migrated) == expected["alerts"] + 1


async def test_an_identity_is_never_lowered(
    migrated: PgServer, migrator: AsyncEngine, stream: Path, probe: str
) -> None:
    pg_one(migrated, "migrator", f"SELECT setval(pg_get_serial_sequence('public.{PROBE}', 'id'), 50, true)")
    write_manifest_file(stream, content(max_ids={PROBE: 10}))
    assert PROBE not in await advance(migrator, stream)
    assert sequence_state(migrated, PROBE) == (50, True)
    assert insert_probe(migrated) == 51
    # Nor below its column's rows: a row stands above the sequence (an explicit id), so the target is the
    # row's id, not the lower bound.
    pg_rows(migrated, "migrator", f"INSERT INTO {PROBE} (id) OVERRIDING SYSTEM VALUE VALUES (80)")  # noqa: S608
    assert (await advance(migrator, stream))[PROBE] == 80
    assert insert_probe(migrated) == 81


async def test_a_table_above_every_bound_is_untouched(
    migrated: PgServer, migrator: AsyncEngine, stream: Path, probe: str
) -> None:
    assert [insert_probe(migrated) for _ in range(3)] == [1, 2, 3]
    write_manifest_file(stream, content(max_ids={PROBE: 2, "alerts": 0}))
    before = sequences(migrated)
    assert await advance(migrator, stream) == {}
    assert sequences(migrated) == before  # no sequence of the schema was set
    assert sequence_state(migrated, PROBE) == (3, True)


async def test_listed_ids_and_serial_columns_are_advanced_past(
    migrated: PgServer, migrator: AsyncEngine, stream: Path, queue_app: procrastinate.App
) -> None:
    # The queue's restored sequence: no job above it yet. `procrastinate_jobs.id` is a bigserial.
    first = next_id(migrated, "procrastinate_jobs")
    listed = [first + 3, first + 9]
    named_by_events = (
        first + 12
    )  # a job id that a `procrastinate_events` deletion names, under its parent bound
    write_manifest_file(
        stream,
        content(
            kind="delete_everything",
            max_ids={"procrastinate_jobs": first - 1},
            operations=[
                {
                    "op": "row_delete",
                    "table": "procrastinate_events",
                    "ids": [named_by_events],
                    "bound": {"by": "parent", "table": "procrastinate_jobs", "column": "job_id"},
                },
                {"op": "row_delete", "table": "procrastinate_jobs", "ids": listed, "bound": {"by": "id"}},
            ],
        ),
    )
    assert await bounds_of(migrator, stream) == {"procrastinate_jobs": named_by_events}
    advanced = await advance(migrator, stream)
    assert advanced["procrastinate_jobs"] == named_by_events
    assert "procrastinate_events" not in advanced  # the ids its deletion lists are job ids
    job_id = await queue_app.configure_task("jobhunter.tests.identity_probe").defer_async()
    assert job_id == named_by_events + 1


async def test_an_uncalled_sequence_at_the_bound_is_advanced(
    migrated: PgServer, migrator: AsyncEngine, stream: Path, probe: str
) -> None:
    assert sequence_state(migrated, PROBE) == (1, False)  # fresh: its next value is 1, the bound itself
    write_manifest_file(stream, content(max_ids={PROBE: 1}))
    assert (await advance(migrator, stream))[PROBE] == 1
    assert sequence_state(migrated, PROBE) == (1, True)
    assert insert_probe(migrated) == 2


async def test_a_called_sequence_at_the_bound_is_left_alone(
    migrated: PgServer, migrator: AsyncEngine, stream: Path, probe: str
) -> None:
    assert insert_probe(migrated) == 1  # `last_value` 1, called: the next value, 2, is above the bound
    write_manifest_file(stream, content(max_ids={PROBE: 1}))
    assert PROBE not in await advance(migrator, stream)
    assert sequence_state(migrated, PROBE) == (1, True)
    assert insert_probe(migrated) == 2


async def test_advancement_takes_no_access_exclusive_lock(
    migrated: PgServer, migrator: AsyncEngine, stream: Path, probe: str
) -> None:
    write_manifest_file(stream, content(max_ids={PROBE: 40, "alerts": next_id(migrated, "alerts") + 40}))
    async with migrator.begin() as conn:
        advanced = await advance_identities(conn, await manifest_bounds(conn, stream))
        assert {PROBE, "alerts"} <= set(advanced)
        pid = (await conn.execute(sa.text("SELECT pg_backend_pid()"))).scalar_one()
        # While the advancing transaction is open, a reader of each table it advanced gets its rows at once: a
        # lock it had to wait for would end the read with SQLSTATE 55P03 after `lock_timeout`.
        for table in sorted(advanced):
            with psycopg.connect(migrated.dsn("app"), autocommit=True) as reader:
                reader.execute("SET lock_timeout = '250ms'")
                reader.execute(f'SELECT count(*) FROM public."{table}"')  # noqa: S608 (catalogue names)
        # Nor does it hold a relation lock that a writer would wait for.
        assert pg_rows(migrated, "superuser", _RELATION_LOCKS, pid) == []


@pytest.mark.parametrize("status", ["executing", "completed", "shipped"])
async def test_a_missing_file_is_rewritten_from_scope_before_advancing(
    migrated: PgServer, migrator: AsyncEngine, make_config: ConfigFactory, stream: Path, status: str
) -> None:
    bound = next_id(migrated, "alerts") + 200
    missing, body = await recorded(migrator, status=status, max_ids={"alerts": bound})
    # A manifest whose file the stream holds keeps it as it is.
    present, present_body = await recorded(migrator, status="completed", max_ids={"alerts": 0})
    write_manifest_file(stream, present_body)
    present_path = stream / manifest_file_name(present.manifest_uuid)
    present_written = present_path.stat().st_mtime_ns

    with capture_logs() as logs:
        ran = await run_migrate_async(
            make_config("migrate"), now=NOW, steps=frozenset({"sequences"}), manifest_dir=stream
        )
    assert ran == ["reconstruct_and_advance", "advance_again"]
    path = stream / manifest_file_name(missing.manifest_uuid)
    assert path.read_bytes() == canonical_bytes(body)
    stored = pg_one(
        migrated, "migrator", "SELECT file_sha256 FROM deletion_manifests WHERE id = %s", missing.id
    )
    assert stored == (hashlib.sha256(path.read_bytes()).digest(),)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert present_path.stat().st_mtime_ns == present_written
    assert names(stream) == sorted([path.name, present_path.name])
    # Step 1 wrote the file, then advanced past its bound.
    events = [
        e for e in logs if e["event"] in ("migrate_manifest_files_rewritten", "migrate_identities_advanced")
    ]
    assert [(e["event"], e["step"]) for e in events] == [
        ("migrate_manifest_files_rewritten", "reconstruct_and_advance"),
        ("migrate_identities_advanced", "reconstruct_and_advance"),
        ("migrate_identities_advanced", "advance_again"),
    ]
    assert events[0]["manifests"] == [str(missing.manifest_uuid)]
    assert events[1]["advanced"]["alerts"] == bound
    assert insert_alert(migrated) == bound + 1


async def test_a_scope_whose_hash_differs_stops_migrate(
    migrated: PgServer, migrator: AsyncEngine, make_config: ConfigFactory, stream: Path
) -> None:
    changed_bound = next_id(migrated, "alerts") + 500
    row, _ = await recorded(migrator, status="completed", max_ids={"alerts": 1})
    # The scope changed after its file was hashed and shipped: a bound the file never held.
    pg_rows(
        migrated,
        "migrator",
        "UPDATE deletion_manifests SET scope = jsonb_set(scope, '{max_ids,alerts}', to_jsonb(%s::bigint)) "
        "WHERE id = %s",
        changed_bound,
        row.id,
    )
    alerts = sequence_state(migrated, "alerts")
    with capture_logs() as logs, pytest.raises(ManifestConflict):
        await run_migrate_async(make_config("migrate"), now=NOW, manifest_dir=stream)
    (failed,) = [e for e in logs if e["event"] == "migrate_failed"]
    assert (failed["step"], failed["error_type"]) == ("reconstruct_and_advance", "ManifestConflict")
    assert [e["step"] for e in logs if e["event"] == "migrate_step"] == ["reconstruct_and_advance"]
    assert names(stream) == []  # no file written from it
    assert sequence_state(migrated, "alerts") == alerts  # and nothing advanced before the stop


@pytest.mark.parametrize("defect", ["unknown_format", "invalid", "name_mismatch"])
async def test_a_scope_that_is_no_manifest_of_its_row_stops_migrate_before_any_file(
    migrated: PgServer, migrator: AsyncEngine, make_config: ConfigFactory, stream: Path, defect: str
) -> None:
    async with migrator.begin() as conn:
        row = await insert_manifest(conn, kind="source_purge", request={}, now=NOW)
    scope: dict[str, Any] = dict(content(str(row.manifest_uuid), max_ids={"alerts": 1}))
    if defect == "unknown_format":
        scope["format"] = 2
    elif defect == "invalid":
        del scope["manifest_uuid"]  # no uuid at all
    else:
        scope["manifest_uuid"] = str(uuid7())  # another manifest's
    # Recorded with the hash of its own bytes, as no purge records it: only the parse can refuse it.
    pg_rows(
        migrated,
        "migrator",
        "UPDATE deletion_manifests SET scope = %s, file_sha256 = %s WHERE id = %s",
        Jsonb(scope),
        hashlib.sha256(canonical_bytes(cast(ManifestContent, scope))).digest(),
        row.id,
    )
    with capture_logs() as logs, pytest.raises(ManifestInvalid) as refused:
        await run_migrate_async(
            make_config("migrate"), now=NOW, steps=frozenset({"sequences"}), manifest_dir=stream
        )
    assert refused.value.reason == defect
    (failed,) = [e for e in logs if e["event"] == "migrate_failed"]
    assert (failed["step"], failed["error_type"]) == ("reconstruct_and_advance", "ManifestInvalid")
    assert names(stream) == []  # stopped before its file was written


async def test_an_executing_row_without_a_hash_has_no_bounds(
    migrated: PgServer, migrator: AsyncEngine, stream: Path
) -> None:
    alerts = sequence_state(migrated, "alerts")
    async with migrator.begin() as conn:
        row = await insert_manifest(conn, kind="source_purge", request={}, now=NOW)
    for scope in (
        None,
        content(str(row.manifest_uuid), max_ids={"alerts": next_id(migrated, "alerts") + 300}),
    ):
        if scope is not None:
            # Its content without the hash A3 records with it: nothing was deleted under it, so no bound yet.
            pg_rows(
                migrated,
                "migrator",
                "UPDATE deletion_manifests SET scope = %s WHERE id = %s",
                Jsonb(dict(scope)),
                row.id,
            )
        async with migrator.begin() as conn:
            assert await rewrite_missing_files(conn, stream) == []
            bounds = await manifest_bounds(conn, stream)
            assert bounds == {}
            assert "alerts" not in await advance_identities(conn, bounds)
    assert names(stream) == []
    assert sequence_state(migrated, "alerts") == alerts
