"""The api's OpenAPI document describes its error contract (spec §14.4, §14.5, ruling T19-a).

Every error response is an RFC 9457 `application/problem+json` document (`errors.problem_body`), so the
generated document says so:

- `Problem` and `FieldError` below are component schemas. They are the shape `problem_body` renders (a unit
  test validates the one against the other), not the exception and dataclass of `errors` with the same names;
- `JobHunterAPI.openapi` post-processes FastAPI's document (`with_problem_contract`): the 4xx/5xx responses
  that a route declares with a description only, and FastAPI's own 422 (`HTTPValidationError`, whose shape is
  not ours), become `application/problem+json` responses that reference `Problem`; `HTTPValidationError` and
  `ValidationError` are removed. A response with a content of its own is kept: the readiness report of
  `/readyz` 503 (`{failing: [...]}`, §14.1) is not a problem document.

What a route does not raise itself but its guards do is declared by the route's `responses=`:
`SESSION_RESPONSES` for what `deps.require_session` raises, `CSRF_RESPONSES` for the edge's CSRF check on a
state-changing method (`edge.csrf_failure`), `LOGIN_CSRF_RESPONSES` for the login's, which checks `Origin`
only, and `BODY_LIMIT_RESPONSES` for the edge's body ceiling on a route with a request body
(`edge.MAX_BODY_BYTES`; the CV upload and the paste document their own 413). Unit tests check that every
`/api` route documents them.
"""

from typing import Any, Final

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict
from pydantic.json_schema import models_json_schema

from jobhunter.api.errors import PROBLEM_MEDIA_TYPE, ProblemCode

COMPONENTS_PREFIX: Final = "#/components/schemas/"
PROBLEM_REF: Final = COMPONENTS_PREFIX + "Problem"
_VALIDATION_ERROR_SCHEMAS: Final = ("HTTPValidationError", "ValidationError")
_VALIDATION_FAILED_DESCRIPTION: Final = "Request validation failed (`validation_failed`)"

# `responses=` of the routes: descriptions only; `with_problem_contract` gives them the problem content.
SESSION_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    401: {"description": "No valid session (`unauthenticated`)"},
    429: {"description": "Too many requests in this session (`throttled`)"},
}
CSRF_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    403: {"description": "Origin or `X-JH-Request` check failed (`csrf_failed`)"},
}
# The edge's body ceiling (`edge.MAX_BODY_BYTES`, final review FR-4) on a route with a request body.
BODY_LIMIT_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    413: {"description": "The request body is larger than 1 MiB (`upload_rejected`, `too_large`)"},
}
# The login has no session yet and is checked for `Origin` only (§16.5, `edge.LOGIN_PATH`).
LOGIN_CSRF_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    403: {"description": "Origin check failed (`csrf_failed`)"},
}


class FieldError(BaseModel):
    """One invalid field of a `validation_failed` problem; `field` is the pydantic `loc` joined with ".".

    `message` can echo the sender's own input (ruling T14-h).
    """

    field: str
    code: str
    message: str


class Problem(BaseModel):
    """The `application/problem+json` document of every error response (RFC 9457, §14.4).

    `type` is the problem base URI plus `code`; `instance` is the request path; `request_id` is the
    `X-Request-Id` of the response. `errors` accompanies a 422; a route adds members of its own (the login's
    429 has `retry_after_s`), which is why undeclared members are allowed.
    """

    model_config = ConfigDict(extra="allow")

    type: str
    title: str
    status: int
    code: ProblemCode
    detail: str | None = None
    instance: str
    request_id: str
    errors: list[FieldError] | None = None


def _problem_content() -> dict[str, Any]:
    return {PROBLEM_MEDIA_TYPE: {"schema": {"$ref": PROBLEM_REF}}}


def _problem_schemas() -> dict[str, Any]:
    _, definitions = models_json_schema(
        [(Problem, "serialization"), (FieldError, "serialization")],
        ref_template=COMPONENTS_PREFIX + "{model}",
    )
    schemas: dict[str, Any] = definitions["$defs"]
    return schemas


def _is_fastapi_validation_response(response: dict[str, Any]) -> bool:
    schema = response.get("content", {}).get("application/json", {}).get("schema", {})
    return bool(schema.get("$ref") == COMPONENTS_PREFIX + "HTTPValidationError")


def with_problem_contract(document: dict[str, Any]) -> dict[str, Any]:
    """`document` with the problem contract applied, in place; applying it again changes nothing."""
    schemas: dict[str, Any] = document.setdefault("components", {}).setdefault("schemas", {})
    for name in _VALIDATION_ERROR_SCHEMAS:
        schemas.pop(name, None)
    schemas.update(_problem_schemas())
    for item in document.get("paths", {}).values():
        for operation in item.values():
            if not isinstance(operation, dict):  # a path item's `parameters`
                continue
            for status, response in operation.get("responses", {}).items():
                if not status.isdigit() or int(status) < 400:
                    continue
                if _is_fastapi_validation_response(response):
                    response["description"] = _VALIDATION_FAILED_DESCRIPTION
                    response["content"] = _problem_content()
                elif "content" not in response:
                    response["content"] = _problem_content()
    return document


class JobHunterAPI(FastAPI):
    """The FastAPI app whose OpenAPI document carries the problem contract."""

    def openapi(self) -> dict[str, Any]:
        # FastAPI caches the document and regenerates it when the routes changed; the contract is applied to
        # whichever it returns, which is cheap and idempotent.
        return with_problem_contract(super().openapi())
