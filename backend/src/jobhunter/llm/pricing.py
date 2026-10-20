"""LLM pricing (spec §10.3, §10.7): the models and efforts, the USD-per-MTok price table, and the two
amounts a call is accounted at, all in `Decimal` (no float touches money).

- `worst_case_usd` is the reservation made before every HTTP try: the input bound at the larger of the input
  and the cache-write price, plus `max_tokens` at the output price, in the price tier the *bound* selects,
  rounded **up** to $0.000001.
- `settled_usd` is what the try cost: each token class at its own price, in the price tier the *actual*
  prompt (input plus both cache counts) selects, rounded **half-up** to $0.000001.

Batch items cost 50% of every price. Haiku is the only model with a second tier: prompts above 100,000
tokens are charged at the higher prices. The second tier only raises prices and the actual prompt is at most
its bound, so the tier the bound selects is never below the tier the actual prompt selects.

Efforts (`SUPPORTED_EFFORTS`): all five efforts apply to every model. `docs/council/research/tech.md` §B1.4
notes that the langchain-anthropic model profile of `claude-haiku-5-5` lists no effort levels; that decides
only whether langchain turns thinking on by itself when `reasoning_effort` is set, and does not narrow what
the API accepts, so no effort is removed from Haiku (spec §10.3: Haiku supports `low…max`).

Context windows (`CONTEXT_WINDOW`): `docs/council/research/tech.md` §B fixes the output side of the 5.5
models (B1.10: `max_output_tokens` 128,000) but states no input window. The 1,000,000 input tokens of every
model are the `max_input_tokens` the Anthropic model catalog lists for the four model ids (cached
2026-10-06).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from types import MappingProxyType
from typing import Final, Literal

Model = Literal["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]

MODELS: Final[tuple[Model, ...]] = (
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-haiku-5-5",
    "claude-fable-5-1",
)

_ALL_EFFORTS: Final[frozenset[Effort]] = frozenset({"low", "medium", "high", "xhigh", "max"})
SUPPORTED_EFFORTS: Final[Mapping[Model, frozenset[Effort]]] = MappingProxyType(
    dict.fromkeys(MODELS, _ALL_EFFORTS)
)

# The input context window per model, in tokens (module docstring: the source and what §B does not state).
CONTEXT_WINDOW: Final[Mapping[Model, int]] = MappingProxyType(dict.fromkeys(MODELS, 1_000_000))


@dataclass(frozen=True)
class Price:
    """USD per MTok (1,000,000 tokens)."""

    input: Decimal
    output: Decimal
    cache_write: Decimal
    cache_read: Decimal


@dataclass(frozen=True)
class Usage:
    """The token counts of one response, as the provider reports them."""

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int


_PRICES: Final[Mapping[Model, Price]] = MappingProxyType(
    {
        "claude-opus-5-5": Price(Decimal("4.00"), Decimal("20.00"), Decimal("5.00"), Decimal("0.20")),
        "claude-sonnet-5-5": Price(Decimal("2.00"), Decimal("10.00"), Decimal("2.50"), Decimal("0.20")),
        # cache write 1.25x and cache read 0.1x the input price
        "claude-haiku-5-5": Price(Decimal("0.10"), Decimal("0.50"), Decimal("0.125"), Decimal("0.01")),
        "claude-fable-5-1": Price(Decimal("10.00"), Decimal("50.00"), Decimal("12.50"), Decimal("0.25")),
    }
)
# The prices of a prompt above `_LONG_PROMPT_TOKENS`, for the models that have a second tier.
_LONG_PROMPT_TOKENS: Final = 100_000
_LONG_PROMPT_PRICES: Final[Mapping[Model, Price]] = MappingProxyType(
    {"claude-haiku-5-5": Price(Decimal("0.50"), Decimal("2.50"), Decimal("0.625"), Decimal("0.05"))}
)

_TOKENS_PER_MTOK: Final = 1_000_000
_USD_QUANTUM: Final = Decimal("0.000001")


def price_for(model: Model, *, prompt_tokens: int, batch: bool = False) -> Price:
    """The prices that apply to a prompt of `prompt_tokens`; `batch` halves every one of them."""
    price = _PRICES[model]
    if prompt_tokens > _LONG_PROMPT_TOKENS:
        price = _LONG_PROMPT_PRICES.get(model, price)
    if batch:
        price = Price(price.input / 2, price.output / 2, price.cache_write / 2, price.cache_read / 2)
    return price


def worst_case_usd(
    model: Model,
    *,
    input_tokens_bound: int,
    max_tokens: int,
    batch: bool = False,
    extra_usd: Decimal = Decimal(0),
) -> Decimal:
    """The reservation for one try, rounded up to $0.000001: `extra_usd` (the native-PDF share of the overrun
    buffer, spec §10.7) is added before the rounding."""
    if input_tokens_bound < 0 or max_tokens < 0 or extra_usd < 0:
        raise ValueError("input_tokens_bound, max_tokens and extra_usd must not be negative")
    price = price_for(model, prompt_tokens=input_tokens_bound, batch=batch)
    micro_usd = input_tokens_bound * max(price.input, price.cache_write) + max_tokens * price.output
    return (micro_usd / _TOKENS_PER_MTOK + extra_usd).quantize(_USD_QUANTUM, rounding=ROUND_CEILING)


def settled_usd(model: Model, usage: Usage, *, batch: bool = False) -> Decimal:
    """The cost of one try from its reported usage, rounded half-up to $0.000001."""
    prompt_tokens = usage.input_tokens + usage.cache_creation_input_tokens + usage.cache_read_input_tokens
    price = price_for(model, prompt_tokens=prompt_tokens, batch=batch)
    micro_usd = (
        usage.input_tokens * price.input
        + usage.cache_creation_input_tokens * price.cache_write
        + usage.cache_read_input_tokens * price.cache_read
        + usage.output_tokens * price.output
    )
    return (micro_usd / _TOKENS_PER_MTOK).quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)


def bound_input_price(model: Model, *, batch: bool) -> Decimal:
    """The price per MTok at which a reservation bounds input: the larger of the input and the cache-write
    price of the model's highest tier (the one a prompt at its context window pays), halved for `batch`."""
    price = price_for(model, prompt_tokens=CONTEXT_WINDOW[model], batch=batch)
    return max(price.input, price.cache_write)
