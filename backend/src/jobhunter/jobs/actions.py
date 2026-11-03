"""The user's actions on a job (spec §14.2 L4991, §7.13, §7.1 L1255; plan M4 Task 7, B11, Ruling T22-a).

`put_actions(conn, job_id, req, *, now)` writes the user's actions on a job in the caller's transaction and
returns the job's `user_job_actions` row after the write, with those of the group members it wrote.

**The values** (B11). For each of `seen`, `saved`, `applied` and `hidden`, `true` sets its timestamp to
`coalesce(existing, now)` (a timestamp already set is kept), `false` clears it and `None` (absent or null)
leaves it. `note` replaces the note, `""` clears it to NULL, `None` leaves it. `seen_on_open` (the drawer
sends it when it opens) sets `seen_at` as `seen: true` does. `ActionsIn` refuses, with a 422 at the model's
edge, `seen_on_open` together with `seen`, and a body that gives none of the five and no `seen_on_open`
(`apply_to_group` alone included). A note holds at most `API_NOTE_LIMIT` (2,000) code points and no NUL or
lone surrogate (pydantic's `string_unicode` refuses the surrogate, `api.models.clean_text` the NUL);
`dedup.user_actions.NOTE_LIMIT` (20,000, the column's CHECK) bounds a merge's concatenation only.

**The group.** With `apply_to_group` and one of `saved`, `applied` or `hidden` given, those three (never
`seen`, never the note) are written on every other member of the job's active duplicate group too (the
`duplicate_group_members` of its group whose status is `active`); `members` is their rows after the write, in
job id order. Without an active group, or without one of the three, only the job is written and `members`
is empty.

**The writes.** Each row is written by one upsert (`INSERT … ON CONFLICT (job_id) DO UPDATE`), so the first
write creates the row even when it sets nothing (a `false`, or a `seen_on_open` on a row that does not exist
yet), and every write sets `updated_at = now` on the job and on every member it writes: the caller's clock
(Ruling T22-a), not the database's. The merge's statements (`dedup.user_actions`) stay as they are.

**The locks** (§3.3.3). First the job rows `FOR SHARE`, in id order, in one statement: the job and, for a
group write, the members its group had when the statement started. Under that lock and before any write,
the job is checked as the drawer checks it (`jobs.detail.ensure_job`: unknown, synthetic, fenced, without a
current revision or merged raises `JobGone`, a merged job with its survivor). A merge holds both its jobs
`FOR UPDATE`, so a PUT that meets one waits for it and, once it has committed, answers the merged job's 404
having written nothing. The group is then read again: a member is written only while it is still a member,
unmerged and held. A member that left in a race is skipped; one that joined meanwhile is not held and not
written. Then the `user_job_actions` rows, one upsert each in job id order, so two writes of overlapping
groups take them in the same order. A merge writes only the rows of the jobs it holds, so it never meets a
PUT at these rows. Last comes `ranking.bump`, for every write but one whose only action is `seen_on_open`:
opening a job does not bump (L1255), while any other write bumps once, whether or not a value changed.

The note is the user's own text: it is never logged, and `ActionsIn` and `UserActionsOut` keep it out of
their repr. The line `job_actions_written` names the job, the members written, the fields given and whether
the ranking was bumped. Every statement here is static SQL with its tag, `/* jobs_actions.<kind> */`.
"""

from collections.abc import Collection, Mapping
from datetime import datetime
from typing import Annotated, Any, Final, Self

from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.api.models import clean_text
from jobhunter.db import ranking
from jobhunter.db.sql import q
from jobhunter.dedup.representative import member_ids
from jobhunter.dedup.user_actions import ACTION_TIMESTAMPS
from jobhunter.jobs.detail import UserActionsOut, ensure_job
from jobhunter.observability.logging import get_logger

API_NOTE_LIMIT: Final = 2_000  # code points: one API edit (L2636); a merge may concatenate up to NOTE_LIMIT
# The fields that write a member too with `apply_to_group`, and every field that is an action (L1255).
GROUP_FIELDS: Final = ("saved", "applied", "hidden")
ACTION_FIELDS: Final = ("seen", *GROUP_FIELDS, "note")

log = get_logger("jobhunter.jobs.actions")

_ACTIVE_GROUP: Final = q(
    "/* jobs_actions.group */ SELECT m.group_id FROM duplicate_group_members m "
    "JOIN duplicate_groups g ON g.id = m.group_id WHERE m.job_id = :job AND g.status = 'active'"
)
_LOCK: Final = q(
    "/* jobs_actions.lock */ SELECT id, merged_into_job_id FROM jobs WHERE id = ANY(CAST(:ids AS bigint[])) "
    "ORDER BY id FOR SHARE"
)
# A flag `true` sets its timestamp unless it holds one, `false` clears it, NULL leaves it; the note is set
# (NULL clears it) only with `set_note`. A new row takes the flags' values alone.
_PUT: Final = q(
    "/* jobs_actions.put */ INSERT INTO user_job_actions AS u "
    "(job_id, seen_at, saved_at, applied_at, hidden_at, note, updated_at) VALUES (:job, "
    "CASE WHEN CAST(:seen AS boolean) THEN CAST(:now AS timestamptz) END, "
    "CASE WHEN CAST(:saved AS boolean) THEN CAST(:now AS timestamptz) END, "
    "CASE WHEN CAST(:applied AS boolean) THEN CAST(:now AS timestamptz) END, "
    "CASE WHEN CAST(:hidden AS boolean) THEN CAST(:now AS timestamptz) END, "
    "CAST(:note AS text), CAST(:now AS timestamptz)) ON CONFLICT (job_id) DO UPDATE SET "
    "seen_at = CASE CAST(:seen AS boolean) WHEN true THEN coalesce(u.seen_at, EXCLUDED.seen_at) "
    "WHEN false THEN NULL ELSE u.seen_at END, "
    "saved_at = CASE CAST(:saved AS boolean) WHEN true THEN coalesce(u.saved_at, EXCLUDED.saved_at) "
    "WHEN false THEN NULL ELSE u.saved_at END, "
    "applied_at = CASE CAST(:applied AS boolean) WHEN true THEN coalesce(u.applied_at, EXCLUDED.applied_at) "
    "WHEN false THEN NULL ELSE u.applied_at END, "
    "hidden_at = CASE CAST(:hidden AS boolean) WHEN true THEN coalesce(u.hidden_at, EXCLUDED.hidden_at) "
    "WHEN false THEN NULL ELSE u.hidden_at END, "
    "note = CASE WHEN CAST(:set_note AS boolean) THEN EXCLUDED.note ELSE u.note END, "
    "updated_at = EXCLUDED.updated_at "
    "RETURNING u.job_id, u.seen_at, u.saved_at, u.applied_at, u.hidden_at, u.note, u.updated_at"
)

Note = Annotated[str, StringConstraints(max_length=API_NOTE_LIMIT), AfterValidator(clean_text)]


class ActionsIn(BaseModel, extra="forbid"):
    """The user's actions to write on a job.

    For each of `seen`, `saved`, `applied` and `hidden`, `true` marks it at the time of the request unless it
    is marked already (the earlier time is kept), `false` clears it, and an absent or null one is left as it
    is. `note` replaces the note: at most 2,000 characters, `""` clears it. `seen_on_open`, which the drawer
    sends when it opens, marks the job seen as `seen: true` does without moving the ranking generation; it
    cannot be combined with `seen`. With `apply_to_group`, `saved`, `applied` and `hidden` are written on the
    other members of the job's active duplicate group too. A body that gives none of `seen`, `saved`,
    `applied`, `hidden`, `note` and `seen_on_open` is refused.
    """

    seen: bool | None = None
    saved: bool | None = None
    applied: bool | None = None
    hidden: bool | None = None
    note: Note | None = Field(default=None, repr=False)  # the user's own text: never in a repr
    apply_to_group: bool = False
    seen_on_open: bool = False

    @model_validator(mode="after")
    def _an_action(self) -> Self:
        if self.seen_on_open and self.seen is not None:
            raise ValueError("seen_on_open cannot be combined with seen")
        if not self.seen_on_open and not self.given:
            raise ValueError("give one of seen, saved, applied, hidden, note or seen_on_open")
        return self

    @property
    def given(self) -> tuple[str, ...]:
        """The action fields the body gives (not null), in `ACTION_FIELDS` order."""
        return tuple(name for name in ACTION_FIELDS if getattr(self, name) is not None)


class ActionsOut(BaseModel):
    """The job's actions after the write and, for a write with `apply_to_group`, those of each other member of
    its active duplicate group it wrote, by job id (otherwise none)."""

    job: UserActionsOut
    members: list[UserActionsOut]


def _out(row: Any) -> UserActionsOut:
    return UserActionsOut(
        job_id=row.job_id,
        **{name: getattr(row, name) for name in ACTION_TIMESTAMPS},
        note=row.note,
        updated_at=row.updated_at,
    )


async def _members(conn: AsyncConnection, job_id: int) -> tuple[int, ...]:
    """The other members of the job's active duplicate group, by job id; none without one."""
    group = (await conn.execute(_ACTIVE_GROUP, {"job": job_id})).scalar_one_or_none()
    if group is None:
        return ()
    return tuple(member for member in await member_ids(conn, int(group)) if member != job_id)


async def _lock(conn: AsyncConnection, job_ids: Collection[int]) -> dict[int, int | None]:
    """The job rows `FOR SHARE` in id order, in one statement; their `merged_into_job_id` by id (a job that
    does not exist is absent)."""
    rows = await conn.execute(_LOCK, {"ids": sorted(set(job_ids))})
    return {int(r.id): None if r.merged_into_job_id is None else int(r.merged_into_job_id) for r in rows}


def _params(req: ActionsIn, job_id: int, *, own: bool, now: datetime) -> Mapping[str, object]:
    """The upsert's parameters of the job (`own`) or of a member (its group fields alone)."""
    return {
        "job": job_id,
        "seen": (True if req.seen_on_open else req.seen) if own else None,
        **{name: getattr(req, name) for name in GROUP_FIELDS},
        "set_note": own and req.note is not None,
        "note": (req.note or None) if own else None,
        "now": now,
    }


async def put_actions(conn: AsyncConnection, job_id: int, req: ActionsIn, *, now: datetime) -> ActionsOut:
    """Write `req` on job `job_id`, and on the other members of its active duplicate group with
    `apply_to_group`, in the caller's transaction (module docstring); the rows after the write. Raises
    `JobGone` for a job without a detail, a merged one with its survivor, having written nothing."""
    group_wide = req.apply_to_group and any(getattr(req, name) is not None for name in GROUP_FIELDS)
    held = await _lock(conn, (job_id, *(await _members(conn, job_id) if group_wide else ())))
    await ensure_job(conn, job_id)
    members = [m for m in await _members(conn, job_id) if m in held and held[m] is None] if group_wide else []
    written: dict[int, UserActionsOut] = {}
    for target in sorted((job_id, *members)):
        params = _params(req, target, own=target == job_id, now=now)
        written[target] = _out((await conn.execute(_PUT, params)).one())
    bumped = bool(req.given)
    if bumped:
        await ranking.bump(conn)
    log.info(
        "job_actions_written",
        job_id=job_id,
        member_ids=members,
        fields=[*req.given, *(["seen_on_open"] if req.seen_on_open else [])],
        bumped=bumped,
    )
    return ActionsOut(job=written[job_id], members=[written[m] for m in members])
