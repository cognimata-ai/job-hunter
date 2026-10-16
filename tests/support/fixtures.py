"""Shared pytest fixtures; later tasks add their fixtures here."""

import logging
from collections.abc import Iterator

import pytest
import structlog
from jobhunter.observability import redact

_QUIET_LOGGERS = ("httpx", "httpcore")


@pytest.fixture(autouse=True)
def _restore_logging_state() -> Iterator[None]:
    """`configure_logging` changes process-wide state; give every test the state it found."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    quiet_levels = {name: logging.getLogger(name).level for name in _QUIET_LOGGERS}
    structlog_config = structlog.get_config()
    bound_context = structlog.contextvars.get_contextvars()
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, quiet_level in quiet_levels.items():
        logging.getLogger(name).setLevel(quiet_level)
    structlog.configure(**structlog_config)
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(**bound_context)
    redact._reset_secret_values()
