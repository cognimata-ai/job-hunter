"""The `publish` node (spec §9.4.1 L3531-3541; plan Task 10, Ruling P28): the attempt's publication, a thin
wrapper over `scoring.publish`.

In one transaction: the steps the caller owns (`publish.lock_config_and_job`: `active_config FOR SHARE`, then
the job `FOR SHARE`, both before the owner row), the attempt's inputs (`nodes.load_inputs`), and
`publish.publish_result` with the state's `publish_mode`, current draft and last critic note, under the claim
predicate (`ClaimPredicate`), `published_at` from the deps' clock. After the commit, the fault point
`score.publish.after_commit` (`publish.AFTER_COMMIT`, C13).

With `outcome = "already_published"` (Task 9's `load_context` found an `llm_results` row of the 5-tuple,
L3523, defensive) it links that stored result instead (`publish.link_result`) and inserts nothing: no second
`llm_results` row, so the unique 5-tuple key is never hit.

The state's `outcome` is the attempt's: `published_accepted` or `published_contested` (the stored result's
status for a link), `claim_lost` (nothing written; logged `score_claim_lost`), `cancelled` (a fenced job) or
`failed_invalid_output` (a publication `Bug`). Task 11's wrapper projects it into the `TargetOutcome`.
"""

from typing import Final
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from jobhunter.db.sql import q
from jobhunter.errors import InvariantViolation
from jobhunter.faults import afault_point
from jobhunter.graphs.score.deps import ScoreDeps
from jobhunter.graphs.score.nodes import Node, load_inputs, log
from jobhunter.graphs.score.state import TargetState
from jobhunter.scoring import publish as publishing
from jobhunter.scoring import routing
from jobhunter.scoring.routing import ALREADY_PUBLISHED, CLAIM_LOST, Route

NAME: Final = "publish"
_ATTEMPT_OUTCOMES: Final = {
    "claim_lost": CLAIM_LOST,
    "fenced": "cancelled",
    "publication_invalid": "failed_invalid_output",
}
_TARGET: Final = q("SELECT job_id, job_revision_id, llm_tuple_id FROM score_targets WHERE id = :t")


def make_publish(deps: ScoreDeps) -> Node:
    async def publish(state: TargetState, config: RunnableConfig) -> Command[Route]:
        target_id, attempt_id = state["target_id"], state["attempt_id"]
        linking = state["outcome"] == ALREADY_PUBLISHED
        mode, draft_id = state["publish_mode"], state["current_draft_id"]
        predicate = publishing.ClaimPredicate(UUID(state["claim_id"]), state["attempt_no"])
        async with deps.engine.begin() as conn:
            ids = (await conn.execute(_TARGET, {"t": target_id})).first()
            if ids is None:
                raise LookupError(f"score target {target_id} does not exist")
            target = publishing.TargetRow(
                target_id, int(ids.job_id), int(ids.job_revision_id), int(ids.llm_tuple_id)
            )
            await publishing.lock_config_and_job(conn, target.job_id)  # steps 1 and 1a, the caller's
            if linking:
                outcome = await publishing.link_result(
                    conn, target=target, attempt_id=attempt_id, predicate=predicate
                )
            elif mode is None or draft_id is None:
                raise InvariantViolation(f"score target {target_id}: publish without a mode or a draft")
            else:
                inputs, _ = await load_inputs(conn, state)
                outcome = await publishing.publish_result(
                    conn,
                    target=target,
                    attempt_id=attempt_id,
                    final_draft_id=draft_id,
                    mode=mode,
                    predicate=predicate,
                    inputs=inputs,
                    last_critic_note_id=state["last_critic_note_id"],
                    now=deps.clock(),
                )
        if outcome.reason == "published":
            await afault_point(publishing.AFTER_COMMIT)
            attempt_outcome = f"published_{outcome.status}"
        else:
            attempt_outcome = _ATTEMPT_OUTCOMES[outcome.reason]
        if outcome.reason == "claim_lost":
            log.info("score_claim_lost", node=NAME, target_id=target_id, attempt_id=attempt_id)
        log.info(
            "score_publish_ended",
            target_id=target_id,
            attempt_id=attempt_id,
            reason=outcome.reason,
            llm_result_id=outcome.result_id,
            linked=linking,
        )
        return Command(goto=routing.END, update={"outcome": attempt_outcome})

    return publish
