"""The release of a lost batch (plan M4 Task 16, U3; spec §10.10 L3958, L3991-3999, L5045, §7.16.6).

A `submitted` submission whose retrieval keeps failing can be released by the user once the critical
`batch.retrieve_failing` of it is open **and** `now > expires_at + 1 h` (`batches.RELEASABLE_SQL`, the one
predicate of the route's lock and of Needs attention's `releasable`): its targets are re-scored, its spend
stays counted as `ambiguous`, and the poll keeps looking for its batch for 29 days (`released → ended →
reconciled`, every item `ignored_fenced`, the entries settled from the provider's evidence) without raising
anything while the retrieval still fails. Any other submission is 409 `conflict_state` with `detail =
"not_releasable"`.

The batch is lost on the fake Batches API (`lost`): the provider ended it within its window, then the fake
forgot it, so every retrieval of its provider id is a 404 until `learn` gives it back. The poll's clock and
the routes' (`jobhunter.api.routes.targets.utcnow`, `...system.utcnow`) are injected; the fake's own clock
stays at the real time (the batch was ended before its expiry). The race between the poll's move to `ended`
and a release (both lock the submission row; the second to commit sees the other's state) is run both ways.
The targets are 250 synthetic ads of the score tuple in one bulk operation; every datum is invented.
"""

import asyncio
import logging
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import httpx
import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.llm.batches import BatchesClient, BatchStatus
from jobhunter.llm.fake import BatchScript, ItemOutcome, StoredBatch
from jobhunter.observability.alerts import resolve_alert
from jobhunter.scoring import batch_results
from jobhunter.scoring import batches as batches_mod
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.batches import (
    SubmittedBatch,
    batch_client,
    bulk_row,
    decisions,
    entries_by_custom_id,
    item_rows,
    open_alert_codes,
    poll_once,
    reconcile_ambiguous_once,
    submission_rows,
    submit_batch,
    targets_by_custom_id,
)
from tests.support.fixtures import count_rows
from tests.support.llm import FakeLLM
from tests.support.pg import PgServer, lock_wait_details
from tests.support.procs import await_until
from tests.support.profile import OpRunner, idem
from tests.support.scoring import attempt_outcomes, operations_of, targets_by_state
from tests.support.settings import fresh_key

pytestmark = pytest.mark.integration
N: Final = 250
CONFIRM: Final = {"confirm": True}
EMPTY: Final = {"count": 0, "items": []}
TARGETS_UTCNOW: Final = "jobhunter.api.routes.targets.utcnow"
SYSTEM_UTCNOW: Final = "jobhunter.api.routes.system.utcnow"
NOT_RELEASABLE: Final = "not_releasable"
RETRIEVE_FAILING: Final = batch_results.RETRIEVE_FAILING_ALERT
# The poll's `resolve_alert`, which it calls inside its move to `ended`.
POLL_RESOLVE_ALERT: Final = "jobhunter.scoring.batch_results.resolve_alert"
_GENERATION: Final = sa.text("SELECT generation FROM ranking_state WHERE id = 1")
# The money (M3): the month of the batch entries, its row's counters, and the entries of the month and of
# the bulk operation, whose counted amounts the test sums by its own rule (`counts_at`).
_BATCH_MONTHS: Final = sa.text(
    "SELECT DISTINCT l.budget_month FROM llm_spend_ledger l JOIN llm_calls c ON c.spend_ledger_id = l.id "
    "WHERE c.transport = 'batch'"
)
_MONTH_COUNTERS: Final = sa.text("SELECT counted_usd, scoring_counted_usd FROM llm_budget WHERE month = :m")
_MONTH_ENTRIES: Final = sa.text(
    "SELECT state, reserved_usd, settled_usd, purpose_class FROM llm_spend_ledger WHERE budget_month = :m"
)
_BULK_ENTRIES: Final = sa.text(
    "SELECT state, reserved_usd, settled_usd FROM llm_spend_ledger WHERE bulk_operation_id = :b"
)


@dataclass(frozen=True)
class Lost:
    """A submitted batch the provider ended within its window and the fake then forgot (`stored`, which
    `learn` gives back): its submission, and its `expires_at`."""

    batch: SubmittedBatch
    stored: StoredBatch
    expires_at: datetime

    @property
    def submission_id(self) -> int:
        return self.batch.submission_id

    @property
    def hour(self) -> datetime:
        """`expires_at + RETRIEVE_FAILING_AFTER`: releasable only after it."""
        return self.expires_at + batch_results.RETRIEVE_FAILING_AFTER

    @property
    def after(self) -> datetime:
        """A minute past the hour."""
        return self.hour + timedelta(minutes=1)


async def lose(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, outcomes: Mapping[int, ItemOutcome]
) -> Lost:
    """`N` targets submitted on the fake, the batch ended by the provider (each item `succeeded`, but the
    target indexes of `outcomes`, which take theirs), then forgotten by the fake."""
    submitted = await submit_batch(engine, op_runner, fake_llm, N)
    items = {submitted.custom_ids[i]: outcome for i, outcome in outcomes.items()}
    fake_llm.batches(BatchScript(items=items, polls_until_ended=0))
    async with fake_llm.client() as http:  # the provider ends it ...
        status = await batch_client(fake_llm, http).retrieve(submitted.provider_id)
    assert status.processing_status == "ended"
    stored = fake_llm.state.batches.pop(submitted.provider_id)  # ... and the fake forgets it
    (row,) = await submission_rows(engine)
    assert row.state == "submitted"
    return Lost(submitted, stored, row.expires_at)


@pytest.fixture
async def lost(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, queue_app: procrastinate.App
) -> Lost:
    """A lost batch whose every item succeeded."""
    return await lose(engine, op_runner, fake_llm, {})


def learn(fake_llm: FakeLLM, lost: Lost) -> None:
    """The fake knows the lost batch again: its retrievals answer it (ended)."""
    fake_llm.state.batches[lost.batch.provider_id] = lost.stored


async def release_at(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, submission_id: int, at: datetime
) -> httpx.Response:
    """`POST /api/batches/{id}/release {"confirm": true}` with a fresh key, the route's clock at `at`."""
    monkeypatch.setattr(TARGETS_UTCNOW, lambda: at)
    return await client.post(
        f"/api/batches/{submission_id}/release", json=CONFIRM, headers=idem(fresh_key("release"))
    )


def assert_not_releasable(r: httpx.Response) -> None:
    body = r.json()
    assert (r.status_code, body["code"], body.get("detail")) == (409, "conflict_state", NOT_RELEASABLE), (
        r.text
    )


async def retrieve_failing_at(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, at: datetime
) -> dict[str, Any]:
    """Needs attention's `retrieve_failing` section, the route's clock at `at`."""
    monkeypatch.setattr(SYSTEM_UTCNOW, lambda: at)
    r = await client.get("/api/system/needs-attention")
    assert r.status_code == 200, r.text
    section: dict[str, Any] = r.json()["retrieve_failing"]
    return section


async def generation(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(_GENERATION)).scalar_one())


async def lost_and_alerted(engine: AsyncEngine, fake_llm: FakeLLM, lost: Lost) -> None:
    """A poll past the hour: the retrieval fails, so `batch.retrieve_failing` is open."""
    report = await poll_once(engine, fake_llm, now=lost.after)
    assert (report.polled, report.ended) == (1, 0)
    assert await open_alert_codes(engine) == [RETRIEVE_FAILING]


def follow_ons_of(ops: list[Any], submission_id: int) -> list[Any]:
    return [op for op in ops if op.payload.get("after_submission_id") == submission_id]


def counts_at(entry: Any) -> Decimal:
    """What a ledger entry counts (§7.11, the test's own rule): billed its cost, not billed nothing, reserved,
    dispatched and ambiguous their reservation."""
    if entry.state == "billed":
        return Decimal(entry.settled_usd)
    return Decimal(0) if entry.state == "not_billed" else Decimal(entry.reserved_usd)


@dataclass(frozen=True)
class Money:
    """The month row's counters and the bulk row's amounts, each with what the ledger entries behind it count
    at (`counts_at`)."""

    month_counted: Decimal
    month_scoring: Decimal
    month_ledger: Decimal
    month_scoring_ledger: Decimal
    bulk_spent: Decimal
    bulk_held: Decimal
    bulk_ledger: Decimal

    def consistent(self) -> bool:
        """The month's counters are its entries' (§7.11) and the bulk's `spent_usd` its funded entries' (§7.17
        #7)."""
        return (self.month_counted, self.month_scoring, self.bulk_spent) == (
            self.month_ledger,
            self.month_scoring_ledger,
            self.bulk_ledger,
        )


async def money(engine: AsyncEngine, bulk_operation_id: int) -> Money:
    async with engine.connect() as conn:
        (month,) = (await conn.execute(_BATCH_MONTHS)).scalars().all()  # one month holds the batch entries
        counters = (await conn.execute(_MONTH_COUNTERS, {"m": month})).one()
        in_month = (await conn.execute(_MONTH_ENTRIES, {"m": month})).all()
        in_bulk = (await conn.execute(_BULK_ENTRIES, {"b": bulk_operation_id})).all()
    account = await bulk_row(engine, bulk_operation_id)
    return Money(
        month_counted=Decimal(counters.counted_usd),
        month_scoring=Decimal(counters.scoring_counted_usd),
        month_ledger=sum((counts_at(e) for e in in_month), Decimal(0)),
        month_scoring_ledger=sum(
            (counts_at(e) for e in in_month if e.purpose_class == "scoring"), Decimal(0)
        ),
        bulk_spent=Decimal(account.spent_usd),
        bulk_held=Decimal(account.followup_held_usd),
        bulk_ledger=sum((counts_at(e) for e in in_bulk), Decimal(0)),
    )


def retrieve_failures(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    """The `batch_retrieve_failed` lines logged: (level, the submission's state)."""
    return [
        (r.levelname, r.msg["state"])
        for r in caplog.records
        if isinstance(r.msg, dict) and r.msg.get("event") == "batch_retrieve_failed"
    ]


async def test_a_submitted_batch_is_not_releasable_before_the_alert_and_the_hour(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    fake_llm: FakeLLM,
    lost: Lost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the hour without the alert (never polled): 409 `not_releasable`, and no Needs attention item.
    With the alert open: 409 at `expires_at + 59 min` and at exactly `expires_at + 1 h` (the bound is
    strict), the item `releasable: false` at both; a minute past the hour the item is `releasable: true` and
    the release is accepted: the button and the route never disagree. Nothing moves before that. The alert
    acknowledged (`POST /api/alerts/{id}/ack`) is still open, so the batch stays releasable: acknowledging
    moves nothing (the predicate reads only `resolved_at`)."""
    s = lost.submission_id
    assert_not_releasable(await release_at(logged_in_client, monkeypatch, s, lost.after))
    assert await retrieve_failing_at(logged_in_client, monkeypatch, lost.after) == EMPTY
    await lost_and_alerted(engine, fake_llm, lost)
    for moment in (lost.hour - timedelta(minutes=1), lost.hour):
        assert_not_releasable(await release_at(logged_in_client, monkeypatch, s, moment))
        section = await retrieve_failing_at(logged_in_client, monkeypatch, moment)
        assert [(i["submission_id"], i["releasable"]) for i in section["items"]] == [(s, False)]
    (row,) = await submission_rows(engine)
    assert (row.state, row.released_at) == ("submitted", None)
    assert await targets_by_state(engine, lost.batch.setup.llm_tuple_id) == {"in_batch": N}
    assert {e.state for e in (await entries_by_custom_id(engine)).values()} == {"dispatched"}
    assert await open_alert_codes(engine) == [RETRIEVE_FAILING]

    (item,) = (await retrieve_failing_at(logged_in_client, monkeypatch, lost.after))["items"]
    ack = await logged_in_client.post(f"/api/alerts/{item['alert_id']}/ack")
    assert ack.status_code == 200, ack.text
    assert await open_alert_codes(engine) == [RETRIEVE_FAILING]  # acknowledged, still open
    section = await retrieve_failing_at(logged_in_client, monkeypatch, lost.after)
    assert [(i["submission_id"], i["releasable"]) for i in section["items"]] == [(s, True)]
    r = await release_at(logged_in_client, monkeypatch, s, lost.after)
    assert (r.status_code, r.json()["state"]) == (202, "released"), r.text
    assert await open_alert_codes(engine) == []  # the acknowledged alert resolved by the release


async def test_a_lost_batch_release_frees_targets_and_keeps_the_spend_ambiguous(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    op_runner: OpRunner,
    fake_llm: FakeLLM,
    lost: Lost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The release of a lost batch is the quarantined release's (L5045): `submitted → released` with its
    provider id kept, every entry `dispatched → ambiguous` (the call `batch_released`) at the amount Needs
    attention showed and the 202 states, the holds released, the targets `pending`, the attempts `released`,
    `batch.retrieve_failing` resolved, the follow-on requested once, the ranking bumped; the singleton is
    free: the follow-on submits a new batch of the same targets. The money: the month row's counters and the
    bulk operation's `spent_usd` are what the entries count at before and after, the release changes neither
    (the reservation stays counted), and the bulk's follow-up hold drops from the submission's to 0."""
    s = lost.submission_id
    setup = lost.batch.setup
    await lost_and_alerted(engine, fake_llm, lost)
    (item,) = (await retrieve_failing_at(logged_in_client, monkeypatch, lost.after))["items"]
    assert (item["submission_id"], item["releasable"]) == (s, True)
    before = await generation(engine)
    (submitted,) = await submission_rows(engine)
    held = await money(engine, setup.bulk_operation_id)
    assert held.consistent(), held
    assert held.bulk_spent == held.month_counted == submitted.reserved_usd > 0  # the reservation, counted
    assert held.bulk_held == submitted.followup_held_usd > 0

    r = await release_at(logged_in_client, monkeypatch, s, lost.after)
    assert r.status_code == 202, r.text
    assert r.json() == {"submission_id": s, "state": "released", "reserved_usd": item["reserved_usd"]}
    entries = await entries_by_custom_id(engine)
    assert {(e.state, e.error_code) for e in entries.values()} == {("ambiguous", batches_mod.RELEASED_CODE)}
    assert sum(e.reserved_usd for e in entries.values()) == Decimal(item["reserved_usd"]) > 0
    (row,) = await submission_rows(engine)
    assert (row.state, row.provider_batch_id) == ("released", lost.batch.provider_id)
    assert row.released_at is not None and row.follow_on_at is not None
    released = await money(engine, setup.bulk_operation_id)
    assert released.consistent(), released
    assert (released.month_counted, released.month_scoring, released.bulk_spent) == (
        held.month_counted,
        held.month_scoring,
        held.bulk_spent,
    )  # still counted, at the reservation
    assert released.bulk_held == 0
    assert await targets_by_state(engine, setup.llm_tuple_id) == {"pending": N}
    assert await attempt_outcomes(engine, setup.llm_tuple_id) == {"released": N}
    assert all(t.batch_submission_id is None for t in (await targets_by_custom_id(engine)).values())
    assert all(i.hold_released_at is not None for i in (await item_rows(engine)).values())
    assert await open_alert_codes(engine) == []  # the alert resolved
    assert await retrieve_failing_at(logged_in_client, monkeypatch, lost.after) == EMPTY
    assert await generation(engine) > before  # the ranking bumped
    (follow_on,) = follow_ons_of(await operations_of(engine, "batch_submit"), s)

    fake_llm.batches(BatchScript(create="ok", polls_until_ended=5))
    await op_runner.run_one(follow_on.id)  # the singleton is free: a new batch of the released targets
    rows = {r.id: r for r in await submission_rows(engine)}
    (new,) = [r for r in rows.values() if r.id != s]
    assert (new.state, rows[s].state) == ("submitted", "released")
    assert await targets_by_state(engine, setup.llm_tuple_id) == {"in_batch": N}
    retried = [t for c, t in (await targets_by_custom_id(engine)).items() if c.endswith("_a2")]
    assert len(retried) == N and {t.batch_submission_id for t in retried} == {new.id}


async def test_a_released_batch_found_later_ends_and_settles_without_publishing(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    op_runner: OpRunner,
    fake_llm: FakeLLM,
    queue_app: procrastinate.App,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The released lifecycle through the poll (§10.10 L3999), the provider having ended the batch with one
    item errored and one expired: while its retrieval still fails it raises nothing, moves nothing and logs
    each failure at debug (a `submitted` batch's at warning), and the ambiguous reconciliation does not list
    it (it has a provider id); once the fake knows the batch again, the next poll moves it `released → ended
    → reconciled` with `released_at` kept, every item `ignored_fenced`, each entry settled from the provider's
    evidence (`ambiguous → billed` from its usage, `ambiguous → not_billed(batch_item_errored |
    batch_item_expired)`) and nothing published, while the follow-on submission stays `submitted` with the
    re-claimed targets (outside the singleton), and no second follow-on. The money: the month row's counters
    and the bulk's `spent_usd` drop by exactly what the settlements free (each the ledger's count), and the
    bulk's follow-up hold stays the new submission's (the released one's holds were released once)."""
    caplog.set_level(logging.DEBUG, logger="jobhunter.scoring.batch_results")
    lost = await lose(engine, op_runner, fake_llm, {0: "errored:other", 1: "expired"})
    errored, expired = lost.batch.custom_ids[0], lost.batch.custom_ids[1]
    s = lost.submission_id
    setup = lost.batch.setup
    await lost_and_alerted(engine, fake_llm, lost)
    assert (await release_at(logged_in_client, monkeypatch, s, lost.after)).status_code == 202
    (follow_on,) = follow_ons_of(await operations_of(engine, "batch_submit"), s)
    fake_llm.batches(BatchScript(create="ok", polls_until_ended=5))
    await op_runner.run_one(follow_on.id)
    (b,) = [r for r in await submission_rows(engine) if r.id != s]
    assert b.state == "submitted"

    for minutes in (5, 10):  # still lost: nothing raised, nothing moved
        report = await poll_once(engine, fake_llm, now=lost.after + timedelta(minutes=minutes))
        assert (report.polled, report.ended, report.decided) == (2, 0, 0)
    assert await open_alert_codes(engine) == []
    assert retrieve_failures(caplog) == [
        ("WARNING", "submitted"),
        ("DEBUG", "released"),
        ("DEBUG", "released"),
    ]
    adoption = await reconcile_ambiguous_once(engine, fake_llm, now=lost.after + timedelta(minutes=10))
    assert adoption.submissions == 0  # the poll's to find, not the ambiguous reconciliation's
    assert {r.id: r.state for r in await submission_rows(engine)} == {s: "released", b.id: "submitted"}
    pending = await money(engine, setup.bulk_operation_id)
    assert pending.consistent(), pending
    assert pending.bulk_held == b.followup_held_usd > 0  # the new submission's hold only

    learn(fake_llm, lost)
    report = await poll_once(engine, fake_llm, now=lost.after + timedelta(minutes=15))
    assert (report.polled, report.ended, report.decided, report.reconciled) == (2, 1, N, 1)
    rows = {r.id: r for r in await submission_rows(engine)}
    a = rows[s]
    assert (a.state, a.provider_batch_id, rows[b.id].state) == (
        "reconciled",
        lost.batch.provider_id,
        "submitted",
    )
    assert a.released_at is not None and a.ended_at is not None and a.results_complete_at is not None
    a_ids = set(lost.batch.custom_ids)
    items = {c: i for c, i in (await item_rows(engine)).items() if c in a_ids}
    assert Counter((i.result_type, i.decision) for i in items.values()) == {
        ("succeeded", "ignored_fenced"): N - 2,
        ("errored", "ignored_fenced"): 1,
        ("expired", "ignored_fenced"): 1,
    }
    assert await decisions(engine) == ["ignored_fenced"] * N  # the new batch's items wait for it
    entries = {c: e for c, e in (await entries_by_custom_id(engine)).items() if c in a_ids}
    assert (entries[errored].state, entries[errored].evidence) == ("not_billed", "batch_item_errored")
    assert (entries[expired].state, entries[expired].evidence) == ("not_billed", "batch_item_expired")
    assert all(entries[c].settled_usd == 0 == entries[c].attempt_cost for c in (errored, expired))
    billed = [e for c, e in entries.items() if c not in (errored, expired)]
    assert {e.state for e in billed} == {"billed"}
    assert all(e.settled_usd > 0 and e.attempt_cost == e.settled_usd for e in billed)
    assert await count_rows(engine, "llm_results") == 0  # nothing published
    targets = await targets_by_custom_id(engine)
    assert {(targets[c].state, targets[c].batch_submission_id) for c in targets if c not in a_ids} == {
        ("in_batch", b.id)
    }
    assert len(follow_ons_of(await operations_of(engine, "batch_submit"), s)) == 1  # the release's only
    assert await open_alert_codes(engine) == []

    found = await money(engine, setup.bulk_operation_id)
    assert found.consistent(), found
    freed = sum((e.reserved_usd for e in entries.values()), Decimal(0)) - sum(
        (e.settled_usd for e in billed), Decimal(0)
    )
    assert freed > 0
    assert (found.month_counted, found.month_scoring, found.bulk_spent) == (
        pending.month_counted - freed,
        pending.month_scoring - freed,
        pending.bulk_spent - freed,
    )
    assert found.bulk_held == pending.bulk_held  # no hold released twice


async def test_after_29_days_the_poll_stops_listing_it(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    fake_llm: FakeLLM,
    lost: Lost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A released lost batch is polled while `submit_started_at > now - 29 days` (the results' retention,
    `RELEASED_LISTED_FOR`), without an alert, and no more after: at 29 days the poll leaves it `released` even
    once its batch is found, its items `awaiting` and its entries `ambiguous`, counted."""
    s = lost.submission_id
    await lost_and_alerted(engine, fake_llm, lost)
    assert (await release_at(logged_in_client, monkeypatch, s, lost.after)).status_code == 202
    (row,) = await submission_rows(engine)
    window_end = row.submit_started_at + batches_mod.RELEASED_LISTED_FOR

    report = await poll_once(engine, fake_llm, now=window_end - timedelta(minutes=1))
    assert (report.polled, report.ended) == (1, 0)  # still listed, still lost
    assert await open_alert_codes(engine) == []
    learn(fake_llm, lost)
    assert await poll_once(engine, fake_llm, now=window_end) == batch_results.PollReport()  # listed no more
    (row,) = await submission_rows(engine)
    assert row.state == "released"
    assert {i.result_type for i in (await item_rows(engine)).values()} == {"awaiting"}
    assert {e.state for e in (await entries_by_custom_id(engine)).values()} == {"ambiguous"}
    assert lost.stored.results_read == 0


async def test_a_release_after_the_poll_ended_the_batch_is_not_releasable(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    fake_llm: FakeLLM,
    lost: Lost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The race's sequential half: the batch found and ended by the poll (the alert resolved, the targets
    published) before the user's release, which then answers 409 `not_releasable` and moves nothing."""
    s = lost.submission_id
    await lost_and_alerted(engine, fake_llm, lost)
    learn(fake_llm, lost)
    moment = lost.after + timedelta(minutes=5)
    report = await poll_once(engine, fake_llm, now=moment)
    assert (report.ended, report.reconciled) == (1, 1)
    assert_not_releasable(await release_at(logged_in_client, monkeypatch, s, moment))
    (row,) = await submission_rows(engine)
    assert (row.state, row.released_at) == ("reconciled", None)
    assert await targets_by_state(engine, lost.batch.setup.llm_tuple_id) == {"published_accepted": N}
    assert {e.state for e in (await entries_by_custom_id(engine)).values()} == {"billed"}
    assert await open_alert_codes(engine) == []
    assert await retrieve_failing_at(logged_in_client, monkeypatch, moment) == EMPTY


async def test_a_release_waiting_on_the_poll_s_move_to_ended_answers_not_releasable(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    fake_llm: FakeLLM,
    lost: Lost,
    monkeypatch: pytest.MonkeyPatch,
    migrated: PgServer,
) -> None:
    """The race, the poll first: the release arrives while the poll's transaction holds the submission row,
    moved `submitted → ended` with the alert resolved; it waits on that row, and once the poll commits its
    lock statement sees `ended` (the predicate is evaluated in the locking statement itself) and answers 409
    `not_releasable`; the poll's pass goes on and reconciles the batch."""
    s = lost.submission_id
    await lost_and_alerted(engine, fake_llm, lost)
    learn(fake_llm, lost)
    moment = lost.after + timedelta(minutes=5)
    real = resolve_alert
    releasing: list[asyncio.Task[httpx.Response]] = []

    async def resolve_then_meet_a_release(conn: Any, *, code: str, subject: str) -> bool:
        resolved = await real(conn, code=code, subject=subject)  # inside the poll's move to `ended`
        task = asyncio.create_task(release_at(logged_in_client, monkeypatch, s, moment))
        releasing.append(task)

        async def waiting() -> bool:
            return task.done() or any(w.relation == "batch_submissions" for w in lock_wait_details(migrated))

        await await_until(waiting, 60, 0.05)
        assert not task.done()  # the release waits for the poll's row
        (wait,) = [w for w in lock_wait_details(migrated) if w.relation == "batch_submissions"]
        assert wait.key == str(s), wait
        return resolved

    monkeypatch.setattr(POLL_RESOLVE_ALERT, resolve_then_meet_a_release)
    report = await poll_once(engine, fake_llm, now=moment)
    (task,) = releasing
    assert_not_releasable(await asyncio.wait_for(task, 60))
    assert (report.ended, report.reconciled) == (1, 1)
    (row,) = await submission_rows(engine)
    assert (row.state, row.released_at) == ("reconciled", None)
    assert await targets_by_state(engine, lost.batch.setup.llm_tuple_id) == {"published_accepted": N}
    assert await open_alert_codes(engine) == []


async def test_a_poll_waiting_on_a_release_ends_the_batch_as_released(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    fake_llm: FakeLLM,
    lost: Lost,
    monkeypatch: pytest.MonkeyPatch,
    migrated: PgServer,
) -> None:
    """The race, the release first: the poll lists the submission as `submitted` and finds its batch ended
    while the release's transaction holds the row (`released`, not committed); the poll's owner lock waits on
    that row, and once the release commits it takes the row as `released` and moves it `released → ended →
    reconciled` as a released one (every item `ignored_fenced`, the entries billed, the released targets left
    `pending`), with no alert re-opened and no second follow-on."""
    s = lost.submission_id
    await lost_and_alerted(engine, fake_llm, lost)
    learn(fake_llm, lost)
    moment = lost.after + timedelta(minutes=5)
    real = batches_mod.follow_on
    polling: list[asyncio.Task[batch_results.PollReport]] = []

    async def meet_a_poll_then_follow_on(
        conn: Any, *, submission_id: int, defer: Any, request: bool = True
    ) -> bool:
        if not polling:  # the release's call, inside its transaction: the row is `released`, uncommitted
            task = asyncio.create_task(poll_once(engine, fake_llm, now=moment))
            polling.append(task)

            async def waiting() -> bool:
                return task.done() or any(
                    w.relation == "batch_submissions" for w in lock_wait_details(migrated)
                )

            await await_until(waiting, 60, 0.05)
            assert not task.done()  # the poll waits for the release's row
            (wait,) = [w for w in lock_wait_details(migrated) if w.relation == "batch_submissions"]
            assert wait.key == str(s), wait
        return await real(conn, submission_id=submission_id, defer=defer, request=request)

    monkeypatch.setattr(batches_mod, "follow_on", meet_a_poll_then_follow_on)
    r = await release_at(logged_in_client, monkeypatch, s, moment)
    assert (r.status_code, r.json()["state"]) == (202, "released"), r.text
    (task,) = polling
    report = await asyncio.wait_for(task, 60)
    assert (report.polled, report.ended, report.decided, report.reconciled) == (1, 1, N, 1)
    (row,) = await submission_rows(engine)
    assert (row.state, row.provider_batch_id) == ("reconciled", lost.batch.provider_id)
    assert row.released_at is not None
    assert Counter(await decisions(engine)) == {"ignored_fenced": N}
    assert {e.state for e in (await entries_by_custom_id(engine)).values()} == {"billed"}
    setup = lost.batch.setup
    assert await targets_by_state(engine, setup.llm_tuple_id) == {"pending": N}
    assert await attempt_outcomes(engine, setup.llm_tuple_id) == {"released": N}
    assert await open_alert_codes(engine) == []
    assert len(follow_ons_of(await operations_of(engine, "batch_submit"), s)) == 1


async def test_two_concurrent_releases_settle_the_batch_once(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    fake_llm: FakeLLM,
    lost: Lost,
    monkeypatch: pytest.MonkeyPatch,
    migrated: PgServer,
) -> None:
    """Two releases of a lost batch at once: the second arrives while the first's transaction holds the row
    (`released`, not committed) and waits on it; once the first commits, the second's lock statement re-checks
    the row and finds nothing releasable: 409 `not_releasable`. The entries are settled once (`ambiguous`, the
    month row's counters and the bulk's `spent_usd` unchanged, each the ledger's count), the holds released
    once, and the follow-on requested once."""
    s = lost.submission_id
    setup = lost.batch.setup
    await lost_and_alerted(engine, fake_llm, lost)
    held = await money(engine, setup.bulk_operation_id)
    real = batches_mod.follow_on
    second: list[asyncio.Task[httpx.Response]] = []

    async def meet_a_second_release(
        conn: Any, *, submission_id: int, defer: Any, request: bool = True
    ) -> bool:
        if not second:  # the first release's call, inside its transaction: the row is `released`, uncommitted
            task = asyncio.create_task(release_at(logged_in_client, monkeypatch, s, lost.after))
            second.append(task)

            async def waiting() -> bool:
                return task.done() or any(
                    w.relation == "batch_submissions" for w in lock_wait_details(migrated)
                )

            await await_until(waiting, 60, 0.05)
            assert not task.done()  # the second release waits for the first's row
            (wait,) = [w for w in lock_wait_details(migrated) if w.relation == "batch_submissions"]
            assert wait.key == str(s), wait
        return await real(conn, submission_id=submission_id, defer=defer, request=request)

    monkeypatch.setattr(batches_mod, "follow_on", meet_a_second_release)
    first = await release_at(logged_in_client, monkeypatch, s, lost.after)
    assert (first.status_code, first.json()["state"]) == (202, "released"), first.text
    (task,) = second
    assert_not_releasable(await asyncio.wait_for(task, 60))

    entries = await entries_by_custom_id(engine)
    assert {(e.state, e.error_code) for e in entries.values()} == {("ambiguous", batches_mod.RELEASED_CODE)}
    assert all(e.attempt_cost == e.reserved_usd for e in entries.values())  # counted once, at the reservation
    assert await attempt_outcomes(engine, setup.llm_tuple_id) == {"released": N}
    assert await targets_by_state(engine, setup.llm_tuple_id) == {"pending": N}
    released = await money(engine, setup.bulk_operation_id)
    assert released.consistent(), released
    assert (released.month_counted, released.bulk_spent, released.bulk_held) == (
        held.month_counted,
        held.bulk_spent,
        Decimal(0),
    )
    assert len(follow_ons_of(await operations_of(engine, "batch_submit"), s)) == 1
    assert await open_alert_codes(engine) == []


async def test_a_retrieval_failing_after_a_release_re_opens_no_alert(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    fake_llm: FakeLLM,
    lost: Lost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The poll lists the submission as `submitted`, the release commits while the poll retrieves, and the
    retrieval fails: the poll raises `batch.retrieve_failing` only for a submission still `submitted` under
    its lock, so the alert the release resolved stays resolved."""
    s = lost.submission_id
    await lost_and_alerted(engine, fake_llm, lost)
    moment = lost.after + timedelta(minutes=5)
    real = BatchesClient.retrieve

    async def release_then_fail(self: BatchesClient, batch_id: str) -> BatchStatus:
        monkeypatch.setattr(BatchesClient, "retrieve", real)  # once
        assert (await release_at(logged_in_client, monkeypatch, s, moment)).status_code == 202
        return await real(self, batch_id)  # still lost: a 404

    monkeypatch.setattr(BatchesClient, "retrieve", release_then_fail)
    report = await poll_once(engine, fake_llm, now=moment)
    assert (report.polled, report.ended) == (1, 0)
    assert await open_alert_codes(engine) == []
    (row,) = await submission_rows(engine)
    assert row.state == "released"
