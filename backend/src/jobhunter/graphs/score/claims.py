"""The claim heartbeats of a score cycle (spec §9.4.3 L3572; plan Task 10, Codex r1 #12, r2 B5, B6, r3 B18,
Rulings P10, P46): `ClaimHeartbeats`, the cycle's `context.ClaimTracker`, one API for a claim's beat.

**The lifecycle, owned by the cycle** (Codex r1 #12, r2 B5, B6). Task 11's `claim` node calls `start(ref)` for
every claim right after the claim transaction committed, so a claim waiting for one of the three fan-out
slots is beaten from the start and never reaches the 3-minute reclaim while the cycle is healthy. Each beat
runs as a cycle-owned task named `heartbeat:claim:<target>`, never a node's: a node's task is done by the
time the next node runs. Task 11's wrapper `run_target` starts the subgraph in an owned child task and
`attach(ref, child)`es it (at most one per claim); `stop(ref)` ends the beat at the wrapper's end and
`stop_all()` at the cycle's. The cycle's own heartbeat (`run_owned`) covers the claims only indirectly,
through the reconciler reclaiming them when the cycle dies.

**The beat** (`worker.heartbeat.heartbeat_loop`, every `period_s`, 30 s, on the injected `sleep`): first the
fault point `claims.beat` (`BEAT_POINT`, Ruling P46: the point Task 28's C14 stalls), then `UPDATE
score_targets SET claim_heartbeat_at = now() WHERE id = :t AND claim_id = :c AND state = 'in_progress'`
(L3572) in a transaction of its own. No row updated is the claim lost: `lost(ref)` turns true and the
attached child, if any, is cancelled (its in-flight stream settles `ambiguous` through the gateway's
`CancelledError` path, M2 V11); the loop ends. A claim lost before any `attach` is only marked lost (the
wrapper returns `claim_lost` without starting the subgraph), and a child attached after the loss is cancelled
at once. The loss is logged with the seconds since the claim's last good beat on the injected `monotonic`
clock (`score_claim_heartbeat_lost`; ids only).

**A beat that raises** (a database blip) changes nothing on the claim's row, and the spec counts only "0 rows
updated" as lost: up to `TOLERATED_FAILURES` (3) raising beats in a row are each logged with the error's class
only (`heartbeat_failed`, the loop's) and the beat goes on; the fourth in a row is the loss. That fourth
beat comes 120 s after the last good one, inside the 3-minute reclaim window (about 5 beats), so a claim this
tolerates has not become reclaimable meanwhile.

**The owner's own end** is no loss. A beat that waits on the target row while the owner's `publish` or `fail`
holds it `FOR UPDATE` finds no `in_progress` row once they commit. On 0 rows the beat reads the claim's
attempt (`score_attempts` by target and `attempt_no`): when it has ended with an outcome other than
`claim_lost` or `cancelled` (the two a rival writes, A2 T12), the attempt ended on its own path, the beat
stops quietly (`score_claim_heartbeat_ended`), `lost(ref)` stays false and the child, which may still be
finishing after its terminal commit, is not cancelled.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.db.sql import q
from jobhunter.faults import afault_point
from jobhunter.graphs.score.state import ClaimRef
from jobhunter.observability.logging import get_logger
from jobhunter.worker.heartbeat import heartbeat_loop

BEAT_POINT: Final = "claims.beat"
PERIOD_S: Final = 30.0
CANCEL_TIMEOUT_S: Final = 30.0
TOLERATED_FAILURES: Final = 3

_BEAT: Final = q(
    "UPDATE score_targets SET claim_heartbeat_at = now() "
    "WHERE id = :t AND claim_id = :c AND state = 'in_progress'"
)
_ATTEMPT_OUTCOME: Final = q("SELECT outcome FROM score_attempts WHERE target_id = :t AND attempt_no = :a")
# The attempt outcomes that are no end of the attempt's own: still running, or ended by a rival (A2 T12).
_NOT_ITS_OWN_END: Final = frozenset({"running", "claim_lost", "cancelled"})

log = get_logger("jobhunter.graphs.score.claims")

type _Key = tuple[int, str, int]


def _key(ref: ClaimRef) -> _Key:
    return ref["target_id"], ref["claim_id"], ref["attempt_no"]


@dataclass
class _Claim:
    """One claim's beat: its task, the child a loss cancels, whether it was lost or stopped, the outcome its
    attempt ended with on its own path (`ended`, module docstring), and the monotonic time of its start or its
    last good beat."""

    ref: ClaimRef
    task: "asyncio.Task[None] | None"
    beaten_at: float
    child: "asyncio.Task[object] | None" = None
    lost: bool = False
    stopped: bool = False
    ended: str | None = None


class ClaimHeartbeats:
    """The claim heartbeats of one cycle (module docstring)."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        period_s: float = PERIOD_S,
        monotonic: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        cancel_timeout_s: float = CANCEL_TIMEOUT_S,
    ) -> None:
        self._engine = engine
        self._period_s = period_s
        self._monotonic = monotonic
        self._sleep = sleep
        self._cancel_timeout_s = cancel_timeout_s
        self._claims: dict[_Key, _Claim] = {}

    def start(self, ref: ClaimRef) -> None:
        """Beat `ref` from now on, as a task of its own; `ValueError` for a claim started already."""
        key = _key(ref)
        if key in self._claims:
            raise ValueError(f"the claim of score target {ref['target_id']} has a heartbeat already")
        claim = _Claim(ref=ref, task=None, beaten_at=self._monotonic())
        self._claims[key] = claim

        async def beat() -> bool:
            return await self._beat(claim)

        claim.task = asyncio.create_task(
            heartbeat_loop(
                beat,
                period_s=self._period_s,
                on_lost=lambda: self._lost(claim),
                sleep=self._sleep,
                tolerated_failures=TOLERATED_FAILURES,
                label=f"claim:{ref['target_id']}",
            ),
            name=f"heartbeat:claim:{ref['target_id']}",
        )

    def attach(self, ref: ClaimRef, child: "asyncio.Task[object]") -> None:
        """Name `child` as the task scoring `ref`, which a loss cancels (module docstring); `ValueError` for a
        claim without a running heartbeat, or one with another live child attached."""
        claim = self._claims.get(_key(ref))
        if claim is None or claim.stopped:
            raise ValueError(f"no heartbeat for the claim of score target {ref['target_id']}")
        if claim.child is not None and claim.child is not child and not claim.child.done():
            raise ValueError(f"the claim of score target {ref['target_id']} has a child attached already")
        claim.child = child
        if claim.lost:
            child.cancel()

    def lost(self, ref: ClaimRef) -> bool:
        """Whether a beat of `ref` updated no row (and its attempt had not ended on its own path), or failed
        more than `TOLERATED_FAILURES` times in a row."""
        claim = self._claims.get(_key(ref))
        return claim is not None and claim.lost

    async def stop(self, ref: ClaimRef) -> None:
        """End `ref`'s beat: its task cancelled and awaited (up to `cancel_timeout_s`)."""
        claim = self._claims.get(_key(ref))
        if claim is None or claim.stopped:
            return
        claim.stopped = True
        task = claim.task
        if task is None:
            return
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=self._cancel_timeout_s)
        if not done:
            log.warning("score_claim_heartbeat_cancel_timeout", target_id=claim.ref["target_id"])

    async def stop_all(self) -> None:
        """End every claim's beat (the cycle's end)."""
        for claim in list(self._claims.values()):
            await self.stop(claim.ref)

    async def _beat(self, claim: _Claim) -> bool:
        await afault_point(BEAT_POINT)
        ref = claim.ref
        params = {"t": ref["target_id"], "c": ref["claim_id"]}
        outcome = None
        async with self._engine.begin() as conn:
            updated = (await conn.execute(_BEAT, params)).rowcount
            if updated != 1:  # a statement of its own: its snapshot sees what the blocking owner committed
                found = await conn.execute(_ATTEMPT_OUTCOME, {"t": ref["target_id"], "a": ref["attempt_no"]})
                outcome = found.scalar_one_or_none()
        if updated == 1:
            claim.beaten_at = self._monotonic()
            return True
        if outcome is not None and str(outcome) not in _NOT_ITS_OWN_END:
            claim.ended = str(outcome)  # the loop's `on_lost` then stops the beat quietly
        return False

    def _lost(self, claim: _Claim) -> None:
        if claim.ended is not None:
            log.info(
                "score_claim_heartbeat_ended",
                target_id=claim.ref["target_id"],
                attempt_no=claim.ref["attempt_no"],
                attempt_outcome=claim.ended,
            )
            return
        claim.lost = True
        child = claim.child
        if child is not None and not child.done():
            child.cancel()
        log.warning(
            "score_claim_heartbeat_lost",
            target_id=claim.ref["target_id"],
            attempt_no=claim.ref["attempt_no"],
            since_last_beat_s=round(self._monotonic() - claim.beaten_at, 3),
            child_cancelled=child is not None,
        )
