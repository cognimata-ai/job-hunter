"""The `propose_queries` node (spec §5.5.1, §9.1, §9.5, §10, §11.12, §13.3; M2 plan Task 20, Deviations 20, 26
and 28): the one node of the `propose` graph, the paid call of a query proposal through the gateway.

1. **Receipts first** (§9.1). In one transaction, after the owner lock (a lost claim is `FencedOut` and reads
   nothing), the proposal and `spend_ledger.receipts(…, purpose='propose_queries')` of this operation: the
   newest completed call per `logical_call_no`, matched on the owner the gateway writes into `input_refs`.
   `llm_outcome.walk` gives the step. A completed call with an output is checked again against the location
   words of the scope version pinned in the operation's payload (`scope_version_id`, the active one when the
   proposal was requested; `validate_proposal` is pure, so the check a crash interrupted gives the same answer
   whatever scope was saved since): a usable set is published with no request; a `set_invalid` on call 1 makes
   the repair (logical call 2) due, and on call 2 fails the proposal `invalid_output`. A structurally invalid
   call 1 (`invalid_output` without an output) makes the repair due as well, and a terminal code fails with
   it, all as `walk` has it. So the one repair is made exactly once across reclaims. A proposal that is no
   longer `running` ends the operation `succeeded` as obsolete.
2. **The request**, only when a call is due: `render.propose_queries_prompt` of the proposal's profile version
   and the location labels of the pinned scope version, the scope active at the request (§5.5.1), with
   `input_refs` of ids and hex hashes only: `proposal_id`, `profile_version_id`, `scope_version_id` and
   `profile_hash` (the gateway adds the owner and `deadline_s`). A version whose profile is gone (purged)
   fails the proposal `profile_unavailable` without a request; a paid usable set is still published.
3. **The call** (`llm_outcome.drive` with the semantic check as its `usable`): `purpose='propose_queries'`,
   non-scoring, the active model policy and execution config (`gateway.active_node_config`, §13.3),
   `OperationOwner(claim)`, `logical_call_no` 1 or 2 with the stage the walk gives it (`start_stage`:
   `enlarged` after a paid first stage that stopped at `max_tokens`), and `deadline = monotonic() +
   deadline_s` of that execution config, per call (Deviation 28). The fault point
   `queries.propose.after_call` follows every gateway call that returned or raised an `LLMError`: after the
   settled tries and before `running → ready`.
   `set_invalid` and `InvalidOutput("structural")` take the one repair (`llm_outcome.after_invalid`); the
   gateway's other errors map as for `extract_profile` (`llm_outcome.after_error`, the same mapping).
4. **The outcome**, each in one transaction after the owner lock and the proposal `FOR UPDATE`: a usable set
   moves the proposal `running → ready` (`proposals.publish`: the kept items and the call's id) and finishes
   the operation `succeeded`; a failure moves it `running → failed` and finishes the operation
   `failed(<code>)`, so the failure hook finds the row ended; a proposal no longer `running` ends the
   operation obsolete. A transient failure gives the operation back (`requeue_in`, `llm_transient`, five
   minutes on the injected clock); a requeue that spends the last attempt ends it `failed`, and its hook the
   proposal.

The packaged gazetteer is loaded in a worker thread (`asyncio.to_thread`): its first load reads the data
files, which would otherwise hold the event loop. The state holds ids only (the call's id, never its output,
§16). Logs carry ids, codes and counts: never a query text, a rationale or a profile value.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Final

from langchain_core.runnables import RunnableConfig
from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.db.sql import q
from jobhunter.errors import InvariantViolation
from jobhunter.faults import afault_point
from jobhunter.geo.gazetteer import Gazetteer
from jobhunter.graphs.profile.deps import ProfileDeps
from jobhunter.graphs.profile.nodes import ProposalNode, execution, llm_outcome, log
from jobhunter.graphs.profile.nodes.llm_outcome import Call, Fail, Requeue, Use
from jobhunter.graphs.profile.state import FAILED, OBSOLETE, READY, REQUEUED, ProposalState, ProposalUpdate
from jobhunter.llm import spend_ledger
from jobhunter.llm.errors import LLMError
from jobhunter.llm.gateway import CallContext, NodeConfig, RenderedPrompt, active_node_config
from jobhunter.llm.prompts.render import propose_queries_prompt
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.llm.schemas.queries import QueryProposalOut
from jobhunter.llm.spend_ledger import OperationOwner, Receipt
from jobhunter.queries import proposals
from jobhunter.queries.proposals import ProposalRow, ProposedQuery
from jobhunter.run_settings import scope_from_payload
from jobhunter.types import JSONValue, get_int
from jobhunter.worker.operations import OperationContext, OperationDeps, lock_operation_owner

NAME: Final = "propose_queries"
PURPOSE: Final = "propose_queries"
AFTER_CALL: Final = "queries.propose.after_call"
PROFILE_UNAVAILABLE: Final = "profile_unavailable"

_PROPOSAL: Final = q("SELECT status, profile_version_id FROM query_proposals WHERE id = :p")
_VERSION: Final = q("SELECT profile_json, profile_hash FROM profile_versions WHERE id = :pv")
_SCOPE: Final = q("SELECT payload FROM scope_versions WHERE id = :s")

type Checked = tuple[list[ProposedQuery], list[str]]


async def _receipts(
    engine: AsyncEngine, op: OperationContext, proposal_id: int
) -> tuple[ProposalRow | None, dict[int, Receipt]]:
    """The proposal and the operation's receipts, read in one transaction after the owner lock (§9.1)."""
    async with engine.begin() as conn:
        await lock_operation_owner(conn, op.claim)
        row = (await conn.execute(_PROPOSAL, {"p": proposal_id})).first()
        receipts = await spend_ledger.receipts(conn, operation_id=op.claim.operation_id, purpose=PURPOSE)
    proposal = None if row is None else ProposalRow(str(row.status), int(row.profile_version_id))
    return proposal, receipts


async def _scope_labels(engine: AsyncEngine, scope_version_id: int) -> tuple[str, ...]:
    """The labels of the scope version's locations (§5.5.1), read without a lock (scope versions are
    immutable)."""
    async with engine.connect() as conn:
        payload = (await conn.execute(_SCOPE, {"s": scope_version_id})).scalar_one()
    return tuple(location.label for location in scope_from_payload(payload).locations)


async def _request(
    engine: AsyncEngine,
    proposal_id: int,
    profile_version_id: int,
    scope_version_id: int,
    labels: tuple[str, ...],
) -> RenderedPrompt | None:
    """The request of the proposal (module docstring, step 2), or `None` when its profile is gone."""
    async with engine.connect() as conn:
        row = (await conn.execute(_VERSION, {"pv": profile_version_id})).first()
    if row is None or row.profile_json is None:
        return None
    prompt = propose_queries_prompt(ConfirmedProfile.model_validate(row.profile_json), labels)
    refs: dict[str, JSONValue] = {
        "proposal_id": proposal_id,
        "profile_version_id": profile_version_id,
        "scope_version_id": scope_version_id,
        "profile_hash": bytes(row.profile_hash).hex(),
    }
    return replace(prompt, input_refs=refs)


class _Checker:
    """The semantic check of the outputs one execution meets, by call id: a receipt's (`output_json`) or a new
    call's (`GatewayResult.parsed`), each checked once (`llm_outcome.drive`'s `usable`)."""

    def __init__(self, location_words: frozenset[str]) -> None:
        self._location_words = location_words
        self._outputs: dict[int, QueryProposalOut] = {}
        self._checked: dict[int, Checked] = {}

    def add(self, llm_call_id: int, output: QueryProposalOut) -> None:
        self._outputs[llm_call_id] = output

    def add_receipts(self, receipts: dict[int, Receipt]) -> None:
        for receipt in receipts.values():
            if receipt.output_json is not None:
                self.add(receipt.llm_call_id, QueryProposalOut.model_validate(receipt.output_json))

    def checked(self, llm_call_id: int) -> Checked:
        if llm_call_id not in self._checked:
            output = self._outputs[llm_call_id]
            self._checked[llm_call_id] = proposals.validate_proposal(
                output, location_words=self._location_words
            )
        return self._checked[llm_call_id]

    def usable(self, use: Use) -> bool:
        return proposals.SET_INVALID not in self.checked(use.llm_call_id)[1]


def _caller(
    op: OperationContext,
    od: OperationDeps,
    node: NodeConfig,
    prompt: RenderedPrompt,
    execution_config_id: int,
    proposal_id: int,
    checker: _Checker,
) -> Callable[[Call], Awaitable[int]]:
    """The gateway call of one logical call (module docstring, step 3), for `llm_outcome.drive`: the call's
    id, or the gateway's `LLMError`. The fault point `queries.propose.after_call` follows the call whether it
    returned or raised."""
    claim = op.claim

    async def call(step: Call) -> int:
        logical_call_no = step.logical_call_no
        ctx = CallContext(
            owner=OperationOwner(claim),
            purpose_class="nonscoring",
            execution_config_id=execution_config_id,
            deadline=od.gateway.monotonic() + node.deadline_s,  # the active deadline, per call
            logical_call_no=logical_call_no,
            start_stage=step.start_stage,
        )
        try:
            result = await od.gateway.call(node, prompt, ctx)
        except LLMError:
            await afault_point(AFTER_CALL)  # no output: its tries settled, or none was reserved
            raise
        await afault_point(AFTER_CALL)
        if not isinstance(result.parsed, QueryProposalOut):
            raise InvariantViolation(f"llm_calls {result.llm_call_id}: not a QueryProposalOut")
        checker.add(result.llm_call_id, result.parsed)
        log.info(
            "query_proposal_called",
            operation_id=claim.operation_id,
            proposal_id=proposal_id,
            llm_call_id=result.llm_call_id,
            logical_call_no=logical_call_no,
            cost_usd=str(result.cost_usd),
        )
        return result.llm_call_id

    return call


async def _end(
    engine: AsyncEngine, op: OperationContext, proposal_id: int, step: Use | Fail, checker: _Checker
) -> str:
    """Publish `step`'s set or fail with its code, with the operation's end, in one transaction (module
    docstring, step 4); the state's `outcome`."""
    claim = op.claim
    kept, issues = checker.checked(step.llm_call_id) if isinstance(step, Use) else ([], [])
    async with engine.begin() as conn:
        await lock_operation_owner(conn, claim)
        proposal = await proposals.lock_proposal(conn, proposal_id)
        if proposal is None or proposal.status != proposals.RUNNING:
            await proposals.end_obsolete(conn, op, proposal_id, None if proposal is None else proposal.status)
            return OBSOLETE
        if isinstance(step, Use):
            await proposals.publish(conn, proposal_id, kept, step.llm_call_id)
            await op.finish_in(conn, status="succeeded")
        else:
            await proposals.fail(conn, proposal_id)
            await op.finish_in(conn, status="failed", error_code=step.code)
    if isinstance(step, Fail):
        log.info(
            "query_proposal_failed", operation_id=claim.operation_id, proposal_id=proposal_id, code=step.code
        )
        return FAILED
    log.info(
        "query_proposal_ready",
        operation_id=claim.operation_id,
        proposal_id=proposal_id,
        llm_call_id=step.llm_call_id,
        logical_call_no=step.logical_call_no,
        items=len(kept),
        issues=sorted(issues),
    )
    return READY


def _log_set_invalid(op: OperationContext, proposal_id: int, use: Use, *, receipt: bool) -> None:
    log.info(
        "query_proposal_set_invalid",
        operation_id=op.claim.operation_id,
        proposal_id=proposal_id,
        llm_call_id=use.llm_call_id,
        logical_call_no=use.logical_call_no,
        receipt=receipt,
    )


def make_propose_queries(deps: ProfileDeps) -> ProposalNode:
    async def propose_queries(state: ProposalState, config: RunnableConfig) -> ProposalUpdate:
        op, od = execution(config)
        proposal_id = state["proposal_id"]
        claim = op.claim
        proposal, receipts = await _receipts(deps.engine, op, proposal_id)
        if proposal is None or proposal.status != proposals.RUNNING:
            async with deps.engine.begin() as conn:  # the owner lock, then the proposal's (§9.1)
                await lock_operation_owner(conn, claim)
                locked = await proposals.lock_proposal(conn, proposal_id)
                await proposals.end_obsolete(conn, op, proposal_id, None if locked is None else locked.status)
            return {"steps": [NAME], "outcome": OBSOLETE}
        scope_version_id = get_int(dict(claim.payload), "scope_version_id")  # pinned at the request
        labels = await _scope_labels(deps.engine, scope_version_id)
        gazetteer = await asyncio.to_thread(Gazetteer.load)  # its first load reads files: off the loop
        checker = _Checker(proposals.location_words(labels, gazetteer))
        checker.add_receipts(receipts)
        step: Use | Call | Fail | Requeue = llm_outcome.walk(receipts)
        if isinstance(step, Use):
            if checker.usable(step):
                log.info(
                    "query_proposal_receipt_used",
                    operation_id=claim.operation_id,
                    proposal_id=proposal_id,
                    llm_call_id=step.llm_call_id,
                    logical_call_no=step.logical_call_no,
                )
            else:
                _log_set_invalid(op, proposal_id, step, receipt=True)
                step = llm_outcome.after_invalid(step.logical_call_no)
        if isinstance(step, Call):
            prompt = await _request(
                deps.engine, proposal_id, proposal.profile_version_id, scope_version_id, labels
            )
            if prompt is None:
                step = Fail(PROFILE_UNAVAILABLE)
            else:
                node, execution_config_id = await active_node_config(deps.engine, PURPOSE, QueryProposalOut)
                call = _caller(op, od, node, prompt, execution_config_id, proposal_id, checker)

                def usable(use: Use) -> bool:
                    ok = checker.usable(use)
                    if not ok:
                        _log_set_invalid(op, proposal_id, use, receipt=False)
                    return ok

                step = await llm_outcome.drive(step, call, usable=usable)
        if isinstance(step, Use | Fail):
            outcome = await _end(deps.engine, op, proposal_id, step, checker)
            update: ProposalUpdate = {"steps": [NAME], "outcome": outcome}
            if isinstance(step, Use) and outcome == READY:
                update["llm_call_id"] = step.llm_call_id
            return update
        if isinstance(step, Call):  # `drive` makes every call a step asks for
            raise InvariantViolation(f"query proposal {proposal_id}: a call was left unmade")
        async with deps.engine.begin() as conn:
            await op.requeue_in(conn, error_code=step.code, not_before=od.clock() + llm_outcome.REQUEUE_AFTER)
        # the requeue of the last attempt ends the operation `failed` (and its hook the proposal)
        spent = op.ended is not None and op.ended.status == "failed"
        log.info(
            "query_proposal_requeued",
            operation_id=claim.operation_id,
            proposal_id=proposal_id,
            code=step.code,
            attempts_spent=spent,
        )
        return {"steps": [NAME], "outcome": FAILED if spent else REQUEUED}

    return propose_queries
