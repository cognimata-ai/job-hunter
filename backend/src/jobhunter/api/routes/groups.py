"""Duplicate groups and the merge family (spec §14.2 L4997-5001, L5043, §8.6.2, §8.7, §7.1 L1255; plan M4
Task 8, B12, Ruling T22-a, M1 Carry-forward 10).

None of these routes takes an `Idempotency-Key` (§14.1 keys none of them). Each runs its write in
`api.retry.with_deadlock_retry`: one transaction per try, a deadlock or a serialization failure tried again
in a fresh one, three tries in all, the last failure 409 `conflict_state` with `retryable: true`. The domain
function's own lock order holds in it (`active_config` first where it takes it, `ranking_state` last), and
each write bumps the ranking generation once, as its last statement: `split_member`, `confirm_group`,
`unmerge` and `resolve_conflict(merge)` themselves, `score_individually` too (the flag is a change of the
group's membership); `resolve_conflict(separate)` changes nothing the list shows and bumps nothing.

- `POST /api/duplicate-groups/{group_id}/split {job_id}` → 200 `GroupOut`: `groups.split_member`, then the
  group read by `jobs.groups.group_view` in a fresh read snapshot after the write's commit, so the bump stays
  the write's last statement. The answer is committed state and may include a later concurrent change; the
  group a split dissolved is answered too (`status = "dissolved"`, no member).
- `POST /api/duplicate-groups/{group_id}/score-individually {job_id}` → 200 `GroupOut`, read the same way:
  `targets.score_individually` at the request's time; a `ScoreNowRefused` is the problem score-now answers
  for it (`api.routes.targets.refused_problem`, the one mapping).
- `POST /api/duplicate-groups/{group_id}/confirm` → 202 `{job_id}`: `groups.confirm_group`, the
  representative every other member was merged into.
- `POST /api/jobs/{job_id}/unmerge {merge_event_id}` → 202 `{unmerge_event_id}`: first the route's own check,
  in the write transaction (a plain read, before `merge.unmerge`'s first lock): no event of that id, or one
  whose `into_job_id` is not `{job_id}`, is 404 `not_found`, without `merged_into_job_id` (the event is what
  is missing, not the job); every other event goes to `merge.unmerge`, whose refusals (an unmerge event, one
  reversed already, one no longer the latest state of its jobs) are its `InvariantViolation`, 409.
- `POST /api/identity-conflicts/{conflict_id}/resolve {decision}` → 202 `{conflict_id, status}`:
  `merge.resolve_conflict`, whose return is the conflict's status after the resolution, read under its row
  lock before the bump.

The errors: `merge.CoMemberChanged` (a read-then-lock round whose job set changed under its lock, having
written nothing) is the retryable 409 (`api.retry.retryable_conflict`), checked before
`api.errors.domain_problem`, which answers the others: a `LookupError` (an unknown group, conflict or member)
404 `not_found`, an `InvariantViolation` or `TransitionConflict` 409 `conflict_state`, a `FencedOut` (a
member under a purge fence) 409 `conflict_state` with `detail = "purge_in_progress"`. Their messages, which
name ids, stay out of the body. `utcnow` is a name of this module, so a test injects the clock by patching
`jobhunter.api.routes.groups.utcnow`.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.api.deps import get_engine, require_session
from jobhunter.api.errors import Problem, domain_problem
from jobhunter.api.openapi import BODY_LIMIT_RESPONSES, CSRF_RESPONSES, SESSION_RESPONSES
from jobhunter.api.retry import retryable_conflict, with_deadlock_retry
from jobhunter.api.routes.targets import refused_problem
from jobhunter.clock import utcnow
from jobhunter.db.snapshot import read_snapshot
from jobhunter.db.sql import q
from jobhunter.dedup import groups, merge
from jobhunter.dedup.merge import CoMemberChanged, ResolvedStatus
from jobhunter.errors import DomainError
from jobhunter.jobs.groups import GroupOut, group_view
from jobhunter.scoring import targets
from jobhunter.scoring.targets import ScoreNowRefused

router = APIRouter(dependencies=[Depends(require_session)], responses=SESSION_RESPONSES)

_BIGINT_MAX: Final = 2**63 - 1
_EVENT_INTO: Final = q("SELECT into_job_id FROM merge_events WHERE id = :id")

_RETRYABLE: Final = "a concurrent change got in the way (`conflict_state`, with `retryable: true`)"
_RETRYABLE_ONLY: Final = "A concurrent change got in the way (`conflict_state`, with `retryable: true`)"
_INVALID: Final = "Invalid path or body (`validation_failed`)"

GroupId = Annotated[int, Path(ge=1, le=_BIGINT_MAX, description="The id of a duplicate group")]
JobId = Annotated[int, Path(ge=1, le=_BIGINT_MAX, description="The id of a job")]
ConflictId = Annotated[int, Path(ge=1, le=_BIGINT_MAX, description="The id of an identity conflict")]


class GroupMemberIn(BaseModel):
    """The member of the duplicate group the move is about."""

    model_config = ConfigDict(extra="forbid")

    job_id: int = Field(ge=1, le=_BIGINT_MAX)


class ConfirmedOut(BaseModel):
    """The job the group's other members were merged into: the group's representative."""

    job_id: int


class UnmergeIn(BaseModel):
    """The merge to reverse: an event that merged another job into this one."""

    model_config = ConfigDict(extra="forbid")

    merge_event_id: int = Field(ge=1, le=_BIGINT_MAX)


class UnmergedOut(BaseModel):
    """The unmerge event that reversed the merge."""

    unmerge_event_id: int


class ResolveIn(BaseModel):
    """How the identity conflict is settled: `separate` keeps the posting's job and the other jobs apart,
    `merge` merges the other jobs into the posting's job."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["separate", "merge"]


class ConflictResolvedOut(BaseModel):
    """The conflict and its status after the resolution."""

    conflict_id: int
    status: ResolvedStatus


def _problem(exc: DomainError | LookupError) -> Problem | None:
    """The problem of a domain error (module docstring): a changed job set first, then `domain_problem`."""
    if isinstance(exc, CoMemberChanged):
        return retryable_conflict()
    return domain_problem(exc)


async def _write[T](engine: AsyncEngine, work: Callable[[AsyncConnection], Awaitable[T]]) -> T:
    """`work` through `with_deadlock_retry`, its domain errors answered as problems."""
    try:
        return await with_deadlock_retry(engine, work)
    except (DomainError, LookupError) as exc:
        if (problem := _problem(exc)) is None:
            raise
        raise problem from None


async def _group(engine: AsyncEngine, group_id: int) -> GroupOut:
    """The committed group, read in a fresh snapshot after the write (module docstring)."""
    try:
        async with read_snapshot(engine) as conn:
            return await group_view(conn, group_id)
    except LookupError as exc:  # gone since the write: a purge's deletion
        if (problem := domain_problem(exc)) is None:
            raise
        raise problem from None


_GROUP_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    404: {"description": "No such duplicate group (`not_found`)"},
}


@router.post(
    "/api/duplicate-groups/{group_id}/split",
    response_model=GroupOut,
    responses={
        **_GROUP_RESPONSES,
        409: {
            "description": "The group is not active or the job is not its member (`conflict_state`), or "
            + _RETRYABLE
        },
        422: {"description": _INVALID},
        **CSRF_RESPONSES,
        **BODY_LIMIT_RESPONSES,
    },
)
async def split_member(
    group_id: GroupId, body: GroupMemberIn, engine: Annotated[AsyncEngine, Depends(get_engine)]
) -> GroupOut:
    """Split the job out of its duplicate group for good: it is never grouped with the other members again,
    and its score is its own. A group left with one member is dissolved. Answers the group after the
    split."""

    async def work(conn: AsyncConnection) -> None:
        await groups.split_member(conn, group_id, body.job_id)

    await _write(engine, work)
    return await _group(engine, group_id)


@router.post(
    "/api/duplicate-groups/{group_id}/score-individually",
    response_model=GroupOut,
    responses={
        404: {"description": "The job is not a member of the group (`not_found`)"},
        409: {"description": _RETRYABLE_ONLY},
        422: {"description": _INVALID},
        **CSRF_RESPONSES,
        **BODY_LIMIT_RESPONSES,
    },
)
async def score_individually(
    group_id: GroupId, body: GroupMemberIn, engine: Annotated[AsyncEngine, Depends(get_engine)]
) -> GroupOut:
    """Score the member on its own: it stays in the group, but its score is no longer the representative's.
    A member waiting for the representative's score is scored in the normal order. Answers the group."""
    now = utcnow()

    async def work(conn: AsyncConnection) -> None:
        moved = await targets.score_individually(conn, group_id=group_id, job_id=body.job_id, now=now)
        if isinstance(moved, ScoreNowRefused):
            raise refused_problem(moved, "Group member")

    await _write(engine, work)
    return await _group(engine, group_id)


@router.post(
    "/api/duplicate-groups/{group_id}/confirm",
    status_code=202,
    response_model=ConfirmedOut,
    responses={
        **_GROUP_RESPONSES,
        409: {
            "description": "The group is not active or has no representative (`conflict_state`), a member is "
            "being purged (`conflict_state`, `purge_in_progress`), or " + _RETRYABLE
        },
        422: {"description": "Invalid path (`validation_failed`)"},
        **CSRF_RESPONSES,
    },
)
async def confirm_group(
    group_id: GroupId, engine: Annotated[AsyncEngine, Depends(get_engine)]
) -> ConfirmedOut:
    """Confirm that the group's members are one job: every other member is merged into the representative
    (202)."""

    async def work(conn: AsyncConnection) -> int:
        return await groups.confirm_group(conn, group_id)

    return ConfirmedOut(job_id=await _write(engine, work))


@router.post(
    "/api/jobs/{job_id}/unmerge",
    status_code=202,
    response_model=UnmergedOut,
    responses={
        404: {"description": "No merge event of that id merged a job into this one (`not_found`)"},
        409: {
            "description": "The event is an unmerge, is reversed already or is no longer the latest state of "
            "its jobs (`conflict_state`), a job is being purged (`conflict_state`, `purge_in_progress`), or "
            + _RETRYABLE
        },
        422: {"description": _INVALID},
        **CSRF_RESPONSES,
        **BODY_LIMIT_RESPONSES,
    },
)
async def unmerge_job(
    job_id: JobId, body: UnmergeIn, engine: Annotated[AsyncEngine, Depends(get_engine)]
) -> UnmergedOut:
    """Reverse the merge of another job into this one: its postings, keys and user actions go back to it, and
    the two jobs are never merged again by a shared key (202)."""

    async def work(conn: AsyncConnection) -> int:
        into = (await conn.execute(_EVENT_INTO, {"id": body.merge_event_id})).scalar()
        if into is None or int(into) != job_id:
            raise Problem(404, "not_found", "Merge event not found")
        return await merge.unmerge(conn, body.merge_event_id)

    return UnmergedOut(unmerge_event_id=await _write(engine, work))


@router.post(
    "/api/identity-conflicts/{conflict_id}/resolve",
    status_code=202,
    response_model=ConflictResolvedOut,
    responses={
        404: {"description": "No such identity conflict (`not_found`)"},
        409: {
            "description": "The conflict is not open (`conflict_state`), a job is being purged "
            "(`conflict_state`, `purge_in_progress`), or " + _RETRYABLE
        },
        422: {"description": _INVALID},
        **CSRF_RESPONSES,
        **BODY_LIMIT_RESPONSES,
    },
)
async def resolve_conflict(
    conflict_id: ConflictId, body: ResolveIn, engine: Annotated[AsyncEngine, Depends(get_engine)]
) -> ConflictResolvedOut:
    """Settle an open identity conflict: keep its jobs apart, or merge them into the posting's job (202)."""

    async def work(conn: AsyncConnection) -> ResolvedStatus:
        return await merge.resolve_conflict(conn, conflict_id, body.decision)

    return ConflictResolvedOut(conflict_id=conflict_id, status=await _write(engine, work))
