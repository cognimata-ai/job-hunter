"""The det-facts change hook (spec §9.7 L3716, §13.4 L4905; plan M3 Task 15, Codex r5 B3, r6 B4): a live
writer that changes a job's det-facts, holding `active_config FOR SHARE` before the job row, calls
`filters.refilter_job_live`, which writes the job's `hard_filter_results` for the active and the pending
scope, a new current `det_results` instance of its published active-tuple result for both scopes (none when
the inputs' hash is unchanged), and moves its unpublished active-tuple targets `filtered_out ⇄ pending` by the
active scope (overrides untouched). The scope precompute's hard-filter chunk is the pure writer
(`filters.refilter_job` through `recompute_stale`): it moves no target and takes no configuration lock.

The jobs are `tests.support.m3` jobs (no location, every work mode, German) and their published results
carry the stored columns of a real publication (`published_result`); the score tuple is the active one.
Every datum is invented.
"""

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, Final
from uuid import uuid4

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.sql import q
from jobhunter.dedup import groups, identity, merge
from jobhunter.run_settings import scope_from_payload
from jobhunter.scoring import det_results, filters
from jobhunter.scoring.det_results import StoredResultInvalid, det_inputs, job_sources
from jobhunter.scoring.versioning import DET_ALGORITHM_VERSION
from jobhunter.settings import scope_apply
from jobhunter.types import JSONValue, get_object
from jobhunter.worker.operations import reclaim_stale_operations, set_progress
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.collect import STEPSTONE_1, STEPSTONE_2, key_pair, pending_item, queued_run, resolved
from tests.support.faults import PausePoint, PausePoints
from tests.support.groups import FULL_TEXT, canonical_job
from tests.support.m3 import Job, insert_id, insert_job, insert_row
from tests.support.pg import PgServer, lock_wait_details, pg_rows
from tests.support.procs import await_until
from tests.support.reconcile import run_reconciler
from tests.support.scoring import (
    activate_score_tuple,
    instances_of,
    place_far,
    published_result,
    set_job_facts,
    state_target,
    swept_of,
)
from tests.support.settings import (
    change_interval,
    generation,
    held_save,
    pending_scope_id,
    save,
    save_scope,
    scope_ids_of,
    settings_body,
)

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
_POINTERS: Final = sa.text(
    "SELECT scope_version_id, pending_scope_version_id FROM active_config WHERE id = 1"
)
_FILTER_HASHES: Final = sa.text(
    "SELECT scope_version_id, inputs_hash, passed FROM hard_filter_results WHERE job_id = :j "
    "ORDER BY scope_version_id"
)
_FEEDBACK: Final = sa.text("SELECT det_result_id FROM score_feedback WHERE id = :f")
_LOCK_JOB: Final = sa.text("SELECT id FROM jobs WHERE id = :j FOR UPDATE")
_LOCK_RESULT: Final = sa.text("SELECT id FROM llm_results WHERE id = :r FOR UPDATE")
_STORED_FACTS: Final = q("UPDATE llm_results SET job_facts = :facts WHERE id = :r", jsonb=["facts"])
_SCOPE_PAYLOAD: Final = sa.text("SELECT payload FROM scope_versions WHERE id = :s")


async def pending_scope(conn: AsyncConnection) -> int:
    """A scope version of the active scope with one industry excluded (an input of every instance), made the
    pending one; its id."""
    digest = hashlib.sha256(uuid4().bytes).digest()
    scope_id = int((await conn.execute(_NEW_SCOPE, {"hash": digest})).scalar_one())
    await conn.execute(_SET_PENDING, {"s": scope_id})
    return scope_id


async def live(engine: AsyncEngine, job_id: int) -> filters.LiveRefiltered:
    """The hook as a live writer calls it: `active_config FOR SHARE` (`lock_active_scopes`), the job row, then
    `refilter_job_live`, in one transaction."""
    async with engine.begin() as conn:
        active = await filters.lock_active_scopes(conn)
        await conn.execute(_LOCK_JOB, {"j": job_id})
        return await filters.refilter_job_live(conn, job_id, active=active)


async def expected_hash(engine: AsyncEngine, job_id: int, scope_version_id: int) -> bytes:
    """The `det_inputs_hash` of the job's current det-facts and sources under the scope."""
    async with engine.connect() as conn:
        payload = (await conn.execute(_SCOPE_PAYLOAD, {"s": scope_version_id})).scalar_one()
        det = await filters.read_det_facts(conn, job_id)
        sources = await job_sources(conn, job_id)
    return det_inputs(det, sources, scope_from_payload(get_object(payload, "")))[1]


# ---- the hook ----------------------------------------------------------------------------------------------


async def test_a_salary_change_writes_new_current_instances_and_leaves_the_feedback_on_the_old(
    engine,
) -> None:
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        job = await insert_job(conn)
        published = await published_result(conn, job, tuple_id)
        pending = await pending_scope(conn)
        active = int((await conn.execute(_POINTERS)).one().scope_version_id)
    first = await live(engine, job.job_id)  # the result had no instance: one per scope
    before = {row.scope_version_id: row for row in await instances_of(engine, published.result_id)}
    assert sorted(first.instances) == sorted(row.id for row in before.values())
    assert set(before) == {active, pending}
    async with engine.begin() as conn:
        feedback = await insert_id(
            conn,
            "score_feedback",
            {
                "job_id": job.job_id,
                "job_revision_id": job.revision_id,
                "llm_result_id": published.result_id,
                "det_result_id": before[active].id,
                "kind": "label",
                "label": "good",
            },
        )
        new_facts_hash = await set_job_facts(conn, job.job_id, salary=SALARY)
    second = await live(engine, job.job_id)
    rows = await instances_of(engine, published.result_id)
    current = {row.scope_version_id: row for row in rows if row.is_current}
    assert set(current) == {active, pending}
    assert sorted(second.instances) == sorted(row.id for row in current.values())
    for scope_id, old in before.items():
        assert current[scope_id].id != old.id
        assert current[scope_id].det_inputs_hash == await expected_hash(engine, job.job_id, scope_id)
        assert current[scope_id].det_inputs_hash != old.det_inputs_hash
        (kept,) = [row for row in rows if row.id == old.id]
        assert not kept.is_current and kept.superseded_at is not None  # superseded first, never updated
        assert (kept.det_inputs_hash, kept.score_low, kept.score_mid, kept.score_high) == (
            old.det_inputs_hash,
            old.score_low,
            old.score_mid,
            old.score_high,
        )
    async with engine.connect() as conn:
        assert (await conn.execute(_FEEDBACK, {"f": feedback})).scalar_one() == before[active].id
        filter_rows = (await conn.execute(_FILTER_HASHES, {"j": job.job_id})).all()
    assert [(row.scope_version_id, bytes(row.inputs_hash)) for row in filter_rows] == [
        (scope_id, new_facts_hash) for scope_id in sorted((active, pending))
    ]


async def test_an_unchanged_hash_writes_no_instance_and_moves_no_target(engine) -> None:
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        job = await insert_job(conn)
        published = await published_result(conn, job, tuple_id)
    first = await live(engine, job.job_id)
    assert len(first.instances) == 1 and first.changed
    again = await live(engine, job.job_id)
    assert (again.instances, again.filtered, again.unfiltered, again.changed) == ((), 0, 0, False)
    assert len(await instances_of(engine, published.result_id)) == 1


async def test_the_job_s_unpublished_targets_move_by_its_new_facts_and_overrides_stay(engine) -> None:
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        plain, overridden = await insert_job(conn), await insert_job(conn)
        moving = await state_target(conn, plain, tuple_id, "pending")
        kept = await state_target(conn, overridden, tuple_id, "pending", override="score_now")
        active = int((await conn.execute(_POINTERS)).one().scope_version_id)
    for job in (plain, overridden):
        await live(engine, job.job_id)  # both pass: nothing moves
    assert (await swept_of(engine, moving)).state == "pending"
    async with engine.begin() as conn:
        for job in (plain, overridden):
            await set_job_facts(conn, job.job_id, ad_language="other")  # not an accepted language
    out = await live(engine, plain.job_id)
    assert (out.filtered, out.unfiltered, out.changed) == (1, 0, True)
    target = await swept_of(engine, moving)
    assert (target.state, target.filter_scope_version_id) == ("filtered_out", active)
    assert (await live(engine, overridden.job_id)).filtered == 0
    assert (await swept_of(engine, kept)).state == "pending"  # an override is never moved (§7.16.3)
    async with engine.begin() as conn:
        await set_job_facts(conn, plain.job_id, ad_language="de")
    back = await live(engine, plain.job_id)
    assert (back.filtered, back.unfiltered) == (0, 1)
    target = await swept_of(engine, moving)
    assert (target.state, target.filter_scope_version_id) == ("pending", None)


async def test_a_re_sighting_with_unchanged_facts_takes_no_result_lock_and_writes_nothing(
    engine, current_epoch
) -> None:
    """Review Minor 5: the live hook compares the current instances' hashes first (`det_results.stale_of`),
    so a re-sighting whose det-facts and sources are unchanged reaches no `compute_and_write`: it takes no
    `llm_results FOR UPDATE` (it completes while another transaction holds the result row) and writes no
    instance."""
    owner = await queued_run(engine)
    native = {"native_id": "SEEN1", "primary_link": STEPSTONE_1}
    first = await pending_item(engine, owner, "jsearch", native, fetched_at=utcnow() - timedelta(hours=2))
    assert await resolved(engine, owner, first) == "linked"
    async with engine.begin() as conn:
        job_id = int((await conn.execute(sa.text("SELECT max(id) FROM jobs"))).scalar_one())
        tuple_id = await activate_score_tuple(conn)
        revision = int(
            (
                await conn.execute(
                    sa.text("SELECT current_revision_id FROM jobs WHERE id = :j"), {"j": job_id}
                )
            ).scalar_one()
        )
        published = await published_result(conn, Job(job_id, revision), tuple_id)
    assert len((await live(engine, job_id)).instances) == 1  # the active scope's instance
    again = await pending_item(engine, owner, "jsearch", native, fetched_at=utcnow() - timedelta(hours=1))
    async with engine.connect() as blocker:
        await blocker.begin()
        await blocker.execute(_LOCK_RESULT, {"r": published.result_id})
        assert await asyncio.wait_for(resolved(engine, owner, again), 10) == "linked"
        await blocker.rollback()
    assert len(await instances_of(engine, published.result_id)) == 1


async def test_a_stored_result_that_fails_its_validation_raises_without_its_content(engine) -> None:
    """Carry T6-g: the hook's recompute of a result whose stored `job_facts` no longer validates raises
    `StoredResultInvalid`, which names the result and pydantic's error types only; neither its cause nor its
    context holds the stored content, and the caller's transaction wrote nothing."""
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        job = await insert_job(conn)
        published = await published_result(conn, job, tuple_id)
        await conn.execute(
            _STORED_FACTS, {"facts": {"invented": "Erfundenes Geheimnis"}, "r": published.result_id}
        )
    with pytest.raises(StoredResultInvalid) as raised:
        await live(engine, job.job_id)
    error = raised.value
    assert error.llm_result_id == published.result_id and error.error_types
    assert "Erfundenes" not in str(error) and "Geheimnis" not in repr(error.error_types)
    assert error.__cause__ is None and error.__context__ is None
    assert await instances_of(engine, published.result_id) == []


# ---- the completeness reconciler refilters the jobs it recomputed ------------------------------------------


async def test_the_completeness_reconciler_moves_the_targets_of_exactly_the_jobs_it_recomputed(
    engine, current_epoch, queue_app
) -> None:
    """`hard_filter_completeness` (M1 Deviation 8 closed): the stale jobs' results are recomputed, then the
    unpublished active-tuple targets of exactly those jobs are refiltered by the active scope: a `pending`
    one its new result fails is filtered out, an override is left alone, the target of a job whose stored
    result was current is not touched (even though that result failed), and the ranking is bumped."""
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        active = (await conn.execute(_POINTERS)).one().scope_version_id
        stale, overridden, current = await insert_job(conn), await insert_job(conn), await insert_job(conn)
        for job in (stale, overridden):  # no result yet for these two: stale, and they fail the scope
            await set_job_facts(conn, job.job_id, ad_language="other")
        moving = await state_target(conn, stale, tuple_id, "pending")
        kept = await state_target(conn, overridden, tuple_id, "pending", override="score_now")
        untouched = await state_target(conn, current, tuple_id, "pending")
        await insert_row(
            conn,
            "hard_filter_results",
            {
                "job_id": current.job_id,
                "scope_version_id": active,
                "passed": False,
                "reasons": ["ad_language_excluded"],
                "inputs_hash": b"\x00",  # the job's own det_facts_hash: current, so not recomputed
                "det_algorithm_version": DET_ALGORITHM_VERSION,
            },
        )
    before = await generation(engine)
    assert await run_reconciler("hard_filter_completeness", engine, now=utcnow(), epoch=current_epoch) == 2
    target = await swept_of(engine, moving)
    assert (target.state, target.filter_scope_version_id) == ("filtered_out", active)
    assert (await swept_of(engine, kept)).state == "pending"  # an override (§7.16.3)
    assert (await swept_of(engine, untouched)).state == "pending"  # not recomputed: not refiltered
    assert await generation(engine) == before + 1


# ---- the precompute's chunk is the pure writer -------------------------------------------------------------


async def test_the_precompute_chunk_moves_no_target_and_takes_no_configuration_lock(
    logged_in_client, engine, op_runner, migrated: PgServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex r5 B3: the hard-filter chunk of the pending scope runs while a configuration writer (a settings
    save) holds `active_config FOR UPDATE` and another reader's `active_config FOR SHARE` queues behind it:
    the chunk commits without waiting on `active_config` (it takes no lock on it), the live hook is never
    called from it, and the target its new filters fail stays `pending` until `_final`'s flip moves it. (A
    real takeover's cycle is the next test's.)"""
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        job = await insert_job(conn)
        await place_far(conn, job.job_id)  # passes the active scope (no location), fails Berlin at 10 km
        target = await state_target(conn, job, tuple_id, "pending")
    saved = await save_scope(logged_in_client, radius_km=10)
    (new_scope,) = await scope_ids_of(engine, saved)
    hooked: list[int] = []
    real_hook = filters.refilter_job_live

    async def spy(conn: AsyncConnection, job_id: int, *, active: filters.ActiveScopes) -> Any:
        hooked.append(job_id)
        return await real_hook(conn, job_id, active=active)

    monkeypatch.setattr(filters, "refilter_job_live", spy)
    pauses = PausePoints(scope_apply.AFTER_CHUNK_POINT, scope_apply.BEFORE_FINAL_POINT)
    async with engine.connect() as reader:
        reader_pid = int((await reader.execute(sa.text("SELECT pg_backend_pid()"))).scalar_one())
        async with held_save(engine, logged_in_client, change_interval):  # active_config FOR UPDATE, held
            queued = asyncio.create_task(
                reader.execute(sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR SHARE"))
            )

            async def reader_queued() -> bool:
                return any(w.pid == reader_pid for w in lock_wait_details(migrated))

            await await_until(reader_queued, 10, 0.02)
            run = asyncio.create_task(op_runner.run_one(saved["apply_scope_operation_id"], pause=pauses))
            await asyncio.wait_for(pauses[scope_apply.AFTER_CHUNK_POINT].reached.wait(), 10)  # committed
            waits = lock_wait_details(migrated)
            assert [(w.pid, w.relation) for w in waits] == [(reader_pid, "active_config")]
            assert not queued.done()
            assert (await swept_of(engine, target)).state == "pending" and hooked == []
        await asyncio.wait_for(queued, 10)  # the save rolled back: the reader's lock is granted
        await reader.rollback()
    pauses[scope_apply.AFTER_CHUNK_POINT].resume()
    await asyncio.wait_for(pauses[scope_apply.BEFORE_FINAL_POINT].reached.wait(), 10)
    assert (await swept_of(engine, target)).state == "pending"  # every chunk done: still not moved
    pauses[scope_apply.BEFORE_FINAL_POINT].resume()
    await asyncio.wait_for(run, 10)
    moved = await swept_of(engine, target)
    assert (moved.state, moved.filter_scope_version_id) == ("filtered_out", new_scope)  # by the flip
    assert hooked == []


async def test_the_chunk_commits_while_a_takeover_holds_the_configuration_and_waits_on_its_operation(
    logged_in_client, engine, op_runner, migrated: PgServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex r5 B3's cycle with a takeover, modelled: while the hard-filter chunk holds its job rows and its
    operation row (and no `active_config` row lock), a takeover sweep holds `active_config FOR SHARE` (its
    first lock) and waits on that operation row, and a configuration writer (a settings save) queues `FOR
    UPDATE` behind the takeover. A chunk that asked for `active_config` in a mode that conflicts with the
    takeover's (`FOR NO KEY UPDATE` or `FOR UPDATE`, as a pointer write would) would wait on it and close the
    cycle; this one asks for nothing more and commits, its progress with it, which lets the takeover in and
    then the writer. (PostgreSQL grants a `FOR SHARE` request that the holders allow even with a writer
    queued, so a later share request is harmless here; the previous test, a held save, pins that the chunk
    asks for none.)"""
    async with engine.begin() as conn:
        await activate_score_tuple(conn)
        await insert_job(conn)
    saved = await save_scope(logged_in_client, radius_km=10)
    op_id = saved["apply_scope_operation_id"]
    in_chunk, release = asyncio.Event(), asyncio.Event()

    async def held_progress(conn: AsyncConnection, claim: Any, progress: Any) -> None:
        if not in_chunk.is_set():  # the first chunk, its rows and its owner row held
            in_chunk.set()
            await release.wait()
        await set_progress(conn, claim, progress)

    monkeypatch.setattr(scope_apply, "set_progress", held_progress)
    pause = PausePoint(scope_apply.BEFORE_FINAL_POINT)
    run = asyncio.create_task(op_runner.run_one(op_id, pause=pause))
    await asyncio.wait_for(in_chunk.wait(), 10)
    chunk_pid = next(
        int(str(pid))
        for (pid,) in pg_rows(
            migrated,
            "superuser",
            "SELECT pid FROM pg_locks WHERE locktype = 'relation' AND granted "
            "AND relation = 'hard_filter_results'::regclass AND pid <> pg_backend_pid()",
        )
    )
    row_locks = (  # a row lock or a write; the pointer's plain read takes only `AccessShareLock`
        "SELECT mode FROM pg_locks WHERE pid = %s AND relation = 'active_config'::regclass "
        "AND mode <> 'AccessShareLock'"
    )
    assert pg_rows(migrated, "superuser", row_locks, chunk_pid) == []  # no configuration lock
    payload = (await settings_body(logged_in_client))["payload"]
    change_interval(payload)
    async with engine.connect() as takeover:
        await takeover.begin()
        await takeover.execute(sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR SHARE"))
        sweep = asyncio.create_task(
            takeover.execute(
                sa.text("SELECT progress FROM operations WHERE id = :i FOR UPDATE"), {"i": op_id}
            )
        )

        def waits_on(relation: str) -> list[Any]:
            return [w for w in lock_wait_details(migrated) if w.relation == relation]

        async def takeover_waits() -> bool:
            return any(chunk_pid in w.blocking_pids for w in waits_on("operations"))

        await await_until(takeover_waits, 10, 0.02)
        writer = asyncio.create_task(save(logged_in_client, payload))
        takeover_pid = waits_on("operations")[0].pid

        async def writer_queued() -> bool:
            return any(takeover_pid in w.blocking_pids for w in waits_on("active_config"))

        await await_until(writer_queued, 10, 0.02)
        release.set()  # the chunk goes on: its last statement and its commit
        progress = (await asyncio.wait_for(sweep, 10)).scalar_one()  # the takeover has the operation row
        assert progress["done"] == 1  # because the chunk committed (not aborted by a deadlock)
        assert not writer.done()
        await takeover.rollback()
    assert (await asyncio.wait_for(writer, 10))["version"]["effect_classes"] == ["schedule"]
    await asyncio.wait_for(pause.reached.wait(), 10)
    pause.resume()
    await asyncio.wait_for(run, 10)
    assert await pending_scope_id(engine) is None


async def test_a_det_chunk_of_a_superseded_execution_writes_nothing(
    logged_in_client, engine, op_runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stale reclaim takes the apply's operation back after its hard-filter chunk: the next chunk, a det
    chunk, fails its owner fence (`lock_operation_owner`, step 5) before any result is touched, so it writes
    no instance (it never reaches `recompute_instance` and its `llm_results FOR UPDATE`), and nothing
    flips."""
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        results = [
            (await published_result(conn, await insert_job(conn), tuple_id)).result_id for _ in range(2)
        ]
    saved = await save_scope(logged_in_client, radius_km=30)
    (sid,) = await scope_ids_of(engine, saved)
    op_id = saved["apply_scope_operation_id"]
    reclaimed: list[int] = []

    async def reclaim_after_the_first_chunk(point: str) -> None:
        if point == scope_apply.AFTER_CHUNK_POINT and not reclaimed:
            async with engine.begin() as conn:
                reclaimed.extend(await reclaim_stale_operations(conn, now=utcnow() + timedelta(hours=1)))

    recomputed: list[int] = []
    real_recompute = det_results.recompute_instance

    async def spy(conn: AsyncConnection, **kwargs: Any) -> Any:
        recomputed.append(kwargs["llm_result_id"])
        return await real_recompute(conn, **kwargs)

    monkeypatch.setattr(scope_apply, "afault_point", reclaim_after_the_first_chunk)
    monkeypatch.setattr(det_results, "recompute_instance", spy)
    await op_runner.run_one(op_id)
    assert reclaimed == [op_id] and recomputed == []
    for result_id in results:
        assert [row for row in await instances_of(engine, result_id) if row.scope_version_id == sid] == []
    async with engine.connect() as conn:
        status = (
            await conn.execute(sa.text("SELECT status FROM operations WHERE id = :i"), {"i": op_id})
        ).scalar_one()
    assert (
        status == "pending" and await pending_scope_id(engine) == sid
    )  # requeued by the reclaim, not flipped


# ---- the live hook's callers hold the configuration lock ---------------------------------------------------

CallerOf = Callable[[AsyncConnection], Awaitable[object]]


async def _identity(engine: AsyncEngine, job_with_postings: Any) -> tuple[int, CallerOf]:
    """A job a first item made, and a second sighting of its posting: `resolve_identity` locks that job."""
    owner = await queued_run(engine)
    native = {"native_id": "LIVE1", "primary_link": STEPSTONE_1}
    first = await pending_item(engine, owner, "jsearch", native, fetched_at=utcnow() - timedelta(hours=2))
    assert await resolved(engine, owner, first) == "linked"
    async with engine.connect() as conn:
        job_id = int((await conn.execute(sa.text("SELECT max(id) FROM jobs"))).scalar_one())
    second = await pending_item(engine, owner, "jsearch", native)

    async def call(conn: AsyncConnection) -> object:
        return await identity.resolve_item(conn, owner=owner, raw_item_id=second)

    return job_id, call


async def _merge(engine: AsyncEngine, job_with_postings: Any) -> tuple[int, CallerOf]:
    """A strong-key merge of B into A, then its unmerge, which locks both jobs."""
    owner = await queued_run(engine)
    at = utcnow() - timedelta(days=1)
    pair = await key_pair(engine, owner, at=at)
    merging = await pending_item(
        engine,
        owner,
        "adzuna",
        {"native_id": "9", "primary_link": STEPSTONE_1},
        fetched_at=at + timedelta(hours=2),
    )
    assert await resolved(engine, owner, merging) == "linked"
    async with engine.connect() as conn:
        event = int((await conn.execute(sa.text("SELECT max(id) FROM merge_events"))).scalar_one())

    async def call(conn: AsyncConnection) -> object:
        return await merge.unmerge(conn, event)

    return pair.a, call


async def _conflict(engine: AsyncEngine, job_with_postings: Any) -> tuple[int, CallerOf]:
    """An identity conflict (a posting whose exclusive key another job owns), resolved by `merge`, which locks
    the posting's job and the candidates and recomputes the posting's job."""
    owner = await queued_run(engine)
    at = utcnow() - timedelta(days=1)
    await key_pair(engine, owner, at=at)
    serp = await pending_item(
        engine,
        owner,
        "serpapi",
        {"native_id": "H", "primary_link": STEPSTONE_2},
        fetched_at=at + timedelta(hours=2),
    )
    assert await resolved(engine, owner, serp) == "linked"
    async with engine.connect() as conn:
        conflict = (
            await conn.execute(
                sa.text("SELECT id, posting_job_id FROM identity_conflicts WHERE status = 'open'")
            )
        ).one()

    async def call(conn: AsyncConnection) -> object:
        await merge.resolve_conflict(conn, int(conflict.id), "merge")
        return None

    return int(conflict.posting_job_id), call


async def _groups(engine: AsyncEngine, job_with_postings: Any) -> tuple[int, CallerOf]:
    """A group of two canonical jobs, then its confirmation, which merges them and recomputes the
    representative."""
    at = utcnow() - timedelta(days=2)
    jobs = [
        await canonical_job(
            engine,
            job_with_postings,
            "jsearch",
            observed_at=at + timedelta(hours=i),
            description_text=FULL_TEXT,
            company_raw="Globex",
        )
        for i in range(2)
    ]

    async def epoch_1(conn: AsyncConnection) -> None:
        pass

    async with engine.begin() as conn:
        effect = await groups.regroup_job(conn, jobs[0], guard=epoch_1)
    assert effect.group_id is not None
    group_id = effect.group_id

    async def call(conn: AsyncConnection) -> object:
        return await groups.confirm_group(conn, group_id)

    return jobs[1], call


@pytest.mark.parametrize(
    "caller",
    [_identity, _merge, _conflict, _groups],
    ids=["resolve_identity", "unmerge", "resolve_conflict_merge", "confirm"],
)
async def test_the_live_hook_runs_under_the_caller_s_configuration_lock(
    engine, current_epoch, job_with_postings, migrated: PgServer, monkeypatch: pytest.MonkeyPatch, caller: Any
) -> None:
    """Each caller of `refilter_job_live` holds `active_config FOR SHARE` before its job rows: while it waits
    on a job row another transaction holds, it already blocks a queued configuration writer, and when the
    hook runs that writer is still queued behind it."""
    job_id, call = await caller(engine, job_with_postings)
    seen: list[tuple[int, bool]] = []
    real_hook = filters.refilter_job_live

    async def spy(conn: AsyncConnection, hooked_job: int, *, active: filters.ActiveScopes) -> Any:
        pid = int((await conn.execute(sa.text("SELECT pg_backend_pid()"))).scalar_one())
        writer_queued = any(
            w.relation == "active_config" and pid in w.blocking_pids for w in lock_wait_details(migrated)
        )
        seen.append((hooked_job, writer_queued))
        return await real_hook(conn, hooked_job, active=active)

    monkeypatch.setattr(filters, "refilter_job_live", spy)

    async def run_caller() -> object:
        async with engine.begin() as conn:
            return await call(conn)

    async with engine.connect() as blocker, engine.connect() as writer:
        await blocker.begin()
        await blocker.execute(_LOCK_JOB, {"j": job_id})
        task = asyncio.create_task(run_caller())

        def waits_on(relation: str) -> list[Any]:
            return [w for w in lock_wait_details(migrated) if w.relation == relation]

        async def on_job() -> bool:
            return bool(waits_on("jobs")) or task.done()

        await await_until(on_job, 10, 0.02)
        assert not task.done()
        (job_wait,) = waits_on("jobs")
        caller_pid = job_wait.pid
        writing = asyncio.create_task(
            writer.execute(sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE"))
        )

        async def writer_queued() -> bool:
            return any(caller_pid in w.blocking_pids for w in waits_on("active_config"))

        await await_until(writer_queued, 10, 0.02)  # the caller holds active_config already
        await blocker.rollback()
        await asyncio.wait_for(task, 10)
        await asyncio.wait_for(writing, 10)
        await writer.rollback()
    assert seen and all(queued for _, queued in seen), seen
