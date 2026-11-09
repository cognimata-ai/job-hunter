"""A duplicate group as the drawer and the group routes show it (spec §15.5, §8.7; plan M4 Tasks 6 and 8,
B12).

`group_view(conn, group_id)` reads, in one statement in the caller's snapshot, the group (its status, its
representative, why it is unscorable and why its representative was elected) and its members: the unmerged
jobs whose `duplicate_group_id` is the group (the membership the cards count, `jobs.cards`), by job id, each
with its title, company, sources, location labels, completeness, availability, the state of its current
revision's target of the active tuple (`None` without one, or without an active tuple), its membership's
`score_individually` flag (§8.7: the user's intent; `false` without a membership row) and whether it is the
representative. A group id that names no group raises `GroupMissing` (a `LookupError`: the api's
`domain_problem` answers it 404 `not_found`).

The statement is static SQL with the tag `/* jobs_groups.view */`; it reads the active tuple from
`active_config` itself.
"""

from typing import Any, Final

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q

_VIEW: Final = q(
    "/* jobs_groups.view */ SELECT g.id, g.status, g.representative_job_id, g.unscorable_reason, "
    "g.election_reason, m.id AS job_id, m.title, m.company_display, m.source_ids, m.locations, "
    "m.completeness, m.availability, tm.state AS target_state, "
    "coalesce(gm.score_individually, false) AS score_individually "
    "FROM duplicate_groups g "
    "LEFT JOIN jobs m ON m.duplicate_group_id = g.id AND m.merged_into_job_id IS NULL "
    "LEFT JOIN score_targets tm ON tm.job_revision_id = m.current_revision_id "
    "AND tm.llm_tuple_id = (SELECT a.llm_tuple_id FROM active_config a WHERE a.id = 1) "
    "LEFT JOIN duplicate_group_members gm ON gm.group_id = g.id AND gm.job_id = m.id "
    "WHERE g.id = :group ORDER BY m.id"
)


class GroupMissing(LookupError):
    """No duplicate group has the id (its message names the id only)."""

    def __init__(self, group_id: int) -> None:
        super().__init__(f"duplicate_groups {group_id}: no such group")
        self.group_id = group_id


class GroupMemberOut(BaseModel):
    job_id: int
    title: str
    company: str
    source_ids: list[str]
    locations: list[str]
    completeness: str
    availability: str
    target_state: str | None
    score_individually: bool
    is_representative: bool


class GroupOut(BaseModel):
    """A duplicate group and its members, by job id."""

    id: int
    status: str
    representative_job_id: int | None
    unscorable_reason: str | None
    election_reason: str | None
    members: list[GroupMemberOut]


def location_labels(locations: object) -> list[str]:
    """The labels of a job's stored locations (`jobs.locations`), an entry without one left out."""
    if not isinstance(locations, list):
        return []
    return [str(x["label"]) for x in locations if isinstance(x, dict) and isinstance(x.get("label"), str)]


def _member(row: Any) -> GroupMemberOut:
    return GroupMemberOut(
        job_id=row.job_id,
        title=row.title,
        company=row.company_display,
        source_ids=list(row.source_ids),
        locations=location_labels(row.locations),
        completeness=row.completeness,
        availability=row.availability,
        target_state=row.target_state,
        score_individually=bool(row.score_individually),
        is_representative=row.job_id == row.representative_job_id,
    )


async def group_view(conn: AsyncConnection, group_id: int) -> GroupOut:
    """The group `group_id` with its members (module docstring); `GroupMissing` for no such group."""
    rows = list(await conn.execute(_VIEW, {"group": group_id}))
    if not rows:
        raise GroupMissing(group_id)
    group = rows[0]
    return GroupOut(
        id=group.id,
        status=group.status,
        representative_job_id=group.representative_job_id,
        unscorable_reason=group.unscorable_reason,
        election_reason=group.election_reason,
        members=[_member(row) for row in rows if row.job_id is not None],
    )
