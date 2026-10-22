"""Node 9, `enqueue_targets` (spec §9.2): the score targets of the run's current revisions, from M3 on.

Node 9 creates a target per current revision for the run's LLM tuple (§9.7). M2 activates a tuple with a
confirmed profile (§12.6) but has no targets table, so while `SCORE_TARGETS_ENABLED` is false the node records
that it ran and changes nothing, with or without a tuple, and `finalize`'s I5 passes with a tuple set,
recording `"targets": "m3"` (plan Deviation 10, superseding M1 Carry-forward 9). M3 sets the switch and fills
both; the switch alone, with a tuple, is refused as a bug (the run ends `failed('bug')`) rather than silently
creating no target.
"""

from typing import Final

from jobhunter.graphs.collect.deps import CollectDeps
from jobhunter.graphs.collect.nodes import Node
from jobhunter.graphs.collect.state import CollectState, CollectUpdate

ENQUEUE_TARGETS: Final = "enqueue_targets"
# M3 sets it, with the target creation below and I5's check of the run's targets (plan Deviation 10).
SCORE_TARGETS_ENABLED: Final = False


def make_enqueue_targets(deps: CollectDeps) -> Node:
    """Node 9 over `deps` (it needs none of them before M3)."""
    del deps

    async def enqueue_targets(state: CollectState) -> CollectUpdate:
        if SCORE_TARGETS_ENABLED and state["llm_tuple_id"] is not None:
            # the switch without the creation would silently create no target: refused as a bug
            raise NotImplementedError("enqueue_targets: the score targets of an LLM tuple arrive in M3")
        return {"steps": [ENQUEUE_TARGETS]}

    return enqueue_targets
