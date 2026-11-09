"""A stored document that the current models refuse, as after a later release tightened one (final review
FR-3): the three GETs that read one answer a degraded body, never a 500 with an alert, and never quote the
stored values or the validation error.

- `GET /api/profile/drafts/{id}`: `draft: null`, one `validation_issues` entry `schema_mismatch` without a
  value, and `repairs` offering `paste_text` (a new draft, which discards this one), so the client can
  discard; a stored issue the current model refuses is left out;
- `GET /api/profile`: the version's `profile: null`, with `purged: false`;
- `GET /api/profile/query-proposals/{id}`: an item the current model refuses is left out of `items`.

Every datum is invented; the stored documents are edited in place as a schema change would leave them.
"""

import json
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.fixtures import count_rows
from tests.support.profile import SYNTHETIC_QUERIES, OpRunner, draft_row, idem
from tests.support.settings import activate_profile, fresh_key

pytestmark = pytest.mark.integration

STORED_VALUE: Final = "grandmaster"  # a seniority no release knows: the stored value the response never shows
# What a pydantic error or the stored document would put in a body.
NEVER_SHOWN: Final = (STORED_VALUE, "literal_error", "Input should", "model_type", "brand_new_code")


async def execute(engine: AsyncEngine, statement: str, **params: object) -> None:
    async with engine.begin() as conn:
        await conn.execute(sa.text(statement), params)


def assert_nothing_quoted(r: httpx.Response) -> None:
    for value in NEVER_SHOWN:
        assert value not in r.text, value


async def assert_no_alert(engine: AsyncEngine) -> None:
    assert await count_rows(engine, "alerts", code="api.unhandled_exception") == 0


async def test_a_draft_the_profile_schema_refuses_reads_as_a_schema_mismatch(
    engine, logged_in_client, ready_draft: int
) -> None:
    stored = {**(await draft_row(engine, ready_draft)).draft_json, "seniority": STORED_VALUE}
    await execute(
        engine,
        "UPDATE profile_drafts SET draft_json = CAST(:j AS jsonb) WHERE id = :d",
        j=json.dumps(stored),
        d=ready_draft,
    )
    r = await logged_in_client.get(f"/api/profile/drafts/{ready_draft}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["status"], body["draft"], body["recomputed_total_years"]) == ("ready", None, None)
    assert [(i["path"], i["code"], i["proposal"]) for i in body["validation_issues"]] == [
        ("", "schema_mismatch", None)
    ]
    assert body["repairs"] == ["paste_text"]
    assert_nothing_quoted(r)
    await assert_no_alert(engine)
    discarded = await logged_in_client.post(f"/api/profile/drafts/{ready_draft}/discard")
    assert discarded.status_code == 204  # the client can leave it


async def test_a_stored_issue_the_current_model_refuses_is_left_out(
    engine, logged_in_client, ready_draft: int
) -> None:
    kept = (await draft_row(engine, ready_draft)).validation_issues
    refused: list[dict[str, Any]] = [{"path": "summary", "code": "brand_new_code", "message": "x"}]
    await execute(
        engine,
        "UPDATE profile_drafts SET validation_issues = CAST(:j AS jsonb) WHERE id = :d",
        j=json.dumps([*refused, *kept]),
        d=ready_draft,
    )
    r = await logged_in_client.get(f"/api/profile/drafts/{ready_draft}")
    assert r.status_code == 200, r.text
    assert [(i["path"], i["code"]) for i in r.json()["validation_issues"]] == [
        (i["path"], i["code"]) for i in kept
    ]
    assert r.json()["draft"] is not None
    assert_nothing_quoted(r)
    await assert_no_alert(engine)


async def test_a_profile_version_the_schema_refuses_reads_as_no_profile_and_not_purged(
    engine, logged_in_client
) -> None:
    version_id, _ = await activate_profile(engine)
    await execute(
        engine,
        "UPDATE profile_versions "
        "SET profile_json = jsonb_set(profile_json, '{seniority}', CAST(:s AS jsonb)) WHERE id = :v",
        s=json.dumps(STORED_VALUE),
        v=version_id,
    )
    r = await logged_in_client.get("/api/profile")
    assert r.status_code == 200, r.text
    (version,) = r.json()["versions"]
    assert (version["id"], version["profile"], version["purged"]) == (version_id, None, False)
    assert r.json()["active"] == version
    assert_nothing_quoted(r)
    await assert_no_alert(engine)


async def test_a_proposal_item_the_current_model_refuses_is_left_out(
    engine, logged_in_client, op_runner: OpRunner
) -> None:
    await activate_profile(engine)
    created = await logged_in_client.post("/api/profile/query-proposals", headers=idem(fresh_key("p")))
    assert created.status_code == 202, created.text
    proposal_id = int(created.json()["proposal_id"])
    await op_runner.run_pending()
    queries = SYNTHETIC_QUERIES["queries"]
    refused = {**queries[0], "lang": STORED_VALUE}
    await execute(
        engine,
        "UPDATE query_proposals SET items = CAST(:j AS jsonb) WHERE id = :p",
        j=json.dumps({"queries": [refused, *queries[1:]]}),
        p=proposal_id,
    )
    r = await logged_in_client.get(f"/api/profile/query-proposals/{proposal_id}")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ready", "items": queries[1:], "failure_code": None}
    assert_nothing_quoted(r)
    await assert_no_alert(engine)
