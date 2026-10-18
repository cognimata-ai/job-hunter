# tests/unit/test_http.py
import asyncio
import itertools
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from jobhunter.config import load_config
from jobhunter.sources import http
from jobhunter.sources.base import ContractViolation
from jobhunter.sources.http import STORED_HEADERS, configure, make_client, send, stored_headers

from tests.unit.test_sources_base import SPEC


def NOW() -> datetime:
    return datetime(2026, 10, 16, 9, 47, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _restore_http_configuration() -> Iterator[None]:
    """`configure` is process-wide; give every test the default configuration back."""
    yield
    http._reset_configuration()


def test_timeouts_user_agent_and_no_redirects() -> None:
    c, a = make_client("jsearch"), make_client("arbeitnow")
    assert (c.timeout.connect, c.timeout.read, c.timeout.write, c.timeout.pool) == (5.0, 30.0, 10.0, 5.0)
    assert a.timeout.read == 60.0
    assert c.headers["user-agent"] == "JobHunter/1.0 (personal job search)" and c.follow_redirects is False


def test_full_board_sources_read_for_60_s_and_unknown_sources_are_refused() -> None:
    assert {s: make_client(s).timeout.read for s in ("greenhouse", "personio", "serpapi", "adzuna")} == {
        "greenhouse": 60.0,
        "personio": 60.0,
        "serpapi": 30.0,
        "adzuna": 30.0,
    }
    with pytest.raises(ValueError, match="unknown source"):
        make_client("jsaerch")


@respx.mock
async def test_send_classifies_transport_outcomes() -> None:
    respx.get("https://api.example/ok").mock(
        return_value=httpx.Response(
            200, json={"a": 1}, headers={"Set-Cookie": "s=1", "X-RateLimit-Requests-Remaining": "199"}
        )
    )
    respx.get("https://api.example/conn").mock(side_effect=httpx.ConnectError("refused"))
    respx.get("https://api.example/slow").mock(side_effect=httpx.ReadTimeout("slow"))
    respx.get("https://api.example/moved").mock(
        return_value=httpx.Response(301, headers={"Location": "https://elsewhere"})
    )
    async with make_client("jsearch") as c:
        ok = await send(c, SPEC, c.build_request("GET", "https://api.example/ok"), now=NOW)
        assert ok.page is not None and ok.error is None
        assert ok.page.headers == {
            "x-ratelimit-requests-remaining": "199",
            "content-type": "application/json",
        }
        assert ok.page.json() == {"a": 1} and (ok.page.status, ok.page.fetched_at, ok.page.spec) == (
            200,
            NOW(),
            SPEC,
        )
        conn = await send(c, SPEC, c.build_request("GET", "https://api.example/conn"), now=NOW)
        assert conn.error is not None and (conn.error.kind, conn.error.before_send) == ("transient", True)
        assert conn.page is None
        slow = await send(c, SPEC, c.build_request("GET", "https://api.example/slow"), now=NOW)
        assert slow.error is not None and (slow.error.kind, slow.error.before_send) == ("ambiguous", False)
        moved = await send(c, SPEC, c.build_request("GET", "https://api.example/moved"), now=NOW)
        assert moved.page is not None and moved.page.status == 301 and moved.error is None
    assert respx.calls.call_count == 4  # the 301 was not followed


@respx.mock
async def test_html_body_and_oversized_body_are_contract_failures() -> None:  # Review Focus 3
    respx.get("https://api.example/html").mock(
        return_value=httpx.Response(
            200, text="<html><body>Service Unavailable</body></html>", headers={"content-type": "text/html"}
        )
    )
    respx.get("https://api.example/big").mock(return_value=httpx.Response(200, content=b"x" * 10_000))
    async with make_client("jsearch", max_body_bytes=8_000) as c:
        html = await send(c, SPEC, c.build_request("GET", "https://api.example/html"), now=NOW)
        assert html.page is not None
        with pytest.raises(ContractViolation, match="not_json"):
            html.page.json()
        big = await send(c, SPEC, c.build_request("GET", "https://api.example/big"), now=NOW)
        assert isinstance(big.error, ContractViolation) and big.error.code == "body_too_large"
        assert big.page is not None and len(big.page.body) == 4096


@respx.mock
async def test_a_body_at_the_cap_is_read_whole() -> None:
    respx.get("https://api.example/exact").mock(return_value=httpx.Response(200, content=b"x" * 8_000))
    async with make_client("jsearch", max_body_bytes=8_000) as c:
        out = await send(c, SPEC, c.build_request("GET", "https://api.example/exact"), now=NOW)
    assert out.error is None and out.page is not None and len(out.page.body) == 8_000


async def test_a_body_that_does_not_decode_is_a_contract_failure() -> None:
    def gzip_that_is_not(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(b"not gzip"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(gzip_that_is_not)) as c:
        out = await send(c, SPEC, c.build_request("GET", "https://api.example/gz"), now=NOW)
    assert isinstance(out.error, ContractViolation) and out.error.code == "undecodable_body"
    assert out.page is not None and out.page.status == 200


@respx.mock
async def test_lost_responses_are_ambiguous() -> None:
    respx.get("https://api.example/lost").mock(side_effect=httpx.RemoteProtocolError("peer closed"))
    respx.get("https://api.example/write").mock(side_effect=httpx.WriteTimeout("write"))
    respx.get("https://api.example/pool").mock(side_effect=httpx.PoolTimeout("pool"))
    async with make_client("jsearch") as c:
        for path in ("lost", "write", "pool"):
            out = await send(c, SPEC, c.build_request("GET", f"https://api.example/{path}"), now=NOW)
            assert out.page is None and out.error is not None
            assert (out.error.kind, out.error.before_send) == ("ambiguous", False)


async def test_the_total_deadline_ends_the_attempt_as_ambiguous() -> None:
    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()  # never set: only the deadline ends this
        raise AssertionError("unreachable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(hang)) as c:
        out = await send(
            c, SPEC, c.build_request("GET", "https://api.example/hang"), now=NOW, deadline_s=0.01
        )
    assert out.page is None and out.error is not None
    assert (out.error.kind, out.error.code, out.error.before_send) == ("ambiguous", "deadline", False)


def redirecting(chain: dict[str, str], final: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        target = chain.get(str(request.url))
        return final if target is None else httpx.Response(302, headers={"Location": target})

    return handle


async def test_personio_redirects_are_followed_within_the_host_family_only() -> None:
    final = httpx.Response(200, content=b"<xml/>", headers={"content-type": "application/xml"})
    within = {
        "https://acme.jobs.personio.de/xml": "https://acme-gmbh.jobs.personio.de/xml",
        "https://acme-gmbh.jobs.personio.de/xml": "/xml?language=de",
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(redirecting(within, final))) as c:
        out = await send(c, SPEC, c.build_request("GET", "https://acme.jobs.personio.de/xml"), now=NOW)
    assert out.error is None and out.page is not None and (out.page.status, out.page.body) == (200, b"<xml/>")

    outside = {"https://acme.jobs.personio.de/xml": "https://acme.jobs.personio.com/xml"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(redirecting(outside, final))) as c:
        out = await send(c, SPEC, c.build_request("GET", "https://acme.jobs.personio.de/xml"), now=NOW)
    assert out.page is not None and out.page.status == 302  # leaving the family: the 3xx is the page


async def test_send_follows_no_other_redirect_even_on_a_client_that_would() -> None:
    final = httpx.Response(200)
    chain = {"https://api.example/a": "https://api.example/b"}
    transport = httpx.MockTransport(redirecting(chain, final))
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as c:
        out = await send(c, SPEC, c.build_request("GET", "https://api.example/a"), now=NOW)
    assert out.page is not None and out.page.status == 302


async def test_personio_redirects_stop_after_three() -> None:
    hops = [f"https://h{i}.jobs.personio.de/xml" for i in range(6)]
    chain = dict(itertools.pairwise(hops))
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return redirecting(chain, httpx.Response(200))(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as c:
        out = await send(c, SPEC, c.build_request("GET", hops[0]), now=NOW)
    assert out.page is not None and out.page.status == 302 and seen == hops[:4]


def test_stored_headers_keep_the_allowlist_only() -> None:
    assert STORED_HEADERS == (
        "date",
        "content-type",
        "x-ratelimit-*",
        "x-rapidapi-proxy-response",
        "retry-after",
        "age",
        "cf-cache-status",
        "cache-control",
    )
    received = httpx.Headers(
        [
            ("Date", "Fri, 16 Oct 2026 09:47:00 GMT"),
            ("X-RateLimit-Limit", "200"),
            ("x-ratelimit-reset", "60"),
            ("X-RapidAPI-Proxy-Response", "true"),
            ("Retry-After", "5"),
            ("Age", "1"),
            ("CF-Cache-Status", "MISS"),
            ("Cache-Control", "no-store"),
            ("Set-Cookie", "session=1"),
            ("Authorization", "Bearer abc"),
            ("X-RapidAPI-Key", "k"),
            ("X-RateLimiter", "x"),  # not an `x-ratelimit-` header
            ("Content-Length", "3"),
        ]
    )
    assert stored_headers(received) == {
        "date": "Fri, 16 Oct 2026 09:47:00 GMT",
        "x-ratelimit-limit": "200",
        "x-ratelimit-reset": "60",
        "x-rapidapi-proxy-response": "true",
        "retry-after": "5",
        "age": "1",
        "cf-cache-status": "MISS",
        "cache-control": "no-store",
    }


@respx.mock
async def test_the_request_log_names_source_path_and_query_without_credentials(
    log_events: Callable[[], list[dict[str, Any]]],
) -> None:
    respx.get("https://api.adzuna.com/v1/api/jobs/de/search/1").mock(
        return_value=httpx.Response(200, json={})
    )
    url = "https://api.adzuna.com/v1/api/jobs/de/search/1?app_id=ID-1234567&app_key=KEY-abcdefgh&what=python"
    async with make_client("adzuna") as c:
        await send(c, SPEC, c.build_request("GET", url), now=NOW)
    (event,) = [e for e in log_events() if e["event"] == "source_request"]
    assert (event["source_instance_id"], event["cell_id"], event["page_no"]) == (1, 7, 1)
    assert (event["source_id"], event["path"], event["query"], event["status"]) == (
        "adzuna",
        "/v1/api/jobs/de/search/1",
        "what=python",
        200,
    )
    rendered = str(log_events())
    assert "ID-1234567" not in rendered and "KEY-abcdefgh" not in rendered and "app_key" not in rendered


@respx.mock
async def test_configure_sets_the_user_agent_and_the_fixture_transport_in_fake_mode(tmp_path: Path) -> None:
    respx.get("https://jsearch.p.rapidapi.com/search-v2").mock(return_value=httpx.Response(418))
    (tmp_path / "jsearch").mkdir()
    (tmp_path / "jsearch/search_v2_page1.json").write_text('{"status": "OK", "data": []}')
    env = {
        "JOBHUNTER_ENV": "test",
        "JOBHUNTER_DB_NAME": "jobhunter",
        "JOBHUNTER_HTTP_USER_AGENT": "JobHunter/test",
        "JOBHUNTER_FAKE_SOURCES": "1",
        "JOBHUNTER_FIXTURES_DIR": str(tmp_path),
    }
    configure(load_config("worker", env, tmp_path / "no-secrets"))
    async with make_client("jsearch") as c:
        assert c.headers["user-agent"] == "JobHunter/test"
        out = await send(c, SPEC, c.build_request("GET", "https://jsearch.p.rapidapi.com/search-v2"), now=NOW)
    assert out.page is not None and out.page.json() == {"status": "OK", "data": []}  # served by the fixtures
    assert respx.calls.call_count == 0

    del env["JOBHUNTER_FAKE_SOURCES"]
    configure(load_config("worker", env, tmp_path / "no-secrets"))
    async with make_client("jsearch") as c:
        out = await send(c, SPEC, c.build_request("GET", "https://jsearch.p.rapidapi.com/search-v2"), now=NOW)
    assert out.page is not None and out.page.status == 418 and respx.calls.call_count == 1  # a real transport


@respx.mock
async def test_send_works_with_a_plain_client_and_the_default_cap() -> None:
    respx.get("https://api.example/ok").mock(return_value=httpx.Response(200, json=[1]))
    async with httpx.AsyncClient() as c:
        out = await send(c, SPEC, c.build_request("GET", "https://api.example/ok"), now=NOW)
    assert out.error is None and out.page is not None and out.page.json() == [1]
