"""The M3 spend owners on PostgreSQL (spec §3.3.3, §9.4.2, §9.4.3, §7.16.7; plan Task 7): a score attempt
(`TargetAttemptOwner`), a batch submission (`BatchSubmissionOwner`) and an eval run under its operation's
claim; the dispatch marker's attempt and target counters, the batch dispatch marker, the reconciler's
treatment of each owner's entries, and the receipts that carry their try's `max_tokens`."""

import dataclasses
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.transitions import transition
from jobhunter.errors import FencedOut
from jobhunter.llm import spend_ledger
from jobhunter.llm.errors import ClaimLost
from jobhunter.llm.spend_ledger import (
    BatchSubmissionOwner,
    CallOutcome,
    EvalRunOwner,
    Reservation,
    TargetAttemptOwner,
    last_call_of_attempt,
    mark_batch_dispatched,
    mark_dispatched,
    owner_holds,
    receipts,
    release_never_dispatched,
    reserve,
    settle,
)
from jobhunter.worker import operations as ops
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import ctx
from tests.support.llm import (
    USAGE,
    BatchSubmission,
    attempt_and_target,
    batch_kw,
    budget_rows,
    claim_owner,
    completed_call,
    count_rows,
    ledger_row,
    new_submission_owner,
    new_target_owner,
    reclaim,
    reclaim_target,
    reserve_kw,
    scoring_kw,
)
from tests.support.m2 import insert_eval_run

pytestmark = pytest.mark.integration
MINUTE = timedelta(minutes=1)


async def reserved(engine: AsyncEngine, kw: dict[str, object]) -> Reservation:
    async with engine.begin() as conn:
        return await reserve(conn, **kw)  # type: ignore[arg-type]


async def dispatched(engine: AsyncEngine, kw: dict[str, object], *, first_try: bool = True) -> Reservation:
    async with engine.begin() as conn:
        res = await reserve(conn, **kw)  # type: ignore[arg-type]
        await mark_dispatched(conn, res, kw["owner"], now=kw["now"], first_try_of_logical_call=first_try)  # type: ignore[arg-type]
    return res


async def unsettled_pass(engine: AsyncEngine, queue_app, now) -> int:
    return await spend_ledger.reconcile_llm_ledger_unsettled(ctx(engine, queue_app, now=now))


async def move_submission(engine: AsyncEngine, sub: BatchSubmission, to: str) -> BatchSubmissionOwner:
    """`sub`'s submission from the state its owner expects to `to`; the owner that expects `to`."""
    async with engine.begin() as conn:
        await transition(conn, "batch_submissions", sub.owner.submission_id, [sub.owner.expected], to)
    return BatchSubmissionOwner(sub.owner.submission_id, to)  # type: ignore[arg-type]


async def call_row(engine: AsyncEngine, call_id: int) -> sa.Row[tuple[object, ...]]:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                sa.text(
                    "SELECT attempt_id, target_id, round, logical_call_no, transport, transport_try, "
                    "batch_item_id, status FROM llm_calls WHERE id = :id"
                ),
                {"id": call_id},
            )
        ).one()


# ---- the score attempt (§9.4.2, §9.4.3) -------------------------------------------------------------------


async def test_a_target_owner_reserves_dispatches_and_charges_the_attempt(engine, target_owner) -> None:
    t = utcnow()
    async with engine.begin() as conn:
        res = await reserve(
            conn,
            **scoring_kw(target_owner, now=t, bound=4000, max_tokens=6000, logical_call_no=1, round=0),
        )
        await mark_dispatched(conn, res, target_owner, now=t, first_try_of_logical_call=True)
    attempt, target = await attempt_and_target(engine, target_owner)
    assert (attempt.logical_calls, attempt.http_calls, attempt.charged, target.charged_attempts) == (
        1,
        1,
        True,
        1,
    )
    async with engine.begin() as conn:  # a retry of the same logical call: http only
        res2 = await reserve(
            conn,
            **scoring_kw(
                target_owner, now=t, bound=4000, max_tokens=6000, logical_call_no=1, round=0, transport_try=2
            ),
        )
        await mark_dispatched(conn, res2, target_owner, now=t)
    attempt, target = await attempt_and_target(engine, target_owner)
    assert (attempt.logical_calls, attempt.http_calls, target.charged_attempts) == (1, 2, 1)
    # the attempt's second logical call (its first critic round): one more of each, the target charged once
    critic = scoring_kw(
        target_owner, now=t, bound=4000, max_tokens=6000, purpose="critic", logical_call_no=2, round=1
    )
    res3 = await dispatched(engine, critic)
    attempt, target = await attempt_and_target(engine, target_owner)
    assert (attempt.logical_calls, attempt.http_calls, target.charged_attempts) == (2, 3, 1)
    row = await call_row(engine, res3.llm_call_id)
    assert (row.attempt_id, row.target_id, row.round, row.logical_call_no, row.transport) == (
        target_owner.attempt_id,
        target_owner.target_id,
        1,
        2,
        "stream",
    )
    assert (await ledger_row(engine, res.ledger_id)).input_refs["owner"] == {
        "kind": "target",
        "id": target_owner.target_id,
        "claim_id": str(target_owner.claim_id),
        "attempt_no": target_owner.attempt_no,
        "attempt_id": target_owner.attempt_id,
    }


async def test_a_target_owner_reserves_only_for_its_own_attempt(engine, target_owner) -> None:
    other = await new_target_owner(engine)
    t = utcnow()
    with pytest.raises(ValueError, match="attempt"):
        await reserved(
            engine,
            scoring_kw(
                target_owner,
                now=t,
                bound=4000,
                max_tokens=6000,
                target_id=other.target_id,
                attempt_id=other.attempt_id,
            ),
        )
    assert await count_rows(engine, "llm_spend_ledger") == 0


async def test_a_reclaimed_target_owner_is_fenced_at_the_dispatch_marker(engine, target_owner) -> None:
    """C28's in-process shape: the claim is lost between the reservation and the dispatch marker."""
    t = utcnow()
    res = await reserved(engine, scoring_kw(target_owner, now=t, bound=4000, max_tokens=6000, round=0))
    successor = await reclaim_target(engine, target_owner)  # a new claim_id and the next attempt
    with pytest.raises(ClaimLost) as lost:
        async with engine.begin() as conn:
            await mark_dispatched(conn, res, target_owner, now=t, first_try_of_logical_call=True)
    assert isinstance(lost.value, FencedOut) and lost.value.owner == "score_target"
    assert not lost.value.worker_superseded  # one piece of work lost, not a reason to stop the worker
    assert (await ledger_row(engine, res.ledger_id)).state == "reserved"  # the marker wrote nothing
    async with engine.begin() as conn:
        await release_never_dispatched(conn, res, now=t)
    row = await ledger_row(engine, res.ledger_id)
    assert (row.state, row.evidence, row.settled_usd, row.call_status) == (
        "not_billed",
        "never_dispatched",
        Decimal(0),
        "cancelled",
    )
    attempt, target = await attempt_and_target(engine, target_owner)
    assert (attempt.logical_calls, attempt.http_calls, attempt.charged, attempt.outcome) == (
        0,
        0,
        False,
        "claim_lost",
    )
    assert target.charged_attempts == 0  # a claim lost before any dispatch charges no attempt (L2863)
    (month,) = await budget_rows(engine, res.month)
    assert month is not None and (month.counted_usd, month.scoring_counted_usd) == (0, 0)
    with pytest.raises(ClaimLost):  # the old claim reserves nothing either
        await reserved(engine, scoring_kw(target_owner, now=t, bound=4000, max_tokens=6000))
    assert await count_rows(engine, "llm_spend_ledger") == 1
    # the new claim reserves and dispatches for its own attempt
    await dispatched(engine, scoring_kw(successor, now=t, bound=4000, max_tokens=6000))
    attempt, target = await attempt_and_target(engine, successor)
    assert (attempt.logical_calls, attempt.http_calls, target.charged_attempts) == (1, 1, 1)


async def test_owner_holds_knows_the_m3_owners(
    engine, target_owner, submission_owner: BatchSubmission
) -> None:
    t = utcnow()
    async with engine.begin() as conn:
        assert await owner_holds(conn, target_owner.durable(), now=t)
        assert await owner_holds(conn, submission_owner.owner.durable(), now=t)
        other_state = {**submission_owner.owner.durable(), "state": "submit_started"}
        assert not await owner_holds(conn, other_state, now=t)
        later_attempt = {**target_owner.durable(), "attempt_no": target_owner.attempt_no + 1}
        assert not await owner_holds(conn, later_attempt, now=t)
        missing = {**target_owner.durable(), "id": 10**9}
        assert not await owner_holds(conn, missing, now=t)
    await reclaim_target(engine, target_owner)
    await move_submission(engine, submission_owner, "abandoned")
    async with engine.begin() as conn:
        assert not await owner_holds(conn, target_owner.durable(), now=t)
        assert not await owner_holds(conn, submission_owner.owner.durable(), now=t)


async def test_an_eval_run_under_its_operations_claim_holds_while_both_do(engine, current_epoch) -> None:
    """Codex r1 #29: an eval run executed by an `eval_run` operation is owned under the operation's claim."""
    t = utcnow()
    async with engine.begin() as conn:
        op_id, _ = await ops.create_operation(
            conn, kind="eval_run", payload={}, dedup_key="eval-run-owner-test", defer=None
        )
        eval_run_id = await insert_eval_run(conn)
    claimed = await claim_owner(engine, op_id, epoch=current_epoch)
    owner = EvalRunOwner(eval_run_id, claim=claimed.claim)
    assert owner.durable() == {
        "kind": "eval_run",
        "id": eval_run_id,
        "operation_id": op_id,
        "claim_id": str(claimed.claim.claim_id),
    }
    async with engine.begin() as conn:
        assert await owner_holds(conn, owner.durable(), now=t)
        assert await owner_holds(conn, EvalRunOwner(eval_run_id).durable(), now=t)  # M2's claimless form
    await reclaim(engine, claimed)
    async with engine.begin() as conn:
        assert not await owner_holds(conn, owner.durable(), now=t)  # the operation's claim no longer holds
        assert await owner_holds(conn, EvalRunOwner(eval_run_id).durable(), now=t)  # the run is running


# ---- the batch submission (§10.10, R6, plan Deviation 13) -------------------------------------------------


async def test_a_batch_submission_owner_reserves_while_its_state_is_the_expected_one(
    engine, submission_owner: BatchSubmission
) -> None:
    t = utcnow()
    sub = submission_owner
    res = await reserved(engine, batch_kw(sub, sub.items[0], now=t, bound=4000))
    row = await call_row(engine, res.llm_call_id)
    assert (row.transport, row.transport_try, row.logical_call_no, row.batch_item_id) == (
        "batch",
        1,
        None,
        sub.items[0].item_id,
    )
    refs = (await ledger_row(engine, res.ledger_id)).input_refs
    assert refs["owner"] == {"kind": "batch_submission", "id": sub.owner.submission_id, "state": "preparing"}
    assert "deadline_s" not in refs  # a batch item has no online deadline
    await move_submission(engine, sub, "abandoned")
    with pytest.raises(FencedOut) as fenced:
        await reserved(engine, batch_kw(sub, sub.items[1], now=t, bound=4000))
    assert fenced.value.owner == "batch_submission"
    assert await count_rows(engine, "llm_spend_ledger") == 1


async def test_the_batch_dispatch_marker_moves_every_item_and_charges_each_attempt(engine, clean_db) -> None:
    t = utcnow()
    sub = await new_submission_owner(engine, items=3)
    entries = [await reserved(engine, batch_kw(sub, item, now=t, bound=4000)) for item in sub.items]
    started = await move_submission(engine, sub, "submit_started")
    with pytest.raises(FencedOut):  # the owner of the earlier state no longer holds
        async with engine.begin() as conn:
            await mark_batch_dispatched(conn, sub.owner, entries, now=t)
    assert {(await ledger_row(engine, r.ledger_id)).state for r in entries} == {"reserved"}
    async with engine.begin() as conn:
        await mark_batch_dispatched(conn, started, entries, now=t)
    for res, item in zip(entries, sub.items, strict=True):
        row = await ledger_row(engine, res.ledger_id)
        assert (row.state, row.call_status) == ("dispatched", "dispatched")
        attempt, target = await attempt_and_target(engine, item)
        assert (attempt.logical_calls, attempt.http_calls, attempt.charged, target.charged_attempts) == (
            1,
            1,
            True,
            1,
        )
    with pytest.raises(FencedOut):  # a second marker finds nothing reserved: it writes nothing
        async with engine.begin() as conn:
            await mark_batch_dispatched(conn, started, entries, now=t)
    attempt, target = await attempt_and_target(engine, sub.items[0])
    assert (attempt.http_calls, target.charged_attempts) == (1, 1)


async def test_the_batch_dispatch_marker_refuses_another_owners_entry(engine, clean_db) -> None:
    t = utcnow()
    sub = await new_submission_owner(engine, items=1)
    online = await new_target_owner(engine)
    mine = await reserved(engine, batch_kw(sub, sub.items[0], now=t, bound=4000))
    theirs = await reserved(engine, scoring_kw(online, now=t, bound=4000, max_tokens=6000))
    started = await move_submission(engine, sub, "submit_started")
    with pytest.raises(FencedOut):
        async with engine.begin() as conn:
            await mark_batch_dispatched(conn, started, [mine, theirs], now=t)
    assert [(await ledger_row(engine, r.ledger_id)).state for r in (mine, theirs)] == ["reserved", "reserved"]
    attempt, _ = await attempt_and_target(engine, sub.items[0])
    assert attempt.http_calls == 0  # the whole marker rolled back with the caller's transaction


async def test_a_rejected_submission_settles_its_items_not_billed(engine, clean_db) -> None:
    """L2335, A2 B17: a submission the provider refused ran none of its items; each item's call ends `error`
    with `batch_submit_rejected` and the rejection's `http_status`, its counters and its allocation debit are
    given back, and its attempt counts nothing. An online try cannot carry that evidence."""
    t = utcnow()
    sub = await new_submission_owner(engine, items=2)
    entries = [await reserved(engine, batch_kw(sub, item, now=t, bound=4000)) for item in sub.items]
    started = await move_submission(engine, sub, "submit_started")
    async with engine.begin() as conn:
        await mark_batch_dispatched(conn, started, entries, now=t)
    await move_submission(engine, dataclasses.replace(sub, owner=started), "submit_rejected")
    rejected = CallOutcome("error", http_status=400, error_code="batch_submit_rejected")
    async with engine.begin() as conn:
        for res in entries:
            await settle(
                conn,
                res,
                outcome="not_billed",
                evidence="batch_submit_rejected",
                usage=None,
                call=rejected,
                now=t,
            )
    for res, item in zip(entries, sub.items, strict=True):
        row = await ledger_row(engine, res.ledger_id)
        assert (row.state, row.evidence, row.settled_usd) == (
            "not_billed",
            "batch_submit_rejected",
            Decimal(0),
        )
        assert (row.call_status, row.error_code, row.cost_usd) == (
            "error",
            "batch_submit_rejected",
            Decimal(0),
        )
        attempt, target = await attempt_and_target(engine, item)
        assert (attempt.cost_usd, attempt.charged, target.charged_attempts) == (
            0,
            True,
            1,
        )  # charged at submit
    (month,) = await budget_rows(engine, entries[0].month)
    assert month is not None and month.counted_usd == 0
    async with engine.connect() as conn:
        spent = await conn.execute(
            sa.text("SELECT spent_usd FROM bulk_operations WHERE id = :id"), {"id": sub.bulk_operation_id}
        )
        assert spent.scalar_one() == 0
    online = await new_target_owner(engine)
    res = await dispatched(engine, scoring_kw(online, now=t, bound=4000, max_tokens=6000))
    with pytest.raises(ValueError, match="evidence"):
        async with engine.begin() as conn:
            await settle(
                conn,
                res,
                outcome="not_billed",
                evidence="batch_submit_rejected",
                usage=None,
                call=rejected,
                now=t,
            )


async def test_batch_entries_are_never_aged_by_the_reconciler(engine, queue_app, current_epoch) -> None:
    """L2979: batch item entries are exempt from the 15-minute rule. A dispatched batch entry older than an
    hour stays dispatched; a reserved batch entry of an abandoned submission stays reserved (Task 16 releases
    it). The online entries of a lost target owner, beside them, are aged as before."""
    t = utcnow()
    abandoned = await new_submission_owner(engine, items=1)
    left = await reserved(engine, batch_kw(abandoned, abandoned.items[0], now=t, bound=4000))
    await move_submission(engine, abandoned, "abandoned")
    submitted = await new_submission_owner(engine, items=1, bulk_operation_id=abandoned.bulk_operation_id)
    entry = await reserved(engine, batch_kw(submitted, submitted.items[0], now=t, bound=4000))
    started = await move_submission(engine, submitted, "submit_started")
    async with engine.begin() as conn:
        await mark_batch_dispatched(conn, started, [entry], now=t)
    online = await new_target_owner(engine)
    stale = await reserved(engine, scoring_kw(online, now=t, bound=4000, max_tokens=6000))
    streaming = await dispatched(
        engine, scoring_kw(online, now=t, bound=4000, max_tokens=6000, transport_try=2), first_try=False
    )
    await reclaim_target(engine, online)  # the online owner is lost

    assert await unsettled_pass(engine, queue_app, t + 14 * MINUTE) == 0
    # the 600 s deadline + 5 min is the 15-minute floor: the online entries are recovered, the batch ones not
    assert await unsettled_pass(engine, queue_app, t + 2 * 60 * MINUTE) == 2
    assert (await ledger_row(engine, left.ledger_id)).state == "reserved"
    assert (await ledger_row(engine, entry.ledger_id)).state == "dispatched"
    assert (await ledger_row(engine, stale.ledger_id)).state == "not_billed"
    assert (await ledger_row(engine, streaming.ledger_id)).state == "ambiguous"
    assert await unsettled_pass(engine, queue_app, t + 30 * 60 * MINUTE) == 0  # never, however old


async def test_a_live_target_owners_reserved_entry_is_kept_and_its_lost_successor_released(
    engine, queue_app, current_epoch, target_owner
) -> None:
    t = utcnow()
    live = await reserved(engine, scoring_kw(target_owner, now=t, bound=4000, max_tokens=6000))
    assert await unsettled_pass(engine, queue_app, t + 16 * MINUTE) == 0  # its claim still holds
    assert (await ledger_row(engine, live.ledger_id)).state == "reserved"
    await reclaim_target(engine, target_owner)
    assert await unsettled_pass(engine, queue_app, t + 16 * MINUTE) == 1
    row = await ledger_row(engine, live.ledger_id)
    assert (row.state, row.evidence, row.call_status) == ("not_billed", "never_dispatched", "cancelled")


# ---- receipts (M2 Carry-forward J2; plan Task 9's start stage) --------------------------------------------


async def test_receipts_and_the_last_call_of_an_attempt_carry_their_max_tokens(
    engine, op_owner, target_owner
) -> None:
    t = utcnow()
    op_try = await dispatched(engine, reserve_kw(op_owner, now=t, bound=4000, max_tokens=1000))
    async with engine.begin() as conn:
        await settle(conn, op_try, outcome="billed", evidence=None, usage=USAGE, call=completed_call(), now=t)
        (receipt,) = (
            await receipts(conn, operation_id=op_owner.claim.operation_id, purpose="extract_profile")
        ).values()
    assert (receipt.llm_call_id, receipt.max_tokens) == (op_try.llm_call_id, 1000)

    first = await dispatched(engine, scoring_kw(target_owner, now=t, bound=4000, max_tokens=6000, round=0))
    async with engine.begin() as conn:
        stopped = completed_call(
            model_served="claude-opus-5-5", stop_reason="max_tokens", error_code="max_tokens_first_stage"
        )
        await settle(conn, first, outcome="billed", evidence=None, usage=USAGE, call=stopped, now=t)
        last = await last_call_of_attempt(conn, attempt_id=target_owner.attempt_id, purpose="generate")
    assert last is not None
    assert (last.llm_call_id, last.error_code, last.max_tokens) == (
        first.llm_call_id,
        "max_tokens_first_stage",
        6000,
    )
    enlarged = await reserved(
        engine,
        scoring_kw(target_owner, now=t, bound=4000, max_tokens=12000, max_tokens_stage="enlarged", round=0),
    )
    async with engine.connect() as conn:  # any status: the newest call of the attempt and purpose
        last = await last_call_of_attempt(conn, attempt_id=target_owner.attempt_id, purpose="generate")
        none = await last_call_of_attempt(conn, attempt_id=target_owner.attempt_id, purpose="critic")
    assert last is not None and (last.llm_call_id, last.error_code, last.max_tokens) == (
        enlarged.llm_call_id,
        None,
        12000,
    )
    assert none is None


def test_the_owners_are_frozen_values_with_their_durable_records() -> None:
    """The owner is a frozen value: two of the same claim are equal (the dispatch marker compares the recorded
    owner with the caller's)."""
    claim = UUID(int=7)
    assert TargetAttemptOwner(1, claim, 2, 3) == TargetAttemptOwner(1, claim, 2, 3)
    assert TargetAttemptOwner(1, claim, 2, 3).durable() == {
        "kind": "target",
        "id": 1,
        "claim_id": str(claim),
        "attempt_no": 2,
        "attempt_id": 3,
    }
    assert BatchSubmissionOwner(4, "submit_started").durable() == {
        "kind": "batch_submission",
        "id": 4,
        "state": "submit_started",
    }
