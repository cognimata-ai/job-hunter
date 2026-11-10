"""Readiness (spec §17.5): the serving prerequisites of the api; the database's are checked on one connection.

`check_readiness` answers; it never raises (ruling T14-e). Its probes, in this order:
- `db`: a connection and `SELECT 1` within `DB_TIMEOUT_S`. When that fails, nothing else of the database is
  probed.
- `migrations`: `alembic_version` holds exactly the code head.
- `worker_heartbeat`: the heartbeat row exists and is at most `HEARTBEAT_STALE_S` old
  (`heartbeats.heartbeat_fresh` over `heartbeats.HEARTBEAT_AGE_SQL`, which the api's heartbeat watch shares).
- `embedder` (Task 22): the embedder's `/health` within `embeddings.HEALTH_TIMEOUT_S` is `ok`, with its
  checksum verified (`EmbedderClient.health`: an embedder this code can use). It runs beside the database's
  probes, so one slow component does not add to the other's time, and it is reported after them. A caller
  without an embedder client (`embedder=None`) skips it.

Each schema probe runs in a savepoint of its own and its database error is caught outside that block, after
the savepoint rolled back, so a failed probe (e.g. `42P01` on a database without the schema) never aborts the
transaction the next probe uses. A probe whose error invalidated the connection means the database went away
mid-check: the answer is then `db`. After `SELECT 1`, the transaction sets a `statement_timeout`, so a probe
that waits behind a lock (a migration holding `ACCESS EXCLUSIVE`) fails by itself instead of holding
`/readyz` and a pool connection. The server enforces that timeout; the client bounds the whole probe sequence
too (`PROBES_TIMEOUT_S`), for a database that stops answering while its socket stays open: the answer is
then `db`.

Every failed probe logs `readiness_probe_failed` with `probe`, `sqlstate` and `error_class` (`None` when no
error was raised; `reason` says what was wrong then: for the embedder `checksum_failed`, `unavailable` when it
gave no usable answer, `unusable` for another model or dimension). Never an exception message.

`system_health` is the detailed health of `GET /api/system/health` (§17.5): readiness plus the run ages and
their `run_age_state`, the queue depths, the open alerts (it refreshes `ALERTS_OPEN`), each provider's quota
window at `now` (`quota_health`, the header status's view), the LLM spend of the month (`llm_spend`, plan
Task 16), the score lane (`scoring_health`, M3 Task 18), disk and database sizes, the versions, the
embedder's health (with its model id) as the readiness check read it and the LangSmith tracing state the
last worker published at its start (`tracing_state`, M5 Task 6, Q17: the api has no LangSmith configuration
of its own; `null` until a worker published one, and kept when its heartbeat goes stale). Run ages are
reported, never readiness gates. `RunOut` is the api's view of a run row, shared with `POST /api/runs` and the
run lists (`observability.lists`); every select of one either names `RUN_COLUMNS`, its fields, or fills a
`worker.runs.RunRow`, whose required `status_reason` the validation of `RunOut` checks too.

**The score lane** (`scoring`, §17.5 L5552 "oldest pending target age", M3 Task 18), read without a lock (a
save holding `active_config FOR UPDATE` never holds the page): the running cycle's id, the pause guard's
reason (`scoring.cycles.pause_reason` over a plain read of the active configuration, in the process's LLM
mode), the age of the active tuple's oldest `pending` target by its `created_at` (`null` without one), its
`pending` and `in_batch` targets (`cycles.target_figures`) and the quarantined batch spend (the reserved
spend of the `submit_ambiguous` submissions, `scoring.batches.quarantined_usd`, Ruling P31).

**One freshness rule** (ruling T17-g): `read_run_freshness` is the one query of the run figures and
`run_age_state` the one rule over them. System health reports that state, and the worker's `stale_data`
reconciler raises `schedule.stale_data` exactly while it is `critical` and the schedule is enabled (a
disabled schedule runs nothing on purpose, M0 Carry-forward 2), so the page and the alert never disagree
while a collection is due.
"""

import asyncio
import functools
import platform
import shutil
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from importlib import metadata
from typing import Any, Final, Literal

import sqlalchemy as sa
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.elements import TextClause

from jobhunter import __version__
from jobhunter.api.models import DecimalOut, usd_micro
from jobhunter.db.sql import pg_error, q
from jobhunter.embeddings import NO_HEALTH, EmbedderClient, EmbedderHealth
from jobhunter.heartbeats import HEARTBEAT_AGE_SQL, heartbeat_fresh
from jobhunter.llm import budget
from jobhunter.llm.budget import BudgetParams, BudgetRow
from jobhunter.llm.spend_ledger import read_month_or_new
from jobhunter.observability.alerts import refresh_alert_gauge
from jobhunter.observability.logging import get_logger
from jobhunter.observability.metrics import refresh_table_bytes
from jobhunter.quota.dryrun import live_windows
from jobhunter.quota.ledger import used_units
from jobhunter.quota.windows import Provider, Window, WindowState
from jobhunter.run_settings import load_run_settings
from jobhunter.scoring import batches, cycles
from jobhunter.scoring.cycles import PauseReason
from jobhunter.worker.tasks import QUEUES

ReadinessCode = Literal["db", "migrations", "worker_heartbeat", "embedder"]

DB_TIMEOUT_S: Final = 2.0
# The client-side bound of the schema probes together; each one also has the server-side statement timeout.
PROBES_TIMEOUT_S: Final = 2 * DB_TIMEOUT_S
PROBE_STATEMENT_TIMEOUT: Final = "2s"

_PING: Final = q("SELECT 1")
# `is_local`: for this transaction only, so the pooled connection gets its default back with the rollback.
_SET_STATEMENT_TIMEOUT: Final = q("SELECT set_config('statement_timeout', :timeout, true)")
_MIGRATION_VERSIONS: Final = q("SELECT version_num FROM alembic_version")
_HEARTBEAT_AGE: Final = q(HEARTBEAT_AGE_SQL)
# What the worker published at its start (`observability.langsmith.publish_tracing_state`); before that, no
# row or NULL.
_TRACING_STATE: Final = q("SELECT tracing FROM worker_heartbeat WHERE id = 1")

log = get_logger("jobhunter.observability.health")


@dataclass(frozen=True)
class ReadinessReport:
    """The verdict; `embedder` is the embedder's health as its probe read it (`None`: not probed)."""

    ok: bool
    failing: list[ReadinessCode]
    embedder: EmbedderHealth | None = None


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

    # The database clock, which also wrote `heartbeat_at`: the verdict `jobhunter worker-health` reaches too,
    # and the api's heartbeat watch (`heartbeats.heartbeat_fresh`, the one rule).
    heartbeat = await _rows(conn, "worker_heartbeat", _HEARTBEAT_AGE)
    if heartbeat is None:
        failing.append("worker_heartbeat")
    elif not heartbeat or not heartbeat_fresh(heartbeat[0][0]):
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


async def _database_probes(engine: AsyncEngine, code_head: str) -> list[ReadinessCode]:
    try:
        return await _probe_all(engine, code_head)
    except Exception as exc:  # noqa: BLE001 - readiness is an answer, never an error (T14-e); the 503 is the outcome
        _log_failed("db", exc)
        return ["db"]


def _embedder_ready(health: EmbedderHealth) -> bool:
    return health.ok and health.checksum_ok is True


async def _probe_embedder(embedder: EmbedderClient) -> EmbedderHealth:
    try:
        health = await embedder.health()
    except Exception as exc:  # noqa: BLE001 - `health()` never raises; readiness stays an answer (T14-e)
        _log_failed("embedder", exc)
        return NO_HEALTH
    if not _embedder_ready(health):
        if health.checksum_ok is False:
            reason = "checksum_failed"
        elif health.model_id is None:
            reason = "unavailable"
        else:
            reason = "unusable"
        _log_failed("embedder", reason=reason)
    return health


async def check_readiness(
    engine: AsyncEngine, *, code_head: str, now: datetime, embedder: EmbedderClient | None
) -> ReadinessReport:
    """Probe the serving prerequisites; `failing` lists the failed ones in the order `db`, `migrations`,
    `worker_heartbeat`, `embedder` (that last probe only with an `embedder` client).

    `now` is the caller's clock (system health passes the one it uses for everything else). No probe reads it:
    the heartbeat age is measured on the database clock that wrote the heartbeat.
    """
    async with asyncio.TaskGroup() as probes:
        database = probes.create_task(_database_probes(engine, code_head))
        embedder_probe = None if embedder is None else probes.create_task(_probe_embedder(embedder))
    failing = database.result()
    health = None if embedder_probe is None else embedder_probe.result()
    if health is not None and not _embedder_ready(health):
        failing.append("embedder")
    return ReadinessReport(ok=not failing, failing=failing, embedder=health)


RunAgeState = Literal["ok", "warn", "critical", "grace", "none"]

# The last successful run's age, in schedule intervals, past which it is `warn` and `critical` (§17.5). The
# grace lasts `RUN_AGE_CRITICAL_INTERVALS` intervals after the first settings version, as for the
# `schedule.stale_data` alert (`worker.runs.reconcile_stale_data`).
RUN_AGE_WARN_INTERVALS: Final = 1.5
RUN_AGE_CRITICAL_INTERVALS: Final = 2
DISK_PATH: Final = "/"
# The job statuses every known queue reports, 0 included; any other status is reported when it has jobs.
DEPTH_STATUSES: Final = ("todo", "doing")

# `runs_last_success` serves both maxima (index-only, backwards), so the header status's 60 s poll, the system
# health and the `stale_data` reconciler never scan `runs`.
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
_ACTIVE_VERSION: Final = q("SELECT settings_version_id FROM active_config WHERE id = 1")
_ACTIVE_PAYLOAD: Final = q(
    "SELECT v.payload FROM active_config a JOIN settings_versions v ON v.id = a.settings_version_id "
    "WHERE a.id = 1"
)


# The values of the `runs.status` CHECK (§7.6), in its order.
RunStatus = Literal[
    "queued",
    "running",
    "succeeded",
    "partial",
    "failed",
    "failed_invariant",
    "cancelled",
    "missed",
    "skipped_overlap",
]


class RunOut(BaseModel):
    """A run as the api shows it (`runs` row, §7.6); its times in UTC. `status_reason` is why it has its
    status, as a code (the first partial cause, the failed invariant, `bug`, `resume_exhausted`,
    `orphaned_queue`, …), null when it has none."""

    id: int
    kind: str
    trigger: str
    status: RunStatus
    status_reason: str | None
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


# The `runs` columns a `RunOut` is built from, `status_reason` included (D5): every select of one names them,
# except those that fill a `worker.runs.RunRow`, whose required `status_reason` `RunOut` validates too.
RUN_COLUMNS: Final = ", ".join(RunOut.model_fields)
# The newest run, with its report (the header status reads its partial causes; a `RunOut` ignores it):
# `runs_recent` serves it.
LAST_RUN: Final = q(
    f"SELECT {RUN_COLUMNS}, report FROM runs ORDER BY queued_at DESC, id DESC LIMIT 1"  # noqa: S608
)


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


class EmbedderOut(BaseModel):
    """The embedder's health as readiness read it: `ok` when this code can use it, the model it serves and
    whether its checksum was verified (`null`: no usable answer)."""

    ok: bool
    model_id: str | None
    checksum_ok: bool | None


class Versions(BaseModel):
    app: str
    python: str
    fastapi: str
    sqlalchemy: str
    psycopg: str
    procrastinate: str
    langgraph: str


class QuotaOut(BaseModel):
    """A provider's quota window at the time of the request (§7.8): counted usage against its limit, and its
    bucket. A stored window whose reset has passed shows its successor, which starts unspent; a provider with
    no stored window shows the window its first request would open."""

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

    @classmethod
    def of(cls, w: Window, used: int) -> "QuotaOut":
        """The view of window `w` with its counted usage `used`."""
        return cls(
            window_start=w.window_start,
            window_end=w.window_end,
            state=w.state,
            limit_units=w.limit_units,
            used_units=used,
            remaining_units=max(0, w.limit_units - used),
            tokens=float(w.tokens),
        )


QUOTA_PROVIDERS: Final[tuple[Provider, ...]] = ("jsearch", "serpapi", "adzuna")


async def quota_views(
    conn: AsyncConnection, windows: Mapping[Provider, Window | None]
) -> dict[Provider, QuotaOut | None]:
    """Each quota provider's view of its window in `windows` (`quota.dryrun.live_windows`: the windows at
    `now`) with the window's counted usage in the ledger; `None` for a provider without one."""
    out: dict[Provider, QuotaOut | None] = {}
    for provider in QUOTA_PROVIDERS:
        w = windows.get(provider)
        out[provider] = (
            None if w is None else QuotaOut.of(w, await used_units(conn, provider, w.window_start))
        )
    return out


async def quota_health(conn: AsyncConnection, *, now: datetime) -> dict[Provider, QuotaOut | None]:
    """Each quota provider's window at `now` with its counted usage, the header status's own view (Ruling
    T11-f): the stored current window rolled forward in memory under the active run settings
    (`quota.dryrun.live_windows`), so a reset that has passed shows the successor before any ledger write
    rolls it, and a provider without a stored window shows the one its first request would bootstrap. `None`
    for SerpApi before its first account read, and for every provider when nothing is seeded."""
    version = (await conn.execute(_ACTIVE_VERSION)).scalar_one_or_none()
    if version is None:
        return dict.fromkeys(QUOTA_PROVIDERS)
    run = await load_run_settings(conn, int(version))
    return await quota_views(conn, await live_windows(conn, run, now))


# §17.5 "LLM spend" as M0 Deviation 6 reads it.
class LlmSpendOut(BaseModel):
    """The month's LLM spend (§17.5 "LLM spend"): the counted spend, the cap, the scoring headroom (§6.9.4)
    and the USD bucket accrued to now, from the month row as `read_month` gives it; a month without a row is
    the one a reservation would create, with the settings' cap and nothing counted."""

    month: date
    counted: DecimalOut
    cap: DecimalOut
    headroom: DecimalOut
    bucket_usd: DecimalOut

    @classmethod
    def of(cls, row: BudgetRow, p: BudgetParams) -> "LlmSpendOut":
        return cls(
            month=row.month,
            counted=usd_micro(row.counted_usd),
            cap=row.cap_usd,
            headroom=usd_micro(budget.headroom(row, "scoring", p)),
            bucket_usd=usd_micro(row.bucket_usd),
        )


async def active_budget_params(conn: AsyncConnection) -> BudgetParams | None:
    """The budget params of the active settings version, read with a plain `SELECT` (no lock, unlike
    `spend_ledger.current_params`); `None` when nothing is seeded."""
    payload = (await conn.execute(_ACTIVE_PAYLOAD)).scalar_one_or_none()
    return BudgetParams.from_payload(payload) if isinstance(payload, dict) else None


async def llm_spend(conn: AsyncConnection, *, now: datetime) -> LlmSpendOut | None:
    """The LLM spend of the month of `now`, read without a lock; `None` when nothing is seeded."""
    p = await active_budget_params(conn)
    if p is None:
        return None
    return LlmSpendOut.of(await read_month_or_new(conn, budget.month_of(now), p, now=now), p)


# What each figure reads: the module docstring, The score lane.
class ScoringHealthOut(BaseModel):
    """The score lane: the running cycle, the pause reason, the oldest pending target's age, the pending and
    batched targets and the quarantined batch spend. Ages are seconds at the caller's `now`, the amount in
    USD."""

    running_cycle_id: int | None
    pause_reason: PauseReason | None
    oldest_pending_target_age_s: float | None
    pending_targets: int
    in_batch_targets: int
    quarantined_batch_usd: DecimalOut


async def scoring_health(conn: AsyncConnection, *, now: datetime, fake_llm: bool) -> ScoringHealthOut:
    """The score lane's section at `now`, read without a lock (module docstring); with nothing seeded, no
    pause is reported."""
    seeded = await active_budget_params(conn) is not None
    config = await cycles.scoring_config(conn, lock=False) if seeded else None
    reason = (
        None if config is None else await cycles.pause_reason(conn, fake_llm=fake_llm, now=now, config=config)
    )
    figures = await cycles.target_figures(conn)
    return ScoringHealthOut(
        running_cycle_id=await cycles.running_cycle_id(conn),
        pause_reason=reason,
        oldest_pending_target_age_s=_age_s(now, figures.oldest_pending_at),
        pending_targets=figures.by_state.get("pending", 0),
        in_batch_targets=figures.by_state.get("in_batch", 0),
        quarantined_batch_usd=usd_micro(await batches.quarantined_usd(conn)),
    )


class TracingStateOut(BaseModel):
    """The tracing state the worker published at its start: whether it sends traces to LangSmith (inputs and
    outputs hidden), and the region it sends them to (`eu`; `null` when it does not trace)."""

    tracing: bool
    region: Literal["eu"] | None


async def tracing_state(conn: AsyncConnection) -> TracingStateOut | None:
    """The last published tracing state, `None` before a worker published one (module docstring)."""
    stored = (await conn.execute(_TRACING_STATE)).scalar_one_or_none()
    return None if stored is None else TracingStateOut.model_validate(stored)


# Its sections come from `quota_health`, `llm_spend`, `scoring_health`, `metrics.refresh_table_bytes`,
# `tracing_state` and the caller's readiness report (`EmbedderOut`).
class SystemHealthOut(BaseModel):
    """The detailed health (§17.5). Ages are seconds at the caller's `now`.

    - `last_run`: the newest run by `(queued_at, id)`; `last_run_age_s` is measured from its `queued_at`.
    - `last_successful_run_age_s`: since the latest `finished_at` of a `succeeded` or `partial` run;
      `last_fully_successful_run_age_s`: of a `succeeded` one only.
    - `run_age_state`: see `run_age_state`.
    - `queue_depths`: procrastinate jobs by queue and status.
    - `quota`: each quota provider's window at the time of the request, as the header status shows it;
      `null` for SerpApi before its first account read.
    - `llm`: the month's LLM spend, `null` when nothing is seeded.
    - `scoring`: the score lane.
    - `table_bytes`: the size of every table of the `public` schema, in bytes.
    - `embedder`: the embedder's health as the readiness check read it.
    - `langsmith`: the tracing state the worker published at its start, `null` until one did; it stays when
      the worker's heartbeat goes stale.
    """

    ready: ReadyState
    last_run: RunOut | None
    last_run_age_s: float | None
    last_successful_run_age_s: float | None
    last_fully_successful_run_age_s: float | None
    run_age_state: RunAgeState
    queue_depths: dict[str, dict[str, int]]
    quota: dict[Provider, QuotaOut | None]
    llm: LlmSpendOut | None
    scoring: ScoringHealthOut
    alerts_open: AlertsOpen
    disk: DiskUsage
    database_bytes: int
    table_bytes: dict[str, int]
    versions: Versions
    embedder: EmbedderOut
    langsmith: TracingStateOut | None


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
    conn: AsyncConnection, *, readiness: ReadinessReport, now: datetime, fake_llm: bool
) -> SystemHealthOut:
    """The detailed health at `now` (the caller's clock), read on `conn`, with the caller's `readiness`;
    `fake_llm` is the process's LLM mode, which the score lane's pause guard reads.

    The caller runs `check_readiness` before it opens `conn`, so it never holds two pool connections at once
    (readiness probes on a connection of its own), and the embedder is asked once, by that check. Read-only,
    apart from the gauges it refreshes: `ALERTS_OPEN` and `TABLE_BYTES`.
    """
    last = (await conn.execute(LAST_RUN)).first()
    freshness = await read_run_freshness(conn)
    alerts = await refresh_alert_gauge(conn)
    disk = shutil.disk_usage(DISK_PATH)
    embedder = NO_HEALTH if readiness.embedder is None else readiness.embedder
    return SystemHealthOut(
        ready=ReadyState(ok=readiness.ok, failing=readiness.failing),
        last_run=None if last is None else RunOut.of(last),
        last_run_age_s=None if last is None else _age_s(now, last.queued_at),
        last_successful_run_age_s=_age_s(now, freshness.last_success_at),
        last_fully_successful_run_age_s=_age_s(now, freshness.last_full_success_at),
        run_age_state=run_age_state(now, freshness),
        queue_depths=await _queue_depths(conn),
        quota=await quota_health(conn, now=now),
        llm=await llm_spend(conn, now=now),
        scoring=await scoring_health(conn, now=now, fake_llm=fake_llm),
        alerts_open=AlertsOpen(info=alerts["info"], warn=alerts["warn"], critical=alerts["critical"]),
        disk=DiskUsage(path=DISK_PATH, total_bytes=disk.total, free_bytes=disk.free),
        database_bytes=int((await conn.execute(_DATABASE_BYTES)).scalar_one()),
        table_bytes=await refresh_table_bytes(conn),
        versions=_versions(),
        embedder=EmbedderOut(ok=embedder.ok, model_id=embedder.model_id, checksum_ok=embedder.checksum_ok),
        langsmith=await tracing_state(conn),
    )
