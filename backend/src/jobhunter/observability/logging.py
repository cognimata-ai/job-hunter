"""Structured JSON logging (spec §16.8).

Structlog events and stdlib `logging` records (uvicorn, sqlalchemy, procrastinate, ...) end up in one
handler, whose formatter runs `redact_event` as the last stage before the JSON renderer.
"""

import logging
import sys
from typing import IO

import structlog
from structlog.typing import Processor

from jobhunter.observability.redact import redact_event

# What happens to an event before redaction and rendering. Stdlib records, which never passed through
# structlog, get the same stages from the formatter's `foreign_pre_chain`.
_PRE_RENDER: list[Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
    structlog.processors.format_exc_info,
]

# Libraries that log full request URLs at INFO, and those URLs may carry API keys in the query string.
_QUIET_LOGGERS = ("httpx", "httpcore")

_installed_handler: logging.Handler | None = None


def configure_logging(level: str = "INFO", *, stream: IO[str] | None = None) -> None:
    """Route structlog and stdlib logging to one JSON handler on `stream` (default `sys.stdout`).

    Calling it again replaces the handler an earlier call installed; other handlers are left alone.
    """
    global _installed_handler

    structlog.configure(
        # `redact_event` runs here as well, so a record that reaches any other root handler through
        # `wrap_for_formatter` already carries redacted values. The formatter below redacts again
        # (idempotent) as the last stage before rendering, which is also what stdlib records get.
        processors=[*_PRE_RENDER, redact_event, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        # Off, so a later call also reaches loggers that modules created at import time.
        cache_logger_on_first_use=False,
    )

    handler = logging.StreamHandler(sys.stdout if stream is None else stream)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=_PRE_RENDER,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                redact_event,
                structlog.processors.JSONRenderer(),
            ],
        )
    )

    root = logging.getLogger()
    if _installed_handler is not None:
        root.removeHandler(_installed_handler)
    root.addHandler(handler)
    root.setLevel(level.upper())
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    _installed_handler = handler


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """A logger that renders as JSON with `logger` set to `name`."""
    return structlog.stdlib.get_logger(name)
