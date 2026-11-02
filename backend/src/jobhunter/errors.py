"""Domain errors (spec §17.1): outcomes a caller handles, as opposed to bugs.

None of them carries content in its message: they name tables, ids, states and owners only, so their messages
are safe to log. `NodeFailed`, the one bug here, is the content-free stand-in for an unclassified error of a
graph node.

`Refusal` (M4 Task 1, R6) is a request the domain refuses, in the terms of the api's answer (§14.4): domain
code raises it, never the api's `Problem`, and never imports `jobhunter.api.errors`; the api answers it in one
handler (`api.errors.problem_of`). Its message (`str`) is its code. Its title, detail and field messages go to
the client only, never to a log line: a field message may echo the sender's own input (ruling T14-h).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from jobhunter.types import JSONValue

# The problem codes of §14.4 (`api.errors.ProblemCode` is this type).
ErrorCode = Literal[
    "unauthenticated",
    "csrf_failed",
    "throttled",
    "invalid_cursor",
    "stale_base_version",
    "validation_failed",
    "budget_exhausted",
    "filtered_requires_force",
    "run_active",
    "not_found",
    "conflict_state",
    "upload_rejected",
    "idempotency_key_required",
    "idempotency_key_reused",
    "idempotency_in_progress",
    "eval_gate_failed",
    "profile_purge_in_progress",
    "internal",
]
# The members a problem document writes itself (`api.errors.problem_body`): no `extra` may replace them, of a
# `Refusal` or of the api's `Problem` (ruling T1-b).
RESERVED_PROBLEM_MEMBERS: Final = frozenset(
    {"type", "title", "status", "code", "detail", "instance", "request_id", "errors"}
)


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


@dataclass(frozen=True)
class RefusalField:
    """A field a refusal names (a member of the problem's `errors`): its path, a code and a message."""

    field: str
    code: str
    message: str


class Refusal(DomainError):
    """A refused request: the status (4xx only), code, title, detail, fields and extra members of its problem.

    `fields=None` gives the problem no `errors` member, an empty sequence an empty one. `extra` cannot hold a
    `RESERVED_PROBLEM_MEMBERS` name (`ValueError`, as the api's `Problem` refuses it). A 5xx is never a
    refusal: it is a bug or an outage, which the api answers itself.
    """

    def __init__(
        self,
        status: int,
        code: ErrorCode,
        title: str,
        *,
        detail: str | None = None,
        fields: Sequence[RefusalField] | None = None,
        extra: Mapping[str, JSONValue] | None = None,
    ) -> None:
        super().__init__(code)
        if not 400 <= status <= 499:
            raise ValueError(f"a refusal has a 4xx status, not {status}")
        if clash := RESERVED_PROBLEM_MEMBERS & set(extra or {}):
            raise ValueError(f"extra cannot replace the standard members {sorted(clash)}")
        self.status = status
        self.code: ErrorCode = code
        self.title = title
        self.detail = detail
        self.fields = None if fields is None else tuple(fields)
        self.extra = dict(extra or {})


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
