"""The `claim` node of `score_graph` (spec §9.3 L3477-3491, §9.4.3; plan Task 11, Codex r1 #15, #21, #27,
r2 B5, r3 B8, B15, r4 B2, Controller carry T9-d): the atomic claim of a round's admissions.

**The transaction** (`claim_round`), in the lock schedule's order: `fence_epoch` (the claim stamps
`claim_epoch`), `active_config FOR SHARE` (`cycles.lock_active_scoring`) for the **active** execution config
and evaluation policy (L3491: the attempt pins them and uses them throughout), the cycle under its owner
predicate (`cycles.lock_cycle`, step 5; a lost cycle is `FencedOut(cycles.CYCLE_OWNER)`), then:
1. The atomic claim (L3477-3488) over the round's admissions, through `transition_where` `pending →
   in_progress` with the caller's `FOR UPDATE SKIP LOCKED` subquery (`CLAIM_WHERE`: still pending, due,
   `charged_attempts < MAX_SCORE_ATTEMPTS`, not purge-fenced), the admissions as the source relation `a`
   (`CLAIM_FROM`: ids, lanes and always-include reasons) and the claim columns (`CLAIM_SET_SQL`: a new
   `claim_id`, the epoch, the times, `attempt_no + 1`, the lane and the reason); it returns the claimed ids,
   claims, attempt numbers and resume drafts (`CLAIM_RETURNING`). `tests.support.scoring.claim_again` runs
   the same statement (Ruling P29).
2. One `score_attempts` row per claimed target: `mode = 'online'`, `outcome = 'running'`, the claim and its
   epoch, the cycle, the active execution config and evaluation policy, the admission's lane, and
   `resumed_from_attempt_id`: when the target has a draft to resume from under that policy
   (`targets.RESUMABLE_DRAFT_SQL`), the newest earlier attempt of the target under the policy, the link
   `load_context`'s resume chain follows (Controller carry T9-d); else `NULL`.
3. `ranking.bump` last, when anything was claimed.
After the commit: the fault point `score.claim.after_claim` (`AFTER_CLAIM`, C29), then `claims.start(ref)` for
every claim of the round (cycle-owned heartbeats from the first second, so a claim waiting for a fan-out slot
is never reclaimed while the cycle lives; Codex r2 B5). The state takes the round's claims (`round_claims`,
which the fan-out sends), appends them to `claimed` and counts the round (`claim_rounds`).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from langchain_core.runnables import RunnableConfig
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db import ranking
from jobhunter.db.sql import q
from jobhunter.db.transitions import transition_where
from jobhunter.errors import FencedOut
from jobhunter.faults import afault_point
from jobhunter.graphs.score.context import execution
from jobhunter.graphs.score.deps import ScoreDeps
from jobhunter.graphs.score.nodes import CycleNode, log
from jobhunter.graphs.score.state import ClaimRef, CycleState, CycleUpdate
from jobhunter.scoring import cycles
from jobhunter.scoring.admission import MAX_SCORE_ATTEMPTS, Lane, Picked, lane_of
from jobhunter.scoring.targets import RESUMABLE_DRAFT_SQL
from jobhunter.worker.epoch import fence_epoch

NAME: Final = "claim"
AFTER_CLAIM: Final = "score.claim.after_claim"

CLAIM_WHERE: Final = (
    "t.id = a.id AND t.id IN (SELECT id FROM score_targets WHERE id = ANY(CAST(:ids AS bigint[])) "
    "AND state = 'pending' AND (not_before IS NULL OR not_before <= now()) "
    "AND charged_attempts < :max_attempts AND NOT is_fenced(job_id, llm_tuple_id) FOR UPDATE SKIP LOCKED)"
)
CLAIM_FROM: Final = (
    "unnest(CAST(:ids AS bigint[]), CAST(:lanes AS text[]), CAST(:reasons AS text[])) AS a(id, lane, reason)"
)
CLAIM_SET_SQL: Final = {
    "claim_id": "gen_random_uuid()",
    "claim_epoch": ":epoch",
    "claimed_at": "now()",
    "claim_heartbeat_at": "now()",
    "attempt_no": "t.attempt_no + 1",
    "lane": "a.lane",
    "always_include_reason": "a.reason",
}
CLAIM_RETURNING: Final = ("id", "claim_id", "attempt_no", "resume_draft_id")

# The resumed attempt: the newest earlier attempt of the target under the policy, when it has a draft to
# resume from (`RESUMABLE_DRAFT_SQL`, correlated to `t`).
_RESUMED_FROM: Final = (
    f"CASE WHEN {RESUMABLE_DRAFT_SQL} THEN (SELECT p.id FROM score_attempts p "  # noqa: S608 (a constant)
    "WHERE p.target_id = c.id "
    "AND p.attempt_no < c.attempt_no AND p.evaluation_policy_id = :policy "
    "ORDER BY p.attempt_no DESC LIMIT 1) END"
)
_ATTEMPTS: Final = q(
    "INSERT INTO score_attempts (target_id, attempt_no, mode, outcome, claim_id, claim_epoch, "  # noqa: S608
    "cycle_id, execution_config_id, evaluation_policy_id, lane, resumed_from_attempt_id) "
    "SELECT c.id, c.attempt_no, 'online', 'running', c.claim_id, :epoch, :cycle, :execution, :policy, "
    f"c.lane, {_RESUMED_FROM} FROM unnest(CAST(:ids AS bigint[]), CAST(:numbers AS integer[]), "
    "CAST(:claims AS uuid[]), CAST(:lanes AS text[])) AS c(id, attempt_no, claim_id, lane) "
    "JOIN score_targets t ON t.id = c.id RETURNING id, target_id"
)


@dataclass(frozen=True)
class Claimed:
    """A claimed target (`id`), its claim, attempt number, lane and resume draft, and its new attempt."""

    id: int
    claim_id: str
    attempt_no: int
    lane: Lane
    resume_draft_id: int | None
    attempt_id: int

    @property
    def ref(self) -> ClaimRef:
        return {"target_id": self.id, "claim_id": self.claim_id, "attempt_no": self.attempt_no}


async def claim_round(
    conn: AsyncConnection,
    picked: Sequence[Picked],
    *,
    epoch: int,
    cycle_id: int,
    execution_config_id: int,
    evaluation_policy_id: int,
) -> list[Claimed]:
    """Steps 1 and 2 of the module docstring in the caller's transaction (which fenced its epoch and holds
    `active_config` and the cycle); the claims in admission order."""
    if not picked:
        return []
    rows = await transition_where(
        conn,
        "score_targets",
        ["pending"],
        "in_progress",
        where=CLAIM_WHERE,
        from_sql=CLAIM_FROM,
        params={
            "ids": [p.target_id for p in picked],
            "lanes": [p.lane for p in picked],
            "reasons": [p.always_include_reason for p in picked],
            "epoch": epoch,
            "max_attempts": MAX_SCORE_ATTEMPTS,
        },
        set_sql=CLAIM_SET_SQL,
        returning=CLAIM_RETURNING,
    )
    if not rows:
        return []
    lanes = {p.target_id: p.lane for p in picked}
    order = {p.target_id: i for i, p in enumerate(picked)}
    claimed = sorted(rows, key=lambda row: order[int(row["id"])])
    params = {
        "ids": [int(row["id"]) for row in claimed],
        "numbers": [int(row["attempt_no"]) for row in claimed],
        "claims": [str(row["claim_id"]) for row in claimed],
        "lanes": [lanes[int(row["id"])] for row in claimed],
        "epoch": epoch,
        "cycle": cycle_id,
        "execution": execution_config_id,
        "policy": evaluation_policy_id,
    }
    attempts = {int(row.target_id): int(row.id) for row in await conn.execute(_ATTEMPTS, params)}
    return [
        Claimed(
            id=int(row["id"]),
            claim_id=str(row["claim_id"]),
            attempt_no=int(row["attempt_no"]),
            lane=lanes[int(row["id"])],
            resume_draft_id=None if row["resume_draft_id"] is None else int(row["resume_draft_id"]),
            attempt_id=attempts[int(row["id"])],
        )
        for row in claimed
    ]


def make_claim(deps: ScoreDeps) -> CycleNode:
    async def claim(state: CycleState, config: RunnableConfig) -> CycleUpdate:
        ctx = execution(config)
        picked = [
            Picked(a["target_id"], lane_of(a["lane"]), a["always_include_reason"])
            for a in state["current_round"]
        ]
        async with deps.engine.begin() as conn:
            await fence_epoch(conn, ctx.worker_epoch)  # step 1: the claim stamps `claim_epoch`
            active = await cycles.lock_active_scoring(conn)  # step 2
            if await cycles.lock_cycle(conn, ctx.cycle_id, epoch=ctx.worker_epoch) is None:  # step 5
                raise FencedOut(cycles.CYCLE_OWNER)
            claimed = await claim_round(
                conn,
                picked,
                epoch=ctx.worker_epoch,
                cycle_id=ctx.cycle_id,
                execution_config_id=active.execution_config_id,
                evaluation_policy_id=active.evaluation_policy_id,
            )
            if claimed:
                await ranking.bump(conn)  # step 11, last
        await afault_point(AFTER_CLAIM)
        refs = [c.ref for c in claimed]
        for ref in refs:  # after the commit: every claim is beaten from its first second (Codex r2 B5)
            ctx.claims.start(ref)
        log.info(
            "score_cycle_claimed",
            cycle_id=ctx.cycle_id,
            round=state["claim_rounds"] + 1,
            admitted=len(picked),
            claimed=len(refs),
            resume_drafts=sum(1 for c in claimed if c.resume_draft_id is not None),
            target_ids=[c.id for c in claimed],
        )
        return {"claimed": refs, "round_claims": refs, "claim_rounds": state["claim_rounds"] + 1}

    return claim
