"""What every graph node shares (spec §9.1, §16): the non-I/O retry and the content-free stand-in of an error.

**`DB_RETRY`** is §9.1's non-I/O retry: `RetryPolicy(max_attempts=3, initial_interval=0.5, backoff_factor=2,
jitter=True)` on `is_retryable_db_error`, a serialization failure (40001), a deadlock (40P01) or a lost
connection, and the stand-in of one (below), so the retry applies to a wrapped node too.

**`sanitized`** (M2 plan Deviation 25; final review FR-12, for the collection graph) wraps a node of either
graph. LangGraph 1.2.15 stores the exception that escapes a node in `checkpoint_writes` (the write
`(__error__, exc)` of `PregelRunner.commit`), through its `JsonPlusSerializer`, which encodes an exception as
its `repr`: the class name and the arguments, never its traceback, its frames' locals, its `__cause__` or its
`__context__`. An SQLAlchemy error can quote bound values in that text, and PostgreSQL's own DETAIL (`Key
(company_key)=(…) already exists`) quotes a row's values even when the bound parameters are hidden
(`db/session.py`); a Pydantic error quotes its input. LangGraph's own retry log line quotes the error's text
too. So only the errors of the caller's `passing` classes, whose text is a code or ids, pass through
unchanged (`asyncio.CancelledError` is a `BaseException` and is never caught). Any other error is logged as
`event` with the node's name and the error's class only, and replaced by `errors.NodeFailed(node,
error_class)`, raised after the `except` block, so neither `__cause__` nor `__context__` refers to the
original. The stand-in of a retryable database error carries `retryable_db_error`, which `DB_RETRY` retries
as it would the original.

The wrapper keeps the node's signature (`functools.wraps`): LangGraph reads it (through `__wrapped__`) to
decide whether the node takes the invoke `config`, so one wrapper serves the collection nodes, which take the
state only, and the profile nodes, which take the config too.
"""

import functools
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Final

from langgraph.types import RetryPolicy
from sqlalchemy.exc import DBAPIError
from structlog.stdlib import BoundLogger

from jobhunter.db.sql import is_serialization_failure
from jobhunter.errors import NodeFailed


def is_retryable_db_error(exc: BaseException) -> bool:
    """A serialization failure (40001), a deadlock (40P01) or a lost connection: the non-I/O retry (§9.1).

    Also the content-free stand-in of one (`errors.NodeFailed` with `retryable_db_error`), which `sanitized`
    raises in its place, so `DB_RETRY` applies to a wrapped node too.
    """
    if isinstance(exc, NodeFailed):
        return exc.retryable_db_error
    return is_serialization_failure(exc) or (isinstance(exc, DBAPIError) and exc.connection_invalidated)


DB_RETRY: Final = RetryPolicy(
    max_attempts=3, initial_interval=0.5, backoff_factor=2, jitter=True, retry_on=is_retryable_db_error
)


def sanitized[**P, U](
    name: str,
    node: Callable[P, Awaitable[U]],
    *,
    passing: tuple[type[BaseException], ...],
    log: BoundLogger,
    event: str,
) -> Callable[P, Coroutine[Any, Any, U]]:
    """`node`, whose errors outside `passing` become `NodeFailed(name, <class>)`, logged as `event` (module
    docstring)."""

    @functools.wraps(node)
    async def guarded(*args: P.args, **kwargs: P.kwargs) -> U:
        try:
            return await node(*args, **kwargs)
        except passing:
            raise
        except Exception as exc:  # noqa: BLE001 - replaced by a content-free stand-in, raised below
            log.error(event, node=name, error_class=type(exc).__name__)
            failure = NodeFailed(name, type(exc).__name__, retryable_db_error=is_retryable_db_error(exc))
        # Outside the handler: the stand-in has no `__context__`, and the original is unreachable from it.
        raise failure

    return guarded
