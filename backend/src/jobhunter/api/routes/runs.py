"""`POST /api/runs`: Run now (spec §3.4, §14.1, §14.2).

One transaction: `active_config` `FOR SHARE` first (the first lock of the global order, before the
`api_idempotency` row), then `run_idempotent` around `request_manual_run`, which returns the active collection
(`created=false`) or queues a `manual` one and defers its job in the same transaction. A repeated
`Idempotency-Key` returns the stored response, even after that run finished, so a retried POST never starts a
second paid run. `request_manual_run` takes the same shared lock again, which is a no-op.
"""

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from starlette.responses import JSONResponse, Response

from jobhunter.api.deps import get_engine, require_session
from jobhunter.api_idempotency import IDEMPOTENCY_HEADER, StoredResponse, request_fingerprint, run_idempotent
from jobhunter.clock import utcnow
from jobhunter.db.sql import q
from jobhunter.observability.health import RunOut
from jobhunter.worker.runs import request_manual_run
from jobhunter.worker.tasks import defer_in_tx

RUN_NOW_ROUTE: Final = "POST /api/runs"

router = APIRouter(prefix="/api/runs", dependencies=[Depends(require_session)])

_LOCK_ACTIVE_CONFIG: Final = q("SELECT 1 FROM active_config WHERE id = 1 FOR SHARE")


class RunNowIn(BaseModel):
    include_paid: bool


class RunNowOut(BaseModel):
    run: RunOut
    created: bool


@router.post(
    "",
    status_code=202,
    response_model=RunNowOut,
    responses={
        400: {"description": "Idempotency-Key missing or malformed (`idempotency_key_required`)"},
        409: {"description": "A request with this key is still running (`idempotency_in_progress`)"},
        422: {
            "description": "Invalid body, or the key was used for another request (`idempotency_key_reused`)"
        },
    },
)
async def run_now(
    body: RunNowIn,
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
) -> Response:
    """202 with the active collection (`created=false`) or a new manual one (`created=true`)."""

    async def handler(conn: AsyncConnection) -> StoredResponse:
        run, created = await request_manual_run(conn, include_paid=body.include_paid, defer=defer_in_tx)
        return StoredResponse(202, RunNowOut(run=RunOut.of(run), created=created).model_dump(mode="json"))

    async with engine.begin() as conn:
        await conn.execute(_LOCK_ACTIVE_CONFIG)
        stored = await run_idempotent(
            conn,
            route=RUN_NOW_ROUTE,
            key=idempotency_key,
            fingerprint=request_fingerprint(body.model_dump(mode="json"), {}),
            now=utcnow(),
            handler=handler,
        )
    return JSONResponse(stored.body, status_code=stored.status)
