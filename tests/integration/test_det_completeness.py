"""The `det_completeness` reconciler on PostgreSQL (spec §7.17 #5, §11.11 L4568, §17.4 L5511; plan M3 Task 18,
Carries T6-d and T15-e).

Every published result of the active tuple gets a current `det_results` instance for the active scope (and the
pending scope while one exists) whose `det_inputs_hash` is that of its job's current det-facts and sources and
whose `det_algorithm_version` is current: a missing, a stale-hash and a stale-version instance are each
recomputed, once; a changed `DET_ALGORITHM_VERSION` recomputes every current instance once. Each chunk is
one transaction in the lock schedule's order: the epoch fence, `active_config FOR SHARE`, the page's job rows
`FOR SHARE` (step 4, carry T6-d), then the results (step 10). The reconciler follows the fence rule.

Carry T15-e: the live det-facts hook writes exactly one new instance when only a field's sources changed (the
facts unchanged), because the inputs' hash covers the sources.

The jobs are `tests.support.m3` jobs whose published results carry the stored columns of a real publication
(`published_result`); the score tuple is the active one. Every datum is invented.
"""

import asyncio
import hashlib
from typing import Any, Final
from uuid import uuid4

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.run_settings import scope_from_payload
from jobhunter.scoring import det_results, filters
from jobhunter.scoring import reconcilers as scoring_reconcilers
from jobhunter.scoring.det_results import det_inputs, job_sources
from jobhunter.scoring.versioning import DET_ALGORITHM_VERSION
from jobhunter.types import JSONValue, get_object
from jobhunter.worker.reconcilers import ReconcileContext
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.collect import instance_id
from tests.support.m3 import Job, Published, insert_job
from tests.support.pg import PgServer, lock_wait_details, relation_locks
from tests.support.procs import await_until
from tests.support.reconcile import assert_fence_rule, run_reconciler
from tests.support.scoring import activate_score_tuple, instances_of, published_result, set_job_facts

pytestmark = pytest.mark.integration

SALARY: Final[dict[str, JSONValue]] = {
    "min": 70000.0,
    "max": 80000.0,
    "currency": "EUR",
    "period": "year",
    "kind": "stated",
    "raw": "70.000-80.000 EUR",
    "annual_eur_min": 70000.0,
    "annual_eur_max": 80000.0,
}
_NEW_SCOPE: Final = sa.text(
    "INSERT INTO scope_versions (scope_hash, payload) SELECT :hash, payload || "
    "jsonb_build_object('industries_exclude', jsonb_build_array('tabak')) FROM scope_versions "
    "WHERE id = (SELECT scope_version_id FROM active_config WHERE id = 1) RETURNING id"
)
_SET_PENDING: Final = sa.text("UPDATE active_config SET pending_scope_version_id = :s WHERE id = 1")
_ACTIVE_SCOPE: Final = sa.text("SELECT scope_version_id FROM active_config WHERE id = 1")
_SCOPE_PAYLOAD: Final = sa.text("SELECT payload FROM scope_versions WHERE id = :s")
_PROVENANCE: Final = sa.text(
    "UPDATE jobs SET field_provenance = jsonb_build_object('salary', jsonb_build_object("
    "'source_instance_id', CAST(:i AS bigint), 'rule', 'first_complete')) WHERE id = :j"
)
_LOCK_JOB: Final = sa.text("SELECT id FROM jobs WHERE id = :j FOR UPDATE")
_ACTIVE_CONFIG_NOWAIT: Final = sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE NOWAIT")
_RESULT_NOWAIT: Final = sa.text("SELECT 1 FROM llm_results WHERE id = :r FOR UPDATE NOWAIT")


@pytest.fixture(autouse=True)
def _task_app(queue_app: procrastinate.App) -> procrastinate.App:
    """The process's task app, which a reconciler's context names (`run_reconciler`)."""
    return queue_app


async def run(engine: AsyncEngine) -> int:
    return await run_reconciler("det_completeness", engine, now=utcnow(), epoch=1)


async def published_jobs(engine: AsyncEngine, n: int) -> list[tuple[Job, Published]]:
    """`n` jobs, each with a published result of the score tuple (made active), without an instance."""
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        made = []
        for _ in range(n):
            job = await insert_job(conn)
            made.append((job, await published_result(conn, job, tuple_id)))
    return made


async def active_scope(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(_ACTIVE_SCOPE)).scalar_one())


async def pending_scope(engine: AsyncEngine) -> int:
    """A scope version of the active scope with one industry excluded (an input of every instance), made the
    pending one; its id."""
    async with engine.begin() as conn:
        digest = hashlib.sha256(uuid4().bytes).digest()
        scope_id = int((await conn.execute(_NEW_SCOPE, {"hash": digest})).scalar_one())
        await conn.execute(_SET_PENDING, {"s": scope_id})
    return scope_id


async def expected_hash(engine: AsyncEngine, job_id: int, scope_version_id: int) -> bytes:
    """The `det_inputs_hash` of the job's current det-facts and sources under the scope."""
    async with engine.connect() as conn:
        payload = (await conn.execute(_SCOPE_PAYLOAD, {"s": scope_version_id})).scalar_one()
        det = await filters.read_det_facts(conn, job_id)
        sources = await job_sources(conn, job_id)
    return det_inputs(det, sources, scope_from_payload(get_object(payload, "")))[1]


async def current(engine: AsyncEngine, result_id: int) -> dict[int, Any]:
    """The result's current instances by scope version."""
    return {row.scope_version_id: row for row in await instances_of(engine, result_id) if row.is_current}


def ctx(engine: AsyncEngine, queue_app: procrastinate.App, *, epoch: int = 1) -> ReconcileContext:
    return ReconcileContext(engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow(), epoch=epoch)


async def test_a_missing_instance_is_computed_once(engine: AsyncEngine, current_epoch: int) -> None:
    ((job, published),) = await published_jobs(engine, 1)
    scope = await active_scope(engine)
    assert await run(engine) == 1
    (row,) = await instances_of(engine, published.result_id)
    assert (row.scope_version_id, row.is_current, row.det_algorithm_version) == (
        scope,
        True,
        DET_ALGORITHM_VERSION,
    )
    assert row.det_inputs_hash == await expected_hash(engine, job.job_id, scope)
    assert await run(engine) == 0  # complete: nothing more to write
    assert len(await instances_of(engine, published.result_id)) == 1


async def test_a_stale_hash_instance_is_recomputed_and_the_old_one_superseded(
    engine: AsyncEngine, current_epoch: int
) -> None:
    ((job, published),) = await published_jobs(engine, 1)
    scope = await active_scope(engine)
    assert await run(engine) == 1
    (old,) = await instances_of(engine, published.result_id)
    async with engine.begin() as conn:
        await set_job_facts(conn, job.job_id, salary=SALARY)  # a det-facts writer that ran no hook
    assert await run(engine) == 1
    rows = await instances_of(engine, published.result_id)
    assert [(r.id, r.is_current) for r in rows] == [(old.id, False), (rows[1].id, True)]
    assert rows[0].superseded_at is not None and rows[0].det_inputs_hash == old.det_inputs_hash
    assert rows[1].det_inputs_hash == await expected_hash(engine, job.job_id, scope) != old.det_inputs_hash
    assert await run(engine) == 0


async def test_a_stale_version_instance_is_recomputed(engine: AsyncEngine, current_epoch: int) -> None:
    ((_, published),) = await published_jobs(engine, 1)
    assert await run(engine) == 1
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE det_results SET det_algorithm_version = 'det-old' WHERE llm_result_id = :r"),
            {"r": published.result_id},
        )
    assert await run(engine) == 1
    rows = await instances_of(engine, published.result_id)
    assert [(r.det_algorithm_version, r.is_current) for r in rows] == [
        ("det-old", False),
        (DET_ALGORITHM_VERSION, True),
    ]
    assert rows[1].det_inputs_hash == rows[0].det_inputs_hash  # the inputs did not change, the version did
    assert await run(engine) == 0


async def test_the_pending_scope_is_covered_while_one_exists(engine: AsyncEngine, current_epoch: int) -> None:
    ((job, published),) = await published_jobs(engine, 1)
    active, pending = await active_scope(engine), await pending_scope(engine)
    assert await run(engine) == 2  # one instance per scope
    found = await current(engine, published.result_id)
    assert set(found) == {active, pending}
    for scope in (active, pending):
        assert found[scope].det_inputs_hash == await expected_hash(engine, job.job_id, scope)
    assert found[active].det_inputs_hash != found[pending].det_inputs_hash  # the scopes differ
    assert await run(engine) == 0


async def test_a_changed_algorithm_version_recomputes_every_current_instance_once(
    engine: AsyncEngine, current_epoch: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await published_jobs(engine, 3)
    active, pending = await active_scope(engine), await pending_scope(engine)
    assert await run(engine) == 6
    before = {p.result_id: await current(engine, p.result_id) for _, p in made}
    monkeypatch.setattr(det_results, "DET_ALGORITHM_VERSION", "det-next")  # a release that changes it (L4568)
    assert await run(engine) == 6  # each (result, scope) once
    for _, p in made:
        after = await current(engine, p.result_id)
        assert set(after) == {active, pending}
        for scope, row in after.items():
            assert row.det_algorithm_version == "det-next" and row.id != before[p.result_id][scope].id
            assert row.det_inputs_hash == before[p.result_id][scope].det_inputs_hash
        assert len(await instances_of(engine, p.result_id)) == 4  # two superseded, two current
    assert await run(engine) == 0


async def test_the_chunks_cover_every_result_of_the_active_tuple(
    engine: AsyncEngine, current_epoch: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await published_jobs(engine, 5)
    monkeypatch.setattr(scoring_reconcilers, "DET_CHUNK", 2)  # three chunks: 2, 2 and 1
    assert await run(engine) == 5
    for _, p in made:
        assert len(await current(engine, p.result_id)) == 1
    assert await run(engine) == 0


async def test_without_an_active_tuple_nothing_is_computed(engine: AsyncEngine, current_epoch: int) -> None:
    ((_, published),) = await published_jobs(engine, 1)
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE active_config SET llm_tuple_id = NULL, profile_version_id = NULL WHERE id = 1")
        )
    assert await run(engine) == 0
    assert await instances_of(engine, published.result_id) == []


async def test_a_chunk_takes_active_config_then_the_job_rows_before_any_result(
    engine: AsyncEngine, current_epoch: int, migrated: PgServer
) -> None:
    # Carry T6-d: the job row `FOR SHARE` (step 4) before `compute_and_write` takes the result (step 10)
    ((job, published),) = await published_jobs(engine, 1)
    async with engine.connect() as blocker:  # a save: `active_config FOR UPDATE`
        await blocker.execute(sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE"))
        task = asyncio.create_task(run(engine))

        async def waits() -> bool:
            return task.done() or bool(lock_wait_details(migrated))

        await await_until(waits, 10, 0.02)
        (wait,) = lock_wait_details(migrated)
        assert (wait.relation, wait.key) == ("active_config", "1") and not task.done()
        assert not relation_locks(migrated, wait.pid) & {"jobs", "llm_results", "det_results"}
        await blocker.rollback()
    assert await asyncio.wait_for(task, 10) == 1
    async with engine.begin() as conn:
        await set_job_facts(conn, job.job_id, salary=SALARY)
    async with engine.connect() as purge:  # a purge chunk: the job row `FOR UPDATE`
        await purge.execute(_LOCK_JOB, {"j": job.job_id})
        task = asyncio.create_task(run(engine))

        async def waits_on_the_job() -> bool:
            return task.done() or bool(lock_wait_details(migrated))

        await await_until(waits_on_the_job, 10, 0.02)
        (wait,) = lock_wait_details(migrated)
        assert (wait.relation, wait.key) == ("jobs", str(job.job_id)) and not task.done()
        async with engine.connect() as probe:
            await probe.begin()  # the result row is free: no result lock before the job's (step 10 after 4)
            assert (await probe.execute(_RESULT_NOWAIT, {"r": published.result_id})).scalar_one() == 1
            await probe.rollback()
            await probe.begin()  # it holds `active_config FOR SHARE` (step 2): a save would wait
            with pytest.raises(sa.exc.OperationalError):
                await probe.execute(_ACTIVE_CONFIG_NOWAIT)
            await probe.rollback()
        await purge.rollback()
    assert await asyncio.wait_for(task, 10) == 1
    assert len(await instances_of(engine, published.result_id)) == 2


async def test_det_completeness_keeps_the_fence_rule(
    engine: AsyncEngine, current_epoch: int, queue_app: procrastinate.App
) -> None:
    ((_, published),) = await published_jobs(engine, 1)
    await assert_fence_rule(
        "det_completeness",
        "heavy",
        ctx(engine, queue_app, epoch=current_epoch + 1),
        ctx(engine, queue_app),
        scoring_reconcilers,
    )
    assert await instances_of(engine, published.result_id) == []  # neither check wrote
    assert await run(engine) == 1


async def live(engine: AsyncEngine, job_id: int) -> filters.LiveRefiltered:
    """The det-facts hook as a live writer calls it: `active_config FOR SHARE` (`lock_active_scopes`), the
    job row, then `refilter_job_live`, in one transaction."""
    async with engine.begin() as conn:
        active = await filters.lock_active_scopes(conn)
        await conn.execute(_LOCK_JOB, {"j": job_id})
        return await filters.refilter_job_live(conn, job_id, active=active)


async def provenance(conn: AsyncConnection, job_id: int, source_id: str) -> None:
    """The job's salary supplied by `source_id`'s default instance; no det-fact changes."""
    await conn.execute(_PROVENANCE, {"i": await instance_id(conn, source_id), "j": job_id})


async def test_the_live_hook_writes_one_instance_when_only_a_field_s_sources_changed(
    engine: AsyncEngine, current_epoch: int
) -> None:
    # Carry T15-e: `recompute_job_instances` -> `stale_of` compares the hash over the facts and their sources
    ((job, published),) = await published_jobs(engine, 1)
    scope = await active_scope(engine)
    async with engine.begin() as conn:
        await provenance(conn, job.job_id, "jsearch")
    first = await live(engine, job.job_id)
    assert len(first.instances) == 1
    (old,) = await instances_of(engine, published.result_id)
    assert (await live(engine, job.job_id)).instances == ()  # unchanged facts and sources: nothing
    async with engine.begin() as conn:
        facts_hash = (
            await conn.execute(sa.text("SELECT det_facts_hash FROM jobs WHERE id = :j"), {"j": job.job_id})
        ).scalar_one()
        await provenance(conn, job.job_id, "arbeitnow")  # the facts stay; their source changes
    second = await live(engine, job.job_id)
    assert len(second.instances) == 1  # exactly one new instance, for the active scope
    rows = await instances_of(engine, published.result_id)
    assert [(r.id, r.is_current) for r in rows] == [(old.id, False), (second.instances[0], True)]
    assert rows[1].det_inputs_hash == await expected_hash(engine, job.job_id, scope) != old.det_inputs_hash
    async with engine.connect() as conn:
        document = (
            await conn.execute(sa.text("SELECT det_inputs FROM det_results WHERE id = :d"), {"d": rows[1].id})
        ).scalar_one()
        assert (
            await conn.execute(sa.text("SELECT det_facts_hash FROM jobs WHERE id = :j"), {"j": job.job_id})
        ).scalar_one() == facts_hash  # the det-facts did not change
    assert document["job"]["salary"]["sources"] == ["arbeitnow"]
    assert await run(engine) == 0  # the reconciler finds it complete
