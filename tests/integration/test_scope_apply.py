"""The scope apply (spec §13.4; plan Task 18, Deviations 12 and 40): the chunked precompute, the checked flip,
the `scope_apply_failed` reconciler and `POST /api/settings/scope/retry-apply`, on PostgreSQL.

Every job is synthetic (`job_factory.many`: an invented company per job, the active scope's results written);
every scope is the seeded one with a Berlin location at some radius (`save_scope`). The fault point
`scope_apply.after_chunk` is reached after each chunk's commit: a test replaces it to pause there, to record
the progress, or to change a job between the last chunk and the final transaction.

Every test also checks, when it ends, that each `scope_apply_*` event it logged carries ids, counts and codes
only (`scope_apply_events`, autouse); one test logs every such event.
"""

import asyncio
import dataclasses
import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.seed import ensure_hashed
from jobhunter.settings import scope_apply
from jobhunter.settings.derive import derived_seniority
from jobhunter.worker.operations import MAX_OPERATION_ATTEMPTS, cancel_operation
from jobhunter.worker.reconcilers import ReconcileContext
from jobhunter.worker.tasks import defer_in_tx, task_app
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.faults import PausePoint, no_fault, raising_at
from tests.support.reconcile import assert_fence_rule, run_reconciler
from tests.support.settings import (
    activate_profile,
    active_row,
    active_scope_id,
    change_interval,
    fail_for_good,
    generation,
    held_save,
    incomplete_jobs_for,
    operation_end,
    pending_scope_id,
    procrastinate_job_exists_for,
    save,
    save_scope,
    scope_ids_of,
    settings_body,
)

pytestmark = pytest.mark.integration

NOW: Final = datetime(2026, 10, 22, 9, 30, tzinfo=UTC)
RETRY_URL: Final = "/api/settings/scope/retry-apply"
RECONCILER: Final = "scope_apply_failed"
SCOPE_APPLY_EVENTS: Final = frozenset(
    {
        "scope_apply_flipped",
        "scope_apply_obsolete",
        "scope_apply_incomplete",
        "scope_apply_failed_cleared",
        "scope_apply_rerequested",
        "scope_apply_retry_requested",
        "scope_apply_retry_refused",
    }
)
# What every rendered event has (`request_id`: the api's own UUIDv7, bound by the edge).
_STANDARD_LOG_KEYS: Final = frozenset({"event", "level", "logger", "ts", "request_id"})
_ID_LOG_KEYS: Final = frozenset(
    {"operation_id", "scope_version_id", "pending_scope_version_id", "previous_operation_id"}
)
_RETRY_REFUSALS: Final = frozenset({"scope_active", "apply_live"})


def scope_apply_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The `scope_apply_*` events of `events`, each checked to be a known event whose fields are ids (an int,
    or null for an absent pointer), the `checks` count, or the retry refusal's `reason` code: no value of a
    payload, a scope or a job."""
    found = [e for e in events if str(e["event"]).startswith("scope_apply_")]
    for event in found:
        assert event["event"] in SCOPE_APPLY_EVENTS, event["event"]
        for key, value in event.items():
            if key in _STANDARD_LOG_KEYS:
                continue
            if key in _ID_LOG_KEYS:
                assert value is None or type(value) is int, (event["event"], key)
            elif key == "checks":
                assert type(value) is int and value >= 1, (event["event"], key)
            elif key == "reason":
                assert event["event"] == "scope_apply_retry_refused" and value in _RETRY_REFUSALS, value
            else:
                pytest.fail(f"{event['event']} logs the field {key!r}")
    return found


@pytest.fixture(autouse=True)
def _scope_apply_logs_carry_ids_and_codes_only(log_events: Any) -> Iterator[None]:
    yield
    scope_apply_events(log_events())


# --- Review Focus 5 pin -------------------------------------------------------------------------------------


async def test_newer_scope_save_supersedes_the_pending_apply(
    logged_in_client, engine, op_runner, job_factory
) -> None:
    await job_factory.many(20)  # retained jobs with det-facts
    s0 = await active_scope_id(engine)
    pause = PausePoint("scope_apply.after_chunk")  # holds the first apply after its first chunk
    first = await save_scope(logged_in_client, radius_km=30)  # pending = A, apply_scope(A)
    run_a = asyncio.create_task(op_runner.run_one(first["apply_scope_operation_id"], pause=pause))
    await asyncio.wait_for(pause.reached.wait(), 10)
    # the view keeps a complete scope
    assert await active_scope_id(engine) == s0 and await incomplete_jobs_for(engine, s0) == 0
    second = await save_scope(logged_in_client, radius_km=50)  # pending = B, apply_scope(B)
    pause.resume()
    await run_a
    await op_runner.run_one(second["apply_scope_operation_id"])
    a, b = await scope_ids_of(engine, first, second)
    assert await operation_end(engine, first["apply_scope_operation_id"]) == ("succeeded", {"obsolete": True})
    assert await active_scope_id(engine) == b and await pending_scope_id(engine) is None
    assert await incomplete_jobs_for(engine, b) == 0 and a != b


# --- helpers ------------------------------------------------------------------------------------------------


async def operation_row(engine: AsyncEngine, op_id: int) -> sa.Row[Any]:
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT * FROM operations WHERE id = :i"), {"i": op_id})).one()


async def apply_operations(engine: AsyncEngine) -> list[tuple[int, str, int]]:
    """Every `apply_scope` operation as `(id, status, payload scope)`, oldest first."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT id, status, payload FROM operations WHERE kind = 'apply_scope' ORDER BY id")
        )
        return [(int(r.id), str(r.status), int(r.payload["scope_version_id"])) for r in rows]


async def open_apply_failed_alerts(engine: AsyncEngine) -> list[tuple[str, str, dict[str, Any]]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT dedup_key, severity, context FROM alerts WHERE code = :c AND resolved_at IS NULL "
                "ORDER BY id"
            ),
            {"c": scope_apply.APPLY_FAILED_ALERT},
        )
        return [(str(r.dedup_key), str(r.severity), dict(r.context)) for r in rows]


async def hard_filter_hash(engine: AsyncEngine, job_id: int, scope_id: int) -> bytes:
    async with engine.connect() as conn:
        found = await conn.execute(
            text("SELECT inputs_hash FROM hard_filter_results WHERE job_id = :j AND scope_version_id = :s"),
            {"j": job_id, "s": scope_id},
        )
        return bytes(found.scalar_one())


async def change_det_facts(engine: AsyncEngine, job_id: int, new_hash: bytes, *, active_scope: int) -> None:
    """The job's det-facts change through a writer that refreshes only the active scope's result (the
    pending scope's row is left stale), under `active_config` `FOR SHARE` as a collection transaction."""
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1 FROM active_config WHERE id = 1 FOR SHARE"))
        await conn.execute(
            text("UPDATE jobs SET det_facts_hash = :h WHERE id = :j"), {"h": new_hash, "j": job_id}
        )
        await conn.execute(
            text(
                "UPDATE hard_filter_results SET inputs_hash = :h WHERE job_id = :j AND scope_version_id = :s"
            ),
            {"h": new_hash, "j": job_id, "s": active_scope},
        )


async def make_due(engine: AsyncEngine, op_id: int) -> None:
    """The operation's retry delay has passed (`not_before` cleared), so the runner claims it again."""
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE operations SET not_before = NULL WHERE id = :i"), {"i": op_id})


def on_fixed_clock(op_runner: Any) -> None:
    """The runner's handlers take their `now` from `NOW` (the injected `OperationDeps.clock`)."""
    op_runner.deps = dataclasses.replace(op_runner.deps, clock=lambda: NOW)


# --- the precompute and the flip ----------------------------------------------------------------------------


async def test_1201_jobs_take_three_chunks_then_the_checked_flip(
    logged_in_client, engine, op_runner, job_factory, monkeypatch
) -> None:
    await job_factory.many(1201)
    s0 = await active_scope_id(engine)
    saved = await save_scope(logged_in_client, radius_km=30)
    (sid,) = await scope_ids_of(engine, saved)
    op_id = saved["apply_scope_operation_id"]
    before = await generation(engine)
    chunks: list[tuple[str, Any, int]] = []

    async def after_chunk(point: str) -> None:
        if point == scope_apply.AFTER_CHUNK_POINT:  # after each chunk's commit, before the next
            status, progress = await operation_end(engine, op_id)
            chunks.append((status, progress, await active_scope_id(engine)))

    monkeypatch.setattr(scope_apply, "afault_point", after_chunk)
    on_fixed_clock(op_runner)
    await op_runner.run_one(op_id)
    assert chunks == [
        ("running", {"done": 500, "total": 1201, "det_done": 0, "det_total": 0}, s0),
        ("running", {"done": 1000, "total": 1201, "det_done": 0, "det_total": 0}, s0),
        ("running", {"done": 1201, "total": 1201, "det_done": 0, "det_total": 0}, s0),
    ]  # the view stays on the old, complete scope through the precompute
    assert await operation_end(engine, op_id) == (
        "succeeded",
        {"done": 1201, "total": 1201, "det_done": 0, "det_total": 0},
    )
    after = await active_row(engine)
    assert (after.scope_version_id, after.pending_scope_version_id, after.updated_at) == (sid, None, NOW)
    assert await generation(engine) == before + 1
    assert await incomplete_jobs_for(engine, sid) == 0
    assert (await settings_body(logged_in_client))["pending_scope"] is None


async def test_a_job_changed_after_the_last_chunk_is_recomputed_before_the_flip(
    logged_in_client, engine, op_runner, job_factory, monkeypatch
) -> None:
    job_ids = await job_factory.many(20)
    s0 = await active_scope_id(engine)
    saved = await save_scope(logged_in_client, radius_km=30)
    (sid,) = await scope_ids_of(engine, saved)
    changed, new_hash = job_ids[7], hashlib.sha256(b"changed det-facts").digest()
    incomplete_after_chunk: list[int] = []

    async def after_chunk(point: str) -> None:
        if point != scope_apply.AFTER_CHUNK_POINT:
            return
        incomplete_after_chunk.append(await incomplete_jobs_for(engine, sid))
        if len(incomplete_after_chunk) == 1:  # after the last (and only) chunk of the first round
            await change_det_facts(engine, changed, new_hash, active_scope=s0)
            assert await incomplete_jobs_for(engine, sid) == 1

    monkeypatch.setattr(scope_apply, "afault_point", after_chunk)
    await op_runner.run_one(saved["apply_scope_operation_id"])
    assert incomplete_after_chunk == [0, 0]  # a second round's chunk recomputed the changed job
    assert await hard_filter_hash(engine, changed, sid) == new_hash
    assert (await active_scope_id(engine), await pending_scope_id(engine)) == (sid, None)
    assert await incomplete_jobs_for(engine, sid) == 0
    assert await operation_end(engine, saved["apply_scope_operation_id"]) == (
        "succeeded",
        {"done": 20, "total": 20, "det_done": 0, "det_total": 0},
    )


async def test_a_scope_that_stays_incomplete_is_requeued_after_the_last_round(
    logged_in_client, engine, op_runner, job_factory, monkeypatch
) -> None:
    job_ids = await job_factory.many(3)
    s0 = await active_scope_id(engine)
    saved = await save_scope(logged_in_client, radius_km=30)
    (sid,) = await scope_ids_of(engine, saved)
    op_id = saved["apply_scope_operation_id"]
    chunks = 0

    async def change_every_time(point: str) -> None:
        nonlocal chunks
        if point == scope_apply.AFTER_CHUNK_POINT:  # every round's one chunk: the job changes again
            chunks += 1
            digest = hashlib.sha256(f"round {chunks}".encode()).digest()
            await change_det_facts(engine, job_ids[0], digest, active_scope=s0)

    monkeypatch.setattr(scope_apply, "afault_point", change_every_time)
    on_fixed_clock(op_runner)
    await op_runner.run_one(op_id)
    assert chunks == 1 + scope_apply.FINAL_CHECK_ROUNDS  # the first round and every re-run of the precompute
    row = await operation_row(engine, op_id)
    assert (row.status, row.last_error_code, row.attempts, row.not_before) == (
        "pending",
        "scope_incomplete",
        1,
        NOW + timedelta(minutes=1),
    )
    assert (await active_scope_id(engine), await pending_scope_id(engine)) == (s0, sid)  # never flipped


async def test_a_pointer_moved_back_keeps_the_reused_apply_and_ends_the_other_obsolete(
    logged_in_client, engine, op_runner, job_factory
) -> None:
    await job_factory.many(5)
    s0 = await active_scope_id(engine)
    first = await save_scope(logged_in_client, radius_km=30)  # pending = A
    second = await save_scope(logged_in_client, radius_km=50)  # pending = B
    third = await save_scope(logged_in_client, radius_km=30)  # pending = A again: A's live apply is reused
    assert third["apply_scope_operation_id"] == first["apply_scope_operation_id"]
    a, b = await scope_ids_of(engine, first, second)
    await op_runner.run_one(second["apply_scope_operation_id"])
    assert await operation_end(engine, second["apply_scope_operation_id"]) == (
        "succeeded",
        {"obsolete": True},
    )
    assert await incomplete_jobs_for(engine, b) == 5  # the chunk's hint sent it to its final check at once
    assert (await active_scope_id(engine), await pending_scope_id(engine)) == (s0, a)
    await op_runner.run_one(first["apply_scope_operation_id"])
    assert await operation_end(engine, first["apply_scope_operation_id"]) == (
        "succeeded",
        {"done": 5, "total": 5, "det_done": 0, "det_total": 0},
    )
    assert (await active_scope_id(engine), await pending_scope_id(engine)) == (a, None)


async def test_a_hint_never_ends_the_apply_whose_pointer_moves_back_before_its_final_check(
    logged_in_client, engine, op_runner, job_factory, monkeypatch
) -> None:
    """Ruling R18-1: A's chunk reads pointer B and only stops its precompute; the pointer moves back to A (a
    save that reuses A's live apply) before A's final transaction, which then finds A pending and incomplete,
    precomputes again and flips. An apply ended on its hint would leave A pending with no live apply."""
    await job_factory.many(5)
    first = await save_scope(logged_in_client, radius_km=30)  # pending = A
    await save_scope(logged_in_client, radius_km=50)  # pending = B
    (a,) = await scope_ids_of(engine, first)
    op_a = first["apply_scope_operation_id"]
    real_final = scope_apply._final
    between: list[tuple[int, int]] = []  # (A's incomplete jobs, the reused apply), once

    async def pointer_back_first(ctx: Any, sid: int, *, now: datetime) -> Any:
        if not between:  # after A's hint returned, before its first final transaction
            reused = await save_scope(logged_in_client, radius_km=30)  # pending = A again
            between.append((await incomplete_jobs_for(engine, a), reused["apply_scope_operation_id"]))
        return await real_final(ctx, sid, now=now)

    monkeypatch.setattr(scope_apply, "_final", pointer_back_first)
    await op_runner.run_one(op_a)
    assert between == [(5, op_a)]  # the hint stopped A before any chunk; the save reused A's live apply
    assert await operation_end(engine, op_a) == (
        "succeeded",
        {"done": 5, "total": 5, "det_done": 0, "det_total": 0},
    )
    assert (await active_scope_id(engine), await pending_scope_id(engine)) == (a, None)
    assert await incomplete_jobs_for(engine, a) == 0


async def test_a_scope_reverted_to_the_active_one_ends_its_apply_obsolete(
    logged_in_client, engine, op_runner, job_factory
) -> None:
    await job_factory.many(5)
    s0 = await active_scope_id(engine)
    before = await generation(engine)
    saved = await save_scope(logged_in_client, radius_km=30)
    payload = (await settings_body(logged_in_client))["payload"]
    payload["scope"]["locations"] = []
    await save(logged_in_client, payload)  # the revert: pending := NULL
    await op_runner.run_one(saved["apply_scope_operation_id"])
    assert await operation_end(engine, saved["apply_scope_operation_id"]) == ("succeeded", {"obsolete": True})
    assert (await active_scope_id(engine), await pending_scope_id(engine)) == (s0, None)
    assert await generation(engine) == before  # nothing flipped, nothing re-ranked


# --- permanent failure, the reconciler and retry-apply -----------------------------------------------------


async def test_a_permanently_failed_apply_is_cleared_and_retry_apply_flips_it(
    logged_in_client, engine, op_runner, job_factory, current_epoch, monkeypatch
) -> None:
    await job_factory.many(5)
    s0 = await active_scope_id(engine)
    saved = await save_scope(logged_in_client, radius_km=30)
    (sid,) = await scope_ids_of(engine, saved)
    op_id = saved["apply_scope_operation_id"]
    monkeypatch.setattr(scope_apply, "afault_point", raising_at(scope_apply.AFTER_CHUNK_POINT))
    for _ in range(MAX_OPERATION_ATTEMPTS):  # the handler always raises: requeued as `bug` each time
        await op_runner.run_one(op_id)
        await make_due(engine, op_id)
    row = await operation_row(engine, op_id)
    assert (row.status, row.last_error_code) == ("failed", "attempts_exhausted")
    assert (await active_scope_id(engine), await pending_scope_id(engine)) == (
        s0,
        sid,
    )  # left for the reconciler

    assert await run_reconciler(RECONCILER, engine, now=NOW, epoch=current_epoch) == 1
    after = await active_row(engine)
    assert (after.scope_version_id, after.pending_scope_version_id, after.updated_at) == (s0, None, NOW)
    assert await open_apply_failed_alerts(engine) == [
        (f"scope.apply_failed:scope:{sid}", "critical", {"scope_version_id": sid, "operation_id": op_id})
    ]
    assert await run_reconciler(RECONCILER, engine, now=NOW, epoch=current_epoch) == 0  # nothing pending

    retry = await logged_in_client.post(RETRY_URL)
    assert retry.status_code == 202, retry.text
    new_op = retry.json()["operation_id"]
    assert set(retry.json()) == {"operation_id"} and new_op != op_id
    assert await apply_operations(engine) == [(op_id, "failed", sid), (new_op, "pending", sid)]
    assert await pending_scope_id(engine) == sid and await procrastinate_job_exists_for(engine, new_op)
    monkeypatch.setattr(scope_apply, "afault_point", no_fault)
    await op_runner.run_one(new_op)
    assert (await active_scope_id(engine), await pending_scope_id(engine)) == (sid, None)
    assert await open_apply_failed_alerts(engine) == []  # resolved by the successful apply


async def test_a_scope_revert_after_a_permanent_failure_resolves_the_alert(
    logged_in_client, engine, current_epoch
) -> None:
    """§13.4 step 5's second remedy: reverting the scope fields (a new settings version whose scope is the
    active one) leaves nothing unapplied, so the save resolves `scope.apply_failed`."""
    s0 = await active_scope_id(engine)
    saved = await save_scope(logged_in_client, radius_km=30)
    (sid,) = await scope_ids_of(engine, saved)
    await fail_for_good(engine, saved["apply_scope_operation_id"])
    assert await run_reconciler(RECONCILER, engine, now=NOW, epoch=current_epoch) == 1
    assert [key for key, _, _ in await open_apply_failed_alerts(engine)] == [
        f"scope.apply_failed:scope:{sid}"
    ]
    payload = (await settings_body(logged_in_client))["payload"]
    payload["scope"]["locations"] = []
    reverted = await save(logged_in_client, payload)
    assert "scope" in reverted["version"]["effect_classes"] and reverted["apply_scope_operation_id"] is None
    assert await open_apply_failed_alerts(engine) == []
    assert (await active_scope_id(engine), await pending_scope_id(engine)) == (s0, None)
    assert await scope_ids_of(engine, reverted) == [s0]


async def test_a_save_that_leaves_the_scope_unapplied_keeps_the_alert(
    logged_in_client, engine, current_epoch
) -> None:
    """The alert stays while the active version's scope is still not the active one: a save of another
    class, and a save of yet another scope (its own apply now pending), resolve nothing."""
    saved = await save_scope(logged_in_client, radius_km=30)
    (sid,) = await scope_ids_of(engine, saved)
    await fail_for_good(engine, saved["apply_scope_operation_id"])
    assert await run_reconciler(RECONCILER, engine, now=NOW, epoch=current_epoch) == 1
    payload = (await settings_body(logged_in_client))["payload"]
    payload["display"]["ui_language"] = "de" if payload["display"]["ui_language"] != "de" else "en"
    assert "scope" not in (await save(logged_in_client, payload))["version"]["effect_classes"]
    await save_scope(logged_in_client, radius_km=60)
    assert [key for key, _, _ in await open_apply_failed_alerts(engine)] == [
        f"scope.apply_failed:scope:{sid}"
    ]


async def test_retry_apply_is_409_while_an_apply_is_live_or_when_the_scope_is_active(
    logged_in_client, engine
) -> None:
    active = await active_scope_id(engine)
    refused = await logged_in_client.post(RETRY_URL)  # the active settings version's scope is the active one
    assert refused.status_code == 409 and refused.json()["code"] == "conflict_state", refused.text
    saved = await save_scope(logged_in_client, radius_km=30)
    (sid,) = await scope_ids_of(engine, saved)
    refused = await logged_in_client.post(RETRY_URL)  # its apply is live
    assert refused.status_code == 409 and refused.json()["code"] == "conflict_state", refused.text
    assert await apply_operations(engine) == [(saved["apply_scope_operation_id"], "pending", sid)]
    assert (await active_scope_id(engine), await pending_scope_id(engine)) == (active, sid)


async def test_retry_apply_queued_behind_a_settings_save_reads_that_save(
    logged_in_client, engine, current_epoch
) -> None:
    """Task 22 fix round 1: a retry that waits at `active_config` while a save is open meets the version the
    save inserts, not the snapshot of its own start (which lacks it, so a joined lock statement found no row
    and the route answered 500). Here it is 202: the save changes no scope, and the failed apply of the active
    version's scope was cleared."""
    s0 = await active_scope_id(engine)
    saved = await save_scope(logged_in_client, radius_km=30)
    (sid,) = await scope_ids_of(engine, saved)
    await fail_for_good(engine, saved["apply_scope_operation_id"])
    assert await run_reconciler(RECONCILER, engine, now=NOW, epoch=current_epoch) == 1
    assert (await active_scope_id(engine), await pending_scope_id(engine)) == (s0, None)

    async with held_save(engine, logged_in_client, change_interval) as held:
        request = asyncio.create_task(logged_in_client.post(RETRY_URL))
        await asyncio.sleep(0.4)
        assert not request.done()
        await held.commit()
    retry = await asyncio.wait_for(request, timeout=10)
    assert retry.status_code == 202, retry.text
    assert await pending_scope_id(engine) == sid
    assert (await active_row(engine)).settings_version_id == held.version_id
    assert [row[1:] for row in await apply_operations(engine)] == [("failed", sid), ("pending", sid)]


async def test_retry_apply_queued_behind_a_settings_save_is_409_when_the_scope_is_active(
    logged_in_client, engine
) -> None:
    async with held_save(engine, logged_in_client, change_interval) as held:
        request = asyncio.create_task(logged_in_client.post(RETRY_URL))
        await asyncio.sleep(0.4)
        assert not request.done()
        await held.commit()
    refused = await asyncio.wait_for(request, timeout=10)
    assert (refused.status_code, refused.json()["code"]) == (409, "conflict_state"), refused.text
    assert await apply_operations(engine) == []


async def test_retry_apply_derives_the_scope_with_the_active_profile(
    logged_in_client, engine, current_epoch
) -> None:
    """The seeded range is `profile_relative`: the retried scope is the one the active settings version gives
    with the profile active now (here a newer one than the failed apply's), as a save derives it."""
    await activate_profile(engine, seniority="senior")
    saved = await save_scope(logged_in_client, radius_km=30)
    (failed_scope,) = await scope_ids_of(engine, saved)
    await fail_for_good(engine, saved["apply_scope_operation_id"])
    assert await run_reconciler(RECONCILER, engine, now=NOW, epoch=current_epoch) == 1
    await activate_profile(engine, seniority="junior")  # a newer profile; its scope part is not done here
    retry = await logged_in_client.post(RETRY_URL)
    assert retry.status_code == 202, retry.text
    retried = await pending_scope_id(engine)
    assert retried is not None and retried != failed_scope
    assert await apply_operations(engine) == [
        (saved["apply_scope_operation_id"], "failed", failed_scope),
        (retry.json()["operation_id"], "pending", retried),
    ]
    low, high = derived_seniority("junior", below=1, above=1)
    async with engine.connect() as conn:
        payload = (
            await conn.execute(text("SELECT payload FROM scope_versions WHERE id = :i"), {"i": retried})
        ).scalar_one()
    assert payload["seniority_range"] == {"mode": "explicit", "below": 1, "above": 1, "min": low, "max": high}


async def test_the_reconciler_leaves_a_pointer_whose_apply_is_live(
    logged_in_client, engine, current_epoch
) -> None:
    saved = await save_scope(logged_in_client, radius_km=30)
    (sid,) = await scope_ids_of(engine, saved)
    assert await run_reconciler(RECONCILER, engine, now=NOW, epoch=current_epoch) == 0
    assert await apply_operations(engine) == [(saved["apply_scope_operation_id"], "pending", sid)]
    assert await pending_scope_id(engine) == sid and await open_apply_failed_alerts(engine) == []


async def test_the_reconciler_rerequests_the_apply_of_a_pointer_whose_apply_ended_without_failing(
    logged_in_client, engine, current_epoch, log_events
) -> None:
    """Ruling R18-2: the pointer names a scope whose newest apply ended, not `failed` (here `cancelled`), with
    none live: the apply is requested again under the same lock, and logged with ids only."""
    saved = await save_scope(logged_in_client, radius_km=30)
    (sid,) = await scope_ids_of(engine, saved)
    op_id = saved["apply_scope_operation_id"]
    async with engine.begin() as conn:
        assert await cancel_operation(conn, op_id, reason="test_cancelled")
    assert await run_reconciler(RECONCILER, engine, now=NOW, epoch=current_epoch) == 1
    (_, (new_op, status, scope)) = await apply_operations(engine)
    assert (status, scope) == ("pending", sid) and await procrastinate_job_exists_for(engine, new_op)
    assert await pending_scope_id(engine) == sid and await open_apply_failed_alerts(engine) == []
    (event,) = [e for e in log_events() if e["event"] == "scope_apply_rerequested"]
    assert event["level"] == "warning"
    assert set(event) - {"event", "level", "logger", "ts"} == {
        "scope_version_id",
        "operation_id",
        "previous_operation_id",
    }  # ids only
    assert (event["scope_version_id"], event["operation_id"], event["previous_operation_id"]) == (
        sid,
        new_op,
        op_id,
    )
    assert await run_reconciler(RECONCILER, engine, now=NOW, epoch=current_epoch) == 0  # now it is live


async def test_the_reconciler_requests_an_apply_for_a_pointer_that_never_had_one(
    engine, current_epoch
) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE"))
        other = await ensure_hashed(conn, "scope_versions", {"test": "a pointer without an apply"})
        await conn.execute(text("UPDATE active_config SET pending_scope_version_id = :p"), {"p": other})
    assert await run_reconciler(RECONCILER, engine, now=NOW, epoch=current_epoch) == 1
    ((new_op, status, scope),) = await apply_operations(engine)
    assert (status, scope) == ("pending", other) and await procrastinate_job_exists_for(engine, new_op)
    assert await pending_scope_id(engine) == other


async def test_the_scope_apply_failed_reconciler_follows_the_fence_rule(
    logged_in_client, engine, current_epoch
) -> None:
    saved = await save_scope(logged_in_client, radius_km=30)
    (sid,) = await scope_ids_of(engine, saved)
    await fail_for_good(engine, saved["apply_scope_operation_id"])  # an unfenced pass would clear the pointer
    current = ReconcileContext(
        engine=engine, queue=task_app(), defer=defer_in_tx, now=utcnow(), epoch=current_epoch
    )
    superseded = dataclasses.replace(current, epoch=current_epoch + 1)
    await assert_fence_rule(RECONCILER, "tick", superseded, current, scope_apply)
    assert (
        await pending_scope_id(engine) == sid and await open_apply_failed_alerts(engine) == []
    )  # nothing written
    assert await run_reconciler(RECONCILER, engine, now=NOW, epoch=current_epoch) == 1
    assert await pending_scope_id(engine) is None


async def test_the_reconciler_takes_no_active_config_lock_when_nothing_is_pending(
    engine, current_epoch
) -> None:
    """With no pending scope a tick's pass ends on its plain read: it does not queue behind a transaction that
    holds `active_config` `FOR SHARE` (a publish, a collection transaction) while it holds the epoch fence."""
    async with engine.connect() as holder:
        await holder.execute(text("SELECT 1 FROM active_config WHERE id = 1 FOR SHARE"))
        assert (
            await asyncio.wait_for(run_reconciler(RECONCILER, engine, now=NOW, epoch=current_epoch), 5) == 0
        )
        await holder.rollback()


async def test_every_scope_apply_event_carries_ids_and_codes_only(
    logged_in_client, engine, op_runner, job_factory, current_epoch, monkeypatch, log_events
) -> None:
    job_ids = await job_factory.many(3)
    s0 = await active_scope_id(engine)
    assert (await logged_in_client.post(RETRY_URL)).status_code == 409  # retry refused: the scope is active
    first = await save_scope(logged_in_client, radius_km=30)
    second = await save_scope(logged_in_client, radius_km=50)
    await op_runner.run_one(first["apply_scope_operation_id"])  # obsolete
    rounds = 0

    async def change_every_round(point: str) -> None:
        nonlocal rounds
        if point == scope_apply.AFTER_CHUNK_POINT:
            rounds += 1
            digest = hashlib.sha256(f"events round {rounds}".encode()).digest()
            await change_det_facts(engine, job_ids[0], digest, active_scope=s0)

    monkeypatch.setattr(scope_apply, "afault_point", change_every_round)
    op_b = second["apply_scope_operation_id"]
    await op_runner.run_one(op_b)  # incomplete: requeued
    monkeypatch.setattr(scope_apply, "afault_point", no_fault)
    await make_due(engine, op_b)
    await op_runner.run_one(op_b)  # flipped
    third = await save_scope(logged_in_client, radius_km=70)
    await fail_for_good(engine, third["apply_scope_operation_id"])
    assert await run_reconciler(RECONCILER, engine, now=NOW, epoch=current_epoch) == 1  # failed, cleared
    retry = await logged_in_client.post(RETRY_URL)  # retry requested
    assert retry.status_code == 202, retry.text
    async with engine.begin() as conn:
        assert await cancel_operation(conn, retry.json()["operation_id"], reason="test_cancelled")
    assert await run_reconciler(RECONCILER, engine, now=NOW, epoch=current_epoch) == 1  # re-requested
    events = scope_apply_events(log_events())
    assert {e["event"] for e in events} == SCOPE_APPLY_EVENTS
    rendered = str(log_events()).lower()
    assert "berlin" not in rendered and "musterfirma" not in rendered and "geonames" not in rendered
