"""A job's score history (spec §14.2 L4990, §15.4 "History", §11.11 L4591; plan M4 Task 6, B25).

`history_page(conn, job_id, after=…, limit=…)` reads, in the caller's snapshot, one page of the results of
the job's revisions (every `llm_results` row whose revision is one of the job's, the active tuple's current
result included), newest first by `(published_at, id)`, each with its profile version's number, its rubric's
version, its tuple (its target's) and its status, and **every instance** of it: each `det_results` row of the
result, current or superseded, under any scope, newest first by `(computed_at, id)`. `after` is the
`(published_at, result_id)` of the last row of the page before (`None`: the first page); the second value it
returns is that position of its own last row when more rows follow, else `None`.

It signs nothing: the route turns the position into `HistoryPageOut.next_cursor` with
`api.pagination.encode_list_cursor` under `JOB_HISTORY_SORT` and the job's id as the filter, so a cursor
pages only the history of the job it was made for.

Two statements, each static SQL with its tag (`/* jobs_history.page */`, `/* jobs_history.instances */`):
the page (`limit + 1` rows, to know whether more follow) and the instances of its results.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.api.models import DecimalOut, UtcDatetime
from jobhunter.db.sql import q

Position = tuple[datetime, int]

_PAGE_HEAD: Final = (
    "/* jobs_history.page */ SELECT r.id, r.published_at, pv.version_no, rv.version AS rubric_version, "
    "t.llm_tuple_id, r.status FROM job_revisions jr "
    "JOIN llm_results r ON r.job_revision_id = jr.id "
    "JOIN score_targets t ON t.id = r.target_id "
    "JOIN profile_versions pv ON pv.id = r.profile_version_id "
    "JOIN rubric_versions rv ON rv.id = r.rubric_version_id "
    "WHERE jr.job_id = :job"
)
_PAGE_ORDER: Final = " ORDER BY r.published_at DESC, r.id DESC LIMIT :limit"
_FIRST_PAGE: Final = q(_PAGE_HEAD + _PAGE_ORDER)
_NEXT_PAGE: Final = q(
    _PAGE_HEAD
    + " AND ROW(r.published_at, r.id) < ROW(CAST(:after_at AS timestamptz), CAST(:after_id AS bigint))"
    + _PAGE_ORDER
)
_INSTANCES: Final = q(
    "/* jobs_history.instances */ SELECT d.llm_result_id, d.id, d.scope_version_id, d.is_current, "
    "d.score_mid, d.score_low, d.score_high, d.coverage, d.confidence, d.computed_at FROM det_results d "
    "WHERE d.llm_result_id = ANY(CAST(:ids AS bigint[])) ORDER BY d.computed_at DESC, d.id DESC"
)


class InstanceOut(BaseModel):
    """One score instance of a result (`None` scores: a verified not-a-job-ad)."""

    det_result_id: int
    scope_version_id: int
    is_current: bool
    mid: DecimalOut | None
    low: DecimalOut | None
    high: DecimalOut | None
    coverage: DecimalOut | None
    confidence: Literal["low", "medium", "high"] | None
    computed_at: UtcDatetime


# Its fields: the module docstring.
class HistoryItemOut(BaseModel):
    """One result of the job's history with every instance of it."""

    result_id: int
    published_at: UtcDatetime
    profile_version_no: int
    rubric_version: str
    llm_tuple_id: int
    status: Literal["accepted", "contested"]
    instances: list[InstanceOut]


class HistoryPageOut(BaseModel):
    """A page of the history and the cursor of the next (`None` on the last page)."""

    items: list[HistoryItemOut]
    next_cursor: str | None


async def history_page(
    conn: AsyncConnection, job_id: int, *, after: Position | None, limit: int
) -> tuple[list[HistoryItemOut], Position | None]:
    """One page of the job's history and the position after it (module docstring)."""
    if limit < 1:
        raise ValueError("a history page holds at least one result")
    params: dict[str, object] = {"job": job_id, "limit": limit + 1}
    if after is None:
        rows = list(await conn.execute(_FIRST_PAGE, params))
    else:
        rows = list(await conn.execute(_NEXT_PAGE, {**params, "after_at": after[0], "after_id": after[1]}))
    more = len(rows) > limit
    rows = rows[:limit]
    instances = await _instances(conn, [int(row.id) for row in rows])
    items = [
        HistoryItemOut(
            result_id=row.id,
            published_at=row.published_at,
            profile_version_no=row.version_no,
            rubric_version=row.rubric_version,
            llm_tuple_id=row.llm_tuple_id,
            status=row.status,
            instances=instances.get(int(row.id), []),
        )
        for row in rows
    ]
    last = rows[-1] if more else None
    return items, None if last is None else (last.published_at, int(last.id))


async def _instances(conn: AsyncConnection, results: Sequence[int]) -> dict[int, list[InstanceOut]]:
    found: dict[int, list[InstanceOut]] = {}
    if not results:
        return found
    for row in await conn.execute(_INSTANCES, {"ids": sorted(results)}):
        found.setdefault(int(row.llm_result_id), []).append(
            InstanceOut(
                det_result_id=row.id,
                scope_version_id=row.scope_version_id,
                is_current=row.is_current,
                mid=row.score_mid,
                low=row.score_low,
                high=row.score_high,
                coverage=row.coverage,
                confidence=row.confidence,
                computed_at=row.computed_at,
            )
        )
    return found
