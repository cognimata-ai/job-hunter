from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from jobhunter.sources.parsing.dates import (
    BERLIN,
    ParsedDate,
    from_epoch,
    jsearch_precision,
    parse_iso,
    parse_relative,
    response_date,
)

REF = datetime(2026, 10, 16, 9, 47, tzinfo=UTC)


def relative(text: str, reference: datetime) -> ParsedDate:
    parsed = parse_relative(text, reference=reference)
    assert parsed is not None, text
    return parsed


@pytest.mark.parametrize(
    ("text", "precision", "hours_back"),
    [
        ("vor 15 Stunden", "hour", 15),
        ("15 hours ago", "hour", 15),
        ("vor 1 Stunde", "hour", 1),
        ("1 hour ago", "hour", 1),
    ],
)
def test_hours(text, precision, hours_back) -> None:
    d = parse_relative(text, reference=REF)
    assert d is not None
    assert (d.precision, (REF - d.at).total_seconds() / 3600) == (precision, hours_back)


@pytest.mark.parametrize(
    ("text", "day"),
    [
        ("vor 2 Tagen", 14),
        ("2 days ago", 14),
        ("vor 1 Tag", 15),
        ("1 day ago", 15),
        ("Gestern", 15),
        ("yesterday", 15),
        ("Heute", 16),
        ("today", 16),
        ("just posted", 16),
    ],
)
def test_days_resolve_to_berlin_midnight(text, day) -> None:
    d = parse_relative(
        text, reference=REF
    )  # 11:47 in Berlin (CEST): the local day starts at 22:00 UTC the evening before
    assert d is not None
    assert d.precision == "relative_day"
    assert d.at == datetime(2026, 10, day - 1, 22, 0, tzinfo=UTC)


def test_relative_day_boundaries_follow_europe_berlin() -> None:  # Review Focus 1
    cest_night = datetime(
        2026, 10, 24, 22, 30, tzinfo=UTC
    )  # 00:30 on 25 Oct in Berlin (CEST): "Gestern" is 24 Oct
    assert relative("Gestern", cest_night).at == datetime(2026, 10, 23, 22, 0, tzinfo=UTC)
    assert relative("Heute", cest_night).at == datetime(2026, 10, 24, 22, 0, tzinfo=UTC)
    cet_morning = datetime(
        2026, 10, 26, 0, 0, tzinfo=UTC
    )  # 01:00 on 26 Oct in Berlin (CET, after the change): "vor 2 Tagen" is 24 Oct, still a CEST day
    assert relative("vor 2 Tagen", cet_morning).at == datetime(2026, 10, 23, 22, 0, tzinfo=UTC)


def test_the_utc_date_is_not_the_berlin_date() -> None:
    # 23:30 UTC on 15 Oct is 01:30 on 16 Oct in Berlin: "Heute" is already the 16th, "Gestern" the 15th.
    late = datetime(2026, 10, 15, 23, 30, tzinfo=UTC)
    assert relative("Heute", late).at == datetime(2026, 10, 15, 22, 0, tzinfo=UTC)
    assert relative("Gestern", late).at == datetime(2026, 10, 14, 22, 0, tzinfo=UTC)
    # 22:30 UTC on 15 Oct is 00:30 on 16 Oct; 21:59 UTC is still 23:59 on the 15th.
    assert relative("Heute", datetime(2026, 10, 15, 22, 0, tzinfo=UTC)).at == datetime(
        2026, 10, 15, 22, 0, tzinfo=UTC
    )
    assert relative("Heute", datetime(2026, 10, 15, 21, 59, tzinfo=UTC)).at == datetime(
        2026, 10, 14, 22, 0, tzinfo=UTC
    )


def test_the_local_day_of_the_clock_change_is_25_hours_long_in_autumn() -> None:
    # 25 Oct 2026 starts at 22:00 UTC on the 24th (CEST) and ends at 23:00 UTC on the 25th (CET).
    midday = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    assert relative("Heute", midday).at == datetime(2026, 10, 24, 22, 0, tzinfo=UTC)
    first_hour_of_next_day = datetime(2026, 10, 25, 23, 0, tzinfo=UTC)  # 00:00 on 26 Oct in Berlin (CET)
    assert relative("Heute", first_hour_of_next_day).at == datetime(2026, 10, 25, 23, 0, tzinfo=UTC)
    assert relative("Gestern", first_hour_of_next_day).at == datetime(2026, 10, 24, 22, 0, tzinfo=UTC)
    last_hour_of_the_25th = datetime(2026, 10, 25, 22, 59, tzinfo=UTC)  # 23:59 on 25 Oct in Berlin (CET)
    assert relative("Heute", last_hour_of_the_25th).at == datetime(2026, 10, 24, 22, 0, tzinfo=UTC)


def test_days_across_the_spring_clock_change_count_calendar_days_not_24_hour_blocks() -> None:
    # 29 Mar 2026 is the 23-hour day: it starts at 23:00 UTC on the 28th (CET) and the clocks jump to CEST.
    tuesday = datetime(2026, 3, 31, 10, 0, tzinfo=UTC)  # 12:00 on 31 Mar in Berlin (CEST)
    assert relative("vor 2 Tagen", tuesday).at == datetime(2026, 3, 28, 23, 0, tzinfo=UTC)
    assert relative("vor 1 Tag", tuesday).at == datetime(2026, 3, 29, 22, 0, tzinfo=UTC)
    assert relative("Heute", tuesday).at == datetime(2026, 3, 30, 22, 0, tzinfo=UTC)
    # Hours are elapsed time, whatever the clocks did: 3 h before 03:30 CEST is 23:30 CET the evening before.
    after_jump = datetime(2026, 3, 29, 1, 30, tzinfo=UTC)  # 03:30 CEST
    assert after_jump - relative("vor 3 Stunden", after_jump).at == timedelta(hours=3)


def test_days_back_are_calendar_days_not_24_hour_blocks_near_midnight() -> None:
    # Subtracting 48 elapsed hours lands on the wrong local date when the clocks changed in between and the
    # reference is within an hour of local midnight; counting calendar days does not.
    after_spring = datetime(
        2026, 3, 30, 22, 30, tzinfo=UTC
    )  # 00:30 CEST on 31 Mar; 48 h earlier is 23:30 CET on 28 Mar
    assert relative("vor 2 Tagen", after_spring).at == datetime(
        2026, 3, 28, 23, 0, tzinfo=UTC
    )  # 29 Mar, 00:00 CET
    after_autumn = datetime(
        2026, 10, 26, 22, 30, tzinfo=UTC
    )  # 23:30 CET on 26 Oct; 48 h earlier is 00:30 CEST on 25 Oct
    assert relative("vor 2 Tagen", after_autumn).at == datetime(
        2026, 10, 23, 22, 0, tzinfo=UTC
    )  # 24 Oct, 00:00 CEST


def test_year_and_month_rollover() -> None:
    new_year = datetime(2027, 1, 1, 8, 0, tzinfo=UTC)  # 09:00 on 1 Jan in Berlin (CET)
    assert relative("Gestern", new_year).at == datetime(2026, 12, 30, 23, 0, tzinfo=UTC)
    assert relative("vor 2 Tagen", new_year).at == datetime(2026, 12, 29, 23, 0, tzinfo=UTC)


def test_results_are_utc_and_keep_the_raw_string() -> None:
    d = relative("vor 2 Tagen", REF)
    assert d.at.utcoffset() == timedelta(0) and d.at.tzinfo is UTC
    assert d.raw == "vor 2 Tagen"
    hours = relative(
        "15 hours ago", REF.astimezone(BERLIN)
    )  # a Berlin-zoned reference gives the same instant
    assert hours.at == REF - timedelta(hours=15) and hours.at.tzinfo is UTC and hours.raw == "15 hours ago"


@pytest.mark.parametrize(
    ("text", "precision", "day"),
    [
        ("  GESTERN ", "relative_day", 15),
        ("Heute", "relative_day", 16),
        ("Just Posted", "relative_day", 16),
        ("Vor 2 Tagen", "relative_day", 14),
        ("vor\xa02\xa0Tagen", "relative_day", 14),
        ("2  days   ago", "relative_day", 14),
        ("vor 0 Tagen", "relative_day", 16),
        ("0 days ago", "relative_day", 16),
    ],
)
def test_case_and_whitespace_do_not_matter(text, precision, day) -> None:
    d = relative(text, REF)
    assert (d.precision, d.at) == (precision, datetime(2026, 10, day - 1, 22, 0, tzinfo=UTC))


@pytest.mark.parametrize(
    "text",
    [
        "irgendwann",
        "",
        "vor einer Woche",
        "vor Tagen",
        "vor 2 Tagen und 3 Stunden",
        "2 days",
        "in 2 days",
        "vor 99999 Tagen",
        "ago",
    ],
)
def test_unrecognised_strings_are_none(text) -> None:
    assert parse_relative(text, reference=REF) is None


def test_a_naive_reference_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone"):
        parse_relative("Heute", reference=datetime(2026, 10, 16, 9, 47))  # noqa: DTZ001


@given(
    reference=st.datetimes(
        min_value=datetime(2020, 1, 1),  # noqa: DTZ001
        max_value=datetime(2035, 12, 31),  # noqa: DTZ001
        timezones=st.just(UTC),
    ),
    n=st.integers(min_value=0, max_value=400),
)
@settings(max_examples=300, deadline=None)
def test_property_a_day_form_is_local_midnight_n_calendar_days_back(reference: datetime, n: int) -> None:
    d = relative(f"vor {n} Tagen", reference)
    local = d.at.astimezone(BERLIN)
    assert (local.hour, local.minute, local.second, local.microsecond) == (0, 0, 0, 0)
    assert (reference.astimezone(BERLIN).date() - local.date()).days == n
    assert d.at.tzinfo is UTC


def test_precision_and_date_header() -> None:
    assert (
        jsearch_precision("vor 15 Stunden"),
        jsearch_precision("vor 2 Tagen"),
        jsearch_precision("2 hours ago"),
    ) == (
        "hour",
        "day",
        "hour",
    )
    assert response_date({"date": "Fri, 16 Oct 2026 09:47:00 GMT"}, REF) == REF
    assert response_date({}, REF) == REF
    assert parse_relative("irgendwann", reference=REF) is None


@pytest.mark.parametrize(
    ("relative_text", "expected"),
    [
        ("vor 1 Stunde", "hour"),
        ("1 hour ago", "hour"),
        ("3 HOURS AGO", "hour"),
        ("vor 3 Tagen", "day"),
        ("3 days ago", "day"),
        ("Gestern", "day"),
        ("Just posted", "day"),
        ("", "day"),
    ],
)
def test_jsearch_precision(relative_text, expected) -> None:
    assert jsearch_precision(relative_text) == expected


def test_response_date_reads_the_date_header_in_any_case_and_zone() -> None:
    stored = datetime(2026, 10, 16, 11, 47, tzinfo=UTC)
    assert response_date({"Date": "Fri, 16 Oct 2026 11:47:00 GMT"}, REF) == stored
    assert response_date({"DATE": "Fri, 16 Oct 2026 11:47:00 +0000"}, REF) == stored
    zoned = response_date({"date": "Fri, 16 Oct 2026 13:47:00 +0200"}, REF)
    assert zoned == stored and zoned.tzinfo is UTC
    # `-0000` means "UTC, local zone unknown": parsedate_to_datetime returns a naive datetime, taken as UTC.
    assert response_date({"date": "Fri, 16 Oct 2026 11:47:00 -0000"}, REF) == stored


@pytest.mark.parametrize("value", ["", "yesterday", "Fri, 99 Oct 2026 09:47:00 GMT", "16 Oct"])
def test_response_date_falls_back_when_the_header_is_missing_or_malformed(value) -> None:
    fallback = datetime(2026, 10, 17, 1, 2, 3, tzinfo=UTC)
    assert response_date({"date": value}, fallback) == fallback
    assert response_date({"content-type": "application/json"}, fallback) == fallback


def test_from_epoch() -> None:
    seconds = from_epoch(1_792_000_000, precision="exact")
    assert seconds == ParsedDate(
        at=datetime(2026, 10, 14, 17, 46, 40, tzinfo=UTC), precision="exact", raw=None
    )
    assert from_epoch(1_792_000_000_000, millis=True, precision="exact").at == seconds.at
    assert from_epoch(1_792_000_000.5, precision="ingest").at == seconds.at + timedelta(milliseconds=500)
    assert from_epoch(1_792_000_000_123, millis=True, precision="exact").at == seconds.at + timedelta(
        milliseconds=123
    )
    assert seconds.at.tzinfo is UTC


@pytest.mark.parametrize("value", [1_792_000_000_000, 1e30, float("inf"), float("nan"), -1e30])
def test_from_epoch_rejects_values_outside_the_datetime_range(value) -> None:
    with pytest.raises(ValueError, match=r"out of range|NaN"):  # milliseconds sent as seconds, for example
        from_epoch(value, precision="exact")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-10-15T08:12:33Z", datetime(2026, 10, 15, 8, 12, 33, tzinfo=UTC)),
        ("2026-10-15T08:12:33.250+00:00", datetime(2026, 10, 15, 8, 12, 33, 250_000, tzinfo=UTC)),
        ("2026-10-15T10:12:33+02:00", datetime(2026, 10, 15, 8, 12, 33, tzinfo=UTC)),
        ("2026-10-15T04:12:33-04:00", datetime(2026, 10, 15, 8, 12, 33, tzinfo=UTC)),
        ("2026-10-15 08:12:33 UTC", datetime(2026, 10, 15, 8, 12, 33, tzinfo=UTC)),
        ("2026-10-15T08:12:33", datetime(2026, 10, 15, 8, 12, 33, tzinfo=UTC)),  # no zone: UTC
        ("2026-10-15", datetime(2026, 10, 15, 0, 0, tzinfo=UTC)),
    ],
)
def test_parse_iso(text, expected) -> None:
    d = parse_iso(text, precision="exact")
    assert d == ParsedDate(at=expected, precision="exact", raw=text)
    assert d is not None and d.at.tzinfo is UTC


@pytest.mark.parametrize(
    "text", ["", "gestern", "15.10.2026", "2026-13-45T00:00:00Z", "2026-10-15T25:00:00Z"]
)
def test_parse_iso_rejects_what_is_not_iso(text) -> None:
    assert parse_iso(text, precision="day") is None


def test_parse_iso_precision_is_passed_through() -> None:
    d = parse_iso("2026-10-15T08:12:33Z", precision="ingest")
    assert d is not None and d.precision == "ingest"
    assert parse_iso(" 2026-10-15T08:12:33Z ", precision="day") is not None
    assert BERLIN.key == "Europe/Berlin" and UTC is UTC
