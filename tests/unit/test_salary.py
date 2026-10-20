from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from jobhunter.sources.parsing.salary import Salary, annualise, from_bounds, parse_salary


def salary(text: str, kind: Literal["stated", "predicted"] = "stated") -> Salary:
    parsed = parse_salary(text, kind=kind)
    assert parsed is not None, text
    return parsed


def test_german_salary_strings() -> None:
    s = salary("60.000 € bis 80.000 € pro Jahr")
    assert (s.min, s.max, s.currency, s.period, s.kind, s.annual_eur_min, s.annual_eur_max) == (
        60000,
        80000,
        "EUR",
        "year",
        "stated",
        60000,
        80000,
    )
    assert salary("5.000 € pro Monat").annual_eur_min == 60000
    assert salary("25 € pro Stunde").annual_eur_min == 43000
    assert salary("60K \u2013 70K EUR a year").max == 70000
    assert salary("60 Tsd. € jährlich").min == 60000
    usd = salary("$80,000 per year")
    assert usd.annual_eur_min is None
    assert parse_salary("Gehalt nach Vereinbarung") is None
    assert (annualise(1, "week"), annualise(1, "day"), annualise(1, "hour")) == (46, 220, 1720)


def test_the_stored_form() -> None:
    raw = "60.000 € bis 80.000 € pro Jahr"
    s = salary(raw)
    assert s.model_dump() == {
        "min": 60000,
        "max": 80000,
        "currency": "EUR",
        "period": "year",
        "kind": "stated",
        "raw": raw,
        "annual_eur_min": 60000,
        "annual_eur_max": 80000,
    }
    assert Salary.model_validate_json(s.model_dump_json()) == s


@pytest.mark.parametrize(
    ("text", "low", "high", "currency", "period"),
    [
        ("60.000 € bis 80.000 € pro Jahr", 60000, 80000, "EUR", "year"),
        ("60.000,50 € pro Jahr", 60000.5, 60000.5, "EUR", "year"),
        ("12,50 € pro Stunde", 12.5, 12.5, "EUR", "hour"),
        ("12.5 EUR an hour", 12.5, 12.5, "EUR", "hour"),
        ("60K\u201370K EUR a year", 60000, 70000, "EUR", "year"),
        ("60k-70k € per year", 60000, 70000, "EUR", "year"),
        ("60-70K EUR a year", 60000, 70000, "EUR", "year"),
        ("€60K - €70K a year", 60000, 70000, "EUR", "year"),
        ("5 \u2013 7 Tsd. € monatlich", 5000, 7000, "EUR", "month"),
        ("60,5 Tsd. € jährlich", 60500, 60500, "EUR", "year"),
        ("60.000-80.000 € pro Jahr", 60000, 80000, "EUR", "year"),
        ("60.000 € to 80.000 € per year", 60000, 80000, "EUR", "year"),
        ("60.000\xa0€ bis 80.000\xa0€ pro\xa0Jahr", 60000, 80000, "EUR", "year"),
        ("60 000 € \u2013 80 000 € pro Jahr", 60000, 80000, "EUR", "year"),
        ("1.234.567 € pro Jahr", 1234567, 1234567, "EUR", "year"),
        ("EUR 4.500 pro Monat", 4500, 4500, "EUR", "month"),
        ("4500 Euro monatlich", 4500, 4500, "EUR", "month"),
        ("$80,000 per year", 80000, 80000, "USD", "year"),
        ("$80,000.50 a year", 80000.5, 80000.5, "USD", "year"),
        ("£45,000 \u2013 £55,000 a year", 45000, 55000, "GBP", "year"),
        ("CHF 95000 pro Jahr", 95000, 95000, "CHF", "year"),
        ("80.000 €", 80000, 80000, "EUR", "year"),
    ],
)
def test_amounts_currencies_and_number_formats(text, low, high, currency, period) -> None:
    s = salary(text)
    assert (s.min, s.max, s.currency, s.period) == (low, high, currency, period)


@pytest.mark.parametrize(
    ("text", "period"),
    [
        ("50.000 € pro Jahr", "year"),
        ("50.000 € jährlich", "year"),
        ("50.000 € im Jahr", "year"),
        ("50.000 € p.a.", "year"),
        ("50.000 € a year", "year"),
        ("50.000 € per year", "year"),
        ("50.000 € yearly", "year"),
        ("50.000 € / Jahr", "year"),
        ("4.000 € pro Monat", "month"),
        ("4.000 € monatlich", "month"),
        ("4.000 € a month", "month"),
        ("4.000 € per month", "month"),
        ("4.000 € monthly", "month"),
        ("4.000 € / Monat", "month"),
        ("25 € pro Stunde", "hour"),
        ("25 € stündlich", "hour"),
        ("25 € an hour", "hour"),
        ("25 € per hour", "hour"),
        ("25 € hourly", "hour"),
        ("25 €/h", "hour"),
        ("1.000 € pro Woche", "week"),
        ("1.000 € wöchentlich", "week"),
        ("1.000 € a week", "week"),
        ("1.000 € per week", "week"),
        ("200 € pro Tag", "day"),
        ("200 € täglich", "day"),
        ("200 € a day", "day"),
        ("200 € per day", "day"),
        ("4.000 € PRO MONAT", "month"),
    ],
)
def test_period_phrases(text, period) -> None:
    assert salary(text).period == period


def test_no_period_means_year() -> None:
    assert salary("60.000 € bis 80.000 €").period == "year"


def test_the_period_is_the_one_that_follows_the_amount() -> None:
    s = salary("Bei 40 Stunden pro Woche: 60.000 € pro Jahr")
    assert (s.min, s.period) == (60000, "year")
    assert salary("5.000 € pro Monat (60.000 € pro Jahr)").period == "month"
    assert salary("Pro Monat: 5.000 €").period == "month"
    # the search is not bounded to the clause (the docstring's "short strings only"): a later period wins
    assert salary("60.000 € bei Vollzeit, Bonus pro Monat").period == "month"


@pytest.mark.parametrize(
    ("text", "low", "high"),
    [
        ("32,3K EUR", 32300, 32300),  # 32.3 * 1000 is 32299.999999999996 as a float
        ("16,1 Tsd. € jährlich", 16100, 16100),
        ("2,01 - 4,03K € monatlich", 2010, 4030),  # the shared multiplier is rounded too
        ("4,02K-32,7K EUR", 4020, 32700),
    ],
)
def test_a_multiplied_amount_is_rounded_to_cents(text: str, low: float, high: float) -> None:
    # fix wave F1 (ledger L40): no float drift in `min`/`max`, so equal amounts compare and hash equal
    s = salary(text)
    assert (s.min, s.max) == (low, high) and s.min is not None and s.min == round(s.min, 2)
    assert s.annual_eur_min == round(s.annual_eur_min or 0, 2)


@pytest.mark.parametrize(
    ("text", "low", "high"),
    [
        ("ab 60.000 € pro Jahr", 60000, None),
        ("from 60K EUR a year", 60000, None),
        ("bis 80.000 € pro Jahr", None, 80000),
        ("bis zu 80.000 € pro Jahr", None, 80000),
        ("up to $90,000 a year", None, 90000),
    ],
)
def test_one_sided_amounts(text, low, high) -> None:
    s = salary(text)
    assert (s.min, s.max) == (low, high)
    if s.currency == "EUR":
        assert (s.annual_eur_min, s.annual_eur_max) == (low, high)


def test_annualisation_of_the_stored_values() -> None:
    s = salary("2.000 € bis 3.000 € pro Woche")
    assert (s.annual_eur_min, s.annual_eur_max) == (92000, 138000)
    d = salary("200 € bis 250 € pro Tag")
    assert (d.annual_eur_min, d.annual_eur_max) == (44000, 55000)
    m = salary("4.500 € bis 5.500 € pro Monat")
    assert (m.annual_eur_min, m.annual_eur_max) == (54000, 66000)
    h = salary("12,50 € pro Stunde")
    assert (h.annual_eur_min, h.annual_eur_max) == (21500, 21500)


@pytest.mark.parametrize("currency", ["$80,000 per year", "£45,000 a year", "CHF 95000 pro Jahr"])
def test_other_currencies_are_stored_but_never_annualised(currency) -> None:
    s = salary(currency)
    assert s.currency != "EUR" and s.annual_eur_min is None and s.annual_eur_max is None


def test_kind_and_raw_are_kept() -> None:
    s = salary("60.000 € pro Jahr", kind="predicted")
    assert s.kind == "predicted" and s.raw == "60.000 € pro Jahr"
    assert salary("60.000 € pro Jahr").kind == "stated"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Gehalt nach Vereinbarung",
        "Vollzeit",
        "vor 4 Tagen",
        "3 days ago",
        "60.000 pro Jahr",  # no currency
        "Home Office 100 %",
        "0 € pro Jahr",
        "Wir bieten 30 Tage Urlaub",
        "€",
    ],
)
def test_text_without_an_amount_is_not_a_salary(text) -> None:
    assert parse_salary(text) is None


def test_annualise() -> None:
    assert [annualise(100, p) for p in ("year", "month", "week", "day", "hour")] == [
        100,
        1200,
        4600,
        22000,
        172000,
    ]
    assert annualise(12.5, "hour") == 21500
    with pytest.raises(ValueError, match="fortnight"):
        annualise(1, "fortnight")


def test_from_bounds() -> None:
    s = from_bounds(40000, 50000, currency="EUR", period="year", kind="predicted", raw=None)
    assert s is not None
    assert s.model_dump() == {
        "min": 40000,
        "max": 50000,
        "currency": "EUR",
        "period": "year",
        "kind": "predicted",
        "raw": None,
        "annual_eur_min": 40000,
        "annual_eur_max": 50000,
    }
    only_min = from_bounds(3000, None, currency="EUR", period="month", kind="stated", raw="ab 3.000")
    assert only_min is not None and (
        only_min.min,
        only_min.max,
        only_min.annual_eur_min,
        only_min.annual_eur_max,
    ) == (
        3000,
        None,
        36000,
        None,
    )
    assert from_bounds(None, None, currency="EUR", period="year", kind="stated", raw="x") is None
    usd = from_bounds(80000, 90000, currency="USD", period="year", kind="stated", raw=None)
    assert usd is not None and usd.annual_eur_min is None and usd.annual_eur_max is None


def test_from_bounds_orders_the_bounds_and_drops_nonsense() -> None:
    swapped = from_bounds(50000, 40000, currency="EUR", period="year", kind="stated", raw=None)
    assert swapped is not None and (swapped.min, swapped.max) == (40000, 50000)
    zero_low = from_bounds(0, 50000, currency="EUR", period="year", kind="stated", raw=None)
    assert zero_low is not None and (zero_low.min, zero_low.max) == (None, 50000)
    assert from_bounds(0, 0, currency="EUR", period="year", kind="stated", raw=None) is None
    assert (
        from_bounds(float("nan"), float("inf"), currency="EUR", period="year", kind="stated", raw=None)
        is None
    )
    with pytest.raises(ValueError, match="period"):
        from_bounds(1, 2, currency="EUR", period="decade", kind="stated", raw=None)


def test_from_bounds_normalises_the_currency_code() -> None:
    s = from_bounds(1000, 2000, currency="eur", period="month", kind="stated", raw=None)
    assert s is not None and s.currency == "EUR" and s.annual_eur_min == 12000


@given(amount=st.integers(min_value=1, max_value=999_999))
def test_property_german_and_english_thousands_agree(amount: int) -> None:
    german = f"{amount:,}".replace(",", ".")
    english = f"{amount:,}"
    de = parse_salary(f"{german} € pro Jahr")
    en = parse_salary(f"{english} € pro Jahr")
    assert de is not None and en is not None
    assert de.min == en.min == amount
