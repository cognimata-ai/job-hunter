"""Run now, the run history and the run report (spec §3.4, §14.1, §14.2 L5033-5035, §17.3; plan M4 Task 10,
D5).

`POST /api/runs`: Run now. A missing or malformed `Idempotency-Key` is refused (400) before any SQL. Then one
transaction: `active_config` `FOR SHARE` first (the first lock of the global order, before the
`api_idempotency` row), then `run_idempotent` around `request_manual_run`, which returns the active collection
(`created=false`) or queues a `manual` one and defers its job in the same transaction. A repeated
`Idempotency-Key` returns the stored response, even after that run finished, so a retried POST never starts a
second paid run. `request_manual_run` takes the same shared lock again, which is a no-op.

`GET /api/runs?cursor&limit&status` → `{items, next_cursor}`: the runs newest first by `(queued_at, id)`
(`observability.lists.runs_page`), keyset-paged with §14.3's signed cursor (`api.pagination`: sort
`runs.queued_at`, `desc`, `last_key = [queued_at as ISO-8601 UTC with microseconds, id]`, the filters
`{"status": …}` when given), limit 1 to 100 (default 50); `status` is one of the run statuses (else 422). A
cursor this list did not sign for these filters is 400 `invalid_cursor`.

`GET /api/runs/{run_id}` → `{run, report, partial_causes}` (`lists.run_detail`): the stored report and the
partial causes it names; 404 `not_found` for no such run (`lists.RunMissing` through
`api.errors.domain_problem`). Both GETs read in one read snapshot (`db.snapshot.read_snapshot`).
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Header, Path, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from starlette.responses import JSONResponse, Response

from jobhunter.api.deps import get_config, get_engine, require_session
from jobhunter.api.errors import domain_problem
from jobhunter.api.openapi import BODY_LIMIT_RESPONSES, CSRF_RESPONSES, SESSION_RESPONSES
from jobhunter.api.pagination import RUNS_SORT, cursor_key, decode_list_cursor, invalid_cursor, list_page
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
from jobhunter.db.sql import q
from jobhunter.observability import lists
from jobhunter.observability.health import RunOut, RunStatus
from jobhunter.observability.lists import RunDetailOut, RunsPageOut
from jobhunter.types import JSONValue
from jobhunter.worker.runs import request_manual_run
from jobhunter.worker.tasks import defer_in_tx

RUN_NOW_ROUTE: Final = "POST /api/runs"
DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 100
_BIGINT_MAX: Final = 2**63 - 1

router = APIRouter(prefix="/api/runs", dependencies=[Depends(require_session)], responses=SESSION_RESPONSES)

_LOCK_ACTIVE_CONFIG: Final = q("SELECT 1 FROM active_config WHERE id = 1 FOR SHARE")


class RunNowIn(BaseModel):
    """`include_paid` only: an unknown member is refused (422 `validation_failed`), as by every other body."""

    model_config = ConfigDict(extra="forbid")

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
        **CSRF_RESPONSES,
        **BODY_LIMIT_RESPONSES,
    },
)
async def run_now(
    body: RunNowIn,
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
) -> Response:
    """202 with the active collection (`created=false`) or a new manual one (`created=true`)."""
    key = check_idempotency_key(idempotency_key)  # a malformed key costs no database round trip

    async def handler(conn: AsyncConnection) -> StoredResponse:
        run, created = await request_manual_run(conn, include_paid=body.include_paid, defer=defer_in_tx)
        return StoredResponse(202, RunNowOut(run=RunOut.of(run), created=created).model_dump(mode="json"))

    async with engine.begin() as conn:
        await conn.execute(_LOCK_ACTIVE_CONFIG)
        stored = await run_idempotent(
            conn,
            route=RUN_NOW_ROUTE,
            key=key,
            fingerprint=request_fingerprint(body.model_dump(mode="json"), {}),
            now=utcnow(),
            handler=handler,
        )
    return JSONResponse(stored.body, status_code=stored.status)


RunId = Annotated[int, Path(ge=1, le=_BIGINT_MAX, description="The id of a run")]


def _filters(status: RunStatus | None) -> dict[str, JSONValue]:
    return {} if status is None else {"status": status}


def _utc_iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _after(cursor: str | None, key: bytes, filters: dict[str, JSONValue]) -> tuple[datetime, int] | None:
    """The `(queued_at, id)` the next page starts after, from a cursor of this list and these filters; `None`
    for the first page."""
    if cursor is None:
        return None
    match decode_list_cursor(cursor, key=key, sort=RUNS_SORT, dir="desc", filters=filters):
        case [str(at), int(run_id)] if not isinstance(run_id, bool) and run_id >= 1:
            try:
                queued_at = datetime.fromisoformat(at)
            except ValueError:
                raise invalid_cursor() from None
            if queued_at.utcoffset() != timedelta(0):
                raise invalid_cursor()
            return queued_at, run_id
    raise invalid_cursor()


@router.get(
    "",
    response_model=RunsPageOut,
    responses={400: {"description": "A cursor this list did not issue for these filters (`invalid_cursor`)"}},
)
async def list_runs(
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    cfg: Annotated[AppConfig, Depends(get_config)],
    cursor: Annotated[
        str | None, Query(max_length=2048, description="`next_cursor` of the page before")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    status: Annotated[RunStatus | None, Query(description="Only the runs of this status")] = None,
) -> RunsPageOut:
    """The runs, newest first, keyset-paged (§14.3)."""
    key = cursor_key(cfg)
    filters = _filters(status)
    after = _after(cursor, key, filters)
    async with read_snapshot(engine) as conn:
        rows = await lists.runs_page(conn, after=after, status=status, limit=limit + 1)
    items, next_cursor = list_page(
        rows,
        limit=limit,
        last_key=lambda run: [_utc_iso(run.queued_at), run.id],
        sort=RUNS_SORT,
        dir="desc",
        filters=filters,
        key=key,
    )
    return RunsPageOut(items=items, next_cursor=next_cursor)


@router.get(
    "/{run_id}",
    response_model=RunDetailOut,
    responses={404: {"description": "No such run (`not_found`)"}},
)
async def get_run(run_id: RunId, engine: Annotated[AsyncEngine, Depends(get_engine)]) -> RunDetailOut:
    """A run with its report and the partial causes the report names."""
    try:
        async with read_snapshot(engine) as conn:
            return await lists.run_detail(conn, run_id)
    except LookupError as exc:
        if (problem := domain_problem(exc)) is None:
            raise
        raise problem from None
