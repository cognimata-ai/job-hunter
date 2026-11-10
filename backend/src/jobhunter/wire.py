"""The wire forms of the values a response carries (spec §14.1: JSON in and out), for every model the api
serves, wherever that model is built: the api's own routes (through `api.models`, which re-exports them) and
the domain modules whose models it serves (`observability.health`, `observability.backup_health`).

- `DecimalOut`: a decimal (a USD amount, a rate) in a response, serialised as a string in plain notation
  (`"93.100000"`, never an exponent and never a float), so a client reads it without binary rounding. Its
  OpenAPI type is `string`.
- `usd_micro`: a computed USD amount as the api shows it, at $0.000001 (the ledger's `numeric(12,6)` scale),
  rounded half-up.
- `UtcDatetime`: a timestamp in a response, converted to UTC (§14.1: timestamps are ISO-8601 UTC), whatever
  time zone the database session returned it in.

It imports nothing of the api (plan M4 Carry-forward 3: a domain module never imports the api).
"""

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from pydantic import AfterValidator, PlainSerializer

from jobhunter.llm.budget import USD_QUANTUM


def _fixed_point(amount: Decimal) -> str:
    return format(amount, "f")


DecimalOut = Annotated[Decimal, PlainSerializer(_fixed_point, return_type=str)]


def _in_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(_in_utc)]


def usd_micro(amount: Decimal) -> Decimal:
    """`amount` at $0.000001, rounded half-up."""
    return amount.quantize(USD_QUANTUM, rounding=ROUND_HALF_UP)
