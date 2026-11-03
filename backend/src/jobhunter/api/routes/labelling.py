"""`GET /api/labelling/queue` (spec §15.6, §11.13; plan M4 Task 9, B16): the week-1 labelling queue.

`GET /api/labelling/queue?limit` (`limit` 1 to 50, default 10) → `jobs.labelling.LabellingQueueOut{items,
progress}` (`jobs.labelling.labelling_queue`) in one read snapshot (`db.snapshot.read_snapshot`): up to
`limit` ads stratified over the score bands, the unlabelled ones first, and the progress of the active
profile's `week1` labels against 50. The answer carries no score and no band, so the label is not anchored
on one; without an active profile it is empty. The labels themselves are written by `POST
/api/jobs/{job_id}/feedback` (`api.routes.golden`).
"""

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.api.deps import get_engine, require_session
from jobhunter.api.openapi import SESSION_RESPONSES
from jobhunter.db.snapshot import read_snapshot
from jobhunter.jobs.labelling import LabellingQueueOut, labelling_queue

router = APIRouter(dependencies=[Depends(require_session)], responses=SESSION_RESPONSES)

DEFAULT_LIMIT: Final = 10
MAX_LIMIT: Final = 50


@router.get(
    "/api/labelling/queue",
    response_model=LabellingQueueOut,
    responses={422: {"description": "A `limit` outside 1 to 50 (`validation_failed`)"}},
)
async def get_labelling_queue(
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> LabellingQueueOut:
    """The next ads to label, spread over the score bands with the unlabelled ones first, and the labelling
    progress; neither shows a score."""
    async with read_snapshot(engine) as conn:
        return await labelling_queue(conn, limit=limit)
