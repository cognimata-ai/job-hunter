"""The signed keyset cursors of the paged lists (spec §14.3; plan M3 Task 17, Deviation 3; M4 D35, B7, B31).

A cursor is `base64url(JSON) + "." + base64url(HMAC-SHA256(key, JSON))`, base64url without padding, where
JSON is the body's canonical JSON (`canonical_json`: sorted keys, no whitespace) and the key is
`HKDF(session_secret, "cursor")` (`cursor_key`). Every body holds the cursor version `"v": 2`, the list's
`sort` and `dir`, a `section`, the keyset position `last_key`, and `filters_hash`, the hash of the request's
filters (`filters_hash`), so a cursor is valid only for the list and the filters it was made for.

The sorts are namespaced by their list (D35): `GOLDEN_CASES_SORT` is `golden_cases.id` and `ALERTS_SORT`
`alerts.id`, so one list never takes another's cursor even where both sort by an id; the job sorts are
`"jobs." + JobSort`.

- A list cursor (`encode_list_cursor` / `decode_list_cursor`) has the section `all`, a direction and a
  `last_key` list whose shape the route checks. A cursor of M3's form (a bare `sort` such as `version_no`) is
  refused: the client starts again from the first page.
- A job cursor (`JobCursor`, `encode_job_cursor` / `decode_job_cursor`) is `desc`, in the `scored` or
  `unscored` section of a score sort or the `all` section of a date sort, with the last row's sort value, its
  job id, the ranking generation `gen` of the first page and `posted_after`, the absolute cut-off of the
  request's relative date filter, fixed by the first page. The filters' hash is computed with that cut-off,
  never with the time of the next request (B31).

A decoder refuses with 400 `invalid_cursor` anything but a cursor this server signed for that list and those
filters: a malformed one, a bad signature, another cursor version, sort, direction, section or
`filters_hash`, a `last_key` of the wrong shape. Decimals in filters hash as their fixed-point strings (B7).
"""

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Final, Literal

from jobhunter.api.errors import Problem
from jobhunter.config import AppConfig
from jobhunter.crypto import hkdf_sha256
from jobhunter.types import JSONValue

CURSOR_VERSION: Final = 2
SECTION_ALL: Final = "all"
_KEY_INFO: Final = b"cursor"
_PART: Final = re.compile(r"[A-Za-z0-9_-]+")

GOLDEN_CASES_SORT: Final = "golden_cases.id"
SETTINGS_VERSIONS_SORT: Final = "settings_versions.version_no"
RUNS_SORT: Final = "runs.queued_at"
SCORE_CYCLES_SORT: Final = "score_cycles.id"
ALERTS_SORT: Final = "alerts.id"
EVAL_RUNS_SORT: Final = "eval_runs.id"
JOB_HISTORY_SORT: Final = "jobs.history"

ListDir = Literal["asc", "desc"]
JobSort = Literal["score_mid", "score_low", "posted", "first_seen"]
JobSection = Literal["scored", "unscored", "all"]

_SECTIONS: Final[Mapping[str, JobSection]] = MappingProxyType(
    {"scored": "scored", "unscored": "unscored", "all": "all"}
)
# §14.3: the score sorts have a scored and an unscored section, the date sorts one section.
_SORT_SECTIONS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "score_mid": frozenset({"scored", "unscored"}),
        "score_low": frozenset({"scored", "unscored"}),
        "posted": frozenset({SECTION_ALL}),
        "first_seen": frozenset({SECTION_ALL}),
    }
)
_SCORE: Final = re.compile(r"\d{1,3}\.\d{2}", re.ASCII)  # `^\d{1,3}\.\d{2}$`, ASCII digits, no newline


def canonical_json(value: Mapping[str, JSONValue | Decimal]) -> bytes:
    """The UTF-8 bytes of `value` as JSON with sorted keys and no insignificant whitespace, a `Decimal` as its
    fixed-point string (`format(d, "f")`: `Decimal("1E+2")` is `"100"`). A NaN or an infinity, float or
    `Decimal`, raises `ValueError`."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_fixed_point,
    ).encode("utf-8")


def _fixed_point(value: object) -> str:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"not a finite decimal: {value}")
        return format(value, "f")
    raise TypeError(f"not JSON: {type(value).__name__}")


def cursor_key(cfg: AppConfig) -> bytes:
    """The cursors' HMAC key: `HKDF(session_secret, "cursor")`, independent of the session pepper."""
    return hkdf_sha256(cfg.secret_b64("session_secret"), _KEY_INFO)


def filters_hash(filters: Mapping[str, JSONValue | Decimal]) -> str:
    """The hex SHA-256 of the canonical JSON of a list request's filters (`{}` for none)."""
    return hashlib.sha256(canonical_json(filters)).hexdigest()


def invalid_cursor() -> Problem:
    return Problem(
        400,
        "invalid_cursor",
        "Invalid cursor",
        detail="the cursor is not one this list issued for these filters: request the first page again",
    )


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(part: str) -> bytes:
    if _PART.fullmatch(part) is None:
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _signature(key: bytes, raw: bytes) -> bytes:
    return hmac.new(key, raw, hashlib.sha256).digest()


def _encode(body: Mapping[str, JSONValue], *, key: bytes) -> str:
    raw = canonical_json(body)
    return f"{_b64(raw)}.{_b64(_signature(key, raw))}"


def _decode(cursor: str, *, key: bytes) -> Mapping[str, JSONValue]:
    """The read-only body of a cursor this server signed, of this cursor version; else 400 `invalid_cursor`.
    The caller checks the rest of the body."""
    data, dot, signature = cursor.partition(".")
    try:
        if not dot:
            raise ValueError("no signature")
        raw, mac = _unb64(data), _unb64(signature)
    except (ValueError, binascii.Error):
        raise invalid_cursor() from None
    if not hmac.compare_digest(mac, _signature(key, raw)):
        raise invalid_cursor()
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):  # signed by this server, so never in practice
        raise invalid_cursor() from None
    if not isinstance(body, dict) or body.get("v") != CURSOR_VERSION:
        raise invalid_cursor()
    return MappingProxyType(body)


def encode_list_cursor(
    *, sort: str, dir: ListDir, last_key: Sequence[JSONValue], filters: Mapping[str, JSONValue], key: bytes
) -> str:
    """The signed cursor of a list page that ends at `last_key`, for that list's namespaced `sort`."""
    return _encode(
        {
            "v": CURSOR_VERSION,
            "sort": sort,
            "dir": dir,
            "section": SECTION_ALL,
            "last_key": list(last_key),
            "filters_hash": filters_hash(filters),
        },
        key=key,
    )


def decode_list_cursor(
    cursor: str, *, key: bytes, sort: str, dir: ListDir, filters: Mapping[str, JSONValue]
) -> list[JSONValue]:
    """The `last_key` of a cursor `encode_list_cursor` made for this sort, direction and these filters; else
    400 `invalid_cursor`. The caller checks the key's shape."""
    body = _decode(cursor, key=key)
    last_key = body.get("last_key")
    if (
        body.get("sort") != sort
        or body.get("dir") != dir
        or body.get("section") != SECTION_ALL
        or body.get("filters_hash") != filters_hash(filters)
        or not isinstance(last_key, list)
    ):
        raise invalid_cursor()
    return list(last_key)


@dataclass(frozen=True)
class JobCursor:
    """The position after the last row of a Jobs list page (§14.3).

    `last_value` is the sort value of that row: in the `scored` section its score as a fixed-point string
    (`"71.50"`); in the `unscored` section its similarity (a number in [-1, 1]) or `None`; under the date
    sorts (section `all`) its time as an ISO-8601 UTC string with microseconds, or `None` for `posted` (an
    unknown posting date sorts last). `job_id` is that row's job id (at least 1), `gen` the ranking generation
    of the first page (at least 0), `posted_after` the cut-off the filters' hash is computed with: `None`, or
    an ISO-8601 UTC time as `last_value` is under the date sorts.

    A cursor that breaks these rules (a section its sort lacks, a value of the wrong shape, a `bool` for a
    number) cannot be made: `ValueError`.
    """

    sort: JobSort
    section: JobSection
    last_value: str | float | None
    job_id: int
    gen: int
    posted_after: str | None

    def __post_init__(self) -> None:
        if not _holds(self):
            raise ValueError("not a job cursor of its sort and section")


def _count(value: object, minimum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _utc_time(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value).utcoffset() == timedelta(0)
    except ValueError:
        return False


def _holds(c: JobCursor) -> bool:
    """Whether `c` keeps the rules of `JobCursor`."""
    if c.section not in _SORT_SECTIONS.get(c.sort, frozenset()):
        return False
    if not (_count(c.job_id, 1) and _count(c.gen, 0)):
        return False
    if not (c.posted_after is None or _utc_time(c.posted_after)):
        return False
    value = c.last_value
    match c.section:
        case "scored":
            return isinstance(value, str) and _SCORE.fullmatch(value) is not None
        case "unscored":
            if value is None:
                return True
            return isinstance(value, int | float) and not isinstance(value, bool) and -1 <= value <= 1
        case _:
            return (value is None and c.sort == "posted") or _utc_time(value)


def encode_job_cursor(c: JobCursor, *, filters_hash: str, key: bytes) -> str:
    """The signed cursor of a Jobs list page that ends at `c`; `filters_hash` is the hash of the request's
    filters with `c.posted_after` as the cut-off."""
    return _encode(
        {
            "v": CURSOR_VERSION,
            "sort": f"jobs.{c.sort}",
            "dir": "desc",
            "section": c.section,
            "last_key": [c.last_value],
            "job_id": c.job_id,
            "gen": c.gen,
            "posted_after": c.posted_after,
            "filters_hash": filters_hash,
        },
        key=key,
    )


def decode_job_cursor(
    cursor: str, *, key: bytes, sort: JobSort, filters_hash: Callable[[str | None], str]
) -> JobCursor:
    """The position of a cursor `encode_job_cursor` made for this sort and the request's filters, whose hash
    `filters_hash` computes from the cursor's own `posted_after` (B31); else 400 `invalid_cursor`."""
    body = _decode(cursor, key=key)
    section, last_key, posted_after = body.get("section"), body.get("last_key"), body.get("posted_after")
    job_id, gen = body.get("job_id"), body.get("gen")
    if (
        body.get("sort") != f"jobs.{sort}"
        or body.get("dir") != "desc"
        or not isinstance(section, str)
        or section not in _SECTIONS
        or not isinstance(last_key, list)
        or len(last_key) != 1
        or not isinstance(job_id, int)
        or not isinstance(gen, int)
        or not (posted_after is None or (isinstance(posted_after, str) and _utc_time(posted_after)))
        or body.get("filters_hash") != filters_hash(posted_after)  # asked only with a valid cut-off
    ):
        raise invalid_cursor()
    last_value = last_key[0]
    if isinstance(last_value, list | dict):
        raise invalid_cursor()
    try:  # `JobCursor` checks the rest: the section of the sort, the value's shape, the counts, no `bool`
        return JobCursor(
            sort=sort,
            section=_SECTIONS[section],
            last_value=last_value,
            job_id=job_id,
            gen=gen,
            posted_after=posted_after,
        )
    except ValueError:
        raise invalid_cursor() from None
