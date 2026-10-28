"""Probable-duplicate groups (spec §8.7, §7.17 #4, §17.4): the rules G1-G5, group formation, revalidation,
split, confirm, leave, the node's grouping pass and the `group_representatives` reconciler. No group ever
merges jobs, except `confirm_group`, the user's confirmation.

**The rules** (`group_rules`, pure; M1 Carry-forward 15): G1-G5 over `JobView`s, the evidence of a pair
(`GroupEvidence`), the revalidation against a representative (`revalidation_departures`) and the choice of a
group or a partner (`choose_group`, `choose_partner`). **Formation**: a job joins an active group whose
representative it confirms (G2-G5), against whose every member it passes G1 and has no `different`
decision, and that has fewer than `group_rules.MAX_MEMBERS` members; a group without a representative cannot
be validated and is not joined. Of several, the one with the best text score, then the lowest id:
`dedup.ambiguous_grouping` is raised as an info alert (subject `job:<id>`, Ruling T20-b) when the job joins,
logged, and kept in the member's evidence; a regroup that only confirms the job raises nothing. The alert is
resolved when the ambiguity is over: the job leaves the group (`representative.leave`: it left, was split
off or merged away, or the group dissolved), or a regroup that keeps it there finds no other group still
qualifying (`_end_ambiguity`). Otherwise the ungrouped candidate with the best pair evidence (text score,
then lowest id) forms a new group with it, its representative elected by §8.7's order (`initial`; none
eligible: the group records why, `representative.unscorable_reason`). Groups are never merged together.

**Eligibility** reads the members' active-tuple targets (`representative.eligible`, M3 Task 15, Ruling
T15-a: no target yet, or a live or published one): the `JobView`s carry their `target_state` and whether a
tuple is active, and the reconciler's violations join `active_config` for `ELIGIBLE_SQL`. Node 6 groups
before node 9 creates the targets, so a fresh group elects at once and a full-text twin is appointed when it
joins; node 9 then creates the representative `pending` and the others deferred.

**Deferral and the group events** (§8.7 L3261-3267, M3 Task 15). `deferrable(member, representative,
target)` is L3261's predicate for entering deferral (not the representative, `override = 'none'`, not
`score_individually`, no `conflicting_facts`, the target `pending` or `budget_deferred`); target creation
and the regroup sweep use its SQL form (`targets.DEFERRAL_ELIGIBLE_SQL`). `deferral_eligible(member,
representative, override=…)` is the same predicate without the entering states, the one a deferred target
stays deferred under (Codex r1 #36, r3 B14). After every group event, in the event's own transaction
(`regroup_job`'s, `leave_group`, `split_member`, the reconciler's repair), `settle_deferred` moves each
target deferred to the group `deferred_duplicate → pending` (`transition`, `deferred_group_id` cleared on
exit) unless its job is still a member of the active group and `deferral_eligible` holds against the group's
(new) representative: a split member, a member that left after a revalidation, a dissolved group's members,
a new representative's own target, and a member whose facts conflict with the new representative return to
`pending`; the others stay deferred. A published representative changes nothing (step 7 of the publish,
`representative_published`, Task 10).

**`regroup_job`** (the grouping of one job, in the caller's transaction): the candidates are the jobs of the
same `company_key`, first seen at most `CANDIDATE_DAYS` apart, unmerged, with no `different` decision against
it. A job already in a group whose representative it still confirms is left as it is, and nothing is
written (idempotence; a group without a representative confirms the members that pass G1 against every
other member). A job that no longer confirms leaves (`member_left`) and is grouped anew. A representative
whose content changed is revalidated against: its members that no longer confirm leave; when it is not
eligible any more the group re-elects (`rep_inactive`). A joining `full` twin of a `partial` representative
is elected (`fuller_member_joined`) when it is eligible. After every representative change the members are
revalidated against the new one and those that fail leave (`member_left`); a group left with fewer than two
members is dissolved. The ranking generation is bumped, as the last statement, when anything was written.

**Revalidation** (`revalidation_departures`, pure; final review I2) is G2-G5 of every member against the
representative, then G1 for every pair (spec §8.7: two postings of one ATS board are never grouped): the join
rule replayed in the order the members joined, the representative first, so a member stays only when it
carries no board key of the representative or of a member that stays before it. A member on the
representative's board therefore leaves, and the representative never leaves for G1: a G1 conflict is no
re-election trigger of §8.7, and every representative change is followed by this revalidation. A regroup of
a member of a group without a representative keeps it only when it passes G1 against every other member.

**Locks (Ruling T20-a, §3.3.3).** `regroup_job` takes the company lock, `pg_advisory_xact_lock(
COMPANY_LOCK_NAMESPACE, hashtext(company_key))` (the two-key form: a key space of its own), FIRST, before any
row, and it must be the caller transaction's first lock: regroups of one company are serialised, and no
transaction waits for a company lock while it holds a row. Then the job, its candidates and the current
members of every group it may join or leave (the groups of the job and of its candidates) in ONE ordered
`FOR UPDATE` statement (`merge.lock_jobs`), so a job is never added to a group whose members another
transaction holds (carry 4). Then `guard(conn)` (the run's owner row, or the reconciler's epoch fence: Ruling
T19-a puts it after the job rows). Then the group rows `FOR UPDATE` in id order, the other domain rows, and
`ranking_state` last. Each read-and-lock round runs in a savepoint: when the job's company or first sighting
changed between the read and the lock, or its groups gained a member the round does not hold, the savepoint
is rolled back, which releases the round's row locks and its advisory lock, and the round runs again (at
most `_LOCK_ROUNDS`). Every decision reads its rows after the lock.

`leave_group` (merge step 5, inside the identity transaction or a standalone merge) takes NO advisory lock: it
relies on the member rows its caller locked in the ordered statement (`held`; `InvariantViolation` for a
member that is not held, before anything is written). So no transaction ever waits for a company lock while
it holds job rows, and a merge and a regroup of one company cannot deadlock: the second waits on the job
rows. `split_member` and `confirm_group` (user actions) take no advisory lock either: their job rows (the
group's members), then the group row; `confirm_group` takes `active_config` `FOR SHARE` before them.

**`group_representatives`** (cadence `heavy`, §17.4): the active groups that break §7.17 #4, read with §8.7's
eligibility (A1 issue 27: a representative that is not an eligible member, a failed, refused, expired,
filtered or cancelled target among them; no representative while a member is eligible), have fewer
than two members, or hold two members whose jobs carry keys of one ATS board (G1, final review I2: a key that
moved outside any run, an unmerge's re-own or a conflict's merge, is found here) are read; each is repaired
in a transaction of its own: its members' rows (and its representative's: the FK of a new one takes
`FOR KEY SHARE` on it) in one ordered statement, then
`fence_epoch` (Ruling T19-a: after the job rows; the brief's "fence first" holds only for a pass that locks no
job row, and this one locks them), then the group row, and the violation is checked again. At most one
election per group per pass (`reconciler_repair`, or `eligible_again` for a group without a representative),
followed by the revalidation of the members, which Task 18's stand-in never did; a group of fewer than two
is dissolved; a group whose only breach is G1 is revalidated (G1 alone without a representative), and no
election runs. An election that finds no eligible member records `all_failed`, `all_filtered` or
`no_active_member`. The deferred targets are settled after each repair. Each repair is logged and counted
in `DEDUP_GROUP_REPAIRS` once its transaction committed. Because ineligible members are never elected,
repair terminates.

**`regroup_run_jobs`** (node 6's grouping pass): every unmerged job with a `job_revision_occurrences` row
whose `became_current_at ≥ runs.started_at` (the first attempt's start: the `running → running` self-edge
keeps it; both in the database clock), and every unmerged job whose strong keys or postings changed since
then (final review I2): one that owns an `ad_keys` row created since (a posting attach) or that a
`merge_events` row created since names (a merge moved postings and keys to its into-job; an unmerge re-owned
them), in id order, one transaction each, guarded by the run's owner row. A key or posting change thus
schedules the validation a revision change schedules, also when the job's material is unchanged. The
selection is the durable trace of "created or changed in this run", never an in-memory result, so a resumed
attempt re-runs it and a job already grouped is only confirmed. Returns `GROUPING_COUNTERS`: `groups_formed`
(new groups), `groups_changed` (a regroup that joined an existing group, made a member leave or changed a
representative) and `elections` (representative changes).

Every timestamp written is the database's `now()`; no function here takes a caller clock (Ruling T18-b's
convention, a deviation from the brief's `now` parameters).
"""

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final

from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.db import ranking
from jobhunter.db.guards import Guard
from jobhunter.db.sql import q
from jobhunter.db.transitions import CLEAR_REASON, transition
from jobhunter.dedup import merge, representative
from jobhunter.dedup.group_rules import (
    Choice,
    GroupEvidence,
    GroupView,
    JobView,
    choose_group,
    choose_partner,
    join_evidence,
    member_view,
    revalidation_departures,
    same_board,
)
from jobhunter.dedup.representative import ELIGIBLE_SQL, TARGET_STATE_SQL, eligible
from jobhunter.errors import FencedOut, InvariantViolation, TransitionConflict
from jobhunter.observability.alerts import raise_alert, resolve_alert
from jobhunter.observability.logging import get_logger
from jobhunter.observability.metrics import DEDUP_GROUP_REPAIRS
from jobhunter.quota import ledger
from jobhunter.scoring import filters
from jobhunter.scoring.targets import DENIED
from jobhunter.sources.parsing.locations import Location
from jobhunter.types import JSONValue
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.reconcilers import ReconcileContext, reconciler
from jobhunter.worker.runs import RunOwner

CANDIDATE_DAYS: Final = 45
GROUPING_COUNTERS: Final = ("groups_formed", "groups_changed", "elections")
# An info alert, subject `job:<id>` (Ruling T20-b), resolved when the job leaves its group (`representative`).
AMBIGUOUS_GROUPING: Final = representative.AMBIGUOUS_GROUPING
# election reasons (§7.9 `duplicate_groups.election_reason`)
INITIAL: Final = "initial"
FULLER_MEMBER_JOINED: Final = "fuller_member_joined"
MEMBER_LEFT: Final = "member_left"
SPLIT: Final = "split"
REP_INACTIVE: Final = "rep_inactive"
RECONCILER_REPAIR: Final = "reconciler_repair"
ELIGIBLE_AGAIN: Final = "eligible_again"
_LOCK_ROUNDS: Final = 3  # read-then-lock rounds before a set that keeps changing is an invariant breach

_JOB: Final = q("SELECT company_key, first_seen_at FROM jobs WHERE id = :job AND merged_into_job_id IS NULL")
# The company lock's key space: the two-key form `(COMPANY_LOCK_NAMESPACE, hashtext(company_key))` keeps it
# apart from the project's single-key locks (7261001 worker singleton, 7261002 login throttle, 7261003
# migrate).
COMPANY_LOCK_NAMESPACE: Final = 7261004
_COMPANY_LOCK: Final = q("SELECT pg_advisory_xact_lock(CAST(:namespace AS integer), hashtext(:company))")
_CANDIDATES: Final = q(
    "SELECT c.id FROM jobs c WHERE c.company_key = :company AND c.id <> :job "
    "AND c.merged_into_job_id IS NULL "
    "AND c.first_seen_at BETWEEN CAST(:first AS timestamptz) - CAST(:days AS integer) * interval '1 day' "
    "AND CAST(:first AS timestamptz) + CAST(:days AS integer) * interval '1 day' "
    "AND NOT EXISTS (SELECT 1 FROM dedup_decisions d WHERE d.decision = 'different' "
    "AND d.job_a = least(c.id, CAST(:job AS bigint)) AND d.job_b = greatest(c.id, CAST(:job AS bigint))) "
    "ORDER BY c.id"
)
_VIEWS: Final = q(
    "SELECT j.id, j.company_key, j.title_norm, j.seniority_tokens, j.locations, j.location_kind, "  # noqa: S608
    "j.completeness, coalesce(r.description_text, '') AS description_text, j.first_seen_at, j.availability, "
    "coalesce((SELECT min(s.precedence_rank) FROM sources s WHERE s.id = ANY(j.source_ids)), :worst) "
    "AS source_rank, "
    "ARRAY(SELECT DISTINCT substr(k.key_ns, 5) FROM ad_keys k WHERE k.job_id = j.id "
    "AND starts_with(k.key_ns, 'ats:')) AS ats_boards, "
    "ARRAY(SELECT DISTINCT o.normalized ->> 'requisition_id' FROM postings p "
    "JOIN posting_observations o ON o.id = p.latest_observation_id WHERE p.job_id = j.id "
    "AND o.normalized ->> 'requisition_id' IS NOT NULL) AS requisition_ids, "
    f"j.merged_into_job_id IS NOT NULL AS merged, {TARGET_STATE_SQL} "
    "FROM jobs j JOIN active_config ac ON ac.id = 1 "
    "LEFT JOIN job_revisions r ON r.id = j.current_revision_id WHERE j.id = ANY(CAST(:ids AS bigint[]))"
)
_SIMILARITY: Final = q(
    "SELECT t.id, similarity(CAST(:title AS text), t.title) AS similarity "
    "FROM unnest(CAST(:ids AS bigint[]), CAST(:titles AS text[])) AS t(id, title)"
)
_DIFFERENT_FROM: Final = q(
    "SELECT CASE WHEN job_a = :job THEN job_b ELSE job_a END FROM dedup_decisions "
    "WHERE decision = 'different' AND (job_a = :job OR job_b = :job)"
)
_MEMBERSHIPS: Final = q(
    "SELECT job_id, group_id FROM duplicate_group_members WHERE job_id = ANY(CAST(:jobs AS bigint[]))"
)
_LOCK_GROUPS: Final = q(
    "SELECT id, status, representative_job_id FROM duplicate_groups WHERE id = ANY(CAST(:ids AS bigint[])) "
    "ORDER BY id FOR UPDATE"
)
_MEMBERS_OF: Final = q(
    "SELECT group_id, job_id FROM duplicate_group_members WHERE group_id = ANY(CAST(:ids AS bigint[])) "
    "ORDER BY group_id, job_id"
)
# The members in the order they joined (the revalidation replays the join rule in that order).
_MEMBERS_JOINED: Final = q(
    "SELECT job_id FROM duplicate_group_members WHERE group_id = :group ORDER BY joined_at, job_id"
)
_GROUP: Final = q("SELECT status, representative_job_id FROM duplicate_groups WHERE id = :group")
_GROUP_OF: Final = q(
    "SELECT g.id, g.status, g.representative_job_id FROM duplicate_group_members m "
    "JOIN duplicate_groups g ON g.id = m.group_id WHERE m.job_id = :job FOR UPDATE OF g"
)
_INSERT_GROUP: Final = q(
    "INSERT INTO duplicate_groups (representative_job_id, status, unscorable_reason, election_reason) "
    "VALUES (:rep, 'active', :reason, :election) RETURNING id"
)
_JOIN: Final = q(
    "INSERT INTO duplicate_group_members (group_id, job_id, evidence) VALUES (:group, :job, :evidence)",
    jsonb=["evidence"],
)
_IN_GROUP: Final = q("UPDATE jobs SET duplicate_group_id = :group WHERE id = ANY(CAST(:jobs AS bigint[]))")
_SAME: Final = q(
    "INSERT INTO dedup_decisions (job_a, job_b, decision, source) VALUES (:a, :b, 'same', :source) "
    "ON CONFLICT (job_a, job_b) DO UPDATE SET decision = 'same', source = EXCLUDED.source, decided_at = now()"
)
# The jobs created or changed in the run: a current revision that became current since its first attempt, a
# key created since (a posting attach) or a merge event since (its into-job; an unmerge's two jobs).
_CHANGED_IN_RUN: Final = q(
    "WITH since AS (SELECT r.started_at AS at FROM runs r WHERE r.id = :run), changed AS ("
    "SELECT o.job_id FROM job_revision_occurrences o, since WHERE o.became_current_at >= since.at "
    "UNION SELECT k.job_id FROM ad_keys k, since WHERE k.created_at >= since.at "
    "UNION SELECT e.into_job_id FROM merge_events e, since WHERE e.created_at >= since.at "
    "UNION SELECT e.from_job_id FROM merge_events e, since WHERE e.created_at >= since.at) "
    "SELECT c.job_id FROM changed c JOIN jobs j ON j.id = c.job_id WHERE j.merged_into_job_id IS NULL "
    "ORDER BY c.job_id"
)
# §7.17 #4 (with §8.7's eligibility over `ac`), a group of fewer than two members, and two members on one ATS
# board (G1).
_VIOLATIONS: Final = q(
    "SELECT g.id FROM duplicate_groups g JOIN active_config ac ON ac.id = 1 "  # noqa: S608 (constants only)
    "WHERE g.status = 'active' AND ("
    "(SELECT count(*) FROM duplicate_group_members m WHERE m.group_id = g.id) < 2 "
    "OR EXISTS (SELECT 1 FROM duplicate_group_members m1 JOIN duplicate_group_members m2 "
    "ON m2.group_id = m1.group_id AND m2.job_id > m1.job_id "
    "JOIN ad_keys k1 ON k1.job_id = m1.job_id AND starts_with(k1.key_ns, 'ats:') "
    "JOIN ad_keys k2 ON k2.job_id = m2.job_id AND k2.key_ns = k1.key_ns WHERE m1.group_id = g.id) "
    "OR (g.representative_job_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM duplicate_group_members m "
    "JOIN jobs j ON j.id = m.job_id WHERE m.group_id = g.id AND m.job_id = g.representative_job_id "
    f"AND {ELIGIBLE_SQL})) "
    "OR (g.representative_job_id IS NULL AND EXISTS (SELECT 1 FROM duplicate_group_members m "
    f"JOIN jobs j ON j.id = m.job_id WHERE m.group_id = g.id AND {ELIGIBLE_SQL}))) ORDER BY g.id"
)

log = get_logger("jobhunter.dedup.groups")


# ---- deferral (§8.7 L3261; M3 Task 15) -------------------------------------------------------------------

# The states a target enters `deferred_duplicate` from.
ENTERING_STATES: Final = ("pending", "budget_deferred")


@dataclass(frozen=True)
class MemberRow:
    """What §8.7's deferral reads of a member, or of the representative: its job, its membership's
    `score_individually`, and the facts whose conflict blocks a deferral (the structured work-mode set, the
    structured employment types, the seniority tokens)."""

    job_id: int
    score_individually: bool
    work_mode_possible: frozenset[str]
    work_mode_source: str
    employment_types: frozenset[str]
    employment_source: str
    seniority_tokens: frozenset[str]


@dataclass(frozen=True)
class TargetRow:
    """A member's target as the group events read it: its id, state, override, reason and group."""

    id: int
    state: str
    override: str
    state_reason: str | None
    deferred_group_id: int | None


def _structured_differ(a: frozenset[str], a_source: str, b: frozenset[str], b_source: str) -> bool:
    return a_source == b_source == "structured" and a != b


def conflicting_facts(member: MemberRow, representative: MemberRow) -> bool:
    """Whether the member has a conflicting scoring-relevant fact against the representative (L3261):
    different structured work-mode sets, different structured employment types (a set that is not
    structured on both sides says nothing), or different seniority tokens."""
    return (
        _structured_differ(
            member.work_mode_possible,
            member.work_mode_source,
            representative.work_mode_possible,
            representative.work_mode_source,
        )
        or _structured_differ(
            member.employment_types,
            member.employment_source,
            representative.employment_types,
            representative.employment_source,
        )
        or member.seniority_tokens != representative.seniority_tokens
    )


def deferral_eligible(member: MemberRow, representative: MemberRow, *, override: str) -> bool:
    """§8.7's deferral without the entering states, the predicate a target stays `deferred_duplicate` under
    (Codex r1 #36, r3 B14): the member is not the representative, its target's `override` is `none`, its
    membership is not `score_individually`, and it has no conflicting fact against the representative.
    `targets.DEFERRAL_ELIGIBLE_SQL` is the same predicate in SQL."""
    return (
        member.job_id != representative.job_id
        and override == "none"
        and not member.score_individually
        and not conflicting_facts(member, representative)
    )


def deferrable(member: MemberRow, representative: MemberRow, target: TargetRow | None) -> bool:
    """L3261 verbatim, the predicate for entering deferral: `deferral_eligible` and the target `pending` or
    `budget_deferred` (no target: nothing to defer)."""
    return (
        target is not None
        and deferral_eligible(member, representative, override=target.override)
        and target.state in ENTERING_STATES
    )


# The targets deferred to the group, in id order.
_DEFERRED_TO: Final = q(
    "SELECT id, job_id, state, override, state_reason, deferred_group_id FROM score_targets "
    "WHERE deferred_group_id = :group AND state = 'deferred_duplicate' ORDER BY id"
)
# The members of the group among `:ids`, with what the deferral reads of them.
_MEMBER_ROWS: Final = q(
    "SELECT j.id, m.score_individually, j.work_mode_possible, j.work_mode_source, j.employment_types, "
    "j.employment_source, j.seniority_tokens FROM duplicate_group_members m JOIN jobs j ON j.id = m.job_id "
    "WHERE m.group_id = :group AND m.job_id = ANY(CAST(:ids AS bigint[]))"
)


def _member_row(row: Row[tuple[object, ...]]) -> MemberRow:
    return MemberRow(
        job_id=int(row.id),
        score_individually=bool(row.score_individually),
        work_mode_possible=frozenset(row.work_mode_possible),
        work_mode_source=str(row.work_mode_source),
        employment_types=frozenset(row.employment_types),
        employment_source=str(row.employment_source),
        seniority_tokens=frozenset(row.seniority_tokens),
    )


async def settle_deferred(conn: AsyncConnection, group_id: int) -> tuple[int, ...]:
    """After a group event (a member left, was split off or merged away, the group dissolved, its
    representative changed), in the group transaction, which holds the members' rows and the group row: a
    target deferred to the group (`deferred_group_id`) stays deferred while its job is still a member of the
    active group and `deferral_eligible` holds against the group's representative, with the target's
    `override`; any other moves `deferred_duplicate → pending` through `transition` (`deferred_group_id`
    cleared on exit; its reason kept but the denial's, Ruling T13-c). The targets moved."""
    deferred = (await conn.execute(_DEFERRED_TO, {"group": group_id})).all()
    if not deferred:
        return ()
    group = (await conn.execute(_GROUP, {"group": group_id})).one()
    rep_id = group.representative_job_id if group.status == "active" else None
    ids = sorted({int(row.job_id) for row in deferred} | ({int(rep_id)} if rep_id is not None else set()))
    rows = {
        int(row.id): _member_row(row)
        for row in await conn.execute(_MEMBER_ROWS, {"group": group_id, "ids": ids})
    }
    rep = None if rep_id is None else rows.get(int(rep_id))
    moved: list[int] = []
    for row in deferred:
        target = TargetRow(
            int(row.id), str(row.state), str(row.override), row.state_reason, row.deferred_group_id
        )
        member = rows.get(int(row.job_id))
        if (
            rep is not None
            and member is not None
            and deferral_eligible(member, rep, override=target.override)
        ):
            continue
        try:
            await transition(
                conn,
                "score_targets",
                target.id,
                ["deferred_duplicate"],
                "pending",
                reason=CLEAR_REASON if target.state_reason == DENIED else None,
            )
        except TransitionConflict:
            continue  # another transaction moved it since the read (a sweep's undeferring half): left alone
        moved.append(target.id)
    if moved:
        log.info("duplicate_group_targets_undeferred", group_id=group_id, target_ids=moved)
    return tuple(moved)


# ---- reading and writing --------------------------------------------------------------------------------


def _view(row: Row[tuple[object, ...]]) -> JobView:
    locations = row.locations if isinstance(row.locations, list) else []
    return JobView(
        id=int(row.id),
        company_key=str(row.company_key),
        title_norm=str(row.title_norm),
        seniority_tokens=frozenset(row.seniority_tokens),
        locations=tuple(Location.model_validate(location) for location in locations),
        location_kind=str(row.location_kind),
        completeness=str(row.completeness),
        description_text=str(row.description_text),
        first_seen_at=row.first_seen_at,
        availability=str(row.availability),
        source_rank=int(row.source_rank),
        ats_boards=frozenset(row.ats_boards),
        requisition_ids=frozenset(row.requisition_ids),
        merged=bool(row.merged),
        target_state=None if row.target_state is None else str(row.target_state),
        has_tuple=bool(row.has_tuple),
    )


async def load_views(conn: AsyncConnection, job_ids: Iterable[int]) -> dict[int, JobView]:
    """The `JobView` of every job of `job_ids` that exists, by id."""
    ids = sorted(set(job_ids))
    if not ids:
        return {}
    rows = await conn.execute(_VIEWS, {"ids": ids, "worst": representative.WORST_RANK})
    return {int(row.id): _view(row) for row in rows}


async def _similarities(conn: AsyncConnection, title: str, views: Iterable[JobView]) -> dict[int, float]:
    """pg_trgm's `similarity(title, title_norm)` of each view, by job id."""
    listed = list(views)
    if not listed:
        return {}
    params = {"title": title, "ids": [v.id for v in listed], "titles": [v.title_norm for v in listed]}
    return {int(row.id): float(row.similarity) for row in await conn.execute(_SIMILARITY, params)}


async def _leave(conn: AsyncConnection, group_id: int, job_ids: Sequence[int], *, reason: str) -> None:
    await representative.leave(conn, group_id, job_ids)
    for job_id in job_ids:
        log.info("duplicate_group_member_left", group_id=group_id, job_id=job_id, reason=reason)


async def _revalidate(conn: AsyncConnection, group_id: int, representative_id: int | None) -> tuple[int, ...]:
    """The revalidation of every member (`revalidation_departures`: G2-G5 against the representative, then G1
    for every pair; G1 alone without one): those that fail leave (`member_left`), and a group left with fewer
    than two members is dissolved. The jobs that left. The caller holds every member's row."""
    member_ids = [int(job) for job in (await conn.execute(_MEMBERS_JOINED, {"group": group_id})).scalars()]
    views = await load_views(conn, member_ids)
    rep = None if representative_id is None else views[representative_id]
    others = [views[member] for member in member_ids if member != representative_id]
    similarity = {} if rep is None else await _similarities(conn, rep.title_norm, others)
    leaving = revalidation_departures(rep, others, similarity)
    if not leaving:
        return ()
    await _leave(conn, group_id, leaving, reason=MEMBER_LEFT)
    if len(member_ids) - len(leaving) < 2:
        return leaving + await representative.dissolve(conn, group_id, reason=MEMBER_LEFT)
    return leaving


async def _elect(conn: AsyncConnection, group_id: int, *, reason: str) -> tuple[int, ...]:
    """An election for `reason`, then the revalidation against the new representative; the jobs that left,
    those of a group of fewer than two members that is dissolved instead (as `reelect` would) among them."""
    group = (await conn.execute(_GROUP, {"group": group_id})).one()
    if group.status == "active" and len(await representative.member_ids(conn, group_id)) < 2:
        return await representative.dissolve(conn, group_id, reason=reason)
    elected = await representative.reelect(conn, group_id, reason=reason)
    return () if elected is None else await _revalidate(conn, group_id, elected)


async def _appoint(conn: AsyncConnection, group_id: int, job_id: int, *, reason: str) -> tuple[int, ...]:
    """`job_id` is elected (a full-text twin), then the revalidation; the jobs that left."""
    await representative.set_representative(conn, group_id, job_id, reason=reason)
    return await _revalidate(conn, group_id, job_id)


# ---- regroup_job ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GroupEffect:
    """What `regroup_job` did: the group the job ends in (or none), whether it joined an existing group or
    formed a new one, the jobs that left a group (the job itself among them when it left its old one), and
    whether a representative changed or the job qualified for several groups."""

    group_id: int | None
    joined: bool
    created: bool
    left: tuple[int, ...]
    reelected: bool
    ambiguous: bool


NO_EFFECT: Final = GroupEffect(None, joined=False, created=False, left=(), reelected=False, ambiguous=False)


@dataclass(frozen=True)
class _Locked:
    found: bool  # the job exists and is unmerged
    candidates: tuple[int, ...] = ()
    held: frozenset[int] = frozenset()


async def _lock_round(conn: AsyncConnection, job_id: int) -> _Locked | None:
    """One read-and-lock round (module docstring); `None` when what it read no longer holds under the lock."""
    row = (await conn.execute(_JOB, {"job": job_id})).first()
    if row is None:
        return _Locked(found=False)
    await conn.execute(_COMPANY_LOCK, {"namespace": COMPANY_LOCK_NAMESPACE, "company": row.company_key})
    params = {"company": row.company_key, "job": job_id, "first": row.first_seen_at, "days": CANDIDATE_DAYS}
    candidates = tuple(int(c) for c in (await conn.execute(_CANDIDATES, params)).scalars())
    jobs = (job_id, *candidates)
    locked = await merge.lock_jobs(conn, jobs, with_groups_of=jobs)
    again = (await conn.execute(_JOB, {"job": job_id})).first()
    if again is None:
        return _Locked(found=False)  # merged (or gone) before the lock: nothing to group
    if (again.company_key, again.first_seen_at) != (row.company_key, row.first_seen_at):
        return None
    if not await merge.co_members(conn, jobs) <= locked.keys():
        return None
    return _Locked(True, candidates, frozenset(locked))


async def _lock(conn: AsyncConnection, job_id: int) -> _Locked:
    for _ in range(_LOCK_ROUNDS):
        attempt = await conn.begin_nested()
        try:
            locked = await _lock_round(conn, job_id)
        except BaseException:
            await attempt.rollback()
            raise
        if locked is not None and locked.found:
            await attempt.commit()  # RELEASE SAVEPOINT: the locks stay with the transaction
            return locked
        await attempt.rollback()  # releases the round's row locks and its company lock
        if locked is not None:
            return locked
        log.info("regroup_set_changed_under_the_lock", job_id=job_id)
    raise InvariantViolation(f"job {job_id}: its company or its groups kept changing under the lock")


@dataclass
class _Groups:
    """The groups of the held jobs, their rows locked: status, representative and members by group id."""

    of: dict[int, int] = field(default_factory=dict)  # job → group
    status: dict[int, str] = field(default_factory=dict)
    representative: dict[int, int | None] = field(default_factory=dict)
    members: dict[int, tuple[int, ...]] = field(default_factory=dict)

    def view(self, group_id: int) -> GroupView:
        return GroupView(group_id, self.representative[group_id], self.members[group_id])


async def _read_groups(conn: AsyncConnection, held: frozenset[int]) -> _Groups:
    """The groups of the held jobs, their rows `FOR UPDATE` in id order; every member must be held."""
    found = _Groups()
    for row in await conn.execute(_MEMBERSHIPS, {"jobs": sorted(held)}):
        found.of[int(row.job_id)] = int(row.group_id)
    ids = sorted(set(found.of.values()))
    if not ids:
        return found
    for row in await conn.execute(_LOCK_GROUPS, {"ids": ids}):
        found.status[int(row.id)] = str(row.status)
        rep = row.representative_job_id
        found.representative[int(row.id)] = None if rep is None else int(rep)
    members: dict[int, list[int]] = {group_id: [] for group_id in ids}
    for row in await conn.execute(_MEMBERS_OF, {"ids": ids}):
        members[int(row.group_id)].append(int(row.job_id))
    found.members = {group_id: tuple(jobs) for group_id, jobs in members.items()}
    if unheld := sorted({job for jobs in members.values() for job in jobs} - held):
        raise InvariantViolation(f"the rows of group members {unheld} are not locked")
    return found


@dataclass
class _Regroup:
    """One `regroup_job` after its locks and its guard: the decisions of the module docstring."""

    conn: AsyncConnection
    job_id: int
    locked: _Locked
    views: dict[int, JobView] = field(default_factory=dict)
    similarity: dict[int, float] = field(default_factory=dict)
    different: frozenset[int] = frozenset()
    groups: _Groups = field(default_factory=_Groups)
    left: list[int] = field(default_factory=list)
    reelected: bool = False
    wrote: bool = False
    touched: set[int] = field(default_factory=set)  # the groups a member left, joined or re-elected in

    def effect(
        self, group_id: int | None, *, joined: bool = False, created: bool = False, ambiguous: bool = False
    ) -> GroupEffect:
        return GroupEffect(group_id, joined, created, tuple(self.left), self.reelected, ambiguous)

    async def run(self) -> GroupEffect:
        self.views = await load_views(self.conn, self.locked.held)
        a = self.views[self.job_id]
        self.similarity = await _similarities(self.conn, a.title_norm, self.views.values())
        rows = await self.conn.execute(_DIFFERENT_FROM, {"job": self.job_id})
        self.different = frozenset(int(other) for other in rows.scalars())
        self.groups = await _read_groups(self.conn, self.locked.held)
        old = self.groups.of.get(self.job_id)
        if old is not None and self.groups.status[old] != "active":  # a dissolved group keeps no member
            await self._write_leave(old, a.id, reason=MEMBER_LEFT)
            old = None
        if old is not None:
            if self.groups.representative[old] == a.id:
                return await self._representative(old, a)
            if self.groups.representative[old] is None:  # no representative to confirm against: G1 alone
                others = (self.views[m] for m in self.groups.members[old] if m != a.id)
                confirmed = not any(same_board(a, other) for other in others)
            else:
                evidence = join_evidence(
                    a, self.groups.view(old), self.views, different=self.different, similarity=self.similarity
                )
                confirmed = evidence is not None
            if confirmed:
                await self._end_ambiguity(old, a)
                return self.effect(old)  # in the right group: nothing written but an ambiguity's end
            await self._write_leave(old, a.id, reason=MEMBER_LEFT)
        return await self._group(a, excluded=old)

    async def _write_leave(self, group_id: int, job_id: int, *, reason: str) -> None:
        """The job (not the representative) leaves; a group left with fewer than two is dissolved."""
        await _leave(self.conn, group_id, [job_id], reason=reason)
        self.left.append(job_id)
        self.wrote = True
        self.touched.add(group_id)
        if self.groups.status[group_id] == "active" and len(self.groups.members[group_id]) - 1 < 2:
            self.left.extend(await representative.dissolve(self.conn, group_id, reason=reason))
        self.groups = await _read_groups(self.conn, self.locked.held)

    async def _representative(self, group_id: int, a: JobView) -> GroupEffect:
        """The job represents its group and its content changed: revalidate, or re-elect if it is not
        eligible any more."""
        elected = not eligible(a.availability, a.merged, a.target_state, has_tuple=a.has_tuple)
        self.touched.add(group_id)
        if elected:
            left = await _elect(self.conn, group_id, reason=REP_INACTIVE)
            self.wrote = True
        else:
            left = await _revalidate(self.conn, group_id, a.id)
        self.left.extend(left)
        self.wrote |= bool(left)
        still = (await self.conn.execute(_GROUP, {"group": group_id})).one().status == "active"
        # A group dissolved instead of electing (fewer than two members), or by the revalidation after its
        # election, has no representative: that is no election (the run's `elections` counter, ledger L330).
        self.reelected |= elected and still
        stays = still and a.id not in left
        if stays:
            await self._end_ambiguity(group_id, a)
        return self.effect(group_id if stays else None)

    async def _end_ambiguity(self, group_id: int, a: JobView) -> None:
        """The job stays in the group it is in. When no other active group of its candidates qualifies for it
        any more (§8.7's join rule, `choose_group`), the choice its `dedup.ambiguous_grouping` alert recorded
        is over, and the alert is resolved (F5 review, Minor 2); a job without an open alert updates no row.
        While another group still qualifies, the alert stays open. The candidates' groups are held (the one
        ordered lock statement), so the answer holds until the commit."""
        others = {self.groups.of[c.id] for c in self._candidates(a) if c.id in self.groups.of} - {group_id}
        groups = [self.groups.view(g) for g in sorted(others) if self.groups.status[g] == "active"]
        if choose_group(a, groups, self.views, different=self.different, similarity=self.similarity) is None:
            await resolve_alert(self.conn, code=AMBIGUOUS_GROUPING, subject=f"job:{a.id}")

    def _candidates(self, a: JobView) -> list[JobView]:
        """The candidates as they are under the lock: unmerged, of `a`'s company, in the window."""
        window = timedelta(days=CANDIDATE_DAYS)
        return [
            view
            for view in (self.views[c] for c in self.locked.candidates if c in self.views)
            if not view.merged
            and view.company_key == a.company_key
            and abs(view.first_seen_at - a.first_seen_at) <= window
            and view.id not in self.different
        ]

    async def _group(self, a: JobView, *, excluded: int | None) -> GroupEffect:
        candidates = self._candidates(a)
        joinable = {g for g, status in self.groups.status.items() if status == "active" and g != excluded}
        group_ids = sorted({self.groups.of[c.id] for c in candidates if c.id in self.groups.of} & joinable)
        groups = [self.groups.view(g) for g in group_ids]
        choice = choose_group(a, groups, self.views, different=self.different, similarity=self.similarity)
        if choice is not None:
            return await self._join(a, choice)
        ungrouped = [c for c in candidates if c.id not in self.groups.of]
        partner = choose_partner(a, ungrouped, different=self.different, similarity=self.similarity)
        if partner is None:
            return self.effect(None)
        return await self._form(a, *partner)

    async def _join(self, a: JobView, choice: Choice) -> GroupEffect:
        rep = self.groups.representative[choice.group_id]
        if rep is None:  # choose_group never picks a group without a representative
            raise InvariantViolation(f"duplicate group {choice.group_id} has no representative")
        ambiguous = len(choice.qualifying) > 1
        evidence = choice.evidence.as_json(rep, ambiguous=choice.qualifying if ambiguous else ())
        await self.conn.execute(_JOIN, {"group": choice.group_id, "job": a.id, "evidence": evidence})
        await self.conn.execute(_IN_GROUP, {"group": choice.group_id, "jobs": [a.id]})
        self.wrote = True
        self.touched.add(choice.group_id)
        log.info("duplicate_group_joined", group_id=choice.group_id, job_id=a.id)
        if ambiguous:  # recorded once, when the job joins; a regroup that confirms it raises nothing
            await raise_alert(
                self.conn,
                code=AMBIGUOUS_GROUPING,
                severity="info",
                subject=f"job:{a.id}",
                message="a job qualified for several duplicate groups and joined the best-scoring one",
                context={
                    "job_id": a.id,
                    "group_id": choice.group_id,
                    "group_ids": list[JSONValue](choice.qualifying),
                },
            )
            log.info(AMBIGUOUS_GROUPING, job_id=a.id, group_ids=list(choice.qualifying))
        if (
            a.completeness == "full"
            and self.views[rep].completeness == "partial"
            and eligible(a.availability, a.merged, a.target_state, has_tuple=a.has_tuple)
        ):
            left = await _appoint(self.conn, choice.group_id, a.id, reason=FULLER_MEMBER_JOINED)
            self.left.extend(left)
            still = (await self.conn.execute(_GROUP, {"group": choice.group_id})).one().status == "active"
            # As in `_representative`: a group its revalidation dissolved has no representative, so that is no
            # election (F5 review, Minor 3). Every rule of the pair is symmetric and the join checked it, so
            # the new representative's old one always stays and this cannot happen with the rules as they are.
            self.reelected |= still
            if not (still and a.id not in left):
                return self.effect(None, joined=True, ambiguous=ambiguous)
        return self.effect(choice.group_id, joined=True, ambiguous=ambiguous)

    async def _form(self, a: JobView, partner: JobView, evidence: GroupEvidence) -> GroupEffect:
        pair = [member_view(a), member_view(partner)]
        elected = representative.elect(pair)
        rep = None if elected is None else elected.job_id
        params = {
            "rep": rep,
            "reason": representative.unscorable_reason(pair) if rep is None else None,
            "election": INITIAL,
        }
        group_id = int((await self.conn.execute(_INSERT_GROUP, params)).scalar_one())
        for job, against in ((a.id, partner.id), (partner.id, a.id)):
            await self.conn.execute(
                _JOIN, {"group": group_id, "job": job, "evidence": evidence.as_json(against)}
            )
        await self.conn.execute(_IN_GROUP, {"group": group_id, "jobs": [a.id, partner.id]})
        self.wrote = True
        log.info(
            "duplicate_group_formed",
            group_id=group_id,
            job_ids=sorted((a.id, partner.id)),
            representative_job_id=rep,
        )
        return self.effect(group_id, created=True)


async def regroup_job(conn: AsyncConnection, job_id: int, *, guard: Guard) -> GroupEffect:
    """Group one job (§8.7; module docstring) in the caller's transaction, under the company lock and in the
    lock order of Ruling T20-a; what it did. The company lock must be the caller transaction's FIRST lock:
    deadlock freedom depends on no transaction ever waiting for it while holding a row (a caller that already
    holds rows must commit them first). A job that is merged or gone is left alone. `guard` runs after
    the job rows and before any write; it raises `FencedOut` to stop. Idempotent: a job already in the right
    group is confirmed and nothing is written, but the resolution of its ambiguous-grouping alert once no
    other group qualifies."""
    locked = await _lock(conn, job_id)
    if not locked.found:
        return NO_EFFECT
    await guard(conn)
    regroup = _Regroup(conn, job_id, locked)
    effect = await regroup.run()
    if regroup.wrote:
        for group_id in sorted(regroup.touched):  # the deferred targets after the group events
            await settle_deferred(conn, group_id)
        await ranking.bump(conn)
    return effect


# ---- a published representative (plan Task 10, Ruling P30) -----------------------------------------------

_PUBLISHED_GROUP: Final = q(
    "SELECT g.id, g.representative_job_id, (SELECT count(*) FROM score_targets t "
    "JOIN duplicate_group_members m ON m.job_id = t.job_id WHERE m.group_id = g.id "
    "AND t.state = 'deferred_duplicate') AS deferred FROM duplicate_group_members gm "
    "JOIN duplicate_groups g ON g.id = gm.group_id WHERE gm.job_id = :job AND g.status = 'active'"
)


async def representative_published(conn: AsyncConnection, job_id: int) -> None:
    """The publish transaction's step 7 (§9.4.1 L3539; Task 10): a published result of job `job_id` keeps the
    deferred members of its active duplicate group deferred, so nothing is written; the publication is
    logged with the group, whether the job is its representative and how many targets stay deferred (a plain
    read, no lock: the caller holds the job row). The re-election after an `all_failed` or `all_filtered`
    group is Task 15's."""
    group = (await conn.execute(_PUBLISHED_GROUP, {"job": job_id})).first()
    if group is None:
        return
    log.info(
        "group_member_published",
        job_id=job_id,
        group_id=int(group.id),
        representative=group.representative_job_id == job_id,
        deferred=int(group.deferred),
    )


# ---- leave, split, confirm -------------------------------------------------------------------------------


async def leave_group(conn: AsyncConnection, job_id: int, *, reason: str, held: Collection[int]) -> None:
    """The job leaves its duplicate group (merge step 5, §8.6.1), in the caller's transaction, which holds the
    row of every member (`held`: `InvariantViolation` otherwise, before anything is written) and takes no
    advisory lock (Ruling T20-a). A group left with fewer than two members is dissolved; a group whose
    representative left re-elects for `reason` and revalidates its members against the new one."""
    group = (await conn.execute(_GROUP_OF, {"job": job_id})).first()
    if group is None:
        return
    group_id = int(group.id)
    member_ids = await representative.member_ids(conn, group_id)
    if unheld := sorted(set(member_ids) - set(held)):
        raise InvariantViolation(f"group {group_id}: the rows of jobs {unheld} are not locked")
    await _leave(conn, group_id, [job_id], reason=reason)
    if group.status == "active":
        if len(member_ids) - 1 < 2:
            await representative.dissolve(conn, group_id, reason=reason)
        elif group.representative_job_id == job_id:
            await _elect(conn, group_id, reason=reason)
    await settle_deferred(conn, group_id)


async def _lock_group(
    conn: AsyncConnection, group_id: int, job_ids: Sequence[int]
) -> tuple[Row[tuple[object, ...]], tuple[int, ...], frozenset[int]]:
    """The rows of `job_ids` and of the group's members in one ordered statement, then the group row: the
    group, its members under the lock and the job rows held. Raises `LookupError` for a group that does not
    exist and `InvariantViolation` for one that is not active or whose members changed meanwhile."""
    read = await representative.member_ids(conn, group_id)
    held = frozenset(await merge.lock_jobs(conn, (*job_ids, *read), with_groups_of=read))
    rows = (await conn.execute(_LOCK_GROUPS, {"ids": [group_id]})).all()
    if not rows:
        raise LookupError(f"duplicate group {group_id} does not exist")
    if rows[0].status != "active":
        raise InvariantViolation(f"duplicate group {group_id} is {rows[0].status}, not active")
    member_ids = await representative.member_ids(conn, group_id)
    if not set(member_ids) <= held:
        raise InvariantViolation(f"duplicate group {group_id} changed while it was being locked")
    return rows[0], member_ids, held


async def split_member(conn: AsyncConnection, group_id: int, job_id: int) -> None:
    """The user splits the member off the group (§8.7): `dedup_decisions(different, user_split)` against every
    other member, the member leaves, and a group left with one member is `dissolved` (`split`); a group whose
    representative left re-elects (`split`) and revalidates. Its last statement bumps the ranking.

    Raises `LookupError` for a group that does not exist and `InvariantViolation` for a group that is not
    active or a job that is not its member, having written nothing."""
    group, member_ids, _ = await _lock_group(conn, group_id, (job_id,))
    if job_id not in member_ids:
        raise InvariantViolation(f"job {job_id} is not a member of duplicate group {group_id}")
    for other in member_ids:
        if other != job_id:
            await merge.decide_different(conn, job_id, other, source="user_split")
    await _leave(conn, group_id, [job_id], reason=SPLIT)
    if len(member_ids) - 1 < 2:
        await representative.dissolve(conn, group_id, reason=SPLIT)
    elif group.representative_job_id == job_id:
        await _elect(conn, group_id, reason=SPLIT)
    await settle_deferred(conn, group_id)  # the split member's target `deferred_duplicate → pending`
    await ranking.bump(conn)


async def confirm_group(conn: AsyncConnection, group_id: int) -> int:
    """The user confirms the group (§8.7): every other member is merged into the representative's job
    (`merge_jobs(user_confirmed_merge)`, whose step 5 dissolves the group when the last one left), with
    `dedup_decisions(same, user_confirm)` for each pair, after the bulk rows of every member's follow-up
    holds, the representative's included (`merge.lock_target_holds`: the merges cancel the members' targets
    and the recompute may supersede the representative's, Ruling P52); the representative is recomputed once,
    its hard-filter results written, a revision it no longer has superseded (`merge.supersede_revision`, §9.7,
    Ruling T22-e), and the ranking bumped last. Returns the representative's job id.

    Raises `LookupError` for a group that does not exist, `InvariantViolation` for one that is not active or
    has no representative, and `FencedOut` for a fenced member, having written nothing."""
    active = await filters.lock_active_scopes(conn)  # active_config FOR SHARE: the first lock (§3.3.3)
    group, member_ids, held = await _lock_group(conn, group_id, ())
    rep = group.representative_job_id
    if rep is None:
        raise InvariantViolation(f"duplicate group {group_id} has no representative to merge into")
    into = int(rep)
    # every merge's bulk rows, and the representative's (its revision may move), before any target row (P52)
    await merge.lock_target_holds(conn, member_ids)
    previous = await merge.current_revision(conn, into)
    evidence: dict[str, JSONValue] = {"rule": "user_confirm", "group_id": group_id}
    for member in member_ids:
        if member == into:
            continue
        await conn.execute(_SAME, {"a": min(into, member), "b": max(into, member), "source": "user_confirm"})
        await merge.merge_jobs(
            conn,
            from_job_id=member,
            into_job_id=into,
            kind="user_confirmed_merge",
            evidence=evidence,
            recompute=False,
            locked=held,
        )
    _, result = await merge.recompute_rollups(conn, into)
    await filters.refilter_job_live(conn, into, active=active)  # the det-facts change hook (Task 15)
    await merge.supersede_revision(  # out of a run (Ruling T22-e)
        conn, into, previous_revision_id=previous, current_revision_id=result.revision_id, active=active
    )
    log.info("duplicate_group_confirmed", group_id=group_id, into_job_id=into, merged=len(member_ids) - 1)
    await ranking.bump(conn)
    return into


# ---- the node's pass and the reconciler -----------------------------------------------------------------


async def regroup_run_jobs(engine: AsyncEngine, owner: RunOwner) -> dict[str, int]:
    """Node 6's grouping pass (module docstring): `regroup_job` of every job changed in `owner`'s run, one
    transaction each, guarded by its owner row; `GROUPING_COUNTERS`. Raises `FencedOut` for a superseded
    owner, whose transaction wrote nothing."""
    async with engine.connect() as conn:
        jobs = [int(job) for job in (await conn.execute(_CHANGED_IN_RUN, {"run": owner.run_id})).scalars()]

    async def owned(conn: AsyncConnection) -> None:
        await ledger.lock_run_owner(conn, owner)

    counts = dict.fromkeys(GROUPING_COUNTERS, 0)
    for job_id in jobs:
        async with engine.begin() as conn:
            effect = await regroup_job(conn, job_id, guard=owned)
        counts["groups_formed"] += effect.created
        counts["groups_changed"] += effect.joined or bool(effect.left) or effect.reelected
        counts["elections"] += effect.reelected
    if jobs:
        log.info("run_jobs_grouped", run_id=owner.run_id, jobs=len(jobs), **counts)
    return counts


def _shares_a_board(views: Iterable[JobView]) -> bool:
    """Whether two of the jobs carry keys of one ATS board (G1 fails for a pair of them)."""
    listed = list(views)
    return any(same_board(x, y) for i, x in enumerate(listed) for y in listed[i + 1 :])


async def repair_group(conn: AsyncConnection, group_id: int, *, guard: Guard) -> bool:
    """One `group_representatives` repair (module docstring) in the caller's transaction: whether the group
    broke the invariant and was repaired. `guard` runs after the job rows; it raises `FencedOut` to stop."""
    row = (await conn.execute(_GROUP, {"group": group_id})).first()
    if row is None or row.status != "active":
        return False
    read = await representative.member_ids(conn, group_id)
    named = [] if row.representative_job_id is None else [int(row.representative_job_id)]
    held = frozenset(await merge.lock_jobs(conn, (*read, *named), with_groups_of=read))
    await guard(conn)
    rows = (await conn.execute(_LOCK_GROUPS, {"ids": [group_id]})).all()
    if not rows or rows[0].status != "active":
        return False
    member_ids = await representative.member_ids(conn, group_id)
    if not set(member_ids) <= held:
        return False  # it changed since the read: the next pass sees it again
    rep = rows[0].representative_job_id
    views = {view.job_id: view for view in await representative.member_views(conn, group_id)}
    if len(member_ids) < 2:
        reason = RECONCILER_REPAIR
        await representative.dissolve(conn, group_id, reason=reason)
    elif rep is not None and not (int(rep) in views and views[int(rep)].eligible):
        reason = RECONCILER_REPAIR
        await _elect(conn, group_id, reason=reason)
    elif rep is None and any(view.eligible for view in views.values()):
        reason = ELIGIBLE_AGAIN
        await _elect(conn, group_id, reason=reason)
    elif _shares_a_board((await load_views(conn, member_ids)).values()):
        reason = MEMBER_LEFT  # G1: no election, the members on a board leave (final review I2)
        await _revalidate(conn, group_id, None if rep is None else int(rep))
    else:
        return False
    await settle_deferred(conn, group_id)
    log.info("duplicate_group_repaired", group_id=group_id, reason=reason, representative_job_id=rep)
    await ranking.bump(conn)
    return True


@reconciler("group_representatives", "heavy")
async def group_representatives(ctx: ReconcileContext) -> int:
    """§17.4 `group_representatives` (module docstring): at most one election per violating group, one
    transaction each, fenced by the worker epoch after the job rows. Returns how many groups it repaired. A
    superseded worker raises `FencedOut` at its first fenced transaction, which wrote nothing, so that
    `run_reconcilers` stops the cadence at this reconciler and the worker requests its stop (Ruling FR-r;
    fix wave F7, Ruling FR-w); a `FencedOut` of another owner ends the pass with the count so far."""
    async with ctx.engine.connect() as conn:
        violating = [int(group_id) for group_id in (await conn.execute(_VIOLATIONS)).scalars()]

    async def fenced(conn: AsyncConnection) -> None:
        await fence_epoch(conn, ctx.epoch)

    repaired = 0
    for group_id in violating:
        try:
            async with ctx.engine.begin() as conn:
                done = await repair_group(conn, group_id, guard=fenced)
        except FencedOut as fenced_out:
            if fenced_out.worker_superseded:
                raise  # `fence_epoch` logged it
            return repaired
        if done:
            repaired += 1
            DEDUP_GROUP_REPAIRS.inc()
    if repaired:
        log.info("group_representatives_repaired", groups=repaired)
    return repaired
