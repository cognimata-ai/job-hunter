"""The `finalize_cycle` node of `score_graph` (spec §9.3, §17.6 L5596; plan Task 11, Ruling P32): the cycle's
report and its completion.

In one transaction: the cycle under its owner predicate (`cycles.lock_cycle`, step 5; a lost cycle is
`FencedOut(cycles.CYCLE_OWNER)`), its attempts' spend and the cache figures of their calls, then
`transition(score_cycles, running → completed, owner={worker_epoch})` with the report and `spent_usd`,
`score_cycle.orphaned` resolved (a completed cycle ends the orphan alert of an earlier one, L5596), and
`scoring.critic_rate_high` evaluated for the cycle's tuple and evaluation policy at the deps' clock
(`cycles.evaluate_critic_rate`, L5595: opened above 1.5 times the projected critic rate, resolved below it;
M3 Task 18, Ruling P32).

**The report** (ids and counts only): `outcomes` (attempt outcome → count) and `lanes` (lane → count) over the
state's `results`, `spent_usd` (Σ `score_attempts.cost_usd` of the cycle's attempts, as a decimal string),
`cache` (the prompt cache over the calls of the cycle's attempts: `reads` and `writes` in input tokens and
`hit_ratio = reads / (uncached input + reads + writes)`, the ratio `CacheMonitor` gauges per role, taken over
this cycle's calls alone), `c_hat` (the cycle's `ĉ`), `admission_size` (`S`) and `claim_rounds`.

It never defers another cycle: the tick decides (L3470).
"""

from collections import Counter
from decimal import Decimal
from typing import Final

from langchain_core.runnables import RunnableConfig

from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.errors import FencedOut
from jobhunter.graphs.score.context import execution
from jobhunter.graphs.score.deps import ScoreDeps
from jobhunter.graphs.score.nodes import CycleNode, log
from jobhunter.graphs.score.state import CycleState, CycleUpdate
from jobhunter.observability.alerts import resolve_alerts
from jobhunter.scoring import cycles
from jobhunter.types import JSONValue

NAME: Final = "finalize_cycle"
_RATIO_DIGITS: Final = 4

_SPENT: Final = q("SELECT coalesce(sum(cost_usd), 0) FROM score_attempts WHERE cycle_id = :c")
_CACHE: Final = q(
    "SELECT coalesce(sum(c.cache_read_input_tokens), 0) AS reads, "
    "coalesce(sum(c.cache_creation_input_tokens), 0) AS writes, coalesce(sum(c.input_tokens), 0) AS uncached "
    "FROM llm_calls c JOIN score_attempts a ON a.id = c.attempt_id WHERE a.cycle_id = :c"
)


def make_finalize_cycle(deps: ScoreDeps) -> CycleNode:
    async def finalize_cycle(state: CycleState, config: RunnableConfig) -> CycleUpdate:
        ctx = execution(config)
        results = state["results"].values()
        outcomes = Counter(result["outcome"] for result in results)
        lanes = Counter(result["lane"] for result in results if result["lane"] is not None)
        async with deps.engine.begin() as conn:
            cycle = await cycles.lock_cycle(conn, ctx.cycle_id, epoch=ctx.worker_epoch)  # step 5
            if cycle is None:
                raise FencedOut(cycles.CYCLE_OWNER)
            spent = Decimal((await conn.execute(_SPENT, {"c": cycle.id})).scalar_one())
            cache = (await conn.execute(_CACHE, {"c": cycle.id})).one()
            reads, writes, uncached = int(cache.reads), int(cache.writes), int(cache.uncached)
            seen = reads + writes + uncached
            report: dict[str, JSONValue] = {
                "outcomes": dict(sorted(outcomes.items())),
                "lanes": dict(sorted(lanes.items())),
                "spent_usd": str(spent),
                "cache": {
                    "reads": reads,
                    "writes": writes,
                    "hit_ratio": round(reads / seen, _RATIO_DIGITS) if seen else 0.0,
                },
                "c_hat": str(cycle.c_hat_usd),
                "admission_size": cycle.admission_size,
                "claim_rounds": state["claim_rounds"],
            }
            await transition(
                conn,
                "score_cycles",
                cycle.id,
                ["running"],
                "completed",
                owner={"worker_epoch": ctx.worker_epoch},
                set_={"report": report, "spent_usd": spent},
            )
            resolved = await resolve_alerts(conn, code=cycles.ORPHANED_ALERT)
            await cycles.evaluate_critic_rate(
                conn,
                llm_tuple_id=cycle.llm_tuple_id,
                evaluation_policy_id=cycle.evaluation_policy_id,
                now=deps.clock(),
            )
        log.info(
            "score_cycle_completed",
            cycle_id=cycle.id,
            outcomes=dict(outcomes),
            spent_usd=str(spent),
            claim_rounds=state["claim_rounds"],
            orphan_alerts_resolved=resolved,
        )
        return {}

    return finalize_cycle
