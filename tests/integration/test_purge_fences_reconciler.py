"""The `purge_fences` reconciler (spec §17.4, §19.5; plan M5 Task 9, D7) and the `operations` retention rule's
purge exception.

For each `executing` manifest: its newest purge operation `failed`, or `running` without a heartbeat for 1 h,
raises `privacy.purge_failed` (critical, subject `manifest:<id>`) and keeps the fence, re-queueing nothing
(the user retries it from Needs attention); with no `pending`, `running` or `failed` purge operation at all, a
new one is created (`purge:<uuid>`) and the alert raised. The alert is resolved once the manifest leaves
`executing`. Retention keeps every purge operation of an `executing` manifest, so a failed one stays to be
retried.
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.graphs.checkpointer import open_saver
from jobhunter.privacy.fences import FenceSpec, insert_fence, release_fences
from jobhunter.privacy.manifests import (
    ManifestContent,
    ManifestRow,
    complete_manifest,
    insert_manifest,
    record_manifest,
    write_manifest_file,
)
from jobhunter.privacy.reconcilers import PURGE_FAILED_ALERT
from jobhunter.privacy.retention import run_retention_daily
from jobhunter.profile.storage import CvStore
from jobhunter.worker import reconcilers
from jobhunter.worker.operations import (
    OperationClaim,
    cancel_operation,
    claim_operation,
    create_operation,
    finish_operation,
)
from jobhunter.worker.reconcilers import ReconcileContext
from jobhunter.worker.tasks import defer_in_tx, job_task_name, task_app
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.pg import PgServer

pytestmark = pytest.mark.integration

_PURGES: Final = sa.text(
    "SELECT id, status, payload, dedup_key FROM operations WHERE kind = 'purge' ORDER BY id"
)
_JOBS: Final = sa.text("SELECT args FROM procrastinate_jobs WHERE task_name = :t ORDER BY id")
_ALERTS: Final = sa.text(
    "SELECT severity, dedup_key, occurrences, resolved_at, context FROM alerts WHERE code = :c ORDER BY id"
)
_FENCES: Final = sa.text("SELECT released_at FROM purge_fences WHERE manifest_id = :m")


async def fenced_manifest(engine: AsyncEngine) -> ManifestRow:
    """An `executing` source purge with its active fence, as A0 commits them."""
    async with engine.begin() as conn:
        manifest = await insert_manifest(
            conn, kind="source_purge", request={"source_id": "adzuna"}, now=utcnow()
        )
        await insert_fence(conn, manifest_id=manifest.id, spec=FenceSpec(kind="source", source_id="adzuna"))
    return manifest


async def purge_operation(engine: AsyncEngine, manifest: ManifestRow) -> int:
    async with engine.begin() as conn:
        operation_id, created = await create_operation(
            conn,
            kind="purge",
            payload={"manifest_id": manifest.id},
            dedup_key=f"purge:{manifest.manifest_uuid}",
            defer=None,
        )
    assert created
    return operation_id


async def claimed(engine: AsyncEngine, operation_id: int) -> OperationClaim:
    async with engine.begin() as conn:
        claim = await claim_operation(conn, operation_id, epoch=1)
    assert claim is not None
    return claim


async def failed(engine: AsyncEngine, manifest: ManifestRow) -> int:
    """A purge operation of the manifest that ran and failed."""
    claim = await claimed(engine, await purge_operation(engine, manifest))
    async with engine.begin() as conn:
        await finish_operation(conn, claim, status="failed", error_code="bug")
    return claim.operation_id


async def complete(engine: AsyncEngine, manifest: ManifestRow, directory: Path) -> None:
    """The purge's last transaction: its file recorded, its fences released, the manifest completed."""
    content: ManifestContent = {
        "format": 1,
        "manifest_uuid": str(manifest.manifest_uuid),
        "kind": manifest.kind,
        "admitted_at": manifest.created_at.isoformat(),
        "fenced_at": manifest.created_at.isoformat(),
        "max_ids": {},
        "operations": [{"op": "fence", "kind": "source", "key": "adzuna", "job_ids": []}],
    }
    sha = write_manifest_file(directory, content)
    async with engine.begin() as conn:
        await record_manifest(conn, manifest, content, sha)
        await release_fences(conn, manifest.id)
        await complete_manifest(conn, manifest.id)


async def reconcile(engine: AsyncEngine, now: datetime) -> int:
    """One pass of the registered `purge_fences` reconciler at `now`, as the heavy cadence runs it."""
    cadences, fn = reconcilers.REGISTRY["purge_fences"]
    assert cadences == {"heavy"}
    return await fn(ReconcileContext(engine=engine, queue=task_app(), defer=defer_in_tx, now=now, epoch=1))


async def purges(engine: AsyncEngine) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(_PURGES)).all())


async def failed_alerts(engine: AsyncEngine) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(_ALERTS, {"c": PURGE_FAILED_ALERT})).all())


async def fence_active(engine: AsyncEngine, manifest: ManifestRow) -> bool:
    async with engine.connect() as conn:
        return (await conn.execute(_FENCES, {"m": manifest.id})).scalar_one() is None


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    root = tmp_path / "manifests"
    root.mkdir(mode=0o700)
    return root


async def test_a_failed_purge_keeps_its_fence_and_alerts_without_requeue(
    engine: AsyncEngine, current_epoch: int, queue_app: Any, directory: Path
) -> None:
    manifest = await fenced_manifest(engine)
    operation_id = await failed(engine, manifest)
    now = utcnow()

    assert await reconcile(engine, now) == 1
    assert await reconcile(engine, now + timedelta(minutes=10)) == 0  # the open alert, no new occurrence
    [alert] = await failed_alerts(engine)
    assert (alert.severity, alert.dedup_key, alert.occurrences, alert.resolved_at) == (
        "critical",
        f"{PURGE_FAILED_ALERT}:manifest:{manifest.id}",
        1,
        None,
    )
    assert alert.context == {"manifest_id": manifest.id, "operation_id": operation_id, "reason": "failed"}
    assert [(p.id, p.status) for p in await purges(engine)] == [(operation_id, "failed")]  # nothing re-queued
    assert await fence_active(engine, manifest)  # fenced work stays blocked

    # A retry the user started (a new operation) is not failed: nothing more is raised while it runs.
    retry = await purge_operation(engine, manifest)
    assert await reconcile(engine, now + timedelta(minutes=20)) == 0
    assert [p.id for p in await purges(engine)] == [operation_id, retry]

    # The retry completes the manifest: the alert is resolved, and stays so.
    await complete(engine, manifest, directory)
    assert await reconcile(engine, now + timedelta(minutes=30)) == 1
    [alert] = await failed_alerts(engine)
    assert alert.resolved_at is not None
    assert await reconcile(engine, now + timedelta(minutes=40)) == 0


async def test_a_stale_running_purge_alerts(engine: AsyncEngine, current_epoch: int, queue_app: Any) -> None:
    manifest = await fenced_manifest(engine)
    claim = await claimed(engine, await purge_operation(engine, manifest))
    now = utcnow()

    assert await reconcile(engine, now + timedelta(minutes=59)) == 0
    assert await failed_alerts(engine) == []

    assert await reconcile(engine, now + timedelta(hours=1, minutes=1)) == 1
    [alert] = await failed_alerts(engine)
    assert alert.context == {
        "manifest_id": manifest.id,
        "operation_id": claim.operation_id,
        "reason": "stale",
    }
    assert [(p.id, p.status) for p in await purges(engine)] == [(claim.operation_id, "running")]
    assert await fence_active(engine, manifest)


async def test_an_executing_manifest_without_an_operation_is_requeued(
    engine: AsyncEngine, current_epoch: int, queue_app: Any, directory: Path
) -> None:
    manifest = await fenced_manifest(engine)
    cancelled = await purge_operation(engine, manifest)
    async with engine.begin() as conn:
        assert await cancel_operation(
            conn, cancelled, reason="superseded"
        )  # a superseding purge cancelled it
    finished = await fenced_manifest(engine)
    await complete(engine, finished, directory)  # completed: never re-queued
    now = utcnow()

    assert await reconcile(engine, now) == 2  # the new operation and its alert
    [old, new] = await purges(engine)
    assert (old.id, old.status) == (cancelled, "cancelled")
    assert (new.status, new.payload, new.dedup_key) == (
        "pending",
        {"manifest_id": manifest.id},
        f"purge:{manifest.manifest_uuid}",
    )
    async with engine.connect() as conn:
        jobs = await conn.execute(_JOBS, {"t": job_task_name("run_operation")})
        assert [row.args for row in jobs] == [{"operation_id": new.id}]
    [alert] = await failed_alerts(engine)
    assert alert.context == {"manifest_id": manifest.id, "operation_id": new.id, "reason": "requeued"}

    # The new operation is live: the next pass creates nothing more.
    assert await reconcile(engine, now + timedelta(minutes=10)) == 0
    assert len(await purges(engine)) == 2


async def test_retention_keeps_purge_operations_of_executing_manifests(
    engine: AsyncEngine,
    current_epoch: int,
    queue_app: Any,
    migrated: PgServer,
    directory: Path,
    tmp_path: Path,
) -> None:
    stuck = await fenced_manifest(engine)
    kept = await failed(engine, stuck)
    done = await fenced_manifest(engine)
    gone = await failed(engine, done)
    await complete(engine, done, directory)
    async with engine.begin() as conn:
        other, _ = await create_operation(conn, kind="export", payload={}, dedup_key="export", defer=None)
        assert await cancel_operation(conn, other, reason="superseded")

    # 31 days later: every finished operation is past the 30-day window.
    later = utcnow() + timedelta(days=31)
    cv_store = tmp_path / "cv"
    cv_store.mkdir(mode=0o700)
    async with open_saver(migrated.dsn("app")) as saver:
        report = await run_retention_daily(
            engine, queue_app, saver, cv_store=CvStore(cv_store), now=later, epoch=current_epoch
        )
    assert report["operations"] == 2
    assert [(p.id, p.status) for p in await purges(engine)] == [(kept, "failed")]
    assert gone != kept
