"""What a task body needs from the worker process (spec §3.3), without any I/O.

`worker_main` (Task 13) composes a `WorkerRuntime` once the lock is held and the epoch is claimed, and hands
it to procrastinate as `additional_context={"runtime": runtime}`. Every task body reads it as
`context.additional_context["runtime"]`, so Tasks 10 to 12 can type their bodies before the process exists.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any

import procrastinate
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.config import AppConfig


@dataclass
class ShutdownState:
    """Shutdown progress, shared by the signal handler and the task bodies.

    `admission_stopped` is true once the worker no longer admits new work; `requested` is set when the process
    was asked to shut down.
    """

    admission_stopped: bool = False
    requested: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class WorkerRuntime:
    cfg: AppConfig
    engine: AsyncEngine
    queue: procrastinate.App
    saver: AsyncPostgresSaver
    collect_graph: CompiledStateGraph[Any, Any, Any, Any]
    epoch: int
    shutdown: ShutdownState
