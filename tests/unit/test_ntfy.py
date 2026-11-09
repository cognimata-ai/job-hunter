"""The ntfy client (spec §17.6 L5700-5704; plan M5 Task 4, O3, O22a).

An alert is pushed as `POST <topic URL>` with the alert's message as its UTF-8 body and four headers, all
ASCII: `Title: Job Hunter - <code>` (an ASCII hyphen, not the spec's em dash: O3), `Priority` 5 or 3,
`Tags: <severity>` and `Click: <public origin>/settings#alerts` (O22a), plus `Authorization: Bearer <token>`
only when the `ntfy_token` secret is set. A non-2xx answer, a timeout and a failed connection each raise
`NtfyError` with a code; a delivery is four tries, 5, 30 and 120 s apart. respx serves the topic: no network.
"""

from datetime import timedelta
from typing import Final

import httpx
import pytest
import respx
from jobhunter.observability import ntfy
from jobhunter.observability.ntfy import DueAlert, NtfyClient, NtfyError, deliver, ntfy_headers

TOPIC: Final = "https://ntfy.test/jh-alerts-4f2a9c"
ORIGIN: Final = "https://jobs.example.test"
TOKEN: Final = "tk_test_0123456789abcdef"


def due(
    severity: ntfy.PushSeverity = "critical", *, code: str = "run.failed", message: str = "m"
) -> DueAlert:
    return DueAlert(id=7, code=code, severity=severity, message=message)


class Sleeps:
    """An injected `sleep` that records what it was asked for and never waits."""

    def __init__(self) -> None:
        self.requested: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.requested.append(seconds)


def test_the_constants_are_the_brief_s() -> None:
    assert ntfy.NTFY_RETRY_DELAYS_S == (5.0, 30.0, 120.0)  # four tries (§17.6: "retries 3 times")
    assert (ntfy.SEND_TIMEOUT_S, ntfy.DELIVERY_FAILED) == (10.0, "notify.delivery_failed")
    assert (ntfy.DISPATCH_INTERVAL_S, ntfy.MAX_SENDS_PER_PASS) == (30.0, 10)
    assert dict(ntfy.RENOTIFY_AFTER) == {"warn": timedelta(hours=6), "critical": timedelta(hours=1)}
    assert (ntfy.PAUSE_FIRST, ntfy.PAUSE_MAX) == (timedelta(minutes=5), timedelta(minutes=60))


def test_headers_are_ascii_with_an_ascii_hyphen_title() -> None:
    headers = ntfy_headers(due(code="backup.stale"), public_origin=ORIGIN, token=None)
    assert headers == {
        "Title": "Job Hunter - backup.stale",
        "Priority": "5",
        "Tags": "critical",
        "Click": f"{ORIGIN}/settings#alerts",
    }
    assert all(name.isascii() and value.isascii() for name, value in headers.items())
    assert not {"\N{EM DASH}", "\N{EN DASH}"} & set(headers["Title"])  # the spec's em dash is not ASCII (O3)


def test_priority_5_for_critical_and_3_for_warn() -> None:
    critical = ntfy_headers(due("critical"), public_origin=ORIGIN, token=None)
    warn = ntfy_headers(due("warn"), public_origin=ORIGIN, token=None)
    assert (critical["Priority"], critical["Tags"]) == ("5", "critical")
    assert (warn["Priority"], warn["Tags"]) == ("3", "warn")


def test_click_points_at_settings_alerts() -> None:
    other = "http://127.0.0.1:8080"
    assert ntfy_headers(due(), public_origin=other, token=None)["Click"] == f"{other}/settings#alerts"


async def test_authorization_only_with_a_token() -> None:
    assert ntfy_headers(due(), public_origin=ORIGIN, token=TOKEN)["Authorization"] == f"Bearer {TOKEN}"
    assert "Authorization" not in ntfy_headers(due(), public_origin=ORIGIN, token=None)
    with respx.mock(assert_all_called=True) as router:
        route = router.post(TOPIC).mock(return_value=httpx.Response(200))
        async with httpx.AsyncClient() as http:
            for token in (TOKEN, None):
                await NtfyClient(http, topic_url=TOPIC, token=token, public_origin=ORIGIN).publish(due())
    with_token, without = (call.request for call in route.calls)
    assert with_token.headers["Authorization"] == f"Bearer {TOKEN}"
    assert "Authorization" not in without.headers


async def test_publish_posts_the_message_as_utf_8_to_the_topic_within_the_send_timeout() -> None:
    message = "Die Sicherung ist älter als 36 h"
    with respx.mock(assert_all_called=True) as router:
        route = router.post(TOPIC).mock(return_value=httpx.Response(200))
        async with httpx.AsyncClient() as http:
            client = NtfyClient(http, topic_url=TOPIC, token=None, public_origin=ORIGIN)
            await client.publish(due("warn", code="backup.stale", message=message))
    [call] = route.calls
    request = call.request
    assert (request.method, str(request.url), request.content) == ("POST", TOPIC, message.encode("utf-8"))
    assert request.headers["Title"] == "Job Hunter - backup.stale" and request.headers["Priority"] == "3"
    assert request.extensions["timeout"] == dict.fromkeys(("connect", "read", "write", "pool"), 10.0)


async def test_four_tries_with_backoff_5_30_120() -> None:
    with respx.mock(assert_all_called=True) as router:
        route = router.post(TOPIC)
        async with httpx.AsyncClient() as http:
            client = NtfyClient(http, topic_url=TOPIC, token=None, public_origin=ORIGIN)
            # three refusals, then the fourth try is delivered
            route.side_effect = [httpx.Response(503)] * 3 + [httpx.Response(200)]
            recovering = Sleeps()
            await deliver(client, due(), sleep=recovering)
            assert (route.call_count, recovering.requested) == (4, [5.0, 30.0, 120.0])
            # four refusals: the last one's error, and no sleep after the last try
            route.reset()
            route.side_effect = None
            route.return_value = httpx.Response(429)
            failing = Sleeps()
            with pytest.raises(NtfyError) as refused:
                await deliver(client, due(), sleep=failing)
            assert (route.call_count, failing.requested, refused.value.code) == (
                4,
                [5.0, 30.0, 120.0],
                "http_429",
            )
            # the first try delivered: no sleep at all
            route.reset()
            route.return_value = httpx.Response(200)
            at_once = Sleeps()
            await deliver(client, due(), sleep=at_once)
            assert (route.call_count, at_once.requested) == (1, [])


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (httpx.ConnectError("refused"), "connect"),
        (httpx.ReadError("reset by peer"), "connect"),  # a connection lost before the answer
        (httpx.ConnectTimeout("slow connect"), "timeout"),
        (httpx.ReadTimeout("slow answer"), "timeout"),
        (httpx.Response(401), "http_401"),
        (httpx.Response(413), "http_413"),
        (httpx.Response(500), "http_500"),
        (httpx.Response(302, headers={"Location": "https://elsewhere.test/"}), "http_302"),  # no redirects
    ],
)
async def test_ntfy_error_codes(outcome: Exception | httpx.Response, code: str) -> None:
    with respx.mock(assert_all_called=True) as router:
        router.post(TOPIC).mock(side_effect=[outcome])
        async with httpx.AsyncClient() as http:
            client = NtfyClient(http, topic_url=TOPIC, token=TOKEN, public_origin=ORIGIN)
            with pytest.raises(NtfyError) as refused:
                await client.publish(due())
    assert refused.value.code == code
    assert TOPIC not in str(refused.value) and TOKEN not in str(refused.value)  # the code only


@pytest.mark.parametrize("status", [200, 201, 204])
async def test_any_2xx_is_delivered(status: int) -> None:
    with respx.mock(assert_all_called=True) as router:
        router.post(TOPIC).mock(return_value=httpx.Response(status))
        async with httpx.AsyncClient() as http:
            await NtfyClient(http, topic_url=TOPIC, token=None, public_origin=ORIGIN).publish(due())


async def test_the_http_client_follows_no_redirect_and_sends_the_user_agent() -> None:
    async with ntfy.make_http_client(user_agent="JobHunter/1.0 (test)") as http:
        assert http.follow_redirects is False and http.headers["User-Agent"] == "JobHunter/1.0 (test)"
        assert http.timeout == httpx.Timeout(10.0)
