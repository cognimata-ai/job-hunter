"""The edge's request-body ceiling (final review FR-4; spec §14.1): every request body but the CV upload's and
the paste's, which read their own under their own limits, is bounded at `MAX_BODY_BYTES` (1 MiB) before the
app sees it. A declared `Content-Length` over the limit is refused before any byte is read; a body without
one (chunked) is counted as it streams and refused as soon as it passes the limit. Either is a 413
`upload_rejected` (`too_large` on `body`), the code the upload routes give a body that is too large, and no
alert. A body within the limit reaches the app whole, in its messages.

Offline: the edge around a recording ASGI app, driven with raw ASGI messages.
"""

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Final, cast

import pytest
from jobhunter.api.edge import MAX_BODY_BYTES, OWN_BODY_LIMITS, EdgeMiddleware
from jobhunter.config import AppConfig
from starlette.types import Message, Receive, Scope, Send

ORIGIN: Final = "https://testserver"
CSRF: Final = [(b"origin", ORIGIN.encode()), (b"x-jh-request", b"1"), (b"content-type", b"application/json")]
CHUNK: Final = 64 * 1024


@dataclass
class Inner:
    """The app behind the edge: it reads the whole body and answers 204, recording what it read."""

    calls: int = 0
    body: bytes = b""
    messages: list[Message] = field(default_factory=list)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.calls += 1
        while True:
            message = await receive()
            self.messages.append(message)
            self.body += message.get("body", b"")
            if message["type"] != "http.request" or not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})


@dataclass
class Client:
    """The server side of one request: the body's messages, how many were taken, and what was sent."""

    chunks: list[bytes]
    taken: int = 0
    sent: list[Message] = field(default_factory=list)

    async def receive(self) -> Message:
        index, self.taken = self.taken, self.taken + 1
        if index >= len(self.chunks):
            return {"type": "http.disconnect"}
        return {"type": "http.request", "body": self.chunks[index], "more_body": index < len(self.chunks) - 1}

    async def send(self, message: Message) -> None:
        self.sent.append(message)

    @property
    def status(self) -> int:
        return int(self.sent[0]["status"])

    @property
    def problem(self) -> Any:
        return json.loads(b"".join(m.get("body", b"") for m in self.sent[1:]))


def chunked(size: int) -> list[bytes]:
    body = b"x" * size
    return [body[i : i + CHUNK] for i in range(0, len(body), CHUNK)] or [b""]


async def request(
    method: str, path: str, chunks: list[bytes], *, declared: int | None = None
) -> tuple[Inner, Client]:
    inner, client = Inner(), Client(chunks)
    cfg = cast(AppConfig, SimpleNamespace(public_origin=ORIGIN))
    edge = EdgeMiddleware(inner, config=lambda: cfg, engine=lambda: None)
    headers = [*CSRF, *([] if declared is None else [(b"content-length", str(declared).encode())])]
    scope = {"type": "http", "method": method, "path": path, "headers": headers, "query_string": b""}
    await edge(scope, client.receive, client.send)
    return inner, client


def assert_too_large(client: Client) -> None:
    assert client.status == 413
    problem = client.problem
    assert (problem["code"], problem["status"]) == ("upload_rejected", 413)
    assert [(e["field"], e["code"]) for e in problem["errors"]] == [("body", "too_large")]


def test_the_limit_is_one_mib_and_the_uploads_read_their_own() -> None:
    assert MAX_BODY_BYTES == 2**20
    assert OWN_BODY_LIMITS == {("POST", "/api/profile/cv"), ("POST", "/api/profile/cv-text")}


async def test_a_declared_length_over_the_limit_is_413_before_any_byte_is_read() -> None:
    inner, client = await request("PUT", "/api/profile/drafts/1", chunked(2 * 2**20), declared=2 * 2**20)
    assert_too_large(client)
    assert (inner.calls, client.taken) == (0, 0)


@pytest.mark.parametrize(("method", "path"), [("PUT", "/api/profile/drafts/1"), ("POST", "/api/settings")])
async def test_a_streamed_body_over_the_limit_is_413_once_it_passes_it(method: str, path: str) -> None:
    chunks = chunked(2 * 2**20)
    inner, client = await request(method, path, chunks)
    assert_too_large(client)
    assert inner.calls == 0
    assert client.taken == MAX_BODY_BYTES // CHUNK + 1  # no message after the one that passed the limit


async def test_a_body_at_the_limit_reaches_the_app_whole() -> None:
    chunks = chunked(MAX_BODY_BYTES)
    for declared in (None, MAX_BODY_BYTES):
        inner, client = await request("POST", "/api/settings", chunks, declared=declared)
        assert (client.status, inner.calls, inner.body) == (204, 1, b"".join(chunks))
        assert [m["type"] for m in inner.messages] == ["http.request"] * len(chunks)


async def test_a_request_without_a_body_passes() -> None:
    inner, client = await request("GET", "/api/profile", [b""])
    assert (client.status, inner.calls, inner.body) == (204, 1, b"")


@pytest.mark.parametrize(("method", "path"), sorted(OWN_BODY_LIMITS))
async def test_the_cv_upload_and_the_paste_read_their_own_bodies(method: str, path: str) -> None:
    """The multipart upload streams up to 10 MiB under its own limit, the paste under its own 1 MiB cap
    (`profile.upload`): the edge reads nothing of them."""
    chunks = chunked(3 * 2**20)
    inner, client = await request(method, path, chunks, declared=3 * 2**20)
    assert (client.status, inner.calls, inner.body) == (204, 1, b"".join(chunks))
