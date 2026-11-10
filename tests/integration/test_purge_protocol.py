"""The purge protocol (spec §19.5 step 3, §20.2, §3.3.3; plan M5 Task 12, D1, D5, D16, Deviations 16, 17, 18,
53 and 56, Ruling T12-1) and the golden-case deletion, its first kind.

Every purge commits its manifest, its fences and its `purge` operation in one transaction (A0, `open_purge`);
the operation (`run_purge`) waits for the quiesce, computes the content, writes the canonical file and records
it (A1-A3) before anything is deleted, runs the kind's idempotent chunks on the stored scope (phase B, a lost
deadlock re-run, then requeued `lock_conflict`), purges the exports, and completes in one transaction that
releases the fences and checks every postcondition (phase C).

The golden kind has no fence, so the protocol's fence cases run on a test-only procedure of kind
`retention_purge` (in the CHECK, with no writer: Deviation 16), registered by the `fenced_kind` fixture and
unregistered after it: a `jobs` fence on one seeded job and a `row_delete` of one `job_tombstones` row.
A crash is `tests.support.faults.raising_at` in place of the module's `afault_point`; the requeued
operation is made due again by hand. Every row is invented.
"""

import asyncio
import hashlib
import json
import logging
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.db.transitions import transition
from jobhunter.dedup import merge
from jobhunter.errors import CoMemberChanged, Refusal
from jobhunter.llm.spend_ledger import EvalRunOwner, mark_dispatched, reserve
from jobhunter.privacy import deletion_graph, fences, purge
from jobhunter.privacy.deletion_graph import export_ops
from jobhunter.privacy.fences import FenceSpec
from jobhunter.privacy.kinds.golden_case import request_golden_case_delete
from jobhunter.privacy.manifests import (
    FileRoots,
    ManifestContent,
    ManifestKind,
    ManifestRow,
    Operation,
    Violation,
    canonical_bytes,
    check_postconditions,
    manifest_file_name,
    max_identity_ids,
    parse_manifest,
    read_manifest,
)
from jobhunter.privacy.purge import (
    KINDS,
    LiveEffects,
    OpenedPurge,
    PurgeOwner,
    PurgeRun,
    new_content,
    open_purge,
    register_kind,
)
from jobhunter.privacy.reconcilers import PURGE_FAILED_ALERT
from jobhunter.profile.storage import CvStore
from jobhunter.types import get_int
from jobhunter.worker import operations as ops
from jobhunter.worker.operations import OperationDeps
from jobhunter.worker.tasks import defer_in_tx, job_task_name
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.faults import no_fault, raising_at
from tests.support.fences import insert_test_fence
from tests.support.llm import ledger_row, reserve_kw
from tests.support.logs import logged
from tests.support.m2 import insert_profile_chain
from tests.support.m3 import Job, insert_golden_case, insert_job, insert_row
from tests.support.pg import PgServer, blocked_or_done
from tests.support.procs import await_until
from tests.support.reconcile import run_reconciler
from tests.support.scoring import member_group

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("current_epoch")]

_LOCK_CONFIG: Final = sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE")
_OPERATION: Final = sa.text(
    "SELECT status, attempts, last_error_code, not_before, payload, dedup_key FROM operations WHERE id = :i"
)
_PURGES: Final = sa.text(
    "SELECT id, payload, dedup_key, status FROM operations WHERE kind = 'purge' ORDER BY id"
)
_FENCES: Final = sa.text(
    "SELECT kind, job_ids, released_at FROM purge_fences WHERE manifest_id = :m ORDER BY id"
)
_ALERT: Final = sa.text(
    "SELECT severity, context, occurrences, resolved_at FROM alerts WHERE dedup_key = :k ORDER BY id"
)
_DUE: Final = sa.text("UPDATE operations SET not_before = NULL WHERE id = :i")
_TOMBSTONE: Final = sa.text("INSERT INTO job_tombstones (former_job_id) VALUES (:former) RETURNING id")
_EXPORT: Final = sa.text(
    "INSERT INTO exports (status, expires_at) VALUES (:status, now() + interval '7 days') RETURNING id"
)
_EXPORT_PATH: Final = sa.text("UPDATE exports SET storage_path = :p WHERE id = :i")
_EXPORT_ROW: Final = sa.text("SELECT status, storage_path FROM exports WHERE id = :i")
_DEADLOCK: Final = sa.text("DO $$ BEGIN RAISE EXCEPTION 'injected' USING ERRCODE = '40P01'; END $$")
_GOLDEN_RUN: Final = sa.text(
    "INSERT INTO eval_runs (kind, route, status, repetitions, metrics) "
    "VALUES ('golden', 'production', 'running', 2, '{\"transport_requested\": \"fake\"}') RETURNING id"
)
_END_RUN: Final = sa.text("UPDATE eval_runs SET status = 'failed', finished_at = now() WHERE id = :r")
_CASE_CALLS: Final = sa.text("SELECT count(*) FROM llm_calls WHERE golden_case_id = :g")
_ENTRY_STATE: Final = sa.text("SELECT state FROM llm_spend_ledger WHERE id = :i")


# ---- the test-only fenced kind -----------------------------------------------------------------------------


@dataclass
class FencedKind:
    """The protocol's test-only procedure (module docstring): a `jobs` fence on the request's job and a
    `row_delete` of the request's `job_tombstones` row, in one chunk over that job and its group's members.
    `keep_the_row` makes `execute` delete nothing (its postcondition then fails); the first `deadlocks`
    chunks lose a deadlock (40P01, raised by the server in the chunk's transaction) after their locks;
    `chunks` records each chunk's held jobs."""

    kind: ManifestKind = "retention_purge"
    deletes_exports: bool = True
    keep_the_row: bool = False
    deadlocks: int = 0
    chunks: list[frozenset[int]] = field(default_factory=list)

    async def compute(self, conn: AsyncConnection, manifest: ManifestRow, *, now: Any) -> ManifestContent:
        max_ids = await max_identity_ids(conn)
        job_id = get_int(dict(manifest.request), "job_id")
        row_id = get_int(dict(manifest.request), "tombstone_id")
        operations: list[Operation] = [
            {"op": "fence", "kind": "jobs", "key": "jobs", "job_ids": [job_id]},
            {"op": "row_delete", "table": "job_tombstones", "ids": [row_id], "bound": {"by": "id"}},
        ]  # the export ops come from the protocol (`purge.compute_content`)
        return new_content(manifest, max_ids=max_ids, operations=operations)

    async def execute(self, run: PurgeRun, content: ManifestContent) -> None:
        jobs = [job for op in content["operations"] if op["op"] == "fence" for job in op["job_ids"]]
        rows = [i for op in content["operations"] if op["op"] == "row_delete" for i in op.get("ids", [])]
        async with run.chunk(job_ids=jobs, with_groups_of=jobs) as chunk:
            self.chunks.append(chunk.held)
            if self.deadlocks:
                self.deadlocks -= 1
                await chunk.conn.execute(_DEADLOCK)
            if not self.keep_the_row:
                await chunk.conn.execute(
                    sa.text("DELETE FROM job_tombstones WHERE id = ANY(CAST(:ids AS bigint[]))"),
                    {"ids": rows},
                )

    async def postconditions(self, conn: AsyncConnection, content: ManifestContent) -> list[Violation]:
        return []

    async def reapply_admission(self, conn: AsyncConnection, manifest: ManifestRow, *, now: Any) -> None:
        return None


@pytest.fixture
def fenced_kind() -> Iterator[FencedKind]:
    """The test kind, registered for the test and unregistered after it: the global registry is unchanged."""
    before = dict(KINDS)
    proc = FencedKind()
    register_kind(proc)
    try:
        yield proc
    finally:
        del KINDS["retention_purge"]
        assert KINDS == before


# ---- the harness -------------------------------------------------------------------------------------------


@pytest.fixture
def purge_cfg(make_config: Any, migrated: PgServer, tmp_path: Path) -> AppConfig:
    """The worker's configuration with a manifests, an exports and a CV directory of this test's own."""
    dirs = {name: tmp_path / name for name in ("manifests", "exports", "cv_store")}
    for directory in dirs.values():
        directory.mkdir(mode=0o700)
    cfg: AppConfig = make_config(
        "worker",
        migrated,
        JOBHUNTER_MANIFESTS_DIR=str(dirs["manifests"]),
        JOBHUNTER_EXPORTS_DIR=str(dirs["exports"]),
        JOBHUNTER_CV_STORE_DIR=str(dirs["cv_store"]),
    )
    return cfg


@dataclass(frozen=True)
class Purger:
    """Runs a purge operation as the worker's `run_operation` does, with the worker's dependencies."""

    engine: AsyncEngine
    cfg: AppConfig
    deps: OperationDeps

    async def run(self, operation_id: int) -> None:
        await ops.execute_operation(self.engine, operation_id, epoch=1, deps=self.deps)

    async def again(self, operation_id: int) -> None:
        """The requeued operation made due at once, then run."""
        async with self.engine.begin() as conn:
            await conn.execute(_DUE, {"i": operation_id})
        await self.run(operation_id)

    def file(self, manifest_uuid: uuid.UUID) -> Path:
        return self.cfg.manifests_dir / manifest_file_name(manifest_uuid)


@pytest.fixture
def purger(engine: AsyncEngine, purge_cfg: AppConfig) -> Purger:
    unused: Any = None  # the purge reads none of the profile, score or LLM collaborators
    deps = OperationDeps(
        cfg=purge_cfg,
        gateway=unused,
        parse_client=unused,
        cv_store=CvStore(purge_cfg.cv_store_dir),
        profile_graphs=unused,
        embedder=None,
        clock=utcnow,
        score_steps=unused,
        batches_client=unused,
    )
    return Purger(engine, purge_cfg, deps)


async def tombstone_row(conn: AsyncConnection) -> int:
    return int((await conn.execute(_TOMBSTONE, {"former": 10**12 + uuid.uuid4().int % 10**9})).scalar_one())


@dataclass(frozen=True)
class FencedSeed:
    job: Job
    tombstone_id: int


async def fenced_seed(engine: AsyncEngine) -> FencedSeed:
    async with engine.begin() as conn:
        return FencedSeed(await insert_job(conn), await tombstone_row(conn))


async def open_fenced(engine: AsyncEngine, seed: FencedSeed, *, defer: Any = None) -> OpenedPurge:
    """The test kind's A0, as a privacy request opens a purge: `active_config FOR UPDATE`, then
    `open_purge`."""
    async with engine.begin() as conn:
        await conn.execute(_LOCK_CONFIG)
        return await open_purge(
            conn,
            kind="retention_purge",
            request={"job_id": seed.job.job_id, "tombstone_id": seed.tombstone_id},
            fences=[FenceSpec(kind="jobs", job_ids=(seed.job.job_id,))],
            now=utcnow(),
            defer=defer,
        )


async def golden_case(engine: AsyncEngine) -> int:
    """An active `week1` golden case on a new job and profile chain; its id."""
    async with engine.begin() as conn:
        job = await insert_job(conn)
        chain = await insert_profile_chain(conn, uuid.uuid4().int % 10**6 + 10)
        return await insert_golden_case(conn, job, chain.profile_version_id)


async def open_golden(engine: AsyncEngine, case_id: int, *, defer: Any = None) -> OpenedPurge:
    async with engine.begin() as conn:
        return await request_golden_case_delete(conn, case_id, now=utcnow(), defer=defer)


async def manifest_of(engine: AsyncEngine, manifest_id: int) -> ManifestRow:
    async with engine.connect() as conn:
        manifest = await read_manifest(conn, manifest_id)
    assert manifest is not None
    return manifest


async def operation(engine: AsyncEngine, operation_id: int) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(_OPERATION, {"i": operation_id})).one()


async def fence_rows(engine: AsyncEngine, manifest_id: int) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(_FENCES, {"m": manifest_id})).all())


async def purge_failed_alerts(engine: AsyncEngine, manifest_id: int) -> list[Any]:
    async with engine.connect() as conn:
        key = f"{PURGE_FAILED_ALERT}:manifest:{manifest_id}"
        return list((await conn.execute(_ALERT, {"k": key})).all())


async def exists(engine: AsyncEngine, table: str, row_id: int) -> bool:
    async with engine.connect() as conn:
        statement = sa.text(f"SELECT EXISTS (SELECT 1 FROM {table} WHERE id = :i)")  # noqa: S608 (a test's table)
        return bool((await conn.execute(statement, {"i": row_id})).scalar_one())


async def export_row(engine: AsyncEngine, export_id: int) -> tuple[str, str | None]:
    async with engine.connect() as conn:
        row = (await conn.execute(_EXPORT_ROW, {"i": export_id})).one()
    return str(row.status), row.storage_path


async def an_export(engine: AsyncEngine, directory: Path, status: str) -> tuple[int, Path]:
    """An export in `status` with its file as Task 17 names it: a `ready` one's published archive (its
    `storage_path`), a `running` one's archive being written; its id and file."""
    async with engine.begin() as conn:
        export_id = int((await conn.execute(_EXPORT, {"status": status})).scalar_one())
        name = f"{export_id}-{uuid.uuid4()}.jhx" + ("" if status == "ready" else ".tmp")
        if status == "ready":
            await conn.execute(_EXPORT_PATH, {"p": name, "i": export_id})
    path = directory / name
    path.write_bytes(b"\x00 encrypted segments")
    return export_id, path


# ---- A0 ----------------------------------------------------------------------------------------------------


async def test_a0_commits_manifest_fence_and_operation_together(engine: AsyncEngine) -> None:
    seed = await fenced_seed(engine)
    before = {
        table: await count(engine, table) for table in ("deletion_manifests", "purge_fences", "operations")
    }

    class RolledBack(Exception):
        pass

    with pytest.raises(RolledBack):
        async with engine.begin() as conn:
            await conn.execute(_LOCK_CONFIG)
            await open_purge(
                conn,
                kind="retention_purge",
                request={"job_id": seed.job.job_id, "tombstone_id": seed.tombstone_id},
                fences=[FenceSpec(kind="jobs", job_ids=(seed.job.job_id,))],
                now=utcnow(),
                defer=None,
            )
            raise RolledBack
    assert {table: await count(engine, table) for table in before} == before  # none of the three

    now = utcnow()
    async with engine.begin() as conn:
        await conn.execute(_LOCK_CONFIG)
        opened = await open_purge(
            conn,
            kind="retention_purge",
            request={"job_id": seed.job.job_id, "tombstone_id": seed.tombstone_id},
            fences=[FenceSpec(kind="jobs", job_ids=(seed.job.job_id,))],
            now=now,
            defer=None,
        )
    manifest = await manifest_of(engine, opened.manifest_id)
    assert (manifest.manifest_uuid, manifest.kind, manifest.status) == (
        opened.manifest_uuid,
        "retention_purge",
        "executing",
    )
    assert manifest.request == {"job_id": seed.job.job_id, "tombstone_id": seed.tombstone_id}
    assert (manifest.file_sha256, manifest.scope, manifest.created_at) == (None, None, now)  # admitted_at
    assert [(f.kind, f.job_ids, f.released_at) for f in await fence_rows(engine, opened.manifest_id)] == [
        ("jobs", [seed.job.job_id], None)
    ]
    row = await operation(engine, opened.operation_id)
    # the payload names the manifest alone: the request lives on its row (Deviation 53)
    assert (row.status, row.payload, row.dedup_key) == (
        "pending",
        {"manifest_id": opened.manifest_id},
        f"{purge.PURGE_DEDUP_PREFIX}{opened.manifest_uuid}",
    )


async def count(engine: AsyncEngine, table: str) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(sa.text(f"SELECT count(*) FROM {table}"))).scalar_one())  # noqa: S608


# ---- A1-A3 -------------------------------------------------------------------------------------------------


async def test_nothing_is_deleted_before_the_file_and_record_exist(
    engine: AsyncEngine, purger: Purger, fenced_kind: FencedKind, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash before the file leaves no file and nothing deleted; a crash after its rename leaves the file
    unrecorded and nothing deleted; the retry adopts that file unchanged although rows were added since (a
    recomputation would give other `max_ids`), and only then deletes."""
    seed = await fenced_seed(engine)
    opened = await open_fenced(engine, seed)
    path = purger.file(opened.manifest_uuid)

    monkeypatch.setattr(purge, "afault_point", raising_at(purge.BEFORE_FILE))
    await purger.run(opened.operation_id)
    assert (await operation(engine, opened.operation_id))[:3] == ("pending", 1, "bug")
    assert not path.exists() and not list(purger.cfg.manifests_dir.iterdir())  # no `.tmp` either
    assert (await manifest_of(engine, opened.manifest_id)).file_sha256 is None
    assert await exists(engine, "job_tombstones", seed.tombstone_id)
    assert fenced_kind.chunks == []

    monkeypatch.setattr(purge, "afault_point", raising_at(purge.AFTER_FILE))
    await purger.again(opened.operation_id)
    written = path.read_bytes()
    assert (await manifest_of(engine, opened.manifest_id)).file_sha256 is None
    assert await exists(engine, "job_tombstones", seed.tombstone_id) and fenced_kind.chunks == []

    async with engine.begin() as conn:  # rows of other tables: a recomputation would differ
        later = await tombstone_row(conn)
        await insert_job(conn)
    monkeypatch.setattr(purge, "afault_point", no_fault)
    await purger.again(opened.operation_id)

    manifest = await manifest_of(engine, opened.manifest_id)
    assert manifest.status == "completed" and path.read_bytes() == written  # adopted, never rewritten
    assert manifest.scope is not None and manifest.scope == parse_manifest(written, file_name=path.name)
    assert manifest.file_sha256 == hashlib.sha256(written).digest()
    assert manifest.scope["max_ids"]["job_tombstones"] < later
    assert not await exists(engine, "job_tombstones", seed.tombstone_id)
    assert await exists(engine, "job_tombstones", later)


async def test_the_file_is_canonical_and_equals_scope(engine: AsyncEngine, purger: Purger) -> None:
    case_id = await golden_case(engine)
    opened = await open_golden(engine, case_id)
    await purger.run(opened.operation_id)

    manifest = await manifest_of(engine, opened.manifest_id)
    path = purger.file(opened.manifest_uuid)
    data = path.read_bytes()
    assert manifest.status == "completed" and manifest.scope is not None
    assert data == canonical_bytes(manifest.scope) and manifest.file_sha256 == hashlib.sha256(data).digest()
    assert parse_manifest(data, file_name=path.name) == manifest.scope
    assert os.stat(path).st_mode & 0o777 == 0o600 and not list(path.parent.glob("*.tmp"))
    content = manifest.scope
    assert (content["format"], content["manifest_uuid"], content["kind"]) == (
        1,
        str(opened.manifest_uuid),
        "golden_case_delete",
    )
    assert content["admitted_at"] == manifest.created_at.isoformat()
    # the fence's commit cutoff, read after A0 committed: never before A0's own clock
    assert datetime.fromisoformat(content["fenced_at"]) >= manifest.created_at
    assert content["operations"] == [
        {"op": "row_delete", "table": "golden_cases", "ids": [case_id], "bound": {"by": "id"}}
    ]  # no export to purge
    assert content["max_ids"]["golden_cases"] >= case_id
    assert not await exists(engine, "golden_cases", case_id)
    assert (await operation(engine, opened.operation_id)).status == "succeeded"


async def test_a_retry_after_record_never_recomputes(
    engine: AsyncEngine, purger: Purger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after A3: the retry runs phase B on the stored scope, so a golden case created after A3 lies
    above `max_ids` and survives, and an export made after A3 stays (a recomputation would name it)."""
    case_id = await golden_case(engine)
    opened = await open_golden(engine, case_id)
    monkeypatch.setattr(purge, "afault_point", raising_at(purge.AFTER_RECORD))
    await purger.run(opened.operation_id)
    recorded = await manifest_of(engine, opened.manifest_id)
    assert recorded.file_sha256 is not None and recorded.scope is not None
    assert await exists(engine, "golden_cases", case_id)  # nothing deleted yet
    written = purger.file(opened.manifest_uuid).read_bytes()

    later = await golden_case(engine)
    export_id, export_file = await an_export(engine, purger.cfg.exports_dir, "ready")
    monkeypatch.setattr(purge, "afault_point", no_fault)
    await purger.again(opened.operation_id)

    manifest = await manifest_of(engine, opened.manifest_id)
    assert manifest.status == "completed"
    assert (manifest.scope, manifest.file_sha256) == (recorded.scope, recorded.file_sha256)
    assert purger.file(opened.manifest_uuid).read_bytes() == written
    assert later > recorded.scope["max_ids"]["golden_cases"] and await exists(engine, "golden_cases", later)
    assert not await exists(engine, "golden_cases", case_id)
    assert await export_row(engine, export_id) == ("ready", export_file.name) and export_file.exists()


async def test_a_quiesce_timeout_requeues(
    engine: AsyncEngine, purger: Purger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transaction of the application role that began before the fence's cutoff and is still open after
    `QUIESCE_TIMEOUT` (made 0 here): the operation is requeued `quiesce_timeout` a minute later, an attempt
    spent, nothing computed, written or deleted; once it has ended, the retry completes."""
    case_id = await golden_case(engine)
    timeout = fences.QUIESCE_TIMEOUT
    monkeypatch.setattr(fences, "QUIESCE_TIMEOUT", timedelta(0))
    async with engine.connect() as old:
        await old.begin()
        await old.execute(sa.text("SELECT 1"))  # its transaction has begun
        opened = await open_golden(engine, case_id)
        before = utcnow()
        await purger.run(opened.operation_id)
        row = await operation(engine, opened.operation_id)
        assert (row.status, row.attempts, row.last_error_code) == ("pending", 1, "quiesce_timeout")
        assert before + purge.RETRY_AFTER <= row.not_before <= utcnow() + purge.RETRY_AFTER
        assert (await manifest_of(engine, opened.manifest_id)).file_sha256 is None
        assert not purger.file(opened.manifest_uuid).exists()
        assert await exists(engine, "golden_cases", case_id)
        await old.rollback()
    monkeypatch.setattr(fences, "QUIESCE_TIMEOUT", timeout)  # a heartbeat's transaction never times it out
    await purger.again(opened.operation_id)
    assert (await manifest_of(engine, opened.manifest_id)).status == "completed"


# ---- phase C -----------------------------------------------------------------------------------------------


async def test_a_postcondition_violation_fails_and_keeps_the_fence(
    engine: AsyncEngine,
    purger: Purger,
    fenced_kind: FencedKind,
    caplog: pytest.LogCaptureFixture,
    queue_app: procrastinate.App,
) -> None:
    """The kind deleted nothing: the completing transaction (which released the fence before its checks)
    rolls back, so the fence stays active, and a second transaction ends the operation
    `failed('postcondition')`, whose failure hook raises `privacy.purge_failed` at once."""
    fenced_kind.keep_the_row = True
    seed = await fenced_seed(engine)
    opened = await open_fenced(engine, seed)
    await purger.run(opened.operation_id)

    row = await operation(engine, opened.operation_id)
    assert (row.status, row.last_error_code) == ("failed", "postcondition")
    manifest = await manifest_of(engine, opened.manifest_id)
    assert manifest.status == "executing" and manifest.file_sha256 is not None
    assert [f.released_at for f in await fence_rows(engine, opened.manifest_id)] == [None]
    assert await exists(engine, "job_tombstones", seed.tombstone_id)
    [alert] = await purge_failed_alerts(engine, opened.manifest_id)
    assert (alert.severity, alert.resolved_at) == ("critical", None)
    assert alert.context == {
        "manifest_id": opened.manifest_id,
        "operation_id": opened.operation_id,
        "reason": "failed",
    }
    [event] = logged(caplog, "purge_postcondition_failed")
    assert event["violations"] == [
        {"op_index": 1, "op": "row_delete", "table": "job_tombstones", "detail": "1 row named by id remains"}
    ]
    # the `purge_fences` reconciler, the backstop, finds the alert open: no occurrence more, no requeue
    assert await run_reconciler("purge_fences", engine, now=utcnow(), epoch=1) == 0
    [alert] = await purge_failed_alerts(engine, opened.manifest_id)
    assert alert.occurrences == 1
    async with engine.connect() as conn:
        assert [r.status for r in (await conn.execute(_PURGES)).all()] == ["failed"]


async def test_completion_releases_fences_and_resolves_purge_failed(
    engine: AsyncEngine, purger: Purger, fenced_kind: FencedKind
) -> None:
    """A fenced kind completes: its `fence` op's postcondition ("no active fence") holds only because the
    completing transaction releases the fence first; the manifest is `completed`, the `privacy.purge_failed`
    alert of an earlier failure resolved, and the operation `succeeded`, all in one transaction."""
    seed = await fenced_seed(engine)
    opened = await open_fenced(engine, seed)
    async with engine.begin() as conn:  # its first operation failed; the user's retry is a new one
        claim = await ops.claim_operation(conn, opened.operation_id, epoch=1)
        assert claim is not None
        await ops.finish_operation(conn, claim, status="failed", error_code="bug")  # the hook raises it
        retry, _ = await ops.create_operation(
            conn,
            kind="purge",
            payload={"manifest_id": opened.manifest_id},
            dedup_key=f"purge:{opened.manifest_uuid}",
            defer=None,
        )
    assert [a.resolved_at for a in await purge_failed_alerts(engine, opened.manifest_id)] == [None]
    await purger.run(retry)

    manifest = await manifest_of(engine, opened.manifest_id)
    assert manifest.status == "completed" and manifest.completed_at is not None
    assert manifest.scope is not None
    assert manifest.scope["operations"][0] == {
        "op": "fence",
        "kind": "jobs",
        "key": "jobs",
        "job_ids": [seed.job.job_id],
    }
    [fence] = await fence_rows(engine, opened.manifest_id)
    assert fence.released_at is not None
    [alert] = await purge_failed_alerts(engine, opened.manifest_id)
    assert alert.resolved_at is not None
    assert (await operation(engine, retry)).status == "succeeded"
    assert not await exists(engine, "job_tombstones", seed.tombstone_id)
    assert await exists(engine, "jobs", seed.job.job_id)  # the fence named it; the purge kept it
    files = FileRoots(purger.cfg.cv_store_dir, purger.cfg.exports_dir)
    async with engine.connect() as conn:
        assert await check_postconditions(conn, manifest.scope, files=files) == []


# ---- the chunk ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Grouped:
    """J1 and J2 in one active duplicate group, J3 in none."""

    j1: Job
    j2: Job
    j3: Job
    group_id: int


async def grouped_jobs(engine: AsyncEngine) -> Grouped:
    async with engine.begin() as conn:
        j1 = await insert_job(conn)
        j2 = await insert_job(conn)
        j3 = await insert_job(conn)
        group = await member_group(conn, j1, [j2])
    return Grouped(j1, j2, j3, group)


async def live_run(engine: AsyncEngine, purger: Purger, opened: OpenedPurge) -> PurgeRun:
    """A live `PurgeRun` of the opened purge, its operation claimed by epoch 1."""
    async with engine.begin() as conn:
        claim = await ops.claim_operation(conn, opened.operation_id, epoch=1)
    assert claim is not None
    unused: Any = None
    return PurgeRun(
        engine=engine,
        cfg=purger.cfg,
        now=utcnow(),
        cv_store=CvStore(purger.cfg.cv_store_dir),
        manifest=await manifest_of(engine, opened.manifest_id),
        mode="live",
        effects=LiveEffects(batches_client=unused, deadman=None),
        owner=PurgeOwner(epoch=1, claim=claim),
    )


def statement_kind(statement: str) -> str:
    """What a chunk's statement locks or writes, by its text."""
    text = " ".join(statement.split())
    if "FROM worker_heartbeat" in text and "FOR KEY SHARE" in text:
        return "epoch"
    if "FROM active_config" in text:
        return "config FOR UPDATE" if text.endswith("FOR UPDATE") else "config FOR SHARE"
    if text.startswith("SELECT id, merged_into_job_id FROM jobs") and text.endswith("FOR UPDATE"):
        return "jobs+co-members" if "duplicate_group_members" in text else "jobs"
    if text.startswith("SELECT co.job_id FROM duplicate_group_members"):
        return "co-members again"
    if "FROM operations" in text and text.endswith("FOR SHARE"):
        return "owner"
    if text.startswith("UPDATE job_tombstones"):
        return "the kind's write"
    return text


@pytest.fixture
def statements(engine: AsyncEngine) -> Iterator[list[str]]:
    """Every statement the engine's connections send while the test runs, in order."""
    sent: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        sent.append(statement)

    sa.event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield sent
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", record)


_WRITE: Final = sa.text("UPDATE job_tombstones SET user_actions = NULL WHERE id = -1")


async def test_chunk_takes_config_then_jobs_then_the_owner(
    engine: AsyncEngine, purger: Purger, fenced_kind: FencedKind, statements: list[str]
) -> None:
    """§3.3.3's order: (1) the epoch fence, (2) `active_config` FOR SHARE (FOR UPDATE for a step that
    re-points it), (4) the job rows and every co-member of `with_groups_of` FOR UPDATE in one statement, the
    co-members read again, (5) the owner row, and only then the kind's first write. A replay's chunk takes
    neither the epoch nor the owner."""
    jobs = await grouped_jobs(engine)
    opened = await open_fenced(engine, FencedSeed(jobs.j3, 0))
    run = await live_run(engine, purger, opened)

    statements.clear()
    async with run.chunk(job_ids=[jobs.j3.job_id], with_groups_of=[jobs.j1.job_id]) as chunk:
        await chunk.conn.execute(_WRITE)
    assert [statement_kind(s) for s in statements] == [
        "epoch",
        "config FOR SHARE",
        "jobs+co-members",
        "co-members again",
        "owner",
        "the kind's write",
    ]
    assert chunk.held == {jobs.j1.job_id, jobs.j2.job_id, jobs.j3.job_id}  # J2 came with J1's group

    statements.clear()
    async with run.chunk(config="update", job_ids=[jobs.j3.job_id]) as chunk:
        await chunk.conn.execute(_WRITE)
    assert [statement_kind(s) for s in statements] == [
        "epoch",
        "config FOR UPDATE",
        "jobs+co-members",
        "owner",
        "the kind's write",
    ]
    assert chunk.held == {jobs.j3.job_id}

    replay = PurgeRun(
        engine=engine,
        cfg=purger.cfg,
        now=utcnow(),
        cv_store=CvStore(purger.cfg.cv_store_dir),
        manifest=run.manifest,
        mode="replay",
        effects=None,
        owner=None,
    )
    statements.clear()
    async with replay.chunk(job_ids=[jobs.j3.job_id], with_groups_of=[jobs.j1.job_id]) as chunk:
        await chunk.conn.execute(_WRITE)
    assert [statement_kind(s) for s in statements] == [
        "config FOR SHARE",
        "jobs+co-members",
        "co-members again",
        "the kind's write",
    ]
    with pytest.raises(ValueError, match="live"):
        PurgeRun(
            engine=engine,
            cfg=purger.cfg,
            now=utcnow(),
            cv_store=CvStore(purger.cfg.cv_store_dir),
            manifest=run.manifest,
            mode="live",
            effects=None,
            owner=None,
        )


async def test_a_chunk_starts_again_when_a_co_member_joins_under_its_lock(
    engine: AsyncEngine,
    purger: Purger,
    fenced_kind: FencedKind,
    migrated: PgServer,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """J3 joins J1's group while the chunk's job statement waits for J1: the statement's snapshot never saw
    J3, so the co-members read after it find a member it does not hold; the round rolls back and the next
    one holds J3 too. A set that changes in every round gives up after `CHUNK_LOCK_ROUNDS` with
    `CoMemberChanged`, which `run_purge` requeues `co_member_changed`."""
    caplog.set_level(logging.INFO)
    jobs = await grouped_jobs(engine)
    opened = await open_fenced(engine, FencedSeed(jobs.j3, 0))
    run = await live_run(engine, purger, opened)
    held: list[frozenset[int]] = []

    async def chunk() -> None:
        async with run.chunk(with_groups_of=[jobs.j1.job_id]) as c:
            held.append(c.held)

    async with engine.connect() as blocker:
        await blocker.begin()
        await blocker.execute(sa.text("SELECT id FROM jobs WHERE id = :j FOR UPDATE"), {"j": jobs.j1.job_id})
        task = asyncio.create_task(chunk())
        await await_until(lambda: blocked_or_done(migrated, task), 10)
        assert not task.done()
        member = {"group_id": jobs.group_id, "job_id": jobs.j3.job_id, "evidence": {}}
        await insert_row(blocker, "duplicate_group_members", member)
        await blocker.commit()
    await asyncio.wait_for(task, 10)
    assert held == [{jobs.j1.job_id, jobs.j2.job_id, jobs.j3.job_id}]
    assert len(logged(caplog, "purge_chunk_set_changed")) == 1

    async def one_more_every_time(conn: AsyncConnection, job_ids: Any) -> frozenset[int]:
        return frozenset({*job_ids, 2**62})  # a member no round holds

    monkeypatch.setattr(merge, "co_members", one_more_every_time)  # the module `purge` calls
    with pytest.raises(CoMemberChanged):
        async with run.chunk(with_groups_of=[jobs.j1.job_id]):
            pass
    assert len(logged(caplog, "purge_chunk_set_changed")) == 1 + purge.CHUNK_LOCK_ROUNDS

    seed = await fenced_seed(engine)
    other = await open_fenced(engine, seed)
    await purger.run(other.operation_id)
    row = await operation(engine, other.operation_id)
    assert (row.status, row.attempts, row.last_error_code) == ("pending", 1, "co_member_changed")
    assert await exists(engine, "job_tombstones", seed.tombstone_id)


# ---- phase B -----------------------------------------------------------------------------------------------


async def test_a_lost_deadlock_reruns_the_phase_then_requeues_lock_conflict(
    engine: AsyncEngine, purger: Purger, fenced_kind: FencedKind, caplog: pytest.LogCaptureFixture
) -> None:
    """Ruling T12-1: a 40P01 in the first try of a chunk re-runs phase B from its start and the purge
    completes; one in every try (`PHASE_TRIES` in all) requeues the operation `lock_conflict` a minute later,
    an attempt spent, never `bug`."""
    fenced_kind.deadlocks = 1
    seed = await fenced_seed(engine)
    opened = await open_fenced(engine, seed)
    await purger.run(opened.operation_id)
    assert (await operation(engine, opened.operation_id)).status == "succeeded"
    assert len(fenced_kind.chunks) == 2 and not await exists(engine, "job_tombstones", seed.tombstone_id)
    assert [e["tries"] for e in logged(caplog, "purge_phase_lost_a_lock")] == [1]

    fenced_kind.deadlocks = purge.PHASE_TRIES
    seed = await fenced_seed(engine)
    opened = await open_fenced(engine, seed)
    before = utcnow()
    await purger.run(opened.operation_id)
    row = await operation(engine, opened.operation_id)
    assert (row.status, row.attempts, row.last_error_code) == ("pending", 1, "lock_conflict")
    assert row.not_before >= before + purge.RETRY_AFTER
    assert await exists(engine, "job_tombstones", seed.tombstone_id)
    assert (await manifest_of(engine, opened.manifest_id)).status == "executing"
    assert [f.released_at for f in await fence_rows(engine, opened.manifest_id)] == [None]


@pytest.mark.parametrize("kind", ["golden_case_delete", "retention_purge"])
async def test_every_kind_but_tombstone_purges_exports(
    engine: AsyncEngine, purger: Purger, fenced_kind: FencedKind, kind: str
) -> None:
    """M5-R3, Deviation 18: the `running` and `ready` exports at A2 end `purged` with no `storage_path`,
    and their files (`<id>-*`: a ready archive, a running one's `.tmp`) are gone after that commit; a file of
    another export whose id begins with the same digits stays."""
    assert sorted(k for k, proc in KINDS.items() if not proc.deletes_exports and k != "job_tombstone") == []
    exports = purger.cfg.exports_dir
    ready, ready_file = await an_export(engine, exports, "ready")
    running, running_file = await an_export(engine, exports, "running")
    decoy = exports / f"{ready}0-{uuid.uuid4()}.jhx"  # the archive of export `<ready>0`, no row of ours
    decoy.write_bytes(b"\x00")
    if kind == "golden_case_delete":
        opened = await open_golden(engine, await golden_case(engine))
    else:
        opened = await open_fenced(engine, await fenced_seed(engine))
    await purger.run(opened.operation_id)

    manifest = await manifest_of(engine, opened.manifest_id)
    assert manifest.status == "completed" and manifest.scope is not None
    assert manifest.scope["operations"][-2:] == [
        {
            "op": "field_redact",
            "table": "exports",
            "ids": [ready, running],
            "columns": ["storage_path"],
            "marker": {"status": "purged"},
        },
        {"op": "file_remove", "volume": "exports", "keys": [f"{ready}-*", f"{running}-*"]},
    ]
    assert [await export_row(engine, i) for i in (ready, running)] == [("purged", None)] * 2
    assert not ready_file.exists() and not running_file.exists() and decoy.exists()


async def test_an_export_key_names_every_file_its_id_prefixes(engine: AsyncEngine, purger: Purger) -> None:
    """The `file_remove` postcondition reads an export's key `<id>-*` as the files whose name begins with
    `<id>-` (`manifests.named_files`): a file left of export 5 fails it, one of export 50 does not; a key with
    a wildcard anywhere else is no file name."""
    exports = purger.cfg.exports_dir
    manifest = await manifest_of(engine, (await open_golden(engine, await golden_case(engine))).manifest_id)
    content = new_content(
        manifest,
        max_ids={},
        operations=[{"op": "file_remove", "volume": "exports", "keys": ["5-*", "a*b"]}],
    )
    files = FileRoots(purger.cfg.cv_store_dir, exports)
    (exports / "50-x.jhx").write_bytes(b"\x00")
    (exports / "5-y.jhx.tmp").write_bytes(b"\x00")
    async with engine.connect() as conn:
        assert [v.detail for v in await check_postconditions(conn, content, files=files)] == [
            "1 key on exports is no file name",
            "1 file on exports remains",
        ]
    (exports / "5-y.jhx.tmp").unlink()
    async with engine.connect() as conn:
        found = await check_postconditions(conn, content, files=files)
    assert [v.detail for v in found] == ["1 key on exports is no file name"]
    assert (exports / "50-x.jhx").exists()


async def test_a_purge_meeting_a_row_already_content_free_and_a_file_left_behind(
    engine: AsyncEngine, purger: Purger
) -> None:
    """The export ops are idempotent (`execute`'s file rule, Task 17's table): a named export that became
    `failed` before phase B is left as it is (its files still removed), and a file a crash left after the
    rows' commit (no row names it any more) is removed by the re-execution."""
    ready, ready_file = await an_export(engine, purger.cfg.exports_dir, "ready")
    running, running_file = await an_export(engine, purger.cfg.exports_dir, "running")
    async with engine.begin() as conn:
        operations = await export_ops(conn, max_export_id=max(ready, running))
        await transition(conn, "exports", running, ["running"], "failed")  # its failure hook, after A2
    run = PurgeRun(
        engine=engine,
        cfg=purger.cfg,
        now=utcnow(),
        cv_store=CvStore(purger.cfg.cv_store_dir),
        manifest=await manifest_of(
            engine, (await open_golden(engine, await golden_case(engine))).manifest_id
        ),
        mode="replay",
        effects=None,
        owner=None,
    )
    assert await deletion_graph.purge_exports(run, operations) == 1
    assert [await export_row(engine, i) for i in (ready, running)] == [("purged", None), ("failed", None)]
    assert not ready_file.exists() and not running_file.exists()
    ready_file.write_bytes(b"\x00")  # as if the unlink after the commit had never run
    assert await deletion_graph.purge_exports(run, operations) == 0  # the rows are left as they are
    assert not ready_file.exists()


async def test_one_purge_at_a_time(engine: AsyncEngine, queue_app: procrastinate.App) -> None:
    """D5: every purge's job runs under the procrastinate lock `purge`, so execution follows the stream."""
    assert ops.OPERATION_LOCKS["purge"] == "purge"
    first = await open_golden(engine, await golden_case(engine), defer=defer_in_tx)
    second = await open_golden(engine, await golden_case(engine), defer=defer_in_tx)
    async with engine.begin() as conn:
        jobs = (
            await conn.execute(
                sa.text("SELECT lock, args FROM procrastinate_jobs WHERE task_name = :task ORDER BY id"),
                {"task": job_task_name("run_operation")},
            )
        ).all()
        assert [(job.lock, job.args) for job in jobs] == [
            ("purge", {"operation_id": first.operation_id}),
            ("purge", {"operation_id": second.operation_id}),
        ]
        worker = (
            await conn.execute(sa.text("SELECT worker_id FROM procrastinate_register_worker_v1()"))
        ).scalar_one()
        fetch = sa.text("SELECT id FROM procrastinate_fetch_job_v2(NULL, :w)")
        assert (await conn.execute(fetch, {"w": worker})).scalar_one_or_none() is not None
        assert (await conn.execute(fetch, {"w": worker})).scalar_one_or_none() is None  # the second waits


async def test_a_replacement_operation_computes_from_the_rows_request(
    engine: AsyncEngine, purger: Purger, queue_app: procrastinate.App
) -> None:
    """Deviation 53: a golden-case delete has no fence naming its case, so its content comes from
    `deletion_manifests.request`. Its operation deleted after A0, the `purge_fences` reconciler queues a new
    one (payload `{manifest_id}` only), which computes the content from the row and completes."""
    case_id = await golden_case(engine)
    opened = await open_golden(engine, case_id)
    async with engine.begin() as conn:
        await conn.execute(sa.text("DELETE FROM operations WHERE id = :i"), {"i": opened.operation_id})
    assert (
        await run_reconciler("purge_fences", engine, now=utcnow(), epoch=1) == 2
    )  # the operation, the alert
    async with engine.connect() as conn:
        [replacement] = (await conn.execute(_PURGES)).all()
    assert (replacement.payload, replacement.dedup_key) == (
        {"manifest_id": opened.manifest_id},
        f"purge:{opened.manifest_uuid}",
    )
    await purger.run(int(replacement.id))
    manifest = await manifest_of(engine, opened.manifest_id)
    assert manifest.status == "completed" and manifest.scope is not None
    assert manifest.scope["operations"][0] == {
        "op": "row_delete",
        "table": "golden_cases",
        "ids": [case_id],
        "bound": {"by": "id"},
    }
    assert not await exists(engine, "golden_cases", case_id)
    [alert] = await purge_failed_alerts(engine, opened.manifest_id)
    assert alert.resolved_at is not None  # the requeue's alert, resolved by the completion


# ---- the golden kind ---------------------------------------------------------------------------------------


async def running_golden_run(engine: AsyncEngine) -> int:
    async with engine.begin() as conn:
        return int((await conn.execute(_GOLDEN_RUN)).scalar_one())


async def dispatched_call(engine: AsyncEngine, eval_run_id: int, case_id: int) -> int:
    """A generator call of the run for the case, dispatched (in flight); its ledger entry's id."""
    owner = EvalRunOwner(eval_run_id)
    now = utcnow()
    kw = reserve_kw(
        owner,
        now=now,
        bound=4000,
        max_tokens=1000,
        purpose="generate",
        model="claude-sonnet-5-5",
        deadline_s=600,
        golden_case_id=case_id,
    )
    async with engine.begin() as conn:
        reservation = await reserve(conn, **kw)
        await mark_dispatched(conn, reservation, owner, now=now)
    return reservation.ledger_id


async def test_golden_delete_refused_while_a_running_eval_includes_it(
    engine: AsyncEngine, purger: Purger, eval_owner: EvalRunOwner
) -> None:
    """While a running eval run includes the case the request is refused (409 `conflict_state`, detail
    `eval_run_running`), nothing opened. A run that starts after the request makes phase B wait 5 min
    without spending an attempt (an eval run has its own end); once it has ended, its in-flight entry is
    settled (`ambiguous`) before the case goes with its calls."""
    case_id = await golden_case(engine)
    run_id = await running_golden_run(engine)  # a golden run includes every active case
    manifests = await count(engine, "deletion_manifests")
    with pytest.raises(Refusal) as refused:
        await open_golden(engine, case_id)
    assert (refused.value.status, refused.value.code, refused.value.detail) == (
        409,
        "conflict_state",
        "eval_run_running",
    )
    assert await count(engine, "deletion_manifests") == manifests
    async with engine.begin() as conn:
        await conn.execute(_END_RUN, {"r": run_id})

    opened = await open_golden(engine, case_id)
    run_id = await running_golden_run(engine)
    ledger_id = await dispatched_call(engine, run_id, case_id)
    before = utcnow()
    await purger.run(opened.operation_id)
    row = await operation(engine, opened.operation_id)
    assert (row.status, row.attempts, row.last_error_code) == ("pending", 0, "eval_run_running")
    assert before + timedelta(minutes=5) <= row.not_before <= utcnow() + timedelta(minutes=5)
    assert await exists(engine, "golden_cases", case_id)
    assert (await ledger_row(engine, ledger_id)).state == "dispatched"
    assert (await manifest_of(engine, opened.manifest_id)).file_sha256 is not None  # A3 came first

    async with engine.begin() as conn:
        await conn.execute(_END_RUN, {"r": run_id})  # the run failed; its call's entry is still dispatched
    await purger.again(opened.operation_id)
    assert (await operation(engine, opened.operation_id)).status == "succeeded"
    assert not await exists(engine, "golden_cases", case_id)
    async with engine.connect() as conn:
        assert (await conn.execute(_CASE_CALLS, {"g": case_id})).scalar_one() == 0  # gone with the case
        # settled before its call went: a later reader joins the call, so it could never be settled after
        assert (await conn.execute(_ENTRY_STATE, {"i": ledger_id})).scalar_one() == "ambiguous"


async def test_request_golden_case_delete_is_refused_under_a_global_fence(engine: AsyncEngine) -> None:
    """Task 8: right after `active_config FOR UPDATE` the request meets delete-everything's fence: 409
    `delete_everything_in_progress`, nothing opened. A case that does not exist is a `LookupError`."""
    case_id = await golden_case(engine)
    with pytest.raises(LookupError):
        await open_golden(engine, 2**62)
    async with engine.begin() as conn:
        await insert_test_fence(conn, kind="global")
    manifests = await count(engine, "deletion_manifests")
    with pytest.raises(Refusal) as refused:
        await open_golden(engine, case_id)
    assert (refused.value.status, refused.value.code) == (409, "delete_everything_in_progress")
    assert await count(engine, "deletion_manifests") == manifests
    assert await count(engine, "operations") == 0
    assert await exists(engine, "golden_cases", case_id)


# ---- adoption ----------------------------------------------------------------------------------------------


def other_kind(data: bytes) -> bytes:
    """The same content as another kind's, in canonical bytes."""
    changed = dict(parse_manifest(data, file_name=f"{json.loads(data)['manifest_uuid']}.json"))
    changed["kind"] = "job_tombstone"
    return canonical_bytes(cast(ManifestContent, changed))


def not_canonical(data: bytes) -> bytes:
    """The same content, parsed alike, in other bytes than `canonical_bytes`."""
    return data.replace(b",", b", ")


@pytest.mark.parametrize(
    "rewrite",
    [lambda data: b"{not json", other_kind, not_canonical],
    ids=["not_json", "another_kind", "not_canonical"],
)
async def test_a_file_that_cannot_be_adopted_fails_manifest_invalid_and_keeps_the_fence(
    engine: AsyncEngine,
    purger: Purger,
    fenced_kind: FencedKind,
    monkeypatch: pytest.MonkeyPatch,
    rewrite: Any,
) -> None:
    """A file of the manifest's uuid that a crash left before A3 is adopted only when it parses with the
    row's uuid, kind and format 1, in its canonical bytes (the hash `record_manifest` stores); anything else
    fails the operation `manifest_invalid` at once, nothing recorded, deleted or released."""
    seed = await fenced_seed(engine)
    opened = await open_fenced(engine, seed)
    monkeypatch.setattr(purge, "afault_point", raising_at(purge.AFTER_FILE))
    await purger.run(opened.operation_id)
    path = purger.file(opened.manifest_uuid)
    path.write_bytes(rewrite(path.read_bytes()))
    monkeypatch.setattr(purge, "afault_point", no_fault)
    await purger.again(opened.operation_id)

    row = await operation(engine, opened.operation_id)
    assert (row.status, row.last_error_code) == ("failed", "manifest_invalid")
    manifest = await manifest_of(engine, opened.manifest_id)
    assert (manifest.status, manifest.file_sha256) == ("executing", None)
    assert [f.released_at for f in await fence_rows(engine, opened.manifest_id)] == [None]
    assert await exists(engine, "job_tombstones", seed.tombstone_id)
    assert [a.resolved_at for a in await purge_failed_alerts(engine, opened.manifest_id)] == [None]


async def test_a_manifest_no_longer_executing_ends_succeeded(engine: AsyncEngine, purger: Purger) -> None:
    """Step 1: a purge operation of a manifest that already completed (a pending retry that replay
    completed, say) ends `succeeded` at once, writing nothing else."""
    case_id = await golden_case(engine)
    opened = await open_golden(engine, case_id)
    await purger.run(opened.operation_id)
    async with engine.begin() as conn:
        again, _ = await ops.create_operation(
            conn,
            kind="purge",
            payload={"manifest_id": opened.manifest_id},
            dedup_key=f"purge:{opened.manifest_uuid}",
            defer=None,
        )
    completed = await manifest_of(engine, opened.manifest_id)
    await purger.run(again)
    assert (await operation(engine, again)).status == "succeeded"
    assert await manifest_of(engine, opened.manifest_id) == completed
