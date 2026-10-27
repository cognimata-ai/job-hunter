"""The `admit` node of `score_graph` (spec §9.3, §9.6; plan Tasks 11 and 12, A2 G3, G4, G13, Codex r1 #14,
Deviation 20): one claim round's admission.

In one transaction, in the lock schedule's order: `active_config FOR SHARE` (step 2), then the cycle under
its owner predicate (`cycles.lock_cycle`, `worker_epoch = :e AND status = 'running' FOR SHARE`, step 5; a
cycle that is no longer this owner's running one ends the graph with `FencedOut(cycles.CYCLE_OWNER)`, and the
owner writes nothing more), then the round's admission, the candidates read without a lock (the claim
re-checks them):
- the first round (`claim_rounds = 0`): `admission.admit` with the cycle's sweeps (`admission.CYCLE_SWEEPS`,
  Task 13's): the sweeps, the lane allocation over the persistent lane credits (step 10), and, when a sweep
  moved a target, `ranking_state` last;
- a later round, while fewer than `MAX_CLAIM_ROUNDS` rounds were claimed: `admission.admit_urgent`, the
  pending Score now targets (outside `S`) and the batch follow-ups up to the slots of `S` the earlier rounds
  left (`S` minus the cycle's `admitted` outside lane `score_now`), the "next claim round" of L3499 that
  serves a Score now or a batch follow-up arriving while the cycle runs; after `MAX_CLAIM_ROUNDS` rounds,
  nothing.
Both add the round's counts per lane to `score_cycles.admitted` under the cycle's owner predicate
(`admission.record_admitted`), and the state's `current_round` becomes the round's admissions in admission
order, as plain `AdmittedRef`s (a checkpoint holds no dataclass).
"""

from collections import Counter
from typing import Final

from langchain_core.runnables import RunnableConfig

from jobhunter.errors import FencedOut
from jobhunter.graphs.score.context import execution
from jobhunter.graphs.score.deps import ScoreDeps
from jobhunter.graphs.score.nodes import CycleNode, log
from jobhunter.graphs.score.state import AdmittedRef, CycleState, CycleUpdate
from jobhunter.scoring import admission, cycles

NAME: Final = "admit"
MAX_CLAIM_ROUNDS: Final = 5  # A2 G3/G4: the first round and up to four urgent ones
_OUTSIDE_S: Final = "score_now"


def make_admit(deps: ScoreDeps) -> CycleNode:
    async def admit(state: CycleState, config: RunnableConfig) -> CycleUpdate:
        ctx = execution(config)
        rounds = state["claim_rounds"]
        now = deps.clock()
        async with deps.engine.begin() as conn:
            await cycles.lock_active_scoring(conn)  # step 2
            cycle = await cycles.lock_cycle(conn, ctx.cycle_id, epoch=ctx.worker_epoch)  # step 5
            if cycle is None:
                raise FencedOut(cycles.CYCLE_OWNER)
            if rounds == 0:
                picked = await admission.admit(conn, cycle, now=now, sweeps=admission.CYCLE_SWEEPS)
            elif rounds >= MAX_CLAIM_ROUNDS:
                picked = []
            else:
                used = sum(n for lane, n in cycle.admitted.items() if lane != _OUTSIDE_S)
                left = max(0, cycle.admission_size - used)
                picked = await admission.admit_urgent(conn, cycle, now=now, slots_left=left)
        counts: Counter[str] = Counter(p.lane for p in picked)
        log.info(
            "score_cycle_admitted",
            cycle_id=ctx.cycle_id,
            round=rounds + 1,
            admitted=dict(counts),
            target_ids=[p.target_id for p in picked],
        )
        current: list[AdmittedRef] = [
            {"target_id": p.target_id, "lane": p.lane, "always_include_reason": p.always_include_reason}
            for p in picked
        ]
        return {"current_round": current}

    return admit
