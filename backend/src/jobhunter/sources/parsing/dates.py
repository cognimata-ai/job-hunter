"""Posted dates (spec §5.3): relative German and English forms, epochs, ISO strings and the `Date` header.

Nothing here reads the clock: every function that needs "now" takes it as a parameter.

A day-based relative form (`Gestern`, `vor 2 Tagen`, `2 days ago`, `today`) is a calendar-day statement made
in the reader's time, which is Europe/Berlin (Review Focus 1). It resolves to the start of that Berlin day,
counted back in calendar days (never in 24-hour blocks, so the 23- and 25-hour days of the clock changes
come out right), and is returned in UTC. An hour form is elapsed time and is subtracted from the reference.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from typing import Final, Literal
from zoneinfo import ZoneInfo

from jobhunter.normalize.text import norm_text

Precision = Literal["exact", "hour", "day", "relative_day", "ingest", "unknown"]

BERLIN: Final = ZoneInfo("Europe/Berlin")


@dataclass(frozen=True)
class ParsedDate:
    """A provider date, always timezone-aware UTC, with how precisely the provider stated it."""

    at: datetime
    precision: Precision
    raw: str | None


# `\d{1,4}` keeps the arithmetic far from datetime's limits: a longer number is not a posting age.
_HOURS: Final = re.compile(r"(?:vor (\d{1,4}) stunden?|(\d{1,4}) hours? ago)")
_DAYS: Final = re.compile(r"(?:vor (\d{1,4}) tag(?:en)?|(\d{1,4}) days? ago)")
_TODAY: Final = frozenset({"heute", "today", "just posted"})
_YESTERDAY: Final = frozenset({"gestern", "yesterday"})
_HOUR_WORD: Final = re.compile(r"\b(?:hours?|stunden?)\b")
_ZONE_WORD: Final = re.compile(r"\s*\b(?:UTC|GMT)$")


def _utc(moment: datetime) -> datetime:
    """`moment` in UTC; a naive datetime is taken to be UTC already."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _berlin_midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=BERLIN).astimezone(UTC)


def parse_relative(text: str, *, reference: datetime) -> ParsedDate | None:
    """Resolve `vor 2 Tagen`, `Gestern`, `15 hours ago`, `just posted`... against `reference`, or `None`."""
    if reference.tzinfo is None:
        raise ValueError("reference must carry a timezone")
    form = norm_text(text).casefold()
    if (hours := _HOURS.fullmatch(form)) is not None:
        count = int(hours[1] or hours[2])
        at = reference.astimezone(UTC) - timedelta(hours=count)
        return ParsedDate(at, "hour", text)
    days_back: int
    if (days := _DAYS.fullmatch(form)) is not None:
        days_back = int(days[1] or days[2])
    elif form in _TODAY:
        days_back = 0
    elif form in _YESTERDAY:
        days_back = 1
    else:
        return None
    local_day = reference.astimezone(BERLIN).date() - timedelta(days=days_back)
    return ParsedDate(_berlin_midnight(local_day), "relative_day", text)


def jsearch_precision(relative: str) -> Literal["hour", "day"]:
    """JSearch's `job_posted_at_timestamp` is exact to the hour when its relative string speaks of hours."""
    return "hour" if _HOUR_WORD.search(norm_text(relative).casefold()) else "day"


def from_epoch(seconds: int | float, *, millis: bool = False, precision: Precision) -> ParsedDate:
    """A unix timestamp (milliseconds with `millis=True`) as UTC; `raw` is `None`: the source is no string.

    Raises `ValueError` for a value outside the range of `datetime` (milliseconds sent as seconds, say).
    """
    try:
        return ParsedDate(datetime.fromtimestamp(seconds / 1000 if millis else seconds, UTC), precision, None)
    except (OverflowError, OSError) as exc:
        raise ValueError(f"epoch out of range: {seconds!r}") from exc


def parse_iso(text: str, *, precision: Precision) -> ParsedDate | None:
    """An ISO 8601 date or datetime (also `2021-07-20 15:08:38 UTC`) in UTC, `None` if it is not one.

    A string without a zone is taken to be UTC.
    """
    candidate = _ZONE_WORD.sub("+00:00", text.strip())
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    return ParsedDate(_utc(parsed), precision, text)


def response_date(headers: Mapping[str, str], fallback: datetime) -> datetime:
    """The response's RFC 7231 `Date` header in UTC (the header name in any case), else `fallback`; so is a
    value that does not parse, or whose UTC instant is out of range (`31 Dec 9999 23:59:59 -2359`)."""
    for name, value in headers.items():
        if name.casefold() == "date":
            try:
                return _utc(parsedate_to_datetime(value))
            except (TypeError, ValueError, OverflowError):
                return fallback
    return fallback
