"""`GET /api/system/spend`: the LLM spend of the current UTC month (spec §6.9, §14.2, §17.5; plan Task 16).

Read without any lock, so it never waits for a reservation or a settings save: the active settings and their
evaluation policy by a plain `SELECT`, the month row through `spend_ledger.read_month` (accrued in memory; a
month without a row is shown as a reservation would create it, `read_month_or_new`), and the totals from the
ledger (`spend_totals`, `spend_by_model_effort_role`).

- `counted`, `cap` and `headroom` (the scoring headroom, §6.9.4) are the month row's; `billed`, `ambiguous`
  and `reserved` (in flight) sum the ledger's tries;
- `bucket` is the USD bucket (§6.9.2) at now, its capacity and its accrual per hour, on the row's cap and
  buffer; a cap at or below the buffer (`A ≤ 0`) accrues nothing, so the rate is never negative, as in the
  dry-run's projection;
- `c_hat` is `scoring.cycles.c_hat` of the active tuple and evaluation policy (the trailing 7-day mean cost
  of its published online attempts from 20 of them on, else §21.2's central cost per target of the active
  model policy; the central one without a tuple), `c_hat_source` which of the two (`history`, `central`);
  `projected_critic_rate` is the active evaluation policy's (M3 Task 21);
- `critic_rate_7d` is `cycles.critic_rate_7d` of the same (`null` below 50 attempts, and without a tuple);
- `bulk_operation` is the active tuple's newest bulk operation (`{id, state, budget_usd, spent_usd,
  followup_held_usd, llm_tuple_id}`; `null` without one); `quarantined_batch_usd` is the reserved spend of
  the `submit_ambiguous` batch submissions (`scoring.batches.quarantined_usd`, S:L1199, M3 Task 17);
- `pause_reason` is why the score lane admits nothing now (`cycles.pause_reason`, in this process's LLM mode),
  or `null`, as the health's score section reports it;
- `by_model_effort_role` groups the month's tries outside eval runs by model, effort and role (the call's
  purpose); `eval_by_model_role` the eval runs' tries (golden, canary and schema dry-run) by model and role,
  from the ledger alone (Carry T19-d): they are non-scoring and never join the live scoring rows, and a
  deleted golden case's call rows go with it by cascade while its ledger entries still count.

USD amounts are decimal strings at $0.000001, the cap at its cents. `utcnow` is a name of this module, so a
test injects the clock by patching `jobhunter.api.routes.spend.utcnow`.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.api.deps import get_config, get_engine, require_session
from jobhunter.api.models import DecimalOut, usd_micro
from jobhunter.api.openapi import SESSION_RESPONSES
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.db.sql import q
from jobhunter.llm import budget
from jobhunter.llm.budget import BudgetParams, Purpose
from jobhunter.llm.spend_ledger import (
    eval_spend_by_model_role,
    read_month_or_new,
    spend_by_model_effort_role,
    spend_totals,
)
from jobhunter.scoring import batches, cycles
from jobhunter.scoring.cycles import PauseReason
from jobhunter.settings.llm_projection import central_cost_per_target
from jobhunter.settings.model import Settings

router = APIRouter(prefix="/api/system", dependencies=[Depends(require_session)], responses=SESSION_RESPONSES)

_ACTIVE: Final = q(
    "SELECT v.payload, e.projected_critic_rate FROM active_config a "
    "JOIN settings_versions v ON v.id = a.settings_version_id "
    "JOIN evaluation_policies e ON e.id = a.evaluation_policy_id WHERE a.id = 1"
)
_BULK_OPERATION: Final = q(
    "SELECT id, state, budget_usd, spent_usd, followup_held_usd, llm_tuple_id FROM bulk_operations "
    "WHERE llm_tuple_id = :t ORDER BY id DESC LIMIT 1"
)


class BucketOut(BaseModel):
    usd: DecimalOut
    capacity: DecimalOut
    rate_per_h: DecimalOut


class SpendGroupOut(BaseModel):
    """The month's tries outside eval runs of one model, effort and role (`effort` is `null` for a try whose
    call row was purged), and what they count; the eval runs' tries are `eval_by_model_role`'s."""

    model: str
    effort: str | None
    role: Purpose
    calls: int
    counted: DecimalOut


class EvalSpendGroupOut(BaseModel):
    """The month's tries of eval runs (golden, canary, schema dry-run) of one model and role, and what they
    count."""

    model: str
    role: Purpose
    calls: int
    counted: DecimalOut


class BulkOperationOut(BaseModel):
    """The active tuple's newest bulk operation (§6.9.3): its fixed budget, its counted spend and the
    follow-up holds of its batched items."""

    id: int
    state: Literal["active", "completed", "cancelled"]
    budget_usd: DecimalOut
    spent_usd: DecimalOut
    followup_held_usd: DecimalOut
    llm_tuple_id: int


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
    c_hat_source: Literal["history", "central"]
    critic_rate_7d: DecimalOut | None
    projected_critic_rate: DecimalOut
    bulk_operation: BulkOperationOut | None
    quarantined_batch_usd: DecimalOut
    pause_reason: PauseReason | None
    by_model_effort_role: list[SpendGroupOut] = Field(
        description="The month's tries outside eval runs by model, effort and role; the eval runs' tries "
        "(golden, canary, schema dry-run) are excluded and counted in `eval_by_model_role`"
    )
    eval_by_model_role: list[EvalSpendGroupOut] = Field(
        description="The month's tries of eval runs by model and role, from the ledger alone"
    )


async def _bulk_operation(conn: AsyncConnection, llm_tuple_id: int | None) -> BulkOperationOut | None:
    if llm_tuple_id is None:
        return None
    row = (await conn.execute(_BULK_OPERATION, {"t": llm_tuple_id})).first()
    return None if row is None else BulkOperationOut.model_validate(row, from_attributes=True)


async def _spend(conn: AsyncConnection, *, fake_llm: bool) -> SpendOut:
    now = utcnow()
    active = (await conn.execute(_ACTIVE)).one()
    settings = Settings.model_validate(active.payload)
    config = await cycles.scoring_config(conn, lock=False)
    tuple_id = config.active.llm_tuple_id
    p = BudgetParams.from_payload(settings.to_payload())
    month = budget.month_of(now)
    row = await read_month_or_new(conn, month, p, now=now)
    on_row = budget.row_params(row, p)
    totals = await spend_totals(conn, month)
    groups = await spend_by_model_effort_role(conn, month)
    c_hat: tuple[Decimal, Literal["history", "central"]] = (
        central_cost_per_target(config.model_policy),
        "central",
    )
    critic_rate_7d: Decimal | None = None
    if tuple_id is not None:
        policy = config.active.evaluation_policy_id
        c_hat = await cycles.c_hat(
            conn,
            llm_tuple_id=tuple_id,
            evaluation_policy_id=policy,
            model_policy=config.model_policy,
            now=now,
        )
        critic_rate_7d, _ = await cycles.critic_rate_7d(
            conn, llm_tuple_id=tuple_id, evaluation_policy_id=policy, now=now
        )
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
            rate_per_h=budget.rate_per_h(on_row, month),
        ),
        c_hat=c_hat[0],
        c_hat_source=c_hat[1],
        critic_rate_7d=critic_rate_7d,
        projected_critic_rate=Decimal(active.projected_critic_rate),
        bulk_operation=await _bulk_operation(conn, tuple_id),
        quarantined_batch_usd=usd_micro(await batches.quarantined_usd(conn)),
        pause_reason=await cycles.pause_reason(conn, fake_llm=fake_llm, now=now, config=config),
        by_model_effort_role=[
            SpendGroupOut(
                model=g.model, effort=g.effort, role=g.role, calls=g.calls, counted=usd_micro(g.counted_usd)
            )
            for g in groups
        ],
        eval_by_model_role=[
            EvalSpendGroupOut(model=g.model, role=g.role, calls=g.calls, counted=usd_micro(g.counted_usd))
            for g in await eval_spend_by_model_role(conn, month)
        ],
    )


@router.get("/spend", response_model=SpendOut)
async def get_spend(
    engine: Annotated[AsyncEngine, Depends(get_engine)], cfg: Annotated[AppConfig, Depends(get_config)]
) -> SpendOut:
    """The LLM spend of the current month: counted, billed, ambiguous and in flight, the cap and headroom,
    the USD bucket, `ĉ`, the critic rates, the bulk operation, the quarantined batch spend, the score lane's
    pause, and the spend per model, effort and role, the eval runs' apart."""
    async with engine.connect() as conn:  # read-only: rolled back when the block ends
        return await _spend(conn, fake_llm=cfg.fake_llm)
