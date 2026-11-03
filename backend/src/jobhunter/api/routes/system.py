"""`/api/system`: the detailed health (spec §17.5), the quota summary (§14.2 L5038; plan M4 Task 10) and the
manual score cycle (§14.2 L5028; M3 Task 21, Deviation 39), for a logged-in session.

- `GET /api/system/health`: readiness, runs, queues, the score lane, alerts, sizes and versions.
- `GET /api/system/quota` → per provider its current window with the window's counted usage, its reserved,
  ambiguous and adjustment units and its bucket, `null` without a current window
  (`observability.lists.quota_summary`), in one read snapshot (`db.snapshot.read_snapshot`).
- `GET /api/system/status` → the header strip's composite (`observability.status.system_status`, plan M4
  Task 11): the last run, the next slots, the quota windows, the LLM spend and its pace, the open gaps, the
  pending targets, the unacknowledged alerts, the re-score, the paid-search pause, the run-now units, the
  scope's apply and every banner, in one read snapshot; no readiness probe and no table sizes, so a down
  embedder never slows it. The UI polls it every 60 s; `/api/system/health` stays the System panel's.
- `POST /api/system/score-cycle` (`Idempotency-Key`, no body) → 202 `{queued, reason}`: one transaction,
  `active_config FOR SHARE` first, then `run_idempotent` around `cycles.request_cycle(trigger="manual")`.
  `queued` is false when a `manual` job already waits (it serves the request). `reason` says at once why the
  cycle would start nothing, read without a lock as `cycles.start_cycle` decides it, in its order:
  `no_pending` without an active tuple, else the score lane's pause reason (`cycles.pause_reason`, in this
  process's LLM mode), else `no_pending` without an admissible target; `null` otherwise (the cycle may still
  find the bucket short, which only the worker's start reads under the month row). The cycle's id is the
  worker's, shown by the health's `running_cycle_id`. Its answer is `ScoreCycleQueuedOut`: the name
  `ScoreCycleOut` is the score-cycle list's item (`observability.lists`, Task 10), and two models of one name
  would give both a module-qualified schema name.

`utcnow` is a name of this module, so a test injects the clock by patching
`jobhunter.api.routes.system.utcnow`.
"""

from datetime import datetime
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from starlette.responses import JSONResponse, Response

from jobhunter.api.deps import get_code_head, get_config, get_embedder, get_engine, require_session
from jobhunter.api.openapi import CSRF_RESPONSES, SESSION_RESPONSES
from jobhunter.api_idempotency import (
    IDEMPOTENCY_HEADER,
    StoredResponse,
    check_idempotency_key,
    request_fingerprint,
    run_idempotent,
)
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.db.snapshot import read_snapshot
from jobhunter.embeddings import EmbedderClient
from jobhunter.observability import lists, status
from jobhunter.observability.health import SystemHealthOut, check_readiness, system_health
from jobhunter.observability.lists import QuotaDetailOut
from jobhunter.observability.status import StatusOut
from jobhunter.quota.windows import Provider
from jobhunter.scoring import cycles
from jobhunter.scoring.admission import any_admissible
from jobhunter.scoring.cycles import PauseReason
from jobhunter.worker.tasks import defer_in_tx

router = APIRouter(prefix="/api/system", dependencies=[Depends(require_session)], responses=SESSION_RESPONSES)

SCORE_CYCLE_ROUTE: Final = "system.score_cycle"
NO_PENDING: Final = "no_pending"


class ScoreCycleQueuedOut(BaseModel):
    queued: bool = Field(description="False when a manual cycle job already waits: it serves this request")
    reason: PauseReason | Literal["no_pending"] | None = Field(
        description="Why the cycle would start nothing now: the score lane's pause reason, or `no_pending` "
        "without an admissible target; null otherwise"
    )


@router.get("/health", response_model=SystemHealthOut)
async def get_system_health(
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    code_head: Annotated[str, Depends(get_code_head)],
    embedder: Annotated[EmbedderClient, Depends(get_embedder)],
    cfg: Annotated[AppConfig, Depends(get_config)],
) -> SystemHealthOut:
    """Readiness, run ages and their state, queue depths, the score lane, open alerts, disk and table sizes,
    versions, and the embedder's health and model id."""
    now = utcnow()
    # Readiness first, on a connection it returns before this route takes one: one pool connection at a time.
    readiness = await check_readiness(engine, code_head=code_head, now=now, embedder=embedder)
    async with engine.connect() as conn:  # read-only: rolled back when the block ends
        return await system_health(conn, readiness=readiness, now=now, fake_llm=cfg.fake_llm)


@router.get("/quota", response_model=dict[Provider, QuotaDetailOut | None])
async def get_quota(
    engine: Annotated[AsyncEngine, Depends(get_engine)],
) -> dict[Provider, QuotaDetailOut | None]:
    """Each quota provider's current window with its counted usage, its reserved, ambiguous and adjustment
    units and its bucket; null for a provider without a current window."""
    async with read_snapshot(engine) as conn:
        return await lists.quota_summary(conn)


@router.get("/status", response_model=StatusOut)
async def get_system_status(
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    cfg: Annotated[AppConfig, Depends(get_config)],
) -> StatusOut:
    """The header strip: the last and the active run, the next slots, the quota windows, the month's LLM
    spend and its pace, the open coverage gaps, the pending targets, the unacknowledged alerts, the re-score,
    the paid-search pause, the run-now estimate, the scope's apply and the banners."""
    now = utcnow()
    async with read_snapshot(engine) as conn:
        return await status.system_status(conn, now=now, fake_llm=cfg.fake_llm)


async def _idle_reason(
    conn: AsyncConnection, *, fake_llm: bool, now: datetime
) -> PauseReason | Literal["no_pending"] | None:
    """Why a cycle would start nothing now (module docstring), in `start_cycle`'s order, without a lock."""
    config = await cycles.scoring_config(conn)  # `FOR SHARE`, which the route holds already
    tuple_id = config.active.llm_tuple_id
    if tuple_id is None:
        return NO_PENDING
    reason = await cycles.pause_reason(conn, fake_llm=fake_llm, now=now, config=config)
    if reason is not None:
        return reason
    return None if await any_admissible(conn, tuple_id, lane=None, now=now) else NO_PENDING


@router.post(
    "/score-cycle",
    status_code=202,
    response_model=ScoreCycleQueuedOut,
    responses={
        400: {"description": "Idempotency-Key missing or malformed (`idempotency_key_required`)"},
        409: {"description": "A request with this key is still running (`idempotency_in_progress`)"},
        422: {"description": "The key was used for another request (`idempotency_key_reused`)"},
        **CSRF_RESPONSES,
    },
)
async def request_score_cycle(
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    cfg: Annotated[AppConfig, Depends(get_config)],
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
) -> Response:
    """Request a manual score cycle (202), and say at once why it would start nothing."""
    key = check_idempotency_key(idempotency_key)  # a malformed key costs no database round trip
    now = utcnow()

    async def handler(conn: AsyncConnection) -> StoredResponse:
        queued = await cycles.request_cycle(conn, trigger="manual", defer=defer_in_tx)
        reason = await _idle_reason(conn, fake_llm=cfg.fake_llm, now=now)
        return StoredResponse(202, ScoreCycleQueuedOut(queued=queued, reason=reason).model_dump(mode="json"))

    async with engine.begin() as conn:
        await cycles.lock_active_scoring(conn)  # `active_config FOR SHARE` (§9.1), before the key's row
        stored = await run_idempotent(
            conn,
            route=SCORE_CYCLE_ROUTE,
            key=key,
            fingerprint=request_fingerprint({}, {}),
            now=now,
            handler=handler,
        )
    return JSONResponse(stored.body, status_code=stored.status)
