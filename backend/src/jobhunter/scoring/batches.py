"""The score lane's batch submissions (spec §10.10, §7.12, §12.6 (e); plan Tasks 14 and 16, Ruling P14, Codex
r1 #24, A1 issue 17, Deviation 27): Task 14 records the cancellation a tuple supersession asks for; Task 16
adds the submission lifecycle.

`request_cancel(conn, *, llm_tuple_id, now) -> RequestedCancel`, in the caller's transaction (the tuple
activation's, which holds `active_config FOR UPDATE` and has already taken the month and bulk rows, so no
batch-lane transaction runs meanwhile):
1. `cancel_requested_at := now` on **every** non-terminal submission of the tuple (`IN_FLIGHT`: `preparing`,
   `submit_started`, `submit_ambiguous`, `submitted`, `ended`; Codex r1 #24), whatever its phase; a request
   recorded earlier keeps its time. This record is the request: the worker calls `messages.batches.cancel`
   after the commit, as soon as a provider id is known (`batch_poll` for a `submitted` one still in
   progress, `record_created` for one created during the request; Task 17). A submission's state does not
   change here;
2. in the same transaction, their `in_batch` targets → `cancelled('tuple_superseded')` with
   `batch_submission_id` cleared, their running attempts ended `cancelled`, each target's follow-up hold
   released (`targets.cancel_batched`: the bulk rows first, Ruling P52). An item that arrives later for such a
   target is fenced (`ignored_fenced`, Task 17).
It returns the counts of the submissions and targets it touched and the attempts it ended. Ids and counts
only reach a log line.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.observability.logging import get_logger
from jobhunter.scoring import targets

# The submissions of a tuple that are not terminal (§7.16.6).
IN_FLIGHT: Final = ("preparing", "submit_started", "submit_ambiguous", "submitted", "ended")
_RECORD: Final = q(
    "UPDATE batch_submissions SET cancel_requested_at = coalesce(cancel_requested_at, :now) "
    "WHERE llm_tuple_id = :tuple AND state = ANY(CAST(:states AS text[])) RETURNING id"
)

log = get_logger("jobhunter.scoring.batches")


@dataclass(frozen=True)
class RequestedCancel:
    """What a cancellation request touched: the submissions it recorded the request on, the `in_batch`
    targets it cancelled and the attempts it ended."""

    submissions: int
    targets: int
    attempts: int


async def request_cancel(conn: AsyncConnection, *, llm_tuple_id: int, now: datetime) -> RequestedCancel:
    """Record the cancellation on every non-terminal submission of the tuple and cancel their `in_batch`
    targets (module docstring)."""
    params = {"now": now, "tuple": llm_tuple_id, "states": list(IN_FLIGHT)}
    submissions = sorted(int(i) for i in (await conn.execute(_RECORD, params)).scalars())
    if not submissions:
        return RequestedCancel(0, 0, 0)
    cancelled = await targets.cancel_batched(
        conn, submission_ids=submissions, reason=targets.TUPLE_SUPERSEDED
    )
    log.info(
        "batch_cancel_requested",
        llm_tuple_id=llm_tuple_id,
        submission_ids=submissions,
        targets=cancelled.targets,
        attempts=cancelled.attempts,
    )
    return RequestedCancel(len(submissions), cancelled.targets, cancelled.attempts)
