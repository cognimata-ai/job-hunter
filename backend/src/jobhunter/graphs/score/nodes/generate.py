"""The `generate` node (spec §9.4.1, §9.4.2, §11.5, §11.6; plan Task 9): the generator call of the attempt,
a thin wrapper over `scoring.steps.generate_step`.

The call (`nodes.score_caller`): the attempt's pinned `generate` node, `logical_call_no =
score_attempts.logical_calls + 1`, the attempt's `start_stage`, `round` 0, and the funded follow-up's bulk
operation. A structurally valid output is stored as the draft of round 0 in one transaction under the owner
lock, between the fault points `score.generate.before_draft` and `score.generate.after_draft`
(`nodes.TargetDraftStore`); the state takes its id. A structurally invalid output is never stored (§11.6):
the routing repairs it once (`repair_used`), and after the repair fails the attempt `invalid_output`
(L3553). Every other error of the call (the gateway's, a lost claim) goes to `fail` with its code
(`llm_step`).
"""

from typing import Final

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from jobhunter.errors import FencedOut
from jobhunter.graphs.score.deps import ScoreDeps
from jobhunter.graphs.score.nodes import (
    Node,
    TargetDraftStore,
    failure,
    goto,
    load_inputs,
    log,
    owner_of,
    score_caller,
)
from jobhunter.graphs.score.state import TargetState, TargetUpdate
from jobhunter.llm.errors import LLMError
from jobhunter.scoring import routing, steps
from jobhunter.scoring.routing import Route

NAME: Final = "generate"


def make_generate(deps: ScoreDeps) -> Node:
    async def generate(state: TargetState, config: RunnableConfig) -> Command[Route]:
        async with deps.engine.connect() as conn:
            inputs, attempt = await load_inputs(conn, state)
        store = TargetDraftStore(deps.engine, owner_of(state))
        try:
            drafted = await steps.generate_step(inputs, store, score_caller(deps, state, inputs, attempt))
        except (LLMError, FencedOut) as error:
            return failure(error, state, NAME)
        update: TargetUpdate = {}
        if drafted.draft_id is None:
            log.info(
                "score_output_invalid",
                node=NAME,
                target_id=state["target_id"],
                attempt_id=state["attempt_id"],
                llm_call_id=drafted.llm_call_id,
                repair_used=state["repair_used"],
                error_class=None if drafted.failure is None else drafted.failure.error_class,
                path=None if drafted.failure is None else drafted.failure.path,
            )
        else:
            update = {"current_draft_id": drafted.draft_id, "round": drafted.round}
        nxt = routing.after_generation(
            NAME, stored=drafted.draft_id is not None, repair_used=state["repair_used"]
        )
        return goto(nxt, update)

    return generate
