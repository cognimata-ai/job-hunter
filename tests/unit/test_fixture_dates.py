"""Fixture posting dates relative to the clock (M4 Task 18): in fake mode (`fixture_transport` with a clock)
every posting date an adapter reads (`fixtures.DATE_FIELDS`) is served moved by the whole days between the
case's reference and the clock, in its own encoding and never past the clock; without a clock (the contract
tests) every body is served as recorded.

The values are read here with a reader of the tests' own (`_values`), not with the module's walker, and every
adapter parses each dated case twice, recorded and shifted (Task 18 resolution (b)): its posting dates move by
exactly the shift.
"""

import json
import re
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

import httpx
import pytest
from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError, fromstring
from jobhunter.sources import fixtures
from jobhunter.sources.adzuna import AdzunaAdapter
from jobhunter.sources.arbeitnow import ArbeitnowAdapter
from jobhunter.sources.base import RawPage, RequestSpec, SourceAdapter, SourceError
from jobhunter.sources.fixtures import (
    DATE_FIELDS,
    FIXTURE_REFERENCE,
    ROUTES,
    DateField,
    fixture_transport,
    load_case,
    shift_dates,
)
from jobhunter.sources.jsearch import JSearchAdapter
from jobhunter.sources.parsing.dates import parse_iso
from jobhunter.sources.watchlist_patterns import AtsType

from tests.support.sources import adapter_for_fixture

FIXTURES: Final = Path("tests/contract/fixtures")
DAYS: Final = 15
LATER: Final = timedelta(days=DAYS, hours=3)  # a clock 15 days and 3 hours after the case's reference
# One request per prefix of `DATE_FIELDS` that the fixture transport answers with a dated case (`ROUTES`).
SERVED: Final = {
    "jsearch/": (
        "https://jsearch.p.rapidapi.com/search-v2?query=x&date_posted=week",
        "https://jsearch.p.rapidapi.com/search-v2?query=x&cursor=c",
    ),
    "adzuna/": ("https://api.adzuna.com/v1/api/jobs/de/search/1?what=x",),
    "arbeitnow/": (
        "https://www.arbeitnow.com/api/job-board-api?page=1",
        "https://www.arbeitnow.com/api/job-board-api?page=2",
    ),
    "ats/greenhouse_": ("https://boards-api.greenhouse.io/v1/boards/getyourguide/jobs?content=true",),
    "ats/lever_": (
        "https://api.lever.co/v0/postings/leverdemo?skip=0&limit=100",
        "https://api.eu.lever.co/v0/postings/leverdemo?skip=0&limit=100",
    ),
    "ats/ashby_": ("https://api.ashbyhq.com/posting-api/job-board/n8n?includeCompensation=true",),
    "ats/personio_": ("https://everphone.jobs.personio.de/xml",),
    "ats/recruitee_": ("https://bunq.recruitee.com/api/offers/",),
    "ats/smartrecruiters_": (
        "https://api.smartrecruiters.com/v1/companies/DeliveryHero/postings?limit=100&offset=0",
        "https://api.smartrecruiters.com/v1/companies/DeliveryHero/postings/744000156183719",
        "https://api.smartrecruiters.com/v1/companies/DeliveryHero/postings/744000156094379",
    ),
}
EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
PER_DAY: Final = {"unix_s": 86_400, "unix_ms": 86_400_000}
# What an adapter parses from a body: each outcome's index, item time and posted date, or its error's code.
Dates = list[tuple[int, datetime | None, datetime | None]] | str


def _case_of(url: str) -> str:
    request = httpx.Request("GET", url)
    return next(route.file for route in ROUTES if route.matches(request))


def _kind(case: str) -> str:
    return next(
        suffix for suffix in (".json", ".xml", ".html", ".txt") if (FIXTURES / f"{case}{suffix}").is_file()
    )


def _json_values(node: Any, steps: list[str]) -> Iterator[Any]:
    """The values at a `DATE_FIELDS` JSON path: `a.b[].c` (`[]`: each element of the array)."""
    if not steps:
        yield node
        return
    step, rest = steps[0], steps[1:]
    name, each = step.removesuffix("[]"), step.endswith("[]")
    if name:
        if not isinstance(node, dict) or name not in node:
            return
        node = node[name]
    if each:
        for item in node if isinstance(node, list) else []:
            yield from _json_values(item, rest)
    else:
        yield from _json_values(node, rest)


def _values(body: bytes, kind: str, field: DateField) -> list[Any]:
    """Every value of `field` in a body: a JSON path, or for XML an ElementTree path below the root."""
    if kind == ".xml":
        try:
            root = fromstring(body, forbid_dtd=True)
        except (ParseError, DefusedXmlException):  # a DTD (`personio_entity`): the adapter refuses it too
            return []
        return [element.text for element in root.findall(field.path)]
    try:
        doc = json.loads(body)
    except ValueError:
        return []
    return list(_json_values(doc, field.path.split(".")))


def _fields(case: str) -> tuple[DateField, ...]:
    return next((f for prefix, f in DATE_FIELDS.items() if case.startswith(prefix)), ())


def _dated_cases() -> list[str]:
    """Every committed case with at least one value of its prefix's `DATE_FIELDS`."""
    cases = []
    for path in sorted(FIXTURES.rglob("*")):
        relative = path.relative_to(FIXTURES).as_posix()
        if path.suffix not in (".json", ".xml") or relative.endswith((".headers.json", ".meta.json")):
            continue
        case = relative.removesuffix(path.suffix)
        body = path.read_bytes()
        if any(value is not None for f in _fields(case) for value in _values(body, path.suffix, f)):
            cases.append(case)
    return cases


DATED: Final = _dated_cases()
# Every committed body of the sources `DATE_FIELDS` covers, found without the table: the per-adapter check
# parses each one, so a field the table missed would show as a date that did not move.
ADAPTER_CASES: Final = sorted(
    path.relative_to(FIXTURES).as_posix().removesuffix(path.suffix)
    for path in FIXTURES.rglob("*")
    if path.suffix in (".json", ".xml")
    and not path.name.endswith((".headers.json", ".meta.json"))
    and path.relative_to(FIXTURES).parts[0] in ("jsearch", "adzuna", "arbeitnow", "ats")
)


def _later(before: Any, encoding: str, days: int) -> Any:
    """`before` moved by `days` whole days in its own encoding (the tests' own arithmetic)."""
    if encoding in PER_DAY:
        return before + days * PER_DAY[encoding]
    moved = (date.fromisoformat(before[:10]) + timedelta(days=days)).isoformat()
    return moved + before[10:]


def _instant(value: Any, encoding: str) -> datetime:
    if encoding == "unix_s":
        return EPOCH + timedelta(seconds=value)
    if encoding == "unix_ms":
        return EPOCH + timedelta(milliseconds=value)
    parsed = parse_iso(value, precision="exact")
    assert parsed is not None, value
    return parsed.at


def _served(url: str, now: datetime | None) -> httpx.Response:
    """The fixture transport's answer to `url` on a clock that reads `now` (no clock for `None`)."""
    transport = fixture_transport(FIXTURES, clock=None if now is None else lambda: now)
    return transport.handle_request(httpx.Request("GET", url))


# ---- the shift on the fixture transport --------------------------------------------------------------------


def test_every_dated_fixture_field_is_shifted_by_whole_days() -> None:
    assert set(SERVED) == set(DATE_FIELDS)  # every prefix is served by one request at least
    for prefix, urls in SERVED.items():
        moved = 0
        for url in urls:
            case = _case_of(url)
            assert case.startswith(prefix) and case in DATED, (url, case)
            now = fixtures.case_reference(FIXTURES, case) + LATER
            response = _served(url, now)
            status, headers, recorded = load_case(FIXTURES, case)
            served, kind = response.content, _kind(case)
            served_headers = {k: v for k, v in response.headers.items() if k != "content-length"}  # httpx's
            assert (response.status_code, served_headers) == (status, headers)  # never the headers
            for field in DATE_FIELDS[prefix]:
                before, after = _values(recorded, kind, field), _values(served, kind, field)
                assert len(after) == len(before), (case, field)
                for old, new in zip(before, after, strict=True):
                    assert type(new) is type(old), (case, old, new)
                    assert new == _later(old, field.encoding, DAYS), (case, old, new)
                    shift = _instant(new, field.encoding) - _instant(old, field.encoding)
                    assert shift == timedelta(days=DAYS), (case, old, new)
                    moved += 1
            # nothing else moved: the body keeps its format and length, and its other values
            assert len(served) == len(recorded) and served != recorded, case
            if kind == ".json":
                assert _blank(json.loads(served), prefix) == _blank(json.loads(recorded), prefix), case
            else:
                tag = re.compile(rb"<createdAt>[^<]*</createdAt>")
                assert tag.sub(b"", served) == tag.sub(b"", recorded), case
        assert moved > 0, prefix


def _blank(doc: Any, prefix: str) -> Any:
    """`doc` with every value of `prefix`'s fields replaced by `None` (the tests' own walk)."""
    for field in DATE_FIELDS[prefix]:
        *parents, leaf = field.path.split(".")
        for holder in _json_values(doc, parents) if parents else [doc]:
            if isinstance(holder, dict) and leaf in holder:
                holder[leaf] = None
    return doc


def test_without_a_clock_the_bodies_are_as_recorded() -> None:
    """The contract tests' transport (no clock) serves every routed case byte for byte; so does a clock in the
    case's own reference day (a shift of 0 days)."""
    for urls in SERVED.values():
        for url in urls:
            case = _case_of(url)
            recorded = load_case(FIXTURES, case)[2]
            assert _served(url, None).content == recorded, case
            same_day = fixtures.case_reference(FIXTURES, case) + timedelta(hours=23, minutes=59)
            assert _served(url, same_day).content == recorded, case


def test_a_body_without_a_listed_field_is_served_as_recorded() -> None:
    """SerpApi (its relative `2 days ago` chips: no field), the 404 bodies, the end pages and the HTML and
    truncated bodies are served as recorded on any clock, and never re-serialised."""
    later = FIXTURE_REFERENCE + LATER
    for url in (
        "https://serpapi.com/search.json?engine=google_jobs&q=x",
        "https://boards-api.greenhouse.io/v1/boards/nobody/jobs",
        "https://api.eu.lever.co/v0/postings/leverdemo?skip=100",
        "https://nobody.jobs.personio.de/xml",
        "https://api.adzuna.com/v1/api/jobs/de/search/2",
        "https://www.arbeitnow.com/api/job-board-api?page=3",
        "https://jsearch.p.rapidapi.com/search",
    ):
        case = _case_of(url)
        assert _served(url, later).content == load_case(FIXTURES, case)[2], case
    for case, kind in (("arbeitnow/page_truncated", ".txt"), ("jsearch/search_v2_html", ".html")):
        body = load_case(FIXTURES, case)[2]
        shifted = shift_dates(body, case=case, kind=kind, now=later, reference=FIXTURE_REFERENCE)
        assert shifted is body
    entity = load_case(FIXTURES, "ats/personio_entity")[2]  # a DTD: the adapter refuses it, so does the shift
    case = "ats/personio_entity"
    assert shift_dates(entity, case=case, kind=".xml", now=later, reference=FIXTURE_REFERENCE) is entity


def test_every_dated_case_has_a_reference() -> None:
    """Each dated case's reference is its recording's `fetched_at` (`<case>.meta.json`) or, for a derived
    case, `FIXTURE_REFERENCE`; no posting date of a case is after its reference, so the committed fixtures are
    never clamped (a posting is never moved past the clock)."""
    assert len(DATED) >= len(SERVED)
    for case in DATED:
        meta = FIXTURES / f"{case}.meta.json"
        reference = fixtures.case_reference(FIXTURES, case)
        if meta.is_file():
            assert reference == datetime.fromisoformat(json.loads(meta.read_text("utf-8"))["fetched_at"])
        else:
            assert reference == FIXTURE_REFERENCE, case
        assert reference.tzinfo is not None
        body, kind = (FIXTURES / f"{case}{_kind(case)}").read_bytes(), _kind(case)
        for field in _fields(case):
            for value in _values(body, kind, field):
                assert _instant(value, field.encoding) <= reference, (case, value)


# ---- the clamp, the negative shift and the encodings -------------------------------------------------------

REF: Final = datetime(2026, 10, 18, 21, 0, tzinfo=UTC)
CLOCK: Final = REF + timedelta(days=1, hours=1, seconds=30, microseconds=123_456)  # a shift of 1 day
TWO_HOURS: Final = timedelta(hours=2)  # a posting 2 h after the reference: 1 day later is past the clock


def _shift_one(case: str, doc: Any, now: datetime = CLOCK) -> Any:
    body = json.dumps(doc).encode()
    return json.loads(shift_dates(body, case=case, kind=".json", now=now, reference=REF))


@pytest.mark.parametrize(
    ("case", "items", "key", "after_ref", "clamped"),
    [
        ("adzuna/x", "results", "created", "2026-10-18T23:00:00Z", "2026-10-19T22:00:30Z"),
        (
            "ats/greenhouse_x",
            "jobs",
            "first_published",
            "2026-10-18T19:00:00-04:00",
            "2026-10-19T18:00:30-04:00",
        ),
        (
            "ats/ashby_x",
            "jobs",
            "publishedAt",
            "2026-10-18T23:00:00.000+00:00",
            "2026-10-19T22:00:30.123+00:00",
        ),
        ("ats/recruitee_x", "offers", "published_at", "2026-10-18 23:00:00 UTC", "2026-10-19 22:00:30 UTC"),
        (
            "ats/smartrecruiters_x",
            "content",
            "releasedDate",
            "2026-10-18T23:00:00.000Z",
            "2026-10-19T22:00:30.123Z",
        ),
    ],
)
def test_a_shifted_date_never_passes_now(
    case: str, items: str, key: str, after_ref: str, clamped: str
) -> None:
    """A posting after its reference would pass the clock once moved: it is the clock instead, in the value's
    own offset, separator, precision and zone (truncated, so never after it); an earlier one moves a day."""
    doc = {items: [{key: after_ref}, {key: "2026-10-16T08:00:00Z"}, {key: None}]}
    shifted = _shift_one(case, doc)[items]
    assert [item[key] for item in shifted] == [clamped, "2026-10-17T08:00:00Z", None]
    clamp_at = parse_iso(clamped, precision="exact")
    assert clamp_at is not None and clamp_at.at <= CLOCK and CLOCK - clamp_at.at < timedelta(seconds=1)


def test_a_shifted_epoch_never_passes_now() -> None:
    stamp = int((REF + TWO_HOURS).timestamp())
    jsearch = _shift_one("jsearch/x", {"data": {"jobs": [{"job_posted_at_timestamp": stamp}, {"x": 1}]}})
    lever = _shift_one("ats/lever_x", [{"createdAt": stamp * 1000}, {"createdAt": stamp * 1000 - 86_400_000}])
    assert jsearch["data"]["jobs"] == [{"job_posted_at_timestamp": int(CLOCK.timestamp())}, {"x": 1}]
    assert [item["createdAt"] for item in lever] == [int(CLOCK.timestamp() * 1000), stamp * 1000]


def test_a_clock_before_the_reference_moves_the_dates_back() -> None:
    """A clock 2 days 1 h before the reference is a shift of -3 days (whole days, rounded down)."""
    before = REF - timedelta(days=2, hours=1)
    doc = {"data": [{"created_at": int(REF.timestamp()) - 3600}]}
    shifted = _shift_one("arbeitnow/x", doc, now=before)
    assert shifted["data"][0]["created_at"] == int(REF.timestamp()) - 3600 - 3 * 86_400


def test_a_date_field_stays_a_date(monkeypatch: pytest.MonkeyPatch) -> None:
    """The `date` encoding (`YYYY-MM-DD`): moved by whole days, and clamped to the clock's UTC date."""
    monkeypatch.setattr(fixtures, "DATE_FIELDS", {"x/": (DateField("items[].day", "date"),)})
    doc = {"items": [{"day": "2026-10-16"}, {"day": "2026-10-19"}, {"day": "not a date"}]}
    shifted = _shift_one("x/a", doc)
    assert [item["day"] for item in shifted["items"]] == ["2026-10-17", "2026-10-19", "not a date"]


def test_the_personio_xml_is_shifted_in_place() -> None:
    """The XML path `position/createdAt` (an ElementTree path below the root): the element's text moves, and
    every other byte of the body is kept."""
    body = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n<workzag-jobs>\n<position>\n  <name>A &amp; B</name>\n'
        b"  <createdAt>2026-10-16T13:22:48+00:00</createdAt>\n</position>\n<position>\n"
        b"  <createdAt>2026-10-18T23:00:00+00:00</createdAt>\n</position>\n</workzag-jobs>\n"
    )
    shifted = shift_dates(body, case="ats/personio_x", kind=".xml", now=CLOCK, reference=REF)
    assert shifted == body.replace(b"2026-10-16T13:22:48", b"2026-10-17T13:22:48").replace(
        b"2026-10-18T23:00:00", b"2026-10-19T22:00:30"
    )


def test_an_xml_body_whose_dates_are_not_all_at_the_path_is_refused() -> None:
    """A `<createdAt>` outside `position/createdAt` cannot be told apart lexically: a loud error, never a
    silent partial shift."""
    body = (
        b"<workzag-jobs><position><createdAt>2026-10-16T13:22:48+00:00</createdAt></position>"
        b"<createdAt>x</createdAt></workzag-jobs>"
    )
    with pytest.raises(ValueError, match="position/createdAt"):
        shift_dates(body, case="ats/personio_x", kind=".xml", now=CLOCK, reference=REF)


# ---- every adapter reads the shifted dates (Task 18 resolution (b)) ----------------------------------------

SPEC: Final = RequestSpec(
    source_instance_id=1,
    operation="list",
    cell_id=7,
    page_no=1,
    params={},
    cursor_in=None,
    detail_native_id=None,
    parent_fetch_request_id=None,
    window=None,
    request_class="free",
)


def _adapter(case: str, now: datetime) -> SourceAdapter:
    client = httpx.AsyncClient()
    source, name = case.split("/", 1)
    if source == "jsearch":
        return JSearchAdapter(client, api_key=b"k", instance_id=1, clock=lambda: now)
    if source == "adzuna":
        return AdzunaAdapter(client, app_id=b"i", app_key=b"k", instance_id=1, clock=lambda: now)
    if source == "arbeitnow":
        return ArbeitnowAdapter(client, instance_id=1, clock=lambda: now)
    return adapter_for_fixture(cast(AtsType, name.split("_", 1)[0]), name)


def _spec(case: str, body: bytes) -> RequestSpec:
    doc = json.loads(body) if _kind(case) == ".json" else None
    if case.startswith("ats/smartrecruiters_") and isinstance(doc, dict):
        if "id" in doc:  # a detail page: the posting asked for
            return replace(SPEC, operation="detail", detail_native_id=str(doc["id"]))
        if "offset" in doc:  # a list page: the offset it echoes
            return replace(SPEC, params={"offset": str(doc["offset"])})
    return SPEC


def _dates(case: str, body: bytes, now: datetime) -> Dates:
    """Each outcome's index, item time and posted date, as the case's real adapter parses `body`; or the code
    of the contract violation it raises (a derived drift case)."""
    status, headers, _ = load_case(FIXTURES, case)
    page = RawPage(_spec(case, body), status, headers, body, now)
    try:
        outcomes = _adapter(case, now).parse(page)
    except SourceError as exc:  # a contract violation or the provider's error page
        return f"{exc.kind}:{exc.code}"
    out = []
    for outcome in outcomes:
        posted = getattr(getattr(outcome, "normalized", None), "posted_at", None)
        out.append((outcome.item_index, outcome.item_ts, posted))
    return out


def _parsed_dates(case: str) -> tuple[Dates, Dates]:
    """The case's dates as its adapter parses them, recorded and shifted by `LATER` (each a list, or the code
    of the error the adapter raises)."""
    now = fixtures.case_reference(FIXTURES, case) + LATER
    recorded = load_case(FIXTURES, case)[2]
    shifted = shift_dates(
        recorded, case=case, kind=_kind(case), now=now, reference=fixtures.case_reference(FIXTURES, case)
    )
    return _dates(case, recorded, now), _dates(case, shifted, now)


def test_the_per_adapter_check_sees_dates_of_every_prefix() -> None:
    """Not vacuous: for every prefix of `DATE_FIELDS` the adapters parse dates from some committed case; and
    complete: every case an adapter parses a date from holds a value of the table (`DATED`). The cases of
    `DATED` without a parsed date are the ones the adapter refuses whole (a derived drift case)."""
    parsed = {case: _parsed_dates(case)[0] for case in ADAPTER_CASES}
    dated = {case for case, dates in parsed.items() if _flat(dates)}
    assert dated == {case for case in DATED if not isinstance(parsed[case], str)}
    assert {case for case in DATED if isinstance(parsed[case], str)} == {"jsearch/search_v2_drift"}
    assert {prefix for prefix in DATE_FIELDS if any(case.startswith(prefix) for case in dated)} == set(
        DATE_FIELDS
    )


def _flat(dates: Dates) -> list[datetime]:
    return (
        []
        if isinstance(dates, str)
        else [d for _, ts, posted in dates for d in (ts, posted) if d is not None]
    )


@pytest.mark.parametrize("case", ADAPTER_CASES)
def test_every_adapter_reads_the_shifted_dates_n_days_later(case: str) -> None:
    now = fixtures.case_reference(FIXTURES, case) + LATER
    before, after = _parsed_dates(case)
    if isinstance(before, str):  # the adapter refuses the recording: it refuses the shifted body alike
        assert after == before, case
        return
    assert not isinstance(after, str) and len(after) == len(before), case
    for (index, old_ts, old_posted), (index_after, new_ts, new_posted) in zip(before, after, strict=True):
        assert index_after == index
        for old, new in ((old_ts, new_ts), (old_posted, new_posted)):
            if old is None:
                assert new is None
            elif old + timedelta(days=DAYS) <= now:
                assert new == old + timedelta(days=DAYS), (case, index)
            else:  # clamped at the clock
                assert new is not None and new <= now and now - new < timedelta(seconds=1)
