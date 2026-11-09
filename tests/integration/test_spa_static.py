"""The SPA served by the api (spec §15, §16.6; M4 Task 19): `api.static.SpaFiles`, on the router's 404 path.

Every app here gets a temporary `dist` (`JOBHUNTER_SPA_DIR`) shaped like Vite's output: `index.html` at the
top, hashed files under `assets/`. A `GET` or `HEAD` that no route matched, outside `/api`, is offered to the
SPA: a file of `dist` is served as it is, any other path that accepts `text/html` gets `index.html` (a deep
link), and everything else stays the 404 `not_found` problem. The edge's §16.6 headers are on every answer.
"""

import mimetypes
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
from jobhunter.api.app import JobHunterApp, create_app
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.fixtures import ConfigFactory

pytestmark = pytest.mark.integration

# §16.6, as the edge sends it.
CSP: Final = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
)
ASSET_CACHE: Final = "public, max-age=31536000, immutable"
# What a browser sends for a navigation (Chromium's), and curl's default.
NAVIGATION: Final = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,*/*;q=0.8"
CURL: Final = "*/*"
INDEX_HTML: Final = (
    '<!doctype html><html lang="en"><head><meta charset="UTF-8" /><title>Job Hunter</title>'
    '<script type="module" crossorigin src="/assets/app-1a2b.js"></script>'
    '<link rel="stylesheet" crossorigin href="/assets/app-3c4d.css"></head>'
    '<body><div id="root"></div></body></html>\n'
)
FILES: Final = {
    "index.html": INDEX_HTML.encode(),
    "favicon.svg": b'<svg xmlns="http://www.w3.org/2000/svg"></svg>\n',
    "assets/app-1a2b.js": b'console.log("job hunter");\n',
    "assets/app-3c4d.css": b"body { margin: 0; }\n",
    "assets/app-1a2b.js.map": b'{"version": 3}\n',
    "assets/inter-5e6f.woff2": b"wOF2\x00\x01\x00\x00",
}
OUTSIDE: Final = b"the content of a file outside dist\n"


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    """A Vite-shaped `dist`, next to `outside.txt` (which it must never serve) and with `assets/escape.txt`,
    a symlink inside it that points at that file."""
    root = tmp_path / "dist"
    for name, content in FILES.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(content)
    (tmp_path / "outside.txt").write_bytes(OUTSIDE)
    (root / "assets/escape.txt").symlink_to(tmp_path / "outside.txt")
    return root


def _client(app: JobHunterApp) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        headers={"Origin": "https://testserver", "X-JH-Request": "1"},
    )


@pytest.fixture
async def spa(
    engine: AsyncEngine, make_config: ConfigFactory, dist: Path
) -> AsyncIterator[httpx.AsyncClient]:
    """A client of the api that serves `dist`."""
    async with _client(create_app(cfg=make_config("api", JOBHUNTER_SPA_DIR=str(dist)), engine=engine)) as c:
        yield c


def assert_edge_headers(r: httpx.Response) -> None:
    assert r.headers["content-security-policy"] == CSP
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cross-origin-resource-policy"] == "same-origin"
    assert r.headers["x-request-id"]


def assert_not_found_problem(r: httpx.Response, path: str) -> None:
    assert (r.status_code, r.headers["content-type"]) == (404, "application/problem+json"), (path, r.text)
    if r.request.method == "HEAD":  # the problem's headers, without its body
        assert r.content == b"", path
    else:
        assert (r.json()["code"], r.json()["status"]) == ("not_found", 404), path
    assert_edge_headers(r)


def assert_index(r: httpx.Response) -> None:
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "text/html; charset=utf-8"
    assert r.headers["cache-control"] == "no-cache"
    assert r.text == INDEX_HTML
    assert_edge_headers(r)


async def test_index_and_assets_with_their_cache_headers_and_the_csp(spa: httpx.AsyncClient) -> None:
    assert_index(await spa.get("/index.html"))  # asked for by name: still no-cache, never immutable
    js = await spa.get("/assets/app-1a2b.js")
    assert (js.status_code, js.content) == (200, FILES["assets/app-1a2b.js"])
    assert (js.headers["content-type"], js.headers["cache-control"]) == (
        "text/javascript; charset=utf-8",
        ASSET_CACHE,
    )
    assert_edge_headers(js)
    css = await spa.get("/assets/app-3c4d.css")
    assert (css.status_code, css.content) == (200, FILES["assets/app-3c4d.css"])
    assert (css.headers["content-type"], css.headers["cache-control"]) == (
        "text/css; charset=utf-8",
        ASSET_CACHE,
    )
    assert_edge_headers(css)
    icon = await spa.get("/favicon.svg")  # a file outside assets/ has no hash in its name: no-cache
    assert (icon.status_code, icon.content) == (200, FILES["favicon.svg"])
    assert (icon.headers["content-type"], icon.headers["cache-control"]) == ("image/svg+xml", "no-cache")
    assert_edge_headers(icon)


async def test_the_types_of_vite_s_files_do_not_depend_on_the_mimetypes_database(
    spa: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A container's database may lack a type or name another one (`application/javascript`, `text/plain`).
    def wrong(path: object, *, strict: bool = True) -> tuple[str, None]:
        return "application/x-wrong", None

    monkeypatch.setattr(mimetypes, "guess_file_type", wrong)
    types = {
        "/assets/app-1a2b.js": "text/javascript; charset=utf-8",
        "/assets/app-3c4d.css": "text/css; charset=utf-8",
        "/assets/app-1a2b.js.map": "application/json",
        "/assets/inter-5e6f.woff2": "font/woff2",
        "/favicon.svg": "image/svg+xml",
    }
    assert {path: (await spa.get(path)).headers["content-type"] for path in types} == types


async def test_a_range_is_answered_as_starlette_ships_it_with_the_edge_headers(
    spa: httpx.AsyncClient,
) -> None:
    size = len(FILES["index.html"])
    part = await spa.get("/index.html", headers={"Range": "bytes=0-3"})
    assert (part.status_code, part.content) == (206, FILES["index.html"][:4])
    assert part.headers["content-range"] == f"bytes 0-3/{size}"
    assert_edge_headers(part)
    past = await spa.get("/index.html", headers={"Range": "bytes=999-"})
    assert (past.status_code, past.headers["content-range"]) == (416, f"bytes */{size}")
    assert_edge_headers(past)


async def test_head_has_the_status_and_headers_of_get_and_no_body(spa: httpx.AsyncClient) -> None:
    for path, accept in (("/assets/app-1a2b.js", CURL), ("/index.html", CURL), ("/jobs/42", NAVIGATION)):
        get = await spa.get(path, headers={"Accept": accept})
        head = await spa.head(path, headers={"Accept": accept})
        assert (head.status_code, head.content) == (200, b""), path
        assert {**head.headers, "x-request-id": ""} == {**get.headers, "x-request-id": ""}, path
        assert int(head.headers["content-length"]) == len(get.content) > 0


async def test_a_deep_link_accepting_html_gets_index_html(spa: httpx.AsyncClient) -> None:
    for path in ("/", "/settings", "/jobs/42", "/jobs/42/", "/runs?status=active", "/assets/", "/assets"):
        r = await spa.get(path, headers={"Accept": NAVIGATION})
        assert_index(r)  # a directory is never listed: `/assets` is a deep link like any other
    for accept in ("text/html", "application/json, TEXT/HTML; q=0.5", "text/html;level=1"):
        assert_index(await spa.get("/settings", headers={"Accept": accept}))
    # A missing file is a deep link too when it accepts HTML: the asset of an older build, say.
    assert_index(await spa.get("/assets/app-0000.js", headers={"Accept": NAVIGATION}))


async def test_an_unknown_api_path_is_a_404_problem_not_the_spa(spa: httpx.AsyncClient) -> None:
    for path in ("/api", "/api/", "/api/nope", "/api/index.html", "/api/assets/app-1a2b.js"):
        assert_not_found_problem(await spa.get(path, headers={"Accept": NAVIGATION}), path)
        assert_not_found_problem(await spa.head(path, headers={"Accept": NAVIGATION}), path)
    assert_index(await spa.get("/apis", headers={"Accept": NAVIGATION}))  # not under /api


async def test_a_traversal_is_refused(spa: httpx.AsyncClient, tmp_path: Path) -> None:
    # httpx removes a literal `..` before sending, so `%2e%2e` and `%2f` carry it: the server decodes the path
    # once, and the app sees `/assets/../../outside.txt`, as it would for a client that sends `..` itself.
    up = "/".join([".."] * (len(tmp_path.parts) + 4))
    refused = [
        "/assets/%2e%2e/%2e%2e/outside.txt",
        "/assets/..%2f..%2foutside.txt",
        "/%2e%2e/outside.txt",
        f"/assets/{up.replace('..', '%2e%2e')}/etc/passwd",  # /etc/passwd itself
        "/assets/escape.txt",  # a symlink inside dist that points outside it
        "/assets/..%5c..%5coutside.txt",  # a backslash
        "/assets/app-1a2b.js%00",  # a NUL
        "/assets/app-1a2b.js%00.png",
    ]
    for path in refused:
        assert_not_found_problem(await spa.get(path, headers={"Accept": CURL}), path)
        r = await spa.get(path, headers={"Accept": NAVIGATION})
        assert_not_found_problem(r, path)  # refused: not even index.html
        assert OUTSIDE not in r.content and b"root:" not in r.content, path
    # Decoded once, never twice: `%252e%252e` is a name of its own (`%2e%2e`), which dist does not hold.
    double = "/assets/%252e%252e/%252e%252e/outside.txt"
    assert_not_found_problem(await spa.get(double, headers={"Accept": CURL}), double)
    assert_index(await spa.get(double, headers={"Accept": NAVIGATION}))
    # Inside dist after resolving: served as the file it names.
    r = await spa.get("/assets/%2e%2e/favicon.svg")
    assert (r.status_code, r.content) == (200, FILES["favicon.svg"])


async def test_a_json_request_to_an_unknown_path_is_404(spa: httpx.AsyncClient) -> None:
    for accept in ("application/json", CURL, "application/xhtml+xml", "text/*", "text/html;q=0, */*"):
        assert_not_found_problem(await spa.get("/settings", headers={"Accept": accept}), accept)
    assert_not_found_problem(await spa.get("/assets/app-0000.js", headers={"Accept": CURL}), "missing asset")
    no_accept = await spa.get("/settings", headers={"Accept": ""})
    assert_not_found_problem(no_accept, "empty Accept")


async def test_only_a_get_or_head_is_offered_to_the_spa(spa: httpx.AsyncClient) -> None:
    for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        r = await spa.request(method, "/settings", headers={"Accept": NAVIGATION})
        assert_not_found_problem(r, method)
        r = await spa.request(method, "/index.html", headers={"Accept": NAVIGATION})
        assert_not_found_problem(r, method)


async def test_without_a_dist_nothing_is_served(
    engine: AsyncEngine,
    make_config: ConfigFactory,
    tmp_path: Path,
    dist: Path,
    log_events: Callable[[], list[dict[str, Any]]],
) -> None:
    absent = tmp_path / "no-dist"
    app = create_app(cfg=make_config("api", JOBHUNTER_SPA_DIR=str(absent)), engine=engine)
    async with _client(app) as c:
        for path in ("/", "/index.html", "/settings", "/assets/app-1a2b.js"):
            assert_not_found_problem(await c.get(path, headers={"Accept": NAVIGATION}), path)
            assert_not_found_problem(await c.head(path, headers={"Accept": NAVIGATION}), path)
        assert (await c.get("/healthz")).status_code == 200
    a_file = tmp_path / "a-file"
    a_file.write_text("not a directory\n")
    create_app(cfg=make_config("api", JOBHUNTER_SPA_DIR=str(a_file)), engine=engine)
    create_app(cfg=make_config("api", JOBHUNTER_SPA_DIR=str(dist)), engine=engine)  # present: nothing logged
    # Once per app with no directory (its `spa_dir` is a path, which the log redaction may mask), at info.
    missing = [(e["level"], e["logger"]) for e in log_events() if e["event"] == "spa_missing"]
    assert missing == [("info", "jobhunter.api.static")] * 2


async def test_a_wrong_method_on_an_api_route_keeps_405_with_allow(spa: httpx.AsyncClient) -> None:
    r = await spa.post("/api/jobs", json={}, headers={"Accept": NAVIGATION})
    assert (r.status_code, r.headers["content-type"]) == (405, "application/problem+json")
    assert (r.json()["code"], r.json()["status"], r.headers["allow"]) == ("not_found", 405, "GET")
    assert_edge_headers(r)
    r = await spa.post("/healthz", headers={"Accept": NAVIGATION})  # outside /api too
    assert (r.status_code, r.json()["code"], r.headers["allow"]) == (405, "not_found", "GET")


async def test_spa_answers_are_counted_unmatched_never_by_path(spa: httpx.AsyncClient) -> None:
    from jobhunter.observability.metrics import REGISTRY

    def count(route: str, status: str) -> float:
        value = REGISTRY.get_sample_value("jobhunter_http_requests_total", {"route": route, "status": status})
        return 0.0 if value is None else value

    before = {status: count("unmatched", status) for status in ("200", "404")}
    assert (await spa.get("/assets/app-1a2b.js")).status_code == 200
    assert (await spa.get("/settings", headers={"Accept": NAVIGATION})).status_code == 200
    assert (await spa.get("/settings", headers={"Accept": CURL})).status_code == 404
    assert {status: count("unmatched", status) - before[status] for status in before} == {
        "200": 2.0,
        "404": 1.0,
    }
    for path in ("/assets/app-1a2b.js", "/settings"):
        assert count(path, "200") == 0.0 and count(path, "404") == 0.0
