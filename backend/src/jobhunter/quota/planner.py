"""The runtime planner (spec §5.5, §6.6, §6.7, §9.2 nodes 1 and 3): the coverage cells of a run's inputs,
their order, and the fetch requests the run starts from.

**Cells** (§5.5.2): `quota.cells.ensure_cells` creates and reads them (one per `(instance, query,
location_key)` for the search sources, one Arbeitnow cell, one per enabled watchlist board); this module
orders them and drafts their requests (M1 Carry-forward 13).

**The primary plan** (`plan_run`, node 1): a run that has fetch requests already reuses them (the receipt,
`_reuse`): the latest attempt of every planned request is the work list, a `skipped(shutdown | superseded)`
one is re-planned as a new `planned` row with `attempt_seq + 1` (at most `ledger.MAX_ATTEMPT_SEQ`; one out of
attempts stays skipped and a search cell's window is recorded as the open gap `shutdown`, §7.16.5), every
other skip is a denial that stays. Otherwise every enabled instance whose health is not `disabled` is
planned, a paid one (JSearch) only in a run that includes paid sources and SerpApi never (its specs come from
the fallback, §6.6):

- by `breaker.available`: a `blocked` instance's cells are `skipped(breaker_open)`, a `half_open` instance
  gets exactly one spec (its first cell; the others are `skipped(breaker_open)`);
- the free cells (Arbeitnow, the boards) are planned every run, `free` class;
- Adzuna's cells, stalest first, and JSearch's, by `order_paid`, go through one `ledger.project_allowance` per
  provider: an admitted spec is `planned` with the projected class (a `probe` on an unvalidated window), a
  denied one `skipped(<code>)` (`paced`, `reserve_held`, `exhausted`, `window_cap`,
  `awaiting_window_validation`), so the deferral is visible.

Every spec is the adapter's `plan(cell_plan)`, with the coverage window computed here once and carried in
`RequestSpec.window` (a resumed attempt reads it back from the row): JSearch and SerpApi the freshness bucket
(`coverage.freshness_bucket`), whose unreachable part is abandoned at once by an inline `coverage_intervals`
row (`gap`, `abandoned`, `beyond_provider_window`) when the spec is planned, from the cell's latest such bound
on, so that the rows of successive runs never overlap, and the cell's open gaps that reach back beyond the
window are abandoned with it (`coverage.abandon_beyond_provider_window`: one that straddles the window's lower
bound keeps its newer part open, so contiguity can advance past it); Adzuna `max_days_old` days; the
free sources the scope's ad age. **Cursor reuse** (§5.3.1, Ruling P3): a JSearch cell whose continuation holds
a cursor obtained less than `coverage.CURSOR_REUSE_TTL` ago, for a page the page-2 rule admitted (page 2 after
a page 1 with at least 7 new ids, `jsearch.admits_next_page`; final review F3), is planned from it,
`next_page(plan(cell_plan), cursor)` over the continuation's window and on its page, so that traversal's
`END` closes its open gap; otherwise from page 1 with a fresh window, the open gap kept. The specs are built
by the registry's adapters (`registry.make_adapter`, the one factory) on a transport that refuses every
request (`sources.spec_adapters._spec_adapters`): `plan` and `next_page` never send, and `quota/` imports
neither the factory nor any concrete adapter (`tests/unit/test_quota_imports.py`).

**The fallback plan** (`plan_fallback`, node 3, §6.6): for every JSearch cell of the run whose
`fallback.cell_outcome` is eligible, one SerpApi spec `phase='fallback'` on the SerpApi cell with the same
`(query_id, location_key)`, through `project_allowance("serpapi", …, account=…)` with the `plan_fallback`
node's free account read (the acquisition reads the account again before every search);
`quota.fallback_active` (info) when any is planned. It is a receipt too. Only the SerpApi projection ever gets
the account (Ruling T13-c).

Every function runs in the caller's transaction, which this module opens with the owner row `FOR SHARE`
(`ledger.lock_run_owner`); `project_allowance` reads the quota windows without a lock.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.observability.alerts import raise_alert
from jobhunter.observability.logging import get_logger
from jobhunter.quota.cells import _COUNTERPARTS, CellRow, _cell_rows, _source_enabled, ensure_cells
from jobhunter.quota.fallback import cell_outcome, eligible
from jobhunter.quota.ledger import MAX_ATTEMPT_SEQ, lock_run_owner, project_allowance
from jobhunter.quota.pacing import Admit, PlannedSpec
from jobhunter.quota.windows import AccountObservation, Provider
from jobhunter.run_settings import RunSettings
from jobhunter.sources.base import (
    CellPlan,
    CoverageWindow,
    RequestClass,
    RequestSpec,
    SourceAdapter,
    request_key,
)
from jobhunter.sources.breaker import available
from jobhunter.sources.coverage import (
    BUCKETS,
    CURSOR_REUSE_TTL,
    SEARCH_SOURCES,
    Bucket,
    JSearchContinuation,
    abandon_beyond_provider_window,
    adzuna_max_days_old,
    freshness_bucket,
    record_search_gap,
)
from jobhunter.sources.instances import InstanceRow
from jobhunter.sources.receipts import insert_fetch_request
from jobhunter.sources.spec_adapters import _spec_adapters

if TYPE_CHECKING:
    # `worker.runs` imports `observability.health`, which imports the quota ledger: the owner type only.
    from jobhunter.worker.runs import RunOwner

Phase = Literal["primary", "fallback"]

# The sources with one cell per (instance, query, location key) (`coverage.SEARCH_SOURCES`, §5.5.2), and
# their quota providers.
PROVIDERS: Final[Mapping[str, Provider]] = MappingProxyType(
    {"jsearch": "jsearch", "serpapi": "serpapi", "adzuna": "adzuna"}
)
# §5.5.3 priority: `staleness_h * weight * (0.5 + yield_ema)`.
NEVER_ATTEMPTED_H: Final = 10_000.0
DEMOTION_STREAK: Final = 3  # zero-yield runs in a row that demote a cell
DEMOTED_WEIGHT: Final = 0.25
DEMOTED_REFRESH: Final = timedelta(days=7)  # a demoted cell is still refreshed at least this often
EXPLORATION_EVERY: Final = 10  # paid units planned since the last exploration request that call for one
# The skips that a new attempt plans again (§7.16.5); every other skip is the request's denial.
REPLANNED: Final = frozenset({"shutdown", "superseded"})
FALLBACK_ACTIVE_ALERT: Final = "quota.fallback_active"
_HOUR: Final = timedelta(hours=1)
_DAY: Final = timedelta(days=1)

log = get_logger("jobhunter.quota.planner")

# Normal-class JSearch entries since the last exploration request of a JSearch cell (all of them before one).
_UNITS_SINCE_EXPLORATION = q(
    "SELECT count(*) FROM quota_ledger WHERE provider = 'jsearch' AND kind = 'request' "
    "AND request_class = 'normal' AND created_at > coalesce((SELECT max(c.last_exploration_at) "
    "FROM coverage_cells c JOIN source_instances i ON i.id = c.source_instance_id "
    "WHERE i.source_id = 'jsearch'), CAST('-infinity' AS timestamptz))"
)
# The latest attempt of every request the planner wrote for the run's phase: per cell, the key of its first
# row (later rows of a cell are the traversal's own pages).
_RECEIPTS = q(
    "WITH firsts AS (SELECT DISTINCT ON (cell_id) request_key, id AS first_id FROM fetch_requests "
    "WHERE run_id = :run AND phase = :phase ORDER BY cell_id, id) "
    "SELECT DISTINCT ON (f.request_key) f.id, f.cell_id, f.source_instance_id, f.attempt_seq, f.status, "
    "f.skip_reason, f.request_key, f.spec, firsts.first_id, i.source_id FROM fetch_requests f "
    "JOIN firsts ON firsts.request_key = f.request_key "
    "JOIN source_instances i ON i.id = f.source_instance_id "
    "WHERE f.run_id = :run ORDER BY f.request_key, f.attempt_seq DESC"
)
_RUN_JSEARCH_CELLS = q(
    "SELECT DISTINCT f.cell_id FROM fetch_requests f JOIN source_instances i ON i.id = f.source_instance_id "
    "WHERE f.run_id = :run AND f.phase = 'primary' AND i.source_id = 'jsearch' ORDER BY f.cell_id"
)
# The unreachable part of a window, from the latest `beyond_provider_window` bound of the cell on: while
# `last_covered_at` stays put, every run's part would otherwise overlap the parts abandoned before it.
_ABANDON = q(
    "INSERT INTO coverage_intervals (cell_id, kind, span, gap_status, reason, run_id, closed_at) "
    "SELECT :cell, 'gap', tstzrange(b.lower, :upper, '[)'), 'abandoned', 'beyond_provider_window', :run, "
    ":now FROM (SELECT greatest(CAST(:lower AS timestamptz), max(upper(span))) AS lower "
    "FROM coverage_intervals WHERE cell_id = :cell AND kind = 'gap' "
    "AND reason = 'beyond_provider_window') AS b "
    "WHERE b.lower < CAST(:upper AS timestamptz) ON CONFLICT (cell_id, kind, span) DO NOTHING"
)


@dataclass(frozen=True)
class PlanReport:
    """`primary_fetch_request_ids`: the requests to fetch (the latest attempt of each, never a denial);
    `skipped`: the denied ones by reason; `cells`: the cells with a request; `replanned`: the re-planned
    shutdown and superseded skips."""

    primary_fetch_request_ids: list[int]
    skipped: dict[str, int]
    cells: int
    replanned: int


def cell_plan(
    cell: CellRow,
    instance: InstanceRow,
    *,
    phase: Phase,
    request_class: RequestClass,
    bucket: Bucket | None,
    window: CoverageWindow,
    max_days_old: int | None,
) -> CellPlan:
    """What the adapter of `instance` needs to plan the first page of `cell`'s traversal (Task 5)."""
    if instance.id != cell.source_instance_id:
        raise ValueError(f"cell {cell.id} belongs to instance {cell.source_instance_id}, not {instance.id}")
    return CellPlan(
        cell_id=cell.id,
        source_id=cell.source_id,
        instance_id=instance.id,
        query_text=cell.query_text,
        query_lang=cell.query_lang,
        location_key=cell.location_key,
        location_label=cell.location_label,
        radius_km=cell.radius_km,
        phase=phase,
        request_class=request_class,
        bucket=bucket,
        window=window,
        max_days_old=max_days_old,
    )


def priority(cell: CellRow, now: datetime) -> float:
    """`staleness_h * weight * (0.5 + yield_ema)` (§5.5.3): `staleness_h` is 10,000 for a never-attempted
    cell, `weight` 0.25 for a demoted one (zero-yield streak ≥ 3) unless it is due its 7-day refresh."""
    if cell.last_attempted_at is None:
        staleness_h, refresh_due = NEVER_ATTEMPTED_H, True
    else:
        stale = now - cell.last_attempted_at
        staleness_h, refresh_due = stale / _HOUR, stale >= DEMOTED_REFRESH
    weight = DEMOTED_WEIGHT if cell.zero_yield_streak >= DEMOTION_STREAK and not refresh_due else 1.0
    return staleness_h * weight * (0.5 + cell.yield_ema)


def _by_priority(cells: Sequence[CellRow], now: datetime) -> list[CellRow]:
    return sorted(cells, key=lambda cell: (-priority(cell, now), cell.id))


def order_paid(cells: Sequence[CellRow], *, now: datetime, units_since_exploration: int) -> list[CellRow]:
    """The paid cells by priority (ties by id); the first exploration cell leads once at least
    `EXPLORATION_EVERY` paid units were planned since the last exploration request (§5.5.3)."""
    ordered = _by_priority(cells, now)
    if units_since_exploration >= EXPLORATION_EVERY:
        explore = next((cell for cell in ordered if cell.exploration), None)
        if explore is not None:
            ordered.remove(explore)
            ordered.insert(0, explore)
    return ordered


def _stalest_first(cells: Sequence[CellRow]) -> list[CellRow]:
    """Never-attempted cells first, then by `last_attempted_at` (the Adzuna rotation, §5.5.3)."""
    return sorted(
        cells, key=lambda cell: (cell.last_attempted_at is not None, cell.last_attempted_at, cell.id)
    )


def _ad_age_days(settings: RunSettings) -> int:
    if settings.scope.max_ad_age_days is None:
        raise ValueError(f"settings version {settings.settings_version_id} has no scope.max_ad_age_days")
    return settings.scope.max_ad_age_days


def _bucket_of(window: CoverageWindow) -> Bucket | None:
    span = window[1] - window[0]
    return next((bucket for bucket, length in BUCKETS.items() if length == span), None)


def _reusable_cursor(cell: CellRow, now: datetime) -> tuple[JSearchContinuation, Bucket] | None:
    """The cell's continuation when its cursor may start this traversal (§5.3.1): obtained less than
    `CURSOR_REUSE_TTL` ago, for a page the page-2 rule admits (`JSearchContinuation.admitted`: page 2 after
    a page 1 with at least 7 new ids, checked here again so that no stored row plans past the rule), over a
    window of a bucket's span (the query that cursor continues)."""
    continuation = JSearchContinuation.parse(cell.continuation)
    if continuation is None or continuation.obtained_at + CURSOR_REUSE_TTL <= now:
        return None
    if not continuation.admitted():
        return None
    bucket = _bucket_of(continuation.window)
    return None if bucket is None else (continuation, bucket)


@dataclass(frozen=True)
class _Draft:
    spec: RequestSpec
    abandoned: CoverageWindow | None  # the unreachable part of the window, abandoned when the spec is planned


def _draft(
    cell: CellRow,
    instance: InstanceRow,
    adapter: SourceAdapter,
    *,
    phase: Phase,
    request_class: RequestClass,
    now: datetime,
    settings: RunSettings,
) -> _Draft:
    """The first spec of the cell's traversal and its window (see the module docstring)."""

    def plan(bucket: Bucket | None, window: CoverageWindow, max_days_old: int | None = None) -> RequestSpec:
        return adapter.plan(
            cell_plan(
                cell,
                instance,
                phase=phase,
                request_class=request_class,
                bucket=bucket,
                window=window,
                max_days_old=max_days_old,
            )
        )

    if cell.source_id in ("jsearch", "serpapi"):
        reusable = _reusable_cursor(cell, now) if cell.source_id == "jsearch" else None
        if reusable is not None:
            continuation, bucket = reusable
            resumed = adapter.next_page(plan(bucket, continuation.window), continuation.cursor)
            if resumed is None:
                raise ValueError(f"cell {cell.id}: the adapter refused the cursor of its continuation")
            return _Draft(replace(resumed, page_no=continuation.page_no, request_class=request_class), None)
        bucket, window, abandoned = freshness_bucket(now, cell.last_covered_at)
        return _Draft(plan(bucket, window), abandoned)
    if cell.source_id == "adzuna":
        days = adzuna_max_days_old(now, cell.last_covered_at, _ad_age_days(settings))
        return _Draft(plan(None, (now - days * _DAY, now), days), None)
    return _Draft(plan(None, (now - _ad_age_days(settings) * _DAY, now)), None)


@dataclass
class _Rows:
    """The fetch requests one planning transaction writes for `phase`, and what its report counts."""

    conn: AsyncConnection
    owner: "RunOwner"
    phase: Phase
    now: datetime
    planned: list[int] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)
    cells: int = 0
    skips_by_source: Counter[str] = field(default_factory=Counter)  # the skipped receipts written, by source

    async def write(self, draft: _Draft, *, skip: str | None, source_id: str) -> None:
        spec = draft.spec
        fetch_request_id = await insert_fetch_request(
            self.conn,
            owner=self.owner,
            cell_id=spec.cell_id,
            source_instance_id=spec.source_instance_id,
            phase=self.phase,
            spec=spec,
            request_key=request_key(spec, self.owner.run_id),
            attempt_seq=1,
            status="planned" if skip is None else "skipped",
            skip_reason=skip,
        )
        self.cells += 1
        if skip is not None:
            self.skipped[skip] += 1
            self.skips_by_source[source_id] += 1
            return
        self.planned.append(fetch_request_id)
        if draft.abandoned is not None:
            lower, upper = draft.abandoned
            await self.conn.execute(
                _ABANDON,
                {
                    "cell": spec.cell_id,
                    "lower": lower,
                    "upper": upper,
                    "run": self.owner.run_id,
                    "now": self.now,
                },
            )
            # the open gaps reaching back beyond the window: abandoned, a straddling one split (§5.6)
            await abandon_beyond_provider_window(self.conn, spec.cell_id, upper)

    def report(self, *, replanned: int = 0) -> PlanReport:
        return PlanReport(list(self.planned), dict(self.skipped), self.cells, replanned)


async def _plan_instance(
    rows: _Rows,
    instance: InstanceRow,
    cells: Sequence[CellRow],
    adapter: SourceAdapter,
    *,
    settings: RunSettings,
    account: AccountObservation | None,
) -> None:
    """One request per cell of the instance, in the order given (see the module docstring). `account` reaches
    the SerpApi projection only (Ruling T13-c)."""
    provider = PROVIDERS.get(instance.source_id)
    base: RequestClass = "normal" if provider is not None else "free"
    match available(instance, rows.now):
        case "blocked":
            admitted: Sequence[CellRow] = ()
        case "half_open":
            admitted = cells[:1]  # one probe of the breaker
        case "available":
            admitted = cells
    decisions: dict[int, tuple[RequestClass, str | None]] = {
        cell.id: (base, "breaker_open") for cell in cells
    }
    if provider is None:
        decisions |= {cell.id: (base, None) for cell in admitted}
    elif admitted:
        verdicts = await project_allowance(
            rows.conn,
            provider,
            [PlannedSpec("normal")] * len(admitted),
            rows.now,
            settings.quota,
            interval_s=settings.interval_s,
            account=account if provider == "serpapi" else None,
        )
        for cell, verdict in zip(admitted, verdicts, strict=True):
            decisions[cell.id] = (
                (verdict.request_class, None) if isinstance(verdict, Admit) else (base, verdict.code)
            )
    for cell in cells:
        request_class, skip = decisions[cell.id]
        draft = _draft(
            cell,
            instance,
            adapter,
            phase=rows.phase,
            request_class=request_class,
            now=rows.now,
            settings=settings,
        )
        await rows.write(draft, skip=skip, source_id=instance.source_id)


def _plans_primary(instance: InstanceRow, settings: RunSettings, *, include_paid: bool) -> bool:
    """Whether node 1 plans the instance: enabled and not `disabled`; JSearch only in a paid run; SerpApi
    never (its specs come from the fallback)."""
    if instance.health == "disabled" or instance.source_id == "serpapi":
        return False
    if instance.source_id == "jsearch" and not include_paid:
        return False
    return _source_enabled(settings, instance)


async def _reuse(conn: AsyncConnection, owner: "RunOwner", phase: Phase) -> PlanReport | None:
    """The receipt of the run's `phase` plan, `None` when there is none (see the module docstring)."""
    found = (await conn.execute(_RECEIPTS, {"run": owner.run_id, "phase": phase})).all()
    if not found:
        return None
    planned: list[int] = []
    skipped: Counter[str] = Counter()
    replanned = 0
    for row in sorted(found, key=lambda row: int(row.first_id)):
        if row.status == "skipped" and row.skip_reason in REPLANNED and row.attempt_seq >= MAX_ATTEMPT_SEQ:
            # out of attempts (§7.16.5): the spec is recorded as a gap, a search cell's window by §5.6
            window = RequestSpec.from_json(row.spec).window
            if row.source_id in SEARCH_SOURCES and window is not None:
                await record_search_gap(
                    conn, int(row.cell_id), window=window, reason="shutdown", run_id=owner.run_id
                )
            skipped[str(row.skip_reason)] += 1
        elif row.status == "skipped" and row.skip_reason in REPLANNED:
            fetch_request_id = await insert_fetch_request(
                conn,
                owner=owner,
                cell_id=int(row.cell_id),
                source_instance_id=int(row.source_instance_id),
                phase=phase,
                spec=RequestSpec.from_json(row.spec),
                request_key=bytes(row.request_key),
                attempt_seq=int(row.attempt_seq) + 1,
                status="planned",
            )
            planned.append(fetch_request_id)
            replanned += 1
        elif row.status == "skipped":
            skipped[str(row.skip_reason)] += 1
        else:
            planned.append(int(row.id))
    return PlanReport(planned, dict(skipped), len(found), replanned)


async def plan_run(
    conn: AsyncConnection,
    *,
    owner: "RunOwner",
    include_paid: bool,
    settings: RunSettings,
    instances: Sequence[InstanceRow],
    now: datetime,
    account: AccountObservation | None,
    skips_written: Counter[str] | None = None,
) -> PlanReport:
    """The primary plan of `owner`'s run (§9.2 node 1; see the module docstring), in the caller's transaction.

    `account` goes to a SerpApi projection only, never to JSearch's or Adzuna's (Ruling T13-c); the primary
    plan has no SerpApi spec, so the `plan` node passes `None`. `skips_written` (when given) gains, by source,
    the `skipped` receipts this call wrote (a pacing deferral, an open breaker): none when the plan is the
    run's receipt already, so the caller counts each once, after its commit (`FETCH_REQUESTS`).
    """
    await lock_run_owner(conn, owner)
    reused = await _reuse(conn, owner, "primary")
    if reused is None:
        cells = await ensure_cells(conn, settings, instances)
        rows = _Rows(conn, owner, "primary", now)
        units = 0
        if any(cell.source_id == "jsearch" for cell in cells) and include_paid:
            units = int((await conn.execute(_UNITS_SINCE_EXPLORATION)).scalar_one())
        async with _spec_adapters(now) as adapter_of:
            for instance in instances:
                mine = [cell for cell in cells if cell.source_instance_id == instance.id]
                if not mine or not _plans_primary(instance, settings, include_paid=include_paid):
                    continue
                if instance.source_id == "jsearch":
                    mine = order_paid(mine, now=now, units_since_exploration=units)
                elif instance.source_id == "adzuna":
                    mine = _stalest_first(mine)
                await _plan_instance(
                    rows, instance, mine, adapter_of(instance), settings=settings, account=account
                )
        reused = rows.report()
        if skips_written is not None:
            skips_written.update(rows.skips_by_source)
    log.info(
        "run_planned",
        run_id=owner.run_id,
        attempt_no=owner.attempt_no,
        planned=len(reused.primary_fetch_request_ids),
        skipped=reused.skipped,
        cells=reused.cells,
        replanned=reused.replanned,
    )
    return reused


async def plan_fallback(
    conn: AsyncConnection,
    *,
    owner: "RunOwner",
    settings: RunSettings,
    instances: Sequence[InstanceRow],
    now: datetime,
    account: AccountObservation | None,
    skips_written: Counter[str] | None = None,
) -> list[int]:
    """The SerpApi fallback of `owner`'s run (§6.6, §9.2 node 3): the ids of its planned specs, in the
    caller's transaction (see the module docstring); `skips_written` as for `plan_run`."""
    await lock_run_owner(conn, owner)
    if (reused := await _reuse(conn, owner, "fallback")) is not None:
        return reused.primary_fetch_request_ids
    serpapi = next((i for i in instances if i.source_id == "serpapi"), None)
    if serpapi is None or serpapi.health == "disabled" or not _source_enabled(settings, serpapi):
        return []
    unserved: list[int] = []
    for cell_id in (await conn.execute(_RUN_JSEARCH_CELLS, {"run": owner.run_id})).scalars():
        if eligible(await cell_outcome(conn, owner.run_id, int(cell_id))):
            unserved.append(int(cell_id))
    if not unserved:
        return []
    params = {"cells": unserved, "instance": serpapi.id}
    cells = _by_priority(await _cell_rows(conn, _COUNTERPARTS, params, settings.scope), now)
    rows = _Rows(conn, owner, "fallback", now)
    async with _spec_adapters(now) as adapter_of:
        await _plan_instance(rows, serpapi, cells, adapter_of(serpapi), settings=settings, account=account)
    if skips_written is not None:
        skips_written.update(rows.skips_by_source)
    if rows.planned:
        await raise_alert(
            conn,
            code=FALLBACK_ACTIVE_ALERT,
            severity="info",
            subject=f"provider:{serpapi.source_id}",
            message="SerpApi serves JSearch cells in this run",
            context={"run_id": owner.run_id, "cells": len(rows.planned)},
        )
    log.info(
        "fallback_planned",
        run_id=owner.run_id,
        attempt_no=owner.attempt_no,
        eligible=len(unserved),
        planned=len(rows.planned),
        skipped=dict(rows.skipped),
    )
    return rows.planned
