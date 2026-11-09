"""The admission gate of the api's Argon2 work (spec §16.3; ruling T21-e).

One Argon2id computation (`passwords.ARGON2`) takes 64 MiB, and the login is unauthenticated: without a bound,
a burst of logins would run as many computations at once as the default executor has threads and exhaust the
api container's memory (`mem_limit: 1g`) before a single failure is recorded. Every Argon2 computation the api
makes (the login's `check_password` or `dummy_verify`, and its rehash `hash_password`) therefore goes through
the process's one `Argon2Gate` (`app.state.argon2_gate`, made by `create_app`):

- a request holds one of `SLOTS` slots for its whole verification (`slot`), and the computations run on a pool
  of `SLOTS` threads (`run`): at most two run at once, also when a request is cancelled while its computation
  is still running (the thread finishes it before it takes the next one);
- at most `QUEUE` requests wait for a slot, in the order they came; one more is refused at once with
  `GateFull`, before any Argon2 work.

The gate holds no connection and no transaction; whoever holds a slot does its database work around the
computations, never during them. The counters are read and changed on the event loop only. The api lifespan
closes the gate when it ends (`close`, M0 Carry-forward 7): the pool's threads end with the app.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Final

SLOTS: Final = 2
QUEUE: Final = 8


class GateFull(Exception):
    """Every slot is taken and `QUEUE` requests already wait for one."""


class Argon2Gate:
    """At most `slots` requests doing Argon2 work at once, and at most `queue` waiting for a slot."""

    def __init__(self, slots: int = SLOTS, queue: int = QUEUE) -> None:
        self.slots = slots
        self.queue = queue
        self._semaphore = asyncio.Semaphore(slots)
        self._admitted = 0  # the requests that hold a slot or wait for one
        self._pool = ThreadPoolExecutor(max_workers=slots, thread_name_prefix="argon2")

    @property
    def admitted(self) -> int:
        """How many requests hold a slot or wait for one."""
        return self._admitted

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Hold a slot for the block, waiting in turn; `GateFull` at once when `queue` requests wait."""
        if self._admitted >= self.slots + self.queue:
            raise GateFull
        self._admitted += 1
        try:
            async with self._semaphore:
                yield
        finally:
            self._admitted -= 1

    async def run[T, *Ts](self, fn: Callable[[*Ts], T], *args: *Ts) -> T:
        """`fn(*args)` on one of the gate's threads; the caller holds a slot. `RuntimeError` after `close`."""
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    def close(self) -> None:
        """Shut the pool down: the computations still queued are cancelled, a running one is waited for, and
        every later `run` raises `RuntimeError`. A second close does nothing."""
        self._pool.shutdown(wait=True, cancel_futures=True)
