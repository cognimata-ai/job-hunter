"""The privacy routes (spec §20.3-§20.5, §19.5, §15.1, §14.2; plan M5 Task 18, Deviations 20, 21 and 22,
Ruling T18-1): `api.routes.privacy`, the golden-case deletion's 202, the `deletion_unshipped` banner and Needs
attention's failed purges.

- `POST /api/privacy/export` → 202 `{export_id}`; `GET /api/privacy/exports/{id}` → the export's state, and
  with `?download=1` on a `ready` export its zip, decrypted segment by segment from the stored file (409 while
  it is not ready, 410 once it expired or was purged, also when a purge took its file between the status read
  and the open, 404 for none); a download under way reads on from its open file whatever happens to its name.
  A refused request rolls back: no `running` export is left without its operation;
- `POST /api/privacy/delete-everything` and `POST /api/privacy/purge-source` → 202 `{manifest_id}` (their
  re-authentication is `test_reauth.py`'s); a request with the key of a completed one returns its stored
  answer, also after delete-everything logged the user out;
- `GET /api/privacy/manifests` (newest first, keyset-paged) and `GET /api/privacy/manifests/{id}`: a deletion
  is reported done at `shipped` only; a source purge's lists the golden cases it deleted;
- `DELETE /api/golden-cases/{id}` → 202 `{manifest_id}`: the worker deletes the case;
- `GET /api/system/status`'s `deletion_unshipped` banner while a manifest is `completed`, and `GET
  /api/system/needs-attention`'s `failed_purges`.

The api shares the worker's exports and manifests directories (`tests.support.purges`' `purge_cfg`); every
`purge` and `export` operation is run as the worker runs it (epoch 1), the shipments written as the backup
container's shipper writes them. Nothing sleeps: the export's expiry is a retention pass at a later `now`.
Every datum is invented.
"""

import asyncio
import copy
import io
import logging
import uuid
import zipfile
from datetime import timedelta
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter import crypto
from jobhunter.api.app import JobHunterApp, create_app
from jobhunter.api.pagination import GOLDEN_CASES_SORT, cursor_key, encode_list_cursor
from jobhunter.api.routes import privacy as privacy_routes
from jobhunter.auth import sessions
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.db.transitions import transition
from jobhunter.privacy import export
from jobhunter.privacy import reconcilers as privacy_reconcilers
from jobhunter.privacy.export import EXPORT_FILES, iter_decrypted
from jobhunter.privacy.manifests import insert_manifest
from jobhunter.worker.operations import claim_operation, finish_operation
from jobhunter.worker.reconcilers import ReconcileContext
from jobhunter.worker.tasks import defer_in_tx, task_app
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import Message

from tests.support.fixtures import TEST_PASSWORD, TEST_USERNAME, ConfigFactory
from tests.support.logs import logged
from tests.support.m2 import insert_profile_chain
from tests.support.m3 import insert_golden_case, insert_job
from tests.support.profile import idem
from tests.support.purges import Purger, completed
from tests.support.settings import fresh_key, save, settings_body

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("current_epoch")]

EXPORT: Final = "/api/privacy/export"
EXPORTS: Final = "/api/privacy/exports"
DELETE_EVERYTHING: Final = "/api/privacy/delete-everything"
PURGE_SOURCE: Final = "/api/privacy/purge-source"
MANIFESTS: Final = "/api/privacy/manifests"
_EXPORT_OPERATION: Final = sa.text(
    "SELECT id FROM operations WHERE kind = 'export' "
    "AND payload -> 'export_id' = to_jsonb(CAST(:e AS bigint))"
)
_PURGE_OPERATIONS: Final = sa.text(
    "SELECT id FROM operations WHERE kind = 'purge' "
    "AND payload -> 'manifest_id' = to_jsonb(CAST(:m AS bigint)) ORDER BY id"
)
_EXPORT: Final = sa.text("SELECT status, storage_path, size_bytes FROM exports WHERE id = :e")
_MANIFEST: Final = sa.text("SELECT manifest_uuid, file_sha256, status FROM deletion_manifests WHERE id = :m")
_SHIPMENT: Final = sa.text("INSERT INTO manifest_shipments (restic_snapshot_id) VALUES (:s) RETURNING id")
_ITEM: Final = sa.text(
    "INSERT INTO manifest_shipment_items (shipment_id, manifest_uuid, file_sha256) VALUES (:s, :u, :h)"
)
_COUNTS: Final = sa.text(
    "SELECT (SELECT count(*) FROM exports) AS exports, "
    "(SELECT count(*) FROM exports WHERE status = 'running') AS running, "
    "(SELECT count(*) FROM api_idempotency) AS keys, "
    "(SELECT count(*) FROM deletion_manifests WHERE kind = 'delete_everything') AS everything, "
    "(SELECT count(*) FROM operations WHERE kind = 'purge') AS purges"
)
_CASE_EXISTS: Final = sa.text("SELECT EXISTS (SELECT 1 FROM golden_cases WHERE id = :g)")
_TO_ADZUNA_ONLY: Final = sa.text("UPDATE golden_cases SET det_input_sources = '{adzuna}' WHERE id = :g")


@pytest.fixture
def api_app(engine: AsyncEngine, make_config: ConfigFactory, purge_cfg: AppConfig) -> JobHunterApp:
    """The api on the worker's volumes (`purge_cfg`): its CV store, its exports and its manifests."""
    cfg = make_config(
        "api",
        JOBHUNTER_CV_STORE_DIR=str(purge_cfg.cv_store_dir),
        JOBHUNTER_EXPORTS_DIR=str(purge_cfg.exports_dir),
        JOBHUNTER_MANIFESTS_DIR=str(purge_cfg.manifests_dir),
    )
    return create_app(cfg=cfg, engine=engine)


@pytest.fixture
def exports_configured(purger: Purger, monkeypatch: pytest.MonkeyPatch) -> Purger:
    """The exports volume of the export's hook and retention rule, as the worker's start configures it."""
    monkeypatch.setattr(export, "_volume", None)
    export.configure(purger.cfg)
    return purger


# ---- helpers -----------------------------------------------------------------------------------------------


def code_of(r: httpx.Response) -> tuple[int, str]:
    return r.status_code, r.json()["code"]


async def keyed(
    client: httpx.AsyncClient, path: str, body: dict[str, Any] | None = None, *, key: str | None = None
) -> httpx.Response:
    return await client.post(path, json=body, headers=idem(key or fresh_key("privacy")))


def delete_everything_body() -> dict[str, Any]:
    return {"confirm": "DELETE EVERYTHING", "password": TEST_PASSWORD}


def purge_source_body() -> dict[str, Any]:
    return {"source": "adzuna", "confirm": "PURGE ADZUNA", "password": TEST_PASSWORD}


async def one(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(statement, params)).one()


async def export_operation(engine: AsyncEngine, export_id: int) -> int:
    return int((await one(engine, _EXPORT_OPERATION, e=export_id)).id)


async def purge_operations(engine: AsyncEngine, manifest_id: int) -> list[int]:
    async with engine.connect() as conn:
        return [int(i) for i in (await conn.execute(_PURGE_OPERATIONS, {"m": manifest_id})).scalars()]


async def counts(engine: AsyncEngine) -> Any:
    return await one(engine, _COUNTS)


async def ready_export(client: httpx.AsyncClient, engine: AsyncEngine, purger: Purger) -> tuple[int, Any]:
    """An export requested over the api and run by the worker to `ready`: its id and its row."""
    r = await keyed(client, EXPORT)
    assert r.status_code == 202, r.text
    export_id = int(r.json()["export_id"])
    await purger.run(await export_operation(engine, export_id))
    row = await one(engine, _EXPORT, e=export_id)
    assert row.status == "ready", row
    return export_id, row


def stored_plaintext(purger: Purger, export_id: int, storage_path: str) -> bytes:
    keys = crypto.data_keys(purger.cfg)
    with (purger.cfg.exports_dir / storage_path).open("rb") as stored:
        return b"".join(iter_decrypted(stored, keys, export_id=export_id))


async def golden_case(engine: AsyncEngine, *, synthetic: bool = False) -> int:
    """A golden case on a new job of its own and a profile version of its own; its id."""
    async with engine.begin() as conn:
        job = await insert_job(conn, synthetic_eval=synthetic)
        chain = await insert_profile_chain(conn, uuid.uuid4().int % 10**6 + 10)
        return await insert_golden_case(conn, job, chain.profile_version_id)


async def deleted_case(client: httpx.AsyncClient, engine: AsyncEngine, purger: Purger) -> int:
    """A golden case deleted over the api, its purge run to completion; the manifest's id."""
    r = await client.delete(f"/api/golden-cases/{await golden_case(engine)}")
    assert r.status_code == 202, r.text
    manifest_id = int(r.json()["manifest_id"])
    [operation_id] = await purge_operations(engine, manifest_id)
    await purger.run(operation_id)
    await completed(purger, manifest_id, operation_id)
    return manifest_id


async def ship(engine: AsyncEngine, manifest_id: int, *, sha: bytes | None = None) -> None:
    """A shipment holding the manifest's file (with `sha` in place of its hash when given), then one pass of
    the `manifest_shipping` reconciler."""
    manifest = await one(engine, _MANIFEST, m=manifest_id)
    async with engine.begin() as conn:
        shipment = int((await conn.execute(_SHIPMENT, {"s": "manifests-snapshot"})).scalar_one())
        params = {"s": shipment, "u": manifest.manifest_uuid, "h": sha or bytes(manifest.file_sha256)}
        await conn.execute(_ITEM, params)
    context = ReconcileContext(engine=engine, queue=task_app(), defer=defer_in_tx, now=utcnow(), epoch=1)
    await privacy_reconcilers.manifest_shipping(context)


async def relogged(client: httpx.AsyncClient) -> None:
    """The client logged in again (delete-everything ended its session)."""
    assert (await client.get("/api/auth/session")).status_code == 401
    r = await client.post("/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    assert r.status_code == 204, r.text


# ---- the export --------------------------------------------------------------------------------------------


async def test_export_202_then_ready_then_download_streams_a_zip(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    exports_configured: Purger,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    key = fresh_key("export")
    r = await keyed(logged_in_client, EXPORT, key=key)
    assert r.status_code == 202, r.text
    export_id = r.json()["export_id"]
    assert (await keyed(logged_in_client, EXPORT, key=key)).json() == {"export_id": export_id}  # the replay
    state = (await logged_in_client.get(f"/api/privacy/exports/{export_id}")).json()
    assert {k: state[k] for k in ("id", "status", "size_bytes")} == {
        "id": export_id,
        "status": "running",
        "size_bytes": None,
    }
    assert state["expires_at"] > state["created_at"]
    await exports_configured.run(await export_operation(engine, export_id))
    row = await one(engine, _EXPORT, e=export_id)
    state = (await logged_in_client.get(f"/api/privacy/exports/{export_id}")).json()
    assert (state["status"], state["size_bytes"]) == ("ready", row.size_bytes)
    r = await logged_in_client.get(f"/api/privacy/exports/{export_id}", params={"download": 1})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["content-disposition"] == f'attachment; filename="jobhunter-export-{export_id}.zip"'
    assert r.content == stored_plaintext(exports_configured, export_id, row.storage_path)
    assert len(r.content) == row.size_bytes
    assert set(EXPORT_FILES) <= set(zipfile.ZipFile(io.BytesIO(r.content)).namelist())
    assert [event["export_id"] for event in logged(caplog, "export_downloaded")] == [export_id]


async def test_download_before_ready_409_and_after_expiry_410(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, exports_configured: Purger
) -> None:
    r = await keyed(logged_in_client, EXPORT)
    export_id = r.json()["export_id"]
    url = f"/api/privacy/exports/{export_id}"
    assert code_of(await logged_in_client.get(url, params={"download": 1})) == (409, "conflict_state")
    assert code_of(await logged_in_client.get("/api/privacy/exports/987654321")) == (404, "not_found")
    assert code_of(await logged_in_client.get("/api/privacy/exports/0")) == (422, "validation_failed")
    await exports_configured.run(await export_operation(engine, export_id))
    assert (await logged_in_client.get(url, params={"download": 1})).status_code == 200
    expires_at = (await logged_in_client.get(url)).json()["expires_at"]
    await export.retain_exports(engine, now=utcnow() + export.EXPORT_TTL + timedelta(minutes=1), epoch=1)
    state = (await logged_in_client.get(url)).json()
    assert (state["status"], state["expires_at"]) == ("expired", expires_at)
    assert code_of(await logged_in_client.get(url, params={"download": 1})) == (410, "gone")


async def test_a_failed_export_is_409_and_a_purged_one_410(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, exports_configured: Purger
) -> None:
    export_id, _ = await ready_export(logged_in_client, engine, exports_configured)
    async with engine.begin() as conn:
        await transition(conn, "exports", export_id, ["ready"], "purged", set_={"storage_path": None})
    url = f"/api/privacy/exports/{export_id}"
    assert code_of(await logged_in_client.get(url, params={"download": 1})) == (410, "gone")
    r = await keyed(logged_in_client, EXPORT)
    failed_id = r.json()["export_id"]
    async with engine.begin() as conn:
        await transition(conn, "exports", failed_id, ["running"], "failed")
    url = f"/api/privacy/exports/{failed_id}"
    assert code_of(await logged_in_client.get(url, params={"download": 1})) == (409, "conflict_state")


async def test_a_refused_export_rolls_back_and_leaves_no_running_export(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Ruling T18-1 (d): an earlier export's operation is still live although a purge ended its row; the new
    request's row is inserted, then refused (409 `export_running`), and the route's transaction rolls back,
    its key and that row included: no `running` export is left without an operation."""
    r = await keyed(logged_in_client, EXPORT)
    first = r.json()["export_id"]
    assert code_of(await keyed(logged_in_client, EXPORT)) == (409, "conflict_state")  # it runs
    async with engine.begin() as conn:  # a purge's phase B ends the row before its operation ran
        await transition(conn, "exports", first, ["running"], "purged")
    before = await counts(engine)
    r = await keyed(logged_in_client, EXPORT)
    assert (*code_of(r), r.json()["detail"]) == (409, "conflict_state", "export_running")
    after = await counts(engine)
    assert (after.exports, after.running, after.keys) == (before.exports, 0, before.keys)


async def test_the_exports_list_finds_the_running_and_the_unexpired_ready_ones_again(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    exports_configured: Purger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M5 follow-ups Task F5 (a): after a reload the SPA finds its exports again with `GET
    /api/privacy/exports`: the running export and the ready ones before their `expires_at`, newest first, at
    most `LISTED_EXPORTS`, each by its id, state and times only (no size, no file name, no address to
    download from: the zip stays `GET /api/privacy/exports/{id}?download=1`'s, behind the session). A failed,
    expired or purged export, and a ready one past its `expires_at` that retention has not expired yet, is not
    listed."""
    assert (await logged_in_client.get(EXPORTS)).json() == {"items": []}
    ready = [(await ready_export(logged_in_client, engine, exports_configured))[0] for _ in range(6)]
    r = await keyed(logged_in_client, EXPORT)
    assert r.status_code == 202, r.text
    running = int(r.json()["export_id"])
    page = (await logged_in_client.get(EXPORTS)).json()
    assert set(page) == {"items"}
    assert privacy_routes.LISTED_EXPORTS == 5
    assert [(e["id"], e["status"]) for e in page["items"]] == [
        (running, "running"),
        *((i, "ready") for i in reversed(ready[2:])),
    ]
    for item in page["items"]:
        assert set(item) == {"id", "status", "created_at", "expires_at"}, item
        state = (await logged_in_client.get(f"/api/privacy/exports/{item['id']}")).json()
        assert {k: state[k] for k in item} == item
    # the running one fails, the newest ready one is purged: neither is listed
    async with engine.begin() as conn:
        await transition(conn, "exports", running, ["running"], "failed")
        await transition(conn, "exports", ready[-1], ["ready"], "purged", set_={"storage_path": None})
    page = (await logged_in_client.get(EXPORTS)).json()
    assert [e["id"] for e in page["items"]] == list(reversed(ready[:5]))
    # past their expiry by the route's clock, before the retention pass: no longer listed, still downloadable
    expires_at = (await logged_in_client.get(f"/api/privacy/exports/{ready[0]}")).json()["expires_at"]
    later = utcnow() + export.EXPORT_TTL + timedelta(minutes=1)
    monkeypatch.setattr(privacy_routes, "utcnow", lambda: later)
    assert (await logged_in_client.get(EXPORTS)).json() == {"items": []}
    r = await logged_in_client.get(f"/api/privacy/exports/{ready[0]}", params={"download": 1})
    assert r.status_code == 200, r.text
    # expired by the retention pass: not listed by the real clock either
    monkeypatch.setattr(privacy_routes, "utcnow", utcnow)
    assert len((await logged_in_client.get(EXPORTS)).json()["items"]) == 5
    await export.retain_exports(engine, now=later, epoch=1)
    assert (await logged_in_client.get(EXPORTS)).json() == {"items": []}
    state = (await logged_in_client.get(f"/api/privacy/exports/{ready[0]}")).json()
    assert (state["status"], state["expires_at"]) == ("expired", expires_at)


async def test_a_download_racing_a_purge_answers_410_not_500(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    exports_configured: Purger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Between the status read (`ready`) and the open, a purge's export op ends the row `purged` and unlinks
    the file: the route reads the row again and answers 410. A file gone while the row still names it is a
    broken invariant: 500."""
    export_id, row = await ready_export(logged_in_client, engine, exports_configured)
    archive = exports_configured.cfg.exports_dir / row.storage_path
    real = privacy_routes.read_export
    reads: list[str] = []

    async def purged_after_the_read(engine_: AsyncEngine, export_id_: int) -> Any:
        read = await real(engine_, export_id_)
        assert read is not None
        reads.append(read.status)
        if len(reads) == 1:
            async with engine.begin() as conn:
                await transition(
                    conn, "exports", export_id_, ["ready"], "purged", set_={"storage_path": None}
                )
            archive.unlink()
        return read

    monkeypatch.setattr(privacy_routes, "read_export", purged_after_the_read)
    r = await logged_in_client.get(f"/api/privacy/exports/{export_id}", params={"download": 1})
    assert code_of(r) == (410, "gone"), r.text
    assert reads == ["ready", "purged"]
    monkeypatch.setattr(privacy_routes, "read_export", real)
    other_id, other = await ready_export(logged_in_client, engine, exports_configured)
    (exports_configured.cfg.exports_dir / other.storage_path).unlink()
    r = await logged_in_client.get(f"/api/privacy/exports/{other_id}", params={"download": 1})
    assert code_of(r) == (500, "internal"), r.text


async def test_a_started_download_completes_after_its_file_is_unlinked(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    exports_configured: Purger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The archive is written in small segments, so its download takes several reads. Once the response has
    started (the first segment is decrypted before it), the file is unlinked, as a purge's export op unlinks
    it: the download reads on from its open file and completes."""
    monkeypatch.setattr(export, "SEGMENT_BYTES", 512)
    export_id, row = await ready_export(logged_in_client, engine, exports_configured)
    assert row.size_bytes > 4 * 512  # several segments
    expected = stored_plaintext(exports_configured, export_id, row.storage_path)
    archive = exports_configured.cfg.exports_dir / row.storage_path
    real = iter_decrypted  # the route's, from the export module
    served: list[int] = []

    def unlinked_once_started(source: Any, keys: Any, *, export_id: int) -> Any:
        for n, segment in enumerate(real(source, keys, export_id=export_id)):
            if n == 1:
                archive.unlink()  # the second segment is read during the response
            served.append(len(segment))
            yield segment

    monkeypatch.setattr(privacy_routes, "iter_decrypted", unlinked_once_started)
    r = await logged_in_client.get(f"/api/privacy/exports/{export_id}", params={"download": 1})
    assert r.status_code == 200, r.text
    assert r.content == expected and not archive.exists()
    assert len(served) > 4 and sum(served) == row.size_bytes
    assert set(EXPORT_FILES) <= set(zipfile.ZipFile(io.BytesIO(r.content)).namelist())


def segment_offsets(data: bytes) -> list[int]:
    """Where each segment's frame of a stored archive starts: a 4-byte big-endian length, then the blob."""
    offsets, offset = [], 0
    while offset < len(data):
        offsets.append(offset)
        offset += 4 + int.from_bytes(data[offset : offset + 4], "big")
    return offsets


async def raw_get(app: JobHunterApp, client: httpx.AsyncClient, path: str, query: bytes) -> list[Message]:
    """A GET of `client`'s session to `app`, driven with raw ASGI messages (an in-process client cannot
    return a response its app never ended); what the app sent."""
    token = client.cookies.get(sessions.SESSION_COOKIE)
    assert token is not None
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": query,
        "headers": [(b"cookie", f"{sessions.SESSION_COOKIE}={token}".encode())],
        "client": ("198.51.100.20", 50000),
        "server": ("testserver", 443),
    }
    sent: list[Message] = []
    requested = asyncio.Event()
    never = asyncio.Event()  # the client stays: no disconnect

    async def receive() -> Message:
        if requested.is_set():
            await never.wait()
        requested.set()
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await asyncio.wait_for(app(scope, receive, send), 60)
    return sent


async def test_a_damaged_segment_aborts_a_started_download_without_its_bytes(
    logged_in_client: httpx.AsyncClient,
    api_app: JobHunterApp,
    engine: AsyncEngine,
    exports_configured: Purger,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A segment that does not open (here the third, one byte of its sealed blob flipped) is met once the
    response has started: the stream stops before it, so the client gets the segments before it and none of
    its bytes, short of `Content-Length`; the failure is logged with the export's id and the error's class
    only, and nothing of a traceback or of the error's text reaches the body (Ruling T18-3, M4)."""
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(export, "SEGMENT_BYTES", 512)
    export_id, row = await ready_export(logged_in_client, engine, exports_configured)
    archive = exports_configured.cfg.exports_dir / row.storage_path
    keys = crypto.data_keys(exports_configured.cfg)
    with archive.open("rb") as stored:
        segments = list(iter_decrypted(stored, keys, export_id=export_id))
    assert len(segments) > 4
    data = bytearray(archive.read_bytes())
    third = segment_offsets(bytes(data))[2]
    data[third + 4 + 20] ^= 0xFF  # inside the third segment's blob: its tag no longer matches
    archive.write_bytes(bytes(data))
    sent = await raw_get(api_app, logged_in_client, f"/api/privacy/exports/{export_id}", b"download=1")
    start, *body = sent
    assert (start["type"], start["status"]) == ("http.response.start", 200)  # the first segment opened
    assert dict(start["headers"])[b"content-length"] == str(row.size_bytes).encode()
    assert all(m["type"] == "http.response.body" and m.get("more_body") for m in body)  # never ended
    received = b"".join(m["body"] for m in body)
    assert received == b"".join(segments[:2])  # not one byte of the damaged segment or after it
    for leaked in (b"Traceback", b"ExportCorrupt", b"does not open"):
        assert leaked not in received
    [aborted] = logged(caplog, "unhandled_exception_after_start")  # the edge's: the class only
    assert aborted["error_class"] == "ExportCorrupt"
    [failed] = logged(caplog, "export_download_failed")
    assert (failed["export_id"], failed["error_class"], failed["level"]) == (
        export_id,
        "ExportCorrupt",
        "error",
    )
    assert set(failed) <= {"event", "export_id", "error_class", "level", "logger", "ts", "request_id"}


# ---- delete everything and the source purge ---------------------------------------------------------------


async def test_delete_everything_is_idempotent_across_the_logout(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, purger: Purger
) -> None:
    """The purge deletes the requester's session, but keeps the request's idempotency row: after a new login,
    the same key returns the stored 202 with the same manifest, and no second purge exists."""
    key = fresh_key("everything")
    first = await keyed(logged_in_client, DELETE_EVERYTHING, delete_everything_body(), key=key)
    assert first.status_code == 202, first.text
    manifest_id = first.json()["manifest_id"]
    [operation_id] = await purge_operations(engine, manifest_id)
    await purger.run(operation_id)
    await completed(purger, manifest_id, operation_id)
    await relogged(logged_in_client)
    again = await keyed(logged_in_client, DELETE_EVERYTHING, delete_everything_body(), key=key)
    assert (again.status_code, again.json()) == (202, {"manifest_id": manifest_id})
    after = await counts(engine)
    assert (after.everything, after.purges) == (1, 1)


async def test_a_new_key_while_a_purge_runs_is_409(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    first = await keyed(logged_in_client, DELETE_EVERYTHING, delete_everything_body())
    assert first.status_code == 202, first.text
    r = await keyed(logged_in_client, DELETE_EVERYTHING, delete_everything_body())
    assert (*code_of(r), r.json()["detail"]) == (409, "conflict_state", "purge_in_progress")
    after = await counts(engine)
    assert (after.everything, after.purges) == (1, 1)


async def test_a_privacy_request_during_delete_everything_is_409_delete_everything_in_progress(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """The privacy routes pass the edge under delete-everything's fence; their own requests refuse it. The
    reads go on."""
    first = await keyed(logged_in_client, DELETE_EVERYTHING, delete_everything_body())
    manifest_id = first.json()["manifest_id"]
    for path, body in ((EXPORT, None), (PURGE_SOURCE, purge_source_body())):
        r = await keyed(logged_in_client, path, body)
        assert code_of(r) == (409, "delete_everything_in_progress"), (path, r.text)
    r = await logged_in_client.delete(f"/api/golden-cases/{await golden_case(engine)}")
    assert code_of(r) == (409, "delete_everything_in_progress")  # the edge's
    assert (await logged_in_client.get(f"{MANIFESTS}/{manifest_id}")).json()["status"] == "executing"
    after = await counts(engine)
    assert (after.exports, after.everything) == (0, 1)


async def test_purge_source_202_and_its_manifest_lists_deleted_golden_cases(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, purger: Purger
) -> None:
    case_id = await golden_case(engine, synthetic=True)
    async with engine.begin() as conn:
        await conn.execute(_TO_ADZUNA_ONLY, {"g": case_id})
    kept = await golden_case(engine, synthetic=True)
    r = await keyed(logged_in_client, PURGE_SOURCE, purge_source_body())
    assert r.status_code == 202, r.text
    manifest_id = r.json()["manifest_id"]
    manifest = (await logged_in_client.get(f"{MANIFESTS}/{manifest_id}")).json()
    assert {k: manifest[k] for k in ("id", "kind", "status", "deleted_golden_case_ids")} == {
        "id": manifest_id,
        "kind": "source_purge",
        "status": "executing",
        "deleted_golden_case_ids": [],  # the content is computed by the worker
    }
    [operation_id] = await purge_operations(engine, manifest_id)
    await purger.run(operation_id)
    await completed(purger, manifest_id, operation_id)
    manifest = (await logged_in_client.get(f"{MANIFESTS}/{manifest_id}")).json()
    assert (manifest["status"], manifest["deleted_golden_case_ids"]) == ("completed", [case_id])
    assert manifest["completed_at"] is not None and manifest["shipped_at"] is None
    async with engine.connect() as conn:
        assert (await conn.execute(_CASE_EXISTS, {"g": case_id})).scalar_one() is False
        assert (await conn.execute(_CASE_EXISTS, {"g": kept})).scalar_one() is True


async def test_purge_source_when_adzuna_is_the_only_source_is_422_naming_sources_enabled(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Ruling T18-1 (c): disabling the only enabled source is refused by the settings rule; the refusal
    reaches the client as 422 `validation_failed` naming `sources.enabled`, and nothing is written."""
    payload = copy.deepcopy((await settings_body(logged_in_client))["payload"])
    payload["sources"]["enabled"] = {name: name == "adzuna" for name in payload["sources"]["enabled"]}
    await save(logged_in_client, payload)
    before = await counts(engine)
    r = await keyed(logged_in_client, PURGE_SOURCE, purge_source_body())
    assert code_of(r) == (422, "validation_failed"), r.text
    assert [e["field"] for e in r.json()["errors"]] == ["payload.sources.enabled"]
    after = await counts(engine)
    assert (after.keys, after.purges) == (before.keys, before.purges)


# ---- the manifests -----------------------------------------------------------------------------------------


async def test_the_manifest_list_pages_newest_first_and_survives_delete_everything(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, purger: Purger, api_app: JobHunterApp
) -> None:
    ids = [await deleted_case(logged_in_client, engine, purger) for _ in range(3)]
    first = (await logged_in_client.get(MANIFESTS, params={"limit": 2})).json()
    assert [m["id"] for m in first["items"]] == [ids[2], ids[1]]
    assert {m["kind"] for m in first["items"]} == {"golden_case_delete"}
    second = (
        await logged_in_client.get(MANIFESTS, params={"limit": 2, "cursor": first["next_cursor"]})
    ).json()
    assert ([m["id"] for m in second["items"]], second["next_cursor"]) == ([ids[0]], None)
    foreign = encode_list_cursor(
        sort=GOLDEN_CASES_SORT,
        dir="desc",
        last_key=[ids[1]],
        filters={},
        key=cursor_key(api_app.api.state.cfg),
    )
    for cursor in ("not-a-cursor", foreign):  # another list's cursor is no cursor of this one
        r = await logged_in_client.get(MANIFESTS, params={"cursor": cursor})
        assert code_of(r) == (400, "invalid_cursor")
    everything = await keyed(logged_in_client, DELETE_EVERYTHING, delete_everything_body())
    manifest_id = everything.json()["manifest_id"]
    [operation_id] = await purge_operations(engine, manifest_id)
    await purger.run(operation_id)
    await relogged(logged_in_client)
    page = (await logged_in_client.get(MANIFESTS)).json()
    assert [(m["id"], m["kind"]) for m in page["items"]] == [
        (manifest_id, "delete_everything"),
        *((i, "golden_case_delete") for i in reversed(ids)),
    ]
    assert page["next_cursor"] is None


async def test_the_manifest_reports_shipped_only_after_shipping(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, purger: Purger
) -> None:
    manifest_id = await deleted_case(logged_in_client, engine, purger)
    url = f"{MANIFESTS}/{manifest_id}"
    manifest = (await logged_in_client.get(url)).json()
    assert (manifest["status"], manifest["shipped_at"]) == ("completed", None)
    assert manifest["deleted_golden_case_ids"] == []  # a source purge's list only
    await ship(engine, manifest_id, sha=b"\x00" * 32)  # another file's hash covers nothing
    assert (await logged_in_client.get(url)).json()["status"] == "completed"
    await ship(engine, manifest_id)
    manifest = (await logged_in_client.get(url)).json()
    assert manifest["status"] == "shipped" and manifest["shipped_at"] is not None
    assert code_of(await logged_in_client.get(f"{MANIFESTS}/987654321")) == (404, "not_found")


# ---- the golden-case deletion, the banner and Needs attention -------------------------------------------


async def test_golden_delete_is_202_with_a_manifest(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, purger: Purger
) -> None:
    case_id = await golden_case(engine)
    r = await logged_in_client.delete(f"/api/golden-cases/{case_id}")
    assert r.status_code == 202, r.text
    manifest_id = r.json()["manifest_id"]
    assert r.json() == {"manifest_id": manifest_id}
    manifest = (await logged_in_client.get(f"{MANIFESTS}/{manifest_id}")).json()
    assert (manifest["kind"], manifest["status"]) == ("golden_case_delete", "executing")
    async with engine.connect() as conn:  # the worker deletes it
        assert (await conn.execute(_CASE_EXISTS, {"g": case_id})).scalar_one() is True
    [operation_id] = await purge_operations(engine, manifest_id)
    await purger.run(operation_id)
    await completed(purger, manifest_id, operation_id)
    async with engine.connect() as conn:
        assert (await conn.execute(_CASE_EXISTS, {"g": case_id})).scalar_one() is False
    assert code_of(await logged_in_client.delete(f"/api/golden-cases/{case_id}")) == (404, "not_found")


async def test_the_deletion_unshipped_banner_follows_shipping(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, purger: Purger
) -> None:
    def deletion_banners(body: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
        return [
            (b["code"], b["severity"], b["params"])
            for b in body["banners"]
            if b["code"] == "deletion_unshipped"
        ]

    r = await logged_in_client.delete(f"/api/golden-cases/{await golden_case(engine)}")
    manifest_id = r.json()["manifest_id"]
    assert deletion_banners((await logged_in_client.get("/api/system/status")).json()) == []  # executing
    [operation_id] = await purge_operations(engine, manifest_id)
    await purger.run(operation_id)
    status = (await logged_in_client.get("/api/system/status")).json()
    assert deletion_banners(status) == [("deletion_unshipped", "warn", {})]
    await ship(engine, manifest_id)
    assert deletion_banners((await logged_in_client.get("/api/system/status")).json()) == []


async def test_needs_attention_lists_failed_purges(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, purger: Purger
) -> None:
    """One item per `executing` manifest whose newest purge operation failed, or which has none; a pending
    purge and a completed manifest are not listed."""
    failed = (await logged_in_client.delete(f"/api/golden-cases/{await golden_case(engine)}")).json()
    [failed_operation] = await purge_operations(engine, failed["manifest_id"])
    async with engine.begin() as conn:  # two earlier tries, each requeued
        await conn.execute(
            sa.text("UPDATE operations SET attempts = 2 WHERE id = :i"), {"i": failed_operation}
        )
        claim = await claim_operation(conn, failed_operation, epoch=1)
    assert claim is not None
    async with engine.begin() as conn:
        await finish_operation(conn, claim, status="failed", error_code="manifest_invalid")
    pending = await logged_in_client.delete(f"/api/golden-cases/{await golden_case(engine)}")
    assert pending.status_code == 202
    await deleted_case(logged_in_client, engine, purger)  # completed
    async with engine.begin() as conn:  # a manifest whose operation is gone (none)
        orphan = await insert_manifest(conn, kind="job_tombstone", request={"job_ids": [1]}, now=utcnow())
    section = (await logged_in_client.get("/api/system/needs-attention")).json()["failed_purges"]
    assert section["count"] == 2
    items = [{k: v for k, v in item.items() if k != "created_at"} for item in section["items"]]
    assert items == [
        {
            "manifest_id": orphan.id,
            "kind": "job_tombstone",
            "operation_id": None,
            "last_error_code": None,
            "attempts": 0,
        },
        {
            "manifest_id": failed["manifest_id"],
            "kind": "golden_case_delete",
            "operation_id": failed_operation,
            "last_error_code": "manifest_invalid",
            "attempts": 2,
        },
    ]
    assert all(item["created_at"] for item in section["items"])


async def test_every_privacy_write_refuses_a_missing_or_wrong_csrf_check(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """A logged-in session's write without `X-JH-Request: 1`, with another value, or from another origin is
    403 `csrf_failed` at the edge, before its route: the export, delete everything, the source purge, the
    purge retry and the golden-case deletion, each with what would otherwise pass (Ruling T18-3, M4)."""
    case_id = await golden_case(engine)
    before = await counts(engine)
    writes: list[tuple[str, str, dict[str, Any] | None]] = [
        ("POST", EXPORT, None),
        ("POST", DELETE_EVERYTHING, delete_everything_body()),
        ("POST", PURGE_SOURCE, purge_source_body()),
        ("POST", "/api/system/purges/1/retry", None),
        ("DELETE", f"/api/golden-cases/{case_id}", None),
    ]
    refusals: list[dict[str, str] | None] = [
        None,  # the header left out
        {"X-JH-Request": "0"},
        {"Origin": "https://evil.example"},
    ]
    for method, path, body in writes:
        for override in refusals:
            request = logged_in_client.build_request(
                method, path, json=body, headers={**idem(fresh_key("csrf")), **(override or {})}
            )
            if override is None:
                del request.headers["X-JH-Request"]
            r = await logged_in_client.send(request)
            assert code_of(r) == (403, "csrf_failed"), (method, path, override, r.text)
    assert await counts(engine) == before
    async with engine.connect() as conn:
        assert (await conn.execute(_CASE_EXISTS, {"g": case_id})).scalar_one() is True
        assert (await conn.execute(sa.text("SELECT count(*) FROM deletion_manifests"))).scalar_one() == 0


async def test_the_privacy_routes_need_a_session_and_the_writes_csrf(api_client: httpx.AsyncClient) -> None:
    for method, path in (
        ("GET", MANIFESTS),
        ("GET", f"{MANIFESTS}/1"),
        ("GET", "/api/privacy/exports/1"),
        ("GET", EXPORTS),
        ("POST", EXPORT),
        ("POST", DELETE_EVERYTHING),
        ("POST", PURGE_SOURCE),
    ):
        headers = {"X-JH-Request": "1", **idem(fresh_key("privacy"))}
        r = await api_client.request(method, path, json={} if method == "POST" else None, headers=headers)
        assert code_of(r) == (401, "unauthenticated"), (method, path)
    r = await api_client.post(EXPORT, headers=idem(fresh_key("privacy")))
    assert code_of(r) == (403, "csrf_failed")


async def test_a_purge_source_or_export_without_a_key_is_400(logged_in_client: httpx.AsyncClient) -> None:
    for path, body in (
        (EXPORT, None),
        (DELETE_EVERYTHING, delete_everything_body()),
        (PURGE_SOURCE, purge_source_body()),
    ):
        r = await logged_in_client.post(path, json=body)
        assert code_of(r) == (400, "idempotency_key_required"), path
