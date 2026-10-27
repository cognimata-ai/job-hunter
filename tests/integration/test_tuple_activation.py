"""The tuple activation on PostgreSQL (spec §12.6 (d)-(f) and (h), §13.3 `llm_tuple`, §10.10, §6.9.3; plan
Task 14, Review Focus 2, Deviations 27 and 38, Codex r1 #24, #35, r2 B7, B25, A1 issue 17, Rulings P14, P52):
the confirm's and the `llm_tuple` save's `targets.activate_tuple`, and `create_targets_for_tuple` after it.

A new tuple supersedes the old one in the activation's transaction: the old tuple's unpublished targets are
cancelled (`in_progress` ones with their attempts), the cancellation is recorded on the old tuple's in-flight
submission whatever its phase and its `in_batch` target cancelled with it (the provider's cancel is the
worker's, after the commit), the old bulk operation is cancelled and a new one opened, and
`create_targets_for_tuple` is recorded with the new operation's id. Its run gives every retained current
revision a target of the new tuple, idempotently across a crash between chunks. An unchanged tuple opens
nothing. C24 stays all-or-nothing (`test_c24_c34_profile_confirm.py`).

Every datum is invented.
"""

from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db import ranking
from jobhunter.llm import fake
from jobhunter.llm.fake import StoredBatch
from jobhunter.scoring import targets
from jobhunter.settings.dryrun import active_settings
from jobhunter.settings.save import SaveRequest, save_settings
from jobhunter.worker.operations import cancel_operation, execute_operation
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.faults import no_fault, raising_at
from tests.support.m3 import (
    insert_attempt,
    insert_batch_item,
    insert_bulk_operation,
    insert_claimed,
    insert_job,
    insert_published,
    insert_submission,
)
from tests.support.scoring import (
    activate_score_tuple,
    attempt_outcomes,
    bulk_states,
    count_retained_current_revisions,
    operations_of,
    revisioned_jobs,
    state_target,
    targets_by_state,
)
from tests.support.settings import confirm_new_profile, generation

pytestmark = pytest.mark.integration

PHASES = ("preparing", "submit_started", "submit_ambiguous", "submitted", "ended")
WITH_PROVIDER_ID = ("submitted", "ended")


class Seeded:
    """Tuple A's work: its targets in every state the activation meets, its bulk operation and the one
    in-flight submission (`batch_one_in_flight`), in the parametrised phase."""

    def __init__(self, tuple_id: int, bulk_id: int, submission_id: int, provider_id: str | None) -> None:
        self.tuple_id = tuple_id
        self.bulk_id = bulk_id
        self.submission_id = submission_id
        self.provider_id = provider_id


async def seed(engine: AsyncEngine, phase: str) -> Seeded:
    now = utcnow()
    provider_id = f"msgbatch_t14_{phase}" if phase in WITH_PROVIDER_ID else None
    async with engine.begin() as conn:
        tuple_a = await activate_score_tuple(conn)
        scope = int((await conn.execute(sa.text("SELECT scope_version_id FROM active_config"))).scalar_one())
        bulk_a = await insert_bulk_operation(conn, tuple_a)
        for _ in range(3):
            await state_target(conn, await insert_job(conn), tuple_a, "pending", bulk_operation_id=bulk_a)
        await insert_claimed(conn, tuple_a, bulk_operation_id=bulk_a)
        await insert_published(conn, await insert_job(conn), tuple_a, bulk_operation_id=bulk_a)
        await state_target(
            conn, await insert_job(conn), tuple_a, "filtered_out", filter_scope_version_id=scope
        )
        submission = await insert_submission(
            conn,
            tuple_a,
            bulk_a,
            state=phase,
            submit_started_at=None if phase == "preparing" else now,
            provider_batch_id=provider_id,
        )
        batched = await state_target(
            conn,
            await insert_job(conn),
            tuple_a,
            "in_batch",
            batch_submission_id=submission,
            bulk_operation_id=bulk_a,
            attempt_no=1,
            lane="batch",
        )
        await insert_attempt(conn, batched, mode="batch", lane="batch")
        await insert_batch_item(conn, submission, batched)
    if provider_id is not None:  # the provider's batch, as the fake stores a created one
        fake.state().batches[provider_id] = StoredBatch(
            id=provider_id,
            seq=1,
            created_at=now,
            purpose="generate",
            requests={f"t{batched}_a1": {}},
            keys={f"t{batched}_a1": f"generate:{batched}:0"},
        )
    return Seeded(tuple_a, bulk_a, submission, provider_id)


async def scalar(engine: AsyncEngine, sql: str, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(sa.text(sql), params)).scalar()


async def active_bulk_id(engine: AsyncEngine) -> int:
    return int(await scalar(engine, "SELECT id FROM bulk_operations WHERE state = 'active'"))


async def run_operation(engine: AsyncEngine, op_id: int, epoch: int) -> Any:
    await execute_operation(engine, op_id, epoch=epoch)
    async with engine.connect() as conn:
        found = sa.text("SELECT status, progress, last_error_code FROM operations WHERE id = :i")
        return (await conn.execute(found, {"i": op_id})).one()


@pytest.mark.parametrize("phase", PHASES)
async def test_tuple_activation_cancels_old_work_and_opens_the_new_bulk_operation(
    engine: AsyncEngine, current_epoch: int, queue_app: object, phase: str
) -> None:
    # tuple A: 3 pending, 1 in_progress, 1 published, 1 filtered_out, 1 in_batch in the ONE in-flight
    # submission, whose phase is the parameter (batch_one_in_flight allows a single non-terminal one)
    seeded = await seed(engine, phase)
    tuple_a = seeded.tuple_id
    version_b, tuple_b = await confirm_new_profile(engine)
    assert tuple_b != tuple_a and version_b > 0
    assert await targets_by_state(engine, tuple_a) == {"cancelled": 6, "published_accepted": 1}
    assert await attempt_outcomes(engine, tuple_a) == {"cancelled": 2, "published_accepted": 1}
    # recorded whatever the phase (Codex r1 #24); for submit_ambiguous this record is the assertion
    cancel_requested = await scalar(
        engine, "SELECT cancel_requested_at FROM batch_submissions WHERE id = :s", s=seeded.submission_id
    )
    assert cancel_requested is not None
    assert (
        await scalar(engine, "SELECT state FROM batch_submissions WHERE id = :s", s=seeded.submission_id)
        == phase
    )
    reasons = await scalar(
        engine,
        "SELECT array_agg(DISTINCT state_reason) FROM score_targets WHERE llm_tuple_id = :t "
        "AND state = 'cancelled'",
        t=tuple_a,
    )
    assert reasons == ["tuple_superseded"]
    assert await bulk_states(engine) == {tuple_a: "cancelled", tuple_b: "active"}
    if seeded.provider_id is not None:  # the worker's batch_poll calls cancel after the commit (Task 17)
        assert fake.state().batches[seeded.provider_id].cancel_requested is False
    (op,) = await operations_of(engine, "create_targets_for_tuple")
    assert op.payload == {"llm_tuple_id": tuple_b, "bulk_operation_id": await active_bulk_id(engine)}
    ended = await run_operation(engine, op.id, current_epoch)
    assert ended.status == "succeeded"
    assert await targets_by_state(engine, tuple_b) == {
        "pending": await count_retained_current_revisions(engine)
    }
    assert (
        await scalar(
            engine,
            "SELECT count(*) FROM score_targets WHERE llm_tuple_id = :t AND bulk_operation_id = :b",
            t=tuple_b,
            b=await active_bulk_id(engine),
        )
        == 7
    )


async def test_a_crash_between_chunks_resumes_idempotently(
    engine: AsyncEngine, current_epoch: int, queue_app: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = await revisioned_jobs(engine, 600)
    _, tuple_b = await confirm_new_profile(engine)
    (op,) = await operations_of(engine, "create_targets_for_tuple")
    monkeypatch.setattr(targets, "afault_point", raising_at("create_targets.after_chunk"))
    crashed = await run_operation(engine, op.id, current_epoch)
    assert (crashed.status, crashed.last_error_code) == ("pending", "bug")  # the crash after chunk 1
    assert crashed.progress == {"done": 500, "total": 600}
    assert await targets_by_state(engine, tuple_b) == {"pending": 500}
    monkeypatch.setattr(targets, "afault_point", no_fault)
    async with engine.begin() as conn:  # the retry is due now
        await conn.execute(sa.text("UPDATE operations SET not_before = NULL WHERE id = :i"), {"i": op.id})
    resumed = await run_operation(engine, op.id, current_epoch)
    assert (resumed.status, resumed.progress) == ("succeeded", {"done": 600, "total": 600})
    async with engine.connect() as conn:
        per_job = (
            await conn.execute(
                sa.text(
                    "SELECT job_id, count(*) AS n FROM score_targets WHERE llm_tuple_id = :t GROUP BY job_id"
                ),
                {"t": tuple_b},
            )
        ).all()
    assert sorted(int(r.job_id) for r in per_job) == [job.job_id for job in jobs]
    assert {int(r.n) for r in per_job} == {1}  # exactly one target per job


async def test_an_unchanged_tuple_opens_no_bulk_operation_and_records_no_operation(
    engine: AsyncEngine, queue_app: object
) -> None:
    # L4786: the same profile and policy is the same tuple, whose every target exists already
    await revisioned_jobs(engine, 2)
    _, tuple_b = await confirm_new_profile(engine, seniority="lead")
    (first,) = await operations_of(engine, "create_targets_for_tuple")
    async with engine.begin() as conn:  # ended, so a new activation would record a new one
        assert await cancel_operation(conn, first.id, reason="t14_test")
    _, again = await confirm_new_profile(engine, seniority="lead")
    assert again == tuple_b
    assert await scalar(engine, "SELECT count(*) FROM bulk_operations") == 1
    assert await bulk_states(engine) == {tuple_b: "active"}
    assert [op.id for op in await operations_of(engine, "create_targets_for_tuple")] == [first.id]


async def test_an_llm_tuple_save_activates_the_new_tuple_after_the_flip(
    engine: AsyncEngine, queue_app: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Codex r1 #35, r2 B7: after `_FLIP`, then `ranking.bump` last
    seeded = await seed(engine, "submitted")
    generation_before = await generation(engine)
    calls: list[str] = []
    activate, bump = targets.activate_tuple, ranking.bump

    async def spy_activate(conn: AsyncConnection, **kwargs: Any) -> targets.Activated:
        pointer = (await conn.execute(sa.text("SELECT llm_tuple_id FROM active_config"))).scalar_one()
        calls.append("activate" if pointer == kwargs["new_tuple_id"] else "activate before the flip")
        return await activate(conn, **kwargs)

    async def spy_bump(conn: AsyncConnection) -> None:
        calls.append("bump")
        await bump(conn)

    monkeypatch.setattr(targets, "activate_tuple", spy_activate)
    monkeypatch.setattr(ranking, "bump", spy_bump)
    async with engine.begin() as conn:
        active = (
            await conn.execute(
                sa.text(
                    "SELECT v.id, v.payload FROM active_config a JOIN settings_versions v "
                    "ON v.id = a.settings_version_id WHERE a.id = 1 FOR UPDATE OF a"
                )
            )
        ).one()
        payload = dict(active.payload)
        payload["scoring"]["model_policy"]["nodes"]["generate"]["effort"] = "high"
        outcome = await save_settings(
            conn,
            SaveRequest(payload=payload, base_version_id=int(active.id), acknowledged_warnings=frozenset()),
            now=utcnow(),
            defer=defer_in_tx,
            internal=True,
        )
    assert outcome.effects == frozenset({"llm_tuple"}) and calls == ["activate", "bump"]
    tuple_b = int(await scalar(engine, "SELECT llm_tuple_id FROM active_config"))
    assert tuple_b != seeded.tuple_id
    assert await targets_by_state(engine, seeded.tuple_id) == {"cancelled": 6, "published_accepted": 1}
    assert await bulk_states(engine) == {seeded.tuple_id: "cancelled", tuple_b: "active"}
    (op,) = await operations_of(engine, "create_targets_for_tuple")
    assert op.payload == {"llm_tuple_id": tuple_b, "bulk_operation_id": await active_bulk_id(engine)}
    assert await generation(engine) == generation_before + 1


async def test_a_stale_live_creation_of_the_tuple_is_superseded_by_the_new_activation(
    engine: AsyncEngine, queue_app: object
) -> None:
    # A -> B -> A again before A's first operation ran: the live operation of A's dedup key names A's first,
    # cancelled bulk operation, so the new activation replaces it with one naming the new operation
    async with engine.begin() as conn:
        tuple_a = await activate_score_tuple(conn)
    async with engine.begin() as conn:
        await conn.execute(sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE"))
        first = await targets.activate_tuple(
            conn,
            old_tuple_id=None,
            new_tuple_id=tuple_a,
            settings=await active_settings(conn),
            now=utcnow(),
            defer=defer_in_tx,
        )
    _, tuple_b = await confirm_new_profile(engine)
    async with engine.begin() as conn:
        await conn.execute(sa.text("UPDATE active_config SET llm_tuple_id = :t WHERE id = 1"), {"t": tuple_a})
        again = await targets.activate_tuple(
            conn,
            old_tuple_id=tuple_b,
            new_tuple_id=tuple_a,
            settings=await active_settings(conn),
            now=utcnow(),
            defer=defer_in_tx,
        )
    assert again.create_operation_id != first.create_operation_id
    ops = {op.id: op for op in await operations_of(engine, "create_targets_for_tuple")}
    assert ops[first.create_operation_id].status == "cancelled"
    assert ops[again.create_operation_id].status == "pending"
    assert ops[again.create_operation_id].payload == {
        "llm_tuple_id": tuple_a,
        "bulk_operation_id": again.bulk_operation_id,
    }


async def _settings(conn: Any) -> Any:
    from jobhunter.settings.dryrun import active_settings

    return await active_settings(conn)
