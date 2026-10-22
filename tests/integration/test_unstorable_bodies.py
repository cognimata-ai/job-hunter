"""The sweep of the M2 request bodies (final review FR-1 (c)): a string that holds a NUL character or a lone
UTF-16 surrogate, both of which a JSON body can carry as escapes (`"\\u0000"`, `"\\ud800"`), never reaches
JSONB, a hash or `encode()`. Each body refuses one with a 4xx before anything is written: in a string member
and in a member name, never a 500 and never an `api.unhandled_exception` alert.

- The settings dry-run answers its refusals with a 200 (they are its preview, plan Deviation 3); the save
  answers the same ones with a 422.
- The paste (`/cv-text`) drops a NUL by design (Task 11, `profile.text.drop_nul`: no stored text holds one)
  and refuses a lone surrogate (422 `upload_rejected`, `not_utf8`); `test_profile_upload_api.py` pins both.

Every value is invented; the bodies are sent as ASCII JSON, so each character travels as its escape.
"""

import json
from typing import Any, Final

import httpx
import pytest

from tests.support.fixtures import count_rows
from tests.support.profile import SYNTHETIC_PROFILE, idem
from tests.support.settings import SETTINGS_URL, fresh_key, settings_body

pytestmark = pytest.mark.integration

JSON: Final = {"content-type": "application/json"}
BAD: Final = {"nul": "\x00", "lone-surrogate": "\ud800"}
QUERIES_URL: Final = "/api/queries"


def profile_with(bad: str) -> dict[str, Any]:
    return {**SYNTHETIC_PROFILE, "summary": f"Backend{bad}Entwicklerin"}


async def send(client: httpx.AsyncClient, method: str, url: str, body: Any, **headers: str) -> httpx.Response:
    return await client.request(method, url, content=json.dumps(body), headers={**JSON, **headers})


async def live_query(client: httpx.AsyncClient) -> int:
    r = await send(client, "POST", QUERIES_URL, {"text": "Data Engineer", "lang": "en"})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def settings_payload(client: httpx.AsyncClient, bad: str) -> dict[str, Any]:
    payload: dict[str, Any] = (await settings_body(client))["payload"]
    payload["scope"]["industries_include"] = [f"Logistik{bad}"]
    return payload


async def cases(client: httpx.AsyncClient, bad: str) -> list[tuple[str, str, Any, dict[str, str]]]:
    """`(method, url, body, headers)` of each M2 body with `bad` in a string member."""
    query_id = await live_query(client)
    base = (await settings_body(client))["version"]["id"]
    payload = await settings_payload(client, bad)
    return [
        ("PUT", "/api/profile/drafts/1", {"draft": profile_with(bad)}, {}),
        (
            "PUT",
            "/api/profile/drafts/1/text",
            {"span_edits": [{"op": f"remove{bad}", "start": 0, "end": 1}]},
            {},
        ),
        ("POST", "/api/profile/drafts/1/retry", {"mode": f"llm{bad}"}, idem(fresh_key("retry"))),
        (
            "POST",
            "/api/profile/drafts/1/confirm",
            {"expected_rescore_ack": f"yes{bad}"},
            idem(fresh_key("c")),
        ),
        ("POST", QUERIES_URL, {"text": f"Data{bad}Engineer", "lang": "en"}, {}),
        ("POST", QUERIES_URL, {"text": "Data Engineer Kafka", "lang": f"en{bad}"}, {}),
        ("PATCH", f"{QUERIES_URL}/{query_id}", {"text": f"Data{bad}Engineer"}, {}),
        ("POST", "/api/watchlist/parse-url", {"url": f"https://boards.greenhouse.io/acme{bad}"}, {}),
        (
            "POST",
            "/api/watchlist",
            {"display_name": f"Ac{bad}me", "ats_type": "greenhouse", "board_slug": "acme"},
            {},
        ),
        (
            "POST",
            "/api/watchlist",
            {"display_name": "Acme", "ats_type": "greenhouse", "board_slug": f"a{bad}"},
            {},
        ),
        (
            "POST",
            SETTINGS_URL,
            {"payload": payload, "base_version_id": base, "acknowledged_warnings": []},
            idem(fresh_key()),
        ),
        (
            "POST",
            SETTINGS_URL,
            {"payload": payload, "base_version_id": base, "acknowledged_warnings": [f"code{bad}"]},
            idem(fresh_key()),
        ),
    ]


async def assert_nothing_unhandled(engine: Any) -> None:
    assert await count_rows(engine, "alerts", code="api.unhandled_exception") == 0


@pytest.mark.parametrize("bad", BAD.values(), ids=BAD.keys())
async def test_a_bad_string_member_is_a_422(engine, logged_in_client, bad: str) -> None:
    for method, url, body, headers in await cases(logged_in_client, bad):
        r = await send(logged_in_client, method, url, body, **headers)
        assert (r.status_code, r.json()["code"]) == (422, "validation_failed"), (method, url, r.text)
    await assert_nothing_unhandled(engine)
    assert await count_rows(engine, "settings_versions") == 1 and await count_rows(engine, "queries") == 1
    assert await count_rows(engine, "watchlist_companies") == 0


@pytest.mark.parametrize("bad", BAD.values(), ids=BAD.keys())
async def test_a_bad_member_name_is_a_422(engine, logged_in_client, bad: str) -> None:
    for method, url, body, headers in await cases(logged_in_client, ""):
        r = await send(logged_in_client, method, url, {**body, f"note{bad}": "x"}, **headers)
        assert (r.status_code, r.json()["code"]) == (422, "validation_failed"), (method, url, r.text)
    await assert_nothing_unhandled(engine)


@pytest.mark.parametrize("bad", BAD.values(), ids=BAD.keys())
async def test_the_settings_preview_shows_the_refusal(engine, logged_in_client, bad: str) -> None:
    payload = await settings_payload(logged_in_client, bad)
    r = await send(logged_in_client, "POST", "/api/settings/dry-run", {"payload": payload})
    assert r.status_code == 200, r.text
    assert [(e["field"], e["code"]) for e in r.json()["errors"]] == [
        ("payload.scope.industries_include[0]", "invalid")
    ]
    nested = {**payload, "scope": {**payload["scope"], f"note{bad}": "x"}}
    r = await send(logged_in_client, "POST", "/api/settings/dry-run", {"payload": nested})
    assert r.status_code == 200 and r.json()["errors"], r.text
    await assert_nothing_unhandled(engine)
