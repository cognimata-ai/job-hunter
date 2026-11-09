"""The M5 routes in the OpenAPI document (spec §14.2; plan M5 Global Constraints, API rules).

Every M5 route is listed in `M5_ROUTES` with the error statuses it documents; each route task adds its rows.
`test_the_m4_rows_of_the_document_are_unchanged` checks that M5's changes leave every M4 row as M4
documented it. The published document is public text, so no `description` or `summary` in it cites the
codebase's module docstrings or the plans (the M4 file's check, `INTERNAL_CITATION`), the M5 routes'
included.
"""

from typing import Any, Final

import pytest
from jobhunter.api.app import create_app

from tests.unit.test_openapi_m4 import INTERNAL_CITATION, M4_ROUTES, error_statuses, texts

M5_ROUTES: Final[dict[tuple[str, str], set[str] | frozenset[str]]] = {
    ("post", "/api/alerts/{alert_id}/resolve"): {"401", "403", "404", "409", "422", "429"},
}


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    return create_app().api.openapi()


@pytest.mark.parametrize(("route", "statuses"), M5_ROUTES.items(), ids=[" ".join(k) for k in M5_ROUTES])
def test_every_m5_route_is_in_the_document_with_its_error_statuses(
    document: dict[str, Any], route: tuple[str, str], statuses: set[str] | frozenset[str]
) -> None:
    method, path = route
    assert error_statuses(document, method, path) == statuses


def test_the_m4_rows_of_the_document_are_unchanged(document: dict[str, Any]) -> None:
    assert M4_ROUTES
    for (method, path), statuses in M4_ROUTES.items():
        assert method in document["paths"].get(path, {}), (method, path)
        assert error_statuses(document, method, path) == statuses, (method, path)


def test_no_description_cites_an_internal_document(document: dict[str, Any]) -> None:
    found = list(texts(document))
    # the walk reaches every M5 route's operation
    for method, path in M5_ROUTES:
        prefix = f"/paths/{path}/{method}/"
        assert any(where.startswith(prefix) for where, _ in found), (method, path)
    assert [(where, text) for where, text in found if INTERNAL_CITATION.search(text)] == []


def test_an_alert_names_its_delivery_state(document: dict[str, Any]) -> None:
    schema = document["components"]["schemas"]["AlertOut"]
    assert {"last_notified_at", "notify_failures", "manual"} <= set(schema["required"])
    assert schema["properties"]["notify_failures"]["type"] == "integer"
    assert schema["properties"]["manual"]["type"] == "boolean"
    resolve = document["paths"]["/api/alerts/{alert_id}/resolve"]["post"]
    assert resolve["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/AlertOut"
    }
