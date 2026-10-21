from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

import pytest
from jobhunter.worker import operations as ops
from jobhunter.worker.runtime import ShutdownState
from jobhunter.worker.tasks import run_operation
from procrastinate.job_context import JobContext
from sqlalchemy.ext.asyncio import AsyncEngine


async def test_run_operation_body_executes_the_operation_with_the_runtime(monkeypatch) -> None:
    calls: list[tuple[object, int, int, object]] = []

    async def fake_execute_operation(
        engine: object, operation_id: int, *, epoch: int, deps: object, on_superseded: Callable[[], None]
    ) -> None:
        calls.append((engine, operation_id, epoch, deps))
        on_superseded()  # as a claim that the epoch fence refused calls it

    monkeypatch.setattr(ops, "execute_operation", fake_execute_operation)
    engine, deps = object(), object()
    # the `WorkerRuntime` fields the body reads (Task 11: the operation dependencies too)
    runtime = SimpleNamespace(engine=engine, epoch=7, shutdown=ShutdownState(), operation_deps=deps)
    context = cast(JobContext, SimpleNamespace(additional_context={"runtime": runtime}))
    await run_operation(context, operation_id=5)
    assert calls == [(engine, 5, 7, deps)]
    # Ruling T24-a: a superseded delivery asks this worker's runtime for the graceful stop.
    assert runtime.shutdown.requested.is_set()


def test_register_operation_handler_refuses_a_second_handler_for_a_kind(monkeypatch) -> None:
    monkeypatch.setattr(ops, "HANDLERS", {})

    async def first(ctx: ops.OperationContext) -> None:
        return None

    async def second(ctx: ops.OperationContext) -> None:
        return None

    assert ops.register_operation_handler("export")(first) is first
    assert ops.register_operation_handler("export")(first) is first  # registering it again is idempotent
    with pytest.raises(ValueError, match="export"):
        ops.register_operation_handler("export")(second)
    assert ops.register_operation_handler("purge")(second) is second
    assert ops.HANDLERS == {"export": first, "purge": second}


def test_a_failure_hook_is_registered_apart_from_the_handlers(monkeypatch) -> None:
    """Plan Deviation 13: `on_failed` goes to `FAILURE_HOOKS`, so `HANDLERS` keeps the handlers only."""
    monkeypatch.setattr(ops, "HANDLERS", {})
    monkeypatch.setattr(ops, "FAILURE_HOOKS", {})

    async def handler(ctx: ops.OperationContext) -> None:
        return None

    async def hook(conn: object, operation_id: int, payload: object, error_code: str | None) -> None:
        return None

    async def other_hook(conn: object, operation_id: int, payload: object, error_code: str | None) -> None:
        return None

    assert ops.register_operation_handler("export", on_failed=hook)(handler) is handler
    assert ops.register_operation_handler("export", on_failed=hook)(handler) is handler  # idempotent
    with pytest.raises(ValueError, match="export"):
        ops.register_operation_handler("export", on_failed=other_hook)(handler)
    assert ops.HANDLERS == {"export": handler} and ops.FAILURE_HOOKS == {"export": hook}


def test_an_operation_context_is_frozen_and_ends_unset() -> None:
    """`deps` defaults to `None` (the M1 handlers run without it); the outcome holder is no init argument."""
    import dataclasses
    import uuid

    claim = ops.OperationClaim(1, uuid.uuid4(), "export", {}, 0)
    ctx = ops.OperationContext(claim=claim, engine=cast(AsyncEngine, None))
    assert ctx.deps is None and ctx.ended is None
    assert [f.name for f in dataclasses.fields(ctx) if f.init] == ["claim", "engine", "deps"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.deps = None  # type: ignore[misc]
