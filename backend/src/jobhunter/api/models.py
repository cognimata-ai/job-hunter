"""Shared output types of the api's response models (spec §14.1: JSON in and out).

- `DecimalOut`: a decimal (a USD amount, a rate) in a response, serialised as a string in plain notation
  (`"93.100000"`, never an exponent and never a float), so a client reads it without binary rounding. Its
  OpenAPI type is `string`.
- `usd_micro`: a computed USD amount as the api shows it, at $0.000001 (the ledger's `numeric(12,6)` scale),
  rounded half-up.

This module imports nothing of the api, so `observability.health`, whose models the api serves, can use it.
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from pydantic import PlainSerializer

from jobhunter.llm.budget import USD_QUANTUM


def _fixed_point(amount: Decimal) -> str:
    return format(amount, "f")


DecimalOut = Annotated[Decimal, PlainSerializer(_fixed_point, return_type=str)]


def usd_micro(amount: Decimal) -> Decimal:
    """`amount` at $0.000001, rounded half-up."""
    return amount.quantize(USD_QUANTUM, rounding=ROUND_HALF_UP)
