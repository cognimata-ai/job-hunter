"""`GET /api/system/spend`: the LLM spend of the current UTC month (spec §6.9, §14.2, §17.5; plan Task 16).

Read without any lock, so it never waits for a reservation or a settings save: the active settings and their
evaluation policy by a plain `SELECT`, the month row through `spend_ledger.read_month` (accrued in memory; a
month without a row is shown as a reservation would create it, `read_month_or_new`), and the totals from the
ledger (`spend_totals`, `spend_by_model_effort_role`).

- `counted`, `cap` and `headroom` (the scoring headroom, §6.9.4) are the month row's; `billed`, `ambiguous`
  and `reserved` (in flight) sum the ledger's tries;
- `bucket` is the USD bucket (§6.9.2) at now, its capacity and its accrual per hour, on the row's cap and
  buffer;
- `c_hat` is §21.2's central cost per target of the active model policy (no attempt history before M3), and
  `projected_critic_rate` the active evaluation policy's;
- `critic_rate_7d` and `bulk_operation` are `null` and `quarantined_batch_usd` is `"0"` until M3 scores
  targets, opens bulk operations and submits batches;
- `by_model_effort_role` groups the month's tries by model, effort and role (the call's purpose).

USD amounts are decimal strings at $0.000001, the cap at its cents. `utcnow` is a name of this module, so a
test injects the clock by patching `jobhunter.api.routes.spend.utcnow`.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated, Final

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.api.deps import get_engine, require_session
from jobhunter.api.openapi import SESSION_RESPONSES
from jobhunter.clock import utcnow
from jobhunter.db.sql import q
from jobhunter.llm import budget
from jobhunter.llm.budget import BudgetParams, Purpose
from jobhunter.llm.spend_ledger import read_month_or_new, spend_by_model_effort_role, spend_totals
from jobhunter.observability.health import DecimalOut, usd_micro
from jobhunter.settings.llm_projection import central_cost_per_target
from jobhunter.settings.model import Settings

router = APIRouter(prefix="/api/system", dependencies=[Depends(require_session)], responses=SESSION_RESPONSES)

_SECONDS_PER_HOUR: Final = 3600
_ACTIVE: Final = q(
    "SELECT v.payload, e.projected_critic_rate FROM active_config a "
    "JOIN settings_versions v ON v.id = a.settings_version_id "
    "JOIN evaluation_policies e ON e.id = a.evaluation_policy_id WHERE a.id = 1"
)


class BucketOut(BaseModel):
    usd: DecimalOut
    capacity: DecimalOut
    rate_per_h: DecimalOut


class SpendGroupOut(BaseModel):
    """The month's tries of one model, effort and role (`effort` is `null` for a try whose call row was
    purged), and what they count."""

    model: str
    effort: str | None
    role: Purpose
    calls: int
    counted: DecimalOut


class SpendOut(BaseModel):
    month: date
    counted: DecimalOut
    billed: DecimalOut
    ambiguous: DecimalOut
    reserved: DecimalOut
    cap: DecimalOut
    headroom: DecimalOut
    bucket: BucketOut
    c_hat: DecimalOut
    critic_rate_7d: DecimalOut | None
    projected_critic_rate: DecimalOut
    bulk_operation: None
    quarantined_batch_usd: DecimalOut
    by_model_effort_role: list[SpendGroupOut]


async def _spend(conn: AsyncConnection) -> SpendOut:
    now = utcnow()
    active = (await conn.execute(_ACTIVE)).one()
    settings = Settings.model_validate(active.payload)
    p = BudgetParams.from_payload(settings.to_payload())
    month = budget.month_of(now)
    row = await read_month_or_new(conn, month, p, now=now)
    on_row = budget.row_params(row, p)
    totals = await spend_totals(conn, month)
    groups = await spend_by_model_effort_role(conn, month)
    return SpendOut(
        month=month,
        counted=usd_micro(row.counted_usd),
        billed=usd_micro(totals.billed),
        ambiguous=usd_micro(totals.ambiguous),
        reserved=usd_micro(totals.reserved),
        cap=row.cap_usd,
        headroom=usd_micro(budget.headroom(row, "scoring", p)),
        bucket=BucketOut(
            usd=usd_micro(row.bucket_usd),
            capacity=usd_micro(budget.bucket_capacity(on_row, month)),
            rate_per_h=usd_micro(budget.rate_per_s(on_row, month) * _SECONDS_PER_HOUR),
        ),
        c_hat=central_cost_per_target(settings.scoring.model_policy),
        critic_rate_7d=None,
        projected_critic_rate=Decimal(active.projected_critic_rate),
        bulk_operation=None,
        quarantined_batch_usd=Decimal(0),
        by_model_effort_role=[
            SpendGroupOut(
                model=g.model, effort=g.effort, role=g.role, calls=g.calls, counted=usd_micro(g.counted_usd)
            )
            for g in groups
        ],
    )


@router.get("/spend", response_model=SpendOut)
async def get_spend(engine: Annotated[AsyncEngine, Depends(get_engine)]) -> SpendOut:
    """The LLM spend of the current month: counted, billed, ambiguous and in flight, the cap and headroom,
    the USD bucket, `ĉ`, the projected critic rate, and the spend per model, effort and role."""
    async with engine.connect() as conn:  # read-only: rolled back when the block ends
        return await _spend(conn)
