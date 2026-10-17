from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from jobhunter.worker.tick import compute_due_slots

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
