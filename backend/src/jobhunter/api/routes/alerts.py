"""`POST /api/alerts/{id}/ack` (spec §14.2 L5030, §17.6; M3 Task 21, R10, Deviation 19, Ruling P13).

→ 200 `{id, acknowledged_at}`: `alerts.acknowledge_alert` in one transaction, a plain `UPDATE` of the alert
row (no `Idempotency-Key`: §14.1 L4963 does not list it, and a repeat changes nothing). The alert stays
open; its first acknowledgement is kept, so a repeat answers the same time. An acknowledged
`llm.reservation_bound_violated` or `llm.auth_failed` no longer pauses the score lane
(`scoring.cycles.pause_reason`), so the next cycle starts. A resolved or unknown alert is 404 `not_found`.
`alert_acknowledged` is logged with the id after the commit.

`utcnow` is a name of this module, so a test injects the clock by patching
`jobhunter.api.routes.alerts.utcnow`.
"""

from datetime import UTC, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Path
from pydantic import AfterValidator, BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.api.deps import get_engine, require_session
from jobhunter.api.errors import Problem
from jobhunter.api.openapi import CSRF_RESPONSES, SESSION_RESPONSES
from jobhunter.clock import utcnow
from jobhunter.observability.alerts import acknowledge_alert
from jobhunter.observability.logging import get_logger

router = APIRouter(prefix="/api/alerts", dependencies=[Depends(require_session)], responses=SESSION_RESPONSES)
log = get_logger("jobhunter.api.alerts")

_BIGINT_MAX: Final = 2**63 - 1


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


class AcknowledgedOut(BaseModel):
    id: int
    acknowledged_at: Annotated[datetime, AfterValidator(_utc)]


AlertId = Annotated[int, Path(ge=1, le=_BIGINT_MAX, description="The id of an alert")]


@router.post(
    "/{alert_id}/ack",
    response_model=AcknowledgedOut,
    responses={
        404: {"description": "No open alert with this id: it does not exist or is resolved (`not_found`)"},
        422: {"description": "Invalid path (`validation_failed`)"},
        **CSRF_RESPONSES,
    },
)
async def acknowledge(
    alert_id: AlertId, engine: Annotated[AsyncEngine, Depends(get_engine)]
) -> AcknowledgedOut:
    """Acknowledge an open alert; it stays open (200)."""
    async with engine.begin() as conn:
        acknowledged_at = await acknowledge_alert(conn, alert_id, now=utcnow())
    if acknowledged_at is None:
        raise Problem(404, "not_found", "Open alert not found")
    log.info("alert_acknowledged", alert_id=alert_id)
    return AcknowledgedOut(id=alert_id, acknowledged_at=acknowledged_at)
