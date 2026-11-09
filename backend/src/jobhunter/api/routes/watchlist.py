"""`/api/watchlist`: the company watchlist the settings editor authors (spec §5.4, §9.1, §13.1, §14.2; plan
Task 22, Deviations 3 and 15; taken from M4; the list: plan M4 Task 13).

- `GET /api/watchlist` → every company by display name, then id (`sources.watchlist_listing.watchlist`), with
  its validation, its flag in the active settings, its instance's health and last complete snapshot, and its
  posting counts, in one read snapshot (`db.snapshot.read_snapshot`), a fixed number of statements, no lock.
- `POST /api/watchlist/parse-url` (`{url}`) → 200 `{ats, slug, region}`, or `{unsupported: <reason>}` for a
  URL that names no supported board (`watchlist_patterns.detect`: `workable`, `unsupported_pattern`,
  `bad_slug`, `not_a_url`). It reads the URL by pattern alone and makes no network request.
- `POST /api/watchlist` (`{display_name, ats_type, board_slug, lever_region?, input_url?}`) → 201 `{id,
  validation_status: "unvalidated"}`: one transaction that
  1. takes `active_config` `FOR UPDATE`, the first lock (§9.1), before it touches any company or instance row;
  2. inserts the `watchlist_companies` row, `ON CONFLICT DO NOTHING` on the board's unique constraint: a board
     already on the list is 409 `conflict_state` with that company's id (`watchlist_company_id`), found by a
     read after the insert met it, never by a check before it;
  3. makes or re-links the board's source instance (`instance_for_company`): a board removed and added again
     gets its old instance back, with its postings, because the removal left that row and only cleared its
     company;
  4. requests the board's validation (`request_validation`: the M1 `watchlist_validate` operation, its job
     deferred in this transaction);
  5. adds `{watchlist_company_id, enabled: true}` to the active settings' `watchlist[]` through an internal
     save (`save_settings(..., internal=True)`, Deviation 15): one new version of class `collection`, which
     copies the active version's acknowledged warnings and whose cells exist for the new board.
  A save the settings refuse (a 101st entry) is 422 `validation_failed`, and the rollback takes the company
  with it.
- `DELETE /api/watchlist/{id}` → 204: `active_config` `FOR UPDATE` first, then the company row (404
  `not_found` without one), then, when the active settings list it, an internal save of `watchlist[]` without
  it (one `collection` version), then the company row is deleted. `source_instances.watchlist_company_id`
  becomes NULL by its foreign key, so the instance and its postings stay. A company no settings version names
  writes no version. A validation still live for the removed company is left alone: the M1 handler ends it
  `succeeded` once it finds the company gone, whether that is before its request or after the board answered.

**The body.** `display_name` is trimmed and has 1 to 120 characters (the column's CHECK, §7.5). `board_slug`
must match `watchlist_patterns.SLUG`, the rule `detect` applies (§5.4), and is stored as written.
`lever_region` is required for Lever and refused for every other ATS (the table's CHECK). `input_url`, when
given, is trimmed (at most 2048 characters) and must be the URL `detect` reads as exactly this ATS, slug and
region: only then is the company's `detection` `pattern`, else `manual`. Strings hold no NUL and no lone
surrogate (422). Bodies refuse unknown members, and a path id is bounded at 2**63 - 1 (422).

Neither route takes an `Idempotency-Key` (§14.1 does not list them): a repeated add meets its own board (409),
a repeated remove the missing row (404). `utcnow` is a name of this module, so a test injects the clock by
patching `jobhunter.api.routes.watchlist.utcnow`. Logs carry ids and the ATS code, never a name, slug or URL.
"""

from datetime import datetime
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, Path
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from starlette.responses import Response

from jobhunter.api.deps import get_engine, require_session
from jobhunter.api.errors import FieldError, Problem
from jobhunter.api.models import clean_text
from jobhunter.api.openapi import BODY_LIMIT_RESPONSES, CSRF_RESPONSES, SESSION_RESPONSES
from jobhunter.clock import utcnow
from jobhunter.db.snapshot import read_snapshot
from jobhunter.db.sql import q
from jobhunter.normalize.companies import company_key
from jobhunter.observability.logging import get_logger
from jobhunter.settings.active import lock_active
from jobhunter.settings.save import SaveOutcome, SaveRequest, save_settings
from jobhunter.sources import watchlist_listing
from jobhunter.sources.instances import instance_for_company
from jobhunter.sources.watchlist_listing import WatchlistCompanyOut
from jobhunter.sources.watchlist_patterns import (
    SLUG,
    AtsType,
    Detected,
    LeverRegion,
    Unsupported,
    detect,
)
from jobhunter.sources.watchlist_validate import request_validation
from jobhunter.types import JSONValue
from jobhunter.worker.tasks import defer_in_tx

router = APIRouter(
    prefix="/api/watchlist", dependencies=[Depends(require_session)], responses=SESSION_RESPONSES
)
log = get_logger("jobhunter.api.watchlist")

_BIGINT_MAX: Final = 2**63 - 1
DISPLAY_NAME_CHARS: Final = (1, 120)  # the CHECK of `watchlist_companies.display_name` (§7.5)
INPUT_URL_MAX_CHARS: Final = 2048
SLUG_MAX_CHARS: Final = 100  # `watchlist_patterns.SLUG` allows 1 to 100 characters
_SLUG_RULE: Final = "Letters, digits, `.`, `_` and `-`, 1 to 100 characters, starting with a letter or digit"
# How often an insert that met a board retries when that company was removed before it could be read.
_INSERT_TRIES: Final = 3
_FIELD_REGION: Final = "body.lever_region"
_FIELD_URL: Final = "body.input_url"

# The conflict target repeats the unique constraint of `watchlist_companies` exactly (NULLS NOT DISTINCT: the
# region of a board that is not Lever's is NULL, and two such boards of one slug still collide).
_INSERT: Final = q(
    "INSERT INTO watchlist_companies (display_name, company_key, ats_type, board_slug, lever_region, "
    "input_url, detection, created_at) "
    "VALUES (:display_name, :company_key, :ats_type, :board_slug, :lever_region, :input_url, :detection, "
    ":now) "
    "ON CONFLICT (ats_type, board_slug, lever_region) DO NOTHING RETURNING id"
)
_EXISTING: Final = q(
    "SELECT id FROM watchlist_companies WHERE ats_type = :ats_type AND board_slug = :board_slug "
    "AND lever_region IS NOT DISTINCT FROM CAST(:lever_region AS text)"
)
_LOCK_COMPANY: Final = q("SELECT id FROM watchlist_companies WHERE id = :id FOR UPDATE")
_DELETE: Final = q("DELETE FROM watchlist_companies WHERE id = :id")

CompanyId = Annotated[int, Path(ge=1, le=_BIGINT_MAX, description="The id of a watchlist company")]
UnsupportedReason = Literal["workable", "unsupported_pattern", "bad_slug", "not_a_url"]
Detection = Literal["pattern", "manual"]


class ParseUrlIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: Annotated[str, StringConstraints(max_length=INPUT_URL_MAX_CHARS), AfterValidator(clean_text)]


class ParsedUrlOut(BaseModel):
    """The board a careers URL names; `region` is Lever's and `null` for every other ATS."""

    ats: AtsType
    slug: str
    region: LeverRegion | None


class UnsupportedUrlOut(BaseModel):
    unsupported: UnsupportedReason


class WatchlistIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=DISPLAY_NAME_CHARS[0], max_length=DISPLAY_NAME_CHARS[1]
        ),
        AfterValidator(clean_text),
    ]
    ats_type: AtsType
    board_slug: Annotated[
        str,
        StringConstraints(max_length=SLUG_MAX_CHARS),
        AfterValidator(clean_text),
        Field(description=_SLUG_RULE + ", taken as written"),
    ]
    lever_region: LeverRegion | None = Field(
        default=None, description="Required for Lever, refused otherwise"
    )
    input_url: (
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, max_length=INPUT_URL_MAX_CHARS),
            AfterValidator(clean_text),
            Field(description="The careers URL the board was parsed from; it makes `detection` `pattern`"),
        ]
        | None
    ) = None

    @field_validator("board_slug")
    @classmethod
    def _slug(cls, value: str) -> str:
        if SLUG.fullmatch(value) is None:
            raise ValueError(_SLUG_RULE)
        return value


class WatchlistCreatedOut(BaseModel):
    id: int
    validation_status: Literal["unvalidated"]


def _checked(body: WatchlistIn) -> Detected:
    """The board `body` names, or 422 `validation_failed` on `body.lever_region` or `body.input_url`: the
    region goes with Lever alone, and an `input_url` must read as this very board (module docstring)."""
    board = Detected(body.ats_type, body.board_slug, body.lever_region)
    if (body.ats_type == "lever") != (body.lever_region is not None):
        code = "required" if body.ats_type == "lever" else "not_applicable"
        message = "A Lever board has its region (global or eu); no other board has one"
        field = _FIELD_REGION
    elif body.input_url is not None and detect(body.input_url) != board:
        code, field = "mismatch", _FIELD_URL
        message = "The URL does not name this board; leave it out for a board entered by hand"
    else:
        return board
    raise Problem(
        422, "validation_failed", "Request validation failed", errors=[FieldError(field, code, message)]
    )


def _detection(body: WatchlistIn) -> Detection:
    """How the board was named: by a careers URL read by pattern, or by hand (Deviation 3)."""
    return "manual" if body.input_url is None else "pattern"


def _entries(payload: JSONValue) -> list[JSONValue]:
    if not isinstance(payload, dict) or not isinstance(entries := payload.get("watchlist"), list):
        raise TypeError("a settings payload holds a watchlist list")
    return entries


def _with_entry(payload: JSONValue, company_id: int) -> dict[str, JSONValue]:
    """`payload` with the entry of `company_id` appended to `watchlist[]`, enabled."""
    entry: JSONValue = {"watchlist_company_id": company_id, "enabled": True}
    return {**_object(payload), "watchlist": [*_entries(payload), entry]}


def _without_entry(payload: JSONValue, company_id: int) -> dict[str, JSONValue] | None:
    """`payload` without the `watchlist[]` entry of `company_id`, or `None` when none names it."""
    entries = _entries(payload)
    kept = [e for e in entries if not (isinstance(e, dict) and e.get("watchlist_company_id") == company_id)]
    return {**_object(payload), "watchlist": kept} if len(kept) != len(entries) else None


def _object(payload: JSONValue) -> dict[str, JSONValue]:
    if not isinstance(payload, dict):
        raise TypeError("a settings payload is an object")
    return payload


async def _save_internally(
    conn: AsyncConnection, payload: dict[str, JSONValue], base_version_id: int, *, now: datetime
) -> SaveOutcome:
    request = SaveRequest(payload=payload, base_version_id=base_version_id, acknowledged_warnings=frozenset())
    return await save_settings(conn, request, now=now, defer=defer_in_tx, internal=True)


async def _insert_company(
    conn: AsyncConnection, body: WatchlistIn, board: Detected, detection: Detection, *, now: datetime
) -> int:
    """The new company's id, or 409 with the company that already holds the board."""
    board_key = {"ats_type": board.ats, "board_slug": board.slug, "lever_region": board.lever_region}
    params = {
        **board_key,
        "display_name": body.display_name,
        "company_key": company_key(body.display_name),
        "input_url": body.input_url,
        "detection": detection,
        "now": now,
    }
    for _ in range(_INSERT_TRIES):
        inserted = (await conn.execute(_INSERT, params)).scalar_one_or_none()
        if inserted is not None:
            return int(inserted)
        # The insert met the board; a statement later its company may have been removed, and the next insert
        # then succeeds.
        existing = (await conn.execute(_EXISTING, board_key)).scalar_one_or_none()
        if existing is not None:
            raise Problem(
                409,
                "conflict_state",
                "This board is already on the watchlist",
                detail="Another company has the same board; edit or enable that one instead.",
                extra={"watchlist_company_id": int(existing)},
            )
    raise RuntimeError("watchlist: the company of a board kept being removed during an insert")


@router.get("", response_model=list[WatchlistCompanyOut])
async def list_companies(engine: Annotated[AsyncEngine, Depends(get_engine)]) -> list[WatchlistCompanyOut]:
    """Every watchlist company by display name, then id: its board, its validation, whether the active
    settings enable it, its source instance's health, last error, last success and last complete snapshot,
    and its postings by availability."""
    async with read_snapshot(engine) as conn:
        return await watchlist_listing.watchlist(conn)


@router.post(
    "/parse-url",
    response_model=ParsedUrlOut | UnsupportedUrlOut,
    responses={
        422: {"description": "Invalid body (`validation_failed`)"},
        **CSRF_RESPONSES,
        **BODY_LIMIT_RESPONSES,
    },
)
async def parse_url(body: ParseUrlIn) -> ParsedUrlOut | UnsupportedUrlOut:
    """Read a careers URL by pattern (200): the board it names, or why it names none. Never fetches it."""
    found = detect(body.url)
    if isinstance(found, Unsupported):
        return UnsupportedUrlOut(unsupported=found.reason)
    return ParsedUrlOut(ats=found.ats, slug=found.slug, region=found.lever_region)


@router.post(
    "",
    status_code=201,
    response_model=WatchlistCreatedOut,
    responses={
        409: {
            "description": "The board is already on the watchlist (`conflict_state`, with the company's "
            "`watchlist_company_id`)"
        },
        422: {
            "description": "Invalid body, or settings the new entry cannot be saved in (`validation_failed`)"
        },
        **CSRF_RESPONSES,
        **BODY_LIMIT_RESPONSES,
    },
)
async def add_company(
    body: WatchlistIn, engine: Annotated[AsyncEngine, Depends(get_engine)]
) -> WatchlistCreatedOut:
    """Add a board (201): company, instance, validation and settings version in one transaction."""
    board = _checked(body)
    detection = _detection(body)
    now = utcnow()
    async with engine.begin() as conn:
        active = await lock_active(conn)  # the first lock (§9.1), then the active document
        company_id = await _insert_company(conn, body, board, detection, now=now)
        await instance_for_company(conn, company_id)
        operation_id, _ = await request_validation(conn, company_id, defer=defer_in_tx)
        payload = _with_entry(active.payload, company_id)
        outcome = await _save_internally(conn, payload, active.settings_version_id, now=now)
    log.info(
        "watchlist_company_added",
        watchlist_company_id=company_id,
        ats=board.ats,
        detection=detection,
        operation_id=operation_id,
        settings_version_id=outcome.version_id,
    )
    return WatchlistCreatedOut(id=company_id, validation_status="unvalidated")


@router.delete(
    "/{company_id}",
    status_code=204,
    response_class=Response,
    responses={
        404: {"description": "No such watchlist company (`not_found`)"},
        422: {"description": "Invalid id (`validation_failed`)"},
        **CSRF_RESPONSES,
    },
)
async def remove_company(
    company_id: CompanyId, engine: Annotated[AsyncEngine, Depends(get_engine)]
) -> Response:
    """Remove a board (204): its settings entry goes in a new version, the company row is deleted, and the
    board's instance and postings stay."""
    now = utcnow()
    async with engine.begin() as conn:
        active = await lock_active(conn)  # the first lock (§9.1), then the active document
        if (await conn.execute(_LOCK_COMPANY, {"id": company_id})).first() is None:
            raise Problem(404, "not_found", "Watchlist company not found")
        outcome: SaveOutcome | None = None
        if (payload := _without_entry(active.payload, company_id)) is not None:
            outcome = await _save_internally(conn, payload, active.settings_version_id, now=now)
        await conn.execute(_DELETE, {"id": company_id})
    log.info(
        "watchlist_company_removed",
        watchlist_company_id=company_id,
        settings_version_id=None if outcome is None else outcome.version_id,
    )
    return Response(status_code=204)
