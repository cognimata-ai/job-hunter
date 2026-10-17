import asyncio

from jobhunter.worker import shutdown
from jobhunter.worker.runtime import ShutdownState

from tests.support.logs import logged


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
