"""Coverage windows and coverage accounting of the cells (spec §5.5.3, §5.6, §5.3.4, §5.3.5, §9.2 node 10).

**Windows.** A traversal started at `now` requests the window `[now - b, now]`. For JSearch and SerpApi `b` is
the span of the freshness bucket (`freshness_bucket`): the smallest of `BUCKETS` that is at least
`now - last_covered_at + 24 h` (the 24 h cover day-truncated provider dates), `month` for a never-covered
cell. A span longer than the provider allows (720 h) is cut to `month`, and the part the window cannot reach,
`[last_covered_at, now - 720 h]`, is returned to be abandoned at once (`beyond_provider_window`). For Adzuna
`b` is `max_days_old` days (`adzuna_max_days_old`).

`JSearchContinuation` is the `coverage_cells.continuation` document of a JSearch cell (Ruling T13-b): the
cursor its last traversal left, when that cursor was obtained, the window of that traversal, the page the
cursor fetches and the new native ids of the page that handed it out. It exists only for a page the §5.3.1
rule admitted but the traversal did not read (`jsearch.admits_next_page`: page 2 after a page 1 with at
least 7 new ids; its reservation was denied or the run ended first), never for a page beyond the rule (final
review F3). The planner reads it for the cursor reuse of §5.3.1 and checks the rule again, `finalize_cells`
writes it, both through `parse` and `dump`; a document that is absent or malformed (one without
`new_native_ids`, written before the rule, included) parses as `None` (plan from page 1).

**Intervals** (`coverage_intervals`, written by the fetch node in the transaction after a traversal). Every
row is inserted `ON CONFLICT (cell_id, kind, span) DO NOTHING` with the run's id, so a resumed attempt that
records the same traversal again changes nothing; a gap's status moves only through `transition()`
(`open -> closed | abandoned`, `closed_at` set).

- A search cell (`record_search_traversal`): a traversal that reached `END` records its window `W` as
  `covered` and closes the open gaps `W` contains; any other end records the open gap
  `[max(W.lower, coalesce(last_covered_at, W.lower)), W.upper)` with the traversal's cursor and its reason
  (`gap_reason`). The window is the planned spec's, so a resumed attempt records the same rows.
- The Arbeitnow cell (`record_firehose_traversal`, Ruling T10-a): the gaps `arbeitnow.conclude` created or
  changed, on the `created_at` axis, and the covered row of `arbeitnow.covered_span` (a read from page 1 to
  the end of the feed, and nothing else).
- A board (`record_ats_snapshot`): one `ats_snapshots` row per instance and run, with its members. A
  structurally complete snapshot closes the board's open `incomplete_snapshot` gaps; an incomplete one records
  that gap `[start, taken_at)`, starting at the latest of the board's last complete snapshot and its previous
  `incomplete_snapshot` gap (so successive ones never overlap; the run's start for a board's first). The
  planning window of a free cell is never ATS coverage.
- `abandon_old_gaps`: an open gap whose upper bound is at or before `now - max_ad_age_days` is abandoned, and
  `coverage.gap_abandoned` (warn) is raised for its cell.

**Cell statistics** (`finalize_cells`, §9.2 node 10): computed from durable rows only and applied once, in the
run's finalize transaction (Task 23), so that a crash keeps or loses them together with the run's end.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Literal

from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.errors import TransitionConflict
from jobhunter.observability.alerts import raise_alert, resolve_alert
from jobhunter.observability.logging import get_logger
from jobhunter.sources.arbeitnow import FirehoseState, Gap
from jobhunter.sources.ats.common import SnapshotDraft
from jobhunter.sources.base import CoverageWindow, GapReason
from jobhunter.sources.jsearch import admits_next_page
from jobhunter.sources.watchlist_patterns import ATS_TYPES
from jobhunter.types import JSONValue

if TYPE_CHECKING:
    # `traverse` imports the acquisition protocol, which the planner (an importer of this module) needs not.
    from jobhunter.sources.traverse import TraversalResult

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
    handed out at `obtained_at` by a page with `new_native_ids` new ids."""

    cursor: str
    obtained_at: datetime
    window: CoverageWindow
    page_no: int
    new_native_ids: int

    def admitted(self) -> bool:
        """Whether the §5.3.1 page rule admits the page the cursor fetches (`jsearch.admits_next_page`)."""
        return admits_next_page(self.page_no - 1, self.new_native_ids)

    @classmethod
    def parse(cls, doc: JSONValue) -> "JSearchContinuation | None":
        """The continuation `dump` wrote; `None` for anything else (absent, malformed, another kind)."""
        if not isinstance(doc, dict):
            return None
        cursor, page_no, bounds = doc.get("cursor"), doc.get("page_no"), doc.get("window")
        obtained_at, new_ids = _aware(doc.get("obtained_at")), doc.get("new_native_ids")
        if not isinstance(cursor, str) or not cursor or obtained_at is None:
            return None
        if isinstance(page_no, bool) or not isinstance(page_no, int) or page_no < 2:
            return None
        if isinstance(new_ids, bool) or not isinstance(new_ids, int) or new_ids < 0:
            return None
        if not isinstance(bounds, list) or len(bounds) != 2:
            return None
        lower, upper = _aware(bounds[0]), _aware(bounds[1])
        if lower is None or upper is None or lower >= upper:
            return None
        return cls(
            cursor=cursor,
            obtained_at=obtained_at,
            window=(lower, upper),
            page_no=page_no,
            new_native_ids=new_ids,
        )

    def dump(self) -> dict[str, JSONValue]:
        """The `coverage_cells.continuation` document."""
        return {
            "cursor": self.cursor,
            "obtained_at": self.obtained_at.isoformat(),
            "window": [self.window[0].isoformat(), self.window[1].isoformat()],
            "page_no": self.page_no,
            "new_native_ids": self.new_native_ids,
        }


# ---- intervals --------------------------------------------------------------------------------------------

# The skips that end a traversal because the run was ending (§7.16.5); `breaker_open` is the source's failure
# (Ruling T14-b), and every other skip a denial of the allowance (`paced`, `reserve_held`, `exhausted`,
# `window_cap`, `awaiting_window_validation`).
_ENDING_SKIPS: Final = frozenset({"shutdown", "superseded", "run_cancelled"})
GAP_ABANDONED_ALERT: Final = "coverage.gap_abandoned"
TOO_BROAD_ALERT: Final = "coverage.too_broad"
TOO_BROAD_AFTER: Final = 3  # consecutive incomplete runs of a paid cell (§5.6)
PAID_SOURCES: Final = frozenset({"jsearch", "serpapi"})
# The search cells (one per instance, query and place, §5.5.2), whose coverage is their planned window.
SEARCH_SOURCES: Final = frozenset({"jsearch", "serpapi", "adzuna"})
YIELD_ALPHA: Final = 0.3  # the EMA of new postings per request (§5.5.3)

log = get_logger("jobhunter.sources.coverage")


@dataclass(frozen=True)
class CoverageEffect:
    """The intervals one recording wrote: gaps opened, gaps closed (or abandoned), covered rows inserted."""

    opened: int
    closed: int
    covered: int


def gap_reason(result: "TraversalResult") -> GapReason | None:
    """The §5.6 reason of the gap a traversal that did not reach `END` leaves; `None` for one that did."""
    match result.end:
        case "end":
            return None
        case "cursor":
            return "allowance" if result.reason == "allowance" else "page_cap"
        case "unknown":
            return "unknown_continuation"
        case "ambiguous":
            return "ambiguous_attempt"
        case "failed":
            return "source_failed"
        case "skipped" if result.reason in _ENDING_SKIPS:
            return "shutdown"
        case "skipped" if result.reason == "breaker_open":
            return "source_failed"  # the source failed before: its breaker blocks the attempt
        case "skipped":
            return "allowance"


_LAST_COVERED = q("SELECT last_covered_at FROM coverage_cells WHERE id = :c")
_INSERT_COVERED = q(
    "INSERT INTO coverage_intervals (cell_id, kind, span, run_id) "
    "VALUES (:c, 'covered', tstzrange(:lower, :upper, '[)'), :run) "
    "ON CONFLICT (cell_id, kind, span) DO NOTHING RETURNING id"
)
_INSERT_GAP = q(
    "INSERT INTO coverage_intervals (cell_id, kind, span, gap_status, gap_cursor, reason, run_id, closed_at) "
    "VALUES (:c, 'gap', tstzrange(:lower, :upper, '[)'), :status, :cursor, :reason, :run, "
    "CASE WHEN :status = 'open' THEN NULL ELSE now() END) "
    "ON CONFLICT (cell_id, kind, span) DO NOTHING RETURNING id",
    jsonb=["cursor"],
)
_OPEN_GAPS_WITHIN = q(
    "SELECT id FROM coverage_intervals WHERE cell_id = :c AND kind = 'gap' AND gap_status = 'open' "
    "AND tstzrange(:lower, :upper, '[)') @> span ORDER BY id"
)
_GAP_AT = q(
    "SELECT id FROM coverage_intervals WHERE cell_id = :c AND kind = 'gap' AND gap_status = 'open' "
    "AND span = tstzrange(:lower, :upper, '[)')"
)


async def _end_gaps(conn: AsyncConnection, ids: Sequence[int], status: Literal["closed", "abandoned"]) -> int:
    """`open -> status` for each gap; one another writer moved first is left as it is. The count moved."""
    moved = 0
    for gap_id in ids:
        try:
            await transition(conn, "coverage_intervals", gap_id, ["open"], status)
        except TransitionConflict:
            continue
        moved += 1
    return moved


async def _insert_gap(
    conn: AsyncConnection,
    cell_id: int,
    span: CoverageWindow,
    *,
    status: Literal["open", "abandoned"],
    reason: GapReason,
    run_id: int,
    cursor: str | None = None,
) -> bool:
    """Insert a gap row (an abandoned one closed at once); whether it was new."""
    params = {
        "c": cell_id,
        "lower": span[0],
        "upper": span[1],
        "status": status,
        "cursor": None if cursor is None else {"cursor": cursor},
        "reason": reason,
        "run": run_id,
    }
    return (await conn.execute(_INSERT_GAP, params)).first() is not None


async def _insert_covered(conn: AsyncConnection, cell_id: int, span: CoverageWindow, *, run_id: int) -> bool:
    params = {"c": cell_id, "lower": span[0], "upper": span[1], "run": run_id}
    return (await conn.execute(_INSERT_COVERED, params)).first() is not None


async def record_search_gap(
    conn: AsyncConnection,
    cell_id: int,
    *,
    window: CoverageWindow,
    reason: GapReason,
    run_id: int,
    cursor: str | None = None,
) -> int:
    """The open gap `[max(W.lower, coalesce(last_covered_at, W.lower)), W.upper)` of a search cell's window
    `W` (§5.6), with `cursor` and `reason`; nothing when `last_covered_at` already reaches `W.upper`. Returns
    the number of rows inserted (0 when the same gap is recorded already)."""
    lower, upper = window
    last_covered = (await conn.execute(_LAST_COVERED, {"c": cell_id})).scalar_one()
    start = lower if last_covered is None else max(lower, last_covered)
    if start >= upper:
        return 0
    inserted = await _insert_gap(
        conn, cell_id, (start, upper), status="open", reason=reason, run_id=run_id, cursor=cursor
    )
    return int(inserted)


async def record_search_traversal(
    conn: AsyncConnection, cell_id: int, *, window: CoverageWindow, result: "TraversalResult", run_id: int
) -> CoverageEffect:
    """The intervals of a search cell's traversal (module docstring), in the caller's transaction; nothing on
    `coverage_cells` is written here (`finalize_cells` does it)."""
    reason = gap_reason(result)
    if reason is not None:
        opened = await record_search_gap(
            conn, cell_id, window=window, reason=reason, run_id=run_id, cursor=result.cursor_out
        )
        return CoverageEffect(opened=opened, closed=0, covered=0)
    covered = await _insert_covered(conn, cell_id, window, run_id=run_id)
    within = {"c": cell_id, "lower": window[0], "upper": window[1]}
    ids = [int(gap_id) for gap_id in (await conn.execute(_OPEN_GAPS_WITHIN, within)).scalars()]
    closed = await _end_gaps(conn, ids, "closed")
    return CoverageEffect(opened=0, closed=closed, covered=int(covered))


async def record_firehose_traversal(
    conn: AsyncConnection,
    cell_id: int,
    state: FirehoseState,
    gaps: Sequence[Gap],
    *,
    covered: CoverageWindow | None,
    reason: GapReason,
    run_id: int,
) -> CoverageEffect:
    """The Arbeitnow intervals of a traversal (Ruling T10-a), in the caller's transaction.

    `state` is the coverage the traversal started from and `gaps` what `arbeitnow.conclude` created or
    changed: a gap of `state` moves to its new status (`closed` or `abandoned`; one already moved stays),
    every other gap is a new row with its own reason when `conclude` gave it one (the gap above a page's
    fill is `unknown_continuation`, Ruling FR-o; a narrowed gap's remainder keeps its reason), else
    `reason` (an abandoned one closed at once). `covered` is
    `arbeitnow.covered_span` of the reads, the run's `covered` row.
    """
    known = {(gap.lower, gap.upper) for gap in state.gaps}
    opened = closed = 0
    for gap in gaps:
        if (gap.lower, gap.upper) in known:
            if gap.status == "open":
                continue
            found = (
                await conn.execute(_GAP_AT, {"c": cell_id, "lower": gap.lower, "upper": gap.upper})
            ).first()
            if found is not None:
                closed += await _end_gaps(conn, [int(found.id)], gap.status)
            continue
        status: Literal["open", "abandoned"] = "abandoned" if gap.status == "abandoned" else "open"
        inserted = await _insert_gap(
            conn,
            cell_id,
            (gap.lower, gap.upper),
            status=status,
            reason=gap.reason or reason,
            run_id=run_id,
        )
        opened += int(inserted and status == "open")
    inserted_covered = covered is not None and await _insert_covered(conn, cell_id, covered, run_id=run_id)
    return CoverageEffect(opened=opened, closed=closed, covered=int(inserted_covered))


_OPEN_REACHING_BACK = q(
    "SELECT id, upper(span) AS upper, gap_cursor, reason, run_id FROM coverage_intervals WHERE cell_id = :c "
    "AND kind = 'gap' AND gap_status = 'open' AND lower(span) < :boundary ORDER BY id"
)
_INSERT_REMAINDER = q(
    "INSERT INTO coverage_intervals (cell_id, kind, span, gap_status, gap_cursor, reason, run_id) "
    "VALUES (:c, 'gap', tstzrange(:lower, :upper, '[)'), 'open', :cursor, :reason, :run) "
    "ON CONFLICT (cell_id, kind, span) DO NOTHING",
    jsonb=["cursor"],
)


async def abandon_beyond_provider_window(conn: AsyncConnection, cell_id: int, boundary: datetime) -> int:
    """The open gaps of a search cell that reach back before `boundary` (`t - 720 h`, the oldest instant the
    cell's window can reach, §5.6), when the planner abandons the part beyond it: each is abandoned (its
    reason kept), and one that straddles the boundary leaves its newer part `[boundary, upper)` open as a new
    row with the same reason, cursor and run, which the run's `END` can close. The number abandoned."""
    abandoned = 0
    for row in (await conn.execute(_OPEN_REACHING_BACK, {"c": cell_id, "boundary": boundary})).all():
        if not await _end_gaps(conn, [int(row.id)], "abandoned"):
            continue
        abandoned += 1
        if row.upper > boundary:
            params = {
                "c": cell_id,
                "lower": boundary,
                "upper": row.upper,
                "cursor": row.gap_cursor,
                "reason": row.reason,
                "run": row.run_id,
            }
            await conn.execute(_INSERT_REMAINDER, params)
    return abandoned


_SNAPSHOT_OF_RUN = q(
    "SELECT id FROM ats_snapshots WHERE source_instance_id = :i AND run_id = :r ORDER BY id LIMIT 1"
)
_INSERT_SNAPSHOT = q(
    "INSERT INTO ats_snapshots (source_instance_id, run_id, status, incomplete_reason, member_count, "
    "taken_at) VALUES (:i, :r, :status, :reason, :members, :taken_at) RETURNING id"
)
_INSERT_MEMBERS = q(
    "INSERT INTO ats_snapshot_members (snapshot_id, native_id) "
    "SELECT :s, m FROM unnest(CAST(:members AS text[])) AS m ON CONFLICT DO NOTHING"
)
# Where a board's new `incomplete_snapshot` gap starts: after its last complete snapshot and after its
# previous such gap (so successive incomplete snapshots never open overlapping gaps); for a board's first,
# the run's start.
_SNAPSHOT_GAP_START = q(
    "SELECT coalesce(greatest((SELECT s.taken_at FROM source_instances i "
    "JOIN ats_snapshots s ON s.id = i.last_complete_snapshot_id WHERE i.id = :i), "
    "(SELECT max(upper(span)) FROM coverage_intervals WHERE cell_id = :c AND kind = 'gap' "
    "AND reason = 'incomplete_snapshot')), (SELECT coalesce(started_at, queued_at) FROM runs WHERE id = :r))"
)
_OPEN_SNAPSHOT_GAPS = q(
    "SELECT id FROM coverage_intervals WHERE cell_id = :c AND kind = 'gap' AND gap_status = 'open' "
    "AND reason = 'incomplete_snapshot' AND upper(span) <= :taken_at ORDER BY id"
)


async def record_ats_snapshot(
    conn: AsyncConnection,
    cell_id: int,
    instance_id: int,
    draft: SnapshotDraft,
    *,
    run_id: int,
    taken_at: datetime,
) -> int:
    """The board's `ats_snapshots` row of the run, `complete` or `incomplete` (the mass classification is
    Task 19's), with its members, in the caller's transaction; returns its id (module docstring).

    Written once per `(source_instance_id, run_id)`: when the run has a snapshot of the board already (a
    resumed attempt replayed its pages), that row's id is returned and nothing is written.
    """
    existing = (await conn.execute(_SNAPSHOT_OF_RUN, {"i": instance_id, "r": run_id})).scalar()
    if existing is not None:
        return int(existing)
    params = {
        "i": instance_id,
        "r": run_id,
        "status": "complete" if draft.complete else "incomplete",
        "reason": None if draft.complete else draft.incomplete_reason,
        "members": draft.member_count,
        "taken_at": taken_at,
    }
    snapshot_id = int((await conn.execute(_INSERT_SNAPSHOT, params)).scalar_one())
    await conn.execute(_INSERT_MEMBERS, {"s": snapshot_id, "members": sorted(draft.members)})
    if draft.complete:
        ids = (await conn.execute(_OPEN_SNAPSHOT_GAPS, {"c": cell_id, "taken_at": taken_at})).scalars()
        await _end_gaps(conn, [int(gap_id) for gap_id in ids], "closed")
    else:
        start = {"i": instance_id, "c": cell_id, "r": run_id}
        lower = (await conn.execute(_SNAPSHOT_GAP_START, start)).scalar_one()
        if lower < taken_at:
            await _insert_gap(
                conn, cell_id, (lower, taken_at), status="open", reason="incomplete_snapshot", run_id=run_id
            )
    return snapshot_id


_OLD_OPEN_GAPS = q(
    "SELECT id, cell_id FROM coverage_intervals WHERE kind = 'gap' AND gap_status = 'open' "
    "AND upper(span) <= :cutoff ORDER BY cell_id, id"
)


async def abandon_old_gaps(conn: AsyncConnection, *, now: datetime, max_ad_age_days: int) -> int:
    """Abandon every open gap whose upper bound is at or before `now - max_ad_age_days` (§5.6) and raise
    `coverage.gap_abandoned` (warn) once per cell, in the caller's transaction; the number abandoned."""
    cutoff = now - timedelta(days=max_ad_age_days)
    by_cell: defaultdict[int, list[int]] = defaultdict(list)
    for row in await conn.execute(_OLD_OPEN_GAPS, {"cutoff": cutoff}):
        by_cell[int(row.cell_id)].append(int(row.id))
    total = 0
    for cell_id, ids in by_cell.items():
        abandoned = await _end_gaps(conn, ids, "abandoned")
        if abandoned:
            await raise_alert(
                conn,
                code=GAP_ABANDONED_ALERT,
                severity="warn",
                subject=f"cell:{cell_id}",
                message="coverage gaps older than the maximum ad age were abandoned",
                context={"cell_id": cell_id, "gaps": abandoned},
            )
            log.warning("coverage_gaps_abandoned", cell_id=cell_id, gaps=abandoned)
        total += abandoned
    return total


# ---- cell statistics (§9.2 node 10) -----------------------------------------------------------------------


@dataclass(frozen=True)
class CellStats:
    """A cell's part in a run: its dispatched requests, the postings first seen through them, whether its
    traversal's pagination reached `END`, and whether the run recorded the cell covered (a `covered` interval,
    or a structurally complete snapshot of a board)."""

    requests: int
    new_postings: int
    reached_end: bool
    covered: bool


_LOCK_CELLS = q(
    "SELECT id FROM coverage_cells WHERE id IN (SELECT cell_id FROM fetch_requests WHERE run_id = :run) "
    "ORDER BY id FOR UPDATE"
)
# A cell's requests of the run; the dispatched ones (`dispatched` and after) are the requests that count.
_RUN_CELLS = q(
    "SELECT f.cell_id, i.source_id, c.source_instance_id, c.last_covered_at, c.yield_ema, "
    "c.zero_yield_streak, c.consecutive_incomplete, coalesce(bool_or(qr.exploration), false) AS exploration, "
    "count(*) FILTER (WHERE f.status IN ('dispatched','succeeded','failed','ambiguous')) AS requests, "
    "min(f.created_at) FILTER (WHERE f.status IN ('dispatched','succeeded','failed','ambiguous')) "
    "AS attempted_at, max(f.finished_at) FILTER (WHERE f.status = 'succeeded') AS succeeded_at, "
    "coalesce(bool_or(f.status = 'succeeded' AND f.operation = 'list' AND f.continuation = 'end'), false) "
    "AS reached_end, coalesce(bool_or(f.operation = 'list' "
    "AND f.status IN ('dispatched','succeeded','failed','ambiguous')), false) AS traversed "
    "FROM fetch_requests f JOIN coverage_cells c ON c.id = f.cell_id "
    "JOIN source_instances i ON i.id = c.source_instance_id LEFT JOIN queries qr ON qr.id = c.query_id "
    "WHERE f.run_id = :run GROUP BY f.cell_id, i.source_id, c.source_instance_id, c.last_covered_at, "
    "c.yield_ema, c.zero_yield_streak, c.consecutive_incomplete ORDER BY f.cell_id"
)
# `new_postings` of every succeeded request of the run: the postings whose earliest observation came from one
# of its raw items (a re-sighting of an older posting is not new).
_NEW_POSTINGS = q(
    "WITH run_items AS (SELECT i.id AS raw_item_id, r.fetch_request_id FROM raw_items i "
    "JOIN raw_responses r ON r.id = i.raw_response_id JOIN fetch_requests f ON f.id = r.fetch_request_id "
    "WHERE f.run_id = :run), "
    "firsts AS (SELECT DISTINCT ON (o.posting_id) o.posting_id, o.raw_item_id FROM posting_observations o "
    "WHERE o.posting_id IN (SELECT s.posting_id FROM posting_observations s "
    "JOIN run_items ri ON ri.raw_item_id = s.raw_item_id) ORDER BY o.posting_id, o.observed_at, o.id), "
    "counted AS (SELECT ri.fetch_request_id, count(*) AS n FROM firsts "
    "JOIN run_items ri ON ri.raw_item_id = firsts.raw_item_id GROUP BY ri.fetch_request_id) "
    "UPDATE fetch_requests f SET new_postings = coalesce((SELECT counted.n FROM counted "
    "WHERE counted.fetch_request_id = f.id), 0) WHERE f.run_id = :run AND f.status = 'succeeded' "
    "RETURNING f.cell_id, f.new_postings"
)
_COVERED_ROWS = q(
    "SELECT cell_id, lower(span) AS lower, upper(span) AS upper FROM coverage_intervals "
    "WHERE run_id = :run AND kind = 'covered' ORDER BY cell_id, upper(span)"
)
_OPENED_GAPS = q(
    "SELECT DISTINCT cell_id FROM coverage_intervals WHERE run_id = :run AND kind = 'gap' "
    "AND reason <> 'beyond_provider_window'"
)
_RUN_SNAPSHOTS = q(
    "SELECT source_instance_id, id, status, taken_at FROM ats_snapshots WHERE run_id = :run ORDER BY id"
)
# The window of the run's last list page of each cell that ended its traversal (`END`).
_END_PAGES = q(
    "SELECT DISTINCT ON (cell_id) cell_id, spec FROM fetch_requests WHERE run_id = :run "
    "AND status = 'succeeded' AND operation = 'list' AND continuation = 'end' ORDER BY cell_id, id DESC"
)
# A committed `covered` row of the cell with exactly this span: an earlier run recorded the same window, so
# the run's own record of it inserted nothing (`ON CONFLICT DO NOTHING`).
_COVERED_SPAN = q(
    "SELECT EXISTS (SELECT 1 FROM coverage_intervals WHERE cell_id = :c AND kind = 'covered' "
    "AND span = tstzrange(:lower, :upper, '[)'))"
)
_OPEN_BETWEEN = q(
    "SELECT EXISTS (SELECT 1 FROM coverage_intervals WHERE cell_id = :c AND kind = 'gap' "
    "AND gap_status = 'open' AND span && tstzrange(:lower, :upper, '[)'))"
)
# The last succeeded list page of each cell of the run, with the time its response (and cursor) arrived.
_LAST_PAGES = q(
    "SELECT DISTINCT ON (f.cell_id) f.cell_id, f.page_no, f.next_cursor, f.new_native_ids, f.spec, "
    "r.fetched_at FROM fetch_requests f JOIN raw_responses r ON r.id = f.raw_response_id "
    "WHERE f.run_id = :run AND f.status = 'succeeded' AND f.operation = 'list' "
    "ORDER BY f.cell_id, f.page_no DESC, f.id DESC"
)
_UPDATE_CELL = q(
    "UPDATE coverage_cells SET last_attempted_at = coalesce(:attempted_at, last_attempted_at), "
    "last_success_at = coalesce(:succeeded_at, last_success_at), last_covered_at = :covered_at, "
    "yield_ema = :ema, zero_yield_streak = :streak, consecutive_incomplete = :incomplete, "
    "last_exploration_at = coalesce(:explored_at, last_exploration_at) WHERE id = :c"
)
_SET_CONTINUATION = q("UPDATE coverage_cells SET continuation = :doc WHERE id = :c", jsonb=["doc"])
_LAST_COMPLETE = q("UPDATE source_instances SET last_complete_snapshot_id = :s WHERE id = :i")


def _window_of(spec: JSONValue) -> CoverageWindow | None:
    bounds = spec.get("window") if isinstance(spec, dict) else None
    if not isinstance(bounds, list) or len(bounds) != 2:
        return None
    lower, upper = _aware(bounds[0]), _aware(bounds[1])
    return None if lower is None or upper is None else (lower, upper)


def _continuation(page: Row[Any]) -> JSONValue:
    """The continuation document of a JSearch cell whose last succeeded page of the run is `page`: its
    cursor, the time it arrived, the traversal's window and the page's new ids; `None` when that page ended
    the traversal or the §5.3.1 rule admits no next page after it (fewer than 7 new ids, or page 2 itself)."""
    cursor = page.next_cursor
    token = cursor.get("cursor") if isinstance(cursor, dict) else None
    window = _window_of(page.spec)
    if not isinstance(token, str) or not token or window is None or page.new_native_ids is None:
        return None
    continuation = JSearchContinuation(
        cursor=token,
        obtained_at=page.fetched_at,
        window=window,
        page_no=int(page.page_no) + 1,
        new_native_ids=int(page.new_native_ids),
    )
    return continuation.dump() if continuation.admitted() else None


def _ema(previous: float, requests: int, new_postings: int) -> float:
    return YIELD_ALPHA * (new_postings / requests) + (1 - YIELD_ALPHA) * previous


async def finalize_cells(conn: AsyncConnection, run_id: int, *, now: datetime) -> dict[int, CellStats]:
    """The `coverage_cells` updates of a run (§9.2 node 10), from durable rows only, in the caller's (the
    finalize) transaction; the statistics by cell. Applied exactly once per run: the caller ends the run
    in the same transaction, so no later attempt applies them again.

    For every cell with a fetch request of the run: `last_attempted_at` = the first dispatched request's
    `created_at` (a cell whose requests were all skipped was not attempted, and keeps its staleness);
    `last_success_at` when one succeeded; `new_postings` per request (`fetch_requests.new_postings`) and
    per cell; `yield_ema` (alpha 0.3 of new postings per dispatched request) and `zero_yield_streak`,
    unchanged without a dispatched request. A search or firehose cell is covered when a list page of the
    run ended its traversal (`END`) or the run recorded a `covered` row, a board when its snapshot of the
    run is structurally complete. `last_covered_at` advances only from a committed `covered` row (Task 23,
    carry 5): to the upper bound of the run's `covered` row, else of a search cell's `END` page's window when
    a `covered` row of exactly that span exists (an earlier run recorded the same window); an `END` page
    without one (its coverage commit never happened) moves nothing. It advances when it is `NULL`, when the
    window starts at or before it, or when no open gap lies between them (§5.6), and to a structurally
    complete snapshot's `taken_at` for a board, whose `complete` snapshot also becomes the instance's
    `last_complete_snapshot_id`. `consecutive_incomplete` is reset by a covered run and incremented by a
    run that traversed the cell (a dispatched list request) without covering it, whether or not it wrote a
    new gap row, or that recorded a new gap of it; `last_exploration_at` is set for an attempted
    exploration cell; a JSearch cell's `continuation` comes from its last succeeded page
    (`JSearchContinuation`), only for a next page the §5.3.1 rule admitted but the run did not read
    (cleared otherwise: that page ended the traversal, had fewer than 7 new ids, or was page 2).
    `coverage.too_broad` (info) is raised for a paid cell at `consecutive_incomplete >= 3` and resolved
    when the cell is covered. `now` is the finalize time, for the log.
    """
    await conn.execute(_LOCK_CELLS, {"run": run_id})
    cells = (await conn.execute(_RUN_CELLS, {"run": run_id})).all()
    new_postings: defaultdict[int, int] = defaultdict(int)
    for row in await conn.execute(_NEW_POSTINGS, {"run": run_id}):
        new_postings[int(row.cell_id)] += int(row.new_postings)
    covered_rows: dict[int, CoverageWindow] = {
        int(row.cell_id): (row.lower, row.upper) for row in await conn.execute(_COVERED_ROWS, {"run": run_id})
    }
    end_windows = {
        int(row.cell_id): window
        for row in await conn.execute(_END_PAGES, {"run": run_id})
        if (window := _window_of(row.spec)) is not None
    }
    opened = {int(cell_id) for cell_id in (await conn.execute(_OPENED_GAPS, {"run": run_id})).scalars()}
    snapshots = {
        int(row.source_instance_id): row for row in await conn.execute(_RUN_SNAPSHOTS, {"run": run_id})
    }
    last_pages = {int(row.cell_id): row for row in await conn.execute(_LAST_PAGES, {"run": run_id})}
    stats: dict[int, CellStats] = {}
    for cell in cells:
        cell_id, requests = int(cell.cell_id), int(cell.requests)
        last_covered: datetime | None = cell.last_covered_at
        board = cell.source_id in ATS_TYPES
        # a search or firehose cell is covered by its END (its window, or the run's covered row: a span an
        # earlier run recorded already is no row of this run), a board by a structurally complete snapshot
        covered = (not board and bool(cell.reached_end)) or cell_id in covered_rows
        windowed = covered and cell.source_id in SEARCH_SOURCES  # a firehose spec's window is no coverage
        # `last_covered_at` moves only by a committed `covered` row (carry 5): an END page whose coverage
        # commit never happened (a `fetch_cell` error the run survived) proves the pagination, not the span
        span = covered_rows.get(cell_id)
        end_window = end_windows.get(cell_id) if windowed else None
        if span is None and end_window is not None:
            same = {"c": cell_id, "lower": end_window[0], "upper": end_window[1]}
            if (await conn.execute(_COVERED_SPAN, same)).scalar_one():
                span = end_window
        if span is not None:
            contiguous = last_covered is None or span[0] <= last_covered
            if not contiguous:
                between = {"c": cell_id, "lower": last_covered, "upper": span[0]}
                contiguous = not (await conn.execute(_OPEN_BETWEEN, between)).scalar_one()
            if contiguous:
                last_covered = span[1] if last_covered is None else max(last_covered, span[1])
        snapshot = snapshots.get(int(cell.source_instance_id))
        if snapshot is not None and snapshot.status in ("complete", "mass_disappearance"):
            covered = True
            taken_at: datetime = snapshot.taken_at
            last_covered = taken_at if last_covered is None else max(last_covered, taken_at)
            if snapshot.status == "complete":
                await conn.execute(_LAST_COMPLETE, {"s": int(snapshot.id), "i": int(cell.source_instance_id)})
        incomplete = int(cell.consecutive_incomplete)
        if covered:
            incomplete = 0
        elif cell.traversed or cell_id in opened:  # its traversal of the cell did not reach END
            incomplete += 1
        ema, streak = float(cell.yield_ema), int(cell.zero_yield_streak)
        new = new_postings.get(cell_id, 0)
        if requests > 0:
            ema = _ema(ema, requests, new)
            streak = 0 if new > 0 else streak + 1
        await conn.execute(
            _UPDATE_CELL,
            {
                "c": cell_id,
                "attempted_at": cell.attempted_at,
                "succeeded_at": cell.succeeded_at,
                "covered_at": last_covered,
                "ema": ema,
                "streak": streak,
                "incomplete": incomplete,
                "explored_at": cell.attempted_at if cell.exploration else None,
            },
        )
        if cell.source_id == "jsearch" and (page := last_pages.get(cell_id)) is not None:
            await conn.execute(_SET_CONTINUATION, {"c": cell_id, "doc": _continuation(page)})
        if cell.source_id in PAID_SOURCES:
            await _too_broad(conn, cell_id, incomplete=incomplete, covered=covered)
        stats[cell_id] = CellStats(requests, new, bool(cell.reached_end), covered)
    log.info("coverage_cells_finalized", run_id=run_id, cells=len(stats), at=now.isoformat())
    return stats


async def _too_broad(conn: AsyncConnection, cell_id: int, *, incomplete: int, covered: bool) -> None:
    subject = f"cell:{cell_id}"
    if covered:
        await resolve_alert(conn, code=TOO_BROAD_ALERT, subject=subject)
    elif incomplete >= TOO_BROAD_AFTER:
        await raise_alert(
            conn,
            code=TOO_BROAD_ALERT,
            severity="info",
            subject=subject,
            message="a paid cell was left incomplete in 3 runs in a row: its query may be too broad",
            context={"cell_id": cell_id, "consecutive_incomplete": incomplete},
        )
