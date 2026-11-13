"""The api app factory (spec §3, §14) and `app`, the object uvicorn serves.

`create_app()` builds the FastAPI app, its routers, exception handlers, per-session rate limiter
(`SessionRateLimiter`) and Argon2 admission gate (`argon2_gate.Argon2Gate`), and wraps it in `EdgeMiddleware`.
The app's engine, the lifespan's or an injected one, gets the write guard of delete-everything's `global`
fence (`db.fence_guard.install_fence_guard`, M5 Task 8): every transaction a guarded request begins on it
reads the fence first (the edge sets the guard; `api.edge`, step 4). No route checks that fence itself.
It touches no configuration, engine or queue: the lifespan builds all of that from the configuration it loads
(`startup_or_exit("api")`, then the task app, the log level, the engine, the code head, the embedder client
and, outside the test env, the metrics server on port 8001). With `cfg` and `engine` injected (tests), the
state, the task app included, is set at once and the lifespan does nothing; the caller owns and disposes that
engine.

The SPA (M4 Task 19) is `api.state.spa`, an `api.static.SpaFiles` on `cfg.spa_dir`, set with the rest of the
state: the 404 handler of `api.errors` offers it what no route matched, so it is no Mount.

The privacy statement's operator details (M5 follow-ups Task F1) are `api.state.operator`, read once from
`cfg.operator_file` with the rest of the state (`privacy.operator.at_start`; `None` outside prod without a
valid file), so a changed file applies after `restart api`.

The embedder client (Task 22) is an `embeddings.EmbedderClient` on `cfg.embedder_url`, which readiness and
system health probe. The lifespan's HTTP client is closed when the lifespan ends. An injected app (tests) gets
one of its own on the injected `cfg`, which nothing closes: the tests' fake embedder closes every connection
after its answer, so the client holds none open.

The heartbeat watch (M5 Task 3, `observability.heartbeat_watch`) is a task of the lifespan, kept as
`api.state.heartbeat_watch`: it raises and resolves `worker.heartbeat_stale` on the lifespan's engine, at the
clock `jobhunter.clock.utcnow`, which no request does (a GET handler never writes, §16.5). It is the first
thing the lifespan's end stops: its stop event is set, and a pass still running is waited for, at most
`WATCH_STOP_S`, then cancelled. An injected app (tests) runs no watch.

The Argon2 gate's thread pool is the next thing the lifespan's end closes (`Argon2Gate.close`, M0
Carry-forward 7): a computation still running is waited for, so the threads end with the app.

The gazetteer (M2 Task 22) is loaded by the first `/api/geo/places` request, in a worker thread, and kept on
the state (`deps.get_gazetteer`): `create_app` sets only its slot and the lock that makes a first load happen
once.

The task app: `defer_in_tx` defers through the process-global `tasks.task_app()`, which the first
`make_queue_app` call of the process creates and registers; building `make_queue_app(cfg.dsn("psycopg"))` here
(kept as `api.state.queue`) is what registers it in the api. Deferring uses its task registry, not its pool or
its DSN, since the job is inserted on the request's own connection. So neither `create_app` nor the lifespan
opens it (which would hold a connection and make the api's start wait for the database); the lifespan closes
it at shutdown, which closes a pool only if something opened one. An injected app's pool is closed by
whoever opened it.
"""

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Final

import httpx
from fastapi import APIRouter, FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import Receive, Scope, Send

from jobhunter import __version__
from jobhunter.api.argon2_gate import Argon2Gate
from jobhunter.api.edge import EdgeMiddleware
from jobhunter.api.errors import exception_handlers
from jobhunter.api.openapi import JobHunterAPI
from jobhunter.api.routes import (
    alerts,
    auth,
    cycles,
    docs,
    eval_runs,
    geo,
    golden,
    groups,
    health,
    jobs,
    labelling,
    privacy,
    profile,
    profile_review,
    queries,
    runs,
    settings,
    spend,
    system,
    targets,
    watchlist,
)
from jobhunter.api.static import SpaFiles
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig, startup_or_exit
from jobhunter.db.alembic_runner import code_head
from jobhunter.db.fence_guard import install_fence_guard
from jobhunter.db.session import make_engine
from jobhunter.embeddings import EmbedderClient, make_http_client
from jobhunter.observability.heartbeat_watch import watch_heartbeat
from jobhunter.observability.logging import configure_logging, get_logger
from jobhunter.observability.metrics import start_metrics_server
from jobhunter.privacy import operator
from jobhunter.privacy.operator import OperatorDetails
from jobhunter.worker.tasks import make_queue_app

METRICS_PORT: Final = 8001
ENGINE_POOL_SIZE: Final = 10
# How long the lifespan's end waits for a heartbeat-watch pass still running before it cancels it.
WATCH_STOP_S: Final = 5.0

log = get_logger("jobhunter.api.app")


class SessionRateLimiter:
    """At most `limit` requests per session in any `window_s` seconds (§14.1: 600 per minute).

    A sliding window of the accepted requests' times per key (the session's token HMAC), in memory: the api
    runs one replica, on one event loop, and neither method awaits, so no lock is needed. A refused request is
    not counted. Keys whose window is empty are dropped at most once per window, which bounds the memory to
    the sessions active in the last window. `limited` only looks, so `require_session` can refuse a request
    before any SQL and still count only the requests of valid sessions (`hit`, after the validation): a
    presented token that opens no session never becomes a key.
    """

    def __init__(self, limit: int = 600, window_s: float = 60.0) -> None:
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[bytes, deque[float]] = {}
        self._next_sweep: float | None = None

    def _current(self, hits: deque[float], now: float) -> deque[float]:
        while hits and hits[0] <= now - self.window_s:
            hits.popleft()
        return hits

    def limited(self, key: bytes, now: float) -> bool:
        """Whether a request of `key` at `now` (monotonic seconds) would be refused; counts nothing."""
        hits = self._hits.get(key)
        return hits is not None and len(self._current(hits, now)) >= self.limit

    def hit(self, key: bytes, now: float) -> bool:
        """Count a request of `key` at `now` (monotonic seconds): `True`, or `False` past the limit."""
        self._sweep(now)
        hits = self._current(self._hits.setdefault(key, deque()), now)
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True

    def __len__(self) -> int:
        """How many sessions it holds request times of (swept ones excluded)."""
        return len(self._hits)

    def _sweep(self, now: float) -> None:
        if self._next_sweep is not None and now < self._next_sweep:
            return
        self._next_sweep = now + self.window_s
        stale = [key for key, hits in self._hits.items() if not hits or hits[-1] <= now - self.window_s]
        for key in stale:
            del self._hits[key]


class JobHunterApp:
    """The ASGI app: `EdgeMiddleware` around the FastAPI app `api` (exposed for the OpenAPI export)."""

    def __init__(self, api: FastAPI, edge: EdgeMiddleware) -> None:
        self.api = api
        self._edge = edge

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._edge(scope, receive, send)


async def _stop_watch(watch: asyncio.Task[None], stop: asyncio.Event) -> None:
    """Stop the heartbeat watch: `stop` set, then the task awaited, a pass still running for at most
    `WATCH_STOP_S`, after which it is cancelled (its transaction rolls back)."""
    stop.set()
    try:
        await asyncio.wait_for(watch, WATCH_STOP_S)
    except TimeoutError:
        log.warning("heartbeat_watch_stop_timeout", waited_s=WATCH_STOP_S)


@asynccontextmanager
async def _lifespan(api: FastAPI) -> AsyncIterator[None]:
    cfg = startup_or_exit("api")
    queue = make_queue_app(cfg.dsn("psycopg"))  # ruling P2: from the config the lifespan loads
    configure_logging(cfg.log_level)  # ruling T14-b
    engine = make_engine(cfg, pool_size=ENGINE_POOL_SIZE)
    install_fence_guard(engine)
    embedder_http: httpx.AsyncClient | None = None
    watch: asyncio.Task[None] | None = None
    stop_watch = asyncio.Event()
    try:
        api.state.code_head = code_head()
        if cfg.env != "test":
            start_metrics_server(METRICS_PORT)
        embedder_http = make_http_client()
        api.state.cfg = cfg
        api.state.engine = engine
        api.state.queue = queue
        api.state.embedder = EmbedderClient(cfg.embedder_url, embedder_http)
        api.state.spa = SpaFiles(cfg.spa_dir)
        api.state.operator = _operator_details(cfg)
        watch = asyncio.create_task(
            watch_heartbeat(engine, clock=utcnow, stop=stop_watch), name="heartbeat_watch"
        )
        api.state.heartbeat_watch = watch
        yield
    finally:
        try:
            if watch is not None:
                await _stop_watch(watch, stop_watch)
        finally:
            try:
                gate = getattr(api.state, "argon2_gate", None)
                if isinstance(gate, Argon2Gate):  # `create_app` made it; a running computation is waited for
                    gate.close()
            finally:
                try:
                    await queue.close_async()  # ruling T17-d: a no-op unless something opened its pool
                finally:
                    try:
                        if embedder_http is not None:
                            await embedder_http.aclose()
                    finally:
                        await engine.dispose()


def _operator_details(cfg: AppConfig) -> OperatorDetails | None:
    """The operator details the api serves for its whole run (`privacy.operator.at_start`)."""
    return operator.at_start(cfg.operator_file, prod=cfg.env == "prod")


def _state_config(api: FastAPI) -> AppConfig | None:
    cfg = getattr(api.state, "cfg", None)
    return cfg if isinstance(cfg, AppConfig) else None


def _state_engine(api: FastAPI) -> AsyncEngine | None:
    engine = getattr(api.state, "engine", None)
    return engine if isinstance(engine, AsyncEngine) else None


def create_app(
    *,
    cfg: AppConfig | None = None,
    engine: AsyncEngine | None = None,
    extra_routers: Sequence[APIRouter] = (),
) -> JobHunterApp:
    """The api; `cfg` and `engine` are injected together (tests) or not at all (the lifespan builds them)."""
    if (cfg is None) != (engine is None):
        raise ValueError("create_app takes cfg and engine together, or neither")
    api = JobHunterAPI(
        title="Job Hunter",
        version=__version__,
        lifespan=_lifespan if cfg is None else None,
        exception_handlers=exception_handlers(),
        # No interactive docs and no unauthenticated schema: the export writes `openapi.json` (§14.5), and
        # `routes.docs` serves the document to a session only (§14.2, `GET /api/openapi.json`).
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        # FastAPI's native OpenTelemetry stays off: the api exports Prometheus metrics only, and its exception
        # records would carry messages.
        telemetry={"tracing": False, "metrics": False, "logs": False, "auto_configure": False},
    )
    api.include_router(health.router)
    api.include_router(auth.router)
    api.include_router(runs.router)
    api.include_router(system.router)
    api.include_router(system.ats_snapshots_router)
    api.include_router(spend.router)
    api.include_router(profile.router)
    api.include_router(profile_review.router)
    api.include_router(settings.router)
    api.include_router(eval_runs.router)
    api.include_router(queries.router)
    api.include_router(watchlist.router)
    api.include_router(geo.router)
    # before any `/api/jobs/{job_id}` GET route, so that `/api/jobs/facets` is matched first
    api.include_router(jobs.router)
    api.include_router(targets.router)
    api.include_router(groups.router)
    api.include_router(golden.router)
    api.include_router(labelling.router)
    api.include_router(alerts.router)
    api.include_router(cycles.router)
    api.include_router(privacy.router)
    api.include_router(docs.router)
    for router in extra_routers:
        api.include_router(router)
    api.state.rate_limiter = SessionRateLimiter()
    api.state.argon2_gate = Argon2Gate()  # every Argon2 computation of the process goes through it
    api.state.gazetteer = None  # loaded by the first `/api/geo/places` request (`deps.get_gazetteer`)
    api.state.gazetteer_lock = asyncio.Lock()
    if cfg is not None and engine is not None:
        install_fence_guard(engine)
        api.state.cfg = cfg
        api.state.engine = engine
        api.state.code_head = code_head()
        api.state.queue = make_queue_app(cfg.dsn("psycopg"))  # ruling P2: built, not opened
        api.state.embedder = EmbedderClient(cfg.embedder_url, make_http_client())
        api.state.spa = SpaFiles(cfg.spa_dir)
        api.state.operator = _operator_details(cfg)
    edge = EdgeMiddleware(api, config=lambda: _state_config(api), engine=lambda: _state_engine(api))
    return JobHunterApp(api, edge)


# Task 18 runs uvicorn on this with `log_config=None`, so uvicorn's records reach the redacting root handler.
app = create_app()
