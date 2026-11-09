"""The coverage cells of a run's inputs (spec §5.5.2): which cells exist, and their rows as the planner reads
them (M1 Carry-forward 13: moved out of `quota.planner`, which orders and drafts them).

**Cells** (`ensure_cells`): one per `(instance, query, location_key)` for the search sources (JSearch,
SerpApi, Adzuna), over the enabled queries of the run's settings and the scope's location keys
(`location_keys`: the slug of every scope location, plus `remote-de` when remote work within Germany is on);
one Arbeitnow cell; one cell per enabled watchlist board (`_source_enabled`). Inserted `ON CONFLICT DO
NOTHING`, then read back as `CellRow`s, each with its place from the scope (`_cell_rows`). Which cells those
are is `planned_cells`, the planner's one selection; `PLANNED_SEARCH_CELL_SQL` is its predicate for a search
cell, which the query lists (`queries.listing`) select a query's planned cells by.

Every function runs in the caller's transaction and takes no lock of its own. The settings save (Task 17)
creates the cells of a new version with `ensure_cells`, and the settings dry-run counts the paid cells with
`location_keys`.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import TextClause

from jobhunter.db.sql import q
from jobhunter.run_settings import RunSettings, ScopeLocation, ScopeSettings
from jobhunter.sources.base import REMOTE_DE
from jobhunter.sources.coverage import SEARCH_SOURCES
from jobhunter.sources.instances import InstanceRow
from jobhunter.sources.watchlist_patterns import ATS_TYPES
from jobhunter.types import JSONValue

# A planned search cell of `coverage_cells c` (`planned_cells`: the binds `:search`, `:queries`, `:keys`).
_SEARCH_PLANNED: Final = (
    "(c.source_instance_id = ANY(CAST(:search AS bigint[])) AND c.query_id = ANY(CAST(:queries AS bigint[])) "
    "AND c.location_key = ANY(CAST(:keys AS text[])))"
)
# ... with the cell's own switch: the predicate the query lists select a query's planned cells by.
PLANNED_SEARCH_CELL_SQL: Final = f"(c.enabled AND {_SEARCH_PLANNED})"
# The columns of a `CellRow` but its location label and radius, which the scope gives.
_CELLS = q(
    "SELECT c.id, c.source_instance_id, c.query_id, c.location_key, c.enabled, c.last_attempted_at, "  # noqa: S608
    "c.last_covered_at, c.last_success_at, c.continuation, c.yield_ema, c.zero_yield_streak, "
    "c.consecutive_incomplete, c.last_exploration_at, i.source_id, i.instance_key, qr.text AS query_text, "
    "qr.lang AS query_lang, coalesce(qr.exploration, false) AS exploration FROM coverage_cells c "
    "JOIN source_instances i ON i.id = c.source_instance_id LEFT JOIN queries qr ON qr.id = c.query_id "
    f"WHERE c.enabled AND ({_SEARCH_PLANNED} "
    "OR (c.source_instance_id = ANY(CAST(:single AS bigint[])) AND c.query_id IS NULL "
    "AND c.location_key IS NULL)) ORDER BY c.id"
)
# The SerpApi cells (instance `:instance`) with the query and location key of the JSearch cells `:cells`.
_COUNTERPARTS = q(
    "SELECT c.id, c.source_instance_id, c.query_id, c.location_key, c.enabled, c.last_attempted_at, "
    "c.last_covered_at, c.last_success_at, c.continuation, c.yield_ema, c.zero_yield_streak, "
    "c.consecutive_incomplete, c.last_exploration_at, i.source_id, i.instance_key, qr.text AS query_text, "
    "qr.lang AS query_lang, coalesce(qr.exploration, false) AS exploration FROM coverage_cells j "
    "JOIN coverage_cells c ON c.query_id = j.query_id AND c.location_key = j.location_key "
    "JOIN source_instances i ON i.id = c.source_instance_id LEFT JOIN queries qr ON qr.id = c.query_id "
    "WHERE j.id = ANY(CAST(:cells AS bigint[])) AND c.source_instance_id = :instance AND c.enabled "
    "ORDER BY c.id"
)
_INSERT_SEARCH_CELLS = q(
    "INSERT INTO coverage_cells (source_instance_id, query_id, location_key) "
    "SELECT i.id, qr.id, k.key FROM unnest(CAST(:instances AS bigint[])) AS i(id) "
    "CROSS JOIN (SELECT id FROM queries WHERE id = ANY(CAST(:queries AS bigint[]))) AS qr "
    "CROSS JOIN unnest(CAST(:keys AS text[])) WITH ORDINALITY AS k(key, ord) "
    "ORDER BY i.id, qr.id, k.ord ON CONFLICT DO NOTHING"
)
_INSERT_SINGLE_CELLS = q(
    "INSERT INTO coverage_cells (source_instance_id) SELECT i.id FROM unnest(CAST(:instances AS bigint[])) "
    "AS i(id) ORDER BY i.id ON CONFLICT DO NOTHING"
)


@dataclass(frozen=True)
class CellRow:
    """A `coverage_cells` row with its instance, its query and (from the scope) its place."""

    id: int
    source_instance_id: int
    query_id: int | None
    location_key: str | None
    enabled: bool
    last_attempted_at: datetime | None
    last_covered_at: datetime | None
    last_success_at: datetime | None
    continuation: JSONValue
    yield_ema: float
    zero_yield_streak: int
    consecutive_incomplete: int
    last_exploration_at: datetime | None
    source_id: str
    instance_key: str
    query_text: str | None
    query_lang: str | None
    exploration: bool
    location_label: str | None  # the scope location's label; `None` for `remote-de` and cells without one
    radius_km: int | None


def _places(scope: ScopeSettings) -> dict[str, ScopeLocation | None]:
    """The location keys of the scope, in order, each with its first location (`None` for `remote-de`)."""
    places: dict[str, ScopeLocation | None] = {}
    for location in scope.locations:
        places.setdefault(location.key, location)
    if scope.remote_within_germany:
        places.setdefault(REMOTE_DE, None)
    return places


def location_keys(scope: ScopeSettings) -> list[tuple[str, str | None]]:
    """`(slug, label)` per scope location, de-duplicated on the slug (the first label wins), then
    `("remote-de", None)` when remote work within Germany is on (§5.5.2)."""
    return [(key, None if place is None else place.label) for key, place in _places(scope).items()]


def _source_enabled(settings: RunSettings, instance: InstanceRow) -> bool:
    """Whether the run's settings enable the instance's source (and, for a board, its watchlist company)."""
    switches = settings.sources
    match instance.source_id:
        case "jsearch":
            return switches.jsearch
        case "serpapi":
            return switches.serpapi
        case "adzuna":
            return switches.adzuna
        case "arbeitnow":
            return switches.arbeitnow
    company = instance.watchlist_company_id
    return (
        instance.source_id in ATS_TYPES
        and switches.ats
        and company is not None
        and settings.watchlist.get(company, False)
    )


async def _cell_rows(
    conn: AsyncConnection, statement: TextClause, params: Mapping[str, Any], scope: ScopeSettings
) -> list[CellRow]:
    places = _places(scope)
    cells: list[CellRow] = []
    for row in (await conn.execute(statement, params)).mappings():
        place = None if row["location_key"] is None else places.get(row["location_key"])
        cells.append(
            CellRow(
                **row,
                location_label=None if place is None else place.label,
                radius_km=None if place is None else place.radius_km,
            )
        )
    return cells


@dataclass(frozen=True)
class PlannedCells:
    """Which cells the run's settings plan (§5.5.2; module docstring): the search cells of the `search`
    instances for the enabled `queries` at the location `keys`, and the one cell of each `single` instance."""

    keys: list[str]
    queries: list[int]
    search: list[int]
    single: list[int]

    def search_params(self) -> dict[str, list[int] | list[str]]:
        """The binds of `PLANNED_SEARCH_CELL_SQL`."""
        return {"search": self.search, "queries": self.queries, "keys": self.keys}


def planned_cells(settings: RunSettings, instances: Sequence[InstanceRow]) -> PlannedCells:
    """The cells `settings` plan among `instances`: the planner's one selection, which `ensure_cells` creates
    and reads and the query lists count."""
    return PlannedCells(
        keys=[key for key, _label in location_keys(settings.scope)],
        queries=sorted(query_id for query_id, enabled in settings.queries.items() if enabled),
        search=[i.id for i in instances if i.source_id in SEARCH_SOURCES and _source_enabled(settings, i)],
        single=[
            i.id for i in instances if i.source_id not in SEARCH_SOURCES and _source_enabled(settings, i)
        ],
    )


async def ensure_cells(
    conn: AsyncConnection, settings: RunSettings, instances: Sequence[InstanceRow]
) -> list[CellRow]:
    """The enabled cells of the run's inputs, created when absent (§5.5.2; see the module docstring)."""
    planned = planned_cells(settings, instances)
    if planned.search and planned.queries and planned.keys:
        params = {"instances": planned.search, "queries": planned.queries, "keys": planned.keys}
        await conn.execute(_INSERT_SEARCH_CELLS, params)
    if planned.single:
        await conn.execute(_INSERT_SINGLE_CELLS, {"instances": planned.single})
    params = {**planned.search_params(), "single": planned.single}
    return await _cell_rows(conn, _CELLS, params, settings.scope)
