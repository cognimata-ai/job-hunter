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
    first arrival is held: a later one (the rival worker's, in the same process) passes at once.
    """

    def __init__(self, point: str) -> None:
        self.point = point
        self.reached = asyncio.Event()
        self._resumed = asyncio.Event()

    async def __call__(self, name: str) -> None:
        if name != self.point or self.reached.is_set():
            return
        self.reached.set()
        await self._resumed.wait()

    def resume(self) -> None:
        self._resumed.set()
