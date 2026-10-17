from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from jobhunter.worker.tick import CATCH_UP_AFTER, compute_due_slots, trigger_for

A = datetime(2026, 10, 16, 4, tzinfo=UTC)
H6 = 21600


def test_due_slots_half_open_interval() -> None:
    assert compute_due_slots(A, H6, A, A + timedelta(hours=12)) == [
        A + timedelta(hours=6),
        A + timedelta(hours=12),
    ]
    assert compute_due_slots(A, H6, A, A + timedelta(hours=5, minutes=59)) == []


def test_slots_with_clock_behind_cursor_are_empty() -> None:  # Review Focus 5 (pure part)
    assert compute_due_slots(A, H6, A + timedelta(hours=12), A + timedelta(hours=1)) == []


def test_dst_never_shifts_utc_slots() -> None:
    a = datetime(2026, 10, 24, 4, tzinfo=UTC)  # 06:00 CEST; DST ends 2026-10-25
    assert compute_due_slots(a, 86400, a, a + timedelta(days=3)) == [a + timedelta(days=d) for d in (1, 2, 3)]


def test_first_evaluation_without_cursor_returns_only_latest() -> None:
    assert compute_due_slots(A, H6, None, A + timedelta(hours=13)) == [A + timedelta(hours=12)]


def test_slots_are_utc_instants_whatever_the_zone_of_the_inputs() -> None:
    berlin = ZoneInfo("Europe/Berlin")  # wall-clock arithmetic in this zone would drift over 2026-10-25
    a = datetime(2026, 10, 24, 4, tzinfo=UTC).astimezone(berlin)
    due = compute_due_slots(a, 86400, a, a + timedelta(days=3))
    assert due == [datetime(2026, 10, 24, 4, tzinfo=UTC) + timedelta(days=d) for d in (1, 2, 3)]
    assert all(slot.utcoffset() == timedelta(0) for slot in due)


def test_slots_before_the_anchor_use_floor_division() -> None:
    assert compute_due_slots(A, H6, None, A - timedelta(hours=1)) == [A - timedelta(hours=6)]
    assert compute_due_slots(A, H6, A - timedelta(hours=13), A + timedelta(hours=1)) == [
        A - timedelta(hours=12),
        A - timedelta(hours=6),
        A,
    ]


def test_naive_datetimes_and_bad_intervals_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        compute_due_slots(A, H6, None, datetime(2026, 10, 17))  # noqa: DTZ001
    with pytest.raises(ValueError, match="positive"):
        compute_due_slots(A, 0, None, A)


def test_trigger_is_schedule_up_to_exactly_five_minutes_and_catch_up_after() -> None:
    assert CATCH_UP_AFTER == timedelta(minutes=5)
    assert trigger_for(A, A) == "schedule"
    assert trigger_for(A + timedelta(minutes=5), A) == "schedule"
    assert trigger_for(A + timedelta(minutes=5, microseconds=1), A) == "catch_up"
    assert trigger_for(A + timedelta(minutes=5, seconds=1), A) == "catch_up"


def test_trigger_compares_instants_whatever_the_zone_of_the_inputs() -> None:
    berlin = ZoneInfo("Europe/Berlin")
    slot = datetime(2026, 10, 25, 0, 30, tzinfo=UTC).astimezone(berlin)  # 02:30 CEST, an hour before DST ends
    five_minutes_later = datetime(2026, 10, 25, 0, 35, tzinfo=UTC).astimezone(berlin)
    just_after = datetime(2026, 10, 25, 0, 36, tzinfo=UTC).astimezone(berlin)
    assert trigger_for(five_minutes_later, slot) == "schedule"
    assert trigger_for(just_after, slot) == "catch_up"
    # Same zone object on both sides: wall-clock subtraction would see 02:30 -> 02:35 as five minutes.
    after_the_change = datetime(2026, 10, 25, 1, 35, tzinfo=UTC).astimezone(berlin)  # 02:35 CET, 65 min on
    assert trigger_for(after_the_change, slot) == "catch_up"
