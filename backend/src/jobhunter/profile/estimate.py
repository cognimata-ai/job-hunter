"""The re-score estimate of a profile confirm (spec §12.6 step 2, §6.9.3, §15.7's confirm modal, §21.3, §21.4;
plan Task 21, M4 Task 15), and what one call of each profile node costs.

A confirm activates a new tuple, whose targets its `create_targets_for_tuple` operation creates for every
retained current revision (M3 Task 14). Before anything is spent the user sees what that costs:

- `targets`: the current revisions of retained jobs (`dryrun.count_retained_current_revisions`), the ones
  the operation creates targets for (`targets_to_create`, M2 Deviation 42);
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
  the row uses): $37.24 at the start of a month with the seeded settings (§21.4). The confirm's `bulk.open`
  fixes the operation's budget with the same function on the locked row. `bulk_share` is that setting;
- `batch_targets = batch_capacity(...)`, the ads of one batch submission (§6.9.3 L1188-1194): a submission of
  `n` items is admitted when its items' worst cases plus their follow-up holds fit the allocation,
  `n x (worst + hold) ≤ bulk_budget_usd`; the largest such `n ≤ min(targets, batch_max_items)`, or 0 with
  `batch_bulk_rescore` off or `n < batch_min_targets`. `worst` is `batch_item_worst_usd`, the batch analogue
  of `budget.r_first`: the generate model's worst case over the §21.4 sizing bound
  (`budget.sizing_input_bound`) and the first stage's `max_tokens`, at the batch prices ($0.077098 for preset
  B, §21.4's $0.0771); `hold` is `bulk.hold_per_item`, `projected_critic_rate x ĉ_followup` ($0.030123 with
  the seeded policy, §21.4's $0.0301). So $22.44 holds 209 items and a month start's $37.24 holds 347,
  §21.4's two cases. The real submission (`scoring.batches`) also stops at the request bytes and the month's
  scoring headroom, which the estimate does not foresee;
- `online_targets = targets - batch_targets`, scored online at the bucket's pace:
  `online_per_day = floor(budget.rate_per_h x 24 / llm_projection.paced_cost_per_target(ĉ))`, the bucket's
  accrual a day on the month row's cap and buffer over `ĉ` with a target's share of its cycle's cache writes
  (§21.3: "about 53 targets per day at the central cost including cache writes"). `ĉ` is `cycles.c_hat` of
  the active tuple and evaluation policy under the active model policy (a profile change keeps the model
  policy), the central estimate without a tuple. With the seeded settings and the central `ĉ` that is
  $3.103344 / $0.057569 = 53 a day in a month of 30 days (§15.7: "about 53 per day"), 52 in one of 31;
- `next_version_no`: one more than the newest of the user's profile versions (the adversarial seed's synthetic
  ones are not counted, as the confirm numbers a new version).

Every amount is rounded half-up to $0.000001; the unit costs are rounded before they are multiplied, as the
dry-run's `llm_rescore` warning multiplies ĉ. `estimate_for` is the pure part, given ĉ and the hold;
`rescore_estimate` reads the count, the month row, the active pointers with the next version number, the hold
and ĉ on the caller's connection, plain reads, and writes nothing.

`node_call_usd(settings, node)` is one call of `extract_profile` or `propose_queries`: §21.1's token row of
the node (`llm_projection.EXTRACT_PROFILE`, `PROPOSE_QUERIES`) priced by `llm_projection.call_usd` (the
tokenizer uplift included) on the node's model in the settings' model policy, rounded half-up: $0.140000 and
$0.094000 under preset B (§21.3: "≈ $0.14 per CV", "≈ $0.09 each").
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from types import MappingProxyType
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.llm import budget, bulk, pricing
from jobhunter.llm.budget import BudgetParams, BudgetRow
from jobhunter.llm.spend_ledger import read_month_or_new
from jobhunter.scoring import cycles
from jobhunter.settings.dryrun import bulk_budget, count_retained_current_revisions
from jobhunter.settings.llm_projection import (
    EXTRACT_PROFILE,
    GENERATE,
    PROPOSE_QUERIES,
    CallTokens,
    call_usd,
    central_cost_per_target,
    paced_cost_per_target,
    target_cost,
    uplift,
)
from jobhunter.settings.model import ModelPolicy, Settings

ProfileNode = Literal["extract_profile", "propose_queries"]

_TOKENS_PER_MTOK: Final = 1_000_000
_HOURS_PER_DAY: Final = 24
_NODE_TOKENS: Final[Mapping[ProfileNode, CallTokens]] = MappingProxyType(
    {"extract_profile": EXTRACT_PROFILE, "propose_queries": PROPOSE_QUERIES}
)
# The active pointers ĉ and the hold are read for, and the number a confirm gives a new version: the user's
# versions only (a synthetic one of the adversarial seed is not counted, as the confirm's own numbering).
_ACTIVE: Final = q(
    "SELECT a.llm_tuple_id, a.evaluation_policy_id, (SELECT coalesce(max(v.version_no), 0) + 1 "
    "FROM profile_versions v WHERE NOT v.synthetic_eval) AS next_version_no "
    "FROM active_config a WHERE a.id = 1"
)


@dataclass(frozen=True)
class RescoreEstimate:
    """The module docstring's figures; USD amounts at $0.000001, `cap` at the month row's cents."""

    targets: int
    est_usd_online: Decimal
    est_usd_batch: Decimal
    month_spend: Decimal
    cap: Decimal
    bulk_budget_usd: Decimal
    bulk_share: Decimal
    batch_targets: int
    online_targets: int
    online_per_day: int
    next_version_no: int


def _usd(amount: Decimal) -> Decimal:
    return amount.quantize(budget.USD_QUANTUM, rounding=ROUND_HALF_UP)


def _floor(amount: Decimal) -> int:
    return int(amount.to_integral_value(rounding=ROUND_FLOOR))


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


def batch_item_worst_usd(settings: Settings) -> Decimal:
    """The worst-case reservation of one batch generator item under `settings` (module docstring), rounded up
    to $0.000001: $0.077098 for preset B."""
    scoring = settings.scoring
    model = scoring.model_policy.nodes.generate.model
    return pricing.worst_case_usd(
        model,
        input_tokens_bound=budget.sizing_input_bound(model),
        max_tokens=scoring.execution.generate.max_tokens,
        batch=True,
    )


def batch_capacity(
    settings: Settings, *, targets: int, bulk_budget_usd: Decimal, hold_per_item_usd: Decimal
) -> int:
    """The largest batch of `targets` that §6.9.3 admits under `bulk_budget_usd`, each item at its worst case
    plus `hold_per_item_usd` (`bulk.hold_per_item`), at most `batch_max_items`; 0 with batches off or below
    `batch_min_targets` (module docstring)."""
    if targets < 0 or bulk_budget_usd < 0 or hold_per_item_usd < 0:
        raise ValueError("targets, the bulk budget and the hold must not be negative")
    scoring = settings.scoring
    if not scoring.batch_bulk_rescore:
        return 0
    fits = _floor(bulk_budget_usd / (batch_item_worst_usd(settings) + hold_per_item_usd))
    n = min(targets, scoring.batch_max_items, fits)
    return n if n >= scoring.batch_min_targets else 0


def estimate_for(
    settings: Settings,
    *,
    targets: int,
    row: BudgetRow,
    c_hat: Decimal,
    hold_per_item_usd: Decimal,
    next_version_no: int,
) -> RescoreEstimate:
    """The estimate of `targets` re-scores under `settings`, with the month's spend as `row` holds it, `c_hat`
    the expected cost of an online target and `hold_per_item_usd` a batch item's follow-up hold."""
    if targets < 0:
        raise ValueError(f"targets must not be negative, got {targets}")
    scoring = settings.scoring
    policy = scoring.model_policy
    on_row = budget.row_params(row, BudgetParams.from_payload(settings.to_payload()))
    bulk_budget_usd = _usd(bulk_budget(settings, on_row, row))
    batch_targets = batch_capacity(
        settings, targets=targets, bulk_budget_usd=bulk_budget_usd, hold_per_item_usd=hold_per_item_usd
    )
    daily = budget.rate_per_h(on_row, row.month) * _HOURS_PER_DAY
    return RescoreEstimate(
        targets=targets,
        est_usd_online=_usd(targets * central_cost_per_target(policy)),
        est_usd_batch=_usd(targets * central_batch_cost_per_target(policy)),
        month_spend=_usd(row.counted_usd),
        cap=row.cap_usd,
        bulk_budget_usd=bulk_budget_usd,
        bulk_share=scoring.bulk_rescore_max_budget_share,
        batch_targets=batch_targets,
        online_targets=targets - batch_targets,
        online_per_day=_floor(daily / paced_cost_per_target(scoring, c_hat)),
        next_version_no=next_version_no,
    )


async def rescore_estimate(conn: AsyncConnection, settings: Settings, *, now: datetime) -> RescoreEstimate:
    """The estimate for the retained current revisions under `settings` in the month of `now` (module
    docstring): plain reads (no lock), nothing written. `LookupError` for an active evaluation policy that
    does not exist."""
    p = BudgetParams.from_payload(settings.to_payload())
    row = await read_month_or_new(conn, budget.month_of(now), p, now=now)
    active = (await conn.execute(_ACTIVE)).one()
    policy = settings.scoring.model_policy
    evaluation_policy_id = int(active.evaluation_policy_id)
    hold = await bulk.hold_per_item(conn, evaluation_policy_id=evaluation_policy_id, model_policy=policy)
    c_hat = central_cost_per_target(policy)
    if active.llm_tuple_id is not None:
        c_hat, _ = await cycles.c_hat(
            conn,
            llm_tuple_id=int(active.llm_tuple_id),
            evaluation_policy_id=evaluation_policy_id,
            model_policy=policy,
            now=now,
        )
    return estimate_for(
        settings,
        targets=await count_retained_current_revisions(conn),
        row=row,
        c_hat=c_hat,
        hold_per_item_usd=hold,
        next_version_no=int(active.next_version_no),
    )


def node_call_usd(settings: Settings, node: ProfileNode) -> Decimal:
    """One call of the profile node `node` under `settings`' model policy (module docstring), rounded half-up
    to $0.000001."""
    model = settings.scoring.model_policy.nodes.by_node()[node].model
    return _usd(call_usd(model, _NODE_TOKENS[node]))
