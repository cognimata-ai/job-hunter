"""`restore verify`: the two checks of a restored database (spec §19.5 "Restore order" steps 5 and 8, §22 V22;
plan M5 Task 26, R23, Deviation 46). They run in the `restore` container (`jobhunter restore run`, `jobhunter
restore verify`) and, for the monthly restore test, in `backup` (`backup.restore_test`), never in `migrate`,
which mounts neither the staging volume nor restic.

**`verify_restored`** (phase `restored`, step 5: after `pg_restore`, before `migrate`, since the replay
deletes rows), on the caller's connection and transaction, against the set's `backup_set.json`
(`BackupSetInventory`):
- the Alembic revision is the set's;
- every table of the application schema (`backup_set.APPLICATION_TABLES`, the tables the set counted) holds
  the set's `row_counts`, no table missing and none added;
- `deletion_manifests` holds `manifest_count` rows;
- for `RAW_RESPONSE_SAMPLE` (100) random raw responses that still have a body, the sha256 of the decompressed
  body is the row's `body_sha256`.
Only when every check holds does it close the backup rows the dump captured mid-run, in that transaction: the
set's own `backup_sets` row, dumped `running`, becomes `succeeded` from `backup_set.json` and the restic
snapshot id (a row already `succeeded` with that snapshot is a verification run again, and holds), and any
other `running` set or run becomes `failed` with error `restored_mid_backup`. A set row in any other state is
a failure. A failing report writes nothing.

**`verify_replayed`** (phase `replayed`, step 8: after `migrate`), read-only: every manifest of the stream and
every `deletion_manifests` row is `satisfied` by §19.5's table (`replay.classify_stream` with the volumes
`files`): `completed` or `shipped`, every postcondition holding (its file postconditions checked in `files`:
the mounted `cv_store` and `exports` volumes for `restore verify`, the scratch directories for the restore
test) and no fence of it active; so a former pending retry passes only once `migrate` completed it and wrote
its file. Then no purge fence is active at all, and every identity's next value lies above every bound of the
stream and of the recorded manifests (`identities.manifest_bounds`, the bounds the advancement used).

A report's failures name a check with ids, table names and counts only, never content. Each report is logged
(`restore_verified`).
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

import zstandard
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.backup.backup_set import APPLICATION_TABLES
from jobhunter.db.sql import q
from jobhunter.db.transitions import transition, transition_where
from jobhunter.observability.logging import get_logger
from jobhunter.privacy.identities import manifest_bounds, next_identity_values
from jobhunter.privacy.manifests import FileRoots, ManifestConflict, ManifestInvalid
from jobhunter.sources.http import MAX_BODY_BYTES
from jobhunter.types import JSONValue

Phase = Literal["restored", "replayed"]

RAW_RESPONSE_SAMPLE: Final = 100
RESTORED_MID_BACKUP: Final = "restored_mid_backup"
LOGGED_FAILURES: Final = 20

_REVISION: Final = q("SELECT version_num FROM alembic_version")
_HAS_REVISION: Final = q("SELECT to_regclass('public.alembic_version') IS NOT NULL")
_TABLES: Final = q(APPLICATION_TABLES)
_BODIES: Final = q(
    "SELECT id, body_zstd, body_sha256 FROM raw_responses WHERE body_zstd IS NOT NULL AND purged_at IS NULL "
    "ORDER BY random() LIMIT :n"
)
_SET_ROW: Final = q("SELECT status, restic_snapshot_id FROM backup_sets WHERE id = :id")
_ACTIVE_FENCES: Final = q("SELECT id FROM purge_fences WHERE released_at IS NULL ORDER BY id")

log = get_logger("jobhunter.privacy.restore_verify")


@dataclass(frozen=True)
class VerifyReport:
    """One phase's outcome: `ok` when `failures` is empty."""

    phase: Phase
    ok: bool
    failures: list[str] = field(default_factory=list)


def _report(phase: Phase, failures: list[str]) -> VerifyReport:
    report = VerifyReport(phase=phase, ok=not failures, failures=failures)
    log.info(
        "restore_verified",
        phase=phase,
        ok=report.ok,
        failures=failures[:LOGGED_FAILURES],
        count=len(failures),
    )
    return report


@dataclass(frozen=True)
class _Inventory:
    """The fields of `backup_set.json` the checks and the set's row read."""

    set_id: int
    alembic_revision: str
    manifest_count: int
    row_counts: dict[str, int]
    dump_sha256: bytes
    db_snapshot_at: datetime
    cv_file_count: int


def _int(value: JSONValue) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("not an integer")
    return value


def _inventory(backup_set: Mapping[str, JSONValue]) -> _Inventory:
    """The inventory's fields; `ValueError` (or `KeyError`, `TypeError`) for one of another shape."""
    counts = backup_set["row_counts"]
    revision, sha, at = (
        backup_set["alembic_revision"],
        backup_set["dump_sha256"],
        backup_set["db_snapshot_at"],
    )
    if not isinstance(counts, dict) or not all(isinstance(t, str) for t in counts):
        raise ValueError("row_counts")
    if not (isinstance(revision, str) and isinstance(sha, str) and isinstance(at, str)):
        raise ValueError("a text field")
    return _Inventory(
        set_id=_int(backup_set["set_id"]),
        alembic_revision=revision,
        manifest_count=_int(backup_set["manifest_count"]),
        row_counts={table: _int(n) for table, n in counts.items()},
        dump_sha256=bytes.fromhex(sha),
        db_snapshot_at=datetime.fromisoformat(at),
        cv_file_count=_int(backup_set["cv_file_count"]),
    )


async def _row_counts(conn: AsyncConnection) -> dict[str, int]:
    quote = conn.dialect.identifier_preparer.quote_identifier
    counts: dict[str, int] = {}
    for table in [str(name) for name in (await conn.execute(_TABLES)).scalars()]:
        # A catalogue name, quoted.
        counted = await conn.execute(q(f"SELECT count(*) FROM public.{quote(table)}"))  # noqa: S608
        counts[table] = int(counted.scalar_one())
    return counts


def _body_holds(body_zstd: bytes, body_sha256: bytes) -> bool:
    try:
        body = zstandard.ZstdDecompressor().decompress(body_zstd, max_output_size=MAX_BODY_BYTES)
    except zstandard.ZstdError:
        return False
    return hashlib.sha256(body).digest() == body_sha256


async def _content_failures(conn: AsyncConnection, inventory: _Inventory) -> list[str]:
    """The revision, the row counts, the manifests and the sampled bodies (module docstring)."""
    failures: list[str] = []
    revision = (
        (await conn.execute(_REVISION)).scalar() if (await conn.execute(_HAS_REVISION)).scalar() else None
    )
    if revision != inventory.alembic_revision:
        failures.append("alembic_revision")
    counts = await _row_counts(conn)
    for table in sorted(counts.keys() | inventory.row_counts.keys()):
        found, expected = counts.get(table), inventory.row_counts.get(table)
        if found != expected:
            failures.append(f"row_count:{table}:{found}:{expected}")
    if counts.get("deletion_manifests") != inventory.manifest_count:
        failures.append(f"manifest_count:{counts.get('deletion_manifests')}:{inventory.manifest_count}")
    sampled = (await conn.execute(_BODIES, {"n": RAW_RESPONSE_SAMPLE})).all()
    failures += [
        f"raw_response:{row.id}"
        for row in sampled
        if not _body_holds(bytes(row.body_zstd), bytes(row.body_sha256))
    ]
    return failures


async def _close_backup_rows(conn: AsyncConnection, inventory: _Inventory, snapshot_id: str) -> list[str]:
    """The set's own row `succeeded` from the inventory, every other `running` set and run `failed` (module
    docstring); the failure of a set row in no state to be closed."""
    row = (await conn.execute(_SET_ROW, {"id": inventory.set_id})).one_or_none()
    if row is None:
        return ["backup_set_row:absent"]
    if row.status == "succeeded" and row.restic_snapshot_id == snapshot_id:
        pass  # closed by an earlier verification of this restore
    elif row.status != "running":
        return [f"backup_set_row:{row.status}"]
    else:
        await transition(
            conn,
            "backup_sets",
            inventory.set_id,
            ["running"],
            "succeeded",
            set_={
                "restic_snapshot_id": snapshot_id,
                "dump_sha256": inventory.dump_sha256,
                "db_snapshot_at": inventory.db_snapshot_at,
                "manifest_count": inventory.manifest_count,
                "row_counts": inventory.row_counts,
                "cv_file_count": inventory.cv_file_count,
            },
        )
    sets = await transition_where(
        conn,
        "backup_sets",
        ["running"],
        "failed",
        where="t.id <> :own",
        params={"own": inventory.set_id},
        reason=RESTORED_MID_BACKUP,
    )
    runs = await transition_where(
        conn, "backup_runs", ["running"], "failed", where="true", params={}, reason=RESTORED_MID_BACKUP
    )
    log.info(
        "restore_backup_rows_closed",
        set_id=inventory.set_id,
        failed_sets=sorted(int(r["id"]) for r in sets),
        failed_runs=sorted(int(r["id"]) for r in runs),
    )
    return []


async def verify_restored(
    conn: AsyncConnection, backup_set: Mapping[str, JSONValue], *, snapshot_id: str
) -> VerifyReport:
    """Phase `restored` on the caller's transaction (module docstring): the restored database against the
    set's `backup_set.json`, then, when it holds, the backup rows it captured mid-run closed."""
    try:
        inventory = _inventory(backup_set)
    except (KeyError, TypeError, ValueError):
        return _report("restored", ["backup_set_json"])
    failures = await _content_failures(conn, inventory)
    if not failures:
        failures = await _close_backup_rows(conn, inventory, snapshot_id)
    return _report("restored", failures)


async def verify_replayed(conn: AsyncConnection, manifest_dir: Path, *, files: FileRoots) -> VerifyReport:
    """Phase `replayed` (module docstring): every manifest satisfied on the volumes `files`, no purge fence
    active, every identity above every bound."""
    # Imported here: the replay loads the purge kinds and, through them, the alerts, which the backup package
    # never imports when it loads (O1, `tests/unit/test_backup_roles.py`); only this phase reads them.
    from jobhunter.privacy.replay import classify_stream

    failures: list[str] = []
    try:
        classified = await classify_stream(conn, manifest_dir, files=files)
        bounds = await manifest_bounds(conn, manifest_dir)
    except (ManifestInvalid, ManifestConflict, FileNotFoundError) as exc:
        return _report("replayed", [f"stream:{type(exc).__name__}"])
    failures += [f"manifest:{c.manifest_uuid}:{c.outcome}" for c in classified if c.outcome != "satisfied"]
    if fences := [int(fence) for fence in (await conn.execute(_ACTIVE_FENCES)).scalars()]:
        failures.append(f"purge_fences_active:{fences[:LOGGED_FAILURES]}")
    for table, next_value in sorted((await next_identity_values(conn)).items()):
        if table in bounds and next_value <= bounds[table]:
            failures.append(f"identity:{table}:{next_value}:{bounds[table]}")
    return _report("replayed", failures)
