"""The `replay_raw_items` operation (spec §7.16.1, §14.2; plan M4 Task 12, Deviation 30, M1 Carry-forward 3
and 10): the explicit replay `rejected -> pending` of rejected raw items, and the resolution of the items it
moved, whose run is over.

**The request** (`POST /api/system/raw-items/replay`, `api.routes.system`): `items_of_response` expands a
stored response into its `rejected` items (`LookupError` for a response that does not exist);
`replayable_items` keeps those of the listed ids that can be replayed at the request's time: `rejected` now,
their response's body not purged and fetched after `now - RAW_BODY_TTL`, at most `MAX_ITEMS`, lowest ids
first; `request_replay` records the operation, payload `{"raw_item_ids": [...]}` (the ids deduplicated and
sorted, 1 to `MAX_ITEMS`), dedup key `replay_raw_items:` + the hex SHA-256 of those ids joined by commas
(`dedup_key`), so the same set reuses the live operation of that set through `operations_live_dedup`.

**The handler** (`run_replay`, `@register_operation_handler("replay_raw_items")`):

1. One transaction: the claim's worker epoch read (no lock; `FencedOut("operation")` without the claim),
   `fence_epoch` with it (step 1 of the lock schedule), the owner row (`lock_operation_owner`), then the
   global purge fence: while one is active the operation is requeued (`requeue_in`, error code
   `purge_in_progress`, `not_before = now + PURGE_RETRY_AFTER`) and nothing else is written (§20.2: a replay
   checks the fence itself, `items.replay_rejected` has no owner of its own). Otherwise
   `items.replay_rejected` over the payload's ids with the planning-time adapters (`spec_adapters`, which
   parse and never send), then the progress, last. The fault point `replay.after_replay_commit`
   (`AFTER_REPLAY_COMMIT`) follows the commit.
2. Every payload id that is `pending` now is resolved in a transaction of its own by
   `identity.resolve_item(owner=claim)` (`ledger.lock_item_owner`: the operation row, then the global purge
   fence), oldest id first, each followed by its progress in a short transaction of its own (the ranking
   generation stays the resolution's last statement). A fence that became active meanwhile requeues the
   operation as in step 1; an item that no longer exists (retention deleted it) is skipped. The last
   transaction writes the final progress and ends the operation `succeeded`.

A crashed execution (a reclaim or the takeover requeues it) runs again from step 1: `replay_rejected` moves
nothing it moved before (those items are no longer `rejected`), and step 2 resumes on the ids still
`pending`. The embedding, the hard filters and the targets of the new jobs follow through their reconcilers
(`awaiting_embedding`, `hard_filter_completeness`, `missing_targets`), as for a run's.

**The progress** (`operations.progress`, which the api polls) is `{"asked", "moved", "resolved"}`: the
payload's ids, those moved `rejected -> pending`, and those resolved since. `moved` and `resolved` carry
over from the execution before (read from the row with the epoch), and step 2 starts `resolved` at least at
`moved` less the ids still `pending`, so a crash between an item's commit and its progress write loses no
count. An item the current parser still refuses stays `rejected`, and is counted as not moved, never as
recovered.

Ids and counts only reach the payload, the progress and the log lines: never a raw item's content.
"""

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.clock import utcnow
from jobhunter.db.guards import global_fence_active
from jobhunter.db.sql import q
from jobhunter.dedup import identity
from jobhunter.errors import FencedOut
from jobhunter.faults import afault_point
from jobhunter.observability.logging import get_logger
from jobhunter.sources import items
from jobhunter.sources.receipts import RAW_BODY_TTL
from jobhunter.sources.spec_adapters import _spec_adapters
from jobhunter.types import JSONValue, get_list
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.operations import (
    OperationContext,
    create_operation,
    lock_operation_owner,
    register_operation_handler,
    set_progress,
)
from jobhunter.worker.tasks import Deferrer

KIND: Final = "replay_raw_items"
MAX_ITEMS: Final = 500
# The requeue's error code while a global purge fence is active, and how long it waits before the next try.
PURGE_IN_PROGRESS: Final = "purge_in_progress"
PURGE_RETRY_AFTER: Final = timedelta(minutes=5)
AFTER_REPLAY_COMMIT: Final = "replay.after_replay_commit"
# The owner `ledger.lock_item_owner` names for an active global purge fence.
_PURGE_FENCE: Final = "purge_fence"

# Whether the response exists, with its `rejected` items (`raw_items (raw_response_id, item_index)`).
_REJECTED_OF_RESPONSE: Final = q(
    "SELECT i.id FROM raw_responses r LEFT JOIN raw_items i ON i.raw_response_id = r.id "
    "AND i.disposition = 'rejected' WHERE r.id = :r ORDER BY i.id"
)
_REPLAYABLE: Final = q(
    "SELECT i.id FROM raw_items i JOIN raw_responses r ON r.id = i.raw_response_id "
    "WHERE i.id = ANY(CAST(:ids AS bigint[])) AND i.disposition = 'rejected' AND r.purged_at IS NULL "
    "AND r.fetched_at > :since ORDER BY i.id LIMIT :limit"
)
_PENDING: Final = q(
    "SELECT id FROM raw_items WHERE id = ANY(CAST(:ids AS bigint[])) AND disposition = 'pending' ORDER BY id"
)
# The claim's epoch and the progress of the executions before it (no lock: the claim is the row's one writer).
_OWN: Final = q(
    "SELECT worker_epoch, progress FROM operations WHERE id = :i AND claim_id = :c AND status = 'running'"
)

log = get_logger("jobhunter.sources.replay")


def _sorted_ids(raw_item_ids: Sequence[int]) -> list[int]:
    ids = sorted(set(raw_item_ids))
    if not 1 <= len(ids) <= MAX_ITEMS:
        raise ValueError(f"a replay names 1 to {MAX_ITEMS} raw items, not {len(ids)}")
    return ids


def dedup_key(raw_item_ids: Sequence[int]) -> str:
    """The dedup key of the replay of `raw_item_ids`: `replay_raw_items:` + the hex SHA-256 of the sorted,
    deduplicated ids joined by commas."""
    joined = ",".join(str(raw_item_id) for raw_item_id in _sorted_ids(raw_item_ids))
    return f"{KIND}:{hashlib.sha256(joined.encode('ascii')).hexdigest()}"


async def items_of_response(conn: AsyncConnection, raw_response_id: int) -> list[int]:
    """The ids of the stored response's `rejected` items, in id order; `LookupError` when no response has the
    id."""
    rows = (await conn.execute(_REJECTED_OF_RESPONSE, {"r": raw_response_id})).scalars().all()
    if not rows:
        raise LookupError(f"raw response {raw_response_id} does not exist")
    return [int(raw_item_id) for raw_item_id in rows if raw_item_id is not None]


async def replayable_items(conn: AsyncConnection, raw_item_ids: Sequence[int], *, now: datetime) -> list[int]:
    """The ids among `raw_item_ids` that can be replayed at `now` (module docstring): `rejected`, their body
    not purged and fetched after `now - RAW_BODY_TTL`; at most `MAX_ITEMS`, lowest ids first."""
    params = {"ids": sorted(set(raw_item_ids)), "since": now - RAW_BODY_TTL, "limit": MAX_ITEMS}
    return [int(raw_item_id) for raw_item_id in (await conn.execute(_REPLAYABLE, params)).scalars()]


async def request_replay(conn: AsyncConnection, *, raw_item_ids: Sequence[int], defer: Deferrer) -> int:
    """The live replay operation of `raw_item_ids` (deduplicated and sorted, 1 to `MAX_ITEMS`; `ValueError`
    otherwise), created and its job deferred in the caller's transaction when there is none; its id."""
    ids = _sorted_ids(raw_item_ids)
    payload: dict[str, JSONValue] = {"raw_item_ids": list[JSONValue](ids)}
    operation_id, _ = await create_operation(
        conn, kind=KIND, payload=payload, dedup_key=dedup_key(ids), defer=defer
    )
    return operation_id


@dataclass
class _Progress:
    """The operation's `{"asked", "moved", "resolved"}` (module docstring)."""

    asked: int
    moved: int
    resolved: int

    @classmethod
    def carried(cls, stored: Mapping[str, JSONValue], *, asked: int) -> "_Progress":
        """The counts an earlier execution recorded (0 for one it did not), for `asked` ids."""

        def count(name: str) -> int:
            value = stored.get(name)
            return value if isinstance(value, int) and not isinstance(value, bool) else 0

        return cls(asked, count("moved"), count("resolved"))

    def as_json(self) -> dict[str, JSONValue]:
        return {"asked": self.asked, "moved": self.moved, "resolved": self.resolved}


def _payload_ids(payload: Mapping[str, JSONValue]) -> list[int]:
    ids: list[int] = []
    for value in get_list(dict(payload), "raw_item_ids"):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("raw_item_ids: expected a list of ids")
        ids.append(value)
    return _sorted_ids(ids)


async def _replay(ctx: OperationContext, ids: Sequence[int], *, now: datetime) -> _Progress | None:
    """Step 1 (module docstring): the progress after the replay, or `None` when the operation was requeued
    under an active global purge fence."""
    claim = ctx.claim
    async with _spec_adapters(now) as adapters, ctx.engine.begin() as conn:
        own = (await conn.execute(_OWN, {"i": claim.operation_id, "c": claim.claim_id})).first()
        if own is None:
            raise FencedOut("operation")
        await fence_epoch(conn, int(own.worker_epoch))
        await lock_operation_owner(conn, claim)
        if await global_fence_active(conn):
            await ctx.requeue_in(conn, error_code=PURGE_IN_PROGRESS, not_before=now + PURGE_RETRY_AFTER)
            return None
        progress = _Progress.carried(own.progress, asked=len(ids))
        progress.moved += await items.replay_rejected(conn, raw_item_ids=ids, now=now, adapters=adapters)
        await set_progress(conn, claim, progress.as_json())  # the last statement
    return progress


async def _requeue_fenced(ctx: OperationContext, progress: _Progress, *, now: datetime) -> None:
    async with ctx.engine.begin() as conn:
        await set_progress(conn, ctx.claim, progress.as_json())
        await ctx.requeue_in(conn, error_code=PURGE_IN_PROGRESS, not_before=now + PURGE_RETRY_AFTER)


@register_operation_handler(KIND)
async def run_replay(ctx: OperationContext) -> None:
    """The `replay_raw_items` operation: the replay, then the resolution of the moved items (module
    docstring)."""
    claim = ctx.claim
    clock = utcnow if ctx.deps is None else ctx.deps.clock
    ids = _payload_ids(claim.payload)
    progress = await _replay(ctx, ids, now=clock())
    if progress is None:
        log.info("replay_fenced", operation_id=claim.operation_id, asked=len(ids))
        return
    await afault_point(AFTER_REPLAY_COMMIT)
    async with ctx.engine.connect() as conn:  # a plain read, rolled back when the block ends
        pending = [int(i) for i in (await conn.execute(_PENDING, {"ids": ids})).scalars()]
    # Those moved and not pending any more were resolved, also by an execution that crashed before it
    # recorded them.
    progress.resolved = max(progress.resolved, progress.moved - len(pending))
    for raw_item_id in pending:
        try:
            async with ctx.engine.begin() as conn:
                await identity.resolve_item(conn, owner=claim, raw_item_id=raw_item_id)
        except FencedOut as fenced:
            if fenced.owner != _PURGE_FENCE:
                raise
            await _requeue_fenced(ctx, progress, now=clock())
            log.info("replay_fenced", operation_id=claim.operation_id, **progress.as_json())
            return
        except LookupError:  # gone since the read (retention): nothing is left to resolve
            log.info("replay_item_gone", operation_id=claim.operation_id, raw_item_id=raw_item_id)
            continue
        progress.resolved += 1
        async with ctx.engine.begin() as conn:
            await set_progress(conn, claim, progress.as_json())
    async with ctx.engine.begin() as conn:
        await set_progress(conn, claim, progress.as_json())
        await ctx.finish_in(conn, status="succeeded")
    log.info("raw_items_replayed", operation_id=claim.operation_id, **progress.as_json())
