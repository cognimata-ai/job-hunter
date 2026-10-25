"""The `validate` node (spec §9.5, §12.3 to §12.5; M2 plan Task 13, Deviation 8): the generation gate of the
paid output, and its validation.

**The gate.** In one transaction in the §9.1 order (the owner row, then the draft `FOR UPDATE`), it reads the
call by the state's `llm_call_id` (`read_call_output`: `llm_calls.output_json` and the generation the call was
made for, from its `input_refs`) and the draft. A draft no longer in `extracting_profile` ends the operation
obsolete; one still there but in another generation than the call's (`generation_owner` of the call's
`native_pdf` and `reviewed_text_sha256`) ends it obsolete loudly (`end_off_generation`: a warning and the
alert `profile.extract_generation_mismatch`), since the output was not made from that draft's text.

**The validation** (`validated`): `validate_profile` (Task 12) on the output, with `transmitted_text =
reviewed_text`, or `None` for a native PDF (whose evidence is then `unverifiable`, Deviation 8), and `today`
the date of the injected clock. It is pure and reads only what the database holds, so the `draft` node runs it
again in its own locked transaction and writes its result with the `ready` transition (§9.1: every node is
self-sufficient from its receipts). Nothing is handed from node to node but ids in the state.

Logs carry ids and issue codes with their counts only.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Final

from langchain_core.runnables import RunnableConfig
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.errors import InvariantViolation
from jobhunter.graphs.profile.deps import ProfileDeps
from jobhunter.graphs.profile.nodes import (
    EXTRACTING_PROFILE,
    ExtractDraft,
    Node,
    end_off_generation,
    execution,
    generation_owner,
    lock_extract_draft,
    log,
)
from jobhunter.graphs.profile.state import ProfileState, ProfileUpdate
from jobhunter.llm.schemas.profile import ProfileOut
from jobhunter.profile.validate import Issue, validate_profile
from jobhunter.types import JSONValue
from jobhunter.worker.operations import lock_operation_owner

NAME: Final = "validate"
NODE_TIMEOUT: Final = timedelta(seconds=30)

_CALL = q("SELECT output_json, input_refs FROM llm_calls WHERE id = :i AND purpose = 'extract_profile'")


@dataclass(frozen=True)
class CallOutput:
    """An `extract_profile` call's output and the generation it was made for: its owner predicate, whether it
    sent a native PDF, and the transmitted text's sha256."""

    llm_call_id: int
    native_pdf: bool
    owner: dict[str, object]
    transmitted_text_sha256: bytes | None
    output_json: JSONValue = field(repr=False)


def _digest(refs: dict[str, JSONValue], key: str) -> bytes | None:
    value = refs.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvariantViolation(f"llm_calls input_refs.{key} is no hex string")
    return bytes.fromhex(value)


async def read_call_output(conn: AsyncConnection, llm_call_id: int) -> CallOutput:
    """Call `llm_call_id`'s output and generation; an `InvariantViolation` for a call without an output."""
    row = (await conn.execute(_CALL, {"i": llm_call_id})).first()
    if row is None or row.output_json is None:
        raise InvariantViolation(f"llm_calls {llm_call_id} holds no extraction output")
    refs = dict(row.input_refs)
    native = refs.get("native_pdf") is True
    reviewed = _digest(refs, "reviewed_text_sha256")
    return CallOutput(
        llm_call_id=llm_call_id,
        native_pdf=native,
        owner=generation_owner(native_pdf=native, reviewed_text_sha256=reviewed),
        transmitted_text_sha256=_digest(refs, "transmitted_text_sha256"),
        output_json=row.output_json,
    )


def validated(call: CallOutput, draft: ExtractDraft, *, today: date) -> tuple[ProfileOut, list[Issue]]:
    """`validate_profile` of the call's output against the draft's reviewed text, or nothing for a native
    PDF (module docstring). Pure: the same inputs and `today` give the same profile and issues."""
    out = ProfileOut.model_validate(call.output_json)
    return validate_profile(
        out, transmitted_text=None if call.native_pdf else draft.reviewed_text, today=today
    )


def make_validate(deps: ProfileDeps) -> Node:
    async def validate(state: ProfileState, config: RunnableConfig) -> ProfileUpdate:
        op, _ = execution(config)
        draft_id, llm_call_id = state["draft_id"], state["llm_call_id"]
        if llm_call_id is None:
            raise InvariantViolation(f"profile draft {draft_id}: validate runs after a call")
        async with deps.engine.begin() as conn:
            await lock_operation_owner(conn, op.claim)
            draft = await lock_extract_draft(conn, draft_id)
            call = await read_call_output(conn, llm_call_id)
            if draft is None or draft.status != EXTRACTING_PROFILE or draft.owner() != call.owner:
                status = None if draft is None else draft.status
                return {"steps": [NAME], "outcome": await end_off_generation(conn, op, draft_id, status)}
            _, issues = validated(call, draft, today=deps.clock().date())
        log.info(
            "profile_validated",
            operation_id=op.claim.operation_id,
            draft_id=draft_id,
            llm_call_id=llm_call_id,
            issues=dict(Counter(issue.code for issue in issues)),
        )
        return {"steps": [NAME]}

    return validate
