"""`GET /api/eval-runs` (spec §11.13, §14.2, §14.3; plan M4 Task 14, D9): the golden runs or the schema
dry-runs for the settings page's eval status. Runs start from the CLI only, so the route only reads.

`GET /api/eval-runs?kind&cursor&limit` → `{items, next_cursor, active_gate}` in one read snapshot
(`db.snapshot.read_snapshot`): the runs of `kind` (`golden` or `schema_dry_run`, required; anything else is
422) by id descending (`scoring.eval_runs.list_eval_runs`), and the active configuration's eval gate with the
number of cases it counts (`eval_runs.active_gate`). Keyset-paged with §14.3's signed cursor
(`api.pagination`: sort `eval_runs.id`, `desc`, `last_key = [id]`, the filters `{"kind": …}`, so a cursor of
one kind is no cursor of the other), limit 1 to 100 (default 50); a cursor this list did not sign for these
filters is 400 `invalid_cursor`. Decimals leave as strings, times in UTC.
"""

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.api.deps import get_config, get_engine, require_session
from jobhunter.api.models import DecimalOut, UtcDatetime
from jobhunter.api.openapi import SESSION_RESPONSES
from jobhunter.api.pagination import EVAL_RUNS_SORT, cursor_key, decode_id_cursor, list_page
from jobhunter.config import AppConfig
from jobhunter.db.snapshot import read_snapshot
from jobhunter.scoring import eval_runs
from jobhunter.scoring.eval_runs import EvalRoute, EvalRunKind, EvalStatus
from jobhunter.settings.validate import GateState
from jobhunter.types import JSONValue

# Unprefixed: the list is no part of `/api/settings`, whose router has that prefix.
router = APIRouter(dependencies=[Depends(require_session)], responses=SESSION_RESPONSES)

DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 100


class EvalRunOut(BaseModel):
    """An eval run with its gate's figures; a run not calibrated (running, crashed, a schema dry-run) has
    none of them."""

    id: int
    kind: EvalRunKind
    route: EvalRoute
    status: EvalStatus
    passed: bool | None = Field(description="Whether it passed; null while it runs or when never gated")
    gate: GateState | None = Field(description="The state of the gate the run measured")
    spearman_rho: DecimalOut | None
    pairwise: DecimalOut | None = Field(description="The pairwise agreement of score and label order")
    contested_rate: DecimalOut | None
    failed_gates: list[str] = Field(description="The gates a failed run missed")
    cost_usd: DecimalOut
    started_at: UtcDatetime
    finished_at: UtcDatetime | None
    rubric_version_id: int | None
    prompt_schema_version_id: int | None


class ActiveGateOut(BaseModel):
    """The active configuration's eval gate and the number of the user's golden cases it counts."""

    state: GateState
    cases: int


class EvalRunsPageOut(BaseModel):
    items: list[EvalRunOut]
    next_cursor: str | None
    active_gate: ActiveGateOut


@router.get(
    "/api/eval-runs",
    response_model=EvalRunsPageOut,
    responses={400: {"description": "A cursor this list did not issue for these filters (`invalid_cursor`)"}},
)
async def list_eval_runs(
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    cfg: Annotated[AppConfig, Depends(get_config)],
    kind: Annotated[EvalRunKind, Query(description="The kind of run to list")],
    cursor: Annotated[
        str | None, Query(max_length=2048, description="`next_cursor` of the page before")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> EvalRunsPageOut:
    """The eval runs of one kind, newest first, keyset-paged (§14.3), with the active configuration's gate."""
    key = cursor_key(cfg)
    filters: dict[str, JSONValue] = {"kind": kind}
    after = (
        None
        if cursor is None
        else decode_id_cursor(cursor, key=key, sort=EVAL_RUNS_SORT, dir="desc", filters=filters)
    )
    async with read_snapshot(engine) as conn:
        rows = await eval_runs.list_eval_runs(conn, kind=kind, after_id=after, limit=limit + 1)
        gate = await eval_runs.active_gate(conn)
    items, next_cursor = list_page(
        rows,
        limit=limit,
        last_key=lambda run: [run.id],
        sort=EVAL_RUNS_SORT,
        dir="desc",
        filters=filters,
        key=key,
    )
    return EvalRunsPageOut(
        items=[EvalRunOut.model_validate(run, from_attributes=True) for run in items],
        next_cursor=next_cursor,
        active_gate=ActiveGateOut(state=gate.state, cases=gate.cases),
    )
