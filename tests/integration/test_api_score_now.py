"""Score now, Retry and the manual score cycle over HTTP (spec §9.6.4, §14.1 L4963, §14.2 L4976, L4985,
L5028; plan Task 21, Review Focus 5, Deviation 39, Carry T19-d).

`POST /api/jobs/{job_id}/score-now` and `POST /api/targets/{target_id}/retry` (Idempotency-Key) run
`targets.score_now` and `targets.retry` after `active_config FOR SHARE` and the idempotency row, and answer
202 `{target_id, state}` or the refusal as its problem (404 `not_found`, 409 `budget_exhausted`,
`filtered_requires_force`, `conflict_state`); a synthetic golden-case job is 404 (Carry T19-d). `POST
/api/system/score-cycle` (Idempotency-Key) requests a `manual` cycle and answers 202 `{queued, reason}`.

Review Focus 5: two concurrent clicks give one override and one cycle job; while the cycle runs, a click on
its claimed target is 409 and a click on another job is served by the same cycle's next claim round.

Every datum is invented.
"""

import asyncio
import json
from typing import Any, Final

import httpx
import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.graphs.score import nodes as nodes_mod
from jobhunter.llm.gateway import LLMGateway
from jobhunter.llm.spend_ledger import BOUND_VIOLATED_ALERT
from jobhunter.observability.alerts import raise_alert
from jobhunter.scoring import targets as targets_mod
from jobhunter.scoring.targets import SyntheticJobRefused
from jobhunter.worker.tasks import job_task_name
from procrastinate.jobs import Status
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.faults import PausePoint
from tests.support.fixtures import count_rows
from tests.support.llm import FakeLLM, save_budget
from tests.support.m3 import insert_job, insert_published, insert_target
from tests.support.profile import idem
from tests.support.scoring import (
    CycleRunner,
    PendingTarget,
    activate_score_tuple,
    aged_job,
    cycle_of,
    cycle_runner,
    insert_targets,
    state_target,
    synthetic_c_draft,
    target_row,
)
from tests.support.settings import fresh_key, generation

pytestmark = pytest.mark.integration

SCORE_CYCLE: Final = job_task_name("score_cycle")
JSON: Final = {"content-type": "application/json"}
CYCLE_URL: Final = "/api/system/score-cycle"


@pytest.fixture
async def gateway(engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int) -> LLMGateway:
    return fake_llm.gateway(engine)


@pytest.fixture
async def runner(engine: AsyncEngine, gateway: LLMGateway) -> CycleRunner:
    return await cycle_runner(engine, gateway)


@pytest.fixture
async def tuple_id(engine: AsyncEngine) -> int:
    async with engine.begin() as conn:
        return await activate_score_tuple(conn)


async def score_now(
    client: httpx.AsyncClient, job_id: int, key: str, body: dict[str, Any] | None = None
) -> httpx.Response:
    return await client.post(f"/api/jobs/{job_id}/score-now", json=body or {}, headers=idem(key))


async def queued_jobs(engine: AsyncEngine, queueing_lock: str) -> int:
    """The `score_cycle` jobs of `queueing_lock` waiting to run."""
    async with engine.connect() as conn:
        found = await conn.execute(
            sa.text(
                "SELECT count(*) FROM procrastinate_jobs WHERE task_name = :t AND queueing_lock = :l "
                "AND status = 'todo'"
            ),
            {"t": SCORE_CYCLE, "l": queueing_lock},
        )
        return int(found.scalar_one())


async def run_queued_cycle(queue_app: procrastinate.App, runner: CycleRunner) -> int:
    """The worker's side of the queued `score_cycle` job: fetched (`doing`), its cycle started and run as the
    task does (`cycles.start_cycle` of its trigger, then the graph), the job finished; the cycle's id."""
    manager = queue_app.job_manager
    worker_id = await manager.register_worker()
    try:
        job = await manager.fetch_job(queues=["score"], worker_id=worker_id)
        assert job is not None and job.task_kwargs == {"trigger": "score_now"}
        cycle_id = await runner.start("score_now")
        await runner.run(cycle_id)
        await manager.finish_job(job, Status.SUCCEEDED, delete_job=False)
    finally:
        await manager.unregister_worker(worker_id)
    return cycle_id


def answer(fake_llm: FakeLLM, *made: PendingTarget) -> None:
    for target in made:
        fake_llm.answer("generate", target.job_revision_id, synthetic_c_draft())


# ---- Review Focus 5 ----------------------------------------------------------------------------------------


async def test_two_concurrent_score_now_clicks_give_one_override_and_one_cycle(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    runner: CycleRunner,
    fake_llm: FakeLLM,
    queue_app: procrastinate.App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending_target, other_target = await insert_targets(engine, 2)
    answer(fake_llm, pending_target, other_target)
    job_id, other_job_id = pending_target.job_id, other_target.job_id
    body = {"force": False}
    r1, r2 = await asyncio.gather(
        score_now(logged_in_client, job_id, "tab-1-score-now-0001", body),
        score_now(logged_in_client, job_id, "tab-2-score-now-0001", body),
    )
    assert {r1.status_code, r2.status_code} == {202}, (r1.text, r2.text)
    assert r1.json() == r2.json() == {"target_id": pending_target.target_id, "state": "pending"}
    row = await target_row(engine, pending_target.target_id)
    assert (row.override, row.lane, row.state) == ("score_now", "score_now", "pending")
    assert row.override_at is not None
    assert await queued_jobs(engine, "score_cycle:score_now") == 1  # one request, not two
    pause = PausePoint(nodes_mod.BEFORE_DRAFT)  # the cycle runs, blocked mid-target
    monkeypatch.setattr(nodes_mod, "afault_point", pause)
    cycle = asyncio.create_task(run_queued_cycle(queue_app, runner))
    await asyncio.wait_for(pause.reached.wait(), 10)
    r3 = await score_now(logged_in_client, job_id, "tab-3-score-now-0001", body)
    assert (r3.status_code, r3.json()["code"]) == (409, "conflict_state")  # its target is in_progress
    r4 = await score_now(logged_in_client, other_job_id, "tab-4-score-now-0001", body)
    assert r4.status_code == 202, r4.text  # another job: recorded, no second cycle
    pause.resume()
    cycle_id = await asyncio.wait_for(cycle, 30)
    assert await count_rows(engine, "score_cycles") == 1
    assert (await cycle_of(engine, cycle_id)).report["claim_rounds"] == 2
    for target in (pending_target, other_target):
        assert (await target_row(engine, target.target_id)).charged_attempts == 1
        assert (await target_row(engine, target.target_id)).state == "published_accepted"


# ---- the refusals ------------------------------------------------------------------------------------------


async def test_score_now_answers_each_refusal_with_its_problem(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, queue_app: procrastinate.App, tuple_id: int
) -> None:
    async with engine.begin() as conn:
        scope = int(
            (
                await conn.execute(sa.text("SELECT scope_version_id FROM active_config WHERE id = 1"))
            ).scalar_one()
        )
        filtered = await aged_job(conn)
        filtered_target = await state_target(
            conn, filtered, tuple_id, "filtered_out", filter_scope_version_id=scope
        )
        published = await aged_job(conn)
        await insert_published(conn, published, tuple_id)
        bare = await aged_job(conn)
    cases = [
        (10**12, {}, 404, "not_found"),
        (bare.job_id, {}, 404, "not_found"),  # no target of the active tuple
        (published.job_id, {}, 409, "conflict_state"),
        (filtered.job_id, {}, 409, "filtered_requires_force"),
        (filtered.job_id, {"force": False}, 409, "filtered_requires_force"),
    ]
    for job_id, body, status, code in cases:
        r = await score_now(logged_in_client, job_id, fresh_key("score-now"), body)
        assert (r.status_code, r.json()["code"]) == (status, code), (job_id, r.text)
    assert await queued_jobs(engine, "score_cycle:score_now") == 0
    r = await score_now(logged_in_client, filtered.job_id, fresh_key("score-now"), {"force": True})
    assert (r.status_code, r.json()) == (202, {"target_id": filtered_target, "state": "pending"})
    assert (await target_row(engine, filtered_target)).override == "score_anyway"


async def test_score_now_without_headroom_is_budget_exhausted(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, queue_app: procrastinate.App, tuple_id: int
) -> None:
    async with engine.begin() as conn:
        job = await aged_job(conn)
        target = await state_target(conn, job, tuple_id, "budget_deferred")
    await save_budget(engine, now=await db_now(engine), cap_usd="2.10")  # a scoring share below r_first
    r = await score_now(logged_in_client, job.job_id, fresh_key("score-now"))
    assert (r.status_code, r.json()["code"]) == (409, "budget_exhausted"), r.text
    assert (await target_row(engine, target)).state == "budget_deferred"


async def db_now(engine: AsyncEngine) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(sa.text("SELECT now()"))).scalar_one()


async def test_score_now_on_a_synthetic_eval_job_is_not_found(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    queue_app: procrastinate.App,
    tuple_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Carry T19-d: an adversarial golden case's job is never scored live, even with a target of the active
    tuple that no creator would make (`create_or_revive` refuses one, `SyntheticJobRefused`); that refusal,
    were it raised, is a 404 too."""
    async with engine.begin() as conn:
        synthetic = await insert_job(conn, synthetic_eval=True)
        target = await insert_target(conn, synthetic, tuple_id)
    before = await generation(engine)
    r = await score_now(logged_in_client, synthetic.job_id, fresh_key("score-now"))
    assert (r.status_code, r.json()["code"]) == (404, "not_found"), r.text
    assert (await target_row(engine, target)).override == "none"
    assert await generation(engine) == before and await queued_jobs(engine, "score_cycle:score_now") == 0

    async def refusing(*args: Any, **kwargs: Any) -> Any:
        raise SyntheticJobRefused(synthetic.job_id)

    monkeypatch.setattr(targets_mod, "score_now", refusing)  # the route calls it through the module
    r = await score_now(logged_in_client, synthetic.job_id, fresh_key("score-now"))
    assert (r.status_code, r.json()["code"]) == (404, "not_found"), r.text


async def test_score_now_needs_a_key_and_replays_its_stored_response(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, queue_app: procrastinate.App, tuple_id: int
) -> None:
    async with engine.begin() as conn:
        job = await aged_job(conn)
        target = await state_target(conn, job, tuple_id, "pending")
    r = await logged_in_client.post(f"/api/jobs/{job.job_id}/score-now", json={})
    assert (r.status_code, r.json()["code"]) == (400, "idempotency_key_required")
    key = fresh_key("score-now")
    first = await score_now(logged_in_client, job.job_id, key)
    assert first.status_code == 202, first.text
    moved = (await target_row(engine, target)).override_at
    async with engine.begin() as conn:  # the target moves on; a replay answers the stored response
        await conn.execute(sa.text("UPDATE score_targets SET lane = 'oldest' WHERE id = :t"), {"t": target})
    replay = await score_now(logged_in_client, job.job_id, key)
    assert (replay.status_code, replay.json()) == (202, first.json())
    row = await target_row(engine, target)
    assert (row.lane, row.override_at) == ("oldest", moved)  # nothing ran again
    other = await score_now(logged_in_client, job.job_id, key, {"force": True})
    assert (other.status_code, other.json()["code"]) == (422, "idempotency_key_reused")


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/jobs/1/score-now", {"force": "\ud800"}),
        ("/api/jobs/1/score-now", {"force": False, "note\ud800": 1}),
        ("/api/jobs/1/score-now", {"force": False, "note": "x"}),
        ("/api/batches/1/release", {"confirm": "\u0000"}),
        ("/api/batches/1/release", {"confirm": True, "x\ud800": 1}),
        ("/api/settings/rubric-versions/1/activate", {"expected_rescore_ack": True, "x\ud800": 1}),
    ],
)
async def test_an_invalid_body_of_an_idempotent_route_is_a_422_never_a_500(
    logged_in_client: httpx.AsyncClient, path: str, body: dict[str, Any]
) -> None:
    r = await logged_in_client.post(path, content=json.dumps(body), headers={**JSON, **idem(fresh_key())})
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed"), r.text


# ---- Retry -------------------------------------------------------------------------------------------------


async def test_retry_moves_a_failed_target_and_refuses_any_other(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, queue_app: procrastinate.App, tuple_id: int
) -> None:
    async with engine.begin() as conn:
        failed = await state_target(
            conn, await aged_job(conn), tuple_id, "failed_request", charged_attempts=3
        )
        pending = await state_target(conn, await aged_job(conn), tuple_id, "pending")
    r = await logged_in_client.post(f"/api/targets/{failed}/retry", headers=idem(fresh_key("retry")))
    assert (r.status_code, r.json()) == (202, {"target_id": failed, "state": "pending"}), r.text
    row = await target_row(engine, failed)
    assert (row.state, row.charged_attempts, row.override, row.lane) == (
        "pending",
        0,
        "score_now",
        "score_now",
    )
    assert await queued_jobs(engine, "score_cycle:score_now") == 1
    r = await logged_in_client.post(f"/api/targets/{pending}/retry", headers=idem(fresh_key("retry")))
    assert (r.status_code, r.json()["code"]) == (409, "conflict_state")
    r = await logged_in_client.post(f"/api/targets/{10**12}/retry", headers=idem(fresh_key("retry")))
    assert (r.status_code, r.json()["code"]) == (404, "not_found")
    r = await logged_in_client.post(f"/api/targets/{failed}/retry")
    assert (r.status_code, r.json()["code"]) == (400, "idempotency_key_required")


# ---- the manual score cycle --------------------------------------------------------------------------------


async def test_the_manual_cycle_is_requested_once_and_says_why_nothing_would_happen(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, queue_app: procrastinate.App
) -> None:
    """Deviation 39: `queued` is false while a `manual` job already waits; `reason` is the pause reason, or
    `no_pending` without an admissible target (no tuple included), read without a lock."""
    r = await logged_in_client.post(CYCLE_URL, headers=idem(fresh_key("cycle")))
    assert (r.status_code, r.json()) == (202, {"queued": True, "reason": "no_pending"}), r.text
    assert await queued_jobs(engine, "score_cycle:manual") == 1
    key = fresh_key("cycle")
    r = await logged_in_client.post(CYCLE_URL, headers=idem(key))
    assert (r.status_code, r.json()) == (202, {"queued": False, "reason": "no_pending"})
    assert await queued_jobs(engine, "score_cycle:manual") == 1
    replay = await logged_in_client.post(CYCLE_URL, headers=idem(key))
    assert (replay.status_code, replay.json()) == (202, r.json())
    async with engine.begin() as conn:
        await conn.execute(sa.text("DELETE FROM procrastinate_jobs WHERE task_name = :t"), {"t": SCORE_CYCLE})
    (target,) = await insert_targets(engine, 1)
    r = await logged_in_client.post(CYCLE_URL, headers=idem(fresh_key("cycle")))
    # this process is a live one and nothing verified the active schema pair
    assert (r.status_code, r.json()) == (202, {"queued": True, "reason": "schema_unverified"})
    async with engine.begin() as conn:
        await raise_alert(
            conn,
            code=BOUND_VIOLATED_ALERT,
            severity="critical",
            subject=f"target:{target.target_id}",
            message="test",
            context={},
        )
    r = await logged_in_client.post(CYCLE_URL, headers=idem(fresh_key("cycle")))
    assert (r.status_code, r.json()) == (202, {"queued": False, "reason": "reservation_bound_violated"})
    r = await logged_in_client.post(CYCLE_URL)
    assert (r.status_code, r.json()["code"]) == (400, "idempotency_key_required")
