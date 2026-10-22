"""The re-score estimate of a profile confirm (spec §12.6 step 2, §6.9.3, §21.4; plan Task 21).

A confirm activates a new tuple, whose targets M3's `create_targets_for_tuple` creates for every retained
current revision (Deviation 10). Before anything is spent the user sees what that costs:

- `targets`: the current revisions of retained jobs (`dryrun.count_retained_current_revisions`);
- `est_usd_online = targets x ĉ`, ĉ the §21.2 central cost per target of the active model policy
  (`llm_projection.central_cost_per_target`), the quantity §6.8's `llm_rescore` warning calls `est_usd`;
- `est_usd_batch = targets x central_batch_cost_per_target`, §21.4's batch path: the generator call in Batches
  at 50% of every price with a **cold cache** (the batch params keep the prefix cache marker, so the prefix is
  written, not read), plus the online follow-ups of §21.2's funnel (the critic rounds and the revisions) and
  the 1% structural repairs of the online generator call. For preset B that is $0.016325 + $0.024942 +
  $0.005180 + $0.000223 = $0.046671 (§21.4: "$0.0467 with the 1% repair allowance");
- `month_spend` and `cap`: the month row's `counted_usd` and `cap_usd`, read without a lock
  (`spend_ledger.read_month_or_new`: a month without a row is shown as a reservation would create it);
- `bulk_budget_usd = bulk_rescore_max_budget_share x (A x (1 - reserve_frac) - scoring_counted_usd)` (§6.9.3,
  `dryrun.bulk_budget`), with `A` the month row's cap less its buffer (`budget.row_params`, what admission on
  the row uses): $37.24 at the start of a month with the seeded settings (§21.4).

Every amount is rounded half-up to $0.000001; the unit costs are rounded before they are multiplied, as the
dry-run's `llm_rescore` warning multiplies ĉ. `estimate_for` is the pure part; `rescore_estimate` reads the
count and the month row on the caller's connection, and writes nothing.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.llm import budget, pricing
from jobhunter.llm.budget import BudgetParams, BudgetRow
from jobhunter.llm.spend_ledger import read_month_or_new
from jobhunter.settings.dryrun import bulk_budget, count_retained_current_revisions
from jobhunter.settings.llm_projection import GENERATE, central_cost_per_target, target_cost, uplift
from jobhunter.settings.model import ModelPolicy, Settings

_TOKENS_PER_MTOK: Final = 1_000_000


@dataclass(frozen=True)
class RescoreEstimate:
    """The module docstring's figures; USD amounts at $0.000001, `cap` at the month row's cents."""

    targets: int
    est_usd_online: Decimal
    est_usd_batch: Decimal
    month_spend: Decimal
    cap: Decimal
    bulk_budget_usd: Decimal


def _usd(amount: Decimal) -> Decimal:
    return amount.quantize(budget.USD_QUANTUM, rounding=ROUND_HALF_UP)


def _cold_batch_generator_usd(model_policy: ModelPolicy) -> Decimal:
    """The generator call as a batch item with a cold cache: the uncached input at the batch input price, the
    prefix at the batch cache-write price, the output at the batch output price (not rounded)."""
    model = model_policy.nodes.generate.model
    factor = uplift(model)
    price = pricing.price_for(
        model, prompt_tokens=int(factor * (GENERATE.uncached + GENERATE.cached)), batch=True
    )
    micro_usd = factor * (
        GENERATE.uncached * price.input + GENERATE.cached * price.cache_write + GENERATE.output * price.output
    )
    return micro_usd / _TOKENS_PER_MTOK


def central_batch_cost_per_target(model_policy: ModelPolicy) -> Decimal:
    """§21.4's batch path for one target under `model_policy` (module docstring), rounded half-up to
    $0.000001: $0.046671 for preset B."""
    online = target_cost(model_policy)
    return _usd(_cold_batch_generator_usd(model_policy) + online.critic + online.revise + online.repairs)


def estimate_for(settings: Settings, *, targets: int, row: BudgetRow) -> RescoreEstimate:
    """The estimate of `targets` re-scores under `settings`, with the month's spend as `row` holds it."""
    if targets < 0:
        raise ValueError(f"targets must not be negative, got {targets}")
    policy = settings.scoring.model_policy
    on_row = budget.row_params(row, BudgetParams.from_payload(settings.to_payload()))
    return RescoreEstimate(
        targets=targets,
        est_usd_online=_usd(targets * central_cost_per_target(policy)),
        est_usd_batch=_usd(targets * central_batch_cost_per_target(policy)),
        month_spend=_usd(row.counted_usd),
        cap=row.cap_usd,
        bulk_budget_usd=_usd(bulk_budget(settings, on_row, row)),
    )


async def rescore_estimate(conn: AsyncConnection, settings: Settings, *, now: datetime) -> RescoreEstimate:
    """The estimate for the retained current revisions under `settings` in the month of `now`: plain reads
    (no lock), nothing written."""
    p = BudgetParams.from_payload(settings.to_payload())
    row = await read_month_or_new(conn, budget.month_of(now), p, now=now)
    return estimate_for(settings, targets=await count_retained_current_revisions(conn), row=row)
