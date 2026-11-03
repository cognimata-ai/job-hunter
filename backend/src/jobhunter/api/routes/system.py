"""`/api/system`: the detailed health (spec §17.5), the quota summary (§14.2 L5038; plan M4 Task 10), the
manual score cycle (§14.2 L5028; M3 Task 21, Deviation 39), needs attention and the replay of rejected raw
items (§14.2 L5040-5042; plan M4 Task 12); and `/api/ats-snapshots`' acknowledgement (L5044), for a
logged-in session.

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

- `GET /api/system/needs-attention` → `AttentionOut` (`observability.attention.needs_attention`, plan M4
  Task 12, D23): each section's full count and at most 50 of its items, the score lane's acknowledgeable
  pause and a failed scope apply, in one read snapshot.
- `POST /api/system/raw-items/replay {raw_response_id | raw_item_ids}` → 202 `{operation_id, items}`
  (`sources.replay`, M1 Carry-forward 3 and 10; not keyed, §14.1): exactly one of the two (both or neither is
  422 `validation_failed`), `raw_item_ids` 1 to 500 ids. A `raw_response_id` is that response's `rejected`
  items (an unknown response is 404 `not_found`). In one transaction: the ids that can be replayed now
  (`replay.replayable_items`: `rejected`, their body kept and fetched within `RAW_BODY_TTL`, at most 500,
  lowest first), then `request_replay`, whose dedup key makes the same set reuse its live operation. `items`
  is how many ids it queued; none is 409 `conflict_state`.

The second router, `ats_snapshots_router` (`/api/ats-snapshots`):
- `POST /api/ats-snapshots/{snapshot_id}/acknowledge` → 200 `{snapshot_id, closed}`
  (`availability.snapshots.acknowledge`, §8.11; not keyed): the mass disappearance confirmed at once, its
  `unknown` postings closed, in `api.retry.with_deadlock_retry` like the merge family's writes; the
  acknowledgement bumps the ranking generation itself, last, when a job's availability changed. An unknown
  snapshot is 404 `not_found` (`api.errors.domain_problem`); one that is no mass disappearance (its
  `ValueError`, caught here first) 409 `conflict_state`. An event that is no longer open closes nothing (200,
  `closed = 0`).

`utcnow` is a name of this module, so a test injects the clock by patching
`jobhunter.api.routes.system.utcnow`.
"""

from datetime import datetime
from typing import Annotated, Final, Literal, Self

from fastapi import APIRouter, Depends, Header, Path
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from starlette.responses import JSONResponse, Response

from jobhunter.api.deps import get_code_head, get_config, get_embedder, get_engine, require_session
from jobhunter.api.errors import Problem, domain_problem
from jobhunter.api.openapi import BODY_LIMIT_RESPONSES, CSRF_RESPONSES, SESSION_RESPONSES
from jobhunter.api.retry import with_deadlock_retry
from jobhunter.api_idempotency import (
    IDEMPOTENCY_HEADER,
    StoredResponse,
    check_idempotency_key,
    request_fingerprint,
    run_idempotent,
)
from jobhunter.availability import snapshots
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.db.snapshot import read_snapshot
from jobhunter.embeddings import EmbedderClient
from jobhunter.errors import DomainError
from jobhunter.observability import attention, lists, status
from jobhunter.observability.attention import AttentionOut
from jobhunter.observability.health import SystemHealthOut, check_readiness, system_health
from jobhunter.observability.lists import QuotaDetailOut
from jobhunter.observability.status import StatusOut
from jobhunter.quota.windows import Provider
from jobhunter.scoring import cycles
from jobhunter.scoring.admission import any_admissible
from jobhunter.scoring.cycles import PauseReason
from jobhunter.sources import replay
from jobhunter.worker.tasks import defer_in_tx

router = APIRouter(prefix="/api/system", dependencies=[Depends(require_session)], responses=SESSION_RESPONSES)
ats_snapshots_router = APIRouter(
    prefix="/api/ats-snapshots", dependencies=[Depends(require_session)], responses=SESSION_RESPONSES
)

SCORE_CYCLE_ROUTE: Final = "system.score_cycle"
NO_PENDING: Final = "no_pending"
_BIGINT_MAX: Final = 2**63 - 1

RawId = Annotated[int, Field(ge=1, le=_BIGINT_MAX)]
SnapshotId = Annotated[int, Path(ge=1, le=_BIGINT_MAX, description="The id of an ATS snapshot")]


class ScoreCycleQueuedOut(BaseModel):
    queued: bool = Field(description="False when a manual cycle job already waits: it serves this request")
    reason: PauseReason | Literal["no_pending"] | None = Field(
        description="Why the cycle would start nothing now: the score lane's pause reason, or `no_pending` "
        "without an admissible target; null otherwise"
    )


class ReplayIn(BaseModel):
    """The rejected raw items to replay: every rejected item of one stored response, or a list of raw item
    ids (1 to 500); exactly one of the two."""

    model_config = ConfigDict(extra="forbid")

    raw_response_id: RawId | None = None
    raw_item_ids: list[RawId] | None = Field(default=None, min_length=1, max_length=replay.MAX_ITEMS)

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.raw_response_id is None) == (self.raw_item_ids is None):
            raise ValueError("give exactly one of raw_response_id and raw_item_ids")
        return self


class ReplayQueuedOut(BaseModel):
    """The replay operation and how many of the listed items it replays: those still rejected whose stored
    body is kept and at most 30 days old."""

    operation_id: int
    items: int


class SnapshotAcknowledgedOut(BaseModel):
    """The acknowledged snapshot and how many postings of its mass disappearance closed (0 for an event that
    is no longer open)."""

    snapshot_id: int
    closed: int


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


@router.get("/needs-attention", response_model=AttentionOut)
async def get_needs_attention(engine: Annotated[AsyncEngine, Depends(get_engine)]) -> AttentionOut:
    """What needs the user: rejected raw items, identity conflicts, refused and failed targets, quarantined
    batches, batches whose retrieval keeps failing, mass disappearances, unscorable duplicate groups, broken
    sources, the score lane's acknowledgeable pause and a failed scope apply."""
    now = utcnow()
    async with read_snapshot(engine) as conn:
        return await attention.needs_attention(conn, now=now)


@router.post(
    "/raw-items/replay",
    status_code=202,
    response_model=ReplayQueuedOut,
    responses={
        404: {"description": "No stored response has the id (`not_found`)"},
        409: {"description": "None of the listed items can be replayed (`conflict_state`)"},
        422: {"description": "Both or neither of the two, or no or too many ids (`validation_failed`)"},
        **CSRF_RESPONSES,
        **BODY_LIMIT_RESPONSES,
    },
)
async def replay_raw_items(
    body: ReplayIn, engine: Annotated[AsyncEngine, Depends(get_engine)]
) -> ReplayQueuedOut:
    """Replay rejected raw items (202): each item still rejected whose stored body is kept and at most 30 days
    old is parsed again by its source's current parser, and the items it now reads are resolved to jobs. The
    same set of items reuses the replay already queued for it."""
    now = utcnow()
    async with engine.begin() as conn:
        if body.raw_response_id is None:
            listed = body.raw_item_ids or []  # the model's rule: exactly one of the two is given
        else:
            try:
                listed = await replay.items_of_response(conn, body.raw_response_id)
            except LookupError as exc:
                if (problem := domain_problem(exc)) is None:
                    raise
                raise problem from None
        queued = await replay.replayable_items(conn, listed, now=now)
        if not queued:
            raise Problem(409, "conflict_state", "No listed raw item can be replayed")
        operation_id = await replay.request_replay(conn, raw_item_ids=queued, defer=defer_in_tx)
    return ReplayQueuedOut(operation_id=operation_id, items=len(queued))


@ats_snapshots_router.post(
    "/{snapshot_id}/acknowledge",
    response_model=SnapshotAcknowledgedOut,
    responses={
        404: {"description": "No ATS snapshot has the id (`not_found`)"},
        409: {
            "description": "The snapshot is no mass disappearance (`conflict_state`), or a concurrent change "
            "got in the way (`conflict_state`, with `retryable: true`)"
        },
        422: {"description": "Invalid path (`validation_failed`)"},
        **CSRF_RESPONSES,
    },
)
async def acknowledge_snapshot(
    snapshot_id: SnapshotId, engine: Annotated[AsyncEngine, Depends(get_engine)]
) -> SnapshotAcknowledgedOut:
    """Confirm the mass disappearance of the snapshot (its opening snapshot or a follow-up) at once: the
    postings of the event that are still unknown close."""
    now = utcnow()

    async def work(conn: AsyncConnection) -> int:
        return await snapshots.acknowledge(conn, snapshot_id, now=now)

    try:
        closed = await with_deadlock_retry(engine, work)
    except ValueError:  # before `domain_problem`, which maps no `ValueError`
        raise Problem(409, "conflict_state", "The snapshot is no mass disappearance") from None
    except (DomainError, LookupError) as exc:
        if (problem := domain_problem(exc)) is None:
            raise
        raise problem from None
    return SnapshotAcknowledgedOut(snapshot_id=snapshot_id, closed=closed)
