"""Structured JSON logging (spec §16.8).

Structlog events and stdlib `logging` records that propagate to the root logger end up in one handler,
whose formatter runs `redact_event` as the last stage before the JSON renderer. Libraries that install
their own handlers and stop propagation (uvicorn's default `log_config` does) bypass this handler until
their logging is pointed at the root logger.

Importing this module already configures structlog with the redacting chain, so a logger obtained or
bound before `configure_logging` runs redacts too, and it replaces `logging.lastResort` (the handler
stdlib uses when no handler exists) with a redacting one; `configure_logging` installs the handler and
the level.
"""

import json
import logging
import sys
from collections.abc import Mapping
from typing import IO

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from jobhunter.observability.redact import redact_event

# What happens to a stdlib record before redaction and rendering. Stdlib does its own `%` formatting.
_FOREIGN_PRE_CHAIN: list[Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
    structlog.processors.format_exc_info,
]


def _format_positional_args(logger: WrappedLogger, method: str, event_dict: EventDict) -> EventDict:
    """Like structlog's `PositionalArgumentsFormatter`, but it never raises.

    If formatting fails for any reason (a malformed call, an argument `%` cannot convert, an argument whose
    `__str__` raises, a `positional_args` keyword that is no sequence), the event text and
    `positional_args` stay as they are. Redaction then masks the arguments as a list, or, when one of them
    cannot even be turned into a string, replaces the event with its fail-closed form.
    """
    try:
        args = event_dict.get("positional_args")
        if args is None:
            return event_dict
        if args:
            # The stdlib accepts both positional arguments and a single mapping.
            if len(args) == 1 and isinstance(args[0], Mapping) and args[0]:
                args = args[0]
            # Nothing in the event changes before this last step that can fail.
            event_dict["event"] = event_dict["event"] % args
        del event_dict["positional_args"]
    except Exception:
        # The outcome is the event as the caller made it, rendered by the rest of the chain.
        return event_dict
    return event_dict


# The chain of a structlog event. `redact_event` runs here as well: `wrap_for_formatter` puts the event
# dict into `record.msg`, so any other handler on the root logger would otherwise print it unredacted.
# The formatter redacts again (idempotent) as the last stage before rendering.
_STRUCTLOG_PROCESSORS: list[Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    _format_positional_args,
    structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
    structlog.processors.format_exc_info,
    redact_event,
    structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
]

# Libraries that log full request URLs at INFO, and those URLs may carry API keys in the query string.
_QUIET_LOGGERS = ("httpx", "httpcore")
# The Anthropic SDK and its HTTP stack (`httpx2`, `httpcore2`). With `ANTHROPIC_LOG=debug` the SDK's import
# sets their levels to DEBUG, and the SDK then logs each request's options: the whole prompt, with CV text
# (privacy rule: CV text, profile JSON and LLM output never reach a log). They are held at WARNING with no
# handler of their own, and the app's handlers drop anything of theirs below WARNING, whatever level a later
# import sets.
_CONTENT_LOGGERS = ("anthropic", "httpx2", "httpcore2")
# python-multipart's parse-error warnings quote the offending byte of a request body ("got 88 at index 9"),
# and a CV upload's body is a CV (M2 Task 9). It logs nothing above WARNING, so it is held at ERROR; the
# upload route logs that a form was refused, with its status and code.
_BODY_QUOTING_LOGGERS = ("python_multipart",)
# The format of the root handler `logging.basicConfig` adds when the SDK is imported with `ANTHROPIC_LOG` set
# and the root logger has no handler yet (anthropic 1.15.0 `_utils/_logs.py`): it writes records unredacted.
_SDK_BASIC_CONFIG_FORMAT = "[%(asctime)s - %(name)s:%(lineno)d - %(levelname)s] %(message)s"

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


def _from_content_logger(name: str) -> bool:
    return any(name == prefix or name.startswith(f"{prefix}.") for prefix in _CONTENT_LOGGERS)


def _no_library_content(record: logging.LogRecord) -> bool:
    """A handler filter: a `_CONTENT_LOGGERS` record below WARNING is dropped."""
    return record.levelno >= logging.WARNING or not _from_content_logger(record.name)


def quiet_library_loggers() -> None:
    """Hold the `_QUIET_LOGGERS` and `_CONTENT_LOGGERS` at WARNING, the latter with no handler of their own
    (so their records reach the app's redacting handler only), and the `_BODY_QUOTING_LOGGERS` at ERROR; and
    remove the unredacted root handler that the SDK's import may have added. `configure_logging` calls it,
    and so does `llm/transport.py` right after it imports the SDK, which may set its levels again at that
    import."""
    for name in (*_QUIET_LOGGERS, *_CONTENT_LOGGERS):
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in _BODY_QUOTING_LOGGERS:
        logging.getLogger(name).setLevel(logging.ERROR)
    for name in _CONTENT_LOGGERS:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        logger.propagate = True
    root = logging.getLogger()
    for handler in list(root.handlers):
        formatter = handler.formatter
        if (
            type(handler) is logging.StreamHandler
            and formatter is not None
            and formatter._fmt == _SDK_BASIC_CONFIG_FORMAT
        ):
            root.removeHandler(handler)


class _JsonStreamHandler(logging.StreamHandler[IO[str]]):
    def __init__(self, stream: IO[str]) -> None:
        super().__init__(stream)
        self.addFilter(_no_library_content)

    def handleError(self, record: logging.LogRecord) -> None:
        # The default prints the failing message and its arguments to stderr, unredacted. This never
        # raises: a log call must not fail because the line about its failure could not be written.
        event = {"level": "error", "event": "logging_error", "logger": record.name}
        try:
            self.stream.write(json.dumps(event) + "\n")
            self.flush()
        except Exception:  # noqa: S110 - there is nobody left to report this failure to
            pass


class _LastResortHandler(_JsonStreamHandler):
    """Takes the place of `logging.lastResort`: writes to the current `sys.stderr`, like the original."""

    def __init__(self) -> None:
        super().__init__(sys.stderr)
        self.setLevel(logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr
        super().emit(record)


def _json_formatter() -> structlog.stdlib.ProcessorFormatter:
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_FOREIGN_PRE_CHAIN,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            redact_event,
            structlog.processors.JSONRenderer(),
        ],
    )


def _install_last_resort() -> None:
    # Used only for a record that no handler on its logger's path takes, i.e. before `configure_logging`.
    handler = _LastResortHandler()
    handler.setFormatter(_json_formatter())
    logging.lastResort = handler


_install_last_resort()


def configure_logging(level: str = "INFO", *, stream: IO[str] | None = None) -> None:
    """Route structlog and stdlib logging to one JSON handler on `stream` (default `sys.stdout`).

    Calling it again replaces the handler an earlier call installed; other handlers are left alone.
    """
    global _installed_handler

    _configure_structlog()

    handler = _JsonStreamHandler(sys.stdout if stream is None else stream)
    handler.setFormatter(_json_formatter())

    root = logging.getLogger()
    if _installed_handler is not None:
        root.removeHandler(_installed_handler)
    root.addHandler(handler)
    root.setLevel(level.upper())
    quiet_library_loggers()
    _installed_handler = handler


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """A logger that renders as JSON with `logger` set to `name`."""
    return structlog.stdlib.get_logger(name)
