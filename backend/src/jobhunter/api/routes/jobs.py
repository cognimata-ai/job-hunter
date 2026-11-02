"""`/api/jobs` (spec §14.2, §14.3, §15.3; plan M4 Task 5, B7, B13, B18, B20, B29, B31): the Jobs list and its
facets.

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
  its cursor with that generation (`gen`) and the window's lower bound (`posted_after`), and has the capped
  `counts`; a later page takes `gen` and `posted_after` from its cursor, so its filters' hash, its cut-off
  and its banner refer to the first page (B31), and has no counts. `ranking_changed` is whether the
  snapshot's generation is above the cursor's `gen` (§14.3: the "Ranking changed" banner); `next_cursor`
  keeps the first page's `gen`. A cursor this list did not sign for this sort and these filters, or whose
  position no page of the sort ends at, is 400 `invalid_cursor`.
- `GET /api/jobs/facets?<filters>&company_q&limit` (`limit` 1 to 100, default 50; `company_q` at most 200
  characters, no NUL) → `FacetsOut{companies, sources}` (`jobs.facets`): the listed cards' companies and
  sources with their counts, each facet under every filter but its own, in one read snapshot; the posted
  window as on a first page.

Both depend on `require_session`; neither changes state, so neither takes `X-JH-Request` or a body. The
outbound links of the cards are never logged (`jobs.links`). `utcnow` is a name of this module, so a test
injects the clock by patching `jobhunter.api.routes.jobs.utcnow`.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Query
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, BaseModel, StringConstraints, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.api.deps import get_config, get_engine, require_session
from jobhunter.api.models import clean_text
from jobhunter.api.openapi import SESSION_RESPONSES
from jobhunter.api.pagination import (
    JobCursor,
    JobSort,
    cursor_key,
    decode_job_cursor,
    encode_job_cursor,
    invalid_cursor,
)
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.db.ranking import generation
from jobhunter.db.snapshot import read_snapshot
from jobhunter.jobs.cards import JobCardOut, build_cards
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
    """A page of the Jobs list in one read snapshot, with its ranking generation (module docstring)."""
    key = cursor_key(cfg)
    now = utcnow()
    async with read_snapshot(engine) as conn:
        current = await generation(conn)
        tz = await display_timezone(conn) if query.posted == "custom" else _UTC
        posted_after, posted_before = resolve_window(query, now=now, tz=tz)
        gen, after = current, None
        if cursor is not None:

            def hashed(cut_off: str | None) -> str:
                moment = None if cut_off is None else datetime.fromisoformat(cut_off)
                return jobs_filters_hash(query, posted_after=moment)

            decoded = decode_job_cursor(cursor, key=key, sort=query.sort, filters_hash=hashed)
            posted_after = (
                None if decoded.posted_after is None else datetime.fromisoformat(decoded.posted_after)
            )
            gen, after = decoded.gen, _position(decoded)
        try:
            selection = await select_page(
                conn, query, window=(posted_after, posted_before), after=after, limit=limit, with_counts=True
            )
        except ValueError:  # a position that no page of this sort ends at
            raise invalid_cursor() from None
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
        )
        next_cursor = encode_job_cursor(
            position, filters_hash=jobs_filters_hash(query, posted_after=posted_after), key=key
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
    """The companies and sources of the listed cards, with their counts (module docstring)."""
    now = utcnow()
    async with read_snapshot(engine) as conn:
        tz = await display_timezone(conn) if query.posted == "custom" else _UTC
        window = resolve_window(query, now=now, tz=tz)
        return await facets(conn, query, window=window, company_q=company_q, limit=limit)
