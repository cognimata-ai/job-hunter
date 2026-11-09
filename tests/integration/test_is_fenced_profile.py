import pytest
import sqlalchemy as sa

from tests.support.fences import insert_test_fence
from tests.support.m2 import insert_profile_chain

pytestmark = pytest.mark.integration


async def test_a_profile_fence_fences_the_tuple_of_its_version_and_no_other(engine) -> None:
    async with engine.connect() as c:

        async def fenced(job: int, tuple_id: int | None) -> object:  # the raw result: `is False` refuses NULL
            row = await c.execute(sa.text("SELECT is_fenced(:j, :t)"), {"j": job, "t": tuple_id})
            return row.scalar_one()

        first = await insert_profile_chain(c, 1)
        second = await insert_profile_chain(c, 2)
        tuples = (first.llm_tuple_id, second.llm_tuple_id, None, -1)
        # no fence: nothing is fenced, with or without a tuple
        assert [await fenced(5, t) for t in tuples] == [False, False, False, False]

        fence = await insert_test_fence(c, kind="profile", profile_version_id=first.profile_version_id)
        assert [await fenced(5, t) for t in tuples] == [True, False, False, False]
        # the fence is about the tuple, whatever job is asked about
        assert [await fenced(6, t) for t in tuples] == [True, False, False, False]
        # the default tuple argument is NULL: a job-only question never matches a profile fence
        assert (await c.execute(sa.text("SELECT is_fenced(5)"))).scalar_one() is False

        # a second fence on the other version fences the other tuple too
        other = await insert_test_fence(c, kind="profile", profile_version_id=second.profile_version_id)
        assert [await fenced(5, t) for t in tuples] == [True, True, False, False]

        # a released fence fences nothing
        await c.execute(sa.text("UPDATE purge_fences SET released_at = now() WHERE id = :f"), {"f": fence})
        assert [await fenced(5, t) for t in tuples] == [False, True, False, False]
        await c.execute(sa.text("UPDATE purge_fences SET released_at = now() WHERE id = :f"), {"f": other})
        assert [await fenced(5, t) for t in tuples] == [False, False, False, False]


async def test_the_other_branches_still_fence_with_the_profile_branch_in_place(engine) -> None:
    async with engine.connect() as c:
        chain = await insert_profile_chain(c)
        await insert_test_fence(c, kind="jobs", job_ids=[5])
        row = await c.execute(
            sa.text("SELECT is_fenced(5, :t), is_fenced(6, :t), is_fenced(5), is_fenced(6)"),
            {"t": chain.llm_tuple_id},
        )
        assert tuple(row.one()) == (True, False, True, False)
        await insert_test_fence(c, kind="global")
        row = await c.execute(sa.text("SELECT is_fenced(6, :t), is_fenced(6)"), {"t": chain.llm_tuple_id})
        assert tuple(row.one()) == (True, True)
