"""Domain errors (spec §17.1): outcomes a caller handles, as opposed to bugs.

None of them carries content: they name tables, ids, states and owners only, so their messages are safe to
log.
"""

from typing import Final


class DomainError(Exception):
    """Base of the errors that describe a legitimate outcome (a lost race, a fenced owner, a broken rule)."""


class TransitionConflict(DomainError):
    """A `transition()` updated no row: the row is absent, is not in a `from` state, or has another owner.

    The caller decides what that means: a no-op when the transition is idempotent, `FencedOut` for a stale
    owner, or an invariant alert (§7.16).
    """

    def __init__(self, table: str, row_id: int, to_state: str) -> None:
        super().__init__(f"{table} {row_id}: no row moved to {to_state!r}")
        self.table = table
        self.row_id = row_id
        self.to_state = to_state


# The owners of the worker's own fences: `worker.epoch.fence_epoch` (a newer worker epoch is current) and the
# epoch claim (this process does not hold the singleton lock).
WORKER_EPOCH: Final = "worker_epoch"
WORKER_LOCK: Final = "worker_lock"


class FencedOut(DomainError):
    """The owner's predicate no longer holds (a newer attempt, claim or epoch owns the work); abort."""

    def __init__(self, owner: str) -> None:
        super().__init__(f"fenced out: {owner}")
        self.owner = owner

    @property
    def worker_superseded(self) -> bool:
        """Whether the fence refused the worker itself (`WORKER_EPOCH`, `WORKER_LOCK`): a newer worker owns
        the work (§3.3.1 item 5), so this one stops (Ruling T24-a). Any other owner (a run, an operation, a
        purge fence, a ledger entry, a fetch request) is one piece of work lost, never a reason to stop."""
        return self.owner in (WORKER_EPOCH, WORKER_LOCK)


class InvariantViolation(DomainError):
    """A cross-table invariant (§7.17) does not hold."""
