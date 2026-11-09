"""The run history and the run report, the score cycles, the alert list and the quota summary as the api shows
them (spec §14.2 L5034-5038, L5047, §17.3, §17.6, §7.8; plan M4 Task 10, D5, D27).

Every function reads on the caller's connection, which the route opens as one read snapshot
(`db.snapshot.read_snapshot`): none writes or locks anything, but `resolve_manual`, the user's resolution of
an alert, which runs in the route's transaction. Every statement is static SQL, every value a bound parameter.

- `runs_page(conn, *, after, status, limit)`: up to `limit` runs by `(queued_at, id)` descending (the index
  `runs_recent`), after the keyset position `after` (the last run of the page before), only those in `status`
  when it is given; each a `RunOut`, `status_reason` included.
- `run_detail(conn, run_id)`: the run, its stored report as it is, and the partial causes `partial_causes`
  finds in it. A run id that names no run raises `RunMissing` (a `LookupError`: the api's `domain_problem`
  answers it 404 `not_found`).
- `partial_causes(run_id, report)`: the list `execution.partial` that the run report builder writes
  (`graphs.collect.report.build_report`), each entry kept in its order when it is a `PartialCause`. An entry
  that is none (a cause a newer version wrote, a value of another type) is dropped, and one content-free
  warning `run_report_unknown_partial_cause` gives the run's id and how many were dropped, never a value. A
  report that is `null`, a no-op run's `{"noop": true}`, a failed run's `{}`, or one whose `execution` is no
  object or whose `execution.partial` is no list has none.
- `score_cycles_page(conn, *, after_id, limit)`: up to `limit` score cycles by id descending, after
  `after_id`.
- `alerts_page(conn, *, after_id, status, limit)`: up to `limit` alerts by id descending, after
  `after_id`, each with its `AlertStatus` (D27), which the statement derives: `resolved` once `resolved_at`
  is set (acknowledged or not), else `acknowledged` once `acknowledged_at` is, else `open`. The `status`
  filter is applied to that derived value in the same statement, so a page is cut after the filter and the
  keyset holds under it. The alert's `context` is returned as stored: alerts carry ids, counts and codes, no
  content. Each also has its delivery state (M5 Task 2, O-ops O22d): `last_notified_at`, when the alert
  dispatcher last pushed it, and `notify_failures`, how often a push of it failed for good; and `manual`,
  whether the user resolves it (`catalogue.is_manual`: its code does not resolve itself).
- `resolve_manual(conn, alert_id, *, now)` (M5 Task 2, O-ops O6): resolves the alert at `now` and returns it.
  An id that names no alert raises `AlertMissing` (a `LookupError`: 404 `not_found`); an alert whose code
  resolves itself is refused (409 `conflict_state`, `detail = "not_manual"`), whatever its status; an alert
  resolved already is returned as it is, its `resolved_at` kept.
- `quota_summary(conn)`: for every provider (`health.QUOTA_PROVIDERS`), its current window as
  `windows.current_snapshot` reads it, with the sums of the window's ledger entries: `used`, the counted
  usage `ledger.used_units` counts (`ledger.COUNTED_USAGE_SQL`); `reserved`, the units of entries in
  `reserved` or `dispatched`; `ambiguous`, of entries in `ambiguous`; `adjustments`, of `adjustment` entries.
  One grouped statement sums every provider's current window. A provider without a current window is `None`.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, JsonValue
from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.api.models import DecimalOut, UtcDatetime
from jobhunter.db.sql import q
from jobhunter.errors import Refusal
from jobhunter.graphs.collect.nodes.finalize import PARTIAL_CAUSES, PartialCause
from jobhunter.observability.catalogue import Severity, is_manual
from jobhunter.observability.health import QUOTA_PROVIDERS, RUN_COLUMNS, RunOut, RunStatus
from jobhunter.observability.logging import get_logger
from jobhunter.quota import ledger
from jobhunter.quota.windows import Provider, WindowState, current_snapshot
from jobhunter.scoring.cycles import CycleTrigger

# The values of the `score_cycles.status` CHECK (§7.6).
CycleStatus = Literal["running", "completed", "failed"]
AlertStatus = Literal["open", "acknowledged", "resolved"]

UNKNOWN_PARTIAL_CAUSE: Final = "run_report_unknown_partial_cause"
# The `detail` of the 409 that refuses to resolve an alert whose code resolves itself.
NOT_MANUAL: Final = "not_manual"
_CAUSES: Final[Mapping[str, PartialCause]] = {cause: cause for cause in PARTIAL_CAUSES}

log = get_logger("jobhunter.observability.lists")


class RunsPageOut(BaseModel):
    items: list[RunOut]
    next_cursor: str | None


class RunDetailOut(BaseModel):
    """A run, its stored report (null while none is stored) and the partial causes the report names."""

    run: RunOut
    report: dict[str, JsonValue] | None
    partial_causes: list[PartialCause]


class RunMissing(LookupError):
    """No run has the id (its message names the id only)."""

    def __init__(self, run_id: int) -> None:
        super().__init__(f"runs {run_id}: no such run")
        self.run_id = run_id


class ScoreCycleOut(BaseModel):
    """A score cycle: its trigger and status (`status_reason` says why it failed), its tuple, its admission
    size and the admitted targets per lane, the counted spend of its attempts in USD, its report (null while
    it runs) and its times in UTC."""

    id: int
    trigger: CycleTrigger
    status: CycleStatus
    status_reason: str | None
    llm_tuple_id: int
    admission_size: int
    admitted: dict[str, int]
    spent_usd: DecimalOut
    report: dict[str, JsonValue] | None
    started_at: UtcDatetime
    finished_at: UtcDatetime | None


class ScoreCyclesPageOut(BaseModel):
    items: list[ScoreCycleOut]
    next_cursor: str | None


class AlertOut(BaseModel):
    """An alert and its status: `resolved` once it is resolved, else `acknowledged` once it is acknowledged,
    else `open`. A client shows it by its `code`; `message` is an untranslated fallback. `last_notified_at` is
    when it was last pushed (null while it never was), `notify_failures` how often its push failed for good;
    `manual` says whether the user resolves it, since its code does not resolve itself."""

    id: int
    code: str
    severity: Severity
    message: str
    context: dict[str, JsonValue]
    first_at: UtcDatetime
    last_at: UtcDatetime
    occurrences: int
    acknowledged_at: UtcDatetime | None
    resolved_at: UtcDatetime | None
    status: AlertStatus
    last_notified_at: UtcDatetime | None
    notify_failures: int
    manual: bool


class AlertsPageOut(BaseModel):
    items: list[AlertOut]
    next_cursor: str | None


class AlertMissing(LookupError):
    """No alert has the id (its message names the id only)."""

    def __init__(self, alert_id: int) -> None:
        super().__init__(f"alerts {alert_id}: no such alert")
        self.alert_id = alert_id


class QuotaDetailOut(BaseModel):
    """A provider's current quota window and its ledger: `used` is the window's counted usage (the attempts
    that count and every adjustment), `reserved` the units reserved or in flight, `ambiguous` the units of
    attempts that settled ambiguous, `adjustments` the units of the adjustments; `tokens` is the window's
    bucket as stored."""

    window_start: UtcDatetime
    window_end: UtcDatetime
    state: WindowState
    limit_units: int
    used: int
    reserved: int
    ambiguous: int
    adjustments: int
    tokens: float


# `runs_recent (queued_at DESC, id DESC)` serves the order and the keyset.
_RUNS: Final = q(
    f"/* lists.runs */ SELECT {RUN_COLUMNS} FROM runs "  # noqa: S608 (constants)
    "WHERE (CAST(:status AS text) IS NULL OR status = CAST(:status AS text)) "
    "AND (CAST(:after_at AS timestamptz) IS NULL "
    "OR (queued_at, id) < (CAST(:after_at AS timestamptz), CAST(:after_id AS bigint))) "
    "ORDER BY queued_at DESC, id DESC LIMIT :limit"
)
_RUN: Final = q(f"/* lists.run */ SELECT {RUN_COLUMNS}, report FROM runs WHERE id = :id")  # noqa: S608
_CYCLES: Final = q(
    "/* lists.score_cycles */ SELECT id, trigger, status, status_reason, llm_tuple_id, admission_size, "
    "admitted, spent_usd, report, started_at, finished_at FROM score_cycles "
    "WHERE (CAST(:after AS bigint) IS NULL OR id < CAST(:after AS bigint)) ORDER BY id DESC LIMIT :limit"
)
# An alert's columns and its derived status, as `AlertOut` has them (but `manual`, which its code decides).
_ALERT_FROM: Final = (
    "SELECT a.id, a.code, a.severity, a.message, a.context, a.first_at, a.last_at, a.occurrences, "
    "a.acknowledged_at, a.resolved_at, s.status, a.last_notified_at, a.notify_failures FROM alerts a "
    "CROSS JOIN LATERAL (SELECT CASE WHEN a.resolved_at IS NOT NULL THEN 'resolved' "
    "WHEN a.acknowledged_at IS NOT NULL THEN 'acknowledged' ELSE 'open' END AS status) s"
)
_ALERTS: Final = q(
    f"/* lists.alerts */ {_ALERT_FROM} "
    "WHERE (CAST(:after AS bigint) IS NULL OR a.id < CAST(:after AS bigint)) "
    "AND (CAST(:status AS text) IS NULL OR s.status = CAST(:status AS text)) "
    "ORDER BY a.id DESC LIMIT :limit"
)
_ALERT: Final = q(f"/* lists.alert */ {_ALERT_FROM} WHERE a.id = :id")
_ALERT_CODE: Final = q("SELECT code FROM alerts WHERE id = :id")
# A concurrent resolution wins: its `resolved_at` is kept.
_RESOLVE_MANUAL: Final = q("UPDATE alerts SET resolved_at = :now WHERE id = :id AND resolved_at IS NULL")
# The current windows' entries, by `quota_ledger_window (provider, window_start, state)`.
_LEDGER_SUMS: Final = q(
    "/* lists.quota */ SELECT provider, "  # noqa: S608 (constants)
    f"COALESCE(SUM(units) FILTER (WHERE {ledger.COUNTED_USAGE_SQL}), 0) AS used, "
    "COALESCE(SUM(units) FILTER (WHERE state IN ('reserved', 'dispatched')), 0) AS reserved, "
    "COALESCE(SUM(units) FILTER (WHERE state = 'ambiguous'), 0) AS ambiguous, "
    "COALESCE(SUM(units) FILTER (WHERE kind = 'adjustment'), 0) AS adjustments FROM quota_ledger "
    "WHERE (provider, window_start) IN (SELECT provider, window_start FROM quota_windows WHERE is_current) "
    "GROUP BY provider"
)


async def runs_page(
    conn: AsyncConnection, *, after: tuple[datetime, int] | None, status: RunStatus | None, limit: int
) -> list[RunOut]:
    """Up to `limit` runs after the keyset position `after` (module docstring)."""
    after_at, after_id = (None, None) if after is None else after
    params = {"status": status, "after_at": after_at, "after_id": after_id, "limit": limit}
    return [RunOut.of(row) for row in await conn.execute(_RUNS, params)]


def partial_causes(run_id: int, report: object) -> list[PartialCause]:
    """The partial causes of run `run_id`'s `report` (module docstring)."""
    execution = report.get("execution") if isinstance(report, dict) else None
    entries = execution.get("partial") if isinstance(execution, dict) else None
    if not isinstance(entries, list):
        return []
    kept = [_CAUSES[entry] for entry in entries if isinstance(entry, str) and entry in _CAUSES]
    if dropped := len(entries) - len(kept):
        log.warning(UNKNOWN_PARTIAL_CAUSE, run_id=run_id, count=dropped)
    return kept


async def run_detail(conn: AsyncConnection, run_id: int) -> RunDetailOut:
    """Run `run_id` with its report and partial causes (module docstring); `RunMissing` for no such run."""
    row = (await conn.execute(_RUN, {"id": run_id})).first()
    if row is None:
        raise RunMissing(run_id)
    return RunDetailOut(
        run=RunOut.of(row), report=row.report, partial_causes=partial_causes(run_id, row.report)
    )


async def score_cycles_page(
    conn: AsyncConnection, *, after_id: int | None, limit: int
) -> list[ScoreCycleOut]:
    """Up to `limit` score cycles after `after_id` (module docstring)."""
    rows = await conn.execute(_CYCLES, {"after": after_id, "limit": limit})
    return [ScoreCycleOut.model_validate(row, from_attributes=True) for row in rows]


def _alert_out(row: Row[Any]) -> AlertOut:
    return AlertOut.model_validate({**row._mapping, "manual": is_manual(row.code)})


async def alerts_page(
    conn: AsyncConnection, *, after_id: int | None, status: AlertStatus | None, limit: int
) -> list[AlertOut]:
    """Up to `limit` alerts after `after_id`, of `status` when it is given (module docstring)."""
    rows = await conn.execute(_ALERTS, {"after": after_id, "status": status, "limit": limit})
    return [_alert_out(row) for row in rows]


async def resolve_manual(conn: AsyncConnection, alert_id: int, *, now: datetime) -> AlertOut:
    """Resolve the alert `alert_id` at `now` and return it (module docstring): `AlertMissing` for no such
    alert, a 409 `not_manual` refusal for one whose code resolves itself, one resolved already as it is."""
    code = (await conn.execute(_ALERT_CODE, {"id": alert_id})).scalar_one_or_none()
    if code is None:
        raise AlertMissing(alert_id)
    if not is_manual(code):
        raise Refusal(
            409, "conflict_state", "This alert resolves itself when its condition clears", detail=NOT_MANUAL
        )
    await conn.execute(_RESOLVE_MANUAL, {"id": alert_id, "now": now})
    return _alert_out((await conn.execute(_ALERT, {"id": alert_id})).one())


async def quota_summary(conn: AsyncConnection) -> dict[Provider, QuotaDetailOut | None]:
    """Every provider's current window with its ledger sums, `None` without one (module docstring)."""
    windows = {provider: await current_snapshot(conn, provider) for provider in QUOTA_PROVIDERS}
    sums = {
        row.provider: (int(row.used), int(row.reserved), int(row.ambiguous), int(row.adjustments))
        for row in await conn.execute(_LEDGER_SUMS, ledger.counted_params())
    }
    out: dict[Provider, QuotaDetailOut | None] = {}
    for provider, w in windows.items():
        if w is None:
            out[provider] = None
            continue
        used, reserved, ambiguous, adjustments = sums.get(provider, (0, 0, 0, 0))  # no row: no entry yet
        out[provider] = QuotaDetailOut(
            window_start=w.window_start,
            window_end=w.window_end,
            state=w.state,
            limit_units=w.limit_units,
            used=used,
            reserved=reserved,
            ambiguous=ambiguous,
            adjustments=adjustments,
            tokens=float(w.tokens),
        )
    return out
