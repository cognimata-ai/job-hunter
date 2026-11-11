"""The retry of a failed purge (spec §19.5 step 3, §20.4; plan M5 Task 18, D6, D8, Deviation 17):
`POST /api/system/purges/{operation_id}/retry` → 202 `{operation_id, manifest_id}`.

The retry inserts a new `purge` operation with the payload and the dedup key of the one it names, for a
manifest that is still `executing` and has no purge operation `pending` or `running`; the failed one stays as
it is. An id that names no purge operation is 404; a live purge, or a manifest that is no longer executing,
409 `conflict_state`. The manifest's fences stay active throughout: the retry never touches them. Under
delete-everything's `global` fence the edge lets the retry through (it is the only recovery of a failed
delete-everything, whose fence stays) while it still refuses every other write.

A purge fails as the worker ends it: claimed (epoch 1), then finished `failed`. Every datum is invented.
"""

import uuid
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.privacy.fences import FenceSpec, insert_fence
from jobhunter.privacy.manifests import ManifestRow, insert_manifest
from jobhunter.worker.operations import claim_operation, create_operation, finish_operation
from jobhunter.worker.tasks import job_task_name
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.fixtures import TEST_PASSWORD
from tests.support.m2 import insert_profile_chain
from tests.support.m3 import insert_golden_case, insert_job
from tests.support.profile import idem
from tests.support.purges import Purger, completed
from tests.support.settings import fresh_key

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("current_epoch")]

_OPERATIONS: Final = sa.text(
    "SELECT id, status, payload, dedup_key FROM operations WHERE kind = 'purge' ORDER BY id"
)
_FENCES: Final = sa.text("SELECT kind, released_at FROM purge_fences WHERE manifest_id = :m ORDER BY id")
_DEFERRED: Final = sa.text(
    "SELECT count(*) FROM procrastinate_jobs WHERE task_name = :t "
    "AND args -> 'operation_id' = to_jsonb(CAST(:i AS bigint))"
)


def retry_url(operation_id: int) -> str:
    return f"/api/system/purges/{operation_id}/retry"


def code_of(r: httpx.Response) -> tuple[int, str]:
    return r.status_code, r.json()["code"]


async def rows(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(statement, params)).all())


async def fenced_manifest(engine: AsyncEngine) -> ManifestRow:
    """An `executing` source purge with its active fence, as A0 commits them."""
    async with engine.begin() as conn:
        manifest = await insert_manifest(
            conn, kind="source_purge", request={"source_id": "adzuna"}, now=utcnow()
        )
        await insert_fence(conn, manifest_id=manifest.id, spec=FenceSpec(kind="source", source_id="adzuna"))
    return manifest


async def purge_operation(engine: AsyncEngine, manifest_id: int, manifest_uuid: object) -> int:
    async with engine.begin() as conn:
        operation_id, created = await create_operation(
            conn,
            kind="purge",
            payload={"manifest_id": manifest_id},
            dedup_key=f"purge:{manifest_uuid}",
            defer=None,
        )
    assert created
    return operation_id


async def failed(engine: AsyncEngine, operation_id: int, error_code: str = "lock_conflict") -> int:
    """The operation claimed by worker epoch 1 and ended `failed`, as the worker ends it."""
    async with engine.begin() as conn:
        claim = await claim_operation(conn, operation_id, epoch=1)
    assert claim is not None
    async with engine.begin() as conn:
        await finish_operation(conn, claim, status="failed", error_code=error_code)
    return operation_id


async def golden_case(engine: AsyncEngine) -> int:
    """A golden case on a new job and a profile version of its own; its id."""
    async with engine.begin() as conn:
        job = await insert_job(conn)
        chain = await insert_profile_chain(conn, uuid.uuid4().int % 10**6 + 10)
        return await insert_golden_case(conn, job, chain.profile_version_id)


async def test_retry_creates_a_new_operation_for_the_same_manifest(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    manifest = await fenced_manifest(engine)
    first = await failed(engine, await purge_operation(engine, manifest.id, manifest.manifest_uuid))
    r = await logged_in_client.post(retry_url(first))
    assert r.status_code == 202, r.text
    retried = r.json()["operation_id"]
    assert r.json() == {"operation_id": retried, "manifest_id": manifest.id}
    operations = await rows(engine, _OPERATIONS)
    assert [(o.id, o.status) for o in operations] == [(first, "failed"), (retried, "pending")]
    assert operations[1].payload == operations[0].payload == {"manifest_id": manifest.id}
    assert operations[1].dedup_key == operations[0].dedup_key == f"purge:{manifest.manifest_uuid}"
    assert (await rows(engine, _DEFERRED, i=retried, t=job_task_name("run_operation")))[0][
        0
    ] == 1  # its job queued with it
    # the new one failing too is retried again, from either id
    await failed(engine, retried)
    r = await logged_in_client.post(retry_url(first))
    assert r.status_code == 202, r.text
    assert r.json()["manifest_id"] == manifest.id and r.json()["operation_id"] > retried


async def test_retry_of_a_non_purge_operation_is_404(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    export = await logged_in_client.post("/api/privacy/export", headers=idem(fresh_key("export")))
    assert export.status_code == 202, export.text
    [(export_operation,)] = await rows(engine, sa.text("SELECT id FROM operations WHERE kind = 'export'"))
    await failed(engine, int(export_operation))
    for operation_id in (int(export_operation), 987654321):
        assert code_of(await logged_in_client.post(retry_url(operation_id))) == (404, "not_found")
    assert code_of(await logged_in_client.post(retry_url(0))) == (422, "validation_failed")
    assert await rows(engine, _OPERATIONS) == []


async def test_retry_while_live_or_completed_is_409(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, purger: Purger
) -> None:
    """A purge operation still `pending` or `running` (the reconciler's re-queue, or a retry already sent) is
    409; so is a retry of a manifest that is no longer `executing`."""
    manifest = await fenced_manifest(engine)
    first = await failed(engine, await purge_operation(engine, manifest.id, manifest.manifest_uuid))
    live = await purge_operation(engine, manifest.id, manifest.manifest_uuid)  # pending
    for operation_id in (first, live):
        r = await logged_in_client.post(retry_url(operation_id))
        assert code_of(r) == (409, "conflict_state"), r.text
    async with engine.begin() as conn:
        assert await claim_operation(conn, live, epoch=1) is not None  # running
    assert code_of(await logged_in_client.post(retry_url(first))) == (409, "conflict_state")
    # a golden-case deletion run to completion: its manifest is no longer executing
    case = await logged_in_client.delete(f"/api/golden-cases/{await golden_case(engine)}")
    golden_manifest = case.json()["manifest_id"]
    [golden_operation] = [
        o.id for o in await rows(engine, _OPERATIONS) if o.payload["manifest_id"] == golden_manifest
    ]
    await purger.run(golden_operation)
    await completed(purger, golden_manifest, golden_operation)
    r = await logged_in_client.post(retry_url(golden_operation))
    assert code_of(r) == (409, "conflict_state"), r.text
    assert len(await rows(engine, _OPERATIONS)) == 3  # nothing was created


async def test_the_fence_stays_active_throughout(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """D6: a failed purge keeps its fence; the retry leaves it active, as its new operation does until the
    manifest completes."""
    manifest = await fenced_manifest(engine)
    first = await failed(engine, await purge_operation(engine, manifest.id, manifest.manifest_uuid))
    assert [tuple(f) for f in await rows(engine, _FENCES, m=manifest.id)] == [("source", None)]
    r = await logged_in_client.post(retry_url(first))
    assert r.status_code == 202, r.text
    assert [tuple(f) for f in await rows(engine, _FENCES, m=manifest.id)] == [("source", None)]
    async with engine.begin() as conn:
        assert await claim_operation(conn, r.json()["operation_id"], epoch=1) is not None
    assert [tuple(f) for f in await rows(engine, _FENCES, m=manifest.id)] == [("source", None)]


async def test_a_failed_delete_everything_is_retried_through_the_edge_under_its_global_fence(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """The real middleware, delete-everything's `global` fence active: the retry answers 202, while another
    write answers the edge's 409 `delete_everything_in_progress` before its route."""
    r = await logged_in_client.post(
        "/api/privacy/delete-everything",
        json={"confirm": "DELETE EVERYTHING", "password": TEST_PASSWORD},
        headers=idem(fresh_key("everything")),
    )
    assert r.status_code == 202, r.text
    manifest_id = r.json()["manifest_id"]
    [operation] = await rows(engine, _OPERATIONS)
    await failed(engine, int(operation.id), "postcondition")
    r = await logged_in_client.post(retry_url(int(operation.id)))
    assert r.status_code == 202, r.text
    assert r.json()["manifest_id"] == manifest_id
    for method, path, body in (
        ("POST", "/api/runs", {"include_paid": False}),
        ("PUT", "/api/jobs/1/actions", {"seen": True}),
        ("POST", "/api/system/score-cycle", None),
    ):
        refused = await logged_in_client.request(method, path, json=body, headers=idem(fresh_key("write")))
        assert code_of(refused) == (409, "delete_everything_in_progress"), (method, path, refused.text)
    assert [tuple(f) for f in await rows(engine, _FENCES, m=manifest_id)] == [("global", None)]
