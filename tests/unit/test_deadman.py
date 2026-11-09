"""The dead-man switch's pure parts and its healthchecks client (spec §17.7; plan M5 Task 5, O-ops Q6, Q7,
O5): the grace of an interval, the check's uuid from the ping URL, the ping kind of a run status, the pings'
three tries at 0, 2 and 10 s that never raise, and the Management API's calls with the key.

respx serves healthchecks (`tests.support.healthchecks`; no network); the pings' sleep is injected on a fake
clock, so no test waits.
"""

import json
from collections.abc import AsyncIterator, Callable
from typing import Any, Final

import httpx
import pytest
import respx
from jobhunter.observability import deadman
from jobhunter.observability.deadman import (
    CheckState,
    DeadmanApiError,
    HealthchecksClient,
    check_uuid,
    grace_s,
    ping_kind,
)

from tests.support.clock import FakeClock
from tests.support.healthchecks import (
    API_KEY,
    API_URL,
    CHECK_URL,
    CHECK_UUID,
    FAIL_URL,
    PAUSE_URL,
    PING_URL,
    fake_healthchecks,
)

# Every status a run can have (the `runs.status` CHECK).
RUN_STATUSES: Final = (
    "queued",
    "running",
    "succeeded",
    "partial",
    "failed",
    "failed_invariant",
    "cancelled",
    "missed",
    "skipped_overlap",
)


class Sleeps:
    """The pings' sleep on `clock`: it moves the clock by what it was asked for and never waits."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.requested: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.requested.append(seconds)
        self.clock.now += seconds


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with deadman.make_http_client(user_agent="JobHunter/test") as client:
        yield client


def client_on(http: httpx.AsyncClient, sleeps: "Sleeps | None" = None) -> HealthchecksClient:
    """A client on `http`; with `sleeps`, its sleep and its monotonic clock are the fake ones."""
    if sleeps is None:
        return HealthchecksClient(http, ping_url=PING_URL, api_url=API_URL, api_key=API_KEY)
    return HealthchecksClient(
        http, ping_url=PING_URL, api_url=API_URL, api_key=API_KEY, sleep=sleeps, monotonic=sleeps.clock
    )


def test_grace_at_1h_4h_6h_24h_7d() -> None:
    # §17.7: clamp(0.25 x interval, 3,600 s, 43,200 s)
    hours = (1, 4, 6, 24, 168)
    assert [grace_s(h * 3600) for h in hours] == [3600, 3600, 5400, 21600, 43200]


def test_check_uuid_parses_the_uuid_form_and_refuses_a_slug_url() -> None:
    assert check_uuid(PING_URL) == CHECK_UUID
    mixed = "00000000-0000-4000-A000-00000000000B"
    assert check_uuid(f"https://hc-ping.com/{mixed}") == mixed.lower()  # one uuid, however it is cased
    assert check_uuid(f"https://hc.example.test:8443/ping/{CHECK_UUID}") == CHECK_UUID  # self-hosted
    refused = (
        "https://hc-ping.com/pingkeyxyz/collection",  # the slug form: `/<ping-key>/<slug>` (O5)
        f"{PING_URL}/fail",
        f"{PING_URL}/",
        "https://hc-ping.com/",
        "https://hc-ping.com/00000000000040008000000000000001",  # a uuid without its hyphens
        "https://[::1/x",  # not a URL at all
    )
    for url in refused:
        with pytest.raises(ValueError) as exc:
            check_uuid(url)
        assert url not in str(exc.value) and "hc-ping" not in str(exc.value)  # never the URL


def test_ping_kind_per_status() -> None:
    assert {status: ping_kind(status) for status in RUN_STATUSES} == {
        "queued": None,
        "running": None,
        "succeeded": "success",
        "partial": "success",  # collection is alive (§17.7)
        "failed": "fail",
        "failed_invariant": "fail",
        "cancelled": None,
        "missed": None,
        "skipped_overlap": None,
    }


async def test_ping_tries_at_0_2_10_and_never_raises(
    http: httpx.AsyncClient, log_events: Callable[[], list[dict[str, Any]]]
) -> None:
    clock = FakeClock(0.0)
    sleeps = Sleeps(clock)
    client = client_on(http, sleeps)
    tried_at: list[float] = []

    def answer(response: httpx.Response | Exception) -> Callable[[httpx.Request], httpx.Response]:
        def respond(request: httpx.Request) -> httpx.Response:
            tried_at.append(clock.now)
            assert "X-Api-Key" not in request.headers  # the key goes to the API only
            if isinstance(response, Exception):
                raise response
            return response

        return respond

    with respx.mock(assert_all_mocked=True) as router:
        success = router.get(PING_URL).mock(side_effect=answer(httpx.Response(503)))
        fail = router.get(FAIL_URL).mock(side_effect=answer(httpx.ConnectError(f"refused {FAIL_URL}")))

        assert await client.ping("success") is False  # three tries, all 503
        assert tried_at == [0.0, 2.0, 10.0] and sleeps.requested == [2.0, 8.0]
        assert await client.ping("fail") is False  # no connection: no exception either
        assert fail.call_count == 3 and tried_at[3:] == [10.0, 12.0, 20.0]
        fail.side_effect = answer(httpx.ReadTimeout(f"timed out {FAIL_URL}"))
        assert await client.ping("fail") is False
        fail.side_effect = answer(RuntimeError("anything else"))
        assert await client.ping("fail") is False

        # the first 2xx ends the tries
        outcomes = iter([httpx.Response(502), httpx.Response(200)])
        success.side_effect = lambda request: next(outcomes)
        tried = success.call_count
        assert await client.ping("success") is True
        assert success.call_count == tried + 2

        # fix round 1, M6: a try that waits out its 10 s timeout delays the next one, never more: each try
        # starts at its offset or when the one before it ended, whichever is later, so 0, 10 and 20 s
        def slow(request: httpx.Request) -> httpx.Response:
            tried_at.append(clock.now)
            clock.now += 10.0  # the try's own timeout
            raise httpx.ReadTimeout("timed out")

        fail.side_effect = slow
        clock.now, slept = 100.0, len(sleeps.requested)
        del tried_at[:]
        assert await client.ping("fail") is False
        assert tried_at == [100.0, 110.0, 120.0] and sleeps.requested[slept:] == []

        # a try that fails after 1 s: the next one still waits for its offset
        def one_second(request: httpx.Request) -> httpx.Response:
            tried_at.append(clock.now)
            clock.now += 1.0
            return httpx.Response(503)

        fail.side_effect = one_second
        clock.now = 200.0
        del tried_at[:]
        assert await client.ping("fail") is False
        assert tried_at == [200.0, 202.0, 210.0] and sleeps.requested[slept:] == [1.0, 7.0]

    failures = [event for event in log_events() if event["event"] == "deadman_ping_failed"]
    assert [(e["kind"], e["tries"], e["error_class"]) for e in failures] == [
        ("success", 3, "http_503"),
        ("fail", 3, "ConnectError"),
        ("fail", 3, "ReadTimeout"),
        ("fail", 3, "RuntimeError"),
        ("fail", 3, "ReadTimeout"),
        ("fail", 3, "http_503"),
    ]
    rendered = json.dumps(log_events())
    assert CHECK_UUID not in rendered and "hc-ping" not in rendered  # never the URL or the uuid


async def test_update_check_sends_timeout_grace_and_the_api_key(http: httpx.AsyncClient) -> None:
    client = client_on(http)
    with fake_healthchecks() as (check, router):
        await client.update_check(timeout_s=21_600, grace_s=5_400)
        assert check.updates == [{"timeout": 21_600, "grace": 5_400}]
        await client.pause()
        assert check.pauses == 1
        assert await client.read_check() == CheckState(timeout_s=21_600, grace_s=5_400, paused=True)
        assert check.api_keys_right == [True, True, True]  # every API call carries the key
        update = router.routes["update"].calls.last.request
        assert (update.method, str(update.url)) == ("POST", CHECK_URL)
        assert update.headers["Content-Type"] == "application/json"
        assert router.routes["pause"].calls.last.request.url == PAUSE_URL
        assert check.pings == []  # an API call is no ping

        check.status = "up"
        assert (await client.read_check()).paused is False
        # A check on a cron schedule reports no timeout: it reads as 0, which no period equals
        router.routes["read"].mock(return_value=httpx.Response(200, json={"status": "up", "grace": 3600}))
        assert await client.read_check() == CheckState(timeout_s=0, grace_s=3600, paused=False)

    # Each failure is a `DeadmanApiError` whose code names it, never the key or a URL
    def failing(response: httpx.Response | Exception) -> Callable[[httpx.Request], httpx.Response]:
        def respond(request: httpx.Request) -> httpx.Response:
            if isinstance(response, Exception):
                raise response
            return response

        return respond

    cases: list[tuple[httpx.Response | Exception, str]] = [
        (httpx.Response(401), "http_401"),
        (httpx.Response(500), "http_500"),
        (httpx.ConnectError(f"refused {CHECK_URL}"), "connect"),
        (httpx.ConnectTimeout(f"timed out {CHECK_URL}"), "timeout"),
        (httpx.Response(200, text="not json"), "invalid_response"),
        (httpx.Response(200, json={"status": "up"}), "invalid_response"),  # no grace
        (httpx.Response(200, json=["up"]), "invalid_response"),
        # fix round 1, M5: any other request error, and a body that is no check, take the failure path
        (httpx.DecodingError(f"undecodable body from {CHECK_URL}"), "invalid_response"),  # a RequestError
        (httpx.Response(200, json={"status": None, "grace": 3600}), "invalid_response"),
        (httpx.Response(200, json={"status": "up", "grace": "1h"}), "invalid_response"),
        (httpx.Response(200, content=b"\xff\xfe not utf-8"), "invalid_response"),
    ]
    for response, code in cases:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            router.get(CHECK_URL).mock(side_effect=failing(response))
            router.post(CHECK_URL).mock(side_effect=failing(response))
            with pytest.raises(DeadmanApiError) as exc:
                await client.read_check()
            assert exc.value.code == code
            assert API_KEY not in str(exc.value) and CHECK_UUID not in str(exc.value)
            # never chained to httpx's error, whose message names the URL
            assert exc.value.__cause__ is None
            assert exc.value.__context__ is None or exc.value.__suppress_context__
            if isinstance(response, Exception) or not response.is_success:
                with pytest.raises(DeadmanApiError) as update_exc:
                    await client.update_check(timeout_s=3600, grace_s=3600)
                assert update_exc.value.code == code
