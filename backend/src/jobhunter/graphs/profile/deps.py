"""What the profile graphs are compiled with (spec §9.1; M2 plan Task 11).

The graphs are compiled once per process (`build_profile_graphs`), so `ProfileDeps` holds only process-wide
objects: the engine and the clock. Everything per execution, the claim and the worker's `OperationDeps`
included, reaches a node through the invoke config, `config["configurable"]["op"]` (the handler's
`OperationContext`), never through the graph's state or these deps.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True)
class ProfileDeps:
    engine: AsyncEngine
    clock: Callable[[], datetime]
