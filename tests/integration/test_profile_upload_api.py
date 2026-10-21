"""`POST /api/profile/cv` and `POST /api/profile/cv-text` (spec §12.1, §14.2; M2 Task 9).

Every name and datum here is invented (Erika Musterfrau, Max Mustermann, Musterfirma GmbH); no real CV.
"""

import asyncio
import hashlib
import json
import stat
import threading
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.api.app import JobHunterApp
from jobhunter.api.errors import Problem
from jobhunter.crypto import data_keys, decrypt
from jobhunter.db.transitions import transition
from jobhunter.llm import bounds
from jobhunter.profile import upload
from jobhunter.profile.storage import CvStore
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.fixtures import count_rows
from tests.support.pg import PgServer, blocked_or_done
from tests.support.procs import await_until
from tests.support.profile import DOCX_MIME, PDF_MIME, docx_bytes, minimal_pdf

pytestmark = pytest.mark.integration

CV_LINES = [
    "Erika Musterfrau",
    "Softwareentwicklerin, Musterfirma GmbH, 2019-2026",
    "Python, PostgreSQL, FastAPI",
]
PDF = minimal_pdf(CV_LINES)
PASTE = "Erika Musterfrau\nSoftwareentwicklerin bei der Musterfirma GmbH in Köln.\n" * 6


@pytest.fixture
def keys(api_app: JobHunterApp) -> list[bytes]:
    return data_keys(api_app.api.state.cfg)


async def post_cv(
    client: httpx.AsyncClient, name: str = "Lebenslauf.pdf", body: bytes = PDF, **data: str
) -> httpx.Response:
    return await client.post("/api/profile/cv", files={"file": (name, body)}, data=data or None)


async def post_text(client: httpx.AsyncClient, text: str = PASTE) -> httpx.Response:
    return await client.post("/api/profile/cv-text", json={"text": text})


async def one(engine: AsyncEngine, statement: str, **params: object) -> sa.Row[Any]:
    async with engine.connect() as conn:
        return (await conn.execute(sa.text(statement), params)).one()


async def cv_of(engine: AsyncEngine, draft_id: int) -> sa.Row[Any]:
    return await one(
        engine,
        "SELECT c.* FROM cv_files c JOIN profile_drafts d ON d.cv_file_id = c.id WHERE d.id = :d",
        d=draft_id,
    )


async def operations(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            sa.text("SELECT kind, payload, dedup_key, status FROM operations ORDER BY id")
        )
        return [tuple(row) for row in rows]


async def jobs(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    async with engine.connect() as conn:
        rows = await conn.execute(sa.text("SELECT task_name, args FROM procrastinate_jobs ORDER BY id"))
        return [tuple(row) for row in rows]


async def statuses(engine: AsyncEngine) -> dict[int, tuple[str, str | None]]:
    async with engine.connect() as conn:
        rows = await conn.execute(sa.text("SELECT id, status, status_reason FROM profile_drafts"))
        return {row.id: (row.status, row.status_reason) for row in rows}


async def seed_draft(engine: AsyncEngine, status: str) -> int:
    """A draft in `status`, inserted as it is (an INSERT passes no transition check), over a pasted file."""
    columns = {
        "failed": ("failure_code", "'low_text'"),
        "ready": ("reviewed_text_sha256, draft_json", "'\\x00', '{}'"),
    }.get(status, ("updated_at", "now()"))
    async with engine.begin() as conn:
        cv_id = (
            await conn.execute(
                sa.text(
                    "INSERT INTO cv_files (sha256, mime, size_bytes) "
                    "VALUES ('\\x00', 'text/plain', 1) RETURNING id"
                )
            )
        ).scalar_one()
        return int(
            (
                await conn.execute(
                    sa.text(
                        f"INSERT INTO profile_drafts (cv_file_id, status, {columns[0]}) "  # noqa: S608 - constants
                        f"VALUES (:cv, :s, {columns[1]}) RETURNING id"
                    ),
                    {"cv": cv_id, "s": status},
                )
            ).scalar_one()
        )


def errors_of(r: httpx.Response) -> list[tuple[str, str]]:
    return [(e["field"], e["code"]) for e in r.json().get("errors") or []]


# tests/integration/test_profile_upload_api.py (Review Focus 3 pin)
async def test_hostile_uploads_are_rejected_before_storage(logged_in_client, engine, cv_store_dir) -> None:
    cases = [
        ("big.pdf", b"%PDF-1.4\n" + b"0" * (10_485_760 + 1), 413, "too_large"),
        ("bomb.docx", docx_bytes(["x"], declared_expansion=60 * 2**20), 422, "docx_expansion"),
        ("macro.docx", docx_bytes(["x"], macro=True), 422, "docx_macro"),
        ("setup.pdf", b"MZ\x90\x00" + b"\x00" * 2048, 422, "unknown_type"),
        ("cv.docx", minimal_pdf(["Erika Musterfrau"]), 422, "extension_mismatch"),
    ]
    for name, body, status, reason in cases:
        r = await logged_in_client.post("/api/profile/cv", files={"file": (name, body)})
        assert r.status_code == status and r.json()["code"] == "upload_rejected", name
        assert r.json()["errors"][0]["code"] == reason
    assert await count_rows(engine, "cv_files") == 0 and list(cv_store_dir.iterdir()) == []


async def test_a_valid_pdf_is_stored_encrypted_with_an_uploaded_draft_and_its_operation(
    logged_in_client, engine, cv_store_dir, keys
) -> None:
    r = await post_cv(logged_in_client)
    assert r.status_code == 202 and set(r.json()) == {"draft_id"}
    draft_id = r.json()["draft_id"]
    cv = await cv_of(engine, draft_id)
    assert (cv.mime, cv.size_bytes, bytes(cv.sha256)) == (PDF_MIME, len(PDF), hashlib.sha256(PDF).digest())
    assert (cv.extraction_method, cv.native_pdf_opt_in, cv.raw_text_enc, cv.page_count) == (
        None,
        False,
        None,
        None,
    )
    stored = cv_store_dir / cv.storage_path
    assert list(cv_store_dir.iterdir()) == [stored] and stat.S_IMODE(stored.stat().st_mode) == 0o600
    sealed = stored.read_bytes()
    assert b"Erika" not in sealed and b"%PDF" not in sealed
    assert decrypt(keys, sealed, aad=b"cv_file:" + cv.storage_path.encode()) == PDF
    assert CvStore(cv_store_dir).read(cv.storage_path, keys) == PDF
    draft = await one(
        engine, "SELECT status, native_pdf, cv_file_id FROM profile_drafts WHERE id = :d", d=draft_id
    )
    assert (draft.status, draft.native_pdf, draft.cv_file_id) == ("uploaded", False, cv.id)
    assert await count_rows(engine, "cv_files") == 1 and await count_rows(engine, "profile_drafts") == 1


async def test_the_operation_names_the_draft_and_its_job_is_deferred(logged_in_client, engine) -> None:
    draft_id = (await post_cv(logged_in_client)).json()["draft_id"]
    assert await operations(engine) == [
        ("extract_profile_text", {"draft_id": draft_id}, f"extract_profile_text:{draft_id}", "pending")
    ]
    op_id = (await one(engine, "SELECT id FROM operations")).id
    assert await jobs(engine) == [("jh:run_operation", {"operation_id": op_id})]


async def test_a_valid_docx_is_accepted(logged_in_client, engine, cv_store_dir, keys) -> None:
    docx = docx_bytes(CV_LINES, [["2019-2026", "Musterfirma GmbH"]])
    r = await post_cv(logged_in_client, "Lebenslauf.DOCX", docx)
    assert r.status_code == 202
    cv = await cv_of(engine, r.json()["draft_id"])
    assert (cv.mime, cv.size_bytes) == (DOCX_MIME, len(docx))
    assert CvStore(cv_store_dir).read(cv.storage_path, keys) == docx


@pytest.mark.parametrize("state", ["uploaded", "extracting_text", "extracting_profile"])
async def test_an_upload_or_paste_while_a_draft_is_extracted_is_409(
    logged_in_client, engine, cv_store_dir, state
) -> None:
    first = (await post_cv(logged_in_client)).json()["draft_id"]
    if state != "uploaded":
        async with engine.begin() as conn:
            native = {"native_pdf": True} if state == "extracting_profile" else None
            await transition(conn, "profile_drafts", first, ["uploaded"], state, set_=native)
    for r in (await post_cv(logged_in_client, "Neu.pdf"), await post_text(logged_in_client)):
        assert r.status_code == 409 and r.json()["code"] == "conflict_state"
    assert await statuses(engine) == {first: (state, None)}
    assert await count_rows(engine, "cv_files") == 1 and len(list(cv_store_dir.iterdir())) == 1
    assert len(await operations(engine)) == 1


@pytest.mark.parametrize("route", ["cv", "cv-text"])
@pytest.mark.parametrize("open_state", ["text_review", "ready"])
async def test_a_new_upload_discards_the_reviewable_draft_and_every_failed_one(
    logged_in_client, engine, open_state, route
) -> None:
    failed = [await seed_draft(engine, "failed"), await seed_draft(engine, "failed")]
    discarded = await seed_draft(engine, "discarded")
    old = await seed_draft(engine, open_state)
    r = await (post_cv(logged_in_client) if route == "cv" else post_text(logged_in_client))
    assert r.status_code == 202
    new = r.json()["draft_id"]
    assert await statuses(engine) == {
        failed[0]: ("discarded", "superseded"),
        failed[1]: ("discarded", "superseded"),
        discarded: ("discarded", None),
        old: ("discarded", "superseded"),
        new: ("uploaded", None),
    }


async def test_a_failed_transaction_after_the_file_write_deletes_the_file(
    logged_in_client, engine, cv_store_dir, monkeypatch
) -> None:
    """Ruling 4: the file is written before the transaction; the transaction's failure removes it again."""
    ready = await seed_draft(engine, "ready")
    written: list[str] = []
    write = CvStore.write

    def spy(self: CvStore, data: bytes, keys: list[bytes]) -> Any:
        stored = write(self, data, keys)
        written.append(stored.storage_path)
        return stored

    async def fault(*args: object, **kwargs: object) -> tuple[int, bool]:
        assert (cv_store_dir / written[0]).exists()  # the file is on the volume while the transaction runs
        raise RuntimeError("injected transaction fault")

    monkeypatch.setattr(CvStore, "write", spy)
    monkeypatch.setattr(upload, "request_draft_operation", fault)
    r = await post_cv(logged_in_client)
    assert r.status_code == 500 and r.json()["code"] == "internal"
    assert len(written) == 1 and list(cv_store_dir.iterdir()) == []
    assert await count_rows(engine, "cv_files", mime=PDF_MIME) == 0
    assert await statuses(engine) == {ready: ("ready", None)}  # the discard rolled back with the rest
    assert await operations(engine) == [] and await jobs(engine) == []


# A row whose deferred foreign key (`profile_versions.draft_id`) names no draft: the INSERT takes it, the
# COMMIT refuses it. The transaction then fails at its commit, after the route's last statement in it.
_FAILS_AT_COMMIT = sa.text(
    "INSERT INTO profile_versions (version_no, draft_id, profile_json, profile_hash) "
    "VALUES (999999, -1, '{}', '\\x00')"
)


async def test_an_error_of_the_commit_itself_leaves_the_file(
    logged_in_client, engine, cv_store_dir, monkeypatch, log_events
) -> None:
    """The commit's outcome is unknown to the route, so the file stays: a committed row may name it, and an
    uncommitted one leaves an orphan for Task 23's `cv_store_orphans` rule."""
    request = upload.request_draft_operation

    async def then_fail_at_commit(conn: AsyncConnection, *args: Any, **kwargs: Any) -> tuple[int, bool]:
        created = await request(conn, *args, **kwargs)
        await conn.execute(_FAILS_AT_COMMIT)
        return created

    monkeypatch.setattr(upload, "request_draft_operation", then_fail_at_commit)
    r = await post_cv(logged_in_client)
    assert r.status_code == 500 and r.json()["code"] == "internal"
    assert len(list(cv_store_dir.iterdir())) == 1  # kept: here an orphan, since nothing was committed
    assert await count_rows(engine, "cv_files") == 0 and await count_rows(engine, "profile_versions") == 0
    assert [e["error_class"] for e in log_events() if e["event"] == "cv_upload_commit_failed"] == [
        "IntegrityError"
    ]


async def test_a_failing_delete_never_hides_the_transaction_error(
    logged_in_client, engine, cv_store_dir, monkeypatch, log_events
) -> None:
    async def fault(*args: object, **kwargs: object) -> tuple[int, bool]:
        raise RuntimeError("injected transaction fault")

    def refuse(self: CvStore, storage_path: str) -> None:
        raise PermissionError("injected delete fault")

    monkeypatch.setattr(upload, "request_draft_operation", fault)
    monkeypatch.setattr(CvStore, "delete", refuse)
    r = await post_cv(logged_in_client)
    assert r.status_code == 500 and r.json()["code"] == "internal"
    events = log_events()
    assert [e["error_class"] for e in events if e["event"] == "cv_file_delete_failed"] == ["PermissionError"]
    assert [e["error_class"] for e in events if e["event"] == "unhandled_exception"] == ["RuntimeError"]
    assert "injected" not in json.dumps(events)  # classes only, never a message
    assert len(list(cv_store_dir.iterdir())) == 1 and await count_rows(engine, "cv_files") == 0


async def test_the_route_checks_the_file_off_the_event_loop(logged_in_client, monkeypatch) -> None:
    """`check_file` is pure and a hostile central directory costs seconds: it runs in a worker thread."""
    loop_thread, check_threads = threading.get_ident(), []
    check_file = upload.check_file

    def spy(head: bytes, data: bytes, filename: str) -> upload.Detected:
        check_threads.append(threading.get_ident())
        return check_file(head, data, filename)

    monkeypatch.setattr(upload, "check_file", spy)
    assert (await post_cv(logged_in_client)).status_code == 202
    assert len(check_threads) == 1 and check_threads[0] != loop_thread


async def test_a_paste_body_over_1_mib_is_413_before_it_is_parsed(logged_in_client, engine) -> None:
    """Declared with a `Content-Length`, or streamed without one (chunked): both stop at the cap."""
    oversized = json.dumps({"text": "Erika Musterfrau " * (2**20 // 16)}).encode()
    assert len(oversized) > upload.PASTE_BODY_MAX_BYTES

    async def stream() -> AsyncIterator[bytes]:
        for i in range(0, len(oversized), 65536):
            yield oversized[i : i + 65536]

    declared = await logged_in_client.post(
        "/api/profile/cv-text", content=oversized, headers={"Content-Type": "application/json"}
    )
    streamed = await logged_in_client.post(
        "/api/profile/cv-text", content=stream(), headers={"Content-Type": "application/json"}
    )
    assert declared.request.headers["content-length"] == str(len(oversized))
    assert "content-length" not in streamed.request.headers
    for r in (declared, streamed):
        assert (r.status_code, r.json()["code"], errors_of(r)) == (
            413,
            "upload_rejected",
            [("text", "too_large")],
        )
    assert await count_rows(engine, "cv_files") == 0
    assert (await post_text(logged_in_client)).status_code == 202  # under the cap the body reaches FastAPI


async def test_a_native_opt_in_while_native_pdf_is_disabled_is_422_and_stores_nothing(
    logged_in_client, engine, cv_store_dir, monkeypatch
) -> None:
    monkeypatch.setattr(bounds, "NATIVE_PDF_ENABLED", False)
    r = await post_cv(logged_in_client, native_pdf_opt_in="true")
    assert r.status_code == 422 and r.json()["code"] == "validation_failed"
    assert errors_of(r) == [("native_pdf_opt_in", "native_pdf_disabled")]
    assert await count_rows(engine, "cv_files") == 0 and await count_rows(engine, "profile_drafts") == 0
    assert await operations(engine) == [] and list(cv_store_dir.iterdir()) == []
    assert (await post_cv(logged_in_client, native_pdf_opt_in="false")).status_code == 202


async def test_a_native_opt_in_opens_the_draft_for_the_native_pdf_extraction(
    logged_in_client, engine
) -> None:
    assert bounds.NATIVE_PDF_ENABLED  # V19 enabled the native path (Task 7)
    r = await post_cv(logged_in_client, native_pdf_opt_in="true")
    assert r.status_code == 202
    draft_id = r.json()["draft_id"]
    cv = await cv_of(engine, draft_id)
    assert (cv.mime, cv.native_pdf_opt_in, cv.extraction_method) == (PDF_MIME, True, "native_pdf")
    draft = await one(engine, "SELECT status, native_pdf FROM profile_drafts WHERE id = :d", d=draft_id)
    assert (draft.status, draft.native_pdf) == ("extracting_profile", True)
    assert await operations(engine) == [
        ("extract_profile", {"draft_id": draft_id}, f"extract_profile:{draft_id}", "pending")
    ]


async def test_a_native_opt_in_for_a_docx_is_422(logged_in_client, engine, cv_store_dir) -> None:
    r = await post_cv(logged_in_client, "Lebenslauf.docx", docx_bytes(CV_LINES), native_pdf_opt_in="true")
    assert r.status_code == 422 and r.json()["code"] == "validation_failed"
    assert errors_of(r) == [("native_pdf_opt_in", "pdf_only")]
    assert await count_rows(engine, "cv_files") == 0 and list(cv_store_dir.iterdir()) == []


async def test_pasted_text_is_sealed_in_its_row_and_never_becomes_a_file(
    logged_in_client, engine, cv_store_dir, keys
) -> None:
    for text, reason in (("x" * 299, "too_short"), ("x" * 50_001, "too_long")):
        r = await post_text(logged_in_client, text)
        assert r.status_code == 422 and r.json()["code"] == "upload_rejected"
        assert errors_of(r) == [("text", reason)]
    lone_surrogate = b'{"text": "' + b"x" * 400 + b'\\ud800"}'
    r = await logged_in_client.post(
        "/api/profile/cv-text", content=lone_surrogate, headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 422 and errors_of(r) == [("text", "not_utf8")]
    assert await count_rows(engine, "cv_files") == 0
    r = await post_text(logged_in_client)
    assert r.status_code == 202
    draft_id = r.json()["draft_id"]
    cv = await cv_of(engine, draft_id)
    utf8 = PASTE.encode()
    assert (cv.mime, cv.extraction_method, cv.storage_path, cv.native_pdf_opt_in) == (
        "text/plain",
        "pasted",
        None,
        False,
    )
    assert (cv.size_bytes, bytes(cv.sha256)) == (len(utf8), hashlib.sha256(utf8).digest())
    assert b"Erika" not in cv.raw_text_enc
    assert CvStore(cv_store_dir).decrypt_text(cv.raw_text_enc, keys, cv_file_id=cv.id) == PASTE
    assert list(cv_store_dir.iterdir()) == []
    assert await operations(engine) == [
        ("extract_profile_text", {"draft_id": draft_id}, f"extract_profile_text:{draft_id}", "pending")
    ]


@pytest.mark.parametrize(
    ("kwargs", "status", "code", "errors"),
    [
        ({"files": {"file": ("cv.pdf", b"")}}, 422, "upload_rejected", [("file", "unknown_type")]),
        ({"files": {"other": ("cv.pdf", PDF)}}, 422, "validation_failed", [("file", "missing")]),
        (
            {"files": [("file", ("a.pdf", PDF)), ("file", ("b.pdf", PDF))]},
            422,
            "validation_failed",
            [("file", "duplicate")],
        ),
        ({"json": {"file": "Erika Musterfrau"}}, 422, "validation_failed", [("body", "multipart_required")]),
        (
            {"content": b"--x\r\n", "headers": {"Content-Type": "multipart/form-data"}},
            422,
            "validation_failed",
            [("body", "boundary_missing")],
        ),
    ],
    ids=["empty-file", "no-file-part", "two-file-parts", "json-body", "no-boundary"],
)
async def test_malformed_upload_requests_store_nothing(
    logged_in_client, engine, cv_store_dir, kwargs, status, code, errors
) -> None:
    r = await logged_in_client.post("/api/profile/cv", **kwargs)
    assert (r.status_code, r.json()["code"], errors_of(r)) == (status, code, errors)
    assert await count_rows(engine, "cv_files") == 0 and list(cv_store_dir.iterdir()) == []


async def test_the_upload_routes_need_a_session_and_the_csrf_header(
    api_client, logged_in_client, engine, cv_store_dir
) -> None:
    requests: list[tuple[str, dict[str, Any]]] = [
        ("/api/profile/cv", {"files": {"file": ("cv.pdf", PDF)}}),
        ("/api/profile/cv-text", {"json": {"text": PASTE}}),
    ]
    for path, kwargs in requests:
        r = await api_client.post(path, headers={"X-JH-Request": "1"}, **kwargs)
        assert r.status_code == 401 and r.json()["code"] == "unauthenticated", path
        r = await logged_in_client.post(path, headers={"X-JH-Request": "0"}, **kwargs)
        assert r.status_code == 403 and r.json()["code"] == "csrf_failed", path
    assert await count_rows(engine, "cv_files") == 0 and list(cv_store_dir.iterdir()) == []


async def test_logs_carry_ids_sizes_and_codes_never_a_name_or_content(logged_in_client, log_events) -> None:
    """Ruling 3: no file name, file content or pasted text reaches a log line."""
    assert (await post_cv(logged_in_client, "Erika-Musterfrau-Lebenslauf.pdf")).status_code == 202
    macro = await post_cv(logged_in_client, "Max-Mustermann.docx", docx_bytes(["Max Mustermann"], macro=True))
    assert macro.status_code == 422  # the checks come first
    conflict = await post_cv(logged_in_client, "Max-Mustermann.docx", docx_bytes(["Max Mustermann"]))
    assert conflict.status_code == 409  # the first draft is still `uploaded`
    assert (await post_text(logged_in_client)).status_code == 409
    # python-multipart's parse warnings quote a body byte (here the \x01): held at ERROR, they never show.
    malformed = b'--x\r\nContent-Disposition: form-data; name="file"\r\nX\x01Y: Musterfrau\r\n\r\n'
    malformed += b"x\r\n--x--\r\n"
    r = await logged_in_client.post(
        "/api/profile/cv", content=malformed, headers={"Content-Type": "multipart/form-data; boundary=x"}
    )
    assert (r.status_code, errors_of(r)) == (422, [("body", "malformed_multipart")])
    events = log_events()
    assert [e["logger"] for e in events if e["logger"].startswith("python_multipart")] == []
    assert [(e["status"], e["code"]) for e in events if e["event"] == "cv_upload_refused"] == [
        (422, "validation_failed")
    ]
    rendered = json.dumps(events)
    for secret in ("Musterfrau", "Mustermann", "Lebenslauf", "Softwareentwicklerin", "Musterfirma"):
        assert secret not in rendered, secret
    uploaded = [e for e in events if e["event"] == "cv_uploaded"]
    assert len(uploaded) == 1 and uploaded[0]["size_bytes"] == len(PDF) and "draft_id" in uploaded[0]


async def test_rejections_are_logged_with_their_reason_only(logged_in_client, log_events) -> None:
    r = await post_cv(logged_in_client, "Erika-Musterfrau.docx", docx_bytes(["Erika Musterfrau"], macro=True))
    assert r.status_code == 422
    r = await post_text(logged_in_client, "Erika Musterfrau")
    assert r.status_code == 422
    events = [e for e in log_events() if e["event"] == "cv_upload_rejected"]
    assert [e["reason"] for e in events] == ["docx_macro", "too_short"]
    assert "Musterfrau" not in json.dumps(log_events())


async def test_two_first_uploads_at_once_open_one_draft_and_the_other_is_409(
    engine: AsyncEngine, migrated: PgServer
) -> None:
    """Neither sees an open draft to lock, so the second waits on the first's insert into the one-open index
    and then gets 409, not a unique violation."""
    cv = upload.NewCvFile(
        mime=PDF_MIME,
        sha256=hashlib.sha256(PDF).digest(),
        size_bytes=len(PDF),
        storage_path="00000000-0000-4000-8000-000000000000.bin",
    )

    async def open_draft(conn: AsyncConnection) -> int:
        return await upload.open_new_draft(conn, cv=cv, native_pdf=False, defer=None)

    async def rival() -> int:
        async with engine.begin() as conn:
            return await open_draft(conn)

    async with engine.connect() as first:
        tx = await first.begin()
        draft_id = await open_draft(first)
        task = asyncio.create_task(rival())
        try:
            await await_until(lambda: blocked_or_done(migrated, task), 5)
            assert not task.done()
        finally:
            await tx.commit()
        with pytest.raises(Problem) as exc:
            await asyncio.wait_for(task, 5)
    assert (exc.value.status, exc.value.code) == (409, "conflict_state")
    assert await statuses(engine) == {draft_id: ("uploaded", None)}
    assert await count_rows(engine, "operations", kind=("extract_profile_text", "extract_profile")) == 1
