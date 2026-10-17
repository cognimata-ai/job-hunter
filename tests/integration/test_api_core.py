import asyncio
import json
import uuid
from collections.abc import MutableMapping
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
import structlog
from fastapi import APIRouter
from pydantic import BaseModel, Field

pytestmark = pytest.mark.integration
HEADERS = {
    "strict-transport-security": "max-age=63072000; includeSubDomains",
    "content-security-policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'self'; object-src 'none'",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
}


def assert_edge(r: httpx.Response) -> None:
    assert (
        all(r.headers[k] == v for k, v in HEADERS.items())
        and uuid.UUID(r.headers["x-request-id"]).version == 7
    )
    if r.headers.get("content-type") == "application/problem+json":
        assert r.json()["request_id"] == r.headers["x-request-id"]


async def fresh_heartbeat(engine) -> None:
    async with engine.begin() as c:
        await c.execute(
            sa.text(
                "INSERT INTO worker_heartbeat VALUES (1, gen_random_uuid(), 1, 'h', 1, 1, 'v', now(), now()) "
                "ON CONFLICT (id) DO UPDATE SET heartbeat_at = now()"
            )
        )


async def test_healthz(api_client) -> None:
    r = await api_client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert_edge(r)


async def test_readyz(api_client, engine) -> None:
    r = await api_client.get("/readyz")
    assert r.status_code == 503 and r.json() == {"failing": ["worker_heartbeat"]}
    await fresh_heartbeat(engine)
    assert (await api_client.get("/readyz")).status_code == 200


async def test_readyz_reports_missing_schema(fresh_pg_server, make_config, log_events) -> None:
    from jobhunter.api.app import create_app
    from jobhunter.db.session import make_engine

    cfg = make_config("api", fresh_pg_server)
    engine = make_engine(cfg)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(cfg=cfg, engine=engine)), base_url="https://testserver"
    ) as c:
        r = await c.get("/readyz")
    assert r.status_code == 503 and r.json() == {"failing": ["migrations", "worker_heartbeat"]}
    # both probes saw 42P01 through SQLAlchemy; a poisoned transaction would give 25P02 for the second one
    assert [(e["probe"], e["sqlstate"]) for e in log_events() if e["event"] == "readiness_probe_failed"] == [
        ("migrations", "42P01"),
        ("worker_heartbeat", "42P01"),
    ]
    await engine.dispose()


async def test_problems_for_404_422_and_500_carry_edge_headers(engine, make_config) -> None:
    from jobhunter.api.app import create_app
    from jobhunter.observability.metrics import REGISTRY

    router = APIRouter()

    class Body(BaseModel):
        n: int = Field(ge=1, le=5)

    @router.post("/api/_probe")
    async def probe(b: Body) -> dict[str, int]:
        return {"n": b.n}

    @router.get("/api/_boom")
    async def boom() -> None:
        raise RuntimeError("secret detail")

    app = create_app(cfg=make_config("api"), engine=engine, extra_routers=[router])

    def count(route: str, status: str) -> float:
        value = REGISTRY.get_sample_value("jobhunter_http_requests_total", {"route": route, "status": status})
        return 0.0 if value is None else value

    boom_500s = count("/api/_boom", "500")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://testserver",
        headers={"Origin": "https://testserver", "X-JH-Request": "1"},
    ) as c:
        r = await c.get("/nope")
        assert_edge(r)
        assert r.status_code == 404 and r.headers["content-type"] == "application/problem+json"
        assert (
            r.json()["code"] == "not_found"
            and r.json()["type"] == "https://jobhunter.local/problems/not_found"
            and r.json()["instance"] == "/nope"
        )
        r = await c.post("/api/_probe", json={"n": 9})
        assert_edge(r)
        assert (
            r.status_code == 422
            and r.json()["code"] == "validation_failed"
            and r.json()["errors"][0]["field"] == "body.n"
        )
        assert r.headers["content-type"] == "application/problem+json"
        assert r.json()["errors"][0]["code"] == "less_than_equal"
        r = await c.get("/api/_boom")
        assert_edge(r)
        assert (
            r.status_code == 500
            and r.json()["code"] == "internal"
            and "secret detail" not in r.text
            and "Traceback" not in r.text
        )
        assert r.headers["content-type"] == "application/problem+json"
    assert count("/api/_boom", "500") - boom_500s == 1.0  # counted under the route template
    async with engine.connect() as conn:  # the last-resort 500 persisted one deduplicated alert
        assert tuple(
            (
                await conn.execute(
                    sa.text("SELECT severity, context FROM alerts WHERE code='api.unhandled_exception'")
                )
            ).one()
        ) == ("warn", {"error_class": "RuntimeError"})


def test_openapi_export(tmp_path) -> None:
    from jobhunter.api.openapi_export import main

    out = tmp_path / "openapi.json"
    assert main([str(out)]) == 0
    assert json.loads(out.read_text())["openapi"].startswith("3.1")


@pytest.mark.slow
async def test_api_recovers_after_db_restart(fresh_pg_server, make_config) -> None:  # Review Focus 1
    from jobhunter.api.app import create_app
    from jobhunter.db.migrate import run_migrate_async
    from jobhunter.db.session import make_engine

    from tests.support.pg import pg_start, pg_stop

    await run_migrate_async(make_config("migrate", fresh_pg_server))
    cfg = make_config("api", fresh_pg_server)
    engine = make_engine(cfg)
    await fresh_heartbeat(engine)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(cfg=cfg, engine=engine)), base_url="https://testserver"
    ) as c:
        assert (await c.get("/readyz")).status_code == 200
        pg_stop(fresh_pg_server)
        r = await c.get("/readyz")
        assert r.status_code == 503 and r.json() == {"failing": ["db"]}
        pg_start(fresh_pg_server)
        await fresh_heartbeat(engine)
        assert (await c.get("/readyz")).status_code == 200
    await engine.dispose()


# --- beyond the plan's tests: the rulings T14-c, T14-d, T14-e and P15b, and the edge's own responses ---


def _boom_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/_boom")
    async def boom() -> None:
        raise RuntimeError("secret detail")

    return router


async def test_405_is_a_not_found_problem_that_keeps_allow(api_client) -> None:
    r = await api_client.post("/healthz")
    assert_edge(r)
    assert r.status_code == 405 and r.headers["content-type"] == "application/problem+json"
    assert r.json()["code"] == "not_found" and r.json()["status"] == 405 and r.headers["allow"] == "GET"


async def test_request_id_is_logged_and_unbound_after_the_response(engine, make_config, log_events) -> None:
    from jobhunter.api.app import create_app

    app = create_app(cfg=make_config("api"), engine=engine, extra_routers=[_boom_router()])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as c:
        first = await c.get("/api/_boom")
        # ASGITransport runs the app in this task: a request id left bound would tag every later log line
        assert "request_id" not in structlog.contextvars.get_contextvars()
        second = await c.get("/healthz")
    assert "request_id" not in structlog.contextvars.get_contextvars()
    assert first.status_code == 500 and second.status_code == 200
    assert first.headers["x-request-id"] != second.headers["x-request-id"]
    assert first.json()["request_id"] == first.headers["x-request-id"]
    # The inner handler logs first, then the edge sees ServerErrorMiddleware's re-raise after the 500 started:
    # the edge wraps ServerErrorMiddleware. Mounted inside it, the edge would render the 500 itself and log it
    # once as `jobhunter.api.edge`; the inner handler would never run.
    rid = first.headers["x-request-id"]
    sequence = [
        (e["event"], e["logger"], e["request_id"], e["error_class"])
        for e in log_events()
        if e["event"] in ("unhandled_exception", "unhandled_exception_after_start")
    ]
    assert sequence == [
        ("unhandled_exception", "jobhunter.api", rid, "RuntimeError"),
        ("unhandled_exception_after_start", "jobhunter.api.edge", rid, "RuntimeError"),
    ]
    assert "secret detail" not in json.dumps(log_events()) and "Traceback" not in json.dumps(log_events())


async def test_http_requests_are_counted_by_route_template(api_client) -> None:
    from jobhunter.observability.metrics import REGISTRY

    def count(route: str, status: str) -> float:
        value = REGISTRY.get_sample_value("jobhunter_http_requests_total", {"route": route, "status": status})
        return 0.0 if value is None else value

    before = {key: count(*key) for key in (("/readyz", "503"), ("unmatched", "404"), ("unmatched", "405"))}
    assert (await api_client.get("/readyz")).status_code == 503
    assert (await api_client.get("/no/such/path/42")).status_code == 404
    assert (await api_client.post("/readyz")).status_code == 405
    assert {key: count(*key) - before[key] for key in before} == {
        ("/readyz", "503"): 1.0,
        ("unmatched", "404"): 1.0,
        ("unmatched", "405"): 1.0,
    }
    assert count("/no/such/path/42", "404") == 0.0  # never the raw path


async def test_route_label_is_the_template_not_the_path(engine, make_config) -> None:
    from jobhunter.api.app import create_app
    from jobhunter.observability.metrics import REGISTRY

    router = APIRouter()

    @router.get("/api/_items/{item_id}")
    async def item(item_id: int) -> dict[str, int]:
        return {"id": item_id}

    def count(route: str) -> float:
        value = REGISTRY.get_sample_value("jobhunter_http_requests_total", {"route": route, "status": "200"})
        return 0.0 if value is None else value

    before = count("/api/_items/{item_id}")
    app = create_app(cfg=make_config("api"), engine=engine, extra_routers=[router])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as c:
        r = await c.get("/api/_items/7")
    assert_edge(r)
    assert (
        r.json() == {"id": 7}
        and count("/api/_items/{item_id}") - before == 1.0
        and count("/api/_items/7") == 0.0
    )


async def test_edge_renders_the_500_when_the_inner_app_fails_before_responding(
    engine, make_config, log_events
) -> None:
    from jobhunter.api.edge import EdgeMiddleware

    cfg = make_config("api")

    async def fails_before_start(scope, receive, send) -> None:
        raise RuntimeError("secret detail")

    edge = EdgeMiddleware(fails_before_start, config=lambda: cfg, engine=lambda: engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=edge), base_url="https://testserver") as c:
        responses = [await c.get("/api/anything"), await c.get("/api/anything")]
    for r in responses:
        assert_edge(r)
        assert r.status_code == 500 and r.headers["content-type"] == "application/problem+json"
        assert r.json()["code"] == "internal" and r.json()["instance"] == "/api/anything"
        assert "secret detail" not in r.text
    async with engine.connect() as conn:  # one alert per exception class, the second raise is an occurrence
        row = (
            await conn.execute(
                sa.text(
                    "SELECT severity, dedup_key, context, occurrences FROM alerts "
                    "WHERE code = 'api.unhandled_exception'"
                )
            )
        ).one()
    assert tuple(row) == ("warn", "api.unhandled_exception:RuntimeError", {"error_class": "RuntimeError"}, 2)
    assert [e["error_class"] for e in log_events() if e["event"] == "unhandled_exception"] == [
        "RuntimeError"
    ] * 2
    assert "secret detail" not in json.dumps(log_events())


async def test_edge_only_logs_an_exception_after_the_response_started(
    engine, make_config, log_events
) -> None:
    from jobhunter.api.edge import EdgeMiddleware

    cfg = make_config("api")

    async def fails_mid_body(scope, receive, send) -> None:
        await send(
            {"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]}
        )
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        raise RuntimeError("secret detail")

    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message) -> None:
        sent.append(message)

    edge = EdgeMiddleware(fails_mid_body, config=lambda: cfg, engine=lambda: engine)
    await edge({"type": "http", "method": "GET", "path": "/stream", "headers": []}, receive, send)
    # nothing is sent after the started response, and nothing is raised to the server
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    headers = {k.decode(): v.decode() for k, v in sent[0]["headers"]}
    assert (
        all(headers[k] == v for k, v in HEADERS.items()) and uuid.UUID(headers["x-request-id"]).version == 7
    )
    assert [e["error_class"] for e in log_events() if e["event"] == "unhandled_exception_after_start"] == [
        "RuntimeError"
    ]
    assert "secret detail" not in json.dumps(log_events())
    async with engine.connect() as conn:  # the inner layer's handler is the one that persists, not the edge
        assert (await conn.execute(sa.text("SELECT count(*) FROM alerts"))).scalar_one() == 0


async def test_alert_persist_failure_is_logged_and_never_raised(log_events) -> None:
    from jobhunter.api.errors import persist_unhandled_alert
    from sqlalchemy.ext.asyncio import create_async_engine

    await persist_unhandled_alert(None, RuntimeError("secret detail"))  # no engine yet
    unreachable = create_async_engine("postgresql+psycopg://nobody:pw@127.0.0.1:1/none")
    try:
        await persist_unhandled_alert(unreachable, KeyError("secret detail"))
    finally:
        await unreachable.dispose()
    failed = [e for e in log_events() if e["event"] == "alert_persist_failed"]
    assert [(e["error_class"], e["persist_error_class"]) for e in failed] == [
        ("RuntimeError", None),
        ("KeyError", "OperationalError"),
    ]
    assert "secret detail" not in json.dumps(log_events())


async def test_readiness_flags_a_stale_heartbeat_and_a_foreign_revision(engine, log_events) -> None:
    from jobhunter.clock import utcnow
    from jobhunter.db.alembic_runner import code_head
    from jobhunter.observability.health import ReadinessReport, check_readiness

    async with engine.begin() as c:
        await c.execute(
            sa.text(
                "INSERT INTO worker_heartbeat VALUES "
                "(1, gen_random_uuid(), 1, 'h', 1, 1, 'v', now(), now() - interval '90 seconds')"
            )
        )
    stale = await check_readiness(engine, code_head="0000_not_the_head", now=utcnow())
    assert stale == ReadinessReport(ok=False, failing=["migrations", "worker_heartbeat"])
    # no error was raised by either probe, so neither has a SQLSTATE
    assert [(e["probe"], e["sqlstate"]) for e in log_events() if e["event"] == "readiness_probe_failed"] == [
        ("migrations", None),
        ("worker_heartbeat", None),
    ]
    await fresh_heartbeat(engine)
    assert await check_readiness(engine, code_head=code_head(), now=utcnow()) == ReadinessReport(
        ok=True, failing=[]
    )


async def test_readiness_reports_db_when_the_connection_dies_mid_check(engine, migrated, log_events) -> None:
    from jobhunter.clock import utcnow
    from jobhunter.db.alembic_runner import code_head
    from jobhunter.observability.health import ReadinessReport, check_readiness

    from tests.support.pg import pg_one

    def kill_own_backend(conn, cursor, statement, parameters, context, executemany) -> None:
        if "alembic_version" in statement:  # after SELECT 1 succeeded, before the first schema probe runs
            pid = conn.connection.driver_connection.info.backend_pid
            assert pg_one(migrated, "app", "SELECT pg_terminate_backend(%s, 5000)", pid) == (True,)

    sa.event.listen(engine.sync_engine, "before_cursor_execute", kill_own_backend)
    try:
        report = await check_readiness(engine, code_head=code_head(), now=utcnow())
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", kill_own_backend)
    assert report == ReadinessReport(ok=False, failing=["db"])
    assert [e["probe"] for e in log_events() if e["event"] == "readiness_probe_failed"] == ["db"]


async def test_a_probe_blocked_behind_a_lock_fails_alone(
    migrated, make_config, log_events, monkeypatch
) -> None:
    import psycopg
    from jobhunter.clock import utcnow
    from jobhunter.db.alembic_runner import code_head
    from jobhunter.db.session import make_engine
    from jobhunter.observability import health

    engine = make_engine(
        make_config("api", migrated), pool_size=1
    )  # one connection: the probe's is reused below
    try:
        await fresh_heartbeat(engine)
        monkeypatch.setattr(health, "PROBE_STATEMENT_TIMEOUT", "200ms")
        # what a migration's ALTER TABLE holds until it commits
        with psycopg.connect(migrated.dsn("migrator")) as locker:
            locker.execute("LOCK TABLE worker_heartbeat IN ACCESS EXCLUSIVE MODE")
            async with asyncio.timeout(10):  # without the statement timeout the probe would wait for the lock
                report = await health.check_readiness(engine, code_head=code_head(), now=utcnow())
            locker.rollback()
        assert report == health.ReadinessReport(ok=False, failing=["worker_heartbeat"])
        assert [
            (e["probe"], e["sqlstate"]) for e in log_events() if e["event"] == "readiness_probe_failed"
        ] == [
            ("worker_heartbeat", "57014")  # query_canceled by the statement timeout
        ]
        # the timeout was local to the probe's transaction: the pooled connection has its default back
        async with engine.connect() as conn:
            assert (await conn.execute(sa.text("SHOW statement_timeout"))).scalar_one() == "0"
    finally:
        await engine.dispose()


async def test_readyz_answers_503_db_for_an_unexpected_error(api_client, log_events, monkeypatch) -> None:
    from jobhunter.observability import health

    async def closed(conn, code_head):  # what SQLAlchemy raises for a connection closed under the check
        raise sa.exc.ResourceClosedError("secret detail")

    monkeypatch.setattr(health, "_schema_probes", closed)
    r = await api_client.get("/readyz")
    assert r.status_code == 503 and r.json() == {"failing": ["db"]}
    failed = [e for e in log_events() if e["event"] == "readiness_probe_failed"]
    assert [(e["probe"], e["error_class"]) for e in failed] == [("db", "ResourceClosedError")]
    assert "secret detail" not in json.dumps(log_events())


async def test_the_lifespan_builds_the_state_from_the_environment(migrated, monkeypatch) -> None:
    import logging
    import os

    from jobhunter.api.app import create_app
    from jobhunter.config import ENV_PREFIXES

    from tests.support.pg import server_env

    for name in [name for name in os.environ if name.startswith(ENV_PREFIXES)]:
        monkeypatch.delenv(name)
    for name, value in server_env(migrated, "api", JOBHUNTER_LOG_LEVEL="WARNING").items():
        monkeypatch.setenv(name, value)
    app = create_app()  # nothing injected: the lifespan loads the config and builds the engine
    assert not hasattr(app.api.state, "engine")

    inbox: asyncio.Queue[MutableMapping[str, Any]] = asyncio.Queue()  # the server's side of the protocol
    outbox: asyncio.Queue[MutableMapping[str, Any]] = asyncio.Queue()
    lifespan = asyncio.create_task(
        app({"type": "lifespan", "asgi": {"version": "3.0"}}, inbox.get, outbox.put)
    )
    await inbox.put({"type": "lifespan.startup"})
    assert (await outbox.get())["type"] == "lifespan.startup.complete"
    try:
        assert logging.getLogger().level == logging.WARNING  # ruling T14-b
        engine = app.api.state.engine
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://testserver"
        ) as c:
            r = await c.get("/readyz")
        assert_edge(r)
        assert r.status_code == 503 and r.json() == {"failing": ["worker_heartbeat"]}
        assert engine.pool.checkedin() == 1  # the readiness connection went back to the lifespan's pool
    finally:
        await inbox.put({"type": "lifespan.shutdown"})
        assert (await outbox.get())["type"] == "lifespan.shutdown.complete"
        await lifespan
    assert engine.pool.checkedin() == 0  # disposed at shutdown


async def test_a_422_message_returns_to_the_sender_and_never_reaches_a_log_line(
    engine, make_config, log_events
) -> None:
    from jobhunter.api.app import create_app
    from pydantic import field_validator

    router = APIRouter()

    class Tagged(BaseModel):
        tag: str

        @field_validator("tag")
        @classmethod
        def known(cls, value: str) -> str:
            raise ValueError(f"unknown tag {value!r}")  # pydantic puts this, with the input, into `msg`

    @router.post("/api/_tagged")
    async def tagged(body: Tagged) -> dict[str, str]:
        return {"tag": body.tag}

    app = create_app(cfg=make_config("api"), engine=engine, extra_routers=[router])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as c:
        r = await c.post("/api/_tagged", json={"tag": "sender-input-7f3a"})
    assert_edge(r)
    assert r.status_code == 422 and r.headers["content-type"] == "application/problem+json"
    # the sender gets its own input back (ruling T14-h) ...
    assert r.json()["errors"] == [
        {
            "field": "body.tag",
            "code": "value_error",
            "message": "Value error, unknown tag 'sender-input-7f3a'",
        }
    ]
    # ... and no log line carries it
    assert "sender-input-7f3a" not in json.dumps(log_events())


async def test_readiness_bounds_the_probe_sequence_client_side(engine, log_events, monkeypatch) -> None:
    from jobhunter.clock import utcnow
    from jobhunter.db.alembic_runner import code_head
    from jobhunter.observability import health

    assert health.PROBES_TIMEOUT_S == 2 * health.DB_TIMEOUT_S
    await fresh_heartbeat(engine)
    probe = health._rows
    never = asyncio.Event()  # a database that stops answering after `SELECT 1`, its socket still open

    async def stuck_heartbeat_probe(conn, name, statement):
        if name == "worker_heartbeat":
            await never.wait()
        return await probe(conn, name, statement)

    monkeypatch.setattr(health, "_rows", stuck_heartbeat_probe)
    monkeypatch.setattr(health, "PROBES_TIMEOUT_S", 0.1)
    async with asyncio.timeout(10):  # without the bound the check would wait for ever
        report = await health.check_readiness(engine, code_head=code_head(), now=utcnow())
    assert report == health.ReadinessReport(ok=False, failing=["db"])
    failed = [(e["probe"], e["error_class"]) for e in log_events() if e["event"] == "readiness_probe_failed"]
    assert failed == [("db", "TimeoutError")]  # the migrations probe passed; its result is not reported alone
