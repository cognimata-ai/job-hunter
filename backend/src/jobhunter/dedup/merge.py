"""Merges, unmerges and identity conflicts (spec §8.6.1, §8.6.2, §8.6 steps 3 and 6, §20.2).

**Locks (Ruling T18-c).** Every job row a transaction writes or references is locked by ONE ordered
statement, `lock_jobs`: `… WHERE id = ANY(:ids) OR id IN (<the duplicate-group co-members of with_groups_of>)
ORDER BY id FOR UPDATE`, before the owner row when there is one (§3.3.3). A job that may be merged away
brings its co-members, because step 5 writes their rows (the last member of a dissolved group) or names
them (an elected representative). Nothing is ever locked lazily: `_leave_group` raises `InvariantViolation`
for a member the transaction does not hold.

**`merge_jobs`** runs the merge procedure of §8.6.1 in the caller's transaction. Standalone, it locks the
pair and the members of both jobs' groups in that one statement; a caller that already holds its jobs
(identity resolution, `resolve_conflict`) passes them as `locked`, and the pair must be among them. A fenced
job (`is_fenced`, §20.2) is never merged (`FencedOut("purge_fence")`), and a job already merged is never
merged again (`InvariantViolation`).

1. **Snapshot** into `merge_events.evidence["snapshot"]`, beside the caller's evidence: the `ad_keys` rows
   the from-job owns and the group memberships of both jobs. The `user_job_actions` rows (step 3) and the
   from-job's non-terminal targets (step 4) arrive with their tables in M4 and M3 (plan Deviation 7).
2. **Move** every posting of the from-job (its ids are the event's `posting_ids`) and every key it owns to
   the into-job. `ad_key_sources` is untouched.
5. **Groups**: the from-job leaves its duplicate group. A group left with one member is `dissolved`
   (`member_left`) and that member leaves it too; a group whose representative left elects the eligible
   member that sorts first by §8.7 (availability, completeness, source rank, description length, first
   seen, id; eligible: unmerged and not `closed`, the targets of M3 aside), or records `no_active_member`.
   Every member must be held by the transaction (`InvariantViolation` otherwise). The revalidation of the
   members against a new representative is Task 20's (`groups.leave_group`).
6. **Conflicts**: every open conflict naming the from-job names the into-job instead. One left without a
   candidate other than its posting's job is `resolved_merged`, its ids unchanged, and its
   `dedup.identity_conflict` alert is resolved.
7. `from.merged_into_job_id := into`, and the from-job's open `dedup.field_conflict` alerts are resolved:
   its selection no longer stands (carry 7). With `recompute` (the default) the into-job's availability and
   seen bounds (`derive_from_postings`) and its canonical fields (`revisions.recompute_job`) are recomputed.
   Identity resolution passes `recompute=False` and recomputes once, after its last merge. The caller
   writes the hard-filter results (it holds `active_config` first, §3.3.3) and bumps the ranking.

**`unmerge`** reverses one merge event (§8.6.2), in the order `active_config` `FOR SHARE` → both job rows and
their groups' members `FOR UPDATE` in one ordered statement → the event row → the other rows →
`ranking_state`: the event's postings that are on the into-job move back and `from.merged_into_job_id :=
NULL` (step 1); every key either job owns is owned by the job of its earliest `ad_key_sources` row (by
`first_seen_at`, then posting id) among the two jobs' postings, and only the keys the two jobs own (step 2;
a plan deviation, Ruling T18-f: a key a third job owns after a conflict keeps its owner);
`dedup_decisions(different, 'unmerge')` (step 3); both jobs' availability, seen bounds and canonical fields
are recomputed, with their hard-filter results (step 5; the from-job's stored
field-conflict markers are dropped first, because the merge resolved their alerts, so a conflict that still
holds is raised again); a `merge_event(unmerge)` and the original's `reversed_by_event_id` (step 6). Step 4
(user actions) arrives with M4. Only the latest state is reversed: the from-job must still be merged into
the into-job, and the into-job unmerged.

**`resolve_conflict`** settles an open conflict in the same lock order (for `merge`, the candidates' groups'
members are locked with the jobs): `separate` writes `dedup_decisions(different, 'conflict_resolution')` for
each (posting job, candidate) pair, the pairs the user judged, never between two candidates (Ruling T18-e),
and moves it to `resolved_separate` (the list does not change, so no bump); `merge` merges every candidate
into the posting's job (`user_confirmed_merge`; the last merge's step 6 resolves it), then recomputes that
job, writes its hard-filter results and bumps the ranking.

`derive_from_postings` gives a job the best availability of its postings (`active > unknown > stale >
closed`, §7.16.2; `availability_changed_at := now()` when it changes) and its postings' first and last
sighting; a `posted_at_effective` beyond a lowered `first_seen_at` is capped to it (the CHECK of §7.9),
and the recompute that follows selects it again. It is the one place a job's availability is derived:
`availability.rules.recompute_job_availability` (the snapshot effects and the stale sweep) reuses it.

Every timestamp written is the database's `now()`, the clock domain of `runs.started_at` (carry 3; Task 17
ruling T17-a; Ruling T18-b: no function here takes a caller clock).
"""

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db import ranking
from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.dedup import revisions
from jobhunter.dedup.revisions import FIELD_CONFLICT_ALERT, RecomputeResult
from jobhunter.errors import FencedOut, InvariantViolation
from jobhunter.normalize.fields import CONFLICT_FIELDS, CONFLICT_RULE
from jobhunter.observability.alerts import raise_alert, resolve_alert
from jobhunter.observability.logging import get_logger
from jobhunter.scoring import filters
from jobhunter.types import JSONValue

MergeKind = Literal["strong_key_merge", "user_confirmed_merge"]
IDENTITY_CONFLICT_ALERT: Final = "dedup.identity_conflict"
# The order of a job's availability (§7.16.2): the first state its postings hold.
AVAILABILITY_ORDER: Final = ("active", "unknown", "stale", "closed")
_MEMBER_LEFT: Final = "member_left"

# The co-members (and the jobs themselves) of the groups of :grouped.
_CO_MEMBERS_OF: Final = (
    "SELECT co.job_id FROM duplicate_group_members m JOIN duplicate_group_members co "
    "ON co.group_id = m.group_id WHERE m.job_id = ANY(CAST(:grouped AS bigint[]))"
)
_LOCK_JOBS: Final = q(
    f"SELECT id, merged_into_job_id FROM jobs WHERE id = ANY(CAST(:ids AS bigint[])) "  # noqa: S608
    f"OR id IN ({_CO_MEMBERS_OF}) ORDER BY id FOR UPDATE"
)
_CO_MEMBERS: Final = q(_CO_MEMBERS_OF)
_PAIR_STATE: Final = q(
    "SELECT id, merged_into_job_id FROM jobs WHERE id = ANY(CAST(:ids AS bigint[])) ORDER BY id"
)
_FENCED: Final = q(
    "SELECT j.id FROM unnest(CAST(:ids AS bigint[])) AS j(id) WHERE is_fenced(j.id) ORDER BY j.id"
)
_DERIVE: Final = q(
    "WITH p AS (SELECT min(first_seen_at) AS first_seen, max(last_seen_at) AS last_seen, "
    "(array_agg(availability ORDER BY array_position(CAST(:order AS text[]), availability)))[1] AS best "
    "FROM postings WHERE job_id = :job), "
    "old AS (SELECT availability, first_seen_at FROM jobs WHERE id = :job) "
    "UPDATE jobs AS j SET availability = p.best, "
    "availability_changed_at = CASE WHEN j.availability = p.best THEN j.availability_changed_at "
    "ELSE now() END, "
    "first_seen_at = p.first_seen, last_seen_at = p.last_seen, "
    "posted_at_effective = CASE WHEN j.posted_at_effective IS NULL THEN NULL "
    "ELSE least(j.posted_at_effective, p.first_seen) END "
    "FROM p, old WHERE j.id = :job AND p.best IS NOT NULL "
    "RETURNING old.availability IS DISTINCT FROM j.availability AS availability_changed, "
    "old.first_seen_at IS DISTINCT FROM j.first_seen_at AS first_seen_changed"
)
_OWNED_KEYS: Final = q(
    "SELECT key_ns, key_value, exclusive FROM ad_keys WHERE job_id = :job ORDER BY key_ns, key_value"
)
_MEMBERSHIPS: Final = q(
    "SELECT m.group_id, m.job_id, m.joined_at, m.score_individually, g.status, g.representative_job_id "
    "FROM duplicate_group_members m JOIN duplicate_groups g ON g.id = m.group_id "
    "WHERE m.job_id = ANY(CAST(:jobs AS bigint[])) ORDER BY m.job_id"
)
_MOVE_POSTINGS: Final = q("UPDATE postings SET job_id = :into_job WHERE job_id = :from_job RETURNING id")
_MOVE_KEYS: Final = q("UPDATE ad_keys SET job_id = :into_job WHERE job_id = :from_job")
_MERGED: Final = q("UPDATE jobs SET merged_into_job_id = :into_job, updated_at = now() WHERE id = :from_job")
_INSERT_EVENT: Final = q(
    "INSERT INTO merge_events (kind, from_job_id, into_job_id, posting_ids, evidence) "
    "VALUES (:kind, :from_job, :into_job, CAST(:postings AS bigint[]), :evidence) RETURNING id",
    jsonb=["evidence"],
)
# -- groups (step 5)
_GROUP_OF: Final = q(
    "SELECT g.id, g.status, g.representative_job_id FROM duplicate_group_members m "
    "JOIN duplicate_groups g ON g.id = m.group_id WHERE m.job_id = :job FOR UPDATE OF g"
)
_LEAVE: Final = q(
    "DELETE FROM duplicate_group_members WHERE group_id = :group AND job_id = ANY(CAST(:jobs AS bigint[]))"
)
_UNGROUP: Final = q(
    "UPDATE jobs SET duplicate_group_id = NULL WHERE id = ANY(CAST(:jobs AS bigint[])) "
    "AND duplicate_group_id = :group"
)
_MEMBERS: Final = q("SELECT job_id FROM duplicate_group_members WHERE group_id = :group ORDER BY job_id")
_CANDIDATE_REPRESENTATIVE: Final = q(
    "SELECT j.id FROM duplicate_group_members m JOIN jobs j ON j.id = m.job_id "
    "LEFT JOIN job_revisions r ON r.id = j.current_revision_id "
    "WHERE m.group_id = :group AND j.merged_into_job_id IS NULL AND j.availability <> 'closed' "
    "ORDER BY array_position(CAST(:order AS text[]), j.availability), (j.completeness = 'full') DESC, "
    "(SELECT min(s.precedence_rank) FROM sources s WHERE s.id = ANY(j.source_ids)), "
    "length(coalesce(r.description_text, '')) DESC, j.first_seen_at, j.id LIMIT 1"
)
_ELECT: Final = q(
    "UPDATE duplicate_groups SET representative_job_id = :rep, unscorable_reason = :reason, "
    "elected_at = now(), election_reason = :election, updated_at = now() WHERE id = :group"
)
# -- conflicts (step 6, §8.6 step 3)
_CONFLICTS_NAMING: Final = q(
    "SELECT id, posting_job_id, candidate_job_ids FROM identity_conflicts WHERE status = 'open' "
    "AND (posting_job_id = :job OR CAST(:job AS bigint) = ANY(candidate_job_ids)) ORDER BY id FOR UPDATE"
)
_REWRITE_CONFLICT: Final = q(
    "UPDATE identity_conflicts SET posting_job_id = :posting_job, "
    "candidate_job_ids = CAST(:candidates AS bigint[]) WHERE id = :id"
)
_OPEN_CONFLICTS_OF_POSTING: Final = q(
    "SELECT candidate_job_ids FROM identity_conflicts WHERE posting_id = :posting AND status = 'open' "
    "AND posting_job_id = :posting_job"
)
_INSERT_CONFLICT: Final = q(
    "INSERT INTO identity_conflicts (posting_id, posting_job_id, candidate_job_ids, evidence, status) "
    "VALUES (:posting, :posting_job, CAST(:candidates AS bigint[]), :evidence, 'open') RETURNING id",
    jsonb=["evidence"],
)
_CONFLICT: Final = q("SELECT posting_job_id, candidate_job_ids FROM identity_conflicts WHERE id = :id")
_LOCK_CONFLICT: Final = q(
    "SELECT posting_job_id, candidate_job_ids, status FROM identity_conflicts WHERE id = :id FOR UPDATE"
)
_DIFFERENT: Final = q(
    "INSERT INTO dedup_decisions (job_a, job_b, decision, source) VALUES (:a, :b, 'different', :source) "
    "ON CONFLICT (job_a, job_b) DO UPDATE SET decision = 'different', source = EXCLUDED.source, "
    "decided_at = now()"
)
# -- unmerge
_EVENT: Final = q("SELECT kind, from_job_id, into_job_id, posting_ids FROM merge_events WHERE id = :id")
_LOCK_EVENT: Final = q("SELECT reversed_by_event_id FROM merge_events WHERE id = :id FOR UPDATE")
_MOVE_BACK: Final = q(
    "UPDATE postings SET job_id = :from_job "
    "WHERE id = ANY(CAST(:postings AS bigint[])) AND job_id = :into_job RETURNING id"
)
_UNMERGED: Final = q("UPDATE jobs SET merged_into_job_id = NULL, updated_at = now() WHERE id = :job")
# Every key the two jobs own goes to the job of its earliest provenance row among the two jobs' postings.
_REOWN_KEYS: Final = q(
    "WITH earliest AS (SELECT DISTINCT ON (s.key_ns, s.key_value) s.key_ns, s.key_value, p.job_id "
    "FROM ad_key_sources s JOIN postings p ON p.id = s.posting_id "
    "WHERE p.job_id = ANY(CAST(:jobs AS bigint[])) "
    "ORDER BY s.key_ns, s.key_value, s.first_seen_at, s.posting_id) "
    "UPDATE ad_keys AS a SET job_id = e.job_id FROM earliest e "
    "WHERE a.key_ns = e.key_ns AND a.key_value = e.key_value AND a.job_id = ANY(CAST(:jobs AS bigint[])) "
    "AND a.job_id <> e.job_id RETURNING a.key_ns, a.key_value, a.job_id"
)
_FORGET_FIELD_CONFLICTS: Final = q(
    "UPDATE jobs SET field_provenance = field_provenance - ARRAY(SELECT e.key "
    "FROM jsonb_each(field_provenance) AS e(key, value) WHERE e.value ->> 'rule' = :rule) WHERE id = :job"
)
_REVERSED: Final = q("UPDATE merge_events SET reversed_by_event_id = :by WHERE id = :id")

log = get_logger("jobhunter.dedup.merge")


@dataclass(frozen=True)
class Derived:
    """What `derive_from_postings` changed on the job."""

    availability_changed: bool
    first_seen_changed: bool

    @property
    def changed(self) -> bool:
        """A column the Jobs list filters or sorts by changed (§7.1): the caller bumps the ranking."""
        return self.availability_changed or self.first_seen_changed


async def lock_jobs(
    conn: AsyncConnection, job_ids: Iterable[int], *, with_groups_of: Iterable[int] = ()
) -> dict[int, int | None]:
    """The job rows of `job_ids` and of every duplicate-group co-member of `with_groups_of` `FOR UPDATE` in
    ascending id, in one statement (§3.3.3, Ruling T18-c); their `merged_into_job_id` by id. A job that does
    not exist is absent from the result. The co-members are those the statement's snapshot sees: a caller
    that depends on them checks `co_members` afterwards."""
    ids, grouped = sorted(set(job_ids)), sorted(set(with_groups_of))
    if not ids and not grouped:
        return {}
    rows = await conn.execute(_LOCK_JOBS, {"ids": ids, "grouped": grouped})
    return {int(r.id): None if r.merged_into_job_id is None else int(r.merged_into_job_id) for r in rows}


async def co_members(conn: AsyncConnection, job_ids: Iterable[int]) -> frozenset[int]:
    """Every member of the duplicate groups of `job_ids`, the grouped jobs themselves included."""
    grouped = sorted(set(job_ids))
    if not grouped:
        return frozenset()
    return frozenset(int(job) for job in (await conn.execute(_CO_MEMBERS, {"grouped": grouped})).scalars())


async def fenced_jobs(conn: AsyncConnection, job_ids: Iterable[int]) -> list[int]:
    """The jobs of `job_ids` that an unreleased purge fence matches (`is_fenced`, §20.2), in id order. Its
    caller holds their rows: the statement runs after the lock, with a snapshot of its own (§20.2)."""
    ids = sorted(set(job_ids))
    if not ids:
        return []
    return [int(job_id) for job_id in (await conn.execute(_FENCED, {"ids": ids})).scalars()]


async def derive_from_postings(conn: AsyncConnection, job_id: int) -> Derived:
    """The job's availability and seen bounds from its postings (module docstring); nothing for a job
    without a posting."""
    row = (await conn.execute(_DERIVE, {"job": job_id, "order": list(AVAILABILITY_ORDER)})).first()
    if row is None:
        return Derived(False, False)
    return Derived(bool(row.availability_changed), bool(row.first_seen_changed))


async def decide_different(conn: AsyncConnection, job_x: int, job_y: int, *, source: str) -> None:
    """`dedup_decisions(min, max, 'different', source)`, replacing any decision between the two jobs."""
    if job_x == job_y:
        raise ValueError(f"job {job_x} cannot be different from itself")
    await conn.execute(_DIFFERENT, {"a": min(job_x, job_y), "b": max(job_x, job_y), "source": source})


def _iso(value: object) -> JSONValue:
    return value.isoformat() if isinstance(value, datetime) else None


async def _snapshot(conn: AsyncConnection, from_job_id: int, into_job_id: int) -> dict[str, JSONValue]:
    keys: list[JSONValue] = [
        {"key_ns": r.key_ns, "key_value": r.key_value, "exclusive": bool(r.exclusive)}
        for r in await conn.execute(_OWNED_KEYS, {"job": from_job_id})
    ]
    memberships: list[JSONValue] = [
        {
            "group_id": int(r.group_id),
            "job_id": int(r.job_id),
            "joined_at": _iso(r.joined_at),
            "score_individually": bool(r.score_individually),
            "status": r.status,
            "representative_job_id": None
            if r.representative_job_id is None
            else int(r.representative_job_id),
        }
        for r in await conn.execute(_MEMBERSHIPS, {"jobs": [from_job_id, into_job_id]})
    ]
    return {"ad_keys": keys, "group_memberships": memberships}


async def _leave_group(conn: AsyncConnection, job_id: int, held: Collection[int]) -> None:
    """Step 5: the job leaves its duplicate group, which dissolves or re-elects (module docstring). Every
    member's row must be among `held`, the rows this transaction locked: `InvariantViolation` otherwise,
    before it writes anything (Ruling T18-c; `_hold_pair` checks the same before the merge writes)."""
    group = (await conn.execute(_GROUP_OF, {"job": job_id})).first()
    if group is None:
        return
    group_id = int(group.id)
    members = [int(member) for member in (await conn.execute(_MEMBERS, {"group": group_id})).scalars()]
    if unheld := sorted(set(members) - set(held)):
        raise InvariantViolation(f"group {group_id}: the rows of jobs {unheld} are not locked")
    await conn.execute(_LEAVE, {"group": group_id, "jobs": [job_id]})
    await conn.execute(_UNGROUP, {"group": group_id, "jobs": [job_id]})
    if group.status != "active":
        return
    remaining = [member for member in members if member != job_id]
    if len(remaining) < 2:
        await conn.execute(_LEAVE, {"group": group_id, "jobs": remaining})
        await conn.execute(_UNGROUP, {"group": group_id, "jobs": remaining})
        await transition(conn, "duplicate_groups", group_id, ["active"], "dissolved", reason=_MEMBER_LEFT)
        log.info("duplicate_group_dissolved", group_id=group_id, job_id=job_id)
        return
    if group.representative_job_id != job_id:
        return
    order = {"group": group_id, "order": list(AVAILABILITY_ORDER)}
    representative = (await conn.execute(_CANDIDATE_REPRESENTATIVE, order)).scalar()
    await conn.execute(
        _ELECT,
        {
            "group": group_id,
            "rep": representative,
            "reason": None if representative is not None else "no_active_member",
            "election": _MEMBER_LEFT,
        },
    )
    log.info("duplicate_group_reelected", group_id=group_id, representative_job_id=representative)


async def _rewrite_conflicts(conn: AsyncConnection, from_job_id: int, into_job_id: int) -> None:
    """Step 6: the open conflicts naming the from-job name the into-job; one left without a candidate is
    `resolved_merged`, its ids as they were (the CHECKs keep the posting's job out of its candidates)."""
    for row in (await conn.execute(_CONFLICTS_NAMING, {"job": from_job_id})).all():
        posting_job = into_job_id if row.posting_job_id == from_job_id else int(row.posting_job_id)
        candidates = sorted(
            {into_job_id if job == from_job_id else int(job) for job in row.candidate_job_ids} - {posting_job}
        )
        if candidates:
            await conn.execute(
                _REWRITE_CONFLICT, {"id": row.id, "posting_job": posting_job, "candidates": candidates}
            )
        else:
            await transition(conn, "identity_conflicts", int(row.id), ["open"], "resolved_merged")
            await resolve_alert(conn, code=IDENTITY_CONFLICT_ALERT, subject=f"conflict:{row.id}")
            log.info("identity_conflict_resolved", conflict_id=int(row.id), status="resolved_merged")


async def _hold_pair(
    conn: AsyncConnection, from_job_id: int, into_job_id: int, locked: Collection[int] | None
) -> frozenset[int]:
    """The rows the merge may touch, held: the pair and, standalone, its groups' members, locked in one
    statement; or the caller's `locked`, which must hold the pair. The pair's state is checked."""
    if from_job_id == into_job_id:
        raise InvariantViolation(f"job {from_job_id} cannot be merged into itself")
    pair = (from_job_id, into_job_id)
    if locked is None:
        state = await lock_jobs(conn, pair, with_groups_of=pair)
        held = frozenset(state)
    else:
        held = frozenset(locked)
        if missing := sorted(set(pair) - held):
            raise InvariantViolation(f"jobs {missing} are not among the rows the caller locked")
        rows = await conn.execute(_PAIR_STATE, {"ids": list(pair)})
        state = {int(r.id): None if r.merged_into_job_id is None else int(r.merged_into_job_id) for r in rows}
    for job_id in pair:
        if job_id not in state:
            raise LookupError(f"job {job_id} does not exist")
        if state[job_id] is not None:
            raise InvariantViolation(f"job {job_id} is merged into job {state[job_id]}")
    if fenced := await fenced_jobs(conn, pair):
        log.warning("merge_refused_fenced", job_ids=fenced)
        raise FencedOut("purge_fence")
    if unheld := sorted(await co_members(conn, (from_job_id,)) - held):  # before the merge writes anything
        raise InvariantViolation(f"the rows of jobs {unheld} of job {from_job_id}'s group are not locked")
    return held


async def recompute_rollups(conn: AsyncConnection, job_id: int) -> tuple[Derived, RecomputeResult]:
    """`derive_from_postings`, then `revisions.recompute_job`, of a job whose row the caller holds."""
    derived = await derive_from_postings(conn, job_id)
    return derived, await revisions.recompute_job(conn, job_id)


async def merge_jobs(
    conn: AsyncConnection,
    *,
    from_job_id: int,
    into_job_id: int,
    kind: MergeKind,
    evidence: Mapping[str, JSONValue],
    recompute: bool = True,
    locked: Collection[int] | None = None,
) -> int:
    """Merge `from_job_id` into `into_job_id` (§8.6.1 steps 1, 2, 5, 6 and 7; module docstring) in the
    caller's transaction; returns the `merge_events` id. `locked` is the job rows the caller holds (the pair
    and the from-job's group members among them); without it the merge locks them itself. The caller writes
    the hard-filter results and bumps the ranking. Raises `FencedOut` for a fenced job, `InvariantViolation`
    for a job already merged, a job merged into itself or a row the transaction does not hold, and
    `LookupError` for a job that does not exist, having written nothing."""
    held = await _hold_pair(conn, from_job_id, into_job_id, locked)
    snapshot = await _snapshot(conn, from_job_id, into_job_id)
    moved = sorted(
        int(p) for p in (await conn.execute(_MOVE_POSTINGS, _pair(from_job_id, into_job_id))).scalars()
    )
    await conn.execute(_MOVE_KEYS, _pair(from_job_id, into_job_id))
    await _leave_group(conn, from_job_id, held)
    await _rewrite_conflicts(conn, from_job_id, into_job_id)
    await conn.execute(_MERGED, _pair(from_job_id, into_job_id))
    for field in CONFLICT_FIELDS:  # carry 7: the from-job's selection no longer stands
        await resolve_alert(conn, code=FIELD_CONFLICT_ALERT, subject=f"job:{from_job_id}:{field}")
    if recompute:
        await recompute_rollups(conn, into_job_id)
    event_id = int(
        (
            await conn.execute(
                _INSERT_EVENT,
                {
                    "kind": kind,
                    "from_job": from_job_id,
                    "into_job": into_job_id,
                    "postings": moved,
                    "evidence": {**evidence, "snapshot": snapshot},
                },
            )
        ).scalar_one()
    )
    log.info("jobs_merged", kind=kind, from_job_id=from_job_id, into_job_id=into_job_id, event_id=event_id)
    return event_id


def _pair(from_job_id: int, into_job_id: int) -> dict[str, int]:
    return {"from_job": from_job_id, "into_job": into_job_id}


async def open_conflict(
    conn: AsyncConnection,
    *,
    posting_id: int,
    posting_job_id: int,
    candidate_job_ids: Sequence[int],
    evidence: Mapping[str, JSONValue],
) -> int | None:
    """An open `identity_conflicts` row for the posting (§8.6 steps 3 and 6) and its `dedup.identity_conflict`
    (info) alert; its id. `None`, writing nothing, when an open conflict of the posting on the same job names
    every candidate already (a posting sighted again repeats no conflict)."""
    candidates = sorted(set(candidate_job_ids) - {posting_job_id})
    if not candidates:
        raise ValueError("a conflict names at least one job other than the posting's")
    params = {"posting": posting_id, "posting_job": posting_job_id}
    for named in (await conn.execute(_OPEN_CONFLICTS_OF_POSTING, params)).scalars():
        if set(candidates) <= set(named):
            return None
    conflict_id = int(
        (
            await conn.execute(
                _INSERT_CONFLICT, {**params, "candidates": candidates, "evidence": dict(evidence)}
            )
        ).scalar_one()
    )
    await raise_alert(
        conn,
        code=IDENTITY_CONFLICT_ALERT,
        severity="info",
        subject=f"conflict:{conflict_id}",
        message="a posting's strong keys name jobs that carry different exclusive keys",
        context={
            "conflict_id": conflict_id,
            "posting_job_id": posting_job_id,
            "candidate_job_ids": [*candidates],
        },
    )
    log.info("identity_conflict_opened", conflict_id=conflict_id, posting_job_id=posting_job_id)
    return conflict_id


async def resolve_conflict(
    conn: AsyncConnection, conflict_id: int, decision: Literal["separate", "merge"]
) -> None:
    """Settle the open conflict as `separate` or `merge` (module docstring) in the caller's transaction.

    Raises `LookupError` for a conflict that does not exist, `InvariantViolation` for one that is not open
    (or changed since it was read) and `FencedOut` when one of its jobs is fenced, having written nothing.
    """
    scopes = await filters.active_scopes(conn)  # active_config FOR SHARE: the first lock
    row = (await conn.execute(_CONFLICT, {"id": conflict_id})).first()
    if row is None:
        raise LookupError(f"identity conflict {conflict_id} does not exist")
    posting_job = int(row.posting_job_id)
    candidates = sorted(int(job) for job in row.candidate_job_ids)
    jobs = sorted({posting_job, *candidates})
    # the candidates merge away under `merge`: their groups' members are locked with the jobs (T18-c)
    locked = await lock_jobs(conn, jobs, with_groups_of=candidates if decision == "merge" else ())
    if fenced := await fenced_jobs(conn, jobs):
        log.warning("conflict_resolution_refused_fenced", conflict_id=conflict_id, job_ids=fenced)
        raise FencedOut("purge_fence")
    current = (await conn.execute(_LOCK_CONFLICT, {"id": conflict_id})).one()
    if current.status != "open":
        raise InvariantViolation(f"identity conflict {conflict_id} is {current.status}, not open")
    if (int(current.posting_job_id), sorted(int(j) for j in current.candidate_job_ids)) != (
        posting_job,
        candidates,
    ):
        raise InvariantViolation(f"identity conflict {conflict_id} changed while it was being resolved")
    if any(locked.get(job, -1) is not None for job in jobs):
        raise InvariantViolation(f"identity conflict {conflict_id} names a merged or missing job")
    if decision == "separate":
        for candidate in candidates:  # the pairs the user judged (Ruling T18-e)
            await decide_different(conn, posting_job, candidate, source="conflict_resolution")
        await transition(conn, "identity_conflicts", conflict_id, ["open"], "resolved_separate")
        await resolve_alert(conn, code=IDENTITY_CONFLICT_ALERT, subject=f"conflict:{conflict_id}")
        log.info("identity_conflict_resolved", conflict_id=conflict_id, status="resolved_separate")
        return
    evidence: dict[str, JSONValue] = {"rule": "conflict_resolution", "conflict_id": conflict_id}
    for candidate in candidates:
        await merge_jobs(
            conn,
            from_job_id=candidate,
            into_job_id=posting_job,
            kind="user_confirmed_merge",
            evidence=evidence,
            recompute=False,
            locked=locked,
        )
    status = (await conn.execute(_LOCK_CONFLICT, {"id": conflict_id})).one().status
    if status == "open":  # the merges' step 6 resolves it; this is the guard
        raise InvariantViolation(f"identity conflict {conflict_id} is still open after its merges")
    await recompute_rollups(conn, posting_job)
    await filters.refilter_job(conn, posting_job, scopes)
    await ranking.bump(conn)


async def unmerge(conn: AsyncConnection, merge_event_id: int) -> int:
    """Reverse the merge event (§8.6.2 steps 1, 2, 3, 5 and 6; module docstring) in the caller's
    transaction; returns the `merge_events(unmerge)` id. Its last statement bumps the ranking.

    Raises `LookupError` for an event that does not exist, `InvariantViolation` for an unmerge event, an
    event already reversed, or a merge that is no longer the latest state of its jobs, and `FencedOut` when
    one of the jobs is fenced, having written nothing.
    """
    scopes = await filters.active_scopes(conn)  # active_config FOR SHARE: the first lock
    event = (await conn.execute(_EVENT, {"id": merge_event_id})).first()
    if event is None:
        raise LookupError(f"merge event {merge_event_id} does not exist")
    if event.kind == "unmerge":
        raise InvariantViolation(f"merge event {merge_event_id} is an unmerge")
    from_job, into_job = int(event.from_job_id), int(event.into_job_id)
    locked = await lock_jobs(conn, (from_job, into_job), with_groups_of=(from_job, into_job))
    if fenced := await fenced_jobs(conn, (from_job, into_job)):
        log.warning("unmerge_refused_fenced", event_id=merge_event_id, job_ids=fenced)
        raise FencedOut("purge_fence")
    if (await conn.execute(_LOCK_EVENT, {"id": merge_event_id})).scalar() is not None:
        raise InvariantViolation(f"merge event {merge_event_id} is already reversed")
    if locked.get(from_job) != into_job or into_job not in locked or locked[into_job] is not None:
        raise InvariantViolation(
            f"merge event {merge_event_id}: job {from_job} is not merged into job {into_job}, or that job "
            "is merged itself"
        )
    moved = sorted(
        int(p)
        for p in (
            await conn.execute(_MOVE_BACK, {**_pair(from_job, into_job), "postings": list(event.posting_ids)})
        ).scalars()
    )
    await conn.execute(_UNMERGED, {"job": from_job})
    reowned: list[JSONValue] = [
        {"key_ns": r.key_ns, "key_value": r.key_value, "job_id": int(r.job_id)}
        for r in await conn.execute(_REOWN_KEYS, {"jobs": [from_job, into_job]})
    ]
    await decide_different(conn, from_job, into_job, source="unmerge")
    # The merge resolved the from-job's field-conflict alerts: its stored markers no longer stand for them.
    await conn.execute(_FORGET_FIELD_CONFLICTS, {"job": from_job, "rule": CONFLICT_RULE})
    for job_id in sorted((from_job, into_job)):
        await recompute_rollups(conn, job_id)
        await filters.refilter_job(conn, job_id, scopes)
    evidence: dict[str, Any] = {"reverses_event_id": merge_event_id, "keys_reowned": reowned}
    reversal = int(
        (
            await conn.execute(
                _INSERT_EVENT,
                {"kind": "unmerge", **_pair(from_job, into_job), "postings": moved, "evidence": evidence},
            )
        ).scalar_one()
    )
    await conn.execute(_REVERSED, {"by": reversal, "id": merge_event_id})
    log.info(
        "jobs_unmerged",
        event_id=merge_event_id,
        reversal_id=reversal,
        from_job_id=from_job,
        into_job_id=into_job,
    )
    await ranking.bump(conn)
    return reversal
