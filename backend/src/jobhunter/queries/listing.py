"""The query table of Queries & sources (spec §14.2, §15.7; plan M4 Task 13, D1, M2 Carry-forward 2, M1
Carry-forward 3's per-query gap counts): `GET /api/queries` and `GET /api/queries/stats`.

`query_list(conn, *, now)` and `cell_stats(conn, *, now)` read on the caller's connection, which the routes
open as one read snapshot (`db.snapshot.read_snapshot`): plain reads, no lock, and the same few statements
however many queries and cells there are (the active settings, the quota windows, the cells, the queries).

**The cells** (`cell_stats`): every coverage cell of a live query (`retired_at IS NULL`), ordered by
`(query_id, source_id, instance_label, location_key)` (unique: one cell per instance, query and place), with
- its open gaps: the `coverage_intervals` rows `kind = 'gap' AND gap_status = 'open'` (the predicate of the
  partial index `coverage_open_gaps`), counted per cell in one `GROUP BY`;
- `too_broad`: `coverage.too_broad`, the one predicate the `coverage.too_broad` alert uses (a paid cell left
  incomplete by 3 runs in a row);
- `cadence_days`: the JSearch cadence below for a JSearch cell, null for any other.
A retired query's cells are not listed, nor the cells without a query (Arbeitnow's, the boards'): the
watchlist list shows a board's coverage. `instance_label` is the instance's key (a query's cells are the
search sources', whose one instance is `default`).

**The JSearch cadence** is the settings dry-run's `projections.jsearch.refresh_cadence_days` for the active
settings: `quota.dryrun.refresh_cadence_days` on JSearch's window at `now` (`quota.dryrun.live_windows`, the
stored window rolled forward in memory, or the one its first reservation would bootstrap) for the active
settings' paid cells (`settings.dryrun.paid_cells`); null when nothing is seeded.

**The queries** (`query_list`): the live queries by `id`, each with
- `enabled`: its flag in the active settings' `queries[]`; `false` for a live query they do not list;
- the aggregates of its cells, as `cell_stats` lists them: `cells` (how many), `last_covered_at` (the oldest
  frontier of its paid cells, JSearch's and SerpApi's; null when one of them was never covered, since that
  one is older than any covered one, and null without a paid cell), `open_gaps` (their sum), `yield_ema`
  (the mean over its cells; the column is never null, so null only without a cell), `too_broad` (any of its
  cells) and `refresh_cadence_days` (the JSearch cadence when it has a JSearch cell, else null).
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from statistics import fmean
from typing import Any, Final

from pydantic import BaseModel, Field
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.api.models import UtcDatetime
from jobhunter.db.sql import q
from jobhunter.queries.authoring import Lang, Origin
from jobhunter.quota.dryrun import live_windows, refresh_cadence_days
from jobhunter.quota.windows import Provider
from jobhunter.run_settings import RunSettings, load_active_run_settings
from jobhunter.settings.dryrun import paid_cells
from jobhunter.sources.coverage import PAID_SOURCES, TOO_BROAD_AFTER, too_broad

_JSEARCH: Final[Provider] = "jsearch"

_CELLS: Final = q(
    "/* queries.cells */ SELECT c.id AS cell_id, c.query_id, i.source_id, i.instance_key AS instance_label, "
    "c.location_key, c.last_covered_at, c.last_success_at, c.yield_ema, c.zero_yield_streak, "
    "c.consecutive_incomplete, coalesce(g.open_gaps, 0) AS open_gaps "
    "FROM coverage_cells c JOIN queries qr ON qr.id = c.query_id "
    "JOIN source_instances i ON i.id = c.source_instance_id "
    "LEFT JOIN (SELECT cell_id, count(*) AS open_gaps FROM coverage_intervals "
    "WHERE kind = 'gap' AND gap_status = 'open' GROUP BY cell_id) g ON g.cell_id = c.id "
    "WHERE qr.retired_at IS NULL ORDER BY c.query_id, i.source_id, i.instance_key, c.location_key"
)
_QUERIES: Final = q(
    "/* queries.live */ SELECT id, text, lang, origin, exploration, proposal_id, created_at FROM queries "
    "WHERE retired_at IS NULL ORDER BY id"
)

_CADENCE: Final = (
    "How many days JSearch takes to refresh each paid cell under the active settings, as the settings "
    "preview projects it"
)


class CellStatsOut(BaseModel):
    """A coverage cell of a live query (one source instance, one query, one place) and its statistics."""

    cell_id: int
    query_id: int
    source_id: str
    instance_label: str = Field(description="The source instance's key")
    location_key: str | None = Field(description="The scope location's key, or `remote-de`")
    last_covered_at: UtcDatetime | None = Field(
        description="Its contiguous coverage frontier; null when it was never covered"
    )
    last_success_at: UtcDatetime | None = Field(description="Its last successful request")
    yield_ema: float = Field(description="New postings per request, smoothed")
    zero_yield_streak: int = Field(description="Consecutive runs in which its requests found no new posting")
    consecutive_incomplete: int = Field(description="Consecutive runs that left it incomplete")
    too_broad: bool = Field(
        description=f"A paid cell (JSearch, SerpApi) left incomplete in {TOO_BROAD_AFTER} runs in a row"
    )
    open_gaps: int = Field(description="Its open coverage gaps")
    cadence_days: float | None = Field(description=f"{_CADENCE}; null for a cell of another source")


class QueryListOut(BaseModel):
    """A live query, whether the active settings enable it, and the coverage of its cells."""

    id: int
    text: str
    lang: Lang
    origin: Origin
    exploration: bool
    proposal_id: int | None
    created_at: UtcDatetime
    enabled: bool = Field(description="Its flag in the active settings; false for a query they do not list")
    cells: int = Field(description="Its coverage cells")
    last_covered_at: UtcDatetime | None = Field(
        description="The oldest coverage frontier of its paid cells (JSearch, SerpApi); null when one of "
        "them was never covered, or without a paid cell"
    )
    open_gaps: int = Field(description="The open coverage gaps of its cells")
    yield_ema: float | None = Field(description="The mean yield of its cells; null without a cell")
    too_broad: bool = Field(description="One of its cells is too broad")
    refresh_cadence_days: float | None = Field(description=f"{_CADENCE}; null without a JSearch cell")


async def _jsearch_cadence(conn: AsyncConnection, run: RunSettings | None, now: datetime) -> float | None:
    """The dry-run's JSearch cadence for the active settings `run` at `now` (module docstring)."""
    if run is None:
        return None
    window = (await live_windows(conn, run, now))[_JSEARCH]
    return None if window is None else refresh_cadence_days(run, window, paid_cells(run))


def _cell(row: Row[Any], cadence: float | None) -> CellStatsOut:
    return CellStatsOut(
        cell_id=row.cell_id,
        query_id=row.query_id,
        source_id=row.source_id,
        instance_label=row.instance_label,
        location_key=row.location_key,
        last_covered_at=row.last_covered_at,
        last_success_at=row.last_success_at,
        yield_ema=row.yield_ema,
        zero_yield_streak=row.zero_yield_streak,
        consecutive_incomplete=row.consecutive_incomplete,
        too_broad=too_broad(row.source_id, row.consecutive_incomplete),
        open_gaps=row.open_gaps,
        cadence_days=cadence if row.source_id == _JSEARCH else None,
    )


async def _cells(conn: AsyncConnection, cadence: float | None) -> list[CellStatsOut]:
    return [_cell(row, cadence) for row in await conn.execute(_CELLS)]


async def cell_stats(conn: AsyncConnection, *, now: datetime) -> list[CellStatsOut]:
    """Every cell of a live query with its statistics, its open gaps and its cadence (module docstring)."""
    cadence = await _jsearch_cadence(conn, await load_active_run_settings(conn), now)
    return await _cells(conn, cadence)


def _oldest_paid_coverage(cells: Sequence[CellStatsOut]) -> datetime | None:
    """The oldest `last_covered_at` of the paid cells; `None` when one was never covered, or without one."""
    paid = [cell.last_covered_at for cell in cells if cell.source_id in PAID_SOURCES]
    covered = [at for at in paid if at is not None]
    return min(covered) if paid and len(covered) == len(paid) else None


def _query(
    row: Row[Any], *, enabled: bool, cells: Sequence[CellStatsOut], cadence: float | None
) -> QueryListOut:
    return QueryListOut(
        id=row.id,
        text=row.text,
        lang=row.lang,
        origin=row.origin,
        exploration=row.exploration,
        proposal_id=row.proposal_id,
        created_at=row.created_at,
        enabled=enabled,
        cells=len(cells),
        last_covered_at=_oldest_paid_coverage(cells),
        open_gaps=sum(cell.open_gaps for cell in cells),
        yield_ema=fmean(cell.yield_ema for cell in cells) if cells else None,
        too_broad=any(cell.too_broad for cell in cells),
        refresh_cadence_days=cadence if any(cell.source_id == _JSEARCH for cell in cells) else None,
    )


async def query_list(conn: AsyncConnection, *, now: datetime) -> list[QueryListOut]:
    """The live queries by id, each with its settings flag and the aggregates of its cells (module
    docstring)."""
    run = await load_active_run_settings(conn)
    cadence = await _jsearch_cadence(conn, run, now)
    by_query: defaultdict[int, list[CellStatsOut]] = defaultdict(list)
    for cell in await _cells(conn, cadence):
        by_query[cell.query_id].append(cell)
    flags: Mapping[int, bool] = {} if run is None else run.queries
    return [
        _query(row, enabled=flags.get(row.id, False), cells=by_query[row.id], cadence=cadence)
        for row in await conn.execute(_QUERIES)
    ]
