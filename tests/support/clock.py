"""Injected clocks for tests: they move only when the test moves them.

- `FakeClock` is a monotonic clock reading `now`, which the test sets or advances itself.
- `ManualSleep` (M3 Task 10: the claim heartbeat's cadence) is an injected `sleep` that waits until the test
  releases it: each call records the seconds asked for in `requested` and waits; `release()` ends the oldest
  waiting call (waiting for one to arrive first), and `until(n)` waits until `n` calls have been made, so a
  test knows the loop finished its beat and slept again. M3 Task 11 adds `release_all()`: every call waiting
  now ends at once (one whose caller was cancelled meanwhile is skipped), for the many beats of a cycle.
"""

import asyncio


class FakeClock:
    """A `Callable[[], float]` clock reading `now`; the test sets or advances `now` itself."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class ManualSleep:
    """An injected `sleep` the test releases (module docstring)."""

    def __init__(self) -> None:
        self.requested: list[float] = []
        self._waiting: asyncio.Queue[asyncio.Future[None]] = asyncio.Queue()
        self._called = asyncio.Condition()

    async def __call__(self, seconds: float) -> None:
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        async with self._called:
            self.requested.append(seconds)
            self._called.notify_all()
        await self._waiting.put(done)
        await done

    async def release(self) -> None:
        """End the oldest waiting sleep (the next one to arrive, when none waits yet)."""
        done = await asyncio.wait_for(self._waiting.get(), 10)
        done.set_result(None)

    def release_all(self) -> int:
        """End every sleep waiting now, without waiting for one (a sleep whose caller was cancelled meanwhile
        is skipped); how many it ended."""
        ended = 0
        while not self._waiting.empty():
            done = self._waiting.get_nowait()
            if not done.done():
                done.set_result(None)
                ended += 1
        return ended

    async def until(self, calls: int) -> None:
        """Wait until `calls` sleeps have been asked for."""
        async with self._called:
            await asyncio.wait_for(self._called.wait_for(lambda: len(self.requested) >= calls), 10)
