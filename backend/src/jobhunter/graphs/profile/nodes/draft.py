"""The `draft` node (spec §9.5; M2 plan Task 13, Deviation 26): the extracted profile published, `ready`.

It takes the `validate` node's result of this execution (`validate.VALIDATED`, by thread), then the fault
point `profile.draft.before_tx`, then one transaction in the §9.1 order: the owner row (a lost claim is
`FencedOut` and writes nothing; `run_owned` ends such an execution as lost), the draft `FOR UPDATE`, then
`extracting_profile → ready` with `draft_json`, `validation_issues`, `llm_call_id` and
`transmitted_text_sha256`, and the operation `succeeded` (`finish_in`), so the draft's `ready` and the
operation's end commit together.

The draft's transition carries the owner predicate of the generation the call was made for
(`generation_owner`: `{"reviewed_text_sha256": <its digest>, "native_pdf": False}`, or `{"native_pdf": True}`
alone for a native PDF), on `profile_drafts.owner_cols`: a defence no reordering can defeat. A draft in
another state, or in another generation (the update matches no row, `TransitionConflict`), is left as it is
and the operation ends `succeeded` as obsolete.

Logs carry ids and counts only.
"""

from datetime import timedelta
from typing import Final

import sqlalchemy as sa
from langchain_core.runnables import RunnableConfig

from jobhunter.db.transitions import transition
from jobhunter.errors import InvariantViolation, TransitionConflict
from jobhunter.faults import afault_point
from jobhunter.graphs.profile.deps import ProfileDeps
from jobhunter.graphs.profile.nodes import (
    EXTRACTING_PROFILE,
    Node,
    end_obsolete,
    execution,
    execution_thread,
    lock_draft,
    log,
)
from jobhunter.graphs.profile.nodes.validate import VALIDATED
from jobhunter.graphs.profile.state import OBSOLETE, READY, ProfileState, ProfileUpdate
from jobhunter.worker.operations import lock_operation_owner

NAME: Final = "draft"
NODE_TIMEOUT: Final = timedelta(seconds=30)
BEFORE_TX: Final = "profile.draft.before_tx"


def make_draft(deps: ProfileDeps) -> Node:
    async def draft(state: ProfileState, config: RunnableConfig) -> ProfileUpdate:
        op, _ = execution(config)
        draft_id = state["draft_id"]
        validated = VALIDATED.get(execution_thread(config))
        if validated is None or validated.llm_call_id != state["llm_call_id"]:
            raise InvariantViolation(f"profile draft {draft_id}: no validated profile of this execution")
        await afault_point(BEFORE_TX)
        async with deps.engine.begin() as conn:
            await lock_operation_owner(conn, op.claim)
            status = await lock_draft(conn, draft_id)
            published = False
            if status == EXTRACTING_PROFILE:
                try:
                    await transition(
                        conn,
                        "profile_drafts",
                        draft_id,
                        [EXTRACTING_PROFILE],
                        READY,
                        owner=validated.owner,
                        set_={
                            "draft_json": validated.draft_json,
                            "validation_issues": validated.validation_issues,
                            "llm_call_id": validated.llm_call_id,
                            "transmitted_text_sha256": validated.transmitted_text_sha256,
                            "updated_at": sa.func.now(),
                        },
                    )
                except TransitionConflict:
                    pass  # another generation: the update matched no row and wrote nothing
                else:
                    published = True
            if not published:
                await end_obsolete(conn, op, draft_id, status)
                return {"steps": [NAME], "outcome": OBSOLETE}
            await op.finish_in(conn, status="succeeded")
        log.info(
            "profile_draft_ready",
            operation_id=op.claim.operation_id,
            draft_id=draft_id,
            llm_call_id=validated.llm_call_id,
            issues=len(validated.validation_issues),
        )
        return {"steps": [NAME], "outcome": READY}

    return draft
