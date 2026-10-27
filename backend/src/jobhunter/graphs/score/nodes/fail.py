"""The `fail` node (spec §9.4.1, §7.16.3 L2865-2908, §6.9.4; plan Task 10, A2 T12, T15, Codex r1 #27,
Deviation 17): the failed step of an attempt, mapped by `llm_step.outcome_for` to its target and attempt, in
one transaction.

The state's `error_code` (an enum the routing set) gives the `StepOutcome`; a refusal's category is read from
the attempt's newest refused call (`llm_calls.refusal_category`, provider metadata stored storable: the state
holds the code alone).

**A lost claim** (`claim_lost`) writes nothing: the attempt row is ended by whoever changed the predicate (a
reclaim writes `claim_lost`, a cancellation `cancelled`; A2 T12, Deviation 17), and `fail` only logs
`score_claim_lost`. So does a claim found lost under the owner lock.

**Every other outcome**, in the lock schedule's order: `active_config FOR SHARE`
(`spend_ledger.current_params`) for a denied reservation only, whose month row it needs; the target `FOR
UPDATE` under the claim predicate (step 5); for `budget_exhausted`, under the month lock,
`spend_ledger.raise_cap_reached` with the reserved spend of the quarantined batch submissions
(`bulk.QUARANTINED_USD_SQL`, Ruling P31) after `ensure_month` of `now`'s month (step 6; A2 T15: owner →
month); for a terminal outcome (`refused`, `failed_*`) the target's follow-up hold
(`bulk.release_target_hold`, the bulk row, step 7; Codex r1 #27: a backoff and a `budget_deferred` keep it,
since the draft will be resumed); then the target and the attempt (step 10) and the ranking bump last. The
target goes from `in_progress` (its claim columns cleared by the transition, its `claimed_at` kept) to:
- `budget_deferred('budget_exhausted')`;
- `refused(category)` with `refusal_category`;
- `failed_*` with its `state_reason`;
- for a transient error, `pending` with `not_before = now + backoff(transient_failures)` and
  `transient_failures + 1` while `charged_attempts < MAX_SCORE_ATTEMPTS` (L2863, L2908: `min(6 h, 15 min *
  2^(failures - 1))` of the count after this failure), else `failed_transient`.
The attempt goes `running → <outcome>` with its `outcome_detail`. The state's `outcome` is the attempt's.

`MAX_SCORE_ATTEMPTS` is `scoring.admission`'s, the one definition (Ruling P12, Controller carry T10-a).
"""

from datetime import timedelta
from typing import Final

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db import ranking
from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.errors import InvariantViolation
from jobhunter.graphs.score.deps import ScoreDeps
from jobhunter.graphs.score.llm_step import BUDGET_EXHAUSTED, StepOutcome, outcome_for
from jobhunter.graphs.score.nodes import Node, log
from jobhunter.graphs.score.state import TargetState
from jobhunter.llm import bulk, spend_ledger
from jobhunter.llm.budget import month_of
from jobhunter.llm.gateway import REFUSED
from jobhunter.scoring import routing
from jobhunter.scoring.admission import MAX_SCORE_ATTEMPTS
from jobhunter.scoring.routing import CLAIM_LOST, Route

NAME: Final = "fail"
BACKOFF_FIRST: Final = timedelta(minutes=15)
BACKOFF_CEILING: Final = timedelta(hours=6)
_CEILING_FROM: Final = 5  # 15 min * 2^5 is above the ceiling
_KEPT: Final = frozenset({"pending", "budget_deferred"})  # the target keeps its follow-up hold

_REFUSAL: Final = q(
    "SELECT refusal_category FROM llm_calls WHERE attempt_id = :a AND error_code = :refused "
    "ORDER BY id DESC LIMIT 1"
)
_LOCK_OWNER: Final = q(
    "SELECT charged_attempts, transient_failures FROM score_targets WHERE id = :t AND claim_id = :c "
    "AND attempt_no = :a AND state = 'in_progress' FOR UPDATE"
)
_QUARANTINED: Final = q(bulk.QUARANTINED_USD_SQL)


def backoff(transient_failures: int) -> timedelta:
    """The wait before a target that failed transiently `transient_failures` times before this failure is due
    again: 15 min, 30 min, 1 h, 2 h, 4 h, then 6 h (module docstring)."""
    if transient_failures >= _CEILING_FROM:
        return BACKOFF_CEILING
    return min(BACKOFF_CEILING, BACKOFF_FIRST * (1 << transient_failures))


async def _refusal_category(conn: AsyncConnection, attempt_id: int) -> str | None:
    found = await conn.execute(_REFUSAL, {"a": attempt_id, "refused": REFUSED})
    category = found.scalar_one_or_none()
    return None if category is None else str(category)


def _lost(state: TargetState, code: str) -> Command[Route]:
    log.info(
        "score_claim_lost",
        node=NAME,
        target_id=state["target_id"],
        attempt_id=state["attempt_id"],
        error_code=code,
    )
    return Command(goto=routing.END, update={"outcome": CLAIM_LOST})


def make_fail(deps: ScoreDeps) -> Node:
    async def fail(state: TargetState, config: RunnableConfig) -> Command[Route]:
        code = state["error_code"]
        target_id, attempt_id = state["target_id"], state["attempt_id"]
        if code is None:
            raise InvariantViolation(f"score target {target_id}: fail without an error code")
        category = None
        if code == REFUSED:
            async with deps.engine.connect() as conn:
                category = await _refusal_category(conn, attempt_id)
        outcome: StepOutcome = outcome_for(code, refusal_category=category)
        to_state = outcome.target_state
        if to_state is None:
            return _lost(state, code)
        now = deps.clock()
        claim = {"t": target_id, "c": state["claim_id"], "a": state["attempt_no"]}
        released = None
        async with deps.engine.begin() as conn:
            denied = outcome.error_code == BUDGET_EXHAUSTED
            params = await spend_ledger.current_params(conn) if denied else None  # step 2, the month's
            owner = (await conn.execute(_LOCK_OWNER, claim)).first()  # step 5
            if owner is None:
                lost = True
            else:
                lost = False
                reason: str | None = outcome.reason
                set_: dict[str, object] = {}
                if outcome.backoff:
                    if int(owner.charged_attempts) < MAX_SCORE_ATTEMPTS:
                        failures = int(owner.transient_failures)
                        to_state, reason = "pending", None
                        set_ = {"not_before": now + backoff(failures), "transient_failures": failures + 1}
                    else:
                        to_state = "failed_transient"
                elif to_state == REFUSED:
                    set_ = {"refusal_category": outcome.reason}
                if params is not None:  # step 6
                    month = month_of(now)
                    await spend_ledger.ensure_month(conn, month, params, now)
                    quarantined = (await conn.execute(_QUARANTINED)).scalar_one()
                    await spend_ledger.raise_cap_reached(conn, month, quarantined_usd=quarantined, now=now)
                if to_state not in _KEPT:  # step 7, before the target and the attempt rows
                    released = await bulk.release_target_hold(conn, target_id)
                await transition(
                    conn,
                    "score_targets",
                    target_id,
                    ["in_progress"],
                    to_state,
                    reason=reason,
                    owner={"claim_id": state["claim_id"], "attempt_no": state["attempt_no"]},
                    set_=set_,
                )
                await transition(
                    conn,
                    "score_attempts",
                    attempt_id,
                    ["running"],
                    outcome.attempt_outcome,
                    reason=outcome.reason,
                )
                await ranking.bump(conn)
        if lost:
            return _lost(state, code)
        log.info(
            "score_attempt_failed",
            target_id=target_id,
            attempt_id=attempt_id,
            error_code=code,
            attempt_outcome=outcome.attempt_outcome,
            target_state=to_state,
            hold_released_usd=None if released is None else str(released),
        )
        return Command(goto=routing.END, update={"outcome": outcome.attempt_outcome})

    return fail
