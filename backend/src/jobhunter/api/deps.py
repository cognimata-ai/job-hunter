"""FastAPI dependencies that read the app state and the request state the edge set."""

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.config import AppConfig


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


def request_id(request: Request) -> str:
    """The id `EdgeMiddleware` gave this request (the `X-Request-Id` of its response)."""
    value = getattr(request.state, "request_id", None)
    if not isinstance(value, str):
        raise RuntimeError("the request has no request id: it did not pass the edge")
    return value
