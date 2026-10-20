import asyncio
import hashlib
from datetime import timedelta
from functools import partial
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.seed import content_hash, scope_payload
from jobhunter.db.sql import LOCK_NOT_AVAILABLE, pg_error, q
from jobhunter.errors import FencedOut
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
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.collect import ctx, make_deps, queued_run, supersede
from tests.support.facts import BERLIN, HAMBURG, facts, load_facts, scope
from tests.support.procs import await_until
from tests.support.quota import seeded_payload
from tests.support.reconcile import assert_fence_rule

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


async def waiting_for_a_lock(engine: AsyncEngine) -> bool:
    """Some session of the database waits for a lock."""
    async with engine.connect() as conn:
        return bool((await conn.execute(WAITING)).scalar_one())


async def allow(conn: AsyncConnection) -> None:
    """A guard that always lets the work through."""


async def active_scope_id(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(ACTIVE_SCOPE)).scalar_one())


async def row(engine: AsyncEngine, job: int, scope_id: int) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(ROW, {"job": job, "scope": scope_id})).first()


async def job_hash(engine: AsyncEngine, job: int) -> bytes:
    async with engine.connect() as conn:
        return bytes((await conn.execute(JOB_HASH, {"job": job})).scalar_one())


async def add_scope_version(engine: AsyncEngine, **changes: Any) -> int:
    """A scope version: the seeded scope with `changes`, not active."""
    payload = {**scope_payload(seeded_payload()), **changes}
    async with engine.begin() as conn:
        return int(
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


async def add_pending_scope(engine: AsyncEngine, **changes: Any) -> int:
    """A scope version (the seeded scope with `changes`) as the active configuration's pending scope."""
    pending = await add_scope_version(engine, **changes)
    async with engine.begin() as conn:
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


async def set_description(engine: AsyncEngine, job: int, text: str) -> None:
    """The job's current revision, with `text` as its `description_text`."""
    async with engine.begin() as conn:
        revision = (
            await conn.execute(
                sa.text(
                    "INSERT INTO job_revisions (job_id, revision_no, material_hash, llm_input, "
                    "description_text, description_completeness, ad_language, embedding_status) "
                    "VALUES (:job, 1, :hash, '{}', :text, 'full', 'de', 'awaiting_embedding') RETURNING id"
                ),
                {"job": job, "hash": H2, "text": text},
            )
        ).scalar_one()
        await conn.execute(
            sa.text("UPDATE jobs SET current_revision_id = :rev WHERE id = :job"),
            {"rev": revision, "job": job},
        )


async def test_load_facts_reads_every_det_fact_from_a_jobs_column_and_the_current_revision(
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
                "UPDATE jobs SET company_key = 'initech', title_norm = 'senior python developer', "
                "seniority_tokens = ARRAY['senior'], locations = :locations, location_kind = 'multi', "
                "work_mode_possible = ARRAY['hybrid','onsite'], work_mode_source = 'text', "
                "remote_countries = ARRAY['DE'], employment_types = ARRAY['full_time'], "
                "employment_source = 'structured', salary = :salary, industry_structured = 'software', "
                "structured_seniority = 'mid_senior_level', "
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
    async with engine.connect() as conn:
        loaded, description = await load_facts(conn, made.job_id)
    assert description == ""  # no current revision
    assert loaded == DetFacts(
        company_key="initech",
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
    # No fact is left at its unknown default: every one of them came from a column of the job.
    unknown = facts()
    assert [
        name for name in DetFacts.__dataclass_fields__ if getattr(loaded, name) == getattr(unknown, name)
    ] == []

    await set_description(engine, made.job_id, "Wir nutzen SAP.")
    async with engine.connect() as conn:
        assert (await load_facts(conn, made.job_id))[1] == "Wir nutzen SAP."
    with pytest.raises(LookupError):
        async with engine.connect() as conn:
            await load_facts(conn, made.job_id + 1000)


async def test_a_job_without_a_structured_seniority_loads_none(engine, current_epoch, job_factory) -> None:
    plain = await job_factory(native_id="a")
    tagged = await job_factory(native_id="b", structured_seniority="entry-level")
    async with engine.connect() as conn:
        assert (await load_facts(conn, plain.job_id))[0].structured_seniority is None
        assert (await load_facts(conn, tagged.job_id))[0].structured_seniority == "entry-level"


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

    async with engine.connect() as identity:
        await identity.execute(SET_JOB, {"language": "other", "hash": H2, "job": job})  # uncommitted
        task = asyncio.create_task(refilter())
        await await_until(partial(waiting_for_a_lock, engine), 10)
        await identity.commit()
    assert await task == {active: HardFilterResult(False, ("ad_language_excluded",))}
    stored = await row(engine, job, active)
    assert (stored.passed, bytes(stored.inputs_hash)) == (False, H2)


async def test_refilter_job_uses_the_scopes_it_is_given(engine, current_epoch, job_factory) -> None:
    made = await job_factory(native_id="a", structured_seniority="entry_level")
    active = await active_scope_id(engine)
    explicit = scope(seniority_range=SeniorityRange("explicit", 0, 0, "senior", "senior"))
    async with engine.begin() as conn:
        assert await filters.refilter_job(conn, made.job_id, {active: explicit}) == {
            active: HardFilterResult(False, ("seniority_out_of_range",))
        }
    with pytest.raises(LookupError):
        async with engine.begin() as conn:
            await filters.refilter_job(conn, made.job_id + 1000, {active: explicit})


async def test_the_description_is_normalised_once_for_all_scopes(
    engine, current_epoch, job_factory, monkeypatch
) -> None:
    job = (await job_factory(native_id="a")).job_id
    text = "Wir suchen Front-End Entwickler mit SAP-Erfahrung."
    await set_description(engine, job, text)
    active = await active_scope_id(engine)
    sap = await add_scope_version(engine, excluded_keywords=["sap"])
    php = await add_scope_version(engine, excluded_keywords=["php"])
    calls: list[str] = []
    real = filters._comparison_form

    def counting(value: str) -> str:
        calls.append(value)
        return real(value)

    monkeypatch.setattr(filters, "_comparison_form", counting)

    def reading(*keywords: str) -> ScopeSettings:
        return scope(excluded_keywords=keywords, exclude_keywords_in_description=True)

    async with engine.begin() as conn:
        results = await filters.refilter_job(
            conn, job, {active: reading("front-end"), sap: reading("sap"), php: reading("php")}
        )
    assert {scope_id: result.reasons for scope_id, result in results.items()} == {
        active: ("excluded_keyword",),
        sap: ("excluded_keyword",),
        php: (),
    }
    assert calls.count(text) == 1  # three scopes read the description, one normalisation of it
    calls.clear()
    async with engine.begin() as conn:
        await filters.refilter_job(conn, job, {active: scope(excluded_keywords=("sap",)), php: scope()})
    assert text not in calls  # no scope reads the description: it is never normalised


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
        loaded, description = await load_facts(conn, job)
    assert loaded == facts() and description == ""
    assert filters.evaluate(loaded, description, scope()).passed


async def test_the_batch_is_locked_before_the_guard_and_a_refused_guard_writes_nothing(
    engine, current_epoch, job_factory
) -> None:
    job = (await job_factory(native_id="a")).job_id
    active = await active_scope_id(engine)
    held: list[str | None] = []

    async def probe(conn: AsyncConnection) -> None:
        async with engine.connect() as other:  # the batch's job rows are already held `FOR SHARE`
            with pytest.raises(sa.exc.DBAPIError) as refused:
                await other.execute(
                    sa.text("SELECT 1 FROM jobs WHERE id = :job FOR UPDATE NOWAIT"), {"job": job}
                )
            held.append(pg_error(refused.value).sqlstate)

    async def refuse(conn: AsyncConnection) -> None:
        raise FencedOut("run")

    async with engine.begin() as conn:
        assert await filters.recompute_stale(conn, await filters.active_scopes(conn), guard=probe) == 1
    assert held == [LOCK_NOT_AVAILABLE]
    async with engine.begin() as conn:
        await conn.execute(sa.text("DELETE FROM hard_filter_results"))
    with pytest.raises(FencedOut):
        async with engine.begin() as conn:
            await filters.recompute_stale(conn, await filters.active_scopes(conn), guard=refuse)
    assert await row(engine, job, active) is None


@pytest.mark.parametrize("change", ["deleted", "merged"])
async def test_a_job_deleted_or_merged_between_the_scan_and_the_lock_is_skipped(
    engine, current_epoch, job_factory, change
) -> None:
    gone = await job_factory(native_id="gone")  # the lower id: the batch waits for it first
    kept = await job_factory(native_id="kept")
    active = await active_scope_id(engine)

    async def recompute() -> int:
        async with engine.begin() as conn:
            return await filters.recompute_stale(conn, await filters.active_scopes(conn), guard=allow)

    async with engine.connect() as other:
        if change == "deleted":
            await other.execute(sa.text("DELETE FROM postings WHERE job_id = :job"), {"job": gone.job_id})
            await other.execute(sa.text("DELETE FROM jobs WHERE id = :job"), {"job": gone.job_id})
        else:
            await other.execute(
                sa.text("UPDATE jobs SET merged_into_job_id = :into WHERE id = :job"),
                {"into": kept.job_id, "job": gone.job_id},
            )
        task = asyncio.create_task(recompute())
        await await_until(partial(waiting_for_a_lock, engine), 10)  # the batch waits for the row
        await other.commit()
    assert await task == 1  # the other job; the gone one is skipped, not raised
    assert (await row(engine, kept.job_id, active)).passed is True
    assert await row(engine, gone.job_id, active) is None


async def test_the_reconciler_of_a_superseded_worker_epoch_writes_nothing(
    engine, current_epoch, queue_app, job_factory
) -> None:
    job = (await job_factory(native_id="a")).job_id
    active = await active_scope_id(engine)
    reconcile = r.REGISTRY["hard_filter_completeness"][1]
    # its own fence propagates, and the cadence stops at the reconciler (Ruling FR-w)
    await assert_fence_rule(
        "hard_filter_completeness",
        "heavy",
        ctx(engine, queue_app, epoch=current_epoch + 1),
        ctx(engine, queue_app),
        filters,
    )
    assert await row(engine, job, active) is None
    assert await reconcile(ctx(engine, queue_app, epoch=current_epoch)) == 1
    assert (await row(engine, job, active)).passed is True


async def test_node_8_of_a_superseded_attempt_is_fenced_out_and_writes_nothing(
    engine, current_epoch, queue_app, job_factory
) -> None:
    owner = await queued_run(engine, include_paid=False)
    job = (await job_factory(native_id="a")).job_id  # touched after the run started
    newer = await supersede(engine, owner)
    node = make_hard_filters(make_deps(engine, no_adapter))
    active = await active_scope_id(engine)
    with pytest.raises(FencedOut):
        await node(initial_state(owner.run_id, owner.attempt_no))
    assert await row(engine, job, active) is None
    update = await node(initial_state(newer.run_id, newer.attempt_no))  # the attempt that owns the run
    assert update["counters"] == {"hard_filter_backfilled": 1}
    assert (await row(engine, job, active)).passed is True
