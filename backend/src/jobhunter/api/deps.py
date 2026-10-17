"""FastAPI dependencies that read the app state and the request state the edge set."""

from time import monotonic
from typing import Protocol, runtime_checkable

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.api.errors import Problem
from jobhunter.auth import sessions
from jobhunter.auth.sessions import SessionInfo
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig


@runtime_checkable
class RateLimiter(Protocol):
    """What `require_session` needs of the app's `rate_limiter` (`app.SessionRateLimiter`)."""

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


def get_code_head(request: Request) -> str:
    """The head revision of the migrations shipped with this code (computed once, at start)."""
    head = getattr(request.app.state, "code_head", None)
    if not isinstance(head, str):
        raise RuntimeError("the api state has no code head: the lifespan has not run")
    return head


def get_rate_limiter(request: Request) -> RateLimiter:
    limiter = getattr(request.app.state, "rate_limiter", None)
    if not isinstance(limiter, RateLimiter):
        raise RuntimeError("the api state has no rate limiter: it was not built by create_app")
    return limiter


def request_id(request: Request) -> str:
    """The id `EdgeMiddleware` gave this request (the `X-Request-Id` of its response)."""
    value = getattr(request.state, "request_id", None)
    if not isinstance(value, str):
        raise RuntimeError("the request has no request id: it did not pass the edge")
    return value


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

    The validation (and its slide) runs in a transaction of its own that commits before anything is raised.
    The limit is 600 requests per minute per session (§14.1), counted in memory: the api runs one replica.
    Both clocks (`utcnow`, `monotonic`) are names of this module, so tests can inject them; patching
    `time.monotonic` itself would also move the event loop's clock.
    """
    token = request.cookies.get(sessions.SESSION_COOKIE)
    if not token:
        raise _unauthenticated()
    pepper = sessions.session_pepper(get_config(request).secret_b64("session_secret"))
    async with get_engine(request).begin() as conn:
        info = await sessions.validate_session(conn, token, pepper=pepper, now=utcnow())
    if info is None:
        raise _unauthenticated()
    if not get_rate_limiter(request).hit(info.token_hmac, monotonic()):
        raise Problem(429, "throttled", "Too many requests")
    return info
