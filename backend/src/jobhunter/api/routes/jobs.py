"""`/api/jobs` (spec §14.2, §14.3, §15.3, §15.4; plan M4 Tasks 5 and 6, B7, B13, B18, B20, B25, B28, B29,
B31): the Jobs list, its facets, a job's detail and its score history.

**The filters** of both routes are `jobs.query.JobsQuery`'s fields as query parameters (`jobs_query`): a set
is a repeated parameter (`work_mode=remote&work_mode=hybrid`), an absent parameter is the field's default
(the ★ of §15.3), and the location is `loc_id` (the string `gazetteer_id` that `GET /api/geo/places` gives),
`loc_lat`, `loc_lon` and `radius_km`, all four or none. A value of the wrong type or outside its set, or one
`JobsQuery` refuses (an empty search text, a custom window without both dates in order, a score range out of
order, a location id that is no gazetteer id, a NUL), is 422 `validation_failed` at its query parameter.

- `GET /api/jobs?<filters>&cursor&limit` (`limit` 1 to 100, default 50; `cursor` at most 2,048 characters)
  → `JobsPageOut{items, next_cursor, ranking_generation, ranking_changed, counts}`. One read snapshot
  (`db.snapshot.read_snapshot`) holds the generation, the page (`jobs.listing.select_page`) and its cards
  (`jobs.cards.build_cards`), so `ranking_generation`, the snapshot's, describes exactly the rows of the
  page. A first page resolves the posted window at `utcnow()` (a `custom` one in `display.timezone`), stamps
  its cursor with that generation (`gen`) and the window's bounds (`posted_after`, `posted_before`), and has
  the capped `counts`; a later page takes `gen` and both bounds from its cursor, so its filters' hash, its
  window and its banner refer to the first page whatever the clock or the zone then (B31), and has no
  counts. `ranking_changed` is whether the
  snapshot's generation is above the cursor's `gen` (§14.3: the "Ranking changed" banner); `next_cursor`
  keeps the first page's `gen`. A cursor this list did not sign for this sort and these filters, or whose
  position no page of the sort ends at, is 400 `invalid_cursor`.
- `GET /api/jobs/facets?<filters>&company_q&limit` (`limit` 1 to 100, default 50; `company_q` at most 200
  characters, no NUL) → `FacetsOut{companies, sources}` (`jobs.facets`): the listed cards' companies and
  sources with their counts, each facet under every filter but its own, in one read snapshot; the posted
  window as on a first page.
- `GET /api/jobs/{job_id}` → `jobs.detail.JobDetailOut` in one read snapshot (`jobs.detail.job_detail`),
  its `history` the first page of the history below with its cursor signed here.
- `GET /api/jobs/{job_id}/history?cursor&limit` (`limit` 1 to 50, default 20; `cursor` at most 2,048
  characters) → `jobs.history.HistoryPageOut` in one read snapshot (`jobs.history.history_page`). Its
  cursor is a list cursor (`encode_list_cursor`) of the sort `JOB_HISTORY_SORT`, `desc`, whose `last_key` is
  the last row's `[published_at (ISO-8601 UTC), result_id]` and whose filters are `{"job_id": job_id}`: a
  cursor of another job's history, of another list, or whose position has another shape is 400
  `invalid_cursor`.

- `PUT /api/jobs/{job_id}/actions` (`jobs.actions.ActionsIn`) → `jobs.actions.ActionsOut{job, members}`
  (`jobs.actions.put_actions`, Task 7, B11) in one transaction at the request's time: the user's actions on
  the job, and with `apply_to_group` its group members' `saved`, `applied` and `hidden`; 404 as below for a
  job without a detail (a merge that held the job meanwhile included); 409 `conflict_state` with `detail =
  "purge_in_progress"` for a group write with a member under a purge fence (M5 Task 8); 422 for a body
  `ActionsIn` refuses.
  No `Idempotency-Key` (§14.1 keys none of these routes): a repeated write leaves the same values, only
  `updated_at` and the ranking generation move.

The `{job_id}` routes are declared after `/facets`, which would otherwise be a `job_id` that is no integer
(422). A job without a detail (`jobs.detail.JobGone`: unknown, synthetic, fenced, without a current
revision, or merged) is 404 `not_found` on each, with the member `merged_into_job_id`, the survivor of a
merged job and `null` for the others (B28); any other domain error is `api.errors.domain_problem`'s. A
`job_id` that is no integer from 1 to 2^63 - 1 is 422.

All depend on `require_session`. The GET routes change no state, so none takes `X-JH-Request` or a body;
the PUT is checked for `Origin` and `X-JH-Request` by the edge (403) and its body for the edge's ceiling
(413). The outbound links and the note are never logged (`jobs.links`, `jobs.actions`). `utcnow` is a name
of this module, so a test injects the clock by patching `jobhunter.api.routes.jobs.utcnow`.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Path, Query
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, BaseModel, StringConstraints, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.api.deps import get_config, get_engine, require_session
from jobhunter.api.errors import Problem, domain_problem
from jobhunter.api.models import clean_text
from jobhunter.api.openapi import BODY_LIMIT_RESPONSES, CSRF_RESPONSES, SESSION_RESPONSES
from jobhunter.api.pagination import (
    JOB_HISTORY_SORT,
    JobCursor,
    JobSort,
    cursor_key,
    decode_job_cursor,
    decode_list_cursor,
    encode_job_cursor,
    encode_list_cursor,
    invalid_cursor,
)
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.db.ranking import generation
from jobhunter.db.snapshot import read_snapshot
from jobhunter.errors import DomainError
from jobhunter.jobs import history
from jobhunter.jobs.actions import ActionsIn, ActionsOut, put_actions
from jobhunter.jobs.cards import JobCardOut, build_cards
from jobhunter.jobs.detail import HISTORY_LIMIT, JobDetailOut, JobGone, ensure_job, job_detail
from jobhunter.jobs.facets import FacetsOut, facets
from jobhunter.jobs.listing import Counts, Position, display_timezone, select_page
from jobhunter.jobs.query import (
    AdLanguage,
    Availability,
    Confidence,
    JobsQuery,
    PostedWindow,
    ScoringBucket,
    Tri,
    UserStatus,
    WorkMode,
    jobs_filters_hash,
    resolve_window,
)

router = APIRouter(dependencies=[Depends(require_session)], responses=SESSION_RESPONSES)

DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 100
MAX_CURSOR_CHARS: Final = 2048
MAX_COMPANY_Q_CHARS: Final = 200
# The query parameters of `JobsQuery.loc`'s fields
_LOC_PARAMETERS: Final = {
    "gazetteer_id": "loc_id",
    "lat": "loc_lat",
    "lon": "loc_lon",
    "radius_km": "radius_km",
}
_UTC: Final = "UTC"  # the zone of a relative window, which no zone moves
HISTORY_MAX_LIMIT: Final = 50
_BIGINT_MAX: Final = 2**63 - 1

JobId = Annotated[int, Path(ge=1, le=_BIGINT_MAX, description="The id of a job")]


class CountsOut(BaseModel):
    total_estimate: int
    unscored: int
    capped: bool


class JobsPageOut(BaseModel):
    items: list[JobCardOut]
    next_cursor: str | None
    ranking_generation: int
    ranking_changed: bool
    counts: CountsOut | None


def _query_error(error: Any) -> dict[str, Any]:
    """A `JobsQuery` error at its query parameter, as FastAPI reports one (no input echoed)."""
    loc = tuple(error["loc"])
    if loc and loc[0] == "loc":
        loc = (_LOC_PARAMETERS.get(str(loc[1]), "loc_id") if len(loc) > 1 else "loc_id", *loc[2:])
    return {"loc": ("query", *loc), "msg": error["msg"], "type": error["type"]}


def jobs_query(
    sort: Annotated[JobSort | None, Query(description="Default `score_mid`")] = None,
    q: Annotated[str | None, Query(description="A keyword: 1 to 200 characters")] = None,
    posted: Annotated[PostedWindow | None, Query(description="Default `30d`")] = None,
    posted_from: Annotated[date | None, Query(description="With `posted=custom` only")] = None,
    posted_to: Annotated[date | None, Query(description="With `posted=custom` only")] = None,
    score_min: Annotated[int | None, Query(description="0 to 100, default 0")] = None,
    score_max: Annotated[int | None, Query(description="0 to 100, default 100")] = None,
    coverage_min: Annotated[int | None, Query(description="0 to 100 (%), default 0")] = None,
    confidence: Annotated[Confidence | None, Query(description="Default `any`")] = None,
    loc_id: Annotated[str | None, Query(description="A `gazetteer_id` of `GET /api/geo/places`")] = None,
    loc_lat: Annotated[float | None, Query(description="The place's `lat`")] = None,
    loc_lon: Annotated[float | None, Query(description="The place's `lon`")] = None,
    radius_km: Annotated[int | None, Query(description="1 to 200")] = None,
    include_remote: Annotated[bool | None, Query(description="Default true")] = None,
    work_mode: Annotated[list[WorkMode] | None, Query(description="Default all four")] = None,
    company: Annotated[list[str] | None, Query(description="Company keys; default any")] = None,
    source: Annotated[list[str] | None, Query(description="Source ids; default any")] = None,
    lang: Annotated[list[AdLanguage] | None, Query(description="Default all four")] = None,
    employment: Annotated[list[str] | None, Query(description="Employment types or `unknown`")] = None,
    seniority: Annotated[list[str] | None, Query(description="Seniority levels")] = None,
    status: Annotated[list[UserStatus] | None, Query(description="Default all but `hidden`")] = None,
    availability: Annotated[
        list[Availability] | None, Query(description="Default `active`, `unknown`")
    ] = None,
    contested: Annotated[Tri | None, Query(description="Default `any`")] = None,
    partial: Annotated[Tri | None, Query(description="Default `any`")] = None,
    scoring_state: Annotated[
        list[ScoringBucket] | None, Query(description="Default all but `not_a_job_ad`")
    ] = None,
) -> JobsQuery:
    """The `JobsQuery` of a request's query parameters (module docstring); a refusal is 422."""
    fields: dict[str, Any] = {
        "sort": sort,
        "q": q,
        "posted": posted,
        "posted_from": posted_from,
        "posted_to": posted_to,
        "score_min": score_min,
        "score_max": score_max,
        "coverage_min": coverage_min,
        "confidence": confidence,
        "include_remote": include_remote,
        "work_mode": work_mode,
        "company": company,
        "source": source,
        "lang": lang,
        "employment": employment,
        "seniority": seniority,
        "status": status,
        "availability": availability,
        "contested": contested,
        "partial": partial,
        "scoring_state": scoring_state,
    }
    given = {name: value for name, value in fields.items() if value is not None}
    location = {"gazetteer_id": loc_id, "lat": loc_lat, "lon": loc_lon, "radius_km": radius_km}
    if any(value is not None for value in location.values()):
        given["loc"] = {name: value for name, value in location.items() if value is not None}
    try:
        return JobsQuery.model_validate(given)
    except ValidationError as exc:
        raise RequestValidationError([_query_error(e) for e in exc.errors(include_url=False)]) from None


# --- the cursor -------------------------------------------------------------------------------------------


def _utc_iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _position(cursor: JobCursor) -> Position:
    """The list position of a decoded cursor (its value checked by `JobCursor`)."""
    value = cursor.last_value
    match cursor.section:
        case "scored":
            return Position("scored", Decimal(str(value)), cursor.job_id)
        case "unscored":
            return Position("unscored", None if value is None else float(value), cursor.job_id)
        case _:
            return Position(
                "all", None if value is None else datetime.fromisoformat(str(value)), cursor.job_id
            )


def _moment(bound: str | None) -> datetime | None:
    """A window bound a cursor carries, as an instant."""
    return None if bound is None else datetime.fromisoformat(bound)


def _cursor_value(position: Position) -> str | float | None:
    match position.value:
        case Decimal():
            return format(position.value, "f")  # B7: a score as its fixed-point string
        case datetime():
            return _utc_iso(position.value)
        case _:
            return position.value


def _counts(counts: Counts | None) -> CountsOut | None:
    if counts is None:
        return None
    return CountsOut(total_estimate=counts.total, unscored=counts.unscored, capped=counts.capped)


@router.get(
    "/api/jobs",
    response_model=JobsPageOut,
    responses={
        400: {
            "description": "A cursor this list did not issue for this sort and these filters "
            "(`invalid_cursor`)"
        },
        422: {
            "description": "A filter of the wrong type, outside its set or refused by the list, or a `limit` "
            "outside 1 to 100 (`validation_failed`)"
        },
    },
)
async def list_jobs(
    query: Annotated[JobsQuery, Depends(jobs_query)],
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    cfg: Annotated[AppConfig, Depends(get_config)],
    cursor: Annotated[
        str | None, Query(max_length=MAX_CURSOR_CHARS, description="`next_cursor` of the page before")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> JobsPageOut:
    """A page of the Jobs list in one read snapshot, with its ranking generation."""
    # How the page is read and paged: the module docstring.
    key = cursor_key(cfg)
    now = utcnow()
    async with read_snapshot(engine) as conn:
        current = await generation(conn)
        tz = await display_timezone(conn) if query.posted == "custom" else _UTC
        posted_after, posted_before = resolve_window(query, now=now, tz=tz)
        gen, after = current, None
        if cursor is not None:

            def hashed(lower: str | None, upper: str | None) -> str:
                return jobs_filters_hash(query, posted_after=_moment(lower), posted_before=_moment(upper))

            decoded = decode_job_cursor(cursor, key=key, sort=query.sort, filters_hash=hashed)
            posted_after, posted_before = _moment(decoded.posted_after), _moment(decoded.posted_before)
            gen, after = decoded.gen, _position(decoded)
        try:
            selection = await select_page(
                conn, query, window=(posted_after, posted_before), after=after, limit=limit, with_counts=True
            )
        except ValueError:
            if after is None:  # no cursor: a bug, which the last-resort handler answers 500 and alerts on
                raise
            raise invalid_cursor() from None  # a position that no page of this sort ends at
        items = await build_cards(conn, selection.rows, now=now)
    next_cursor = None
    if selection.next is not None:
        position = JobCursor(
            sort=query.sort,
            section=selection.next.section,
            last_value=_cursor_value(selection.next),
            job_id=selection.next.job_id,
            gen=gen,
            posted_after=None if posted_after is None else _utc_iso(posted_after),
            posted_before=None if posted_before is None else _utc_iso(posted_before),
        )
        next_cursor = encode_job_cursor(
            position,
            filters_hash=jobs_filters_hash(query, posted_after=posted_after, posted_before=posted_before),
            key=key,
        )
    return JobsPageOut(
        items=items,
        next_cursor=next_cursor,
        ranking_generation=current,
        ranking_changed=current > gen,
        counts=_counts(selection.counts),
    )


@router.get(
    "/api/jobs/facets",
    response_model=FacetsOut,
    responses={
        422: {
            "description": "A filter of the wrong type, outside its set or refused by the list, a "
            "`company_q` over 200 characters or with a NUL, or a `limit` outside 1 to 100 "
            "(`validation_failed`)"
        }
    },
)
async def list_job_facets(
    query: Annotated[JobsQuery, Depends(jobs_query)],
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    company_q: Annotated[
        str,
        StringConstraints(max_length=MAX_COMPANY_Q_CHARS),
        AfterValidator(clean_text),
        Query(description="A prefix of a company's name or key, folded; empty: every company"),
    ] = "",
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> FacetsOut:
    """The companies and sources of the listed cards, with their counts."""
    # What the facets count: the module docstring.
    now = utcnow()
    async with read_snapshot(engine) as conn:
        tz = await display_timezone(conn) if query.posted == "custom" else _UTC
        window = resolve_window(query, now=now, tz=tz)
        return await facets(conn, query, window=window, company_q=company_q, limit=limit)


# --- a job's detail and history ---------------------------------------------------------------------------

_GONE_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    404: {
        "description": "No such job, or one without a detail: a synthetic, fenced or merged job "
        "(`not_found`, with `merged_into_job_id`: a merged job's survivor, else null)"
    },
}


def _gone(gone: JobGone) -> Problem:
    """B28's 404: content-free, with the survivor of a merged job."""
    return Problem(404, "not_found", "Not found", extra={"merged_into_job_id": gone.merged_into_job_id})


def _history_filters(job_id: int) -> dict[str, Any]:
    return {"job_id": job_id}


def _history_cursor(job_id: int, position: history.Position | None, key: bytes) -> str | None:
    """The signed cursor after `position` of the job's history (module docstring), `None` without one."""
    if position is None:
        return None
    published_at, result_id = position
    return encode_list_cursor(
        sort=JOB_HISTORY_SORT,
        dir="desc",
        last_key=[_utc_iso(published_at), result_id],
        filters=_history_filters(job_id),
        key=key,
    )


def _history_after(cursor: str, job_id: int, key: bytes) -> history.Position:
    """The position of a cursor of the job's history; 400 `invalid_cursor` for any other."""
    last_key = decode_list_cursor(
        cursor, key=key, sort=JOB_HISTORY_SORT, dir="desc", filters=_history_filters(job_id)
    )
    match last_key:
        case [str(at), int(result_id)] if not isinstance(result_id, bool) and result_id >= 1:
            try:
                published_at = datetime.fromisoformat(at)
            except ValueError:
                raise invalid_cursor() from None
            if published_at.utcoffset() != timedelta(0):
                raise invalid_cursor()
            return published_at, result_id
        case _:
            raise invalid_cursor()


def _domain(exc: DomainError | LookupError) -> Problem | None:
    return _gone(exc) if isinstance(exc, JobGone) else domain_problem(exc)


@router.get(
    "/api/jobs/{job_id}",
    response_model=JobDetailOut,
    responses={
        **_GONE_RESPONSES,
        422: {"description": "A `job_id` that is no integer from 1 to 2^63 - 1 (`validation_failed`)"},
    },
)
async def get_job(
    job_id: JobId,
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    cfg: Annotated[AppConfig, Depends(get_config)],
) -> JobDetailOut:
    """A job's detail in one read snapshot, its history's first page with a signed cursor."""
    # What the detail holds and how its history is paged: the module docstring.
    now = utcnow()
    try:
        async with read_snapshot(engine) as conn:
            detail = await job_detail(conn, job_id, now=now)
    except (DomainError, LookupError) as exc:
        if (problem := _domain(exc)) is None:
            raise
        raise problem from None
    detail.history.next_cursor = _history_cursor(job_id, detail.history_next, cursor_key(cfg))
    return detail


@router.get(
    "/api/jobs/{job_id}/history",
    response_model=history.HistoryPageOut,
    responses={
        400: {"description": "A cursor this job's history did not issue (`invalid_cursor`)"},
        **_GONE_RESPONSES,
        422: {
            "description": "A `job_id` that is no integer from 1 to 2^63 - 1, a `limit` outside 1 to 50 or a "
            "`cursor` over 2,048 characters (`validation_failed`)"
        },
    },
)
async def get_job_history(
    job_id: JobId,
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    cfg: Annotated[AppConfig, Depends(get_config)],
    cursor: Annotated[
        str | None, Query(max_length=MAX_CURSOR_CHARS, description="`next_cursor` of the page before")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=HISTORY_MAX_LIMIT)] = HISTORY_LIMIT,
) -> history.HistoryPageOut:
    """A page of the job's score history in one read snapshot. The cursor is read before the snapshot, so a
    cursor this route did not sign is 400 even for a gone job, before its 404."""
    # The history's order and cursor: the module docstring.
    key = cursor_key(cfg)
    after = None if cursor is None else _history_after(cursor, job_id, key)
    try:
        async with read_snapshot(engine) as conn:
            await ensure_job(conn, job_id)
            items, position = await history.history_page(conn, job_id, after=after, limit=limit)
    except (DomainError, LookupError) as exc:
        if (problem := _domain(exc)) is None:
            raise
        raise problem from None
    return history.HistoryPageOut(items=items, next_cursor=_history_cursor(job_id, position, key))


@router.put(
    "/api/jobs/{job_id}/actions",
    response_model=ActionsOut,
    responses={
        **_GONE_RESPONSES,
        409: {
            "description": "With `apply_to_group`, a member of the job's group is being purged "
            "(`conflict_state`, detail `purge_in_progress`)"
        },
        422: {
            "description": "A `job_id` that is no integer from 1 to 2^63 - 1, or an invalid body: an unknown "
            "field, none of `seen`, `saved`, `applied`, `hidden`, `note` and `seen_on_open`, `seen_on_open` "
            "with `seen`, or a note over 2,000 characters or holding a NUL or a lone surrogate "
            "(`validation_failed`)"
        },
        **CSRF_RESPONSES,
        **BODY_LIMIT_RESPONSES,
    },
)
async def put_job_actions(
    job_id: JobId, body: ActionsIn, engine: Annotated[AsyncEngine, Depends(get_engine)]
) -> ActionsOut:
    """Mark the job seen, saved, applied or hidden, clear a mark or edit its note, and with `apply_to_group`
    write `saved`, `applied` and `hidden` on the other members of its duplicate group; the rows after the
    write."""
    now = utcnow()
    try:
        async with engine.begin() as conn:
            written = await put_actions(conn, job_id, body, now=now)
    except DomainError as exc:
        # Not `LookupError`: the only one the write can meet is `ranking.bump`'s missing row, a bug (500).
        if (problem := _domain(exc)) is None:
            raise
        raise problem from None
    return written
