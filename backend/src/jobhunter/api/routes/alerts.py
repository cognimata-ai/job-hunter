"""The alert list and the acknowledgement (spec §14.2 L5047-5048, §17.6; M3 Task 21, R10, Deviation 19, Ruling
P13; plan M4 Task 10, D27).

`GET /api/alerts?status&cursor&limit` → `{items, next_cursor}` (`observability.lists.alerts_page`): the alerts
by id descending, read in one read snapshot (`db.snapshot.read_snapshot`), each with its derived status
(`open`, `acknowledged` or `resolved`, D27), only those of `status` when it is given (an unknown one is 422).
Keyset-paged with §14.3's signed cursor (`api.pagination`: sort `alerts.id`, `desc`, `last_key = [id]`, the
filters `{"status": …}` when given), limit 1 to 100 (default 50); a cursor this list did not sign for these
filters is 400 `invalid_cursor`. A client shows an alert by its code (`alert.<code>`), never by its `message`.

`POST /api/alerts/{id}/ack` → 200 `{id, acknowledged_at}`: `alerts.acknowledge_alert` in one transaction, a
plain `UPDATE` of the alert row (no `Idempotency-Key`: §14.1 L4963 does not list it, and a repeat changes
nothing). The alert stays open; its first acknowledgement is kept, so a repeat answers the same time. An
acknowledged `llm.reservation_bound_violated` or `llm.auth_failed` no longer pauses the score lane
(`scoring.cycles.pause_reason`), so the next cycle starts. A resolved or unknown alert is 404 `not_found`.
`alert_acknowledged` is logged with the id after the commit.

`utcnow` is a name of this module, so a test injects the clock by patching
`jobhunter.api.routes.alerts.utcnow`.
"""

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.api.deps import get_config, get_engine, require_session
from jobhunter.api.errors import Problem
from jobhunter.api.models import UtcDatetime
from jobhunter.api.openapi import CSRF_RESPONSES, SESSION_RESPONSES
from jobhunter.api.pagination import ALERTS_SORT, cursor_key, decode_id_cursor, list_page
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.db.snapshot import read_snapshot
from jobhunter.observability import lists
from jobhunter.observability.alerts import acknowledge_alert
from jobhunter.observability.lists import AlertsPageOut, AlertStatus
from jobhunter.observability.logging import get_logger
from jobhunter.types import JSONValue

router = APIRouter(prefix="/api/alerts", dependencies=[Depends(require_session)], responses=SESSION_RESPONSES)
log = get_logger("jobhunter.api.alerts")

_BIGINT_MAX: Final = 2**63 - 1
DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 100


class AcknowledgedOut(BaseModel):
    id: int
    acknowledged_at: UtcDatetime


AlertId = Annotated[int, Path(ge=1, le=_BIGINT_MAX, description="The id of an alert")]


def _filters(status: AlertStatus | None) -> dict[str, JSONValue]:
    return {} if status is None else {"status": status}


@router.get(
    "",
    response_model=AlertsPageOut,
    responses={400: {"description": "A cursor this list did not issue for these filters (`invalid_cursor`)"}},
)
async def list_alerts(
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    cfg: Annotated[AppConfig, Depends(get_config)],
    status: Annotated[AlertStatus | None, Query(description="Only the alerts of this status")] = None,
    cursor: Annotated[
        str | None, Query(max_length=2048, description="`next_cursor` of the page before")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> AlertsPageOut:
    """The alerts, newest first, with their status, keyset-paged (§14.3)."""
    key = cursor_key(cfg)
    filters = _filters(status)
    after = (
        None
        if cursor is None
        else decode_id_cursor(cursor, key=key, sort=ALERTS_SORT, dir="desc", filters=filters)
    )
    async with read_snapshot(engine) as conn:
        rows = await lists.alerts_page(conn, after_id=after, status=status, limit=limit + 1)
    items, next_cursor = list_page(
        rows,
        limit=limit,
        last_key=lambda alert: [alert.id],
        sort=ALERTS_SORT,
        dir="desc",
        filters=filters,
        key=key,
    )
    return AlertsPageOut(items=items, next_cursor=next_cursor)


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
