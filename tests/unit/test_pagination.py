"""The signed keyset cursors (spec §14.3; plan M3 Task 17, Deviation 3; M4 Task 2, D35, B7, B31):
`base64url(JSON) + "." + base64url(HMAC-SHA256(key, JSON))`, the key `HKDF(session_secret, "cursor")`.

A list cursor (`encode_list_cursor`) carries its list's namespaced sort, so one list never accepts another's
cursor; a job cursor (`encode_job_cursor`) carries its section, the sort value of its last row, the job id,
the ranking generation and the `posted_after` cut-off the filters' hash is computed with.
"""

import base64
import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from decimal import Decimal
from functools import partial
from types import SimpleNamespace
from typing import Any, cast, get_args

import pytest
from jobhunter.api.errors import Problem
from jobhunter.api.pagination import (
    ALERTS_SORT,
    CURSOR_VERSION,
    EVAL_RUNS_SORT,
    GOLDEN_CASES_SORT,
    JOB_HISTORY_SORT,
    RUNS_SORT,
    SCORE_CYCLES_SORT,
    SETTINGS_VERSIONS_SORT,
    JobCursor,
    JobSort,
    ListDir,
    canonical_json,
    cursor_key,
    decode_job_cursor,
    decode_list_cursor,
    encode_job_cursor,
    encode_list_cursor,
    filters_hash,
)
from jobhunter.config import AppConfig
from jobhunter.crypto import hkdf_sha256
from jobhunter.types import JSONValue

KEY = hkdf_sha256(b"\x07" * 32, b"cursor")
FILTERS: dict[str, JSONValue] = {"status": "open"}
LIST_BODY: dict[str, Any] = {
    "v": 2,
    "sort": SETTINGS_VERSIONS_SORT,
    "dir": "desc",
    "section": "all",
    "last_key": [17],
    "filters_hash": filters_hash(FILTERS),
}


def _unb64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def sign(body: object, key: bytes = KEY) -> str:
    """A cursor of any body, signed with `key` as the server signs its own: the forgeries below are bodies
    this server never issues, so only the body's own checks can refuse them."""
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return f"{_b64(raw)}.{_b64(hmac.new(key, raw, hashlib.sha256).digest())}"


def body_of(cursor: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(_unb64(cursor.split(".")[0])))


def refused(decode: Callable[[], object]) -> None:
    with pytest.raises(Problem) as caught:
        decode()
    assert (caught.value.status, caught.value.code) == (400, "invalid_cursor")


def list_cursor(
    *,
    sort: str = SETTINGS_VERSIONS_SORT,
    dir: ListDir = "desc",
    last_key: list[JSONValue] | None = None,
    filters: Mapping[str, JSONValue] = FILTERS,
    key: bytes = KEY,
) -> str:
    return encode_list_cursor(
        sort=sort, dir=dir, last_key=[17] if last_key is None else last_key, filters=filters, key=key
    )


def decode_list(
    cursor: str,
    *,
    sort: str = SETTINGS_VERSIONS_SORT,
    dir: ListDir = "desc",
    filters: Mapping[str, JSONValue] = FILTERS,
    key: bytes = KEY,
) -> list[JSONValue]:
    return decode_list_cursor(cursor, key=key, sort=sort, dir=dir, filters=filters)


# ---- the list cursors --------------------------------------------------------------------------------------


def test_round_trip() -> None:
    assert decode_list(list_cursor()) == [17]
    assert decode_list(list_cursor(last_key=["2026-10-26T12:00:00.000000+00:00", 4])) == [
        "2026-10-26T12:00:00.000000+00:00",
        4,
    ]
    assert CURSOR_VERSION == 2


def test_the_format_is_the_json_and_its_hmac_in_base64url() -> None:
    cursor = list_cursor()
    data, signature = cursor.split(".")
    assert _unb64(data) == canonical_json(LIST_BODY) and json.loads(_unb64(data)) == LIST_BODY
    assert _unb64(signature) == hmac.new(KEY, canonical_json(LIST_BODY), hashlib.sha256).digest()
    assert "=" not in cursor and "+" not in cursor and "/" not in cursor  # safe in a query string


def test_a_bad_signature_is_refused() -> None:
    data, signature = list_cursor().split(".")
    forged = _b64(canonical_json({**LIST_BODY, "last_key": [3]}))
    refused(lambda: decode_list(f"{forged}.{signature}"))
    refused(lambda: decode_list(f"{data}.{_b64(b'\x00' * 32)}"))
    refused(lambda: decode_list(list_cursor(key=hkdf_sha256(b"\x08" * 32, b"cursor"))))


def test_other_filters_another_sort_or_another_direction_is_refused() -> None:
    cursor = list_cursor()
    refused(lambda: decode_list(cursor, filters={"status": "resolved"}))
    refused(lambda: decode_list(cursor, filters={}))
    refused(lambda: decode_list(cursor, sort=RUNS_SORT))
    refused(lambda: decode_list(cursor, dir="asc"))


def test_a_golden_case_cursor_is_refused_by_another_list() -> None:
    """The sorts are namespaced (D35): M3's lists sorted by a bare `id`, so one list's cursor would have paged
    another; the same last key and filters under another list's sort are refused."""
    cursor = list_cursor(sort=GOLDEN_CASES_SORT, dir="asc", last_key=[5], filters={})
    assert decode_list(cursor, sort=GOLDEN_CASES_SORT, dir="asc", filters={}) == [5]
    refused(lambda: decode_list(cursor, sort=ALERTS_SORT, dir="asc", filters={}))


def test_a_cursor_of_the_m3_form_is_refused() -> None:
    """Resolution (c): a cursor issued before the namespaced sorts (`"sort": "version_no"`), or one without a
    `dir`, is 400 `invalid_cursor`; the client starts again from the first page."""
    refused(lambda: decode_list(sign({**LIST_BODY, "sort": "version_no"})))
    refused(lambda: decode_list(sign({k: v for k, v in LIST_BODY.items() if k != "dir"})))
    assert decode_list(sign(LIST_BODY)) == [17]  # the same body, namespaced and with its `dir`, is accepted


def test_another_cursor_version_is_refused() -> None:
    refused(lambda: decode_list(sign({**LIST_BODY, "v": 1})))
    refused(lambda: decode_list(sign({k: v for k, v in LIST_BODY.items() if k != "v"})))


@pytest.mark.parametrize(
    "change",
    [
        {"section": "scored"},
        {"section": None},
        {"last_key": 17},
        {"last_key": None},
        {"last_key": {"id": 17}},
    ],
    ids=["scored-section", "no-section", "scalar-key", "null-key", "object-key"],
)
def test_a_list_cursor_needs_the_all_section_and_a_list_key(change: dict[str, Any]) -> None:
    refused(lambda: decode_list(sign({**LIST_BODY, **change})))


@pytest.mark.parametrize(
    "cursor",
    ["", ".", "abc", "a.b.c", "!!!.???", f"{_b64(b'[1, 2]')}.x", f"{_b64(b'not json')}.x"],
)
def test_malformed_cursors_are_refused(cursor: str) -> None:
    refused(lambda: decode_list(cursor))
    refused(lambda: decode_job_cursor(cursor, key=KEY, sort="score_mid", filters_hash=lambda _: "h"))


def test_a_signed_body_that_is_no_object_is_refused() -> None:
    refused(lambda: decode_list(sign([2])))
    refused(lambda: decode_job_cursor(sign([2]), key=KEY, sort="posted", filters_hash=lambda _: "h"))


def test_filters_hash_is_the_canonical_json_digest() -> None:
    assert filters_hash({"b": 1, "a": [True]}) == filters_hash({"a": [True], "b": 1})
    assert filters_hash({}) == hashlib.sha256(b"{}").hexdigest()
    assert filters_hash({"a": 1}) != filters_hash({"a": True})


def test_decimals_encode_as_fixed_point_strings() -> None:
    """B7: a `Decimal` filter is its fixed-point string, never an exponent and never a float."""
    assert canonical_json(
        {"min_score": Decimal("71.50"), "radius_km": Decimal("1E+2"), "tiny": Decimal("1E-7")}
    ) == (b'{"min_score":"71.50","radius_km":"100","tiny":"0.0000001"}')
    assert canonical_json({"b": Decimal("0.10"), "a": "München"}) == '{"a":"München","b":"0.10"}'.encode()
    assert filters_hash({"min_score": Decimal("71.50")}) == filters_hash({"min_score": "71.50"})
    assert (
        filters_hash({"min_score": Decimal("71.50")}) == hashlib.sha256(b'{"min_score":"71.50"}').hexdigest()
    )
    for not_finite in (Decimal("NaN"), Decimal("Infinity"), float("nan")):
        with pytest.raises(ValueError):
            canonical_json({"min_score": not_finite})


def test_the_list_sorts_are_namespaced_and_distinct() -> None:
    sorts = [
        GOLDEN_CASES_SORT,
        SETTINGS_VERSIONS_SORT,
        RUNS_SORT,
        SCORE_CYCLES_SORT,
        ALERTS_SORT,
        EVAL_RUNS_SORT,
        JOB_HISTORY_SORT,
        *(f"jobs.{sort}" for sort in get_args(JobSort)),
    ]
    assert sorts == [
        "golden_cases.id",
        "settings_versions.version_no",
        "runs.queued_at",
        "score_cycles.id",
        "alerts.id",
        "eval_runs.id",
        "jobs.history",
        "jobs.score_mid",
        "jobs.score_low",
        "jobs.posted",
        "jobs.first_seen",
    ]
    assert len(set(sorts)) == len(sorts)


def test_cursor_key_is_hkdf_of_the_session_secret() -> None:
    secret = b"\x05" * 32
    cfg = cast(
        AppConfig, SimpleNamespace(secret_b64=lambda name: secret if name == "session_secret" else b"")
    )
    assert cursor_key(cfg) == hkdf_sha256(secret, b"cursor") != hkdf_sha256(secret, b"session")


# ---- the job cursors ---------------------------------------------------------------------------------------

POSTED_AFTER = "2026-10-19T12:00:00.000000+00:00"


def job_filters_hash(posted_after: str | None) -> str:
    """The jobs list's filters with the cut-off the cursor carries (B31)."""
    return filters_hash({"remote": True, "posted_after": posted_after})


def job_cursor(c: JobCursor) -> str:
    return encode_job_cursor(c, filters_hash=job_filters_hash(c.posted_after), key=KEY)


def decode_job(
    cursor: str, sort: JobSort, hash_of: Callable[[str | None], str] = job_filters_hash
) -> JobCursor:
    return decode_job_cursor(cursor, key=KEY, sort=sort, filters_hash=hash_of)


ROUND_TRIPS = [
    JobCursor("score_mid", "scored", "71.50", 4711, 1834, None),
    JobCursor("score_mid", "scored", "100.00", 1, 0, POSTED_AFTER),
    JobCursor("score_mid", "unscored", 0.8312000036239624, 12, 7, None),
    JobCursor("score_mid", "unscored", None, 12, 7, POSTED_AFTER),
    JobCursor("score_low", "scored", "0.00", 9, 3, None),
    JobCursor("score_low", "unscored", -1.0, 9, 3, None),
    JobCursor("posted", "all", "2026-10-26T12:00:00.123456+00:00", 5, 2, POSTED_AFTER),
    JobCursor("posted", "all", None, 5, 2, None),
    JobCursor("first_seen", "all", "2026-10-26T12:00:00.000001+00:00", 8, 9, None),
    JobCursor("first_seen", "all", "2026-10-26T12:00:00Z", 8, 9, POSTED_AFTER),
]


@pytest.mark.parametrize("c", ROUND_TRIPS, ids=[f"{c.sort}-{c.section}-{c.last_value}" for c in ROUND_TRIPS])
def test_job_cursor_round_trips_per_sort_and_section(c: JobCursor) -> None:
    cursor = job_cursor(c)
    assert decode_job(cursor, c.sort) == c
    assert body_of(cursor) == {
        "v": 2,
        "sort": f"jobs.{c.sort}",
        "dir": "desc",
        "section": c.section,
        "last_key": [c.last_value],
        "job_id": c.job_id,
        "gen": c.gen,
        "posted_after": c.posted_after,
        "filters_hash": job_filters_hash(c.posted_after),
    }
    for other in get_args(JobSort):
        if other != c.sort:
            refused(partial(decode_job, cursor, other))  # another sort's cursor


SCORED = body_of(job_cursor(ROUND_TRIPS[0]))
UNSCORED = body_of(job_cursor(ROUND_TRIPS[2]))
POSTED = body_of(job_cursor(ROUND_TRIPS[6]))
FIRST_SEEN = body_of(job_cursor(ROUND_TRIPS[8]))


@pytest.mark.parametrize(
    ("sort", "section", "value"),
    [
        ("score_mid", "all", "2026-10-26T12:00:00.000000+00:00"),
        ("score_low", "all", None),
        ("posted", "scored", "71.50"),
        ("posted", "unscored", 0.5),
        ("first_seen", "scored", "71.50"),
        ("first_seen", "unscored", None),
        ("score_mid", "other", "71.50"),
        ("posted", None, "2026-10-26T12:00:00.000000+00:00"),
    ],
)
def test_job_cursor_refuses_a_section_the_sort_lacks(
    sort: JobSort, section: str | None, value: JSONValue
) -> None:
    """`scored` and `unscored` belong to the score sorts, `all` to the date sorts (§14.3); each value would
    be valid in its section, so the section alone is what is refused."""
    forged = sign({**SCORED, "sort": f"jobs.{sort}", "section": section, "last_key": [value]})
    refused(lambda: decode_job(forged, sort))
    with pytest.raises(ValueError):
        JobCursor(sort, cast(Any, section), cast(Any, value), 5, 2, None)  # nor can an encoder make one


@pytest.mark.parametrize(
    ("template", "change"),
    [
        (SCORED, {"last_key": [71.5]}),  # a float score
        (SCORED, {"last_key": ["71.5"]}),
        (SCORED, {"last_key": ["1071.50"]}),
        (SCORED, {"last_key": ["71.50\n"]}),
        (SCORED, {"last_key": ["\u0667\u0661.\u0665\u0660"]}),  # Arabic-Indic digits, not ASCII ones
        (SCORED, {"last_key": [None]}),  # the scored section has a score
        (SCORED, {"gen": -1}),
        (SCORED, {"gen": True}),
        (SCORED, {"gen": 1.0}),
        (SCORED, {"gen": None}),
        (SCORED, {"job_id": 0}),
        (SCORED, {"job_id": True}),
        (SCORED, {"job_id": "4711"}),
        (SCORED, {"last_key": []}),
        (SCORED, {"last_key": ["71.50", 4711]}),
        (SCORED, {"last_key": "71.50"}),
        (SCORED, {"dir": "asc"}),
        (SCORED, {"posted_after": 5}),
        (UNSCORED, {"last_key": [1.5]}),
        (UNSCORED, {"last_key": [-1.0000001]}),
        (UNSCORED, {"last_key": [True]}),
        (UNSCORED, {"last_key": ["0.5"]}),
        (UNSCORED, {"last_key": [[0.5]]}),
        (POSTED, {"last_key": ["2026-13-01T00:00:00+00:00"]}),  # an unparsable time
        (POSTED, {"last_key": ["yesterday"]}),
        (POSTED, {"last_key": ["2026-10-26T12:00:00.123456"]}),  # no zone
        (POSTED, {"last_key": ["2026-10-26T14:00:00.123456+02:00"]}),  # not UTC
        (POSTED, {"last_key": [1761480000]}),
        (FIRST_SEEN, {"last_key": [None]}),  # `first_seen_at` is never null
    ],
)
def test_job_cursor_refuses_a_malformed_last_key(template: dict[str, Any], change: dict[str, Any]) -> None:
    sort = cast(JobSort, template["sort"].removeprefix("jobs."))
    assert decode_job(sign(template), sort).job_id == template["job_id"]  # the template itself is accepted
    refused(lambda: decode_job(sign({**template, **change}), sort))


@pytest.mark.parametrize(
    "fields",
    [
        {"last_value": 71.5},
        {"last_value": "71.5"},
        {"job_id": 0},
        {"job_id": True},
        {"gen": -1},
        {"gen": False},
    ],
)
def test_a_job_cursor_that_breaks_its_rules_cannot_be_made(fields: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "sort": "score_mid",
        "section": "scored",
        "last_value": "71.50",
        "job_id": 4711,
        "gen": 1834,
        "posted_after": None,
        **fields,
    }
    with pytest.raises(ValueError):
        JobCursor(**values)


def test_job_cursor_hash_is_checked_with_its_own_posted_after() -> None:
    """B31: the cut-off of a relative date filter is fixed by the first page and travels in the cursor; the
    hash is computed from the cursor's `posted_after`, never from the time the next page is requested."""
    c = ROUND_TRIPS[6]
    cursor = job_cursor(c)
    asked: list[str | None] = []

    def recording(posted_after: str | None) -> str:
        asked.append(posted_after)
        return job_filters_hash(posted_after)

    assert decode_job(cursor, "posted", recording) == c
    assert asked == [POSTED_AFTER]
    moved = sign({**body_of(cursor), "posted_after": "2026-10-20T12:00:00.000000+00:00"})
    refused(lambda: decode_job(moved, "posted"))  # another cut-off than the hash was made with
    other_filters = lambda pa: filters_hash({"remote": False, "posted_after": pa})  # noqa: E731
    refused(lambda: decode_job(cursor, "posted", other_filters))
    refused(lambda: decode_job(sign({**body_of(cursor), "posted_after": None}), "posted"))
