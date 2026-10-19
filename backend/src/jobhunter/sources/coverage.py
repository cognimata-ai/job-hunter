"""Coverage windows of the search cells (spec §5.5.3, §5.6; Task 14 adds the persisted coverage rules).

A traversal started at `now` requests the window `[now - b, now]`. For JSearch and SerpApi `b` is the span of
the freshness bucket (`freshness_bucket`): the smallest of `BUCKETS` that is at least
`now - last_covered_at + 24 h` (the 24 h cover day-truncated provider dates), `month` for a never-covered
cell. A span longer than the provider allows (720 h) is cut to `month`, and the part the window cannot reach,
`[last_covered_at, now - 720 h]`, is returned to be abandoned at once (`beyond_provider_window`). For Adzuna
`b` is `max_days_old` days (`adzuna_max_days_old`).

`JSearchContinuation` is the `coverage_cells.continuation` document of a JSearch cell (Ruling T13-b): the
cursor its last traversal left, when that cursor was obtained, the window of that traversal and the page the
cursor fetches. The planner reads it for the cursor reuse of §5.3.1, Task 14 writes it, both through
`parse` and `dump`; a document that is absent or malformed parses as `None` (plan from page 1).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Final, Literal

from jobhunter.sources.base import CoverageWindow
from jobhunter.types import JSONValue

Bucket = Literal["today", "3days", "week", "month"]
# The JSearch `date_posted` buckets, smallest first (§5.5.3).
BUCKETS: Final[Mapping[Bucket, timedelta]] = MappingProxyType(
    {
        "today": timedelta(hours=24),
        "3days": timedelta(hours=72),
        "week": timedelta(hours=168),
        "month": timedelta(hours=720),
    }
)
PROVIDER_SPAN: Final = BUCKETS["month"]  # the longest span a search provider serves (JSearch: 720 h)
DATE_SLACK: Final = timedelta(hours=24)  # provider dates are truncated to the day
_DAY: Final = timedelta(days=1)


def freshness_bucket(
    now: datetime, last_covered_at: datetime | None
) -> tuple[Bucket, CoverageWindow, CoverageWindow | None]:
    """The bucket of a search cell at `now`, its window `[now - span, now]`, and the unreachable part
    `[last_covered_at, now - 720 h]` to abandon, `None` unless the needed span exceeds 720 h and the coverage
    ends before the window starts."""
    if last_covered_at is None:
        return "month", (now - PROVIDER_SPAN, now), None
    needed = now - last_covered_at + DATE_SLACK
    for bucket, span in BUCKETS.items():
        if span >= needed:
            return bucket, (now - span, now), None
    lower = now - PROVIDER_SPAN
    return "month", (lower, now), (last_covered_at, lower) if last_covered_at < lower else None


def adzuna_max_days_old(now: datetime, last_covered_at: datetime | None, max_ad_age_days: int) -> int:
    """`min(max_ad_age_days, ceil((now - last_covered_at) / 1 d) + 1)`, at least 1; `max_ad_age_days` for a
    never-covered cell (§5.5.3)."""
    if last_covered_at is None:
        return max_ad_age_days
    days = -((last_covered_at - now) // _DAY)  # the ceiling, in whole days
    return max(1, min(max_ad_age_days, days + 1))


def _aware(value: JSONValue) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


@dataclass(frozen=True)
class JSearchContinuation:
    """A JSearch cell's continuation: `cursor` fetches page `page_no` (≥ 2) of the traversal of `window`,
    handed out at `obtained_at`."""

    cursor: str
    obtained_at: datetime
    window: CoverageWindow
    page_no: int

    @classmethod
    def parse(cls, doc: JSONValue) -> "JSearchContinuation | None":
        """The continuation `dump` wrote; `None` for anything else (absent, malformed, another kind)."""
        if not isinstance(doc, dict):
            return None
        cursor, page_no, bounds = doc.get("cursor"), doc.get("page_no"), doc.get("window")
        obtained_at = _aware(doc.get("obtained_at"))
        if not isinstance(cursor, str) or not cursor or obtained_at is None:
            return None
        if isinstance(page_no, bool) or not isinstance(page_no, int) or page_no < 2:
            return None
        if not isinstance(bounds, list) or len(bounds) != 2:
            return None
        lower, upper = _aware(bounds[0]), _aware(bounds[1])
        if lower is None or upper is None or lower >= upper:
            return None
        return cls(cursor=cursor, obtained_at=obtained_at, window=(lower, upper), page_no=page_no)

    def dump(self) -> dict[str, JSONValue]:
        """The `coverage_cells.continuation` document."""
        return {
            "cursor": self.cursor,
            "obtained_at": self.obtained_at.isoformat(),
            "window": [self.window[0].isoformat(), self.window[1].isoformat()],
            "page_no": self.page_no,
        }
