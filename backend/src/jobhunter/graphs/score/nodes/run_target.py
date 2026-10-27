"""The `score_target` node of `score_graph` (spec §9.3, §9.4.3; plan Task 11, Codex r1 #12, #13, r2 B6, B25,
Rulings T11-a, T11-f, T11-g, Controller carries T9-b, T9-c): `run_target`, the owned-child wrapper that runs
one claim's attempt through the compiled `score_target` subgraph (never the subgraph registered as the node
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

**How the attempt's end is classified** (Rulings T11-a, T11-f, T11-g; Controller carry T9-c: whatever escapes
the subgraph becomes the claim's outcome, never the cycle's failure):

| what ended the attempt                                       | case | outcome                       |
|--------------------------------------------------------------|------|-------------------------------|
| its normal end                                               |      | the subgraph's                |
| a node's `NodeTimeoutError`                                  | (a)  | `fail`, code `node_timeout`   |
| a retryable database error past its node's retries          | (a)  | `fail`, code `db_transient`   |
| `ClaimLost`; the child cancelled while the claim is lost     | (b)  | `claim_lost`, nothing written |
| this node's cancellation; the worker's own `FencedOut`       | (c)  | propagates                    |
| anything else; the child cancelled while the claim holds     | (d)  | `fail`, code `bug`            |

- (a) a node's `NodeTimeoutError` (a deterministic node's: an LLM node maps its own to `fail`), and a
  retryable database error that outlasted its node's retries (`graphs.sanitize.is_retryable_db_error`: a
  `NodeFailed` with `retryable_db_error`, a deterministic node's after `DB_RETRY`'s three tries or an LLM
  node's, which takes no `RetryPolicy`, or a raw one of the checkpointer; Ruling T11-g), take `fail`'s
  transient path with the code `node_timeout` or `db_transient`: the target back to `pending` with
  `not_before = now + min(6 h, 15 min x 2^transient_failures)` and `transient_failures + 1`, or
  `failed_transient(<code>)` once its charged attempts are spent; the attempt `transient_error(<code>)`;
- (b) the claim's own fence (`ClaimLost`, the `FencedOut` of `SCORE_TARGET_OWNER`: the claim predicate failed
  under an owner lock), and the child's cancellation while the claim is lost (a refused beat cancelled it),
  are ownership loss: `claim_lost`, with no error path and no error line (whoever changed the predicate ended
  the attempt; the gateway's own cancellation path settled any stream `ambiguous`);
- (c) the cancellation of this node itself (the cycle's shutdown, a sibling's failure) cancels the child,
  awaits it up to `CANCEL_TIMEOUT_S` (less than the 30 s the worker waits for the cycle's task), and
  propagates; so does the worker's own `FencedOut` (`worker_superseded`: a newer worker took over, and the
  cycle is the takeover's now);
- (d) any other exception (a non-retryable `NodeFailed`, an invariant, a `FencedOut` of another owner), and
  the child's cancellation while the claim holds and this node is not being cancelled, is a bug (Ruling
  T11-f): logged with its class only (`score_target_failed`), then `fail`'s path with the code `bug`: the
  target `failed_invalid_output('bug')`, the attempt `failed_invalid_output('bug')`. The reason `bug` is what
  tells a code fault from a model's invalid output (`invalid_output`, `unexpected_stop_reason`): the same
  terminal family as `publish`'s `failed_invalid_output('publication_invalid')`, which the user's Retry
  resets like any `failed_*`, so no later claim round admits the target again into the same fault.
Neither (a) nor (d) runs the attempt again (Controller carry T9-b): a new attempt is a new claim's. When the
claim was lost meanwhile, `fail` writes nothing and the outcome is `claim_lost`.

**`fail` here** is the subgraph's own node function (`nodes.fail.make_fail`), in a transaction of its own
under the claim predicate, with what the subgraph's `fail` node has: up to `DB_RETRY.max_attempts` (3) tries
on `is_retryable_db_error`, `DB_RETRY`'s interval between them, and each try bounded by the subgraph's
`NODE_TIMEOUTS["fail"]` (a lock wait cannot stall the cycle). An error left after the tries, a non-retryable
one or the bound's `TimeoutError` escapes, and the cycle fails `failed('bug')` (`cycles.fail_cycle`
reclaims its claims).

It returns `{"results": {work_id: TargetOutcome}}`: the attempt's outcome (the subgraph's or `fail`'s, else
the attempt row's), the target's state, the attempt's lane and its `cost_usd`, read after the attempt ended.
One lost or failed claim never aborts its siblings or the cycle.
"""

import asyncio
import random
from typing import Final

from langchain_core.runnables import RunnableConfig
from langgraph.errors import NodeTimeoutError
from langgraph.types import Command

from jobhunter.db.sql import q
from jobhunter.errors import FencedOut
from jobhunter.graphs.sanitize import DB_RETRY, is_retryable_db_error
from jobhunter.graphs.score import subgraph as target_graph
from jobhunter.graphs.score.context import execution
from jobhunter.graphs.score.deps import ScoreDeps
from jobhunter.graphs.score.llm_step import BUG, DB_TRANSIENT, NODE_TIMEOUT
from jobhunter.graphs.score.nodes import CycleStateNode, log
from jobhunter.graphs.score.nodes import fail as fail_node
from jobhunter.graphs.score.state import (
    ClaimRef,
    CycleUpdate,
    TargetOutcome,
    TargetState,
    initial_state,
    work_id,
)
from jobhunter.graphs.score.subgraph import ScoreTargetGraph
from jobhunter.llm.errors import SCORE_TARGET_OWNER
from jobhunter.scoring.routing import CLAIM_LOST, Route

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


def _retry_interval(tries: int) -> float:
    """`DB_RETRY`'s wait after the `tries`-th failed try (LangGraph's own formula, jitter included)."""
    interval = min(DB_RETRY.max_interval, DB_RETRY.initial_interval * DB_RETRY.backoff_factor ** (tries - 1))
    return interval + random.uniform(0, 1) if DB_RETRY.jitter else interval  # noqa: S311 - jitter, no secret


def _cancelling() -> bool:
    """Whether this node's own task is being cancelled (case (c))."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def make_run_target(deps: ScoreDeps, subgraph: ScoreTargetGraph) -> TargetNode:
    fail = fail_node.make_fail(deps)

    async def lost(ref: ClaimRef) -> CycleUpdate:
        log.info("score_claim_lost", node=NAME, target_id=ref["target_id"], attempt_no=ref["attempt_no"])
        return await _projected(deps, ref, CLAIM_LOST)

    async def fail_tried(state: TargetState, config: RunnableConfig) -> Command[Route]:
        """`fail`, retried and bounded (module docstring)."""
        tries = 0
        while True:
            tries += 1
            try:
                async with asyncio.timeout(target_graph.NODE_TIMEOUTS["fail"].total_seconds()):
                    return await fail(state, config)
            except Exception as exc:
                if tries >= DB_RETRY.max_attempts or not is_retryable_db_error(exc):
                    raise
                log.warning(
                    "score_target_fail_retried",
                    target_id=state["target_id"],
                    attempt_id=state["attempt_id"],
                    tries=tries,
                    error_class=type(exc).__name__,
                )
            await asyncio.sleep(_retry_interval(tries))

    async def failed(ref: ClaimRef, attempt_id: int, code: str, config: RunnableConfig) -> CycleUpdate:
        """`fail`'s path for `code` (cases (a) and (d)); the claim's outcome."""
        state = initial_state(ref, attempt_id)
        state["error_code"] = code
        update = (await fail_tried(state, config)).update
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
            if is_retryable_db_error(error):  # (a), Ruling T11-g
                log.warning(
                    "score_target_db_transient",
                    target_id=ref["target_id"],
                    attempt_no=ref["attempt_no"],
                    error_class=type(error).__name__,
                )
                return await failed(ref, attempt_id, DB_TRANSIENT, config)
            if isinstance(error, FencedOut) and error.owner == SCORE_TARGET_OWNER:  # (b)
                return await lost(ref)
            return await bug(ref, attempt_id, type(error).__name__, config)  # (d)
        finally:
            await claims.stop(ref)

    return run_target
