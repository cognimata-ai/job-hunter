"""The `critic` node (spec §9.4.1, §9.4.2, §11.7; plan Task 9, A2 T8): one critic round of the current draft,
a thin wrapper over `scoring.steps.critic_step`.

`critic_rounds + 1 ≤ 3` (the routing's bound, checked again here). The round's effort is the model policy's
light effort when the draft's triggers are all light ones (`triggers.critic_effort`), else its normal one;
the call (`nodes.score_caller`) is the attempt's pinned `critic` node at that effort, the next logical call,
`llm_calls.round` the critic round (1-3), and the funded follow-up's bulk operation. A structurally valid
review is validated (`validate_review`) and stored as the round's `critic_notes` row under the owner lock;
`jobhunter_critic_verdict_mismatch_total` counts a returned verdict that is not the derived one, after that
commit (L4429). The state takes `critic_rounds + 1`, the note, its derived outcome and draft,
`not_a_job_ad_verified` and the deterministic failures without the rejected facts'.

The routing (`routing.after_critic`): a derived `accept` with no failure publishes `accepted`; otherwise a
revision while `round < 2`, else `contested`; a structurally invalid output is repaired once and then
publishes `contested`; every other error goes to `fail` (A2 T2).
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
from jobhunter.graphs.score.state import TargetState, TargetUpdate, state_failure_keys
from jobhunter.llm.errors import LLMError
from jobhunter.observability.metrics import CRITIC_VERDICT_MISMATCH
from jobhunter.scoring import routing, steps
from jobhunter.scoring.routing import MAX_CRITIC_ROUNDS, Route
from jobhunter.scoring.triggers import TRIGGERS

NAME: Final = "critic"


def make_critic(deps: ScoreDeps) -> Node:
    async def critic(state: TargetState, config: RunnableConfig) -> Command[Route]:
        draft_id = state["current_draft_id"]
        critic_round = state["critic_rounds"] + 1
        if critic_round > MAX_CRITIC_ROUNDS or draft_id is None:
            raise InvariantViolation(f"score target {state['target_id']}: no critic round is due")
        async with deps.engine.connect() as conn:
            inputs, attempt = await load_inputs(conn, state)
        store = TargetDraftStore(deps.engine, owner_of(state))
        note_id = state["last_critic_note_id"]
        try:
            reviewed = await steps.critic_step(
                inputs,
                store,
                score_caller(deps, state, inputs, attempt),
                draft_id=draft_id,
                note=None if note_id is None else await store.note(note_id),
                critic_round=critic_round,
                triggers=[code for code in TRIGGERS if code in state["triggers"]],
            )
        except (LLMError, FencedOut) as error:
            return failure(error, state, NAME)
        update: TargetUpdate = {}
        if reviewed.note_id is None:
            log.info(
                "score_output_invalid",
                node=NAME,
                target_id=state["target_id"],
                attempt_id=state["attempt_id"],
                llm_call_id=reviewed.llm_call_id,
                repair_used=state["repair_used"],
            )
        else:
            if reviewed.verdict_mismatch:
                CRITIC_VERDICT_MISMATCH.inc()  # the note's transaction has committed
            update = {
                "critic_rounds": critic_round,
                "last_critic_note_id": reviewed.note_id,
                "last_critic_outcome": reviewed.derived_outcome,
                "last_critic_draft_id": draft_id,
                "not_a_job_ad_verified": reviewed.not_a_job_ad_verified,
                "det_failures": state_failure_keys(reviewed.det_failures),
            }
        nxt = routing.after_critic(
            review=reviewed.derived_outcome,
            det_failures=reviewed.det_failures,
            round=state["round"],
            repair_used=state["repair_used"],
        )
        return goto(nxt, update)

    return critic
