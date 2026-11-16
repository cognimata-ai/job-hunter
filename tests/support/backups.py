"""Backup sets, prune and restore-test runs, manifest shipments and manifests for the tests (M5 Task 7).

Each row is written in the state a test needs, with the times it gives, and satisfies migration 0010's CHECKs
as the backup role and a purge leave it: a `succeeded` set has its snapshot, dump hash, snapshot time and row
counts; a `completed` manifest its file hash and completion time; a `shipped` one its shipment, whose items
name the manifest's uuid and file hash (§19.5). Every value is a stand-in: no snapshot id or hash is real.
Each helper commits its rows and returns the id.

`FakeRestic` (M5 Task 10, shared since Task 12) is restic for the shipper in process: its `backup` records the
file list it is given and returns a stand-in snapshot id, and nothing else ever runs a command.

`FakeRepository` (M5 Task 26) is a whole restic repository in a directory of the test: `backup` copies the
directories and the listed files it is given into a new snapshot, each under its absolute path, with its tags;
`snapshots` lists them, oldest first, with their tags and paths; `restore(snapshot=, subfolder=, target=)`
copies the content of the snapshot's `subfolder` into `target`, as `restic restore <id>:<subfolder> --target`
does (a subfolder the snapshot lacks fails as restic does). `failing` names the calls that raise
`ResticError` instead (e.g. `snapshots` for an unreachable repository); `restores` records each restore.
`whole_paths` names subfolders whose restore lands under `target` at their whole path, as a restore naming no
subfolder would (`restic restore <id> --target`): the files one level deeper, or more, than the step expects.
`stored(snapshot, path)` is where a snapshot holds a backed-up file, for a test to alter it. `unreadable`
names listed files restic cannot read (final review C1): as restic 0.19.1 does on exit 3, `backup` still saves
the snapshot of the files it could read, with the tags given and its file count (`SnapshotInfo.files`, 0 when
none was readable: then the snapshot has no manifests folder at all), then raises `ResticError("backup",
"incomplete", 3)`. `forget_snapshots(ids)` forgets the snapshots it names, their directories with them,
recorded in `forgets` as `("ids", ids)` (final fix C1); `forget_prune` is recorded there too and forgets no
set (the set policy is real restic's, which
`tests/stack/test_prod_backup.py` runs). Snapshot ids and times count every snapshot made, forgotten ones
included, so a later one never takes a forgotten one's id.
"""

import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

import sqlalchemy as sa
from jobhunter.backup.restic import Restic, ResticError, SnapshotInfo
from jobhunter.backup.runner import CommandResult
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.observability.backup_health import BackupStatus
from sqlalchemy.ext.asyncio import AsyncEngine

_SET: Final = sa.text(
    "INSERT INTO backup_sets (status, restic_snapshot_id, dump_sha256, db_snapshot_at, row_counts, error, "
    "started_at, finished_at) VALUES (:status, :snapshot, :sha, :db_snapshot_at, CAST(:row_counts AS jsonb), "
    ":error, :started_at, :finished_at) RETURNING id"
)
_RUN: Final = sa.text(
    "INSERT INTO backup_runs (kind, backup_set_id, status, error, started_at, finished_at) "
    "VALUES (:kind, :set_id, :status, :error, :started_at, :finished_at) RETURNING id"
)
_SHIPMENT: Final = sa.text(
    "INSERT INTO manifest_shipments (restic_snapshot_id, shipped_at) VALUES ('manifests-snapshot', :at) "
    "RETURNING id"
)
_SHIPPED: Final = sa.text(
    "WITH m AS (INSERT INTO deletion_manifests (manifest_uuid, kind, status, file_sha256, completed_at, "
    "shipped_at, shipment_id) VALUES (gen_random_uuid(), 'source_purge', 'shipped', '\\x02', :completed_at, "
    ":shipped_at, :shipment) RETURNING manifest_uuid, file_sha256) "
    "INSERT INTO manifest_shipment_items (shipment_id, manifest_uuid, file_sha256) "
    "SELECT :shipment, manifest_uuid, file_sha256 FROM m"
)
_COMPLETED: Final = sa.text(
    "INSERT INTO deletion_manifests (manifest_uuid, kind, status, file_sha256, completed_at) "
    "VALUES (gen_random_uuid(), 'source_purge', :status, :sha, :completed_at) RETURNING id"
)


async def _insert(engine: AsyncEngine, statement: sa.TextClause, params: dict[str, object]) -> int:
    async with engine.begin() as conn:
        return int((await conn.execute(statement, params)).scalar_one())


async def backup_set(engine: AsyncEngine, status: BackupStatus, finished_at: datetime | None = None) -> int:
    """A backup set: `succeeded` with its snapshot, dump hash and row counts, `failed` with its error code, or
    `running` (pass no `finished_at`)."""
    done = status == "succeeded"
    params: dict[str, object] = {
        "status": status,
        "snapshot": "set-snapshot" if done else None,
        "sha": b"\x01" if done else None,
        "db_snapshot_at": finished_at if done else None,
        "row_counts": '{"jobs": 1}' if done else None,
        "error": "restic_failed" if status == "failed" else None,
        "started_at": utcnow() if finished_at is None else finished_at - timedelta(minutes=10),
        "finished_at": finished_at,
    }
    return await _insert(engine, _SET, params)


async def backup_run(
    engine: AsyncEngine,
    status: BackupStatus,
    finished_at: datetime | None = None,
    *,
    kind: Literal["restore_test", "prune"] = "restore_test",
    set_id: int | None = None,
) -> int:
    """A restore-test (or prune) run, of the set `set_id`."""
    params: dict[str, object] = {
        "kind": kind,
        "set_id": set_id,
        "status": status,
        "error": "verify_failed" if status == "failed" else None,
        "started_at": utcnow() if finished_at is None else finished_at - timedelta(minutes=30),
        "finished_at": finished_at,
    }
    return await _insert(engine, _RUN, params)


async def shipment(engine: AsyncEngine, shipped_at: datetime, *, manifests: int) -> int:
    """A manifest shipment of `manifests` shipped manifests, each named by an item of it."""
    async with engine.begin() as conn:
        shipment_id = int((await conn.execute(_SHIPMENT, {"at": shipped_at})).scalar_one())
        for _ in range(manifests):
            params = {"completed_at": shipped_at - timedelta(hours=1), "shipped_at": shipped_at}
            await conn.execute(_SHIPPED, {**params, "shipment": shipment_id})
    return shipment_id


async def manifest(
    engine: AsyncEngine, status: Literal["executing", "completed"], completed_at: datetime | None = None
) -> int:
    """A manifest not shipped: `completed` at `completed_at`, with its file hash, or still `executing`."""
    sha = b"\x02" if status == "completed" else None
    return await _insert(engine, _COMPLETED, {"status": status, "sha": sha, "completed_at": completed_at})


def lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


class Unreachable:
    async def run(self, argv: Sequence[str], **_: Any) -> CommandResult:
        raise AssertionError(f"the fake restic ran {argv[1]}")


class FakeRestic(Restic):
    """`backup` records the file list it is given (the files, its tags) and returns the next snapshot id, or
    raises `failure`; nothing else is called."""

    def __init__(self, cfg: AppConfig, *, failure: ResticError | None = None) -> None:
        super().__init__(cfg, Unreachable())
        self.failure = failure
        self.backups: list[tuple[list[str], tuple[str, ...]]] = []

    async def backup(
        self, *, paths: Sequence[Path] = (), files_from: Path | None = None, tags: Sequence[str]
    ) -> str:
        assert not paths and files_from is not None
        self.backups.append((lines(files_from), tuple(tags)))
        if self.failure is not None:
            raise self.failure
        return f"{len(self.backups):064x}"


@dataclass(frozen=True)
class Restored:
    """One `FakeRepository.restore` call."""

    snapshot: str
    subfolder: str
    target: Path


class FakeRepository(Restic):
    """restic on a directory of the test (module docstring)."""

    _EPOCH: Final = datetime(2026, 11, 1, tzinfo=UTC)

    def __init__(self, cfg: AppConfig, root: Path, *, failing: Sequence[str] = ()) -> None:
        super().__init__(cfg, Unreachable())
        self.root = root
        self.failing = set(failing)
        self.whole_paths: set[str] = set()
        self.unreadable: set[Path] = set()
        self.infos: list[SnapshotInfo] = []
        self.restores: list[Restored] = []
        self.forgets: list[tuple[str, Any]] = []
        # Added to the time of every snapshot made from now on: a clock stepped back makes it negative.
        self.shift = timedelta(0)
        self._made = 0

    def stored(self, snapshot: str, path: Path) -> Path:
        """Where `snapshot` holds the backed-up file `path`."""
        return self._inside(self.root / snapshot, path)

    def _fail(self, command: str) -> None:
        if command in self.failing:
            raise ResticError(command, "failed", 1)

    @staticmethod
    def _inside(snapshot_dir: Path, path: str | Path) -> Path:
        return snapshot_dir / str(path).lstrip("/")

    async def backup(
        self, *, paths: Sequence[Path] = (), files_from: Path | None = None, tags: Sequence[str]
    ) -> str:
        self._fail("backup")
        self._made += 1
        snapshot_id = f"{self._made:064x}"
        stored = self.root / snapshot_id
        stored.mkdir(parents=True)
        named = [Path(name) for name in lines(files_from)] if files_from is not None else []
        listed = [file for file in named if file not in self.unreadable]
        for path in paths:
            shutil.copytree(path, self._inside(stored, path))
        for file in listed:
            self._inside(stored, file).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, self._inside(stored, file))
        time = self._EPOCH + timedelta(minutes=self._made - 1) + self.shift
        names = tuple(str(path) for path in [*paths, *listed])
        files = len(listed) + sum(1 for path in paths for found in path.rglob("*") if found.is_file())
        self.infos.append(SnapshotInfo(snapshot_id, time, tuple(tags), names, files))
        if len(listed) < len(named):  # exit 3: the incomplete snapshot is saved all the same
            raise ResticError("backup", "incomplete", 3)
        return snapshot_id

    async def snapshots(self, *, tags: Sequence[str]) -> list[SnapshotInfo]:
        self._fail("snapshots")
        return [info for info in self.infos if set(tags) <= set(info.tags)]

    async def forget_snapshots(self, ids: Sequence[str]) -> None:
        self._fail("forget")
        self.forgets.append(("ids", list(ids)))
        for snapshot_id in ids:
            shutil.rmtree(self.root / snapshot_id, ignore_errors=True)
        self.infos = [info for info in self.infos if info.id not in set(ids)]

    async def forget_prune(self, *, tag: str, keep_daily: int, keep_weekly: int, keep_monthly: int) -> None:
        self._fail("forget")
        self.forgets.append(
            (tag, {"keep_daily": keep_daily, "keep_weekly": keep_weekly, "keep_monthly": keep_monthly})
        )

    async def restore(self, *, snapshot: str, subfolder: str, target: Path) -> None:
        self._fail("restore")
        self.restores.append(Restored(snapshot, subfolder, target))
        source = self._inside(self.root / snapshot, subfolder)
        if not source.is_dir():
            raise ResticError("restore", "failed", 1)
        landing = self._inside(target, subfolder) if subfolder in self.whole_paths else target
        shutil.copytree(source, landing, dirs_exist_ok=True)
