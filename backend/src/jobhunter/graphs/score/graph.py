"""`score_graph`, the score cycle (spec §9.1, §9.3; plan Task 11, Codex r1 #12-#14, r2 B5, B6, B25, A2 G3,
G4, Deviation 20): one invocation per cycle, on the thread `cycle-<cycle_id>`, never resumed.

**The graphs** (`build_score_graphs(deps)`, once per process, `WorkerRuntime.score_graphs`): the
`score_target` subgraph (`subgraph.build_score_target`, compiled with `checkpointer=None`, so it checkpoints
in its parent's thread) and the cycle graph on the worker's saver (`deps.saver`), whose `score_target` node is
the wrapper `nodes.run_target.make_run_target(deps, subgraph)`, never the compiled subgraph registered
directly (Codex r2 B25).

**The edges**: `START → admit →(more) claim →(fan_out) Send("score_target", ref) per claim of the round →
admit`, and `finalize_cycle → END`, where
- `fan_out` sends one `score_target` per claim of the round (`round_claims`) and goes to `finalize_cycle`
  when the round claimed nothing;
- `score_target → admit` is the join: the next claim round's `admit` runs once, after every claim of the
  round ended (LangGraph runs a conditional edge out of `score_target` once per task, on that task's own
  writes, so a decision made there could differ between the tasks of a round and start `admit` and
  `finalize_cycle` together; deciding after the join cannot);
- `more` goes to `claim` while the round admitted targets, and to `finalize_cycle` when it admitted none: a
  later round admits the pending targets of lanes `score_now` and `batch_followup` (`admission.admit_urgent`)
  while fewer than `MAX_CLAIM_ROUNDS` rounds were claimed (A2 G3/G4, the "next claim round" of L3499; Codex r1
  #14: a later round re-admits, it never re-claims the first round's list).

**The policies.** `admit`, `claim` and `finalize_cycle` make no LLM call: each takes `DB_RETRY` and its
`TimeoutPolicy` (`CYCLE_NODE_TIMEOUTS`: `admit` 120 s, for Task 12's sweeps; `claim` and `finalize_cycle`
60 s). The `score_target` wrapper takes neither: a retry would re-run the attempt under its live claim
(Controller carry T9-b) and its subgraph's nodes bound it with their own timeouts. Every node is wrapped by
`sanitized` with the subgraph's `PASS_THROUGH` (an unclassified error's text never reaches a checkpoint).

**The invocation** (`invoke_cycle(graph, ctx)`): `ainvoke(initial_cycle_state(ctx), config=invoke_config(ctx),
durability="sync")` with `{"configurable": {"thread_id": "cycle-<id>", "cycle": ctx}, "recursion_limit": 200,
"max_concurrency": 3}` (L3374, L3469); every claim beat of the cycle is stopped when it ends, however it ends
(`ctx.claims.stop_all()`).

**The cycle task** (`execute_cycle`, the body of `tasks.score_cycle`): nothing while admission is stopped
(`score_cycle_not_started`, reason `admission_stopped`); `cycles.start_cycle` in its first transaction
(`None`: the job ends); then the graph through `run_owned(("cycle", id), token=epoch, …)` with the cycle's
heartbeat (`UPDATE score_cycles SET heartbeat_at = now() WHERE id = :c AND worker_epoch = :e AND status =
'running'` every `CYCLE_HEARTBEAT_S`) and `ClaimHeartbeats` for its claims. A refused beat or `FencedOut`
(`lost`) cancels the graph and writes nothing more (its claims are the reconciler's); an unclassified error
(`raised`: a node's `NodeFailed` or timeout, an `OutcomeConflict` of `results`) fails the cycle
`failed('bug')` with the critical alert `score_cycle.failed` and reclaims its claims (`cycles.fail_cycle`). A
delivery never resumes a cycle: each creates a fresh one or starts nothing, so a crashed cycle's thread is
never invoked again.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType
from typing import Final, Literal, cast

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send, TimeoutPolicy

from jobhunter.db.sql import q
from jobhunter.graphs.sanitize import DB_RETRY
from jobhunter.graphs.sanitize import sanitized as shared_sanitized
from jobhunter.graphs.score.claims import ClaimHeartbeats
from jobhunter.graphs.score.context import ClaimTracker, ScoreContext
from jobhunter.graphs.score.deps import ScoreDeps
from jobhunter.graphs.score.nodes import CycleNode, CycleStateNode, log
from jobhunter.graphs.score.nodes.admit import MAX_CLAIM_ROUNDS as MAX_CLAIM_ROUNDS
from jobhunter.graphs.score.nodes.admit import make_admit
from jobhunter.graphs.score.nodes.claim import make_claim
from jobhunter.graphs.score.nodes.finalize_cycle import make_finalize_cycle
from jobhunter.graphs.score.nodes.run_target import make_run_target
from jobhunter.graphs.score.state import ClaimRef, CycleState
from jobhunter.graphs.score.subgraph import PASS_THROUGH, ScoreTargetGraph, build_score_target
from jobhunter.scoring import cycles
from jobhunter.scoring.cycles import CycleTrigger
from jobhunter.worker.executions import EXECUTIONS, AlreadyExecuting, ExecutionRegistry, run_owned

type ScoreCycleGraph = CompiledStateGraph[CycleState, None, CycleState, CycleState]

RECURSION_LIMIT: Final = 200
MAX_CONCURRENCY: Final = 3  # L3469: three attempts in flight
CYCLE_HEARTBEAT_S: Final = 30.0  # §3.3.4
CYCLE_NODE_TIMEOUTS: Final[Mapping[str, timedelta]] = MappingProxyType(
    {
        "admit": timedelta(seconds=120),
        "claim": timedelta(seconds=60),
        "finalize_cycle": timedelta(seconds=60),
    }
)

_BEAT: Final = q(
    "UPDATE score_cycles SET heartbeat_at = now() WHERE id = :c AND worker_epoch = :e AND status = 'running'"
)
_CYCLE_IDS: Final = q(
    "SELECT worker_epoch, llm_tuple_id, scope_version_id, evaluation_policy_id FROM score_cycles "
    "WHERE id = :c"
)


@dataclass(frozen=True)
class ScoreGraphs:
    """The process's compiled score graphs: the cycle graph and the subgraph its wrapper invokes."""

    cycle: ScoreCycleGraph
    target: ScoreTargetGraph


def thread_id(cycle_id: int) -> str:
    """The cycle's checkpoint thread (§9.3)."""
    return f"cycle-{cycle_id}"


def invoke_config(ctx: ScoreContext) -> RunnableConfig:
    """The invoke config of the cycle `ctx` (module docstring)."""
    return {
        "configurable": {"thread_id": thread_id(ctx.cycle_id), "cycle": ctx},
        "recursion_limit": RECURSION_LIMIT,
        "max_concurrency": MAX_CONCURRENCY,
    }


def initial_cycle_state(ctx: ScoreContext) -> CycleState:
    """The state a cycle starts from: its ids, no claim, no result, no round yet."""
    return {
        "cycle_id": ctx.cycle_id,
        "worker_epoch": ctx.worker_epoch,
        "llm_tuple_id": ctx.llm_tuple_id,
        "scope_version_id": ctx.scope_version_id,
        "evaluation_policy_id": ctx.evaluation_policy_id,
        "claimed": [],
        "results": {},
        "claim_rounds": 0,
        "current_round": [],
        "round_claims": [],
    }


def fan_out(state: CycleState) -> list[Send] | Literal["finalize_cycle"]:
    """One `score_target` per claim of the round, or `finalize_cycle` when it claimed none."""
    if not state["round_claims"]:
        return "finalize_cycle"
    return [Send("score_target", ref) for ref in state["round_claims"]]


def more(state: CycleState) -> Literal["claim", "finalize_cycle"]:
    """`claim` while the round admitted targets, else `finalize_cycle` (module docstring)."""
    return "claim" if state["current_round"] else "finalize_cycle"


def sanitized[S](name: str, node: CycleStateNode[S]) -> CycleStateNode[S]:
    """`node` whose unclassified errors become `NodeFailed(name, <class>)` (the subgraph's `PASS_THROUGH`)."""
    return shared_sanitized(name, node, passing=PASS_THROUGH, log=log, event="score_cycle_node_failed")


def _add_node(graph: StateGraph[CycleState], name: str, node: CycleNode) -> None:
    graph.add_node(
        name,
        sanitized(name, node),
        retry_policy=DB_RETRY,
        timeout=TimeoutPolicy(run_timeout=CYCLE_NODE_TIMEOUTS[name]),
    )


def build_score_graph(deps: ScoreDeps, subgraph: ScoreTargetGraph) -> ScoreCycleGraph:
    """The cycle graph over the process-wide `deps` and the compiled `subgraph`, on `deps.saver`."""
    graph = StateGraph(CycleState)
    _add_node(graph, "admit", make_admit(deps))
    _add_node(graph, "claim", make_claim(deps))
    graph.add_node(
        "score_target", sanitized("score_target", make_run_target(deps, subgraph)), input_schema=ClaimRef
    )
    _add_node(graph, "finalize_cycle", make_finalize_cycle(deps))
    graph.add_edge(START, "admit")
    graph.add_conditional_edges("admit", more, ["claim", "finalize_cycle"])
    graph.add_conditional_edges("claim", fan_out, ["score_target", "finalize_cycle"])
    graph.add_edge("score_target", "admit")
    graph.add_edge("finalize_cycle", END)
    return graph.compile(checkpointer=deps.saver)


def build_score_graphs(deps: ScoreDeps) -> ScoreGraphs:
    """The subgraph, compiled once, and the cycle graph that invokes it (module docstring)."""
    target = build_score_target(deps)
    return ScoreGraphs(cycle=build_score_graph(deps, target), target=target)


async def cycle_context(deps: ScoreDeps, cycle_id: int, *, claims: ClaimTracker) -> ScoreContext:
    """The `ScoreContext` of the started cycle `cycle_id`, with `claims` as its claim heartbeats."""
    async with deps.engine.connect() as conn:
        row = (await conn.execute(_CYCLE_IDS, {"c": cycle_id})).one()
    return ScoreContext(
        cycle_id=cycle_id,
        worker_epoch=int(row.worker_epoch),
        llm_tuple_id=int(row.llm_tuple_id),
        scope_version_id=int(row.scope_version_id),
        evaluation_policy_id=int(row.evaluation_policy_id),
        deps=deps,
        claims=claims,
    )


async def invoke_cycle(graph: ScoreCycleGraph, ctx: ScoreContext) -> CycleState:
    """The cycle's one invocation (module docstring); its final state. Every claim beat is stopped at the
    end, also when the invocation is cancelled or raises."""
    try:
        final = await graph.ainvoke(initial_cycle_state(ctx), config=invoke_config(ctx), durability="sync")
    finally:
        await ctx.claims.stop_all()
    return cast(CycleState, final)


async def _work(graph: ScoreCycleGraph, ctx: ScoreContext) -> None:
    await invoke_cycle(graph, ctx)


async def execute_cycle(
    graphs: ScoreGraphs,
    deps: ScoreDeps,
    *,
    trigger: CycleTrigger,
    epoch: int,
    fake_llm: bool,
    admission_stopped: Callable[[], bool],
    heartbeat_period_s: float = CYCLE_HEARTBEAT_S,
    claims_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    registry: ExecutionRegistry = EXECUTIONS,
) -> int | None:
    """One cycle of `trigger` as worker `epoch` (module docstring): its id, or `None` when none started.
    `FencedOut` from its first transaction when `epoch` is not the current worker epoch."""
    if admission_stopped():
        log.info("score_cycle_not_started", trigger=trigger, reason="admission_stopped")
        return None
    engine = deps.engine
    async with engine.begin() as conn:
        cycle_id = await cycles.start_cycle(
            conn, trigger=trigger, now=deps.clock(), epoch=epoch, fake_llm=fake_llm
        )
    if cycle_id is None:
        return None
    started = cycle_id
    claims = ClaimHeartbeats(engine, monotonic=deps.monotonic, sleep=claims_sleep)
    ctx = await cycle_context(deps, started, claims=claims)

    async def beat() -> bool:
        async with engine.begin() as conn:
            return (await conn.execute(_BEAT, {"c": started, "e": epoch})).rowcount == 1

    try:
        outcome = await run_owned(
            ("cycle", started),
            epoch,
            _work(graphs.cycle, ctx),
            beat=beat,
            period_s=heartbeat_period_s,
            registry=registry,
        )
    except AlreadyExecuting:  # never for a fresh cycle id; the row is left to `orphaned_cycles` then
        log.warning("score_cycle_already_executing", cycle_id=started)
        return started
    if outcome.status == "lost":
        log.warning("score_cycle_lost", cycle_id=started)
    elif outcome.status == "raised":
        error_class = type(outcome.error).__name__
        async with engine.begin() as conn:
            failed = await cycles.fail_cycle(conn, started, epoch=epoch, error_class=error_class)
        if not failed:
            log.warning("score_cycle_failed_after_its_end", cycle_id=started, error_class=error_class)
    return started
