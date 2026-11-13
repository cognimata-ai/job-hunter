"""The M5 routes in the OpenAPI document (spec §14.2; plan M5 Global Constraints, API rules).

Every M5 route is listed in `M5_ROUTES` with the error statuses it documents; each route task adds its rows.
`test_the_m4_rows_of_the_document_are_unchanged` checks that M5's changes leave every M4 row as M4
documented it, but for the one change Task 18 makes (Ruling T18-1 (b)): every write route the edge stops
under delete-everything's `global` fence documents that 409, `delete_everything_in_progress`
(`test_openapi_m3.documented_since_m5`). The three keyed privacy requests take an `Idempotency-Key` (§14.1),
no other M5 route does. The published document is public text, so no `description` or `summary` in it cites
the codebase's module docstrings or the plans (the M4 file's check, `INTERNAL_CITATION`), the M5 routes'
included.
"""

from typing import Any, Final

import pytest
from jobhunter.api.app import create_app
from jobhunter.api.openapi import FENCE_RESPONSES

from tests.unit.test_openapi_m3 import WRITE_METHODS, documented_since_m5, fenced_write
from tests.unit.test_openapi_m4 import INTERNAL_CITATION, M4_ROUTES, error_statuses, texts

# The error statuses of a keyed privacy request (Task 18).
KEYED_PRIVACY: Final = frozenset({"400", "401", "403", "409", "413", "422", "429"})

M5_ROUTES: Final[dict[tuple[str, str], set[str] | frozenset[str]]] = {
    ("post", "/api/alerts/{alert_id}/resolve"): {"401", "403", "404", "409", "422", "429"},
    ("post", "/api/privacy/export"): KEYED_PRIVACY,
    ("get", "/api/privacy/exports/{export_id}"): {"401", "404", "409", "410", "422", "429"},
    ("post", "/api/privacy/delete-everything"): KEYED_PRIVACY,
    ("post", "/api/privacy/purge-source"): KEYED_PRIVACY,
    ("get", "/api/privacy/manifests"): {"400", "401", "422", "429"},
    ("get", "/api/privacy/manifests/{manifest_id}"): {"401", "404", "422", "429"},
    # M5 follow-ups Task F1: the statement's operator values, no parameter.
    ("get", "/api/privacy/operator"): {"401", "429"},
    ("post", "/api/system/purges/{operation_id}/retry"): {"401", "403", "404", "409", "422", "429"},
    ("delete", "/api/golden-cases/{case_id}"): {"401", "403", "404", "409", "422", "429"},
}
KEYED: Final = frozenset(
    {
        ("post", "/api/privacy/export"),
        ("post", "/api/privacy/delete-everything"),
        ("post", "/api/privacy/purge-source"),
    }
)
# The route-level purge refusals Task 8 left undocumented (Ruling T8-3).
ROUTE_PURGE_409: Final = frozenset(
    {("put", "/api/jobs/{job_id}/actions"), ("patch", "/api/golden-cases/{case_id}")}
)


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
        assert error_statuses(document, method, path) == documented_since_m5(method, path, statuses), (
            method,
            path,
        )


def test_every_write_the_edge_fences_documents_its_409(document: dict[str, Any]) -> None:
    """Ruling T18-1 (b): each write route the edge stops under delete-everything's fence has a 409 whose
    description holds the fence's; a write the edge lets through (the login, the logout, the purge retry and
    the privacy routes) has none of it."""
    fence = FENCE_RESPONSES[409]["description"]
    writes = [
        (method, path, operation)
        for path, item in document["paths"].items()
        for method, operation in item.items()
        if method in WRITE_METHODS and path.startswith("/api/")
    ]
    fenced = [(method, path) for method, path, _ in writes if fenced_write(method, path)]
    assert len(fenced) >= 30 and len(fenced) < len(writes)
    for method, path, operation in writes:
        description = operation["responses"].get("409", {}).get("description", "")
        assert (fence in description) == fenced_write(method, path), (method, path, description)


def test_the_route_level_purge_refusals_are_documented(document: dict[str, Any]) -> None:
    for method, path in sorted(ROUTE_PURGE_409):
        description = document["paths"][path][method]["responses"]["409"]["description"]
        assert "`purge_in_progress`" in description, (method, path)


@pytest.mark.parametrize(("method", "path"), sorted(M5_ROUTES))
def test_exactly_the_keyed_privacy_requests_take_an_idempotency_key(
    document: dict[str, Any], method: str, path: str
) -> None:
    parameters = document["paths"][path][method].get("parameters", [])
    keyed = {"name": "Idempotency-Key", "in": "header"} in [
        {"name": p["name"], "in": p["in"]} for p in parameters
    ]
    assert keyed == ((method, path) in KEYED)


def test_the_privacy_documents_name_their_fields(document: dict[str, Any]) -> None:
    schemas = document["components"]["schemas"]
    assert set(schemas["ExportOut"]["required"]) == {"id", "status", "created_at", "expires_at", "size_bytes"}
    assert set(schemas["ManifestOut"]["required"]) == {
        "id",
        "kind",
        "status",
        "created_at",
        "completed_at",
        "shipped_at",
        "deleted_golden_case_ids",
    }
    assert set(schemas["DeleteEverythingIn"]["properties"]) == {"confirm", "password", "totp"}
    assert set(schemas["PurgeSourceIn"]["properties"]) == {"source", "confirm", "password", "totp"}
    assert schemas["PurgeSourceIn"]["properties"]["source"]["const"] == "adzuna"
    item = schemas["FailedPurgeOut"]
    assert set(item["required"]) == {
        "manifest_id",
        "kind",
        "created_at",
        "operation_id",
        "last_error_code",
        "attempts",
    }
    assert schemas["AttentionOut"]["properties"]["failed_purges"] == {
        "$ref": "#/components/schemas/FailedPurgesSectionOut"
    }
    # F1: the five operator values, each a string or, outside prod without a valid operator file, null.
    operator = schemas["OperatorOut"]
    assert set(operator["required"]) == {"operator", "contact", "controller", "vps_provider", "s3_provider"}
    assert set(operator["properties"]) == set(operator["required"])
    for name, field in operator["properties"].items():
        assert field["anyOf"] == [{"type": "string"}, {"type": "null"}], name
    read = document["paths"]["/api/privacy/operator"]["get"]["responses"]["200"]["content"]
    assert read["application/json"]["schema"] == {"$ref": "#/components/schemas/OperatorOut"}
    download = document["paths"]["/api/privacy/exports/{export_id}"]["get"]["responses"]["200"]["content"]
    assert set(download) == {"application/json", "application/zip"}
    deleted = document["paths"]["/api/golden-cases/{case_id}"]["delete"]["responses"]
    assert "202" in deleted and "204" not in deleted


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


def test_the_system_health_names_the_published_tracing_state(document: dict[str, Any]) -> None:
    """M5 (Q17): `langsmith` is required and nullable, `null` until a worker published its tracing state."""
    schemas = document["components"]["schemas"]
    health = schemas["SystemHealthOut"]
    assert "langsmith" in health["required"]
    assert health["properties"]["langsmith"]["anyOf"] == [
        {"$ref": "#/components/schemas/TracingStateOut"},
        {"type": "null"},
    ]
    state = schemas["TracingStateOut"]
    assert set(state["required"]) == {"tracing", "region"}
    assert state["properties"]["tracing"]["type"] == "boolean"
    assert state["properties"]["region"]["anyOf"] == [{"const": "eu", "type": "string"}, {"type": "null"}]


def test_the_system_health_names_the_backups(document: dict[str, Any]) -> None:
    """M5 (§17.5): `backups` is required, with its seven fields, the latest set and restore test nullable."""
    schemas = document["components"]["schemas"]
    health = schemas["SystemHealthOut"]
    assert "backups" in health["required"]
    assert health["properties"]["backups"] == {"$ref": "#/components/schemas/BackupStateOut"}
    backups = schemas["BackupStateOut"]
    assert set(backups["required"]) == {
        "latest_set",
        "backup_age_state",
        "latest_shipment",
        "unshipped_manifests",
        "oldest_unshipped_completed_at",
        "latest_restore_test",
        "restore_test_age_state",
    }
    run = {"$ref": "#/components/schemas/BackupRunOut"}
    assert backups["properties"]["latest_set"]["anyOf"] == [run, {"type": "null"}]
    assert backups["properties"]["latest_restore_test"]["anyOf"] == [run, {"type": "null"}]
    assert backups["properties"]["backup_age_state"]["enum"] == ["ok", "warn", "critical", "grace"]
    assert backups["properties"]["restore_test_age_state"]["enum"] == ["ok", "warn", "none"]
