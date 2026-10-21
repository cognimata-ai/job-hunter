"""In-process fault points: `PausePoint` stands in for `JOBHUNTER_FAULT=<point>:pause` in the test process.

A real `pause` stops the whole process with SIGSTOP, which a crash test can do to a child but an integration
test cannot do to itself. A test monkeypatches a module's `afault_point` with a `PausePoint` instead: the
first coroutine that reaches the point waits there until the test calls `resume()`, while the test's other
tasks (a rival worker's claim, an admission) go on. Every other point passes, as an unarmed fault point does.
"""

import asyncio


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
