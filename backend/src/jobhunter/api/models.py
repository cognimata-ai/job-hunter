"""Shared input and output types of the api's models (spec §14.1: JSON in and out).

- `clean_text`: the `AfterValidator` of a request string that may hold no NUL character and no lone surrogate.
  Neither can reach PostgreSQL (a `text` value refuses both), so a field with it answers 422
  `validation_failed` at the edge of the model, however the route then uses it (M2 Task 22). A field lists it
  after its `StringConstraints`, as in `Annotated[str, StringConstraints(max_length=…),
  AfterValidator(clean_text)]`, so that trimming and length rules come first and every length error has
  pydantic's one code, `string_too_long`.
- `DecimalOut`: a decimal (a USD amount, a rate) in a response, serialised as a string in plain notation
  (`"93.100000"`, never an exponent and never a float), so a client reads it without binary rounding. Its
  OpenAPI type is `string`.
- `usd_micro`: a computed USD amount as the api shows it, at $0.000001 (the ledger's `numeric(12,6)` scale),
  rounded half-up.
- `UtcDatetime`: a timestamp in a response, converted to UTC (§14.1: timestamps are ISO-8601 UTC), whatever
  time zone the database session returned it in.

This module imports nothing of the api, so `observability.health`, whose models the api serves, can use it.
"""

from datetime import UTC, datetime
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


DecimalOut = Annotated[Decimal, PlainSerializer(_fixed_point, return_type=str)]


def _in_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(_in_utc)]


def usd_micro(amount: Decimal) -> Decimal:
    """`amount` at $0.000001, rounded half-up."""
    return amount.quantize(USD_QUANTUM, rounding=ROUND_HALF_UP)
