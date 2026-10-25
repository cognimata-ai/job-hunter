"""Domain errors (spec §17.1): outcomes a caller handles, as opposed to bugs.

None of them carries content: they name tables, ids, states and owners only, so their messages are safe to
log. `NodeFailed`, the one bug here, is the content-free stand-in for an unclassified error of a graph node.
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


class NodeFailed(Exception):
    """The content-free stand-in for an unclassified error (a §17.1 `Bug`) that escaped a graph node.

    LangGraph stores the exception that escapes a node in `checkpoint_writes` (its `repr`), and an SQLAlchemy
    or Pydantic error can quote bound values, CV text among them, in its message. A node wrapper
    (`graphs.profile.graph.sanitized`, plan Deviation 25) logs the original's class and raises this instead,
    outside the handler that caught it, so neither `__cause__` nor `__context__` refers to the original.

    `retryable_db_error` says the original was a serialization failure, a deadlock or a lost connection:
    `graphs.collect.graph.is_retryable_db_error` then retries the stand-in, so `DB_RETRY` still applies to a
    wrapped node. Not a `DomainError`: it is a bug, which no caller handles as an outcome.
    """

    def __init__(self, node: str, error_class: str, *, retryable_db_error: bool = False) -> None:
        super().__init__(f"node {node} failed: {error_class}")
        self.node = node
        self.error_class = error_class
        self.retryable_db_error = retryable_db_error
