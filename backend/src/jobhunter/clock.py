"""The one place that reads the wall clock, so tests can substitute it."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    """The current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)
