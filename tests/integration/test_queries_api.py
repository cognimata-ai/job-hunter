"""Query authoring (spec §5.5.1, §13.3, §13.5, §14.2; plan Task 20, Deviations 3 and 15): `POST /api/queries`
adds a query without enabling it, `PATCH /api/queries/{id}` retires the row and creates its successor, and
moves the active settings' `queries[]` entry to it in one internal settings save of class `collection`.

Every query text is an invented job title; the proposal rows are synthetic.
"""

import copy
import json
from datetime import UTC, datetime
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.db.sql import q
from jobhunter.queries.authoring import create_query
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.fixtures import count_rows
from tests.support.m2 import insert_profile_chain
from tests.support.settings import active_row, berlin, save, settings_body

pytestmark = pytest.mark.integration

URL: Final = "/api/queries"
_PROPOSAL: Final = q(
    "INSERT INTO query_proposals (profile_version_id, status, items) "
    "VALUES (:pv, :status, NULLIF(:items, CAST('null' AS jsonb))) RETURNING id",
    jsonb=["items"],
)
_QUERY: Final = sa.text("SELECT * FROM queries WHERE id = :i")
_CELLS: Final = sa.text(
    "SELECT i.source_id, c.location_key FROM coverage_cells c JOIN source_instances i "
    "ON i.id = c.source_instance_id WHERE c.query_id = :q ORDER BY i.source_id, c.location_key"
)
ITEM: Final = {
    "text": "Platform Engineer",
    "lang": "en",
    "target_title": "Senior Backend Developer",
    "exploration": True,
    "rationale": "A neighbouring platform role.",
}


async def post(client: httpx.AsyncClient, body: dict[str, Any]) -> httpx.Response:
    """The body as ASCII JSON: a lone surrogate travels as its escape (`"\\ud800"`), as a client sends it."""
    return await client.post(URL, content=json.dumps(body), headers={"content-type": "application/json"})


async def created(client: httpx.AsyncClient, body: dict[str, Any]) -> dict[str, Any]:
    r = await post(client, body)
    assert r.status_code == 201, (r.status_code, r.text)
    out: dict[str, Any] = r.json()
    return out


async def query_row(engine: AsyncEngine, query_id: int) -> sa.Row[Any]:
    async with engine.connect() as conn:
        return (await conn.execute(_QUERY, {"i": query_id})).one()


async def proposal(engine: AsyncEngine, status: str = "ready") -> int:
    """A proposal of a synthetic profile chain, `ready` with one item unless `status` says otherwise."""
    async with engine.begin() as conn:
        chain = await insert_profile_chain(conn)
        items = {"queries": [ITEM]} if status == "ready" else None
        row = await conn.execute(
            _PROPOSAL, {"pv": chain.profile_version_id, "status": status, "items": items}
        )
        return int(row.scalar_one())


async def enabled_in_settings(client: httpx.AsyncClient, query_id: int, *, enabled: bool) -> dict[str, Any]:
    """The active payload with `query_id` in `queries[]`, a Berlin scope location, saved (the 201 body)."""
    payload = copy.deepcopy((await settings_body(client))["payload"])
    payload["scope"]["locations"] = [berlin(30)]
    payload["queries"] = [{"query_id": query_id, "enabled": enabled}]
    return await save(client, payload)


# --- POST /api/queries ------------------------------------------------------------------------------------


async def test_a_new_query_is_a_user_query_and_is_not_enabled(engine, logged_in_client) -> None:
    before = await settings_body(logged_in_client)
    body = await created(logged_in_client, {"text": "  Backend Entwickler Python ", "lang": "de"})
    assert body == {
        "id": body["id"],
        "text": "Backend Entwickler Python",  # trimmed
        "lang": "de",
        "origin": "user",
        "exploration": False,
        "proposal_id": None,
    }
    row = await query_row(engine, body["id"])
    assert (row.text, row.origin, row.retired_at) == ("Backend Entwickler Python", "user", None)
    after = await settings_body(logged_in_client)
    assert after == before  # enabling is a settings save with its preview (§13.5)
    assert await count_rows(engine, "coverage_cells", query_id=body["id"]) == 0


async def test_accepting_a_proposal_item_keeps_its_origin(engine, logged_in_client) -> None:
    proposal_id = await proposal(engine)
    body = await created(
        logged_in_client,
        {"text": ITEM["text"], "lang": "en", "proposal_id": proposal_id, "exploration": True},
    )
    assert (body["origin"], body["proposal_id"], body["exploration"]) == ("llm_proposal", proposal_id, True)
    row = await query_row(engine, body["id"])
    assert (row.origin, row.proposal_id, row.exploration) == ("llm_proposal", proposal_id, True)


@pytest.mark.parametrize("status", ["running", "failed"])
async def test_a_proposal_that_is_not_ready_cannot_be_accepted(engine, logged_in_client, status: str) -> None:
    proposal_id = await proposal(engine, status)
    r = await post(logged_in_client, {"text": ITEM["text"], "lang": "en", "proposal_id": proposal_id})
    assert (r.status_code, r.json()["code"]) == (409, "conflict_state")
    assert await count_rows(engine, "queries") == 0


async def test_an_unknown_proposal_is_refused(engine, logged_in_client) -> None:
    r = await post(logged_in_client, {"text": ITEM["text"], "lang": "en", "proposal_id": 987654})
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed")
    assert [e["field"] for e in r.json()["errors"]] == ["body.proposal_id"]
    assert await count_rows(engine, "queries") == 0


async def test_a_duplicate_live_text_is_a_conflict_naming_the_existing_query(
    engine, logged_in_client
) -> None:
    first = await created(logged_in_client, {"text": "Data Engineer", "lang": "en"})
    r = await post(logged_in_client, {"text": "DATA ENGINEER", "lang": "de"})
    assert (r.status_code, r.json()["code"], r.json()["query_id"]) == (409, "conflict_state", first["id"])
    assert await count_rows(engine, "queries") == 1


@pytest.mark.parametrize("text", ["A", "  B  ", "x" * 121, "Data\x00Engineer", "Data \ud800 Engineer"])
async def test_an_invalid_text_is_refused(engine, logged_in_client, text: str) -> None:
    r = await post(logged_in_client, {"text": text, "lang": "en"})
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed")
    assert [e["field"] for e in r.json()["errors"]] == ["body.text"]
    assert await count_rows(engine, "queries") == 0


async def test_create_query_takes_the_injected_clock(engine) -> None:
    now = datetime(2026, 10, 22, 9, 30, tzinfo=UTC)
    async with engine.begin() as conn:
        row = await create_query(conn, text="Site Reliability Engineer", lang="en", now=now)
    assert (await query_row(engine, row.id)).created_at == now


# --- PATCH /api/queries/{id} ------------------------------------------------------------------------------


async def test_an_edit_retires_the_row_and_moves_the_settings_entry_to_its_successor(
    engine, logged_in_client
) -> None:
    old = await created(logged_in_client, {"text": "Data Engineer", "lang": "en"})
    await enabled_in_settings(logged_in_client, old["id"], enabled=True)
    before = await settings_body(logged_in_client)
    r = await logged_in_client.patch(f"{URL}/{old['id']}", json={"text": "Data Engineer Kafka"})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["id"] != old["id"]
    assert {k: v for k, v in new.items() if k != "id"} == {
        "text": "Data Engineer Kafka",
        "lang": "en",
        "origin": "user",
        "exploration": False,
        "proposal_id": None,
    }
    retired = await query_row(engine, old["id"])
    assert retired.retired_at is not None and retired.text == "Data Engineer"
    after = await settings_body(logged_in_client)
    assert after["version"]["version_no"] == before["version"]["version_no"] + 1  # one new version
    assert after["version"]["effect_classes"] == ["collection"]
    assert after["payload"]["queries"] == [{"query_id": new["id"], "enabled": True}]  # its flag kept
    expected = copy.deepcopy(before["payload"])
    expected["queries"] = after["payload"]["queries"]
    assert after["payload"] == expected  # nothing else changed
    async with engine.connect() as conn:
        cells = [tuple(row) for row in await conn.execute(_CELLS, {"q": new["id"]})]
        version = (
            await conn.execute(
                sa.text("SELECT acknowledged_warnings, base_version_id FROM settings_versions WHERE id = :v"),
                {"v": after["version"]["id"]},
            )
        ).one()
    assert cells == [
        ("adzuna", "berlin"),
        ("adzuna", "remote-de"),
        ("jsearch", "berlin"),
        ("jsearch", "remote-de"),
        ("serpapi", "berlin"),
        ("serpapi", "remote-de"),
    ]
    assert version.base_version_id == before["version"]["id"]
    assert (await active_row(engine)).settings_version_id == after["version"]["id"]


async def test_an_edit_keeps_a_disabled_flag_and_the_proposal_origin(engine, logged_in_client) -> None:
    proposal_id = await proposal(engine)
    old = await created(
        logged_in_client,
        {"text": ITEM["text"], "lang": "en", "proposal_id": proposal_id, "exploration": True},
    )
    await enabled_in_settings(logged_in_client, old["id"], enabled=False)
    r = await logged_in_client.patch(f"{URL}/{old['id']}", json={"lang": "de"})
    assert r.status_code == 200, r.text
    new = r.json()
    assert (new["text"], new["lang"], new["origin"], new["exploration"], new["proposal_id"]) == (
        ITEM["text"],
        "de",
        "llm_proposal",
        True,
        proposal_id,
    )
    after = await settings_body(logged_in_client)
    assert after["payload"]["queries"] == [{"query_id": new["id"], "enabled": False}]
    assert after["version"]["effect_classes"] == ["collection"]
    assert await count_rows(engine, "coverage_cells", query_id=new["id"]) == 0  # a disabled query has none


async def test_a_case_only_edit_replaces_the_live_text(engine, logged_in_client) -> None:
    old = await created(logged_in_client, {"text": "data engineer", "lang": "en"})
    r = await logged_in_client.patch(f"{URL}/{old['id']}", json={"text": "Data Engineer"})
    assert (r.status_code, r.json()["text"]) == (200, "Data Engineer")
    assert (await query_row(engine, old["id"])).retired_at is not None


async def test_an_edit_of_a_query_outside_the_settings_writes_no_version(engine, logged_in_client) -> None:
    old = await created(logged_in_client, {"text": "Data Engineer", "lang": "en"})
    before = await settings_body(logged_in_client)
    r = await logged_in_client.patch(f"{URL}/{old['id']}", json={"text": "Data Engineer Kafka"})
    assert r.status_code == 200
    assert await settings_body(logged_in_client) == before


async def test_an_edit_that_changes_nothing_keeps_the_row(engine, logged_in_client) -> None:
    old = await created(logged_in_client, {"text": "Data Engineer", "lang": "en"})
    r = await logged_in_client.patch(f"{URL}/{old['id']}", json={"text": " Data Engineer ", "lang": "en"})
    assert (r.status_code, r.json()) == (200, old)
    assert (await query_row(engine, old["id"])).retired_at is None
    assert await count_rows(engine, "queries") == 1


async def test_an_edit_of_a_retired_query_is_not_found(engine, logged_in_client) -> None:
    old = await created(logged_in_client, {"text": "Data Engineer", "lang": "en"})
    r = await logged_in_client.patch(f"{URL}/{old['id']}", json={"text": "Data Engineer Kafka"})
    assert r.status_code == 200
    again = await logged_in_client.patch(f"{URL}/{old['id']}", json={"text": "Data Engineer Spark"})
    assert (again.status_code, again.json()["code"]) == (404, "not_found")
    unknown = await logged_in_client.patch(f"{URL}/987654", json={"text": "Data Engineer Spark"})
    assert (unknown.status_code, unknown.json()["code"]) == (404, "not_found")
    assert await count_rows(engine, "queries") == 2


async def test_an_edit_onto_another_live_text_changes_nothing(engine, logged_in_client) -> None:
    taken = await created(logged_in_client, {"text": "Platform Engineer", "lang": "en"})
    old = await created(logged_in_client, {"text": "Data Engineer", "lang": "en"})
    await enabled_in_settings(logged_in_client, old["id"], enabled=True)
    before = await settings_body(logged_in_client)
    r = await logged_in_client.patch(f"{URL}/{old['id']}", json={"text": "platform engineer"})
    assert (r.status_code, r.json()["code"], r.json()["query_id"]) == (409, "conflict_state", taken["id"])
    assert (await query_row(engine, old["id"])).retired_at is None  # the retire rolled back with it
    assert await settings_body(logged_in_client) == before
    assert await count_rows(engine, "queries") == 2


async def test_an_edit_with_an_invalid_text_is_refused(engine, logged_in_client) -> None:
    old = await created(logged_in_client, {"text": "Data Engineer", "lang": "en"})
    r = await logged_in_client.patch(f"{URL}/{old['id']}", json={"text": "x"})
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed")
    assert (await query_row(engine, old["id"])).retired_at is None
