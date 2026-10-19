import asyncio
import hashlib
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.seed import content_hash, scope_payload
from jobhunter.db.sql import LOCK_NOT_AVAILABLE, pg_error, q
from jobhunter.graphs.collect.nodes.hard_filters import make_hard_filters
from jobhunter.graphs.collect.state import initial_state
from jobhunter.run_settings import ScopeSettings, SeniorityRange
from jobhunter.scoring import filters
from jobhunter.scoring.filters import DetFacts, HardFilterResult
from jobhunter.scoring.versioning import DET_ALGORITHM_VERSION
from jobhunter.sources.base import SourceAdapter
from jobhunter.sources.instances import InstanceRow
from jobhunter.worker import main as worker_main
from jobhunter.worker import reconcilers as r
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import ctx, make_deps, queued_run, seed_inputs
from tests.support.facts import BERLIN, HAMBURG, facts, scope
from tests.support.procs import await_until
from tests.support.quota import seeded_payload

pytestmark = pytest.mark.integration

ROW = sa.text(
    "SELECT passed, reasons, inputs_hash, det_algorithm_version, computed_at FROM hard_filter_results "
    "WHERE job_id = :job AND scope_version_id = :scope"
)
ROWS = sa.text(
    "SELECT job_id, scope_version_id, passed, reasons, inputs_hash, det_algorithm_version "
    "FROM hard_filter_results ORDER BY job_id, scope_version_id"
)
ACTIVE_SCOPE = sa.text("SELECT scope_version_id FROM active_config WHERE id = 1")
JOB_HASH = sa.text("SELECT det_facts_hash FROM jobs WHERE id = :job")
SET_JOB = sa.text("UPDATE jobs SET ad_language = :language, det_facts_hash = :hash WHERE id = :job")
SET_UPDATED = sa.text("UPDATE jobs SET updated_at = :at WHERE id = :job")
STARTED_AT = sa.text("SELECT started_at FROM runs WHERE id = :run")
WAITING = sa.text(
    "SELECT count(*) > 0 FROM pg_stat_activity "
    "WHERE wait_event_type = 'Lock' AND datname = current_database()"
)
H2 = hashlib.sha256(b"det facts changed").digest()


async def active_scope_id(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(ACTIVE_SCOPE)).scalar_one())


async def row(engine: AsyncEngine, job: int, scope_id: int) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(ROW, {"job": job, "scope": scope_id})).first()


async def job_hash(engine: AsyncEngine, job: int) -> bytes:
    async with engine.connect() as conn:
        return bytes((await conn.execute(JOB_HASH, {"job": job})).scalar_one())


async def add_pending_scope(engine: AsyncEngine, **changes: Any) -> int:
    """A scope version (the seeded scope with `changes`) as the active configuration's pending scope."""
    payload = {**scope_payload(seeded_payload()), **changes}
    async with engine.begin() as conn:
        pending = int(
            (
                await conn.execute(
                    q(
                        "INSERT INTO scope_versions (scope_hash, payload) "
                        "VALUES (:hash, :payload) RETURNING id",
                        jsonb=["payload"],
                    ),
                    {"hash": content_hash(payload), "payload": payload},
                )
            ).scalar_one()
        )
        await conn.execute(sa.text("UPDATE active_config SET pending_scope_version_id = :p"), {"p": pending})
    return pending


async def test_write_recompute_and_reconciler(engine, current_epoch, queue_app, job_factory) -> None:
    job = (await job_factory(native_id="a")).job_id
    scope_id = await active_scope_id(engine)
    h1 = await job_hash(engine, job)
    async with engine.begin() as conn:
        await filters.write_result(conn, job, scope_id, HardFilterResult(True, ()), inputs_hash=h1)
    stored = await row(engine, job, scope_id)
    assert (stored.passed, stored.reasons, bytes(stored.inputs_hash), stored.det_algorithm_version) == (
        True,
        [],
        h1,
        "1",
    )

    # The job's det facts change (its hash moves): the reconciler recomputes the row from the facts now.
    async with engine.begin() as conn:
        await conn.execute(SET_JOB, {"language": "other", "hash": H2, "job": job})
    reconcile = r.REGISTRY["hard_filter_completeness"][1]
    assert await reconcile(ctx(engine, queue_app)) == 1
    recomputed = await row(engine, job, scope_id)
    assert (recomputed.passed, recomputed.reasons, bytes(recomputed.inputs_hash)) == (
        False,
        ["ad_language_excluded"],
        H2,
    )
    assert recomputed.computed_at >= stored.computed_at
    assert await reconcile(ctx(engine, queue_app)) == 0  # nothing is stale now

    # A row of another algorithm version is recomputed, whatever its hash.
    async with engine.begin() as conn:
        await conn.execute(sa.text("UPDATE hard_filter_results SET det_algorithm_version = '0'"))
    assert await reconcile(ctx(engine, queue_app)) == 1
    assert (await row(engine, job, scope_id)).det_algorithm_version == DET_ALGORITHM_VERSION
    assert await reconcile(ctx(engine, queue_app)) == 0

    # A job merged away is skipped, a job without a row is computed.
    merged = (await job_factory(native_id="merged")).job_id
    fresh = (await job_factory(native_id="fresh")).job_id
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE jobs SET merged_into_job_id = :into WHERE id = :job"),
            {"into": job, "job": merged},
        )
    assert await reconcile(ctx(engine, queue_app)) == 1
    assert await row(engine, merged, scope_id) is None
    assert (await row(engine, fresh, scope_id)).passed is True
    assert await reconcile(ctx(engine, queue_app)) == 0


async def test_write_result_upserts_on_the_primary_key(engine, current_epoch, job_factory) -> None:
    job = (await job_factory(native_id="a")).job_id
    scope_id = await active_scope_id(engine)
    inputs = hashlib.sha256(b"inputs").digest()
    failed = HardFilterResult(False, ("outside_germany", "excluded_keyword"))
    async with engine.begin() as conn:
        await filters.write_result(conn, job, scope_id, HardFilterResult(True, ()), inputs_hash=inputs)
    async with engine.begin() as conn:
        await filters.write_result(conn, job, scope_id, failed, inputs_hash=H2)
    async with engine.connect() as conn:
        rows = (await conn.execute(ROWS)).all()
    assert [(x.job_id, x.scope_version_id, x.passed, x.reasons, bytes(x.inputs_hash)) for x in rows] == [
        (job, scope_id, False, ["outside_germany", "excluded_keyword"], H2)
    ]


async def test_the_database_refuses_a_result_whose_passed_and_reasons_disagree(
    engine, current_epoch, job_factory
) -> None:
    job = (await job_factory(native_id="a")).job_id
    scope_id = await active_scope_id(engine)
    with pytest.raises(sa.exc.IntegrityError):
        async with engine.begin() as conn:
            bad = HardFilterResult(True, ("outside_germany",))
            await filters.write_result(conn, job, scope_id, bad, inputs_hash=H2)


async def test_active_scopes_reads_the_active_and_the_pending_scope_under_share(
    engine, current_epoch
) -> None:
    active = await active_scope_id(engine)
    async with engine.begin() as conn:
        scopes = await filters.active_scopes(conn)
    assert list(scopes) == [active]
    seeded = scopes[active]
    assert isinstance(seeded, ScopeSettings) and seeded.max_ad_age_days is None  # a `scope_versions` payload
    assert seeded.remote_within_germany and seeded.seniority_range.mode == "profile_relative"

    pending = await add_pending_scope(engine, excluded_keywords=["python"], remote_within_germany=False)
    async with engine.begin() as conn:
        scopes = await filters.active_scopes(conn)
        assert sorted(scopes) == sorted([active, pending])
        assert scopes[pending].excluded_keywords == ("python",) and not scopes[pending].remote_within_germany
        # `active_config` is held `FOR SHARE` until the transaction ends: a writer cannot take it meanwhile.
        async with engine.connect() as other:
            with pytest.raises(sa.exc.DBAPIError) as refused:
                await other.execute(sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE NOWAIT"))
            assert pg_error(refused.value).sqlstate == LOCK_NOT_AVAILABLE
            await other.rollback()
            await other.execute(
                sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR SHARE NOWAIT")
            )  # a reader can


async def test_load_facts_reads_the_job_its_current_revision_and_the_structured_seniority(
    engine, current_epoch, job_factory
) -> None:
    made = await job_factory(native_id="a")
    posted = utcnow() - timedelta(days=3)
    salary = {
        "min": 60000,
        "max": 80000,
        "currency": "EUR",
        "period": "year",
        "kind": "stated",
        "raw": "60-80k",
        "annual_eur_min": 60000,
        "annual_eur_max": 80000,
    }
    async with engine.begin() as conn:
        await conn.execute(
            q(
                "UPDATE jobs SET company_key = 'acme', title_norm = 'senior python developer', "
                "seniority_tokens = ARRAY['senior'], locations = :locations, location_kind = 'multi', "
                "work_mode_possible = ARRAY['hybrid','onsite'], work_mode_source = 'text', "
                "remote_countries = ARRAY['DE'], employment_types = ARRAY['full_time'], "
                "employment_source = 'structured', salary = :salary, industry_structured = 'software', "
                "ad_language = 'de', posted_precision = 'exact', posted_at_effective = :posted "
                "WHERE id = :job",
                jsonb=["locations", "salary"],
            ),
            {
                "locations": [BERLIN.model_dump(mode="json"), HAMBURG.model_dump(mode="json")],
                "salary": salary,
                "posted": posted,
                "job": made.job_id,
            },
        )
        await conn.execute(
            q("UPDATE posting_observations SET normalized = :n WHERE id = :o", jsonb=["n"]),
            {"n": {"source_structured": {"seniority": "mid_senior_level"}}, "o": made.observation_ids[0]},
        )
    async with engine.connect() as conn:
        loaded, description = await filters.load_facts(conn, made.job_id)
    assert description == ""  # no current revision
    assert loaded == DetFacts(
        company_key="acme",
        title_norm="senior python developer",
        seniority_tokens=frozenset({"senior"}),
        structured_seniority="mid_senior_level",
        locations=(BERLIN, HAMBURG),
        location_kind="multi",
        work_mode_possible=frozenset({"hybrid", "onsite"}),
        work_mode_source="text",
        remote_countries=("DE",),
        employment_types=frozenset({"full_time"}),
        employment_source="structured",
        salary=loaded.salary,
        industry_structured="software",
        ad_language="de",
        posted_at_effective=posted,
    )
    assert loaded.salary is not None and loaded.salary.model_dump(mode="json") == salary

    async with engine.begin() as conn:
        revision = (
            await conn.execute(
                sa.text(
                    "INSERT INTO job_revisions (job_id, revision_no, material_hash, llm_input, "
                    "description_text, description_completeness, ad_language, embedding_status) "
                    "VALUES (:job, 1, :hash, '{}', "
                    "'Wir nutzen SAP.', 'full', 'de', 'awaiting_embedding') RETURNING id"
                ),
                {"job": made.job_id, "hash": H2},
            )
        ).scalar_one()
        await conn.execute(
            sa.text("UPDATE jobs SET current_revision_id = :rev WHERE id = :job"),
            {"rev": revision, "job": made.job_id},
        )
    async with engine.connect() as conn:
        assert (await filters.load_facts(conn, made.job_id))[1] == "Wir nutzen SAP."
    with pytest.raises(LookupError):
        async with engine.connect() as conn:
            await filters.load_facts(conn, made.job_id + 1000)


async def test_the_structured_seniority_is_the_best_ranked_latest_observation(
    engine, current_epoch, job_factory
) -> None:
    """§8.8 precedence: the best source rank (an ATS before JSearch), then the most recent `observed_at`."""
    await seed_inputs(
        engine, companies=(("greenhouse", "acme-gh"), ("smartrecruiters", "acme-sr")), enabled=("ats",)
    )
    now = utcnow()
    first = await job_factory(
        native_id="a",
        source_id="greenhouse",
        instance_key="acme-gh",
        observations=[(now - timedelta(hours=2), None)],
    )
    second = await job_factory(
        native_id="b",
        source_id="smartrecruiters",
        instance_key="acme-sr",
        observations=[(now - timedelta(hours=1), None)],
    )
    third = await job_factory(native_id="c", source_id="jsearch", observations=[(now, None)])
    levels = {first.observation_ids[0]: "entry-level", second.observation_ids[0]: "director"}
    levels[third.observation_ids[0]] = "senior"
    async with engine.begin() as conn:
        for other in (second, third):  # one job with the three postings
            await conn.execute(
                sa.text("UPDATE postings SET job_id = :job WHERE id = :posting"),
                {"job": first.job_id, "posting": other.posting_id},
            )
        for observation, level in levels.items():
            await conn.execute(
                q("UPDATE posting_observations SET normalized = :n WHERE id = :o", jsonb=["n"]),
                {"n": {"source_structured": {"seniority": level}}, "o": observation},
            )

    async def structured() -> str | None:
        async with engine.connect() as conn:
            return (await filters.load_facts(conn, first.job_id))[0].structured_seniority

    assert await structured() == "director"  # both ATS (rank 1): the later observation; JSearch is rank 2
    for observation, expected in (
        (second.observation_ids[0], "entry-level"),
        (first.observation_ids[0], "senior"),
    ):
        async with engine.begin() as conn:
            await conn.execute(
                sa.text("UPDATE posting_observations SET normalized = '{}' WHERE id = :o"), {"o": observation}
            )
        assert await structured() == expected
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "UPDATE posting_observations SET normalized = '{\"source_structured\": {}}' WHERE id = :o"
            ),
            {"o": third.observation_ids[0]},
        )
    assert await structured() is None


async def test_refilter_job_writes_a_row_per_scope_with_the_stored_hash(
    engine, current_epoch, job_factory
) -> None:
    job = (await job_factory(native_id="a", title="Python Developer")).job_id
    active = await active_scope_id(engine)
    pending = await add_pending_scope(engine, excluded_keywords=["python"])
    async with engine.begin() as conn:
        scopes = await filters.active_scopes(conn)
        results = await filters.refilter_job(conn, job, scopes)
    assert results == {
        active: HardFilterResult(True, ()),
        pending: HardFilterResult(False, ("excluded_keyword",)),
    }
    stored = await job_hash(engine, job)
    async with engine.connect() as conn:
        rows = (await conn.execute(ROWS)).all()
    assert [(x.scope_version_id, x.passed, x.reasons, bytes(x.inputs_hash)) for x in rows] == [
        (active, True, [], stored),
        (pending, False, ["excluded_keyword"], stored),
    ]


async def test_refilter_job_computes_from_the_facts_an_overlapping_transaction_commits(
    engine, current_epoch, job_factory
) -> None:
    """The job row is taken `FOR SHARE` before its facts are read: a refilter that overlaps an identity
    transaction (which holds the row `FOR UPDATE`) waits for it, and writes a result for the facts it
    committed. A plain read would use the old facts and write a row for the old hash."""
    job = (await job_factory(native_id="a")).job_id
    active = await active_scope_id(engine)

    async def refilter() -> dict[int, HardFilterResult]:
        async with engine.begin() as conn:
            return await filters.refilter_job(conn, job, {active: scope()})

    async def waiting_for_a_lock() -> bool:
        async with engine.connect() as conn:
            return bool((await conn.execute(WAITING)).scalar_one())

    async with engine.connect() as identity:
        await identity.execute(SET_JOB, {"language": "other", "hash": H2, "job": job})  # uncommitted
        task = asyncio.create_task(refilter())
        await await_until(waiting_for_a_lock, 10)
        await identity.commit()
    assert await task == {active: HardFilterResult(False, ("ad_language_excluded",))}
    stored = await row(engine, job, active)
    assert (stored.passed, bytes(stored.inputs_hash)) == (False, H2)


async def test_refilter_job_uses_the_scopes_it_is_given(engine, current_epoch, job_factory) -> None:
    made = await job_factory(native_id="a")
    async with engine.begin() as conn:
        await conn.execute(
            q("UPDATE posting_observations SET normalized = :n WHERE id = :o", jsonb=["n"]),
            {"n": {"source_structured": {"seniority": "entry_level"}}, "o": made.observation_ids[0]},
        )
    active = await active_scope_id(engine)
    explicit = scope(seniority_range=SeniorityRange("explicit", 0, 0, "senior", "senior"))
    async with engine.begin() as conn:
        assert await filters.refilter_job(conn, made.job_id, {active: explicit}) == {
            active: HardFilterResult(False, ("seniority_out_of_range",))
        }
    with pytest.raises(LookupError):
        async with engine.begin() as conn:
            await filters.refilter_job(conn, made.job_id + 1000, {active: explicit})


async def test_reconciler_works_through_stale_jobs_in_batches_of_a_pass(
    engine, current_epoch, queue_app, job_factory, monkeypatch
) -> None:
    jobs = [(await job_factory(native_id=name)).job_id for name in "abcde"]
    monkeypatch.setattr(filters, "BATCH", 2)
    reconcile = r.REGISTRY["hard_filter_completeness"][1]
    assert [await reconcile(ctx(engine, queue_app)) for _ in range(4)] == [2, 2, 1, 0]
    scope_id = await active_scope_id(engine)
    assert [(await row(engine, job, scope_id)).passed for job in jobs] == [True] * 5


async def test_the_pending_scope_is_computed_by_the_reconciler_too(
    engine, current_epoch, queue_app, job_factory
) -> None:
    job = (await job_factory(native_id="a", title="Python Developer")).job_id
    active = await active_scope_id(engine)
    reconcile = r.REGISTRY["hard_filter_completeness"][1]
    assert await reconcile(ctx(engine, queue_app)) == 1  # a job that has a row for the active scope only
    pending = await add_pending_scope(engine, excluded_keywords=["python"])
    assert await reconcile(ctx(engine, queue_app)) == 1  # lacks the pending one
    assert (await row(engine, job, active)).passed is True
    assert (await row(engine, job, pending)).reasons == ["excluded_keyword"]
    assert await reconcile(ctx(engine, queue_app)) == 0


async def test_the_reconciler_is_a_heavy_one_and_the_worker_imports_its_module(
    engine, current_epoch, queue_app, job_factory
) -> None:
    assert r.REGISTRY["hard_filter_completeness"][0] == {"heavy"}
    assert "jobhunter.scoring.filters" in worker_main._REGISTERING_MODULES
    job = (await job_factory(native_id="a")).job_id
    counts = await r.run_reconcilers(ctx(engine, queue_app), "heavy")
    assert counts["hard_filter_completeness"] == 1
    assert (await row(engine, job, await active_scope_id(engine))) is not None


def no_adapter(instance: InstanceRow) -> SourceAdapter:
    raise AssertionError(f"node 8 needs no adapter, asked for {instance.source_id}")


async def started_at(engine: AsyncEngine, run_id: int) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(STARTED_AT, {"run": run_id})).scalar_one()


async def test_node_8_backfills_the_jobs_the_run_touched(
    engine, current_epoch, job_factory, monkeypatch
) -> None:
    untouched = (await job_factory(native_id="old")).job_id
    owner = await queued_run(engine, include_paid=False)
    begun = await started_at(engine, owner.run_id)
    async with engine.begin() as conn:
        await conn.execute(SET_UPDATED, {"at": begun - timedelta(hours=1), "job": untouched})
    touched = (await job_factory(native_id="new")).job_id
    node = make_hard_filters(make_deps(engine, no_adapter))
    state = initial_state(owner.run_id, owner.attempt_no)
    scope_id = await active_scope_id(engine)

    assert await node(state) == {"counters": {"hard_filter_backfilled": 1}, "steps": ["hard_filters"]}
    assert await row(engine, untouched, scope_id) is None  # not this run's job: the reconciler's
    assert (await row(engine, touched, scope_id)).passed is True
    assert await node(state) == {"counters": {"hard_filter_backfilled": 0}, "steps": ["hard_filters"]}

    # A stale row of a touched job (its facts moved on) is verified and repaired too.
    async with engine.begin() as conn:
        await conn.execute(SET_JOB, {"language": "other", "hash": H2, "job": touched})
    assert (await node(state))["counters"] == {"hard_filter_backfilled": 1}
    repaired = await row(engine, touched, scope_id)
    assert (repaired.passed, repaired.reasons, bytes(repaired.inputs_hash)) == (
        False,
        ["ad_language_excluded"],
        H2,
    )

    # More jobs than a batch: the node keeps going until none is left.
    monkeypatch.setattr(filters, "BATCH", 2)
    more = [(await job_factory(native_id=name)).job_id for name in "abcde"]
    assert (await node(state))["counters"] == {"hard_filter_backfilled": 5}
    assert [(await row(engine, job, scope_id)).passed for job in more] == [True] * 5


async def test_node_8_with_nothing_to_do_counts_zero(engine, current_epoch) -> None:
    owner = await queued_run(engine, include_paid=False)
    node = make_hard_filters(make_deps(engine, no_adapter))
    update = await node(initial_state(owner.run_id, owner.attempt_no))
    assert update == {"counters": {"hard_filter_backfilled": 0}, "steps": ["hard_filters"]}


async def test_the_facts_of_a_job_that_knows_nothing_pass_every_filter(
    engine, current_epoch, job_factory
) -> None:
    """The factory's job has an unknown work mode, language and location: positive evidence only."""
    job = (await job_factory(native_id="a")).job_id
    async with engine.connect() as conn:
        loaded, description = await filters.load_facts(conn, job)
    assert loaded == facts() and description == ""
    assert filters.evaluate(loaded, description, scope()).passed
