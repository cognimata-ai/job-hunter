"""UUIDv7 generation (RFC 9562 §5.7, with the §6.2 method 1 counter).

Layout: 48-bit Unix milliseconds, version `0b0111`, a 12-bit counter, variant `0b10`, 62 random bits.
Within one millisecond the counter increments, so ids from one process are strictly increasing. The
state is per process and guarded by a lock, so threads share one ordered sequence.
"""

import os
import threading
import time
import uuid

_COUNTER_MAX = 0xFFF
_RANDOM_MASK = (1 << 62) - 1

_lock = threading.Lock()
_last_ms = 0
_counter = 0


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def uuid7() -> uuid.UUID:
    """A new UUIDv7, greater than every id this process generated before."""
    global _last_ms, _counter
    with _lock:
        now = _now_ms()
        if now > _last_ms:
            _last_ms, _counter = now, 0
        else:
            # Same millisecond, or the clock went backwards: keep the stored millisecond.
            _counter += 1
            if _counter > _COUNTER_MAX:
                # Counter exhausted: borrow the next millisecond (RFC 9562 §6.2).
                _last_ms, _counter = _last_ms + 1, 0
        ms, counter = _last_ms, _counter
    random_bits = int.from_bytes(os.urandom(8)) & _RANDOM_MASK
    return uuid.UUID(int=(ms << 80) | (0x7 << 76) | (counter << 64) | (0b10 << 62) | random_bits)
