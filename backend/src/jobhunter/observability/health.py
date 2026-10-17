"""Readiness (spec §17.5): the serving prerequisites of the api, checked on one connection.

`check_readiness` answers; it never raises (ruling T14-e). Its probes, in this order:
- `db`: a connection and `SELECT 1` within `DB_TIMEOUT_S`. When that fails, nothing else is probed.
- `migrations`: `alembic_version` holds exactly the code head.
- `worker_heartbeat`: the heartbeat row exists and is at most `HEARTBEAT_STALE_S` old.

Each schema probe runs in a savepoint of its own and its database error is caught outside that block, after
the savepoint rolled back, so a failed probe (e.g. `42P01` on a database without the schema) never aborts the
transaction the next probe uses. A probe whose error invalidated the connection means the database went away
mid-check: the answer is then `db`. After `SELECT 1`, the transaction sets a `statement_timeout`, so a probe
that waits behind a lock (a migration holding `ACCESS EXCLUSIVE`) fails by itself instead of holding
`/readyz` and a pool connection. The server enforces that timeout; the client bounds the whole probe sequence
too (`PROBES_TIMEOUT_S`), for a database that stops answering while its socket stays open: the answer is
then `db`.

Every failed probe logs `readiness_probe_failed` with `probe`, `sqlstate` and `error_class` (`None` when no
error was raised; `reason` says what was wrong then). Never an exception message.
"""

import asyncio
from collections.abc import Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.elements import TextClause

from jobhunter.db.sql import pg_error, q
from jobhunter.observability.logging import get_logger
from jobhunter.worker.lock import HEARTBEAT_STALE_S

ReadinessCode = Literal["db", "migrations", "worker_heartbeat"]

DB_TIMEOUT_S: Final = 2.0
# The client-side bound of the schema probes together; each one also has the server-side statement timeout.
PROBES_TIMEOUT_S: Final = 2 * DB_TIMEOUT_S
PROBE_STATEMENT_TIMEOUT: Final = "2s"
HEARTBEAT_STALE: Final = timedelta(seconds=HEARTBEAT_STALE_S)

_PING: Final = q("SELECT 1")
# `is_local`: for this transaction only, so the pooled connection gets its default back with the rollback.
_SET_STATEMENT_TIMEOUT: Final = q("SELECT set_config('statement_timeout', :timeout, true)")
_MIGRATION_VERSIONS: Final = q("SELECT version_num FROM alembic_version")
# The database clock, which also wrote `heartbeat_at`: the verdict `jobhunter worker-health` reaches too.
_HEARTBEAT_AGE: Final = q("SELECT now() - heartbeat_at FROM worker_heartbeat WHERE id = 1")

log = get_logger("jobhunter.observability.health")


@dataclass(frozen=True)
class ReadinessReport:
    ok: bool
    failing: list[ReadinessCode]


class _ConnectionLost(Exception):
    """A probe's database error invalidated the connection."""

    def __init__(self, error: sa.exc.DBAPIError) -> None:
        super().__init__("connection lost during the readiness check")
        self.error = error


def _log_failed(
    probe: ReadinessCode, error: BaseException | None = None, *, reason: str | None = None
) -> None:
    log.warning(
        "readiness_probe_failed",
        probe=probe,
        sqlstate=pg_error(error).sqlstate if isinstance(error, sa.exc.DBAPIError) else None,
        error_class=None if error is None else type(error).__name__,
        reason=reason,
    )


async def _rows(
    conn: AsyncConnection, probe: ReadinessCode, statement: TextClause
) -> Sequence[sa.Row[Any]] | None:
    """The rows of `statement`, run in a savepoint; `None` (logged) when it failed with a database error."""
    try:
        async with conn.begin_nested():
            rows = (await conn.execute(statement)).all()
    except sa.exc.DBAPIError as exc:
        if exc.connection_invalidated:
            raise _ConnectionLost(exc) from exc
        _log_failed(probe, exc)
        return None
    return rows


async def _schema_probes(conn: AsyncConnection, code_head: str) -> list[ReadinessCode]:
    failing: list[ReadinessCode] = []

    versions = await _rows(conn, "migrations", _MIGRATION_VERSIONS)
    if versions is None:
        failing.append("migrations")
    elif [row[0] for row in versions] != [code_head]:
        _log_failed("migrations", reason="no_revision" if not versions else "revision_mismatch")
        failing.append("migrations")

    heartbeat = await _rows(conn, "worker_heartbeat", _HEARTBEAT_AGE)
    if heartbeat is None:
        failing.append("worker_heartbeat")
    elif not heartbeat or heartbeat[0][0] > HEARTBEAT_STALE:
        _log_failed("worker_heartbeat", reason="no_heartbeat" if not heartbeat else "stale")
        failing.append("worker_heartbeat")

    return failing


async def _probe_all(engine: AsyncEngine, code_head: str) -> list[ReadinessCode]:
    async with AsyncExitStack() as stack:
        try:
            async with asyncio.timeout(DB_TIMEOUT_S):
                conn = await stack.enter_async_context(engine.connect())
                await conn.execute(_PING)
                await conn.execute(_SET_STATEMENT_TIMEOUT, {"timeout": PROBE_STATEMENT_TIMEOUT})
        except (sa.exc.DBAPIError, OSError, TimeoutError) as exc:
            _log_failed("db", exc)
            return ["db"]
        try:
            async with asyncio.timeout(PROBES_TIMEOUT_S):
                return await _schema_probes(conn, code_head)
        except _ConnectionLost as lost:
            _log_failed("db", lost.error)
            return ["db"]
        except TimeoutError as exc:
            _log_failed("db", exc)
            return ["db"]


async def check_readiness(engine: AsyncEngine, *, code_head: str, now: datetime) -> ReadinessReport:
    """Probe the serving prerequisites; `failing` lists the failed ones in the order `db`, `migrations`,
    `worker_heartbeat`.

    `now` is the caller's clock (system health passes the one it uses for everything else). No probe reads it:
    the heartbeat age is measured on the database clock that wrote the heartbeat.
    """
    try:
        failing = await _probe_all(engine, code_head)
    except Exception as exc:  # noqa: BLE001 - readiness is an answer, never an error (T14-e); the 503 is the outcome
        _log_failed("db", exc)
        failing = ["db"]
    return ReadinessReport(ok=not failing, failing=failing)
