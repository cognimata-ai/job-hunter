"""The release of a quarantined batch submission over HTTP (spec §10.10 L3991-3997, §14.1 L4963, §14.2 L5027,
§18.3 C21; plan Task 21).

`POST /api/batches/{id}/release` (`Idempotency-Key`, `{confirm: true}`) runs `batches.release` after
`active_config FOR SHARE` and the idempotency row: 202 `{submission_id, state: "released", reserved_usd}`,
the amount the submission's entries keep counted as `ambiguous`. 409 `conflict_state` unless the submission
is `submit_ambiguous`, 404 for no such submission, 422 without `confirm: true`.

C21's user half: the connection dropped before the batch existed, the submission stays quarantined until the
user releases it; the release re-scores its targets. The targets are 250 synthetic ads of the score tuple in
one bulk operation; every datum is invented.
"""

from collections import Counter
from typing import Any, Final

import httpx
import procrastinate
import pytest
from jobhunter.llm.fake import BatchScript
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.batches import (
    PUBLISHED_BODY,
    BulkTargets,
    batch_ledger,
    bulk_with_targets,
    submission_rows,
)
from tests.support.llm import FakeLLM
from tests.support.profile import OpRunner, idem
from tests.support.scoring import attempt_outcomes, targets_by_state
from tests.support.settings import fresh_key

pytestmark = pytest.mark.integration
N: Final = 250
CONFIRM: Final = {"confirm": True}


@pytest.fixture
async def setup(
    engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int, queue_app: procrastinate.App
) -> BulkTargets:
    made = await bulk_with_targets(engine, N)
    for target in made.targets:
        fake_llm.answer("generate", target.job_revision_id, PUBLISHED_BODY)
    return made


async def quarantined(engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, setup: BulkTargets) -> Any:
    """C21: the setup's create lost before the batch existed (`ambiguous_not_created`): `submit_ambiguous`."""
    fake_llm.batches(BatchScript(create="ambiguous_not_created"))
    assert setup.operation_id is not None
    await op_runner.run_one(setup.operation_id)
    (row,) = await submission_rows(engine)
    assert row.state == "submit_ambiguous"
    return row


def release_url(submission_id: int) -> str:
    return f"/api/batches/{submission_id}/release"


async def test_the_release_of_a_quarantined_submission_re_scores_its_targets(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    op_runner: OpRunner,
    fake_llm: FakeLLM,
    setup: BulkTargets,
) -> None:
    row = await quarantined(engine, op_runner, fake_llm, setup)
    for body in ({}, {"confirm": False}):
        r = await logged_in_client.post(release_url(row.id), json=body, headers=idem(fresh_key("release")))
        assert (r.status_code, r.json()["code"]) == (422, "validation_failed"), r.text
    r = await logged_in_client.post(release_url(row.id), json=CONFIRM)
    assert (r.status_code, r.json()["code"]) == (400, "idempotency_key_required")
    assert (await submission_rows(engine))[0].state == "submit_ambiguous"  # nothing moved yet

    key = fresh_key("release")
    r = await logged_in_client.post(release_url(row.id), json=CONFIRM, headers=idem(key))
    assert r.status_code == 202, r.text
    assert r.json() == {
        "submission_id": row.id,
        "state": "released",
        "reserved_usd": format(row.reserved_usd, "f"),
    }
    (after,) = await submission_rows(engine)
    assert (after.state, after.released_at is not None) == ("released", True)
    assert await targets_by_state(engine, setup.llm_tuple_id) == {"pending": N}
    assert await attempt_outcomes(engine, setup.llm_tuple_id) == {"released": N}
    assert Counter(state for state, _ in await batch_ledger(engine)) == {"ambiguous": N}

    replay = await logged_in_client.post(release_url(row.id), json=CONFIRM, headers=idem(key))
    assert (replay.status_code, replay.json()) == (202, r.json())  # the stored response
    again = await logged_in_client.post(release_url(row.id), json=CONFIRM, headers=idem(fresh_key("release")))
    assert (again.status_code, again.json()["code"]) == (409, "conflict_state")  # released already
    r = await logged_in_client.post(release_url(10**12), json=CONFIRM, headers=idem(fresh_key("release")))
    assert (r.status_code, r.json()["code"]) == (404, "not_found")


async def test_a_submission_that_is_not_quarantined_is_a_state_conflict(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    op_runner: OpRunner,
    fake_llm: FakeLLM,
    setup: BulkTargets,
) -> None:
    fake_llm.batches(BatchScript(polls_until_ended=5))  # created and answered: `submitted`
    assert setup.operation_id is not None
    await op_runner.run_one(setup.operation_id)
    (row,) = await submission_rows(engine)
    assert row.state == "submitted"
    r = await logged_in_client.post(release_url(row.id), json=CONFIRM, headers=idem(fresh_key("release")))
    assert (r.status_code, r.json()["code"]) == (409, "conflict_state"), r.text
    (after,) = await submission_rows(engine)
    assert after.state == "submitted" and await targets_by_state(engine, setup.llm_tuple_id) == {
        "in_batch": N
    }
