# tests/unit/test_fixture_transport.py
import json
import os
import re
from datetime import UTC, datetime
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


SHIPPED = Path(__file__).resolve().parents[2] / "tests/contract/fixtures"
# What the fake worker sends and the case each request is served (Task 6: a route per recorded case it uses).
SHIPPED_REQUESTS = [
    (
        "https://jsearch.p.rapidapi.com/search-v2?query=x&country=de&date_posted=week",
        "jsearch/search_v2_page1",
    ),
    ("https://jsearch.p.rapidapi.com/search-v2?query=x&date_posted=week&cursor=c", "jsearch/search_v2_page2"),
    ("https://jsearch.p.rapidapi.com/search-v2?query=x&date_posted=today", "jsearch/search_v2_today"),
    ("https://jsearch.p.rapidapi.com/search?query=x", "jsearch/search_legacy_404"),
    ("https://serpapi.com/account.json?api_key=k", "serpapi/account_before"),
    ("https://serpapi.com/search.json?engine=google_jobs&q=x&api_key=k", "serpapi/google_jobs_hl_en"),
    ("https://api.adzuna.com/v1/api/jobs/de/search/1?what=x", "adzuna/adzuna_reingest_pair"),
    ("https://api.adzuna.com/v1/api/jobs/de/search/2?what=x", "adzuna/search_empty"),
    ("https://www.arbeitnow.com/api/job-board-api?page=1", "arbeitnow/page1"),
    ("https://www.arbeitnow.com/api/job-board-api?page=2", "arbeitnow/page2"),
    ("https://www.arbeitnow.com/api/job-board-api?page=3", "arbeitnow/page_end"),
    (
        "https://boards-api.greenhouse.io/v1/boards/getyourguide/jobs?content=true",
        "ats/greenhouse_getyourguide",
    ),
    ("https://boards-api.greenhouse.io/v1/boards/nobody/jobs?content=true", "ats/greenhouse_404"),
    ("https://api.lever.co/v0/postings/leverdemo?mode=json&skip=0&limit=100", "ats/lever_leverdemo"),
    ("https://api.eu.lever.co/v0/postings/leverdemo?mode=json&skip=0&limit=100", "ats/lever_eu_leverdemo"),
    ("https://api.eu.lever.co/v0/postings/leverdemo?mode=json&skip=100&limit=100", "ats/lever_eu_end"),
    ("https://api.lever.co/v0/postings/nobody?mode=json&skip=0&limit=100", "ats/lever_404"),
    ("https://api.ashbyhq.com/posting-api/job-board/n8n?includeCompensation=true", "ats/ashby_n8n"),
    ("https://api.ashbyhq.com/posting-api/job-board/nobody?includeCompensation=true", "ats/ashby_404"),
    ("https://everphone.jobs.personio.de/xml", "ats/personio_everphone"),
    ("https://nobody.jobs.personio.de/xml", "ats/personio_404"),
    ("https://bunq.recruitee.com/api/offers/", "ats/recruitee_bunq"),
    ("https://nobody.recruitee.com/api/offers/", "ats/recruitee_404"),
    (
        "https://api.smartrecruiters.com/v1/companies/DeliveryHero/postings?limit=100&offset=0",
        "ats/smartrecruiters_deliveryhero_list",
    ),
    (
        "https://api.smartrecruiters.com/v1/companies/DeliveryHero/postings/744000156183719",
        "ats/smartrecruiters_deliveryhero_detail_1",
    ),
    (
        "https://api.smartrecruiters.com/v1/companies/DeliveryHero/postings/744000156094379",
        "ats/smartrecruiters_deliveryhero_detail_2",
    ),
    ("https://api.smartrecruiters.com/v1/companies/DeliveryHero/postings/1", "ats/smartrecruiters_404"),
    (
        "https://api.smartrecruiters.com/v1/companies/nobody/postings?limit=100&offset=0",
        "ats/smartrecruiters_404",
    ),
]


def test_every_shipped_route_serves_a_shipped_fixture() -> None:
    for route in ROUTES:
        status, _headers, body = fixtures.load_case(SHIPPED, route.file)  # raises when the case is missing
        assert body and 200 <= status < 600, route.file


@pytest.mark.parametrize(("url", "case"), SHIPPED_REQUESTS)
def test_the_shipped_routes_serve_the_fake_worker(url: str, case: str) -> None:
    request = httpx.Request("GET", url)
    assert next(route.file for route in ROUTES if route.matches(request)) == case
    if "_detail_" in case:  # a detail is the posting the path names (6a review, finding 1)
        _status, _headers, body = fixtures.load_case(SHIPPED, case)
        assert json.loads(body)["id"] == request.url.path.rsplit("/", 1)[1]


def test_a_wildcard_host_matches_every_subdomain_and_only_those() -> None:
    route = Route("*.jobs.personio.de", re.compile(r"/xml"), {}, "x")
    assert route.matches(httpx.Request("GET", "https://acme.jobs.personio.de/xml"))
    assert not route.matches(httpx.Request("GET", "https://jobs.personio.de.evil.example/xml"))
    assert not route.matches(httpx.Request("GET", "https://acmejobs.personio.de/xml"))


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


async def test_non_json_bodies_and_load_case(tmp_path: Path) -> None:
    (tmp_path / "jsearch").mkdir()
    html = "<!DOCTYPE html><html><body>Bad gateway</body></html>"
    (tmp_path / "jsearch/search_v2_html.html").write_text(html)
    (tmp_path / "jsearch/search_v2_html.headers.json").write_text(
        json.dumps({"status": 200, "headers": {"Content-Type": "text/html; charset=utf-8"}})
    )
    (tmp_path / "jsearch/page_truncated.txt").write_text('{"data": [{"slug": "a"}, {"sl')
    assert fixtures.load_case(tmp_path, "jsearch/search_v2_html") == (
        200,
        {"content-type": "text/html; charset=utf-8"},
        html.encode(),
    )
    assert fixtures.load_case(tmp_path, "jsearch/page_truncated") == (
        200,
        {"content-type": "text/plain"},
        b'{"data": [{"slug": "a"}, {"sl',
    )
    with pytest.raises(FileNotFoundError):
        fixtures.load_case(tmp_path, "jsearch/missing")


async def test_a_clock_renews_the_recorded_serpapi_account_and_changes_nothing_else() -> None:
    """Ruling I1 (Task 14 fix round 1): with a clock the recorded account renews 20 days after its date, so
    a fake-mode run's SerpApi window is current on any date; without one the recording is served as it is."""
    today = datetime(2027, 3, 1, 23, 59, tzinfo=UTC)
    account = "https://serpapi.com/account.json?api_key=k"
    search = "https://serpapi.com/search.json?engine=google_jobs&q=x&api_key=k"
    async with httpx.AsyncClient(transport=fixture_transport(SHIPPED, clock=lambda: today)) as c:
        renewed, served_search = (await c.get(account)).json(), (await c.get(search)).content
    async with httpx.AsyncClient(transport=fixture_transport(SHIPPED)) as c:
        recorded = (await c.get(account)).json()
    assert (renewed["plan_renewal_date"], recorded["plan_renewal_date"]) == ("2027-03-21", "2026-11-08")
    assert {**renewed, "plan_renewal_date": None} == {**recorded, "plan_renewal_date": None}
    assert served_search == fixtures.load_case(SHIPPED, "serpapi/google_jobs_hl_en")[2]  # only the account
