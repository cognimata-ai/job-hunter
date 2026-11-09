"""The built SPA (`frontend/dist`: `index.html`, hashed files under `assets/`), served by the api same-origin
(spec §15, §16.6).

`SpaFiles` is not a Mount: Starlette prefers a Mount's full match to a route's partial one (its path with
another method), so a Mount at `/` would answer `POST /api/jobs` with its own 405 and lose FastAPI's 405 with
`Allow`. The `HTTPException` handler of `api.errors` offers it instead what no route matched: a 404 to a `GET`
or `HEAD` outside `/api`. `SpaFiles.response` then answers:

- a regular file of `dist` that the path names → that file, with `IMMUTABLE` under `assets/` (Vite puts a
  content hash in those names) and `no-cache` otherwise (`index.html` asked for by name included);
- any other path whose `Accept` lists `text/html` → `index.html` with `no-cache`: a deep link, which the
  SPA's router resolves (`*/*` alone, curl's default, is not a page request);
- anything else → `None`, and the caller keeps its 404 `not_found` problem. A path with a NUL or a backslash,
  or one that resolves (`..`, a symlink) outside `dist`, is refused outright, whatever it accepts.

A request with `Range` (a deep link's `index.html` included) is answered as Starlette's `FileResponse` ships
it: 206 with the part, or its own plain-text 400 (a malformed range) or 416 (a range past the end), still
with the edge headers.

The path is the server's, percent-decoded once; it is never decoded again, so `%252e%252e` stays a name of
its own. A directory is never listed or served. The content type of each file Vite emits comes from
`CONTENT_TYPES`, since a container's `mimetypes` database may lack or misname it; anything else from
`mimetypes`. The edge's §16.6 headers (`api.edge.SECURITY_HEADERS`) are on every answer, as on every other
response. A `dist` that is missing at the start (tests, a backend-only run) serves nothing, logged once as
`spa_missing`.
"""

import mimetypes
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final

from starlette.datastructures import Headers
from starlette.responses import FileResponse
from starlette.types import Scope

from jobhunter.observability.logging import get_logger

INDEX: Final = "index.html"
ASSETS: Final = "assets"
IMMUTABLE: Final = "public, max-age=31536000, immutable"
NO_CACHE: Final = "no-cache"
HTML: Final = "text/html"
CONTENT_TYPES: Final[Mapping[str, str]] = MappingProxyType(
    {
        ".html": "text/html",
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".css": "text/css",
        ".svg": "image/svg+xml",
        ".woff2": "font/woff2",
        ".json": "application/json",
        ".map": "application/json",
    }
)

log = get_logger("jobhunter.api.static")


def content_type(path: Path) -> str:
    """The media type of `path`: `CONTENT_TYPES` by its suffix, else `mimetypes`, else a byte stream."""
    known = CONTENT_TYPES.get(path.suffix.lower())
    return known or mimetypes.guess_file_type(path)[0] or "application/octet-stream"


def _quality(params: list[str]) -> float:
    for param in params:
        name, _, value = param.partition("=")
        if name.strip().lower() == "q":
            try:
                return float(value.strip())
            except ValueError:
                return 1.0
    return 1.0


def accepts_html(headers: Headers) -> bool:
    """Whether an `Accept` header lists `text/html` with a quality above 0 (any parameters, any case)."""
    for header in headers.getlist("accept"):
        for item in header.split(","):
            media_type, *params = item.split(";")
            if media_type.strip().lower() == HTML and _quality(params) > 0:
                return True
    return False


def _inside(root: Path, path: str) -> Path | None:
    """The resolved path that request path `path` names under `root`; `None` when it is refused."""
    if "\x00" in path or "\\" in path:
        return None
    try:
        resolved = (root / path.lstrip("/")).resolve()
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_relative_to(root) else None


def _regular_file(path: Path) -> os.stat_result | None:
    try:
        found = path.stat()
    except OSError:
        return None
    return found if stat.S_ISREG(found.st_mode) else None


def _file_response(path: Path, found: os.stat_result, cache: str) -> FileResponse:
    return FileResponse(
        path, media_type=content_type(path), stat_result=found, headers={"Cache-Control": cache}
    )


class SpaFiles:
    """The files of `dist` and its deep-link fallback (module docstring)."""

    def __init__(self, dist: Path) -> None:
        self._root = dist.resolve() if dist.is_dir() else None
        if self._root is None:
            log.info("spa_missing", spa_dir=str(dist))

    def response(self, scope: Scope) -> FileResponse | None:
        """The SPA's answer to the request of `scope`, or `None` when it has none (module docstring)."""
        root = self._root
        if root is None:
            return None
        target = _inside(root, str(scope.get("path", "")))
        if target is None:
            return None
        if (found := _regular_file(target)) is not None:
            cache = IMMUTABLE if target.is_relative_to(root / ASSETS) else NO_CACHE
            return _file_response(target, found, cache)
        index = _inside(root, INDEX)
        if index is None or not accepts_html(Headers(scope=scope)):
            return None
        found = _regular_file(index)
        return None if found is None else _file_response(index, found, NO_CACHE)
