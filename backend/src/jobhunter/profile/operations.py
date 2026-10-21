"""The profile operations (spec §7.16.8, §9.5, §12; M2 plan Task 11): their handlers and failure hooks.

**`extract_profile_text`** (payload `{"draft_id": <id>}`, created with the draft by the upload,
`upload.request_draft_operation`). In one transaction, in the §9.1 order: the owner row
(`lock_operation_owner`), then the draft `FOR UPDATE`. A draft in `uploaded` moves to `extracting_text`; one
already in `extracting_text` (an earlier execution's start, before a reclaim) goes on as it is; any other
state, or a draft that is gone, ends the operation `succeeded` as obsolete without touching it (`finish_in`).
Then the handler invokes `profile_graphs.text` on a thread of its own (`graph.thread_id("t", draft_id,
claim.operation_id, claim.attempts)`) with the §9.1 invoke shape (`configurable["op"]` is this context). The
graph's nodes end the operation in the transaction of their last domain write: `text_review`, or
`failed(code)` (`graphs.profile.nodes.parse`, `.strip_contacts`).

**Its failure hook** (`text_phase_failed`, Deviation 13) runs in every transaction that ends such an
operation `failed`. It reads the draft `FOR UPDATE` and moves it only while it is still live in the text
phase: `extracting_text → failed(<code or 'attempts_exhausted'>)`; a draft still `uploaded` (the operation
failed before its first transition: `no_handler`, or ten `bug` requeues) takes the two §7.16.8 edges
`uploaded → extracting_text → failed(code)` with the same `failure_code`, so it never stays `uploaded` and
answers 409 to every later upload. A draft the handler's own write already ended in that transaction (a
node's `failed(code)` with its `finish_in`), or one in a later phase, is left as it is, so `transition()`
never meets a terminal row. The draft row comes after the owner row in the §9.1 order.

Tasks 13 (`extract_profile`), 19 (`embed_profile`) and 20 (`propose_queries`) add their handlers and hooks
here. Logs carry ids and codes only.
"""

from collections.abc import Mapping
from typing import Final

from langchain_core.runnables import RunnableConfig
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.transitions import transition
from jobhunter.graphs.profile.graph import RECURSION_LIMIT, thread_id
from jobhunter.graphs.profile.nodes import end_obsolete, fail_draft, lock_draft
from jobhunter.graphs.profile.state import initial_state
from jobhunter.observability.logging import get_logger
from jobhunter.types import JSONValue, get_int
from jobhunter.worker.operations import (
    ATTEMPTS_EXHAUSTED,
    OperationContext,
    lock_operation_owner,
    register_operation_handler,
)

EXTRACT_PROFILE_TEXT: Final = "extract_profile_text"
_DRAFTS: Final = "profile_drafts"

log = get_logger("jobhunter.profile.operations")


def _draft_id(payload: Mapping[str, JSONValue]) -> int:
    return get_int(dict(payload), "draft_id")


async def text_phase_failed(
    conn: AsyncConnection, operation_id: int, payload: Mapping[str, JSONValue], error_code: str | None
) -> None:
    """The text phase's failure hook (module docstring)."""
    draft_id = _draft_id(payload)
    status = await lock_draft(conn, draft_id)
    if status not in ("uploaded", "extracting_text"):
        return  # ended by the handler's own write, or in another phase: not this hook's to move
    code = error_code or ATTEMPTS_EXHAUSTED
    if status == "uploaded":
        await transition(conn, _DRAFTS, draft_id, ["uploaded"], "extracting_text")
    await fail_draft(conn, draft_id, code, from_state="extracting_text")
    log.warning("profile_draft_failed", operation_id=operation_id, draft_id=draft_id, code=code)


@register_operation_handler("extract_profile_text", on_failed=text_phase_failed)
async def extract_profile_text(ctx: OperationContext) -> None:
    """The text phase of a draft (module docstring)."""
    if ctx.deps is None:
        raise RuntimeError("extract_profile_text runs with the worker's OperationDeps")
    claim = ctx.claim
    draft_id = _draft_id(claim.payload)
    async with ctx.engine.begin() as conn:
        await lock_operation_owner(conn, claim)
        status = await lock_draft(conn, draft_id)
        if status == "uploaded":
            await transition(conn, _DRAFTS, draft_id, ["uploaded"], "extracting_text")
        elif status != "extracting_text":
            await end_obsolete(conn, ctx, draft_id, status)
            return
    config: RunnableConfig = {
        "configurable": {
            "thread_id": thread_id("t", draft_id, claim.operation_id, claim.attempts),
            "op": ctx,
        },
        "recursion_limit": RECURSION_LIMIT,
    }
    await ctx.deps.profile_graphs.text.ainvoke(
        initial_state(draft_id, claim.operation_id), config, durability="sync"
    )
