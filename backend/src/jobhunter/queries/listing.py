"""The query table of Queries & sources (spec §14.2, §15.7; plan M4 Task 13, D1, M2 Carry-forward 2, M1
Carry-forward 3's per-query gap counts; Rulings T13-a, T13-b, T13-d and T13-e): `GET /api/queries` and `GET
/api/queries/stats`.

`query_list(conn, *, now)` and `cell_stats(conn, *, now)` read on the caller's connection, which the routes
open as one read snapshot (`db.snapshot.read_snapshot`): plain reads, no lock, and the same few statements
however many queries and cells there are (the active settings, the quota windows, the queries, the source
instances, the cells).

**The planned search** (Rulings T13-b, T13-d and T13-e): the run's planner selection under the active settings
(`quota.cells.planned_cells` and its search predicate `PLANNED_SEARCH_CELL_SQL`), the planned search instances
(the enabled search sources') at the scope's location keys, applied to every live query, enabled or not, so
that a disabled or unlisted query is judged on the scope as it is now (T13-d). A query's **planned cells** are
one per planned instance and location key (T13-e): an enabled query's exist, since its save made them, but a
disabled query's need not (a location added to the scope, or a source switched on, after it was disabled),
and the rule does not depend on that. A cell whose location left the scope or whose source was switched off
is neither counted nor listed. Nothing is planned when nothing is seeded. The header's open-gap total
(`observability.status`) counts every open gap, so it also counts the gaps of cells no longer planned, of
retired queries and of the cells without a query; both route descriptions say so.

**The cells** (`cell_stats`): every existing planned cell of a live query (`retired_at IS NULL`), ordered by
`(query_id, source_id, instance_label, location_key)` (unique: one cell per instance, query and place); a
planned cell that does not exist yet has no id and is not listed. Each with
- its open gaps: the `coverage_intervals` rows `kind = 'gap' AND gap_status = 'open'` (the predicate of the
  partial index `coverage_open_gaps`), counted per cell in one `GROUP BY`;
- `too_broad`: `coverage.too_broad`, the one predicate the `coverage.too_broad` alert uses (a paid cell left
  incomplete by 3 runs in a row);
- `cadence_days`: the JSearch cadence below for a JSearch cell, null for any other.
The cells without a query (Arbeitnow's, the boards') are not listed: the watchlist list shows a board's
coverage. `instance_label` is the instance's key (a query's cells are the search sources', whose one instance
is `default`); `location_key` is never null (the planned predicate requires a key of the scope).

**The JSearch cadence** is the settings dry-run's `projections.jsearch.refresh_cadence_days` for the active
settings: `quota.dryrun.refresh_cadence_days` on JSearch's window at `now` (`quota.dryrun.live_windows`, the
stored window rolled forward in memory, or the one its first reservation would bootstrap) for the active
settings' paid cells (`settings.dryrun.paid_cells`); null when nothing is seeded.

**The queries** (`query_list`): the live queries by `id`, each with
- `enabled`: its flag in the active settings' `queries[]`; `false` for a live query they do not list;
- `cells`: its planned cells, existing or not: the planned instances times the location keys (T13-e);
- `last_covered_at` (Rulings T13-a and T13-e), judged per location key of the scope: a location is covered as
  of the newest frontier among the query's paid cells there (its JSearch cell and its SerpApi fallback cell,
  which serves the location only when JSearch cannot), and the query as of the oldest location; null when a
  location has no covered paid cell (one whose cells do not exist yet included), or when no paid source is
  planned;
- over its existing planned cells: `open_gaps` (their sum) and `too_broad` (any of them);
- `yield_ema` (Ruling T13-e, replacing resolution (d)): the mean over its existing planned cells that a run
  has searched (`last_attempted_at` or `last_success_at` set); null when none has. A cell never searched
  keeps the column's starting 0.5, which is no measurement: the SerpApi fallback cells mostly;
- `refresh_cadence_days`: the JSearch cadence when JSearch is planned at a location key, else null.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from statistics import fmean
from typing import Any, Final

from pydantic import BaseModel, Field
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.api.models import UtcDatetime
from jobhunter.db.sql import q
from jobhunter.queries.authoring import Lang, Origin
from jobhunter.quota.cells import PLANNED_SEARCH_CELL_SQL, planned_cells
from jobhunter.quota.dryrun import live_windows, refresh_cadence_days
from jobhunter.quota.windows import Provider
from jobhunter.run_settings import RunSettings, load_active_run_settings
from jobhunter.settings.dryrun import paid_cells
from jobhunter.sources.coverage import PAID_SOURCES, TOO_BROAD_AFTER, too_broad
from jobhunter.sources.instances import list_instances

_JSEARCH: Final[Provider] = "jsearch"

_CELLS: Final = q(
    "/* queries.cells */ SELECT c.id AS cell_id, c.query_id, i.source_id, i.instance_key AS instance_label, "  # noqa: S608
    "c.location_key, c.last_covered_at, c.last_success_at, c.yield_ema, c.zero_yield_streak, "
    "c.consecutive_incomplete, coalesce(g.open_gaps, 0) AS open_gaps, "
    "(c.last_attempted_at IS NOT NULL OR c.last_success_at IS NOT NULL) AS searched "
    "FROM coverage_cells c JOIN queries qr ON qr.id = c.query_id "
    "JOIN source_instances i ON i.id = c.source_instance_id "
    "LEFT JOIN (SELECT cell_id, count(*) AS open_gaps FROM coverage_intervals "
    "WHERE kind = 'gap' AND gap_status = 'open' GROUP BY cell_id) g ON g.cell_id = c.id "
    f"WHERE qr.retired_at IS NULL AND {PLANNED_SEARCH_CELL_SQL} "
    "ORDER BY c.query_id, i.source_id, i.instance_key, c.location_key"
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
    """An existing planned coverage cell of a live query (one source instance, one query, one place) and its
    statistics."""

    cell_id: int
    query_id: int
    source_id: str
    instance_label: str = Field(description="The source instance's key")
    location_key: str = Field(description="The scope location's key, or `remote-de`")
    last_covered_at: UtcDatetime | None = Field(
        description="Its contiguous coverage frontier; null when it was never covered"
    )
    last_success_at: UtcDatetime | None = Field(description="Its last successful request")
    yield_ema: float = Field(
        description="New postings per request, smoothed; a cell never searched shows its starting value"
    )
    zero_yield_streak: int = Field(description="Consecutive runs in which its requests found no new posting")
    consecutive_incomplete: int = Field(description="Consecutive runs that left it incomplete")
    too_broad: bool = Field(
        description=f"A paid cell (JSearch, SerpApi) left incomplete in {TOO_BROAD_AFTER} runs in a row"
    )
    open_gaps: int = Field(description="Its open coverage gaps")
    cadence_days: float | None = Field(description=f"{_CADENCE}; null for a cell of another source")


class QueryListOut(BaseModel):
    """A live query, whether the active settings enable it, and the coverage of its planned cells: one per
    enabled source and scope location, which a run searches while it is enabled, judged on the scope as it is
    now whether the query is enabled or not."""

    id: int
    text: str
    lang: Lang
    origin: Origin
    exploration: bool
    proposal_id: int | None
    created_at: UtcDatetime
    enabled: bool = Field(description="Its flag in the active settings; false for a query they do not list")
    cells: int = Field(
        description="Its planned coverage cells, one per enabled source and scope location, counted whether "
        "a run has created them yet or not"
    )
    last_covered_at: UtcDatetime | None = Field(
        description="The oldest coverage of the scope's locations for it, a location being covered as of the "
        "newer frontier of its JSearch cell and its SerpApi fallback cell; null when a location is covered "
        "by neither (also when those cells do not exist yet), or when neither JSearch nor SerpApi is enabled"
    )
    open_gaps: int = Field(description="The open coverage gaps of its planned cells")
    yield_ema: float | None = Field(
        description="The mean yield of its planned cells that a run has searched; null when none has been"
    )
    too_broad: bool = Field(description="One of its planned cells is too broad")
    refresh_cadence_days: float | None = Field(
        description=f"{_CADENCE}; null when JSearch is not enabled or the scope has no location"
    )


@dataclass(frozen=True)
class _Plan:
    """The planned search of the active settings: the scope's location keys and the planned search
    instances, how many, and whether a paid one (JSearch, SerpApi) and JSearch are among them."""

    keys: tuple[str, ...]
    instances: int
    paid: bool
    jsearch: bool


@dataclass(frozen=True)
class _Cell:
    """An existing planned cell as listed, and whether a run has searched it (attempted or succeeded)."""

    stats: CellStatsOut
    searched: bool


_NOTHING_PLANNED: Final = _Plan(keys=(), instances=0, paid=False, jsearch=False)


async def _jsearch_cadence(conn: AsyncConnection, run: RunSettings | None, now: datetime) -> float | None:
    """The dry-run's JSearch cadence for the active settings `run` at `now` (module docstring)."""
    if run is None:
        return None
    window = (await live_windows(conn, run, now))[_JSEARCH]
    return None if window is None else refresh_cadence_days(run, window, paid_cells(run))


def _cell(row: Row[Any], cadence: float | None) -> _Cell:
    stats = CellStatsOut(
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
    return _Cell(stats=stats, searched=bool(row.searched))


async def _planned(
    conn: AsyncConnection, run: RunSettings | None, live: Sequence[int], cadence: float | None
) -> tuple[_Plan, list[_Cell]]:
    """The planned search of the active settings `run` and the existing planned cells of the `live` queries,
    each query enabled or not (nothing planned when unseeded; module docstring)."""
    if run is None:
        return _NOTHING_PLANNED, []
    instances = await list_instances(conn)
    planned = planned_cells(run, instances)
    searched = set(planned.search)
    sources = {instance.source_id for instance in instances if instance.id in searched}
    plan = _Plan(
        keys=tuple(planned.keys),
        instances=len(planned.search),
        paid=bool(sources & PAID_SOURCES),
        jsearch=_JSEARCH in sources,
    )
    if not live:
        return plan, []
    params = {**planned.search_params(), "queries": list(live)}  # every live query, not only the enabled
    return plan, [_cell(row, cadence) for row in await conn.execute(_CELLS, params)]


async def _live(conn: AsyncConnection) -> list[Row[Any]]:
    return list((await conn.execute(_QUERIES)).all())


async def cell_stats(conn: AsyncConnection, *, now: datetime) -> list[CellStatsOut]:
    """Every existing planned cell of a live query with its statistics, its open gaps and its cadence
    (module docstring)."""
    run = await load_active_run_settings(conn)
    live = [int(row.id) for row in await _live(conn)]
    _plan, cells = await _planned(conn, run, live, await _jsearch_cadence(conn, run, now))
    return [cell.stats for cell in cells]


def _location_coverage(plan: _Plan, cells: Sequence[_Cell]) -> datetime | None:
    """The query's coverage over the scope's locations: each as of the newest frontier of the query's paid
    cells there, the query as of the oldest location; `None` when a location has no covered paid cell (its
    cells missing included), or when no paid source is planned (module docstring)."""
    if not plan.paid or not plan.keys:
        return None
    frontiers: defaultdict[str, list[datetime]] = defaultdict(list)
    for cell in cells:
        if cell.stats.source_id in PAID_SOURCES and cell.stats.last_covered_at is not None:
            frontiers[cell.stats.location_key].append(cell.stats.last_covered_at)
    if any(not frontiers[key] for key in plan.keys):
        return None
    return min(max(frontiers[key]) for key in plan.keys)


def _query(
    row: Row[Any], *, enabled: bool, plan: _Plan, cells: Sequence[_Cell], cadence: float | None
) -> QueryListOut:
    searched = [cell.stats.yield_ema for cell in cells if cell.searched]
    return QueryListOut(
        id=row.id,
        text=row.text,
        lang=row.lang,
        origin=row.origin,
        exploration=row.exploration,
        proposal_id=row.proposal_id,
        created_at=row.created_at,
        enabled=enabled,
        cells=plan.instances * len(plan.keys),
        last_covered_at=_location_coverage(plan, cells),
        open_gaps=sum(cell.stats.open_gaps for cell in cells),
        yield_ema=fmean(searched) if searched else None,
        too_broad=any(cell.stats.too_broad for cell in cells),
        refresh_cadence_days=cadence if plan.jsearch and plan.keys else None,
    )


async def query_list(conn: AsyncConnection, *, now: datetime) -> list[QueryListOut]:
    """The live queries by id, each with its settings flag and the aggregates of its planned cells (module
    docstring)."""
    run = await load_active_run_settings(conn)
    cadence = await _jsearch_cadence(conn, run, now)
    queries = await _live(conn)
    plan, cells = await _planned(conn, run, [int(row.id) for row in queries], cadence)
    by_query: defaultdict[int, list[_Cell]] = defaultdict(list)
    for cell in cells:
        by_query[cell.stats.query_id].append(cell)
    flags: Mapping[int, bool] = {} if run is None else run.queries
    return [
        _query(row, enabled=flags.get(row.id, False), plan=plan, cells=by_query[row.id], cadence=cadence)
        for row in queries
    ]
