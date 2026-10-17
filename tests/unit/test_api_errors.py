import httpx
import pytest
from fastapi import APIRouter, HTTPException
from jobhunter.api.app import create_app
from jobhunter.api.errors import code_for_http_status


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "validation_failed"),
        (401, "unauthenticated"),
        (403, "validation_failed"),
        (404, "not_found"),
        (405, "not_found"),
        (413, "upload_rejected"),
        (415, "validation_failed"),
        (422, "validation_failed"),
        (500, "internal"),
        (503, "internal"),
    ],
)
def test_library_http_statuses_map_to_problem_codes(status, code) -> None:
    assert code_for_http_status(status) == code


@pytest.mark.parametrize(
    ("status", "code"), [(400, "validation_failed"), (401, "unauthenticated"), (413, "upload_rejected")]
)
async def test_a_library_http_exception_becomes_its_problem(status, code) -> None:
    router = APIRouter()

    @router.get("/api/_raise")
    async def raise_it() -> None:
        raise HTTPException(status_code=status, detail="library text", headers={"X-Probe": "kept"})

    app = create_app(extra_routers=[router])  # no state: these responses need neither config nor database
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as c:
        r = await c.get("/api/_raise")
    assert r.status_code == status and r.headers["content-type"] == "application/problem+json"
    body = r.json()
    assert body["code"] == code and body["type"] == f"https://jobhunter.local/problems/{code}"
    assert body["status"] == status and body["request_id"] == r.headers["x-request-id"]
    assert "library text" not in r.text and r.headers["x-probe"] == "kept"
