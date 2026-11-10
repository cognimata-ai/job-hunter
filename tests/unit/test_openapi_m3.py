"""The M3 routes in the OpenAPI document (spec §14.2, §14.5; plan Task 21, B R18).

Every route of Task 21 is in the generated document with the error statuses it documents; the routes §14.1
L4963 lists take an `Idempotency-Key` header and document its 400; the spend document's `bulk_operation` is
the `BulkOperationOut` object; and the committed `frontend/src/api/schema.d.ts` has exactly the document's
paths (`scripts/check-openapi-drift.sh` compares the whole file; this keeps the paths honest without npm).
"""

import re
from pathlib import Path
from typing import Any, Final

import pytest
from jobhunter.api.app import create_app

SCHEMA_TS: Final = Path(__file__).resolve().parents[2] / "frontend" / "src" / "api" / "schema.d.ts"
KEYED: Final = {"400", "401", "403", "404", "409", "422", "429"}
M3_ROUTES: Final[dict[tuple[str, str], set[str]]] = {
    ("get", "/api/settings/rubric-versions"): {"401", "429"},
    ("get", "/api/settings/prompt-schema-versions"): {"401", "429"},
    ("post", "/api/settings/rubric-versions/{version_id}/activate"): KEYED | {"413"},
    ("post", "/api/settings/prompt-schema-versions/{version_id}/activate"): KEYED | {"413"},
    ("post", "/api/jobs/{job_id}/score-now"): KEYED | {"413"},
    ("post", "/api/targets/{target_id}/retry"): KEYED,
    ("post", "/api/batches/{submission_id}/release"): KEYED | {"413"},
    ("post", "/api/system/score-cycle"): {"400", "401", "403", "409", "422", "429"},
    ("post", "/api/jobs/{job_id}/feedback"): {"401", "403", "404", "409", "413", "422", "429"},
    ("get", "/api/golden-cases"): {"400", "401", "422", "429"},
    ("patch", "/api/golden-cases/{case_id}"): {"401", "403", "404", "409", "413", "422", "429"},
    ("delete", "/api/golden-cases/{case_id}"): {"401", "403", "404", "409", "422", "429"},
    ("post", "/api/alerts/{alert_id}/ack"): {"401", "403", "404", "422", "429"},
    ("get", "/api/system/spend"): {"401", "429"},
}
IDEMPOTENT: Final = {
    ("post", "/api/settings/rubric-versions/{version_id}/activate"),
    ("post", "/api/settings/prompt-schema-versions/{version_id}/activate"),
    ("post", "/api/jobs/{job_id}/score-now"),
    ("post", "/api/targets/{target_id}/retry"),
    ("post", "/api/batches/{submission_id}/release"),
    ("post", "/api/system/score-cycle"),
}


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    return create_app().api.openapi()


@pytest.mark.parametrize(("route", "statuses"), M3_ROUTES.items(), ids=[" ".join(k) for k in M3_ROUTES])
def test_every_m3_route_is_in_the_document_with_its_error_statuses(
    document: dict[str, Any], route: tuple[str, str], statuses: set[str]
) -> None:
    method, path = route
    operation = document["paths"][path][method]
    assert {status for status in operation["responses"] if int(status) >= 400} == statuses


@pytest.mark.parametrize(("method", "path"), sorted(IDEMPOTENT))
def test_the_routes_that_start_paid_work_or_activate_a_version_take_an_idempotency_key(
    document: dict[str, Any], method: str, path: str
) -> None:
    parameters = document["paths"][path][method].get("parameters", [])
    assert {"name": "Idempotency-Key", "in": "header"} in [
        {"name": p["name"], "in": p["in"]} for p in parameters
    ]


def test_the_spend_document_names_its_m3_fields(document: dict[str, Any]) -> None:
    schemas = document["components"]["schemas"]
    spend = schemas["SpendOut"]["properties"]
    assert {"c_hat_source", "pause_reason", "bulk_operation", "eval_by_model_role"} <= set(spend)
    assert {"$ref": "#/components/schemas/BulkOperationOut"} in spend["bulk_operation"]["anyOf"]
    assert set(schemas["BulkOperationOut"]["required"]) == {
        "id",
        "state",
        "budget_usd",
        "spent_usd",
        "followup_held_usd",
        "llm_tuple_id",
    }


def test_the_committed_frontend_types_have_the_document_s_paths(document: dict[str, Any]) -> None:
    typed = set(re.findall(r'^    "(/[^"]*)": \{$', SCHEMA_TS.read_text(encoding="utf-8"), flags=re.M))
    assert typed == set(document["paths"])
