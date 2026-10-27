"""Bulk operations (spec §6.9.3, §7.11, §10.10, §17.4; plan Tasks 10 and 14, R5, Deviation 8, A1 issue 19,
Rulings P8, P9, P31, P52, Codex r1 #27, r2 B7, r4 B8): the bulk operations themselves, the follow-up holds of
their batched items and the quarantined batch spend.

**The operation** (§6.9.3). A tuple change opens one (`targets.activate_tuple`, Task 14), in the
activation's transaction, after cancelling the old tuple's active one (the `bulk_operations_one_active`
singleton holds at most one `active` row):
- `open(conn, *, llm_tuple_id, settings, now) -> int`: `ensure_month` (`lock_month` raises on an absent row,
  so a fresh UTC month gets its row first; Codex r4 B8), the month row `FOR UPDATE` (`lock_month`, step 6),
  then the row inserted `active` (step 7) with `budget_usd = bulk_budget(settings, p, row)`: the
  `bulk_rescore_max_budget_share` of the month's scoring headroom at the start, `A x (1 - reserve_frac) -
  scoring_counted_usd` with `A` of the row's own cap and buffer (the confirm's preview uses the same
  `dryrun.bulk_budget`), rounded half-up to $0.000001 and fixed for the operation's life;
- `cancel(conn, bulk_operation_id, *, now) -> bool`: `active -> cancelled` (the tuple is superseded); `False`
  for an operation that has ended already. Its unspent budget lapses; its holds go with it;
- `complete_if_done(conn, bulk_operation_id, *, now) -> bool` (R5, Deviation 8): under the bulk row's lock
  (`FOR NO KEY UPDATE`, step 7), `active -> completed` only when the `create_targets_for_tuple` operation
  whose payload names it is no longer `pending` or `running`, no target of it is `pending`, `in_progress` or
  `in_batch`, and no submission of it is in flight (the `batch_one_in_flight` states, unreleased). On
  completion `followup_held_usd := 0` in the same statement and every unreleased item of its submissions gets
  `hold_released_at := now` (other domain rows, after the bulk row). A `budget_deferred` or
  `deferred_duplicate` target does not hold it open (L1194, A1 issue 19): when it returns to `pending` later,
  its reservation is refused the finished operation's allocation and debits the bucket (Task 7);
- the heavy reconciler `bulk_operations` (§17.4, L5508): `fence_epoch`, `active_config FOR SHARE` (step 2,
  so it never completes an operation while an activation is under way), then `complete_if_done` of the
  active operation.

**The follow-up hold** (§6.9.3 L1188-1190). A submission adds `n * p_followup * ĉ_followup` to its operation's
`followup_held_usd`, each item holding its share (`batch_items.hold_usd`). An item gives its share back once:
when it needs no follow-up, or when its follow-up attempt ends. The one release is keyed by the item:
- `release_item_hold(conn, *, item_id)`: in the caller's transaction, the item's `bulk_operations` row (the
  operation of its submission) is locked (schedule step 7, before the item row, step 10), the item is stamped
  once, `UPDATE batch_items SET hold_released_at = now() WHERE id = :i AND hold_released_at IS NULL RETURNING
  hold_usd`, and the operation's `followup_held_usd := greatest(0, followup_held_usd - hold)`. It returns the
  amount released: 0 when the item held nothing or was released already (a second call changes nothing).
- `release_target_hold(conn, target_id)`: the same for the target's unreleased `followup` item (a target has
  at most one: a follow-up is never batched again, §10.10); 0 when it has none.
The bulk row's lock is `FOR NO KEY UPDATE`, the lock of step 7 every writer of the operation's accounts takes
(`spend_ledger`'s reservations and settlements of `spent_usd`, the holds here): the writers are serialised,
and the foreign-key checks of the rows that reference the operation (`FOR KEY SHARE`) are not blocked.

Every later releaser calls one of these two: `release_target_hold` the terminal moves of a target (`publish`
and `fail`, Task 10; the sweeps, cancellations, merges and deletions of Tasks 13, 14, 22 and 23),
`release_item_hold` the item decisions (Tasks 16 and 17).

**Moving targets not as their owner** (Ruling P52). A transaction that moves targets whose rows it locks as
other domain rows (step 10: a sweep, a cancellation, a merge, a deletion) and releases their holds first calls
`lock_for_targets(conn, candidate_ids)`, with its candidate ids read without a lock, **before its first
target-row lock**: the bulk rows of the candidates' unreleased follow-up items, `FOR NO KEY UPDATE` in id
order. So the bulk row (step 7) always precedes the target rows, and the later `release_target_hold` re-takes
a lock the transaction holds already. A candidate the move then skips keeps its hold. A target's owner (the
claim's `publish` and `fail`) holds its row at step 5 and needs no such pre-lock: it releases right after its
owner lock.

**The holds of a submission** (§6.9.3 L1188-1190, plan Task 16):
- `hold_per_item(conn, *, evaluation_policy_id, model_policy) -> Decimal`: one item's planning hold,
  `p_followup x ĉ_followup`, the evaluation policy's stored `projected_critic_rate` (§11.7.4, the row's own
  column) times `budget.c_hat_followup(model_policy)`, rounded half-up to $0.000001 (the precision of
  `batch_items.hold_usd`, so `n x hold` is exactly the sum of `n` items' holds);
- `add_hold(conn, bulk_operation_id, usd)` and `release_hold(conn, bulk_operation_id, usd) -> Decimal`: the
  operation's `followup_held_usd` up, or down by at most what it holds (never below 0), under the bulk row's
  lock (step 7); the release returns the amount given back;
- `fits(conn, *, bulk_operation_id, worst_case_usd, hold_usd) -> bool`: L1189 / §7.17 #7, `spent_usd +
  followup_held_usd + worst_case_usd + hold_usd ≤ budget_usd` of an `active` operation, read under the bulk
  row's lock (`lock_accounts` returns the row's `Accounts`, whose `fits` is the same inequality, for a
  caller that keeps the row).

**The quarantined batch spend** (§6.9.4 L1199, Ruling P31). `QUARANTINED_USD_SQL` is the one statement of
"the amount held by quarantined batch submissions": the reserved spend of the `submit_ambiguous` submissions,
which `llm.cap_reached` names (`fail`, Task 10) and Task 17's `batches.quarantined_usd` reuses.

Nothing here logs content: ids and amounts only.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.errors import FencedOut, TransitionConflict
from jobhunter.llm import budget, spend_ledger
from jobhunter.llm.budget import BudgetParams
from jobhunter.observability.logging import get_logger
from jobhunter.settings.dryrun import bulk_budget
from jobhunter.settings.model import ModelPolicy, Settings
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.reconcilers import ReconcileContext, reconciler

QUARANTINED_USD_SQL: Final = (
    "SELECT coalesce(sum(reserved_usd), 0) FROM batch_submissions WHERE state = 'submit_ambiguous'"
)

_ITEM_OPERATION: Final = q(
    "SELECT s.bulk_operation_id FROM batch_items i JOIN batch_submissions s ON s.id = i.submission_id "
    "WHERE i.id = :i"
)
_LOCK_OPERATION: Final = q("SELECT id FROM bulk_operations WHERE id = :b FOR NO KEY UPDATE")
_STAMP: Final = q(
    "UPDATE batch_items SET hold_released_at = now() WHERE id = :i AND hold_released_at IS NULL "
    "RETURNING hold_usd"
)
_GIVE_BACK: Final = q(
    "UPDATE bulk_operations SET followup_held_usd = greatest(0, followup_held_usd - :hold) WHERE id = :b"
)
# The unreleased follow-up items of a target, and of a set of targets (the funding rule of
# `spend_ledger.funded_bulk_operation_id`).
_TARGET_ITEMS: Final = q(
    "SELECT id FROM batch_items WHERE target_id = :t AND decision = 'followup' AND hold_released_at IS NULL "
    "ORDER BY id"
)
_LOCK_FOR_TARGETS: Final = q(
    "SELECT id FROM bulk_operations WHERE id IN (SELECT s.bulk_operation_id FROM batch_items i "
    "JOIN batch_submissions s ON s.id = i.submission_id WHERE i.target_id = ANY(CAST(:ids AS bigint[])) "
    "AND i.decision = 'followup' AND i.hold_released_at IS NULL) ORDER BY id FOR NO KEY UPDATE"
)
_ZERO: Final = Decimal(0)
_TABLE: Final = "bulk_operations"
_OPEN: Final = q(
    "INSERT INTO bulk_operations (kind, llm_tuple_id, state, budget_usd) "
    "VALUES ('tuple_rescore', :tuple, 'active', :budget) RETURNING id"
)
_ACTIVE_OPERATION: Final = q("SELECT id FROM bulk_operations WHERE state = 'active' ORDER BY id")
_LOCK_STATE: Final = q("SELECT state FROM bulk_operations WHERE id = :b FOR NO KEY UPDATE")
# What still holds operation `:b` open (module docstring): its creation, a target of it being scored, a
# submission of it in flight (the `batch_one_in_flight` predicate).
_HELD_OPEN: Final = q(
    "SELECT EXISTS (SELECT 1 FROM operations WHERE kind = 'create_targets_for_tuple' "
    "AND payload ->> 'bulk_operation_id' = CAST(:b AS text) AND status IN ('pending', 'running')) "
    "AS creating, "
    "EXISTS (SELECT 1 FROM score_targets WHERE bulk_operation_id = :b "
    "AND state IN ('pending', 'in_progress', 'in_batch')) AS scoring, "
    "EXISTS (SELECT 1 FROM batch_submissions WHERE bulk_operation_id = :b AND released_at IS NULL "
    "AND state IN ('preparing', 'submit_started', 'submitted', 'submit_ambiguous', 'ended')) AS batching"
)
_STAMP_ITEMS: Final = q(
    "UPDATE batch_items SET hold_released_at = :now WHERE hold_released_at IS NULL "
    "AND submission_id IN (SELECT id FROM batch_submissions WHERE bulk_operation_id = :b) RETURNING id"
)
_SHARE_ACTIVE_CONFIG: Final = q("SELECT 1 FROM active_config WHERE id = 1 FOR SHARE")

log = get_logger("jobhunter.llm.bulk")


async def open(conn: AsyncConnection, *, llm_tuple_id: int, settings: Settings, now: datetime) -> int:
    """Open the tuple's bulk operation with its fixed budget, under the month row's lock (module docstring);
    its id. The caller cancelled the active one first (the singleton refuses a second)."""
    p = BudgetParams.from_payload(settings.to_payload())
    month = budget.month_of(now)
    await spend_ledger.ensure_month(conn, month, p, now)
    row = await spend_ledger.lock_month(conn, month, p, now=now)  # step 6
    budget_usd = bulk_budget(settings, budget.row_params(row, p), row).quantize(
        budget.USD_QUANTUM, rounding=ROUND_HALF_UP
    )
    opened = int((await conn.execute(_OPEN, {"tuple": llm_tuple_id, "budget": budget_usd})).scalar_one())
    log.info(
        "bulk_operation_opened",
        bulk_operation_id=opened,
        llm_tuple_id=llm_tuple_id,
        month=month.isoformat(),
        budget_usd=str(budget_usd),
    )
    return opened


async def active_operation(conn: AsyncConnection) -> int | None:
    """The id of the `active` bulk operation (at most one), read without a lock."""
    found = (await conn.execute(_ACTIVE_OPERATION)).scalars().first()
    return None if found is None else int(found)


async def cancel(conn: AsyncConnection, bulk_operation_id: int, *, now: datetime) -> bool:
    """`active -> cancelled` (its tuple superseded); `False` when it had ended already."""
    try:
        await transition(conn, _TABLE, bulk_operation_id, ["active"], "cancelled")
    except TransitionConflict:
        return False
    log.info("bulk_operation_cancelled", bulk_operation_id=bulk_operation_id, at=now.isoformat())
    return True


async def complete_if_done(conn: AsyncConnection, bulk_operation_id: int, *, now: datetime) -> bool:
    """Complete the operation when nothing holds it open any more (module docstring), its holds zeroed and
    its unreleased items stamped `now`; whether it completed (`False` for one that has ended already)."""
    state = (await conn.execute(_LOCK_STATE, {"b": bulk_operation_id})).scalar_one_or_none()  # step 7
    if state != "active":
        return False
    held = (await conn.execute(_HELD_OPEN, {"b": bulk_operation_id})).one()
    if held.creating or held.scoring or held.batching:
        return False
    await transition(
        conn, _TABLE, bulk_operation_id, ["active"], "completed", set_={"followup_held_usd": _ZERO}
    )
    stamped = (await conn.execute(_STAMP_ITEMS, {"b": bulk_operation_id, "now": now})).all()
    log.info("bulk_operation_completed", bulk_operation_id=bulk_operation_id, items_released=len(stamped))
    return True


@reconciler("bulk_operations", "heavy")
async def reconcile_bulk_operations(ctx: ReconcileContext) -> int:
    """`complete_if_done` of the active bulk operation at `ctx.now` (module docstring); 1 when it completed.
    The worker's own `FencedOut` propagates (Ruling FR-w); another owner's ends the pass with 0."""
    try:
        async with ctx.engine.begin() as conn:
            await fence_epoch(conn, ctx.epoch)
            await conn.execute(_SHARE_ACTIVE_CONFIG)  # step 2
            active = await active_operation(conn)
            if active is None:
                return 0
            return int(await complete_if_done(conn, active, now=ctx.now))
    except FencedOut as fenced:
        if fenced.worker_superseded:
            raise
        return 0


async def release_item_hold(conn: AsyncConnection, *, item_id: int) -> Decimal:
    """Release item `item_id`'s follow-up hold once (module docstring), in the caller's transaction; the
    amount released, 0 when it held nothing or was released already. `LookupError` for an item that does not
    exist."""
    operation = (await conn.execute(_ITEM_OPERATION, {"i": item_id})).scalar_one_or_none()
    if operation is None:
        raise LookupError(f"batch item {item_id} does not exist")
    await conn.execute(_LOCK_OPERATION, {"b": operation})  # step 7, before the item row
    hold = (await conn.execute(_STAMP, {"i": item_id})).scalar_one_or_none()
    if hold is None:
        return _ZERO  # released already
    released = Decimal(hold)
    if released > 0:
        await conn.execute(_GIVE_BACK, {"hold": released, "b": operation})
    log.info("bulk_hold_released", item_id=item_id, bulk_operation_id=int(operation), hold_usd=str(released))
    return released


_ACCOUNTS: Final = q(
    "SELECT state, budget_usd, spent_usd, followup_held_usd FROM bulk_operations WHERE id = :b "
    "FOR NO KEY UPDATE"
)
_ADD_HOLD: Final = q("UPDATE bulk_operations SET followup_held_usd = followup_held_usd + :usd WHERE id = :b")
_RELEASE_HOLD: Final = q(
    "UPDATE bulk_operations SET followup_held_usd = followup_held_usd - least(followup_held_usd, :usd) "
    "WHERE id = :b RETURNING followup_held_usd"
)
_CRITIC_RATE: Final = q("SELECT projected_critic_rate FROM evaluation_policies WHERE id = :p")


async def hold_per_item(
    conn: AsyncConnection, *, evaluation_policy_id: int, model_policy: ModelPolicy
) -> Decimal:
    """One item's follow-up hold under the evaluation policy and the model policy (module docstring);
    `LookupError` for a policy that does not exist."""
    rate = (await conn.execute(_CRITIC_RATE, {"p": evaluation_policy_id})).scalar_one_or_none()
    if rate is None:
        raise LookupError(f"evaluation policy {evaluation_policy_id} does not exist")
    hold = Decimal(rate) * budget.c_hat_followup(model_policy)
    return hold.quantize(budget.USD_QUANTUM, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Accounts:
    """A bulk operation's allocation accounts, as read under its row's lock."""

    state: str
    budget_usd: Decimal
    spent_usd: Decimal
    followup_held_usd: Decimal

    def fits(self, *, worst_case_usd: Decimal, hold_usd: Decimal) -> bool:
        """L1189 / §7.17 #7: an `active` operation and `spent_usd + followup_held_usd + worst_case_usd +
        hold_usd ≤ budget_usd`."""
        used = self.spent_usd + self.followup_held_usd + worst_case_usd + hold_usd
        return self.state == "active" and used <= self.budget_usd


async def lock_accounts(conn: AsyncConnection, bulk_operation_id: int) -> Accounts:
    """The operation's accounts under its row's lock (`FOR NO KEY UPDATE`, step 7); `LookupError` for an
    operation that does not exist."""
    row = (await conn.execute(_ACCOUNTS, {"b": bulk_operation_id})).first()
    if row is None:
        raise LookupError(f"bulk operation {bulk_operation_id} does not exist")
    return Accounts(row.state, row.budget_usd, row.spent_usd, row.followup_held_usd)


async def add_hold(conn: AsyncConnection, bulk_operation_id: int, usd: Decimal) -> None:
    """`followup_held_usd += usd` under the bulk row's lock (module docstring); `ValueError` for a negative
    amount."""
    if usd < 0:
        raise ValueError("a hold is added with a non-negative amount (release_hold gives one back)")
    await lock_accounts(conn, bulk_operation_id)
    await conn.execute(_ADD_HOLD, {"usd": usd, "b": bulk_operation_id})


async def release_hold(conn: AsyncConnection, bulk_operation_id: int, usd: Decimal) -> Decimal:
    """`followup_held_usd -= usd`, never below 0, under the bulk row's lock (module docstring); the amount
    given back. `ValueError` for a negative amount."""
    if usd < 0:
        raise ValueError("a hold is released with a non-negative amount")
    before = (await lock_accounts(conn, bulk_operation_id)).followup_held_usd
    after = (await conn.execute(_RELEASE_HOLD, {"usd": usd, "b": bulk_operation_id})).scalar_one()
    return before - Decimal(after)


async def fits(
    conn: AsyncConnection, *, bulk_operation_id: int, worst_case_usd: Decimal, hold_usd: Decimal
) -> bool:
    """Whether a submission of these worst cases and holds fits the operation's allocation (module
    docstring), read under the bulk row's lock."""
    accounts = await lock_accounts(conn, bulk_operation_id)
    return accounts.fits(worst_case_usd=worst_case_usd, hold_usd=hold_usd)


async def release_target_hold(conn: AsyncConnection, target_id: int) -> Decimal:
    """Release the follow-up hold of target `target_id`'s unreleased `followup` item, once (module
    docstring); the amount released, 0 when it has none."""
    items = [int(i) for i in (await conn.execute(_TARGET_ITEMS, {"t": target_id})).scalars()]
    released = _ZERO
    for item_id in items:
        released += await release_item_hold(conn, item_id=item_id)
    return released


async def lock_for_targets(conn: AsyncConnection, target_ids: Sequence[int]) -> None:
    """Lock the bulk rows of the candidates' unreleased follow-up items, in id order, before the caller's
    first target-row lock (module docstring, Ruling P52)."""
    if target_ids:
        await conn.execute(_LOCK_FOR_TARGETS, {"ids": list(target_ids)})
