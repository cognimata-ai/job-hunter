"""The OpenAPI document describes the api's error contract (spec §14.4, §14.5, ruling T19-a)."""

import json
from typing import Any, Final, get_args

import pytest
from jobhunter.api import errors, openapi
from jobhunter.api.app import create_app
from jobhunter.api.edge import LOGIN_PATH, STATE_CHANGING_METHODS

PROBLEM_REF: Final = "#/components/schemas/Problem"
HTTP_METHODS: Final = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
# The one error response that is not a problem document: the readiness report (§14.1, §17.5).
READINESS_503: Final = ("get", "/readyz", "503")


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    return create_app().api.openapi()


def operations(document: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (method, path): operation
        for path, item in document["paths"].items()
        for method, operation in item.items()
        if method in HTTP_METHODS
    }


def error_responses(document: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
    return {
        (method, path, status): response
        for (method, path), operation in operations(document).items()
        for status, response in operation["responses"].items()
        if int(status) >= 400
    }


def refs(node: object) -> set[str]:
    if isinstance(node, dict):
        found = {node["$ref"]} if isinstance(node.get("$ref"), str) else set()
        return found.union(*(refs(value) for value in node.values()))
    if isinstance(node, list):
        return set().union(*(refs(value) for value in node))
    return set()


def test_fastapi_default_validation_schemas_are_gone(document) -> None:
    assert not {"HTTPValidationError", "ValidationError"} & set(document["components"]["schemas"])
    assert "ValidationError" not in json.dumps(document)


def test_every_ref_resolves(document) -> None:
    prefix = "#/components/schemas/"
    assert {r for r in refs(document) if not r.startswith(prefix)} == set()
    assert {r.removeprefix(prefix) for r in refs(document)} <= set(document["components"]["schemas"])


def test_problem_and_field_error_are_components(document) -> None:
    schemas = document["components"]["schemas"]
    problem = schemas["Problem"]
    assert problem["required"] == ["type", "title", "status", "code", "instance", "request_id"]
    assert problem["properties"]["code"]["enum"] == list(get_args(errors.ProblemCode))
    assert problem["properties"]["errors"]["anyOf"][0]["items"] == {"$ref": "#/components/schemas/FieldError"}
    assert schemas["FieldError"]["required"] == ["field", "code", "message"]


def test_the_problem_schema_describes_what_problem_body_renders() -> None:
    problem = errors.Problem(
        422,
        "validation_failed",
        "Request validation failed",
        detail="d",
        errors=[errors.FieldError(field="a.b", code="missing", message="Field required")],
        extra={"retry_after_s": 3},
    )
    body = errors.problem_body(problem, instance="/api/x", request_id="r-1")
    rendered = openapi.Problem.model_validate(body).model_dump(exclude_none=True)
    assert rendered == body


def test_every_error_response_is_a_problem_but_the_readiness_report(document) -> None:
    problems = {key: response for key, response in error_responses(document).items() if key != READINESS_503}
    assert problems
    for key, response in problems.items():
        assert response["content"] == {"application/problem+json": {"schema": {"$ref": PROBLEM_REF}}}, key
        assert response["description"], key


def test_the_readiness_503_is_the_report_not_a_problem(document) -> None:
    response = error_responses(document)[READINESS_503]
    assert response["content"] == {
        "application/json": {"schema": {"$ref": "#/components/schemas/NotReadyOut"}}
    }


@pytest.mark.parametrize(
    ("method", "path", "statuses"),
    [
        ("post", "/api/auth/login", {"401", "403", "422", "429"}),
        ("post", "/api/auth/logout", {"401", "403", "429"}),
        ("get", "/api/auth/session", {"401", "429"}),
        ("post", "/api/runs", {"400", "401", "403", "409", "422", "429"}),
        ("get", "/api/system/health", {"401", "429"}),
        ("post", "/api/profile/cv", {"401", "403", "409", "413", "422", "429"}),
        ("post", "/api/profile/cv-text", {"401", "403", "409", "413", "422", "429"}),
        ("get", "/readyz", {"503"}),
        ("get", "/healthz", set()),
    ],
)
def test_routes_document_their_error_statuses(document, method, path, statuses) -> None:
    responses = operations(document)[(method, path)]["responses"]
    assert {status for status in responses if int(status) >= 400} == statuses


def test_edge_and_session_errors_are_documented_on_every_api_route(document) -> None:
    """403 on what the edge's CSRF check covers; 401 and 429 on what `require_session` guards."""
    api_operations = {key: op for key, op in operations(document).items() if key[1].startswith("/api/")}
    assert api_operations
    for (method, path), operation in api_operations.items():
        statuses = set(operation["responses"])
        assert ("403" in statuses) == (method.upper() in STATE_CHANGING_METHODS), (method, path)
        if path != LOGIN_PATH:
            assert {"401", "429"} <= statuses, (method, path)


def test_only_the_403_of_routes_that_need_x_jh_request_names_it(document) -> None:
    """The login is checked for `Origin` only (§16.5, `edge.LOGIN_PATH`): its 403 does not name the header."""
    forbidden = {key[:2]: response for key, response in error_responses(document).items() if key[2] == "403"}
    assert ("post", LOGIN_PATH) in forbidden and len(forbidden) > 1
    for (method, path), response in forbidden.items():
        assert ("X-JH-Request" in response["description"]) == (path != LOGIN_PATH), (method, path)
        assert "Origin" in response["description"], (method, path)
