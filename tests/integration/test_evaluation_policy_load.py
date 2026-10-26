"""Loading an evaluation policy row (spec §11.7.4; M3 Task 6): `evaluation_policy.load` reads
`evaluation_policies.payload` by id and validates it as `from_payload` does."""

import pytest
from jobhunter.db.seed import load_settings_v1
from jobhunter.scoring import evaluation_policy
from jobhunter.scoring.evaluation_policy import EvaluationPolicy
from jobhunter.types import get_object
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.m3 import active_ids

pytestmark = pytest.mark.integration


async def test_load_reads_the_seeded_policy(engine: AsyncEngine) -> None:
    async with engine.connect() as c:
        policy_id = (await active_ids(c)).evaluation_policy_id
        loaded = await evaluation_policy.load(c, policy_id)
    assert loaded == EvaluationPolicy.from_payload(get_object(load_settings_v1(), "evaluation"))


async def test_load_of_an_unknown_policy_is_a_lookup_error(engine: AsyncEngine) -> None:
    async with engine.connect() as c:
        policy_id = (await active_ids(c)).evaluation_policy_id
        with pytest.raises(LookupError):
            await evaluation_policy.load(c, policy_id + 1000)
