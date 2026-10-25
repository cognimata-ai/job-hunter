"""The anchor's DST rule (spec §13.1 `schedule.anchor_local`, M0 Carry-forward 1; plan Task 15).

`local_anchor_to_utc(day, hhmm, tz)` converts the local anchor once at save. A wall time the zone skips (the
spring-forward gap) maps to the first valid instant after the gap, the transition itself; a wall time the zone
repeats (the autumn fold) maps to its first occurrence (`fold=0`).
"""

from datetime import UTC, date, datetime

import pytest
from jobhunter.db.seed import anchor_utc_for, local_anchor_to_utc

BERLIN = "Europe/Berlin"


def utc(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def test_a_time_in_the_spring_forward_gap_is_the_first_instant_after_it() -> None:
    # 2026-03-29: 02:00 CET jumps to 03:00 CEST at 01:00 UTC, so 02:30 does not exist
    assert local_anchor_to_utc(date(2026, 3, 29), "02:30", BERLIN) == utc(2026, 3, 29, 1, 0)


def test_a_time_in_the_autumn_fold_is_its_first_occurrence() -> None:
    # 2026-10-25: 03:00 CEST falls back to 02:00 CET at 01:00 UTC, so 02:30 occurs at 00:30 and 01:30 UTC
    assert local_anchor_to_utc(date(2026, 10, 25), "02:30", BERLIN) == utc(2026, 10, 25, 0, 30)


@pytest.mark.parametrize(
    ("hhmm", "expected"),
    [
        ("01:59", utc(2026, 3, 29, 0, 59)),  # before the gap: CET
        ("02:00", utc(2026, 3, 29, 1, 0)),  # the first skipped minute
        ("02:59", utc(2026, 3, 29, 1, 0)),  # the last skipped minute
        ("03:00", utc(2026, 3, 29, 1, 0)),  # the first valid instant after the gap: CEST
        ("03:01", utc(2026, 3, 29, 1, 1)),
    ],
)
def test_the_edges_of_the_berlin_gap(hhmm: str, expected: datetime) -> None:
    assert local_anchor_to_utc(date(2026, 3, 29), hhmm, BERLIN) == expected


@pytest.mark.parametrize(
    ("hhmm", "expected"),
    [
        ("01:59", utc(2026, 10, 24, 23, 59)),  # before the fold: CEST
        ("02:00", utc(2026, 10, 25, 0, 0)),  # the first repeated minute, its CEST occurrence
        ("02:59", utc(2026, 10, 25, 0, 59)),  # the last repeated minute, its CEST occurrence
        ("03:00", utc(2026, 10, 25, 2, 0)),  # after the fold: CET
    ],
)
def test_the_edges_of_the_berlin_fold(hhmm: str, expected: datetime) -> None:
    assert local_anchor_to_utc(date(2026, 10, 25), hhmm, BERLIN) == expected


def test_other_zones_and_a_half_hour_shift() -> None:
    # New York: 02:00 EST jumps to 03:00 EDT at 07:00 UTC; 01:30 repeats on 1 November, first as EDT
    assert local_anchor_to_utc(date(2026, 3, 8), "02:30", "America/New_York") == utc(2026, 3, 8, 7, 0)
    assert local_anchor_to_utc(date(2026, 11, 1), "01:30", "America/New_York") == utc(2026, 11, 1, 5, 30)
    # Lord Howe shifts by 30 minutes: 02:00 (+10:30) jumps to 02:30 (+11:00) at 15:30 UTC the day before
    assert local_anchor_to_utc(date(2026, 10, 4), "02:15", "Australia/Lord_Howe") == utc(2026, 10, 3, 15, 30)
    assert local_anchor_to_utc(date(2026, 10, 4), "02:30", "Australia/Lord_Howe") == utc(2026, 10, 3, 15, 30)


def test_a_zone_without_dst_is_a_fixed_offset() -> None:
    assert local_anchor_to_utc(date(2026, 3, 29), "02:30", "UTC") == utc(2026, 3, 29, 2, 30)
    assert local_anchor_to_utc(date(2026, 3, 29), "02:30", "Asia/Tokyo") == utc(2026, 3, 28, 17, 30)


def test_the_anchor_on_the_local_date_of_a_gap_day() -> None:
    # A save at 10:00 UTC on the gap day: the anchor 02:30 of that local date is the gap's end
    assert anchor_utc_for(utc(2026, 3, 29, 10, 0), "02:30", BERLIN) == utc(2026, 3, 29, 1, 0)
    assert anchor_utc_for(utc(2026, 10, 25, 10, 0), "02:30", BERLIN) == utc(2026, 10, 25, 0, 30)
