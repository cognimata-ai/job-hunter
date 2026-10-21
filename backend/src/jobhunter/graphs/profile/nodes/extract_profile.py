"""The `extract_profile` node (spec §9.1, §9.5, §10, §12.3, §12.7, §13.3; M2 plan Task 13, Deviations 26 and
28): the one paid call of the extract phase, through the gateway.

1. **Receipts first** (§9.1). In one transaction, after the owner lock (a lost claim is `FencedOut` and reads
   nothing), the draft and `spend_ledger.receipts(…, purpose='extract_profile')` of this operation: the
   newest completed call per `logical_call_no`. `llm_outcome.walk` gives the step: use a paid output with no
   request, fail with a terminal code, or make logical call 1 or the repair (2). A draft that left
   `extracting_profile` ends the operation `succeeded` as obsolete.
2. **The request.** A reviewed text must hash to `reviewed_text_sha256` (else an `InvariantViolation`, a §17.1
   bug), and the rendered `<cv_text>…</cv_text>` must hold exactly it: `transmitted_text_sha256`, the
   sha256 of the string inside the tags, equals `reviewed_text_sha256` (§9.5). A native draft sends its PDF
   as a `document` block (§12.7; a missing or undecryptable file is `cv_file_unreadable`), and transmits no
   text. `input_refs` holds ids and hex hashes only: `profile_draft_id`, `cv_file_id`,
   `reviewed_text_sha256`, `transmitted_text_sha256` and `native_pdf` (the gateway adds the owner, whose
   operation id is what the receipts match, and `deadline_s`).
3. **The call** (`llm_outcome.drive`): `purpose='extract_profile'`, non-scoring, the **active** model policy
   and execution config (a non-score call uses the active ones, §13.3), `OperationOwner(claim)`, the draft's
   id, the native flag (the gateway then counts the exact request and reserves `native_pdf_bound` plus the
   locked month row's buffer share), `logical_call_no` 1 or 2, and `deadline = monotonic() + deadline_s` of
   that execution config, per call (Deviation 28). The fault point `profile.extract.after_call` follows every
   call that settled, whether it returned or raised.
4. **The outcome** (`llm_outcome`'s mapping): the call's id goes to the state (`llm_call_id`; its output stays
   in `llm_calls.output_json`, never in a checkpoint); a failure ends the draft `failed(code)` with the
   operation (`fail_extraction`, guarded by the generation's owner predicate); a transient failure gives the
   operation back with `ctx.requeue_in(…, error_code='llm_transient', not_before=now + 5 min)`, whose attempt
   limit and failure hook end the draft once it is spent.

Logs carry ids, codes and amounts only: never the text, the PDF or the output.
"""

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Final

from langchain_core.runnables import RunnableConfig
from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.crypto import DecryptError, data_keys
from jobhunter.db.sql import q
from jobhunter.errors import InvariantViolation
from jobhunter.faults import afault_point
from jobhunter.graphs.profile.deps import ProfileDeps
from jobhunter.graphs.profile.nodes import (
    CV_FILE_UNREADABLE,
    EXTRACTING_PROFILE,
    ExtractDraft,
    Node,
    end_obsolete_in_own_tx,
    execution,
    fail_extraction,
    fetch_extract_draft,
    llm_outcome,
    log,
)
from jobhunter.graphs.profile.nodes.llm_outcome import Call, Fail, Requeue, Use
from jobhunter.graphs.profile.state import REQUEUED, ProfileState, ProfileUpdate
from jobhunter.llm import spend_ledger
from jobhunter.llm.errors import LLMError
from jobhunter.llm.gateway import CallContext, NodeConfig, RenderedPrompt, node_config
from jobhunter.llm.prompts.render import extract_profile_native_pdf_prompt, extract_profile_prompt
from jobhunter.llm.schemas.profile import ProfileOut
from jobhunter.llm.spend_ledger import OperationOwner, Receipt
from jobhunter.types import JSONValue
from jobhunter.worker.operations import OperationContext, OperationDeps, lock_operation_owner

NAME: Final = "extract_profile"
PURPOSE: Final = "extract_profile"
AFTER_CALL: Final = "profile.extract.after_call"
_CV_OPEN: Final = "<cv_text>"
_CV_CLOSE: Final = "</cv_text>"

# The active model policy and execution config (§13.3: every non-score call uses the active ones).
_ACTIVE_LLM_CONFIG = q(
    "SELECT a.execution_config_id, m.payload AS model_policy, e.payload AS execution FROM active_config a "
    "JOIN model_policies m ON m.id = a.model_policy_id "
    "JOIN execution_configs e ON e.id = a.execution_config_id WHERE a.id = 1"
)


def sha256(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8")).digest()


async def _receipts(
    engine: AsyncEngine, op: OperationContext, draft_id: int
) -> tuple[ExtractDraft | None, dict[int, Receipt]]:
    """The draft and the operation's receipts, read in one transaction after the owner lock (§9.1)."""
    async with engine.begin() as conn:
        await lock_operation_owner(conn, op.claim)
        draft = await fetch_extract_draft(conn, draft_id)
        receipts = await spend_ledger.receipts(conn, operation_id=op.claim.operation_id, purpose=PURPOSE)
    return draft, receipts


async def _active_node(engine: AsyncEngine) -> tuple[NodeConfig, int]:
    """`extract_profile`'s node from the active model policy and execution config, and the config's id."""
    async with engine.connect() as conn:
        row = (await conn.execute(_ACTIVE_LLM_CONFIG)).one()
    node = node_config(PURPOSE, model_policy=row.model_policy, execution=row.execution, schema=ProfileOut)
    return node, int(row.execution_config_id)


def _transmitted_text(prompt: RenderedPrompt) -> str:
    """The exact string inside `<cv_text>…</cv_text>` of a rendered reviewed-text request."""
    content = prompt.messages[0].content
    if not isinstance(content, str) or not (content.startswith(_CV_OPEN) and content.endswith(_CV_CLOSE)):
        raise InvariantViolation("the extract_profile request does not frame its CV text in <cv_text>")
    return content[len(_CV_OPEN) : len(content) - len(_CV_CLOSE)]


def _hex(digest: bytes | None) -> str | None:
    return None if digest is None else digest.hex()


def _request(od: OperationDeps, draft: ExtractDraft, draft_id: int) -> RenderedPrompt | str:
    """The request of `draft` (module docstring, step 2), or the code that ends the draft."""
    transmitted: bytes | None = None
    if draft.native_pdf:
        if draft.storage_path is None:
            return CV_FILE_UNREADABLE
        try:
            pdf = od.cv_store.read(draft.storage_path, data_keys(od.cfg))
        except (FileNotFoundError, DecryptError) as exc:
            log.warning("profile_cv_unreadable", draft_id=draft_id, error_class=type(exc).__name__)
            return CV_FILE_UNREADABLE
        prompt = extract_profile_native_pdf_prompt(pdf)
    else:
        text, reviewed = draft.reviewed_text, draft.reviewed_text_sha256
        if text is None or reviewed is None or sha256(text) != reviewed:
            raise InvariantViolation(f"profile draft {draft_id}: reviewed_text does not match its sha256")
        prompt = extract_profile_prompt(text)
        transmitted = sha256(_transmitted_text(prompt))
        if transmitted != reviewed:  # §9.5: the model receives exactly the approved text
            raise InvariantViolation(f"profile draft {draft_id}: the text sent is not the reviewed one")
    refs: dict[str, JSONValue] = {
        "profile_draft_id": draft_id,
        "cv_file_id": draft.cv_file_id,
        "reviewed_text_sha256": _hex(draft.reviewed_text_sha256),
        "transmitted_text_sha256": _hex(transmitted),
        "native_pdf": draft.native_pdf,
    }
    return replace(prompt, input_refs=refs)


def _caller(
    op: OperationContext,
    od: OperationDeps,
    node: NodeConfig,
    prompt: RenderedPrompt,
    execution_config_id: int,
    draft_id: int,
    *,
    native_pdf: bool,
) -> Callable[[int], Awaitable[int]]:
    """The gateway call of one logical call (module docstring, step 3), for `llm_outcome.drive`: the call's
    id, or the gateway's `LLMError`. The fault point `profile.extract.after_call` follows the settled call."""
    claim = op.claim

    async def call(logical_call_no: int) -> int:
        ctx = CallContext(
            owner=OperationOwner(claim),
            purpose_class="nonscoring",
            execution_config_id=execution_config_id,
            deadline=od.gateway.monotonic() + node.deadline_s,  # the active deadline, per call
            profile_draft_id=draft_id,
            logical_call_no=logical_call_no,
            native_pdf=native_pdf,
        )
        try:
            result = await od.gateway.call(node, prompt, ctx)
        except LLMError:
            await afault_point(AFTER_CALL)  # the call settled (its tries did), with no output
            raise
        await afault_point(AFTER_CALL)
        log.info(
            "profile_extract_called",
            operation_id=claim.operation_id,
            draft_id=draft_id,
            llm_call_id=result.llm_call_id,
            logical_call_no=logical_call_no,
            cost_usd=str(
                result.cost_usd
            ),  # the logical call's total over its tries, as the gateway counts it
        )
        return result.llm_call_id

    return call


def make_extract_profile(deps: ProfileDeps) -> Node:
    async def extract_profile(state: ProfileState, config: RunnableConfig) -> ProfileUpdate:
        op, od = execution(config)
        draft_id = state["draft_id"]
        claim = op.claim
        draft, receipts = await _receipts(deps.engine, op, draft_id)
        if draft is None or draft.status != EXTRACTING_PROFILE:
            return {"steps": [NAME], "outcome": await end_obsolete_in_own_tx(deps.engine, op, draft_id)}
        step: Use | Call | Fail | Requeue = llm_outcome.walk(receipts)
        if isinstance(step, Use):
            log.info(
                "profile_extract_receipt_used",
                operation_id=claim.operation_id,
                draft_id=draft_id,
                llm_call_id=step.llm_call_id,
                logical_call_no=step.logical_call_no,
            )
        elif isinstance(step, Call):
            request = _request(od, draft, draft_id)
            if isinstance(request, str):
                step = Fail(request)
            else:
                node, execution_config_id = await _active_node(deps.engine)
                call = _caller(
                    op, od, node, request, execution_config_id, draft_id, native_pdf=draft.native_pdf
                )
                step = await llm_outcome.drive(step, call)
        if isinstance(step, Use):
            return {"steps": [NAME], "llm_call_id": step.llm_call_id}
        if isinstance(step, Fail):
            outcome = await fail_extraction(deps.engine, op, draft_id, step.code, owner=draft.owner())
            return {"steps": [NAME], "outcome": outcome}
        if isinstance(step, Call):  # `drive` makes every call a step asks for
            raise InvariantViolation(f"profile draft {draft_id}: a call was left unmade")
        async with deps.engine.begin() as conn:
            await op.requeue_in(conn, error_code=step.code, not_before=od.clock() + llm_outcome.REQUEUE_AFTER)
        log.info(
            "profile_extract_requeued", operation_id=claim.operation_id, draft_id=draft_id, code=step.code
        )
        return {"steps": [NAME], "outcome": REQUEUED}

    return extract_profile
