"""The M4 routes in the OpenAPI document (spec §14.2; plan Global Constraints, API rules).

Every M4 route is listed in `M4_ROUTES` with the error statuses it documents; each route task adds its rows.
M4 adds no route that takes an `Idempotency-Key`, so there is no idempotency set here.
`test_the_m3_rows_of_the_document_are_unchanged` checks that M4's changes leave every M3 row as M3
documented it. `test_no_description_cites_an_internal_document` (Ruling T6-g, Task 10): the published
document is public text, so no `description` or `summary` in it names the codebase's module docstrings or the
plan's rulings, deviations, tasks, blocker ids or spec line numbers.
"""

import re
from collections.abc import Iterator
from typing import Any, Final

import pytest
from jobhunter.api.app import create_app

from tests.unit.test_openapi_m3 import M3_ROUTES

# The error statuses of a group or merge-family write (Task 8).
GROUP_WRITE: Final = frozenset({"401", "403", "404", "409", "413", "422", "429"})

M4_ROUTES: Final[dict[tuple[str, str], set[str] | frozenset[str]]] = {
    ("get", "/api/jobs"): {"400", "401", "422", "429"},
    ("get", "/api/jobs/facets"): {"401", "422", "429"},
    ("get", "/api/jobs/{job_id}"): {"401", "404", "422", "429"},
    ("get", "/api/jobs/{job_id}/history"): {"400", "401", "404", "422", "429"},
    ("put", "/api/jobs/{job_id}/actions"): {"401", "403", "404", "413", "422", "429"},
    ("delete", "/api/feedback/{feedback_id}"): {"401", "403", "404", "422", "429"},
    ("post", "/api/duplicate-groups/{group_id}/split"): GROUP_WRITE,
    ("post", "/api/duplicate-groups/{group_id}/score-individually"): GROUP_WRITE,
    ("post", "/api/duplicate-groups/{group_id}/confirm"): GROUP_WRITE - {"413"},  # no body
    ("post", "/api/jobs/{job_id}/unmerge"): GROUP_WRITE,
    ("post", "/api/identity-conflicts/{conflict_id}/resolve"): GROUP_WRITE,
    ("get", "/api/labelling/queue"): {"401", "422", "429"},
}
# What a published description must not cite (Ruling T6-g).
INTERNAL_CITATION: Final = re.compile(
    r"module docstring|\bRuling\b|\bDeviation \d|\bTask \d+\b|\bB\d{2}\b|\bL\d{3,4}\b"
)


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    return create_app().api.openapi()


def error_statuses(document: dict[str, Any], method: str, path: str) -> set[str]:
    return {status for status in document["paths"][path][method]["responses"] if int(status) >= 400}


@pytest.mark.parametrize(("route", "statuses"), M4_ROUTES.items(), ids=[" ".join(k) for k in M4_ROUTES])
def test_every_m4_route_is_in_the_document_with_its_error_statuses(
    document: dict[str, Any], route: tuple[str, str], statuses: set[str] | frozenset[str]
) -> None:
    method, path = route
    assert error_statuses(document, method, path) == statuses


def test_the_m3_rows_of_the_document_are_unchanged(document: dict[str, Any]) -> None:
    assert M3_ROUTES
    for (method, path), statuses in M3_ROUTES.items():
        assert method in document["paths"].get(path, {}), (method, path)
        assert error_statuses(document, method, path) == statuses, (method, path)


def texts(node: object, path: str = "") -> Iterator[tuple[str, str]]:
    """Every `description` and `summary` string in `node`, recursively, with its path."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}/{key}"
            if key in {"description", "summary"} and isinstance(value, str):
                yield here, value
            yield from texts(value, here)
    elif isinstance(node, list):
        for n, value in enumerate(node):
            yield from texts(value, f"{path}/{n}")


def test_no_description_cites_an_internal_document(document: dict[str, Any]) -> None:
    found = list(texts(document))
    assert len(found) > 100  # the walk reaches the operations' and the schemas' texts
    assert [(path, text) for path, text in found if INTERNAL_CITATION.search(text)] == []
