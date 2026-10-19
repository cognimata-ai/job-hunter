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

`system_health` is the detailed health of `GET /api/system/health` (§17.5): readiness plus the run ages and
their `run_age_state`, the queue depths, the open alerts (it refreshes `ALERTS_OPEN`), the current quota
window of each provider, disk and database sizes and the versions. Run ages are reported, never readiness
gates. `RunOut` is the api's view of a run row, shared with `POST /api/runs`.

**One freshness rule** (ruling T17-g): `read_run_freshness` is the one query of the run figures and
`run_age_state` the one rule over them. System health reports that state, and the worker's `stale_data`
reconciler raises `schedule.stale_data` exactly while it is `critical`, so the page and the alert never
disagree.
"""

import asyncio
import functools
import platform
import shutil
from collections.abc import Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import metadata
from typing import Any, Final, Literal

import sqlalchemy as sa
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.elements import TextClause

from jobhunter import __version__
from jobhunter.db.sql import pg_error, q
from jobhunter.heartbeats import HEARTBEAT_STALE_S
from jobhunter.observability.alerts import refresh_alert_gauge
from jobhunter.observability.logging import get_logger
from jobhunter.observability.metrics import refresh_table_bytes
from jobhunter.quota.ledger import used_units
from jobhunter.quota.windows import Provider, WindowState, current_snapshot
from jobhunter.worker.tasks import QUEUES

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


RunAgeState = Literal["ok", "warn", "critical", "grace", "none"]

# The last successful run's age, in schedule intervals, past which it is `warn` and `critical` (§17.5). The
# grace lasts `RUN_AGE_CRITICAL_INTERVALS` intervals after the first settings version, as for the
# `schedule.stale_data` alert (`worker.runs.reconcile_stale_data`).
RUN_AGE_WARN_INTERVALS: Final = 1.5
RUN_AGE_CRITICAL_INTERVALS: Final = 2
DISK_PATH: Final = "/"
# The job statuses every known queue reports, 0 included; any other status is reported when it has jobs.
DEPTH_STATUSES: Final = ("todo", "doing")

# `runs_recent` serves it.
_LAST_RUN: Final = q(
    "SELECT id, kind, trigger, status, scheduled_slot, include_paid, attempt_no, queued_at, started_at, "
    "finished_at FROM runs ORDER BY queued_at DESC, id DESC LIMIT 1"
)
_RUN_FRESHNESS: Final = q(
    "SELECT EXISTS (SELECT 1 FROM runs) AS any_run, "
    "(SELECT max(finished_at) FROM runs WHERE status IN ('succeeded', 'partial')) AS last_success_at, "
    "(SELECT max(finished_at) FROM runs WHERE status = 'succeeded') AS last_full_success_at, "
    "(SELECT min(created_at) FROM settings_versions) AS first_settings_at, "
    "(SELECT interval_s FROM schedule_state WHERE id = 1) AS interval_s"
)
_QUEUE_DEPTHS: Final = q(
    "SELECT queue_name, status::text AS status, count(*) AS n FROM procrastinate_jobs "
    "GROUP BY queue_name, status ORDER BY queue_name, status"
)
_DATABASE_BYTES: Final = q("SELECT pg_database_size(current_database())")


class RunOut(BaseModel):
    """A run as the api shows it (`runs` row, §7.6); its times in UTC."""

    id: int
    kind: str
    trigger: str
    status: str
    scheduled_slot: datetime | None
    include_paid: bool
    attempt_no: int
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @field_validator("scheduled_slot", "queued_at", "started_at", "finished_at")
    @classmethod
    def _in_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else value.astimezone(UTC)

    @classmethod
    def of(cls, run: object) -> "RunOut":
        """The `RunOut` of anything with the run's columns as attributes (a `RunRow`, a result row)."""
        return cls.model_validate(run, from_attributes=True)


class ReadyState(BaseModel):
    ok: bool
    failing: list[ReadinessCode]


class AlertsOpen(BaseModel):
    info: int
    warn: int
    critical: int


class DiskUsage(BaseModel):
    path: str
    total_bytes: int
    free_bytes: int


class Versions(BaseModel):
    app: str
    python: str
    fastapi: str
    sqlalchemy: str
    psycopg: str
    procrastinate: str
    langgraph: str


class QuotaOut(BaseModel):
    """A provider's current quota window as stored (§7.8): counted usage against its limit, and its bucket."""

    window_start: datetime
    window_end: datetime
    state: WindowState
    limit_units: int
    used_units: int
    remaining_units: int
    tokens: float

    @field_validator("window_start", "window_end")
    @classmethod
    def _in_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


QUOTA_PROVIDERS: Final[tuple[Provider, ...]] = ("jsearch", "serpapi", "adzuna")


async def quota_health(conn: AsyncConnection) -> dict[Provider, QuotaOut | None]:
    """Each quota provider's current window with its counted usage, `None` when it has none yet."""
    out: dict[Provider, QuotaOut | None] = {}
    for provider in QUOTA_PROVIDERS:
        w = await current_snapshot(conn, provider)
        if w is None:
            out[provider] = None
            continue
        used = await used_units(conn, provider, w.window_start)
        out[provider] = QuotaOut(
            window_start=w.window_start,
            window_end=w.window_end,
            state=w.state,
            limit_units=w.limit_units,
            used_units=used,
            remaining_units=max(0, w.limit_units - used),
            tokens=float(w.tokens),
        )
    return out


class SystemHealthOut(BaseModel):
    """The detailed health (§17.5). Ages are seconds at the caller's `now`.

    - `last_run`: the newest run by `(queued_at, id)`; `last_run_age_s` is measured from its `queued_at`.
    - `last_successful_run_age_s`: since the latest `finished_at` of a `succeeded` or `partial` run;
      `last_fully_successful_run_age_s`: of a `succeeded` one only.
    - `run_age_state`: see `run_age_state`.
    - `queue_depths`: procrastinate jobs by queue and status.
    - `quota`: each quota provider's current window, `null` before its first one (`quota_health`).
    - `table_bytes`: every table of the `public` schema (`metrics.refresh_table_bytes`).
    """

    ready: ReadyState
    last_run: RunOut | None
    last_run_age_s: float | None
    last_successful_run_age_s: float | None
    last_fully_successful_run_age_s: float | None
    run_age_state: RunAgeState
    queue_depths: dict[str, dict[str, int]]
    quota: dict[Provider, QuotaOut | None]
    alerts_open: AlertsOpen
    disk: DiskUsage
    database_bytes: int
    table_bytes: dict[str, int]
    versions: Versions


@dataclass(frozen=True)
class RunFreshness:
    """The run figures the freshness rule reads (`read_run_freshness`).

    `last_success_at` is the latest `finished_at` of a `succeeded` or `partial` run, `last_full_success_at`
    of a `succeeded` one; `first_settings_at` and `interval_s` are `None` when nothing is seeded.
    """

    any_run: bool
    last_success_at: datetime | None
    last_full_success_at: datetime | None
    first_settings_at: datetime | None
    interval_s: int | None


async def read_run_freshness(conn: AsyncConnection) -> RunFreshness:
    """The run figures of the freshness rule, in one statement on `conn`."""
    row = (await conn.execute(_RUN_FRESHNESS)).one()
    return RunFreshness(
        any_run=bool(row.any_run),
        last_success_at=row.last_success_at,
        last_full_success_at=row.last_full_success_at,
        first_settings_at=row.first_settings_at,
        interval_s=None if row.interval_s is None else int(row.interval_s),
    )


def run_age_state(now: datetime, freshness: RunFreshness) -> RunAgeState:
    """How stale the collection is at `now`: the one freshness rule (ruling T17-g), in this order.

    - `none`: nothing is seeded (no schedule to be stale against);
    - until `RUN_AGE_CRITICAL_INTERVALS` intervals after the first settings version's `created_at` (its end
      included): `grace`, or `none` while no run has been recorded yet;
    - after it, by the age of the last successful (`succeeded` or `partial`) run, or of the first settings
      version when none succeeded yet: `critical` past 2 intervals, `warn` past 1.5 intervals, else `ok`. So
      no run at all after the grace is `critical`, exactly while the `schedule.stale_data` alert is open.
    """
    first, interval_s = freshness.first_settings_at, freshness.interval_s
    if first is None or interval_s is None:
        return "none"
    interval = timedelta(seconds=interval_s)
    if now <= first + RUN_AGE_CRITICAL_INTERVALS * interval:
        return "grace" if freshness.any_run else "none"
    age = now - (freshness.last_success_at or first)
    if age > RUN_AGE_CRITICAL_INTERVALS * interval:
        return "critical"
    if age > RUN_AGE_WARN_INTERVALS * interval:
        return "warn"
    return "ok"


def _age_s(now: datetime, at: datetime | None) -> float | None:
    return None if at is None else (now - at).total_seconds()


async def _queue_depths(conn: AsyncConnection) -> dict[str, dict[str, int]]:
    depths = {queue: dict.fromkeys(DEPTH_STATUSES, 0) for queue in QUEUES}
    for row in await conn.execute(_QUEUE_DEPTHS):
        depths.setdefault(str(row.queue_name), {})[str(row.status)] = int(row.n)
    return depths


@functools.cache  # fixed for the life of the process
def _versions() -> Versions:
    return Versions(
        app=__version__,
        python=platform.python_version(),
        fastapi=metadata.version("fastapi"),
        sqlalchemy=metadata.version("sqlalchemy"),
        psycopg=metadata.version("psycopg"),
        procrastinate=metadata.version("procrastinate"),
        langgraph=metadata.version("langgraph"),
    )


async def system_health(
    conn: AsyncConnection, *, readiness: ReadinessReport, now: datetime
) -> SystemHealthOut:
    """The detailed health at `now` (the caller's clock), read on `conn`, with the caller's `readiness`.

    The caller runs `check_readiness` before it opens `conn`, so it never holds two pool connections at once
    (readiness probes on a connection of its own). Read-only, apart from the gauges it refreshes:
    `ALERTS_OPEN` and `TABLE_BYTES`.
    """
    last = (await conn.execute(_LAST_RUN)).first()
    freshness = await read_run_freshness(conn)
    alerts = await refresh_alert_gauge(conn)
    disk = shutil.disk_usage(DISK_PATH)
    return SystemHealthOut(
        ready=ReadyState(ok=readiness.ok, failing=readiness.failing),
        last_run=None if last is None else RunOut.of(last),
        last_run_age_s=None if last is None else _age_s(now, last.queued_at),
        last_successful_run_age_s=_age_s(now, freshness.last_success_at),
        last_fully_successful_run_age_s=_age_s(now, freshness.last_full_success_at),
        run_age_state=run_age_state(now, freshness),
        queue_depths=await _queue_depths(conn),
        quota=await quota_health(conn),
        alerts_open=AlertsOpen(info=alerts["info"], warn=alerts["warn"], critical=alerts["critical"]),
        disk=DiskUsage(path=DISK_PATH, total_bytes=disk.total, free_bytes=disk.free),
        database_bytes=int((await conn.execute(_DATABASE_BYTES)).scalar_one()),
        table_bytes=await refresh_table_bytes(conn),
        versions=_versions(),
    )
