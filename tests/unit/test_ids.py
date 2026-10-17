# tests/unit/test_ids.py
import itertools
import time

from jobhunter import ids as idgen
from jobhunter.ids import uuid7


def test_uuid7_layout_and_order() -> None:
    before = time.time_ns() // 1_000_000
    ids = [uuid7() for _ in range(1000)]
    after = time.time_ns() // 1_000_000
    for u in ids:
        assert u.version == 7 and (u.int >> 62) & 0b11 == 0b10
        assert before <= (u.int >> 80) <= after
    assert ids == sorted(ids) and len(set(ids)) == 1000


def test_uuid7_stays_ordered_when_the_counter_overflows_within_one_millisecond(monkeypatch) -> None:
    start_ms = 1_800_000_000_000
    monkeypatch.setattr(idgen, "_now_ms", lambda: start_ms)
    monkeypatch.setattr(idgen, "_last_ms", 0)
    monkeypatch.setattr(idgen, "_counter", 0)

    out = [uuid7() for _ in range(2 * 4096 + 5)]

    assert all(a < b for a, b in itertools.pairwise(out))
    assert [u.int >> 80 for u in (out[0], out[4095], out[4096], out[8191], out[8192], out[-1])] == [
        start_ms,
        start_ms,
        start_ms + 1,
        start_ms + 1,
        start_ms + 2,
        start_ms + 2,
    ]
    assert [(u.int >> 64) & 0xFFF for u in (out[0], out[4095], out[4096], out[8192])] == [0, 0xFFF, 0, 0]
    assert all(u.version == 7 and (u.int >> 62) & 0b11 == 0b10 for u in out)


def test_uuid7_keeps_its_last_millisecond_when_the_clock_goes_backwards(monkeypatch) -> None:
    now = [1_800_000_000_000]
    monkeypatch.setattr(idgen, "_now_ms", lambda: now[0])
    monkeypatch.setattr(idgen, "_last_ms", 0)
    monkeypatch.setattr(idgen, "_counter", 0)

    first = uuid7()
    now[0] -= 5_000
    second = uuid7()
    now[0] += 5_001
    third = uuid7()

    assert first < second < third
    assert first.int >> 80 == second.int >> 80 == 1_800_000_000_000
    assert third.int >> 80 == 1_800_000_000_001 and (third.int >> 64) & 0xFFF == 0


def test_uuid7_draws_fresh_random_bits_for_every_id(monkeypatch) -> None:
    monkeypatch.setattr(idgen, "_now_ms", lambda: 1_800_000_000_000)
    monkeypatch.setattr(idgen, "_last_ms", 0)
    monkeypatch.setattr(idgen, "_counter", 0)
    tails = {uuid7().int & ((1 << 62) - 1) for _ in range(5)}
    assert len(tails) == 5
