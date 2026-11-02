"""The claim heartbeats of a score cycle on PostgreSQL (spec §9.4.3 L3572; plan Task 10, Codex r1 #12, #27,
r2 B5, B6, r3 B18, Rulings P10, P46): `graphs.score.claims.ClaimHeartbeats`, its unit behaviour (the three
graph-level cases need Task 11's `run_target` and cycle, and are Task 11's).

The beat updates the claim's row every period on the injected clock and only then; a refused beat marks the
claim lost and cancels the child attached to it, whose stream the gateway settles `ambiguous`; a beat refused
before any attachment cancels nothing; a beat that waited on the owner's own publish ends quietly; up to three
raising beats in a row keep the claim and the fourth loses it; `stop` and `stop_all` end the beats; the beat
passes the fault point `claims.beat` first. Then the follow-up hold of a target is released once, by its
terminal attempt only.

Every datum is invented.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from jobhunter.graphs.score import claims as claims_mod
from jobhunter.graphs.score.claims import ClaimHeartbeats
from jobhunter.graphs.score.deps import ScoreDeps
from jobhunter.graphs.score.nodes import publish as publish_node
from jobhunter.llm import fake
from jobhunter.llm.prompts.render import prompt_schema_v2
from jobhunter.scoring import publish as publish_mod
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.clock import FakeClock, ManualSleep
from tests.support.faults import PausePoint
from tests.support.llm import FakeLLM, tries
from tests.support.logs import logged
from tests.support.m3 import NOW, insert_batch_item, insert_bulk_operation, insert_submission
from tests.support.pg import PgServer, lock_wait_details
from tests.support.procs import await_until
from tests.support.scoring import (
    ClaimedTarget,
    activate_test_policy,
    claim_again,
    claimed_target,
    invoke_subgraph,
    reclaim_now,
    score_deps,
    score_llm_tuple,
    synthetic_c_draft,
    target_of,
)

pytestmark = pytest.mark.integration
PERIOD = 30.0


@pytest.fixture
async def deps(engine: AsyncEngine, fake_llm: FakeLLM, clean_db: None) -> ScoreDeps:
    return await score_deps(engine, fake_llm.gateway(engine))


def heartbeats(engine: AsyncEngine, sleep: ManualSleep) -> ClaimHeartbeats:
    return ClaimHeartbeats(engine, period_s=PERIOD, monotonic=FakeClock(100.0), sleep=sleep)


async def backdate(engine: AsyncEngine, target_id: int) -> None:
    """The claim's heartbeat an hour old, so a beat shows."""
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE score_targets SET claim_heartbeat_at = now() - interval '1 hour' WHERE id = :t"),
            {"t": target_id},
        )


async def fresh(engine: AsyncEngine, target_id: int) -> bool:
    """Whether the claim beat in the last minute."""
    async with engine.connect() as conn:
        found = await conn.execute(
            sa.text(
                "SELECT claim_heartbeat_at > now() - interval '1 minute' FROM score_targets WHERE id = :t"
            ),
            {"t": target_id},
        )
        return bool(found.scalar_one())


def beat_tasks(target_id: int) -> list[asyncio.Task[Any]]:
    return [t for t in asyncio.all_tasks() if t.get_name() == f"heartbeat:claim:{target_id}" and not t.done()]


async def reclaimed(engine: AsyncEngine, claimed: ClaimedTarget) -> None:
    """A rival takes the claim: the reclaim of L3572 and a new claim (the heartbeat's predicate fails)."""
    await reclaim_now(engine, claimed.target_id)
    await claim_again(engine, claimed.target_id)


async def test_the_beat_updates_the_claim_every_period_on_the_injected_clock(
    engine: AsyncEngine, deps: ScoreDeps
) -> None:
    claimed = await claimed_target(engine)
    sleep = ManualSleep()
    claims = heartbeats(engine, sleep)
    await backdate(engine, claimed.target_id)
    claims.start(claimed.ref)
    await sleep.until(1)
    assert sleep.requested == [PERIOD] and not await fresh(engine, claimed.target_id)  # no beat before
    for beats in (1, 2, 3):
        await sleep.release()  # one period passes
        await sleep.until(beats + 1)  # the beat is done and the loop sleeps again
        assert await fresh(engine, claimed.target_id), beats
        await backdate(engine, claimed.target_id)
    assert sleep.requested == [PERIOD] * 4
    assert not claims.lost(claimed.ref)
    await claims.stop(claimed.ref)
    assert beat_tasks(claimed.target_id) == []


async def test_heartbeat_loss_cancels_the_attached_child(
    engine: AsyncEngine, deps: ScoreDeps, fake_llm: FakeLLM
) -> None:
    # Codex r1 #12, r2 B6: a stalling stream held open in a child task attached to the claim
    _, schemas = prompt_schema_v2()
    generator = schemas["generator"]
    assert isinstance(generator, dict)
    fake_llm.record("generate", "claude-sonnet-5-5", synthetic_c_draft(), schema=generator)
    claimed = await claimed_target(engine)
    fake.answer(
        fake.answer_key("generate", {"job_revision_id": claimed.job_revision_id}, 0),
        "stall_after_first_event",
    )
    sleep = ManualSleep()
    claims = heartbeats(engine, sleep)
    claims.start(claimed.ref)
    before = asyncio.all_tasks()
    child: asyncio.Task[Any] = asyncio.create_task(invoke_subgraph(deps, claimed))
    claims.attach(claimed.ref, child)
    for _ in range(500):  # the stream is open
        if fake_llm.served:
            break
        await asyncio.sleep(0.01)
    assert fake_llm.served == 1
    await reclaimed(engine, claimed)
    await sleep.release()  # the next beat updates no row
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(child, 10)
    assert claims.lost(claimed.ref) and child.cancelled()
    left = asyncio.all_tasks() - before - {asyncio.current_task()}
    left = {t for t in left if not t.get_name().startswith("heartbeat:")}
    if left:
        _, pending = await asyncio.wait(left, timeout=10)
        assert not pending
    (row,) = await tries(engine)
    assert (row.state, row.status, row.error_code) == ("ambiguous", "ambiguous", "cancelled")
    await claims.stop_all()


async def test_a_beat_refused_before_any_attach_marks_the_claim_lost_and_cancels_nothing(
    engine: AsyncEngine, deps: ScoreDeps
) -> None:
    claimed = await claimed_target(engine)
    bystander = asyncio.create_task(asyncio.sleep(30))
    sleep = ManualSleep()
    claims = heartbeats(engine, sleep)
    claims.start(claimed.ref)
    await sleep.until(1)
    await reclaimed(engine, claimed)
    await sleep.release()
    for _ in range(500):
        if claims.lost(claimed.ref):
            break
        await asyncio.sleep(0.01)
    assert claims.lost(claimed.ref) and not bystander.done()
    assert beat_tasks(claimed.target_id) == []  # the loop ended with the loss
    late: asyncio.Task[Any] = asyncio.create_task(asyncio.sleep(30))
    claims.attach(claimed.ref, late)  # a child attached after the loss is cancelled at once
    with pytest.raises(asyncio.CancelledError):
        await late
    bystander.cancel()
    await claims.stop_all()


async def test_a_beat_blocked_by_its_own_publish_ends_quietly_after_the_commit(
    engine: AsyncEngine,
    deps: ScoreDeps,
    fake_llm: FakeLLM,
    migrated: PgServer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The beat waits on the target row the owner's publish holds FOR UPDATE and finds no `in_progress` row
    # after the commit: the attempt ended with its own outcome, so the beat stops and cancels nothing
    caplog.set_level(logging.INFO)
    claimed = await claimed_target(engine)
    fake_llm.answer("generate", claimed.job_revision_id, synthetic_c_draft())
    before_commit = PausePoint(publish_mod.BEFORE_COMMIT)
    after_commit = PausePoint(publish_mod.AFTER_COMMIT)
    monkeypatch.setattr(publish_mod, "afault_point", before_commit)
    monkeypatch.setattr(publish_node, "afault_point", after_commit)
    sleep = ManualSleep()
    claims = heartbeats(engine, sleep)
    claims.start(claimed.ref)
    child: asyncio.Task[Any] = asyncio.create_task(invoke_subgraph(deps, claimed))
    claims.attach(claimed.ref, child)
    await asyncio.wait_for(before_commit.reached.wait(), 10)  # the publish holds the target FOR UPDATE
    await sleep.release()  # the beat's UPDATE waits on that row

    async def beat_waits() -> bool:
        return any(wait.relation == "score_targets" for wait in lock_wait_details(migrated))

    await await_until(beat_waits, 10, 0.02)
    before_commit.resume()  # the publish commits
    await asyncio.wait_for(after_commit.reached.wait(), 10)  # the child is past its commit, still running

    async def beat_ended() -> bool:
        return beat_tasks(claimed.target_id) == []

    await await_until(beat_ended, 10, 0.02)
    assert not claims.lost(claimed.ref) and not child.done()
    after_commit.resume()
    final = await asyncio.wait_for(child, 10)
    assert final["outcome"] == "published_accepted" and not child.cancelled()
    [ended] = logged(caplog, "score_claim_heartbeat_ended")
    assert (ended["target_id"], ended["attempt_outcome"]) == (claimed.target_id, "published_accepted")
    assert logged(caplog, "score_claim_heartbeat_lost") == []
    await claims.stop_all()


def failing_beats(times: int) -> Callable[[str], Awaitable[None]]:
    """An `afault_point` replacement under which the next `times` beats raise, as a database blip would."""
    left = times

    async def fault(name: str) -> None:
        nonlocal left
        if name == claims_mod.BEAT_POINT and left > 0:
            left -= 1
            raise ConnectionError("synthetic blip password=hunter2")

    return fault


async def test_up_to_three_raising_beats_in_a_row_keep_the_claim(
    engine: AsyncEngine, deps: ScoreDeps, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Only "0 rows updated" is a lost claim (L3572); the 3-minute reclaim window covers about 5 beats
    claimed = await claimed_target(engine)
    sleep = ManualSleep()
    claims = heartbeats(engine, sleep)
    child: asyncio.Task[Any] = asyncio.create_task(asyncio.sleep(3600))  # a live stream
    await backdate(engine, claimed.target_id)
    monkeypatch.setattr(claims_mod, "afault_point", failing_beats(3))
    claims.start(claimed.ref)
    claims.attach(claimed.ref, child)
    for beats in (1, 2, 3):
        await sleep.release()
        await sleep.until(beats + 1)  # tolerated: the loop sleeps again
    assert not claims.lost(claimed.ref) and not child.done()
    assert not await fresh(engine, claimed.target_id)
    await sleep.release()
    await sleep.until(5)
    assert await fresh(engine, claimed.target_id)  # the fourth beat updates the row
    monkeypatch.setattr(claims_mod, "afault_point", failing_beats(3))  # a good beat restarts the count
    for beats in (5, 6, 7):
        await sleep.release()
        await sleep.until(beats + 1)
    assert not claims.lost(claimed.ref) and not child.done()
    failures = logged(caplog, "heartbeat_failed")
    assert [f["consecutive"] for f in failures] == [1, 2, 3, 1, 2, 3]
    assert {(f["error_class"], f["heartbeat"]) for f in failures} == {
        ("ConnectionError", f"claim:{claimed.target_id}")
    }
    assert "hunter2" not in caplog.text and "synthetic blip" not in caplog.text
    child.cancel()
    await claims.stop_all()


async def test_the_fourth_raising_beat_in_a_row_loses_the_claim(
    engine: AsyncEngine, deps: ScoreDeps, monkeypatch: pytest.MonkeyPatch
) -> None:
    claimed = await claimed_target(engine)
    sleep = ManualSleep()
    claims = heartbeats(engine, sleep)
    child: asyncio.Task[Any] = asyncio.create_task(asyncio.sleep(3600))
    monkeypatch.setattr(claims_mod, "afault_point", failing_beats(4))
    claims.start(claimed.ref)
    claims.attach(claimed.ref, child)
    for beats in (1, 2, 3):
        await sleep.release()
        await sleep.until(beats + 1)
    assert not claims.lost(claimed.ref)
    await sleep.release()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(child, 10)
    assert claims.lost(claimed.ref) and beat_tasks(claimed.target_id) == []
    await claims.stop_all()


async def test_a_beat_that_waits_past_its_bound_is_a_tolerated_failure(
    engine: AsyncEngine, deps: ScoreDeps, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A beat that cannot get its row (a held lock, a full pool) raises `TimeoutError` after `BEAT_TIMEOUT_S`,
    # so four failures in a row stay inside the 3-minute reclaim window
    monkeypatch.setattr(claims_mod, "BEAT_TIMEOUT_S", 0.3)
    claimed = await claimed_target(engine)
    sleep = ManualSleep()
    claims = heartbeats(engine, sleep)
    child: asyncio.Task[Any] = asyncio.create_task(asyncio.sleep(3600))
    await backdate(engine, claimed.target_id)
    claims.start(claimed.ref)
    claims.attach(claimed.ref, child)
    async with engine.connect() as blocker:
        await blocker.execute(
            sa.text("SELECT 1 FROM score_targets WHERE id = :t FOR UPDATE"), {"t": claimed.target_id}
        )
        await sleep.release()
        await sleep.until(2)  # the bounded beat gave up and the loop sleeps again
        await blocker.rollback()
    assert not claims.lost(claimed.ref) and not child.done()
    assert [(f["error_class"], f["consecutive"]) for f in logged(caplog, "heartbeat_failed")] == [
        ("TimeoutError", 1)
    ]
    await sleep.release()
    await sleep.until(3)
    assert await fresh(engine, claimed.target_id)  # the next beat updates the row
    child.cancel()
    await claims.stop_all()


async def test_stop_and_stop_all_cancel_and_await_the_beats(engine: AsyncEngine, deps: ScoreDeps) -> None:
    first, second, third = [await claimed_target(engine) for _ in range(3)]
    sleep = ManualSleep()
    claims = heartbeats(engine, sleep)
    for claimed in (first, second, third):
        claims.start(claimed.ref)
    await sleep.until(3)
    await claims.stop(first.ref)
    assert beat_tasks(first.target_id) == []
    assert beat_tasks(second.target_id) and beat_tasks(third.target_id)
    await claims.stop_all()
    assert [beat_tasks(c.target_id) for c in (first, second, third)] == [[], [], []]
    assert not any(claims.lost(c.ref) for c in (first, second, third))  # stopped, never lost
    with pytest.raises(ValueError, match="no heartbeat"):
        claims.attach({"target_id": 0, "claim_id": str(uuid4()), "attempt_no": 1}, asyncio.current_task())  # type: ignore[arg-type]


async def test_the_beat_passes_its_fault_point_before_its_update(
    engine: AsyncEngine, deps: ScoreDeps, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ruling P46: Task 28's C14 stalls the beat at `claims.beat`
    claimed = await claimed_target(engine)
    pause = PausePoint(claims_mod.BEAT_POINT)
    monkeypatch.setattr(claims_mod, "afault_point", pause)
    sleep = ManualSleep()
    claims = heartbeats(engine, sleep)
    await backdate(engine, claimed.target_id)
    claims.start(claimed.ref)
    await sleep.release()
    await asyncio.wait_for(pause.reached.wait(), 10)
    assert not await fresh(engine, claimed.target_id)  # held at the point: no update yet
    pause.resume()
    await sleep.until(2)
    assert await fresh(engine, claimed.target_id)
    await claims.stop_all()


async def test_a_terminal_end_releases_the_item_hold_once(
    engine: AsyncEngine, deps: ScoreDeps, fake_llm: FakeLLM
) -> None:
    # Codex r1 #27: a follow-up target's transient attempt keeps the hold; its publishing one releases it
    async with engine.begin() as conn:
        await activate_test_policy(conn)
        tuple_id = await score_llm_tuple(conn)
        operation = await insert_bulk_operation(conn, tuple_id)
        await conn.execute(
            sa.text("UPDATE bulk_operations SET followup_held_usd = 0.4 WHERE id = :b"), {"b": operation}
        )
        submission = await insert_submission(
            conn,
            tuple_id,
            operation,
            state="reconciled",
            submit_started_at=NOW,
            provider_batch_id="msgbatch_hold",
            results_complete_at=NOW,
            reconciled_at=NOW,
        )
    claimed = await claimed_target(engine, bulk_operation_id=operation)
    async with engine.begin() as conn:
        item = await insert_batch_item(
            conn,
            submission,
            claimed.target_id,
            attempt_no=9,
            result_type="succeeded",
            processed_at=NOW,
            decision="followup",
            hold_usd=Decimal("0.15"),
        )
    key = fake.answer_key("generate", {"job_revision_id": claimed.job_revision_id}, 0)
    fake.answer(key, "overloaded_529")  # the gateway's tries are spent: a transient error
    final = await invoke_subgraph(deps, claimed)
    assert final["outcome"] == "transient_error"
    assert (await target_of(engine, claimed.target_id)).state == "pending"

    async def hold() -> tuple[Any, Decimal]:
        async with engine.connect() as conn:
            released = (
                await conn.execute(
                    sa.text("SELECT hold_released_at FROM batch_items WHERE id = :i"), {"i": item}
                )
            ).scalar_one()
            held = (
                await conn.execute(
                    sa.text("SELECT followup_held_usd FROM bulk_operations WHERE id = :b"), {"b": operation}
                )
            ).scalar_one()
        return released, Decimal(held)

    assert await hold() == (None, Decimal("0.4"))  # kept: the draft would be resumed
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE score_targets SET not_before = NULL WHERE id = :t"), {"t": claimed.target_id}
        )
    again = await claim_again(engine, claimed.target_id, resumed_from_attempt_id=claimed.attempt_id)
    fake_llm.answer("generate", claimed.job_revision_id, synthetic_c_draft())
    assert (await invoke_subgraph(deps, again))["outcome"] == "published_accepted"
    released, held = await hold()
    assert released is not None and held == Decimal("0.25")  # released once, by the terminal attempt
    async with engine.connect() as conn:
        ledger = (
            await conn.execute(
                sa.text("SELECT count(*) FROM llm_spend_ledger WHERE bulk_operation_id = :b"),
                {"b": operation},
            )
        ).scalar_one()
    assert ledger >= 1  # the funded attempts reserved against the allocation
