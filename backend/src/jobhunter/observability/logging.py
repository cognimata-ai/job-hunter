"""Structured JSON logging (spec §16.8).

Structlog events and stdlib `logging` records that propagate to the root logger end up in one handler,
whose formatter runs `redact_event` as the last stage before the JSON renderer. Libraries that install
their own handlers and stop propagation (uvicorn's default `log_config` does) bypass this handler until
their logging is pointed at the root logger.

Importing this module already configures structlog with the redacting chain, so a logger obtained or
bound before `configure_logging` runs redacts too; `configure_logging` installs the handler and the level.
"""

import json
import logging
import sys
from typing import IO

import structlog
from structlog.typing import Processor

from jobhunter.observability.redact import redact_event

# What happens to a stdlib record before redaction and rendering. Stdlib does its own `%` formatting.
_FOREIGN_PRE_CHAIN: list[Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
    structlog.processors.format_exc_info,
]

# The chain of a structlog event. `redact_event` runs here as well: `wrap_for_formatter` puts the event
# dict into `record.msg`, so any other handler on the root logger would otherwise print it unredacted.
# The formatter redacts again (idempotent) as the last stage before rendering.
_STRUCTLOG_PROCESSORS: list[Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.stdlib.PositionalArgumentsFormatter(),
    structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
    structlog.processors.format_exc_info,
    redact_event,
    structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
]

# Libraries that log full request URLs at INFO, and those URLs may carry API keys in the query string.
_QUIET_LOGGERS = ("httpx", "httpcore")

_installed_handler: logging.Handler | None = None


def _configure_structlog() -> None:
    structlog.configure(
        processors=_STRUCTLOG_PROCESSORS,
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        # Off, so a later call also reaches loggers that modules created at import time.
        cache_logger_on_first_use=False,
    )


_configure_structlog()


class _JsonStreamHandler(logging.StreamHandler[IO[str]]):
    def handleError(self, record: logging.LogRecord) -> None:
        # The default prints the failing message and its arguments to stderr, unredacted.
        event = {"level": "error", "event": "logging_error", "logger": record.name}
        try:
            self.stream.write(json.dumps(event) + "\n")
            self.flush()
        except (OSError, ValueError):
            pass


def configure_logging(level: str = "INFO", *, stream: IO[str] | None = None) -> None:
    """Route structlog and stdlib logging to one JSON handler on `stream` (default `sys.stdout`).

    Calling it again replaces the handler an earlier call installed; other handlers are left alone.
    """
    global _installed_handler

    _configure_structlog()

    handler = _JsonStreamHandler(sys.stdout if stream is None else stream)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=_FOREIGN_PRE_CHAIN,
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
