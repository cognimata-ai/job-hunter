"""A healthchecks.io stand-in on respx (M5 Task 5; plan Global Constraints: no paid or real call anywhere).

`fake_healthchecks()` serves, in process, the ping URL and its `/fail`, and the Management API's three calls
on the one check: its read (`GET <api>/checks/<uuid>`), its update (`POST`, JSON `timeout` and `grace`) and
its pause (`POST …/pause`); the routes are named `ping`, `fail`, `read`, `update` and `pause`. A request to
any other URL fails (`assert_all_mocked`). `FakeCheck` is the check as healthchecks keeps it: a ping resumes a
paused check and marks it `up`, a `/fail` ping marks it `down`. It records what it was sent: the pings by
kind, the updates' bodies, the pauses, and per API call only whether an `X-Api-Key` header was present and
right (never a value elsewhere). `api_status` and `ping_status`, when set to a non-2xx, are what every API
call or ping answers, changing nothing.

The uuid, the URLs and the key are low-entropy stand-ins: no secret scanner reads them as credentials.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Final

import httpx
import respx

CHECK_UUID: Final = "00000000-0000-4000-8000-000000000001"
PING_URL: Final = f"https://hc-ping.test/{CHECK_UUID}"
FAIL_URL: Final = f"{PING_URL}/fail"
API_URL: Final = "https://healthchecks.test/api/v3"
CHECK_URL: Final = f"{API_URL}/checks/{CHECK_UUID}"
PAUSE_URL: Final = f"{CHECK_URL}/pause"
API_KEY: Final = "hc-test-key"


@dataclass
class FakeCheck:
    """The one check (module docstring); its fields are what healthchecks reports."""

    timeout: int = 86_400
    grace: int = 3_600
    status: str = "new"
    api_status: int = 200
    ping_status: int = 200
    pings: list[str] = field(default_factory=list)
    updates: list[dict[str, object]] = field(default_factory=list)
    pauses: int = 0
    api_keys_right: list[bool] = field(default_factory=list)  # per API call: the key header present and right

    def as_json(self) -> dict[str, object]:
        """The check as `GET <api>/checks/<uuid>` answers it (the fields the worker reads, and a few more)."""
        return {
            "name": "collection",
            "slug": "collection",
            "status": self.status,
            "timeout": self.timeout,
            "grace": self.grace,
            "n_pings": len(self.pings),
        }

    def _api(self, request: httpx.Request) -> httpx.Response | None:
        """The key check and the scripted failure of an API call; `None` when the call goes on."""
        right = request.headers.get("X-Api-Key") == API_KEY
        self.api_keys_right.append(right)
        if not right:
            return httpx.Response(401, json={"error": "missing api key"})
        if self.api_status != 200:
            return httpx.Response(self.api_status)
        return None

    def read(self, request: httpx.Request) -> httpx.Response:
        return self._api(request) or httpx.Response(200, json=self.as_json())

    def update(self, request: httpx.Request) -> httpx.Response:
        if (refused := self._api(request)) is not None:
            return refused
        body = json.loads(request.content)
        self.updates.append(body)
        self.timeout, self.grace = int(body["timeout"]), int(body["grace"])
        return httpx.Response(200, json=self.as_json())

    def pause(self, request: httpx.Request) -> httpx.Response:
        if (refused := self._api(request)) is not None:
            return refused
        self.pauses += 1
        self.status = "paused"
        return httpx.Response(200, json=self.as_json())

    def ping(self, kind: str) -> httpx.Response:
        if self.ping_status != 200:
            return httpx.Response(self.ping_status)
        self.pings.append(kind)
        self.status = "up" if kind == "success" else "down"
        return httpx.Response(200, text="OK")


@contextmanager
def fake_healthchecks(check: FakeCheck | None = None) -> Iterator[tuple[FakeCheck, respx.MockRouter]]:
    """`check` (a new one by default) served on respx, with its router (module docstring)."""
    served = FakeCheck() if check is None else check
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        router.get(PING_URL, name="ping").mock(side_effect=lambda request: served.ping("success"))
        router.get(FAIL_URL, name="fail").mock(side_effect=lambda request: served.ping("fail"))
        router.get(CHECK_URL, name="read").mock(side_effect=served.read)
        router.post(CHECK_URL, name="update").mock(side_effect=served.update)
        router.post(PAUSE_URL, name="pause").mock(side_effect=served.pause)
        yield served, router
