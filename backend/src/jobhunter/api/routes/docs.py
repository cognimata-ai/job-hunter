"""`GET /api/openapi.json` (spec §14.2 L5054, §14.5; plan M4 Task 10, Deviation 37): the api's OpenAPI 3.1
document, for a logged-in session.

It answers `request.app.openapi()`, the document the export writes (`api.openapi_export`), the problem
contract applied (`api.openapi.JobHunterAPI`). The route is `include_in_schema=False`, so the document never
lists it and the committed `openapi.json` and `schema.d.ts` stay the export's. FastAPI's own `/openapi.json`,
`/docs` and `/redoc` stay off (`create_app`): no route serves the document without a session.
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from starlette.responses import JSONResponse

from jobhunter.api.deps import require_session
from jobhunter.api.openapi import SESSION_RESPONSES

router = APIRouter(dependencies=[Depends(require_session)], responses=SESSION_RESPONSES)


@router.get("/api/openapi.json", include_in_schema=False)
async def get_openapi_document(request: Request) -> JSONResponse:
    """The api's OpenAPI document."""
    document: dict[str, Any] = request.app.openapi()
    return JSONResponse(document)
