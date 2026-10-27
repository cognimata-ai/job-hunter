"""The execution context of one score cycle (spec §9.3, §9.4.3; plan Task 9): what a `score_target` node reads
from `config["configurable"]["cycle"]` (`execution`), as the profile graph's nodes read their operation.

LangGraph never checkpoints `configurable` (it copies only its string and number values into a checkpoint's
metadata, and the context is neither), so the cycle's ids, the process-wide `ScoreDeps` and the claim tracker
reach every node without entering the state.

`ScoreContext` is the cycle's: its id and worker epoch, the LLM tuple, the scope version and the evaluation
policy it was started under (an attempt pins its own policy at its claim and uses it throughout; `validate`
reads the active scope itself, L4400), the deps, and `claims`.

`ClaimTracker` is the claim heartbeat of §9.4.3: `start(ref)` begins beating a claim, `attach(ref, child)`
names the task scoring it (which a lost claim cancels), `lost(ref)` says whether the claim was lost,
`stop(ref)` ends one claim's beats and `stop_all()` every claim's. Task 10's `ClaimHeartbeats` implements it.

Task 11's cycle graph builds the context and hands it to every `score_target` invocation; the nodes of Task 9
work from the deps they were built with and the state, and read nothing of it yet.
"""

import asyncio
from dataclasses import dataclass
from typing import Protocol

from langchain_core.runnables import RunnableConfig

from jobhunter.graphs.score.deps import ScoreDeps
from jobhunter.graphs.score.state import ClaimRef


class ClaimTracker(Protocol):
    """The claim heartbeats of a cycle (module docstring)."""

    def start(self, ref: ClaimRef) -> None: ...

    def attach(self, ref: ClaimRef, child: "asyncio.Task[object]") -> None: ...

    def lost(self, ref: ClaimRef) -> bool: ...

    async def stop(self, ref: ClaimRef) -> None: ...

    async def stop_all(self) -> None: ...


@dataclass(frozen=True)
class ScoreContext:
    cycle_id: int
    worker_epoch: int
    llm_tuple_id: int
    scope_version_id: int
    evaluation_policy_id: int
    deps: ScoreDeps
    claims: ClaimTracker


def execution(config: RunnableConfig) -> ScoreContext:
    """The cycle's `ScoreContext`, from `config["configurable"]["cycle"]`."""
    cycle = config.get("configurable", {}).get("cycle")
    if not isinstance(cycle, ScoreContext):
        raise TypeError("a score_target node runs with its cycle's ScoreContext in configurable['cycle']")
    return cycle
