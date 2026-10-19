"""Probable-duplicate groups (spec §8.7, §7.17 #4, §17.4): the rules G1-G5, group formation, revalidation,
split, confirm, leave, the node's grouping pass and the `group_representatives` reconciler. No group ever
merges jobs, except `confirm_group`, the user's confirmation.

**The rules.** A `JobView` is what they read of a job: its canonical columns, its current revision's
description, its best source precedence rank, the `<type>:<board>` of its `ats:` keys and the Greenhouse
requisition ids of its postings' latest observations. `pair_evidence(a, b, title_similarity)` applies:

- **G1**: two jobs that carry a key of one ATS board are never grouped (Adzuna re-ingests may be);
- **G2** (`compatible_location`): both `remote`; both `country_wide`; or a gazetteer candidate id the two
  share (`shared_place`); or every candidate pair within `G2_KM` (`within_25km`). An unresolved location
  gives no evidence, so a job with no resolved location is compatible only by the two kinds (an
  interpretation: a resolved location beside an unresolved one still counts);
- **G3**: identical seniority-token sets;
- **G4**: `similarity(title_norm)` (pg_trgm, computed by the caller) ≥ `G4_TITLE`;
- **G5**: `shingles.text_confirms` of the two descriptions and their completeness.

It returns the `GroupEvidence` (with a requisition id match as extra evidence, never in place of G1-G5), or
`None` when a rule fails. `confirms(a, b, …)` is G2-G5 alone.

**Formation** (`choose_group`, `choose_partner`, pure): a job joins an active group whose representative it
confirms (G2-G5), against whose every member it passes G1 and has no `different` decision, and that has fewer
than `MAX_MEMBERS` members; a group without a representative cannot be validated and is not joined. Of
several, the one with the best text score, then the lowest id (`dedup.ambiguous_grouping`, info, logged and
kept in the member's evidence). Otherwise the ungrouped candidate with the best pair evidence (text score,
then lowest id) forms a new group with it, its representative elected by §8.7's order (`initial`). Groups
are never merged together.

**`regroup_job`** (the grouping of one job, in the caller's transaction): the candidates are the jobs of the
same `company_key`, first seen at most `CANDIDATE_DAYS` apart, unmerged, with no `different` decision against
it. A job already in a group whose representative it still confirms is left as it is, and nothing is
written (idempotence; a group without a representative confirms its members too). A job that no longer
confirms leaves (`member_left`) and is grouped anew. A representative whose content changed is revalidated
against: its members that no longer confirm leave; when it is not eligible any more the group re-elects
(`rep_inactive`). A joining `full` twin of a `partial` representative is elected (`fuller_member_joined`) when
it is eligible. After every representative change the members are revalidated against the new one (G2-G5)
and those that fail leave (`member_left`); a group left with fewer than two members is dissolved. The ranking
generation is bumped, as the last statement, when anything was written.

**Locks (Ruling T20-a, §3.3.3).** `regroup_job` takes `pg_advisory_xact_lock(hashtext(company_key))` FIRST,
before any row: regroups of one company are serialised. Then the job, its candidates and the current members
of every group it may join or leave (the groups of the job and of its candidates) in ONE ordered `FOR UPDATE`
statement (`merge.lock_jobs`), so a job is never added to a group whose members another transaction holds
(carry 4). Then `guard(conn)` (the run's owner row, or the reconciler's epoch fence: Ruling T19-a puts it
after the job rows). Then the group rows `FOR UPDATE` in id order, the other domain rows, and
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

**`group_representatives`** (cadence `heavy`, §17.4): the active groups that break §7.17 #4 as it reads in M1
(a representative that is not an eligible member; no representative while a member is eligible) or have
fewer than two members are read; each is repaired in a transaction of its own: its members' rows (and its
representative's: the FK of a new one takes `FOR KEY SHARE` on it) in one ordered statement, then
`fence_epoch` (Ruling T19-a: after the job rows; the brief's "fence first" holds only for a pass that locks no
job row, and this one locks them), then the group row, and the violation is checked again. At most one
election per group per pass (`reconciler_repair`, or `eligible_again` for a group without a representative),
followed by the revalidation of the members, which Task 18's stand-in never did; a group of fewer than two
is dissolved. Each repair is logged and counted in `DEDUP_GROUP_REPAIRS` once its transaction committed.
Because ineligible members are never elected, repair terminates.

**`regroup_run_jobs`** (node 6's grouping pass): every unmerged job with a `job_revision_occurrences` row
whose `became_current_at ≥ runs.started_at` (the first attempt's start: the `running → running` self-edge
keeps it; both in the database clock), in id order, one transaction each, guarded by the run's owner row. The
selection is the durable trace of "created or changed in this run", never an in-memory result, so a resumed
attempt re-runs it and a job already grouped is only confirmed. Returns `GROUPING_COUNTERS`: `groups_formed`
(new groups), `groups_changed` (a regroup that joined an existing group, made a member leave or changed a
representative) and `elections` (representative changes).

Every timestamp written is the database's `now()`; no function here takes a caller clock (Ruling T18-b's
convention, a deviation from the brief's `now` parameters).
"""

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.db import ranking
from jobhunter.db.guards import Guard
from jobhunter.db.sql import q
from jobhunter.dedup import merge, representative
from jobhunter.dedup.representative import ELIGIBLE_SQL, MemberView, eligible
from jobhunter.dedup.shingles import TextSide, text_confirms
from jobhunter.errors import FencedOut, InvariantViolation
from jobhunter.geo.gazetteer import distance_km
from jobhunter.observability.logging import get_logger
from jobhunter.observability.metrics import DEDUP_GROUP_REPAIRS
from jobhunter.quota import ledger
from jobhunter.scoring import filters
from jobhunter.sources.parsing.locations import Candidate, Location
from jobhunter.types import JSONValue
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.reconcilers import ReconcileContext, reconciler
from jobhunter.worker.runs import RunOwner

G4_TITLE: Final = 0.85
CANDIDATE_DAYS: Final = 45
MAX_MEMBERS: Final = 10
G2_KM: Final = 25.0
GROUPING_COUNTERS: Final = ("groups_formed", "groups_changed", "elections")
AMBIGUOUS_GROUPING: Final = "dedup.ambiguous_grouping"
# election reasons (§7.9 `duplicate_groups.election_reason`)
INITIAL: Final = "initial"
FULLER_MEMBER_JOINED: Final = "fuller_member_joined"
MEMBER_LEFT: Final = "member_left"
SPLIT: Final = "split"
REP_INACTIVE: Final = "rep_inactive"
RECONCILER_REPAIR: Final = "reconciler_repair"
ELIGIBLE_AGAIN: Final = "eligible_again"
_LOCK_ROUNDS: Final = 3  # read-then-lock rounds before a set that keeps changing is an invariant breach
_DIGITS: Final = 4  # the precision of a score in a member's evidence

_JOB: Final = q("SELECT company_key, first_seen_at FROM jobs WHERE id = :job AND merged_into_job_id IS NULL")
_COMPANY_LOCK: Final = q("SELECT pg_advisory_xact_lock(hashtext(:company))")
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
    "SELECT j.id, j.company_key, j.title_norm, j.seniority_tokens, j.locations, j.location_kind, "
    "j.completeness, coalesce(r.description_text, '') AS description_text, j.first_seen_at, j.availability, "
    "coalesce((SELECT min(s.precedence_rank) FROM sources s WHERE s.id = ANY(j.source_ids)), :worst) "
    "AS source_rank, "
    "ARRAY(SELECT DISTINCT substr(k.key_ns, 5) FROM ad_keys k WHERE k.job_id = j.id "
    "AND starts_with(k.key_ns, 'ats:')) AS ats_boards, "
    "ARRAY(SELECT DISTINCT o.normalized ->> 'requisition_id' FROM postings p "
    "JOIN posting_observations o ON o.id = p.latest_observation_id WHERE p.job_id = j.id "
    "AND o.normalized ->> 'requisition_id' IS NOT NULL) AS requisition_ids, "
    "j.merged_into_job_id IS NOT NULL AS merged "
    "FROM jobs j LEFT JOIN job_revisions r ON r.id = j.current_revision_id "
    "WHERE j.id = ANY(CAST(:ids AS bigint[]))"
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
_MEMBERS: Final = q("SELECT job_id FROM duplicate_group_members WHERE group_id = :group ORDER BY job_id")
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
# The jobs created or changed in the run: a current revision that became current since its first attempt.
_CHANGED_IN_RUN: Final = q(
    "SELECT DISTINCT o.job_id FROM job_revision_occurrences o JOIN jobs j ON j.id = o.job_id "
    "WHERE j.merged_into_job_id IS NULL "
    "AND o.became_current_at >= (SELECT r.started_at FROM runs r WHERE r.id = :run) ORDER BY o.job_id"
)
# §7.17 #4 as it reads in M1, and a group of fewer than two members.
_VIOLATIONS: Final = q(
    "SELECT g.id FROM duplicate_groups g WHERE g.status = 'active' AND ("  # noqa: S608 (constants only)
    "(SELECT count(*) FROM duplicate_group_members m WHERE m.group_id = g.id) < 2 "
    "OR (g.representative_job_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM duplicate_group_members m "
    "JOIN jobs j ON j.id = m.job_id WHERE m.group_id = g.id AND m.job_id = g.representative_job_id "
    f"AND {ELIGIBLE_SQL})) "
    "OR (g.representative_job_id IS NULL AND EXISTS (SELECT 1 FROM duplicate_group_members m "
    f"JOIN jobs j ON j.id = m.job_id WHERE m.group_id = g.id AND {ELIGIBLE_SQL}))) ORDER BY g.id"
)

log = get_logger("jobhunter.dedup.groups")


# ---- the rules ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class JobView:
    """What the grouping rules read of a job (module docstring)."""

    id: int
    company_key: str
    title_norm: str
    seniority_tokens: frozenset[str]
    locations: tuple[Location, ...]
    location_kind: str
    completeness: str
    description_text: str
    first_seen_at: datetime
    availability: str
    source_rank: int
    ats_boards: frozenset[str]  # `<type>:<board>` of its `ats:` keys
    requisition_ids: frozenset[str]
    merged: bool


@dataclass(frozen=True)
class GroupEvidence:
    """Why a pair satisfies G1-G5: the title similarity, the G5 score, the G2 rule and a requisition match."""

    title_similarity: float
    text_score: float
    location_rule: str
    requisition_match: bool

    def as_json(self, against_job_id: int, *, ambiguous: Sequence[int] = ()) -> dict[str, JSONValue]:
        """The membership's `evidence`: these values, validated against `against_job_id`."""
        evidence: dict[str, JSONValue] = {
            "against_job_id": against_job_id,
            "title_similarity": round(self.title_similarity, _DIGITS),
            "text_score": round(self.text_score, _DIGITS),
            "location_rule": self.location_rule,
            "requisition_match": self.requisition_match,
        }
        if ambiguous:
            evidence["ambiguous_group_ids"] = list[JSONValue](ambiguous)
        return evidence


def _places(view: JobView) -> list[Candidate]:
    return [
        c for location in view.locations if location.resolution != "unresolved" for c in location.candidates
    ]


def location_rule(a: JobView, b: JobView) -> str | None:
    """The G2 rule the pair satisfies (module docstring), or `None`."""
    if a.location_kind == b.location_kind == "remote":
        return "both_remote"
    if a.location_kind == b.location_kind == "country_wide":
        return "both_country_wide"
    places_a, places_b = _places(a), _places(b)
    if not places_a or not places_b:
        return None
    if {c.gazetteer_id for c in places_a} & {c.gazetteer_id for c in places_b}:
        return "shared_place"
    if all(distance_km(x.lat, x.lon, y.lat, y.lon) <= G2_KM for x in places_a for y in places_b):
        return "within_25km"
    return None


def compatible_location(a: JobView, b: JobView) -> bool:
    """G2: the two jobs' locations are compatible."""
    return location_rule(a, b) is not None


def same_board(a: JobView, b: JobView) -> bool:
    """G1 fails: both jobs carry a key of one ATS board."""
    return bool(a.ats_boards & b.ats_boards)


def _side(view: JobView) -> TextSide:
    return TextSide(view.description_text, "full" if view.completeness == "full" else "partial")


def confirms(a: JobView, b: JobView, title_similarity: float) -> GroupEvidence | None:
    """G2-G5 of the pair (the test against a representative); `None` when one fails."""
    rule = location_rule(a, b)
    if rule is None or a.seniority_tokens != b.seniority_tokens or title_similarity < G4_TITLE:
        return None
    confirmed, score = text_confirms(_side(a), _side(b))
    if not confirmed:
        return None
    return GroupEvidence(title_similarity, score, rule, bool(a.requisition_ids & b.requisition_ids))


def pair_evidence(a: JobView, b: JobView, title_similarity: float) -> GroupEvidence | None:
    """G1-G5 of the pair (module docstring); `None` when one fails."""
    return None if same_board(a, b) else confirms(a, b, title_similarity)


@dataclass(frozen=True)
class GroupView:
    """An active group as formation reads it: its representative (or none) and its members."""

    group_id: int
    representative: int | None
    members: tuple[int, ...]


@dataclass(frozen=True)
class Choice:
    """The group a job joins, its evidence against the representative, and every group it qualified for
    (the chosen one first)."""

    group_id: int
    evidence: GroupEvidence
    qualifying: tuple[int, ...]


def join_evidence(
    a: JobView,
    group: GroupView,
    views: Mapping[int, JobView],
    *,
    different: Collection[int],
    similarity: Mapping[int, float],
) -> GroupEvidence | None:
    """Whether `a` may be in `group` (module docstring): G1 and no `different` decision against every other
    member, fewer than `MAX_MEMBERS` others, and G2-G5 against the representative; its evidence or `None`."""
    others = [member for member in group.members if member != a.id]
    if group.representative is None or group.representative == a.id or len(others) >= MAX_MEMBERS:
        return None
    if any(member in different or same_board(a, views[member]) for member in others):
        return None
    return confirms(a, views[group.representative], similarity[group.representative])


def choose_group(
    a: JobView,
    groups: Iterable[GroupView],
    views: Mapping[int, JobView],
    *,
    different: Collection[int],
    similarity: Mapping[int, float],
) -> Choice | None:
    """The group `a` joins: of those it qualifies for, the best text score, then the lowest id."""
    qualifying = [
        (evidence, group.group_id)
        for group in groups
        if (evidence := join_evidence(a, group, views, different=different, similarity=similarity))
        is not None
    ]
    if not qualifying:
        return None
    qualifying.sort(key=lambda found: (-found[0].text_score, found[1]))
    return Choice(qualifying[0][1], qualifying[0][0], tuple(group_id for _, group_id in qualifying))


def choose_partner(
    a: JobView,
    candidates: Iterable[JobView],
    *,
    different: Collection[int],
    similarity: Mapping[int, float],
) -> tuple[JobView, GroupEvidence] | None:
    """The ungrouped candidate `a` forms a new group with: G1-G5, no `different` decision, the best text
    score, then the lowest id."""
    found = [
        (evidence, candidate)
        for candidate in candidates
        if candidate.id != a.id
        and candidate.id not in different
        and (evidence := pair_evidence(a, candidate, similarity[candidate.id])) is not None
    ]
    if not found:
        return None
    evidence, partner = min(found, key=lambda pair: (-pair[0].text_score, pair[1].id))
    return partner, evidence


def member_view(view: JobView) -> MemberView:
    """The election's view of a job."""
    return MemberView(
        job_id=view.id,
        availability=view.availability,
        completeness=view.completeness,
        source_rank=view.source_rank,
        description_length=len(view.description_text),
        first_seen_at=view.first_seen_at,
        eligible=eligible(view.availability, view.merged),
    )


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


async def _members(conn: AsyncConnection, group_id: int) -> list[int]:
    return [int(job) for job in (await conn.execute(_MEMBERS, {"group": group_id})).scalars()]


async def _leave(conn: AsyncConnection, group_id: int, job_ids: Sequence[int], *, reason: str) -> None:
    await representative.leave(conn, group_id, job_ids)
    for job_id in job_ids:
        log.info("duplicate_group_member_left", group_id=group_id, job_id=job_id, reason=reason)


async def _revalidate(conn: AsyncConnection, group_id: int, representative_id: int) -> tuple[int, ...]:
    """G2-G5 of every member against the representative: those that fail leave (`member_left`), and a group
    left with fewer than two members is dissolved. The jobs that left. The caller holds every member's row."""
    member_ids = await _members(conn, group_id)
    views = await load_views(conn, member_ids)
    rep = views[representative_id]
    others = [views[member] for member in member_ids if member != representative_id]
    similarity = await _similarities(conn, rep.title_norm, others)
    leaving = tuple(view.id for view in others if confirms(view, rep, similarity[view.id]) is None)
    if not leaving:
        return ()
    await _leave(conn, group_id, leaving, reason=MEMBER_LEFT)
    if len(member_ids) - len(leaving) < 2:
        return leaving + await representative.dissolve(conn, group_id, reason=MEMBER_LEFT)
    return leaving


async def _elect(conn: AsyncConnection, group_id: int, *, reason: str) -> tuple[int, ...]:
    """An election for `reason`, then the revalidation against the new representative; the jobs that left."""
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
    await conn.execute(_COMPANY_LOCK, {"company": row.company_key})
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
            confirmed = self.groups.representative[old] is None or (
                join_evidence(
                    a, self.groups.view(old), self.views, different=self.different, similarity=self.similarity
                )
                is not None
            )
            if confirmed:
                return self.effect(old)  # in the right group: nothing written
            await self._write_leave(old, a.id, reason=MEMBER_LEFT)
        return await self._group(a, excluded=old)

    async def _write_leave(self, group_id: int, job_id: int, *, reason: str) -> None:
        """The job (not the representative) leaves; a group left with fewer than two is dissolved."""
        await _leave(self.conn, group_id, [job_id], reason=reason)
        self.left.append(job_id)
        self.wrote = True
        if self.groups.status[group_id] == "active" and len(self.groups.members[group_id]) - 1 < 2:
            self.left.extend(await representative.dissolve(self.conn, group_id, reason=reason))
        self.groups = await _read_groups(self.conn, self.locked.held)

    async def _representative(self, group_id: int, a: JobView) -> GroupEffect:
        """The job represents its group and its content changed: revalidate, or re-elect if it is not
        eligible any more."""
        if eligible(a.availability, a.merged):
            left = await _revalidate(self.conn, group_id, a.id)
        else:
            left = await _elect(self.conn, group_id, reason=REP_INACTIVE)
            self.reelected = self.wrote = True
        self.left.extend(left)
        self.wrote |= bool(left)
        still = (await self.conn.execute(_GROUP, {"group": group_id})).one().status == "active"
        return self.effect(group_id if still and a.id not in left else None)

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
        log.info("duplicate_group_joined", group_id=choice.group_id, job_id=a.id)
        if ambiguous:
            log.info(AMBIGUOUS_GROUPING, job_id=a.id, group_ids=list(choice.qualifying))
        if (
            a.completeness == "full"
            and self.views[rep].completeness == "partial"
            and eligible(a.availability, a.merged)
        ):
            self.left.extend(await _appoint(self.conn, choice.group_id, a.id, reason=FULLER_MEMBER_JOINED))
            self.reelected = True
        return self.effect(choice.group_id, joined=True, ambiguous=ambiguous)

    async def _form(self, a: JobView, partner: JobView, evidence: GroupEvidence) -> GroupEffect:
        elected = representative.elect([member_view(a), member_view(partner)])
        rep = None if elected is None else elected.job_id
        params = {
            "rep": rep,
            "reason": representative.NO_ACTIVE_MEMBER if rep is None else None,
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
    lock order of Ruling T20-a; what it did. A job that is merged or gone is left alone. `guard` runs after
    the job rows and before any write; it raises `FencedOut` to stop. Idempotent: a job already in the right
    group is confirmed and nothing is written."""
    locked = await _lock(conn, job_id)
    if not locked.found:
        return NO_EFFECT
    await guard(conn)
    regroup = _Regroup(conn, job_id, locked)
    effect = await regroup.run()
    if regroup.wrote:
        await ranking.bump(conn)
    return effect


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
    member_ids = await _members(conn, group_id)
    if unheld := sorted(set(member_ids) - set(held)):
        raise InvariantViolation(f"group {group_id}: the rows of jobs {unheld} are not locked")
    await _leave(conn, group_id, [job_id], reason=reason)
    if group.status != "active":
        return
    if len(member_ids) - 1 < 2:
        await representative.dissolve(conn, group_id, reason=reason)
    elif group.representative_job_id == job_id:
        await _elect(conn, group_id, reason=reason)


async def _lock_group(
    conn: AsyncConnection, group_id: int, job_ids: Sequence[int]
) -> tuple[Row[tuple[object, ...]], list[int], frozenset[int]]:
    """The rows of `job_ids` and of the group's members in one ordered statement, then the group row: the
    group, its members under the lock and the job rows held. Raises `LookupError` for a group that does not
    exist and `InvariantViolation` for one that is not active or whose members changed meanwhile."""
    read = await _members(conn, group_id)
    held = frozenset(await merge.lock_jobs(conn, (*job_ids, *read), with_groups_of=read))
    rows = (await conn.execute(_LOCK_GROUPS, {"ids": [group_id]})).all()
    if not rows:
        raise LookupError(f"duplicate group {group_id} does not exist")
    if rows[0].status != "active":
        raise InvariantViolation(f"duplicate group {group_id} is {rows[0].status}, not active")
    member_ids = await _members(conn, group_id)
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
    await ranking.bump(conn)


async def confirm_group(conn: AsyncConnection, group_id: int) -> int:
    """The user confirms the group (§8.7): every other member is merged into the representative's job
    (`merge_jobs(user_confirmed_merge)`, whose step 5 dissolves the group when the last one left), with
    `dedup_decisions(same, user_confirm)` for each pair; the representative is recomputed once, its
    hard-filter results written, and the ranking bumped last. Returns the representative's job id.

    Raises `LookupError` for a group that does not exist, `InvariantViolation` for one that is not active or
    has no representative, and `FencedOut` for a fenced member, having written nothing."""
    scopes = await filters.active_scopes(conn)  # active_config FOR SHARE: the first lock (§3.3.3)
    group, member_ids, held = await _lock_group(conn, group_id, ())
    rep = group.representative_job_id
    if rep is None:
        raise InvariantViolation(f"duplicate group {group_id} has no representative to merge into")
    into = int(rep)
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
    await merge.recompute_rollups(conn, into)
    await filters.refilter_job(conn, into, scopes)
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


async def repair_group(conn: AsyncConnection, group_id: int, *, guard: Guard) -> bool:
    """One `group_representatives` repair (module docstring) in the caller's transaction: whether the group
    broke the invariant and was repaired. `guard` runs after the job rows; it raises `FencedOut` to stop."""
    row = (await conn.execute(_GROUP, {"group": group_id})).first()
    if row is None or row.status != "active":
        return False
    read = await _members(conn, group_id)
    named = [] if row.representative_job_id is None else [int(row.representative_job_id)]
    held = frozenset(await merge.lock_jobs(conn, (*read, *named), with_groups_of=read))
    await guard(conn)
    rows = (await conn.execute(_LOCK_GROUPS, {"ids": [group_id]})).all()
    if not rows or rows[0].status != "active":
        return False
    member_ids = await _members(conn, group_id)
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
    else:
        return False
    log.info("duplicate_group_repaired", group_id=group_id, reason=reason, representative_job_id=rep)
    await ranking.bump(conn)
    return True


@reconciler("group_representatives", "heavy")
async def group_representatives(ctx: ReconcileContext) -> int:
    """§17.4 `group_representatives` (module docstring): at most one election per violating group, one
    transaction each, fenced by the worker epoch after the job rows. Returns how many groups it repaired: 0
    for a superseded worker, which writes nothing."""
    async with ctx.engine.connect() as conn:
        violating = [int(group_id) for group_id in (await conn.execute(_VIOLATIONS)).scalars()]

    async def fenced(conn: AsyncConnection) -> None:
        await fence_epoch(conn, ctx.epoch)

    repaired = 0
    for group_id in violating:
        try:
            async with ctx.engine.begin() as conn:
                done = await repair_group(conn, group_id, guard=fenced)
        except FencedOut:
            return repaired  # `fence_epoch` logged it
        if done:
            repaired += 1
            DEDUP_GROUP_REPAIRS.inc()
    if repaired:
        log.info("group_representatives_repaired", groups=repaired)
    return repaired
