from types import SimpleNamespace
from typing import cast

import pytest
from jobhunter.worker import operations as ops
from jobhunter.worker.tasks import run_operation
from procrastinate.job_context import JobContext


async def test_run_operation_body_executes_the_operation_with_the_runtime(monkeypatch) -> None:
    calls: list[tuple[object, int, int]] = []

    async def fake_execute_operation(engine: object, operation_id: int, *, epoch: int) -> None:
        calls.append((engine, operation_id, epoch))

    monkeypatch.setattr(ops, "execute_operation", fake_execute_operation)
    engine = object()
    runtime = SimpleNamespace(engine=engine, epoch=7)  # the `WorkerRuntime` fields the body reads
    context = cast(JobContext, SimpleNamespace(additional_context={"runtime": runtime}))
    await run_operation(context, operation_id=5)
    assert calls == [(engine, 5, 7)]


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
