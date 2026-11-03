"""`GET /api/openapi.json` (spec §14.2 L5054, §14.5; plan M4 Task 10, Deviation 37): the application's OpenAPI
document, served to a session only and never listed in itself, so the committed export stays the document.
FastAPI's own documentation routes stay off.
"""

import httpx
import pytest
from jobhunter.api.app import create_app

pytestmark = pytest.mark.integration

URL = "/api/openapi.json"


async def test_the_document_is_served_to_a_session_only(
    api_client: httpx.AsyncClient, logged_in_client: httpx.AsyncClient
) -> None:
    r = await api_client.get(URL)
    assert (r.status_code, r.json()["code"]) == (401, "unauthenticated")
    r = await logged_in_client.get(URL)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/json"
    document = r.json()
    assert document["paths"] == create_app().api.openapi()["paths"]
    assert URL not in document["paths"]
    for default in ("/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"):  # FastAPI's, disabled
        assert (await logged_in_client.get(default)).status_code == 404, default
