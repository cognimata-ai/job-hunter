"""Domain errors (spec §17.1): outcomes a caller handles, as opposed to bugs.

None of them carries content: they name tables, ids, states and owners only, so their messages are safe to
log.
"""


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


class FencedOut(DomainError):
    """The owner's predicate no longer holds (a newer attempt, claim or epoch owns the work); abort."""

    def __init__(self, owner: str) -> None:
        super().__init__(f"fenced out: {owner}")
        self.owner = owner


class InvariantViolation(DomainError):
    """A cross-table invariant (§7.17) does not hold."""
