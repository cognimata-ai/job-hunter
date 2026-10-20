"""`/healthz` and `/readyz` (spec §17.5): no authentication, outside `/api`."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.responses import JSONResponse, Response

from jobhunter.api.deps import get_code_head, get_embedder, get_engine
from jobhunter.clock import utcnow
from jobhunter.embeddings import EmbedderClient
from jobhunter.observability.health import ReadinessCode, check_readiness

router = APIRouter()


class HealthOut(BaseModel):
    status: Literal["ok"]


class ReadyOut(BaseModel):
    """Every serving prerequisite holds; the body is `{}`."""

    model_config = ConfigDict(extra="forbid")


class NotReadyOut(BaseModel):
    failing: list[ReadinessCode]


@router.get("/healthz", response_model=HealthOut)
async def healthz() -> HealthOut:
    """The process is up and its event loop answers."""
    return HealthOut(status="ok")


@router.get(
    "/readyz",
    response_model=ReadyOut,
    responses={503: {"model": NotReadyOut, "description": "A serving prerequisite does not hold"}},
)
async def readyz(
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    code_head: Annotated[str, Depends(get_code_head)],
    embedder: Annotated[EmbedderClient, Depends(get_embedder)],
) -> Response | ReadyOut:
    """200 when the database, the schema revision, the worker heartbeat and the embedder (healthy, with its
    checksum verified) hold, else 503 with the codes."""
    report = await check_readiness(engine, code_head=code_head, now=utcnow(), embedder=embedder)
    if report.ok:
        return ReadyOut()
    return JSONResponse(NotReadyOut(failing=report.failing).model_dump(), status_code=503)
