"""Node 10, `finalize` (spec §9.2, §17.2, §17.3, §7.16.4, §17.6): the run's end, owned by the graph.

**One transaction** (`finalize_from_graph`), after the fault point `collect.before_finalize`, in the §3.3.3
lock order: the run's owner row `FOR SHARE` first (`ledger.lock_run_owner`), then the coverage cells
(`coverage.finalize_cells`, the `coverage_cells` updates of §9.2 node 10, applied once because they commit
with the run's end) and the gaps older than the ad age (`coverage.abandon_old_gaps`), the invariants
(`check_invariants`), the partial conditions (`partial_conditions`), the status (`decide_status`), the
alerts, the report (`build_report`) and, last, `runs.finalize_run`. A lost owner raises `FencedOut`, which
propagates having written nothing: the attempt is lost and the run's next owner ends it. After the commit
the end is observed (`observe_run_ends` with `finalize_run`'s duration, ruling T12-e) and logged
`run_finished`. The dead-man ping of §17.7 arrives in M5 (plan Deviation 12).

**Durable rows only** (Task 23, carry 1). Every invariant, partial cause and report section is counted from
the tables, never from the state's per-attempt counters, which undercount after a resume or a node's
database retry; those counters appear as they are, informational, under `execution.counters`. A row is "of
the run" by its run column (fetch requests, their responses and raw items, ledger entries, recorded gaps),
or, in the tables without one, by a time at or after the run's `started_at` (its first attempt's start;
one collection runs at a time): revisions, jobs (their revision 1), merges, identity conflicts, duplicate
groups, alerts, posting availability changes and gaps closed or abandoned.

**Invariants** (§17.2, `check_invariants`, in order):
- I1: every `raw_response` of the run has as many raw items as its `item_count` (each item has one
  disposition, `deferred('job_fenced')` included) and none `pending`;
- I2: every fetch request of the run is terminal;
- I3: every quota window the run's ledger entries touched has counted units (`ledger.used_units`) within
  its `limit_units`, and the run's counted normal units of a paid provider (JSearch, SerpApi) are within the
  per-run cap of its current window (`pacing.per_run_cap`, §6.4);
- I4: every `linked` raw item of the run has an observation whose posting has a job;
- I5: vacuous while `active_config.llm_tuple_id` is NULL (plan Deviation 11); with a tuple it fails, since
  M1 creates no targets;
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
status); every open `run.failed` resolved by a `succeeded` end; `quota.fallback_active` resolved when the
run dispatched no fallback request; `coverage.gap_abandoned` by `abandon_old_gaps`.

**Report** (§17.3, `build_report`, `runs.report`): `sources`, `identity`, `revisions`, `quota` and
`execution`, ids, counts and codes only, never ad text or a company name (carry 7): a board's key is
`<ats>/instance:<id>`, never its slug. Per source instance of the run (`<source_id>/<instance_key>`):
`requests` by status, `units` billed, ambiguous and not billed, `items` by disposition and
`items_out_of_scope` by reason (carry 3: SmartRecruiters' in-scope list items are `detail_call`), `postings`
`new` (first seen through the run, `fetch_requests.new_postings` as `finalize_cells` wrote it) and `updated`
(the other postings the run observed), `availability` changes by `<from>→<to>` (the from-state is not
stored: where §7.16.2 has several edges into the state, the key names them all, `active|unknown→closed`),
`gaps` opened (and not abandoned at once), closed and abandoned, and the `health` at the end. `quota` per
provider of the run: `projected` (its cells the projection admitted: a request that was not skipped),
`used` (counted units) and `window_start`; `fallback`: `used`, `cells` dispatched and the `reason` (the
most common §6.6 outcome of the JSearch cells it serves).
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal, cast, get_args

from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.db.transitions import TRANSITIONS
from jobhunter.errors import InvariantViolation
from jobhunter.faults import afault_point
from jobhunter.graphs.collect.deps import CollectDeps
from jobhunter.graphs.collect.nodes import Node
from jobhunter.graphs.collect.state import CollectState, CollectUpdate
from jobhunter.observability.alerts import raise_alert, resolve_alert, resolve_alerts
from jobhunter.observability.logging import get_logger
from jobhunter.quota import ledger
from jobhunter.quota.fallback import cell_outcome
from jobhunter.quota.pacing import fair_share, per_run_cap
from jobhunter.quota.planner import FALLBACK_ACTIVE_ALERT
from jobhunter.quota.windows import Provider, current_snapshot
from jobhunter.run_settings import RunSettings, load_run_settings
from jobhunter.sources.coverage import abandon_old_gaps, finalize_cells
from jobhunter.sources.watchlist_patterns import ATS_TYPES
from jobhunter.types import JSONValue, get_list, get_str
from jobhunter.worker.runs import (
    FAILED_ALERT,
    FAILED_INVARIANT_ALERT,
    FinalStatus,
    RunEnd,
    RunOwner,
    finalize_run,
    observe_run_ends,
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
# The counters of `graph.error_handled` (`SOURCE_ERRORS`, `EMBEDDER_ERRORS`): a warning when not zero
# (carry 4).
WARNING_COUNTERS: Final = ("source_errors", "embedder_errors")
# The alert domains of a collection run: `execution.alerts` lists theirs, raised while the run ran.
ALERT_DOMAINS: Final = ("run", "source", "quota", "coverage", "embedder", "ats", "dedup")
PARTIAL_ALERT: Final = "run.partial"
LIMIT_BREACH_ALERT: Final = "quota.limit_breach"
_SERPAPI_SUBJECT: Final = "provider:serpapi"
_UNIT_STATES: Final = ("billed", "ambiguous", "not_billed")
_OPEN_REQUEST: Final = ("planned", "reserved", "dispatched")

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
_ACTIVE_TUPLE = q("SELECT llm_tuple_id FROM active_config WHERE id = 1")
_REVISIONS = q(
    "SELECT count(*) AS created, count(*) FILTER (WHERE embedding_status = 'embedded') AS embedded, "
    "count(*) FILTER (WHERE embedding_status = 'awaiting_embedding') AS awaiting FROM job_revisions "
    "WHERE created_at >= (SELECT started_at FROM runs WHERE id = :run)"
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
class _Revisions:
    created: int
    embedded: int
    awaiting: int


async def _run_revisions(conn: AsyncConnection, run_id: int) -> _Revisions:
    """The revisions created in the run, by embedding status."""
    row = (await conn.execute(_REVISIONS, {"run": run_id})).one()
    return _Revisions(int(row.created), int(row.embedded), int(row.awaiting))


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
    row = (await conn.execute(_I2, {"run": run_id, "open": list(_OPEN_REQUEST)})).one()
    open_requests = int(row.non_terminal)
    detail: dict[str, JSONValue] = {"requests": int(row.requests), "non_terminal": open_requests}
    return InvariantResult("I2", open_requests == 0, detail)


async def _i3(conn: AsyncConnection, run_id: int, settings: RunSettings) -> InvariantResult:
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
        window = await current_snapshot(conn, provider)
        if window is None:
            raise InvariantViolation(f"{provider} has ledger entries of run {run_id} but no current window")
        cap = per_run_cap(settings.quota, fair_share(window, settings.interval_s))
        normal = await ledger.normal_count_for_run(conn, provider, run_id)
        if normal > cap:
            breaches.append({"provider": provider, "kind": "per_run_cap", "used": normal, "limit": cap})
    return InvariantResult("I3", not breaches, {"windows": len(windows), "breaches": breaches})


async def _i4(conn: AsyncConnection, run_id: int) -> InvariantResult:
    row = (await conn.execute(_I4, {"run": run_id})).one()
    orphaned = int(row.orphaned)
    return InvariantResult("I4", orphaned == 0, {"linked": int(row.linked), "orphaned": orphaned})


async def _i5(conn: AsyncConnection) -> InvariantResult:
    tuple_id = (await conn.execute(_ACTIVE_TUPLE)).scalar_one()
    return InvariantResult(
        "I5", tuple_id is None, {"llm_tuple_id": None if tuple_id is None else int(tuple_id)}
    )


async def check_invariants(
    conn: AsyncConnection, run_id: int, *, settings: RunSettings, outcome_conflicts: int
) -> list[InvariantResult]:
    """The §17.2 invariants I1 to I7 of run `run_id`, in order, from its rows (module docstring)."""
    revisions = await _run_revisions(conn, run_id)
    return [
        await _i1(conn, run_id),
        await _i2(conn, run_id),
        await _i3(conn, run_id, settings),
        await _i4(conn, run_id),
        await _i5(conn),
        InvariantResult("I6", revisions.awaiting == 0, {"awaiting": revisions.awaiting}),
        InvariantResult("I7", outcome_conflicts == 0, {"conflicts": outcome_conflicts}),
    ]


async def partial_conditions(conn: AsyncConnection, run_id: int) -> list[PartialCause]:
    """The §7.16.4 partial conditions run `run_id` meets, in the order of `PartialCause` (module
    docstring)."""
    row = (await conn.execute(_PARTIAL, {"run": run_id})).one()
    found: set[PartialCause] = {_HEALTH_CAUSES[h] for h in row.healths if h in _HEALTH_CAUSES}
    if row.failed:
        found.add("fetch_failed")
    if row.ambiguous:
        found.add("fetch_ambiguous")
    if (await _run_revisions(conn, run_id)).awaiting:
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


# ---- the report (§17.3) -----------------------------------------------------------------------------------

_INSTANCES = q(
    "SELECT id, source_id, instance_key, health FROM source_instances WHERE id IN "
    "(SELECT source_instance_id FROM fetch_requests WHERE run_id = :run) ORDER BY id"
)
_REQUESTS = q(
    "SELECT source_instance_id, status, count(*) AS n FROM fetch_requests WHERE run_id = :run GROUP BY 1, 2"
)
_UNITS = q(
    "SELECT f.source_instance_id, l.state, sum(l.units) AS units FROM fetch_requests f "
    "JOIN quota_ledger l ON l.id = f.quota_ledger_id WHERE f.run_id = :run AND l.state = ANY(:states) "
    "GROUP BY 1, 2"
)
_ITEMS = q(
    "SELECT f.source_instance_id, i.disposition, i.reason_code, count(*) AS n FROM raw_items i "
    "JOIN raw_responses r ON r.id = i.raw_response_id JOIN fetch_requests f ON f.id = r.fetch_request_id "
    "WHERE f.run_id = :run GROUP BY 1, 2, 3"
)
# `new_postings` is written by `finalize_cells` (earlier in this transaction) for every succeeded request.
_NEW_POSTINGS = q(
    "SELECT source_instance_id, coalesce(sum(new_postings), 0) AS n FROM fetch_requests "
    "WHERE run_id = :run GROUP BY 1"
)
_OBSERVED_POSTINGS = q(
    "SELECT f.source_instance_id, count(DISTINCT o.posting_id) AS n FROM raw_items i "
    "JOIN raw_responses r ON r.id = i.raw_response_id JOIN fetch_requests f ON f.id = r.fetch_request_id "
    "JOIN posting_observations o ON o.id = i.observation_id WHERE f.run_id = :run GROUP BY 1"
)
# The postings of the run's instances that changed availability while it ran, but those it first saw.
_AVAILABILITY = q(
    "SELECT source_instance_id, availability, availability_reason, count(*) AS n FROM postings "
    "WHERE source_instance_id IN (SELECT source_instance_id FROM fetch_requests WHERE run_id = :run) "
    "AND availability_changed_at >= :started AND first_seen_at < :started GROUP BY 1, 2, 3"
)
# A gap the run recorded already abandoned (`beyond_provider_window`, or an Arbeitnow gap concluded so) was
# closed in its own insert: `closed_at = created_at`.
_GAPS = q(
    "SELECT c.source_instance_id, count(*) FILTER (WHERE g.run_id = :run "
    "AND NOT (g.gap_status = 'abandoned' AND g.closed_at = g.created_at)) AS opened, "
    "count(*) FILTER (WHERE g.gap_status = 'closed' AND g.closed_at >= :started) AS closed, "
    "count(*) FILTER (WHERE g.gap_status = 'abandoned' AND g.closed_at >= :started) AS abandoned "
    "FROM coverage_intervals g JOIN coverage_cells c ON c.id = g.cell_id WHERE g.kind = 'gap' "
    "AND c.source_instance_id IN (SELECT source_instance_id FROM fetch_requests WHERE run_id = :run) "
    "AND (g.run_id = :run OR g.closed_at >= :started) GROUP BY 1"
)
_IDENTITY = q(
    "SELECT (SELECT count(*) FROM job_revisions WHERE revision_no = 1 AND created_at >= :started) "
    "AS jobs_created, (SELECT count(*) FROM merge_events WHERE kind = 'strong_key_merge' "
    "AND created_at >= :started) AS merges, "
    "(SELECT count(*) FROM identity_conflicts WHERE created_at >= :started) AS conflicts, "
    "(SELECT count(*) FROM duplicate_groups WHERE created_at >= :started) AS groups_formed, "
    "(SELECT count(*) FROM duplicate_groups g WHERE g.created_at < :started AND (g.updated_at >= :started "
    "OR EXISTS (SELECT 1 FROM duplicate_group_members m WHERE m.group_id = g.id "
    "AND m.joined_at >= :started))) AS groups_changed, (SELECT count(*) FROM duplicate_groups "
    "WHERE elected_at >= :started "
    "AND election_reason <> 'initial') AS elections"
)
_QUOTA = q(
    "SELECT p.provider, (SELECT count(DISTINCT f.cell_id) FROM fetch_requests f JOIN source_instances i "
    "ON i.id = f.source_instance_id WHERE f.run_id = :run AND i.source_id = p.provider "
    "AND f.status <> 'skipped') AS projected, (SELECT coalesce(sum(l.units), 0) FROM quota_ledger l "
    "WHERE l.run_id = :run AND l.provider = p.provider AND l.state = ANY(:counted)) AS used, "
    "coalesce((SELECT max(l.window_start) FROM quota_ledger l WHERE l.run_id = :run "
    "AND l.provider = p.provider), (SELECT w.window_start FROM quota_windows w "
    "WHERE w.provider = p.provider AND w.is_current)) AS window_start "
    "FROM (SELECT DISTINCT i.source_id AS provider FROM fetch_requests f JOIN source_instances i "
    "ON i.id = f.source_instance_id WHERE f.run_id = :run AND i.source_id = ANY(:providers)) p ORDER BY 1"
)
_FALLBACK = q(
    "SELECT count(*) AS planned, count(DISTINCT cell_id) FILTER (WHERE status IN "
    "('dispatched', 'succeeded', 'failed', 'ambiguous')) AS served FROM fetch_requests "
    "WHERE run_id = :run AND phase = 'fallback'"
)
# The JSearch cells of the run whose SerpApi counterpart (the same query and location) the fallback planned,
# with every request of the run terminal (`cell_outcome` reads only a terminal one; I2 reports the others).
_FALLBACK_SOURCES = q(
    "SELECT DISTINCT j.id FROM fetch_requests f JOIN coverage_cells s ON s.id = f.cell_id "
    "JOIN coverage_cells j ON j.query_id IS NOT DISTINCT FROM s.query_id "
    "AND j.location_key IS NOT DISTINCT FROM s.location_key "
    "JOIN source_instances ji ON ji.id = j.source_instance_id AND ji.source_id = 'jsearch' "
    "WHERE f.run_id = :run AND f.phase = 'fallback' "
    "AND EXISTS (SELECT 1 FROM fetch_requests g WHERE g.run_id = :run AND g.cell_id = j.id) "
    "AND NOT EXISTS (SELECT 1 FROM fetch_requests g WHERE g.run_id = :run AND g.cell_id = j.id "
    "AND g.status = ANY(:open)) ORDER BY j.id"
)
_ALERTS = q(
    "SELECT DISTINCT dedup_key FROM alerts WHERE last_at >= (SELECT started_at FROM runs WHERE id = :run) "
    "AND split_part(code, '.', 1) = ANY(:domains) ORDER BY dedup_key"
)


def _counts(counter: Mapping[str, int]) -> dict[str, JSONValue]:
    return {name: n for name, n in sorted(counter.items()) if n}


def _source_key(source_id: str, instance_key: str, instance_id: int) -> str:
    """`<source_id>/<instance_key>`; a board's key is its instance id, never its slug (a company's name)."""
    if source_id in ATS_TYPES:
        return f"{source_id}/instance:{instance_id}"
    return f"{source_id}/{instance_key}"


_POSTING_EDGES: Final = TRANSITIONS["postings"].allowed


def _availability_key(to_state: str, reason: str | None) -> str:
    """The `<from>→<to>` key of a posting found in `to_state` with `reason`: the §7.16.2 edges into the state
    name the from-state, all of them when there are several (the from-state is not stored); a confirmed mass
    disappearance closes only `unknown` postings."""
    if to_state == "closed" and reason == "mass_disappearance_confirmed":
        return "unknown→closed"
    sources = sorted(source for source, target in _POSTING_EDGES if target == to_state)
    return f"{'|'.join(sources)}→{to_state}"


@dataclass
class _Source:
    """One source instance's part of the run (`build_report`'s `sources` entry)."""

    health: str
    requests: Counter[str] = field(default_factory=Counter)
    units: Counter[str] = field(default_factory=Counter)
    items: Counter[str] = field(default_factory=Counter)
    out_of_scope: Counter[str] = field(default_factory=Counter)
    new: int = 0
    observed: int = 0
    availability: Counter[str] = field(default_factory=Counter)
    gaps: Counter[str] = field(default_factory=Counter)

    def as_json(self) -> dict[str, JSONValue]:
        return {
            "requests": _counts(self.requests),
            "units": {state: self.units[state] for state in _UNIT_STATES},
            "items": _counts(self.items),
            "items_out_of_scope": _counts(self.out_of_scope),
            "postings": {"new": self.new, "updated": max(0, self.observed - self.new)},
            "availability": _counts(self.availability),
            "gaps": {kind: self.gaps[kind] for kind in ("opened", "closed", "abandoned")},
            "health": self.health,
        }


async def _rows(conn: AsyncConnection, statement: Any, **params: object) -> Sequence[Row[Any]]:
    return (await conn.execute(statement, params)).all()


async def _sources(conn: AsyncConnection, run_id: int, started: datetime) -> dict[str, JSONValue]:
    by_id: dict[int, _Source] = {}
    keys: dict[int, str] = {}
    for row in await _rows(conn, _INSTANCES, run=run_id):
        by_id[int(row.id)] = _Source(health=row.health)
        keys[int(row.id)] = _source_key(row.source_id, row.instance_key, int(row.id))
    for row in await _rows(conn, _REQUESTS, run=run_id):
        by_id[int(row.source_instance_id)].requests[row.status] += int(row.n)
    for row in await _rows(conn, _UNITS, run=run_id, states=list(_UNIT_STATES)):
        by_id[int(row.source_instance_id)].units[row.state] += int(row.units)
    for row in await _rows(conn, _ITEMS, run=run_id):
        source = by_id[int(row.source_instance_id)]
        source.items[row.disposition] += int(row.n)
        if row.disposition == "out_of_scope":
            source.out_of_scope[row.reason_code] += int(row.n)
    for row in await _rows(conn, _NEW_POSTINGS, run=run_id):
        by_id[int(row.source_instance_id)].new = int(row.n)
    for row in await _rows(conn, _OBSERVED_POSTINGS, run=run_id):
        by_id[int(row.source_instance_id)].observed = int(row.n)
    for row in await _rows(conn, _AVAILABILITY, run=run_id, started=started):
        key = _availability_key(row.availability, row.availability_reason)
        by_id[int(row.source_instance_id)].availability[key] += int(row.n)
    for row in await _rows(conn, _GAPS, run=run_id, started=started):
        by_id[int(row.source_instance_id)].gaps.update(
            opened=int(row.opened), closed=int(row.closed), abandoned=int(row.abandoned)
        )
    return {keys[instance_id]: source.as_json() for instance_id, source in by_id.items()}


async def _identity(conn: AsyncConnection, started: datetime) -> dict[str, JSONValue]:
    row = (await conn.execute(_IDENTITY, {"started": started})).one()
    names = ("jobs_created", "merges", "conflicts", "groups_formed", "groups_changed", "elections")
    return {name: int(getattr(row, name)) for name in names}


async def _fallback(conn: AsyncConnection, run_id: int) -> dict[str, JSONValue]:
    row = (await conn.execute(_FALLBACK, {"run": run_id})).one()
    reasons: Counter[str] = Counter()
    if int(row.planned):
        cells = (
            await conn.execute(_FALLBACK_SOURCES, {"run": run_id, "open": list(_OPEN_REQUEST)})
        ).scalars()
        for cell_id in cells.all():
            reasons[await cell_outcome(conn, run_id, int(cell_id))] += 1
    reason = min(reasons, key=lambda outcome: (-reasons[outcome], outcome)) if reasons else None
    return {"used": int(row.served) > 0, "reason": reason, "cells": int(row.served)}


async def _quota(conn: AsyncConnection, run_id: int) -> dict[str, JSONValue]:
    params = {"run": run_id, "counted": list(ledger.COUNTED), "providers": ["jsearch", "serpapi", "adzuna"]}
    quota: dict[str, JSONValue] = {}
    for row in (await conn.execute(_QUOTA, params)).all():
        window_start = None if row.window_start is None else row.window_start.isoformat()
        quota[row.provider] = {
            "projected": int(row.projected),
            "used": int(row.used),
            "window_start": window_start,
        }
    quota["fallback"] = await _fallback(conn, run_id)
    return quota


async def build_report(
    conn: AsyncConnection,
    run_id: int,
    *,
    invariants: Sequence[InvariantResult],
    partial: Sequence[PartialCause],
    node_durations: Mapping[str, float],
    counters: Mapping[str, int],
    alerts_raised: Sequence[str],
) -> dict[str, JSONValue]:
    """The §17.3 run report of run `run_id` (module docstring): `sources`, `identity`, `revisions`, `quota`
    and `execution`. `node_durations` and `counters` are the attempt's own (informational)."""
    started: datetime = (await conn.execute(_RUN, {"run": run_id})).one().started_at
    revisions = await _run_revisions(conn, run_id)
    warnings: list[JSONValue] = [
        {"code": name, "count": counters[name]} for name in WARNING_COUNTERS if counters.get(name, 0)
    ]
    execution: dict[str, JSONValue] = {
        "nodes": {name: round(seconds, 3) for name, seconds in sorted(node_durations.items())},
        "invariants": {r.code: {"ok": r.ok, "detail": r.detail} for r in invariants},
        "partial": [*partial],
        "alerts": [*sorted(alerts_raised)],
        "warnings": warnings,
        "counters": {name: n for name, n in sorted(counters.items())},
    }
    return {
        "sources": await _sources(conn, run_id, started),
        "identity": await _identity(conn, started),
        "revisions": {
            "created": revisions.created,
            "embedded": revisions.embedded,
            "awaiting": revisions.awaiting,
            "targets": {},  # M3 creates them (plan Deviation 11)
        },
        "quota": await _quota(conn, run_id),
        "execution": execution,
    }


async def alerts_raised(conn: AsyncConnection, run_id: int) -> list[str]:
    """The `<code>:<subject>` keys of the collection's alerts raised (or raised again) since the run
    started."""
    params = {"run": run_id, "domains": list(ALERT_DOMAINS)}
    return [str(key) for key in (await conn.execute(_ALERTS, params)).scalars()]


# ---- the run end ------------------------------------------------------------------------------------------


async def _alerts(
    conn: AsyncConnection,
    run_id: int,
    status: FinalStatus,
    invariants: Sequence[InvariantResult],
    partial: Sequence[PartialCause],
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
    if not int((await conn.execute(_FALLBACK, {"run": run_id})).one().served):
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
        if settings.scope.max_ad_age_days is None:  # `load_run_settings` refuses such a version
            raise InvariantViolation(
                f"settings version {settings.settings_version_id} has no max_ad_age_days"
            )
        await finalize_cells(conn, owner.run_id, now=now)
        await abandon_old_gaps(conn, now=now, max_ad_age_days=settings.scope.max_ad_age_days)
        invariants = await check_invariants(conn, owner.run_id, settings=settings, outcome_conflicts=0)
        partial = await partial_conditions(conn, owner.run_id)
        status, reason = decide_status(invariants, partial)
        await _alerts(conn, owner.run_id, status, invariants, partial)
        report = await build_report(
            conn,
            owner.run_id,
            invariants=invariants,
            partial=partial,
            node_durations=state["node_durations"],
            counters=state["counters"],
            alerts_raised=await alerts_raised(conn, owner.run_id),
        )
        duration = await finalize_run(conn, owner, status=status, reason=reason, report=report)
    observe_run_ends([RunEnd(status=status, duration_s=duration)])
    log.info("run_finished", run_id=owner.run_id, attempt_no=owner.attempt_no, status=status, reason=reason)
    return status


def make_finalize(deps: CollectDeps) -> Node:
    """Node 10 over `deps`: `finalize_from_graph` for the state's attempt; `final_status` is its status."""

    async def finalize(state: CollectState) -> CollectUpdate:
        owner = RunOwner(state["run_id"], state["attempt_no"])
        return {"final_status": await finalize_from_graph(deps, owner, state), "steps": [FINALIZE]}

    return finalize
