"""The ambiguous-submit recovery (spec §10.10 L3984-3999, §18.3 C20-C22; plan Task 17, Rulings P16, chair
decision C3, Codex r1 #23, #24, r5 B5, B6, r6 B2, B3, r7 B3, A2 B12, Carry T8-g) on the fake Batches API.

- C20: a create whose answer was lost leaves the submission `submit_ambiguous`; the reconciler confirms its
  batch by `custom_id`-set equality once the batch has ended, adopts it and reconciles it in the same pass
  (no `batch_poll` between), and the results publish.
- P16, Codex r1 #23, r6 B3: an execution lost after `submit_started` (before the send, or after it) is
  adopted when its batch exists, and left `submit_ambiguous` when none does.
- Codex r5 B5, r6 B2: a confirmed `submit_ambiguous` and a confirmed `released` submission each end
  `reconciled` in the adoption's own pass; a candidate whose results lack one of our items is a partial
  intersection: `batch.custom_id_anomaly`, nothing adopted.
- Codex r5 B6: a batch item parses with its tuple version's parser family.
- C21, C22, chair decision C3: no batch created, or the listing failing for 26 h: never a transition, the
  critical `batch.submit_ambiguous_unresolved` at 25 h, and the user's release; a released submission older
  than 29 days is listed no more.
- Codex r1 #24, r7 B3, Carry T8-g: an adopted batch has ended, so its recorded cancel is a record only; the
  provider cancel goes out once where a provider id becomes known for an unfinished batch (`record_created`
  during the send, `batch_poll` on a `submitted` batch still in progress).

The targets are 250 synthetic ads of the score tuple in one bulk operation; every datum is invented.
"""

import asyncio
import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from typing import Any

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.llm.batches import BatchApiError, BatchesClient, BatchRequest, BatchStatus, Created
from jobhunter.llm.fake import BatchScript
from jobhunter.scoring import batch_adoption, batch_items, batch_results, cycles, validator
from jobhunter.scoring import batch_operations as batch_ops
from jobhunter.scoring import batches as batches_mod
from jobhunter.types import JSONValue
from jobhunter.worker.epoch import WorkerIdentity, claim_epoch_and_sweep
from jobhunter.worker.lock import LockSession
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.batches import (
    PUBLISHED_BODY,
    BulkTargets,
    batch_ledger,
    bulk_with_targets,
    decisions,
    make_due,
    open_alert_codes,
    poll_once,
    reconcile_ambiguous_once,
    submission_rows,
    submit_batch,
    targets_by_custom_id,
)
from tests.support.faults import PausePoint, no_fault, raising_at
from tests.support.fixtures import count_rows
from tests.support.llm import FakeLLM
from tests.support.profile import OpRunner
from tests.support.scoring import attempt_outcomes, targets_by_state

pytestmark = pytest.mark.integration
N = 250
UNRESOLVED = batches_mod.UNRESOLVED_ALERT
_ANOMALY_CONTEXT = sa.text("SELECT context FROM alerts WHERE code = 'batch.custom_id_anomaly'")


@pytest.fixture
async def setup(
    engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int, queue_app: procrastinate.App
) -> BulkTargets:
    """`N` targets of a bulk operation, each answered by `PUBLISHED_BODY`, and its `batch_submit`."""
    made = await bulk_with_targets(engine, N)
    for target in made.targets:
        fake_llm.answer("generate", target.job_revision_id, PUBLISHED_BODY)
    return made


@pytest.fixture
def cancels(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Every provider cancel any `BatchesClient` sends in the test, by batch id."""
    sent: list[str] = []
    real = BatchesClient.cancel

    async def counting(self: BatchesClient, batch_id: str) -> BatchStatus:
        sent.append(batch_id)
        return await real(self, batch_id)

    monkeypatch.setattr(BatchesClient, "cancel", counting)
    yield sent


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Every provider retrieval and cancel any `BatchesClient` sends in the test, in order."""
    sent: list[str] = []
    real_retrieve, real_cancel = BatchesClient.retrieve, BatchesClient.cancel

    async def retrieving(self: BatchesClient, batch_id: str) -> BatchStatus:
        sent.append("retrieve")
        return await real_retrieve(self, batch_id)

    async def cancelling(self: BatchesClient, batch_id: str) -> BatchStatus:
        sent.append("cancel")
        return await real_cancel(self, batch_id)

    monkeypatch.setattr(BatchesClient, "retrieve", retrieving)
    monkeypatch.setattr(BatchesClient, "cancel", cancelling)
    yield sent


async def ambiguous(engine: AsyncEngine, op_runner: OpRunner, setup: BulkTargets) -> Any:
    """The setup's operation run with the fake's current script (an ambiguous `create`): the
    `submit_ambiguous` row."""
    assert setup.operation_id is not None
    await op_runner.run_one(setup.operation_id)
    rows = [r for r in await submission_rows(engine) if r.state == "submit_ambiguous"]
    assert len(rows) == 1, [r.state for r in await submission_rows(engine)]
    return rows[0]


async def release(engine: AsyncEngine, submission_id: int) -> batches_mod.Released:
    """The route's transaction (Task 21): `active_config FOR SHARE`, then `batches.release`."""
    async with engine.begin() as conn:
        await cycles.lock_active_scoring(conn)
        return await batches_mod.release(conn, submission_id, now=utcnow(), defer=defer_in_tx)


async def test_c20_an_ambiguous_create_is_confirmed_by_its_custom_ids_after_it_ends(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, setup: BulkTargets
) -> None:
    """C20: the batch was created, its answer lost. A pass while it is in progress adopts nothing (only an
    ended candidate is confirmed) and records the pass; once it has ended, the pass downloads its results
    whole, finds our `custom_id` set, adopts it and reconciles it; the results publish."""
    fake_llm.batches(BatchScript(create="ambiguous_created", polls_until_ended=5))
    row = await ambiguous(engine, op_runner, setup)
    first = await reconcile_ambiguous_once(engine, fake_llm)
    assert (first.listed, first.adopted) == (1, 0)
    (row,) = await submission_rows(engine)
    assert (row.state, row.provider_batch_id) == ("submit_ambiguous", None) and row.last_reconcile_at
    assert row.reconcile_report == {"error_class": "APIConnectionError", "candidates": 1, "listed": 1}
    fake_llm.batches(BatchScript(polls_until_ended=0))  # it ends at its next observation
    second = await reconcile_ambiguous_once(engine, fake_llm)
    assert (second.adopted, second.reconciled) == (1, 1)
    (row,) = await submission_rows(engine)
    assert row.state == "reconciled" and row.provider_batch_id == "msgbatch_1"
    assert row.results_complete_at is not None and row.ended_at is not None
    assert Counter(await decisions(engine)) == {"published": N}
    assert await count_rows(engine, "llm_results") == N
    assert Counter(state for state, _ in await batch_ledger(engine)) == {"billed": N}


def claimant(lock: LockSession, pid: int) -> WorkerIdentity:
    assert lock.backend_pid is not None
    return WorkerIdentity(uuid.uuid4(), "h", pid, lock.backend_pid, "v")


@pytest.mark.parametrize("point", [batch_ops.AFTER_SUBMIT_START, batch_ops.AFTER_CREATE])
async def test_a_lost_execution_is_adopted_or_left_ambiguous(
    engine: AsyncEngine,
    op_runner: OpRunner,
    fake_llm: FakeLLM,
    setup: BulkTargets,
    worker_lock: LockSession,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
) -> None:
    """Ruling P16 (Task 16's lost-execution setup, its adoption half), Codex r1 #23, r6 B3: the execution
    killed with the row `submit_started`; the takeover and the re-deferred operation leave it
    `submit_ambiguous`. Killed after the create, its batch exists: the reconciler confirms it and the adoption
    pass reconciles it with `results_complete_at` set. Killed before, no batch exists: it stays
    `submit_ambiguous` with no marker."""
    fake_llm.batches(BatchScript(create="ok", polls_until_ended=0))
    monkeypatch.setattr(batch_ops, "afault_point", raising_at(point))
    await op_runner.run_pending()  # the execution dies with the row submit_started
    await claim_epoch_and_sweep(engine, claimant(worker_lock, 2))
    monkeypatch.setattr(batch_ops, "afault_point", no_fault)
    op_runner.epoch = 2
    await make_due(engine)
    await op_runner.run_pending()  # the operation recovers, never resends
    (row,) = await submission_rows(engine)
    assert row.state == "submit_ambiguous"
    report = await reconcile_ambiguous_once(engine, fake_llm, epoch=2)
    (row,) = await submission_rows(engine)
    if point == batch_ops.AFTER_CREATE:
        assert (report.adopted, report.reconciled) == (1, 1)
        assert row.state == "reconciled" and row.results_complete_at is not None
        assert await count_rows(engine, "llm_results") == N
    else:
        assert (report.listed, report.adopted) == (0, 0)
        assert (row.state, row.results_complete_at) == ("submit_ambiguous", None)
        assert await count_rows(engine, "score_targets", state="in_batch") == N  # still quarantined


async def test_adoption_reconciles_in_its_own_pass(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, setup: BulkTargets
) -> None:
    """Codex r5 B5, r6 B2: submission A is released (its follow-on submits B, ambiguous too); one adoption
    pass confirms both, each by its own `custom_id` set (the other's batch, of the same size and in the same
    window, is disjoint), and ends each `reconciled` with `results_complete_at` set, no `batch_poll` between.
    A's items are all `ignored_fenced` (its targets were released, L3999); B's publish."""
    fake_llm.batches(BatchScript(create="ambiguous_created", polls_until_ended=5))
    a = await ambiguous(engine, op_runner, setup)
    await release(engine, a.id)
    await op_runner.run_pending()  # A's follow-on prepares B from the released targets
    rows = {r.id: r for r in await submission_rows(engine)}
    (b,) = [r for r in rows.values() if r.id != a.id]
    assert (rows[a.id].state, b.state) == ("released", "submit_ambiguous")
    fake_llm.batches(BatchScript(polls_until_ended=0))
    report = await reconcile_ambiguous_once(engine, fake_llm)
    assert (report.submissions, report.adopted, report.reconciled) == (2, 2, 2)
    rows = {r.id: r for r in await submission_rows(engine)}
    assert {(r.state, r.results_complete_at is not None) for r in rows.values()} == {("reconciled", True)}
    assert rows[a.id].released_at is not None  # adopted outside the singleton
    assert {rows[a.id].provider_batch_id, rows[b.id].provider_batch_id} == {"msgbatch_1", "msgbatch_2"}
    assert Counter(await decisions(engine)) == {"ignored_fenced": N, "published": N}
    assert await targets_by_state(engine, setup.llm_tuple_id) == {"published_accepted": N}


async def test_one_submissions_error_leaves_the_others_to_the_adoption_pass(
    engine: AsyncEngine,
    op_runner: OpRunner,
    fake_llm: FakeLLM,
    setup: BulkTargets,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruling T17-a: the reconciliation isolates each submission: A's unexpected error (here its items
    cannot be read) is logged with its class and counted, A is left as it was, and B is still adopted and
    reconciled in the same pass; the next pass, without the error, adopts A."""
    fake_llm.batches(BatchScript(create="ambiguous_created", polls_until_ended=5))
    a = await ambiguous(engine, op_runner, setup)
    await release(engine, a.id)
    await op_runner.run_pending()  # A's follow-on prepares B from the released targets
    (b,) = [r for r in await submission_rows(engine) if r.id != a.id]
    real = batch_results.items_of

    async def failing(conn: Any, submission_id: int) -> Any:
        if submission_id == a.id:
            raise RuntimeError("test: the items cannot be read")
        return await real(conn, submission_id)

    monkeypatch.setattr(batch_adoption, "items_of", failing)
    fake_llm.batches(BatchScript(polls_until_ended=0))
    report = await reconcile_ambiguous_once(engine, fake_llm)
    assert (report.submissions, report.failed, report.adopted, report.reconciled) == (2, 1, 1, 1)
    rows = {r.id: r for r in await submission_rows(engine)}
    assert (rows[a.id].state, rows[b.id].state) == ("released", "reconciled")
    monkeypatch.undo()
    again = await reconcile_ambiguous_once(engine, fake_llm)
    assert (again.submissions, again.failed, again.adopted) == (1, 0, 1)
    assert {r.state for r in await submission_rows(engine)} == {"reconciled"}


async def test_a_partial_intersection_is_an_anomaly_and_never_adopted(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, setup: BulkTargets
) -> None:
    """Rule 4 (L3990): a candidate whose results lack one of our items intersects ours partially: the
    critical `batch.custom_id_anomaly`; the submission stays `submit_ambiguous`, nothing adopted."""
    fake_llm.batches(BatchScript(create="ambiguous_created", polls_until_ended=0))
    row = await ambiguous(engine, op_runner, setup)
    lost = f"t{setup.targets[7].target_id}_a1"
    fake_llm.batches(BatchScript(items={lost: "missing"}, polls_until_ended=0))
    report = await reconcile_ambiguous_once(engine, fake_llm)
    assert (report.anomalies, report.adopted) == (1, 0)
    (row,) = await submission_rows(engine)
    assert (row.state, row.provider_batch_id, row.results_complete_at) == ("submit_ambiguous", None, None)
    assert await open_alert_codes(engine) == ["batch.custom_id_anomaly"]
    async with engine.connect() as conn:
        context = (await conn.execute(_ANOMALY_CONTEXT)).scalar_one()
    assert context == {"submission_id": row.id, "foreign_lines": 0, "lacking_lines": 1}  # a subset of ours
    assert await count_rows(engine, "batch_items", result_type="awaiting") == N


async def partial_batch(fake_llm: FakeLLM, ours: list[str], *, shared: int) -> str:
    """A batch of the workspace with as many requests as ours, `shared` of them with our `custom_id`s (no
    submission of ours made it): its id."""
    params: dict[str, JSONValue] = {"model": "claude-sonnet-5-5", "max_tokens": 256}
    fake_llm.batches(BatchScript(create="ok", polls_until_ended=0))
    ids = ours[:shared] + [f"t{900000 + i}_a9" for i in range(len(ours) - shared)]
    async with fake_llm.client() as http:
        created = await BatchesClient(fake_llm.cfg, http_client=http).create(
            [BatchRequest(cid, params) for cid in ids]
        )
    assert isinstance(created, Created)
    return created.provider_batch_id


@pytest.mark.parametrize("ours_first", [True, False])
async def test_the_adoption_does_not_depend_on_the_listing_order(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, setup: BulkTargets, ours_first: bool
) -> None:
    """Minor 4 (rule 3 and rule 4, L3989-3990): every ended candidate of the window is downloaded before any
    decision; our batch is adopted only when it is the one candidate whose `custom_id` set equals ours and no
    candidate intersects ours partially. With a partially intersecting batch of the same size in the window,
    listed after ours or before it, the submission stays `submit_ambiguous` and `batch.custom_id_anomaly`
    names how many of its lines were foreign and how many of ours it lacked."""
    ours = [f"t{t.target_id}_a1" for t in setup.targets]
    if not ours_first:  # the partial batch is the older one: listed after ours (newest first)
        await partial_batch(fake_llm, ours, shared=100)
    fake_llm.batches(BatchScript(create="ambiguous_created", polls_until_ended=0))
    row = await ambiguous(engine, op_runner, setup)
    fake_llm.batches(BatchScript(polls_until_ended=0))
    if ours_first:  # the partial batch is the newer one: listed before ours
        await partial_batch(fake_llm, ours, shared=100)
    report = await reconcile_ambiguous_once(engine, fake_llm)
    assert (report.listed, report.adopted, report.anomalies) == (2, 0, 1)
    (row,) = await submission_rows(engine)
    assert (row.state, row.provider_batch_id) == ("submit_ambiguous", None)
    async with engine.connect() as conn:
        context = (await conn.execute(_ANOMALY_CONTEXT)).scalar_one()
    assert context == {"submission_id": row.id, "foreign_lines": N - 100, "lacking_lines": N - 100}


async def test_the_adoption_decides_nothing_while_a_candidate_cannot_be_read_whole(
    engine: AsyncEngine,
    op_runner: OpRunner,
    fake_llm: FakeLLM,
    setup: BulkTargets,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 3 and rule 4 (L3989-3990): an ended candidate of the window whose results cannot be read whole may
    intersect ours, so a pass with one decides nothing for the submission, not even with our exact batch
    beside it: nothing adopted, no anomaly, the row untouched; the next pass, which reads every candidate
    whole, adopts ours (the other is disjoint)."""
    ours = [f"t{t.target_id}_a1" for t in setup.targets]
    fake_llm.batches(BatchScript(create="ambiguous_created", polls_until_ended=0))
    await ambiguous(engine, op_runner, setup)
    (before,) = await submission_rows(engine)
    other = await partial_batch(fake_llm, ours, shared=0)
    real = BatchesClient.results

    async def failing(self: BatchesClient, batch_id: str) -> Any:
        if batch_id == other:
            raise BatchApiError("results", status=500, error_class="InternalServerError")
        return await real(self, batch_id)

    monkeypatch.setattr(BatchesClient, "results", failing)
    report = await reconcile_ambiguous_once(engine, fake_llm)
    assert (report.listed, report.adopted, report.anomalies) == (2, 0, 0)
    assert report.unread == 1
    (row,) = await submission_rows(engine)
    assert (row.state, row.provider_batch_id) == ("submit_ambiguous", None)
    assert (row.last_reconcile_at, row.reconcile_report) == (
        before.last_reconcile_at,
        before.reconcile_report,
    )
    monkeypatch.setattr(BatchesClient, "results", real)
    report = await reconcile_ambiguous_once(engine, fake_llm)
    assert (report.adopted, report.reconciled, report.unread) == (1, 1, 0)


async def test_a_batch_item_parses_with_the_stored_family(
    engine: AsyncEngine,
    op_runner: OpRunner,
    fake_llm: FakeLLM,
    queue_app: procrastinate.App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex r5 B6: `process_item` parses a valid item with its tuple version's `parser_family`, as
    `load_context` loads it."""
    submitted = await submit_batch(engine, op_runner, fake_llm, N)
    calls: list[dict[str, Any]] = []
    real = validator.parse_generator

    def spy(wire: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return real(wire, **kwargs)

    monkeypatch.setattr(batch_items, "parse_generator", spy)
    fake_llm.batches(BatchScript(polls_until_ended=0))
    await poll_once(engine, fake_llm)
    assert len(calls) == N and all(kwargs == {"family": "generator_v2_items"} for kwargs in calls)
    assert await count_rows(engine, "llm_results") == N and submitted.submission_id


async def test_c21_no_batch_created_stays_quarantined_until_the_users_release(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, setup: BulkTargets
) -> None:
    """C21, chair decision C3: the connection dropped before the batch existed; a complete listing finds no
    candidate and the submission stays `submit_ambiguous` with its spend reserved and counted; at 25 h the
    critical alert (with the amount it holds), never a transition; after the release the targets are
    `pending`, the attempts `released`, the spend `ambiguous` and the alert resolved."""
    fake_llm.batches(BatchScript(create="ambiguous_not_created"))
    row = await ambiguous(engine, op_runner, setup)
    started = row.submit_started_at
    for hours in (1, 24):
        report = await reconcile_ambiguous_once(engine, fake_llm, now=started + timedelta(hours=hours))
        assert (report.listed, report.adopted, report.unresolved) == (0, 0, 0)
    report = await reconcile_ambiguous_once(engine, fake_llm, now=started + timedelta(hours=25, minutes=1))
    assert report.unresolved == 1
    (row,) = await submission_rows(engine)
    assert row.state == "submit_ambiguous" and row.reconcile_report["candidates"] == 0
    assert Counter(state for state, _ in await batch_ledger(engine)) == {"dispatched": N}
    assert await open_alert_codes(engine) == [UNRESOLVED]
    assert (
        await reconcile_ambiguous_once(engine, fake_llm, now=started + timedelta(hours=26))
    ).unresolved == 0
    released = await release(engine, row.id)
    assert released.reserved_usd == row.reserved_usd
    (row,) = await submission_rows(engine)
    assert row.state == "released" and row.released_at is not None
    assert await targets_by_state(engine, setup.llm_tuple_id) == {"pending": N}
    assert await attempt_outcomes(engine, setup.llm_tuple_id) == {"released": N}
    assert Counter(state for state, _ in await batch_ledger(engine)) == {"ambiguous": N}
    assert await open_alert_codes(engine) == []


async def test_c22_a_failing_listing_never_moves_the_submission_and_alerts_at_25_h(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, setup: BulkTargets
) -> None:
    """C22: the listing fails for 26 h on the injected clock: every row untouched (`batch_listing_failed`),
    no transition, the critical alert at 25 h."""
    fake_llm.batches(BatchScript(create="ambiguous_created", polls_until_ended=0))
    row = await ambiguous(engine, op_runner, setup)
    fake_llm.batches(BatchScript(listing="error_500", polls_until_ended=0))
    started = row.submit_started_at
    alerts = []
    for hours in range(0, 27):
        report = await reconcile_ambiguous_once(
            engine, fake_llm, now=started + timedelta(hours=hours, seconds=1)
        )
        assert (report.listed, report.adopted) == (None, 0)
        alerts.append(report.unresolved)
    assert alerts.index(1) == 25 and sum(alerts) == 1
    (after,) = await submission_rows(engine)
    assert (after.state, after.last_reconcile_at, after.reconcile_report) == (
        "submit_ambiguous",
        None,
        row.reconcile_report,
    )
    assert Counter(state for state, _ in await batch_ledger(engine)) == {"dispatched": N}


async def test_a_released_submission_is_listed_for_29_days_only(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, setup: BulkTargets
) -> None:
    """A2 B12: a `released` submission is reconciled while `submit_started_at > now - 29 days`; after that
    it is `released` for good and no listing is made for it."""
    fake_llm.batches(BatchScript(create="ambiguous_not_created"))
    row = await ambiguous(engine, op_runner, setup)
    await release(engine, row.id)
    fake_llm.batches(BatchScript(listing="error_500"))  # a listing made would fail: `listed` None
    started = row.submit_started_at
    within = await reconcile_ambiguous_once(engine, fake_llm, now=started + timedelta(days=28, hours=23))
    assert (within.submissions, within.listed) == (1, None)
    after = await reconcile_ambiguous_once(engine, fake_llm, now=started + timedelta(days=29, minutes=1))
    assert after == batch_adoption.ReconcileReport()  # nothing to reconcile, nothing listed
    assert (await submission_rows(engine))[0].state == "released"


async def test_an_adopted_batch_keeps_its_recorded_cancel_and_is_not_cancelled(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, setup: BulkTargets, cancels: list[str]
) -> None:
    """Codex r1 #24, r7 B3: a confirmed candidate has ended, so a request recorded on the ambiguous
    submission is carried forward as a record only: no cancel is sent, the targets the request cancelled stay
    cancelled, and every item is `ignored_fenced` with its spend settled."""
    fake_llm.batches(BatchScript(create="ambiguous_created", polls_until_ended=0))
    row = await ambiguous(engine, op_runner, setup)
    async with engine.begin() as conn:  # the tuple superseded meanwhile: the request recorded
        requested = await batches_mod.request_cancel(conn, llm_tuple_id=setup.llm_tuple_id, now=utcnow())
    assert (requested.submissions, requested.targets) == (1, N)
    report = await reconcile_ambiguous_once(engine, fake_llm)
    assert (report.adopted, report.reconciled) == (1, 1)
    (row,) = await submission_rows(engine)
    assert row.state == "reconciled" and row.cancel_requested_at is not None
    assert cancels == []
    assert set(await decisions(engine)) == {"ignored_fenced"}
    assert await targets_by_state(engine, setup.llm_tuple_id) == {"cancelled": N}
    assert Counter(state for state, _ in await batch_ledger(engine)) == {"billed": N}


async def test_record_created_sends_a_cancel_recorded_during_the_send_once(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, setup: BulkTargets, cancels: list[str]
) -> None:
    """Codex r1 #24, r7 B3: the confirm comes during the send; `record_created` carries the request and the
    worker calls `cancel` right after its commit, exactly once; the next poll finds the batch ending."""
    fake_llm.batches(BatchScript(create="ok", polls_until_ended=5))  # in progress at the cancel (T8-g)
    pause = PausePoint(batch_ops.AFTER_SUBMIT_START)
    assert setup.operation_id is not None
    running = asyncio.create_task(op_runner.run_one(setup.operation_id, pause=pause))
    await asyncio.wait_for(pause.reached.wait(), 60)
    async with engine.begin() as conn:
        await batches_mod.request_cancel(conn, llm_tuple_id=setup.llm_tuple_id, now=utcnow())
    pause.resume()
    await asyncio.wait_for(running, 120)
    (row,) = await submission_rows(engine)
    assert row.state == "submitted" and cancels == [row.provider_batch_id]
    assert fake_llm.state.batches[row.provider_batch_id].processing_status == "canceling"


async def test_batch_poll_sends_the_recorded_cancel_of_a_batch_in_progress_once(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, setup: BulkTargets, cancels: list[str]
) -> None:
    """Codex r1 #24, Carry T8-g: a `submitted` batch still in progress when its tuple is superseded; the poll
    sends the cancel once, then retrieves the batch, which has ended at its cancel: the items the provider
    canceled are `ignored_fenced` (A2 B1) and spend nothing."""
    fake_llm.batches(BatchScript(create="ok", polls_until_ended=5))
    assert setup.operation_id is not None
    await op_runner.run_one(setup.operation_id)
    async with engine.begin() as conn:
        await batches_mod.request_cancel(conn, llm_tuple_id=setup.llm_tuple_id, now=utcnow())
    report = await poll_once(engine, fake_llm)
    (row,) = await submission_rows(engine)
    assert (report.cancels, report.ended, cancels) == (1, 1, [row.provider_batch_id])
    assert row.state == "reconciled"
    assert set(await decisions(engine)) == {"ignored_fenced"}
    assert set(await batch_ledger(engine)) == {("not_billed", "batch_item_canceled")}
    targets = await targets_by_custom_id(engine)
    assert {(t.state, t.outcome) for t in targets.values()} == {("cancelled", "cancelled")}


async def test_a_failing_cancel_is_tried_cancel_tries_times_per_pass_then_given_up(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, setup: BulkTargets, calls: list[str]
) -> None:
    """L6443: the poll retrieves the batch first and, while it is `in_progress`, sends the recorded cancel up
    to `CANCEL_TRIES` times; a cancel failing every time is given up for the pass (the submission stays
    `submitted`) and sent again by the next poll; once one is accepted, the batch is retrieved again, ended
    at its cancel, and reconciled in that pass."""
    fake_llm.batches(BatchScript(create="ok", polls_until_ended=5, cancel="error_500"))
    assert setup.operation_id is not None
    await op_runner.run_one(setup.operation_id)
    async with engine.begin() as conn:
        await batches_mod.request_cancel(conn, llm_tuple_id=setup.llm_tuple_id, now=utcnow())
    given_up = ["retrieve", *["cancel"] * batch_results.CANCEL_TRIES]
    for _ in range(2):
        calls.clear()
        report = await poll_once(engine, fake_llm)
        assert (report.cancels, report.ended, calls) == (0, 0, given_up)
        (row,) = await submission_rows(engine)
        assert row.state == "submitted"
    fake_llm.batches(BatchScript(create="ok", polls_until_ended=5))
    calls.clear()
    report = await poll_once(engine, fake_llm)
    assert (report.cancels, report.ended, calls) == (1, 1, ["retrieve", "cancel", "retrieve"])
    (row,) = await submission_rows(engine)
    assert row.state == "reconciled"
    assert set(await decisions(engine)) == {"ignored_fenced"}


@pytest.mark.parametrize("seen", ["ended", "canceling"])
async def test_no_cancel_is_sent_to_a_batch_no_longer_in_progress(
    engine: AsyncEngine,
    op_runner: OpRunner,
    fake_llm: FakeLLM,
    setup: BulkTargets,
    calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
    seen: str,
) -> None:
    """L6443: the poll retrieves a `submitted` batch whose cancel is recorded before it sends one; an ended
    batch has nothing to cancel (it moves to `ended` and is reconciled) and a `canceling` one has its cancel
    already (it stays `submitted`): no cancel either way."""
    fake_llm.batches(BatchScript(create="ok", polls_until_ended=0 if seen == "ended" else 5))
    assert setup.operation_id is not None
    await op_runner.run_one(setup.operation_id)
    async with engine.begin() as conn:
        await batches_mod.request_cancel(conn, llm_tuple_id=setup.llm_tuple_id, now=utcnow())
    if seen == "canceling":  # the provider still winding a cancel accepted earlier down
        counted = BatchesClient.retrieve

        async def canceling(self: BatchesClient, batch_id: str) -> BatchStatus:
            return replace(await counted(self, batch_id), processing_status="canceling")

        monkeypatch.setattr(BatchesClient, "retrieve", canceling)
    calls.clear()
    report = await poll_once(engine, fake_llm)
    (row,) = await submission_rows(engine)
    assert (report.cancels, calls) == (0, ["retrieve"])
    assert row.state == ("reconciled" if seen == "ended" else "submitted")
