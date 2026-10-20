"""Salaries (spec §8.10): the stored form, the parser for provider strings and the annualisation.

`parse_salary` reads German and English salary strings: `60.000 € bis 80.000 € pro Jahr`, `5.000 € pro Monat`,
`60K-70K EUR a year`, `60 Tsd. € jährlich`, `$80,000 per year`. A string is a salary only when it holds an
amount next to a currency (`€`, `EUR`, `$`, `USD`, `£`, `GBP`, `CHF`); `vor 4 Tagen` is not a salary.
Number formats: `.` or a space groups thousands and `,` starts the decimals (`60.000,50`); the English
`80,000.50` is read as well. A lone separator followed by exactly three digits (`60.000`, `80,000`) is a
thousands separator, as no salary has three decimals. `K`, `Tsd.` and `Tausend` multiply by 1,000. Only the
first amount expression of a string is read; the period is the one named right after it (else right before
it), and `year` when the string names none.

Annualisation exists for comparison only; the stated value is always displayed unconverted. Only EUR salaries
are annualised: other currencies are not comparable and stay unknown for the salary criterion.
"""

import math
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from jobhunter.normalize.text import norm_text

Period = Literal["hour", "day", "week", "month", "year"]
SalaryKind = Literal["stated", "predicted"]

# Spec §8.10: month x 12, week x 46, day x 220, hour x 1,720.
ANNUALISATION: Final[Mapping[Period, int]] = MappingProxyType(
    {"year": 1, "month": 12, "week": 46, "day": 220, "hour": 1720}
)


class Salary(BaseModel):
    """The stored form of a salary (§8.10). `annual_eur_*` is `None` unless the currency is EUR."""

    model_config = ConfigDict(frozen=True)

    min: float | None
    max: float | None
    currency: str
    period: Period
    kind: SalaryKind
    raw: str | None
    annual_eur_min: float | None
    annual_eur_max: float | None


def _period(name: str) -> Period:
    for period in ANNUALISATION:
        if period == name:
            return period
    raise ValueError(f"unknown salary period {name!r}")


def annualise(amount: float, period: str) -> float:
    """`amount` per `period` as a yearly amount: month x 12, week x 46, day x 220, hour x 1,720."""
    return amount * ANNUALISATION[_period(period)]


def _usable(amount: float | None) -> float | None:
    return amount if amount is not None and math.isfinite(amount) and amount > 0 else None


def from_bounds(
    min_: float | None,
    max_: float | None,
    *,
    currency: str,
    period: str,
    kind: SalaryKind,
    raw: str | None,
) -> Salary | None:
    """A `Salary` from structured bounds; `None` when neither bound is a positive finite amount.

    The bounds are put in order. Raises `ValueError` for a period outside hour/day/week/month/year.
    """
    stored_period = _period(period)
    low, high = _usable(min_), _usable(max_)
    if low is None and high is None:
        return None
    if low is not None and high is not None and low > high:
        low, high = high, low
    code = currency.strip().upper()
    in_eur = code == "EUR"
    return Salary(
        min=low,
        max=high,
        currency=code,
        period=stored_period,
        kind=kind,
        raw=raw,
        annual_eur_min=round(annualise(low, stored_period), 2) if in_eur and low is not None else None,
        annual_eur_max=round(annualise(high, stored_period), 2) if in_eur and high is not None else None,
    )


_CURRENCY_CODES: Final = {
    "€": "EUR",
    "eur": "EUR",
    "euro": "EUR",
    "$": "USD",
    "usd": "USD",
    "£": "GBP",
    "gbp": "GBP",
    "chf": "CHF",
}
# `[^\W\d_]` is a letter: a currency code or a word such as `bis` must not be the middle of a longer word.
_NO_LETTER_BEFORE: Final = r"(?<![^\W\d_])"
_NO_LETTER_AFTER: Final = r"(?![^\W\d_])"
_CURRENCY: Final = rf"(?:€|\$|£|{_NO_LETTER_BEFORE}(?:eur|euro|usd|gbp|chf){_NO_LETTER_AFTER})"
_NUMBER: Final = r"(?:\d{1,3}(?:[ .,]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
_MULTIPLIER: Final = rf"(?:k{_NO_LETTER_AFTER}|tsd\.?|tausend{_NO_LETTER_AFTER})"
_JOINER: Final = rf"(?:\u2013|\u2014|-|{_NO_LETTER_BEFORE}(?:bis|to){_NO_LETTER_AFTER})"


def _amount(tag: str) -> str:
    """One amount: currency before, the number (`60.000,-` too), a multiplier, currency after (optional)."""
    return (
        rf"(?:(?P<c1{tag}>{_CURRENCY})\s*)?(?<![\d.,])(?P<n{tag}>{_NUMBER})(?:,[-\u2013])?"
        rf"(?:\s*(?P<m{tag}>{_MULTIPLIER}))?(?:\s*(?P<c2{tag}>{_CURRENCY}))?"
    )


_EXPRESSION: Final = re.compile(rf"{_amount('a')}(?:\s*{_JOINER}\s*{_amount('b')})?", re.IGNORECASE)
_AT_LEAST: Final = re.compile(rf"{_NO_LETTER_BEFORE}(?:ab|from)\s*$", re.IGNORECASE)
_AT_MOST: Final = re.compile(rf"{_NO_LETTER_BEFORE}(?:bis(?:\s+zu)?|up\s+to)\s*$", re.IGNORECASE)

_PERIOD_PHRASES: Final[Mapping[Period, re.Pattern[str]]] = {
    "year": re.compile(
        r"\b(?:pro|je|im)\s+jahr\b|\bj(?:ä|ae)hrlich\b|\bp\.\s?a\.|\b(?:a|per|each)\s+year\b|\byearly\b"
        r"|\bannual(?:ly)?\b|\bper\s+annum\b|/\s?(?:jahr|year)\b",
        re.IGNORECASE,
    ),
    "month": re.compile(
        r"\b(?:pro|je|im)\s+monat\b|\bmonatlich\b|\b(?:a|per)\s+month\b|\bmonthly\b|/\s?(?:monat|month)\b",
        re.IGNORECASE,
    ),
    "week": re.compile(
        r"\b(?:pro|je)\s+woche\b|\bw(?:ö|oe)chentlich\b|\b(?:a|per)\s+week\b|\bweekly\b|/\s?(?:woche|week)\b",
        re.IGNORECASE,
    ),
    "day": re.compile(
        r"\b(?:pro|je)\s+tag\b|\bt(?:ä|ae)glich\b|\b(?:a|per)\s+day\b|\bdaily\b|/\s?(?:tag|day)\b",
        re.IGNORECASE,
    ),
    "hour": re.compile(
        r"\b(?:pro|je)\s+stunde\b|\bst(?:ü|ue)ndlich\b|\b(?:an|a|per)\s+hour\b|\bhourly\b"
        rf"|/\s?(?:h|std\.?|stunde|hour){_NO_LETTER_AFTER}",
        re.IGNORECASE,
    ),
}


def _number(token: str) -> float | None:
    """`60.000,50`, `80,000.50`, `60.000`, `80,000`, `12,5`, `60 000` as a float; `None` if malformed."""
    t = token.replace(" ", "")
    if "." in t and "," in t:
        decimal = "." if t.rfind(".") > t.rfind(",") else ","
        t = t.replace("," if decimal == "." else ".", "").replace(decimal, ".")
    elif "." in t or "," in t:
        separator = "." if "." in t else ","
        parts = t.split(separator)
        grouped = len(parts) > 2 or (len(parts[-1]) == 3 and 1 <= len(parts[0]) <= 3 and parts[0] != "0")
        t = "".join(parts) if grouped else ".".join(parts)
    try:
        return float(t)
    except ValueError:
        return None


def _thousands(amount: float) -> float:
    """`amount` times 1,000 (`K`, `Tsd.`, `Tausend`), rounded to cents (fix wave F1): `32,3K` is 32300.0,
    not the float product 32299.999999999996."""
    return round(amount * 1000, 2)


def _period_near(text: str, start: int, end: int) -> Period:
    """The period of the amount at `text[start:end]`: the first period phrase anywhere after it, else the last
    one before it, else `year`.

    The search is not bounded to the amount's clause: a period named later in the string wins ("60.000 € bei
    Vollzeit, Bonus pro Monat" reads as monthly). It is meant for the short salary strings that providers send
    (a SerpApi salary chip), not for an ad's free text.
    """
    after = [
        (m.start(), period) for period, pattern in _PERIOD_PHRASES.items() if (m := pattern.search(text, end))
    ]
    if after:
        return min(after)[1]
    before = [
        (m.end(), period)
        for period, pattern in _PERIOD_PHRASES.items()
        for m in pattern.finditer(text, 0, start)
    ]
    return max(before)[1] if before else "year"


def parse_salary(text: str, *, kind: SalaryKind = "stated") -> Salary | None:
    """The first amount or range with a currency in `text` as a `Salary`, or `None` if the text names none."""
    normalised = norm_text(text)
    for match in _EXPRESSION.finditer(normalised):
        code = next(
            (
                _CURRENCY_CODES[value.casefold()]
                for tag in "ab"
                for group in (f"c1{tag}", f"c2{tag}")
                if (value := match[group])
            ),
            None,
        )
        low = _number(match["na"])
        if code is None or low is None:
            continue
        low = _thousands(low) if match["ma"] else low
        high: float | None = low
        if match["nb"] is not None:
            high = _number(match["nb"])
            if high is None:
                continue
            if match["mb"]:
                high = _thousands(high)
                low = low if match["ma"] else _thousands(low)  # `60-70K`: the multiplier is shared
        else:
            before = normalised[: match.start()]
            if _AT_LEAST.search(before):
                high = None
            elif _AT_MOST.search(before):
                low, high = None, low
        period = _period_near(normalised, match.start(), match.end())
        return from_bounds(low, high, currency=code, period=period, kind=kind, raw=text)
    return None
