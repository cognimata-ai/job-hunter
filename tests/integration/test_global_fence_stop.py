"""The global fence as an admission stop (plan M5 Task 8, Deviation 15, P3, D15).

While delete-everything's `global` fence stands:

- the operation claim leaves a non-`purge` operation `pending`, `not_before` one minute ahead and no attempt
  spent, and claims a `purge` operation as always; a handler that waits on a condition of its own ends its
  claim with `wait_in`, which spends no attempt either;
- the api's state-changing requests answer 409 `delete_everything_in_progress` at the edge, before routing
  and before their bodies are read, except the login, the logout, the purge retry and every route under
  `/api/privacy/` (matched by method and path, `edge.fence_exempt`); reads are unaffected;
- every transaction a guarded request begins reads the global fence as its first statement
  (`db.fence_guard`), so a request that passed the edge before the fence committed writes nothing after it:
  the CV upload (which takes no `active_config`) and a settings save among them. Transactions outside a
  guarded request are not guarded; a 500's alert and a read snapshot run outside the guard, and the
  dry-run's `SET TRANSACTION READ ONLY` follows the guard's read.

The fence is the one delete-everything's A0 commits (a `global` fence over a manifest of its own,
`tests.support.fences`); a fence that has to commit in the middle of a request is committed by a hook of the
request's own path, on a connection of its own. Every datum is invented.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi import APIRouter
from jobhunter.api import edge
from jobhunter.api.app import JobHunterApp, create_app
from jobhunter.api.routes import groups as groups_route
from jobhunter.api.routes import settings as settings_route
from jobhunter.api_idempotency import check_idempotency_key
from jobhunter.auth import sessions
from jobhunter.db import fence_guard
from jobhunter.db.guards import GLOBAL_FENCE_SQL
from jobhunter.db.snapshot import read_snapshot
from jobhunter.db.sql import pg_error
from jobhunter.errors import FencedOut, Refusal
from jobhunter.worker import operations as ops
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import Message, Receive

from tests.support.fences import insert_test_fence
from tests.support.fixtures import TEST_PASSWORD, TEST_USERNAME, ConfigFactory, count_rows
from tests.support.jobs_list import Card, seed_cards
from tests.support.pg import PgServer, pg_one
from tests.support.profile import PDF_MIME, idem, minimal_pdf
from tests.support.settings import fresh_key, settings_body

pytestmark = pytest.mark.integration

DELETE_EVERYTHING: Final = "delete_everything_in_progress"
# A0 of delete-everything as one autocommitted statement: the manifest, then the `global` fence over it.
_COMMIT_GLOBAL_FENCE: Final = (
    "WITH m AS (INSERT INTO deletion_manifests (manifest_uuid, kind, status) "
    "VALUES (gen_random_uuid(), 'delete_everything', 'executing') RETURNING id) "
    "INSERT INTO purge_fences (kind, manifest_id) SELECT 'global', id FROM m RETURNING id"
)
_OPERATION: Final = sa.text(
    "SELECT status, attempts, claim_id, last_error_code, not_before, not_before - now() AS wait "
    "FROM operations WHERE id = :i"
)
_DUE: Final = sa.text("UPDATE operations SET not_before = now() - interval '1 second' WHERE id = :i")
_RELEASE_ALL: Final = sa.text("UPDATE purge_fences SET released_at = now() WHERE released_at IS NULL")
_DB_NOW: Final = sa.text("SELECT now()")
_TARGET_STATE: Final = sa.text("SELECT state FROM score_targets WHERE job_id = :j")
_FLAG: Final = sa.text("SELECT score_individually FROM duplicate_group_members WHERE job_id = :j")


async def global_fence(engine: AsyncEngine) -> int:
    """Delete-everything's A0: its `global` fence, committed under `active_config FOR UPDATE`; its id."""
    async with engine.begin() as conn:
        await conn.execute(sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE"))
        return await insert_test_fence(conn, kind="global")


def commit_global_fence(server: PgServer) -> None:
    """A0 committed from the middle of a request, on a connection of its own (sync: a hook of the request's
    path runs it)."""
    assert pg_one(server, "app", _COMMIT_GLOBAL_FENCE) is not None


async def operation(engine: AsyncEngine, operation_id: int) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(_OPERATION, {"i": operation_id})).one()


async def new_operation(engine: AsyncEngine, kind: ops.OperationKind) -> int:
    async with engine.begin() as conn:
        operation_id, _ = await ops.create_operation(
            conn, kind=kind, payload={"manifest_id": 1}, dedup_key=f"t8:{uuid4()}", defer=None
        )
    return operation_id


async def claim(engine: AsyncEngine, operation_id: int) -> ops.OperationClaim | None:
    async with engine.begin() as conn:
        return await ops.claim_operation(conn, operation_id, epoch=1)


@contextmanager
def statements_of(engine: AsyncEngine) -> Iterator[list[str]]:
    """The statements `engine` runs inside the block, whitespace-normalised, in order."""
    seen: list[str] = []

    def record(*args: Any) -> None:  # before_cursor_execute(conn, cursor, statement, parameters, ...)
        seen.append(" ".join(str(args[2]).split()))

    sa.event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", record)


@contextmanager
def guarded() -> Iterator[None]:
    """The block as part of a guarded request (`fence_guard.WRITE_GUARD` set, as the edge sets it)."""
    token = fence_guard.WRITE_GUARD.set(True)
    try:
        yield
    finally:
        fence_guard.WRITE_GUARD.reset(token)


def code_of(r: httpx.Response) -> tuple[int, str]:
    return r.status_code, r.json()["code"]


# ---- the operation claim and the handler's wait ------------------------------------------------------------


async def test_a_non_purge_operation_is_not_claimed_under_a_global_fence(
    engine: AsyncEngine, current_epoch: int
) -> None:
    assert ops.GLOBAL_FENCE_RETRY == timedelta(minutes=1)
    waiting = await new_operation(engine, "watchlist_validate")
    await global_fence(engine)
    assert await claim(engine, waiting) is None
    row = await operation(engine, waiting)
    assert (row.status, row.attempts, row.claim_id) == ("pending", 0, None)
    assert timedelta(seconds=50) < row.wait <= ops.GLOBAL_FENCE_RETRY  # the database's now, one minute on
    # not due yet: nothing happens, and the wait is not moved again
    assert await claim(engine, waiting) is None
    assert (await operation(engine, waiting)).not_before == row.not_before
    async with engine.begin() as conn:
        await conn.execute(_RELEASE_ALL)
        await conn.execute(_DUE, {"i": waiting})
    claimed = await claim(engine, waiting)
    assert claimed is not None and claimed.attempts == 0  # the wait spent no attempt


async def test_a_purge_operation_is_claimed_under_a_global_fence(
    engine: AsyncEngine, current_epoch: int
) -> None:
    purge = await new_operation(engine, "purge")
    await global_fence(engine)
    claimed = await claim(engine, purge)
    assert claimed is not None and claimed.kind == "purge"
    assert (await operation(engine, purge)).status == "running"


async def test_a_wait_spends_no_attempt(engine: AsyncEngine, current_epoch: int) -> None:
    waiting = await new_operation(engine, "export")
    async with engine.connect() as conn:
        base = (await conn.execute(_DB_NOW)).scalar_one() - timedelta(hours=1)  # every wait already due
    last: ops.OperationClaim | None = None
    for n in range(12):  # past `MAX_OPERATION_ATTEMPTS`: a requeue each time would have failed it
        last = await claim(engine, waiting)
        assert last is not None and last.attempts == 0, n
        context = ops.OperationContext(claim=last, engine=engine)
        async with engine.begin() as conn:
            await context.wait_in(
                conn, error_code="purge_in_progress", not_before=base + timedelta(minutes=n)
            )
        assert context.ended == ops.OperationEnd("waited", "pending", "purge_in_progress")
        row = await operation(engine, waiting)
        assert (row.status, row.attempts, row.claim_id, row.last_error_code, row.not_before) == (
            "pending",
            0,
            None,
            "purge_in_progress",
            base + timedelta(minutes=n),
        )
    assert ops.MAX_OPERATION_ATTEMPTS < 12 and last is not None
    with pytest.raises(FencedOut):  # a claim that no longer owns the row writes nothing
        async with engine.begin() as conn:
            await ops.wait_operation(conn, last, error_code="purge_in_progress", not_before=base)
    assert (await operation(engine, waiting)).not_before == base + timedelta(minutes=11)


# ---- the edge ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "exempt"),
    [
        ("POST", "/api/auth/login", True),
        ("POST", "/api/auth/logout", True),
        ("POST", "/api/system/purges/7/retry", True),
        ("POST", "/api/system/purges/123456789/retry", True),
        ("POST", "/api/privacy/exports", True),
        ("DELETE", "/api/privacy/exports/3", True),
        ("POST", "/api/privacy/delete-everything", True),
        ("PUT", "/api/auth/login", False),
        ("DELETE", "/api/auth/logout", False),
        ("PUT", "/api/system/purges/7/retry", False),
        ("POST", "/api/system/purges/7/retry/x", False),
        ("POST", "/api/system/purges/x/retry", False),
        ("POST", "/api/system/purges//retry", False),
        ("POST", "/api/system/purges/7/retry\n", False),
        ("POST", "/api/system/purges/\u0667/retry", False),  # a digit, but no ASCII one
        ("POST", "/api/privacyx", False),
        ("POST", "/api/privacy", False),
        ("POST", "/api/settings", False),
        ("PUT", "/api/jobs/1/actions", False),
    ],
)
def test_fence_exempt_matches_method_and_path(method: str, path: str, exempt: bool) -> None:
    assert edge.fence_exempt(method, path) is exempt


async def test_api_writes_answer_409_delete_everything_in_progress(
    logged_in_client: httpx.AsyncClient, api_app: JobHunterApp, engine: AsyncEngine, active_tuple: int
) -> None:
    (job,) = await seed_cards(engine, [Card(state="pending")])
    active = await settings_body(logged_in_client)
    versions = await count_rows(engine, "settings_versions")
    await global_fence(engine)
    answers = [
        await logged_in_client.put(f"/api/jobs/{job}/actions", json={"saved": True}),
        await logged_in_client.post(
            "/api/settings",
            json={
                "payload": active["payload"],
                "base_version_id": active["version"]["id"],
                "acknowledged_warnings": [],
            },
            headers=idem(fresh_key()),
        ),
        await logged_in_client.post(
            "/api/runs", json={"include_paid": False}, headers=idem(fresh_key("run"))
        ),
    ]
    for r in answers:
        assert code_of(r) == (409, DELETE_EVERYTHING), r.text
        assert r.headers["content-type"] == "application/problem+json"
    assert await count_rows(engine, "user_job_actions") == 0 and await count_rows(engine, "runs") == 0
    assert await count_rows(engine, "settings_versions") == versions
    # before routing and before the body is read: the app's `receive` is never called
    received: list[Message] = []

    async def receive() -> Message:
        received.append({"type": "http.request"})
        return {"type": "http.request", "body": b'{"include_paid": false}', "more_body": False}

    sent = await call(
        api_app, logged_in_client, "POST", "/api/runs", receive, [(b"content-type", b"application/json")]
    )
    assert sent[0]["status"] == 409 and received == []


async def test_login_logout_privacy_and_purge_retry_are_exempt_before_routing(
    logged_in_client: httpx.AsyncClient, api_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await global_fence(engine)
    r = await api_client.post(
        "/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD + "x"}
    )
    assert r.status_code == 401, r.text
    # Each answer is its route's own, after the edge let the request through (the edge's would be 409
    # `delete_everything_in_progress`): the retry of an operation that does not exist, a privacy request
    # without its `Idempotency-Key`.
    r = await logged_in_client.post("/api/system/purges/1/retry")
    assert code_of(r) == (404, "not_found"), r.text
    r = await logged_in_client.post("/api/privacy/export", json={})
    assert code_of(r) == (400, "idempotency_key_required"), r.text
    r = await logged_in_client.post("/api/auth/logout")
    assert r.status_code == 204, r.text


async def test_reads_are_unaffected(logged_in_client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    await global_fence(engine)
    for path in ("/api/settings", "/api/auth/session", "/api/jobs"):
        r = await logged_in_client.get(path)  # the session read slides the session: a write, unguarded
        assert r.status_code == 200, (path, r.text)


# ---- the guard ---------------------------------------------------------------------------------------------


async def call(
    app: JobHunterApp,
    client: httpx.AsyncClient,
    method: str,
    path: str,
    receive: Receive,
    headers: list[tuple[bytes, bytes]],
) -> list[Message]:
    """One request of `client`'s session to `app`, driven with raw ASGI messages; what the app sent."""
    token = client.cookies.get(sessions.SESSION_COOKIE)
    assert token is not None
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [
            *headers,
            (b"origin", b"https://testserver"),
            (b"x-jh-request", b"1"),
            (b"cookie", f"{sessions.SESSION_COOKIE}={token}".encode()),
        ],
        "client": ("198.51.100.20", 50000),
        "server": ("testserver", 443),
    }
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


async def test_an_upload_whose_transaction_begins_after_the_fence_commit_is_refused(
    logged_in_client: httpx.AsyncClient, api_app: JobHunterApp, engine: AsyncEngine, cv_store_dir: Path
) -> None:
    upload = httpx.Request(
        "POST",
        "https://testserver/api/profile/cv",
        files={"file": ("Lebenslauf.pdf", minimal_pdf(["Erika Musterfrau", "Musterfirma GmbH"]), PDF_MIME)},
    )
    body = upload.read()
    reached, go, done = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def receive() -> Message:
        if reached.is_set():  # the body was given: the client waits for the answer
            await done.wait()
            return {"type": "http.disconnect"}
        reached.set()  # the route reads the body after the edge's read and the session's transaction
        await go.wait()
        return {"type": "http.request", "body": body, "more_body": False}

    headers = [(b"content-type", upload.headers["content-type"].encode())]
    task = asyncio.create_task(call(api_app, logged_in_client, "POST", "/api/profile/cv", receive, headers))
    await asyncio.wait_for(reached.wait(), 10)
    await global_fence(engine)  # A0 commits while the body is held
    go.set()
    sent = await asyncio.wait_for(task, 30)
    done.set()
    assert sent[0]["status"] == 409
    assert b'"code":"delete_everything_in_progress"' in b"".join(m.get("body", b"") for m in sent[1:])
    for table in ("cv_files", "profile_drafts", "operations"):
        assert await count_rows(engine, table) == 0, table
    assert await asyncio.to_thread(os.listdir, cv_store_dir) == []  # the stored file was removed


async def test_a_settings_save_racing_the_fence_commit_is_refused_by_the_guard(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    migrated: PgServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = await settings_body(logged_in_client)
    versions = await count_rows(engine, "settings_versions")

    def fence_then_check(key: str | None) -> str:
        # after the edge's read and the session's transaction, right before the save's own transaction
        commit_global_fence(migrated)
        return check_idempotency_key(key)

    monkeypatch.setattr(settings_route, "check_idempotency_key", fence_then_check)
    r = await logged_in_client.post(
        "/api/settings",
        json={
            "payload": active["payload"],
            "base_version_id": active["version"]["id"],
            "acknowledged_warnings": [],
        },
        headers=idem(fresh_key()),
    )
    assert code_of(r) == (409, DELETE_EVERYTHING), r.text
    assert await count_rows(engine, "settings_versions") == versions
    assert await count_rows(engine, "api_idempotency") == 0


async def test_the_guard_ignores_unguarded_transactions(
    api_app: JobHunterApp, api_client: httpx.AsyncClient, make_user: Any, engine: AsyncEngine
) -> None:
    await make_user()
    fence_id = await global_fence(engine)
    with statements_of(engine) as seen:
        async with engine.begin() as conn:  # the worker's: outside any request
            await conn.execute(sa.text("SELECT 1"))
        r = await api_client.post(  # an exempt request's: the login, under the fence
            "/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD + "x"}
        )
        assert r.status_code == 401, r.text
    assert GLOBAL_FENCE_SQL not in seen
    assert await count_rows(engine, "login_attempts") == 1  # the exempt login wrote its attempt
    # a transaction of a guarded request: the fence read first, and nothing after it
    with statements_of(engine) as seen, guarded(), pytest.raises(Refusal) as refused:
        async with engine.begin() as conn:
            await conn.execute(sa.text("SELECT 1"))
    assert (refused.value.status, refused.value.code) == (409, DELETE_EVERYTHING)
    assert seen == [GLOBAL_FENCE_SQL]
    # installed once, whatever the calls: one read per guarded transaction
    fence_guard.install_fence_guard(engine)
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE purge_fences SET released_at = now() WHERE id = :f"), {"f": fence_id}
        )
    with statements_of(engine) as seen, guarded():
        async with engine.begin() as conn:
            await conn.execute(sa.text("SELECT 1"))
    assert seen == [GLOBAL_FENCE_SQL, "SELECT 1"]


async def test_a_500_under_the_guard_still_persists_its_alert(
    engine: AsyncEngine, make_config: ConfigFactory, cv_store_dir: Path
) -> None:
    refusals: list[str] = []
    router = APIRouter()

    @router.post("/api/test/fence-then-fail")
    async def fence_then_fail() -> None:
        async with engine.begin() as conn:  # begins before the fence: let through, commits A0
            await insert_test_fence(conn, kind="global")
        try:
            async with engine.begin() as conn:  # begins after it: refused by the guard
                await conn.execute(sa.text("SELECT 1"))
        except Refusal as refusal:
            refusals.append(refusal.code)
        raise RuntimeError("an invented failure")

    cfg = make_config("api", JOBHUNTER_CV_STORE_DIR=str(cv_store_dir))
    app = create_app(cfg=cfg, engine=engine, extra_routers=[router])
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False, client=("198.51.100.30", 50000))
    headers = {"Origin": "https://testserver", "X-JH-Request": "1"}
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver", headers=headers
    ) as client:
        r = await client.post("/api/test/fence-then-fail")
    assert code_of(r) == (500, "internal") and refusals == [DELETE_EVERYTHING]
    assert await count_rows(engine, "alerts", code="api.unhandled_exception") == 1


@pytest.mark.parametrize("fence_before_snapshot", [False, True])
async def test_group_split_and_score_individually_answer_after_their_commit(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    migrated: PgServer,
    active_tuple: int,
    monkeypatch: pytest.MonkeyPatch,
    fence_before_snapshot: bool,
) -> None:
    rep, split, individual = await seed_cards(
        engine, [Card(state="pending"), Card(deferred_to=0), Card(deferred_to=0)]
    )
    async with engine.connect() as conn:
        group = int(
            (
                await conn.execute(sa.text("SELECT duplicate_group_id FROM jobs WHERE id = :j"), {"j": rep})
            ).scalar_one()
        )
    snapshot = read_snapshot

    @asynccontextmanager
    async def fenced_snapshot(engine_: AsyncEngine) -> AsyncIterator[Any]:
        # delete-everything's A0 commits between the write's commit and the answer's snapshot
        commit_global_fence(migrated)
        async with snapshot(engine_) as conn:
            yield conn

    if fence_before_snapshot:
        monkeypatch.setattr(groups_route, "read_snapshot", fenced_snapshot)
    r = await logged_in_client.post(f"/api/duplicate-groups/{group}/split", json={"job_id": split})
    assert r.status_code == 200, r.text
    assert sorted(m["job_id"] for m in r.json()["members"]) == sorted([rep, individual])
    async with engine.connect() as conn:
        assert (await conn.execute(_TARGET_STATE, {"j": split})).scalar_one() == "pending"
        await conn.execute(_RELEASE_ALL)
        await conn.commit()
    r = await logged_in_client.post(
        f"/api/duplicate-groups/{group}/score-individually", json={"job_id": individual}
    )
    assert r.status_code == 200, r.text
    assert [m["score_individually"] for m in r.json()["members"] if m["job_id"] == individual] == [True]
    async with engine.connect() as conn:
        assert (await conn.execute(_FLAG, {"j": individual})).scalar_one() is True


async def test_a_read_snapshot_outside_the_guard_still_refuses_a_write(engine: AsyncEngine) -> None:
    await global_fence(engine)
    fence_guard.install_fence_guard(engine)
    with statements_of(engine) as seen, guarded():
        async with read_snapshot(engine) as conn:
            assert (await conn.execute(sa.text("SELECT 1"))).scalar_one() == 1  # opened under the fence
            with pytest.raises(sa.exc.DBAPIError) as refused:
                await conn.execute(sa.text("UPDATE active_config SET updated_at = updated_at WHERE id = 1"))
    assert pg_error(refused.value).sqlstate == "25006"  # read_only_sql_transaction
    assert GLOBAL_FENCE_SQL not in seen and seen[0].startswith(
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
    )


async def test_settings_dry_run_answers_under_the_guard(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    active = await settings_body(logged_in_client)
    with statements_of(engine) as seen:
        r = await logged_in_client.post("/api/settings/dry-run", json={"payload": active["payload"]})
    assert r.status_code == 200, r.text
    read_only = seen.index("SET TRANSACTION READ ONLY")
    assert seen[read_only - 1] == GLOBAL_FENCE_SQL  # the guard's read began the dry-run's transaction
