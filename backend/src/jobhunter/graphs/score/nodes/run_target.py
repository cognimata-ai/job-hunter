"""The `score_target` node of `score_graph` (spec §9.3, §9.4.3; plan Task 11, Codex r1 #12, #13, r2 B6, B25,
Rulings T11-a, T11-f, Controller carries T9-b, T9-c): `run_target`, the owned-child wrapper that runs one
claim's attempt through the compiled `score_target` subgraph (never the subgraph registered as the node
itself).

For the claim `ref` the fan-out sent (`ClaimRef`), with the cycle's claim heartbeats (`ctx.claims`, started by
the `claim` node after its commit):
- a claim found lost already (its beat was refused while it waited for a fan-out slot) returns `claim_lost` at
  once, without starting the subgraph;
- otherwise the subgraph runs in a child task, `subgraph.ainvoke(initial_state(ref, attempt_id), config)` (the
  parent's config: the subgraph, compiled with `checkpointer=None`, checkpoints in the cycle's thread),
  attached to the claim (`claims.attach`) and awaited without letting its own end raise here (`asyncio.wait`),
  both inside the guard that cancels the child: an `attach` that raises leaves no attempt running that nobody
  awaits;
- `claims.stop(ref)` always, at the end.

**How the attempt's end is classified** (Ruling T11-a; Controller carry T9-c: whatever escapes the subgraph
becomes the claim's outcome, never the cycle's failure):
- its normal end is the subgraph's outcome;
- (a) a node's `NodeTimeoutError` (a deterministic node's: an LLM node maps its own to `fail`) takes `fail`'s
  transient path (`nodes.fail`, run here in a transaction of its own, under the claim predicate) with the
  code `node_timeout`: the target back to `pending` with `not_before = now + min(6 h, 15 min x
  2^transient_failures)` and `transient_failures + 1`, or `failed_transient('node_timeout')` once its charged
  attempts are spent; the attempt `transient_error('node_timeout')`;
- (b) the claim's own fence (`ClaimLost`, the `FencedOut` of `SCORE_TARGET_OWNER`: the claim predicate failed
  under an owner lock), and the child's cancellation while the claim is lost (a refused beat cancelled it),
  are ownership loss: `claim_lost`, with no error path and no error line (whoever changed the predicate ended
  the attempt; the gateway's own cancellation path settled any stream `ambiguous`);
- (c) the cancellation of this node itself (the cycle's shutdown, a sibling's failure) cancels the child,
  awaits it up to `CANCEL_TIMEOUT_S` (less than the 30 s the worker waits for the cycle's task), and
  propagates; so does the worker's own `FencedOut` (`worker_superseded`: a newer worker took over, and the
  cycle is the takeover's now);
- (d) any other exception (a `NodeFailed`, an invariant, a `FencedOut` of another owner), and the child's
  cancellation while the claim holds and this node is not being cancelled, is a bug (Ruling T11-f): logged
  with its class only (`score_target_failed`), then `fail`'s path with the code `bug`: the target
  `failed_invalid_output('bug')`, the attempt `failed_invalid_output('bug')`. The reason `bug` is what tells a
  code fault from a model's invalid output (`invalid_output`, `unexpected_stop_reason`): the same terminal
  family as `publish`'s `failed_invalid_output('publication_invalid')`, which the user's Retry resets like
  any `failed_*`, so no later claim round admits the target again into the same fault.
Neither (a) nor (d) runs the attempt again (Controller carry T9-b): a new attempt is a new claim's. When the
claim was lost meanwhile, `fail` writes nothing and the outcome is `claim_lost`.

It returns `{"results": {work_id: TargetOutcome}}`: the attempt's outcome (the subgraph's or `fail`'s, else
the attempt row's), the target's state, the attempt's lane and its `cost_usd`, read after the attempt ended.
One lost or failed claim never aborts its siblings or the cycle.
"""

import asyncio
from typing import Final

from langchain_core.runnables import RunnableConfig
from langgraph.errors import NodeTimeoutError

from jobhunter.db.sql import q
from jobhunter.errors import FencedOut
from jobhunter.graphs.score.context import execution
from jobhunter.graphs.score.deps import ScoreDeps
from jobhunter.graphs.score.llm_step import BUG, NODE_TIMEOUT
from jobhunter.graphs.score.nodes import CycleStateNode, log
from jobhunter.graphs.score.nodes import fail as fail_node
from jobhunter.graphs.score.state import ClaimRef, CycleUpdate, TargetOutcome, initial_state, work_id
from jobhunter.graphs.score.subgraph import ScoreTargetGraph
from jobhunter.llm.errors import SCORE_TARGET_OWNER
from jobhunter.scoring.routing import CLAIM_LOST

NAME: Final = "score_target"
# Below the worker's own bound on the cycle's task (`worker.main.EXECUTIONS_CANCEL_S`, 30 s).
CANCEL_TIMEOUT_S: Final = 20.0

_ATTEMPT_ID: Final = q("SELECT id FROM score_attempts WHERE target_id = :t AND attempt_no = :a")
_PROJECTION: Final = q(
    "SELECT a.outcome, a.lane, a.cost_usd, t.state FROM score_attempts a "
    "JOIN score_targets t ON t.id = a.target_id WHERE a.target_id = :t AND a.attempt_no = :a"
)

type TargetNode = CycleStateNode[ClaimRef]


async def _projected(deps: ScoreDeps, ref: ClaimRef, outcome: str | None) -> CycleUpdate:
    """The claim's `TargetOutcome` (module docstring), keyed by its work id."""
    async with deps.engine.connect() as conn:
        row = (await conn.execute(_PROJECTION, {"t": ref["target_id"], "a": ref["attempt_no"]})).one()
    projected: TargetOutcome = {
        "target_id": ref["target_id"],
        "attempt_no": ref["attempt_no"],
        "outcome": str(row.outcome) if outcome is None else outcome,
        "state": str(row.state),
        "lane": None if row.lane is None else str(row.lane),
        "cost_usd": str(row.cost_usd),
    }
    return {"results": {work_id(ref["target_id"], ref["attempt_no"]): projected}}


def _retrieved(child: "asyncio.Task[object]") -> None:
    """Mark the ended `child`'s exception as retrieved (a cancellation outranks it)."""
    if child.done() and not child.cancelled() and (error := child.exception()) is not None:
        log.warning("score_target_failed_while_cancelled", error_class=type(error).__name__)


def _cancelling() -> bool:
    """Whether this node's own task is being cancelled (case (c))."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def make_run_target(deps: ScoreDeps, subgraph: ScoreTargetGraph) -> TargetNode:
    fail = fail_node.make_fail(deps)

    async def lost(ref: ClaimRef) -> CycleUpdate:
        log.info("score_claim_lost", node=NAME, target_id=ref["target_id"], attempt_no=ref["attempt_no"])
        return await _projected(deps, ref, CLAIM_LOST)

    async def failed(ref: ClaimRef, attempt_id: int, code: str, config: RunnableConfig) -> CycleUpdate:
        """`fail`'s path for `code` (cases (a) and (d)), in a transaction of its own; the claim's outcome."""
        state = initial_state(ref, attempt_id)
        state["error_code"] = code
        update = (await fail(state, config)).update
        outcome = update.get("outcome") if isinstance(update, dict) else None
        return await _projected(deps, ref, None if outcome is None else str(outcome))

    async def bug(ref: ClaimRef, attempt_id: int, error_class: str, config: RunnableConfig) -> CycleUpdate:
        log.error(
            "score_target_failed",
            target_id=ref["target_id"],
            attempt_no=ref["attempt_no"],
            error_class=error_class,
        )
        return await failed(ref, attempt_id, BUG, config)

    async def run_target(state: ClaimRef, config: RunnableConfig) -> CycleUpdate:
        ref = state  # the node's input is the claim the fan-out sent
        claims = execution(config).claims
        try:
            if claims.lost(ref):
                return await lost(ref)
            async with deps.engine.connect() as conn:
                found = await conn.execute(_ATTEMPT_ID, {"t": ref["target_id"], "a": ref["attempt_no"]})
                attempt_id = int(found.scalar_one())

            async def attempt() -> object:
                return await subgraph.ainvoke(initial_state(ref, attempt_id), config)

            child = asyncio.create_task(attempt(), name=f"score_target:{ref['target_id']}")
            try:
                claims.attach(ref, child)
                await asyncio.wait({child})
            except BaseException:  # this node's own cancellation, or `attach`'s error: no child outlives it
                child.cancel()
                await asyncio.wait({child}, timeout=CANCEL_TIMEOUT_S)
                _retrieved(child)
                raise
            if child.cancelled():
                if claims.lost(ref):  # (b) ownership loss: the reclaim ended the attempt
                    return await lost(ref)
                if _cancelling():  # (c)
                    raise asyncio.CancelledError
                return await bug(ref, attempt_id, "CancelledError", config)  # (d)
            error = child.exception()
            if error is None:
                final = child.result()
                outcome = final.get("outcome") if isinstance(final, dict) else None
                return await _projected(deps, ref, None if outcome is None else str(outcome))
            if not isinstance(error, Exception) or (isinstance(error, FencedOut) and error.worker_superseded):
                raise error  # (c)
            if isinstance(error, NodeTimeoutError):  # (a)
                log.warning(
                    "score_target_timed_out",
                    target_id=ref["target_id"],
                    attempt_no=ref["attempt_no"],
                    timed_out_node=error.node,
                    timeout_s=error.timeout,
                )
                return await failed(ref, attempt_id, NODE_TIMEOUT, config)
            if isinstance(error, FencedOut) and error.owner == SCORE_TARGET_OWNER:  # (b)
                return await lost(ref)
            return await bug(ref, attempt_id, type(error).__name__, config)  # (d)
        finally:
            await claims.stop(ref)

    return run_target
