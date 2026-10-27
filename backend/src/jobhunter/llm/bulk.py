"""Bulk operations (spec §6.9.3, §7.11, §10.10; plan Task 10, Rulings P8, P31, P52, Codex r1 #27, r2 B7): the
follow-up holds of a bulk operation's batched items and the quarantined batch spend. Task 14 adds the bulk
operations themselves (`open`, the completion) here.

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

**The quarantined batch spend** (§6.9.4 L1199, Ruling P31). `QUARANTINED_USD_SQL` is the one statement of
"the amount held by quarantined batch submissions": the reserved spend of the `submit_ambiguous` submissions,
which `llm.cap_reached` names (`fail`, Task 10) and Task 17's `batches.quarantined_usd` reuses.

Nothing here logs content: ids and amounts only.
"""

from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.observability.logging import get_logger

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

log = get_logger("jobhunter.llm.bulk")


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
