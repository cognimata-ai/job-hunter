from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db import ranking
from jobhunter.dedup import revisions
from jobhunter.dedup.revisions import RecomputeResult
from jobhunter.errors import InvariantViolation
from jobhunter.normalize.companies import company_key
from jobhunter.scoring.filters import det_facts_hash, load_facts
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.collect import JobWithPostings, add_observation, add_posting
from tests.support.facts import BERLIN, HAMBURG
from tests.support.observations import predicted, stated

pytestmark = pytest.mark.integration

LOCK_JOB = sa.text("SELECT id FROM jobs WHERE id = :job FOR UPDATE")
JOB = sa.text("SELECT * FROM jobs WHERE id = :job")
REVISIONS = sa.text(
    "SELECT id, revision_no, material_hash, embedding_status, description_text FROM job_revisions "
    "WHERE job_id = :job ORDER BY id"
)
LINEAGE = sa.text(
    "SELECT field, source_id, posting_id, observation_id FROM job_revision_sources "
    "WHERE job_revision_id = :revision ORDER BY field, source_id"
)
OCCURRENCES = sa.text(
    "SELECT job_revision_id, became_current_at FROM job_revision_occurrences WHERE job_id = :job "
    "ORDER BY became_current_at"
)
GENERATION = sa.text("SELECT generation FROM ranking_state WHERE id = 1")
ALERT = sa.text(
    "SELECT severity, occurrences, resolved_at FROM alerts WHERE code = 'dedup.field_conflict' "
    "AND dedup_key = :key"
)
LLM_INPUT_FIELDS = (
    "ad_language",
    "company_display",
    "description_completeness",
    "description_text",
    "employment_source",
    "employment_types",
    "locations",
    "title",
    "work_mode_possible",
    "work_mode_source",
)
# A caller clock far from the database's: nothing `recompute_job` writes may come from it (carry 3).
CALLER_NOW = datetime(2001, 1, 1, tzinfo=UTC)


async def recompute_in(
    conn: AsyncConnection, job_id: int, *, fenced: frozenset[str] = frozenset()
) -> RecomputeResult:
    """`recompute_job` as its callers run it: the job row `FOR UPDATE` first, the ranking bumped last when the
    job changed (§7.1)."""
    await conn.execute(LOCK_JOB, {"job": job_id})
    result = await revisions.recompute_job(conn, job_id, now=CALLER_NOW, fenced_source_ids=fenced)
    if result.current_changed or result.det_facts_changed:
        await ranking.bump(conn)
    return result


async def recompute(
    engine: AsyncEngine, job_id: int, *, fenced: frozenset[str] = frozenset()
) -> RecomputeResult:
    async with engine.begin() as conn:
        return await recompute_in(conn, job_id, fenced=fenced)


async def fetch(engine: AsyncEngine, statement: sa.TextClause, **params: Any) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(statement, params)).all())


async def job_row(engine: AsyncEngine, job_id: int) -> Any:
    return (await fetch(engine, JOB, job=job_id))[0]


async def generation(engine: AsyncEngine) -> int:
    return int((await fetch(engine, GENERATION))[0].generation)


async def test_revision_reuse_lineage_and_occurrences(
    engine: AsyncEngine, current_epoch: int, job_with_postings: JobWithPostings
) -> None:
    made = await job_with_postings(
        {"jsearch": {"title_raw": "Python Developer"}, "adzuna": {"description_completeness": "partial"}}
    )
    job, posting = made.job_id, made.postings["jsearch"]

    # the first recompute: revision 1, its lineage (one row per llm_input field), one occurrence
    first = await recompute(engine, job)
    assert (first.revision_created, first.current_changed, first.det_facts_changed) == (True, True, True)
    assert (first.tainted_reuse_refused, first.conflicts) == (False, ())
    [rev1] = await fetch(engine, REVISIONS, job=job)
    assert (rev1.id, rev1.revision_no, rev1.embedding_status) == (first.revision_id, 1, "awaiting_embedding")
    lineage = await fetch(engine, LINEAGE, revision=rev1.id)
    assert [row.field for row in lineage] == list(LLM_INPUT_FIELDS)
    assert {(row.source_id, row.posting_id, row.observation_id) for row in lineage} == {
        ("jsearch", posting, made.observations["jsearch"])
    }
    row = await job_row(engine, job)
    assert (row.current_revision_id, row.title, row.source_ids) == (
        rev1.id,
        "Python Developer",
        ["adzuna", "jsearch"],
    )
    # no salary is SQL NULL, not the JSON `null` a bare JSONB parameter would store
    assert (await fetch(engine, sa.text("SELECT salary IS NULL AS none FROM jobs WHERE id = :j"), j=job))[
        0
    ].none
    assert [o.job_revision_id for o in await fetch(engine, OCCURRENCES, job=job)] == [rev1.id]

    # nothing changed: the same revision, no occurrence
    again = await recompute(engine, job)
    assert (again.revision_id, again.revision_created, again.current_changed, again.det_facts_changed) == (
        rev1.id,
        False,
        False,
        False,
    )

    # the title changes: revision 2 and an occurrence
    async with engine.begin() as conn:
        await add_observation(conn, posting, observed_at=utcnow(), title_raw="Senior Python Developer")
    changed = await recompute(engine, job)
    assert (changed.revision_created, changed.current_changed, changed.det_facts_changed) == (
        True,
        True,
        True,
    )
    revs = await fetch(engine, REVISIONS, job=job)
    assert [(r.id, r.revision_no) for r in revs] == [(rev1.id, 1), (changed.revision_id, 2)]
    assert len(await fetch(engine, LINEAGE, revision=changed.revision_id)) == len(LLM_INPUT_FIELDS)

    # the title back: revision 1 again, reused, and a third occurrence
    async with engine.begin() as conn:
        await add_observation(conn, posting, observed_at=utcnow(), title_raw="Python Developer")
    back = await recompute(engine, job)
    assert (back.revision_id, back.revision_created, back.current_changed) == (rev1.id, False, True)
    assert (await job_row(engine, job)).current_revision_id == rev1.id
    assert len(await fetch(engine, REVISIONS, job=job)) == 2
    occurrences = [o.job_revision_id for o in await fetch(engine, OCCURRENCES, job=job)]
    assert occurrences == [rev1.id, changed.revision_id, rev1.id]
    # the ranking generation advanced once per current change
    assert await generation(engine) == 3


async def test_fenced_lineage_is_refused_until_the_purge_removes_it(
    engine: AsyncEngine, current_epoch: int, job_with_postings: JobWithPostings
) -> None:
    # the ad was first seen on Adzuna (revision 1, every field from Adzuna), then the same ad on JSearch
    made = await job_with_postings({"adzuna": {}})
    job = made.job_id
    first = await recompute(engine, job)
    [rev1] = await fetch(engine, REVISIONS, job=job)
    assert {row.source_id for row in await fetch(engine, LINEAGE, revision=rev1.id)} == {"adzuna"}
    async with engine.begin() as conn:
        jsearch_posting, _ = await add_posting(conn, job, "jsearch", observed_at=utcnow())
    reused = await recompute(engine, job)
    assert (reused.revision_id, reused.revision_created, reused.current_changed) == (rev1.id, False, False)

    # with Adzuna fenced the projection (now from JSearch alone) has revision 1's hash: no reuse, no twin
    refused = await recompute(engine, job, fenced=frozenset({"adzuna"}))
    assert (refused.tainted_reuse_refused, refused.revision_created, refused.current_changed) == (
        True,
        False,
        False,
    )
    assert refused.revision_id == first.revision_id == rev1.id
    row = await job_row(engine, job)
    assert (row.current_revision_id, row.source_ids) == (rev1.id, ["jsearch"])
    assert row.field_provenance["title"]["posting_id"] == jsearch_posting
    assert len(await fetch(engine, REVISIONS, job=job)) == 1

    # no current revision while the tainted one still exists is a broken purge: refused loudly (rolled back)
    async with engine.connect() as conn:
        await conn.execute(
            sa.text("UPDATE jobs SET current_revision_id = NULL WHERE id = :job"), {"job": job}
        )
        with pytest.raises(InvariantViolation):
            await recompute_in(conn, job, fenced=frozenset({"adzuna"}))

    # the M5 purge: no current revision, the tainted one deleted, then the same recompute creates it anew
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE jobs SET current_revision_id = NULL WHERE id = :job"), {"job": job}
        )
        await conn.execute(sa.text("DELETE FROM job_revisions WHERE id = :id"), {"id": rev1.id})
        rebuilt = await recompute_in(conn, job, fenced=frozenset({"adzuna"}))
    assert (rebuilt.revision_created, rebuilt.current_changed, rebuilt.tainted_reuse_refused) == (
        True,
        True,
        False,
    )
    [rev2] = await fetch(engine, REVISIONS, job=job)
    # a new row with the purged one's material hash; `revision_no` is max + 1 over the job's revisions
    assert (rev2.id, rev2.material_hash, rev2.revision_no) == (rebuilt.revision_id, rev1.material_hash, 1)
    assert rev2.id != rev1.id
    assert (await job_row(engine, job)).current_revision_id == rev2.id
    assert {row.source_id for row in await fetch(engine, LINEAGE, revision=rev2.id)} == {"jsearch"}
    assert [o.job_revision_id for o in await fetch(engine, OCCURRENCES, job=job)] == [rev2.id]


async def test_the_det_facts_round_trip_through_the_jobs_columns(  # carry 2
    engine: AsyncEngine, current_epoch: int, job_with_postings: JobWithPostings
) -> None:
    sighted = utcnow() - timedelta(hours=1)
    made = await job_with_postings(
        {
            "greenhouse": {
                "title_raw": "Senior Backend Engineer (m/w/d)",
                "locations": [BERLIN],
                "location_kind": "city",
                "source_structured": {"language": "de"},
                "posted_at": sighted - timedelta(days=2, microseconds=123),
                "posted_precision": "exact",
            },
            "lever": {
                "locations": [HAMBURG],
                "location_kind": "city",
                "work_mode_possible": frozenset({"hybrid", "onsite"}),
                "work_mode_source": "structured",
                "remote_countries": ("DE",),
                "employment_types": ("full_time",),
                "employment_source": "structured",
                "salary": stated(60000.5, 80000),
            },
            "smartrecruiters": {
                "source_structured": {"seniority": "mid_senior_level", "industry": "Internet"}
            },
            "adzuna": {"salary": predicted(70000, 90000), "company_raw": "Acme Holding"},
        },
        observed_at=sighted,
        watchlist_display="Acme Software GmbH",
    )
    result = await recompute(engine, made.job_id)
    assert result.det_facts_changed
    async with engine.connect() as conn:
        facts, description = await load_facts(conn, made.job_id)
    row = await job_row(engine, made.job_id)
    assert det_facts_hash(facts) == bytes(row.det_facts_hash)
    [rev] = await fetch(engine, REVISIONS, job=made.job_id)
    assert description == rev.description_text
    # the columns the selection wrote
    assert (row.company_display, row.company_key) == ("Acme Software GmbH", company_key("Acme Software GmbH"))
    assert (row.title, row.title_norm, row.seniority_tokens) == (
        "Senior Backend Engineer",
        "senior backend engineer",
        ["senior"],
    )
    assert ([location["label"] for location in row.locations], row.location_kind) == (
        ["Berlin", "Hamburg"],
        "multi",
    )
    assert (row.work_mode_possible, row.work_mode_source) == (["hybrid", "onsite"], "structured")
    assert (row.remote_countries, row.employment_types, row.employment_source) == (
        ["DE"],
        ["full_time"],
        "structured",
    )
    assert (row.salary["kind"], row.salary["min"]) == ("stated", 60000.5)
    assert (row.structured_seniority, row.industry_structured, row.ad_language) == (
        "mid_senior_level",
        "Internet",
        "de",
    )
    assert (row.posted_at_effective, row.posted_precision) == (
        sighted - timedelta(days=2, microseconds=123),
        "exact",
    )
    assert row.source_ids == ["adzuna", "greenhouse", "lever", "smartrecruiters"]
    seniority = row.field_provenance["structured_seniority"]
    assert (seniority["posting_id"], seniority["observation_id"], seniority["rule"]) == (
        made.postings["smartrecruiters"],
        made.observations["smartrecruiters"],
        "structured",
    )
    assert datetime.fromisoformat(seniority["observed_at"]) == sighted
    # every DetFacts field as the selection made it
    assert (facts.structured_seniority, facts.industry_structured, facts.location_kind) == (
        "mid_senior_level",
        "Internet",
        "multi",
    )
    assert facts.salary is not None and facts.salary.kind == "stated"
    # the union of the ATS locations names both boards in the lineage
    lineage = await fetch(engine, LINEAGE, revision=rev.id)
    assert {r.source_id for r in lineage if r.field == "locations"} == {"greenhouse", "lever"}
    # an unchanged recompute leaves the hash and the revision alone
    again = await recompute(engine, made.job_id)
    assert (again.det_facts_changed, again.revision_created, again.current_changed) == (False, False, False)
    assert bytes((await job_row(engine, made.job_id)).det_facts_hash) == bytes(row.det_facts_hash)


async def test_a_work_mode_conflict_raises_an_info_alert(
    engine: AsyncEngine, current_epoch: int, job_with_postings: JobWithPostings
) -> None:
    made = await job_with_postings(
        {
            "lever": {"work_mode_possible": frozenset({"remote"}), "work_mode_source": "structured"},
            "ashby": {"work_mode_possible": frozenset({"onsite"}), "work_mode_source": "structured"},
        }
    )
    result = await recompute(engine, made.job_id)
    assert result.conflicts == ("work_mode_possible",)
    assert (await job_row(engine, made.job_id)).work_mode_possible == ["remote"]  # the lowest posting id
    key = f"dedup.field_conflict:job:{made.job_id}:work_mode_possible"
    [alert] = await fetch(engine, ALERT, key=key)
    assert (alert.severity, alert.occurrences, alert.resolved_at) == ("info", 1, None)
    await recompute(engine, made.job_id)
    [alert] = await fetch(engine, ALERT, key=key)
    assert alert.occurrences == 2


async def test_the_postings_of_a_job_merged_into_it_count(
    engine: AsyncEngine, current_epoch: int, job_with_postings: JobWithPostings
) -> None:
    into = await job_with_postings({"jsearch": {}})
    merged = await job_with_postings({"lever": {"title_raw": "Platform Engineer"}})
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE jobs SET merged_into_job_id = :into WHERE id = :job"),
            {"into": into.job_id, "job": merged.job_id},
        )
    await recompute(engine, into.job_id)
    row = await job_row(engine, into.job_id)
    assert (row.source_ids, row.title) == (["jsearch", "lever"], "Platform Engineer")
    assert row.field_provenance["title"]["posting_id"] == merged.postings["lever"]


async def test_the_timestamps_are_the_database_clock(  # carry 3
    engine: AsyncEngine, current_epoch: int, job_with_postings: JobWithPostings
) -> None:
    made = await job_with_postings({"jsearch": {}})
    async with engine.begin() as conn:
        db_now = (await conn.execute(sa.text("SELECT now()"))).scalar_one()
        await recompute_in(conn, made.job_id)
    assert (await job_row(engine, made.job_id)).updated_at == db_now
    [occurrence] = await fetch(engine, OCCURRENCES, job=made.job_id)
    assert occurrence.became_current_at == db_now != CALLER_NOW


async def test_two_current_changes_in_one_transaction_record_the_last(
    engine: AsyncEngine, current_epoch: int, job_with_postings: JobWithPostings
) -> None:
    made = await job_with_postings({"jsearch": {}})
    first = await recompute(engine, made.job_id)
    async with engine.begin() as conn:
        await add_observation(conn, made.postings["jsearch"], observed_at=utcnow(), title_raw="Data Engineer")
        changed = await recompute_in(conn, made.job_id)
        await add_observation(conn, made.postings["jsearch"], observed_at=utcnow(), title_raw="ML Engineer")
        last = await recompute_in(conn, made.job_id)
    assert changed.current_changed and last.current_changed and last.revision_id != changed.revision_id
    occurrences = [o.job_revision_id for o in await fetch(engine, OCCURRENCES, job=made.job_id)]
    assert occurrences == [first.revision_id, last.revision_id]


async def test_recompute_refuses_a_job_without_an_observation(
    engine: AsyncEngine, current_epoch: int, job_with_postings: JobWithPostings
) -> None:
    made = await job_with_postings({"adzuna": {}})
    async with engine.connect() as conn:
        with pytest.raises(LookupError, match="no observation"):
            await recompute_in(conn, made.job_id, fenced=frozenset({"adzuna"}))
    async with engine.connect() as conn:
        with pytest.raises(LookupError, match="does not exist"):
            await revisions.recompute_job(conn, made.job_id + 1000, now=CALLER_NOW)


async def test_the_ranking_bump_counts_once_and_needs_its_row(engine: AsyncEngine) -> None:
    before = await generation(engine)
    async with engine.begin() as conn:
        await ranking.bump(conn)
    assert await generation(engine) == before + 1
    async with engine.connect() as conn:  # rolled back on close
        await conn.execute(sa.text("DELETE FROM ranking_state"))
        with pytest.raises(LookupError):
            await ranking.bump(conn)
    assert await generation(engine) == before + 1
