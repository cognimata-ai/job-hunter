"""The user's data export (spec §20.3, §20.2, §19.5; plan M5 Task 17, D9, Deviations 18, 19, 48, 52 and 55,
Ruling T17-1): `privacy.export`.

`request_export` runs in the test's transaction; the `export` operation is run as the worker runs it
(`operations.execute_operation`, epoch 1) with the purge harness's dependencies (`tests.support.purges`: the
worker's configuration, its exports directory and the CV store), its clock replaced where a test moves time.
The exports volume the failure hook and the retention rule use is configured from the same configuration
(`export.configure`). A purge is the golden-case deletion (fence-free) or a test fence
(`tests.support.fences`); a crash is `tests.support.faults.raising_at` in place of a module's `afault_point`,
a state between two steps a `PausePoint`. Nothing sleeps to move time: the handler's `now` and the retention
pass's `now` are given. Every datum is invented.
"""

import asyncio
import dataclasses
import hashlib
import io
import json
import os
import uuid
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter import crypto
from jobhunter.clock import utcnow
from jobhunter.db.snapshot import read_snapshot
from jobhunter.db.transitions import transition
from jobhunter.errors import Refusal
from jobhunter.graphs.checkpointer import open_saver
from jobhunter.privacy import export, purge
from jobhunter.privacy.export import (
    AFTER_RENAME,
    AFTER_SNAPSHOT,
    AFTER_TMP,
    BEFORE_RENAME,
    BEFORE_TMP,
    EXPORT_FILES,
    EXPORT_TTL,
    EXPORT_WAIT_LIMIT,
    iter_decrypted,
    request_export,
    write_archive,
)
from jobhunter.privacy.kinds.golden_case import request_golden_case_delete
from jobhunter.privacy.purge import OpenedPurge
from jobhunter.privacy.retention import run_retention_daily
from jobhunter.worker import operations as ops
from jobhunter.worker.operations import OperationDeps
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.faults import PausePoint, PausePoints, no_fault, raising_at
from tests.support.fences import insert_test_fence, insert_test_manifest
from tests.support.logs import logged
from tests.support.m2 import insert_profile_chain
from tests.support.m3 import insert_golden_case, insert_job
from tests.support.pg import PgServer, blocked_or_done
from tests.support.privacy_seed import seed_two_year_old_graph
from tests.support.procs import await_until
from tests.support.purges import Purger, completed, manifest_of
from tests.support.scoring import fence_jobs

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("current_epoch")]

AFTER_RECORD: Final = "privacy.purge.after_record"  # the purge's A1-A3 done, nothing deleted yet
_DUE: Final = sa.text("UPDATE operations SET not_before = NULL WHERE id = :i")
_EXPORT_OPERATION: Final = sa.text(
    "SELECT id FROM operations WHERE kind = 'export' "
    "AND payload -> 'export_id' = to_jsonb(CAST(:e AS bigint))"
)
_EXPORT: Final = sa.text(
    "SELECT status, storage_path, sha256, size_bytes, purge_generation, created_at, expires_at "
    "FROM exports WHERE id = :e"
)
_OPERATION: Final = sa.text(
    "SELECT status, attempts, last_error_code, not_before FROM operations WHERE id = :i"
)
_COUNT_EXPORTS: Final = sa.text("SELECT count(*) FROM exports")
_COUNT_EXPORT_OPERATIONS: Final = sa.text("SELECT count(*) FROM operations WHERE kind = 'export'")
_MAX_MANIFEST: Final = sa.text("SELECT coalesce(max(id), 0) FROM deletion_manifests")
_FAILED_ALERT: Final = sa.text(
    "SELECT count(*) FROM alerts WHERE code = 'operation.failed' AND dedup_key = :k AND resolved_at IS NULL"
)
_CV_ROW: Final = sa.text(
    "INSERT INTO cv_files (sha256, mime, size_bytes, storage_path, extraction_method) "
    "VALUES (:sha, :mime, :size, :path, :method) RETURNING id"
)
_SEAL_TEXT: Final = sa.text("UPDATE cv_files SET raw_text_enc = :enc WHERE id = :i")
_NOTE: Final = sa.text(
    "INSERT INTO user_job_actions (job_id, saved_at, note) VALUES (:j, now(), :note) "
    "ON CONFLICT (job_id) DO UPDATE SET note = EXCLUDED.note"
)
_LOCK_CONFIG: Final = sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE")
_FENCE_OF: Final = sa.text(
    "INSERT INTO purge_fences (kind, job_ids, manifest_id) VALUES ('jobs', CAST(:jobs AS bigint[]), :m)"
)
# What each JSON file of the archive lists, by id, as the database holds it (none of them fenced).
_EXPECTED_IDS: Final = {
    "profile_versions.json": "SELECT id FROM profile_versions WHERE profile_json IS NOT NULL ORDER BY id",
    "settings_versions.json": "SELECT id FROM settings_versions ORDER BY id",
    "jobs.json": "SELECT id FROM jobs ORDER BY id",
    "scores.json": "SELECT id FROM llm_results ORDER BY id",
    "feedback.json": "SELECT id FROM score_feedback ORDER BY id",
    "golden_cases.json": "SELECT id FROM golden_cases ORDER BY id",
    "runs.json": "SELECT id FROM runs ORDER BY id",
}


# ---- the harness -------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Exporter:
    """Runs an `export` operation as the worker's `run_operation` does, with the worker's dependencies."""

    engine: AsyncEngine
    deps: OperationDeps

    @property
    def volume(self) -> Path:
        return self.deps.cfg.exports_dir

    async def run(self, operation_id: int, *, now: datetime | None = None) -> None:
        """Execute the operation, its handler's clock at `now` when given."""
        deps = self.deps if now is None else dataclasses.replace(self.deps, clock=lambda: now)
        await ops.execute_operation(self.engine, operation_id, epoch=1, deps=deps)

    async def again(self, operation_id: int, *, now: datetime | None = None) -> None:
        """The waiting or requeued operation made due at once, then run."""
        async with self.engine.begin() as conn:
            await conn.execute(_DUE, {"i": operation_id})
        await self.run(operation_id, now=now)

    def files(self, export_id: int) -> list[str]:
        """The names of the exports directory that the export's key `<id>-*` names."""
        return sorted(name for name in os.listdir(self.volume) if name.startswith(f"{export_id}-"))

    def archive(self, export_id: int, storage_path: str) -> zipfile.ZipFile:
        """The stored archive decrypted and opened."""
        keys = crypto.data_keys(self.deps.cfg)
        with (self.volume / storage_path).open("rb") as stored:
            plain = b"".join(iter_decrypted(stored, keys, export_id=export_id))
        return zipfile.ZipFile(io.BytesIO(plain))


@pytest.fixture
def exporter(engine: AsyncEngine, purger: Purger, monkeypatch: pytest.MonkeyPatch) -> Exporter:
    """The worker's export runner; the exports volume configured from the same configuration, as the worker's
    start does (restored after the test)."""
    monkeypatch.setattr(export, "_volume", None)
    export.configure(purger.cfg)
    return Exporter(engine, purger.deps)


async def requested(engine: AsyncEngine, *, now: datetime | None = None) -> tuple[int, int]:
    """A new export requested at `now` (the clock's by default): its id and its operation's."""
    async with engine.begin() as conn:
        export_id = await request_export(conn, now=utcnow() if now is None else now, defer=None)
        operation_id = int((await conn.execute(_EXPORT_OPERATION, {"e": export_id})).scalar_one())
    return export_id, operation_id


async def export_of(engine: AsyncEngine, export_id: int) -> Row[Any] | None:
    async with engine.connect() as conn:
        return (await conn.execute(_EXPORT, {"e": export_id})).first()


async def status_of(engine: AsyncEngine, export_id: int) -> str | None:
    row = await export_of(engine, export_id)
    return None if row is None else str(row.status)


async def operation_of(engine: AsyncEngine, operation_id: int) -> Row[Any]:
    async with engine.connect() as conn:
        return (await conn.execute(_OPERATION, {"i": operation_id})).one()


async def scalar(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(statement, params)).scalar_one()


async def golden_purge(engine: AsyncEngine) -> OpenedPurge:
    """A golden-case deletion opened (A0) over a new case: its manifest `executing`, no fence."""
    async with engine.begin() as conn:
        job = await insert_job(conn)
        chain = await insert_profile_chain(conn, uuid.uuid4().int % 10**6 + 10)
        case_id = await insert_golden_case(conn, job, chain.profile_version_id)
    async with engine.begin() as conn:
        return await request_golden_case_delete(conn, case_id, now=utcnow(), defer=None)


async def recorded(purger: Purger, opened: OpenedPurge, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The opened purge run up to its recorded content (A1-A3) and stopped there, nothing deleted: the export
    ids its export ops name."""
    monkeypatch.setattr(purge, "afault_point", raising_at(AFTER_RECORD))
    await purger.run(opened.operation_id)
    monkeypatch.setattr(purge, "afault_point", no_fault)
    manifest = await manifest_of(purger.engine, opened.manifest_id)
    assert manifest.status == "executing" and manifest.scope is not None
    operations: list[Any] = list(manifest.scope["operations"])
    return [
        i for op in operations if op["op"] == "field_redact" and op["table"] == "exports" for i in op["ids"]
    ]


async def retention_at(
    engine: AsyncEngine, queue_app: procrastinate.App, migrated: PgServer, now: datetime, exporter: Exporter
) -> dict[str, int]:
    """One `retention_daily` pass at `now` by worker epoch 1."""
    async with open_saver(migrated.dsn("app")) as saver:
        return await run_retention_daily(
            engine, queue_app, saver, cv_store=exporter.deps.cv_store, now=now, epoch=1
        )


async def cv_originals(engine: AsyncEngine, deps: OperationDeps) -> dict[str, bytes]:
    """An uploaded CV file (in the CV store, sealed) and a pasted CV (its sealed text, no file), each in a
    `cv_files` row of its own: their names in the archive and their plaintexts."""
    keys = crypto.data_keys(deps.cfg)
    pdf = b"%PDF-1.7\n% erfundener Lebenslauf\n" + os.urandom(256)
    stored = deps.cv_store.write(pdf, keys)
    pasted = "Erfundener Lebenslauf: zehn Jahre Datenbanken und Datenschutz.\n" * 8
    async with engine.begin() as conn:
        params = {"mime": "application/pdf", "size": len(pdf), "path": stored.storage_path, "method": "pypdf"}
        pdf_id = int((await conn.execute(_CV_ROW, {"sha": stored.sha256, **params})).scalar_one())
        text = pasted.encode()
        params = {"mime": "text/plain", "size": len(text), "path": None, "method": "pasted"}
        paste_id = int(
            (await conn.execute(_CV_ROW, {"sha": hashlib.sha256(text).digest(), **params})).scalar_one()
        )
        sealed = deps.cv_store.encrypt_text(pasted, keys, cv_file_id=paste_id)
        await conn.execute(_SEAL_TEXT, {"enc": sealed, "i": paste_id})
    return {f"cv_files/{pdf_id}.pdf": pdf, f"cv_files/{paste_id}.txt": text}


def listed(archive: zipfile.ZipFile, name: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = json.loads(archive.read(name))
    return items


# ---- the archive -------------------------------------------------------------------------------------------


async def test_an_export_holds_every_file_and_decrypts(engine: AsyncEngine, exporter: Exporter) -> None:
    """§20.3 over the two-year-old seed (every table holds a row) and two CVs of its own: the archive holds
    every file of `EXPORT_FILES`, the `cv_files/` directory with each original that still exists (the seed's
    rows name files that do not, and are left out), and each JSON file lists exactly the database's rows. The
    row is `ready` with its stored file's sha256, the zip's size, the purge generation its snapshot saw and
    `expires_at = created_at + 7 d`."""
    async with engine.begin() as conn:
        seed = await seed_two_year_old_graph(conn, now=utcnow())
    originals = await cv_originals(engine, exporter.deps)
    export_id, operation_id = await requested(engine)
    await exporter.run(operation_id)

    assert (await operation_of(engine, operation_id)).status == "succeeded"
    row = await export_of(engine, export_id)
    assert row is not None and row.status == "ready"
    assert exporter.files(export_id) == [row.storage_path]
    assert row.storage_path.startswith(f"{export_id}-") and row.storage_path.endswith(".jhx")
    stored = (exporter.volume / row.storage_path).read_bytes()
    assert row.sha256 == hashlib.sha256(stored).digest()
    assert row.purge_generation == await scalar(engine, _MAX_MANIFEST) and row.purge_generation > 0
    assert row.expires_at - row.created_at == EXPORT_TTL
    archive = exporter.archive(export_id, row.storage_path)
    keys = crypto.data_keys(exporter.deps.cfg)
    with (exporter.volume / row.storage_path).open("rb") as source:
        assert row.size_bytes == sum(
            len(chunk) for chunk in iter_decrypted(source, keys, export_id=export_id)
        )

    assert set(archive.namelist()) == {*EXPORT_FILES, "cv_files/", *originals}
    for name, plain in originals.items():
        assert archive.read(name) == plain
    async with engine.connect() as conn:
        for name, sql in _EXPECTED_IDS.items():
            expected = [int(i) for i in (await conn.execute(sa.text(sql))).scalars()]
            assert expected and [item["id"] for item in listed(archive, name)] == expected, name
        profiles: dict[int, Any] = {
            int(r.id): r.profile_json
            for r in await conn.execute(sa.text("SELECT id, profile_json FROM profile_versions"))
        }
        note = (
            await conn.execute(
                sa.text("SELECT note FROM user_job_actions WHERE job_id = :j"), {"j": seed.closed_job_id}
            )
        ).scalar_one()
    assert all(item["profile"] == profiles[item["id"]] for item in listed(archive, "profile_versions.json"))
    [closed] = [item for item in listed(archive, "jobs.json") if item["id"] == seed.closed_job_id]
    assert note and closed["note"] == note and closed["user_actions"] is not None
    scores = listed(archive, "scores.json")
    assert any(item["evidence"] and item["scores"] and item["critic_notes"] for item in scores)
    assert all(
        set(item["versions"]) >= {"profile_version_id", "rubric_version_id", "model_policy_id"}
        for item in scores
    )
    assert any(item["report"] is not None for item in listed(archive, "runs.json"))
    readme = archive.read("README.txt").decode()
    assert "7 days" in readme and "PRIVACY.md" in readme and all(name in readme for name in EXPORT_FILES)


async def test_a_fenced_job_is_absent_from_the_export(engine: AsyncEngine, exporter: Exporter) -> None:
    """L6560: no export reads a fenced job. The snapshot's archive leaves a job an active fence matches out of
    every file that names jobs (its row, its results, its feedback, its golden cases), and keeps the others.
    The handler never reaches the archive while a fence's manifest is `executing` (it waits), so the archive
    is checked here, in a snapshot taken under the fence."""
    async with engine.begin() as conn:
        seed = await seed_two_year_old_graph(conn, now=utcnow())
    fenced = seed.closed_job_id
    keys = crypto.data_keys(exporter.deps.cfg)

    async def snapshot_archive() -> zipfile.ZipFile:
        sink = io.BytesIO()
        async with read_snapshot(engine) as snap:
            await write_archive(snap, sink, export_id=1, cv_store=exporter.deps.cv_store, keys=keys)
        return zipfile.ZipFile(io.BytesIO(sink.getvalue()))

    def naming(archive: zipfile.ZipFile) -> dict[str, set[int]]:
        return {
            "jobs.json": {item["id"] for item in listed(archive, "jobs.json")},
            "scores.json": {item["job_id"] for item in listed(archive, "scores.json")},
            "feedback.json": {item["job_id"] for item in listed(archive, "feedback.json")},
            "golden_cases.json": {item["job_id"] for item in listed(archive, "golden_cases.json")},
        }

    before = naming(await snapshot_archive())
    assert all(fenced in jobs for jobs in before.values())  # the job holds a row in each file
    async with engine.begin() as conn:
        await fence_jobs(conn, fenced)
    after = naming(await snapshot_archive())
    assert after == {name: jobs - {fenced} for name, jobs in before.items()}
    assert after["jobs.json"]  # the other jobs stay


async def test_the_stored_file_is_never_plaintext(engine: AsyncEngine, exporter: Exporter) -> None:
    """§20.3: the zip exists only encrypted at rest. The stored file holds no zip signature and none of the
    content the decrypted archive shows."""
    note = "Erfundene Notiz: Gespräch am Montag, Gehalt verhandeln."
    async with engine.begin() as conn:
        job = await insert_job(conn)
        await conn.execute(_NOTE, {"j": job.job_id, "note": note})
    originals = await cv_originals(engine, exporter.deps)
    export_id, operation_id = await requested(engine)
    await exporter.run(operation_id)
    row = await export_of(engine, export_id)
    assert row is not None and row.status == "ready"
    stored = (exporter.volume / row.storage_path).read_bytes()
    archive = exporter.archive(export_id, row.storage_path)
    assert [item["note"] for item in listed(archive, "jobs.json")] == [note]
    for marker in (b"PK\x03\x04", b"PK\x05\x06", note.encode(), b"README", *originals.values()):
        assert marker not in stored
    assert os.stat(exporter.volume / row.storage_path).st_mode & 0o777 == 0o600


# ---- the request -------------------------------------------------------------------------------------------


async def test_one_export_at_a_time(engine: AsyncEngine, exporter: Exporter) -> None:
    """D9: a second request while one export runs → 409 `conflict_state` (`export_running`), from the
    unique index; once it is `ready`, the next request is accepted."""
    first, operation_id = await requested(engine)
    with pytest.raises(Refusal) as refused:
        await requested(engine)
    assert (refused.value.status, refused.value.code, refused.value.detail) == (
        409,
        "conflict_state",
        "export_running",
    )
    assert (await scalar(engine, _COUNT_EXPORTS), await scalar(engine, _COUNT_EXPORT_OPERATIONS)) == (1, 1)
    await exporter.run(operation_id)
    assert await status_of(engine, first) == "ready"
    second, _ = await requested(engine)
    assert second > first


async def test_a_request_waits_for_an_earlier_exports_live_operation(
    engine: AsyncEngine, exporter: Exporter
) -> None:
    """The operation's dedup key is `export`: an earlier export that a purge ended while its operation still
    waits holds that key, so a new request is refused 409 (`export_running`) rather than left without an
    operation of its own; once that operation has run (it finds its export purged and ends `succeeded`), the
    next request is accepted."""
    first, operation_id = await requested(engine)
    async with engine.begin() as conn:
        await transition(conn, "exports", first, ["running"], "purged")  # as a purge's phase B moves it
    with pytest.raises(Refusal) as refused:
        await requested(engine)
    assert (refused.value.code, refused.value.detail) == ("conflict_state", "export_running")
    assert await scalar(engine, _COUNT_EXPORTS) == 1
    await exporter.run(operation_id)
    assert (await operation_of(engine, operation_id)).status == "succeeded"
    second, _ = await requested(engine)
    assert second > first


async def test_request_export_is_refused_under_a_global_fence(engine: AsyncEngine) -> None:
    """Task 8: while delete-everything's fence stands, the request is refused 409
    `delete_everything_in_progress`, and writes nothing."""
    async with engine.begin() as conn:
        await insert_test_fence(conn, kind="global")
    with pytest.raises(Refusal) as refused:
        await requested(engine)
    assert (refused.value.status, refused.value.code) == (409, "delete_everything_in_progress")
    assert (await scalar(engine, _COUNT_EXPORTS), await scalar(engine, _COUNT_EXPORT_OPERATIONS)) == (0, 0)


async def test_the_request_waits_for_a_fence_committing_under_active_config_and_is_refused(
    engine: AsyncEngine, migrated: PgServer
) -> None:
    """§20.3 L6508, the request's side (fix round 1, M3): delete-everything commits its `global` fence under
    `active_config FOR UPDATE`. A request that arrives while that transaction is open waits on its
    `active_config FOR SHARE`, reads the fence once it has committed, and is refused 409
    `delete_everything_in_progress`, writing nothing."""
    async with engine.connect() as holder:
        await holder.begin()
        await holder.execute(_LOCK_CONFIG)
        await insert_test_fence(holder, kind="global")
        requesting = asyncio.create_task(requested(engine))
        await await_until(lambda: blocked_or_done(migrated, requesting), timeout_s=30)
        assert not requesting.done()  # a lock wait, on `active_config`
        await holder.commit()
    with pytest.raises(Refusal) as refused:
        await asyncio.wait_for(requesting, 30)
    assert (refused.value.status, refused.value.code) == (409, "delete_everything_in_progress")
    assert (await scalar(engine, _COUNT_EXPORTS), await scalar(engine, _COUNT_EXPORT_OPERATIONS)) == (0, 0)


# ---- purges and fences -------------------------------------------------------------------------------------


async def test_an_export_waits_while_a_purge_is_executing(
    engine: AsyncEngine, exporter: Exporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deviations 19 and 52: a golden-case deletion opened and not completed (its manifest `executing`, no
    fence): every execution reads no content, writes no file and gives the operation back `pending` until a
    minute later with `purge_in_progress`, spending no attempt, however many minutes pass (more executions
    than `MAX_OPERATION_ATTEMPTS`)."""
    await golden_purge(engine)
    read: list[int] = []

    async def unread(*args: Any, **kwargs: Any) -> None:
        read.append(1)

    monkeypatch.setattr(export, "write_archive", unread)
    t0 = utcnow()
    export_id, operation_id = await requested(engine, now=t0)
    for minutes in (0, 1, 2, 5, 30, 60, 120, 240, 480, 720, 1000, 1200, 1439):
        at = t0 + timedelta(minutes=minutes)
        await exporter.again(operation_id, now=at)
        row = await operation_of(engine, operation_id)
        assert (row.status, row.attempts, row.last_error_code, row.not_before) == (
            "pending",
            0,
            "purge_in_progress",
            at + timedelta(minutes=1),
        ), minutes
    assert read == [] and exporter.files(export_id) == []
    assert await status_of(engine, export_id) == "running"


async def test_an_export_waiting_past_24_hours_fails(engine: AsyncEngine, exporter: Exporter) -> None:
    """Deviation 52: an export still waiting `EXPORT_WAIT_LIMIT` after its `created_at` ends `failed`
    (`purge_in_progress`, with the `operation.failed` alert), its row `failed` by the failure hook; the next
    request is accepted."""
    await golden_purge(engine)
    t0 = utcnow()
    export_id, operation_id = await requested(engine, now=t0)
    await exporter.run(operation_id, now=t0 + EXPORT_WAIT_LIMIT - timedelta(seconds=1))
    assert (await operation_of(engine, operation_id)).status == "pending"
    await exporter.again(operation_id, now=t0 + EXPORT_WAIT_LIMIT)
    row = await operation_of(engine, operation_id)
    assert (row.status, row.attempts, row.last_error_code) == ("failed", 0, "purge_in_progress")
    assert await scalar(engine, _FAILED_ALERT, k=f"operation.failed:operation:{operation_id}") == 1
    assert await status_of(engine, export_id) == "failed"
    second, _ = await requested(engine)
    assert second > export_id


async def test_an_export_requested_after_a_purges_a2_waits_then_ends_ready(
    engine: AsyncEngine, exporter: Exporter, purger: Purger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling T17-1 (d): an export requested after a purge's A2 lies above its `max_ids["exports"]`, so that
    purge never touches it; the export waits while the manifest is `executing`, then ends `ready` with that
    manifest as its purge generation."""
    opened = await golden_purge(engine)
    assert await recorded(purger, opened, monkeypatch) == []
    manifest = await manifest_of(engine, opened.manifest_id)
    assert manifest.scope is not None
    bound = manifest.scope["max_ids"]["exports"]
    export_id, operation_id = await requested(engine)
    assert export_id > bound
    await exporter.run(operation_id)
    assert (await operation_of(engine, operation_id)).last_error_code == "purge_in_progress"
    await purger.again(opened.operation_id)
    await completed(purger, opened.manifest_id, opened.operation_id)
    assert await status_of(engine, export_id) == "running"
    await exporter.again(operation_id)
    row = await export_of(engine, export_id)
    assert row is not None and (row.status, row.purge_generation) == ("ready", opened.manifest_id)
    assert exporter.files(export_id) == [row.storage_path]


async def test_an_export_meeting_a_fence_ends_purged(
    engine: AsyncEngine, exporter: Exporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§20.3 L6508: the final transaction ends the export `purged`, its file unlinked, when any fence is
    active, also a fence whose manifest its snapshot saw (`completed`, at or below the generation: a replay
    re-raises such a manifest's fence, §19.5 phase A)."""
    async with engine.begin() as conn:
        job = await insert_job(conn)
        manifest_id = await insert_test_manifest(conn, kind="job_tombstone")
        await transition(
            conn, "deletion_manifests", manifest_id, ["executing"], "completed", set_={"file_sha256": b"\x01"}
        )
    export_id, operation_id = await requested(engine)
    pause = PausePoint(AFTER_SNAPSHOT)
    monkeypatch.setattr(export, "afault_point", pause)
    running = asyncio.create_task(exporter.run(operation_id))
    await asyncio.wait_for(pause.reached.wait(), 30)
    async with engine.begin() as conn:
        await conn.execute(_FENCE_OF, {"jobs": [job.job_id], "m": manifest_id})
    pause.resume()
    await running
    row = await export_of(engine, export_id)
    assert row is not None and (row.status, row.storage_path, row.purge_generation) == ("purged", None, None)
    assert await scalar(engine, _MAX_MANIFEST) == manifest_id  # no manifest newer than the snapshot's
    assert exporter.files(export_id) == []
    assert (await operation_of(engine, operation_id)).status == "succeeded"


async def test_the_publication_waits_for_a_manifest_committing_under_active_config_and_ends_purged(
    engine: AsyncEngine, exporter: Exporter, migrated: PgServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§20.3 L6508, the publication's side (fix round 1, M3): every purge's A0 commits its manifest under
    `active_config FOR UPDATE`. A publication that arrives while that transaction is open waits on its
    `active_config FOR SHARE`, reads the manifest (above its generation) once it has committed, and ends the
    export `purged`, its file removed, never `ready`."""
    export_id, operation_id = await requested(engine)
    pause = PausePoint(AFTER_RENAME)
    monkeypatch.setattr(export, "afault_point", pause)
    running = asyncio.create_task(exporter.run(operation_id))
    await asyncio.wait_for(pause.reached.wait(), 30)
    async with engine.connect() as holder:
        await holder.begin()
        await holder.execute(_LOCK_CONFIG)
        await insert_test_manifest(holder, kind="golden_case_delete")  # an A0's manifest, uncommitted
        pause.resume()
        await await_until(lambda: blocked_or_done(migrated, running), timeout_s=30)
        assert not running.done()  # a lock wait, on `active_config`
        assert await status_of(engine, export_id) == "running"
        await holder.commit()
    await asyncio.wait_for(running, 30)
    row = await export_of(engine, export_id)
    assert row is not None and (row.status, row.storage_path, row.purge_generation) == ("purged", None, None)
    assert exporter.files(export_id) == []
    assert (await operation_of(engine, operation_id)).status == "succeeded"


async def test_a_publication_never_names_a_missing_archive(
    engine: AsyncEngine, exporter: Exporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix round 1, M1: a file removal does not roll back, so a failure hook whose transaction rolled back
    (a stale reclaim of several rows that fails on a later one) can have removed the unpublished archive of a
    handler that goes on. The publication checks that its archive still exists: a missing one fails the
    attempt (`bug`, the row still `running`, no `storage_path`), and the retry rebuilds and publishes it."""
    export_id, operation_id = await requested(engine)
    pause = PausePoint(AFTER_RENAME)
    monkeypatch.setattr(export, "afault_point", pause)
    running = asyncio.create_task(exporter.run(operation_id))
    await asyncio.wait_for(pause.reached.wait(), 30)
    [archive] = exporter.files(export_id)
    (exporter.volume / archive).unlink()  # what the rolled-back hook's removal leaves
    pause.resume()
    await asyncio.wait_for(running, 30)
    retried = await operation_of(engine, operation_id)
    assert (retried.status, retried.attempts, retried.last_error_code) == ("pending", 1, "bug")
    row = await export_of(engine, export_id)
    assert row is not None and (row.status, row.storage_path) == ("running", None)
    await exporter.again(operation_id)
    row = await export_of(engine, export_id)
    assert row is not None and row.status == "ready" and exporter.files(export_id) == [row.storage_path]


@pytest.mark.parametrize("stage", ["opened", "completed"])
async def test_a_purge_opened_after_the_snapshot_purges_the_export_at_publication(
    engine: AsyncEngine, exporter: Exporter, purger: Purger, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """Deviation 19: a fence-free golden-case deletion opened (`opened`: only A0, so its manifest lies above
    the snapshot's generation and nothing else moves the export) or opened and completed (`completed`: its
    phase B purged the running export) between the export's snapshot and its publication: the export ends
    `purged`, no file of it left, and the purge completes with every postcondition holding."""
    export_id, operation_id = await requested(engine)
    pause = PausePoint(AFTER_SNAPSHOT)
    monkeypatch.setattr(export, "afault_point", pause)
    running = asyncio.create_task(exporter.run(operation_id))
    await asyncio.wait_for(pause.reached.wait(), 30)
    opened = await golden_purge(engine)
    if stage == "completed":
        await purger.run(opened.operation_id)
        await completed(purger, opened.manifest_id, opened.operation_id)
    pause.resume()
    await running
    row = await export_of(engine, export_id)
    assert row is not None and (row.status, row.storage_path, row.purge_generation) == ("purged", None, None)
    assert exporter.files(export_id) == []
    assert (await operation_of(engine, operation_id)).status == "succeeded"
    if stage == "opened":
        await purger.run(opened.operation_id)  # its A2 comes after the publication ended the export
        manifest = await completed(purger, opened.manifest_id, opened.operation_id)
        assert manifest.scope is not None
        operations: list[Any] = list(manifest.scope["operations"])
        assert [op for op in operations if "exports" in (op.get("table"), op.get("volume"))] == []


@pytest.mark.parametrize("point", [AFTER_TMP, AFTER_RENAME])
async def test_a_purge_that_marks_a_running_export_first_leaves_no_file(
    engine: AsyncEngine, exporter: Exporter, purger: Purger, monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    """D9, Deviation 19: a golden-case deletion that runs whole while the export holds its archive file (its
    `.tmp`, or its archive renamed but not yet published) purges the running export and unlinks `<id>-*`;
    the handler's final transaction finds the row `purged`, publishes nothing and leaves no file."""
    export_id, operation_id = await requested(engine)
    pause = PausePoint(point)
    monkeypatch.setattr(export, "afault_point", pause)
    running = asyncio.create_task(exporter.run(operation_id))
    await asyncio.wait_for(pause.reached.wait(), 30)
    [held] = exporter.files(export_id)
    assert held.endswith(".jhx.tmp" if point == AFTER_TMP else ".jhx")
    opened = await golden_purge(engine)
    await purger.run(opened.operation_id)
    await completed(purger, opened.manifest_id, opened.operation_id)
    assert await status_of(engine, export_id) == "purged"
    assert exporter.files(export_id) == []
    pause.resume()
    await running
    row = await export_of(engine, export_id)
    assert row is not None and (row.status, row.storage_path) == ("purged", None)
    assert exporter.files(export_id) == []
    assert (await operation_of(engine, operation_id)).status == "succeeded"


@pytest.mark.parametrize("point", [BEFORE_TMP, BEFORE_RENAME])
async def test_a_purge_waits_for_a_guarded_name_and_removes_the_new_file(
    engine: AsyncEngine,
    exporter: Exporter,
    purger: Purger,
    migrated: PgServer,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
) -> None:
    """Ruling T17-1 (c): the handler creates its `.tmp` (O_EXCL) and renames it to its `.jhx` each in a
    transaction that holds the export row `FOR SHARE` and found it `running`. A purge whose A2 named the
    export and whose phase B reaches it while such a transaction is open waits for it (its `running →
    purged` is a lock wait); once the name is made and committed, the purge's after-commit unlink takes it,
    and nothing is published: no file of the export is left, and the purge's `file_remove` postcondition
    holds."""
    export_id, operation_id = await requested(engine)
    pauses = PausePoints(AFTER_SNAPSHOT, point)
    monkeypatch.setattr(export, "afault_point", pauses)
    handler = asyncio.create_task(exporter.run(operation_id))
    await asyncio.wait_for(pauses[AFTER_SNAPSHOT].reached.wait(), 30)
    opened = await golden_purge(engine)
    assert await recorded(purger, opened, monkeypatch) == [export_id]
    pauses[AFTER_SNAPSHOT].resume()
    await asyncio.wait_for(pauses[point].reached.wait(), 30)  # the row held FOR SHARE, the name not made yet
    held = exporter.files(export_id)
    assert held == [] if point == BEFORE_TMP else [name[-8:] for name in held] == [".jhx.tmp"]
    deleting = asyncio.create_task(purger.again(opened.operation_id))
    await await_until(lambda: blocked_or_done(migrated, deleting), timeout_s=30)
    assert not deleting.done()
    assert await status_of(engine, export_id) == "running"
    pauses[point].resume()
    await asyncio.wait_for(deleting, 60)
    await asyncio.wait_for(handler, 60)
    await completed(purger, opened.manifest_id, opened.operation_id)
    row = await export_of(engine, export_id)
    assert row is not None and (row.status, row.storage_path) == ("purged", None)
    assert exporter.files(export_id) == []
    assert (await operation_of(engine, operation_id)).status == "succeeded"


async def test_a_tmp_removed_by_another_hand_is_never_published(
    engine: AsyncEngine, exporter: Exporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `.tmp` gone while the export still runs is no state of the protocol (only a guarded transaction's
    purge or the failure hook removes one, and both end the row or the claim first): the rename fails, the
    attempt is retried (`bug`) and nothing names a missing file; the retry publishes."""
    export_id, operation_id = await requested(engine)
    pause = PausePoint(AFTER_TMP)
    monkeypatch.setattr(export, "afault_point", pause)
    running = asyncio.create_task(exporter.run(operation_id))
    await asyncio.wait_for(pause.reached.wait(), 30)
    [tmp] = exporter.files(export_id)
    (exporter.volume / tmp).unlink()
    pause.resume()
    await running
    retried = await operation_of(engine, operation_id)
    assert (retried.status, retried.attempts, retried.last_error_code) == ("pending", 1, "bug")
    row = await export_of(engine, export_id)
    assert row is not None and (row.status, row.storage_path) == ("running", None)
    assert exporter.files(export_id) == []
    await exporter.again(operation_id)
    row = await export_of(engine, export_id)
    assert row is not None and row.status == "ready" and exporter.files(export_id) == [row.storage_path]


async def test_a_purge_after_ready_deletes_it(
    engine: AsyncEngine, exporter: Exporter, purger: Purger
) -> None:
    """§20.3 L6507: a `ready` export ends `purged` with no `storage_path` and its file gone, by the purge's
    export ops (`deletion_graph.purge_exports`)."""
    export_id, operation_id = await requested(engine)
    await exporter.run(operation_id)
    row = await export_of(engine, export_id)
    assert row is not None and row.status == "ready"
    path = exporter.volume / row.storage_path
    assert path.exists()
    opened = await golden_purge(engine)
    await purger.run(opened.operation_id)
    await completed(purger, opened.manifest_id, opened.operation_id)
    row = await export_of(engine, export_id)
    assert row is not None and (row.status, row.storage_path) == ("purged", None)
    assert not path.exists() and exporter.files(export_id) == []


@pytest.mark.parametrize("ended", ["failed", "expired"])
async def test_a_named_export_that_failed_or_expired_before_phase_b_satisfies_the_op(
    engine: AsyncEngine,
    exporter: Exporter,
    purger: Purger,
    queue_app: procrastinate.App,
    migrated: PgServer,
    monkeypatch: pytest.MonkeyPatch,
    ended: str,
) -> None:
    """Task 17's state table: an export the purge's A2 named (`running` or `ready`) that its failure hook
    (`failed`) or retention (`expired`) made content-free before phase B satisfies the op as it is: the purge
    completes with every postcondition holding, the row is unchanged, and no `<id>-*` file is left."""
    t0 = utcnow()
    export_id, operation_id = await requested(engine, now=t0)
    if ended == "expired":
        await exporter.run(operation_id)
        assert await status_of(engine, export_id) == "ready"
    opened = await golden_purge(engine)
    assert await recorded(purger, opened, monkeypatch) == [export_id]
    if ended == "failed":
        (exporter.volume / f"{export_id}-{uuid.uuid4()}.jhx.tmp").write_bytes(b"\x00 an attempt's segments")
        async with engine.begin() as conn:
            claim = await ops.claim_operation(conn, operation_id, epoch=1)
            assert claim is not None
            await ops.finish_operation(conn, claim, status="failed", error_code="bug")  # its hook runs here
    else:
        report = await retention_at(
            engine, queue_app, migrated, t0 + EXPORT_TTL + timedelta(seconds=1), exporter
        )
        assert report["exports"] == 1
    before = await export_of(engine, export_id)
    assert before is not None and (before.status, before.storage_path) == (ended, None)
    assert exporter.files(export_id) == []
    await purger.again(opened.operation_id)
    await completed(purger, opened.manifest_id, opened.operation_id)
    assert await export_of(engine, export_id) == before
    assert exporter.files(export_id) == []


# ---- attempts and failures ---------------------------------------------------------------------------------


async def test_a_handler_whose_export_is_no_longer_running_writes_nothing(
    engine: AsyncEngine, exporter: Exporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A handler that finds its export `purged` (a purge's phase B) or `failed` (a restore) when it starts
    reads nothing, writes no file and ends `succeeded` at once."""
    read: list[int] = []

    async def unread(*args: Any, **kwargs: Any) -> None:
        read.append(1)

    monkeypatch.setattr(export, "write_archive", unread)
    for to_state in ("purged", "failed"):
        export_id, operation_id = await requested(engine)
        async with engine.begin() as conn:
            await transition(conn, "exports", export_id, ["running"], to_state)
        await exporter.run(operation_id)
        assert (await operation_of(engine, operation_id)).status == "succeeded"
        row = await export_of(engine, export_id)
        assert row is not None and (row.status, row.storage_path) == (to_state, None)
        assert exporter.files(export_id) == []
    assert read == []


async def test_an_attempt_removes_an_earlier_attempts_archive(
    engine: AsyncEngine, exporter: Exporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after the rename leaves an archive no row names; the retry first removes it, then publishes
    its own: one file, the published one."""
    monkeypatch.setattr(export, "afault_point", raising_at(AFTER_RENAME))
    export_id, operation_id = await requested(engine)
    await exporter.run(operation_id)
    [first] = exporter.files(export_id)
    assert first.endswith(".jhx")
    assert await status_of(engine, export_id) == "running"
    retried = await operation_of(engine, operation_id)
    assert (retried.status, retried.attempts, retried.last_error_code) == ("pending", 1, "bug")
    monkeypatch.setattr(export, "afault_point", no_fault)
    await exporter.again(operation_id)
    row = await export_of(engine, export_id)
    assert row is not None and row.status == "ready" and row.storage_path != first
    assert exporter.files(export_id) == [row.storage_path]


async def test_an_exhausted_export_fails_and_a_new_export_succeeds(
    engine: AsyncEngine, exporter: Exporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deviation 52: ten failing attempts (each leaves its renamed archive behind, which the next removes
    first) end the operation `failed('attempts_exhausted')`; its failure hook moves the export `failed` and
    removes the last attempt's archive, so `exports_one_running` lets the next request in, which ends
    `ready`."""
    monkeypatch.setattr(export, "afault_point", raising_at(AFTER_RENAME))
    export_id, operation_id = await requested(engine)
    left: list[str] = []
    for attempt in range(ops.MAX_OPERATION_ATTEMPTS):
        await exporter.again(operation_id)
        if attempt < ops.MAX_OPERATION_ATTEMPTS - 1:
            [archive] = exporter.files(export_id)  # this attempt's, the earlier one removed
            assert archive not in left
            left.append(archive)
    row = await operation_of(engine, operation_id)
    assert (row.status, row.attempts, row.last_error_code) == ("failed", 10, "attempts_exhausted")
    exported = await export_of(engine, export_id)
    assert exported is not None and (exported.status, exported.storage_path) == ("failed", None)
    assert exporter.files(export_id) == []
    monkeypatch.setattr(export, "afault_point", no_fault)
    second, second_operation = await requested(engine)
    await exporter.run(second_operation)
    assert await status_of(engine, second) == "ready"


@pytest.mark.parametrize("way", ["reclaim", "takeover"])
async def test_an_export_reclaimed_on_its_last_attempt_fails_through_the_hook(
    engine: AsyncEngine, exporter: Exporter, way: str
) -> None:
    """The failure hook runs in every transaction that ends the operation `failed`: a stale reclaim or a
    takeover on its last attempt moves the export `failed` and unlinks its files (an attempt's `.tmp` and
    an unpublished archive), and no other export's (`<id>0-…`)."""
    export_id, operation_id = await requested(engine)
    async with engine.begin() as conn:
        await conn.execute(sa.text("UPDATE operations SET attempts = 9 WHERE id = :i"), {"i": operation_id})
        assert await ops.claim_operation(conn, operation_id, epoch=1) is not None
    files = [exporter.volume / f"{export_id}-{uuid.uuid4()}{suffix}" for suffix in (".jhx.tmp", ".jhx")]
    decoy = exporter.volume / f"{export_id}0-{uuid.uuid4()}.jhx"
    for path in (*files, decoy):
        path.write_bytes(b"\x00 encrypted segments")
    async with engine.begin() as conn:
        if way == "reclaim":
            later = utcnow() + ops.OPERATION_STALE + timedelta(minutes=1)
            assert await ops.reclaim_stale_operations(conn, now=later) == [operation_id]
        else:
            assert await ops.sweep_older_epoch_operations(conn, 2) == 1
    row = await operation_of(engine, operation_id)
    assert (row.status, row.last_error_code) == ("failed", "attempts_exhausted")
    exported = await export_of(engine, export_id)
    assert exported is not None and exported.status == "failed"
    assert not any(path.exists() for path in files) and decoy.exists()


async def test_without_a_configured_volume_the_hook_ends_the_row_and_logs_the_files_left(
    engine: AsyncEngine, purger: Purger, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Fix round 1, M2: in a process that never configured the exports volume (`export.configure`, which only
    the worker's start calls), the failure hook still moves the export `failed`, leaves its files to the next
    configured pass and logs `export_files_left` with the export's id."""
    monkeypatch.setattr(export, "_volume", None)
    export_id, operation_id = await requested(engine)
    left = purger.cfg.exports_dir / f"{export_id}-{uuid.uuid4()}.jhx.tmp"
    left.write_bytes(b"\x00 an attempt's segments")
    async with engine.begin() as conn:
        claim = await ops.claim_operation(conn, operation_id, epoch=1)
        assert claim is not None
        await ops.finish_operation(conn, claim, status="failed", error_code="bug")  # its hook runs here
    assert await status_of(engine, export_id) == "failed"
    assert left.exists()
    left_logged = [(e["export_id"], e["level"]) for e in logged(caplog, "export_files_left")]
    assert left_logged == [(export_id, "warning")]


# ---- retention ---------------------------------------------------------------------------------------------


async def test_it_expires_after_7_days_and_the_row_goes_after_30(
    engine: AsyncEngine, exporter: Exporter, queue_app: procrastinate.App, migrated: PgServer
) -> None:
    """Deviation 48: a `ready` export becomes `expired` once `expires_at` (created + 7 d) has passed, its
    `storage_path` NULL and its file removed after that commit; the row goes 30 days after `expires_at`."""
    t0 = utcnow()
    export_id, operation_id = await requested(engine, now=t0)
    await exporter.run(operation_id)
    row = await export_of(engine, export_id)
    assert row is not None and row.status == "ready" and row.expires_at == t0 + EXPORT_TTL
    path = exporter.volume / row.storage_path
    second = timedelta(seconds=1)

    async def at(now: datetime) -> int:
        return (await retention_at(engine, queue_app, migrated, now, exporter))["exports"]

    assert await at(t0 + EXPORT_TTL - second) == 0
    assert await status_of(engine, export_id) == "ready" and path.exists()
    assert await at(t0 + EXPORT_TTL + second) == 1
    row = await export_of(engine, export_id)
    assert row is not None and (row.status, row.storage_path) == ("expired", None) and not path.exists()
    assert await at(t0 + EXPORT_TTL + timedelta(days=30) - second) == 0
    assert await export_of(engine, export_id) is not None
    assert await at(t0 + EXPORT_TTL + timedelta(days=30) + second) == 1
    assert await export_of(engine, export_id) is None


async def test_retention_fails_a_running_export_without_a_live_operation(
    engine: AsyncEngine, exporter: Exporter, queue_app: procrastinate.App, migrated: PgServer
) -> None:
    """Deviation 52: a `running` export whose operation is neither `pending` nor `running` (lost without its
    hook: cancelled here) ends `failed`, its files unlinked; one whose operation is live stays `running`."""
    export_id, operation_id = await requested(engine)
    leftover = exporter.volume / f"{export_id}-{uuid.uuid4()}.jhx.tmp"
    leftover.write_bytes(b"\x00 an attempt's segments")
    assert (await retention_at(engine, queue_app, migrated, utcnow(), exporter))["exports"] == 0
    assert await status_of(engine, export_id) == "running"
    assert leftover.exists()
    async with engine.begin() as conn:
        assert await ops.cancel_operation(conn, operation_id, reason="test")
    assert (await retention_at(engine, queue_app, migrated, utcnow(), exporter))["exports"] == 1
    assert await status_of(engine, export_id) == "failed"
    assert not leftover.exists()
    second, _ = await requested(engine)  # no running row left
    assert second > export_id


async def test_retention_removes_an_unnamed_archive(
    engine: AsyncEngine, exporter: Exporter, queue_app: procrastinate.App, migrated: PgServer
) -> None:
    """As `cv_store_orphans` does for CV files: a `<id>-*.jhx` or `.jhx.tmp` of the exports directory written
    more than a day ago that no `ready` row names is unlinked; the `ready` export's archive, a newer one and
    a file of another form stay."""
    export_id, operation_id = await requested(engine)
    await exporter.run(operation_id)
    row = await export_of(engine, export_id)
    assert row is not None and row.status == "ready"
    named = exporter.volume / row.storage_path
    unnamed = [
        exporter.volume / f"{export_id + 100}-{uuid.uuid4()}.jhx",
        exporter.volume / f"{export_id + 101}-{uuid.uuid4()}.jhx.tmp",
    ]
    recent = exporter.volume / f"{export_id + 102}-{uuid.uuid4()}.jhx"
    other = exporter.volume / "notes.txt"
    for path in (*unnamed, recent, other):
        path.write_bytes(b"\x00")
    old = (utcnow() - timedelta(days=2)).timestamp()
    for path in (named, *unnamed, other):
        os.utime(path, (old, old))
    assert (await retention_at(engine, queue_app, migrated, utcnow(), exporter))["exports"] == 2
    assert named.exists() and recent.exists() and other.exists()
    assert not any(path.exists() for path in unnamed)
