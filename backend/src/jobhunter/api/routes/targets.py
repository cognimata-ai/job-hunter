"""The user's moves of the score lane (spec §9.6.4, §10.10 L3991-3997, §14.1 L4963, §14.2 L4976, L4985,
L5027; plan Task 21, Review Focus 5, Deviation 39, Codex r1 #19, #20, Carry T19-d).

Each route needs an `Idempotency-Key` (§14.1: each can start paid work), refuses a malformed one before any
SQL, and runs one transaction in the lock schedule's order: `active_config FOR SHARE` first (step 2), then
the key's row (`run_idempotent`, step 3), then the domain function, whose own `FOR SHARE` is a no-op. No API
transaction stamps an epoch (Deviation 39), so none calls `fence_epoch`. A refusal raises its problem inside
the handler, so the key's row rolls back with the rest and a retry with the key runs again.

- `POST /api/jobs/{job_id}/score-now` (`{force?: bool}`) → 202 `{target_id, state}`: `targets.score_now`
  (the job's current-revision target of the active tuple to `pending` with its override and lane `score_now`,
  then the `score_now` cycle requested; a running cycle's next claim round, or the waiting job, serves it).
  404 `not_found` for a job without a target of the active tuple, a synthetic golden-case job among them
  (Carry T19-d: `targets.create_or_revive` refuses one, `SyntheticJobRefused`, a 404 here too); 409
  `filtered_requires_force` for a filtered-out target without `force`, `budget_exhausted` when the month's
  scoring headroom is below one attempt's first reservation, `conflict_state` for a target being scored,
  batched, published or cancelled.
- `POST /api/targets/{target_id}/retry` → 202 `{target_id, state}`: `targets.retry` of a `refused` or
  `failed_*` target (the Retry edge: its charged attempts reset); 404 `not_found`, 409 `conflict_state`.
- `POST /api/batches/{submission_id}/release` (`{confirm: true}`) → 202 `{submission_id, state:
  "released", reserved_usd}`: `batches.release` of a `submit_ambiguous` submission (the owner row, then the
  month row its entries' settlements lock, the bulk rows, the targets: after `active_config` and the key's
  row, before any target row); `reserved_usd` is the amount its entries keep counted as `ambiguous` unless
  the batch is found later. 409 `conflict_state` for a submission in any other state, 404 `not_found` for
  no such submission, 422 without `confirm: true`.

`utcnow` is a name of this module, so a test injects the clock by patching
`jobhunter.api.routes.targets.utcnow`. The domain functions log their moves and refusals (ids and codes).
"""

from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, Header, Path
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from starlette.responses import JSONResponse, Response

from jobhunter.api.deps import get_engine, require_session
from jobhunter.api.errors import Problem
from jobhunter.api.models import DecimalOut
from jobhunter.api.openapi import BODY_LIMIT_RESPONSES, CSRF_RESPONSES, SESSION_RESPONSES
from jobhunter.api_idempotency import (
    IDEMPOTENCY_HEADER,
    StoredResponse,
    check_idempotency_key,
    request_fingerprint,
    run_idempotent,
)
from jobhunter.clock import utcnow
from jobhunter.db.sql import q
from jobhunter.errors import TransitionConflict
from jobhunter.scoring import batches, targets
from jobhunter.scoring.targets import ScoreNowCode, ScoreNowRefused, SyntheticJobRefused
from jobhunter.worker.tasks import defer_in_tx

router = APIRouter(dependencies=[Depends(require_session)], responses=SESSION_RESPONSES)

SCORE_NOW_ROUTE: Final = "jobs.score_now"
RETRY_ROUTE: Final = "targets.retry"
RELEASE_ROUTE: Final = "batches.release"
_BIGINT_MAX: Final = 2**63 - 1

_LOCK_ACTIVE_CONFIG: Final = q("SELECT 1 FROM active_config WHERE id = 1 FOR SHARE")
_SUBMISSION_EXISTS: Final = q("SELECT EXISTS (SELECT 1 FROM batch_submissions WHERE id = :s)")

_KEY_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    400: {"description": "Idempotency-Key missing or malformed (`idempotency_key_required`)"},
}
_IN_PROGRESS: Final = "a request with this key is still running (`idempotency_in_progress`)"
_KEY_REUSED: Final = "the key was used for another request (`idempotency_key_reused`)"


class ScoreNowIn(BaseModel):
    """Score now (§9.6.4): `force` scores a filtered-out job anyway (`override = 'score_anyway'`)."""

    model_config = ConfigDict(extra="forbid")

    force: bool = False


class TargetMovedOut(BaseModel):
    """The target a move took, and its state after it (`pending`)."""

    target_id: int
    state: str


class ReleaseIn(BaseModel):
    """The release of a quarantined submission (§10.10): `confirm` must be `true`."""

    model_config = ConfigDict(extra="forbid")

    confirm: Literal[True]


class ReleasedOut(BaseModel):
    """A released submission and the reservation its entries keep counted as `ambiguous`."""

    submission_id: int
    state: Literal["released"]
    reserved_usd: DecimalOut


JobId = Annotated[int, Path(ge=1, le=_BIGINT_MAX, description="The id of a job")]
TargetId = Annotated[int, Path(ge=1, le=_BIGINT_MAX, description="The id of a score target")]
SubmissionId = Annotated[int, Path(ge=1, le=_BIGINT_MAX, description="The id of a batch submission")]


def _not_found(what: str) -> Problem:
    return Problem(404, "not_found", f"{what} not found")


# The 409 refusals of a move: the title and the detail of each code.
_REFUSALS: Final[Mapping[ScoreNowCode, tuple[str, str]]] = MappingProxyType(
    {
        "filtered_requires_force": (
            "The job is filtered out",
            "it failed a hard filter of the active scope; send force: true to score it anyway",
        ),
        "budget_exhausted": (
            "The scoring budget of this month is exhausted",
            "raise the monthly cap or wait for the next month",
        ),
        "conflict_state": (
            "The target cannot be moved now",
            "it is being scored, batched, published or cancelled",
        ),
    }
)


def _refused(refusal: ScoreNowRefused, what: str) -> Problem:
    """The problem of a refused move."""
    if refusal.code == "not_found":
        return _not_found(what)
    title, detail = _REFUSALS[refusal.code]
    return Problem(409, refusal.code, title, detail=detail)


async def _idempotent(
    engine: AsyncEngine,
    *,
    route: str,
    key: str,
    fingerprint: bytes,
    handler: Callable[[AsyncConnection], Awaitable[StoredResponse]],
) -> Response:
    """One transaction: `active_config FOR SHARE` (step 2), then `run_idempotent` around `handler` (step
    3)."""
    async with engine.begin() as conn:
        await conn.execute(_LOCK_ACTIVE_CONFIG)
        stored = await run_idempotent(
            conn, route=route, key=key, fingerprint=fingerprint, now=utcnow(), handler=handler
        )
    return JSONResponse(stored.body, status_code=stored.status)


@router.post(
    "/api/jobs/{job_id}/score-now",
    status_code=202,
    response_model=TargetMovedOut,
    responses={
        **_KEY_RESPONSES,
        404: {"description": "No target of the active tuple for the job (`not_found`)"},
        409: {
            "description": "The job is filtered out and `force` is not set (`filtered_requires_force`), the "
            "month's scoring budget is exhausted (`budget_exhausted`), the target is being scored, batched, "
            f"published or cancelled (`conflict_state`), or {_IN_PROGRESS}"
        },
        422: {"description": f"Invalid body (`validation_failed`), or {_KEY_REUSED}"},
        **CSRF_RESPONSES,
        **BODY_LIMIT_RESPONSES,
    },
)
async def score_now(
    job_id: JobId,
    body: ScoreNowIn,
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
) -> Response:
    """Score the job's target now, outside the cycle's admission and the bucket (202)."""
    key = check_idempotency_key(idempotency_key)  # a malformed key costs no database round trip
    now = utcnow()

    async def handler(conn: AsyncConnection) -> StoredResponse:
        try:
            moved = await targets.score_now(conn, job_id=job_id, force=body.force, now=now, defer=defer_in_tx)
        except SyntheticJobRefused:
            moved = ScoreNowRefused("not_found")  # a golden case's job is never scored live (Carry T19-d)
        if isinstance(moved, ScoreNowRefused):
            raise _refused(moved, "Job target")
        return StoredResponse(202, TargetMovedOut(target_id=moved.target_id, state=moved.state).model_dump())

    fingerprint = request_fingerprint(body.model_dump(mode="json"), {"job_id": job_id})
    return await _idempotent(engine, route=SCORE_NOW_ROUTE, key=key, fingerprint=fingerprint, handler=handler)


@router.post(
    "/api/targets/{target_id}/retry",
    status_code=202,
    response_model=TargetMovedOut,
    responses={
        **_KEY_RESPONSES,
        404: {"description": "No such target (`not_found`)"},
        409: {"description": f"The target is not refused or failed (`conflict_state`), or {_IN_PROGRESS}"},
        422: {"description": f"Invalid path (`validation_failed`), or {_KEY_REUSED}"},
        **CSRF_RESPONSES,
    },
)
async def retry_target(
    target_id: TargetId,
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
) -> Response:
    """Retry a refused or failed target: its charged attempts reset, scored now (202)."""
    key = check_idempotency_key(idempotency_key)
    now = utcnow()

    async def handler(conn: AsyncConnection) -> StoredResponse:
        moved = await targets.retry(conn, target_id=target_id, now=now, defer=defer_in_tx)
        if isinstance(moved, ScoreNowRefused):
            raise _refused(moved, "Target")
        return StoredResponse(202, TargetMovedOut(target_id=moved.target_id, state=moved.state).model_dump())

    fingerprint = request_fingerprint({}, {"target_id": target_id})
    return await _idempotent(engine, route=RETRY_ROUTE, key=key, fingerprint=fingerprint, handler=handler)


@router.post(
    "/api/batches/{submission_id}/release",
    status_code=202,
    response_model=ReleasedOut,
    responses={
        **_KEY_RESPONSES,
        404: {"description": "No such batch submission (`not_found`)"},
        409: {"description": f"The submission is not quarantined (`conflict_state`), or {_IN_PROGRESS}"},
        422: {"description": f"Invalid body or no `confirm: true` (`validation_failed`), or {_KEY_REUSED}"},
        **CSRF_RESPONSES,
        **BODY_LIMIT_RESPONSES,
    },
)
async def release_batch(
    submission_id: SubmissionId,
    body: ReleaseIn,
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
) -> Response:
    """Release a quarantined batch submission and re-score its targets; its reserved spend stays counted as
    `ambiguous` unless the batch is found later (202)."""
    key = check_idempotency_key(idempotency_key)
    now = utcnow()

    async def handler(conn: AsyncConnection) -> StoredResponse:
        try:
            released = await batches.release(conn, submission_id, now=now, defer=defer_in_tx)
        except TransitionConflict:
            exists = bool((await conn.execute(_SUBMISSION_EXISTS, {"s": submission_id})).scalar_one())
            if not exists:
                raise _not_found("Batch submission") from None
            raise Problem(
                409,
                "conflict_state",
                "The batch submission is not quarantined",
                detail="only a submission whose send is ambiguous can be released",
            ) from None
        out = ReleasedOut(submission_id=submission_id, state="released", reserved_usd=released.reserved_usd)
        return StoredResponse(202, out.model_dump(mode="json"))

    fingerprint = request_fingerprint(body.model_dump(mode="json"), {"submission_id": submission_id})
    return await _idempotent(engine, route=RELEASE_ROUTE, key=key, fingerprint=fingerprint, handler=handler)
