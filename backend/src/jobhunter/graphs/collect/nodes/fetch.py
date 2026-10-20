"""Nodes 2 and 4, `fetch_primary` and `fetch_fallback`, and their `fetch_cell` Sends (spec §9.2).

The two fan-out nodes record that they ran; their conditional edges (`route_after_primary`,
`route_after_fallback`) send one `Send("fetch_cell", {"fetch_request_id": id, "attempt_no": n})` per planned
request of the phase, or go straight on when there is none. The run's `max_concurrency` (4) bounds the Sends
of a step. After a `fetch_cell`, `route_after_cell` leads the primary phase to `plan_fallback` and the
fallback phase to `parse_normalize` (only the fallback phase has fallback ids).

`fetch_cell` (`traverse_and_record`): loads the planned row, its cell and its run (`load_planned`), builds the
`CellPlan` from the stored spec (its window is the planned one, never recomputed) and the adapter of the
instance, holds the source's semaphore (`deps.fetch_ctx`: a board holds its host's), runs
`traverse.traverse_cell(…, limiter=deps.limiter)`, then records the coverage in one transaction that first
holds the owner row `FOR SHARE` (`coverage.record_search_traversal`, `record_firehose_traversal` or
`record_ats_snapshot`; a board whose traversal a shutdown or a takeover cut short writes no snapshot), then
passes `afault_point("collect.after_coverage_commit")`, and counts in `FETCH_REQUESTS` the pages this attempt
sent (a page a resumed attempt replays from its receipt, or a denied one, is not counted again). It returns
the traversal's page outcomes and counters. It settles its own `fetch_requests` rows and never raises
`SourceError`; a lost owner (`FencedOut`) and bugs propagate.
"""

import asyncio
from dataclasses import dataclass
from typing import Final, Literal, Protocol, cast

from langgraph.types import Send
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.db.sql import q
from jobhunter.errors import InvariantViolation
from jobhunter.faults import afault_point
from jobhunter.graphs.collect.deps import CollectDeps
from jobhunter.graphs.collect.nodes import Node
from jobhunter.graphs.collect.state import CollectState, CollectUpdate, FetchCellInput
from jobhunter.observability.metrics import FETCH_REQUESTS
from jobhunter.quota import ledger
from jobhunter.quota.planner import PROVIDERS
from jobhunter.run_settings import RunSettings, load_run_settings
from jobhunter.sources import coverage
from jobhunter.sources.acquire import AcquireContext
from jobhunter.sources.ats.common import AtsAdapter
from jobhunter.sources.base import CellPlan, RequestSpec, SourceAdapter
from jobhunter.sources.coverage import BUCKETS, SEARCH_SOURCES, Bucket
from jobhunter.sources.instances import InstanceRow, list_instances
from jobhunter.sources.traverse import TraversalResult, traverse_cell
from jobhunter.worker.runs import RunOwner

FETCH_CELL: Final = "fetch_cell"
# The ends of a traversal that a shutdown or a takeover cut short: no snapshot is written for them.
INTERRUPTED: Final = frozenset({"shutdown", "superseded"})

_ROW = q(
    "SELECT f.run_id, f.cell_id, f.source_instance_id, f.phase, f.spec, c.location_key, "
    "qr.text AS query_text, qr.lang AS query_lang, r.attempt_no, r.settings_version_id "
    "FROM fetch_requests f JOIN coverage_cells c ON c.id = f.cell_id "
    "LEFT JOIN queries qr ON qr.id = c.query_id JOIN runs r ON r.id = f.run_id WHERE f.id = :i"
)


class FetchCellNode(Protocol):
    async def __call__(self, state: FetchCellInput) -> CollectUpdate: ...


@dataclass(frozen=True)
class PlannedCell:
    """A planned fetch request with what its traversal needs: the owner it acts for, the instance, the
    `CellPlan` built from the stored spec, and the run's settings."""

    fetch_request_id: int
    owner: RunOwner
    instance: InstanceRow
    plan: CellPlan
    settings: RunSettings


def _bucket(spec: RequestSpec) -> Bucket | None:
    value = spec.params.get("date_posted")
    return cast(Bucket, value) if value in BUCKETS else None


async def load_planned(engine: AsyncEngine, fetch_request_id: int, *, attempt_no: int | None) -> PlannedCell:
    """The planned request `fetch_request_id`, for attempt `attempt_no` of its run (the run's current
    attempt when `None`). Raises `LookupError` for an unknown request and `InvariantViolation` for a planned
    spec without a coverage window (the planner gives every spec one)."""
    async with engine.connect() as conn:
        row = (await conn.execute(_ROW, {"i": fetch_request_id})).first()
        if row is None:
            raise LookupError(f"fetch request {fetch_request_id} does not exist")
        settings = await load_run_settings(conn, int(row.settings_version_id))
        instances = await list_instances(conn)
    instance = next((i for i in instances if i.id == int(row.source_instance_id)), None)
    if instance is None:
        raise LookupError(f"source instance {row.source_instance_id} does not exist")
    spec = RequestSpec.from_json(row.spec)
    if spec.window is None:
        raise InvariantViolation(f"fetch request {fetch_request_id} was planned without a coverage window")
    days = spec.params.get("max_days_old")
    plan = CellPlan(
        cell_id=int(row.cell_id),
        source_id=instance.source_id,
        instance_id=instance.id,
        query_text=row.query_text,
        query_lang=row.query_lang,
        location_key=row.location_key,
        location_label=None,  # the scope's: the stored spec holds the query it made
        radius_km=None,
        phase=row.phase,
        request_class=spec.request_class,
        bucket=_bucket(spec),
        window=spec.window,
        max_days_old=None if days is None else int(days),
    )
    owner = RunOwner(int(row.run_id), int(row.attempt_no) if attempt_no is None else attempt_no)
    return PlannedCell(fetch_request_id, owner, instance, plan, settings)


def acquire_context(deps: CollectDeps, planned: PlannedCell, adapter: SourceAdapter) -> AcquireContext:
    """The acquisition context of a planned cell's traversal on `adapter`."""
    return AcquireContext(
        engine=deps.engine,
        owner=planned.owner,
        adapter=adapter,
        billing=adapter.capabilities().billing,
        provider=PROVIDERS.get(planned.instance.source_id),
        settings=planned.settings,
        clock=deps.clock,
        sleep=deps.sleep,
        admission_stopped=deps.admission_stopped,
    )


def _semaphore(deps: CollectDeps, source_id: str, adapter: SourceAdapter) -> asyncio.Semaphore:
    """The §9.2 semaphore of a traversal: its ATS host's for a board, else its source's."""
    if isinstance(adapter, AtsAdapter):
        return deps.fetch_ctx.host(adapter.host)
    return deps.fetch_ctx.sources[source_id]


def _interrupted(result: TraversalResult) -> bool:
    """A traversal the run's end cut short (a shutdown or a takeover): a board then writes no snapshot, so
    the resumed attempt's own read is the run's snapshot (a snapshot is written once per run)."""
    return result.end == "skipped" and result.reason in INTERRUPTED


async def _record(conn: AsyncConnection, planned: PlannedCell, result: TraversalResult) -> None:
    cell_id, run_id = planned.plan.cell_id, planned.owner.run_id
    if planned.instance.source_id in SEARCH_SOURCES:
        await coverage.record_search_traversal(
            conn, cell_id, window=planned.plan.window, result=result, run_id=run_id
        )
    elif result.firehose is not None:
        state, gaps = result.firehose
        await coverage.record_firehose_traversal(
            conn,
            cell_id,
            state,
            gaps,
            covered=result.covered,
            reason=coverage.gap_reason(result) or "page_cap",  # an END read's gap: earlier reads were capped
            run_id=run_id,
        )
    elif result.snapshot is not None and result.taken_at is not None and not _interrupted(result):
        await coverage.record_ats_snapshot(
            conn, cell_id, planned.instance.id, result.snapshot, run_id=run_id, taken_at=result.taken_at
        )


async def traverse_and_record(
    deps: CollectDeps, fetch_request_id: int, *, attempt_no: int | None = None
) -> TraversalResult:
    """`fetch_cell`'s work (module docstring): the traversal of a planned request and its coverage."""
    planned = await load_planned(deps.engine, fetch_request_id, attempt_no=attempt_no)
    adapter = deps.adapters(planned.instance)
    ctx = acquire_context(deps, planned, adapter)
    async with _semaphore(deps, planned.instance.source_id, adapter):
        result = await traverse_cell(ctx, planned.plan, fetch_request_id, limiter=deps.limiter)
    async with deps.engine.begin() as conn:
        await ledger.lock_run_owner(conn, planned.owner)
        await _record(conn, planned, result)
    await afault_point("collect.after_coverage_commit")
    for outcome in result.sent:  # the pages this attempt sent: never a replayed receipt or a denial
        FETCH_REQUESTS.labels(source=planned.instance.source_id, status=outcome["status"]).inc()
    return result


def make_fetch_cell(deps: CollectDeps) -> FetchCellNode:
    async def fetch_cell(state: FetchCellInput) -> CollectUpdate:
        result = await traverse_and_record(
            deps, state["fetch_request_id"], attempt_no=state.get("attempt_no")
        )
        return {
            "outcomes": {outcome["work_id"]: outcome for outcome in result.outcomes},
            "counters": {"fetch_pages": result.pages, "fetch_items": result.items},
            "steps": [FETCH_CELL],
        }

    return fetch_cell


def _fan_out_node(name: str) -> Node:
    async def node(state: CollectState) -> CollectUpdate:
        return {"steps": [name]}

    return node


def make_fetch_primary(deps: CollectDeps) -> Node:
    """Node 2, the primary fan-out: it records its step, and `route_after_primary` makes its Sends (only
    `fetch_cell` needs `deps`)."""
    del deps
    return _fan_out_node("fetch_primary")


def make_fetch_fallback(deps: CollectDeps) -> Node:
    """Node 4, the fallback fan-out (SerpApi's specs, one traversal at a time by its semaphore): it records
    its step, and `route_after_fallback` makes its Sends."""
    del deps
    return _fan_out_node("fetch_fallback")


def _sends(state: CollectState, ids: list[int]) -> list[Send]:
    attempt = state["attempt_no"]
    return [Send(FETCH_CELL, FetchCellInput(fetch_request_id=i, attempt_no=attempt)) for i in ids]


def route_after_primary(state: CollectState) -> list[Send] | Literal["plan_fallback"]:
    """One `Send` to `fetch_cell` per planned primary request; `plan_fallback` when there is none."""
    ids = state["primary_fetch_request_ids"]
    return _sends(state, ids) if ids else "plan_fallback"


def route_after_fallback(state: CollectState) -> list[Send] | Literal["parse_normalize"]:
    """One `Send` to `fetch_cell` per planned fallback request; `parse_normalize` when there is none."""
    ids = state["fallback_fetch_request_ids"]
    return _sends(state, ids) if ids else "parse_normalize"


def route_after_cell(state: CollectState) -> Literal["plan_fallback", "parse_normalize"]:
    """After the primary traversals `plan_fallback`, after the fallback ones `parse_normalize`: only the
    fallback phase fans out fallback ids."""
    return "parse_normalize" if state["fallback_fetch_request_ids"] else "plan_fallback"
