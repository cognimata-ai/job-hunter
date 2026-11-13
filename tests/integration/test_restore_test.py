"""The monthly restore test (spec §19.5, §22 V22; plan M5 Task 26, R15, R23, Deviations 46, 55 and 56):
`backup.restore_test.run_restore_test`, `jobhunter backup restore-test`.

The sets are the shared database as `run_backup_set` takes it, with the backup role's configuration: its
`pg_dump` and `pg_restore` the server's own, run in the server's container by `ContainerRunner` (the base name
of `argv[0]`: `/opt/pgclient/bin` exists only in the backup image), restic `FakeRepository` (a repository in a
directory of the test), the stream the purger's manifests directory (`tests.support.purges`) shipped by
`ship_manifests`, and the staging directory a temporary one. The restore test then restores the latest set and
stream into `jobhunter_restore_test` and `<staging>/restore_test` as `jobhunter_restore`. Every datum is
invented; nothing leaves the process (delete-everything's live run cancels through M3's fake Batches API, the
restore test's replay calls nothing).
"""

from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import pytest
import sqlalchemy as sa
from jobhunter.backup import restore_test
from jobhunter.backup.backup_set import run_backup_set
from jobhunter.backup.restic import ResticError
from jobhunter.backup.restore_test import RESTORE_TEST_DB, RestoreTestFailed, run_restore_test
from jobhunter.backup.shipper import ship_manifests
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.db.session import make_engine
from jobhunter.ids import uuid7
from jobhunter.llm.batches import BatchesClient
from jobhunter.observability.deadman import DeadmanPinger
from jobhunter.privacy.manifests import FileRoots, ManifestContent, write_manifest_file
from jobhunter.privacy.restore_verify import VerifyReport, verify_replayed
from jobhunter.scoring import batch_results
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from structlog.testing import capture_logs

from tests.support.backups import FakeRepository
from tests.support.pg import PgServer, config_for, pg_one, pg_rows
from tests.support.purges import Purger, completed, request_delete_everything, seeded
from tests.support.restore import ContainerRunner

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("current_epoch")]

_RUN: Final = sa.text("SELECT * FROM backup_runs WHERE id = :id")
_SCRATCH_EXISTS: Final = f"SELECT count(*) FROM pg_database WHERE datname = '{RESTORE_TEST_DB}'"  # noqa: S608
_MANIFEST_ROWS: Final = sa.text("SELECT count(*) FROM deletion_manifests WHERE manifest_uuid = :u")
_SUBMISSION: Final = sa.text("SELECT count(*) FROM batch_submissions WHERE id = :s")
# What the scratch database is while the restore test verifies it.
_SCRATCH: Final = sa.text(
    "SELECT current_user AS login, current_database() AS db, "
    "(SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()) AS owner, "
    "(SELECT count(*) FROM pg_extension WHERE extname = 'vector') AS vector, "
    "to_regclass('public.leftover') IS NULL AS fresh"
)
REPLAY_STEPS: Final = ["reconstruct_and_advance", "alembic", "advance_again", "replay"]


@pytest.fixture
def backup_cfg(migrated: PgServer, purge_cfg: AppConfig, tmp_path: Path) -> AppConfig:
    """The backup role with the purger's stream and CV store, and a staging directory of the test's own."""
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    return config_for(
        migrated,
        "backup",
        JOBHUNTER_STAGING_DIR=str(staging),
        JOBHUNTER_CV_STORE_DIR=str(purge_cfg.cv_store_dir),
        JOBHUNTER_MANIFESTS_DIR=str(purge_cfg.manifests_dir),
        RESTIC_REPOSITORY=str(tmp_path / "repository"),
    )


@pytest.fixture
def repo(backup_cfg: AppConfig, tmp_path: Path) -> FakeRepository:
    return FakeRepository(backup_cfg, tmp_path / "repository")


@pytest.fixture
def runner(migrated: PgServer) -> ContainerRunner:
    return ContainerRunner(migrated)


@pytest.fixture
async def backup_engine(backup_cfg: AppConfig) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(backup_cfg, pool_size=2)
    try:
        yield engine
    finally:
        await engine.dispose()


async def one(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(statement, params)).one()


async def scalar(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(statement, params)).scalar_one()


def events(logs: Sequence[Mapping[str, Any]], name: str) -> list[Mapping[str, Any]]:
    return [entry for entry in logs if entry["event"] == name]


def a_manifest() -> ManifestContent:
    """An invented golden-case deletion that deletes nothing: a replay inserts, runs and completes it."""
    stamp = utcnow().isoformat()
    built: dict[str, Any] = {
        "format": 1,
        "manifest_uuid": str(uuid7()),
        "kind": "golden_case_delete",
        "admitted_at": stamp,
        "fenced_at": stamp,
        "max_ids": {"alerts": 40},
        "operations": [],
    }
    return built  # type: ignore[return-value]  # the TypedDict's keys are built above


async def restore_tested(
    cfg: AppConfig, repo: FakeRepository, runner: ContainerRunner, engine: AsyncEngine
) -> int:
    return await run_restore_test(cfg, repo, runner, engine, clock=utcnow)


async def test_it_runs_as_jobhunter_restore_into_a_template_database_and_drops_it(
    migrated: PgServer,
    engine: AsyncEngine,
    backup_cfg: AppConfig,
    backup_engine: AsyncEngine,
    repo: FakeRepository,
    runner: ContainerRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scratch database a dead run left is dropped first; the new one is created by `jobhunter_restore` from
    `jobhunter_template` (its extensions, nothing else), loaded, migrated and replayed as that role, verified,
    and dropped; the scratch directory too. The run is `succeeded` with its report and names its set; the
    application database never sees the replayed manifest."""
    pg_rows(
        migrated, "restore", f"CREATE DATABASE {RESTORE_TEST_DB} TEMPLATE jobhunter_template", db="postgres"
    )
    pg_rows(migrated, "restore", "CREATE TABLE leftover (x int)", db=RESTORE_TEST_DB)
    manifest = a_manifest()
    write_manifest_file(backup_cfg.manifests_dir, manifest)
    assert (await ship_manifests(backup_cfg, repo, backup_engine, clock=utcnow)).shipped
    set_id = await run_backup_set(backup_cfg, repo, runner, clock=utcnow)
    seen: list[Any] = []
    verify = verify_replayed

    async def observed(conn: AsyncConnection, manifest_dir: Path, *, files: FileRoots) -> VerifyReport:
        seen.append((await conn.execute(_SCRATCH)).one())
        return await verify(conn, manifest_dir, files=files)

    monkeypatch.setattr(restore_test, "verify_replayed", observed)

    run_id = await restore_tested(backup_cfg, repo, runner, backup_engine)

    assert [tuple(row) for row in seen] == [
        ("jobhunter_restore", RESTORE_TEST_DB, "jobhunter_restore", 1, True)
    ]
    loads = [argv for argv in runner.calls if argv[0].endswith("pg_restore") and argv[1] != "--list"]
    assert len(loads) == 1
    assert loads[0][loads[0].index("--username") + 1] == "jobhunter_restore"
    assert loads[0][loads[0].index("--dbname") + 1] == RESTORE_TEST_DB
    row = await one(engine, _RUN, id=run_id)
    assert (row.kind, row.status, row.error, row.backup_set_id) == ("restore_test", "succeeded", None, set_id)
    assert row.report == {
        "stream": {"snapshots": 1, "manifests": 1},
        "set_id": set_id,
        "restic_snapshot_id": repo.infos[-1].id,
        "cv_files": 0,
        "restored": {"ok": True, "failures": []},
        "migrate": REPLAY_STEPS,
        "exports": {"expired": 0, "failed": 0, "files_removed": 0},
        "replayed": {"ok": True, "failures": []},
    }
    assert pg_one(migrated, "superuser", _SCRATCH_EXISTS, db="postgres") == (0,)
    assert sorted(path.name for path in backup_cfg.staging_dir.iterdir()) == []
    assert await scalar(engine, _MANIFEST_ROWS, u=manifest["manifest_uuid"]) == 0


async def test_a_failure_records_a_failed_run(
    migrated: PgServer,
    engine: AsyncEngine,
    backup_cfg: AppConfig,
    backup_engine: AsyncEngine,
    repo: FakeRepository,
    runner: ContainerRunner,
) -> None:
    """A load that fails ends the run `failed` with `<step>:<code>` and the report so far (Task 7 raises its
    alert from the row); the scratch database and directory are dropped all the same."""
    set_id = await run_backup_set(backup_cfg, repo, runner, clock=utcnow)

    with pytest.raises(RestoreTestFailed) as failed:
        await restore_tested(backup_cfg, repo, ContainerRunner(migrated, fail="pg_restore"), backup_engine)

    assert failed.value.error == "pg_restore:pg_restore_failed"
    row = await one(engine, _RUN, id=failed.value.run_id)
    assert (row.kind, row.status, row.error, row.backup_set_id) == (
        "restore_test",
        "failed",
        "pg_restore:pg_restore_failed",
        set_id,
    )
    assert row.finished_at is not None
    assert row.report == {
        "stream": None,
        "set_id": set_id,
        "restic_snapshot_id": repo.infos[-1].id,
        "cv_files": 0,
    }
    assert pg_one(migrated, "superuser", _SCRATCH_EXISTS, db="postgres") == (0,)
    assert sorted(path.name for path in backup_cfg.staging_dir.iterdir()) == []


async def test_a_restore_test_before_any_manifest_succeeds(
    engine: AsyncEngine,
    backup_cfg: AppConfig,
    backup_engine: AsyncEngine,
    repo: FakeRepository,
    runner: ContainerRunner,
) -> None:
    """No `manifests` snapshot exists yet (no purge ever ran): an empty stream, restored as an empty
    directory, and the test succeeds."""
    await run_backup_set(backup_cfg, repo, runner, clock=utcnow)

    with capture_logs() as logs:
        run_id = await restore_tested(backup_cfg, repo, runner, backup_engine)

    row = await one(engine, _RUN, id=run_id)
    assert (row.status, row.report["stream"], row.report["replayed"]) == (
        "succeeded",
        None,
        {"ok": True, "failures": []},
    )
    assert len(events(logs, "manifest_stream_empty")) == 1
    assert [restored.subfolder for restored in repo.restores] == [
        str(backup_cfg.staging_dir / "set"),
        str(backup_cfg.cv_store_dir),
    ]


async def test_the_restore_test_reads_every_manifests_snapshot(
    engine: AsyncEngine,
    backup_cfg: AppConfig,
    backup_engine: AsyncEngine,
    repo: FakeRepository,
    runner: ContainerRunner,
) -> None:
    """Final review C1, M2: the restore test reads the stream as `restore run` does, the union of every
    `manifests` snapshot. The set predates M1 and M2; one shipment confirms both, then restic exits 3 on the
    next (M1 unreadable, M3 new), leaving the newest snapshot without M1. The scratch replay inserts and
    completes all three, and the test succeeds."""
    await run_backup_set(backup_cfg, repo, runner, clock=utcnow)
    m1, m2, m3 = a_manifest(), a_manifest(), a_manifest()
    write_manifest_file(backup_cfg.manifests_dir, m1)
    write_manifest_file(backup_cfg.manifests_dir, m2)
    assert (await ship_manifests(backup_cfg, repo, backup_engine, clock=utcnow)).shipped
    write_manifest_file(backup_cfg.manifests_dir, m3)
    repo.unreadable.add(backup_cfg.manifests_dir / f"{m1['manifest_uuid']}.json")
    with pytest.raises(ResticError):
        await ship_manifests(backup_cfg, repo, backup_engine, clock=utcnow)

    with capture_logs() as logs:
        run_id = await restore_tested(backup_cfg, repo, runner, backup_engine)

    row = await one(engine, _RUN, id=run_id)
    assert (row.status, row.report["stream"], row.report["replayed"]) == (
        "succeeded",
        {"snapshots": 2, "manifests": 3},
        {"ok": True, "failures": []},
    )
    assert len(events(logs, "replay_admitted")) == 3
    for manifest in (m1, m2, m3):  # replayed in the scratch database only
        assert await scalar(engine, _MANIFEST_ROWS, u=manifest["manifest_uuid"]) == 0


async def test_the_restore_test_replays_a_submitted_batch_without_worker_credentials(
    engine: AsyncEngine,
    backup_cfg: AppConfig,
    backup_engine: AsyncEngine,
    repo: FakeRepository,
    runner: ContainerRunner,
    live: Purger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deviation 56: the set holds a `submitted` batch, and the stream a later delete-everything manifest. The
    backup role holds no Anthropic or healthchecks secret; its restore test replays the manifest with no
    Batches client and no pinger (each would fail the test), drops the recorded cancel, and succeeds."""
    seed = await seeded(engine, live_submission="submitted")
    await run_backup_set(backup_cfg, repo, runner, clock=utcnow)
    opened = await request_delete_everything(engine)
    await live.run(opened.operation_id)
    await completed(live, opened.manifest_id, opened.operation_id)
    await ship_manifests(backup_cfg, repo, backup_engine, clock=utcnow)

    def no_call(*args: object, **kwargs: object) -> Any:
        raise AssertionError("the restore test made an external call")

    monkeypatch.setattr(BatchesClient, "__init__", no_call)
    monkeypatch.setattr(batch_results, "send_purge_cancel", no_call)
    monkeypatch.setattr(DeadmanPinger, "__init__", no_call)
    monkeypatch.setattr(DeadmanPinger, "ping_pending", no_call)
    assert set(backup_cfg.secrets) == {"db_backup", "db_restore", "restic_password"}

    with capture_logs() as logs:
        run_id = await restore_tested(backup_cfg, repo, runner, backup_engine)

    row = await one(engine, _RUN, id=run_id)
    assert (row.status, row.error) == ("succeeded", None)
    assert row.report["restored"] == row.report["replayed"] == {"ok": True, "failures": []}
    skipped = events(logs, "batch_cancel_skipped_replay")
    assert [e["submission_id"] for e in skipped] == [seed.live_submission_id]
    # The application database keeps its own state: the live purge already deleted the submission there.
    assert await scalar(engine, _SUBMISSION, s=seed.live_submission_id) == 0


async def test_an_error_outside_a_step_still_fails_the_run(
    migrated: PgServer,
    engine: AsyncEngine,
    backup_cfg: AppConfig,
    backup_engine: AsyncEngine,
    repo: FakeRepository,
    runner: ContainerRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review M7: an exception raised outside every step (here making the scratch database's engine, between
    two steps) ends the run `failed` with `unexpected:<class>` and the report so far, never `running`; the
    scratch database and directory are dropped all the same."""
    set_id = await run_backup_set(backup_cfg, repo, runner, clock=utcnow)

    def broken(*args: object, **kwargs: object) -> AsyncEngine:
        raise RuntimeError("an engine that cannot be made")

    monkeypatch.setattr(restore_test, "null_engine", broken)

    with pytest.raises(RestoreTestFailed) as failed:
        await restore_tested(backup_cfg, repo, runner, backup_engine)

    assert failed.value.error == "unexpected:RuntimeError"
    row = await one(engine, _RUN, id=failed.value.run_id)
    assert (row.status, row.error, row.backup_set_id) == ("failed", "unexpected:RuntimeError", set_id)
    assert row.finished_at is not None
    assert row.report == {
        "stream": None,
        "set_id": set_id,
        "restic_snapshot_id": repo.infos[-1].id,
        "cv_files": 0,
    }
    assert pg_one(migrated, "superuser", _SCRATCH_EXISTS, db="postgres") == (0,)
    assert sorted(path.name for path in backup_cfg.staging_dir.iterdir()) == []
