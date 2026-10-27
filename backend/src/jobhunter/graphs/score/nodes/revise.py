"""The `revise` node (spec §9.4.1, §9.4.2, §11.9; plan Task 9): the revision of the current draft after the
critic's review of it, a thin wrapper over `scoring.steps.revise_step`.

`round + 1 ≤ 2` (the routing's bound, checked again here). The call (`nodes.score_caller`): the attempt's
pinned `revise` node, the next logical call, `round` the new draft's, and the funded follow-up's bulk
operation; the prompt carries `<previous_draft>` and `<issues>` in a new user message (no assistant turn).
A structurally valid output is the draft of `round + 1`, stored under the owner lock, and goes to
`validate`; a structurally invalid one is repaired once and, after the repair, publishes `contested` from
the last validated draft (L4551, A2 T3); every other error goes to `fail` (A2 T2).
"""

from typing import Final

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from jobhunter.errors import FencedOut, InvariantViolation
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
from jobhunter.scoring.routing import MAX_REVISIONS, Route

NAME: Final = "revise"


def make_revise(deps: ScoreDeps) -> Node:
    async def revise(state: TargetState, config: RunnableConfig) -> Command[Route]:
        draft_id, note_id = state["current_draft_id"], state["last_critic_note_id"]
        if state["round"] + 1 > MAX_REVISIONS or draft_id is None or note_id is None:
            raise InvariantViolation(f"score target {state['target_id']}: no revision is due")
        if state["last_critic_draft_id"] != draft_id:
            raise InvariantViolation(f"score target {state['target_id']}: the current draft has no review")
        async with deps.engine.connect() as conn:
            inputs, attempt = await load_inputs(conn, state)
        store = TargetDraftStore(deps.engine, owner_of(state))
        note = await store.note(note_id)
        try:
            drafted = await steps.revise_step(
                inputs,
                store,
                score_caller(deps, state, inputs, attempt),
                draft_id=draft_id,
                note=note,
                round=state["round"],
            )
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

    return revise
