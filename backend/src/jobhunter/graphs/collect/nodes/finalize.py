"""Node 10, `finalize` (spec §9.2, §17.2, §17.3, §7.16.4, §17.6): the run's end, owned by the graph.

**One transaction** (`finalize_from_graph`), after the fault point `collect.before_finalize`, in the §3.3.3
lock order: the run's owner row `FOR SHARE` first (`ledger.lock_run_owner`), then the coverage cells
(`coverage.finalize_cells`, the `coverage_cells` updates of §9.2 node 10, applied once because they commit
with the run's end) and the gaps older than the ad age (`coverage.abandon_old_gaps`), the invariants
(`check_invariants`), the partial conditions (`partial_conditions`), the status (`decide_status`), the
alerts, the report (`build_report`) and, last, `runs.finalize_run`. A lost owner raises `FencedOut`, which
propagates having written nothing: the attempt is lost and the run's next owner ends it. After the commit
the end is observed (`runs.observe_graph_end` with `finalize_run`'s duration, ruling T12-e) and logged
`run_finished`; then the dead-man's fast path pings the check (§17.7, M5 Task 5: `deps.deadman.after_run`,
never inside the commit, never raising into the node; the tick's `deadman_ping` sends what it misses).

**Durable rows only** (Task 23, carry 1). Every invariant, partial cause and report section (but the report's
`revisions.targets`, node 9's own counts, `report.targets_section`) is counted from the tables, never from the
state's per-attempt counters, which undercount after a resume or a node's database retry; those counters
appear as they are, informational, under `execution.counters`. A row is "of the run" by its run column (fetch
requests, their responses and raw items, ledger entries, recorded gaps), or, in the tables without one, by a
time at or after the run's `started_at` (its first attempt's start; one collection runs at a time): revisions,
jobs (their revision 1), merges, identity conflicts, duplicate groups, alerts and gaps closed or abandoned. A
posting's availability change is the run's when the run made it: a revival that the run sighted, an absence
from one of the run's snapshots (below).

**Invariants** (§17.2, `check_invariants`, in order):
- I1: every `raw_response` of the run has as many raw items as its `item_count` (each item has one
  disposition, `deferred('job_fenced')` included) and none `pending`;
- I2: every fetch request of the run is terminal;
- I3: every quota window the run's ledger entries touched has counted units (`ledger.used_units`) within
  its `limit_units`, and the run's counted normal units of a paid provider (JSearch, SerpApi) are within the
  per-run cap they were admitted under (`ledger.admitted_run_cap`, §6.4: the cap each reservation recorded,
  never the one of the window as it is now, which a later refinement changes; final review F3);
- I4: every `linked` raw item of the run has an observation whose posting has a job;
- I5 (M3 Task 14, R9), against **the run's tuple** (`runs.llm_tuple_id`): vacuous without one (no profile was
  active, M1 Deviation 11); when the run's tuple is no longer the active one at finalize (a confirm or a save
  activated another during the run), it passes with `{"llm_tuple_id": t, "targets": "tuple_superseded"}` (the
  activation's `create_targets_for_tuple` owns the new tuple); otherwise every current revision of the run's
  jobs (`enqueue_targets.RUN_JOB_SQL`) that is retained and unfenced has a live target of the tuple
  (`targets.NEEDS_TARGET_SQL` is false), detail `{"llm_tuple_id", "revisions", "missing"}`;
- I6: the run's revisions still `awaiting_embedding`, counted (`{"awaiting": n}`): never a failure, the
  partial cause `awaiting_embedding`;
- I7: the outcome reducer saw no conflict. A conflict raises outside any node and never reaches this node
  (`execute_run` ends that run `failed_invariant('I7')`), so the node checks `outcome_conflicts == 0`.

**Status** (`decide_status`): `failed_invariant(<first failed code>)` when any of I1 to I5 or I7 fails, else
`partial(<first cause>)`, else `succeeded`. **Partial conditions** (§7.16.4, `partial_conditions`, in the
order of `PartialCause`): a fetch request of the run `failed` or `ambiguous`; an instance of the run (one
with a fetch request in it) ending the run `breaker_open`, `auth_failed`, `contract_broken` or `not_found`;
a revision of the run left `awaiting_embedding`; a raw item of the run `rejected`. Coverage gaps alone are
reported, never a cause; so is a source error that a node's error handler caught: a warning in
`execution.warnings` (carry 4).

**Alerts** (§17.6): `run.failed_invariant` (critical, subject `run:<id>`) for a `failed_invariant` end;
`quota.limit_breach` (critical, `provider:<name>`) per provider I3 found over its limit; `run.partial` (warn,
`collect:<cause>`) per cause of a `partial` end, and every absent cause's alert resolved (whatever the
status); every open `run.failed` and `run.orphaned_queue` resolved by a `succeeded` end (§17.6 "next
success", Task 24); `quota.fallback_active` resolved when the
run dispatched no fallback request; `coverage.gap_abandoned` by `abandon_old_gaps`.

**Report** (§17.3): `graphs.collect.report.build_report` (M1 Carry-forward 16, M3 Task 14), from the rows
this transaction reads and writes.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, cast, get_args

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.embeddings import NOT_SYNTHETIC_REVISION_SQL
from jobhunter.errors import InvariantViolation
from jobhunter.faults import afault_point
from jobhunter.graphs.collect.deps import CollectDeps
from jobhunter.graphs.collect.nodes import Node
from jobhunter.graphs.collect.nodes.enqueue_targets import RUN_JOB_SQL
from jobhunter.graphs.collect.report import (
    OPEN_REQUEST,
    FallbackRequests,
    alerts_raised,
    build_report,
    fallback_requests,
)
from jobhunter.graphs.collect.state import CollectState, CollectUpdate
from jobhunter.observability.alerts import raise_alert, resolve_alert, resolve_alerts
from jobhunter.observability.logging import get_logger
from jobhunter.quota import ledger
from jobhunter.quota.planner import FALLBACK_ACTIVE_ALERT
from jobhunter.quota.windows import Provider
from jobhunter.run_settings import load_run_settings
from jobhunter.scoring.targets import NEEDS_TARGET_SQL, RETAINED_JOB_SQL, TUPLE_SUPERSEDED
from jobhunter.sources.coverage import abandon_old_gaps, finalize_cells
from jobhunter.types import JSONValue, get_list, get_str
from jobhunter.worker.runs import (
    FAILED_ALERT,
    FAILED_INVARIANT_ALERT,
    ORPHANED_QUEUE_ALERT,
    FinalStatus,
    RunEnd,
    RunOwner,
    finalize_run,
    observe_graph_end,
)

FINALIZE: Final = "finalize"
BEFORE_FINALIZE: Final = "collect.before_finalize"

InvariantCode = Literal["I1", "I2", "I3", "I4", "I5", "I6", "I7"]
PartialCause = Literal[
    "fetch_failed",
    "fetch_ambiguous",
    "source_breaker_open",
    "source_auth_failed",
    "source_contract_broken",
    "source_not_found",
    "awaiting_embedding",
    "raw_item_rejected",
]
PARTIAL_CAUSES: Final[tuple[PartialCause, ...]] = get_args(PartialCause)
# The invariants whose violation fails the run; I6 makes it partial (`awaiting_embedding`) instead.
FAILING: Final = frozenset({"I1", "I2", "I3", "I4", "I5", "I7"})
# The instance states that make a run partial (§7.16.4), by cause.
_HEALTH_CAUSES: Final[Mapping[str, PartialCause]] = {
    "breaker_open": "source_breaker_open",
    "auth_failed": "source_auth_failed",
    "contract_broken": "source_contract_broken",
    "not_found": "source_not_found",
}
# The providers whose normal units a run may spend up to the per-run cap (§6.4: the paid ones).
PER_RUN_CAPPED: Final[frozenset[Provider]] = frozenset({"jsearch", "serpapi"})
PARTIAL_ALERT: Final = "run.partial"
LIMIT_BREACH_ALERT: Final = "quota.limit_breach"
_SERPAPI_SUBJECT: Final = "provider:serpapi"

log = get_logger("jobhunter.graphs.collect.finalize")


@dataclass(frozen=True)
class InvariantResult:
    code: InvariantCode
    ok: bool
    detail: dict[str, JSONValue]


# A raw item of the run is one of a response of one of its fetch requests (`i`, `r`, `f` below).
_RUN = q("SELECT started_at, settings_version_id FROM runs WHERE id = :run")
_I1 = q(
    "SELECT count(*) AS responses, "
    "count(*) FILTER (WHERE c.item_count IS DISTINCT FROM c.items) AS mismatched, "
    "coalesce(sum(c.pending), 0) AS pending FROM (SELECT r.item_count, count(i.id) AS items, "
    "count(i.id) FILTER (WHERE i.disposition = 'pending') AS pending FROM raw_responses r "
    "JOIN fetch_requests f ON f.id = r.fetch_request_id LEFT JOIN raw_items i ON i.raw_response_id = r.id "
    "WHERE f.run_id = :run GROUP BY r.id, r.item_count) c"
)
_I2 = q(
    "SELECT count(*) AS requests, count(*) FILTER (WHERE status = ANY(:open)) AS non_terminal "
    "FROM fetch_requests WHERE run_id = :run"
)
# The quota windows the run's ledger entries belong to.
_RUN_WINDOWS = q(
    "SELECT w.provider, w.window_start, w.limit_units FROM quota_windows w "
    "WHERE (w.provider, w.window_start) IN (SELECT provider, window_start FROM quota_ledger "
    "WHERE run_id = :run) ORDER BY w.provider, w.window_start"
)
_I4 = q(
    "SELECT count(*) AS linked, count(*) FILTER (WHERE j.id IS NULL) AS orphaned FROM raw_items i "
    "JOIN raw_responses r ON r.id = i.raw_response_id JOIN fetch_requests f ON f.id = r.fetch_request_id "
    "LEFT JOIN posting_observations o ON o.id = i.observation_id LEFT JOIN postings p ON p.id = o.posting_id "
    "LEFT JOIN jobs j ON j.id = p.job_id WHERE f.run_id = :run AND i.disposition = 'linked'"
)
_RUN_TUPLE = q(
    "SELECT r.llm_tuple_id, r.started_at, "
    "(SELECT a.llm_tuple_id FROM active_config a WHERE a.id = 1) AS active FROM runs r WHERE r.id = :run"
)
_I5 = q(
    f"SELECT count(*) AS revisions, count(*) FILTER (WHERE {NEEDS_TARGET_SQL}) AS missing "  # noqa: S608
    f"FROM jobs j WHERE {RETAINED_JOB_SQL} AND {RUN_JOB_SQL} AND NOT is_fenced(j.id, :tuple)"
)
# The run's revisions: the user's jobs' only (a synthetic golden-case job's are never embedded, Ruling T19-a).
_REVISIONS = q(
    "SELECT count(*) AS created, count(*) FILTER (WHERE r.embedding_status = 'embedded') AS embedded, "  # noqa: S608
    "count(*) FILTER (WHERE r.embedding_status = 'awaiting_embedding') AS awaiting FROM job_revisions r "
    f"WHERE r.created_at >= (SELECT started_at FROM runs WHERE id = :run) AND {NOT_SYNTHETIC_REVISION_SQL}"
)
_PARTIAL = q(
    "SELECT EXISTS (SELECT 1 FROM fetch_requests WHERE run_id = :run AND status = 'failed') AS failed, "
    "EXISTS (SELECT 1 FROM fetch_requests WHERE run_id = :run AND status = 'ambiguous') AS ambiguous, "
    "ARRAY(SELECT DISTINCT health FROM source_instances WHERE id IN "
    "(SELECT source_instance_id FROM fetch_requests WHERE run_id = :run) ORDER BY health) AS healths, "
    "EXISTS (SELECT 1 FROM raw_items i JOIN raw_responses r ON r.id = i.raw_response_id "
    "JOIN fetch_requests f ON f.id = r.fetch_request_id WHERE f.run_id = :run "
    "AND i.disposition = 'rejected') AS rejected"
)


@dataclass(frozen=True)
class RunRevisions:
    """The revisions created in the run, by embedding status: read once by the run end, for I6, the partial
    cause `awaiting_embedding` and the report's `revisions`."""

    created: int
    embedded: int
    awaiting: int


async def run_revisions(conn: AsyncConnection, run_id: int) -> RunRevisions:
    """The revisions created in the run, by embedding status."""
    row = (await conn.execute(_REVISIONS, {"run": run_id})).one()
    return RunRevisions(int(row.created), int(row.embedded), int(row.awaiting))


async def _i1(conn: AsyncConnection, run_id: int) -> InvariantResult:
    row = (await conn.execute(_I1, {"run": run_id})).one()
    mismatched, pending = int(row.mismatched), int(row.pending)
    detail: dict[str, JSONValue] = {
        "responses": int(row.responses),
        "mismatched": mismatched,
        "pending": pending,
    }
    return InvariantResult("I1", mismatched == 0 and pending == 0, detail)


async def _i2(conn: AsyncConnection, run_id: int) -> InvariantResult:
    row = (await conn.execute(_I2, {"run": run_id, "open": list(OPEN_REQUEST)})).one()
    open_requests = int(row.non_terminal)
    detail: dict[str, JSONValue] = {"requests": int(row.requests), "non_terminal": open_requests}
    return InvariantResult("I2", open_requests == 0, detail)


async def _i3(conn: AsyncConnection, run_id: int) -> InvariantResult:
    breaches: list[JSONValue] = []
    providers: set[Provider] = set()
    windows = (await conn.execute(_RUN_WINDOWS, {"run": run_id})).all()
    for row in windows:
        provider = cast(Provider, row.provider)
        providers.add(provider)
        used = await ledger.used_units(conn, provider, row.window_start)
        if used > int(row.limit_units):
            breach: dict[str, JSONValue] = {
                "provider": provider,
                "kind": "window",
                "window_start": row.window_start.isoformat(),
                "used": used,
                "limit": int(row.limit_units),
            }
            breaches.append(breach)
    for provider in sorted(providers & PER_RUN_CAPPED):
        normal = await ledger.normal_count_for_run(conn, provider, run_id)
        if normal == 0:
            continue
        cap = await ledger.admitted_run_cap(conn, provider, run_id)
        if cap is None:
            raise InvariantViolation(f"run {run_id} has {provider} normal entries without an admitted cap")
        if normal > cap:
            breaches.append({"provider": provider, "kind": "per_run_cap", "used": normal, "limit": cap})
    return InvariantResult("I3", not breaches, {"windows": len(windows), "breaches": breaches})


async def _i4(conn: AsyncConnection, run_id: int) -> InvariantResult:
    row = (await conn.execute(_I4, {"run": run_id})).one()
    orphaned = int(row.orphaned)
    return InvariantResult("I4", orphaned == 0, {"linked": int(row.linked), "orphaned": orphaned})


async def _i5(conn: AsyncConnection, run_id: int) -> InvariantResult:
    run = (await conn.execute(_RUN_TUPLE, {"run": run_id})).one()
    if run.llm_tuple_id is None:
        return InvariantResult("I5", True, {"llm_tuple_id": None})
    tuple_id = int(run.llm_tuple_id)
    if run.active != tuple_id:  # R9: superseded during the run
        return InvariantResult("I5", True, {"llm_tuple_id": tuple_id, "targets": TUPLE_SUPERSEDED})
    found = (await conn.execute(_I5, {"tuple": tuple_id, "started": run.started_at})).one()
    missing = int(found.missing)
    detail: dict[str, JSONValue] = {
        "llm_tuple_id": tuple_id,
        "revisions": int(found.revisions),
        "missing": missing,
    }
    return InvariantResult("I5", missing == 0, detail)


async def check_invariants(
    conn: AsyncConnection, run_id: int, *, outcome_conflicts: int, revisions: RunRevisions
) -> list[InvariantResult]:
    """The §17.2 invariants I1 to I7 of run `run_id`, in order, from its rows (module docstring); I6 from
    `revisions` (`run_revisions`)."""
    return [
        await _i1(conn, run_id),
        await _i2(conn, run_id),
        await _i3(conn, run_id),
        await _i4(conn, run_id),
        await _i5(conn, run_id),
        InvariantResult("I6", revisions.awaiting == 0, {"awaiting": revisions.awaiting}),
        InvariantResult("I7", outcome_conflicts == 0, {"conflicts": outcome_conflicts}),
    ]


async def partial_conditions(
    conn: AsyncConnection, run_id: int, *, revisions: RunRevisions
) -> list[PartialCause]:
    """The §7.16.4 partial conditions run `run_id` meets, in the order of `PartialCause` (module
    docstring); `awaiting_embedding` from `revisions` (`run_revisions`)."""
    row = (await conn.execute(_PARTIAL, {"run": run_id})).one()
    found: set[PartialCause] = {_HEALTH_CAUSES[h] for h in row.healths if h in _HEALTH_CAUSES}
    if row.failed:
        found.add("fetch_failed")
    if row.ambiguous:
        found.add("fetch_ambiguous")
    if revisions.awaiting:
        found.add("awaiting_embedding")
    if row.rejected:
        found.add("raw_item_rejected")
    return [cause for cause in PARTIAL_CAUSES if cause in found]


def decide_status(
    invariants: Sequence[InvariantResult], partial: Sequence[PartialCause]
) -> tuple[FinalStatus, str | None]:
    """`failed_invariant(<first failed code>)` when any of I1 to I5 or I7 fails, else
    `partial(<first cause>)` when any partial condition holds, else `succeeded`."""
    failed = next((r.code for r in invariants if not r.ok and r.code in FAILING), None)
    if failed is not None:
        return "failed_invariant", failed
    if partial:
        return "partial", partial[0]
    return "succeeded", None


# ---- the run end ------------------------------------------------------------------------------------------


async def _alerts(
    conn: AsyncConnection,
    run_id: int,
    status: FinalStatus,
    invariants: Sequence[InvariantResult],
    partial: Sequence[PartialCause],
    *,
    fallback: FallbackRequests,
) -> None:
    """The run end's alerts (module docstring)."""
    if status == "failed_invariant":
        failed: list[JSONValue] = [r.code for r in invariants if not r.ok and r.code in FAILING]
        await raise_alert(
            conn,
            code=FAILED_INVARIANT_ALERT,
            severity="critical",
            subject=f"run:{run_id}",
            message="run invariant violated",
            context={"run_id": run_id, "invariants": failed},
        )
    for result in invariants:
        if result.code == "I3" and not result.ok:
            breached = sorted({get_str(breach, "provider") for breach in get_list(result.detail, "breaches")})
            for provider in breached:
                await raise_alert(
                    conn,
                    code=LIMIT_BREACH_ALERT,
                    severity="critical",
                    subject=f"provider:{provider}",
                    message="counted quota units exceed the limit",
                    context={"run_id": run_id},
                )
    for cause in PARTIAL_CAUSES:
        subject = f"collect:{cause}"
        if cause not in partial:
            await resolve_alert(conn, code=PARTIAL_ALERT, subject=subject)
        elif status == "partial":
            await raise_alert(
                conn,
                code=PARTIAL_ALERT,
                severity="warn",
                subject=subject,
                message="run partial",
                context={"run_id": run_id, "cause": cause},
            )
    if status == "succeeded":
        await resolve_alerts(conn, code=FAILED_ALERT)
        await resolve_alerts(conn, code=ORPHANED_QUEUE_ALERT)
    if not fallback.served:
        await resolve_alert(conn, code=FALLBACK_ACTIVE_ALERT, subject=_SERPAPI_SUBJECT)


async def finalize_from_graph(deps: CollectDeps, owner: RunOwner, state: CollectState) -> FinalStatus:
    """End `owner`'s run (module docstring); its status. Raises `FencedOut`, having written nothing, when
    `owner` no longer owns the run."""
    await afault_point(BEFORE_FINALIZE)
    now = deps.clock()
    async with deps.engine.begin() as conn:
        await ledger.lock_run_owner(conn, owner)
        run = (await conn.execute(_RUN, {"run": owner.run_id})).one()
        settings = await load_run_settings(conn, int(run.settings_version_id))
        # Narrows the type only: `RunSettings.from_settings` always sets `max_ad_age_days` (a settings payload
        # without it takes the default 30, `settings.model.Scope`); the field is `None` only in a scope read
        # from a `scope_versions` payload, which no run settings hold.
        if settings.scope.max_ad_age_days is None:
            raise InvariantViolation(
                f"settings version {settings.settings_version_id} has no max_ad_age_days"
            )
        await finalize_cells(conn, owner.run_id, now=now)
        await abandon_old_gaps(conn, now=now, max_ad_age_days=settings.scope.max_ad_age_days)
        revisions = await run_revisions(conn, owner.run_id)
        invariants = await check_invariants(conn, owner.run_id, outcome_conflicts=0, revisions=revisions)
        partial = await partial_conditions(conn, owner.run_id, revisions=revisions)
        status, reason = decide_status(invariants, partial)
        fallback = await fallback_requests(conn, owner.run_id)
        await _alerts(conn, owner.run_id, status, invariants, partial, fallback=fallback)
        report = await build_report(
            conn,
            owner.run_id,
            invariants=invariants,
            partial=partial,
            revisions=revisions,
            fallback=fallback,
            node_durations=state["node_durations"],
            counters=state["counters"],
            alerts_raised=await alerts_raised(conn, owner.run_id),
        )
        duration = await finalize_run(conn, owner, status=status, reason=reason, report=report)
    observe_graph_end(owner, RunEnd(status=status, duration_s=duration))
    log.info("run_finished", run_id=owner.run_id, attempt_no=owner.attempt_no, status=status, reason=reason)
    if deps.deadman is not None:  # after the commit, and after the end is counted (M5 Task 5)
        await deps.deadman.after_run(owner.run_id)
    return status


def make_finalize(deps: CollectDeps) -> Node:
    """Node 10 over `deps`: `finalize_from_graph` for the state's attempt; `final_status` is its status."""

    async def finalize(state: CollectState) -> CollectUpdate:
        owner = RunOwner(state["run_id"], state["attempt_no"])
        return {"final_status": await finalize_from_graph(deps, owner, state), "steps": [FINALIZE]}

    return finalize
