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
"""

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

import httpx

from jobhunter.sources.base import public_query
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
    _r("serpapi.com", r"/account\.json", "serpapi/account_before"),
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


def _serve(fixtures_dir: Path, case: str) -> httpx.Response:
    status, headers, body = load_case(fixtures_dir, case)
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


def fixture_transport(fixtures_dir: Path, *, log_path: Path | None = None) -> httpx.MockTransport:
    """A transport that answers from `fixtures_dir` by `ROUTES` (read at each request)."""

    def handle(request: httpx.Request) -> httpx.Response:
        route = next((route for route in ROUTES if route.matches(request)), None)
        if route is None:
            response = httpx.Response(404, json=_NO_FIXTURE)
        else:
            response = _serve(fixtures_dir, route.file)
        if log_path is not None:
            _log_served(log_path, request, None if route is None else route.file, response.status_code)
        return response

    return httpx.MockTransport(handle)
