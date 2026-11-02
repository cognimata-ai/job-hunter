"""The settings read routes (spec §13.4, §14.2, §14.3; plan Task 17): `GET /api/settings`, the version list
with its signed keyset cursor, and a version with its diff against the previous one."""

import base64
import copy
import json
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.api.pagination import SETTINGS_VERSIONS_SORT, encode_list_cursor, filters_hash
from jobhunter.crypto import hkdf_sha256
from jobhunter.db.seed import load_settings_v1

from tests.support.settings import active_row, berlin, save, save_scope, settings_body

pytestmark = pytest.mark.integration

VERSIONS_URL = "/api/settings/versions"


async def test_get_settings_shows_the_active_version(logged_in_client, engine) -> None:
    body = await settings_body(logged_in_client)
    assert set(body) == {"version", "payload", "pending_scope"}
    assert set(body["version"]) == {"id", "version_no", "created_at", "effect_classes"}
    assert body["version"]["id"] == (await active_row(engine)).settings_version_id
    assert (body["version"]["version_no"], body["version"]["effect_classes"]) == (1, ["none"])
    assert body["version"]["created_at"].endswith("Z")
    assert body["payload"] == load_settings_v1() and body["pending_scope"] is None


async def test_get_settings_shows_the_pending_scope_and_its_progress(logged_in_client, engine) -> None:
    saved = await save_scope(logged_in_client, radius_km=40)
    op_id = saved["apply_scope_operation_id"]
    pending = (await active_row(engine)).pending_scope_version_id
    body = await settings_body(logged_in_client)
    assert body["version"]["id"] == saved["version"]["id"]
    assert body["pending_scope"] == {
        "version_id": pending,
        "progress": {"done": 0, "total": 0},
    }  # not started
    async with engine.begin() as conn:
        await conn.execute(
            sa.text('UPDATE operations SET progress = \'{"done": 500, "total": 1201}\' WHERE id = :i'),
            {"i": op_id},
        )
    body = await settings_body(logged_in_client)
    assert body["pending_scope"] == {"version_id": pending, "progress": {"done": 500, "total": 1201}}
    async with engine.begin() as conn:  # the apply failed for good; the reconciler has not cleared it yet
        for statement in (
            "UPDATE operations SET status = 'running', claim_id = gen_random_uuid(), worker_epoch = 1, "
            "heartbeat_at = now(), started_at = now() WHERE id = :i",
            "UPDATE operations SET status = 'failed', claim_id = NULL, worker_epoch = NULL, "
            "heartbeat_at = NULL, last_error_code = 'attempts_exhausted', finished_at = now() WHERE id = :i",
        ):
            await conn.execute(sa.text(statement), {"i": op_id})
    body = await settings_body(logged_in_client)
    assert body["pending_scope"] == {"version_id": pending, "progress": None}


async def make_versions(client: Any, n: int) -> list[int]:
    """`n` saves of the active payload, each a new version of class `none`; their ids, oldest first."""
    ids = []
    for _ in range(n):
        ids.append(int((await save(client, (await settings_body(client))["payload"]))["version"]["id"]))
    return ids


async def test_the_version_list_pages_with_a_signed_cursor(logged_in_client, engine) -> None:
    v1 = (await active_row(engine)).settings_version_id
    ids = [v1, *await make_versions(logged_in_client, 4)]
    first = await logged_in_client.get(VERSIONS_URL, params={"limit": 2})
    assert first.status_code == 200, first.text
    page = first.json()
    assert [i["version_no"] for i in page["items"]] == [5, 4]
    assert [i["id"] for i in page["items"]] == [ids[4], ids[3]]
    assert set(page["items"][0]) == {"id", "version_no", "created_at", "effect_classes"}
    assert page["next_cursor"] is not None
    second = (
        await logged_in_client.get(VERSIONS_URL, params={"limit": 2, "cursor": page["next_cursor"]})
    ).json()
    assert [i["version_no"] for i in second["items"]] == [3, 2]
    third = (
        await logged_in_client.get(VERSIONS_URL, params={"limit": 2, "cursor": second["next_cursor"]})
    ).json()
    assert [i["version_no"] for i in third["items"]] == [1] and third["next_cursor"] is None
    everything = (await logged_in_client.get(VERSIONS_URL)).json()  # default limit 50
    assert [i["version_no"] for i in everything["items"]] == [5, 4, 3, 2, 1] and everything[
        "next_cursor"
    ] is None
    exact = (await logged_in_client.get(VERSIONS_URL, params={"limit": 5})).json()
    assert len(exact["items"]) == 5 and exact["next_cursor"] is None  # no empty last page


def _unb64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


async def test_a_tampered_cursor_is_400_invalid_cursor(logged_in_client, api_app) -> None:
    await make_versions(logged_in_client, 3)
    cursor = (await logged_in_client.get(VERSIONS_URL, params={"limit": 1})).json()["next_cursor"]
    data, signature = cursor.split(".")
    body = json.loads(_unb64(data))
    assert body == {
        "v": 2,
        "sort": "settings_versions.version_no",
        "dir": "desc",
        "section": "all",
        "last_key": [4],
        "filters_hash": filters_hash({}),
    }
    key = hkdf_sha256(api_app.api.state.cfg.secret_b64("session_secret"), b"cursor")

    def signed(*, key: bytes = key, **changes: Any) -> str:
        fields: dict[str, Any] = {
            "sort": SETTINGS_VERSIONS_SORT,
            "dir": "desc",
            "last_key": [4],
            "filters": {},
        }
        return encode_list_cursor(**{**fields, **changes}, key=key)

    assert signed() == cursor  # signed with HKDF(session_secret, "cursor")
    forged = _b64(json.dumps({**body, "last_key": [2]}, separators=(",", ":"), sort_keys=True).encode())
    other_key = hkdf_sha256(b"\x01" * 32, b"cursor")
    other_sort = signed(sort="created_at")
    other_filters = signed(filters={"effect": "scope"})
    bad_key = signed(last_key=["4"])
    for tampered in (
        f"{forged}.{signature}",
        f"{data}.{'B' if signature[0] == 'A' else 'A'}{signature[1:]}",
        signed(key=other_key),
        signed(dir="asc"),
        signed(sort="version_no"),  # the M3 form of this cursor (resolution (c))
        other_sort,
        other_filters,
        bad_key,
        "not-a-cursor",
        f"{data}.{signature}.x",
        "",
    ):
        r = await logged_in_client.get(VERSIONS_URL, params={"limit": 1, "cursor": tampered})
        assert r.status_code == 400 and r.json()["code"] == "invalid_cursor", tampered


async def test_the_limit_is_bounded(logged_in_client) -> None:
    for limit in (0, 101, -1):
        r = await logged_in_client.get(VERSIONS_URL, params={"limit": limit})
        assert r.status_code == 422 and r.json()["code"] == "validation_failed"
    assert (await logged_in_client.get(VERSIONS_URL, params={"limit": 100})).status_code == 200


async def test_a_version_shows_its_diff_against_the_previous_one(logged_in_client, engine) -> None:
    v1 = (await active_row(engine)).settings_version_id
    first = await logged_in_client.get(f"{VERSIONS_URL}/{v1}")
    assert first.status_code == 200, first.text
    assert set(first.json()) == {"version", "payload", "diff"}
    assert first.json()["diff"] == [] and first.json()["payload"] == load_settings_v1()
    payload = copy.deepcopy((await settings_body(logged_in_client))["payload"])
    payload["schedule"]["interval_hours"] = 12
    payload["display"]["ui_language"] = "de"
    payload["scope"]["locations"] = [berlin(25)]
    payload["scope"]["work_modes"] = ["remote", "onsite"]
    payload["llm"]["cap_usd"] = "120"
    v2 = (await save(logged_in_client, payload))["version"]["id"]
    second = (await logged_in_client.get(f"{VERSIONS_URL}/{v2}")).json()
    assert second["version"]["id"] == v2 and second["version"]["version_no"] == 2
    assert second["version"]["effect_classes"] == ["budget", "collection", "schedule", "scope"]
    assert second["diff"] == [
        {"path": "payload.schedule.interval_hours", "before": 6, "after": 12},
        {"path": "payload.display.ui_language", "before": "en", "after": "de"},
        {"path": "payload.scope.locations[0]", "before": None, "after": berlin(25)},
        {"path": "payload.scope.work_modes[1]", "before": "hybrid", "after": "onsite"},
        {"path": "payload.scope.work_modes[2]", "before": "onsite", "after": None},
        {"path": "payload.llm.cap_usd", "before": "100.00", "after": "120.00"},  # normal forms
    ]
    # a radius change of the same place is matched by its gazetteer id
    payload = copy.deepcopy((await settings_body(logged_in_client))["payload"])
    payload["scope"]["locations"][0]["radius_km"] = 40
    v3 = (await save(logged_in_client, payload))["version"]["id"]
    third = (await logged_in_client.get(f"{VERSIONS_URL}/{v3}")).json()
    assert third["diff"] == [{"path": "payload.scope.locations[0].radius_km", "before": 25, "after": 40}]


async def test_an_unknown_version_is_404(logged_in_client) -> None:
    r = await logged_in_client.get(f"{VERSIONS_URL}/987654321")
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    assert (await logged_in_client.get(f"{VERSIONS_URL}/0")).status_code == 422


async def test_the_read_routes_need_a_session(api_client) -> None:
    for path in ("/api/settings", VERSIONS_URL, f"{VERSIONS_URL}/1"):
        r = await api_client.get(path)
        assert r.status_code == 401 and r.json()["code"] == "unauthenticated"
