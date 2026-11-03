"""Identity resolution (spec §8.6, §7.16.1, §7.16.2, §20.2): each pending raw item becomes a job's sighting.

**`resolve_item`** runs the §8.6 transaction of one raw item on the caller's connection, inside a savepoint,
in the lock order of §3.3.3: `active_config` `FOR SHARE` (`filters.lock_active_scopes`, the first statement) →
the job rows → the run's owner row (`ledger.lock_run_owner`) → the other domain rows → `ranking_state`.

1. **Fence and idempotency.** An item no longer `pending` or `deferred` is left as it is: its disposition is
   returned (a replay of a linked item writes nothing). An active `source` purge fence for the item's source
   makes it `out_of_scope('source_purged')`; an observation that already names the item links it to that
   observation. Both are owner writes, after `lock_run_owner`.
2. **Read before writing** (plain reads): the sighting time `t = raw_responses.fetched_at`; the posting `P`
   by `(source_instance_id, native_id)`; the keys `K` (`collect_keys`) and their `ad_keys` owners; with `P`'s
   job, every job followed through `merged_into_job_id` to its root: the job set `J`.
3. **Decide the job.** The jobs of `J`, with the duplicate-group co-members of every job of `J` but the
   lowest (the jobs a merge can take away: its target is always the lowest id), are locked `FOR UPDATE` in
   one ordered statement (`merge.lock_jobs`; the transaction writes every job of `J`, so the spec's `FOR
   SHARE` would only be upgraded: plan Deviation 21; Ruling T18-c), then the owner row. Each read-and-lock
   round runs in a savepoint: when a job of the set was merged (or deleted) between the read and the lock,
   the co-members changed, or `P`'s job or an owner of a key of `K` is no longer what the round read (an
   unmerge moves `P` back and re-owns the keys without merging any job of `J`, so only a re-read of both
   under the lock sees it: M1 Carry-forward 10), the savepoint is rolled back, which releases that round's
   row locks, and the round runs again with a fresh read, so the second set is locked in id order too (at
   most `_ROOT_ROUNDS`, then `merge.CoMemberChanged`). A fenced job (`merge.fenced_jobs`) makes the item
   `deferred('job_fenced')`, `normalized` kept, and nothing else is written. Then (`_decide`):
   - `J` empty: a new job, its row from `select_canonical` of this observation alone (its provenance is
     written in step 7, when the posting and observation ids exist), `restored_from_tombstone_id` from the
     first key of `K` whose `sha256(ns ‖ 0x00 ‖ value)` is in `tombstone_keys`, and then its
     `user_job_actions` row from that tombstone's timestamps (`job_tombstones.user_actions`: `seen_at`,
     `saved_at`, `applied_at`, `hidden_at`, each an ISO 8601 time with its offset; never a note), so a hidden
     ad that is re-posted stays hidden and an applied one stays applied (M3 Task 22, L3188). A value that is
     no such time is dropped (`tombstone_actions_unreadable`, its field names only), and a tombstone without a
     timestamp writes no row;
   - a `different` decision between two jobs of `J`: no merge and no conflict; the posting's own job, or a
     new job for a new posting (an interpretation: the spec names only the posting's own job);
   - an exclusive conflict (the observation and a job other than the posting's, or two jobs of `J`, carry
     disjoint values of one exclusive namespace): no merge; the posting's job, or a new job for a new
     posting, and an open conflict naming the other jobs (`merge.open_conflict`, which repeats none);
   - one job: it; several: the lowest id, the others merged into it in step 6; the bulk rows of the follow-up
     holds of every merged job's targets are locked next (`merge.lock_target_holds`, before any write: the
     merges cancel those targets, Ruling P52).
4. **Posting and observation.** A new posting is inserted `ON CONFLICT DO NOTHING` (a posting another
   transaction inserted meanwhile is locked and used when it is on the decided job) with `seen_count = 1`
   and `t` as its first and last sighting and availability change; an existing one is locked `FOR UPDATE`
   (it stays on its job, which is the decided job or merged into it). The observation has `observed_at = t`,
   the response's `body_sha256` and the item's own `material_hash` as `content_hash`. The sighting of an
   existing posting (§7.16.2), the only place one is counted, is `availability.rules.apply_sighting`:
   `seen_count + 1`; `last_seen_at := greatest`; `latest_observation_id` only when `t ≥` the latest's
   `observed_at`; when `t > availability_changed_at`, the absence reset (also only after `first_absent_at`)
   and the revival of a `stale`, `unknown` or `closed` posting through `transition()` (a `closed` one logs
   `posting_reopened`), with `availability_changed_at := t` (Ruling T18-d, a plan deviation: the sighting's
   own time, so later receipts compare `t > availability_changed_at` in one time domain). An observation
   of a SmartRecruiters detail call (its response's request is a `detail` one) also sets the posting's
   change markers of §5.3.5, which the traversal's `detail_specs` reads: `list_item_sha256`, the
   `item_sha256` of the posting's list item on the detail call's parent list page (`element_sha256` of its
   element, the hash `detail_specs` compares), and `detail_fetched_at = t`. Both move together and only
   forward (`detail_fetched_at < t`), so an older observation resolved later never moves them back.
5. **Keys, then provenance**: the missing `ad_keys` rows owned by the decided job (`ON CONFLICT DO NOTHING`:
   ownership never changes here), then the `ad_key_sources` rows of the posting.
6. **Merges or the conflict**: `merge.merge_jobs(strong_key_merge, recompute=False, locked=<the rows of
   step 3>)` per merged job, in id order, with the keys and the observation as evidence; or the conflict row.
   A revision the recompute of step 7 supersedes is node 9's to retarget, later in the same run
   (`enqueue_targets`: the recompute writes `jobs.updated_at`), so `merge.supersede_revision` is not run here
   (Ruling T22-e).
7. **Projections**: the decided job's availability and seen bounds (`merge.derive_from_postings`), its
   canonical fields and revision (`revisions.recompute_job`, once, after the last merge), and the det-facts
   change hook (`filters.refilter_job_live`, M3 Task 15, with what step 1 read under `active_config`): its
   hard-filter results for the active and the pending scope, the current instances of its published results
   of the active tuple for both scopes, and its unpublished targets' moves `filtered_out ⇄ pending`. Then
   `afault_point(IN_ITEM_TX)` (crash case C6), the item `→ linked` with its `observation_id` and
   `normalized = NULL`, and, last, `ranking.bump` when the list changed: a job created, a merge, a new current
   revision or a list column (`RecomputeResult`), the job's availability or first sighting (Ruling T18-a,
   §7.1), or an instance or a target the hook wrote or moved.

A `DomainError` other than `FencedOut` rolls the savepoint back, and the item becomes
`rejected('identity_error')` in a savepoint of its own (owner row first); `FencedOut` propagates. Every final
transition of the item passes the fault point first.

**`resolve_run_items`** resolves the run's `pending` items, oldest first, one transaction each;
**`resolve_deferred_items`** the `deferred` items of earlier runs whose jobs are no longer fenced, oldest
first, at most `limit`, under the current run's owner. The selection re-derives each item's job set in SQL
(its posting's job and its `raw_keys` owners, followed to their roots) and leaves out every item a fence
still holds, so a large fenced backlog never starves the released items; an item fenced again before its
transaction stays `deferred` and is counted so. Both return the resolutions that occurred, by count, and add
the jobs created, merges, conflicts and new revisions to `tally` when it is given. Every timestamp written is
the database's `now()` or `t` (carry 3; Task 17 ruling T17-a; Ruling T18-b: no caller clock is taken but
`resolve_run_items`' `clock`, which times the pass for its log line).
"""

import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
from typing import Final, Literal, get_args

from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.availability import rules
from jobhunter.db import ranking
from jobhunter.db.sql import q
from jobhunter.db.transitions import CLEAR_REASON, transition
from jobhunter.dedup import merge
from jobhunter.dedup.user_actions import ACTION_TIMESTAMPS
from jobhunter.errors import DomainError, FencedOut, InvariantViolation
from jobhunter.faults import afault_point
from jobhunter.normalize.fields import CanonicalFields, ObservationView, WatchlistDisplay, select_canonical
from jobhunter.normalize.keys import AdKey
from jobhunter.observability.logging import get_logger
from jobhunter.quota import ledger
from jobhunter.scoring import filters
from jobhunter.sources import items
from jobhunter.sources.base import NormalizedPosting
from jobhunter.types import JSONValue
from jobhunter.worker.runs import RunOwner

Resolution = Literal["linked", "out_of_scope", "rejected", "deferred"]
RESOLUTIONS: Final[tuple[Resolution, ...]] = get_args(Resolution)
IN_ITEM_TX: Final = "collect.identity.in_item_tx"
BEFORE_JOB_LOCK: Final = "collect.identity.before_job_lock"  # between step 2's reads and step 3's lock
JOB_FENCED: Final = "job_fenced"
DEFERRED_LIMIT: Final = 1000
_ROOT_ROUNDS: Final = 3  # read-then-lock rounds before a job set that keeps changing is an invariant breach
_PLACEHOLDER_ID: Final = 0  # the posting and observation ids of the view a new job's row is selected from

_ITEM: Final = q(
    "SELECT i.disposition, i.normalized, r.fetched_at, r.body_sha256, r.source_instance_id, s.source_id, "
    "w.display_name AS watchlist_display, "
    "(SELECT o.id FROM posting_observations o WHERE o.raw_item_id = i.id) AS observation_id, "
    # a SmartRecruiters detail observation: its list item on the parent list page (the first, as
    # `detail_specs` reads it), whose `item_sha256` is the posting's change marker (§5.3.5)
    "(SELECT l.item_sha256 FROM fetch_requests f JOIN fetch_requests p ON p.id = f.parent_fetch_request_id "
    "JOIN raw_items l ON l.raw_response_id = p.raw_response_id WHERE f.id = r.fetch_request_id "
    "AND f.operation = 'detail' AND l.native_id = i.native_id ORDER BY l.item_index LIMIT 1) "
    "AS list_item_sha256 "
    "FROM raw_items i JOIN raw_responses r ON r.id = i.raw_response_id "
    "JOIN source_instances s ON s.id = r.source_instance_id "
    "LEFT JOIN watchlist_companies w ON w.id = s.watchlist_company_id WHERE i.id = :item"
)
_SOURCE_FENCED: Final = q(
    "SELECT EXISTS (SELECT 1 FROM purge_fences WHERE kind = 'source' AND source_id = :source "
    "AND released_at IS NULL)"
)
_POSTING: Final = q(
    "SELECT id, job_id FROM postings WHERE source_instance_id = :instance AND native_id = :native_id"
)
_KEY_OWNERS: Final = q(
    "SELECT a.key_ns, a.key_value, a.job_id FROM ad_keys a "
    "JOIN unnest(CAST(:ns AS text[]), CAST(:values AS text[])) AS k(ns, value) "
    "ON a.key_ns = k.ns AND a.key_value = k.value"
)
_ROOTS: Final = q(
    "WITH RECURSIVE up (start, id, parent) AS ("
    "SELECT j.id, j.id, j.merged_into_job_id FROM jobs j WHERE j.id = ANY(CAST(:ids AS bigint[])) "
    "UNION ALL SELECT up.start, j.id, j.merged_into_job_id FROM up JOIN jobs j ON j.id = up.parent) "
    "SELECT start, id AS root FROM up WHERE parent IS NULL"
)
_EXCLUSIVE_KEYS: Final = q(
    "SELECT key_ns, key_value, job_id FROM ad_keys WHERE exclusive AND job_id = ANY(CAST(:jobs AS bigint[]))"
)
_DIFFERENT: Final = q(
    "SELECT EXISTS (SELECT 1 FROM dedup_decisions WHERE decision = 'different' "
    "AND job_a = ANY(CAST(:jobs AS bigint[])) AND job_b = ANY(CAST(:jobs AS bigint[])))"
)
_TOMBSTONES: Final = q(
    "SELECT k.key_hash, k.tombstone_id, t.user_actions FROM tombstone_keys k "
    "JOIN job_tombstones t ON t.id = k.tombstone_id WHERE k.key_hash = ANY(CAST(:hashes AS bytea[]))"
)
_RESTORE_ACTIONS: Final = q(
    "INSERT INTO user_job_actions (job_id, seen_at, saved_at, applied_at, hidden_at) "
    "VALUES (:job, :seen_at, :saved_at, :applied_at, :hidden_at)"
)
_INSERT_JOB: Final = q(
    "INSERT INTO jobs (company_key, company_display, title, title_norm, seniority_tokens, "
    "structured_seniority, locations, location_kind, work_mode_possible, work_mode_source, remote_countries, "
    "employment_types, employment_source, salary, industry_structured, ad_language, completeness, "
    "posted_at_effective, "
    "posted_precision, first_seen_at, last_seen_at, availability, availability_changed_at, source_ids, "
    "field_provenance, det_facts_hash, restored_from_tombstone_id) VALUES (:company_key, :company_display, "
    ":title, :title_norm, CAST(:seniority_tokens AS text[]), :structured_seniority, :locations, "
    ":location_kind, CAST(:work_mode_possible AS text[]), :work_mode_source, "
    "CAST(:remote_countries AS text[]), CAST(:employment_types AS text[]), :employment_source, "
    "NULLIF(:salary, 'null'::jsonb), :industry_structured, :ad_language, :completeness, "
    ":posted_at_effective, :posted_precision, :t, :t, 'active', :t, CAST(:source_ids AS text[]), '{}', "
    ":det_facts_hash, :tombstone) RETURNING id",
    jsonb=["locations", "salary"],
)
_INSERT_POSTING: Final = q(
    "INSERT INTO postings (source_instance_id, native_id, job_id, first_seen_at, last_seen_at, seen_count, "
    "availability, availability_changed_at, outbound_url_private) VALUES (:instance, :native_id, :job, :t, "
    ":t, 1, 'active', :t, :outbound) ON CONFLICT (source_instance_id, native_id) DO NOTHING RETURNING id"
)
_LOCK_POSTING: Final = q(
    "SELECT id, job_id FROM postings WHERE source_instance_id = :instance AND native_id = :native_id "
    "FOR UPDATE"
)
_INSERT_OBSERVATION: Final = q(
    "INSERT INTO posting_observations (posting_id, raw_item_id, source_instance_id, raw_response_sha256, "
    "observed_at, normalized, content_hash, completeness, quality) VALUES (:posting, :item, :instance, :sha, "
    ":t, :normalized, :content_hash, :completeness, :quality) RETURNING id",
    jsonb=["normalized"],
)
_FIRST_LATEST: Final = q("UPDATE postings SET latest_observation_id = :observation WHERE id = :posting")
# The SmartRecruiters change markers of a detail observation (§5.3.5), together and only forward.
_DETAIL_MARKERS: Final = q(
    "UPDATE postings SET list_item_sha256 = :sha, detail_fetched_at = :t WHERE id = :posting "
    "AND (detail_fetched_at IS NULL OR detail_fetched_at < :t)"
)
_INSERT_KEY: Final = q(
    "INSERT INTO ad_keys (key_ns, key_value, exclusive, job_id) VALUES (:ns, :value, :exclusive, :job) "
    "ON CONFLICT (key_ns, key_value) DO NOTHING"
)
_INSERT_KEY_SOURCE: Final = q(
    "INSERT INTO ad_key_sources (key_ns, key_value, posting_id, observation_id, first_seen_at) "
    "VALUES (:ns, :value, :posting, :observation, :t) ON CONFLICT (key_ns, key_value, posting_id) DO NOTHING"
)
_DISPOSITION: Final = q("SELECT disposition FROM raw_items WHERE id = :item FOR UPDATE")
# The deferred items of earlier runs whose job set (the posting's job and the owners of `raw_keys`, followed
# to their roots: `J` of §8.6 step 2) no fence holds, oldest first.
_RELEASED_DEFERRED: Final = q(
    "SELECT i.id FROM raw_items i JOIN raw_responses r ON r.id = i.raw_response_id "
    "JOIN fetch_requests f ON f.id = r.fetch_request_id "
    "WHERE i.disposition = 'deferred' AND f.run_id <> :run AND NOT EXISTS ("
    "WITH RECURSIVE up (id, parent) AS ("
    "SELECT j.id, j.merged_into_job_id FROM jobs j WHERE j.id IN ("
    "SELECT p.job_id FROM postings p WHERE p.source_instance_id = r.source_instance_id "
    "AND p.native_id = i.normalized ->> 'native_id' "
    "UNION SELECT a.job_id FROM jsonb_array_elements(i.normalized -> 'raw_keys') AS k(key) "
    "JOIN ad_keys a ON a.key_ns = k.key ->> 'ns' AND a.key_value = k.key ->> 'value') "
    "UNION ALL SELECT j.id, j.merged_into_job_id FROM up JOIN jobs j ON j.id = up.parent) "
    "SELECT 1 FROM up WHERE up.parent IS NULL AND is_fenced(up.id)) "
    "ORDER BY i.id LIMIT :limit"
)

log = get_logger("jobhunter.dedup.identity")


@dataclass(frozen=True)
class ResolveOutcome:
    """What resolving one raw item did, for the node's counters."""

    resolution: Resolution
    job_created: bool = False
    merges: int = 0
    conflict: bool = False
    revision_created: bool = False


@dataclass
class IdentityTally:
    """The node's counters beside the resolutions: jobs created, merges, conflicts opened, new revisions."""

    jobs_created: int = 0
    merges: int = 0
    conflicts: int = 0
    revisions_new: int = 0

    def add(self, outcome: ResolveOutcome) -> None:
        self.jobs_created += outcome.job_created
        self.merges += outcome.merges
        self.conflicts += outcome.conflict
        self.revisions_new += outcome.revision_created


def collect_keys(n: NormalizedPosting) -> list[AdKey]:
    """The observation's strong keys (§8.4): `raw_keys` without repeats, the exclusive namespaces first, each
    group in the adapter's order."""
    return sorted(dict.fromkeys(n.raw_keys), key=lambda key: not key.exclusive)


def tombstone_key_hash(key: AdKey) -> bytes:
    """`tombstone_keys.key_hash` of a key: `sha256(ns ‖ 0x00 ‖ value)` (§7.9; M5's tombstoning writes it)."""
    return hashlib.sha256(key.ns.encode() + b"\x00" + key.value.encode()).digest()


def _tombstone_actions(user_actions: JSONValue) -> tuple[dict[str, datetime], tuple[str, ...]]:
    """The user-action timestamps of `job_tombstones.user_actions` (step 3): each of `ACTION_TIMESTAMPS`
    that holds an ISO 8601 time with its offset, and the names of those that hold something else (dropped,
    never guessed). Any other key is ignored."""
    if not isinstance(user_actions, dict):
        return {}, ()
    found: dict[str, datetime] = {}
    unreadable: list[str] = []
    for name in ACTION_TIMESTAMPS:
        value = user_actions.get(name)
        if value is None:
            continue
        try:
            parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            parsed = None
        if parsed is None or parsed.tzinfo is None:
            unreadable.append(name)
        else:
            found[name] = parsed
    return found, tuple(unreadable)


@dataclass(frozen=True)
class _JobSet:
    posting: Row[tuple[object, ...]] | None  # P: id, job_id
    posting_job: int | None  # the root of P's job
    jobs: tuple[int, ...]  # J, ascending, locked
    owners: Mapping[AdKey, int]  # the root that owns each key of K that has an owner
    locked: frozenset[int]  # every job row step 3 locked: J and the co-members of J but its lowest


@dataclass(frozen=True)
class _Decision:
    target: int | None  # None: a new job
    rule: str
    merged: tuple[int, ...] = ()
    candidates: tuple[int, ...] = ()
    conflicting: tuple[JSONValue, ...] = ()


@dataclass(frozen=True)
class _Item:
    raw_item_id: int
    from_states: tuple[str, ...]
    normalized: dict[str, JSONValue]
    posting: NormalizedPosting
    t: datetime
    raw_response_sha256: bytes
    source_instance_id: int
    source_id: str
    watchlist_display: str | None
    list_item_sha256: bytes | None  # a SmartRecruiters detail observation's list-item marker (§5.3.5)


def _exclusive_conflicts(
    observation: Mapping[str, frozenset[str]],
    owned: Mapping[int, Mapping[str, frozenset[str]]],
    jobs: Sequence[int],
    posting_job: int | None,
) -> list[JSONValue]:
    """The exclusive namespaces in which two holders carry disjoint values (§8.6 step 3): the observation and
    a job other than the posting's own, or two jobs of `J`; the evidence of each, in namespace order."""
    holders: list[tuple[int | None, Mapping[str, frozenset[str]]]] = [(None, observation)]  # None: it
    holders += [(job, owned.get(job, {})) for job in jobs]
    conflicting: list[JSONValue] = []
    for ns in sorted({ns for _, values in holders for ns in values}):
        clash = any(
            values_x.get(ns) and values_y.get(ns) and not (values_x[ns] & values_y[ns])
            for (x, values_x), (y, values_y) in combinations(holders, 2)
            if not (x is None and y == posting_job)  # the observation is the first holder
        )
        if clash:
            jobs_json: list[JSONValue] = [
                {"job_id": job, "values": _json_strings(values[ns])}
                for job, values in holders
                if job is not None and values.get(ns)
            ]
            conflicting.append(
                {"key_ns": ns, "observation": _json_strings(observation.get(ns, ())), "jobs": jobs_json}
            )
    return conflicting


def _json_strings(values: Iterable[str]) -> list[JSONValue]:
    return list[JSONValue](sorted(values))


async def _decide(conn: AsyncConnection, found: _JobSet, keys: Sequence[AdKey]) -> _Decision:
    jobs, own = found.jobs, found.posting_job
    if not jobs:
        return _Decision(None, "new")
    rows = (await conn.execute(_EXCLUSIVE_KEYS, {"jobs": list(jobs)})).all()
    owned: dict[int, dict[str, frozenset[str]]] = {}
    for row in rows:
        by_ns = owned.setdefault(int(row.job_id), {})
        by_ns[row.key_ns] = by_ns.get(row.key_ns, frozenset()) | {row.key_value}
    observation: dict[str, frozenset[str]] = {}
    for key in keys:
        if key.exclusive:
            observation[key.ns] = observation.get(key.ns, frozenset()) | {key.value}
    if len(jobs) > 1 and (await conn.execute(_DIFFERENT, {"jobs": list(jobs)})).scalar_one():
        return _Decision(own, "different")
    if conflicting := _exclusive_conflicts(observation, owned, jobs, own):
        others = tuple(job for job in jobs if job != own)
        return _Decision(own, "exclusive_conflict", candidates=others, conflicting=tuple(conflicting))
    if len(jobs) == 1:
        return _Decision(jobs[0], "single")
    return _Decision(jobs[0], "strong_key_merge", merged=jobs[1:])


@dataclass(frozen=True)
class _Read:
    posting: Row[tuple[object, ...]] | None  # P: id, job_id
    direct: Mapping[AdKey, int]  # the job that owns each key of K that has an owner

    @property
    def state(self) -> tuple[tuple[int, int] | None, Mapping[AdKey, int]]:
        """What a re-read compares: `P` and its job, and the owner of each key."""
        posting = None if self.posting is None else (int(self.posting.id), int(self.posting.job_id))
        return posting, self.direct


async def _read(conn: AsyncConnection, item: _Item, keys: Sequence[AdKey]) -> _Read:
    """Step 2's plain reads: `P` and the owners of `K`."""
    posting = (
        await conn.execute(
            _POSTING, {"instance": item.source_instance_id, "native_id": item.posting.native_id}
        )
    ).first()
    direct = {
        AdKey(row.key_ns, row.key_value): int(row.job_id)
        for row in await conn.execute(
            _KEY_OWNERS, {"ns": [key.ns for key in keys], "values": [key.value for key in keys]}
        )
    }
    return _Read(posting, direct)


async def _read_and_lock(conn: AsyncConnection, item: _Item, keys: Sequence[AdKey]) -> _JobSet | None:
    """One round of step 2's reads and step 3's lock: `P`, the owners of `K` and their roots `J`, then `J` and
    the group co-members of every job of `J` but the lowest in one ordered statement. `None` when what was
    read no longer holds under the lock: a job of `J` merged or gone, other co-members, or `P`'s job or a
    key's owner moved (an unmerge, which merges no job of `J`; module docstring)."""
    read = await _read(conn, item, keys)
    posting, direct = read.posting, read.direct
    starts = {*direct.values(), *([int(posting.job_id)] if posting is not None else [])}
    roots = (
        {int(row.start): int(row.root) for row in await conn.execute(_ROOTS, {"ids": sorted(starts)})}
        if starts
        else {}
    )
    jobs = tuple(sorted(set(roots.values())))
    mergeable = jobs[1:]  # a merge's target is the lowest id: only these can be merged away
    await afault_point(BEFORE_JOB_LOCK)
    locked = await merge.lock_jobs(conn, jobs, with_groups_of=mergeable)
    if any(job not in locked or locked[job] is not None for job in jobs):
        return None
    if not await merge.co_members(conn, mergeable) <= locked.keys():
        return None
    if (await _read(conn, item, keys)).state != read.state:  # the re-read under the lock (M1 Carry 10)
        return None
    return _JobSet(
        posting=posting,
        posting_job=None if posting is None else roots[int(posting.job_id)],
        jobs=jobs,
        owners={key: roots[job] for key, job in direct.items()},
        locked=frozenset(locked),
    )


async def _lock_job_set(conn: AsyncConnection, item: _Item, keys: Sequence[AdKey]) -> _JobSet:
    """Step 2 and step 3's lock, each round in a savepoint: a round whose reads no longer hold is rolled back,
    which releases its row locks, so the next round locks its whole set in id order."""
    for _ in range(_ROOT_ROUNDS):
        attempt = await conn.begin_nested()
        try:
            found = await _read_and_lock(conn, item, keys)
        except BaseException:
            await attempt.rollback()
            raise
        if found is not None:
            await attempt.commit()  # RELEASE SAVEPOINT: the locks stay with the transaction
            return found
        await attempt.rollback()
        log.info("job_set_changed_under_the_lock", raw_item_id=item.raw_item_id)
    raise merge.CoMemberChanged(f"raw item {item.raw_item_id}: its job set kept changing under the lock")


async def _dispose(
    conn: AsyncConnection,
    raw_item_id: int,
    from_states: Sequence[str],
    to_state: Resolution,
    *,
    reason: str | None = None,
    observation_id: int | None = None,
) -> None:
    """The item's final transition, after the crash point `IN_ITEM_TX`; `normalized` dropped unless the item
    stays `deferred`."""
    await afault_point(IN_ITEM_TX)
    set_: dict[str, object] = {} if to_state == "deferred" else {"normalized": None}
    if observation_id is not None:
        set_["observation_id"] = observation_id
    await transition(
        conn,
        "raw_items",
        raw_item_id,
        from_states,
        to_state,
        reason=CLEAR_REASON if reason is None else reason,
        set_=set_,
    )


def _view(item: _Item) -> tuple[ObservationView, WatchlistDisplay | None]:
    view = ObservationView(
        posting_id=_PLACEHOLDER_ID,
        observation_id=_PLACEHOLDER_ID,
        source_instance_id=item.source_instance_id,
        source_id=item.source_id,
        observed_at=item.t,
        normalized=item.posting,
        completeness=item.posting.description_completeness,
        quality=item.posting.description_quality,
    )
    display = None if item.watchlist_display is None else WatchlistDisplay(item.watchlist_display, view)
    return view, display


async def _new_job(conn: AsyncConnection, item: _Item, fields: CanonicalFields, keys: Sequence[AdKey]) -> int:
    """A job from this observation alone (step 3), flagged by the first key a tombstone holds, with that
    tombstone's user-action timestamps."""
    hashes = [tombstone_key_hash(key) for key in keys]
    rows = (await conn.execute(_TOMBSTONES, {"hashes": hashes})).all()
    found = {bytes(r.key_hash): int(r.tombstone_id) for r in rows}
    actions_of: dict[int, JSONValue] = {int(r.tombstone_id): r.user_actions for r in rows}
    tombstone = next((found[h] for h in hashes if h in found), None)
    params = {
        "company_key": fields.company_key,
        "company_display": fields.company_display,
        "title": fields.title,
        "title_norm": fields.title_norm,
        "seniority_tokens": sorted(fields.seniority_tokens),
        "structured_seniority": fields.structured_seniority,
        "locations": [location.model_dump(mode="json") for location in fields.locations],
        "location_kind": fields.location_kind,
        "work_mode_possible": sorted(fields.work_mode_possible),
        "work_mode_source": fields.work_mode_source,
        "remote_countries": list(fields.remote_countries),
        "employment_types": list(fields.employment_types),
        "employment_source": fields.employment_source,
        "salary": None if fields.salary is None else fields.salary.model_dump(mode="json"),
        "industry_structured": fields.industry_structured,
        "ad_language": fields.ad_language,
        "completeness": fields.completeness,
        "posted_at_effective": fields.posted_at_effective,
        "posted_precision": fields.posted_precision,
        "t": item.t,
        "source_ids": list(fields.source_ids),
        "det_facts_hash": fields.det_facts_hash,
        "tombstone": tombstone,
    }
    job_id = int((await conn.execute(_INSERT_JOB, params)).scalar_one())
    if tombstone is not None:
        await _restore_actions(conn, job_id, tombstone, actions_of[tombstone])
    return job_id


async def _restore_actions(
    conn: AsyncConnection, job_id: int, tombstone_id: int, user_actions: JSONValue
) -> None:
    """The new job's `user_job_actions` row from the tombstone's timestamps (step 3); none without one."""
    restored, unreadable = _tombstone_actions(user_actions)
    if unreadable:
        log.warning("tombstone_actions_unreadable", tombstone_id=tombstone_id, fields=list(unreadable))
    if restored:
        stamps = {name: restored.get(name) for name in ACTION_TIMESTAMPS}
        await conn.execute(_RESTORE_ACTIONS, {"job": job_id, **stamps})
    log.info(
        "job_restored_from_tombstone", job_id=job_id, tombstone_id=tombstone_id, actions=sorted(restored)
    )


async def _posting_for(
    conn: AsyncConnection, item: _Item, found: _JobSet, decision: _Decision, job: int
) -> tuple[int, bool]:
    """Step 4's posting: `(id, inserted)`. An existing posting stays where it is: on the decided job, or on a
    job merged into it in step 6."""
    key = {"instance": item.source_instance_id, "native_id": item.posting.native_id}
    if found.posting is None:
        inserted = (
            await conn.execute(
                _INSERT_POSTING,
                {**key, "job": job, "t": item.t, "outbound": item.posting.outbound_url_private},
            )
        ).scalar()
        if inserted is not None:
            return int(inserted), True
    posting = (await conn.execute(_LOCK_POSTING, key)).one()
    if int(posting.job_id) != job and int(posting.job_id) not in decision.merged:
        raise InvariantViolation(
            f"posting {posting.id} is on job {posting.job_id}, not on the decided job {job}"
        )
    return int(posting.id), False


async def _link(
    conn: AsyncConnection, owner: RunOwner, item: _Item, active: filters.ActiveScopes
) -> ResolveOutcome:
    """Steps 2 to 7 of an item that no fence and no earlier observation settled."""
    keys = collect_keys(item.posting)
    found = await _lock_job_set(conn, item, keys)
    await ledger.lock_run_owner(conn, owner)  # the owner row after the job rows (§3.3.3)
    if fenced := await merge.fenced_jobs(conn, found.jobs):
        log.info("raw_item_deferred", raw_item_id=item.raw_item_id, fenced_job_ids=fenced)
        await _dispose(conn, item.raw_item_id, item.from_states, "deferred", reason=JOB_FENCED)
        return ResolveOutcome("deferred")
    decision = await _decide(conn, found, keys)
    if decision.merged:  # the bulk rows of every merge's targets, before any write (module docstring)
        await merge.lock_target_holds(conn, decision.merged)
    view, display = _view(item)
    fields = select_canonical([view], first_seen_at=item.t, watchlist_display=display)
    created = decision.target is None
    job = await _new_job(conn, item, fields, keys) if decision.target is None else decision.target

    posting_id, inserted = await _posting_for(conn, item, found, decision, job)
    observation_id = int(
        (
            await conn.execute(
                _INSERT_OBSERVATION,
                {
                    "posting": posting_id,
                    "item": item.raw_item_id,
                    "instance": item.source_instance_id,
                    "sha": item.raw_response_sha256,
                    "t": item.t,
                    "normalized": item.normalized,
                    "content_hash": fields.material_hash,
                    "completeness": item.posting.description_completeness,
                    "quality": item.posting.description_quality,
                },
            )
        ).scalar_one()
    )
    if inserted:
        await conn.execute(_FIRST_LATEST, {"posting": posting_id, "observation": observation_id})
    else:
        await rules.apply_sighting(conn, posting_id, observed_at=item.t, observation_id=observation_id)
    if item.list_item_sha256 is not None:
        markers = {"posting": posting_id, "sha": item.list_item_sha256, "t": item.t}
        await conn.execute(_DETAIL_MARKERS, markers)

    if keys:
        await conn.execute(
            _INSERT_KEY, [{"ns": k.ns, "value": k.value, "exclusive": k.exclusive, "job": job} for k in keys]
        )
        await conn.execute(
            _INSERT_KEY_SOURCE,
            [
                {
                    "ns": k.ns,
                    "value": k.value,
                    "posting": posting_id,
                    "observation": observation_id,
                    "t": item.t,
                }
                for k in keys
            ],
        )

    evidence: dict[str, JSONValue] = {
        "rule": "strong_key",
        "raw_item_id": item.raw_item_id,
        "observation_id": observation_id,
        "keys": [{"key_ns": k.ns, "key_value": k.value, "job_id": found.owners.get(k)} for k in keys],
    }
    for merged in decision.merged:
        await merge.merge_jobs(
            conn,
            from_job_id=merged,
            into_job_id=job,
            kind="strong_key_merge",
            evidence=evidence,
            recompute=False,
            locked=found.locked,
        )
    conflict = None
    if decision.candidates:
        conflict = await merge.open_conflict(
            conn,
            posting_id=posting_id,
            posting_job_id=job,
            candidate_job_ids=decision.candidates,
            evidence={"raw_item_id": item.raw_item_id, "conflicting_keys": list(decision.conflicting)},
        )

    derived, result = await merge.recompute_rollups(conn, job)
    live = await filters.refilter_job_live(conn, job, active=active)
    await _dispose(conn, item.raw_item_id, item.from_states, "linked", observation_id=observation_id)
    if (
        created
        or decision.merged
        or result.current_changed
        or result.list_changed
        or derived.changed
        or live.changed
    ):
        await ranking.bump(conn)
    log.debug(
        "raw_item_linked",
        raw_item_id=item.raw_item_id,
        job_id=job,
        rule=decision.rule,
        job_created=created,
        merges=len(decision.merged),
    )
    return ResolveOutcome(
        "linked",
        job_created=created,
        merges=len(decision.merged),
        conflict=conflict is not None,
        revision_created=result.revision_created,
    )


async def _resolve(conn: AsyncConnection, owner: RunOwner, raw_item_id: int) -> ResolveOutcome:
    active = await filters.lock_active_scopes(conn)  # active_config FOR SHARE: the first lock (§3.3.3, §8.6)
    row = (await conn.execute(_ITEM, {"item": raw_item_id})).first()
    if row is None:
        raise LookupError(f"raw item {raw_item_id} does not exist")
    if row.disposition not in ("pending", "deferred"):
        if row.disposition not in RESOLUTIONS:
            raise LookupError(f"raw item {raw_item_id} is {row.disposition}: there is nothing to resolve")
        resolution: Resolution = row.disposition
        return ResolveOutcome(resolution)
    from_states = (str(row.disposition),)
    if (await conn.execute(_SOURCE_FENCED, {"source": row.source_id})).scalar_one():
        await ledger.lock_run_owner(conn, owner)
        await _dispose(conn, raw_item_id, from_states, "out_of_scope", reason=items.SOURCE_PURGED)
        return ResolveOutcome("out_of_scope")
    if row.observation_id is not None:
        await ledger.lock_run_owner(conn, owner)
        await _dispose(conn, raw_item_id, from_states, "linked", observation_id=int(row.observation_id))
        return ResolveOutcome("linked")
    item = _Item(
        raw_item_id=raw_item_id,
        from_states=from_states,
        normalized=row.normalized,
        posting=NormalizedPosting.model_validate(row.normalized),
        t=row.fetched_at,
        raw_response_sha256=bytes(row.body_sha256),
        source_instance_id=int(row.source_instance_id),
        source_id=row.source_id,
        watchlist_display=row.watchlist_display,
        list_item_sha256=None if row.list_item_sha256 is None else bytes(row.list_item_sha256),
    )
    return await _link(conn, owner, item, active)


async def _reject(conn: AsyncConnection, owner: RunOwner, raw_item_id: int) -> ResolveOutcome:
    """`rejected('identity_error')`, under the owner row, from whichever of `pending` and `deferred` it is."""
    await ledger.lock_run_owner(conn, owner)
    disposition = (await conn.execute(_DISPOSITION, {"item": raw_item_id})).scalar_one()
    await _dispose(conn, raw_item_id, (str(disposition),), "rejected", reason=items.IDENTITY_ERROR)
    return ResolveOutcome("rejected")


async def _resolve_counted(conn: AsyncConnection, owner: RunOwner, raw_item_id: int) -> ResolveOutcome:
    try:
        async with conn.begin_nested():
            return await _resolve(conn, owner, raw_item_id)
    except FencedOut:
        raise
    except DomainError as err:  # its message names tables, ids and states only (errors.py)
        log.warning("identity_error", raw_item_id=raw_item_id, error=type(err).__name__, detail=str(err))
        async with conn.begin_nested():
            return await _reject(conn, owner, raw_item_id)


async def resolve_item(conn: AsyncConnection, *, owner: RunOwner, raw_item_id: int) -> Resolution:
    """Resolve one raw item (the §8.6 transaction; module docstring) on the caller's connection, in a
    savepoint; its resolution. Raises `FencedOut` for a superseded owner (or an active global purge fence),
    having written nothing, and `LookupError` for an item that does not exist or cannot be resolved."""
    return (await _resolve_counted(conn, owner, raw_item_id)).resolution


async def _resolve_each(
    engine: AsyncEngine,
    owner: RunOwner,
    raw_item_ids: Sequence[int],
    tally: IdentityTally | None,
) -> dict[Resolution, int]:
    counts: dict[Resolution, int] = {}
    for raw_item_id in raw_item_ids:
        async with engine.begin() as conn:
            outcome = await _resolve_counted(conn, owner, raw_item_id)
        counts[outcome.resolution] = counts.get(outcome.resolution, 0) + 1
        if tally is not None:
            tally.add(outcome)
    return counts


async def resolve_run_items(
    engine: AsyncEngine,
    owner: RunOwner,
    *,
    clock: Callable[[], datetime],
    tally: IdentityTally | None = None,
) -> dict[Resolution, int]:
    """Resolve every `pending` item of `owner`'s run, oldest first, one transaction each; the resolutions
    by count. A resumed attempt continues from the items' stored `normalized` (§7.16.1). `clock` times the
    pass for its log line."""
    started = clock()
    async with engine.connect() as conn:
        pending = await items.pending_items_of_run(conn, owner.run_id)
    counts = await _resolve_each(engine, owner, pending, tally)
    log.info(
        "run_items_resolved",
        run_id=owner.run_id,
        items=len(pending),
        seconds=round((clock() - started).total_seconds(), 3),
        **counts,
    )
    return counts


async def resolve_deferred_items(
    engine: AsyncEngine,
    owner: RunOwner,
    *,
    limit: int = DEFERRED_LIMIT,
    tally: IdentityTally | None = None,
) -> dict[Resolution, int]:
    """Re-resolve up to `limit` `deferred` items of earlier runs whose jobs no fence holds any more, oldest
    first, one transaction each under `owner`'s predicate (§7.16.1); the resolutions by count (an item fenced
    again before its transaction counts as `deferred`)."""
    async with engine.connect() as conn:
        deferred = [
            int(raw_item_id)
            for raw_item_id in (
                await conn.execute(_RELEASED_DEFERRED, {"run": owner.run_id, "limit": limit})
            ).scalars()
        ]
    counts = await _resolve_each(engine, owner, deferred, tally)
    if deferred:
        log.info("deferred_items_resolved", run_id=owner.run_id, items=len(deferred), **counts)
    return counts
