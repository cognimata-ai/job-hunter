"""`/api/settings` (spec §6.8, §13.5, §14.2; plan Task 16: the dry-run; Task 17 adds the save and the reads).

`POST /api/settings/dry-run` (`{payload}`) → 200 `{errors, warnings, projections, effects}`: the preview of a
proposed settings document against the active one (`settings.dryrun.dry_run`). Errors are part of the
preview and come with a 200; the save answers them with a 422. The dry-run runs in a read-only transaction
(`SET TRANSACTION READ ONLY`, the transaction's first statement), where PostgreSQL refuses any write and any
`SELECT … FOR UPDATE | SHARE` (SQLSTATE 25006): it writes nothing and takes no row lock, so a preview never
waits for a settings save or a reservation in progress. `effects` are sorted by name.

`utcnow` is a name of this module, so a test injects the clock by patching
`jobhunter.api.routes.settings.utcnow`. The log line holds counts, warning codes and effect classes only,
never a value of the payload.
"""

from typing import Annotated, Final

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.api.deps import get_engine, require_session
from jobhunter.api.openapi import CSRF_RESPONSES, SESSION_RESPONSES
from jobhunter.clock import utcnow
from jobhunter.db.sql import q
from jobhunter.observability.logging import get_logger
from jobhunter.settings.dryrun import active_settings, dry_run
from jobhunter.settings.effects import EffectClass
from jobhunter.settings.validate import IssueCode

router = APIRouter(
    prefix="/api/settings", dependencies=[Depends(require_session)], responses=SESSION_RESPONSES
)
log = get_logger("jobhunter.api.settings")

_READ_ONLY: Final = q("SET TRANSACTION READ ONLY")
_NEVER: Final = (
    "null means never: with no Adzuna allowance (a monthly cap of 1 leaves a bucket of 0 units) no cell is "
    "ever refreshed"
)


class DryRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: JsonValue


class FieldIssueOut(BaseModel):
    """A refused field (`settings.validate.FieldIssue`): its path from `payload`, a code and a message that
    never repeats the value."""

    field: str
    code: IssueCode
    message: str


class WarningOut(BaseModel):
    """A warning a save must acknowledge by its `code` (§13.2), with its details."""

    code: str
    detail: dict[str, JsonValue] = Field(
        description=f"The warning's details. In `adzuna_rotation`, `cadence_days` is days or null; {_NEVER}."
    )


class DryRunOut(BaseModel):
    errors: list[FieldIssueOut]
    warnings: list[WarningOut]
    projections: dict[str, JsonValue] = Field(
        description=f"The preview by part (§13.5). In `adzuna`, `cadence_days` is days or null; {_NEVER}."
    )
    effects: list[EffectClass]


@router.post(
    "/dry-run",
    response_model=DryRunOut,
    responses={422: {"description": "Invalid body (`validation_failed`)"}, **CSRF_RESPONSES},
)
async def settings_dry_run(body: DryRunIn, engine: Annotated[AsyncEngine, Depends(get_engine)]) -> DryRunOut:
    """Validate and preview a proposed settings document: its errors, warnings, projections and effects."""
    async with engine.connect() as conn:  # rolled back when the block ends
        await conn.execute(_READ_ONLY)
        result = await dry_run(conn, body.payload, active=await active_settings(conn), now=utcnow())
    log.info(
        "settings_dry_run",
        errors=len(result.errors),
        warnings=[w.code for w in result.warnings],
        effects=sorted(result.effects),
    )
    return DryRunOut(
        errors=[FieldIssueOut(field=e.field, code=e.code, message=e.message) for e in result.errors],
        warnings=[WarningOut(code=w.code, detail=dict(w.detail)) for w in result.warnings],
        projections=result.projections,
        effects=sorted(result.effects),
    )
