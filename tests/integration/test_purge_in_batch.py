"""Purging an in-batch target (spec §20.2, §20.4, §10.10; plan M5 Task 11, P1, P2, G04, M5-R3, Deviation 13).

M3's `targets.delete_target_set` alone deletes a batched target's item and call, so the batch's late line for
it names no item of ours: `batch.custom_id_anomaly`, and the submission stays `ended` for good, holding
`batch_one_in_flight` (P1). `privacy.deletion_graph.purge_target_set` abandons a `preparing` submission,
records the cancel of a later one (sent after the commit), settles the set's batch entries `ambiguous`,
records `purged_batch_items` for every deleted item of a submission that can still return results (the
evidence on which the poll skips a line as `purged` and the adoption still matches the original membership),
and settles the online entries before their calls go (G04). A fenced item stores no output (P2), and a line
of no item without that evidence stays foreign.

Each purge runs as Tasks 12-16 run a chunk (`purge`): its A0 committed first (a manifest and a `jobs` fence
over the targets' jobs), then `active_config FOR SHARE`, the job rows `FOR UPDATE` in id order,
`purge_target_set`, and the ranking bumped last. The batches are driven step by step (the preparation, then
the handler's submit-start, owner check, create on the fake Batches API and recording), so a submission holds
two targets. Every datum is invented.
"""

import asyncio
import logging
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db import ranking
from jobhunter.db.sql import is_unique_violation
from jobhunter.errors import FencedOut
from jobhunter.llm import spend_ledger
from jobhunter.llm.batches import Ambiguous, BatchesClient, BatchRequest, BatchStatus, Created
from jobhunter.llm.fake import BatchScript
from jobhunter.llm.gateway import LLMGateway
from jobhunter.llm.spend_ledger import Reservation, TargetAttemptOwner
from jobhunter.privacy.deletion_graph import PurgedTargets, purge_target_set
from jobhunter.privacy.fences import FenceSpec, insert_fence, lock_jobs_shared
from jobhunter.scoring import batch_items, batch_results, batches, cycles
from jobhunter.scoring.targets import REVISION_SUPERSEDED, cancel_targets, delete_target_set
from jobhunter.settings.dryrun import active_settings
from jobhunter.settings.model import Settings
from jobhunter.worker.operations import OperationClaim
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.batches import (
    PUBLISHED_BODY,
    BulkTargets,
    batch_client,
    bulk_with_targets,
    decisions,
    eligible_for_batching,
    entries_by_custom_id,
    item_ids,
    item_rows,
    items_of,
    open_alert_codes,
    poll_once,
    prepared_submission,
    reconcile_ambiguous_once,
    submission_rows,
    targets_by_custom_id,
)
from tests.support.collect import ctx
from tests.support.fences import insert_test_manifest
from tests.support.fixtures import count_rows
from tests.support.llm import USAGE, FakeLLM, _read_execution_config, budget_rows, completed_call, scoring_kw
from tests.support.logs import logged
from tests.support.m3 import insert_submission
from tests.support.pg import PgServer, lock_wait_details
from tests.support.procs import await_until
from tests.support.scoring import PendingTarget, claim_again

pytestmark = pytest.mark.integration
N: Final = 2
ANOMALY: Final = batch_results.CUSTOM_ID_ANOMALY_ALERT
_LOCK_JOBS: Final = sa.text(
    "SELECT id FROM jobs WHERE id = ANY(CAST(:ids AS bigint[])) ORDER BY id FOR UPDATE"
)
_ENTRY_IDS: Final = sa.text(
    "SELECT i.custom_id, c.spend_ledger_id FROM batch_items i JOIN llm_calls c ON c.id = i.llm_call_id "
    "WHERE i.submission_id = :s"
)
_LEDGER: Final = sa.text(
    "SELECT l.state, l.evidence, l.reserved_usd, l.settled_usd, "
    "EXISTS (SELECT 1 FROM llm_calls c WHERE c.spend_ledger_id = l.id) AS has_call "
    "FROM llm_spend_ledger l WHERE l.id = :id"
)
_PURGED_ITEMS: Final = sa.text(
    "SELECT submission_id, custom_id, manifest_id FROM purged_batch_items ORDER BY submission_id, custom_id"
)
# G04: an unsettled entry whose call is gone, which no reader of an online entry finds again.
_STRANDED: Final = sa.text(
    "SELECT count(*) FROM llm_spend_ledger l WHERE l.state IN ('reserved', 'dispatched') "
    "AND NOT EXISTS (SELECT 1 FROM llm_calls c WHERE c.spend_ledger_id = l.id)"
)
_ANOMALIES: Final = sa.text("SELECT context FROM alerts WHERE code = :c AND resolved_at IS NULL")
_MONTHS: Final = sa.text(
    "SELECT DISTINCT budget_month FROM llm_spend_ledger WHERE id = ANY(CAST(:ids AS bigint[]))"
)
_BULK: Final = sa.text("SELECT spent_usd, followup_held_usd FROM bulk_operations WHERE id = :b")
_TARGET: Final = sa.text(
    "SELECT t.state, t.batch_submission_id, a.outcome FROM score_targets t "
    "JOIN score_attempts a ON a.target_id = t.id AND a.attempt_no = t.attempt_no WHERE t.id = :t"
)


# ---- the setup and the batch, step by step -----------------------------------------------------------------


@pytest.fixture
async def gateway(engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int) -> LLMGateway:
    async with engine.connect() as conn:  # `scoring_kw` names the seeded execution config
        await _read_execution_config(conn)
    return fake_llm.gateway(engine)


async def answered(engine: AsyncEngine, fake_llm: FakeLLM, n: int) -> BulkTargets:
    """`n` pending targets of a bulk operation and its `batch_submit`, each answered by `PUBLISHED_BODY`."""
    made = await bulk_with_targets(engine, n)
    for target in made.targets:
        fake_llm.answer("generate", target.job_revision_id, PUBLISHED_BODY)
    return made


@pytest.fixture
async def setup(
    engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int, queue_app: procrastinate.App
) -> BulkTargets:
    return await answered(engine, fake_llm, N)


@pytest.fixture
def infos(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """`caplog` capturing the INFO events of the result reader and of the spend ledger too."""
    for logger in ("jobhunter.scoring.batch_results", "jobhunter.llm.spend_ledger"):
        caplog.set_level(logging.INFO, logger=logger)
    return caplog


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


@dataclass(frozen=True)
class Batch:
    """A submission of the setup's targets: its id, the claim of the operation that prepared it, its requests
    and, once known, its provider batch."""

    setup: BulkTargets
    submission_id: int
    claim: OperationClaim
    requests: tuple[BatchRequest, ...]
    provider_id: str | None = None

    def custom_id(self, target: PendingTarget) -> str:
        (found,) = [r.custom_id for r in self.requests if r.custom_id.startswith(f"t{target.target_id}_a")]
        return found

    @property
    def custom_ids(self) -> frozenset[str]:
        return frozenset(r.custom_id for r in self.requests)


async def small(engine: AsyncEngine) -> Settings:
    """The active settings with `batch_min_targets` 1, unvalidated (a direct `prepare` takes them)."""
    async with engine.connect() as conn:
        active = await active_settings(conn)
    return active.model_copy(update={"scoring": active.scoring.model_copy(update={"batch_min_targets": 1})})


async def prepared(engine: AsyncEngine, gateway: LLMGateway, setup: BulkTargets) -> Batch:
    """The setup's eligible targets prepared into one `preparing` submission."""
    result, claim = await prepared_submission(engine, gateway, setup, settings=await small(engine))
    assert isinstance(result, batches.Prepared), result
    return Batch(setup, result.submission_id, claim, result.requests)


async def sent(engine: AsyncEngine, fake_llm: FakeLLM, batch: Batch, script: BatchScript) -> Batch:
    """The `batch_submit` handler's steps 4 to 7 (`scoring.batch_operations`): submit-start, the owner checked
    again, the create on the fake under `script`, and its recording: `submitted`, or `submit_ambiguous` for a
    create whose answer was lost (the batch then known by the fake alone)."""
    s = batch.submission_id
    async with engine.begin() as conn:
        assert await batches.submit_start(conn, s, operation=batch.claim, now=utcnow()) == "submit_started"
    async with engine.begin() as conn:
        await batches.check_sendable(conn, s, operation=batch.claim)
    fake_llm.batches(script)
    async with fake_llm.client() as http:
        outcome = await batch_client(fake_llm, http).create(batch.requests)
    async with engine.begin() as conn:
        if isinstance(outcome, Created):
            await batches.record_created(conn, s, outcome, operation=batch.claim, now=utcnow())
            return replace(batch, provider_id=outcome.provider_batch_id)
        assert isinstance(outcome, Ambiguous), outcome
        await batches.record_ambiguous(conn, s, outcome, operation=batch.claim, now=utcnow())
    (provider_id,) = fake_llm.state.batches
    return replace(batch, provider_id=provider_id)


async def release(engine: AsyncEngine, submission_id: int, *, now: datetime) -> None:
    """The user's release (the route's transaction: `active_config FOR SHARE`, then `batches.release`)."""
    async with engine.begin() as conn:
        await cycles.lock_active_scoring(conn)
        await batches.release(conn, submission_id, now=now, defer=defer_in_tx)


async def supersede(engine: AsyncEngine, target: PendingTarget) -> None:
    """Node 9's move for a revision that stopped being current (`active_config FOR SHARE`, the job row `FOR
    SHARE`, then `cancel_targets`): the `in_batch` target cancelled individually, its submission untouched."""
    async with engine.begin() as conn:
        await cycles.lock_active_scoring(conn)
        await lock_jobs_shared(conn, [target.job_id])
        cancelled = await cancel_targets(
            conn,
            llm_tuple_id=target.llm_tuple_id,
            revision_ids=[target.job_revision_id],
            reason=REVISION_SUPERSEDED,
            now=utcnow(),
        )
    assert cancelled.targets == 1


# ---- the purge, as a chunk of Tasks 12-16 runs it ---------------------------------------------------------


async def fenced_manifest(engine: AsyncEngine, targets: Sequence[PendingTarget]) -> int:
    """A purge's A0 (M5 Task 9), committed: its manifest and a `jobs` fence over the targets' jobs."""
    jobs = tuple(sorted({t.job_id for t in targets}))
    async with engine.begin() as conn:
        manifest = await insert_test_manifest(conn, kind="job_tombstone")
        await insert_fence(conn, manifest_id=manifest, spec=FenceSpec("jobs", job_ids=jobs))
    return manifest


async def chunk_locks(conn: AsyncConnection, targets: Sequence[PendingTarget]) -> None:
    """A purge chunk's first locks: `active_config FOR SHARE` (step 2), the job rows `FOR UPDATE` in id order
    (step 4)."""
    await cycles.lock_active_scoring(conn)
    await conn.execute(_LOCK_JOBS, {"ids": sorted({t.job_id for t in targets})})


async def purge(
    engine: AsyncEngine, targets: Sequence[PendingTarget], *, now: datetime | None = None
) -> PurgedTargets:
    """The targets purged by one chunk (module docstring)."""
    manifest = await fenced_manifest(engine, targets)
    async with engine.begin() as conn:
        await chunk_locks(conn, targets)
        purged = await purge_target_set(
            conn, [t.target_id for t in targets], manifest_id=manifest, now=now or utcnow()
        )
        await ranking.bump(conn)  # the caller's, last
    return purged


async def m3_delete(engine: AsyncEngine, targets: Sequence[PendingTarget]) -> None:
    """The M3 path: the same chunk with `targets.delete_target_set` alone."""
    await fenced_manifest(engine, targets)
    async with engine.begin() as conn:
        await chunk_locks(conn, targets)
        await delete_target_set(conn, [t.target_id for t in targets], now=utcnow())


def purged_targets(
    targets: int,
    *,
    cancel_requested: tuple[int, ...] = (),
    abandoned: tuple[int, ...] = (),
    entries_ambiguous: int = 0,
    online_released: int = 0,
    online_ambiguous: int = 0,
) -> PurgedTargets:
    return PurgedTargets(
        targets=targets,
        submissions_cancel_requested=cancel_requested,
        submissions_abandoned=abandoned,
        entries_ambiguous=entries_ambiguous,
        online_entries_released=online_released,
        online_entries_ambiguous=online_ambiguous,
    )


# ---- the readers ------------------------------------------------------------------------------------------


async def entry_ids(engine: AsyncEngine, submission_id: int) -> dict[str, int]:
    """The submission's items' ledger entries by `custom_id` (read while their calls exist)."""
    async with engine.connect() as conn:
        rows = await conn.execute(_ENTRY_IDS, {"s": submission_id})
        return {str(r.custom_id): int(r.spend_ledger_id) for r in rows}


async def ledger(engine: AsyncEngine, ledger_id: int) -> sa.Row[Any]:
    """A ledger row's state, evidence and amounts, and whether its call still exists."""
    async with engine.connect() as conn:
        return (await conn.execute(_LEDGER, {"id": ledger_id})).one()


async def purged_items(engine: AsyncEngine) -> list[tuple[int, str, int]]:
    async with engine.connect() as conn:
        rows = await conn.execute(_PURGED_ITEMS)
        return [(int(r.submission_id), str(r.custom_id), int(r.manifest_id)) for r in rows]


async def anomalies(engine: AsyncEngine) -> list[Any]:
    async with engine.connect() as conn:
        return [r.context for r in await conn.execute(_ANOMALIES, {"c": ANOMALY})]


async def stranded(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(_STRANDED)).scalar_one())


async def target_now(engine: AsyncEngine, target: PendingTarget) -> tuple[str, int | None, str]:
    """The target's state and batch pin, and its current attempt's outcome."""
    async with engine.connect() as conn:
        row = (await conn.execute(_TARGET, {"t": target.target_id})).one()
    return str(row.state), row.batch_submission_id, str(row.outcome)


async def bulk_accounts(engine: AsyncEngine, setup: BulkTargets) -> sa.Row[Any]:
    async with engine.connect() as conn:
        return (await conn.execute(_BULK, {"b": setup.bulk_operation_id})).one()


async def new_submission_starts(engine: AsyncEngine, setup: BulkTargets) -> bool:
    """Whether a new `preparing` submission is accepted now (`batch_one_in_flight` free)."""
    try:
        async with engine.begin() as conn:
            await insert_submission(conn, setup.llm_tuple_id, setup.bulk_operation_id)
    except sa.exc.IntegrityError as exc:
        assert is_unique_violation(exc, "batch_one_in_flight"), exc
        return False
    return True


async def months_agree(engine: AsyncEngine, ledger_ids: Iterable[int]) -> bool:
    """Whether the month rows of these entries count what their ledger rows count (§7.11)."""
    async with engine.connect() as conn:
        months = (await conn.execute(_MONTHS, {"ids": list(ledger_ids)})).scalars().all()
        return all([await spend_ledger.recompute(conn, month) is None for month in months])


async def no_drift(engine: AsyncEngine, res: Reservation) -> bool:
    return await months_agree(engine, [res.ledger_id])


# ---- P1: the batch completes -------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["m3_delete_target_set", "purge_target_set"])
async def test_a_purged_in_batch_target_no_longer_blocks_the_batch(
    engine: AsyncEngine, gateway: LLMGateway, fake_llm: FakeLLM, setup: BulkTargets, path: str
) -> None:
    """P1. The M3 path first (`targets.delete_target_set` alone, the bug it documents): the purged target's
    late line names no item of ours, so `batch.custom_id_anomaly` opens, the submission stays `ended` for
    good with `batch_one_in_flight` taken, and the purged item's entry stays `dispatched` without its call.
    Then the fixed path (`purge_target_set`), each over a database of its own (the singleton): a `submitted`
    submission of two targets, one purged; at its batch's end it reaches `reconciled` in one poll, the purged
    line counted `purged`, no anomaly, the purged item's entry `ambiguous`, the other target published, and a
    new submission can start."""
    batch = await sent(
        engine, fake_llm, await prepared(engine, gateway, setup), BatchScript(polls_until_ended=0)
    )
    gone, kept = setup.targets
    ledger_id = (await entry_ids(engine, batch.submission_id))[batch.custom_id(gone)]
    if path == "m3_delete_target_set":
        await m3_delete(engine, [gone])
        report = await poll_once(engine, fake_llm)
        (row,) = await submission_rows(engine)
        assert (row.state, row.results_complete_at, report.reconciled) == ("ended", None, 0)
        assert await anomalies(engine) == [{"submission_id": batch.submission_id, "foreign_lines": 1}]
        entry = await ledger(engine, ledger_id)
        assert (entry.state, entry.has_call) == ("dispatched", False)  # stranded (G04)
        assert not await new_submission_starts(engine, setup)
        return
    purged = await purge(engine, [gone])
    assert purged == purged_targets(1, cancel_requested=(batch.submission_id,), entries_ambiguous=1)
    report = await poll_once(engine, fake_llm)
    assert (report.ended, report.decided, report.purged, report.reconciled) == (1, 1, 1, 1)
    (row,) = await submission_rows(engine)
    assert row.state == "reconciled" and row.results_complete_at is not None
    assert await open_alert_codes(engine) == []
    entry = await ledger(engine, ledger_id)
    assert (entry.state, entry.has_call) == ("ambiguous", False)
    assert [t.state for t in (await targets_by_custom_id(engine)).values()] == ["published_accepted"]
    assert await target_now(engine, kept) == ("published_accepted", None, "published_accepted")
    assert await new_submission_starts(engine, setup)


async def test_the_submission_cancel_is_recorded_and_sent_after_commit(
    engine: AsyncEngine, gateway: LLMGateway, fake_llm: FakeLLM, setup: BulkTargets, cancels: list[str]
) -> None:
    """§20.2, L6443: the purge records the cancel of the `submitted` submission (`cancel_requested_at`, the
    purge's clock) and sends nothing in its transaction; the next poll retrieves the batch, still in
    progress, and calls `messages.batches.cancel` once; the batch ends at it and is reconciled."""
    batch = await sent(
        engine, fake_llm, await prepared(engine, gateway, setup), BatchScript(polls_until_ended=5)
    )
    assert batch.provider_id is not None
    at = utcnow()
    purged = await purge(engine, [setup.targets[0]], now=at)
    assert purged.submissions_cancel_requested == (batch.submission_id,)
    (row,) = await submission_rows(engine)
    assert (row.state, row.cancel_requested_at) == ("submitted", at)
    assert cancels == [] and not fake_llm.state.batches[batch.provider_id].cancel_requested
    report = await poll_once(engine, fake_llm)
    assert cancels == [batch.provider_id] and report.cancels == 1
    assert fake_llm.state.batches[batch.provider_id].cancel_requested
    assert (report.ended, report.purged, report.reconciled) == (1, 1, 1)


async def test_the_other_target_stays_in_batch_and_retries_online_when_canceled(
    engine: AsyncEngine, gateway: LLMGateway, fake_llm: FakeLLM, setup: BulkTargets
) -> None:
    """P1, M5-R3: the purge cancels the purged target alone; the batch's other target stays `in_batch` under
    its pin with its attempt running. Its item comes back `canceled` (the cancel the purge requested) and
    retries online through `batch_canceled`: `pending('batch_canceled')`, its attempt `transient_error`, its
    entry `not_billed`, never batched again."""
    batch = await sent(
        engine, fake_llm, await prepared(engine, gateway, setup), BatchScript(polls_until_ended=5)
    )
    gone, kept = setup.targets
    held = (await bulk_accounts(engine, setup)).followup_held_usd
    await purge(engine, [gone])
    assert await target_now(engine, kept) == ("in_batch", batch.submission_id, "running")
    assert (await bulk_accounts(engine, setup)).followup_held_usd == held / 2  # the purged item's hold back
    await poll_once(engine, fake_llm)
    (target,) = (await targets_by_custom_id(engine)).values()
    assert (target.state, target.state_reason, target.outcome, target.outcome_detail) == (
        "pending",
        "batch_canceled",
        "transient_error",
        "batch_canceled",
    )
    assert await decisions(engine) == ["retry_online"]
    (entry,) = (await entries_by_custom_id(engine)).values()
    assert (entry.state, entry.evidence) == ("not_billed", "batch_item_canceled")
    assert await eligible_for_batching(engine, setup, [kept.target_id]) == []
    (row,) = await submission_rows(engine)
    assert row.state == "reconciled"


async def test_a_preparing_submission_is_abandoned_and_never_sent(
    engine: AsyncEngine, gateway: LLMGateway, fake_llm: FakeLLM, setup: BulkTargets
) -> None:
    """A purge meeting a `preparing` submission abandons it (`abandon_preparing`): every entry released
    (`not_billed('never_dispatched')`), the holds and the allocation given back, the other target `pending`
    with its attempt `cancelled('batch_abandoned')`; no `purged_batch_items` (nothing can come back). The
    preparing execution's next step is fenced, so nothing is sent."""
    batch = await prepared(engine, gateway, setup)
    ids = await entry_ids(engine, batch.submission_id)
    gone, kept = setup.targets
    purged = await purge(engine, [gone])
    assert purged == purged_targets(1, abandoned=(batch.submission_id,))
    (row,) = await submission_rows(engine)
    assert row.state == "abandoned"
    assert await target_now(engine, kept) == ("pending", None, "cancelled")
    for ledger_id in ids.values():
        entry = await ledger(engine, ledger_id)
        assert (entry.state, entry.evidence, entry.settled_usd) == ("not_billed", "never_dispatched", 0)
    assert tuple(await bulk_accounts(engine, setup)) == (0, 0)
    assert await purged_items(engine) == []
    with pytest.raises(FencedOut):
        async with engine.begin() as conn:
            await batches.submit_start(conn, batch.submission_id, operation=batch.claim, now=utcnow())
    with pytest.raises(FencedOut):
        async with engine.begin() as conn:
            await batches.check_sendable(conn, batch.submission_id, operation=batch.claim)
    assert fake_llm.state.batches == {}
    assert await months_agree(engine, ids.values())


# ---- the ambiguous submission and the released ones --------------------------------------------------------


async def test_a_submit_ambiguous_submission_found_later_settles_without_output(
    engine: AsyncEngine,
    gateway: LLMGateway,
    fake_llm: FakeLLM,
    setup: BulkTargets,
    infos: pytest.LogCaptureFixture,
) -> None:
    """A `submit_ambiguous` submission whose every target is purged: the cancel recorded, both entries
    `ambiguous`. Found later, its batch is adopted on the purge evidence alone (the submission has no item
    left) and reconciled in the same pass, both lines counted `purged`: nothing of the late results is stored,
    no call, draft or result, and the entries stay `ambiguous`."""
    batch = await sent(
        engine,
        fake_llm,
        await prepared(engine, gateway, setup),
        BatchScript(create="ambiguous_created", polls_until_ended=5),
    )
    (row,) = await submission_rows(engine)
    assert row.state == "submit_ambiguous"
    ids = await entry_ids(engine, batch.submission_id)
    purged = await purge(engine, list(setup.targets))
    assert purged == purged_targets(2, cancel_requested=(batch.submission_id,), entries_ambiguous=2)
    fake_llm.batches(BatchScript(polls_until_ended=0))
    report = await reconcile_ambiguous_once(engine, fake_llm)
    assert (report.adopted, report.anomalies, report.reconciled) == (1, 0, 1)
    (row,) = await submission_rows(engine)
    assert (row.state, row.provider_batch_id) == ("reconciled", batch.provider_id)
    assert [line["purged"] for line in logged(infos, "batch_results_purged")] == [2]
    for ledger_id in ids.values():
        entry = await ledger(engine, ledger_id)
        assert (entry.state, entry.has_call) == ("ambiguous", False)
    for table in ("llm_calls", "score_drafts", "llm_results", "batch_items"):
        assert await count_rows(engine, table) == 0, table


async def test_an_ambiguous_submission_with_a_purged_item_is_still_adopted(
    engine: AsyncEngine,
    gateway: LLMGateway,
    fake_llm: FakeLLM,
    setup: BulkTargets,
    infos: pytest.LogCaptureFixture,
) -> None:
    """Rule 1 (L3984-3999): the provider's batch names the original two custom ids while the submission keeps
    one item; its membership is its items plus its purged items, so the batch is `equal`, not `partial`: it is
    adopted, the purged line counted `purged`, the other target published, no anomaly."""
    batch = await sent(
        engine,
        fake_llm,
        await prepared(engine, gateway, setup),
        BatchScript(create="ambiguous_created", polls_until_ended=5),
    )
    assert batch.provider_id is not None
    gone, kept = setup.targets
    await purge(engine, [gone])
    assert await items_of(engine, batch.submission_id) == [(kept.target_id, 1)]
    assert fake_llm.state.batches[batch.provider_id].custom_ids == batch.custom_ids
    fake_llm.batches(BatchScript(polls_until_ended=0))
    report = await reconcile_ambiguous_once(engine, fake_llm)
    assert (report.adopted, report.anomalies, report.reconciled) == (1, 0, 1)
    (row,) = await submission_rows(engine)
    assert (row.state, row.provider_batch_id) == ("reconciled", batch.provider_id)
    assert [line["purged"] for line in logged(infos, "batch_results_purged")] == [1]
    assert await decisions(engine) == ["published"]
    assert await open_alert_codes(engine) == []


async def test_a_released_lost_batch_with_a_purged_item_reconciles_on_late_retrieval(
    engine: AsyncEngine, gateway: LLMGateway, fake_llm: FakeLLM, setup: BulkTargets
) -> None:
    """A lost `submitted` submission (its batch ended, the fake forgot it, `batch.retrieve_failing` open past
    `expires_at + 1 h`) is released, then one of its targets purged: its item is recorded whatever the
    target's state (`pending` since the release), no cancel is requested and no entry moves (the release made
    them `ambiguous`). The poll still observes the released batch: once retrievable it ends, the purged line
    counts `purged`, the other item is `ignored_fenced` and settles from usage with no output stored, no
    anomaly, `reconciled` with `released_at` kept."""
    batch = await sent(
        engine, fake_llm, await prepared(engine, gateway, setup), BatchScript(polls_until_ended=0)
    )
    assert batch.provider_id is not None
    stored = fake_llm.state.batches.pop(batch.provider_id)  # the provider ended it, the fake forgot it
    (row,) = await submission_rows(engine)
    after = row.expires_at + batches.RETRIEVE_FAILING_AFTER + timedelta(minutes=1)
    await poll_once(engine, fake_llm, now=after)
    assert await open_alert_codes(engine) == [batches.RETRIEVE_FAILING_ALERT]
    await release(engine, batch.submission_id, now=after)
    gone, kept = setup.targets
    assert await purge(engine, [gone], now=after) == purged_targets(1)
    assert [(s, c) for s, c, _ in await purged_items(engine)] == [
        (batch.submission_id, batch.custom_id(gone))
    ]
    fake_llm.state.batches[batch.provider_id] = stored  # retrievable again
    report = await poll_once(engine, fake_llm, now=after)
    assert (report.ended, report.purged, report.reconciled) == (1, 1, 1)
    (row,) = await submission_rows(engine)
    assert row.state == "reconciled" and row.released_at is not None
    assert await open_alert_codes(engine) == []
    assert await decisions(engine) == ["ignored_fenced"]
    entry = (await entries_by_custom_id(engine))[batch.custom_id(kept)]
    assert (entry.state, entry.status, entry.has_output) == ("billed", "completed", False)


async def test_a_released_ambiguous_submission_with_a_purged_item_is_still_adopted(
    engine: AsyncEngine, gateway: LLMGateway, fake_llm: FakeLLM, setup: BulkTargets
) -> None:
    """A `submit_ambiguous` submission released, then one of its targets purged (its item recorded, its entry
    `ambiguous` since the release): the provider's ids equal the original set, so the adoption confirms the
    batch (rule 1 `equal`) and reconciles it with `released_at` kept, the other item `ignored_fenced`."""
    batch = await sent(
        engine,
        fake_llm,
        await prepared(engine, gateway, setup),
        BatchScript(create="ambiguous_created", polls_until_ended=5),
    )
    await release(engine, batch.submission_id, now=utcnow())
    gone, _ = setup.targets
    assert await purge(engine, [gone]) == purged_targets(1)
    fake_llm.batches(BatchScript(polls_until_ended=0))
    report = await reconcile_ambiguous_once(engine, fake_llm)
    assert (report.adopted, report.anomalies, report.reconciled) == (1, 0, 1)
    (row,) = await submission_rows(engine)
    assert (row.state, row.provider_batch_id) == ("reconciled", batch.provider_id)
    assert row.released_at is not None
    assert await decisions(engine) == ["ignored_fenced"]
    assert await open_alert_codes(engine) == []


async def test_a_target_cancelled_out_of_a_running_batch_leaves_evidence_when_purged(
    engine: AsyncEngine, gateway: LLMGateway, fake_llm: FakeLLM, setup: BulkTargets
) -> None:
    """§10.10: a revision supersession cancelled a target individually, its submission still `submitted`. Its
    purge requests no cancel (no `in_batch` target of the set), settles its item's entry `ambiguous` before
    the call goes (no entry is stranded), and records its item: its late line counts `purged`."""
    batch = await sent(
        engine, fake_llm, await prepared(engine, gateway, setup), BatchScript(polls_until_ended=0)
    )
    gone, kept = setup.targets
    await supersede(engine, gone)
    ledger_id = (await entry_ids(engine, batch.submission_id))[batch.custom_id(gone)]
    assert await purge(engine, [gone]) == purged_targets(1, entries_ambiguous=1)
    (row,) = await submission_rows(engine)
    assert (row.state, row.cancel_requested_at) == ("submitted", None)
    assert [(s, c) for s, c, _ in await purged_items(engine)] == [
        (batch.submission_id, batch.custom_id(gone))
    ]
    entry = await ledger(engine, ledger_id)
    assert (entry.state, entry.has_call) == ("ambiguous", False)
    report = await poll_once(engine, fake_llm)
    assert (report.purged, report.reconciled) == (1, 1)
    assert await open_alert_codes(engine) == []
    assert await target_now(engine, kept) == ("published_accepted", None, "published_accepted")


# ---- P2: a fenced item stores no output --------------------------------------------------------------------


@pytest.mark.parametrize("when", ["before_its_line", "during_its_settlement"])
async def test_a_fenced_item_stores_no_output_json(
    engine: AsyncEngine,
    gateway: LLMGateway,
    fake_llm: FakeLLM,
    setup: BulkTargets,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    """P2: an item whose target is no longer `in_batch` under its pin settles its spend from usage (`billed`,
    its call `completed`) and stores no `output_json`; the other item publishes with its output. The fence is
    decided before the settlement stores anything; a supersession that commits between that decision and the
    item's own target lock (node 9 holds the job `FOR SHARE` too) leaves no output either."""
    batch = await sent(
        engine, fake_llm, await prepared(engine, gateway, setup), BatchScript(polls_until_ended=0)
    )
    cancelled, kept = setup.targets
    if when == "before_its_line":
        await supersede(engine, cancelled)
    else:
        ledger_id = (await entry_ids(engine, batch.submission_id))[batch.custom_id(cancelled)]
        real = spend_ledger.settle

        async def superseded_first(conn: AsyncConnection, res: Reservation, **kw: Any) -> Any:
            if res.ledger_id == ledger_id:
                await supersede(engine, cancelled)  # commits while the item's transaction is open
            return await real(conn, res, **kw)

        monkeypatch.setattr(spend_ledger, "settle", superseded_first)
    await poll_once(engine, fake_llm)
    entries = await entries_by_custom_id(engine)
    fenced, published = entries[batch.custom_id(cancelled)], entries[batch.custom_id(kept)]
    assert (fenced.state, fenced.status, fenced.has_output) == ("billed", "completed", False)
    assert fenced.settled_usd > 0
    assert (published.state, published.has_output) == ("billed", True)
    items = await item_rows(engine)
    assert items[batch.custom_id(cancelled)].decision == "ignored_fenced"


# ---- the online entries (G04) ------------------------------------------------------------------------------


async def online_owner(engine: AsyncEngine, target: PendingTarget) -> TargetAttemptOwner:
    """The target claimed (`in_progress`) with its running online attempt, as its spend owner."""
    claimed = await claim_again(engine, target.target_id)
    return TargetAttemptOwner(
        target.target_id, UUID(claimed.claim_id), claimed.attempt_no, claimed.attempt_id
    )


async def online_try(
    engine: AsyncEngine,
    owner: TargetAttemptOwner,
    *,
    now: datetime,
    bulk_operation_id: int | None = None,
    dispatch: bool = False,
) -> Reservation:
    """One reserved generator try of the owner (funded by `bulk_operation_id` when given), dispatched with
    `dispatch`."""
    kw = scoring_kw(owner, now=now, bound=4000, max_tokens=1000, bulk_operation_id=bulk_operation_id)
    async with engine.begin() as conn:
        res = await spend_ledger.reserve(conn, **kw)
    if dispatch:
        async with engine.begin() as conn:
            await spend_ledger.mark_dispatched(conn, res, owner, now=now, first_try_of_logical_call=True)
    return res


async def test_an_in_progress_targets_reserved_entry_is_released_before_its_call_goes(
    engine: AsyncEngine, gateway: LLMGateway, setup: BulkTargets, infos: pytest.LogCaptureFixture
) -> None:
    """G04: the purged target's coroutine holds a `reserved` funded try. The purge releases it before its call
    is deleted: `not_billed('never_dispatched')`, the month's counters `-r`, the allocation credited `r`. The
    coroutine's dispatch marker is then fenced, and its `release_never_dispatched` finds the ledger row alone,
    already released: a no-op logged `llm_settle_call_purged`, never a `LookupError`."""
    t = utcnow()
    target = setup.targets[0]
    owner = await online_owner(engine, target)
    res = await online_try(engine, owner, now=t, bulk_operation_id=setup.bulk_operation_id)
    r = res.reserved_usd
    (during,) = await budget_rows(engine, res.month)
    assert (await bulk_accounts(engine, setup)).spent_usd == r  # funded
    assert await purge(engine, [target], now=t) == purged_targets(1, online_released=1)
    entry = await ledger(engine, res.ledger_id)
    assert (entry.state, entry.evidence, entry.settled_usd, entry.has_call) == (
        "not_billed",
        "never_dispatched",
        0,
        False,
    )
    (after,) = await budget_rows(engine, res.month)
    assert (after.counted_usd, after.scoring_counted_usd) == (
        during.counted_usd - r,
        during.scoring_counted_usd - r,
    )
    assert (await bulk_accounts(engine, setup)).spent_usd == 0
    with pytest.raises(FencedOut):  # the coroutine's marker: its owner is gone
        async with engine.begin() as conn:
            await spend_ledger.mark_dispatched(conn, res, owner, now=t, first_try_of_logical_call=True)
    async with engine.begin() as conn:  # the gateway's release of a fenced try
        await spend_ledger.release_never_dispatched(conn, res, now=t)
    assert [e["ledger_id"] for e in logged(infos, "llm_settle_call_purged")] == [res.ledger_id]
    (final,) = await budget_rows(engine, res.month)
    assert final.counted_usd == after.counted_usd
    assert await no_drift(engine, res)


async def test_a_dispatched_online_entry_ends_ambiguous_and_its_late_response_is_a_no_op(
    engine: AsyncEngine, gateway: LLMGateway, setup: BulkTargets, infos: pytest.LogCaptureFixture
) -> None:
    """G04, §20.4: the purged target's coroutine has dispatched its try. The purge makes the entry `ambiguous`
    before its call goes (counted at its reservation, the counters unchanged); the coroutine's late `settle`
    of its response finds the ledger row alone: a no-op logged `llm_settle_call_purged`, nothing raised, the
    entry still `ambiguous`."""
    t = utcnow()
    target = setup.targets[0]
    owner = await online_owner(engine, target)
    res = await online_try(engine, owner, now=t, dispatch=True)
    (during,) = await budget_rows(engine, res.month)
    assert await purge(engine, [target], now=t) == purged_targets(1, online_ambiguous=1)
    entry = await ledger(engine, res.ledger_id)
    assert (entry.state, entry.has_call) == ("ambiguous", False)
    (after,) = await budget_rows(engine, res.month)
    assert after.counted_usd == during.counted_usd
    async with engine.begin() as conn:
        await spend_ledger.settle(
            conn, res, outcome="billed", evidence=None, usage=USAGE, call=completed_call(), now=t
        )
    assert [e["ledger_id"] for e in logged(infos, "llm_settle_call_purged")] == [res.ledger_id]
    assert (await ledger(engine, res.ledger_id)).state == "ambiguous"
    (final,) = await budget_rows(engine, res.month)
    assert final.counted_usd == during.counted_usd
    assert await no_drift(engine, res)


async def test_a_try_reserved_after_the_first_read_is_settled_by_the_hook(
    engine: AsyncEngine, gateway: LLMGateway, setup: BulkTargets, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G04 (fix round 1, M2): the purged target's coroutine reserves a try after the chunk's first read of the
    online entries, in its own transaction, which commits before the chunk takes the target (here while the
    chunk records its cancels; the target has no other entry, so the chunk holds no month row yet). The
    hook's read after the cancel finds it and releases it before its call goes: never stranded."""
    t = utcnow()
    target = setup.targets[0]
    owner = await online_owner(engine, target)
    late: list[Reservation] = []
    real = batches.record_cancel

    async def reserving_first(conn: AsyncConnection, **kw: Any) -> list[int]:
        late.append(await online_try(engine, owner, now=t))  # another connection: it commits
        return await real(conn, **kw)

    monkeypatch.setattr(batches, "record_cancel", reserving_first)
    purged = await purge(engine, [target], now=t)
    (res,) = late
    assert purged == purged_targets(1, online_released=1)
    assert await stranded(engine) == 0
    entry = await ledger(engine, res.ledger_id)
    assert (entry.state, entry.evidence, entry.has_call) == ("not_billed", "never_dispatched", False)
    assert await no_drift(engine, res)


async def test_a_settlement_finding_its_entry_stranded_logs_an_error(
    engine: AsyncEngine, gateway: LLMGateway, setup: BulkTargets, infos: pytest.LogCaptureFixture
) -> None:
    """G04 (fix round 1, M3): a ledger row still `dispatched` whose call another deleter removed unsettled
    (here M3's `targets.delete_target_set` alone, the path the purge replaces) is a breach no reader finds
    again. The coroutine's late `settle` and `release_never_dispatched` still write and raise nothing (a late
    response never crashes its caller), but each logs the error `llm_entry_stranded`, never the info
    `llm_settle_call_purged` of an entry a purge settled."""
    t = utcnow()
    target = setup.targets[0]
    res = await online_try(engine, await online_owner(engine, target), now=t, dispatch=True)
    await m3_delete(engine, [target])
    assert await stranded(engine) == 1
    async with engine.begin() as conn:
        await spend_ledger.settle(
            conn, res, outcome="billed", evidence=None, usage=USAGE, call=completed_call(), now=t
        )
    async with engine.begin() as conn:
        await spend_ledger.release_never_dispatched(conn, res, now=t)
    errors = [(e["ledger_id"], e["state"], e["level"]) for e in logged(infos, "llm_entry_stranded")]
    assert errors == [(res.ledger_id, "dispatched", "error")] * 2
    assert logged(infos, "llm_settle_call_purged") == []
    assert (await ledger(engine, res.ledger_id)).state == "dispatched"


async def test_ledger_unsettled_finds_nothing_stranded_after_a_purge(
    engine: AsyncEngine,
    gateway: LLMGateway,
    fake_llm: FakeLLM,
    current_epoch: int,
    queue_app: procrastinate.App,
) -> None:
    """G04: a purge of a batched target, a target with a `reserved` online try and one with a `dispatched`
    one leaves no `reserved` or `dispatched` entry without its call (the batch entry and the dispatched try
    `ambiguous`, the reserved try released), and `llm_ledger_unsettled` has nothing to recover."""
    made = await answered(engine, fake_llm, 4)
    batched, other, reserving, dispatching = made.targets
    t = utcnow()
    owners = [await online_owner(engine, target) for target in (reserving, dispatching)]
    batch = await sent(
        engine, fake_llm, await prepared(engine, gateway, made), BatchScript(polls_until_ended=5)
    )
    assert {target for target, _ in await items_of(engine, batch.submission_id)} == {
        batched.target_id,
        other.target_id,
    }
    reserved = await online_try(engine, owners[0], now=t)
    dispatched = await online_try(engine, owners[1], now=t, dispatch=True)
    batch_entry = (await entry_ids(engine, batch.submission_id))[batch.custom_id(batched)]
    purged = await purge(engine, [batched, reserving, dispatching], now=t)
    assert purged == purged_targets(
        3,
        cancel_requested=(batch.submission_id,),
        entries_ambiguous=1,
        online_released=1,
        online_ambiguous=1,
    )
    assert await stranded(engine) == 0
    ids = (batch_entry, reserved.ledger_id, dispatched.ledger_id)
    assert [(await ledger(engine, i)).state for i in ids] == ["ambiguous", "not_billed", "ambiguous"]
    later = ctx(engine, queue_app, t + timedelta(hours=1), current_epoch)
    assert await spend_ledger.reconcile_llm_ledger_unsettled(later) == 0
    assert await no_drift(engine, reserved)


# ---- the foreign line and a purge meeting the poll ---------------------------------------------------------


async def test_a_foreign_line_still_raises_the_anomaly(
    engine: AsyncEngine,
    gateway: LLMGateway,
    fake_llm: FakeLLM,
    current_epoch: int,
    queue_app: procrastinate.App,
) -> None:
    """Rule 4: the purge evidence names its own line only. With one target purged, a line `t999999_a9` and
    an unparsable one, neither named by a `purged_batch_items` row, are foreign: `batch.custom_id_anomaly`
    with two foreign lines, the submission left `ended` with nothing completed, while the purged line counts
    `purged`."""
    made = await answered(engine, fake_llm, 3)
    batch = await sent(
        engine, fake_llm, await prepared(engine, gateway, made), BatchScript(polls_until_ended=0)
    )
    assert batch.provider_id is not None
    gone, renamed, unparsable = made.targets
    await purge(engine, [gone])
    stored = fake_llm.state.batches[batch.provider_id]  # ended at its create: its lines are fixed
    for target, custom_id in ((renamed, "t999999_a9"), (unparsable, "unparsable-line")):
        key = batch.custom_id(target)
        stored.lines[key] = {**stored.lines[key], "custom_id": custom_id}
    report = await poll_once(engine, fake_llm)
    assert (report.purged, report.reconciled) == (1, 0)
    (row,) = await submission_rows(engine)
    assert (row.state, row.results_complete_at) == ("ended", None)
    assert await anomalies(engine) == [{"submission_id": batch.submission_id, "foreign_lines": 2}]
    assert await count_rows(engine, "batch_items", result_type="missing") == 0


async def test_a_purge_meeting_the_poll_mid_stream_counts_its_line_purged(
    engine: AsyncEngine,
    gateway: LLMGateway,
    fake_llm: FakeLLM,
    setup: BulkTargets,
    migrated: PgServer,
    infos: pytest.LogCaptureFixture,
) -> None:
    """A purge that commits while the poll reads the batch's results: the stream read the purged item with
    the submission's items, and the item's processing waits for the purge's job lock (step 4). Once the purge
    has committed the item is gone and its settlement finds the entry alone (a no-op), so the line counts
    `purged`, never a failed item, and the same pass reconciles the submission."""
    batch = await sent(
        engine,
        fake_llm,
        await prepared(engine, gateway, setup),
        BatchScript(polls_until_ended=0, stream_cut_after=0),
    )
    assert batch.provider_id is not None
    await poll_once(engine, fake_llm)  # `ended`, its stream cut before any line
    (row,) = await submission_rows(engine)
    assert (row.state, row.results_complete_at) == ("ended", None)
    fake_llm.batches(BatchScript(polls_until_ended=0))
    async with fake_llm.client() as http:  # the first line of the stream: its target is purged
        stream = await batch_client(fake_llm, http).results(batch.provider_id)
        lines = [line.custom_id async for line in stream]
    (gone,) = [t for t in setup.targets if batch.custom_id(t) == lines[0]]
    manifest = await fenced_manifest(engine, [gone])
    async with engine.connect() as conn:
        await conn.begin()
        await chunk_locks(conn, [gone])
        await purge_target_set(conn, [gone.target_id], manifest_id=manifest, now=utcnow())
        polling = asyncio.create_task(poll_once(engine, fake_llm))

        async def waiting() -> bool:
            return bool(lock_wait_details(migrated)) or polling.done()

        await await_until(waiting, 30, 0.05)
        assert not polling.done()
        (wait,) = lock_wait_details(migrated)
        assert (wait.relation, wait.key) == ("jobs", str(gone.job_id)), wait
        await conn.commit()
    report = await asyncio.wait_for(polling, 30)
    assert (report.decided, report.purged, report.reconciled) == (1, 1, 1)
    assert logged(infos, "batch_item_failed") == []
    assert len(logged(infos, "llm_settle_call_purged")) == 1
    (row,) = await submission_rows(engine)
    assert row.state == "reconciled" and "failed_items" not in (row.reconcile_report or {})


async def test_an_unreadable_purge_evidence_leaves_the_item_failed_not_the_pass(
    engine: AsyncEngine,
    gateway: LLMGateway,
    fake_llm: FakeLLM,
    setup: BulkTargets,
    monkeypatch: pytest.MonkeyPatch,
    infos: pytest.LogCaptureFixture,
) -> None:
    """Ruling T17-a (fix round 1, M5): an item whose processing raises is checked against the purge evidence
    on a connection of its own; when that read raises too, the item is a failed one of the pass (logged with
    its processing error's class, counted in `failed_items`) and the pass goes on with the next item: the
    error never ends it."""
    batch = await sent(
        engine, fake_llm, await prepared(engine, gateway, setup), BatchScript(polls_until_ended=0)
    )
    failing, _ = setup.targets
    failing_item = (await item_ids(engine))[batch.custom_id(failing)]
    real = batch_items.process_item

    async def raising(conn: AsyncConnection, **kw: Any) -> batch_items.Decision:
        if kw["item"].id == failing_item:
            raise RuntimeError("synthetic processing failure")
        return await real(conn, **kw)

    async def unreadable(*args: object) -> bool:
        raise ConnectionError("synthetic: the evidence cannot be read")

    monkeypatch.setattr(batch_items, "process_item", raising)
    monkeypatch.setattr(batch_results, "_purged_line", unreadable)
    report = await poll_once(engine, fake_llm)
    assert (report.decided, report.purged, report.reconciled) == (1, 0, 0)
    (row,) = await submission_rows(engine)
    assert row.state == "ended" and row.reconcile_report["failed_items"] == {str(failing_item): 1}
    assert [e["error_class"] for e in logged(infos, "batch_item_failed")] == ["RuntimeError"]
    assert [e["error_class"] for e in logged(infos, "batch_purge_evidence_unread")] == ["ConnectionError"]
