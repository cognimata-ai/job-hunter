"""The edge's login limit (plan M5 Task 21, Deviation 34; M4 Ruling T17-b): stock Caddy has no rate limiter,
so the api's edge refuses a `POST /api/auth/login` past `LOGIN_LIMIT_PER_IP` (10 a minute from one client
address) or `LOGIN_LIMIT_GLOBAL` (100 a minute in all) with 429 `throttled` and `Retry-After`, before the
request's body is read and before any database work, in memory (one api replica). A refused login is not
counted. §16.3's database throttle, behind it, is unchanged.

Offline: the edge around a recording ASGI app, driven with raw ASGI messages, at an injected monotonic clock.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Final, cast

import pytest
from jobhunter.api import edge
from jobhunter.api.edge import LOGIN_LIMIT_GLOBAL, LOGIN_LIMIT_PER_IP, EdgeMiddleware, LoginLimiter
from jobhunter.config import AppConfig
from starlette.types import Message, Receive, Scope, Send

ORIGIN: Final = "https://testserver"
BODY: Final = json.dumps({"username": "ada", "password": "Tannenbaum-Ölfass-7"}).encode()


@dataclass
class Inner:
    """The app behind the edge: reads the body, answers 204, and counts what reached it."""

    calls: int = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.calls += 1
        while (await receive()).get("more_body", False):
            pass
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})


class NoDatabase:
    """The edge's engine: any use of it is database work, which a refused login must never do."""

    def connect(self) -> Any:
        raise AssertionError("the edge touched the database")

    def begin(self) -> Any:
        raise AssertionError("the edge touched the database")


@dataclass
class Exchange:
    """One request's server side: how many body messages the edge took, and what it sent."""

    taken: int = 0
    sent: list[Message] = field(default_factory=list)

    async def receive(self) -> Message:
        self.taken += 1
        return {"type": "http.request", "body": BODY, "more_body": False}

    async def send(self, message: Message) -> None:
        self.sent.append(message)

    @property
    def status(self) -> int:
        return int(self.sent[0]["status"])

    @property
    def headers(self) -> dict[str, str]:
        return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in self.sent[0]["headers"]}

    @property
    def problem(self) -> Any:
        return json.loads(b"".join(m.get("body", b"") for m in self.sent[1:]))


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(edge, "monotonic", clock)
    return clock


@pytest.fixture
def inner() -> Inner:
    return Inner()


@pytest.fixture
def api(inner: Inner) -> EdgeMiddleware:
    """The edge with an engine it must never use: a login, admitted or not, reads nothing at the edge (its
    route does, behind it), and a refused one never reaches the route."""
    cfg = cast(AppConfig, SimpleNamespace(public_origin=ORIGIN))
    return EdgeMiddleware(inner, config=lambda: cfg, engine=lambda: cast(Any, NoDatabase()))


@pytest.fixture
def api_without_engine(inner: Inner) -> EdgeMiddleware:
    """The edge without an engine (the state not set yet): it then reads no fence, for any route."""
    cfg = cast(AppConfig, SimpleNamespace(public_origin=ORIGIN))
    return EdgeMiddleware(inner, config=lambda: cfg, engine=lambda: None)


async def call(
    api: EdgeMiddleware, address: str, *, method: str = "POST", path: str = "/api/auth/login"
) -> Exchange:
    headers = [(b"origin", ORIGIN.encode()), (b"content-type", b"application/json")]
    if path != "/api/auth/login":
        headers.append((b"x-jh-request", b"1"))
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [*headers, (b"content-length", str(len(BODY)).encode())],
        "query_string": b"",
        "client": (address, 50000),
    }
    exchange = Exchange()
    await api(scope, exchange.receive, exchange.send)
    return exchange


def assert_throttled(exchange: Exchange, retry_after_s: int) -> None:
    assert exchange.status == 429
    assert exchange.taken == 0  # not a byte of the body was read
    problem = exchange.problem
    assert (problem["code"], problem["status"], problem["title"]) == ("throttled", 429, "Too many logins")
    assert problem["retry_after_s"] == retry_after_s == int(exchange.headers["retry-after"])
    assert exchange.headers["x-content-type-options"] == "nosniff"  # an edge response like any other


def test_the_limits_are_ten_a_minute_from_one_address_and_a_hundred_in_all() -> None:
    assert (LOGIN_LIMIT_PER_IP, LOGIN_LIMIT_GLOBAL) == ((10, 60.0), (100, 60.0))


async def test_the_eleventh_login_in_a_minute_from_one_address_is_429_before_the_body(
    api: EdgeMiddleware, inner: Inner, clock: Clock
) -> None:
    for n in range(10):
        clock.now = 1000.0 + n
        assert (await call(api, "198.51.100.7")).status == 204
    assert inner.calls == 10
    clock.now = 1012.5
    assert_throttled(await call(api, "198.51.100.7"), retry_after_s=48)  # the first falls out at 1060
    assert inner.calls == 10  # the app never saw it: no route, no database work
    assert (await call(api, "198.51.100.8")).status == 204  # another address has a window of its own
    assert (await call(api, "::ffff:198.51.100.7")).status == 429  # the same address, IPv4-mapped
    clock.now = 1059.9
    assert_throttled(await call(api, "198.51.100.7"), retry_after_s=1)  # at least one second
    # The first login's minute is over: one more gets through, and the window is full again. The three refused
    # logins of that minute (at 1012.5 and 1059.9) were not counted: had they been, it would still be full.
    clock.now = 1060.0
    assert (await call(api, "198.51.100.7")).status == 204
    assert (await call(api, "198.51.100.7")).status == 429
    # A minute later the window is empty: ten logins, and the eleventh refused again.
    clock.now = 1130.0
    for _ in range(10):
        assert (await call(api, "198.51.100.7")).status == 204
    assert (await call(api, "198.51.100.7")).status == 429


async def test_the_global_window(api: EdgeMiddleware, inner: Inner, clock: Clock) -> None:
    for n in range(100):
        clock.now = 1000.0 + n * 0.25
        assert (await call(api, f"10.0.{n // 50}.{n % 50 + 1}")).status == 204
    clock.now = 1030.0
    assert_throttled(await call(api, "203.0.113.9"), retry_after_s=30)  # a new address, refused in all
    assert inner.calls == 100
    clock.now = 1060.0  # the first of the hundred is out of the window
    assert (await call(api, "203.0.113.9")).status == 204
    assert (await call(api, "203.0.113.10")).status == 429


async def test_other_routes_are_not_limited(
    api_without_engine: EdgeMiddleware, inner: Inner, clock: Clock
) -> None:
    api = api_without_engine
    for _ in range(30):
        assert (await call(api, "198.51.100.7", path="/api/auth/logout")).status == 204
        assert (await call(api, "198.51.100.7", method="GET", path="/api/auth/session")).status == 204
        assert (await call(api, "198.51.100.7", method="GET")).status == 204  # not a login: no POST
        assert (await call(api, "198.51.100.7", path="/api/auth/login/")).status == 204  # another path
    assert inner.calls == 120
    for _ in range(10):  # and none of them counted towards the login's window
        assert (await call(api, "198.51.100.7")).status == 204
    assert (await call(api, "198.51.100.7")).status == 429


def test_the_windows_of_quiet_addresses_are_dropped() -> None:
    """The memory stays bounded: once a window has passed, the addresses with no login in it are dropped (at
    the first login after the window, at most once per window)."""
    limiter = LoginLimiter()
    for n in range(50):
        assert limiter.admit(f"10.0.0.{n + 1}", 1000.0 + n * 0.1) is None
    assert limiter.admit("10.0.1.1", 1059.0) is None
    assert len(limiter) == 51  # within the window: nothing is dropped
    assert limiter.admit("10.0.1.2", 1065.0) is None
    assert len(limiter) == 2  # the fifty quiet ones went; 10.0.1.1 (at 1059) and the new one stay
    assert limiter.admit("10.0.1.3", 1070.0) is None and len(limiter) == 3  # no sweep before 1125


async def test_each_refusal_is_logged_with_its_address_and_nothing_else_is(
    api: EdgeMiddleware, clock: Clock, log_events: Callable[[], list[dict[str, Any]]]
) -> None:
    """A refused login is a sign-in event: logged at warning as `login`, outcome `rate_limited`, with the
    client address (as the login logs its own outcomes, ruling T16-b). An admitted one is logged by its route,
    not here."""
    for _ in range(10):
        assert (await call(api, "198.51.100.7")).status == 204
    assert [e for e in log_events() if e["logger"] == "jobhunter.api.edge"] == []
    assert (await call(api, "::ffff:198.51.100.7")).status == 429
    [refusal] = [e for e in log_events() if e["logger"] == "jobhunter.api.edge"]
    assert {k: refusal[k] for k in ("event", "outcome", "ip", "level")} == {
        "event": "login",
        "outcome": "rate_limited",
        "ip": "198.51.100.7",  # canonical: the IPv4-mapped form as IPv4
        "level": "warning",
    }
    assert set(refusal) <= {"event", "outcome", "ip", "level", "logger", "ts", "request_id"}
