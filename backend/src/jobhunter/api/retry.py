"""The api's retry of a transaction the server aborted (spec §9.1 L3456; plan M4 Task 8, Ruling T22-a).

`with_deadlock_retry(engine, work)` runs `work` in a transaction of its own (`engine.begin()`) and returns
what it returns. A serialization failure (40001) or a deadlock (40P01) (`db.sql.is_serialization_failure`:
the server rolled the transaction back, so nothing of it was written) runs `work` again in a fresh
transaction, `attempts` tries in all (three: the non-LLM retry policy's `max_attempts`), each retry logged
`api_deadlock_retried` with its attempt number and SQLSTATE only. The last failure is logged
`api_deadlock_retries_exhausted` (the attempts and the SQLSTATE) and is 409 `conflict_state` with
`retryable: true` (`api.errors.retryable_conflict`): the same request may succeed once the other transaction
ends. No wait between the tries: the aborted transaction released its locks, and the one it met holds them
only until its commit.

Nothing else is tried again: a lost connection (whose commit outcome is unknown), a domain error, a
`Refusal` or a `Problem` propagates from the try it was raised in. A read-then-lock round's give-up
(`errors.CoMemberChanged`) is no exception to that: its rounds ran already, so `api.errors.domain_problem`
answers it with the same `retryable_conflict` and leaves the retry to the client.
"""

from collections.abc import Awaitable, Callable
from typing import Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.api.errors import retryable_conflict
from jobhunter.db.sql import is_serialization_failure, pg_error
from jobhunter.observability.logging import get_logger

ATTEMPTS: Final = 3  # the non-LLM retry policy's `max_attempts` (§9.1 L3456)

log = get_logger("jobhunter.api.retry")


async def with_deadlock_retry[T](
    engine: AsyncEngine, work: Callable[[AsyncConnection], Awaitable[T]], *, attempts: int = ATTEMPTS
) -> T:
    """`work` in a transaction of its own, run again in a fresh one after a serialization failure or a
    deadlock, `attempts` tries in all; the last failure is `retryable_conflict()` (module docstring)."""
    if attempts < 1:
        raise ValueError("attempts counts the tries, at least one")
    attempt = 0
    while True:
        attempt += 1
        try:
            async with engine.begin() as conn:
                return await work(conn)
        except sa.exc.DBAPIError as exc:
            if not is_serialization_failure(exc):
                raise
            sqlstate = pg_error(exc).sqlstate
            if attempt >= attempts:
                log.warning("api_deadlock_retries_exhausted", attempts=attempts, sqlstate=sqlstate)
                raise retryable_conflict() from None
            log.warning("api_deadlock_retried", attempt=attempt, sqlstate=sqlstate)
