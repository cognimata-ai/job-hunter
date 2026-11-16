import pytest
import sqlalchemy as sa
from jobhunter.db.sql import FOREIGN_KEY_VIOLATION, PgErrorInfo, pg_error

from tests.support.fences import insert_test_fence

pytestmark = pytest.mark.integration

JOB = (
    "INSERT INTO jobs (company_key, company_display, title, title_norm, location_kind, work_mode_possible, "
    "work_mode_source, employment_source, ad_language, completeness, posted_precision, first_seen_at, "
    "last_seen_at, availability, availability_changed_at, source_ids, field_provenance, det_facts_hash) "
    "VALUES ('acme','Acme','T','t','unknown','{remote,hybrid,onsite}','none','none','unknown','full',"
    "'unknown',"
    "now(),now(),'active',now(),'{adzuna}','{}','\\x00') RETURNING id"
)
POSTING = (
    "INSERT INTO postings (source_instance_id, native_id, job_id, first_seen_at, last_seen_at, availability, "
    "availability_changed_at) VALUES (:i,'1',:j,now(),now(),'active',now())"
)
REVISION = (
    "INSERT INTO job_revisions (job_id, revision_no, material_hash, llm_input, description_text, "
    "description_completeness, ad_language, embedding_status) "
    "VALUES (:j,1,'\\x01','{}','d','full','de','awaiting_embedding') RETURNING id"
)


async def test_source_fence_matches_posting_lineage_and_stored_set(engine) -> None:
    async with engine.begin() as c:

        async def fenced(job: int) -> object:  # the raw result: `is False` below also refuses a NULL
            return (await c.execute(sa.text("SELECT is_fenced(:j)"), {"j": job})).scalar_one()

        job_a, job_b, job_c = [(await c.execute(sa.text(JOB))).scalar_one() for _ in range(3)]
        adz = (
            await c.execute(sa.text("SELECT id FROM source_instances WHERE source_id='adzuna'"))
        ).scalar_one()
        await c.execute(sa.text(POSTING), {"i": adz, "j": job_a})  # a: a posting of the source
        rev = (await c.execute(sa.text(REVISION), {"j": job_b})).scalar_one()
        await c.execute(
            sa.text(
                "INSERT INTO job_revision_sources (job_revision_id, field, source_id) "
                "VALUES (:r,'description_text','adzuna')"
            ),
            {"r": rev},
        )  # b: a revision whose lineage names the source
        # c: in the stored affected set only
        fence = await insert_test_fence(c, kind="source", source_id="adzuna", job_ids=[job_c])
        assert [await fenced(j) for j in (job_a, job_b, job_c)] == [True, True, True]
        other = (await c.execute(sa.text(JOB))).scalar_one()  # `jobs.source_ids` alone does not fence
        assert await fenced(other) is False
        await c.execute(sa.text("UPDATE purge_fences SET released_at = now() WHERE id=:f"), {"f": fence})
        assert await fenced(job_a) is False
        # purge_fences.source_id -> sources (M0 Deviation 1)
        with pytest.raises(sa.exc.IntegrityError) as bad:
            async with c.begin_nested():
                await insert_test_fence(c, kind="source", source_id="nope")
    # The exact foreign key, through M0's helper: no other constraint refused the row.
    assert pg_error(bad.value) == PgErrorInfo(FOREIGN_KEY_VIOLATION, "purge_fences_source_id_fkey")
