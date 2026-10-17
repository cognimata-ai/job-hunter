"""`GET /api/system/health`: the detailed health (spec §17.5), for a logged-in session.

`utcnow` is a name of this module, so a test injects the clock by patching
`jobhunter.api.routes.system.utcnow`.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.api.deps import get_code_head, get_engine, require_session
from jobhunter.clock import utcnow
from jobhunter.observability.health import SystemHealthOut, system_health

router = APIRouter(prefix="/api/system", dependencies=[Depends(require_session)])


@router.get("/health", response_model=SystemHealthOut)
async def get_system_health(
    engine: Annotated[AsyncEngine, Depends(get_engine)], code_head: Annotated[str, Depends(get_code_head)]
) -> SystemHealthOut:
    """Readiness, run ages and their state, queue depths, open alerts, disk and table sizes, versions."""
    async with engine.connect() as conn:  # read-only: rolled back when the block ends
        return await system_health(conn, engine=engine, code_head=code_head, now=utcnow())
