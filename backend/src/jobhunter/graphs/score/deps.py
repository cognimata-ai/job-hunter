"""What the `score_target` subgraph is compiled with (spec §9.1, §9.4; plan Task 9).

The subgraph is compiled once per process (`subgraph.build_score_target`), so `ScoreDeps` holds the
process-wide objects only: the engine, the LLM gateway, the clock that stamps rows, `monotonic`, the clock
every call's deadline is built from (the gateway's own, `LLMGateway.monotonic`, which the gateway compares
`CallContext.deadline` with), and the checkpoint saver of the cycle graph (Task 11; the subgraph itself is
compiled with `checkpointer=None` and inherits its parent's, L3376). Everything per cycle reaches a node
through the invoke config (`context.execution`), never through these deps or the state.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.llm.gateway import LLMGateway


@dataclass(frozen=True)
class ScoreDeps:
    engine: AsyncEngine
    gateway: LLMGateway
    clock: Callable[[], datetime]
    monotonic: Callable[[], float]
    saver: BaseCheckpointSaver[str] | None
