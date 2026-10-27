"""The `score_target` node of `score_graph` (spec §9.3, §9.4.3; plan Task 11, Codex r1 #12, #13, r2 B6, B25,
Controller carries T9-b, T9-c): `run_target`, the owned-child wrapper that runs one claim's attempt through
the compiled `score_target` subgraph (never the subgraph registered as the node itself).

For the claim `ref` the fan-out sent (`ClaimRef`), with the cycle's claim heartbeats (`ctx.claims`, started by
the `claim` node after its commit):
- a claim found lost already (its beat was refused while it waited for a fan-out slot) returns `claim_lost` at
  once, without starting the subgraph;
- otherwise the subgraph runs in a child task, `subgraph.ainvoke(initial_state(ref, attempt_id), config)` (the
  parent's config: the subgraph, compiled with `checkpointer=None`, checkpoints in the cycle's thread),
  attached to the claim (`claims.attach`), and awaited without letting its own end raise here
  (`asyncio.wait`):
  - its normal end returns the subgraph's outcome;
  - its cancellation while the claim is lost is ownership loss (the reclaim ended the attempt; the gateway's
    own cancellation path settled any stream `ambiguous`): swallowed, `claim_lost`;
  - the cancellation of this node itself (the cycle's shutdown, a sibling's failure) cancels the child,
    awaits it up to `CANCEL_TIMEOUT_S`, and propagates;
  - the worker's own `FencedOut` (a newer worker took over) propagates: the cycle is the takeover's now;
  - any other exception (a deterministic node's timeout, a `NodeFailed`, an invariant; Controller carry
    T9-c) never ends the cycle: logged with its class only (`score_target_failed`), the claim is reclaimed by
    its owner here (`cycles.reclaim`, kind `claim`, detail `subgraph_failed`: the target back to `pending`,
    or `failed_attempts_exhausted`, the attempt `claim_lost`), so the target is scored again by a new claim
    and a new attempt, never by a re-run of this one (Controller carry T9-b);
- `claims.stop(ref)` always, at the end.
It returns `{"results": {work_id: TargetOutcome}}`: the attempt's outcome (the subgraph's, else the attempt
row's), the target's state, the attempt's lane and its `cost_usd`, read after the attempt ended. One lost or
failed claim never aborts its siblings or the cycle.
"""

import asyncio
from typing import Final

from langchain_core.runnables import RunnableConfig

from jobhunter.db.sql import q
from jobhunter.errors import FencedOut
from jobhunter.graphs.score.context import execution
from jobhunter.graphs.score.deps import ScoreDeps
from jobhunter.graphs.score.nodes import CycleStateNode, log
from jobhunter.graphs.score.state import ClaimRef, CycleUpdate, TargetOutcome, initial_state, work_id
from jobhunter.graphs.score.subgraph import ScoreTargetGraph
from jobhunter.scoring import cycles
from jobhunter.scoring.routing import CLAIM_LOST

NAME: Final = "score_target"
CANCEL_TIMEOUT_S: Final = 30.0

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


async def _failed(deps: ScoreDeps, ref: ClaimRef, error_class: str) -> CycleUpdate:
    """A child that raised: the claim reclaimed by its owner (module docstring), then its outcome."""
    log.error(
        "score_target_failed",
        target_id=ref["target_id"],
        attempt_no=ref["attempt_no"],
        error_class=error_class,
    )
    params = {"target": ref["target_id"], "claim": ref["claim_id"], "attempt_no": ref["attempt_no"]}
    async with deps.engine.begin() as conn:
        await cycles.reclaim(conn, "claim", params, detail=cycles.SUBGRAPH_FAILED)
    return await _projected(deps, ref, None)


def _retrieved(child: "asyncio.Task[object]") -> None:
    """Mark the ended `child`'s exception as retrieved (a cancellation outranks it)."""
    if child.done() and not child.cancelled() and (error := child.exception()) is not None:
        log.warning("score_target_failed_while_cancelled", error_class=type(error).__name__)


def make_run_target(deps: ScoreDeps, subgraph: ScoreTargetGraph) -> TargetNode:
    async def run_target(state: ClaimRef, config: RunnableConfig) -> CycleUpdate:
        ref = state  # the node's input is the claim the fan-out sent
        claims = execution(config).claims
        try:
            if claims.lost(ref):
                log.info(
                    "score_claim_lost", node=NAME, target_id=ref["target_id"], attempt_no=ref["attempt_no"]
                )
                return await _projected(deps, ref, CLAIM_LOST)
            async with deps.engine.connect() as conn:
                found = await conn.execute(_ATTEMPT_ID, {"t": ref["target_id"], "a": ref["attempt_no"]})
                attempt_id = int(found.scalar_one())

            async def attempt() -> object:
                return await subgraph.ainvoke(initial_state(ref, attempt_id), config)

            child = asyncio.create_task(attempt(), name=f"score_target:{ref['target_id']}")
            claims.attach(ref, child)
            try:
                await asyncio.wait({child})
            except asyncio.CancelledError:  # this node's own cancellation: no child outlives it
                child.cancel()
                await asyncio.wait({child}, timeout=CANCEL_TIMEOUT_S)
                _retrieved(child)
                raise
            if child.cancelled():
                if claims.lost(ref):  # ownership loss: the reclaim ended the attempt
                    log.info(
                        "score_claim_lost",
                        node=NAME,
                        target_id=ref["target_id"],
                        attempt_no=ref["attempt_no"],
                    )
                    return await _projected(deps, ref, CLAIM_LOST)
                return await _failed(deps, ref, "CancelledError")
            error = child.exception()
            if error is None:
                final = child.result()
                outcome = final.get("outcome") if isinstance(final, dict) else None
                return await _projected(deps, ref, None if outcome is None else str(outcome))
            if not isinstance(error, Exception) or (isinstance(error, FencedOut) and error.worker_superseded):
                raise error
            return await _failed(deps, ref, type(error).__name__)
        finally:
            await claims.stop(ref)

    return run_target
