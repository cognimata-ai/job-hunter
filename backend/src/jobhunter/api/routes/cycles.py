"""`GET /api/score-cycles` (spec §14.2 L5036, §7.6; plan M4 Task 10): the score cycles, newest first.

`GET /api/score-cycles?cursor&limit` → `{items, next_cursor}` (`observability.lists.score_cycles_page`): the
cycles by id descending, read in one read snapshot (`db.snapshot.read_snapshot`), keyset-paged with §14.3's
signed cursor (`api.pagination`: sort `score_cycles.id`, `desc`, `last_key = [id]`, no filters), limit 1 to
100 (default 50). A cursor this list did not sign (the alert list's included, which also sorts by an id
descending) is 400 `invalid_cursor`.
"""

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.api.deps import get_config, get_engine, require_session
from jobhunter.api.openapi import SESSION_RESPONSES
from jobhunter.api.pagination import SCORE_CYCLES_SORT, cursor_key, decode_id_cursor, list_page
from jobhunter.config import AppConfig
from jobhunter.db.snapshot import read_snapshot
from jobhunter.observability import lists
from jobhunter.observability.lists import ScoreCyclesPageOut

router = APIRouter(
    prefix="/api/score-cycles", dependencies=[Depends(require_session)], responses=SESSION_RESPONSES
)

DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 100


@router.get(
    "",
    response_model=ScoreCyclesPageOut,
    responses={400: {"description": "A cursor this list did not issue (`invalid_cursor`)"}},
)
async def list_score_cycles(
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    cfg: Annotated[AppConfig, Depends(get_config)],
    cursor: Annotated[
        str | None, Query(max_length=2048, description="`next_cursor` of the page before")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> ScoreCyclesPageOut:
    """The score cycles, newest first, keyset-paged (§14.3)."""
    key = cursor_key(cfg)
    after = (
        None
        if cursor is None
        else decode_id_cursor(cursor, key=key, sort=SCORE_CYCLES_SORT, dir="desc", filters={})
    )
    async with read_snapshot(engine) as conn:
        rows = await lists.score_cycles_page(conn, after_id=after, limit=limit + 1)
    items, next_cursor = list_page(
        rows,
        limit=limit,
        last_key=lambda cycle: [cycle.id],
        sort=SCORE_CYCLES_SORT,
        dir="desc",
        filters={},
        key=key,
    )
    return ScoreCyclesPageOut(items=items, next_cursor=next_cursor)
