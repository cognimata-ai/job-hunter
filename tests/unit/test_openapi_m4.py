"""The M4 routes in the OpenAPI document (spec §14.2; plan Global Constraints, API rules).

Every M4 route is listed in `M4_ROUTES` with the error statuses it documents; each route task adds its rows.
M4 adds no route that takes an `Idempotency-Key`, so there is no idempotency set here.
`test_the_m3_rows_of_the_document_are_unchanged` checks that M4's changes leave every M3 row as M3
documented it.
"""

from typing import Any, Final

import pytest
from jobhunter.api.app import create_app

from tests.unit.test_openapi_m3 import M3_ROUTES

M4_ROUTES: Final[dict[tuple[str, str], set[str]]] = {
    ("get", "/api/jobs"): {"400", "401", "422", "429"},
    ("get", "/api/jobs/facets"): {"401", "422", "429"},
    ("get", "/api/jobs/{job_id}"): {"401", "404", "422", "429"},
    ("get", "/api/jobs/{job_id}/history"): {"400", "401", "404", "422", "429"},
}


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    return create_app().api.openapi()


def error_statuses(document: dict[str, Any], method: str, path: str) -> set[str]:
    return {status for status in document["paths"][path][method]["responses"] if int(status) >= 400}


@pytest.mark.parametrize(("route", "statuses"), M4_ROUTES.items(), ids=[" ".join(k) for k in M4_ROUTES])
def test_every_m4_route_is_in_the_document_with_its_error_statuses(
    document: dict[str, Any], route: tuple[str, str], statuses: set[str]
) -> None:
    method, path = route
    assert error_statuses(document, method, path) == statuses


def test_the_m3_rows_of_the_document_are_unchanged(document: dict[str, Any]) -> None:
    assert M3_ROUTES
    for (method, path), statuses in M3_ROUTES.items():
        assert method in document["paths"].get(path, {}), (method, path)
        assert error_statuses(document, method, path) == statuses, (method, path)
