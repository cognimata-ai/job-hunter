import asyncio
import re
from pathlib import Path

from jobhunter.worker import shutdown
from jobhunter.worker.runtime import ShutdownState

from tests.support.clock import FakeClock
from tests.support.logs import logged

COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "compose.dev.yaml"


def forever() -> asyncio.Future[None]:
    return asyncio.get_running_loop().create_future()  # never resolved: a loop that runs until cancelled


def test_the_grace_period_is_100_s() -> None:
    assert shutdown.GRACE_S == 100


async def test_a_requested_shutdown_stops_admission_before_it_cancels_every_loop_and_returns_0(
    caplog,
) -> None:
    state = ShutdownState()
    drained: list[tuple[str, bool]] = []

    async def loop(name: str) -> None:
        try:
            await forever()
        except asyncio.CancelledError:
            drained.append((name, state.admission_stopped))
            await asyncio.sleep(0)  # like procrastinate: the loop drains before its cancellation ends it
            raise

    loops = [asyncio.create_task(loop(name), name=f"jh-{name}") for name in ("collect", "aux")]
    coordinator = asyncio.create_task(shutdown.coordinate(loops, state))
    await asyncio.sleep(0)
    assert not coordinator.done() and not state.admission_stopped
    state.requested.set()
    assert await coordinator == 0
    assert sorted(drained) == [("aux", True), ("collect", True)]
    assert all(task.cancelled() for task in loops)
    assert logged(caplog, "worker_loop_ended") == []


async def test_a_loop_that_ends_by_itself_stops_the_others_and_returns_1(caplog) -> None:
    state = ShutdownState()
    fail = asyncio.Event()

    async def broken() -> None:
        await fail.wait()
        raise RuntimeError("detail that must not be logged")

    async def healthy() -> None:
        await forever()

    loops = [asyncio.create_task(broken(), name="jh-collect"), asyncio.create_task(healthy(), name="jh-aux")]
    coordinator = asyncio.create_task(shutdown.coordinate(loops, state))
    await asyncio.sleep(0)
    fail.set()
    assert await coordinator == 1
    assert state.admission_stopped and not state.requested.is_set()
    assert loops[1].cancelled()
    (event,) = logged(caplog, "worker_loop_ended")
    assert (event["worker"], event["error_class"]) == ("jh-collect", "RuntimeError")
    assert "must not be logged" not in caplog.text


async def test_loops_that_outlive_the_grace_are_logged_and_left_to_the_exit(caplog) -> None:
    # The stop begins at 0, and every later reading of the clock is `GRACE_S` later: the grace is used up when
    # the coordinator would wait for the loops, so it does not wait at all.
    readings = iter([0.0])

    def clock() -> float:
        return next(readings, float(shutdown.GRACE_S))

    state = ShutdownState()
    release = asyncio.Event()

    async def stubborn() -> None:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue  # ignores its cancellation until the test releases it

    loops = [asyncio.create_task(stubborn(), name="jh-collect")]
    state.requested.set()
    try:
        assert await shutdown.coordinate(loops, state, clock=clock) == 0
        (event,) = logged(caplog, "worker_loops_not_stopped")
        assert event["workers"] == ["jh-collect"] and not loops[0].done()
    finally:  # also when an assertion failed: the stubborn loop would outlive the test's event loop
        release.set()
        await loops[0]


# --- Final fix wave, item 6: one deadline for the whole stop, within Compose's stop grace ------------------


def test_the_whole_stop_fits_in_the_compose_stop_grace_with_room_for_the_exit() -> None:
    (grace,) = re.findall(r"stop_grace_period: (\d+)s", COMPOSE.read_text(encoding="utf-8"))
    assert shutdown.GRACE_S + shutdown.TEARDOWN_S == shutdown.STOP_BUDGET_S < int(grace)


async def test_the_stop_starts_one_deadline_and_the_teardown_gets_what_the_loops_left() -> None:
    clock = FakeClock(500.0)
    state = ShutdownState()

    async def drains() -> None:
        try:
            await forever()
        except asyncio.CancelledError:
            clock.now += 30  # its running jobs took 30 s to finish
            raise

    loops = [asyncio.create_task(drains(), name="jh-collect")]
    state.requested.set()
    assert await shutdown.coordinate(loops, state, clock=clock) == 0
    deadline = state.stop_deadline
    assert deadline is not None and deadline.at == 500.0 + shutdown.STOP_BUDGET_S
    assert deadline.remaining() == shutdown.STOP_BUDGET_S - 30  # the rest of the one budget ...
    assert deadline.remaining(15.0) == 15.0  # ... capped by a step's own bound
    clock.now = deadline.at + 1
    assert deadline.remaining() == 0.0  # never negative
