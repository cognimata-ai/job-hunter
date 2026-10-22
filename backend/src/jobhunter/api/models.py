"""Shared input and output types of the api's models (spec §14.1: JSON in and out).

- `CleanStr`, `clean_text`: a string of a request that holds no NUL character and no lone surrogate. Neither
  can reach PostgreSQL (a `text` value refuses both), so a field of this type answers 422 `validation_failed`
  at the edge of the model, however the route then uses it (M2 Task 22). A field that is also trimmed or
  length-checked lists `clean_text` after its `StringConstraints`, which `Annotated[CleanStr, …]` would not.
- `DecimalOut`: a decimal (a USD amount, a rate) in a response, serialised as a string in plain notation
  (`"93.100000"`, never an exponent and never a float), so a client reads it without binary rounding. Its
  OpenAPI type is `string`.
- `usd_micro`: a computed USD amount as the api shows it, at $0.000001 (the ledger's `numeric(12,6)` scale),
  rounded half-up.

This module imports nothing of the api, so `observability.health`, whose models the api serves, can use it.
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from pydantic import AfterValidator, PlainSerializer

from jobhunter.llm.budget import USD_QUANTUM


def _fixed_point(amount: Decimal) -> str:
    return format(amount, "f")


def clean_text(text: str) -> str:
    """`text` itself, or a `ValueError` (pydantic's 422) for a NUL character or a lone surrogate."""
    if "\x00" in text:
        raise ValueError("must not contain a NUL character")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("must be valid Unicode text") from None
    return text


CleanStr = Annotated[str, AfterValidator(clean_text)]

DecimalOut = Annotated[Decimal, PlainSerializer(_fixed_point, return_type=str)]


def usd_micro(amount: Decimal) -> Decimal:
    """`amount` at $0.000001, rounded half-up."""
    return amount.quantize(USD_QUANTUM, rounding=ROUND_HALF_UP)
