"""The api app factory (spec §3, §14) and `app`, the object uvicorn serves.

`create_app()` builds the FastAPI app, its routers, exception handlers and per-session rate limiter
(`SessionRateLimiter`), and wraps it in `EdgeMiddleware`.
It touches no configuration, engine or queue: the lifespan builds all of that from the configuration it loads
(`startup_or_exit("api")`, then the log level, the engine, the code head and, outside the test env, the
metrics server on port 8001). With `cfg` and `engine` injected (tests), the state is set at once and the
lifespan does nothing; the caller owns and disposes that engine.
"""

from collections import deque
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Final

from fastapi import APIRouter, FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import Receive, Scope, Send

from jobhunter import __version__
from jobhunter.api.edge import EdgeMiddleware
from jobhunter.api.errors import exception_handlers
from jobhunter.api.routes import auth, health
from jobhunter.config import AppConfig, startup_or_exit
from jobhunter.db.alembic_runner import code_head
from jobhunter.db.session import make_engine
from jobhunter.observability.logging import configure_logging
from jobhunter.observability.metrics import start_metrics_server

METRICS_PORT: Final = 8001
ENGINE_POOL_SIZE: Final = 10


class SessionRateLimiter:
    """At most `limit` requests per session in any `window_s` seconds (§14.1: 600 per minute).

    A sliding window of the accepted requests' times per key (the session's token HMAC), in memory: the api
    runs one replica, on one event loop, and `hit` never awaits, so no lock is needed. A refused request is
    not counted. Keys whose window is empty are dropped at most once per window, which bounds the memory to
    the sessions active in the last window.
    """

    def __init__(self, limit: int = 600, window_s: float = 60.0) -> None:
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[bytes, deque[float]] = {}
        self._next_sweep: float | None = None

    def hit(self, key: bytes, now: float) -> bool:
        """Count a request of `key` at `now` (monotonic seconds): `True`, or `False` past the limit."""
        self._sweep(now)
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - self.window_s:
            hits.popleft()
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


@asynccontextmanager
async def _lifespan(api: FastAPI) -> AsyncIterator[None]:
    cfg = startup_or_exit("api")
    configure_logging(cfg.log_level)  # ruling T14-b
    engine = make_engine(cfg, pool_size=ENGINE_POOL_SIZE)
    try:
        api.state.code_head = code_head()
        if cfg.env != "test":
            start_metrics_server(METRICS_PORT)
        api.state.cfg = cfg
        api.state.engine = engine
        yield
    finally:
        await engine.dispose()


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
    api = FastAPI(
        title="Job Hunter",
        version=__version__,
        lifespan=_lifespan if cfg is None else None,
        exception_handlers=exception_handlers(),
        # No interactive docs and no served schema: the export writes `openapi.json` (§14.5).
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        # FastAPI's native OpenTelemetry stays off: the api exports Prometheus metrics only, and its exception
        # records would carry messages.
        telemetry={"tracing": False, "metrics": False, "logs": False, "auto_configure": False},
    )
    api.include_router(health.router)
    api.include_router(auth.router)
    for router in extra_routers:
        api.include_router(router)
    api.state.rate_limiter = SessionRateLimiter()
    if cfg is not None and engine is not None:
        api.state.cfg = cfg
        api.state.engine = engine
        api.state.code_head = code_head()
    edge = EdgeMiddleware(api, config=lambda: _state_config(api), engine=lambda: _state_engine(api))
    return JobHunterApp(api, edge)


# Task 18 runs uvicorn on this with `log_config=None`, so uvicorn's records reach the redacting root handler.
app = create_app()
