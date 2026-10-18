# tests/unit/test_fixture_transport.py
import json
import os
import re
from pathlib import Path

import httpx
import pytest
from jobhunter.sources import fixtures
from jobhunter.sources.fixtures import ROUTES, Route, fixture_transport


async def test_routes_by_host_path_and_query(tmp_path: Path) -> None:
    (tmp_path / "jsearch").mkdir()
    (tmp_path / "jsearch/search_v2_page1.json").write_text('{"status":"OK","data":[]}')
    (tmp_path / "jsearch/search_v2_page1.headers.json").write_text(
        json.dumps({"status": 200, "headers": {"x-ratelimit-requests-remaining": "199"}})
    )
    (tmp_path / "jsearch/search_legacy_404.json").write_text(
        '{"message":"Endpoint \'/search\' does not exist"}'
    )
    (tmp_path / "jsearch/search_legacy_404.headers.json").write_text(
        json.dumps({"status": 404, "headers": {"x-rapidapi-proxy-response": "true"}})
    )
    async with httpx.AsyncClient(transport=fixture_transport(tmp_path)) as c:
        ok = await c.get("https://jsearch.p.rapidapi.com/search-v2?query=x&country=de")
        assert (ok.status_code, ok.headers["x-ratelimit-requests-remaining"], ok.json()["status"]) == (
            200,
            "199",
            "OK",
        )
        assert (await c.get("https://jsearch.p.rapidapi.com/search?query=x")).status_code == 404
        nowhere = await c.get("https://nowhere.example/x")
        assert nowhere.status_code == 404 and nowhere.json() == {"error": "no fixture"}


def test_the_shipped_routes_are_the_two_jsearch_routes() -> None:
    assert [(r.host, r.path.pattern, dict(r.query), r.file) for r in ROUTES] == [
        ("jsearch.p.rapidapi.com", "/search-v2", {}, "jsearch/search_v2_page1"),
        ("jsearch.p.rapidapi.com", "/search", {}, "jsearch/search_legacy_404"),
    ]


async def test_query_constraints_first_match_and_the_served_request_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        fixtures,
        "ROUTES",
        (
            Route("api.example", re.compile(r"/feed"), {"page": "1"}, "feed/page1"),
            Route("api.example", re.compile(r"/feed"), {"cursor": None}, "feed/by_cursor"),
            Route("api.example", re.compile(r"/feed"), {}, "feed/page_end"),
            Route("api.example", re.compile(r"/board/[a-z]+"), {}, "feed/board"),
        ),
    )
    (tmp_path / "feed").mkdir()
    for case in ("page1", "by_cursor", "page_end"):
        (tmp_path / f"feed/{case}.json").write_text(json.dumps({"case": case}))
    (tmp_path / "feed/board.xml").write_text("<board/>")
    log = tmp_path / "served.jsonl"
    async with httpx.AsyncClient(transport=fixture_transport(tmp_path, log_path=log)) as c:
        cases = [
            (await c.get(f"https://api.example{target}")).json()["case"]
            for target in ("/feed?page=1", "/feed?page=2&cursor=abc", "/feed?page=3", "/feed")
        ]
        board = await c.get("https://api.example/board/acme?api_key=SECRET-123456789&lang=de")
        missing = await c.get("https://api.example/board/ACME")
    assert cases == ["page1", "by_cursor", "page_end", "page_end"]
    assert (board.status_code, board.text, board.headers["content-type"]) == (
        200,
        "<board/>",
        "application/xml",
    )
    assert missing.status_code == 404
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert [line["case"] for line in lines] == [
        "feed/page1",
        "feed/by_cursor",
        "feed/page_end",
        "feed/page_end",
        "feed/board",
        None,
    ]
    assert lines[4] == {
        "pid": os.getpid(),
        "method": "GET",
        "host": "api.example",
        "path": "/board/acme",
        "query": "lang=de",  # a credential parameter is never written down
        "case": "feed/board",
        "status": 200,
    }
    assert lines[5]["status"] == 404 and "SECRET" not in log.read_text()


async def test_a_route_without_its_fixture_file_fails_loudly(tmp_path: Path) -> None:
    async with httpx.AsyncClient(transport=fixture_transport(tmp_path)) as c:
        with pytest.raises(FileNotFoundError, match="search_v2_page1"):
            await c.get("https://jsearch.p.rapidapi.com/search-v2")
