"""`Argon2Gate.close` (M0 Carry-forward 7, M4 Task 1): the gate's thread pool shuts down, its threads end, a
queued computation never runs, and a computation asked for afterwards is refused."""

import asyncio
import threading

import pytest
from jobhunter.api.argon2_gate import Argon2Gate


def _gate_threads(before: set[threading.Thread]) -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t not in before and t.name.startswith("argon2")]


async def test_close_shuts_the_pool_and_is_idempotent() -> None:
    before = set(threading.enumerate())
    gate = Argon2Gate()
    assert await gate.run(pow, 2, 10) == 1024
    started = _gate_threads(before)
    assert started  # the computation ran on a thread of the gate's own pool
    gate.close()
    assert not any(thread.is_alive() for thread in started)  # joined: `wait=True`
    gate.close()  # a second close is a no-op
    with pytest.raises(RuntimeError):
        await gate.run(pow, 2, 10)


async def test_close_cancels_the_queued_computations() -> None:
    gate = Argon2Gate(slots=1)
    started, release = threading.Event(), threading.Event()
    ran: list[int] = []

    def hold() -> bool:
        started.set()
        return release.wait(5.0)  # bounded: a failing test ends, never hangs

    running = asyncio.ensure_future(gate.run(hold))
    assert await asyncio.to_thread(started.wait, 5.0)  # the gate's one thread is taken
    queued = asyncio.ensure_future(gate.run(ran.append, 1))  # waits behind it
    await asyncio.sleep(0)  # submitted
    closing = asyncio.ensure_future(asyncio.to_thread(gate.close))  # waits for the running one
    with pytest.raises(asyncio.CancelledError):
        await queued  # cancelled before the thread is free: `cancel_futures=True`
    release.set()
    await closing
    assert await running is True and ran == []
