"""The header strip's composite (spec §15.1; plan M4 Task 11, D4-D6, D11, D13-D16): `GET
/api/system/status`, which every open page polls every 60 s, so every read is an indexed lookup or a bounded
count, and every banner is computed here, never in the client. The two reads that would otherwise grow with
history have their indexes in 0009 (Ruling T11-d): the freshness rule's maxima (`runs_last_success`) and the
newest ended apply of the active scope (`operations_dedup_newest`).

`system_status(conn, *, now, fake_llm)` reads on the caller's connection, which the route opens as one read
snapshot (`db.snapshot.read_snapshot`): plain reads, no lock, no readiness probe (the embedder is never asked)
and no table sizes (D5). What each field reads:
- `last_run`: the newest run by `(queued_at, id)` (`runs_recent`), the system health's `last_run`; `age_s`
  is measured from its `queued_at`, as the health measures it; `partial_causes` are those its report names
  (`lists.partial_causes`, the run detail's own extractor).
- `active_run_id`: the collection `queued` or `running` (`runs_one_active_collect`).
- `next_slots`: the next 3 slots of `schedule_state` after `now` (`next_slots`), none while it is disabled.
- `quota`: each provider's window at `now` (Ruling T11-c): `quota.dryrun.live_windows`, the stored current
  window rolled forward in memory as a run would roll it, computed once and judged alike by `quota`,
  `paid_search` and `run_now`. A stored window that has ended is replaced by its successor, which starts
  unspent, so a past reset never pauses paid search; a provider without a stored window shows the window
  its first reservation would bootstrap (JSearch and Adzuna; SerpApi has none before an account read).
  Each shows its counted usage in the ledger (`health.quota_views`: `ledger.used_units` of the window shown),
  its limit, its reset (the window's end) and its state; `/api/system/health` shows the same view
  (`health.quota_health`, Ruling T11-f).
- `llm`: `null` when nothing is seeded (`health.active_budget_params`); otherwise the month's counted spend
  and cap (`health.LlmSpendOut` of the month row `spend_ledger.read_month_or_new` gives, as `llm_spend`
  builds it) and the `pace` of the scoring lane's counted spend against the scoring share `A x (1 -
  reserve_frac)` of the row's cap and buffer.
- `coverage_gaps_open`: the open coverage gaps (`coverage_open_gaps`).
- `pending_targets`: the active tuple's `pending` and `budget_deferred` targets (`score_targets_admission`).
- `alerts`: the unresolved and unacknowledged alerts, and the critical ones among them (D4).
- `rescore` (D15): the active tuple's `active` bulk operation (`bulk_operations_one_active`), `total` its
  targets and `done` those no longer `pending`, `budget_deferred`, `in_progress` or `in_batch`
  (`score_targets_bulk`), `creating` while its `create_targets_for_tuple` operation is `pending` or
  `running` (`bulk.creation_live`); `null` without one.
- `paid_search` (D14): `paid_search` over the JSearch and SerpApi windows at `now`, the settings' SerpApi
  switch and the health of SerpApi's default instance as the run's planner judges it
  (`breaker.instance_availability`: healthy unless `blocked`, so a breaker past its half-open time, which
  admits a probe, is healthy).
- `run_now` (D16): `quota.dryrun.run_now_units` with the active run settings on those windows: an estimate
  (the even pace), 0 for a disabled provider and for SerpApi without a validated window; JSearch without a
  stored window shows the units of the window its first run would bootstrap.
- `scope` (D11): the pending scope, the progress of its live `apply_scope` operation (`null` without a live
  one; a count it has not reported yet is 0), and `scope_apply.scope_applied`.
- `banners`: `compute_banners` over the facts above, the score lane's pause reason (`cycles.pause_reason`
  over a plain read of the active configuration, in the process's LLM mode, as the system health reads it;
  none with nothing seeded) and whether a deletion manifest is `completed` but not yet `shipped`
  (`deletion_manifests_open`; M5 Task 18).

**The banners** (`compute_banners`), built in this table's order, then sorted critical, warn, info, the
table's order kept within a severity:

| code | severity | condition | params |
|---|---|---|---|
| `stale_data` | critical | `run_age_state == "critical"` | `{}` |
| `cap_reached` | critical | the pause reason is `cap_reached` (D6) | `{}` |
| `reservation_bound_violated` | critical | the pause reason is `reservation_bound_violated` (D4) | `{}` |
| `scoring_paused` | critical, warn for `global_fence` | any other pause reason (D6) | `{reason}` |
| `critical_alerts` | critical | an unresolved, unacknowledged critical alert (D4) | `{count}` |
| `paid_search_paused` | warn | `paid_search.paused` | `{until, reason}` |
| `scope_not_applied` | warn | nothing pending and the scope not applied (D11) | `{}` |
| `deletion_unshipped` | warn | a deletion manifest `completed` and not `shipped` (§15.1, §19.5) | `{}` |
| `applying_settings` | info | a pending scope | `{done, total}`, or `{failed: true}` (below) |
| `rescoring` | info | a re-score with `done < total`, or still `creating` | `{done, total}` |

`applying_settings` sums the jobs and the published results of a live apply; a pending scope without a live
apply (its apply failed for good, until the reconciler clears the pointer) gives `{failed: true}`. Every param
is a JSON value: an instant as an ISO 8601 UTC string, an amount as a fixed-point string.

**Pace** (D13): `ok` while the month's counted scoring spend is at most the scoring share times the elapsed
fraction of `now`'s UTC month, compared exactly (in microseconds, no rounding), else `ahead`.

**Paid search** (D14): paused when JSearch is exhausted (its window `exhausted_until_reset`, or its counted
usage at its limit) and SerpApi is disabled (`serpapi_disabled`), has no window, an unvalidated one or an
unhealthy instance (`serpapi_unavailable`), or is exhausted itself (`serpapi_exhausted`), the first that
holds; `until` is JSearch's reset, or SerpApi's when only its exhaustion holds and it resets first.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal

from pydantic import BaseModel, Field, JsonValue
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.api.models import DecimalOut, UtcDatetime
from jobhunter.db.seed import latest_slot_at_or_before
from jobhunter.db.sql import q
from jobhunter.graphs.collect.nodes.finalize import PartialCause
from jobhunter.llm import budget, bulk
from jobhunter.llm.spend_ledger import read_month_or_new
from jobhunter.observability import lists
from jobhunter.observability.health import (
    LAST_RUN,
    LlmSpendOut,
    QuotaOut,
    RunAgeState,
    RunOut,
    active_budget_params,
    quota_views,
    read_run_freshness,
    run_age_state,
)
from jobhunter.quota.dryrun import live_windows, run_now_units
from jobhunter.quota.windows import Provider, WindowState
from jobhunter.run_settings import load_run_settings
from jobhunter.scoring import cycles
from jobhunter.scoring.cycles import PauseReason
from jobhunter.settings import scope_apply
from jobhunter.sources.breaker import instance_availability
from jobhunter.types import JSONValue

BannerCode = Literal[
    "stale_data",
    "cap_reached",
    "reservation_bound_violated",
    "scoring_paused",
    "critical_alerts",
    "paid_search_paused",
    "scope_not_applied",
    "deletion_unshipped",
    "applying_settings",
    "rescoring",
]
BannerSeverity = Literal["critical", "warn", "info"]
Pace = Literal["ok", "ahead"]
PaidSearchReason = Literal["serpapi_disabled", "serpapi_unavailable", "serpapi_exhausted"]

NEXT_SLOTS: Final = 3
_SEVERITY_RANK: Final[dict[BannerSeverity, int]] = {"critical": 0, "warn": 1, "info": 2}
_MICROSECOND: Final = timedelta(microseconds=1)
# The target states that keep a target of a bulk operation unscored (the unscored bucket).
_UNSCORED_STATES: Final = ("pending", "budget_deferred", "in_progress", "in_batch")

# `runs_one_active_collect` allows one.
_ACTIVE_RUN: Final = q(
    "/* status.active_run */ SELECT id FROM runs WHERE kind = 'collect' AND status IN ('queued', 'running')"
)
_SCHEDULE: Final = q(
    "/* status.schedule */ SELECT anchor_utc, interval_s, enabled FROM schedule_state WHERE id = 1"
)
# `coverage_open_gaps` (a partial index of exactly these rows).
_OPEN_GAPS: Final = q(
    "/* status.open_gaps */ SELECT count(*) FROM coverage_intervals "
    "WHERE kind = 'gap' AND gap_status = 'open'"
)
# `score_targets_admission (llm_tuple_id, state, …) WHERE state IN ('pending', 'budget_deferred')`.
_PENDING_TARGETS: Final = q(
    "/* status.pending_targets */ SELECT count(*) FROM score_targets t "
    "JOIN active_config a ON a.llm_tuple_id = t.llm_tuple_id "
    "WHERE a.id = 1 AND t.state IN ('pending', 'budget_deferred')"
)
# `deletion_manifests_open (status, completed_at) WHERE status <> 'shipped'`.
_UNSHIPPED: Final = q(
    "/* status.unshipped */ SELECT EXISTS (SELECT 1 FROM deletion_manifests WHERE status = 'completed')"
)
_UNACKED_ALERTS: Final = q(
    "/* status.alerts */ SELECT count(*) FILTER (WHERE severity = 'critical') AS critical, count(*) AS total "
    "FROM alerts WHERE resolved_at IS NULL AND acknowledged_at IS NULL"
)
# `bulk_operations_one_active`: at most one, and only the active tuple's counts.
_ACTIVE_BULK: Final = q(
    "/* status.active_bulk */ SELECT b.id FROM bulk_operations b "
    "JOIN active_config a ON a.llm_tuple_id = b.llm_tuple_id WHERE a.id = 1 AND b.state = 'active'"
)
# `score_targets_bulk (bulk_operation_id, state)`.
_BULK_TARGETS: Final = q(
    "/* status.bulk_targets */ SELECT count(*) AS total, "
    "count(*) FILTER (WHERE state <> ALL(CAST(:unscored AS text[]))) AS done "
    "FROM score_targets WHERE bulk_operation_id = :b"
)
# The unique `(source_id, instance_key)`.
_SERPAPI_INSTANCE: Final = q(
    "/* status.serpapi_instance */ SELECT id FROM source_instances "
    "WHERE source_id = 'serpapi' AND instance_key = 'default'"
)
# The pending scope and its live apply (`operations_live_dedup`), as `GET /api/settings` reads them.
_PENDING_SCOPE: Final = q(
    "/* status.pending_scope */ SELECT a.pending_scope_version_id, o.id AS operation_id, o.progress "
    "FROM active_config a LEFT JOIN operations o ON a.pending_scope_version_id IS NOT NULL "
    "AND o.kind = :kind AND o.dedup_key = :kind || ':' || a.pending_scope_version_id "
    "AND o.status IN ('pending', 'running') WHERE a.id = 1"
)


class LastRunOut(BaseModel):
    """The newest run, its age in seconds since it was queued, and the causes of a partial run."""

    run: RunOut
    age_s: float
    partial_causes: list[PartialCause]


class HeaderQuotaOut(BaseModel):
    """A provider's current quota window: the counted usage against its limit, its reset and its state."""

    used: int
    limit: int
    reset_at: UtcDatetime
    state: WindowState


class HeaderLlmOut(BaseModel):
    """The month's counted LLM spend against the cap, in USD, and whether the month's counted scoring spend is
    ahead of the even pace of the scoring share over the month."""

    counted: DecimalOut
    cap: DecimalOut
    pace: Pace


class UnackedAlertsOut(BaseModel):
    """The alerts that are unresolved and unacknowledged, and the critical ones among them."""

    critical_unacked: int
    total_unacked: int


class RescoreOut(BaseModel):
    """The re-score of the active scoring tuple: of its targets (`total`), those that left the unscored
    states (`done`); `creating` while its targets are still being created."""

    bulk_operation_id: int
    done: int
    total: int
    creating: bool


class PaidSearchOut(BaseModel):
    """Whether paid search is paused (JSearch exhausted and SerpApi unable to fall back), until when and
    why."""

    paused: bool
    until: UtcDatetime | None
    reason: PaidSearchReason | None


class RunNowUnitsOut(BaseModel):
    """What a run with paid sources may spend, in whole units: an estimate (the even pace)."""

    jsearch_units: int
    serpapi_fallback_units: int


class ScopeProgressOut(BaseModel):
    """A scope apply's progress in both of its phases: the jobs (`done` of `total`) and the published results
    (`det_done` of `det_total`)."""

    done: int
    total: int
    det_done: int
    det_total: int


class ScopeStatusOut(BaseModel):
    """The scope being applied, if any, with its live apply's progress (null while no apply is live), and
    whether the active settings version's scope is the applied one."""

    pending_version_id: int | None
    progress: ScopeProgressOut | None
    applied: bool


class BannerOut(BaseModel):
    """A banner: a client shows it by its `code`, with `params` (JSON values; instants as ISO 8601 UTC
    strings)."""

    code: BannerCode
    severity: BannerSeverity
    params: dict[str, JsonValue]


class StatusOut(BaseModel):
    """The header strip: the last and the active run, the next slots, the quota windows, the month's LLM
    spend, the open coverage gaps, the pending targets, the unacknowledged alerts, the re-score, the
    paid-search pause, the run-now estimate, the scope's apply and the banners, critical first."""

    last_run: LastRunOut | None
    active_run_id: int | None
    next_slots: list[UtcDatetime] = Field(
        description="The schedule's next 3 slots; empty while it is disabled"
    )
    quota: dict[Provider, HeaderQuotaOut | None]
    llm: HeaderLlmOut | None
    coverage_gaps_open: int
    pending_targets: int
    alerts: UnackedAlertsOut
    rescore: RescoreOut | None
    paid_search: PaidSearchOut
    run_now: RunNowUnitsOut
    scope: ScopeStatusOut
    banners: list[BannerOut]


@dataclass(frozen=True)
class BannerFacts:
    """What the banners are computed from (module docstring, The banners)."""

    run_age_state: RunAgeState
    pause_reason: PauseReason | None
    critical_unacked: int
    paid_search: PaidSearchOut
    scope: ScopeStatusOut
    rescore: RescoreOut | None
    deletion_unshipped: bool = False


# ---- the pure parts ---------------------------------------------------------------------------------------


def pace(*, scoring_counted_usd: Decimal, scoring_share_usd: Decimal, now: datetime) -> Pace:
    """`ok` while the counted scoring spend is at most the scoring share times the elapsed fraction of `now`'s
    UTC month, else `ahead` (module docstring)."""
    month = budget.month_of(now)
    elapsed_us = (now - budget.month_start(month)) // _MICROSECOND
    length_us = budget.month_length_s(month) * 1_000_000
    return "ok" if scoring_counted_usd * length_us <= scoring_share_usd * elapsed_us else "ahead"


def _exhausted(w: QuotaOut) -> bool:
    return w.state == "exhausted_until_reset" or w.used_units >= w.limit_units


def paid_search(
    jsearch: QuotaOut | None, serpapi: QuotaOut | None, *, serpapi_enabled: bool, serpapi_healthy: bool
) -> PaidSearchOut:
    """Whether paid search is paused, until when and why (module docstring, Paid search)."""
    if jsearch is None or not _exhausted(jsearch):
        return PaidSearchOut(paused=False, until=None, reason=None)
    until = jsearch.window_end
    reason: PaidSearchReason
    if not serpapi_enabled:
        reason = "serpapi_disabled"
    elif serpapi is None or serpapi.state == "extrapolated_unvalidated" or not serpapi_healthy:
        reason = "serpapi_unavailable"
    elif _exhausted(serpapi):
        reason = "serpapi_exhausted"
        until = min(until, serpapi.window_end)
    else:
        return PaidSearchOut(paused=False, until=None, reason=None)
    return PaidSearchOut(paused=True, until=until, reason=reason)


def next_slots(
    anchor_utc: datetime, interval_s: int, enabled: bool, now: datetime, n: int = NEXT_SLOTS
) -> list[datetime]:
    """The `n` schedule slots after `now` (`anchor_utc` plus whole intervals; a slot at `now` is not one of
    them), none while the schedule is disabled."""
    if not enabled:
        return []
    latest = latest_slot_at_or_before(anchor_utc, interval_s, now)
    step = timedelta(seconds=interval_s)
    return [latest + k * step for k in range(1, n + 1)]


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _pause_banner(reason: PauseReason) -> BannerOut:
    """The one banner of a pause reason: its own for the cap and the violated bound, else `scoring_paused`."""
    if reason == "cap_reached":
        return BannerOut(code="cap_reached", severity="critical", params={})
    if reason == "reservation_bound_violated":
        return BannerOut(code="reservation_bound_violated", severity="critical", params={})
    severity: BannerSeverity = "warn" if reason == "global_fence" else "critical"
    return BannerOut(code="scoring_paused", severity=severity, params={"reason": reason})


def compute_banners(f: BannerFacts) -> list[BannerOut]:
    """The banners of `f`, critical first, in the table's order within a severity (module docstring)."""
    found: list[BannerOut] = []
    if f.run_age_state == "critical":
        found.append(BannerOut(code="stale_data", severity="critical", params={}))
    if f.pause_reason is not None:
        found.append(_pause_banner(f.pause_reason))
    if f.critical_unacked > 0:
        found.append(
            BannerOut(code="critical_alerts", severity="critical", params={"count": f.critical_unacked})
        )
    if f.paid_search.paused:
        params: dict[str, JsonValue] = {"until": _iso(f.paid_search.until), "reason": f.paid_search.reason}
        found.append(BannerOut(code="paid_search_paused", severity="warn", params=params))
    scope = f.scope
    if scope.pending_version_id is None and not scope.applied:
        found.append(BannerOut(code="scope_not_applied", severity="warn", params={}))
    if f.deletion_unshipped:
        found.append(BannerOut(code="deletion_unshipped", severity="warn", params={}))
    if scope.pending_version_id is not None:
        progress = scope.progress
        applying: dict[str, JsonValue] = (
            {"failed": True}
            if progress is None
            else {"done": progress.done + progress.det_done, "total": progress.total + progress.det_total}
        )
        found.append(BannerOut(code="applying_settings", severity="info", params=applying))
    rescore = f.rescore
    if rescore is not None and (rescore.done < rescore.total or rescore.creating):
        found.append(
            BannerOut(
                code="rescoring", severity="info", params={"done": rescore.done, "total": rescore.total}
            )
        )
    return sorted(found, key=lambda banner: _SEVERITY_RANK[banner.severity])  # stable: the table order stays


# ---- the reads --------------------------------------------------------------------------------------------


def _count(progress: JSONValue, name: str) -> int:
    value = progress.get(name) if isinstance(progress, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def scope_progress(progress: JSONValue) -> ScopeProgressOut:
    """A live `apply_scope` operation's `operations.progress` in both phases; a count it has not reported yet
    is 0. `GET /api/settings` reads it so too."""
    names = ("done", "total", "det_done", "det_total")
    return ScopeProgressOut(**{name: _count(progress, name) for name in names})


async def _scope(conn: AsyncConnection) -> ScopeStatusOut:
    row = (await conn.execute(_PENDING_SCOPE, {"kind": scope_apply.KIND})).one()
    progress = None if row.operation_id is None else scope_progress(row.progress)
    return ScopeStatusOut(
        pending_version_id=row.pending_scope_version_id,
        progress=progress,
        applied=await scope_apply.scope_applied(conn),
    )


async def _rescore(conn: AsyncConnection) -> RescoreOut | None:
    operation = (await conn.execute(_ACTIVE_BULK)).scalar_one_or_none()
    if operation is None:
        return None
    counts = (await conn.execute(_BULK_TARGETS, {"b": operation, "unscored": list(_UNSCORED_STATES)})).one()
    return RescoreOut(
        bulk_operation_id=int(operation),
        done=int(counts.done),
        total=int(counts.total),
        creating=await bulk.creation_live(conn, int(operation)),
    )


async def _serpapi_healthy(conn: AsyncConnection, now: datetime) -> bool:
    instance = (await conn.execute(_SERPAPI_INSTANCE)).scalar_one_or_none()
    return instance is not None and await instance_availability(conn, int(instance), now=now) != "blocked"


async def _last_run(conn: AsyncConnection, now: datetime) -> LastRunOut | None:
    row = (await conn.execute(LAST_RUN)).first()
    if row is None:
        return None
    run = RunOut.of(row)
    return LastRunOut(
        run=run,
        age_s=(now - run.queued_at).total_seconds(),
        partial_causes=lists.partial_causes(run.id, row.report),
    )


async def system_status(conn: AsyncConnection, *, now: datetime, fake_llm: bool) -> StatusOut:
    """The header strip at `now` (the caller's clock), read on `conn` without a lock; `fake_llm` is the
    process's LLM mode, which the score lane's pause guard reads (module docstring)."""
    schedule = (await conn.execute(_SCHEDULE)).one()
    p = await active_budget_params(conn)
    config = None if p is None else await cycles.scoring_config(conn, lock=False)
    llm = None
    pause = None
    run = None
    if config is not None and p is not None:  # seeded
        row = await read_month_or_new(conn, budget.month_of(now), p, now=now)
        spend = LlmSpendOut.of(row, p)
        share = budget.scoring_share(budget.row_params(row, p))
        llm = HeaderLlmOut(
            counted=spend.counted,
            cap=spend.cap,
            pace=pace(scoring_counted_usd=row.scoring_counted_usd, scoring_share_usd=share, now=now),
        )
        pause = await cycles.pause_reason(conn, fake_llm=fake_llm, now=now, config=config)
        run = await load_run_settings(conn, config.active.settings_version_id)
    # One view of each window at `now`, rolled forward once, for the quota block, the pause and run now.
    windows = {} if run is None else await live_windows(conn, run, now)
    quota = await quota_views(conn, windows)
    paid = paid_search(
        quota["jsearch"],
        quota["serpapi"],
        serpapi_enabled=run is not None and run.sources.serpapi,
        serpapi_healthy=await _serpapi_healthy(conn, now),
    )
    units = None if run is None else run_now_units(run, windows)
    alerts = (await conn.execute(_UNACKED_ALERTS)).one()
    unacked = UnackedAlertsOut(critical_unacked=int(alerts.critical), total_unacked=int(alerts.total))
    rescore = await _rescore(conn)
    scope = await _scope(conn)
    facts = BannerFacts(
        run_age_state=run_age_state(now, await read_run_freshness(conn)),
        pause_reason=pause,
        critical_unacked=unacked.critical_unacked,
        paid_search=paid,
        scope=scope,
        rescore=rescore,
        deletion_unshipped=bool((await conn.execute(_UNSHIPPED)).scalar_one()),
    )
    return StatusOut(
        last_run=await _last_run(conn, now),
        active_run_id=(await conn.execute(_ACTIVE_RUN)).scalar_one_or_none(),
        next_slots=next_slots(schedule.anchor_utc, int(schedule.interval_s), bool(schedule.enabled), now),
        quota={
            provider: None
            if w is None
            else HeaderQuotaOut(used=w.used_units, limit=w.limit_units, reset_at=w.window_end, state=w.state)
            for provider, w in quota.items()
        },
        llm=llm,
        coverage_gaps_open=int((await conn.execute(_OPEN_GAPS)).scalar_one()),
        pending_targets=int((await conn.execute(_PENDING_TARGETS)).scalar_one()),
        alerts=unacked,
        rescore=rescore,
        paid_search=paid,
        run_now=RunNowUnitsOut(
            jsearch_units=0 if units is None else units.jsearch_units,
            serpapi_fallback_units=0 if units is None else units.serpapi_fallback_units,
        ),
        scope=scope,
        banners=compute_banners(facts),
    )
