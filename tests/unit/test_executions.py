import asyncio
import inspect
from typing import Any

import pytest
from jobhunter.errors import FencedOut
from jobhunter.worker.executions import AlreadyExecuting, ExecutionRegistry, run_owned
from jobhunter.worker.heartbeat import heartbeat_loop

from tests.support.logs import logged


def _live_tasks_named(name: str) -> list[asyncio.Task[Any]]:
    return [task for task in asyncio.all_tasks() if task.get_name() == name]


async def _alive() -> bool:
    return True


async def test_one_live_task_per_key_and_cancel_and_await() -> None:
    reg, started = ExecutionRegistry(), asyncio.Event()

    async def forever() -> None:
        started.set()
        await asyncio.sleep(3600)

    task = reg.start(("run", 1), 1, forever())
    await started.wait()
    with pytest.raises(AlreadyExecuting):
        reg.start(("run", 1), 2, forever())
    assert await reg.cancel_and_await(("run", 1)) is True and task.cancelled() and reg.get(("run", 1)) is None
    assert await reg.cancel_and_await(("run", 1)) is False


async def test_refused_start_closes_the_coroutine_and_get_returns_the_token() -> None:
    reg, started = ExecutionRegistry(), asyncio.Event()

    async def forever() -> None:
        started.set()
        await asyncio.sleep(3600)

    task = reg.start(("operation", 5), "claim-a", forever())
    await started.wait()
    refused = forever()
    with pytest.raises(AlreadyExecuting):
        reg.start(("operation", 5), "claim-b", refused)
    assert inspect.getcoroutinestate(refused) == inspect.CORO_CLOSED  # never left un-awaited
    assert reg.get(("operation", 5)) == ("claim-a", task)
    assert reg.get(("run", 5)) is None  # the key is (kind, id), not the id alone
    await reg.cancel_and_await(("operation", 5))


async def test_finished_tasks_leave_the_registry_and_free_the_key() -> None:
    reg = ExecutionRegistry()

    async def done() -> None:
        return None

    first = reg.start(("run", 2), 1, done())
    await first
    assert reg.get(("run", 2)) is None
    second = reg.start(("run", 2), 2, done())  # the key is free again
    await second
    assert reg.get(("run", 2)) is None


async def test_cancel_and_await_gives_up_on_a_task_that_ignores_cancellation(caplog) -> None:
    reg, started, cancelled, release = ExecutionRegistry(), asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def stubborn() -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()  # ignores the cancellation until released

    task = reg.start(("operation", 7), "claim", stubborn())
    await started.wait()
    # The task cannot end before `release` is set, so the short timeout decides nothing by itself.
    assert await reg.cancel_and_await(("operation", 7), timeout_s=0.05) is False
    assert cancelled.is_set() and reg.get(("operation", 7)) == ("claim", task)
    [event] = logged(caplog, "execution_cancel_timeout")
    assert event["key"] == "operation:7"
    assert set(event) <= {"event", "key", "level", "logger", "ts"}  # the key only
    release.set()
    await task
    assert reg.get(("operation", 7)) is None


async def test_cancel_and_await_never_swallows_the_callers_cancellation() -> None:
    reg, started, cancelled, release = ExecutionRegistry(), asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def stubborn() -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    task = reg.start(("run", 3), 1, stubborn())
    await started.wait()
    caller = asyncio.create_task(reg.cancel_and_await(("run", 3)))
    await cancelled.wait()  # the registry has cancelled the execution and now waits for it
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert caller.cancelled()
    release.set()
    await task


async def test_cancel_all_never_swallows_the_callers_cancellation() -> None:
    reg, started, cancelled, release = ExecutionRegistry(), asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def stubborn() -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    task = reg.start(("operation", 12), 1, stubborn())
    await started.wait()
    caller = asyncio.create_task(reg.cancel_all())
    await cancelled.wait()  # cancel_all has cancelled the execution and now waits for it
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert caller.cancelled()
    release.set()
    await task


async def test_cancel_all_counts_the_executions_that_stopped(caplog) -> None:
    reg, release = ExecutionRegistry(), asyncio.Event()
    started = [asyncio.Event() for _ in range(3)]

    async def forever(i: int) -> None:
        started[i].set()
        await asyncio.sleep(3600)

    async def stubborn() -> None:
        started[2].set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await release.wait()

    tasks = [reg.start(("run", 10), 1, forever(0)), reg.start(("operation", 10), 1, forever(1))]
    held = reg.start(("operation", 11), 1, stubborn())
    for event in started:
        await event.wait()
    assert await reg.cancel_all(timeout_s=0.05) == 2
    assert all(task.cancelled() for task in tasks)
    assert reg.get(("run", 10)) is None and reg.get(("operation", 10)) is None
    assert [m["key"] for m in logged(caplog, "execution_cancel_timeout")] == ["operation:11"]
    release.set()
    await held
    assert await reg.cancel_all() == 0


async def test_heartbeat_loop_reports_a_lost_beat_once_and_returns() -> None:
    beats, lost = iter([True, True, False]), list[int]()

    async def beat() -> bool:
        return next(beats)

    await asyncio.wait_for(heartbeat_loop(beat, period_s=0, on_lost=lambda: lost.append(1)), 5)
    assert lost == [1]


async def test_heartbeat_loop_logs_the_failure_class_only_and_reports_loss(caplog) -> None:
    lost: list[int] = []

    async def beat() -> bool:
        raise ConnectionError("password=hunter2 detail")

    await asyncio.wait_for(heartbeat_loop(beat, period_s=0, on_lost=lambda: lost.append(1)), 5)
    assert lost == [1]
    [event] = logged(caplog, "heartbeat_failed")
    assert event["error_class"] == "ConnectionError"
    assert "hunter2" not in caplog.text and "detail" not in caplog.text


async def test_heartbeat_loop_rides_out_the_tolerated_failures_in_a_row(caplog) -> None:
    # M3 Task 10: the claim heartbeat tolerates 3 raising beats in a row; a good beat restarts the count
    outcomes = iter(["raise", "raise", "raise", True, "raise", "raise", "raise", True, False])
    lost: list[int] = []

    async def beat() -> bool:
        outcome = next(outcomes)
        if outcome == "raise":
            raise ConnectionError("password=hunter2 detail")
        return bool(outcome)

    await asyncio.wait_for(
        heartbeat_loop(
            beat, period_s=0, on_lost=lambda: lost.append(1), tolerated_failures=3, label="claim:7"
        ),
        5,
    )
    assert lost == [1]  # the refused beat only
    events = logged(caplog, "heartbeat_failed")
    assert [e["consecutive"] for e in events] == [1, 2, 3, 1, 2, 3]
    assert {(e["error_class"], e["heartbeat"]) for e in events} == {("ConnectionError", "claim:7")}
    assert "hunter2" not in caplog.text and "detail" not in caplog.text


async def test_heartbeat_loop_reports_the_first_failure_past_the_tolerance_as_lost() -> None:
    calls, lost = 0, list[int]()

    async def beat() -> bool:
        nonlocal calls
        calls += 1
        raise ConnectionError("blip")

    await asyncio.wait_for(
        heartbeat_loop(beat, period_s=0, on_lost=lambda: lost.append(1), tolerated_failures=3), 5
    )
    assert (calls, lost) == (4, [1])


async def test_run_owned_maps_return_error_and_fencing_to_outcomes() -> None:
    reg = ExecutionRegistry()

    async def ok() -> None:
        return None

    async def boom() -> None:
        raise ValueError("bad")

    async def fenced() -> None:
        raise FencedOut("operation")

    returned = await run_owned(("operation", 1), "c", ok(), beat=_alive, period_s=3600, registry=reg)
    raised = await run_owned(("operation", 1), "c", boom(), beat=_alive, period_s=3600, registry=reg)
    lost = await run_owned(("operation", 1), "c", fenced(), beat=_alive, period_s=3600, registry=reg)
    assert (returned.status, returned.error) == ("returned", None)
    assert raised.status == "raised" and isinstance(raised.error, ValueError)
    assert (lost.status, lost.error) == ("lost", None)
    assert reg.get(("operation", 1)) is None and not _live_tasks_named("heartbeat:operation:1")


async def test_run_owned_lost_heartbeat_cancels_the_work() -> None:
    reg, started, cancelled = ExecutionRegistry(), asyncio.Event(), asyncio.Event()

    async def work() -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def beat() -> bool:
        await started.wait()
        return False  # another owner holds the row now

    outcome = await asyncio.wait_for(run_owned(("run", 2), 1, work(), beat=beat, period_s=0, registry=reg), 5)
    assert outcome.status == "lost" and cancelled.is_set()
    assert reg.get(("run", 2)) is None and not _live_tasks_named("heartbeat:run:2")


async def test_run_owned_lets_the_callers_cancellation_through_and_stops_the_work() -> None:
    reg, started, cancelled = ExecutionRegistry(), asyncio.Event(), asyncio.Event()

    async def work() -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    runner = asyncio.create_task(run_owned(("run", 4), 1, work(), beat=_alive, period_s=3600, registry=reg))
    await started.wait()
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    assert runner.cancelled() and cancelled.is_set()
    assert reg.get(("run", 4)) is None and not _live_tasks_named("heartbeat:run:4")


async def test_run_owned_counts_work_that_swallowed_its_cancellation_as_lost() -> None:
    # Ruling T10-f: a cancel requested by the heartbeat (or a reconciler, or cancel_all) means ownership is
    # gone or going, even when the work catches the CancelledError and returns normally.
    reg, started, cancelled = ExecutionRegistry(), asyncio.Event(), asyncio.Event()

    async def swallowing() -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()  # and returns as if it had finished

    async def beat() -> bool:
        await started.wait()
        return False

    outcome = await asyncio.wait_for(
        run_owned(("operation", 9), "c", swallowing(), beat=beat, period_s=0, registry=reg), 5
    )
    assert cancelled.is_set() and (outcome.status, outcome.error) == ("lost", None)
    assert reg.get(("operation", 9)) is None and not _live_tasks_named("heartbeat:operation:9")
