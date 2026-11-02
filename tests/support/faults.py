"""In-process fault points: `PausePoint` stands in for `JOBHUNTER_FAULT=<point>:pause` in the test process.

A real `pause` stops the whole process with SIGSTOP, which a crash test can do to a child but an integration
test cannot do to itself. A test monkeypatches a module's `afault_point` with a `PausePoint` instead: the
first coroutine that reaches the point waits there until the test calls `resume()`, while the test's other
tasks (a rival worker's claim, an admission) go on. Every other point passes, as an unarmed fault point does.

M2 Task 17 adds the crash side (Task 21 reuses it): `raising_at(point)` is an `afault_point` replacement
that raises `SimulatedCrash` at `point`, inside the transaction that reached it, so the transaction rolls
back as it would if the process had been killed there; `no_fault` is the unarmed replacement, which puts a
module back to passing every point.
"""

import asyncio
from collections.abc import Awaitable, Callable


class PausePoint:
    """An `afault_point` replacement that holds the first coroutine reaching `point` until `resume()`.

    `reached` is set when it arrives (the readiness acknowledgement, like `fault_point_reached`). Only that
    first arrival is held: a later one (the rival worker's, in the same process) passes at once. With
    `arrival=n` (M2 Task 13) the n-th arrival is the one held, and the earlier ones pass: the point after a
    node's second logical call, or a gateway's second reservation within one call.
    """

    def __init__(self, point: str, *, arrival: int = 1) -> None:
        if arrival < 1:
            raise ValueError("arrivals count from 1")
        self.point = point
        self.arrival = arrival
        self.arrivals = 0
        self.reached = asyncio.Event()
        self._resumed = asyncio.Event()

    async def __call__(self, name: str) -> None:
        if name != self.point or self.reached.is_set():
            return
        self.arrivals += 1
        if self.arrivals < self.arrival:
            return
        self.reached.set()
        await self._resumed.wait()

    def resume(self) -> None:
        self._resumed.set()


class PausePoints:
    """An `afault_point` replacement holding the first arrival at each of several points (`PausePoint`s):
    `pauses[point].reached` and `pauses[point].resume()`."""

    def __init__(self, *points: str) -> None:
        self._points = {point: PausePoint(point) for point in points}

    def __getitem__(self, point: str) -> PausePoint:
        return self._points[point]

    async def __call__(self, name: str) -> None:
        pause = self._points.get(name)
        if pause is not None:
            await pause(name)


class SimulatedCrash(Exception):
    """The failure `raising_at` injects, a stand-in for a killed process; its argument is the point."""


def raising_at(point: str) -> Callable[[str], Awaitable[None]]:
    """An `afault_point` replacement that raises `SimulatedCrash` at `point` and passes every other point."""

    async def fault(name: str) -> None:
        if name == point:
            raise SimulatedCrash(point)

    return fault


class SimulatedKill(BaseException):
    """What `killing_at` injects: a killed process that no `except Exception` of the code under test can
    absorb (a loop that isolates one row's failure from the others, M3 Ruling T17-a, still dies with it)."""


def killing_at(point: str) -> Callable[[str], Awaitable[None]]:
    """An `afault_point` replacement that raises `SimulatedKill` at `point` and passes every other point."""

    async def fault(name: str) -> None:
        if name == point:
            raise SimulatedKill(point)

    return fault


async def no_fault(name: str) -> None:
    """An `afault_point` replacement that passes every point, as an unarmed fault point does."""
