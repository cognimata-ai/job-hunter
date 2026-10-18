"""The fixture transport of fake mode (`JOBHUNTER_FAKE_SOURCES`, spec §18.4; plan Global Constraints).

`fixture_transport` serves every request from the scrubbed recordings in a fixtures directory, through the
real adapter code: the first `ROUTES` entry that matches the request's host, path and query names the case
`<file>`, whose body is `<file>.json` (or `<file>.xml`) and whose status and headers are those of
`<file>.headers.json` (`{"status": …, "headers": {…}}`; without it 200 and the content type of the body's
kind). A request that no route matches is answered 404 `{"error": "no fixture"}`; a route whose body file is
missing is a broken fixture set and raises `FileNotFoundError`.

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

_CONTENT_TYPES: Final = MappingProxyType({".json": "application/json", ".xml": "application/xml"})
_NO_FIXTURE: Final[dict[str, JSONValue]] = {"error": "no fixture"}


@dataclass(frozen=True)
class Route:
    """`host` exactly, `path` as a whole (`fullmatch`), and each `query` parameter: the value given, or
    present with any value for `None`. `file` is the case, relative to the fixtures directory."""

    host: str
    path: re.Pattern[str]
    query: Mapping[str, str | None]
    file: str

    def matches(self, request: httpx.Request) -> bool:
        if request.url.host != self.host or self.path.fullmatch(request.url.path) is None:
            return False
        params = request.url.params
        return all(
            name in params and (value is None or params[name] == value) for name, value in self.query.items()
        )


# First match wins. Task 6 adds a route for every case it records.
ROUTES: Final[tuple[Route, ...]] = (
    Route("jsearch.p.rapidapi.com", re.compile(r"/search-v2"), {}, "jsearch/search_v2_page1"),
    Route("jsearch.p.rapidapi.com", re.compile(r"/search"), {}, "jsearch/search_legacy_404"),
)


def _body(fixtures_dir: Path, case: str) -> tuple[Path, str]:
    """The body file of `case` and its content type."""
    for suffix, content_type in _CONTENT_TYPES.items():
        path = fixtures_dir / f"{case}{suffix}"
        if path.is_file():
            return path, content_type
    raise FileNotFoundError(f"fixture {case!r} has no .json or .xml body in {fixtures_dir}")


def _serve(fixtures_dir: Path, case: str) -> httpx.Response:
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
    return httpx.Response(status, headers=headers, content=body_path.read_bytes())


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
