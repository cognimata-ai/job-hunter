"""The Arbeitnow adapter, a free firehose feed (spec §5.3.4), and its coverage model (§5.6, P-AN1).

Request: `GET https://www.arbeitnow.com/api/job-board-api?page=N`, no key; `N` is the spec's `page_no`, so
a spec's own parameters stay empty. A page holds `PAGE_SIZE` items.

Envelope (spike V8, `docs/spikes/V8.md`): an object with a `data` array, a `meta` object and a `links`
object whose `next` is the next page's URL, or `null` (or absent) on the last page. `check_envelope` asserts
all of them; `peek_native_ids`, `parse` and `continuation` apply the same check, so no path reads a broken
body as a page or as the end of the feed. A `next` that is neither `null` nor a URL is a violation, never
the end. The end of the feed is an empty `data` or a missing `next`, whichever the envelope shows (T10-a).

Items: `native_id` is the `slug`, the key `arbeitnow:<numeric suffix of the slug>` (the namespace of the
Arbeitnow URLs found in Google results); `created_at` (unix seconds) is the posted date, precision `exact`,
and the item time on the coverage axis. The description is HTML, sanitised with nh3. About a fifth of the
recorded items (114 of V8's 600) carry the employer's HTML entity-escaped (`&lt;p&gt;...`) followed by
Arbeitnow's own real-HTML footer: a description that starts with an escaped tag is unescaped once before it
is sanitised. `remote: true` is structured evidence of remote work, `false` says nothing. `job_types` are
text evidence of the employment types. The location text is parsed with the gazetteer, and an item is
`OutOfScope("not_germany")` only on positive evidence: every place it names is outside Germany (§8.12).

Settlement: a free source is never billed and has no ledger entry; `settle` says `not_billed` for any
outcome and `observation` is `None`. `remaining_low` reads `x-ratelimit-remaining` (§5.3.4: stop at <= 5).
V8 found the header frozen in Cloudflare cache hits; a miss carries the live count, which is when it matters.

The coverage model (ruling T10-a, which applies T6-d)
------------------------------------------------------

§5.3.4 assumes a feed ordered by `created_at`. V8 recorded otherwise: every page is four runs of 100, 50, 50
and 100 items, each newest first. Runs 1 to 3 continue exactly across the page boundary (page 1's first run
ends at 11:55:23 and page 2's begins there). Run 4 does not: page 2's fourth run opens at 20:00:44, 12 s
below page 1's newest item (20:00:56), and reaches back 53 h, to 14:40 two days earlier. The pages are also
cached apart, each filled at its own time, up to an hour old. So **no page bounds the creation times of the
pages after it.** Every stop on a time is unsafe, and every run reads to the page cap; only the end of the
feed stops it earlier.

The covered bound this module proves, and nothing more:

- **A read from page 1 to the end of the feed covers `[oldest item read, newest item read)`, its upper bound
  clamped to the earliest fill instant of its pages** (`filled_at`: the response's `Date` less its `Age`, the
  moment the cached page was made, fix wave F4, ledger L137). It saw every item the feed held when its pages
  were made. An item created after the earliest of those instants can be missing although the read reaches
  past it: its page was made before it existed, and by the time a later page was made the item sat on an
  earlier page (pages fill one after the other, so in-order fills hide such an item as well). So nothing
  newer than the earliest fill is proven, and the read part above it is recorded as a gap, its reason
  `unknown_continuation` (Ruling FR-o: no page cap stopped the read; what lies above is unknown). A page
  whose age is unknown (a cached response without a readable `Age`, Ruling FR-q) proves nothing above the
  cell's previous covered bound. Half-open, the
  upper instant itself is left to the next run: a claim may fall short of the truth, never exceed it. A
  residual risk stays (Ruling T10-b, accepted): an item older than every fill can still move from a page
  made later to a page made earlier and be read on neither; only reading every page at one instant would
  close it, which a cached feed does not allow.
- **A read stopped by the cap or by the remaining-low rule covers nothing.** The bound "everything newer
  than the newest of the runs' oldest items is read" does not survive V8's own data: after page 1 that bound
  is 19:30:49 (run 3's oldest), and page 2's fourth run holds five items at 20:00:43 and 20:00:44. With run 4
  restarting at the top of every page, an unread page can hold items from anywhere in the span, even seconds
  below the newest item read, so the only bound that survives is the empty one.
- **No seek leg is planned.** §5.3.4's seek to an open gap (a page found by a count of newer items) does not
  locate a time in such a feed, and the read from page 1 runs to the cap or to the end of the feed, so no
  page budget would ever remain for one (the end of the feed ends the traversal). Every traversal is that
  one read; a gap is closed only by a later read that reaches the end of the feed (plan Deviation 27).

`newest_covered_upper` is the upper bound of the newest proven covered span (`None` before the first). The
cell's accounting frontier is the newest of it and every gap's upper bound: every instant below the
frontier, back to the cell's first read, is in a covered span or in a recorded gap. A read that proves
nothing records the gap `[frontier, newest item read)` (from its oldest item when there is no frontier yet: a
never-covered cell claims nothing older than what it read). A read that reached the end records the gap
`[frontier, oldest item read)` when the feed no longer reaches back to the frontier (those items left the
feed unread) and, when the earliest fill clamped its span, the gap from the span's upper bound (or the
frontier, when that is newer) to its newest item; it closes every open gap its covered span touches and
reopens the parts outside it as new gaps (narrowing is recorded, never silent). A gap whose upper bound is
at or older than `now - max_ad_age_days` is abandoned. Every interval is `[older, newer)`; gaps are never
dropped.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from math import ceil, isfinite
from typing import Final, Literal

import httpx

from jobhunter.db.seed import canonical_json
from jobhunter.normalize.html import html_to_text, sanitize_html, unescape_html
from jobhunter.normalize.keys import AdKey
from jobhunter.normalize.quality import Quality, description_quality
from jobhunter.normalize.urls import canonical_url
from jobhunter.sources.base import (
    Capabilities,
    CellPlan,
    Continuation,
    ContractViolation,
    CoverageWindow,
    DuplicateInPage,
    GapReason,
    ItemOutcome,
    NormalizedPosting,
    Observation,
    OutOfScope,
    Parsed,
    RateLimit,
    RawPage,
    Rejected,
    RequestSpec,
    Settlement,
)
from jobhunter.sources.http import HttpOutcome, send
from jobhunter.sources.parsing.dates import from_epoch, response_date
from jobhunter.sources.parsing.employment import EmploymentType, parse_employment
from jobhunter.sources.parsing.locations import ParsedLocations, parse_location_text
from jobhunter.types import JSONValue

SOURCE_ID: Final = "arbeitnow"
FEED_URL: Final = "https://www.arbeitnow.com/api/job-board-api"
PAGE_SIZE: Final = 300
LOW_REMAINING: Final = 5  # stop when `x-ratelimit-remaining` is at most this (§5.3.4)
MIN_PAGE_CAP: Final = 3
MAX_PAGE_CAP: Final = 10
_HOURS_PER_PAGE: Final = 30  # `ceil(interval_h / 30) + 1` (§5.3.4)

_ALL_WORK_MODES: Final = frozenset({"remote", "hybrid", "onsite"})
# The `cf-cache-status` values of a response the edge cache served (V8: every recorded page was a HIT).
_CACHE_SERVED: Final = frozenset({"hit", "stale", "updating", "revalidated"})
_NUMERIC_SUFFIX: Final = re.compile(r"-(\d+)$")
_ESCAPED_TAG_FIRST: Final = re.compile(r"^\s*&lt;/?[A-Za-z]")


def page_cap(interval_s: int) -> int:
    """`min(10, max(3, ceil(interval_h / 30) + 1))` for a run interval of `interval_s` seconds (§5.3.4)."""
    if interval_s < 0:
        raise ValueError(f"a run interval is not negative, got {interval_s}")
    pages_for_interval = -(-interval_s // (_HOURS_PER_PAGE * 3600)) + 1  # exact: no float ceil
    return min(MAX_PAGE_CAP, max(MIN_PAGE_CAP, pages_for_interval))


# ---- the coverage model -------------------------------------------------------------------------------

GapStatus = Literal["open", "closed", "abandoned"]
# What ended a run (T10-a): the page cap, the remaining-low rule, or the end of the feed. No time stops a run.
StopReason = Literal["cap", "remaining", "end"]


@dataclass(frozen=True)
class Gap:
    """A hole in the cell's coverage on the `created_at` axis, `[lower, upper)`.

    `reason` is why it is open when that is known here (`None`: the recorder gives it the reason of the run's
    traversal): a gap above the earliest fill of a read's pages is `unknown_continuation` (Ruling FR-o), and
    the remainder of a narrowed gap keeps its gap's reason. It is no part of a gap's identity
    (`compare=False`): a gap is its span and its status.
    """

    lower: datetime
    upper: datetime
    status: GapStatus
    reason: GapReason | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not self.lower < self.upper:
            raise ValueError(
                f"a gap is [older, newer): {self.lower.isoformat()} is not before {self.upper.isoformat()}"
            )


@dataclass(frozen=True)
class FirehoseState:
    """The cell's coverage, rebuilt by Task 14 from `coverage_cells.last_covered_at` and its gap intervals.

    `newest_covered_upper` is the upper bound of the newest proven covered span. `gaps` holds the gap
    intervals of every status: the newest gap's upper bound is part of the accounting frontier (see the
    module docstring).
    """

    newest_covered_upper: datetime | None
    gaps: tuple[Gap, ...]


def _cutoff(now: datetime, max_ad_age_days: int) -> datetime:
    return now - timedelta(days=max_ad_age_days)


def _age_s(headers: Mapping[str, str]) -> int | None:
    """The page's age in whole seconds, or `None` when it is unknown (Ruling FR-q: fail safe, never a claim
    the headers do not prove). A joined `Age` (`1440, 1440`, two headers) takes the largest, a fraction is
    rounded up; any other `Age` is unknown. Without an `Age`, a response the cache served
    (`cf-cache-status`, V8's signal) has an unknown age, and any other one (a miss, or no cache at all) is
    as old as its `Date`."""
    raw = headers.get("age")
    if raw is None:
        return None if headers.get("cf-cache-status", "").strip().casefold() in _CACHE_SERVED else 0
    seconds: list[int] = []
    for part in raw.split(","):
        try:
            value = float(part.strip())
        except ValueError:
            return None
        if not isfinite(value) or value < 0:
            return None
        seconds.append(ceil(value))
    return max(seconds)


def filled_at(page: RawPage) -> datetime | None:
    """When the page's content was made: its `Date` header (else its arrival, `fetched_at`) less its `Age`;
    `None` when its age is unknown (`_age_s`)."""
    age = _age_s(page.headers)
    if age is None:
        return None
    return response_date(page.headers, page.fetched_at) - timedelta(seconds=age)


def covered_span(
    times: Sequence[datetime], *, stopped_by: StopReason, filled_at: datetime | None = None
) -> CoverageWindow | None:
    """The span the read from page 1 proves covered: `[oldest, newest)` of the item times `times` it read,
    when it reached the end of the feed, its upper bound at most `filled_at` (the earliest fill instant of
    its pages, or the cell's previous covered bound when a page's age is unknown; `None`: no clamp, as for a
    page read live); `None` otherwise or when that span is empty.

    Task 14 writes it as the run's `covered` interval; `conclude` applies the same span.
    """
    if stopped_by != "end" or not times:
        return None
    lower, upper = min(times), max(times)
    if filled_at is not None:
        upper = min(upper, filled_at)
    return (lower, upper) if lower < upper else None


def _frontier(state: FirehoseState) -> datetime | None:
    bounds = [gap.upper for gap in state.gaps]
    if state.newest_covered_upper is not None:
        bounds.append(state.newest_covered_upper)
    return max(bounds, default=None)


def _validate(*, pages: int, cap: int, stopped_by: StopReason) -> None:
    if pages < 1:
        raise ValueError(f"a traversal reads at least one page, got {pages}")
    if pages > cap:
        raise ValueError(f"{pages} pages exceed the page cap {cap}")
    if stopped_by == "cap" and pages != cap:
        raise ValueError(f"a traversal stopped by the cap read {cap} pages, not {pages}")


def _with_age(lower: datetime, upper: datetime, cutoff: datetime, reason: GapReason | None = None) -> Gap:
    return Gap(lower, upper, "abandoned" if upper <= cutoff else "open", reason)


def conclude(
    state: FirehoseState,
    times: Sequence[datetime],
    *,
    pages: int,
    cap: int,
    stopped_by: StopReason,
    now: datetime,
    max_ad_age_days: int,
    filled_at: datetime | None = None,
) -> tuple[FirehoseState, list[Gap]]:
    """The cell's coverage after a traversal (T10-a; the rules are the module docstring's).

    `times` holds the `created_at` of every item the read from page 1 saw, in reading order; `pages` counts
    its pages; `filled_at` is the earliest fill instant of those pages (`covered_span`). Returns the new
    state, whose `gaps` keep every earlier gap in place (with its new status), each narrowed gap followed by
    its remainders, then the gaps this run opened (below the covered span, then above it); and the gaps this
    run created or changed, in the same order: the rows Task 14 writes, with `covered_span` for the covered
    row. Raises `ValueError` for reads no traversal produces.
    """
    _validate(pages=pages, cap=cap, stopped_by=stopped_by)
    cutoff = _cutoff(now, max_ad_age_days)
    span = covered_span(times, stopped_by=stopped_by, filled_at=filled_at)
    frontier = _frontier(state)

    gaps: list[Gap] = []
    changed: list[Gap] = []
    for gap in state.gaps:
        updated, remainders = gap, []
        if gap.status == "open" and span is not None and gap.lower < span[1] and span[0] < gap.upper:
            updated = replace(gap, status="closed")
            if gap.lower < span[0]:
                remainders.append(_with_age(gap.lower, span[0], cutoff, gap.reason))
            if span[1] < gap.upper:
                remainders.append(_with_age(span[1], gap.upper, cutoff, gap.reason))
        elif gap.status == "open" and gap.upper <= cutoff:
            updated = replace(gap, status="abandoned")
        gaps += [updated, *remainders]
        if updated != gap:
            changed += [updated, *remainders]

    newest_covered = state.newest_covered_upper
    if times:
        oldest, newest = min(times), max(times)
        # A read to the end whose pages were made before its newest item proves nothing above the earliest
        # fill (`cut`, at least its oldest item): that part is a gap of its own (FR-o, FR-q).
        cut = None
        if stopped_by == "end" and filled_at is not None and filled_at < newest:
            cut = max(filled_at, oldest)
        # the unproven part below: below the covered span (or the cut) when there is one, else everything read
        if cut is not None:
            unproven_upper = oldest
        else:
            unproven_upper = span[0] if span is not None else newest
        if frontier is not None:
            unproven_lower = frontier
        else:  # a never-covered cell claims nothing older than what it read
            unproven_lower = span[0] if span is not None else oldest
        if unproven_lower < unproven_upper:
            opened = _with_age(unproven_lower, unproven_upper, cutoff)
            gaps.append(opened)
            changed.append(opened)
        if cut is not None:
            # read but not proven: newer than the earliest fill (the part the frontier has not accounted)
            above = cut if frontier is None else max(cut, frontier)
            if above < newest:
                newer = _with_age(above, newest, cutoff, "unknown_continuation")
                gaps.append(newer)
                changed.append(newer)
        if span is not None and (newest_covered is None or span[1] > newest_covered):
            newest_covered = span[1]
    return FirehoseState(newest_covered, tuple(gaps)), changed


# ---- the adapter ----------------------------------------------------------------------------------------


def _envelope(page: RawPage) -> tuple[list[JSONValue], str | None]:
    """The `data` array and the `links.next` URL (`None` on the last page) of the envelope object; any other
    body raises `ContractViolation`."""
    doc = page.json()
    if not isinstance(doc, dict):
        raise ContractViolation("not_object", status=page.status)
    data, meta, links = doc.get("data"), doc.get("meta"), doc.get("links")
    if not isinstance(data, list):
        raise ContractViolation("data_not_list", status=page.status)
    if not isinstance(meta, dict):
        raise ContractViolation("meta_missing", status=page.status)
    if not isinstance(links, dict):
        raise ContractViolation("links_missing", status=page.status)
    following = links.get("next")
    if following is None:
        return data, None
    if isinstance(following, str) and following:
        return data, following
    raise ContractViolation("links_next_invalid", status=page.status)


def _text(job: dict[str, JSONValue], key: str) -> str | None:
    value = job.get(key)
    return value if isinstance(value, str) else None


def _slug(job: JSONValue) -> str | None:
    slug = job.get("slug") if isinstance(job, dict) else None
    return slug if isinstance(slug, str) and slug else None


def _created(job: dict[str, JSONValue]) -> datetime | None:
    stamp = job.get("created_at")
    if isinstance(stamp, bool) or not isinstance(stamp, int):
        return None
    try:
        return from_epoch(stamp, precision="exact").at
    except ValueError:  # a timestamp outside the calendar: the date is unknown, the ad is not rejected
        return None


def _description(raw: JSONValue) -> tuple[str, str | None, Quality]:
    """The plain text, the sanitised HTML (`None` without a description) and the quality of the text."""
    if not isinstance(raw, str):
        return "", None, description_quality("")
    html = unescape_html(raw) if _ESCAPED_TAG_FIRST.match(raw) else raw
    sanitized = sanitize_html(html)
    text = html_to_text(sanitized)
    return text, sanitized, description_quality(text)


def _employment(value: JSONValue) -> tuple[EmploymentType, ...]:
    found: list[EmploymentType] = []
    for entry in value if isinstance(value, list) else []:
        for kind in parse_employment(entry) if isinstance(entry, str) else []:
            if kind not in found:
                found.append(kind)
    return tuple(found)


def _rejected(index: int, native_id: str | None, detail: str) -> Rejected:
    return Rejected(index, native_id, None, "item_contract", detail)


def _parse_item(index: int, job: JSONValue) -> Parsed | OutOfScope | Rejected:
    if not isinstance(job, dict):
        return _rejected(index, None, "the item is not an object")
    slug = _slug(job)
    if slug is None:
        return _rejected(index, None, "slug is missing or not a string")
    title, company = job.get("title"), job.get("company_name")
    if not isinstance(title, str):
        return _rejected(index, slug, "title is not a string")
    if not isinstance(company, str):
        return _rejected(index, slug, "company_name is not a string")

    created = _created(job)
    location = _text(job, "location")
    places = (
        parse_location_text(location, source=SOURCE_ID) if location else ParsedLocations([], "unknown", False)
    )
    if places.positively_non_de:
        return OutOfScope(index, slug, created, "not_germany")
    text, sanitized, quality = _description(job.get("description"))
    employment = _employment(job.get("job_types"))
    remote = job.get("remote") is True
    suffix = _NUMERIC_SUFFIX.search(slug)
    link = (_text(job, "url") or "").strip()
    posting = NormalizedPosting(
        native_id=slug,
        title_raw=title,
        company_raw=company,
        locations=places.locations,
        location_kind=places.kind,
        work_mode_possible=frozenset({"remote"}) if remote else _ALL_WORK_MODES,
        work_mode_source="structured" if remote else "none",
        remote_countries=(),
        employment_types=employment,
        employment_source="text" if employment else "none",
        salary=None,
        posted_at=created,
        posted_precision="unknown" if created is None else "exact",
        posted_raw=None,
        item_ts=created,
        description_text=text,
        description_html_sanitized=sanitized,
        description_completeness="full",
        description_quality=quality,
        ad_language_structured=None,
        primary_link=canonical_url(link) if link else None,
        outbound_url_private=None,
        publisher=None,
        raw_keys=() if suffix is None else (AdKey(SOURCE_ID, suffix[1]),),
        requisition_id=None,
        source_structured={},
    )
    return Parsed(index, slug, created, posting)


def _remaining(page: RawPage) -> int | None:
    value = page.headers.get("x-ratelimit-remaining", "").strip()
    return int(value) if value.isascii() and value.isdigit() else None


class ArbeitnowAdapter:
    """`SourceAdapter` for the Arbeitnow feed."""

    def __init__(self, client: httpx.AsyncClient, *, instance_id: int, clock: Callable[[], datetime]) -> None:
        self._client = client
        self._instance_id = instance_id
        self._clock = clock

    def capabilities(self) -> Capabilities:
        return Capabilities(
            SOURCE_ID, "free", 0, "page", PAGE_SIZE, "exact", "full", False, 1, [RateLimit(SOURCE_ID, 0.5, 1)]
        )

    def plan(self, cell: CellPlan) -> RequestSpec:
        """Page 1, always: every run reads the head of the feed first (§5.3.4, T10-a)."""
        if cell.source_id != SOURCE_ID:
            raise ValueError(f"cell {cell.cell_id} is a {cell.source_id} cell, not an {SOURCE_ID} cell")
        if cell.instance_id != self._instance_id:
            raise ValueError(
                f"cell {cell.cell_id} is planned for instance {cell.instance_id}, not {self._instance_id}"
            )
        return RequestSpec(
            source_instance_id=self._instance_id,
            operation="list",
            cell_id=cell.cell_id,
            page_no=1,
            params={},
            cursor_in=None,
            detail_native_id=None,
            parent_fetch_request_id=None,
            window=cell.window,
            request_class=cell.request_class,
        )

    async def fetch(self, spec: RequestSpec) -> HttpOutcome:
        """One attempt for page `spec.page_no`; never a retry."""
        params = {**spec.params, "page": str(spec.page_no)}
        request = self._client.build_request("GET", FEED_URL, params=params)
        return await send(self._client, spec, request, now=self._clock)

    def check_envelope(self, page: RawPage) -> None:
        _envelope(page)

    def peek_native_ids(self, page: RawPage) -> list[str | None]:
        return [_slug(job) for job in _envelope(page)[0]]

    def elements(self, page: RawPage) -> list[bytes]:
        return [canonical_json(job) for job in _envelope(page)[0]]

    def parse(self, page: RawPage) -> list[ItemOutcome]:
        outcomes: list[ItemOutcome] = []
        first_seen: dict[str, int] = {}
        for index, job in enumerate(_envelope(page)[0]):
            outcome: ItemOutcome = _parse_item(index, job)
            if isinstance(outcome, Parsed | OutOfScope):
                first = first_seen.setdefault(outcome.native_id, index)
                if first != index:
                    outcome = DuplicateInPage(index, outcome.native_id, outcome.item_ts, first)
            outcomes.append(outcome)
        return outcomes

    def continuation(self, page: RawPage) -> tuple[Continuation, str | None]:
        """`END` on an empty `data` or without `links.next` (T10-a); otherwise the next page's number."""
        data, following = _envelope(page)
        if not data or following is None:
            return Continuation.END, None
        return Continuation.CURSOR, str(page.spec.page_no + 1)

    def next_page(self, spec: RequestSpec, cursor: str | None) -> RequestSpec | None:
        """The page `cursor` names (a page number, from `continuation`), in the same class."""
        if not cursor:
            return None
        if not (cursor.isascii() and cursor.isdigit()) or int(cursor) < 1:
            raise ValueError(f"an Arbeitnow cursor is a page number >= 1, got {cursor!r}")
        return replace(spec, page_no=int(cursor), cursor_in=None)

    def remaining_low(self, page: RawPage) -> bool:
        """`x-ratelimit-remaining ≤ LOW_REMAINING`; a missing or unreadable header is not low."""
        remaining = _remaining(page)
        return remaining is not None and remaining <= LOW_REMAINING

    def settle(self, outcome: HttpOutcome) -> Settlement:
        """A free source is never billed: no ledger entry exists for it."""
        return "not_billed", None

    def observation(self, page: RawPage) -> Observation | None:
        return None
