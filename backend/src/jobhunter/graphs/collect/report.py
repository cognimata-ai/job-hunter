"""The run report (spec §17.3, `runs.report`; M1 Carry-forward 16, plan M3 Task 14, R9): `build_report`, which
node 10 (`nodes.finalize`) calls in its one owner-fenced transaction, and the reads it shares with the run end
(`fallback_requests`, `alerts_raised`).

**Report** (§17.3, `build_report`, `runs.report`): `sources`, `identity`, `revisions`, `quota` and
`execution`, ids, counts and codes only, never ad text or a company name (carry 7): a board's key is
`<ats>/instance:<id>`, never its slug. Per source instance of the run (`<source_id>/<instance_key>`):
`requests` by status, `units` billed, ambiguous and not billed, `items` by disposition and
`items_out_of_scope` by reason (carry 3: SmartRecruiters' in-scope list items are `detail_call`), `postings`
`new` (first seen through the run, `fetch_requests.new_postings` as `finalize_cells` wrote it) and `updated`
(seen before the run, and observed by it with another content than the posting's latest observation from
outside it: a posting the run only sighted again is not updated, ledger L372), `availability` changes by
`<from>→<to>` (the from-state is not stored: where §7.16.2 has several edges into the state, the key names
them all, `active|unknown→closed`) that the run made, each by its own evidence (ledger L372): a revival
(`→ active`) of a posting the run sighted (an observation of one of its raw items, or a member of one of its
snapshots), an `unknown` or `closed` posting absent from an applied snapshot of the run on its board, and
never `→ stale`, which only the stale sweep (a reconciler) makes; `gaps` opened (and not abandoned at once),
closed and abandoned, and the `health` at the end. `quota` per provider of the run: `projected` (its cells the
projection admitted: a request that was not skipped), `used` (counted units) and `window_start`; `fallback`:
`used`, `cells` dispatched and the `reason` (the most common §6.6 outcome of the JSearch cells it serves).
`revisions`: `created`, `embedded` and `awaiting` (the run's revisions by embedding status,
`finalize.run_revisions`) and `targets`, what node 9 did for the run's tuple (`targets_section`): `{"created",
"revived", "cancelled", "skipped": {<reason>: n}, "by_state": {<state>: n}}`, the skip reasons of
`targets.create_or_revive` (`fenced`, `merged`, `not_current`, `tuple_inactive`) and `tuple_superseded` (the
run's revisions left without a target of the run's tuple because a confirm or a save made another tuple active
during the run: the activation's `create_targets_for_tuple` owns those), and `by_state`, the state each
created or revived target was left in (`pending`, `filtered_out`, `deferred_duplicate`), §17.3's "targets
created by state" (Ruling T14-a). Unlike the other sections these are node 9's own counts (`targets_*` of
`CollectState.counters`): no row records whether a target was created or revived by this run. So they
undercount: an attempt resumed after node 9 counts only what its own node 9 did, and a database retry of node
9 within one attempt (`DB_RETRY`) counts only its last pass, to which the chunks the failed pass committed
come back `Unchanged`. And `skipped.tuple_superseded` can count a job that `created` counts too: a revision
node 9 gave a target of the run's tuple in an early chunk, which the activation that superseded the tuple then
cancelled, is still one without a live target of it when a later chunk finds the tuple superseded. I5 checks
the durable outcome, every revision of the run with its target. `execution`: node durations, the invariant
results, the partial causes, the alerts raised (`alerts_raised`), the warnings of the error handler's counters
(`WARNING_COUNTERS`) and the attempt's counters, informational.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.db.transitions import TRANSITIONS
from jobhunter.embeddings import NOT_SYNTHETIC_REVISION_SQL
from jobhunter.graphs.collect.nodes import run_started_at
from jobhunter.graphs.collect.nodes.enqueue_targets import (
    SKIPPED_PREFIX,
    STATE_PREFIX,
    TARGETS_CANCELLED,
    TARGETS_CREATED,
    TARGETS_REVIVED,
)
from jobhunter.quota import ledger
from jobhunter.quota.fallback import cell_outcome
from jobhunter.sources.watchlist_patterns import ATS_TYPES
from jobhunter.types import JSONValue

if TYPE_CHECKING:
    # Only for the annotations: `finalize` imports this module.
    from jobhunter.graphs.collect.nodes.finalize import InvariantResult, PartialCause, RunRevisions

# The counters of `graph.error_handled` (`SOURCE_ERRORS`, `EMBEDDER_ERRORS`): a warning when not zero
# (carry 4).
WARNING_COUNTERS: Final = ("source_errors", "embedder_errors")
# The alert domains of a collection run: `execution.alerts` lists theirs, raised while the run ran.
ALERT_DOMAINS: Final = ("run", "source", "quota", "coverage", "embedder", "ats", "dedup")
# A fetch request not yet terminal (I2 counts them; the fallback's reason reads only terminal ones).
OPEN_REQUEST: Final = ("planned", "reserved", "dispatched")
_UNIT_STATES: Final = ("billed", "ambiguous", "not_billed")

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
# The postings the run observed with another content (`content_hash`) than the posting's latest observation
# from outside the run: the updated postings. One the run first saw has no such observation.
_UPDATED_POSTINGS = q(
    "SELECT f.source_instance_id, count(DISTINCT o.posting_id) AS n FROM raw_items i "
    "JOIN raw_responses r ON r.id = i.raw_response_id JOIN fetch_requests f ON f.id = r.fetch_request_id "
    "JOIN posting_observations o ON o.id = i.observation_id "
    "JOIN LATERAL (SELECT b.content_hash FROM posting_observations b WHERE b.posting_id = o.posting_id "
    "AND NOT EXISTS (SELECT 1 FROM raw_items bi JOIN raw_responses br ON br.id = bi.raw_response_id "
    "JOIN fetch_requests bf ON bf.id = br.fetch_request_id WHERE bi.id = b.raw_item_id AND bf.run_id = :run) "
    "ORDER BY b.observed_at DESC, b.id DESC LIMIT 1) prior ON true "
    "WHERE f.run_id = :run AND o.content_hash <> prior.content_hash GROUP BY 1"
)
# The availability changes the run made (module docstring) among the postings of its instances that changed
# while it ran, but those it first saw: `→ active` sighted by the run (an observation of one of its raw items,
# or a member of one of its snapshots), `unknown` or `closed` absent from an applied snapshot of the run on
# the posting's instance, and never `→ stale` (the stale sweep's alone).
_AVAILABILITY = q(
    "SELECT p.source_instance_id, p.availability, p.availability_reason, count(*) AS n FROM postings p "
    "WHERE p.source_instance_id IN (SELECT source_instance_id FROM fetch_requests WHERE run_id = :run) "
    "AND p.availability_changed_at >= :started AND p.first_seen_at < :started AND CASE p.availability "
    "WHEN 'active' THEN EXISTS (SELECT 1 FROM raw_items i JOIN raw_responses r ON r.id = i.raw_response_id "
    "JOIN fetch_requests f ON f.id = r.fetch_request_id JOIN posting_observations o "
    "ON o.id = i.observation_id WHERE o.posting_id = p.id AND f.run_id = :run) "
    "OR EXISTS (SELECT 1 FROM ats_snapshots s JOIN ats_snapshot_members m ON m.snapshot_id = s.id "
    "WHERE s.run_id = :run AND s.source_instance_id = p.source_instance_id AND m.native_id = p.native_id) "
    "WHEN 'stale' THEN false "
    "ELSE EXISTS (SELECT 1 FROM ats_snapshots s WHERE s.run_id = :run "
    "AND s.source_instance_id = p.source_instance_id AND s.applied_at IS NOT NULL AND NOT EXISTS "
    "(SELECT 1 FROM ats_snapshot_members m WHERE m.snapshot_id = s.id AND m.native_id = p.native_id)) END "
    "GROUP BY 1, 2, 3"
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
# `jobs_created`: the user's jobs only (a synthetic golden-case job is the eval's, Ruling T19-a).
_IDENTITY = q(
    "SELECT (SELECT count(*) FROM job_revisions r WHERE r.revision_no = 1 "  # noqa: S608
    f"AND r.created_at >= :started AND {NOT_SYNTHETIC_REVISION_SQL}) AS jobs_created, "
    "(SELECT count(*) FROM merge_events WHERE kind = 'strong_key_merge' AND created_at >= :started) "
    "AS merges, "
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
    updated: int = 0
    availability: Counter[str] = field(default_factory=Counter)
    gaps: Counter[str] = field(default_factory=Counter)

    def as_json(self) -> dict[str, JSONValue]:
        return {
            "requests": _counts(self.requests),
            "units": {state: self.units[state] for state in _UNIT_STATES},
            "items": _counts(self.items),
            "items_out_of_scope": _counts(self.out_of_scope),
            "postings": {"new": self.new, "updated": self.updated},
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
    for row in await _rows(conn, _UPDATED_POSTINGS, run=run_id):
        by_id[int(row.source_instance_id)].updated = int(row.n)
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


@dataclass(frozen=True)
class FallbackRequests:
    """The run's SerpApi fallback requests (`phase = 'fallback'`): how many it planned and how many cells a
    dispatched one served. Read once by the run end, for the report's `quota.fallback` and the alert."""

    planned: int
    served: int


async def fallback_requests(conn: AsyncConnection, run_id: int) -> FallbackRequests:
    """Run `run_id`'s fallback requests, planned and served (`FallbackRequests`)."""
    row = (await conn.execute(_FALLBACK, {"run": run_id})).one()
    return FallbackRequests(planned=int(row.planned), served=int(row.served))


async def _fallback(conn: AsyncConnection, run_id: int, requests: FallbackRequests) -> dict[str, JSONValue]:
    reasons: Counter[str] = Counter()
    if requests.planned:
        cells = (await conn.execute(_FALLBACK_SOURCES, {"run": run_id, "open": list(OPEN_REQUEST)})).scalars()
        for cell_id in cells.all():
            reasons[await cell_outcome(conn, run_id, int(cell_id))] += 1
    reason = min(reasons, key=lambda outcome: (-reasons[outcome], outcome)) if reasons else None
    return {"used": requests.served > 0, "reason": reason, "cells": requests.served}


async def _quota(conn: AsyncConnection, run_id: int, fallback: FallbackRequests) -> dict[str, JSONValue]:
    params = {"run": run_id, "counted": list(ledger.COUNTED), "providers": ["jsearch", "serpapi", "adzuna"]}
    quota: dict[str, JSONValue] = {}
    for row in (await conn.execute(_QUOTA, params)).all():
        window_start = None if row.window_start is None else row.window_start.isoformat()
        quota[row.provider] = {
            "projected": int(row.projected),
            "used": int(row.used),
            "window_start": window_start,
        }
    quota["fallback"] = await _fallback(conn, run_id, fallback)
    return quota


def _prefixed(counters: Mapping[str, int], prefix: str) -> dict[str, JSONValue]:
    return {
        name.removeprefix(prefix): n for name, n in sorted(counters.items()) if name.startswith(prefix) and n
    }


def targets_section(counters: Mapping[str, int]) -> dict[str, JSONValue]:
    """The report's `revisions.targets` from node 9's counters (module docstring)."""
    return {
        "created": counters.get(TARGETS_CREATED, 0),
        "revived": counters.get(TARGETS_REVIVED, 0),
        "cancelled": counters.get(TARGETS_CANCELLED, 0),
        "skipped": _prefixed(counters, SKIPPED_PREFIX),
        "by_state": _prefixed(counters, STATE_PREFIX),
    }


async def build_report(
    conn: AsyncConnection,
    run_id: int,
    *,
    invariants: "Sequence[InvariantResult]",
    partial: "Sequence[PartialCause]",
    revisions: "RunRevisions",
    fallback: FallbackRequests,
    node_durations: Mapping[str, float],
    counters: Mapping[str, int],
    alerts_raised: Sequence[str],
) -> dict[str, JSONValue]:
    """The §17.3 run report of run `run_id` (module docstring): `sources`, `identity`, `revisions`, `quota`
    and `execution`. `node_durations` and `counters` are the attempt's own (informational)."""
    started = await run_started_at(conn, run_id)
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
            "targets": targets_section(counters),
        },
        "quota": await _quota(conn, run_id, fallback),
        "execution": execution,
    }


async def alerts_raised(conn: AsyncConnection, run_id: int) -> list[str]:
    """The `<code>:<subject>` keys of the collection's alerts raised (or raised again) since the run
    started."""
    params = {"run": run_id, "domains": list(ALERT_DOMAINS)}
    return [str(key) for key in (await conn.execute(_ALERTS, params)).scalars()]
