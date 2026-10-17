"""Reading structlog events back from pytest's `caplog`.

`structlog.stdlib.ProcessorFormatter.wrap_for_formatter` puts the event dict into `record.msg`, so a captured
record of one of our loggers carries the event as a dict; stdlib records of other loggers carry strings.
"""

from typing import Any

import pytest


def logged(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, Any]]:
    """The captured structlog events named `event`, in the order they were logged."""
    return [rec.msg for rec in caplog.records if isinstance(rec.msg, dict) and rec.msg.get("event") == event]
