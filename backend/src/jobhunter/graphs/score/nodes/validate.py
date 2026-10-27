"""The `validate` node (spec §9.4.1, §11.6, §11.7.1, §11.8; plan Task 9, Codex r1 #9, r2 B16): stage 2 of the
current draft, its provisional aggregation and its triggers, a thin wrapper over
`scoring.steps.validate_step`.

One transaction, in the lock schedule's order: `active_config FOR SHARE` and the active scope (L4400: a scope
save or flip cannot commit in between), the owner lock (a lost claim goes to `fail` with `claim_lost`), then
the step's reads and its one `UPDATE score_drafts` (`nodes.TargetDraftStore`, bound to this transaction): the
corrected draft as `output_json`, so the critic, the reviser, the publish and a resumed attempt all read it
(the raw wire output stays in `llm_calls.output_json`), its validation, failures, provisional aggregation and
triggers. The provisional aggregation reads the confirmations, the injection assessment and the
`not_a_job_ad_verified` of the attempt's last critic note (`last_critic_note_id`).

The state takes the triggers and the unresolved failure keys (`state.state_failure_keys`); the routing
(`routing.after_validate`) goes to `publish`, `critic` or, for a resumed draft that already has its review
(the last note reviewed the current draft), where that review routes (`revise` or `publish`), with the
failures of the facts it rejected dropped as after the live critic round (`validator.unresolved_failures`).
"""

from typing import Final

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.errors import FencedOut, InvariantViolation
from jobhunter.graphs.score.deps import ScoreDeps
from jobhunter.graphs.score.nodes import Node, TargetDraftStore, failure, goto, load_inputs, log, owner_of
from jobhunter.graphs.score.state import TargetState, state_failure_keys
from jobhunter.run_settings import ScopeSettings, scope_from_payload
from jobhunter.scoring import routing, steps
from jobhunter.scoring.routing import Route
from jobhunter.types import get_object

NAME: Final = "validate"

_LOCK_ACTIVE_SCOPE: Final = q("SELECT scope_version_id FROM active_config WHERE id = 1 FOR SHARE")
# A statement of its own after the lock (`settings.active`: a joined row is read in the snapshot the locking
# statement started with).
_SCOPE: Final = q("SELECT payload FROM scope_versions WHERE id = :s")


async def active_scope(conn: AsyncConnection) -> tuple[int, ScopeSettings]:
    """The active scope version and its scope, `active_config` held `FOR SHARE` for the transaction."""
    scope_version_id = (await conn.execute(_LOCK_ACTIVE_SCOPE)).scalar_one()
    payload = (await conn.execute(_SCOPE, {"s": scope_version_id})).scalar_one()
    return int(scope_version_id), scope_from_payload(get_object(payload, ""))


def make_validate(deps: ScoreDeps) -> Node:
    async def validate(state: TargetState, config: RunnableConfig) -> Command[Route]:
        draft_id = state["current_draft_id"]
        if draft_id is None:
            raise InvariantViolation(f"score target {state['target_id']}: no draft to validate")
        owner = owner_of(state)
        try:
            async with deps.engine.begin() as conn:
                scope_version_id, scope = await active_scope(conn)
                await owner.lock(conn)
                inputs, _ = await load_inputs(conn, state)
                store = TargetDraftStore(deps.engine, owner, conn=conn)
                note_id = state["last_critic_note_id"]
                validated = await steps.validate_step(
                    inputs,
                    store,
                    draft_id=draft_id,
                    scope=scope,
                    note=None if note_id is None else await store.note(note_id),
                    critic_rounds=state["critic_rounds"],
                )
        except FencedOut as error:
            return failure(error, state, NAME)
        log.info(
            "score_draft_validated",
            target_id=state["target_id"],
            attempt_id=state["attempt_id"],
            draft_id=draft_id,
            scope_version_id=scope_version_id,
            triggers=validated.triggers,
            failures=len(validated.failures),
            unresolved=len(validated.det_failures),
            review=validated.review,
        )
        nxt = routing.after_validate(
            triggers=validated.triggers,
            det_failures=validated.det_failures,
            critic_rounds=state["critic_rounds"],
            round=state["round"],
            review=validated.review,
        )
        triggers: list[str] = list(validated.triggers)
        return goto(nxt, {"triggers": triggers, "det_failures": state_failure_keys(validated.det_failures)})

    return validate
