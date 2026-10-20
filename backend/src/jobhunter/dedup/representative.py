"""The representative of a duplicate group (spec §8.7, §7.17 #4): eligibility, the election order, elections
and dissolution.

**Eligibility** (M1): a member is eligible when its job is unmerged and not `closed`. The clause on its
active-tuple target (`pending`, `budget_deferred`, `in_progress`, `in_batch`, `deferred_duplicate` or
published) arrives with the targets in M3 (plan Deviation 9). `ELIGIBLE_SQL` is the same predicate over a
`jobs` row aliased `j`, for the statements that select by it; the two change together.

**`elect`** returns the eligible member that sorts first by the six keys of §8.7: availability (`active` >
`unknown` > `stale`), completeness (`full` > `partial`), the best source precedence rank (ATS 1, JSearch and
SerpApi 2, Arbeitnow 3, Adzuna 4), description length descending, the earliest `first_seen_at`, the lowest
job id; `None` when no member is eligible.

**`reelect`** runs an election in the caller's transaction, which holds the row of every member (the job rows
first, §3.3.3): the group row is locked `FOR UPDATE`; a group with fewer than two members is dissolved
instead (`dissolve`); otherwise `representative_job_id`, `election_reason`, `elected_at` and `updated_at`
are set, and `unscorable_reason` from the result: `NULL` with a representative, `no_active_member` without
one (the M3 reasons `all_failed` and `all_filtered` need the targets). An election never keeps an earlier
reason it did not find again. Representative changes are not state transitions (§7.16.8); the group stays
`active`. The revalidation of the members against a new representative is `groups`' (§8.7).

**`dissolve`**: the remaining members leave (`duplicate_group_members` rows deleted, `jobs.duplicate_group_id
:= NULL`) and the group moves `active → dissolved` through `transition()` with the reason as its
`election_reason`; its representative and `unscorable_reason` are cleared, since a dissolved group has none.
A job that leaves a group (`leave`, which `dissolve` and every path of `groups` use) has its
`dedup.ambiguous_grouping` alert resolved.

Every timestamp written is the database's `now()` (Ruling T18-b's convention: no caller clock).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.observability.alerts import resolve_alert
from jobhunter.observability.logging import get_logger

NO_ACTIVE_MEMBER: Final = "no_active_member"
# An info alert, subject `job:<id>` (Ruling T20-b): raised by `groups` when a job joins the best of several
# groups, resolved here when it leaves.
AMBIGUOUS_GROUPING: Final = "dedup.ambiguous_grouping"
# §8.7's order of the eligible availabilities; `closed` is never eligible.
AVAILABILITY_ORDER: Final = ("active", "unknown", "stale")
WORST_RANK: Final = 99  # the precedence rank of a job without a known source
ELIGIBLE_SQL: Final = "(j.merged_into_job_id IS NULL AND j.availability <> 'closed')"

_GROUP: Final = q("SELECT status, representative_job_id FROM duplicate_groups WHERE id = :group FOR UPDATE")
_MEMBER_VIEWS: Final = q(
    "SELECT j.id, j.availability, j.completeness, j.first_seen_at, "
    "j.merged_into_job_id IS NOT NULL AS merged, "
    "coalesce((SELECT min(s.precedence_rank) FROM sources s WHERE s.id = ANY(j.source_ids)), :worst) "
    "AS source_rank, length(coalesce(r.description_text, '')) AS description_length "
    "FROM duplicate_group_members m JOIN jobs j ON j.id = m.job_id "
    "LEFT JOIN job_revisions r ON r.id = j.current_revision_id WHERE m.group_id = :group ORDER BY j.id"
)
_ELECT: Final = q(
    "UPDATE duplicate_groups SET representative_job_id = :rep, unscorable_reason = :reason, "
    "election_reason = :election, elected_at = now(), updated_at = now() WHERE id = :group"
)
_MEMBERS: Final = q("SELECT job_id FROM duplicate_group_members WHERE group_id = :group ORDER BY job_id")
_LEAVE: Final = q(
    "DELETE FROM duplicate_group_members WHERE group_id = :group AND job_id = ANY(CAST(:jobs AS bigint[]))"
)
_UNGROUP: Final = q(
    "UPDATE jobs SET duplicate_group_id = NULL WHERE id = ANY(CAST(:jobs AS bigint[])) "
    "AND duplicate_group_id = :group"
)

log = get_logger("jobhunter.dedup.representative")


@dataclass(frozen=True)
class MemberView:
    """What the election order reads of a member."""

    job_id: int
    availability: str
    completeness: str
    source_rank: int
    description_length: int
    first_seen_at: datetime
    eligible: bool


def eligible(availability: str, merged: bool) -> bool:
    """§8.7's eligibility as it reads in M1: the job is unmerged and not `closed` (module docstring)."""
    return not merged and availability != "closed"


def _order(member: MemberView) -> tuple[int, int, int, int, datetime, int]:
    return (
        AVAILABILITY_ORDER.index(member.availability),
        0 if member.completeness == "full" else 1,
        member.source_rank,
        -member.description_length,
        member.first_seen_at,
        member.job_id,
    )


def elect(members: Sequence[MemberView]) -> MemberView | None:
    """The eligible member that sorts first by §8.7's six keys; `None` when none is eligible."""
    return min((m for m in members if m.eligible), key=_order, default=None)


def member_view(
    *,
    job_id: int,
    availability: str,
    completeness: str,
    source_rank: int,
    description_length: int,
    first_seen_at: datetime,
    merged: bool,
) -> MemberView:
    """The election's view of a job, its eligibility derived here (`eligible`): the one constructor, for a
    member read from the database (`member_views`) and for a job `groups` holds in memory."""
    return MemberView(
        job_id=job_id,
        availability=availability,
        completeness=completeness,
        source_rank=source_rank,
        description_length=description_length,
        first_seen_at=first_seen_at,
        eligible=eligible(availability, merged),
    )


def _member_view(row: Row[tuple[object, ...]]) -> MemberView:
    return member_view(
        job_id=int(row.id),
        availability=str(row.availability),
        completeness=str(row.completeness),
        source_rank=int(row.source_rank),
        description_length=int(row.description_length),
        first_seen_at=row.first_seen_at,
        merged=bool(row.merged),
    )


async def member_views(conn: AsyncConnection, group_id: int) -> list[MemberView]:
    """The `MemberView` of every member of the group, in job id order."""
    rows = await conn.execute(_MEMBER_VIEWS, {"group": group_id, "worst": WORST_RANK})
    return [_member_view(row) for row in rows]


async def member_ids(conn: AsyncConnection, group_id: int) -> tuple[int, ...]:
    """The job ids of the group's members, in id order."""
    return tuple(int(job) for job in (await conn.execute(_MEMBERS, {"group": group_id})).scalars())


async def leave(conn: AsyncConnection, group_id: int, job_ids: Sequence[int]) -> None:
    """The jobs leave the group: their membership rows and their `duplicate_group_id`. The caller holds
    their rows. A job that joined as the best of several groups stands in that choice no longer, so its
    `dedup.ambiguous_grouping` alert is resolved (ledger L330): every leave path comes here, a member that
    leaves or is split off, a dissolution and the from-job of a merge."""
    await conn.execute(_LEAVE, {"group": group_id, "jobs": list(job_ids)})
    await conn.execute(_UNGROUP, {"group": group_id, "jobs": list(job_ids)})
    for job_id in job_ids:
        await resolve_alert(conn, code=AMBIGUOUS_GROUPING, subject=f"job:{job_id}")


async def dissolve(conn: AsyncConnection, group_id: int, *, reason: str) -> tuple[int, ...]:
    """Dissolve the active group (module docstring); the members that left it with the dissolution."""
    remaining = await member_ids(conn, group_id)
    await leave(conn, group_id, remaining)
    await transition(
        conn,
        "duplicate_groups",
        group_id,
        ["active"],
        "dissolved",
        reason=reason,
        set_={"representative_job_id": None, "unscorable_reason": None},
    )
    log.info("duplicate_group_dissolved", group_id=group_id, reason=reason, job_ids=list(remaining))
    return remaining


async def set_representative(
    conn: AsyncConnection, group_id: int, job_id: int | None, *, reason: str
) -> None:
    """`job_id` represents the group (`None`: none does, `no_active_member`), elected for `reason`."""
    params = {
        "group": group_id,
        "rep": job_id,
        "reason": NO_ACTIVE_MEMBER if job_id is None else None,
        "election": reason,
    }
    await conn.execute(_ELECT, params)
    log.info("duplicate_group_elected", group_id=group_id, representative_job_id=job_id, reason=reason)


async def reelect(conn: AsyncConnection, group_id: int, *, reason: str) -> int | None:
    """Elect the group's representative for `reason` (module docstring) in the caller's transaction; the new
    representative's job id, or `None` when no member is eligible or the group is not (or no longer) active.
    A group with fewer than two members is dissolved instead."""
    group = (await conn.execute(_GROUP, {"group": group_id})).first()
    if group is None or group.status != "active":
        return None
    members = await member_views(conn, group_id)
    if len(members) < 2:
        await dissolve(conn, group_id, reason=reason)
        return None
    elected = elect(members)
    representative = None if elected is None else elected.job_id
    await set_representative(conn, group_id, representative, reason=reason)
    return representative
