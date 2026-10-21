"""The `validate` node (spec §9.5, §12.3 to §12.5; M2 plan Task 13, Deviation 8): `validate_profile` over the
paid output.

It reads the output from `llm_calls.output_json` by the state's `llm_call_id`, with the call's `input_refs`
(the generation the call was made for), and the draft. A draft no longer in `extracting_profile`, or in
another generation than the call's (`generation_owner` of the call's `native_pdf` and
`reviewed_text_sha256`), ends the operation obsolete: its result could only land on text it was not made
from. Then `validate_profile` (Task 12) runs with `transmitted_text = reviewed_text`, or `None` for a native
PDF (whose evidence is then `unverifiable`, Deviation 8), against the clock's date: the repaired profile and
its issues.

**What it hands on.** The `draft` node writes the profile and the issues with the draft's own transition,
under the owner predicate, so that the result and the `ready` state land together or not at all. The
checkpointed state holds ids only (§9.1), so the result goes to the `draft` node of the same execution in
process memory: `VALIDATED`, keyed by the execution's thread (`graph.thread_id`, unique per operation and
attempt), which the handler discards when its graph has returned or raised. A later execution never finds an
earlier one's: it has a thread of its own and validates again (validation is pure, and its input is in the
database).

Logs carry ids and issue codes with their counts only.
"""

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Final

from langchain_core.runnables import RunnableConfig

from jobhunter.db.sql import q
from jobhunter.errors import InvariantViolation
from jobhunter.graphs.profile.deps import ProfileDeps
from jobhunter.graphs.profile.nodes import (
    EXTRACTING_PROFILE,
    Node,
    end_obsolete_in_own_tx,
    execution,
    execution_thread,
    generation_owner,
    log,
    read_extract_draft,
)
from jobhunter.graphs.profile.state import ProfileState, ProfileUpdate
from jobhunter.llm.schemas.profile import ProfileOut
from jobhunter.profile.validate import validate_profile
from jobhunter.types import JSONValue

NAME: Final = "validate"
NODE_TIMEOUT: Final = timedelta(seconds=30)

_CALL = q("SELECT output_json, input_refs FROM llm_calls WHERE id = :i AND purpose = 'extract_profile'")


@dataclass(frozen=True)
class Validated:
    """The `validate` node's result for the `draft` node: the call it came from, the generation that call was
    made for (its owner predicate and the transmitted text's hash), the profile and its issues."""

    llm_call_id: int
    owner: dict[str, object]
    transmitted_text_sha256: bytes | None
    draft_json: dict[str, JSONValue] = field(repr=False)
    validation_issues: list[dict[str, JSONValue]] = field(repr=False)


class ValidatedProfiles:
    """The results of the executions in this process, by thread (module docstring)."""

    def __init__(self) -> None:
        self._by_thread: dict[str, Validated] = {}

    def put(self, thread: str, validated: Validated) -> None:
        self._by_thread[thread] = validated

    def get(self, thread: str) -> Validated | None:
        return self._by_thread.get(thread)

    def discard(self, thread: str) -> None:
        self._by_thread.pop(thread, None)

    def __len__(self) -> int:
        return len(self._by_thread)


VALIDATED: Final = ValidatedProfiles()


def _digest(refs: dict[str, JSONValue], key: str) -> bytes | None:
    value = refs.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvariantViolation(f"llm_calls input_refs.{key} is no hex string")
    return bytes.fromhex(value)


def make_validate(deps: ProfileDeps) -> Node:
    async def validate(state: ProfileState, config: RunnableConfig) -> ProfileUpdate:
        op, _ = execution(config)
        thread = execution_thread(config)
        draft_id, llm_call_id = state["draft_id"], state["llm_call_id"]
        if llm_call_id is None:
            raise InvariantViolation(f"profile draft {draft_id}: validate runs after a call")
        async with deps.engine.connect() as conn:
            call = (await conn.execute(_CALL, {"i": llm_call_id})).first()
        if call is None or call.output_json is None:
            raise InvariantViolation(f"llm_calls {llm_call_id} holds no extraction output")
        refs = dict(call.input_refs)
        native = refs.get("native_pdf") is True
        reviewed = _digest(refs, "reviewed_text_sha256")
        owner = generation_owner(native_pdf=native, reviewed_text_sha256=reviewed)
        draft = await read_extract_draft(deps.engine, draft_id)
        if draft is None or draft.status != EXTRACTING_PROFILE or draft.owner() != owner:
            return {"steps": [NAME], "outcome": await end_obsolete_in_own_tx(deps.engine, op, draft_id)}
        out = ProfileOut.model_validate(call.output_json)
        profile, issues = validate_profile(
            out, transmitted_text=None if native else draft.reviewed_text, today=deps.clock().date()
        )
        VALIDATED.put(
            thread,
            Validated(
                llm_call_id=llm_call_id,
                owner=owner,
                transmitted_text_sha256=_digest(refs, "transmitted_text_sha256"),
                draft_json=profile.model_dump(mode="json"),
                validation_issues=[asdict(issue) for issue in issues],
            ),
        )
        log.info(
            "profile_validated",
            operation_id=op.claim.operation_id,
            draft_id=draft_id,
            llm_call_id=llm_call_id,
            issues=dict(Counter(issue.code for issue in issues)),
        )
        return {"steps": [NAME]}

    return validate
