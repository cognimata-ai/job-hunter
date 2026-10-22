"""The LLM part of the settings dry-run (spec §6.8, §6.9.2, §13.5, §21; plan Task 16).

**`central_cost_per_target`** is the §21.1/§21.2 central estimate of one target's scoring spend under a model
policy: §21.1's token assumptions priced with `llm.pricing` (each prefix read from the cache; §21.3's cache
writes are added per cycle in `llm_projection`), under §21.1's critic funnel:
- one generator call (2,200 uncached, 4,500 cached, 1,700 out: the slim outputs of U14);
- a first critic round for `first_round_rate` (0.45) of the targets, at the policy's light critic effort for
  the `light_share` (35%) of rounds whose triggers are T2 and/or T8 alone, at the critic's effort otherwise
  (3,400 uncached, 4,500 cached; 800 out at `low`, 1,300 at any other effort);
- a revision for `q1` (0.35) of the reviewed drafts and a second one for `q2` (0.30) of those, each followed
  by a critic round at the critic's effort (`q1 + q1 x q2 = 0.455` per reviewed draft; 3,700 uncached, 4,500
  cached, 1,700 out);
- structural repairs at `repairs` (1%) of the generator's cost.

§21.1 states the generator's and the reviser's output at one effort only, and the critic's at `medium` and
`low`, so every other effort takes the `medium` figure. Two assumptions of §21.2 belong to a model, not to a
preset label: a Haiku node counts 30% more tokens (its tokenizer uplift), and a Haiku generator, "the weaker
generator", triggers more reviews (critic rate 0.55, `q1 = 0.45`, `q2 = 0.35`, §21.2's config C). The figure
is rounded half-up to $0.000001. It needs no input bound, so `PROMPT_OVERHEAD_TOKENS` plays no part. For the
default policy B it is $0.052646 (§21.2: $0.0526), for A $0.079255 ($0.0793) and for C $0.019352 ($0.0194).

**`llm_projection`** gives the §6.9.2 bucket of the settings in the month of `now` (rate per hour and
capacity), `ĉ`, the evaluation policy's projected critic rate, and §21.3's capacity of a month:
`targets_per_month = floor(scoring share / (ĉ + 0.04923 / 10))`, the scoring share `A x (1 - reserve_frac)`,
with §21.3's cache writes of config B ($0.04923 per cycle) over its ten-target cycles; `projected_month_spend`
is what the targets that arrive and fit cost with those cache writes. A cap at or below the overrun buffer
admits nothing (`A ≤ 0`): no target, no accrual. `cap_binding` is §6.8's `llm_cap_binding` condition: more
arrivals than capacity, or no capacity at all.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from types import MappingProxyType
from typing import Final

from jobhunter.llm import budget, pricing
from jobhunter.llm.budget import BudgetParams
from jobhunter.llm.pricing import Effort, Model
from jobhunter.settings.model import ModelPolicy, Settings

DEFAULT_ARRIVAL_PER_MONTH: Final = 3000  # §6.8: new targets a month when there is no target history
CACHE_WRITE_USD_PER_CYCLE: Final = Decimal("0.04923")  # §21.1/§21.3, config B
TARGETS_PER_CYCLE: Final = 10  # §21.1: "the model below assumes ten-target cycles"
_CACHE_WRITE_PER_TARGET: Final = CACHE_WRITE_USD_PER_CYCLE / TARGETS_PER_CYCLE
_SECONDS_PER_HOUR: Final = 3600
_TOKENS_PER_MTOK: Final = 1_000_000
_ZERO: Final = Decimal(0)


@dataclass(frozen=True)
class CallTokens:
    """One call's §21.1 token assumption: uncached input, the cached prefix (read) and the output."""

    uncached: int
    cached: int
    output: int


# §21.1, slim outputs (U14)
GENERATE: Final = CallTokens(2_200, 4_500, 1_700)
CRITIC: Final = CallTokens(3_400, 4_500, 1_300)
CRITIC_LOW: Final = CallTokens(3_400, 4_500, 800)
REVISE: Final = CallTokens(3_700, 4_500, 1_700)


@dataclass(frozen=True)
class Funnel:
    """The §21.1 critic funnel per target."""

    first_round_rate: Decimal
    light_share: Decimal
    q1: Decimal
    q2: Decimal
    repairs: Decimal


DEFAULT_FUNNEL: Final = Funnel(
    Decimal("0.45"), Decimal("0.35"), Decimal("0.35"), Decimal("0.30"), Decimal("0.01")
)
# §21.2, config C: "critic rate assumed 0.55 with q1 = 0.45, q2 = 0.35, because the weaker generator triggers
# more reviews"
WEAK_GENERATOR_FUNNEL: Final = Funnel(
    Decimal("0.55"), Decimal("0.35"), Decimal("0.45"), Decimal("0.35"), Decimal("0.01")
)
WEAK_GENERATORS: Final[frozenset[Model]] = frozenset({"claude-haiku-5-5"})
# §21.2, config C: "Haiku generator and reviser with a 30% tokenizer uplift"
TOKENIZER_UPLIFT: Final = MappingProxyType({"claude-haiku-5-5": Decimal("1.3")})


def _call_usd(model: Model, tokens: CallTokens) -> Decimal:
    """The cost of one call of `tokens` on `model`, the cached prefix read (not rounded)."""
    uplift = TOKENIZER_UPLIFT.get(model, Decimal(1))
    uncached, cached, output = (uplift * n for n in (tokens.uncached, tokens.cached, tokens.output))
    price = pricing.price_for(model, prompt_tokens=int(uncached + cached))
    micro_usd = uncached * price.input + cached * price.cache_read + output * price.output
    return micro_usd / _TOKENS_PER_MTOK


def _critic_tokens(effort: Effort) -> CallTokens:
    return CRITIC_LOW if effort == "low" else CRITIC


def central_cost_per_target(model_policy: ModelPolicy) -> Decimal:
    """The §21.2 central cost of one target under `model_policy` (module docstring), rounded half-up to
    $0.000001."""
    nodes = model_policy.nodes
    funnel = WEAK_GENERATOR_FUNNEL if nodes.generate.model in WEAK_GENERATORS else DEFAULT_FUNNEL
    generator = _call_usd(nodes.generate.model, GENERATE)
    full_critic = _call_usd(nodes.critic.model, _critic_tokens(nodes.critic.effort))
    light_critic = _call_usd(nodes.critic.model, _critic_tokens(model_policy.critic_light_effort))
    revision = _call_usd(nodes.revise.model, REVISE)
    follow_ups = funnel.q1 + funnel.q1 * funnel.q2  # revisions per reviewed draft, each re-reviewed
    first_round = funnel.light_share * light_critic + (1 - funnel.light_share) * full_critic
    critic = funnel.first_round_rate * (first_round + follow_ups * full_critic)
    revise = funnel.first_round_rate * follow_ups * revision
    total = generator + critic + revise + funnel.repairs * generator
    return total.quantize(budget.USD_QUANTUM, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class LlmProjection:
    """§13.5's LLM preview of a settings document; USD amounts at $0.000001."""

    bucket_rate_per_h: Decimal
    bucket_capacity: Decimal
    c_hat: Decimal
    projected_critic_rate: Decimal
    targets_per_month: int
    arrival_per_month: int
    projected_month_spend: Decimal


def _usd(amount: Decimal) -> Decimal:
    return amount.quantize(budget.USD_QUANTUM, rounding=ROUND_HALF_UP)


def scoring_share(p: BudgetParams) -> Decimal:
    """`A x (1 - reserve_frac)`, never below zero (§6.9.1: $93.10 with the defaults)."""
    return max(_ZERO, budget.admissible(p) * (1 - budget.reserve_frac(p)))


def llm_projection(
    settings: Settings,
    *,
    now: datetime,
    c_hat: Decimal,
    projected_critic_rate: Decimal,
    arrival_per_month: int,
) -> LlmProjection:
    """The LLM projection of `settings` in the month of `now` (module docstring)."""
    p = BudgetParams.from_payload(settings.to_payload())
    month = budget.month_of(now)
    per_target = c_hat + _CACHE_WRITE_PER_TARGET
    targets = int((scoring_share(p) / per_target).to_integral_value(rounding=ROUND_FLOOR))
    return LlmProjection(
        # A ≤ 0 accrues nothing; the capacity is then its floor, `min_bucket_capacity_usd`
        bucket_rate_per_h=_usd(max(_ZERO, budget.rate_per_s(p, month)) * _SECONDS_PER_HOUR),
        bucket_capacity=_usd(budget.bucket_capacity(p, month)),
        c_hat=c_hat,
        projected_critic_rate=projected_critic_rate,
        targets_per_month=targets,
        arrival_per_month=arrival_per_month,
        projected_month_spend=_usd(min(arrival_per_month, targets) * per_target),
    )


def cap_binding(projection: LlmProjection) -> bool:
    """§6.8's `llm_cap_binding`: more targets arrive than the cap lets score, or none can be scored."""
    return projection.targets_per_month == 0 or projection.arrival_per_month > projection.targets_per_month
