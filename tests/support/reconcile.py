"""The single fence rule of the reconcilers (Rulings FR-r, FR-w), checked on a real reconciler.

A reconciler fenced out by the worker's own fence lets it propagate: `run_reconcilers` stops the cadence at
that reconciler (no count, no `reconciler.failed` resolve, no later reconciler) and the superseded worker
requests its stop. A `FencedOut` of another owner keeps the reconciler's own handling (here: its pass ends
with 0).
"""

import dataclasses
from types import ModuleType

import pytest
from jobhunter.errors import FencedOut
from jobhunter.worker import reconcilers
from jobhunter.worker.reconcilers import Cadence, ReconcileContext


async def assert_fence_rule(
    name: str, cadence: Cadence, superseded: ReconcileContext, current: ReconcileContext, module: ModuleType
) -> None:
    """Check reconciler `name` (registered for `cadence`) against the rule above: `superseded` is a context
    whose epoch is not the current one, `current` the current worker's, and `module` the module whose
    `fence_epoch` its reconciler calls. The caller's data must make the reconciler reach its fence; nothing
    is written by either check (the caller asserts that on its data)."""
    fn = reconcilers.REGISTRY[name][1]
    assert cadence in reconcilers.REGISTRY[name][0]
    with pytest.raises(FencedOut) as fenced:
        await fn(superseded)
    assert fenced.value.worker_superseded

    ran: list[str] = []
    stops: list[str] = []

    async def after(_: ReconcileContext) -> int:
        ran.append("after")
        return 1

    async def resolve_attempted(*args: object, **kwargs: object) -> None:
        ran.append("resolve")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(reconcilers, "REGISTRY", {})
        patch.setattr(reconcilers, "_resolve_failure", resolve_attempted)
        reconcilers.reconciler(name, cadence)(fn)
        reconcilers.reconciler("after", cadence)(after)
        stopping = dataclasses.replace(superseded, on_superseded=lambda: stops.append("stop"))
        assert await reconcilers.run_reconcilers(stopping, cadence) == {}  # stopped at the reconciler
    assert ran == [] and stops == ["stop"]  # neither its resolve nor the next reconciler ran

    async def another_owners_fence(*args: object, **kwargs: object) -> None:
        raise FencedOut("purge_fence")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(module, "fence_epoch", another_owners_fence)
        assert await fn(current) == 0  # its own handling: the pass ends, nothing raised
