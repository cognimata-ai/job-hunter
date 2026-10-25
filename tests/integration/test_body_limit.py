"""The edge's 1 MiB body ceiling on the real routes (final review FR-4): a 2 MiB JSON body on `PUT
/api/profile/drafts/{id}` and on `POST /api/settings`, declared or streamed without a `Content-Length`, is a
413 `upload_rejected` before the handler runs (nothing is stored, no key is recorded) and raises no alert.
The CV upload keeps its own 10 MiB limit, and the paste its own 1 MiB cap with its own field
(`test_profile_upload_api.py`).

Every value is invented.
"""

import json
from collections.abc import AsyncIterator
from typing import Any, Final

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.fixtures import count_rows
from tests.support.profile import draft_row, draft_row_api, idem
from tests.support.settings import SETTINGS_URL, fresh_key, settings_body

pytestmark = pytest.mark.integration

JSON: Final = {"content-type": "application/json"}
TWO_MIB: Final = 2 * 2**20


def streamed(body: bytes) -> AsyncIterator[bytes]:
    async def chunks() -> AsyncIterator[bytes]:
        for i in range(0, len(body), 65536):
            yield body[i : i + 65536]

    return chunks()


def assert_too_large(r: httpx.Response) -> None:
    assert (r.status_code, r.json()["code"]) == (413, "upload_rejected"), r.text
    assert [(e["field"], e["code"]) for e in r.json()["errors"]] == [("body", "too_large")]


async def assert_no_alert(engine: AsyncEngine) -> None:
    assert await count_rows(engine, "alerts", code="api.unhandled_exception") == 0


async def test_a_2_mib_draft_is_413_before_the_handler_runs(
    engine: AsyncEngine, logged_in_client: httpx.AsyncClient, ready_draft: int
) -> None:
    draft: dict[str, Any] = (await draft_row_api(logged_in_client, ready_draft))["draft"]
    draft["summary"] = "Backend-Entwicklerin " * (TWO_MIB // 20)  # within no limit of the model itself
    body = json.dumps({"draft": draft}).encode()
    assert len(body) > TWO_MIB
    before = (await draft_row(engine, ready_draft)).draft_json
    url = f"/api/profile/drafts/{ready_draft}"
    declared = await logged_in_client.put(url, content=body, headers=JSON)
    chunked = await logged_in_client.put(url, content=streamed(body), headers=JSON)
    assert "content-length" not in chunked.request.headers
    for r in (declared, chunked):
        assert_too_large(r)
    assert (await draft_row(engine, ready_draft)).draft_json == before  # the handler never ran
    await assert_no_alert(engine)


async def test_a_2_mib_settings_save_is_413_before_the_handler_runs(
    engine: AsyncEngine, logged_in_client: httpx.AsyncClient
) -> None:
    current = await settings_body(logged_in_client)
    payload = current["payload"]
    payload["scope"]["excluded_keywords"] = ["Praktikum"] * (TWO_MIB // 12)
    body = json.dumps(
        {"payload": payload, "base_version_id": current["version"]["id"], "acknowledged_warnings": []}
    ).encode()
    assert len(body) > TWO_MIB
    declared = await logged_in_client.post(SETTINGS_URL, content=body, headers={**JSON, **idem(fresh_key())})
    chunked = await logged_in_client.post(
        SETTINGS_URL, content=streamed(body), headers={**JSON, **idem(fresh_key())}
    )
    for r in (declared, chunked):
        assert_too_large(r)
    assert await count_rows(engine, "api_idempotency") == 0  # the handler never ran
    assert await count_rows(engine, "settings_versions") == 1
    await assert_no_alert(engine)


async def test_the_cv_upload_keeps_its_own_limit(
    engine: AsyncEngine, logged_in_client: httpx.AsyncClient
) -> None:
    """A 3 MiB file is within the upload's 10 MiB: the edge's ceiling does not apply to it."""
    pdf = b"%PDF-1.4\n" + b"0" * (3 * 2**20)
    r = await logged_in_client.post("/api/profile/cv", files={"file": ("Lebenslauf.pdf", pdf)})
    assert r.status_code == 202, r.text
    await assert_no_alert(engine)
