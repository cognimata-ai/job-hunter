"""Crash cases C24 and C34 of the profile confirm (spec §12.6 step 2, §18.3; plan Task 21).

In process: an injected exception inside the confirm's transaction (`raising_at`, a `SimulatedCrash` at one of
`profile.activation`'s fault points) rolls the transaction back, as a killed process would. The SPA sees the
M0 500 `internal`; the client's retry is the test's own. The draft is the synthetic CV through the real
pipeline (`ready_draft`); no real CV and no paid call.
"""

import pytest
import sqlalchemy as sa
from jobhunter.profile import activation as activation_module
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.faults import no_fault, raising_at
from tests.support.fixtures import count_rows
from tests.support.profile import active_profile, draft_row, idem, ranking_generation

pytestmark = pytest.mark.integration


async def only_tuple_id(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(sa.text("SELECT id FROM llm_tuples"))).scalar_one())


@pytest.mark.parametrize("point", ["confirm.after_version", "confirm.after_flip", "confirm.before_commit"])
async def test_c24_crash_inside_activation_is_all_or_nothing(
    logged_in_client, engine, ready_draft, point, monkeypatch
) -> None:
    generation_before = await ranking_generation(engine)
    monkeypatch.setattr(activation_module, "afault_point", raising_at(point))  # SimulatedCrash at the point
    r = await logged_in_client.post(
        f"/api/profile/drafts/{ready_draft}/confirm",
        json={"expected_rescore_ack": True},
        headers=idem("c24-key-000000001"),
    )
    assert r.status_code == 500 and r.json()["code"] == "internal"
    assert await active_profile(engine) == (None, None)  # profile_version_id, llm_tuple_id
    assert await count_rows(engine, "profile_versions") == 0 and await count_rows(engine, "llm_tuples") == 0
    assert (
        await count_rows(engine, "operations", kind=("embed_profile", "propose_queries", "apply_scope")) == 0
    )
    assert await count_rows(engine, "query_proposals") == 0
    assert await count_rows(engine, "api_idempotency", route="profile.confirm") == 0
    assert await ranking_generation(engine) == generation_before
    draft = await draft_row(engine, ready_draft)
    assert (draft.status, draft.confirmed_profile_version_id, draft.confirm_response) == ("ready", None, None)


async def test_c34_retry_with_the_same_key_activates_once(
    logged_in_client, engine, ready_draft, monkeypatch
) -> None:
    key, generation_before = idem("c34-key-000000001"), await ranking_generation(engine)
    url = f"/api/profile/drafts/{ready_draft}/confirm"
    monkeypatch.setattr(activation_module, "afault_point", raising_at("confirm.before_commit"))
    crashed = await logged_in_client.post(url, json={"expected_rescore_ack": True}, headers=key)
    assert crashed.status_code == 500
    monkeypatch.setattr(activation_module, "afault_point", no_fault)
    first = await logged_in_client.post(url, json={"expected_rescore_ack": True}, headers=key)
    again = await logged_in_client.post(url, json={"expected_rescore_ack": True}, headers=key)
    other = await logged_in_client.post(
        url, json={"expected_rescore_ack": True}, headers=idem("c34-key-000000002")
    )
    assert first.status_code == again.status_code == other.status_code == 201
    assert first.json() == again.json() == other.json()
    assert await count_rows(engine, "profile_versions") == 1 and await count_rows(engine, "llm_tuples") == 1
    assert await ranking_generation(engine) == generation_before + 1
    assert await count_rows(engine, "operations", kind=("embed_profile",)) == 1
    assert await count_rows(engine, "operations", kind=("propose_queries",)) == 1
    assert await active_profile(engine) == (first.json()["profile_version_id"], await only_tuple_id(engine))
