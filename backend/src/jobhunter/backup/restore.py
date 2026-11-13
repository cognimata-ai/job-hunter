"""The disaster restore (spec §19.5 "Restore order", §22 V22; plan M5 Tasks 25 and 26, R16, R23, Deviations
29, 46 and 55): `jobhunter restore run`, the `restore` service's command, and the pieces the monthly restore
test shares (`backup.restore_test`).

**`restore_list(listing)`** is the filtered list of `pg_restore -L`: the entries of a dump's `pg_restore
--list` output that a restore runs, every entry but those of what the bootstrap superuser created in the
database before the restore (`deploy/postgres/init/10-init.sh`, and the template a scratch database is created
from): the extensions (`EXTENSION`), their comments (`COMMENT - EXTENSION`) and the `public` schema (`SCHEMA -
public`, which PostgreSQL 17 dumps only when its definition differs from the default). The migrator and the
restore role own none of them, so `pg_restore --no-owner --no-acl --exit-on-error` would stop at the first.
Every other entry is kept, in its order and with its text, so the restore runs exactly the dump's statements.
A line of the listing is a comment (`;` first), blank, or an entry: `<dump id>; <catalog oid> <object oid>
<description> <schema> <name> <owner>`, where an object outside a schema has `-` for its schema. A line of
any other shape is no listing `pg_restore` wrote: it raises `ValueError`, and no list is built on it. It is a
pure function of the text, which the tests' `dump_and_restore` (`tests/support/restore.py`) and step 4 below
both run.

**`run_restore`** runs §19.5's order, each step logged `restore_step` with its name; a failure stops there
and raises `RestoreFailed(step, error)` (logged `restore_failed`; `jobhunter restore run` prints both and
exits 1):
- `refuse` (R16): the application database must hold no `alembic_version` and `cv_store` no entry, else
  `database_not_empty` or `cv_store_not_empty`: a restore never runs over data;
- `stream` (1): `restic snapshots --tag manifests`; a restic failure (an unreachable repository, a wrong
  password) is `restic_failed`. A snapshot whose listing counts no file (`SnapshotInfo.files == 0`: restic's
  exit 3 with no listed file readable, final fix B8) holds no manifest, so it can hide none: it contributes
  nothing, is never restored (its manifests folder may not exist, which would fail the restore) and is logged
  `manifest_snapshot_empty` with its id. A snapshot without a count (no summary) is restored as any other. No
  snapshot left is an empty stream (`manifest_stream_empty` logged, nothing restored).
  Otherwise the stream is the **union of every `manifests` snapshot** (final review C1, M2), never the newest
  alone: a snapshot restic saved on exit 3 (incomplete, named by no shipment) or an older list two
  unserialised shippers uploaded last (before the shipment lock, `shipper.SHIP_LOCK_KEY`) may lack a manifest
  an earlier snapshot holds; the weekly prune keeps the newest ten (`prune.MANIFESTS_KEPT`, which bounds this
  step's work). Oldest first, each snapshot's
  `cfg.manifests_dir` is restored into `<scratch>/snapshot` (made fresh) and its `<uuid>.json` files merged
  into `<scratch>/union`: a file name already there must hold the same bytes (sha256; a manifest file is
  written once), else `ManifestStreamConflict` (`manifest_stream_conflict`, logged with the file name and both
  snapshot ids) stops the restore before anything of the stream is restored: it never picks one. Then the
  union is copied into `cfg.manifests_dir`, and `<scratch>` is removed whatever the outcome. `<scratch>` is
  `<staging_dir>/restore/stream` here (the restore test's `<staging_dir>/restore_test/stream`). Every restic
  restore names its subfolder (`Restic.restore(snapshot=, subfolder=, target=)`, the backup container's mount,
  which is the restore container's too), so its files land at exact paths, which the step checks for each
  snapshot (no directory where the `<uuid>.json` files land) and for the union (at least one manifest), else
  `manifest_stream_not_restored`. The directory is created first, so an empty stream is an existing empty
  directory (Ruling T25-2 (9));
- `set` (2): the snapshot tagged `set:<id>` (`--set`), else the latest tagged `set` (`backup_set_not_found`
  without one); its `<staging_dir>/set` restored into `<staging_dir>/restore` (made fresh), where
  `jobhunter.dump` and `backup_set.json` must lie (`backup_set_not_restored`); the json must parse
  (`backup_set_json_invalid`), name the snapshot's set (`backup_set_id_mismatch`) and the dump's sha256
  (`dump_sha256_mismatch`). The set's `data_key_ids` are logged (`restore_set_staged`, hex ids only): the
  `restore` container holds no data key and decrypts nothing; after the restore, before `up -d`, the operator
  runs one `crypto rotate` pass per key id of the set other than the current key (Task 28). Step 3 (`up -d
  postgres` on an empty `pgdata`) is the operator's (RUNBOOK);
- `pg_restore` (4): `pg_restore --list`, filtered by `restore_list` into `<staging_dir>/restore/restore.list`,
  then `pg_restore --no-owner --no-acl --exit-on-error -L <list>` as the migrator, its password in the child's
  environment only (`pg_restore_failed` for either's non-zero exit);
- `verify_restored` (5): `privacy.restore_verify.verify_restored` in one transaction (`verify_failed`), and,
  with an empty stream (no `manifests` snapshot that may hold a file), a restored `deletion_manifests` row
  that is `shipped` stops it with `manifest_stream_missing` (a shipped manifest must be in the stream;
  `executing` and `completed` rows without files are §19.5's pending retries and rewrites); either rolls the
  transaction back;
- `cv` (6): the set's `cfg.cv_store_dir` restored into `cfg.cv_store_dir`, checked to hold no directory
  (`cv_not_restored`);
- `migrate` (7): `run_migrate_async(cfg)`, every step as the migrator on the volumes of the configuration
  (`migrate_failed`), then every CV file of the store that no `cv_files` row names deleted;
- `exports` (7b, Deviation 55, Task 17's restore column): a backup set holds no export archive (each is the
  user's content and lives 7 days), so every export still `ready` becomes `expired` and every one still
  `running` `failed` (its restored operation then finds it no longer `running` and ends), the rows a replayed
  manifest left `purged` as they are, and the exports volume, which the `restore` service mounts read-write,
  emptied: no row names a file in it any more. It runs after the replay, so an export a replayed manifest
  names ends `purged`, as its live purge left it, and the replay's own file removal acts on the volume first;
- `verify_replayed` (8): `privacy.restore_verify.verify_replayed` on the stream and the mounted `cv_store` and
  `exports` volumes (`verify_failed`).
Finally, whatever the outcome, `<staging_dir>/restore` is removed: its `jobhunter.dump` is a full copy of the
database that no later purge would reach. Step 9 (start `api` and `worker`) is the operator's. The paths are
`cfg.manifests_dir`, `cfg.staging_dir` and `cfg.cv_store_dir` (`/data/manifests`, `/data/staging`, `/data/cv`
in the image). `complete_restore` is steps 7, 7b and 8 alone, on a database a restore loaded. `run_restore`
returns the set restored with its data key ids (`RestoreResult`), which `restore run` prints.

**`restore_verify`** is `jobhunter restore verify --set <id> --phase restored|replayed [--database <db>]`
(review M4; `cli_backup` parses and prints): the `restored` phase is step 5's `check_restored` on a set an
operator staged in `<staging_dir>/restore` (the stream empty when the repository holds no `manifests`
snapshot that may hold a file, as step 1 reads it), the `replayed` phase step 8 on the restored stream, the
union.

Log lines carry steps, codes, ids and counts only: no row content, no path of a file's content, no secret.
"""

import asyncio
import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal, cast

import psycopg
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from jobhunter import crypto
from jobhunter.backup.backup_set import (
    DUMP_NAME,
    DUMP_TIMEOUT_S,
    INVENTORY_NAME,
    LIST_TIMEOUT_S,
    PG_RESTORE,
    SET_STAGING,
    SET_TAG,
)
from jobhunter.backup.restic import Restic, ResticError, SnapshotInfo
from jobhunter.backup.runner import CommandRunner
from jobhunter.backup.shipper import MANIFESTS_TAG
from jobhunter.config import AppConfig, DbRole, db_login
from jobhunter.db.sql import q
from jobhunter.db.transitions import transition_where
from jobhunter.observability.logging import get_logger
from jobhunter.privacy.manifests import FileRoots
from jobhunter.privacy.restore_verify import VerifyReport, verify_replayed, verify_restored
from jobhunter.profile.storage import CvStore, fsync_directory
from jobhunter.types import JSONValue

_ENTRY: Final = re.compile(r"\d+; \d+ \d+ (?P<what>\S.*)")
# The entries the bootstrap superuser owns, by their description, schema and name (module docstring).
_SUPERUSER_OWNED: Final = re.compile(r"(?:EXTENSION - \S+|COMMENT - EXTENSION \S+|SCHEMA - public)(?: .*)?")

RESTORE_STAGING: Final = "restore"
# Step 1's scratch directory inside the restore's staging (module docstring).
STREAM_SCRATCH: Final = "stream"
LIST_NAME: Final = "restore.list"
LOGGED_SNAPSHOTS: Final = 20
MANIFEST_STREAM_MISSING: Final = "manifest_stream_missing"
MANIFEST_STREAM_CONFLICT: Final = "manifest_stream_conflict"
VERIFY_FAILED: Final = "verify_failed"
RESTIC_FAILED: Final = "restic_failed"
_SET_TAG_PREFIX: Final = f"{SET_TAG}:"
# A data key id as `backup_set.json` lists it: `crypto.key_id`, in hex.
_KEY_ID: Final = re.compile(rf"[0-9a-f]{{{2 * crypto.KEY_ID_BYTES}}}")
_HASH_CHUNK: Final = 1024 * 1024

RestoreStep = Literal[
    "refuse",
    "stream",
    "set",
    "pg_restore",
    "verify_restored",
    "cv",
    "migrate",
    "exports",
    "verify_replayed",
    # the restore test's own (`backup.restore_test`)
    "scratch",
]

_HAS_REVISION: Final = "SELECT to_regclass('public.alembic_version') IS NOT NULL"
_SHIPPED: Final = q("SELECT count(*) FROM deletion_manifests WHERE status = 'shipped'")
_NAMED_FILES: Final = q("SELECT storage_path FROM cv_files WHERE storage_path = ANY(CAST(:names AS text[]))")

log = get_logger("jobhunter.backup.restore")


def restore_list(listing: str) -> list[str]:
    """The entries of `listing` (a dump's `pg_restore --list` output) to restore, in their order (module
    docstring); `ValueError` for a line that is neither a comment nor an entry."""
    entries: list[str] = []
    for line in listing.splitlines():
        if not line.strip() or line.startswith(";"):
            continue
        entry = _ENTRY.fullmatch(line)
        if entry is None:
            raise ValueError("a line of the listing is no entry of pg_restore --list")
        if not _SUPERUSER_OWNED.fullmatch(entry["what"]):
            entries.append(line)
    return entries


class RestoreFailed(Exception):
    """The restore stopped at `step` with the code `error` (module docstring); no content, no output."""

    def __init__(self, step: RestoreStep, error: str) -> None:
        super().__init__(f"{step}: {error}")
        self.step = step
        self.error = error


class ManifestStreamConflict(RestoreFailed):
    """Two `manifests` snapshots hold the file `file_name` with different bytes (module docstring, step 1):
    the restore stops at its stream step and never picks one. `snapshot_ids` are the first snapshot that held
    it and the one that differs."""

    def __init__(self, file_name: str, snapshot_ids: tuple[str, str]) -> None:
        super().__init__("stream", MANIFEST_STREAM_CONFLICT)
        self.args = (
            f"stream: {MANIFEST_STREAM_CONFLICT}: {file_name} in snapshots {', '.join(snapshot_ids)}",
        )
        self.file_name = file_name
        self.snapshot_ids = snapshot_ids


@contextmanager
def restore_step(step: RestoreStep) -> Iterator[None]:
    """A step of a restore (module docstring): logged by name; a failure inside is `RestoreFailed(step, code)`
    (`restic_failed` for restic, the error's class for anything unforeseen), one from a step within passes
    unchanged."""
    log.info("restore_step", step=step)
    try:
        yield
    except RestoreFailed:
        raise
    except ResticError as exc:
        raise RestoreFailed(step, RESTIC_FAILED) from exc
    except Exception as exc:  # the step's failure, by class (no message: it may carry a path or a value)
        raise RestoreFailed(step, type(exc).__name__) from exc


def null_engine(cfg: AppConfig, *, db_role: DbRole | None = None, database: str | None = None) -> AsyncEngine:
    """An engine without a pool on `database` as `db_role` (default: the configuration role's login), its
    parameters kept out of its errors."""
    dsn = cfg.dsn("sqlalchemy", db_role=db_role, database=database)
    return create_async_engine(dsn, poolclass=NullPool, hide_parameters=True)


# ---- the files: stream, set and CV store ------------------------------------------------------------------


def _entries(directory: Path) -> list[os.DirEntry[str]]:
    with os.scandir(directory) as found:
        return list(found)


def _holds_a_directory(directory: Path) -> bool:
    return any(entry.is_dir(follow_symlinks=False) for entry in _entries(directory))


async def latest_snapshot(restic: Restic, tag: str) -> SnapshotInfo | None:
    """The newest snapshot tagged `tag` (`Restic.snapshots` sorts them by time), `None` when there is none."""
    found = await restic.snapshots(tags=[tag])
    return found[-1] if found else None


@dataclass(frozen=True)
class RestoredStream:
    """Step 1's stream (module docstring): the `manifests` snapshots it is the union of, oldest first, and the
    number of manifest files the union holds."""

    snapshot_ids: tuple[str, ...]
    manifests: int


@dataclass(frozen=True)
class _Merged:
    """A file of the union: its bytes' sha256 and the first snapshot that held it."""

    sha256: bytes
    snapshot_id: str


def _merge(landing: Path, union: Path, snapshot_id: str, merged: dict[str, _Merged]) -> None:
    """One snapshot's files, restored into `landing`, merged into `union` (module docstring, step 1)."""
    if _holds_a_directory(landing):
        raise RestoreFailed("stream", "manifest_stream_not_restored")
    for entry in _entries(landing):
        if not (entry.name.endswith(".json") and entry.is_file(follow_symlinks=False)):
            continue
        data = Path(entry.path).read_bytes()
        digest = hashlib.sha256(data).digest()
        known = merged.get(entry.name)
        if known is None:
            shutil.copy2(entry.path, union / entry.name)
            merged[entry.name] = _Merged(digest, snapshot_id)
        elif known.sha256 != digest:
            log.error(
                MANIFEST_STREAM_CONFLICT, file=entry.name, snapshot_ids=[known.snapshot_id, snapshot_id]
            )
            raise ManifestStreamConflict(entry.name, (known.snapshot_id, snapshot_id))


def _copy_union(union: Path, target: Path) -> None:
    for entry in _entries(union):
        shutil.copy2(entry.path, target / entry.name)
    fsync_directory(target)


def holds_files(snapshot: SnapshotInfo) -> bool:
    """Whether `snapshot` may hold a file: all but one whose listing counts none (`SnapshotInfo.files == 0`,
    module docstring, step 1); one without a count may."""
    return snapshot.files != 0


async def stream_snapshots(restic: Restic) -> list[SnapshotInfo]:
    """The `manifests` snapshots that may hold a manifest, oldest first; each of the others, which holds no
    file, logged `manifest_snapshot_empty` with its id (module docstring, step 1)."""
    snapshots = []
    for snapshot in await restic.snapshots(tags=[MANIFESTS_TAG]):  # oldest first
        if holds_files(snapshot):
            snapshots.append(snapshot)
        else:
            log.info("manifest_snapshot_empty", snapshot_id=snapshot.id)
    return snapshots


async def restore_stream(
    restic: Restic, *, subfolder: Path, target: Path, scratch: Path
) -> RestoredStream | None:
    """Step 1 (module docstring): the union of every `manifests` snapshot's `subfolder` restored into
    `target`, which is created first, through `scratch` (removed whatever the outcome); the stream, or `None`
    for an empty one. `ManifestStreamConflict` for a file name two snapshots hold with different bytes,
    `RestoreFailed('stream', 'manifest_stream_not_restored')` when the files are not where they must be."""
    snapshots = await stream_snapshots(restic)
    await asyncio.to_thread(target.mkdir, mode=0o700, parents=True, exist_ok=True)
    if not snapshots:
        log.info("manifest_stream_empty")
        return None
    union, landing = scratch / "union", scratch / "snapshot"
    merged: dict[str, _Merged] = {}
    try:
        await asyncio.to_thread(_fresh, scratch)
        await asyncio.to_thread(union.mkdir, mode=0o700)
        for snapshot in snapshots:
            await asyncio.to_thread(_fresh, landing)
            await restic.restore(snapshot=snapshot.id, subfolder=str(subfolder), target=landing)
            await asyncio.to_thread(_merge, landing, union, snapshot.id, merged)
        if not merged:
            raise RestoreFailed("stream", "manifest_stream_not_restored")
        await asyncio.to_thread(_copy_union, union, target)
    finally:
        await asyncio.to_thread(shutil.rmtree, scratch, ignore_errors=True)
    ids = tuple(snapshot.id for snapshot in snapshots)
    log.info(
        "manifest_stream_restored",
        snapshots=len(ids),
        snapshot_ids=list(ids[-LOGGED_SNAPSHOTS:]),
        manifests=len(merged),
    )
    return RestoredStream(ids, len(merged))


def _fresh(directory: Path) -> None:
    """`directory` empty (whatever a dead run left there removed), mode 0700."""
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(mode=0o700, parents=True)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as dump:
        while chunk := dump.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _tagged_set(snapshot: SnapshotInfo) -> int | None:
    for tag in snapshot.tags:
        if tag.startswith(_SET_TAG_PREFIX) and tag[len(_SET_TAG_PREFIX) :].isdigit():
            return int(tag[len(_SET_TAG_PREFIX) :])
    return None


@dataclass(frozen=True)
class StagedSet:
    """A backup set restored into staging: its snapshot, its directory and its `backup_set.json`."""

    snapshot: SnapshotInfo
    directory: Path
    inventory: Mapping[str, JSONValue]

    @property
    def set_id(self) -> int:
        return cast(int, self.inventory["set_id"])

    @property
    def dump(self) -> Path:
        return self.directory / DUMP_NAME

    @property
    def data_key_ids(self) -> tuple[str, ...]:
        """The hex ids of the data keys the set's ciphertexts name (checked when it was staged)."""
        return tuple(cast(list[str], self.inventory["data_key_ids"]))


@dataclass(frozen=True)
class RestoreResult:
    """What `restore run` restored: the set, its snapshot, and the hex ids of the data keys its ciphertexts
    name, for the operator's `crypto rotate` passes (one per key id other than the current key, Task 28)."""

    set_id: int
    snapshot_id: str
    data_key_ids: tuple[str, ...]


def _read_inventory(staged: Path, snapshot: SnapshotInfo) -> Mapping[str, JSONValue]:
    """`backup_set.json` beside the dump, checked against the dump and the snapshot's tag (module
    docstring)."""
    dump, inventory_file = staged / DUMP_NAME, staged / INVENTORY_NAME
    if not (dump.is_file() and inventory_file.is_file()) or _holds_a_directory(staged):
        raise RestoreFailed("set", "backup_set_not_restored")
    try:
        inventory = json.loads(inventory_file.read_text(encoding="utf-8"))
    except ValueError:
        raise RestoreFailed("set", "backup_set_json_invalid") from None
    set_id = inventory.get("set_id") if isinstance(inventory, dict) else None
    if not isinstance(set_id, int) or not isinstance(inventory.get("dump_sha256"), str):
        raise RestoreFailed("set", "backup_set_json_invalid")
    keys = inventory.get("data_key_ids")
    if not isinstance(keys, list) or not all(isinstance(k, str) and _KEY_ID.fullmatch(k) for k in keys):
        raise RestoreFailed("set", "backup_set_json_invalid")  # hex key ids only: what `restore run` prints
    if _tagged_set(snapshot) != set_id:
        raise RestoreFailed("set", "backup_set_id_mismatch")
    if _sha256_of(dump) != inventory["dump_sha256"]:
        raise RestoreFailed("set", "dump_sha256_mismatch")
    return cast(dict[str, JSONValue], inventory)


async def restore_set(restic: Restic, *, set_id: int | None, subfolder: Path, target: Path) -> StagedSet:
    """Step 2 (module docstring): the set's snapshot (`set:<set_id>`, else the latest `set`), its `subfolder`
    restored into `target` (made fresh), and its `backup_set.json` checked."""
    latest = await _set_snapshot(restic, set_id, step="set")
    await asyncio.to_thread(_fresh, target)
    await restic.restore(snapshot=latest.id, subfolder=str(subfolder), target=target)
    inventory = await asyncio.to_thread(_read_inventory, target, latest)
    staged = StagedSet(latest, target, inventory)
    log.info(
        "restore_set_staged",
        set_id=staged.set_id,
        snapshot_id=latest.id,
        data_key_ids=list(staged.data_key_ids),
    )
    return staged


async def _set_snapshot(restic: Restic, set_id: int | None, *, step: RestoreStep) -> SnapshotInfo:
    """The snapshot of the set `set_id` (`set:<id>`), else the latest `set`; `backup_set_not_found`."""
    latest = await latest_snapshot(restic, SET_TAG if set_id is None else f"{_SET_TAG_PREFIX}{set_id}")
    if latest is None:
        raise RestoreFailed(step, "backup_set_not_found")
    return latest


async def restore_cv_files(restic: Restic, staged: StagedSet, *, subfolder: Path, target: Path) -> int:
    """Step 6 (module docstring): the set's `subfolder` restored into `target`; the store's files there."""
    await restic.restore(snapshot=staged.snapshot.id, subfolder=str(subfolder), target=target)
    if _holds_a_directory(target):
        raise RestoreFailed("cv", "cv_not_restored")
    files = len(CvStore(target).stored())
    log.info("restore_cv_files_restored", snapshot_id=staged.snapshot.id, files=files)
    return files


# ---- the database: pg_restore and the checks ---------------------------------------------------------------


class CommandFailed(Exception):
    """`pg_restore` exited non-zero; its output is never kept."""


async def load_dump(
    cfg: AppConfig, runner: CommandRunner, staged: StagedSet, *, db_role: DbRole, database: str | None = None
) -> int:
    """Step 4 (module docstring): the staged dump into `database` (default `JOBHUNTER_DB_NAME`) as `db_role`,
    through the filtered list; the entries restored. `RestoreFailed('pg_restore', 'pg_restore_failed')` for
    either command's non-zero exit."""
    listed = await runner.run([PG_RESTORE, "--list", str(staged.dump)], env={}, timeout_s=LIST_TIMEOUT_S)
    if listed.returncode != 0:
        raise RestoreFailed("pg_restore", "pg_restore_failed")
    entries = restore_list(listed.stdout.decode("utf-8"))
    listing = staged.directory / LIST_NAME
    listing.write_text("\n".join(entries) + "\n", encoding="utf-8")
    user, secret = db_login(db_role)
    argv = [
        PG_RESTORE,
        "--no-owner",
        "--no-acl",
        "--exit-on-error",
        "-L",
        str(listing),
        "--no-password",
        "--host",
        cfg.db.host,
        "--port",
        str(cfg.db.port),
        "--username",
        user,
        "--dbname",
        cfg.db.name if database is None else database,
        str(staged.dump),
    ]
    env = {"PGPASSWORD": cfg.secret(secret).decode("utf-8")}
    restored = await runner.run(argv, env=env, timeout_s=DUMP_TIMEOUT_S)
    if restored.returncode != 0:
        raise RestoreFailed("pg_restore", "pg_restore_failed")
    log.info("restore_dump_loaded", set_id=staged.set_id, entries=len(entries))
    return len(entries)


async def check_restored(engine: AsyncEngine, staged: StagedSet, *, empty_stream: bool) -> VerifyReport:
    """Step 5 (module docstring): `verify_restored` and the empty stream's rule (`empty_stream`: no
    `manifests` snapshot that may hold a file), in one transaction that commits only a report that holds."""
    async with engine.connect() as conn:
        async with conn.begin() as transaction:
            report = await verify_restored(conn, staged.inventory, snapshot_id=staged.snapshot.id)
            if report.ok and empty_stream and (await conn.execute(_SHIPPED)).scalar_one():
                report = VerifyReport(phase="restored", ok=False, failures=[MANIFEST_STREAM_MISSING])
                log.error(MANIFEST_STREAM_MISSING, set_id=staged.set_id)
            if not report.ok:
                await transaction.rollback()
    return report


def restored_failure(report: VerifyReport) -> RestoreFailed:
    """The `verify_restored` step's failure for a report that does not hold."""
    code = MANIFEST_STREAM_MISSING if report.failures == [MANIFEST_STREAM_MISSING] else VERIFY_FAILED
    return RestoreFailed("verify_restored", code)


async def _refuse_unless_empty(cfg: AppConfig) -> None:
    """R16 (module docstring)."""
    async with await psycopg.AsyncConnection.connect(cfg.dsn("psycopg")) as conn:
        row = await (await conn.execute(_HAS_REVISION)).fetchone()
    if row is not None and row[0]:
        raise RestoreFailed("refuse", "database_not_empty")
    if _entries(cfg.cv_store_dir):
        raise RestoreFailed("refuse", "cv_store_not_empty")


# ---- steps 7, 7b and 8 -------------------------------------------------------------------------------------


async def remove_unreferenced_cv_files(engine: AsyncEngine, cv_store_dir: Path) -> int:
    """The CV files of the store that no `cv_files` row names, deleted (step 7's end); how many."""
    store = CvStore(cv_store_dir)
    names = [stored.storage_path for stored in store.stored()]
    if not names:
        return 0
    async with engine.connect() as conn:
        named = {str(name) for name in (await conn.execute(_NAMED_FILES, {"names": names})).scalars()}
    orphans = [name for name in names if name not in named]
    for name in orphans:
        store.delete(name)
    if orphans:
        fsync_directory(cv_store_dir)
    return len(orphans)


def _empty(directory: Path) -> int:
    """Every entry of `directory` removed (a directory with what it holds, a link itself); how many."""
    removed = 0
    for entry in _entries(directory):
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path)
        else:
            Path(entry.path).unlink(missing_ok=True)
        removed += 1
    if removed:
        fsync_directory(directory)
    return removed


@dataclass(frozen=True)
class ExportsEnded:
    """Step 7b's counts: the exports it expired and failed, and the entries it removed from the volume."""

    expired: int
    failed: int
    files_removed: int


async def end_restored_exports(engine: AsyncEngine, exports_dir: Path) -> ExportsEnded:
    """Step 7b (module docstring) on the database of `engine` and the exports volume `exports_dir`: `ready` →
    `expired`, `running` → `failed`, in one transaction, then the volume emptied."""
    async with engine.begin() as conn:
        expired = await transition_where(
            conn, "exports", ["ready"], "expired", where="true", params={}, set_={"storage_path": None}
        )
        failed = await transition_where(conn, "exports", ["running"], "failed", where="true", params={})
    ended = ExportsEnded(len(expired), len(failed), _empty(exports_dir))
    log.info(
        "restore_exports_ended",
        expired=sorted(int(r["id"]) for r in expired),
        failed=sorted(int(r["id"]) for r in failed),
        files_removed=ended.files_removed,
    )
    return ended


async def complete_restore(cfg: AppConfig, *, now: datetime) -> VerifyReport:
    """Steps 7, 7b and 8 (module docstring) on the database and the volumes of `cfg`, as the migrator; the
    `replayed` report, which holds (`RestoreFailed` otherwise)."""
    # Imported here: `migrate` loads procrastinate, langgraph and alembic, which only these steps need.
    from jobhunter.db.migrate import run_migrate_async

    engine = null_engine(cfg)
    try:
        with restore_step("migrate"):
            try:
                await run_migrate_async(cfg, now=now)
            except Exception as exc:
                raise RestoreFailed("migrate", "migrate_failed") from exc
            removed = await remove_unreferenced_cv_files(engine, cfg.cv_store_dir)
            log.info("restore_cv_files_unreferenced_removed", files=removed)
        with restore_step("exports"):
            await end_restored_exports(engine, cfg.exports_dir)
        with restore_step("verify_replayed"):
            async with engine.connect() as conn:
                files = FileRoots(cv_store=cfg.cv_store_dir, exports=cfg.exports_dir)
                report = await verify_replayed(conn, cfg.manifests_dir, files=files)
            if not report.ok:
                raise RestoreFailed("verify_replayed", VERIFY_FAILED)
        return report
    finally:
        await engine.dispose()


# ---- the restore -------------------------------------------------------------------------------------------


async def _restore(
    cfg: AppConfig, restic: Restic, runner: CommandRunner, *, set_id: int | None, now: datetime, staging: Path
) -> RestoreResult:
    with restore_step("refuse"):
        await _refuse_unless_empty(cfg)
    with restore_step("stream"):
        stream = await restore_stream(
            restic, subfolder=cfg.manifests_dir, target=cfg.manifests_dir, scratch=staging / STREAM_SCRATCH
        )
    with restore_step("set"):
        staged = await restore_set(
            restic, set_id=set_id, subfolder=cfg.staging_dir / SET_STAGING, target=staging
        )
    with restore_step("pg_restore"):
        await load_dump(cfg, runner, staged, db_role="migrator")
    engine = null_engine(cfg)
    try:
        with restore_step("verify_restored"):
            report = await check_restored(engine, staged, empty_stream=stream is None)
            if not report.ok:
                raise restored_failure(report)
    finally:
        await engine.dispose()
    with restore_step("cv"):
        await restore_cv_files(restic, staged, subfolder=cfg.cv_store_dir, target=cfg.cv_store_dir)
    await complete_restore(cfg, now=now)
    return RestoreResult(staged.set_id, staged.snapshot.id, staged.data_key_ids)


async def run_restore(
    cfg: AppConfig,
    restic: Restic,
    runner: CommandRunner,
    *,
    set_id: int | None,
    clock: Callable[[], datetime],
) -> RestoreResult:
    """Restore the backup set `set_id` (the latest without one) and the manifest stream (the union of every
    `manifests` snapshot) into the empty application database and volumes of `cfg`, as §19.5 orders it (module
    docstring); the set restored and its data key ids (review M3: `restore run` prints them). `RestoreFailed`
    names the step that stopped it; `<staging_dir>/restore` is removed whatever the outcome."""
    staging = cfg.staging_dir / RESTORE_STAGING
    log.info("restore_started", set_id=set_id)
    try:
        result = await _restore(cfg, restic, runner, set_id=set_id, now=clock(), staging=staging)
    except RestoreFailed as failed:
        log.error("restore_failed", step=failed.step, error=failed.error)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    log.info("restore_completed", set_id=result.set_id)
    return result


# ---- `jobhunter restore verify` ----------------------------------------------------------------------------


async def _staged_by_hand(restic: Restic, *, set_id: int, directory: Path) -> StagedSet:
    """The set `set_id` as an operator staged it in `directory` (`restore run`'s own staging is gone once it
    ends), checked as step 2 checks it; `backup_set_not_staged` without its `backup_set.json`."""
    if not await asyncio.to_thread((directory / INVENTORY_NAME).is_file):
        raise RestoreFailed("verify_restored", "backup_set_not_staged")
    snapshot = await _set_snapshot(restic, set_id, step="verify_restored")
    return StagedSet(snapshot, directory, await asyncio.to_thread(_read_inventory, directory, snapshot))


async def restore_verify(
    cfg: AppConfig,
    restic: Restic | None,
    *,
    set_id: int,
    phase: Literal["restored", "replayed"],
    database: str | None = None,
) -> VerifyReport:
    """`jobhunter restore verify` (review M4) on `database` (default `JOBHUNTER_DB_NAME`) as the
    migrator. `restored`: the set `set_id` staged in `<staging_dir>/restore`, checked by step 5 itself
    (`check_restored`: it commits the closing of the backup rows only when the check holds, with the
    empty-stream rule when the repository holds no `manifests` snapshot). `replayed`: step 8 on the stream
    and the mounted volumes of `cfg`. The report; `RestoreFailed` for a phase that cannot run
    (`backup_set_not_staged`, `backup_set_not_found`, `not_configured` without restic, `restic_failed`, a
    staged set's own checks)."""
    engine = null_engine(cfg, database=database)
    try:
        if phase == "replayed":
            with restore_step("verify_replayed"):
                async with engine.connect() as conn:
                    files = FileRoots(cv_store=cfg.cv_store_dir, exports=cfg.exports_dir)
                    return await verify_replayed(conn, cfg.manifests_dir, files=files)
        with restore_step("verify_restored"):
            if restic is None:
                raise RestoreFailed("verify_restored", "not_configured")
            staged = await _staged_by_hand(restic, set_id=set_id, directory=cfg.staging_dir / RESTORE_STAGING)
            empty_stream = not await stream_snapshots(restic)
            return await check_restored(engine, staged, empty_stream=empty_stream)
    finally:
        await engine.dispose()
