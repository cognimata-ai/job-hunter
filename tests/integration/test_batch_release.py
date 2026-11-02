"""The user's release of a quarantined submission and the recovery of a released one (spec §10.10 L3991-3999,
§7.16.6, §7.16.7, §18.3 C33, L5726; plan Task 17, Codex r2 B7, B13, B15, N9, R3-2, Carry T7-b, Ruling
T16-c).

- The release, in the route's transaction: `submit_ambiguous → released`, every entry `dispatched →
  ambiguous` counted at its reservation and added to its attempt's `cost_usd` (Carry T7-b), the holds
  released, the targets `pending` (a cancelled one untouched), the running attempts `released` (a cancelled
  one keeps its outcome), the unresolved alert resolved, the follow-on inserted once; `quarantined_usd`
  (and `/api/system/spend`) shows the reservation before and nothing after.
- C33 (L5726, Codex R3-2): the released submission's batch found later with a succeeded, an errored and an
  expired item settles `ambiguous → billed | not_billed(...)`, every item `ignored_fenced`, the re-claimed
  targets untouched, and it ends `reconciled` outside the singleton while the current submission stays
  `submitted`.

The targets are 250 synthetic ads of the score tuple in one bulk operation; every datum is invented.
"""

from collections import Counter
from decimal import Decimal
from typing import Any

import httpx
import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.errors import TransitionConflict
from jobhunter.llm.fake import BatchScript
from jobhunter.scoring import batches as batches_mod
from jobhunter.scoring import cycles, targets
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.batches import (
    PUBLISHED_BODY,
    BulkTargets,
    batch_ledger,
    bulk_row,
    bulk_with_targets,
    decisions,
    entries_by_custom_id,
    item_rows,
    reconcile_ambiguous_once,
    submission_rows,
    targets_by_custom_id,
)
from tests.support.llm import FakeLLM
from tests.support.profile import OpRunner
from tests.support.scoring import operations_of

pytestmark = pytest.mark.integration
N = 250
_ALERT = sa.text(
    "INSERT INTO alerts (code, severity, dedup_key, message, context) "
    "VALUES (:code, 'critical', :key, 'test', '{}')"
)
_ATTEMPT_COSTS = sa.text(
    "SELECT a.outcome, a.cost_usd, l.reserved_usd FROM batch_items i "
    "JOIN score_attempts a ON a.target_id = i.target_id AND a.attempt_no = i.attempt_no "
    "JOIN llm_calls c ON c.id = i.llm_call_id JOIN llm_spend_ledger l ON l.id = c.spend_ledger_id "
    "WHERE i.submission_id = :s ORDER BY i.id"
)


@pytest.fixture
async def setup(
    engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int, queue_app: procrastinate.App
) -> BulkTargets:
    made = await bulk_with_targets(engine, N)
    for target in made.targets:
        fake_llm.answer("generate", target.job_revision_id, PUBLISHED_BODY)
    return made


async def quarantined(engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, setup: BulkTargets) -> Any:
    """The setup's batch created with its answer lost (`ambiguous_created`): the `submit_ambiguous` row."""
    fake_llm.batches(BatchScript(create="ambiguous_created", polls_until_ended=5))
    assert setup.operation_id is not None
    await op_runner.run_one(setup.operation_id)
    (row,) = await submission_rows(engine)
    assert row.state == "submit_ambiguous"
    return row


async def release(engine: AsyncEngine, submission_id: int) -> batches_mod.Released:
    async with engine.begin() as conn:  # the route's first lock (Task 21)
        await cycles.lock_active_scoring(conn)
        return await batches_mod.release(conn, submission_id, now=utcnow(), defer=defer_in_tx)


async def quarantined_usd(engine: AsyncEngine) -> Decimal:
    async with engine.connect() as conn:
        return await batches_mod.quarantined_usd(conn)


async def test_the_release_and_its_effects(
    engine: AsyncEngine,
    op_runner: OpRunner,
    fake_llm: FakeLLM,
    setup: BulkTargets,
    logged_in_client: httpx.AsyncClient,
) -> None:
    """L3991-3997, in the schedule's order (Ruling T16-c: the targets before the attempts)."""
    row = await quarantined(engine, op_runner, fake_llm, setup)
    cancelled = setup.targets[3]
    async with engine.begin() as conn:  # a revision supersession cancels one target on its own
        await cycles.lock_active_scoring(conn)
        await targets.cancel_targets(
            conn, revision_ids=[cancelled.job_revision_id], reason="revision_superseded", now=utcnow()
        )
        await conn.execute(
            _ALERT,
            {"code": batches_mod.UNRESOLVED_ALERT, "key": f"batch.submit_ambiguous_unresolved:{row.id}"},
        )
    assert await quarantined_usd(engine) == row.reserved_usd > 0
    body = (await logged_in_client.get("/api/system/spend")).json()
    assert body["quarantined_batch_usd"] == format(row.reserved_usd, "f")
    released = await release(engine, row.id)
    assert released == batches_mod.Released(row.reserved_usd)
    (row,) = await submission_rows(engine)
    assert row.state == "released" and row.released_at is not None and row.follow_on_at is not None
    assert Counter(state for state, _ in await batch_ledger(engine)) == {"ambiguous": N}
    assert {e.status for e in (await entries_by_custom_id(engine)).values()} == {"ambiguous"}
    async with engine.connect() as conn:
        costs = (await conn.execute(_ATTEMPT_COSTS, {"s": row.id})).all()
    assert Counter(c.outcome for c in costs) == {"released": N - 1, "cancelled": 1}  # a cancelled one kept
    assert all(c.cost_usd == c.reserved_usd for c in costs)  # Carry T7-b: counted at its reservation
    by_target = await targets_by_custom_id(engine)
    assert Counter((t.state, t.state_reason) for t in by_target.values()) == {
        ("pending", None): N - 1,
        ("cancelled", "revision_superseded"): 1,
    }
    assert all(t.batch_submission_id is None for t in by_target.values())
    account = await bulk_row(engine, setup.bulk_operation_id)
    assert account.followup_held_usd == 0 and account.spent_usd == row.reserved_usd  # still counted
    assert all(item.hold_released_at is not None for item in (await item_rows(engine)).values())
    async with engine.connect() as conn:
        open_unresolved = (
            await conn.execute(
                sa.text("SELECT count(*) FROM alerts WHERE code = :c AND resolved_at IS NULL"),
                {"c": batches_mod.UNRESOLVED_ALERT},
            )
        ).scalar_one()
    assert open_unresolved == 0
    follow_ons = [op for op in await operations_of(engine, "batch_submit") if op.id != setup.operation_id]
    assert [op.payload["after_submission_id"] for op in follow_ons] == [row.id]  # the follow-on, once
    with pytest.raises(TransitionConflict):  # a second release finds no quarantine
        await release(engine, row.id)
    async with engine.begin() as conn:
        assert not await batches_mod.follow_on(conn, submission_id=row.id, defer=None)
    assert len([op for op in await operations_of(engine, "batch_submit") if op.id != setup.operation_id]) == 1
    assert await quarantined_usd(engine) == 0
    body = (await logged_in_client.get("/api/system/spend")).json()
    assert body["quarantined_batch_usd"] == "0.000000"


async def test_released_batch_recovery(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, setup: BulkTargets
) -> None:
    """C33 (L5726, Codex R3-2, N9): submission A is released, its follow-on submission B reaches `submitted`
    with the released targets, then A's batch is found with one succeeded, one errored and one expired item
    (the others succeeded too): A's entries settle `ambiguous → billed | not_billed(batch_item_errored |
    batch_item_expired)` and its attempts' costs follow; every item is `ignored_fenced`; B's targets stay
    `in_batch`; A moves `released → ended → reconciled` while B stays `submitted`, with no unique-index
    violation and no second follow-on."""
    a = await quarantined(engine, op_runner, fake_llm, setup)
    await release(engine, a.id)
    fake_llm.batches(BatchScript(create="ok", polls_until_ended=5))
    await op_runner.run_pending()  # A's follow-on: B, created and submitted
    rows = {r.id: r for r in await submission_rows(engine)}
    (b,) = [r for r in rows.values() if r.id != a.id]
    assert b.state == "submitted"
    ids = [f"t{t.target_id}_a1" for t in setup.targets]
    errored, expired = ids[0], ids[1]
    fake_llm.batches(BatchScript(items={errored: "errored:other", expired: "expired"}, polls_until_ended=0))
    report = await reconcile_ambiguous_once(engine, fake_llm)
    assert (report.submissions, report.adopted, report.reconciled) == (1, 1, 1)
    rows = {r.id: r for r in await submission_rows(engine)}
    assert (rows[a.id].state, rows[b.id].state) == ("reconciled", "submitted")
    assert rows[a.id].released_at is not None and rows[a.id].provider_batch_id == "msgbatch_1"
    entries = await entries_by_custom_id(engine)
    a_entries = {c: entries[c] for c in ids}
    assert (a_entries[errored].state, a_entries[errored].evidence) == ("not_billed", "batch_item_errored")
    assert (a_entries[expired].state, a_entries[expired].evidence) == ("not_billed", "batch_item_expired")
    assert {e.state for c, e in a_entries.items() if c not in (errored, expired)} == {"billed"}
    a_items = {c: item for c, item in (await item_rows(engine)).items() if c in a_entries}
    assert {item.decision for item in a_items.values()} == {"ignored_fenced"}
    assert Counter(item.result_type for item in a_items.values()) == {
        "succeeded": N - 2,
        "errored": 1,
        "expired": 1,
    }
    assert await decisions(engine) == ["ignored_fenced"] * N  # B's items wait for B's batch
    by_target = await targets_by_custom_id(engine)
    assert {
        (by_target[f"t{t.target_id}_a2"].state, by_target[f"t{t.target_id}_a2"].batch_submission_id)
        for t in setup.targets
    } == {("in_batch", b.id)}  # the re-claimed targets untouched
    async with engine.connect() as conn:
        a_costs = (await conn.execute(_ATTEMPT_COSTS, {"s": a.id})).all()
    assert {row.outcome for row in a_costs} == {"released"}
    settled = {c: entries[c].settled_usd for c in ids}
    assert [row.cost_usd for row in a_costs] == [
        settled[c] for c in ids
    ]  # late billed its cost, 0 not billed
    follow_ons = [op for op in await operations_of(engine, "batch_submit") if op.id != setup.operation_id]
    assert [op.payload["after_submission_id"] for op in follow_ons] == [a.id]  # none at the adoption
