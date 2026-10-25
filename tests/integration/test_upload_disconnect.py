"""A client that disconnects in the middle of its body on `/api/profile/cv` and `/api/profile/cv-text` (final
review FR-5): the readers turn Starlette's `ClientDisconnect` into a 400 `validation_failed`, logged
`client_disconnected` at info, and no `api.unhandled_exception` alert is persisted. The response is moot.

The whole api is driven with raw ASGI messages (an HTTP client cannot vanish mid-body), on a logged-in
session. Every datum is invented.
"""

import json
from collections.abc import Callable
from typing import Any, Final

import httpx
import pytest
from jobhunter.auth import sessions
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import Message

from tests.support.fixtures import count_rows

pytestmark = pytest.mark.integration

BOUNDARY: Final = "jh-disconnect-boundary"
FORM_START: Final = (
    f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="Lebenslauf.pdf"\r\n'
    "Content-Type: application/pdf\r\n\r\n%PDF-1.4\n"
).encode() + b"0" * 4096
CASES: Final = {
    "/api/profile/cv": (f"multipart/form-data; boundary={BOUNDARY}", FORM_START),
    "/api/profile/cv-text": ("application/json", b'{"text": "' + b"Erika Musterfrau " * 64),
}


async def post_and_vanish(app: Any, client: httpx.AsyncClient, path: str) -> list[Message]:
    """One POST whose client sends the first part of its body, then disconnects; what the app sent."""
    content_type, first = CASES[path]
    token = client.cookies.get(sessions.SESSION_COOKIE)
    assert token is not None
    headers = [
        (b"content-type", content_type.encode()),
        (b"origin", b"https://testserver"),
        (b"x-jh-request", b"1"),
        (b"cookie", f"{sessions.SESSION_COOKIE}={token}".encode()),
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("198.51.100.20", 50000),
        "server": ("testserver", 443),
    }
    pending = [{"type": "http.request", "body": first, "more_body": True}]
    sent: list[Message] = []

    async def receive() -> Message:
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


@pytest.mark.parametrize("path", sorted(CASES))
async def test_a_disconnect_mid_body_is_a_400_without_an_alert(
    engine: AsyncEngine,
    api_app: Any,
    logged_in_client: httpx.AsyncClient,
    log_events: Callable[[], list[dict[str, Any]]],
    path: str,
) -> None:
    sent = await post_and_vanish(api_app, logged_in_client, path)
    assert sent[0]["status"] == 400
    problem = json.loads(b"".join(m.get("body", b"") for m in sent[1:]))
    assert problem["code"] == "validation_failed"
    assert await count_rows(engine, "alerts", code="api.unhandled_exception") == 0
    events = [e for e in log_events() if e["event"] in ("client_disconnected", "unhandled_exception")]
    assert [(e["event"], e["level"]) for e in events] == [("client_disconnected", "info")]
    assert await count_rows(engine, "cv_files") == 0
