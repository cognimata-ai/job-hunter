"""The query of a Jobs list request (spec §15.3, §14.3; plan M4 Task 4, B4, B5, B7, B15, B19, B31).

`JobsQuery` holds the sort and every §15.3 filter with its default (the ★ of the table). It refuses, each at
its own field (a 422 there): an empty multi-select of a closed set (`work_mode`, `lang`, `status`,
`availability`, `scoring_state`); a value outside its set; a `custom` posted window without both dates in
order, or dates without `custom`; a score range out of order; a search text that is empty, longer than 200
characters, or holds a NUL or a lone surrogate (neither reaches PostgreSQL or UTF-8), and a company or source
key that holds one; a location number that is out of range, NaN or infinite. No non-finite number reaches
`filters_hash`, which would refuse it.

`resolve_window` turns the posted window into absolute UTC bounds, fixed once per first page: a relative
window ends at the request's `now`; a `custom` one runs from midnight of `posted_from` to midnight after
`posted_to` in `display.timezone` (B31). The cursor carries both bounds, so a later page keeps them whatever
the clock or the zone then. `normalized_filters` is every filter but the sort with its defaults applied, each
set as a sorted list without repeats, and `posted_after` and `posted_before` as ISO-8601 UTC times with
microseconds, so equivalent requests hash equal (`jobs_filters_hash`, B7). `score_filter_active` is whether
any score filter is off its default: then only scored cards are listed (B15).
"""

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Annotated, Final, Literal, get_args
from zoneinfo import ZoneInfo

from pydantic import AfterValidator, BaseModel, Field, ValidationInfo, field_validator
from pydantic_core import PydanticCustomError

from jobhunter.api.pagination import JobSort, filters_hash
from jobhunter.normalize.titles import SENIORITY_LADDER
from jobhunter.sources.parsing.employment import EmploymentType
from jobhunter.types import JSONValue

PostedWindow = Literal["24h", "3d", "7d", "14d", "30d", "any", "custom"]
WorkMode = Literal["remote", "hybrid", "onsite", "unknown"]
UserStatus = Literal["unseen", "seen", "saved", "applied", "hidden"]
ScoringBucket = Literal["scored", "unscored", "filtered", "failed", "expired", "not_a_job_ad"]
Tri = Literal["any", "only", "exclude"]
AdLanguage = Literal["de", "en", "other", "unknown"]
Availability = Literal["active", "unknown", "stale", "closed"]
Confidence = Literal["any", "medium", "high"]

# The values of the open-typed multi-selects: the employment types plus `unknown` (no type known), and the
# seniority ladder (`mid` also matches a job without a token or structured level, `normalize/titles.py`).
EMPLOYMENT_VALUES: Final = frozenset((*get_args(EmploymentType), "unknown"))
SENIORITY_VALUES: Final = frozenset(SENIORITY_LADDER)

_WINDOWS: Final[Mapping[str, timedelta]] = MappingProxyType(
    {
        "24h": timedelta(hours=24),
        "3d": timedelta(days=3),
        "7d": timedelta(days=7),
        "14d": timedelta(days=14),
        "30d": timedelta(days=30),
    }
)
_SURROGATES: Final = range(0xD800, 0xE000)


def _storable(value: str) -> bool:
    """Whether PostgreSQL and UTF-8 can hold `value`: no NUL, no lone surrogate."""
    return not any(char == "\x00" or ord(char) in _SURROGATES for char in value)


def _text(value: str) -> str:
    if not _storable(value):
        raise PydanticCustomError("unstorable_text", "the text holds a NUL or a lone surrogate")
    return value


def _keys(values: frozenset[str]) -> frozenset[str]:
    if not all(_storable(value) for value in values):
        raise PydanticCustomError("unstorable_text", "a value holds a NUL or a lone surrogate")
    return values


def _members(allowed: frozenset[str]) -> Callable[[frozenset[str]], frozenset[str]]:
    def check(values: frozenset[str]) -> frozenset[str]:
        if not values <= allowed:
            raise PydanticCustomError(
                "unknown_value", "unknown values: {unknown}", {"unknown": sorted(values - allowed)}
            )
        return values

    return check


Keys = Annotated[frozenset[str], AfterValidator(_keys)]


class LocationFilter(BaseModel, frozen=True, extra="forbid"):
    """A gazetteer place and a radius around it (§8.12): the `/api/geo/places` and `settings.model` form."""

    gazetteer_id: str = Field(pattern=r"^geonames:[0-9]{1,12}$")
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)
    radius_km: int = Field(ge=1, le=200)


class JobsQuery(BaseModel, frozen=True, extra="forbid"):
    """The sort and the filters of one Jobs list request, each default the ★ of §15.3."""

    sort: JobSort = "score_mid"
    q: Annotated[str, AfterValidator(_text)] | None = Field(None, min_length=1, max_length=200)
    posted: PostedWindow = "30d"
    posted_from: date | None = Field(None, validate_default=True)
    posted_to: date | None = Field(None, validate_default=True)
    score_min: int = Field(0, ge=0, le=100)
    score_max: int = Field(100, ge=0, le=100)
    coverage_min: int = Field(0, ge=0, le=100)
    confidence: Confidence = "any"  # `medium` is "≥ medium"
    loc: LocationFilter | None = None
    include_remote: bool = True
    work_mode: frozenset[WorkMode] = Field(frozenset(get_args(WorkMode)), min_length=1)  # B19
    company: Keys = frozenset()  # empty: any company
    source: Keys = frozenset()  # empty: any source
    lang: frozenset[AdLanguage] = Field(frozenset(get_args(AdLanguage)), min_length=1)
    employment: Annotated[frozenset[str], AfterValidator(_members(EMPLOYMENT_VALUES))] = frozenset()
    seniority: Annotated[frozenset[str], AfterValidator(_members(SENIORITY_VALUES))] = frozenset()
    status: frozenset[UserStatus] = Field(frozenset({"unseen", "seen", "saved", "applied"}), min_length=1)
    availability: frozenset[Availability] = Field(frozenset({"active", "unknown"}), min_length=1)
    contested: Tri = "any"
    partial: Tri = "any"
    scoring_state: frozenset[ScoringBucket] = Field(
        frozenset(set(get_args(ScoringBucket)) - {"not_a_job_ad"}), min_length=1
    )

    @field_validator("posted_from", "posted_to")
    @classmethod
    def _custom_dates(cls, value: date | None, info: ValidationInfo) -> date | None:
        """Both dates are given exactly when the window is `custom`, `posted_from` not after `posted_to`."""
        posted = info.data.get("posted")
        if posted is None:  # the window itself was refused, at its own field
            return value
        if (posted == "custom") != (value is not None):
            raise PydanticCustomError(
                "custom_window", "posted_from and posted_to are given exactly when posted is custom"
            )
        start = info.data.get("posted_from")
        if info.field_name == "posted_to" and value is not None and start is not None and start > value:
            raise PydanticCustomError("custom_window_order", "posted_from is after posted_to")
        return value

    @field_validator("score_max")
    @classmethod
    def _score_range(cls, value: int, info: ValidationInfo) -> int:
        low = info.data.get("score_min")
        if low is not None and low > value:
            raise PydanticCustomError("score_range_order", "score_min is above score_max")
        return value


def resolve_window(q: JobsQuery, *, now: datetime, tz: str) -> tuple[datetime | None, datetime | None]:
    """`(posted_after, posted_before)` in UTC: `now` minus a relative window and no upper bound; none for
    `any`; for `custom`, midnight of `posted_from` and midnight after `posted_to` in the time zone `tz`
    (`display.timezone`, B31). `now` must be timezone-aware."""
    if now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    match q.posted:
        case "any":
            return None, None
        case "custom":
            if q.posted_from is None or q.posted_to is None:  # JobsQuery refuses it
                raise ValueError("a custom window needs posted_from and posted_to")
            zone = ZoneInfo(tz)
            start = datetime.combine(q.posted_from, time(0), tzinfo=zone)
            end = datetime.combine(q.posted_to + timedelta(days=1), time(0), tzinfo=zone)
            return start.astimezone(UTC), end.astimezone(UTC)
        case window:
            return now.astimezone(UTC) - _WINDOWS[window], None


def _iso_utc(moment: datetime | None) -> str | None:
    """The one spelling of a window bound in the hash: UTC, microseconds, `+00:00`; `None` for no bound."""
    if moment is None:
        return None
    if moment.utcoffset() is None:
        raise ValueError("a window bound must be timezone-aware")
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _normalized(value: object) -> JSONValue:
    match value:
        case frozenset():
            items: list[JSONValue] = [str(item) for item in sorted(value)]
            return items
        case date():
            return value.isoformat()
        case LocationFilter():
            return {name: _normalized(item) for name, item in value.model_dump().items()}
        case bool() | int() | float() | str() | None:
            return value
    raise TypeError(f"not a filter value: {type(value).__name__}")


def normalized_filters(
    q: JobsQuery, *, posted_after: datetime | None, posted_before: datetime | None = None
) -> dict[str, JSONValue]:
    """Every filter of `q` but `sort`, defaults applied, each set a sorted list without repeats, and the
    window's bounds `posted_after` and `posted_before` as ISO-8601 UTC times with microseconds (`None` for a
    bound the window lacks: every window but `custom` has no upper one)."""
    filters = {name: _normalized(getattr(q, name)) for name in type(q).model_fields if name != "sort"}
    filters["posted_after"] = _iso_utc(posted_after)
    filters["posted_before"] = _iso_utc(posted_before)
    return filters


def jobs_filters_hash(
    q: JobsQuery, *, posted_after: datetime | None, posted_before: datetime | None = None
) -> str:
    """The cursor's `filters_hash` of `q` with the window's bounds `posted_after` and `posted_before` (B7,
    B31)."""
    filters: dict[str, JSONValue | Decimal] = dict(
        normalized_filters(q, posted_after=posted_after, posted_before=posted_before)
    )
    return filters_hash(filters)


def score_filter_active(q: JobsQuery) -> bool:
    """Whether a score filter is off its default (B15): then only cards with a score are listed."""
    return q.score_min > 0 or q.score_max < 100 or q.coverage_min > 0 or q.confidence != "any"
