"""Shared input and output types of the api's models (spec §14.1: JSON in and out).

- `clean_text`: the `AfterValidator` of a request string that may hold no NUL character and no lone surrogate.
  Neither can reach PostgreSQL (a `text` value refuses both), so a field with it answers 422
  `validation_failed` at the edge of the model, however the route then uses it (M2 Task 22). A field lists it
  after its `StringConstraints`, as in `Annotated[str, StringConstraints(max_length=…),
  AfterValidator(clean_text)]`, so that trimming and length rules come first and every length error has
  pydantic's one code, `string_too_long`.
- `DecimalOut`, `usd_micro` and `UtcDatetime`: a response's decimals, USD amounts and timestamps, re-exported
  from `jobhunter.wire`, where the domain modules whose models the api serves take them from (plan M5 Task 7,
  M4 Carry-forward 3).
"""

from jobhunter.wire import DecimalOut, UtcDatetime, usd_micro

__all__ = ["DecimalOut", "UtcDatetime", "clean_text", "usd_micro"]


def clean_text(text: str) -> str:
    """`text` itself, or a `ValueError` (pydantic's 422) for a NUL character or a lone surrogate."""
    if "\x00" in text:
        raise ValueError("must not contain a NUL character")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("must be valid Unicode text") from None
    return text
