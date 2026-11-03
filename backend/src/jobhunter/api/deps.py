"""FastAPI dependencies that read the app state and the request state the edge set."""

import asyncio
from time import monotonic
from typing import Final, Protocol, runtime_checkable

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.api.argon2_gate import Argon2Gate
from jobhunter.api.errors import Problem
from jobhunter.auth import sessions
from jobhunter.auth.sessions import SessionInfo
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.crypto import data_keys
from jobhunter.embeddings import EmbedderClient
from jobhunter.geo.gazetteer import Gazetteer

# The request header of the SPA's polls: `X-JH-Background: 1` (that exact value only) marks a poll, not a
# user's action, which `require_session` validates without sliding the session's idle expiry (spec §16.4).
BACKGROUND_HEADER: Final = "X-JH-Background"


@runtime_checkable
class RateLimiter(Protocol):
    """What `require_session` needs of the app's `rate_limiter` (`app.SessionRateLimiter`)."""

    def limited(self, key: bytes, now: float) -> bool: ...

    def hit(self, key: bytes, now: float) -> bool: ...


def get_engine(request: Request) -> AsyncEngine:
    engine = getattr(request.app.state, "engine", None)
    if not isinstance(engine, AsyncEngine):
        raise RuntimeError("the api state has no engine: the lifespan has not run")
    return engine


def get_config(request: Request) -> AppConfig:
    cfg = getattr(request.app.state, "cfg", None)
    if not isinstance(cfg, AppConfig):
        raise RuntimeError("the api state has no config: the lifespan has not run")
    return cfg


def get_data_keys(request: Request) -> list[bytes]:
    """The data keys of the app's config (`crypto.data_keys`): the sealing one first, then a previous one."""
    return data_keys(get_config(request))


def get_code_head(request: Request) -> str:
    """The head revision of the migrations shipped with this code (computed once, at start)."""
    head = getattr(request.app.state, "code_head", None)
    if not isinstance(head, str):
        raise RuntimeError("the api state has no code head: the lifespan has not run")
    return head


def get_embedder(request: Request) -> EmbedderClient:
    """The app's embedder client (`create_app`, or the lifespan): readiness and system health probe it."""
    embedder = getattr(request.app.state, "embedder", None)
    if not isinstance(embedder, EmbedderClient):
        raise RuntimeError("the api state has no embedder client: the lifespan has not run")
    return embedder


async def get_gazetteer(request: Request) -> Gazetteer:
    """The packaged gazetteer, loaded on first use and kept on the app state (M2 Task 22).

    The first load reads the data files (about 0.1 s), so it runs in a worker thread, never on the event
    loop; the app's lock makes requests that arrive together share that one load.
    """
    state = request.app.state
    loaded = getattr(state, "gazetteer", None)
    if isinstance(loaded, Gazetteer):
        return loaded
    lock = getattr(state, "gazetteer_lock", None)
    if not isinstance(lock, asyncio.Lock):
        raise RuntimeError("the api state has no gazetteer lock: it was not built by create_app")
    async with lock:
        loaded = getattr(state, "gazetteer", None)  # a request that waited here finds it loaded
        if isinstance(loaded, Gazetteer):
            return loaded
        gazetteer = await asyncio.to_thread(Gazetteer.load)
        state.gazetteer = gazetteer
        return gazetteer


def get_rate_limiter(request: Request) -> RateLimiter:
    limiter = getattr(request.app.state, "rate_limiter", None)
    if not isinstance(limiter, RateLimiter):
        raise RuntimeError("the api state has no rate limiter: it was not built by create_app")
    return limiter


def get_argon2_gate(request: Request) -> Argon2Gate:
    """The process's one Argon2 admission gate (ruling T21-e)."""
    gate = getattr(request.app.state, "argon2_gate", None)
    if not isinstance(gate, Argon2Gate):
        raise RuntimeError("the api state has no Argon2 gate: it was not built by create_app")
    return gate


def request_id(request: Request) -> str:
    """The id `EdgeMiddleware` gave this request (the `X-Request-Id` of its response)."""
    value = getattr(request.state, "request_id", None)
    if not isinstance(value, str):
        raise RuntimeError("the request has no request id: it did not pass the edge")
    return value


def _too_many_requests() -> Problem:
    return Problem(429, "throttled", "Too many requests")


def _unauthenticated() -> Problem:
    # The cookie is cleared with the attributes it was set with, so the browser drops it (ruling T16-e).
    return Problem(
        401,
        "unauthenticated",
        "Authentication required",
        headers={"Set-Cookie": sessions.CLEARED_SESSION_COOKIE},
    )


async def require_session(request: Request) -> SessionInfo:
    """The valid session of the request's cookie; 401 `unauthenticated` without one, 429 past the rate limit.

    The limit is 600 requests per minute per session (§14.1), counted in memory: the api runs one replica. It
    is keyed by the token's HMAC and looked at first, before any SQL, so a request past it costs no database
    round trip; only a request of a valid session is counted (after the validation, which refuses it too
    when a concurrent one took the last place). The validation (and its slide) runs in a transaction of its
    own that commits before anything is raised. A background request (`BACKGROUND_HEADER: 1`) is validated
    without the slide: it is refused past either expiry and counted by the limit like any other, but it keeps
    no session alive. Both clocks (`utcnow`, `monotonic`) are names of this module, so tests can inject them;
    patching `time.monotonic` itself would also move the event loop's clock.
    """
    token = request.cookies.get(sessions.SESSION_COOKIE)
    if not token:
        raise _unauthenticated()
    pepper = sessions.session_pepper(get_config(request).secret_b64("session_secret"))
    limiter = get_rate_limiter(request)
    if limiter.limited(sessions.token_hmac(token, pepper), monotonic()):
        raise _too_many_requests()
    slide = request.headers.get(BACKGROUND_HEADER) != "1"
    async with get_engine(request).begin() as conn:
        info = await sessions.validate_session(conn, token, pepper=pepper, now=utcnow(), slide=slide)
    if info is None:
        raise _unauthenticated()
    if not limiter.hit(info.token_hmac, monotonic()):
        raise _too_many_requests()
    return info
