from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st
from jobhunter.worker.tick import compute_due_slots

A = datetime(2026, 1, 1, tzinfo=UTC)


@given(st.integers(3600, 604800), st.lists(st.integers(0, 10**7), min_size=1, max_size=40))
def test_every_slot_exactly_once_in_order(interval: int, offsets: list[int]) -> None:
    cursor, seen = A, []
    for t in sorted(offsets):
        due = compute_due_slots(A, interval, cursor, A + timedelta(seconds=t))
        seen += due
        cursor = due[-1] if due else cursor
    expected = [A + timedelta(seconds=k * interval) for k in range(1, max(offsets) // interval + 1)]
    assert seen == expected
