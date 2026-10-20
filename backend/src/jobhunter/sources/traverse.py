"""Cell traversal (spec §5.3, §5.6, §9.2 node 2; plan Task 14): one cell, page by page, through receipts.

`traverse_cell` starts from the planned fetch request (its stored spec is page 1, or the page a reused JSearch
cursor fetches) and acquires each page through `acquire.acquire_page`, so every page has its own receipt, a
resumed attempt replays what was served and no page is ever sent twice (§6.2 rule 2). Every page yields one
`FetchOutcome(work_id=f"fr:{id}")` with `new_postings = 0`: postings exist only after node 6, so the yield is
counted from the tables at finalize (`coverage.finalize_cells`). The window is the planned spec's
(`plan.window`, read from the row), never recomputed on a resumed attempt.

The page rules (§5.3):

- **JSearch and SerpApi**: page 2 only when page 1 had at least `jsearch.PAGE2_MIN_NEW` new native ids (the
  count `acquire_page` persisted in `fetch_requests.new_native_ids`) and its own reservation is admitted; no
  traversal reads past page 2 (§5.3.1 defines no page 3, and SerpApi, the fallback for JSearch's cells,
  follows JSearch's rule). A cursor left unread ends the traversal `cursor` with reason `page_cap`.
- **Adzuna**: up to `adzuna.PAGE_CAP` pages; a full page at the cap ends it `unknown`
  (`unknown_continuation`).
- **Arbeitnow** (Ruling T10-a): the fresh head from page 1, read to the end of the feed (`end`), the page cap
  `arbeitnow.page_cap(interval_s)` (`cursor`, `page_cap`) or `remaining_low` (`cursor`, `allowance`); no
  item time stops it. Its reads go to `arbeitnow.conclude` and `covered_span` against the cell's
  `FirehoseState`, rebuilt from its newest `covered` row and its gaps of every status. The read from page 1
  ends every traversal (the end of the feed, the cap, the remaining-low rule or a failure), so no seek leg is
  planned: no page would be left for one (plan Deviation 27). A traversal that fails after its first page
  concludes with `stopped_by="remaining"` (its reads prove nothing), one that read nothing does not
  conclude.
- **ATS boards**: until `END` (Lever and SmartRecruiters by offset), at most `ATS_PAGE_CAP` list pages;
  SmartRecruiters detail calls for the postings `detail_specs` names after each list page (none once
  admission has stopped), at most `ratelimit.SMARTRECRUITERS_DETAIL.concurrency` at a time, which never end
  the traversal. The snapshot's
  membership comes from the list pages alone (`ats.common.membership`, one final attempt per page, numbered
  from 1); `snapshot` is `None` when no list page was served (a board whose first page failed writes no
  snapshot and ages nothing, §8.11).

Every traversal: an instance whose breaker is `half_open` gets one page (its probe); `admission_stopped()`
between two pages ends it `skipped('shutdown')` (inside a page `acquire_page` makes the receipt itself); a
page that is `Failed`, `Ambiguous` or `Denied` ends it `failed`, `ambiguous` or `skipped`. After the last
search of a SerpApi traversal that sent one, the free account read is made once more and persisted with
`ledger.record_account` (§5.3.2); a read that fails as `auth` (a key revoked mid-run: a 401/403) is counted
by the instance's breaker, which opens `auth_failed` with `source.auth_failed` at once, as the read before a
search does (Ruling FR-l); any other failed read is logged and changes nothing. The sends go through the
process's `RateLimiter`: the source's own key, `ats-host:<host>` for a board's list pages and
`smartrecruiters-detail` for the detail calls. `traverse_cell` never raises `SourceError`.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.db.sql import q
from jobhunter.errors import FencedOut, InvariantViolation
from jobhunter.graphs.collect.state import FetchOutcome
from jobhunter.observability.logging import get_logger
from jobhunter.quota import ledger
from jobhunter.sources import adzuna, jsearch
from jobhunter.sources.acquire import (
    AccountAdapter,
    AcquireContext,
    Ambiguous,
    Denied,
    Failed,
    PageResult,
    Served,
    acquire_page,
    failure_kind,
    record_account_auth_failure,
)
from jobhunter.sources.arbeitnow import (
    ArbeitnowAdapter,
    FirehoseState,
    Gap,
    StopReason,
    conclude,
    covered_span,
    page_cap,
)
from jobhunter.sources.ats.common import AtsAdapter, SnapshotDraft, membership
from jobhunter.sources.ats.smartrecruiters import SmartRecruitersAdapter
from jobhunter.sources.base import CellPlan, Continuation, CoverageWindow, RawPage, RequestSpec, SourceError
from jobhunter.sources.breaker import available
from jobhunter.sources.instances import list_instances
from jobhunter.sources.ratelimit import SMARTRECRUITERS_DETAIL, RateLimiter

End = Literal["end", "cursor", "unknown", "ambiguous", "failed", "skipped"]
# The paid aggregators that follow the page-2 rule of §5.3.1 (`jsearch.admits_next_page`).
PAGE2_SOURCES: Final = frozenset({"jsearch", "serpapi"})
# The list pages a board traversal reads at most (10,000 postings at 100 a page): a guard against a
# pagination that never ends, not a rule of §5.3.5. A board cut there ends `unknown`, its snapshot incomplete.
ATS_PAGE_CAP: Final = 100

log = get_logger("jobhunter.sources.traverse")

_SPEC = q("SELECT spec FROM fetch_requests WHERE id = :i")
_KNOWN = q(
    "SELECT count(*) FROM postings WHERE source_instance_id = :i AND native_id = ANY(CAST(:ids AS text[]))"
)
_DETAILED = q(
    "SELECT native_id, list_item_sha256, detail_fetched_at FROM postings WHERE source_instance_id = :i "
    "AND native_id = ANY(CAST(:ids AS text[])) AND list_item_sha256 IS NOT NULL"
)
_INTERVALS = q(
    "SELECT kind, lower(span) AS lower, upper(span) AS upper, gap_status FROM coverage_intervals "
    "WHERE cell_id = :c ORDER BY lower(span), upper(span), id"
)


@dataclass(frozen=True)
class TraversalResult:
    """One cell's traversal.

    `end` is how it ended and `reason` why (`page_cap`, `allowance`, `unknown_continuation`,
    `ambiguous_attempt`, the error code of a failed page, the skip reason of a denied one; `None` at `END`);
    `pages` and `items` count the list pages served and their items, `cursor_out` is the continuation of
    the last one when it had one. `snapshot` is a board's membership (`None` without a served list page),
    `firehose` the Arbeitnow cell's coverage before the traversal with the gaps `conclude` created or
    changed, and `covered` the span the Arbeitnow reads prove covered (`covered_span`). `taken_at` is the
    arrival of the first served list page (a snapshot's `taken_at`). `sent` holds the outcomes of the pages
    this attempt sent, which `FETCH_REQUESTS` counts: a receipt a resumed attempt replays and a denied page
    are not among them.
    """

    outcomes: list[FetchOutcome]
    end: End
    reason: str | None
    pages: int
    items: int
    cursor_out: str | None
    snapshot: SnapshotDraft | None
    firehose: tuple[FirehoseState, list[Gap]] | None
    covered: CoverageWindow | None = None
    taken_at: datetime | None = None
    sent: list[FetchOutcome] = field(default_factory=list)


def _outcome(result: PageResult, items: int) -> FetchOutcome:
    work_id = f"fr:{result.fetch_request_id}"
    match result:
        case Served():
            return {"work_id": work_id, "status": "succeeded", "items": items, "new_postings": 0}
        case Failed():
            return {"work_id": work_id, "status": "failed", "items": 0, "new_postings": 0}
        case Ambiguous():
            return {"work_id": work_id, "status": "ambiguous", "items": 0, "new_postings": 0}
        case Denied():
            return {"work_id": work_id, "status": "skipped", "items": 0, "new_postings": 0}


def _stopped(result: Failed | Ambiguous | Denied) -> tuple[End, str]:
    """The end of a traversal that a page ended."""
    match result:
        case Failed():
            return "failed", result.error_code
        case Ambiguous():
            return "ambiguous", "ambiguous_attempt"
        case Denied():
            return "skipped", result.skip_reason


@dataclass
class _Traversal:
    """What one traversal has acquired so far."""

    ctx: AcquireContext
    plan: CellPlan
    limiter: RateLimiter
    probe: bool  # a half-open instance: one page
    outcomes: list[FetchOutcome] = field(default_factory=list)
    sent_outcomes: list[FetchOutcome] = field(default_factory=list)  # of the pages this attempt sent
    pages: list[RawPage] = field(default_factory=list)  # the served list pages, in order
    items: int = 0
    sent: bool = False  # whether an attempt of this traversal may have reached the provider

    async def acquire(
        self, spec: RequestSpec, planned_id: int | None, *, key: str, listing: bool = True
    ) -> PageResult:
        """One page through `acquire_page`, throttled by `key`; a list page's new ids are counted."""
        result = await acquire_page(
            self.ctx,
            spec,
            cell_id=self.plan.cell_id,
            phase=self.plan.phase,
            planned_id=planned_id,
            known_native_ids=self._known if listing else None,
            throttle=lambda: self.limiter.acquire(key),
        )
        items = 0
        if isinstance(result, Served):
            items = len(self.ctx.adapter.peek_native_ids(result.page))
            if listing:
                self.pages.append(result.page)
                self.items += items
        fresh = isinstance(result, Served | Failed) and not result.replayed
        self.sent = self.sent or fresh or isinstance(result, Ambiguous)
        outcome = _outcome(result, items)
        self.outcomes.append(outcome)
        if isinstance(result, Served | Failed | Ambiguous) and not result.replayed:  # sent by this call
            self.sent_outcomes.append(outcome)
        return result

    async def _known(self, conn: AsyncConnection, ids: Sequence[str | None]) -> int:
        """How many distinct ids of a served page are postings of the instance already (§5.3.1), read on the
        page's settlement transaction (`conn`)."""
        distinct = sorted({native_id for native_id in ids if native_id is not None})
        if not distinct:
            return 0
        params = {"i": self.plan.instance_id, "ids": distinct}
        return int((await conn.execute(_KNOWN, params)).scalar_one())

    def result(
        self,
        end: End,
        reason: str | None,
        *,
        cursor_out: str | None = None,
        snapshot: SnapshotDraft | None = None,
        firehose: tuple[FirehoseState, list[Gap]] | None = None,
        covered: CoverageWindow | None = None,
    ) -> TraversalResult:
        log.info(
            "cell_traversed",
            cell_id=self.plan.cell_id,
            source_id=self.plan.source_id,
            end=end,
            reason=reason,
            pages=len(self.pages),
            items=self.items,
            requests=len(self.outcomes),
        )
        return TraversalResult(
            outcomes=list(self.outcomes),
            end=end,
            reason=reason,
            pages=len(self.pages),
            items=self.items,
            cursor_out=cursor_out,
            snapshot=snapshot,
            firehose=firehose,
            covered=covered,
            taken_at=self.pages[0].fetched_at if self.pages else None,
            sent=list(self.sent_outcomes),
        )


async def _first_spec(engine: AsyncEngine, fetch_request_id: int) -> RequestSpec:
    async with engine.connect() as conn:
        doc = (await conn.execute(_SPEC, {"i": fetch_request_id})).scalar()
    if doc is None:
        raise LookupError(f"fetch request {fetch_request_id} does not exist")
    return RequestSpec.from_json(doc)


async def _half_open(ctx: AcquireContext, instance_id: int) -> bool:
    async with ctx.engine.connect() as conn:
        instances = await list_instances(conn)
    instance = next((i for i in instances if i.id == instance_id), None)
    if instance is None:
        raise LookupError(f"source instance {instance_id} does not exist")
    return available(instance, ctx.clock()) == "half_open"


async def traverse_cell(
    ctx: AcquireContext, plan: CellPlan, first_fetch_request_id: int, *, limiter: RateLimiter
) -> TraversalResult:
    """Traverse `plan`'s cell from the planned fetch request `first_fetch_request_id` (module docstring)."""
    first = await _first_spec(ctx.engine, first_fetch_request_id)
    if (first.cell_id, first.source_instance_id) != (plan.cell_id, plan.instance_id):
        raise ValueError(f"fetch request {first_fetch_request_id} is not a request of cell {plan.cell_id}")
    if first.window != plan.window:
        raise InvariantViolation(f"cell {plan.cell_id}: the plan's window is not its planned spec's")
    t = _Traversal(ctx, plan, limiter, probe=await _half_open(ctx, plan.instance_id))
    adapter = ctx.adapter
    if isinstance(adapter, ArbeitnowAdapter):
        return await _firehose(t, adapter, first, first_fetch_request_id)
    if isinstance(adapter, AtsAdapter):
        return await _board(t, adapter, first, first_fetch_request_id)
    result = await _search(t, first, first_fetch_request_id)
    if isinstance(adapter, AccountAdapter) and t.sent:
        await _account_after(ctx, adapter, plan.instance_id)
    return result


def _rules_allow_next(plan: CellPlan, spec: RequestSpec, served: Served) -> bool:
    """Whether the §5.3 page rules let a search traversal read on after the served page `spec`."""
    if plan.source_id in PAGE2_SOURCES:
        return jsearch.admits_next_page(spec.page_no, served.new_native_ids)
    if plan.source_id == "adzuna":
        return spec.page_no < adzuna.PAGE_CAP
    return True


async def _search(t: _Traversal, first: RequestSpec, first_id: int | None) -> TraversalResult:
    adapter, plan = t.ctx.adapter, t.plan
    spec, planned_id = first, first_id
    cursor: str | None = None  # the continuation of the last served page (§5.6: the gap keeps it)
    while True:
        result = await t.acquire(spec, planned_id, key=plan.source_id)
        if not isinstance(result, Served):
            return t.result(*_stopped(result), cursor_out=cursor)
        kind, cursor = adapter.continuation(result.page)
        if kind is Continuation.END:
            return t.result("end", None)
        if kind is Continuation.UNKNOWN:
            return t.result("unknown", "unknown_continuation", cursor_out=cursor)
        if t.probe or not _rules_allow_next(plan, spec, result):
            if plan.source_id == "adzuna" and not t.probe:  # a full page at the cap (§5.3.3)
                return t.result("unknown", "unknown_continuation", cursor_out=cursor)
            return t.result("cursor", "page_cap", cursor_out=cursor)
        nxt = adapter.next_page(spec, cursor)
        if nxt is None:  # a continuation the adapter cannot follow: completeness is unknown
            return t.result("unknown", "unknown_continuation", cursor_out=cursor)
        if t.ctx.admission_stopped():
            return t.result("skipped", "shutdown", cursor_out=cursor)
        spec, planned_id = nxt, None


async def _account_after(ctx: AcquireContext, adapter: AccountAdapter, instance_id: int) -> None:
    """The account read after the traversal's last search (§5.3.2), persisted under the owner lock; one that
    fails as `auth` is the breaker's `auth` failure (Ruling FR-l)."""
    try:
        observation = await adapter.account()
    except SourceError as err:  # the read is information only: nothing else depends on it
        log.warning("account_read_failed", error_kind=err.kind, error_code=err.code, after_search=True)
        if failure_kind(err) == "auth":  # a revoked key: visible at once, never a silent read
            await record_account_auth_failure(
                ctx.engine, ctx.owner, instance_id, billing=ctx.billing, now=ctx.clock()
            )
        return
    async with ctx.engine.begin() as conn:
        await ledger.lock_run_owner(conn, ctx.owner)
        await ledger.record_account(conn, observation, settings=ctx.settings, now=ctx.clock())


async def _firehose_state(engine: AsyncEngine, cell_id: int) -> FirehoseState:
    """The cell's coverage: the upper bound of its newest `covered` row and its gaps of every status."""
    newest: datetime | None = None
    gaps: list[Gap] = []
    async with engine.connect() as conn:
        for row in await conn.execute(_INTERVALS, {"c": cell_id}):
            if row.kind == "covered":
                newest = row.upper if newest is None else max(newest, row.upper)
            else:
                gaps.append(Gap(row.lower, row.upper, row.gap_status))
    return FirehoseState(newest, tuple(gaps))


async def _firehose(
    t: _Traversal, adapter: ArbeitnowAdapter, first: RequestSpec, first_id: int | None
) -> TraversalResult:
    ctx, plan = t.ctx, t.plan
    max_age = ctx.settings.scope.max_ad_age_days
    if max_age is None:
        raise ValueError(f"settings version {ctx.settings.settings_version_id} has no scope.max_ad_age_days")
    cap = 1 if t.probe else page_cap(ctx.settings.interval_s)
    state = await _firehose_state(ctx.engine, plan.cell_id)
    times: list[datetime] = []
    spec, planned_id = first, first_id
    end: End
    reason: str | None
    stopped_by: StopReason
    cursor: str | None = None
    while True:
        result = await t.acquire(spec, planned_id, key=plan.source_id)
        if not isinstance(result, Served):
            (end, reason), stopped_by = _stopped(result), "remaining"
            break
        times += [item.item_ts for item in adapter.parse(result.page) if item.item_ts is not None]
        kind, cursor = adapter.continuation(result.page)
        if kind is Continuation.END:
            end, reason, stopped_by = "end", None, "end"
            break
        if adapter.remaining_low(result.page):
            end, reason, stopped_by = "cursor", "allowance", "remaining"
            break
        if len(t.pages) >= cap:
            end, reason, stopped_by = "cursor", "page_cap", "cap"
            break
        if ctx.admission_stopped():
            end, reason, stopped_by = "skipped", "shutdown", "remaining"
            break
        nxt = adapter.next_page(spec, cursor)
        if nxt is None:
            raise InvariantViolation(f"cell {plan.cell_id}: an Arbeitnow page continued without a next page")
        spec, planned_id = nxt, None
    if not t.pages:
        return t.result(end, reason)
    _, changed = conclude(
        state,
        times,
        pages=len(t.pages),
        cap=cap,
        stopped_by=stopped_by,
        now=ctx.clock(),
        max_ad_age_days=max_age,
    )
    return t.result(
        end,
        reason,
        cursor_out=None if end == "end" else cursor,
        firehose=(state, changed),
        covered=covered_span(times, stopped_by=stopped_by),
    )


async def _details(t: _Traversal, adapter: SmartRecruitersAdapter, listing: Served) -> None:
    """The detail calls of a SmartRecruiters list page (§5.3.5), at most `SMARTRECRUITERS_DETAIL.concurrency`
    inside `acquire_page` at a time (the gate: the limiter's key bounds only the sends, the gate also the
    database work around them, so a page of many postings cannot drain the pool); their outcomes are the
    traversal's, and none of them ends it. An error of a call (a lost owner, a bug) is raised as it is, never
    wrapped in the `ExceptionGroup` of the task group: a `FencedOut` must still end the attempt quietly."""
    ids = sorted({native_id for native_id in adapter.peek_native_ids(listing.page) if native_id is not None})
    known: dict[str, tuple[bytes, datetime | None]] = {}
    if ids:
        async with t.ctx.engine.connect() as conn:
            rows = await conn.execute(_DETAILED, {"i": t.plan.instance_id, "ids": ids})
            known = {row.native_id: (bytes(row.list_item_sha256), row.detail_fetched_at) for row in rows}
    specs = adapter.detail_specs(
        listing.page, known, now=t.ctx.clock(), parent_fetch_request_id=listing.fetch_request_id
    )
    gate = asyncio.Semaphore(SMARTRECRUITERS_DETAIL.concurrency)

    async def detail(spec: RequestSpec) -> None:
        async with gate:
            await t.acquire(spec, None, key=SMARTRECRUITERS_DETAIL.key, listing=False)

    try:
        async with asyncio.TaskGroup() as group:
            for spec in specs:
                group.create_task(detail(spec))
    except ExceptionGroup as failed:
        raise _first(failed) from None


def _first(group: BaseExceptionGroup[Exception]) -> Exception:
    """The error a detail task group failed with: its first `FencedOut` (the attempt is lost), else its first
    error, unwrapped from every nesting."""
    leaves: list[Exception] = []
    pending: list[BaseException] = [group]
    while pending:
        error = pending.pop(0)
        if isinstance(error, BaseExceptionGroup):
            pending[:0] = list(error.exceptions)
        elif isinstance(error, Exception):
            leaves.append(error)
    return next((error for error in leaves if isinstance(error, FencedOut)), leaves[0])


async def _board(
    t: _Traversal, adapter: AtsAdapter, first: RequestSpec, first_id: int | None
) -> TraversalResult:
    ctx = t.ctx
    spec, planned_id = first, first_id
    end: End
    reason: str | None
    cursor: str | None = None
    while True:
        result = await t.acquire(spec, planned_id, key=f"ats-host:{adapter.host}")
        if not isinstance(result, Served):
            end, reason = _stopped(result)
            break
        if isinstance(adapter, SmartRecruitersAdapter) and not t.probe and not ctx.admission_stopped():
            await _details(t, adapter, result)
        kind, cursor = adapter.continuation(result.page)
        if kind is Continuation.END:
            end, reason = "end", None
            break
        if t.probe:
            end, reason = "cursor", "page_cap"
            break
        nxt = adapter.next_page(spec, cursor) if len(t.pages) < ATS_PAGE_CAP else None
        if nxt is None:
            end, reason = "unknown", "unknown_continuation"
            break
        if ctx.admission_stopped():
            end, reason = "skipped", "shutdown"
            break
        spec, planned_id = nxt, None
    snapshot = membership(t.pages, adapter, reached_end=end == "end") if t.pages else None
    return t.result(end, reason, cursor_out=None if end == "end" else cursor, snapshot=snapshot)
