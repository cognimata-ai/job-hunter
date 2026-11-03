"""The watchlist list of the settings page (spec §14.2, §15.7; plan M4 Task 13, D1): `GET /api/watchlist`.

`watchlist(conn)` reads on the caller's connection, which the route opens as one read snapshot
(`db.snapshot.read_snapshot`): plain reads, no lock, and the same statements however many companies there are
(the active settings, then one statement for the companies).

Every `watchlist_companies` row, ordered by `display_name`, then `id`, with
- its board (`ats_type`, `board_slug`, `lever_region`) and its validation (`validation_status`,
  `validated_at`);
- `enabled`: its flag in the active settings' `watchlist[]`, as the run's planner reads it
  (`RunSettings.watchlist`); `false` for a company they do not list;
- `instance`: the board's source instance (`source_instances.watchlist_company_id`, unique): its `health`,
  `last_error_code`, `last_success_at` and `last_complete_snapshot_at`, the `taken_at` of its
  `last_complete_snapshot_id`'s snapshot (null before its first complete one); `null` for a company without an
  instance;
- `postings`: the instance's postings by `availability`, every bucket present (0 when empty, and all 0 without
  an instance), counted in one `GROUP BY` over the watchlist boards' postings, read through the leading
  `source_instance_id` of the `postings` key.
"""

from collections.abc import Mapping
from typing import Any, Final, Literal

from pydantic import BaseModel, Field
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.api.models import UtcDatetime
from jobhunter.db.sql import q
from jobhunter.run_settings import load_active_run_settings
from jobhunter.sources.breaker import Health
from jobhunter.sources.watchlist_patterns import AtsType, LeverRegion
from jobhunter.sources.watchlist_validate import Validation

ValidationStatus = Literal["unvalidated", Validation]

_COMPANIES: Final = q(
    "/* watchlist.companies */ SELECT w.id, w.display_name, w.ats_type, w.board_slug, w.lever_region, "
    "w.validation_status, w.validated_at, i.id AS instance_id, i.health, i.last_error_code, "
    "i.last_success_at, n.taken_at AS last_complete_snapshot_at, coalesce(p.active, 0) AS active, "
    "coalesce(p.stale, 0) AS stale, coalesce(p.unknown, 0) AS unknown, coalesce(p.closed, 0) AS closed "
    "FROM watchlist_companies w LEFT JOIN source_instances i ON i.watchlist_company_id = w.id "
    "LEFT JOIN ats_snapshots n ON n.id = i.last_complete_snapshot_id "
    "LEFT JOIN (SELECT source_instance_id, count(*) FILTER (WHERE availability = 'active') AS active, "
    "count(*) FILTER (WHERE availability = 'stale') AS stale, "
    "count(*) FILTER (WHERE availability = 'unknown') AS unknown, "
    "count(*) FILTER (WHERE availability = 'closed') AS closed FROM postings "
    "WHERE source_instance_id IN (SELECT id FROM source_instances WHERE watchlist_company_id IS NOT NULL) "
    "GROUP BY source_instance_id) p ON p.source_instance_id = i.id "
    "ORDER BY w.display_name, w.id"
)


class WatchlistInstanceOut(BaseModel):
    """The board as a source: its health, its last error, its last success and its last complete snapshot."""

    health: Health
    last_error_code: str | None
    last_success_at: UtcDatetime | None
    last_complete_snapshot_at: UtcDatetime | None = Field(
        description="When its last structurally complete snapshot was taken; null before the first"
    )


class PostingCountsOut(BaseModel):
    """The board's postings by availability."""

    active: int
    stale: int
    unknown: int
    closed: int


class WatchlistCompanyOut(BaseModel):
    """A watchlist company: its board, its validation, whether the active settings enable it, its source
    instance and its postings."""

    id: int
    display_name: str
    ats_type: AtsType
    board_slug: str
    lever_region: LeverRegion | None = Field(description="Lever's region; null for every other ATS")
    validation_status: ValidationStatus
    validated_at: UtcDatetime | None
    enabled: bool = Field(description="Its flag in the active settings; false for a company they do not list")
    instance: WatchlistInstanceOut | None = Field(description="Null for a company without a source instance")
    postings: PostingCountsOut = Field(description="All 0 for a company without a source instance")


def _company(row: Row[Any], *, enabled: bool) -> WatchlistCompanyOut:
    instance = None
    if row.instance_id is not None:
        instance = WatchlistInstanceOut(
            health=row.health,
            last_error_code=row.last_error_code,
            last_success_at=row.last_success_at,
            last_complete_snapshot_at=row.last_complete_snapshot_at,
        )
    return WatchlistCompanyOut(
        id=row.id,
        display_name=row.display_name,
        ats_type=row.ats_type,
        board_slug=row.board_slug,
        lever_region=row.lever_region,
        validation_status=row.validation_status,
        validated_at=row.validated_at,
        enabled=enabled,
        instance=instance,
        postings=PostingCountsOut(active=row.active, stale=row.stale, unknown=row.unknown, closed=row.closed),
    )


async def watchlist(conn: AsyncConnection) -> list[WatchlistCompanyOut]:
    """Every watchlist company with its validation, its settings flag, its instance's health and last
    complete snapshot, and its posting counts, by display name, then id (module docstring)."""
    run = await load_active_run_settings(conn)
    flags: Mapping[int, bool] = {} if run is None else run.watchlist
    return [_company(row, enabled=flags.get(row.id, False)) for row in await conn.execute(_COMPANIES)]
