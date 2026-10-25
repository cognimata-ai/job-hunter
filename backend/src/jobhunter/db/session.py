"""SQLAlchemy async engines (spec §3.3)."""

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from jobhunter.config import AppConfig


def make_engine(cfg: AppConfig, *, pool_size: int = 10) -> AsyncEngine:
    """The pooled engine of the process, as the database role of `cfg`; the caller disposes it.

    `hide_parameters`: a statement's error text never quotes its bound values (CV text, posting content),
    whatever logs or stores that text (M2 Task 11 fix round 1; defence in depth next to the class-only logs).
    """
    return create_async_engine(
        cfg.dsn("sqlalchemy"), pool_pre_ping=True, pool_size=pool_size, max_overflow=0, hide_parameters=True
    )


def make_null_engine(dsn: str) -> AsyncEngine:
    """An engine without a pool (one connection per use), for one-shot work such as `migrate` and tests."""
    return create_async_engine(dsn, poolclass=NullPool)
