"""The fixture transport of fake mode (`JOBHUNTER_FAKE_SOURCES`, spec §18.4; plan Global Constraints).

`fixture_transport` serves every request from the scrubbed recordings in a fixtures directory, through the
real adapter code: the first `ROUTES` entry that matches the request's host, path and query names the case
`<file>`, whose body is `<file>.json` (or `.xml`, `.html`, `.txt`: the derived bodies that are not JSON, an
HTML error page and a truncated page) and whose status and headers are those of `<file>.headers.json`
(`{"status": …, "headers": {…}}`; without it 200 and the content type of the body's kind). `load_case` reads a
case the same way. A request that no route matches is answered 404 `{"error": "no fixture"}`; a route whose
body file is missing is a broken fixture set and raises `FileNotFoundError`.

With `log_path` (from `JOBHUNTER_FIXTURE_LOG`, tests only) every served request appends one JSON line
`{"pid", "method", "host", "path", "query", "case", "status"}`: the instrument the crash tests use to count
the real sends across worker processes. Each line is one `write` on an `O_APPEND` descriptor, so the lines of
concurrent processes never interleave. The logged query has no credential parameter (`SECRET_PARAMS`).

With `clock` (the fake mode of `http.configure` passes `utcnow`), the SerpApi account read (`ACCOUNT_CASE`)
is served with its plan renewing `ACCOUNT_RENEWAL_AHEAD` after `clock()`'s date (`renewed_account`), so the
recorded account describes a current window whatever the date, and every other 2xx body has its posting dates
moved with the clock (`shift_dates`, M4 Task 18); without one every case is served as recorded (the contract
tests, R4).

**The shift** (`shift_dates`). `DATE_FIELDS` names, by fixture prefix, every field an adapter reads as a
posting date (`DateField`: its path and its encoding; SerpApi's relative `2 days ago` chips are no field). A
case's reference (`case_reference`) is its recording's `fetched_at` (`<case>.meta.json`), or
`FIXTURE_REFERENCE` for a derived case without one. At `now`, every listed value moves by the whole days
`floor((now - reference) / 1 day)` (negative before the reference), so a body changes at most once a day, at
its reference's time of day, and the ages the recording saw stay what they were; a value that would then be
after `now` is `now` itself (none of the committed fixtures has one). Each value
keeps its encoding exactly: an ISO string its offset, separator, precision and zone word (only its date part
moves; a clamped one is `now` in its own offset, truncated to its own precision), a `date` stays `YYYY-MM-DD`,
unix seconds and milliseconds stay numbers of their type. A value the adapter would not read as a date (a
`null`, a string that is no ISO date) is left as it is, and a value the adapter reads whose form the shift
cannot keep raises `ValueError`, so no ad ages out unnoticed. A body with none of its prefix's values moved
is served byte for byte; a JSON body with one is written back in the recordings' own form (two-space indent,
UTF-8 as is, the final newline kept: the same bytes but for the dates); an XML body is changed in place, the
lexical `<tag>` texts checked against the parsed path first. Headers, metas and non-2xx bodies are never
touched: the recorded `Date` header stays as recorded (it dates SerpApi's relative chips and Arbeitnow's
fill instants, which therefore do not move).
"""

import json
import os
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal

import httpx
from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError, fromstring

from jobhunter.sources.base import public_query
from jobhunter.sources.parsing.dates import parse_iso
from jobhunter.types import JSONValue, get_int, get_object, get_str

_CONTENT_TYPES: Final = MappingProxyType(
    {".json": "application/json", ".xml": "application/xml", ".html": "text/html", ".txt": "text/plain"}
)
_NO_FIXTURE: Final[dict[str, JSONValue]] = {"error": "no fixture"}


@dataclass(frozen=True)
class Route:
    """`host` exactly (or, written `*.<domain>`, any sub-domain of `<domain>`: the board subdomains of
    Personio and Recruitee), `path` as a whole (`fullmatch`), and each `query` parameter: the value given, or
    present with any value for `None`. `file` is the case, relative to the fixtures directory."""

    host: str
    path: re.Pattern[str]
    query: Mapping[str, str | None]
    file: str

    def _host_matches(self, host: str) -> bool:
        if self.host.startswith("*."):
            return host.endswith(self.host[1:]) and len(host) > len(self.host) - 1
        return host == self.host

    def matches(self, request: httpx.Request) -> bool:
        if not self._host_matches(request.url.host) or self.path.fullmatch(request.url.path) is None:
            return False
        params = request.url.params
        return all(
            name in params and (value is None or params[name] == value) for name, value in self.query.items()
        )


def _r(host: str, path: str, file: str, **query: str | None) -> Route:
    return Route(host, re.compile(path), query, file)


ACCOUNT_CASE: Final = "serpapi/account_before"
ACCOUNT_RENEWAL_AHEAD: Final = timedelta(days=20)
_SR: Final = "api.smartrecruiters.com"
_SR_DH: Final = "/v1/companies/DeliveryHero/postings"
# First match wins. One route per recorded case the fake worker uses (Task 6; tests/contract/README.md):
# JSearch page 2 by its cursor, the `today` bucket, page 1, the legacy route's gateway 404; SerpApi's account
# read and search; Adzuna page 1 (the re-ingest pair: one ad under two ids) and any later page (empty);
# Arbeitnow pages 1 and 2 and every later page (the end of the feed); the six ATS boards by host and slug,
# with an unknown slug answered by that ATS's 404.
ROUTES: Final[tuple[Route, ...]] = (
    _r("jsearch.p.rapidapi.com", r"/search-v2", "jsearch/search_v2_page2", cursor=None),
    _r("jsearch.p.rapidapi.com", r"/search-v2", "jsearch/search_v2_today", date_posted="today"),
    _r("jsearch.p.rapidapi.com", r"/search-v2", "jsearch/search_v2_page1"),
    _r("jsearch.p.rapidapi.com", r"/search", "jsearch/search_legacy_404"),
    _r("serpapi.com", r"/account\.json", ACCOUNT_CASE),
    _r("serpapi.com", r"/search\.json", "serpapi/google_jobs_hl_en"),
    _r("api.adzuna.com", r"/v1/api/jobs/de/search/1", "adzuna/adzuna_reingest_pair"),
    _r("api.adzuna.com", r"/v1/api/jobs/de/search/\d+", "adzuna/search_empty"),
    _r("www.arbeitnow.com", r"/api/job-board-api", "arbeitnow/page1", page="1"),
    _r("www.arbeitnow.com", r"/api/job-board-api", "arbeitnow/page2", page="2"),
    _r("www.arbeitnow.com", r"/api/job-board-api", "arbeitnow/page_end"),
    _r("boards-api.greenhouse.io", r"/v1/boards/getyourguide/jobs", "ats/greenhouse_getyourguide"),
    _r("boards-api.greenhouse.io", r"/v1/boards/[^/]+/jobs", "ats/greenhouse_404"),
    _r("api.lever.co", r"/v0/postings/leverdemo", "ats/lever_leverdemo"),
    _r("api.eu.lever.co", r"/v0/postings/leverdemo", "ats/lever_eu_leverdemo", skip="0"),
    _r("api.eu.lever.co", r"/v0/postings/leverdemo", "ats/lever_eu_end"),
    _r("api.lever.co", r"/v0/postings/[^/]+", "ats/lever_404"),
    _r("api.eu.lever.co", r"/v0/postings/[^/]+", "ats/lever_404"),
    _r("api.ashbyhq.com", r"/posting-api/job-board/n8n", "ats/ashby_n8n"),
    _r("api.ashbyhq.com", r"/posting-api/job-board/[^/]+", "ats/ashby_404"),
    _r("everphone.jobs.personio.de", r"/xml", "ats/personio_everphone"),
    _r("*.jobs.personio.de", r"/xml", "ats/personio_404"),
    _r("bunq.recruitee.com", r"/api/offers/?", "ats/recruitee_bunq"),
    _r("*.recruitee.com", r"/api/offers/?", "ats/recruitee_404"),
    _r(_SR, rf"{_SR_DH}/744000156183719", "ats/smartrecruiters_deliveryhero_detail_1"),
    _r(_SR, rf"{_SR_DH}/744000156094379", "ats/smartrecruiters_deliveryhero_detail_2"),
    _r(_SR, _SR_DH, "ats/smartrecruiters_deliveryhero_list"),
    _r(_SR, r"/v1/companies/[^/]+/postings(?:/[^/]+)?", "ats/smartrecruiters_404"),
)


def _body(fixtures_dir: Path, case: str) -> tuple[Path, str]:
    """The body file of `case` and its content type."""
    for suffix, content_type in _CONTENT_TYPES.items():
        path = fixtures_dir / f"{case}{suffix}"
        if path.is_file():
            return path, content_type
    raise FileNotFoundError(f"fixture {case!r} has no {', '.join(_CONTENT_TYPES)} body in {fixtures_dir}")


def load_case(fixtures_dir: Path, case: str) -> tuple[int, dict[str, str], bytes]:
    """The status, the headers (lower-case names, `content-type` always set) and the body of `case`."""
    body_path, content_type = _body(fixtures_dir, case)
    status, headers = 200, {"content-type": content_type}
    headers_path = fixtures_dir / f"{case}.headers.json"
    if headers_path.is_file():
        recorded: JSONValue = json.loads(headers_path.read_text("utf-8"))
        status = get_int(recorded, "status")
        recorded_headers = {
            name.lower(): get_str(value, "") for name, value in get_object(recorded, "headers").items()
        }
        headers = {"content-type": content_type, **recorded_headers}
    return status, headers, body_path.read_bytes()


def renewed_account(body: bytes, today: date, **changes: JSONValue) -> bytes:
    """A recorded SerpApi `account.json` body whose plan renews `ACCOUNT_RENEWAL_AHEAD` after `today`, with
    its other fields changed by `changes`."""
    doc = json.loads(body)
    if not isinstance(doc, dict):
        raise ValueError("a SerpApi account document is an object")
    doc.update({"plan_renewal_date": (today + ACCOUNT_RENEWAL_AHEAD).isoformat(), **changes})
    return json.dumps(doc).encode("utf-8")


# --- the posting dates of fake mode (M4 Task 18) -----------------------------------------------------------

# The reference of a derived case without a recording's meta: the evening of the recordings (2026-10-18, the
# routed cases were fetched between 20:49 and 21:14 UTC).
FIXTURE_REFERENCE: Final = datetime(2026, 10, 18, 21, 0, tzinfo=UTC)
DateEncoding = Literal["iso", "unix_s", "unix_ms", "date"]


@dataclass(frozen=True)
class DateField:
    """A posting date an adapter reads. `path`, in a JSON body: keys joined by `.`, `[]` after a key (or alone
    first, for a body that is an array) for each element of that array (`data.jobs[].job_posted_at_timestamp`,
    `[].createdAt`); in an XML body: an ElementTree path below the root element, the element's text
    (`position/createdAt`). `encoding`: an ISO 8601 string, unix seconds or milliseconds, or a `YYYY-MM-DD`
    date."""

    path: str
    encoding: DateEncoding


# By fixture prefix, every field the prefix's adapter reads as a posting date (its `posted_at` and item time):
# JSearch `job_posted_at_timestamp` (`job_posted_at` is relative text that only sets the precision), Adzuna
# `created`, Arbeitnow `created_at`, Greenhouse `first_published`, Lever `createdAt`, Ashby `publishedAt`,
# Personio `<createdAt>`, Recruitee `published_at`, SmartRecruiters `releasedDate` of a list item (its
# `OutOfScope` item time) and of a detail (the posting). SerpApi's dates are relative chips: no field.
DATE_FIELDS: Final[Mapping[str, tuple[DateField, ...]]] = MappingProxyType(
    {
        "jsearch/": (DateField("data.jobs[].job_posted_at_timestamp", "unix_s"),),
        "adzuna/": (DateField("results[].created", "iso"),),
        "arbeitnow/": (DateField("data[].created_at", "unix_s"),),
        "ats/greenhouse_": (DateField("jobs[].first_published", "iso"),),
        "ats/lever_": (DateField("[].createdAt", "unix_ms"),),
        "ats/ashby_": (DateField("jobs[].publishedAt", "iso"),),
        "ats/personio_": (DateField("position/createdAt", "iso"),),
        "ats/recruitee_": (DateField("offers[].published_at", "iso"),),
        "ats/smartrecruiters_": (
            DateField("content[].releasedDate", "iso"),
            DateField("releasedDate", "iso"),
        ),
    }
)
_DAY: Final = timedelta(days=1)
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_EPOCH_UNITS: Final = MappingProxyType({"unix_s": timedelta(seconds=1), "unix_ms": timedelta(milliseconds=1)})
_DATE: Final = re.compile(r"\d{4}-\d{2}-\d{2}")
# An ISO string's parts: its date, then (optionally) the separator, hours and minutes, seconds and fraction,
# then whatever names its zone (`Z`, `+02:00`, ` UTC`, nothing).
_ISO_STYLE: Final = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:(?P<sep>[T ])(?P<hm>\d{2}:\d{2})(?P<s>:\d{2})?(?P<frac>[.,]\d+)?)?"
    r"(?P<zone>(?:Z|[+-]\d{2}(?::?\d{2})?| ?UTC| ?GMT)?)"
)


def case_reference(fixtures_dir: Path, case: str) -> datetime:
    """The instant `case`'s dates are relative to: its recording's `fetched_at`, else `FIXTURE_REFERENCE`."""
    meta = fixtures_dir / f"{case}.meta.json"
    if not meta.is_file():
        return FIXTURE_REFERENCE
    fetched_at = datetime.fromisoformat(get_str(json.loads(meta.read_text("utf-8")), "fetched_at"))
    if fetched_at.tzinfo is None:
        raise ValueError(f"fixture {case!r}: fetched_at has no zone")
    return fetched_at


def _fields_of(case: str) -> tuple[DateField, ...]:
    return next((fields for prefix, fields in DATE_FIELDS.items() if case.startswith(prefix)), ())


def _moved_epoch(value: JSONValue, unit: timedelta, days: int, now: datetime) -> JSONValue:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return value
    moved = value + days * (_DAY // unit)
    try:
        if _EPOCH + unit * moved <= now:
            return moved
    except OverflowError:  # outside the calendar: the adapter reads no date either
        return value
    ceiling = (now - _EPOCH) // unit  # `now`, truncated to the unit
    return float(ceiling) if isinstance(value, float) else ceiling


def _zone_offset(zone: str) -> timedelta:
    if zone.strip() in ("", "Z", "UTC", "GMT"):
        return timedelta(0)
    return datetime.fromisoformat(f"2000-01-01T00:00:00{zone}").utcoffset() or timedelta(0)


def _like(style: re.Match[str], now: datetime) -> str:
    """`now` written as the ISO string `style` matched: its offset, separator, parts and fraction digits, and
    its zone text; truncated, so never after `now`."""
    local = now.astimezone(timezone(_zone_offset(style["zone"])))
    text = local.date().isoformat()
    if style["hm"] is not None:
        text += f"{style['sep']}{local:%H:%M}"
        if style["s"] is not None:
            text += f":{local:%S}"
        if (fraction := style["frac"]) is not None:
            digits = len(fraction) - 1
            text += fraction[0] + f"{local.microsecond:06d}".ljust(digits, "0")[:digits]
    return text + style["zone"]


def _moved_iso(value: JSONValue, days: int, now: datetime, *, path: str) -> JSONValue:
    if not isinstance(value, str) or parse_iso(value, precision="exact") is None:
        return value  # not a date the adapter reads
    style = _ISO_STYLE.fullmatch(value)
    if style is None:
        raise ValueError(f"the {path} value {value!r} has an ISO form the shift cannot keep")
    moved_day = date.fromisoformat(style["date"]) + timedelta(days=days)
    moved = moved_day.isoformat() + value[style.end("date") :]
    at = parse_iso(moved, precision="exact")
    return moved if at is not None and at.at <= now else _like(style, now)


def _moved_date(value: JSONValue, days: int, now: datetime) -> JSONValue:
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        return value
    moved = date.fromisoformat(value) + timedelta(days=days)
    if datetime.combine(moved, time.min, UTC) > now:
        moved = now.astimezone(UTC).date()
    return moved.isoformat()


def _moved(value: JSONValue, field: DateField, days: int, now: datetime) -> JSONValue:
    if field.encoding == "iso":
        return _moved_iso(value, days, now, path=field.path)
    if field.encoding == "date":
        return _moved_date(value, days, now)
    return _moved_epoch(value, _EPOCH_UNITS[field.encoding], days, now)


def _steps(path: str) -> list[tuple[str, bool]]:
    """A JSON path's steps: each key and whether it is an array to walk (a leading `[]` has no key)."""
    steps: list[tuple[str, bool]] = []
    for part in path.split("."):
        name, each = part.removesuffix("[]"), part.endswith("[]")
        if not name and (steps or not each):
            raise ValueError(f"date field path {path!r} has an empty step")
        steps.append((name, each))
    if steps[-1][1] or not steps[-1][0]:
        raise ValueError(f"date field path {path!r} must end with a key")
    return steps


def _holders(node: JSONValue, steps: list[tuple[str, bool]]) -> Iterator[dict[str, JSONValue]]:
    """The objects that hold the value at `steps` (its last key), in document order."""
    (name, each), rest = steps[0], steps[1:]
    if not rest:
        if isinstance(node, dict) and name in node:
            yield node
        return
    if name:
        if not isinstance(node, dict) or name not in node:
            return
        node = node[name]
    for child in (node if isinstance(node, list) else []) if each else [node]:
        yield from _holders(child, rest)


def _shift_json(body: bytes, fields: tuple[DateField, ...], days: int, now: datetime) -> bytes:
    try:
        doc: JSONValue = json.loads(body)
    except ValueError:  # not JSON (a truncated page): no date the adapter reads
        return body
    changed = False
    for field in fields:
        steps = _steps(field.path)
        key = steps[-1][0]
        for holder in _holders(doc, steps):
            moved = _moved(holder[key], field, days, now)
            if moved != holder[key]:
                holder[key], changed = moved, True
    if not changed:
        return body
    text = json.dumps(doc, ensure_ascii=False, indent=2) + ("\n" if body.endswith(b"\n") else "")
    return text.encode("utf-8")


def _shift_xml(body: bytes, fields: tuple[DateField, ...], days: int, now: datetime) -> bytes:
    try:
        root = fromstring(body, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (DefusedXmlException, ParseError):  # a DTD, an entity, no XML: the adapter refuses it as well
        return body
    shifted = body
    for field in fields:
        name = field.path.rsplit("/", 1)[-1]
        tag = re.escape(name.encode("ascii"))
        lexical = list(re.finditer(rb"<" + tag + rb">([^<]*)</" + tag + rb">", shifted))
        parsed = [element.text or "" for element in root.findall(field.path)]
        if [match[1].decode("utf-8") for match in lexical] != parsed:
            raise ValueError(f"the XML body's {name} elements are not all at {field.path}")
        parts, at = [], 0
        for match in lexical:
            text = match[1].decode("utf-8")
            moved = _moved(text.strip(), field, days, now)
            parts += [shifted[at : match.start(1)], text.replace(text.strip(), str(moved), 1).encode("utf-8")]
            at = match.end(1)
        shifted = b"".join([*parts, shifted[at:]])
    return body if shifted == body else shifted


def shift_dates(body: bytes, *, case: str, kind: str, now: datetime, reference: datetime) -> bytes:
    """`body` of `case` (its file suffix `kind`: `.json`, `.xml`, ...) with every `DATE_FIELDS` value of its
    prefix moved by the whole days from `reference` to `now`, none past `now` (the module docstring); `body`
    itself when nothing moved."""
    fields = _fields_of(case)
    if not fields:
        return body
    days = (now - reference) // _DAY
    if kind == ".json":
        return _shift_json(body, fields, days, now)
    if kind == ".xml":
        return _shift_xml(body, fields, days, now)
    return body


def _serve(fixtures_dir: Path, case: str, clock: Callable[[], datetime] | None) -> httpx.Response:
    status, headers, body = load_case(fixtures_dir, case)
    if clock is not None:
        now = clock()
        if case == ACCOUNT_CASE:
            body = renewed_account(body, now.date())
        elif 200 <= status < 300:
            kind = _body(fixtures_dir, case)[0].suffix
            reference = case_reference(fixtures_dir, case)
            body = shift_dates(body, case=case, kind=kind, now=now, reference=reference)
    return httpx.Response(status, headers=headers, content=body)


def _log_served(log_path: Path, request: httpx.Request, case: str | None, status: int) -> None:
    line = {
        "pid": os.getpid(),
        "method": request.method,
        "host": request.url.host,
        "path": request.url.path,
        "query": public_query(request.url),
        "case": case,
        "status": status,
    }
    data = (json.dumps(line, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def fixture_transport(
    fixtures_dir: Path, *, log_path: Path | None = None, clock: Callable[[], datetime] | None = None
) -> httpx.MockTransport:
    """A transport that answers from `fixtures_dir` by `ROUTES` (read at each request); with `clock`, the
    SerpApi account renews after `clock()` and the posting dates move with it (see the module docstring)."""

    def handle(request: httpx.Request) -> httpx.Response:
        route = next((route for route in ROUTES if route.matches(request)), None)
        if route is None:
            response = httpx.Response(404, json=_NO_FIXTURE)
        else:
            response = _serve(fixtures_dir, route.file, clock)
        if log_path is not None:
            _log_served(log_path, request, None if route is None else route.file, response.status_code)
        return response

    return httpx.MockTransport(handle)
