"""The single fence rule of the reconcilers (Rulings FR-r, FR-w), checked on a real reconciler.

A reconciler fenced out by the worker's own fence lets it propagate: `run_reconcilers` stops the cadence at
that reconciler (no count, no `reconciler.failed` resolve, no later reconciler) and the superseded worker
requests its stop. A `FencedOut` of another owner keeps the reconciler's own handling (here: its pass ends
with 0).

M2 Task 18 adds `run_reconciler(name, engine, *, now, epoch)`: one pass of the registered reconciler `name`
(its module must be imported), with a context of `engine`, the process's task app (`tasks.task_app()`, not
opened: enough for `defer_in_tx`; a reconciler that queries the job manager, `stalled_jobs`, needs the open
app of the `queue_app` fixture) and the given clock and epoch. It returns the pass's count.

M3 Task 15 moves `registered_by(modules)` here from `tests/unit/test_worker_main.py` (Ruling P15's api check
uses it too): what a fresh interpreter has registered (reconcilers with their cadences, takeover sweeps,
operation handlers and failure hooks) after importing only `modules`; a registry module those imports never
reach registered nothing.
"""

import dataclasses
import json
import subprocess
import sys
from datetime import datetime
from types import ModuleType

import pytest
from jobhunter.errors import FencedOut
from jobhunter.worker import reconcilers
from jobhunter.worker.reconcilers import Cadence, ReconcileContext
from jobhunter.worker.tasks import defer_in_tx, task_app
from sqlalchemy.ext.asyncio import AsyncEngine

# Run with the module names as arguments: it imports those modules and nothing else, then reads the registries
# from `sys.modules` (each registry module is imported by the modules that register into it; one that is not
# there registered nothing).
_REGISTRY_SCRIPT = """\
import importlib, json, sys
for module in sys.argv[1:]:
    importlib.import_module(module)
def registry(module, name):
    return getattr(sys.modules.get(module), name, {})
print(json.dumps({
    "reconcilers": {
        name: sorted(cadences)
        for name, (cadences, _) in registry("jobhunter.worker.reconcilers", "REGISTRY").items()
    },
    "sweeps": sorted(registry("jobhunter.worker.epoch", "_SWEEPS")),
    "handlers": sorted(registry("jobhunter.worker.operations", "HANDLERS")),
    "failure_hooks": sorted(registry("jobhunter.worker.operations", "FAILURE_HOOKS")),
    "main_imported": "jobhunter.worker.main" in sys.modules,
}))
"""


def registered_by(modules: tuple[str, ...]) -> dict[str, object]:
    """What a fresh interpreter has registered after importing only `modules` (module docstring)."""
    done = subprocess.run(
        [sys.executable, "-c", _REGISTRY_SCRIPT, *modules],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    registered: dict[str, object] = json.loads(done.stdout.strip().splitlines()[-1])
    return registered


async def run_reconciler(name: str, engine: AsyncEngine, *, now: datetime, epoch: int) -> int:
    """One pass of the registered reconciler `name` at `now` as worker `epoch` (module docstring)."""
    _, fn = reconcilers.REGISTRY[name]
    return await fn(
        ReconcileContext(engine=engine, queue=task_app(), defer=defer_in_tx, now=now, epoch=epoch)
    )


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
