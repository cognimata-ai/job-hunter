"""The atomic claim on PostgreSQL (spec §9.3 L3477-3491, §18.2 L5708; plan Task 11, Codex r1 #15, #21, #27,
r3 B8, B15, r4 B2, Controller carry T9-d): `nodes.claim.claim_round`, the `claim` node's one transaction.

Two concurrent sessions never claim one target twice; a fenced, a backed-off and an exhausted target stay
pending; an always-include admission goes through the real statement against the real lane CHECKs and is
written to the attempt; the attempt pins the active execution config and evaluation policy and names the
attempt it resumes (the link `load_context`'s resume chain follows) when the target has a resumable draft; and
the statement is the one `tests.support.scoring.claim_again` runs (Ruling P29).

Every datum is invented.
"""

import asyncio
from collections.abc import Sequence
from datetime import timedelta

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.graphs.score.nodes import claim as claim_node
from jobhunter.graphs.score.nodes.claim import Claimed
from jobhunter.scoring.admission import Picked
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.fences import insert_test_fence
from tests.support.fixtures import count_rows
from tests.support.m3 import active_ids, insert_attempt, insert_cycle, insert_draft, insert_scoring_call
from tests.support.scoring import (
    PendingTarget,
    activate_test_policy,
    claim_again,
    insert_targets,
    score_llm_tuple,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def pending_targets(engine: AsyncEngine, clean_db: None) -> list[PendingTarget]:
    return await insert_targets(engine, 4)


@pytest.fixture
async def cycle_id(engine: AsyncEngine, pending_targets: list[PendingTarget]) -> int:
    async with engine.begin() as conn:
        return await insert_cycle(conn, await score_llm_tuple(conn))


async def claim_picked(
    engine: AsyncEngine, picked: Sequence[Picked], *, cycle_id: int, epoch: int = 1
) -> list[Claimed]:
    """The claim's transaction over `picked`: the statement and one attempt per claimed target."""
    async with engine.begin() as conn:
        active = await active_ids(conn)
        return await claim_node.claim_round(
            conn,
            picked,
            epoch=epoch,
            cycle_id=cycle_id,
            execution_config_id=active.execution_config_id,
            evaluation_policy_id=active.evaluation_policy_id,
        )


async def claim_ids(
    engine: AsyncEngine, ids: Sequence[int], *, cycle_id: int, epoch: int = 1
) -> list[Claimed]:
    return await claim_picked(
        engine, [Picked(i, lane="similarity") for i in ids], cycle_id=cycle_id, epoch=epoch
    )


async def one_value(engine: AsyncEngine, sql: str, **params: object) -> object:
    async with engine.connect() as conn:
        return (await conn.execute(sa.text(sql), params)).scalar_one()


async def test_two_sessions_never_double_claim(
    engine: AsyncEngine, pending_targets: list[PendingTarget], cycle_id: int
) -> None:
    ids = [t.target_id for t in pending_targets]
    a, b = await asyncio.gather(
        claim_ids(engine, ids, cycle_id=cycle_id, epoch=1), claim_ids(engine, ids, cycle_id=cycle_id, epoch=1)
    )
    assert not {r.id for r in a} & {r.id for r in b} and len(a) + len(b) == len(ids)
    assert await count_rows(engine, "score_attempts", outcome="running") == len(ids)


async def test_the_claim_skips_fenced_and_backed_off_targets(
    engine: AsyncEngine, pending_targets: list[PendingTarget], cycle_id: int
) -> None:
    fenced, backed_off, exhausted, free = pending_targets
    async with engine.begin() as conn:
        await insert_test_fence(conn, kind="jobs", job_ids=[fenced.job_id])
        await conn.execute(
            sa.text("UPDATE score_targets SET not_before = :later WHERE id = :t"),
            {"later": utcnow() + timedelta(hours=1), "t": backed_off.target_id},
        )
        await conn.execute(
            sa.text("UPDATE score_targets SET charged_attempts = 4 WHERE id = :t"), {"t": exhausted.target_id}
        )
    claimed = await claim_ids(engine, [t.target_id for t in pending_targets], cycle_id=cycle_id)
    assert [r.id for r in claimed] == [free.target_id]
    for left in (fenced, backed_off, exhausted):
        state = await one_value(engine, "SELECT state FROM score_targets WHERE id = :t", t=left.target_id)
        assert state == "pending"
    assert await count_rows(engine, "score_attempts") == 1


async def test_an_always_include_admission_is_claimed_and_written_to_the_attempt(
    engine: AsyncEngine, pending_targets: list[PendingTarget], cycle_id: int
) -> None:
    # Codex r4 B2: a Picked(lane="always_include") goes through the real claim statement against the real
    # CHECKs
    target = pending_targets[0].target_id
    rows = await claim_picked(
        engine, [Picked(target, lane="always_include", always_include_reason="watchlist")], cycle_id=cycle_id
    )
    assert rows[0].lane == "always_include"
    assert await one_value(engine, "SELECT lane FROM score_attempts WHERE target_id = :t", t=target) == (
        "always_include"
    )
    assert (
        await one_value(engine, "SELECT lane FROM score_targets WHERE id = :t", t=target) == "always_include"
    )
    assert (
        await one_value(engine, "SELECT always_include_reason FROM score_targets WHERE id = :t", t=target)
        == "watchlist"
    )


async def test_the_attempt_pins_the_active_configuration_and_its_cycle(
    engine: AsyncEngine, pending_targets: list[PendingTarget], cycle_id: int
) -> None:
    # L3491: the active execution config and evaluation policy, which the attempt uses throughout
    async with engine.begin() as conn:
        policy = await activate_test_policy(conn, t3_min_mid=75)
        active = await active_ids(conn)
    (row,) = await claim_ids(engine, [pending_targets[0].target_id], cycle_id=cycle_id, epoch=1)
    async with engine.connect() as conn:
        attempt = (
            await conn.execute(
                sa.text(
                    "SELECT a.id, a.attempt_no, a.mode, a.outcome, a.claim_id, a.claim_epoch, a.cycle_id, "
                    "a.execution_config_id, a.evaluation_policy_id, a.resumed_from_attempt_id, t.claim_id "
                    "AS target_claim, t.claim_epoch AS target_epoch, t.attempt_no AS target_attempt_no "
                    "FROM score_attempts a JOIN score_targets t ON t.id = a.target_id WHERE a.target_id = :t"
                ),
                {"t": row.id},
            )
        ).one()
    assert (attempt.id, attempt.attempt_no, row.attempt_no) == (row.attempt_id, 1, 1)
    assert (attempt.mode, attempt.outcome, attempt.cycle_id) == ("online", "running", cycle_id)
    assert str(attempt.claim_id) == str(attempt.target_claim) == row.claim_id
    assert (attempt.claim_epoch, attempt.target_epoch, attempt.target_attempt_no) == (1, 1, 1)
    assert (attempt.execution_config_id, attempt.evaluation_policy_id) == (active.execution_config_id, policy)
    assert attempt.resumed_from_attempt_id is None  # nothing to resume
    assert row.ref == {"target_id": row.id, "claim_id": row.claim_id, "attempt_no": 1}


async def test_a_target_with_a_resumable_draft_links_its_newest_earlier_attempt(
    engine: AsyncEngine, pending_targets: list[PendingTarget], cycle_id: int
) -> None:
    # Controller carry T9-d: the link the resume chain of `load_context` follows (Codex r3 B8)
    drafted, undrafted = pending_targets[0].target_id, pending_targets[1].target_id
    async with engine.begin() as conn:
        first = await insert_attempt(conn, drafted, attempt_no=1, outcome="claim_lost")
        _, call = await insert_scoring_call(conn, attempt_id=first, target_id=drafted)
        await insert_draft(conn, first, call)
        second = await insert_attempt(conn, drafted, attempt_no=2, outcome="claim_lost")
        await insert_attempt(conn, undrafted, attempt_no=1, outcome="claim_lost")
        await conn.execute(sa.text("UPDATE score_targets SET attempt_no = 2 WHERE id = :t"), {"t": drafted})
        await conn.execute(sa.text("UPDATE score_targets SET attempt_no = 1 WHERE id = :t"), {"t": undrafted})
    rows = {r.id: r for r in await claim_ids(engine, [drafted, undrafted], cycle_id=cycle_id)}
    assert (rows[drafted].attempt_no, rows[undrafted].attempt_no) == (3, 2)
    links = sa.text("SELECT resumed_from_attempt_id FROM score_attempts WHERE id = :a")
    async with engine.connect() as conn:
        assert (await conn.execute(links, {"a": rows[drafted].attempt_id})).scalar_one() == second
        assert (await conn.execute(links, {"a": rows[undrafted].attempt_id})).scalar_one() is None


async def test_a_batch_follow_up_links_its_batch_attempt_whatever_the_policy(
    engine: AsyncEngine, pending_targets: list[PendingTarget], cycle_id: int
) -> None:
    # Spec L3589 (final review, Fable I1): a batch's `resume_draft_id` is resumed under a later policy too, so
    # the claim links its attempt, and the reclaim of a follow-up links the follow-up's attempt (whose own
    # link reaches the batch); an online target's draft under another policy is not resumed (no link)
    follow_up, reclaimed, online = (t.target_id for t in pending_targets[:3])
    batches: dict[int, int] = {}
    async with engine.begin() as conn:
        for target in (follow_up, reclaimed):
            batch = await insert_attempt(
                conn, target, mode="batch", lane="batch", outcome="batch_draft_ready"
            )
            _, call = await insert_scoring_call(conn, attempt_id=batch, target_id=target, transport="batch")
            draft = await insert_draft(conn, batch, call)
            batches[target] = batch
            await conn.execute(
                sa.text(
                    "UPDATE score_targets SET attempt_no = 1, lane = 'batch_followup', resume_draft_id = :d "
                    "WHERE id = :t"
                ),
                {"d": draft, "t": target},
            )
        lost = await insert_attempt(conn, online, outcome="claim_lost")
        _, call = await insert_scoring_call(conn, attempt_id=lost, target_id=online)
        await insert_draft(conn, lost, call)
        await conn.execute(sa.text("UPDATE score_targets SET attempt_no = 1 WHERE id = :t"), {"t": online})
        policy = await activate_test_policy(conn, t3_min_mid=75)  # the attempts above pinned the one before
        # the follow-up of `reclaimed` was claimed under the new policy and lost before any draft of its own
        lost_follow_up = await insert_attempt(
            conn,
            reclaimed,
            attempt_no=2,
            lane="batch_followup",
            outcome="claim_lost",
            evaluation_policy_id=policy,
            resumed_from_attempt_id=batches[reclaimed],
        )
        await conn.execute(sa.text("UPDATE score_targets SET attempt_no = 2 WHERE id = :t"), {"t": reclaimed})
    rows = {r.id: r for r in await claim_ids(engine, [follow_up, reclaimed, online], cycle_id=cycle_id)}
    links = sa.text("SELECT resumed_from_attempt_id FROM score_attempts WHERE id = :a")
    async with engine.connect() as conn:
        assert (await conn.execute(links, {"a": rows[follow_up].attempt_id})).scalar_one() == batches[
            follow_up
        ]
        assert (await conn.execute(links, {"a": rows[reclaimed].attempt_id})).scalar_one() == lost_follow_up
        assert (await conn.execute(links, {"a": rows[online].attempt_id})).scalar_one() is None


async def test_the_claim_statement_is_the_one_claim_again_runs(
    engine: AsyncEngine, pending_targets: list[PendingTarget], cycle_id: int
) -> None:
    # Ruling P29: the claim node's `transition_where` statement and `claim_again`'s (Task 9) compile to the
    # same SQL
    executed: list[str] = []

    def record(conn: object, cursor: object, statement: str, *args: object) -> None:
        if statement.startswith("UPDATE score_targets AS t"):
            executed.append(statement)

    sa.event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        await claim_ids(engine, [pending_targets[0].target_id], cycle_id=cycle_id)
        await claim_again(engine, pending_targets[1].target_id)
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", record)
    node_sql, support_sql = executed
    assert node_sql == support_sql
    assert "FOR UPDATE SKIP LOCKED" in node_sql and node_sql.endswith(
        "RETURNING t.id, t.claim_id, t.attempt_no, t.resume_draft_id"
    )
