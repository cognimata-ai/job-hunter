"""The representative of a duplicate group (spec §8.7, §7.17 #4): eligibility, the election order, elections
and dissolution.

**Eligibility** (§8.7 L3251; M3 Task 15 closes M1 Deviation 9): a member is eligible when its job is
unmerged and not `closed` and, while a tuple is active, its active-tuple target (the target of its current
revision for `active_config.llm_tuple_id`) is in `ELIGIBLE_TARGET_STATES`: `pending`, `budget_deferred`,
`in_progress`, `in_batch`, `deferred_duplicate` or published. A member without such a target, or whose
target failed, was refused, expired, was filtered out or cancelled, is not eligible; with no active tuple M1's
rule holds. §7.17 #4 is read with this list (A1 issue 27). `ELIGIBLE_SQL` is the same predicate over a `jobs`
row aliased `j` and the `active_config` row aliased `ac`, which every statement that selects by it joins; the
two change together (`eligible(availability, merged, target_state, has_tuple=…)`).

**`elect`** returns the eligible member that sorts first by the six keys of §8.7: availability (`active` >
`unknown` > `stale`), completeness (`full` > `partial`), the best source precedence rank (ATS 1, JSearch and
SerpApi 2, Arbeitnow 3, Adzuna 4), description length descending, the earliest `first_seen_at`, the lowest
job id; `None` when no member is eligible.

**`reelect`** runs an election in the caller's transaction, which holds the row of every member (the job rows
first, §3.3.3): the group row is locked `FOR UPDATE`; a group with fewer than two members is dissolved
instead (`dissolve`); otherwise `representative_job_id`, `election_reason`, `elected_at` and `updated_at`
are set, and `unscorable_reason` from the result: `NULL` with a representative, else `unscorable_reason`
of the members (L3259): `all_failed` when every member's target failed (`failed_*`), was refused or expired
(`expired_unscored`), `all_filtered` when every one is `filtered_out`, `no_active_member` otherwise. An
election never keeps an earlier reason it did not find again. Representative changes are not state
transitions (§7.16.8); the group stays `active`. The revalidation of the members against a new
representative, and the deferred members' targets after it, are `groups`' (§8.7).

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
ALL_FAILED: Final = "all_failed"
ALL_FILTERED: Final = "all_filtered"
# An info alert, subject `job:<id>` (Ruling T20-b): raised by `groups` when a job joins the best of several
# groups, resolved here when it leaves.
AMBIGUOUS_GROUPING: Final = "dedup.ambiguous_grouping"
# §8.7's order of the eligible availabilities; `closed` is never eligible.
AVAILABILITY_ORDER: Final = ("active", "unknown", "stale")
WORST_RANK: Final = 99  # the precedence rank of a job without a known source
# The active-tuple target states of an eligible member (§8.7 L3251, the published ones included).
ELIGIBLE_TARGET_STATES: Final = (
    "pending",
    "budget_deferred",
    "in_progress",
    "in_batch",
    "deferred_duplicate",
    "published_accepted",
    "published_contested",
)
# The target states of `all_failed` (L3259: every target failed, refused or expired), besides `failed_*`.
_FAILED_STATES: Final = frozenset({"refused", "expired_unscored"})
# `eligible` over `jobs j` and `active_config ac` (module docstring; the plan's text, verbatim).
ELIGIBLE_SQL: Final = (
    "(j.merged_into_job_id IS NULL AND j.availability <> 'closed' AND (ac.llm_tuple_id IS NULL OR EXISTS "
    "(SELECT 1 FROM score_targets t WHERE t.job_revision_id = j.current_revision_id AND t.llm_tuple_id = "
    "ac.llm_tuple_id AND t.state IN ('pending', 'budget_deferred', 'in_progress', 'in_batch', "
    "'deferred_duplicate', 'published_accepted', 'published_contested'))))"
)
# A job's active-tuple target state (`target_state`, NULL without one) and whether a tuple is active, over
# `jobs j` and `active_config ac`: what `eligible` reads beside the job's own columns.
TARGET_STATE_SQL: Final = (
    "(SELECT t.state FROM score_targets t WHERE t.job_revision_id = j.current_revision_id "
    "AND t.llm_tuple_id = ac.llm_tuple_id) AS target_state, ac.llm_tuple_id IS NOT NULL AS has_tuple"
)

_GROUP: Final = q("SELECT status, representative_job_id FROM duplicate_groups WHERE id = :group FOR UPDATE")
_MEMBER_VIEWS: Final = q(
    "SELECT j.id, j.availability, j.completeness, j.first_seen_at, "  # noqa: S608 (the module's constants)
    "j.merged_into_job_id IS NOT NULL AS merged, "
    "coalesce((SELECT min(s.precedence_rank) FROM sources s WHERE s.id = ANY(j.source_ids)), :worst) "
    f"AS source_rank, length(coalesce(r.description_text, '')) AS description_length, {TARGET_STATE_SQL} "
    "FROM duplicate_group_members m JOIN jobs j ON j.id = m.job_id JOIN active_config ac ON ac.id = 1 "
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
    target_state: str | None = None  # its active-tuple target's state (`None`: none, or no active tuple)


def eligible(availability: str, merged: bool, target_state: str | None, *, has_tuple: bool) -> bool:
    """§8.7's eligibility (module docstring): the job is unmerged and not `closed`, and, while a tuple is
    active (`has_tuple`), its active-tuple target is in `ELIGIBLE_TARGET_STATES`."""
    if merged or availability == "closed":
        return False
    return not has_tuple or target_state in ELIGIBLE_TARGET_STATES


def _failed(state: str | None) -> bool:
    return state is not None and (state.startswith("failed_") or state in _FAILED_STATES)


def unscorable_reason(members: Sequence[MemberView]) -> str:
    """Why a group without an eligible member is not scorable (L3259; module docstring): `all_failed`,
    `all_filtered`, else `no_active_member`."""
    states = [member.target_state for member in members]
    if states and all(_failed(state) for state in states):
        return ALL_FAILED
    if states and all(state == "filtered_out" for state in states):
        return ALL_FILTERED
    return NO_ACTIVE_MEMBER


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
    target_state: str | None,
    has_tuple: bool,
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
        eligible=eligible(availability, merged, target_state, has_tuple=has_tuple),
        target_state=target_state,
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
        target_state=None if row.target_state is None else str(row.target_state),
        has_tuple=bool(row.has_tuple),
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
    conn: AsyncConnection,
    group_id: int,
    job_id: int | None,
    *,
    reason: str,
    unscorable: str = NO_ACTIVE_MEMBER,
) -> None:
    """`job_id` represents the group (`None`: none does, and the group records `unscorable`), elected for
    `reason`."""
    params = {
        "group": group_id,
        "rep": job_id,
        "reason": unscorable if job_id is None else None,
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
    await set_representative(
        conn, group_id, representative, reason=reason, unscorable=unscorable_reason(members)
    )
    return representative
