"""`/api/queries`: listing, adding and editing search queries (spec §5.5.1, §14.2; plan Task 20, Deviations 3
and 15; plan M4 Task 13).

- `GET /api/queries` → the live queries by id (`queries.listing.query_list`), each with its flag in the active
  settings and the aggregates of its planned coverage cells; `GET /api/queries/stats` → every planned cell of
  a live query with its statistics and open gaps (`queries.listing.cell_stats`). Planned: the planner's
  selection under the active settings (Ruling T13-b); both descriptions say that the header's open-gap total
  also counts the gaps of cells no longer planned. Each reads in one read snapshot
  (`db.snapshot.read_snapshot`), a fixed number of statements, no lock.
- `POST /api/queries` (`{text, lang, proposal_id?, exploration?}`) → 201 `QueryOut`: a new query
  (`queries.authoring.create_query`), not enabled; enabling it is a settings save with its preview (§13.5).
  `proposal_id` and `exploration` go beyond §14.2's body (Deviation 3), so an accepted proposal item keeps its
  origin (`llm_proposal`) and its flag. 409 `conflict_state` for a live query with the same text (its id in
  `query_id`) or a proposal that is not `ready`; 422 `validation_failed` for a text outside 2 to 120
  characters or an unknown proposal.
- `PATCH /api/queries/{id}` (`{text?, lang?}`) → 200 `QueryOut`, the new query (`authoring.edit_query`,
  "retire + create"): `active_config` `FOR UPDATE` first (§9.1), as the settings save route takes it, then the
  edit, whose internal settings save (class `collection`) moves the active `queries[]` entry to the new id.
  An absent member keeps its value; an edit that changes nothing answers the query as it is. A retired or
  unknown id is 404 `not_found`.

Neither takes an `Idempotency-Key` (§14.1 does not list them): a repeated create meets its own text (409), a
repeated edit the retired row (404). `utcnow` is a name of this module, so a test injects the clock by
patching `jobhunter.api.routes.queries.utcnow`. Logs carry ids only, never a query text.
"""

from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.api.deps import get_engine, require_session
from jobhunter.api.openapi import BODY_LIMIT_RESPONSES, CSRF_RESPONSES, SESSION_RESPONSES
from jobhunter.clock import utcnow
from jobhunter.db.snapshot import read_snapshot
from jobhunter.db.sql import q
from jobhunter.observability.logging import get_logger
from jobhunter.queries import authoring, listing
from jobhunter.queries.listing import CellStatsOut, QueryListOut
from jobhunter.worker.tasks import defer_in_tx

router = APIRouter(
    prefix="/api/queries", dependencies=[Depends(require_session)], responses=SESSION_RESPONSES
)
log = get_logger("jobhunter.api.queries")

_BIGINT_MAX: Final = 2**63 - 1
_LOCK_ACTIVE_CONFIG: Final = q("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE")

QueryId = Annotated[int, Path(ge=1, le=_BIGINT_MAX, description="The id of a live query")]


class QueryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    lang: Literal["de", "en"]
    proposal_id: int | None = Field(
        default=None, ge=1, le=_BIGINT_MAX, description="The ready proposal whose item this query accepts"
    )
    exploration: bool = False


class QueryEditIn(BaseModel):
    """An absent member keeps the query's value."""

    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    lang: Literal["de", "en"] | None = None


class QueryOut(BaseModel):
    id: int
    text: str
    lang: Literal["de", "en"]
    origin: Literal["llm_proposal", "user"]
    exploration: bool
    proposal_id: int | None


def _out(row: authoring.QueryRow) -> QueryOut:
    return QueryOut(
        id=row.id,
        text=row.text,
        lang=row.lang,
        origin=row.origin,
        exploration=row.exploration,
        proposal_id=row.proposal_id,
    )


@router.get("", response_model=list[QueryListOut])
async def list_queries(engine: Annotated[AsyncEngine, Depends(get_engine)]) -> list[QueryListOut]:
    """The live queries by id, each with whether the active settings enable it and the coverage of its planned
    cells: how many, the oldest coverage of its locations, their open gaps, their mean yield, whether one is
    too broad, and JSearch's refresh cadence.

    A query's planned cells are those the next run would search while the query is enabled: its cells of the
    enabled sources at the scope's locations. Cells no longer planned (a location removed from the scope, a
    source switched off, a query disabled or edited) are not counted here, while the open-gap total of the
    status header still counts their open gaps."""
    now = utcnow()
    async with read_snapshot(engine) as conn:
        return await listing.query_list(conn, now=now)


@router.get("/stats", response_model=list[CellStatsOut])
async def query_cell_stats(engine: Annotated[AsyncEngine, Depends(get_engine)]) -> list[CellStatsOut]:
    """Every planned coverage cell of a live query, by query, source, instance and place: its coverage, its
    yield, its zero-yield and incomplete runs, whether it is too broad, its open gaps, and JSearch's refresh
    cadence for a JSearch cell.

    A planned cell is one the next run would search: a cell of an enabled query, of an enabled source, at one
    of the scope's locations. Cells no longer planned (a location removed from the scope, a source switched
    off, a query disabled or edited) are not listed, while the open-gap total of the status header still
    counts their open gaps."""
    now = utcnow()
    async with read_snapshot(engine) as conn:
        return await listing.cell_stats(conn, now=now)


@router.post(
    "",
    status_code=201,
    response_model=QueryOut,
    responses={
        409: {
            "description": "A live query has this text (`conflict_state`, with its `query_id`), or the "
            "proposal is not ready (`conflict_state`)"
        },
        422: {"description": "Invalid body, text or proposal (`validation_failed`)"},
        **CSRF_RESPONSES,
        **BODY_LIMIT_RESPONSES,
    },
)
async def add_query(body: QueryIn, engine: Annotated[AsyncEngine, Depends(get_engine)]) -> QueryOut:
    """Add a query (201); it is not enabled until a settings save lists it."""
    async with engine.begin() as conn:
        row = await authoring.create_query(
            conn,
            text=body.text,
            lang=body.lang,
            proposal_id=body.proposal_id,
            exploration=body.exploration,
            now=utcnow(),
        )
    log.info("query_created", query_id=row.id, origin=row.origin, proposal_id=row.proposal_id)
    return _out(row)


@router.patch(
    "/{query_id}",
    response_model=QueryOut,
    responses={
        404: {"description": "No such live query (`not_found`)"},
        409: {"description": "A live query has the new text (`conflict_state`, with its `query_id`)"},
        422: {"description": "Invalid body or text, or settings the swap cannot save (`validation_failed`)"},
        **CSRF_RESPONSES,
        **BODY_LIMIT_RESPONSES,
    },
)
async def edit_query(
    query_id: QueryId, body: QueryEditIn, engine: Annotated[AsyncEngine, Depends(get_engine)]
) -> QueryOut:
    """Edit a query: the row is retired and the new one answered (200)."""
    async with engine.begin() as conn:
        await conn.execute(_LOCK_ACTIVE_CONFIG)  # the first lock (§9.1), as the settings save route
        row = await authoring.edit_query(
            conn, query_id, text=body.text, lang=body.lang, now=utcnow(), defer=defer_in_tx
        )
    log.info("query_edited", query_id=query_id, new_query_id=row.id, retired=row.id != query_id)
    return _out(row)
