"""Node 9, `enqueue_targets` (spec §9.2): the score targets of the run's current revisions, from M3 on.

Node 9 creates a target per current revision for the run's LLM tuple (§9.7). M1 has no tuple
(`runs.llm_tuple_id` is NULL until M2 activates one, plan Deviation 11), so the node records that it ran and
changes nothing, and I5 is vacuous (`finalize.check_invariants`). A run that has a tuple reaches code that
M3 fills: the node refuses it as a bug (the run ends `failed('bug')`) rather than silently creating no
target.
"""

from typing import Final

from jobhunter.graphs.collect.deps import CollectDeps
from jobhunter.graphs.collect.nodes import Node
from jobhunter.graphs.collect.state import CollectState, CollectUpdate

ENQUEUE_TARGETS: Final = "enqueue_targets"


def make_enqueue_targets(deps: CollectDeps) -> Node:
    """Node 9 over `deps` (it needs none of them in M1)."""
    del deps

    async def enqueue_targets(state: CollectState) -> CollectUpdate:
        if state["llm_tuple_id"] is not None:
            raise NotImplementedError("enqueue_targets: the score targets of an LLM tuple arrive in M3")
        return {"steps": [ENQUEUE_TARGETS]}

    return enqueue_targets
