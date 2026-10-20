"""tests/support/sources.py (Task 6): the helpers the adapter, crash and end-to-end tests read fixtures by."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import respx
from jobhunter.types import get_list, get_str

from tests.support.sources import fixture_page, load_fixture, mock_fixture, served_requests
from tests.unit.test_sources_base import SPEC

T = datetime(2026, 10, 16, 9, 47, tzinfo=UTC)


def test_load_fixture_and_fixture_page_read_a_case_as_served() -> None:
    status, headers, body = load_fixture("jsearch", "search_legacy_404")
    assert (
        status == 404
        and headers["x-rapidapi-proxy-response"] == "true"
        and get_str(json.loads(body), "message")
    )
    page = fixture_page("jsearch", "search_v2_page1", SPEC, fetched_at=T)
    assert page.status == 200 and page.fetched_at == T and page.spec is SPEC
    assert (
        page.headers["x-ratelimit-requests-limit"] == "200" and len(get_list(page.json(), "data.jobs")) == 10
    )
    html = fixture_page("jsearch", "search_v2_html", SPEC, fetched_at=T)
    assert html.headers["content-type"].startswith("text/html") and html.text().lstrip().startswith(
        "<!DOCTYPE"
    )


@respx.mock
async def test_mock_fixture_serves_the_case_for_any_query() -> None:
    route = mock_fixture(
        respx.mock, "GET", "https://api.adzuna.com/v1/api/jobs/de/search/1", "adzuna", "search_401"
    )
    async with httpx.AsyncClient() as client:
        response = await client.get("https://api.adzuna.com/v1/api/jobs/de/search/1?what=x")
    assert (
        route.call_count == 1 and response.status_code == 401 and response.json()["exception"] == "AUTH_FAIL"
    )


def test_served_requests_names_the_case_and_stringifies(tmp_path: Path) -> None:
    log = tmp_path / "served.jsonl"
    assert served_requests(log) == []
    lines = [
        {
            "pid": 7,
            "method": "GET",
            "host": "h",
            "path": "/p",
            "query": "",
            "case": "jsearch/search_v2_page1",
            "status": 200,
        },
        {"pid": 7, "method": "GET", "host": "h", "path": "/x", "query": "", "case": None, "status": 404},
    ]
    log.write_text("".join(json.dumps(line) + "\n" for line in lines))
    served = served_requests(log)
    assert (
        [r["case"] for r in served] == ["search_v2_page1", ""]
        and served[0]["pid"] == "7"
        and served[1]["status"] == "404"
    )
