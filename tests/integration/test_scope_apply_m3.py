"""The scope apply's `det_results` half and its target moves (spec §13.4 L4910-4920, §6.8 `det_recompute`;
plan M3 Task 15, M2 Deviations 12 and 40, Codex r5 B3).

Beside its hard-filter chunks the precompute writes, in chunks of `DET_BATCH` under `active_config FOR SHARE`,
a current `det_results` instance for the pending scope of every published result of the active tuple that
lacks one whose hash and algorithm version are current (`operations.progress` counts them in `det_done` of
`det_total`). The final transaction fails its check while `incomplete_results` is non-zero and, after the
flip, moves the active tuple's unpublished targets `filtered_out ⇄ pending` by the new filters (overrides
untouched), then bumps the ranking. `scope_apply.before_final` is reached right before each final
transaction.

The published results carry the stored columns of a real publication (`published_result`) or are published
through the `score_target` subgraph on the fake LLM; the score tuple is the active one. Every datum is
invented.
"""

import asyncio
import dataclasses
from typing import Any, Final

import pytest
import sqlalchemy as sa
from jobhunter.graphs.score.deps import ScoreDeps
from jobhunter.run_settings import scope_from_payload
from jobhunter.scoring import filters
from jobhunter.scoring.det_results import det_inputs, job_sources
from jobhunter.settings import dryrun, scope_apply
from jobhunter.types import JSONValue, get_object
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.faults import PausePoint
from tests.support.llm import FakeLLM
from tests.support.m2 import insert_profile_chain
from tests.support.m3 import NOW, insert_job
from tests.support.scoring import (
    activate_score_tuple,
    claimed_target,
    instances_of,
    invoke_subgraph,
    place_far,
    published_result,
    score_deps,
    set_job_facts,
    state_target,
    swept_of,
    synthetic_c_draft,
)
from tests.support.settings import berlin, operation_end, pending_scope_id, save_scope, scope_ids_of

pytestmark = pytest.mark.integration

SALARY: Final[dict[str, JSONValue]] = {
    "min": 60000.0,
    "max": 65000.0,
    "currency": "EUR",
    "period": "year",
    "kind": "stated",
    "raw": "60.000-65.000 EUR",
    "annual_eur_min": 60000.0,
    "annual_eur_max": 65000.0,
}
_SCOPE_PAYLOAD: Final = sa.text("SELECT payload FROM scope_versions WHERE id = :s")
_ACTIVE: Final = sa.text("SELECT scope_version_id FROM active_config WHERE id = 1")
_REFRESH_FILTERS: Final = sa.text("UPDATE hard_filter_results SET inputs_hash = :h WHERE job_id = :j")


@pytest.fixture
async def deps(engine: AsyncEngine, fake_llm: FakeLLM, clean_db: None) -> ScoreDeps:
    return await score_deps(engine, fake_llm.gateway(engine))


async def incomplete(engine: AsyncEngine, scope_version_id: int, llm_tuple_id: int) -> int:
    async with engine.connect() as conn:
        return await scope_apply.incomplete_results(
            conn, scope_version_id=scope_version_id, llm_tuple_id=llm_tuple_id
        )


async def current_for(engine: AsyncEngine, result_id: int, scope_version_id: int) -> list[Any]:
    """The result's current instances for the scope."""
    return [
        row
        for row in await instances_of(engine, result_id)
        if row.is_current and row.scope_version_id == scope_version_id
    ]


async def expected_hash(engine: AsyncEngine, job_id: int, scope_version_id: int) -> bytes:
    """The `det_inputs_hash` of the job's current det-facts and sources under the scope."""
    async with engine.connect() as conn:
        payload = (await conn.execute(_SCOPE_PAYLOAD, {"s": scope_version_id})).scalar_one()
        det = await filters.read_det_facts(conn, job_id)
        sources = await job_sources(conn, job_id)
    return det_inputs(det, sources, scope_from_payload(get_object(payload, "")))[1]


def flipped_checks(events: list[dict[str, Any]]) -> list[int]:
    return [int(e["checks"]) for e in events if e["event"] == "scope_apply_flipped"]


# ---- the flip ----------------------------------------------------------------------------------------------


async def test_the_flip_never_sees_a_published_result_without_a_pending_scope_instance(
    logged_in_client, engine, op_runner, deps, fake_llm, log_events
) -> None:
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        published = [await published_result(conn, await insert_job(conn), tuple_id) for _ in range(2)]
        far, overridden, back = await insert_job(conn), await insert_job(conn), await insert_job(conn)
        for job in (far, overridden):
            await place_far(conn, job.job_id)  # pass the active scope (no location), fail Berlin at 10 km
        far_target = await state_target(conn, far, tuple_id, "pending")
        overridden_target = await state_target(conn, overridden, tuple_id, "pending", override="score_now")
        old_scope = int((await conn.execute(_ACTIVE)).scalar_one())
        back_target = await state_target(  # filtered out once; its job passes the new scope
            conn, back, tuple_id, "filtered_out", filter_scope_version_id=old_scope
        )
    one_more = await claimed_target(engine)  # its job exists before the apply, so the precompute filters it
    fake_llm.answer("generate", one_more.job_revision_id, synthetic_c_draft())
    saved = await save_scope(logged_in_client, radius_km=10)  # the pending scope and its apply_scope
    (new_scope,) = await scope_ids_of(engine, saved)
    pause = PausePoint(scope_apply.BEFORE_FINAL_POINT)
    task = asyncio.create_task(op_runner.run_one(saved["apply_scope_operation_id"], pause=pause))
    await asyncio.wait_for(pause.reached.wait(), 30)
    assert await incomplete(engine, new_scope, tuple_id) == 0  # the precompute covered the two results
    assert (await swept_of(engine, far_target)).state == "pending"  # no target moves before the flip
    final = await invoke_subgraph(deps, one_more)  # writes current instances for both scopes
    assert final["outcome"] == "published_accepted"
    pause.resume()
    await asyncio.wait_for(task, 30)
    assert await pending_scope_id(engine) is None
    assert await incomplete(engine, new_scope, tuple_id) == 0
    assert flipped_checks(log_events()) == [1]  # the first final check passed: nothing was missing
    async with engine.connect() as conn:
        third = int(
            (
                await conn.execute(
                    sa.text("SELECT published_result_id FROM score_targets WHERE id = :t"),
                    {"t": one_more.target_id},
                )
            ).scalar_one()
        )
    for result_id in [p.result_id for p in published] + [third]:
        assert len(await current_for(engine, result_id, new_scope)) == 1, result_id
    moves = {t: await swept_of(engine, t) for t in (far_target, overridden_target, back_target)}
    assert (moves[far_target].state, moves[far_target].filter_scope_version_id) == ("filtered_out", new_scope)
    assert moves[overridden_target].state == "pending"  # an override is never moved (§7.16.3)
    assert (moves[back_target].state, moves[back_target].filter_scope_version_id) == ("pending", None)


async def test_the_precompute_writes_one_current_instance_per_published_result(
    logged_in_client, engine, op_runner, monkeypatch
) -> None:
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        jobs = [await insert_job(conn) for _ in range(3)]
        results = [(await published_result(conn, job, tuple_id)).result_id for job in jobs]
        chain = await insert_profile_chain(conn, 5)  # another tuple's result: not the active tuple's
        foreign = (await published_result(conn, await insert_job(conn), chain.llm_tuple_id)).result_id
    saved = await save_scope(logged_in_client, radius_km=30)
    (sid,) = await scope_ids_of(engine, saved)
    op_id = saved["apply_scope_operation_id"]
    assert await incomplete(engine, sid, tuple_id) == 3
    monkeypatch.setattr(scope_apply, "DET_BATCH", 2)
    progress: list[Any] = []

    async def after_chunk(point: str) -> None:
        if point == scope_apply.AFTER_CHUNK_POINT:
            progress.append((await operation_end(engine, op_id))[1])

    monkeypatch.setattr(scope_apply, "afault_point", after_chunk)
    await op_runner.run_one(op_id)
    assert progress == [
        {"done": 4, "total": 4, "det_done": 0, "det_total": 3},  # the hard-filter chunk
        {"done": 4, "total": 4, "det_done": 2, "det_total": 3},  # a det chunk of DET_BATCH results
        {"done": 4, "total": 4, "det_done": 3, "det_total": 3},  # the last, shorter one
    ]
    assert await operation_end(engine, op_id) == (
        "succeeded",
        {"done": 4, "total": 4, "det_done": 3, "det_total": 3},
    )
    for job, result_id in zip(jobs, results, strict=True):
        (instance,) = await current_for(engine, result_id, sid)
        assert instance.det_inputs_hash == await expected_hash(engine, job.job_id, sid)
    assert await instances_of(engine, foreign) == []
    assert await incomplete(engine, sid, tuple_id) == 0


async def test_a_stale_instance_fails_the_final_check_once_and_the_retry_recomputes_it(
    logged_in_client, engine, op_runner, monkeypatch, log_events
) -> None:
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        jobs = [await insert_job(conn) for _ in range(2)]
        results = [(await published_result(conn, job, tuple_id)).result_id for job in jobs]
    saved = await save_scope(logged_in_client, radius_km=30)
    (sid,) = await scope_ids_of(engine, saved)
    changed = jobs[0]
    checks: list[tuple[int, int]] = []

    async def before_final(point: str) -> None:
        if point != scope_apply.BEFORE_FINAL_POINT:
            return
        if (
            not checks
        ):  # after the precompute: a det-facts writer that refreshed the filters, not the instances
            async with engine.begin() as conn:
                digest = await set_job_facts(conn, changed.job_id, salary=SALARY)
                await conn.execute(_REFRESH_FILTERS, {"h": digest, "j": changed.job_id})
        async with engine.connect() as conn:
            checks.append(
                (
                    await scope_apply.incomplete_jobs(conn, sid),
                    await scope_apply.incomplete_results(conn, scope_version_id=sid, llm_tuple_id=tuple_id),
                )
            )

    monkeypatch.setattr(scope_apply, "afault_point", before_final)
    await op_runner.run_one(saved["apply_scope_operation_id"])
    assert checks == [(0, 1), (0, 0)]  # the det half failed the first check alone; the retry recomputed it
    assert flipped_checks(log_events()) == [2]
    assert await pending_scope_id(engine) is None
    (instance,) = await current_for(engine, results[0], sid)
    assert instance.det_inputs_hash == await expected_hash(engine, changed.job_id, sid)
    superseded = [r for r in await instances_of(engine, results[0]) if r.scope_version_id == sid]
    assert len(superseded) == 2 and not superseded[0].is_current and superseded[0].superseded_at is not None
    assert len(await current_for(engine, results[1], sid)) == 1


async def test_a_result_that_stays_stale_requeues_the_apply_after_the_last_round(
    logged_in_client, engine, op_runner, monkeypatch
) -> None:
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        job = await insert_job(conn)
        await published_result(conn, job, tuple_id)
    s0 = await active_scope(engine)
    saved = await save_scope(logged_in_client, radius_km=30)
    (sid,) = await scope_ids_of(engine, saved)
    op_id = saved["apply_scope_operation_id"]
    rounds = 0

    async def change_every_time(point: str) -> None:
        nonlocal rounds
        if point == scope_apply.BEFORE_FINAL_POINT:  # before every final check, the salary changes again
            rounds += 1
            async with engine.begin() as conn:
                salary = {**SALARY, "min": 60000.0 + rounds, "annual_eur_min": 60000.0 + rounds}
                digest = await set_job_facts(conn, job.job_id, salary=salary)
                await conn.execute(_REFRESH_FILTERS, {"h": digest, "j": job.job_id})

    monkeypatch.setattr(scope_apply, "afault_point", change_every_time)
    op_runner.deps = dataclasses.replace(op_runner.deps, clock=lambda: NOW)
    await op_runner.run_one(op_id)
    assert rounds == 1 + scope_apply.FINAL_CHECK_ROUNDS
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.text("SELECT status, last_error_code FROM operations WHERE id = :i"), {"i": op_id}
            )
        ).one()
    assert (row.status, row.last_error_code) == ("pending", scope_apply.INCOMPLETE_CODE)
    assert (await active_scope(engine), await pending_scope_id(engine)) == (s0, sid)  # never flipped


async def active_scope(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(_ACTIVE)).scalar_one())


# ---- the dry-run's det_recompute ---------------------------------------------------------------------------


async def test_count_published_results_active_tuple_counts_them(engine) -> None:
    async with engine.connect() as conn:
        assert await dryrun.count_published_results_active_tuple(conn) == 0  # no active tuple
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        for _ in range(3):
            await published_result(conn, await insert_job(conn), tuple_id)
        await state_target(conn, await insert_job(conn), tuple_id, "pending")  # unpublished: not counted
        chain = await insert_profile_chain(conn, 6)
        await published_result(conn, await insert_job(conn), chain.llm_tuple_id)  # another tuple's
    async with engine.connect() as conn:
        assert await dryrun.count_published_results_active_tuple(conn) == 3
        active = await dryrun.active_settings(conn)
        payload = active.to_payload()
        payload["scope"] = {**get_object(payload, "scope"), "locations": [berlin(30)]}
        result = await dryrun.dry_run(conn, payload, active=active, now=NOW)
        await conn.rollback()
    assert "scope" in result.effects and result.projections["det_recompute"] == 3
