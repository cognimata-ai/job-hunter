"""Crash case C31 (spec §18.3, §19.5 step 3; plan M5 Task 12, Deviation 17): a real worker dies in a purge.

A golden-case delete is opened in process (`request_golden_case_delete`, its job deferred), with a `ready`
export and its file on the worker's exports volume. Worker A (`JOBHUNTER_FAULT=<point>`) claims the purge and
is killed at the point (exit 86, no cleanup); its procrastinate heartbeat is made stale, so worker B's start
releases the job, and B's takeover sweep gives the operation back.

- **`privacy.purge.after_file`** (C31 proper): A dies after the manifest file's rename, before A3. The
  manifest is `executing`, its file present and its hash not recorded. Rows are then inserted in other
  tables (a golden case, an export), so a recomputation would give other `max_ids` and name the new export.
  B adopts the file unchanged (its `scope` equals the file, its hash the file's) and resumes to `completed`:
  the case and the export of A2 are gone, the new ones stay. The deletion is reported done only once one
  shipment (the shipper in process, as `jobhunter_backup`, on a fake restic) has covered the file and
  `manifest_shipping` has made it `shipped`.
- A file of the manifest's uuid that does not parse fails B's operation `manifest_invalid`, the fence kept.
  The golden kind raises no fence, so that purge is opened with a `jobs` fence beside its request: the fence
  is the protocol's, and the kept fence is observable.
- **The other four points** (`before_file`, `after_record`, `mid_delete`, `before_complete`): each resumes
  to `completed` with the same file and scope.

Every datum is invented; no paid call (the worker is in fake LLM mode and the purge calls nothing).
"""

import hashlib
import signal
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.backup.shipper import ship_manifests
from jobhunter.clock import utcnow
from jobhunter.db.session import make_engine
from jobhunter.privacy import purge
from jobhunter.privacy.fences import FenceSpec
from jobhunter.privacy.kinds.golden_case import request_golden_case_delete
from jobhunter.privacy.manifests import canonical_bytes, manifest_file_name, parse_manifest
from jobhunter.privacy.purge import OpenedPurge, open_purge
from jobhunter.privacy.reconcilers import PURGE_FAILED_ALERT  # registers `manifest_shipping` too
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.backups import FakeRestic
from tests.support.m2 import insert_profile_chain
from tests.support.m3 import Job, insert_golden_case, insert_job
from tests.support.pg import PgServer, config_for, pg_one
from tests.support.procs import Proc, ProcessGroup, wait_until
from tests.support.reconcile import run_reconciler

pytestmark = [pytest.mark.crash, pytest.mark.slow]
WORKER: Final = [sys.executable, "-m", "jobhunter.cli", "worker"]
FAULT_EXIT: Final = 86
READY_WITHIN_S: Final = 60
ENDED_WITHIN_S: Final = 120
_EXPORT: Final = sa.text(
    "INSERT INTO exports (status, expires_at) VALUES ('ready', now() + interval '7 days') RETURNING id"
)
_EXPORT_PATH: Final = sa.text("UPDATE exports SET storage_path = :p WHERE id = :i")


@dataclass(frozen=True)
class Volumes:
    """The worker's manifests and exports volumes, this test's own (0700, as the images make them)."""

    manifests: Path
    exports: Path
    staging: Path


@pytest.fixture
def volumes(tmp_path: Path) -> Volumes:
    made = Volumes(tmp_path / "manifests", tmp_path / "exports", tmp_path / "staging")
    for directory in (made.manifests, made.exports, made.staging):
        directory.mkdir(mode=0o700)
    return made


@dataclass(frozen=True)
class Workers:
    """Spawns `jobhunter worker` children on this test's volumes, with an optional fault point."""

    server: PgServer
    proc_env: Any
    procs: ProcessGroup
    volumes: Volumes

    def env(self, **extra: str) -> dict[str, str]:
        dirs = {
            "JOBHUNTER_MANIFESTS_DIR": str(self.volumes.manifests),
            "JOBHUNTER_EXPORTS_DIR": str(self.volumes.exports),
        }
        env: dict[str, str] = self.proc_env("worker", self.server, **dirs, **extra)
        return env

    def crash_at(self, point: str) -> None:
        """Worker A, killed at `point`; its procrastinate heartbeat then made stale (as in C25), so the next
        worker's start releases its job."""
        a = self.procs.spawn(WORKER, self.env(JOBHUNTER_FAULT=point))
        a.wait_event("fault_point_reached", READY_WITHIN_S + ENDED_WITHIN_S, point=point)
        assert a.exit_within(30) == FAULT_EXIT
        pg_one(
            self.server,
            "superuser",
            "UPDATE procrastinate_workers SET last_heartbeat = now() - interval '5 minutes'",
        )

    def resume(self, operation_id: int) -> Proc:
        """Worker B, ready; it returns once the purge operation has ended (`succeeded` or `failed`)."""
        b = self.procs.spawn(WORKER, self.env())
        b.wait_event("worker_ready", READY_WITHIN_S)
        try:
            wait_until(
                lambda: operation(self.server, operation_id)[0] in ("succeeded", "failed"), ENDED_WITHIN_S
            )
        except TimeoutError as exc:
            tail = "".join(b.output().splitlines(True)[-30:])
            raise TimeoutError(f"{exc}\noperation: {operation(self.server, operation_id)}\n{tail}") from None
        return b


@pytest.fixture
def workers(migrated: PgServer, proc_env: Any, procs: ProcessGroup, volumes: Volumes) -> Workers:
    return Workers(migrated, proc_env, procs, volumes)


def one(server: PgServer, statement: str, *params: object) -> tuple[Any, ...]:
    found = pg_one(server, "superuser", statement, *params)
    assert found is not None, statement
    return found


def manifest(server: PgServer, manifest_id: int) -> tuple[Any, ...]:
    """`(status, file_sha256, scope, shipped_at)` of the manifest."""
    found = one(
        server,
        "SELECT status, file_sha256, scope, shipped_at FROM deletion_manifests WHERE id = %s",
        manifest_id,
    )
    status, sha, scope, shipped_at = found
    return status, None if sha is None else bytes(sha), scope, shipped_at


def operation(server: PgServer, operation_id: int) -> tuple[Any, ...]:
    """`(status, last_error_code)` of the operation."""
    return one(server, "SELECT status, last_error_code FROM operations WHERE id = %s", operation_id)


def exists(server: PgServer, table: str, row_id: int) -> bool:
    return bool(one(server, f"SELECT EXISTS (SELECT 1 FROM {table} WHERE id = %s)", row_id)[0])  # noqa: S608


def export_of(server: PgServer, export_id: int) -> tuple[Any, ...]:
    return one(server, "SELECT status, storage_path FROM exports WHERE id = %s", export_id)


@dataclass(frozen=True)
class Case:
    job: Job
    case_id: int


async def golden_case(engine: AsyncEngine) -> Case:
    async with engine.begin() as conn:
        job = await insert_job(conn)
        chain = await insert_profile_chain(conn, uuid.uuid4().int % 10**6 + 10)
        return Case(job, await insert_golden_case(conn, job, chain.profile_version_id))


async def ready_export(engine: AsyncEngine, directory: Path) -> tuple[int, Path]:
    """A `ready` export with its published archive on the exports volume, named as Task 17 names it."""
    async with engine.begin() as conn:
        export_id = int((await conn.execute(_EXPORT)).scalar_one())
        name = f"{export_id}-{uuid.uuid4()}.jhx"
        await conn.execute(_EXPORT_PATH, {"p": name, "i": export_id})
    path = directory / name
    path.write_bytes(b"\x00 encrypted segments")
    return export_id, path


async def open_delete(engine: AsyncEngine, case_id: int) -> OpenedPurge:
    async with engine.begin() as conn:
        return await request_golden_case_delete(conn, case_id, now=utcnow(), defer=defer_in_tx)


def assert_completed_from_its_file(server: PgServer, volumes: Volumes, opened: OpenedPurge) -> bytes:
    """The manifest `completed`, its `scope` the file's content and its hash the file's; the file's bytes."""
    status, sha, scope, shipped_at = manifest(server, opened.manifest_id)
    data = (volumes.manifests / manifest_file_name(opened.manifest_uuid)).read_bytes()
    assert (status, shipped_at) == ("completed", None)
    assert scope == parse_manifest(data, file_name=manifest_file_name(opened.manifest_uuid))
    assert data == canonical_bytes(scope) and sha == hashlib.sha256(data).digest()
    return data


async def test_c31_a_crash_after_the_rename_adopts_the_file_and_is_done_once_shipped(
    migrated: PgServer,
    engine: AsyncEngine,
    queue_app: procrastinate.App,
    workers: Workers,
    volumes: Volumes,
) -> None:
    case = await golden_case(engine)
    export_id, export_file = await ready_export(engine, volumes.exports)
    opened = await open_delete(engine, case.case_id)
    workers.crash_at(purge.AFTER_FILE)

    status, sha, _, _ = manifest(migrated, opened.manifest_id)
    path = volumes.manifests / manifest_file_name(opened.manifest_uuid)
    written = path.read_bytes()
    assert (status, sha) == ("executing", None) and not list(volumes.manifests.glob("*.tmp"))
    assert exists(migrated, "golden_cases", case.case_id) and export_file.exists()

    later = await golden_case(engine)  # a recomputation would give other max_ids
    later_export, later_file = await ready_export(engine, volumes.exports)  # and name this export too
    b = workers.resume(opened.operation_id)
    assert operation(migrated, opened.operation_id) == ("succeeded", None)
    assert assert_completed_from_its_file(migrated, volumes, opened) == written  # adopted unchanged
    scope = manifest(migrated, opened.manifest_id)[2]
    assert later.case_id > scope["max_ids"]["golden_cases"] and later_export > scope["max_ids"]["exports"]
    assert not exists(migrated, "golden_cases", case.case_id) and exists(
        migrated, "golden_cases", later.case_id
    )
    assert export_of(migrated, export_id) == ("purged", None) and not export_file.exists()
    assert export_of(migrated, later_export) == ("ready", later_file.name) and later_file.exists()
    b.signal(signal.SIGTERM)
    assert b.exit_within(60) == 0

    # completed is not done: only a shipment covering the file makes the deletion `shipped`
    async with backup_engine(migrated, volumes) as (cfg, backup):
        result = await ship_manifests(cfg, FakeRestic(cfg), backup, clock=utcnow)
    assert result.shipped and manifest(migrated, opened.manifest_id)[0] == "completed"
    current = int(one(migrated, "SELECT worker_epoch FROM worker_heartbeat")[0])
    await run_reconciler("manifest_shipping", engine, now=utcnow(), epoch=current)
    status, _, _, shipped_at = manifest(migrated, opened.manifest_id)
    assert status == "shipped" and shipped_at is not None


@asynccontextmanager
async def backup_engine(server: PgServer, volumes: Volumes) -> AsyncIterator[tuple[Any, AsyncEngine]]:
    """The backup role's configuration on the worker's manifests volume (and a staging directory and a
    repository of its own), with its engine, disposed after."""
    cfg = config_for(
        server,
        "backup",
        JOBHUNTER_MANIFESTS_DIR=str(volumes.manifests),
        JOBHUNTER_STAGING_DIR=str(volumes.staging),
        RESTIC_REPOSITORY=str(volumes.staging.parent / "repo"),
    )
    engine = make_engine(cfg, pool_size=2)
    try:
        yield cfg, engine
    finally:
        await engine.dispose()


async def test_c31_a_file_that_does_not_parse_fails_manifest_invalid_and_keeps_the_fence(
    migrated: PgServer, engine: AsyncEngine, queue_app: procrastinate.App, workers: Workers, volumes: Volumes
) -> None:
    case = await golden_case(engine)
    async with engine.begin() as conn:
        await conn.execute(sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE"))
        opened = await open_purge(
            conn,
            kind="golden_case_delete",
            request={"golden_case_id": case.case_id},
            fences=[FenceSpec(kind="jobs", job_ids=(case.job.job_id,))],
            now=utcnow(),
            defer=defer_in_tx,
        )
    workers.crash_at(purge.AFTER_FILE)
    path = volumes.manifests / manifest_file_name(opened.manifest_uuid)
    path.write_bytes(b'{"format": 1, "manifest_uuid": ')  # a file of the uuid that does not parse

    b = workers.resume(opened.operation_id)
    assert operation(migrated, opened.operation_id) == ("failed", "manifest_invalid")
    status, sha, _, _ = manifest(migrated, opened.manifest_id)
    assert (status, sha) == ("executing", None) and path.read_bytes() == b'{"format": 1, "manifest_uuid": '
    fence = one(migrated, "SELECT released_at FROM purge_fences WHERE manifest_id = %s", opened.manifest_id)
    assert fence == (None,)  # kept: fenced work stays blocked
    assert exists(migrated, "golden_cases", case.case_id)
    alert = one(
        migrated,
        "SELECT severity, resolved_at FROM alerts WHERE dedup_key = %s",
        f"{PURGE_FAILED_ALERT}:manifest:{opened.manifest_id}",
    )
    assert alert == ("critical", None)
    b.signal(signal.SIGTERM)
    assert b.exit_within(60) == 0


@pytest.mark.parametrize(
    "point", [purge.BEFORE_FILE, purge.AFTER_RECORD, purge.MID_DELETE, purge.BEFORE_COMPLETE]
)
async def test_c31_every_other_fault_point_resumes_to_completed(
    migrated: PgServer,
    engine: AsyncEngine,
    queue_app: procrastinate.App,
    workers: Workers,
    volumes: Volumes,
    point: str,
) -> None:
    case = await golden_case(engine)
    export_id, export_file = await ready_export(engine, volumes.exports)
    opened = await open_delete(engine, case.case_id)
    workers.crash_at(point)

    status, sha, _, _ = manifest(migrated, opened.manifest_id)
    path = volumes.manifests / manifest_file_name(opened.manifest_uuid)
    assert status == "executing"
    # what the crash left: nothing deleted before the record; the export ops after the kind's chunks
    left = {
        purge.BEFORE_FILE: (False, False, True, "ready"),
        purge.AFTER_RECORD: (True, True, True, "ready"),
        purge.MID_DELETE: (True, True, False, "ready"),
        purge.BEFORE_COMPLETE: (True, True, False, "purged"),
    }[point]
    assert (path.exists(), sha is not None, exists(migrated, "golden_cases", case.case_id)) == left[:3]
    assert export_of(migrated, export_id)[0] == left[3]

    b = workers.resume(opened.operation_id)
    assert operation(migrated, opened.operation_id) == ("succeeded", None)
    assert_completed_from_its_file(migrated, volumes, opened)
    assert not exists(migrated, "golden_cases", case.case_id)
    assert export_of(migrated, export_id) == ("purged", None) and not export_file.exists()
    b.signal(signal.SIGTERM)
    assert b.exit_within(60) == 0
