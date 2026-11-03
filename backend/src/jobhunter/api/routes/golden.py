"""Feedback and the golden set (spec §11.13 L4610-4632, §14.2 L4977-4979, §14.3; plan Task 21, R2,
Deviation 21, Carry T19-d): thin routes over `scoring.golden`, each one transaction, none with an
`Idempotency-Key` (§14.1 L4963 lists none of them).

- `POST /api/jobs/{job_id}/feedback` (`{kind: label | wrong_score, label: good | ok | poor, comment?}`) → 201
  `{feedback_id, golden_case_id}`: `golden.record_feedback` (`active_config FOR SHARE`, then the job `FOR
  SHARE`), the score snapshot and the golden case of (revision, profile) upserted with the active scope. 409
  `conflict_state` without a published result of the active tuple, 404 `not_found` for no such job. The
  comment is at most 2,000 characters and holds no NUL and no lone surrogate (422 at the model's edge,
  `models.clean_text`; M2 Deviation 44).
- `GET /api/golden-cases?cursor&limit&active` → `{items, next_cursor}`: the cases by id ascending,
  keyset-paged with §14.3's signed cursor (`api.pagination`: sort `golden_cases.id`, `asc`,
  `last_key = [id]`, the filters `{"active": …}` when given), limit 1 to 100 (default 50); a cursor this list
  did not sign for these filters is 400 `invalid_cursor`. Each item `{id, job_id, label, comment,
  profile_version, scope_version, source, active, is_canary}` (`golden.CaseRow`; `profile_version` the
  version's number).
- `PATCH /api/golden-cases/{id}` (`{active?, is_canary?}`) → 200 the case (`golden.set_flags`): a third
  canary is 422 `validation_failed` (L4632), an unknown case 404.
- `DELETE /api/golden-cases/{id}` → 204 (`golden.delete_case_unless_running`, cascading to its eval rows; the
  deletion manifest is M5's): 404 for no such case, 409 `conflict_state` while a running eval run includes it.
- `DELETE /api/feedback/{feedback_id}` → 204 (`golden.delete_feedback`, plan M4 Task 7): the `score_feedback`
  row alone; the golden case it upserted stays (Deviation 24). 404 `not_found` for no such feedback
  (`golden.FeedbackMissing` through `api.errors.domain_problem`).

The domain functions log ids, kinds, labels and flags, never a comment. `utcnow` is a name of this module, so
a test injects the clock by patching `jobhunter.api.routes.golden.utcnow`.
"""

from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Path, Query
from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.responses import Response

from jobhunter.api.deps import get_config, get_engine, require_session
from jobhunter.api.errors import Problem, domain_problem
from jobhunter.api.models import clean_text
from jobhunter.api.openapi import BODY_LIMIT_RESPONSES, CSRF_RESPONSES, SESSION_RESPONSES
from jobhunter.api.pagination import (
    GOLDEN_CASES_SORT,
    cursor_key,
    decode_list_cursor,
    encode_list_cursor,
    invalid_cursor,
)
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.scoring import golden
from jobhunter.scoring.golden import COMMENT_MAX_CHARS, CaseRow, CaseSource, FeedbackKind, Label
from jobhunter.types import JSONValue

router = APIRouter(dependencies=[Depends(require_session)], responses=SESSION_RESPONSES)

DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 100
_BIGINT_MAX: Final = 2**63 - 1

Comment = Annotated[str, StringConstraints(max_length=COMMENT_MAX_CHARS), AfterValidator(clean_text)]


class FeedbackIn(BaseModel):
    """A label, or a "wrong score" with the label the user expected (§14.2 L4977)."""

    model_config = ConfigDict(extra="forbid")

    kind: FeedbackKind
    label: Label
    comment: Comment | None = None


class FeedbackOut(BaseModel):
    feedback_id: int
    golden_case_id: int


class GoldenCaseOut(BaseModel):
    """A golden case: its job, label and comment, its profile version's number and its scope version's id, its
    source and its flags."""

    id: int
    job_id: int
    label: Label
    comment: str | None
    profile_version: int
    scope_version: int
    source: CaseSource
    active: bool
    is_canary: bool

    @classmethod
    def of(cls, case: CaseRow) -> "GoldenCaseOut":
        return cls(
            id=case.id,
            job_id=case.job_id,
            label=case.label,
            comment=case.comment,
            profile_version=case.profile_version,
            scope_version=case.scope_version,
            source=case.source,
            active=case.active,
            is_canary=case.is_canary,
        )


class GoldenCasesPageOut(BaseModel):
    items: list[GoldenCaseOut]
    next_cursor: str | None


class FlagsIn(BaseModel):
    """The case's flags to set; an absent one is kept. At most two cases are canaries (L4632)."""

    model_config = ConfigDict(extra="forbid")

    active: bool | None = None
    is_canary: bool | None = None


JobId = Annotated[int, Path(ge=1, le=_BIGINT_MAX, description="The id of a job")]
CaseId = Annotated[int, Path(ge=1, le=_BIGINT_MAX, description="The id of a golden case")]
FeedbackId = Annotated[int, Path(ge=1, le=_BIGINT_MAX, description="The id of a label or a wrong score")]
_NOT_FOUND: Final[dict[int | str, dict[str, Any]]] = {
    404: {"description": "No such golden case (`not_found`)"}
}


@router.post(
    "/api/jobs/{job_id}/feedback",
    status_code=201,
    response_model=FeedbackOut,
    responses={
        404: {"description": "No such job (`not_found`)"},
        409: {
            "description": "The job has no published score of the active tuple to label (`conflict_state`)"
        },
        422: {
            "description": "Invalid body: a comment over 2,000 characters or holding a NUL or a lone "
            "surrogate, an unknown kind or label (`validation_failed`)"
        },
        **CSRF_RESPONSES,
        **BODY_LIMIT_RESPONSES,
    },
)
async def record_feedback(
    job_id: JobId, body: FeedbackIn, engine: Annotated[AsyncEngine, Depends(get_engine)]
) -> FeedbackOut:
    """Label a scored job, or mark its score wrong: the feedback with its score snapshot, and its golden case
    (201)."""
    async with engine.begin() as conn:
        recorded = await golden.record_feedback(
            conn, job_id=job_id, kind=body.kind, label=body.label, comment=body.comment, now=utcnow()
        )
    return FeedbackOut(feedback_id=recorded.feedback_id, golden_case_id=recorded.golden_case_id)


def _filters(active: bool | None) -> dict[str, JSONValue]:
    return {} if active is None else {"active": active}


def _after(cursor: str | None, key: bytes, filters: dict[str, JSONValue]) -> int | None:
    """The id the next page starts after, from a cursor of this list and these filters; `None` for the first
    page."""
    if cursor is None:
        return None
    match decode_list_cursor(cursor, key=key, sort=GOLDEN_CASES_SORT, dir="asc", filters=filters):
        case [int(case_id)] if not isinstance(case_id, bool) and case_id >= 1:
            return case_id
    raise invalid_cursor()


@router.get(
    "/api/golden-cases",
    response_model=GoldenCasesPageOut,
    responses={400: {"description": "A cursor this list did not issue for these filters (`invalid_cursor`)"}},
)
async def list_golden_cases(
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    cfg: Annotated[AppConfig, Depends(get_config)],
    cursor: Annotated[
        str | None, Query(max_length=2048, description="`next_cursor` of the page before")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    active: Annotated[bool | None, Query(description="Only the cases whose `active` flag is this")] = None,
) -> GoldenCasesPageOut:
    """The golden cases by id, keyset-paged (§14.3)."""
    key = cursor_key(cfg)
    filters = _filters(active)
    after = _after(cursor, key, filters)
    async with engine.connect() as conn:
        rows = await golden.list_cases(conn, after_id=after, limit=limit + 1, active=active)
    items = [GoldenCaseOut.of(row) for row in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        next_cursor = encode_list_cursor(
            sort=GOLDEN_CASES_SORT, dir="asc", last_key=[items[-1].id], filters=filters, key=key
        )
    return GoldenCasesPageOut(items=items, next_cursor=next_cursor)


@router.patch(
    "/api/golden-cases/{case_id}",
    response_model=GoldenCaseOut,
    responses={
        **_NOT_FOUND,
        422: {"description": "Invalid body, or a third canary (`validation_failed`)"},
        **CSRF_RESPONSES,
        **BODY_LIMIT_RESPONSES,
    },
)
async def set_golden_case_flags(
    case_id: CaseId, body: FlagsIn, engine: Annotated[AsyncEngine, Depends(get_engine)]
) -> GoldenCaseOut:
    """Set the case's `active` and `is_canary` flags; the case."""
    async with engine.begin() as conn:
        case = await golden.set_flags(
            conn, case_id, active=body.active, is_canary=body.is_canary, now=utcnow()
        )
    return GoldenCaseOut.of(case)


@router.delete(
    "/api/golden-cases/{case_id}",
    status_code=204,
    response_class=Response,
    responses={
        **_NOT_FOUND,
        409: {"description": "A running eval run includes the case (`conflict_state`)"},
        422: {"description": "Invalid path (`validation_failed`)"},
        **CSRF_RESPONSES,
    },
)
async def delete_golden_case(
    case_id: CaseId, engine: Annotated[AsyncEngine, Depends(get_engine)]
) -> Response:
    """Delete a golden case and its eval rows (204); its job's inputs lose their retention exemption."""
    async with engine.begin() as conn:
        if not await golden.delete_case_unless_running(conn, case_id):
            raise Problem(404, "not_found", "Golden case not found")
    return Response(status_code=204)


@router.delete(
    "/api/feedback/{feedback_id}",
    status_code=204,
    response_class=Response,
    responses={
        404: {"description": "No such label or wrong score (`not_found`)"},
        422: {"description": "Invalid path (`validation_failed`)"},
        **CSRF_RESPONSES,
    },
)
async def delete_feedback(
    feedback_id: FeedbackId, engine: Annotated[AsyncEngine, Depends(get_engine)]
) -> Response:
    """Delete a label or a wrong score (204); the golden case it made stays in the golden set."""
    try:
        async with engine.begin() as conn:
            await golden.delete_feedback(conn, feedback_id)
    except LookupError as exc:
        if (problem := domain_problem(exc)) is None:
            raise
        raise problem from None
    return Response(status_code=204)
