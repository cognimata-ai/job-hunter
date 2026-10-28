"""The M3 retention rules and the target-set deletion (spec §20.1 L6304-6305, L6308, L6312, L6320, §20.2
L6367-6378, §10.10 L3979, L3984; plan Task 23, B R13, R17, Deviation 31, Rulings P8, P52, Carry T10-f, Codex
r1 #37-#39, r4 B11, r5 B9).

- `targets.delete_target_set`, the nine steps for one chunk: a non-terminal target is cancelled `purged`
  first (a claim cleared and its attempt `cancelled`, a batched one cancelled alone with its submission
  untouched, a follow-up's hold given back once), the bulk rows locked before the first target row (P52),
  then every row the set owns deleted, the spend ledger kept.
- `score_history`: a terminal target of an inactive tuple older than `retention.score_history_months` goes
  with every row it owns (its superseded and current instances, its items and calls; the ledger rows stay and
  its feedback is detached); a target of the active tuple, a younger one and one whose result an active golden
  case labels stay with all their instances, however old; a chunk whose tuple became active meanwhile is
  skipped; a target an unresolved submission still needs stays with its item and call until the reconciler
  stops listing the submission; a failing chunk fails the rule (`retention.failed`) and the pass goes on.
- `cycle_threads` and `score_cycles`: a `cycle-` thread goes 14 days after its cycle ended, and the cycle 13
  months after it, never before its thread is gone.
- `batch_submissions` and `bulk_operations`: a terminal submission goes 13 months after it ended once its
  items went, then its bulk operation; a `submit_ambiguous` one and a released one still listed stay with
  their operation.

The rows are written directly where only a rule is under test, the times moved by aging them, so a pass runs
at the real `now` (or a later one) and leaves the test's session alone; the quarantine of
`test_an_unresolved_submission_keeps_its_targets_through_retention` goes through the batch lane on the fake
Message Batches API. Every datum is invented.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.sql import q
from jobhunter.graphs.checkpointer import open_saver
from jobhunter.graphs.score.graph import thread_id
from jobhunter.llm import budget, bulk
from jobhunter.llm.fake import BatchScript
from jobhunter.privacy import retention
from jobhunter.privacy.retention import run_retention_daily
from jobhunter.profile.storage import CvStore
from jobhunter.scoring import batches, cycles, targets
from jobhunter.types import get_object
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.batches import PUBLISHED_BODY, bulk_row, bulk_with_targets, submission_rows
from tests.support.faults import PausePoint, no_fault
from tests.support.llm import FakeLLM
from tests.support.logs import logged
from tests.support.m2 import insert_profile_chain
from tests.support.m3 import (
    Job,
    active_ids,
    insert_attempt,
    insert_batch_item,
    insert_bulk_operation,
    insert_cycle,
    insert_det_result,
    insert_draft,
    insert_golden_case,
    insert_id,
    insert_job,
    insert_row,
    insert_scoring_call,
    insert_submission,
    insert_target,
    result_values,
)
from tests.support.pg import PgServer, lock_wait_details
from tests.support.procs import await_until
from tests.support.profile import OpRunner
from tests.support.scoring import activate_score_tuple, followup_hold, revisioned_jobs, state_target
from tests.support.settings import activate_profile

pytestmark = pytest.mark.integration

CURRENT_EPOCH: Final = 1  # the worker epoch the `current_epoch` fixture records
DAY: Final = timedelta(days=1)
THIRTEEN_MONTHS: Final = timedelta(days=397)
CHECKPOINT_TABLES: Final = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
_THREAD_ROWS: Final = (
    sa.text("INSERT INTO checkpoints (thread_id, checkpoint_id, checkpoint) VALUES (:t, '1', '{}')"),
    sa.text(
        "INSERT INTO checkpoint_blobs (thread_id, channel, version, type) VALUES (:t, 'steps', '1', 'empty')"
    ),
    sa.text(
        "INSERT INTO checkpoint_writes (thread_id, checkpoint_id, task_id, idx, channel, blob) "
        "VALUES (:t, '1', 'task', 0, 'steps', '\\x00')"
    ),
)
_NEXT_RESULT_ID: Final = sa.text("SELECT nextval(pg_get_serial_sequence('llm_results', 'id'))")
_LINK_ITEM: Final = sa.text("UPDATE batch_items SET llm_call_id = :c WHERE id = :i")
_ENDED_AT: Final = sa.text(
    "UPDATE score_targets SET state_changed_at = :at WHERE id = ANY(CAST(:ids AS bigint[]))"
)
_ACTIVATE_TUPLE: Final = sa.text(
    "UPDATE active_config SET profile_version_id = u.profile_version_id, llm_tuple_id = u.id "
    "FROM llm_tuples u WHERE active_config.id = 1 AND u.id = :t"
)
_ACTIVE_PAYLOAD: Final = sa.text(
    "SELECT v.payload FROM active_config a JOIN settings_versions v ON v.id = a.settings_version_id "
    "WHERE a.id = 1"
)
_SETTINGS_VERSION: Final = q(
    "INSERT INTO settings_versions (version_no, schema_version, payload, scope_version_id, model_policy_id, "
    "execution_config_id, evaluation_policy_id, effect_classes) "
    "SELECT (SELECT max(version_no) + 1 FROM settings_versions), v.schema_version, :payload, "
    "v.scope_version_id, v.model_policy_id, v.execution_config_id, v.evaluation_policy_id, '{none}' "
    "FROM active_config a JOIN settings_versions v ON v.id = a.settings_version_id WHERE a.id = 1 "
    "RETURNING id",
    jsonb=["payload"],
)
_POINT_SETTINGS: Final = sa.text("UPDATE active_config SET settings_version_id = :v WHERE id = 1")
_OPEN_FAILURE: Final = sa.text(
    "SELECT context FROM alerts WHERE dedup_key = :k AND resolved_at IS NULL AND code = 'retention.failed'"
)


@pytest.fixture
def cv_store(cv_store_dir: Path) -> CvStore:
    """The CV store of the passes: an empty directory, as these rules delete no CV file."""
    return CvStore(cv_store_dir)


async def retention_pass(
    engine: AsyncEngine,
    queue_app: procrastinate.App,
    migrated: PgServer,
    cv_store: CvStore,
    *,
    now: datetime | None = None,
    delete_thread: Callable[[Callable[[str], Awaitable[None]], str], Awaitable[None]] | None = None,
) -> dict[str, int]:
    """One `retention_daily` pass at `now` (by default the real one); `delete_thread(real, thread)`, when
    given, stands in for the saver's deletion of each thread."""
    async with open_saver(migrated.dsn("app")) as saver:
        if delete_thread is not None:
            real = saver.adelete_thread

            async def deleting(thread: str) -> None:
                await delete_thread(real, thread)

            saver.adelete_thread = deleting  # type: ignore[method-assign, assignment]
        return await run_retention_daily(
            engine, queue_app, saver, cv_store=cv_store, now=now or utcnow(), epoch=CURRENT_EPOCH
        )


async def scalar(engine: AsyncEngine, statement: str, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(sa.text(statement), params)).scalar_one()


async def ids_in(engine: AsyncEngine, table: str, ids: tuple[int, ...]) -> set[int]:
    """Which of `ids` `table` still holds."""
    statement = f"SELECT id FROM {table} WHERE id = ANY(CAST(:ids AS bigint[]))"  # noqa: S608 (test names)
    async with engine.connect() as conn:
        return {int(i) for i in (await conn.execute(sa.text(statement), {"ids": list(ids)})).scalars()}


async def superseded_tuple(conn: AsyncConnection) -> int:
    """A tuple over a profile of its own, never the active one."""
    return (await insert_profile_chain(conn, 91)).llm_tuple_id


# ---- a scored target and every row it owns ---------------------------------------------------------------


@dataclass(frozen=True)
class History:
    """A published target and the rows it owns (§20.1 L6304): a batch attempt with its item, call and draft,
    and the online attempt resumed from it with a critic note, a revision and the result, its evidence, its
    superseded and current instances, and a feedback row on it."""

    job: Job
    target_id: int
    attempts: tuple[int, ...]
    calls: tuple[int, ...]
    ledger: tuple[int, ...]
    drafts: tuple[int, ...]
    note_id: int
    result_id: int
    evidence_id: int
    instances: tuple[int, ...]
    item_id: int
    submission_id: int
    feedback_id: int

    def owned(self) -> dict[str, tuple[int, ...]]:
        """The ids of each table the target's deletion removes."""
        return {
            "score_targets": (self.target_id,),
            "score_attempts": self.attempts,
            "llm_calls": self.calls,
            "score_drafts": self.drafts,
            "critic_notes": (self.note_id,),
            "llm_results": (self.result_id,),
            "result_evidence": (self.evidence_id,),
            "det_results": self.instances,
            "batch_items": (self.item_id,),
        }


async def scored_history(
    conn: AsyncConnection, llm_tuple_id: int, bulk_operation_id: int, *, ended: datetime
) -> History:
    """A target of a new job for the tuple, published at `ended`, with every row of `History`; its superseded
    instance was computed and superseded three years before `ended`."""
    job = await insert_job(conn)
    result_id = int((await conn.execute(_NEXT_RESULT_ID)).scalar_one())
    target = await insert_target(
        conn,
        job,
        llm_tuple_id,
        state="published_accepted",
        published_result_id=result_id,
        attempt_no=2,
        bulk_operation_id=bulk_operation_id,
    )
    batch_attempt = await insert_attempt(
        conn, target, attempt_no=1, mode="batch", outcome="batch_draft_ready", lane="batch"
    )
    submission = await insert_submission(
        conn,
        llm_tuple_id,
        bulk_operation_id,
        state="reconciled",
        submit_started_at=ended - 2 * DAY,
        provider_batch_id=f"msgbatch_t23_{target}",
        results_complete_at=ended - DAY,
        reconciled_at=ended - DAY,
    )
    item = await insert_batch_item(
        conn,
        submission,
        target,
        1,
        result_type="succeeded",
        processed_at=ended - DAY,
        decision="followup",
        hold_released_at=ended,
    )
    batch_ledger, batch_call = await insert_scoring_call(
        conn,
        attempt_id=batch_attempt,
        target_id=target,
        transport="batch",
        batch_item_id=item,
        bulk_operation_id=bulk_operation_id,
    )
    await conn.execute(_LINK_ITEM, {"c": batch_call, "i": item})
    batch_draft = await insert_draft(conn, batch_attempt, batch_call)
    online = await insert_attempt(
        conn,
        target,
        attempt_no=2,
        outcome="published_accepted",
        lane="batch_followup",
        resumed_from_attempt_id=batch_attempt,
    )
    critic_ledger, critic_call = await insert_scoring_call(
        conn, attempt_id=online, target_id=target, purpose="critic"
    )
    note = await insert_id(
        conn,
        "critic_notes",
        {
            "attempt_id": online,
            "round": 1,
            "draft_id": batch_draft,
            "llm_call_id": critic_call,
            "effort": "medium",
            "triggers": ["T2"],
            "verdict_returned": "revise",
            "derived_outcome": "revise",
            "review_json": {},
            "validation": {},
        },
    )
    revise_ledger, revise_call = await insert_scoring_call(
        conn, attempt_id=online, target_id=target, purpose="revise"
    )
    revised = await insert_draft(conn, online, revise_call, 1)
    values = await result_values(
        conn, target_id=target, attempt_id=online, draft_id=revised, job=job, llm_tuple_id=llm_tuple_id
    )
    await insert_row(conn, "llm_results", {"id": result_id, **values}, overriding=True)
    await conn.execute(
        sa.text("UPDATE score_targets SET resume_draft_id = :d, state_changed_at = :at WHERE id = :t"),
        {"d": batch_draft, "at": ended, "t": target},
    )
    evidence = await insert_id(
        conn,
        "result_evidence",
        {
            "llm_result_id": result_id,
            "subject": "role_fit",
            "source_kind": "structured_field",
            "field_name": "title",
            "quote": "Testrolle",
        },
    )
    scope = (await active_ids(conn)).scope_version_id
    old = await insert_det_result(
        conn, result_id, job.job_id, scope, low=Decimal(40), high=Decimal(60), current=False
    )
    await conn.execute(
        sa.text("UPDATE det_results SET computed_at = :at, superseded_at = :at WHERE id = :d"),
        {"at": ended - timedelta(days=3 * 365), "d": old},
    )
    current = await insert_det_result(conn, result_id, job.job_id, scope, low=Decimal(50), high=Decimal(70))
    feedback = await insert_id(
        conn,
        "score_feedback",
        {
            "job_id": job.job_id,
            "job_revision_id": job.revision_id,
            "llm_result_id": result_id,
            "det_result_id": current,
            "score_snapshot": {"M": 60},
            "kind": "label",
            "label": "good",
        },
    )
    return History(
        job=job,
        target_id=target,
        attempts=(batch_attempt, online),
        calls=(batch_call, critic_call, revise_call),
        ledger=(batch_ledger, critic_ledger, revise_ledger),
        drafts=(batch_draft, revised),
        note_id=note,
        result_id=result_id,
        evidence_id=evidence,
        instances=(old, current),
        item_id=item,
        submission_id=submission,
        feedback_id=feedback,
    )


async def assert_kept(engine: AsyncEngine, history: History) -> None:
    for table, ids in history.owned().items():
        assert await ids_in(engine, table, ids) == set(ids), table


async def assert_gone(engine: AsyncEngine, history: History) -> None:
    for table, ids in history.owned().items():
        assert await ids_in(engine, table, ids) == set(), table


# ---- score_history (§20.1 L6304-6305) --------------------------------------------------------------------


async def test_an_expired_target_of_an_inactive_tuple_goes_with_every_row_it_owns(
    engine, current_epoch, queue_app, migrated, cv_store
) -> None:
    """Codex r1 #39: the nine steps for the expired target (its superseded and current instances, its item and
    calls; the ledger rows stay; the feedback and an inactive golden case detached by their foreign keys),
    while a target of the active tuple, a younger one and one whose result an active golden case labels stay
    with every row, their superseded instances included, however old."""
    now = utcnow()
    async with engine.begin() as c:
        active = await activate_score_tuple(c)
        superseded = await superseded_tuple(c)
        old_bulk = await insert_bulk_operation(c, superseded, state="cancelled")
        active_bulk = await insert_bulk_operation(c, active, state="completed")
        expired = await scored_history(c, superseded, old_bulk, ended=now - timedelta(days=380))
        of_active = await scored_history(c, active, active_bulk, ended=now - timedelta(days=1100))
        younger = await scored_history(c, superseded, old_bulk, ended=now - timedelta(days=330))
        labelled = await scored_history(c, superseded, old_bulk, ended=now - timedelta(days=1100))
        profile = int((await c.execute(sa.text("SELECT id FROM profile_versions LIMIT 1"))).scalar_one())
        golden = await insert_golden_case(
            c, labelled.job, profile, labelled_from_result_id=labelled.result_id
        )
        retired = await insert_golden_case(
            c, expired.job, profile, labelled_from_result_id=expired.result_id, active=False
        )
    ledger = await scalar(engine, "SELECT count(*) FROM llm_spend_ledger")

    report = await retention_pass(engine, queue_app, migrated, cv_store, now=now)

    assert report["score_history"] == 1
    await assert_gone(engine, expired)
    for kept in (of_active, younger, labelled):
        await assert_kept(engine, kept)
    # §20.2 step 7: the spend ledger rows stay, the expired target's with the others
    assert await scalar(engine, "SELECT count(*) FROM llm_spend_ledger") == ledger
    assert await ids_in(engine, "llm_spend_ledger", expired.ledger) == set(expired.ledger)
    async with engine.connect() as c:
        feedback = (
            await c.execute(
                sa.text(
                    "SELECT llm_result_id, det_result_id, score_snapshot, job_id FROM score_feedback "
                    "WHERE id = :f"
                ),
                {"f": expired.feedback_id},
            )
        ).one()
        cases = {
            int(case.id): case.labelled_from_result_id
            for case in await c.execute(sa.text("SELECT id, labelled_from_result_id FROM golden_cases"))
        }
    assert tuple(feedback) == (None, None, {"M": 60}, expired.job.job_id)  # detached, the snapshot kept
    assert cases == {golden: labelled.result_id, retired: None}  # the inactive case detached, not deleted
    # the job, its revision and the submission stay (the submission is younger than 13 months)
    assert await ids_in(engine, "jobs", (expired.job.job_id,)) == {expired.job.job_id}
    assert await ids_in(engine, "job_revisions", (expired.job.revision_id,)) == {expired.job.revision_id}
    assert await ids_in(engine, "batch_submissions", (expired.submission_id,)) == {expired.submission_id}
    assert (await retention_pass(engine, queue_app, migrated, cv_store, now=now))["score_history"] == 0


async def keep_score_history_for(conn: AsyncConnection, months: int) -> None:
    """A new settings version, the active one's copy with `retention.score_history_months = months`, made
    active (a test's own copy of the save's flip)."""
    payload = dict(get_object((await conn.execute(_ACTIVE_PAYLOAD)).scalar_one(), ""))
    payload["retention"] = {"score_history_months": months}
    version = int((await conn.execute(_SETTINGS_VERSION, {"payload": payload})).scalar_one())
    await conn.execute(_POINT_SETTINGS, {"v": version})


async def ended_targets(
    conn: AsyncConnection, llm_tuple_id: int, days: tuple[int, ...], *, state: str = "cancelled"
) -> list[int]:
    """One target of a new job per age in `days`, of the tuple, ended in `state` that many days ago."""
    made = []
    for age in days:
        target = await state_target(conn, await insert_job(conn), llm_tuple_id, state)
        await conn.execute(_ENDED_AT, {"at": utcnow() - timedelta(days=age), "ids": [target]})
        made.append(target)
    return made


async def target_ids(engine: AsyncEngine, ids: list[int]) -> set[int]:
    return await ids_in(engine, "score_targets", tuple(ids))


async def test_the_window_is_the_active_settings_score_history_months(
    engine, current_epoch, queue_app, migrated, cv_store
) -> None:
    """`retention.score_history_months` calendar months of the active settings at the start of the pass: 13
    months keep the target of 380 days that 12 months delete; each ended state of §20.1 counts."""
    async with engine.begin() as c:
        await activate_score_tuple(c)
        superseded = await superseded_tuple(c)
        thirteen = await ended_targets(c, superseded, (400,))
        twelve = await ended_targets(c, superseded, (380,))
        states = [
            await ended_targets(c, superseded, (400,), state=state)
            for state in (
                "refused",
                "failed_max_tokens",
                "failed_invalid_output",
                "failed_request",
                "failed_transient",
                "failed_attempts_exhausted",
                "expired_unscored",
            )
        ]
        await keep_score_history_for(c, 13)
    report = await retention_pass(engine, queue_app, migrated, cv_store)
    assert report["score_history"] == 1 + len(states)
    assert await target_ids(engine, thirteen + [t for made in states for t in made]) == set()
    assert await target_ids(engine, twelve) == set(twelve)
    async with engine.begin() as c:
        await keep_score_history_for(c, 12)
    assert (await retention_pass(engine, queue_app, migrated, cv_store))["score_history"] == 1
    assert await target_ids(engine, twelve) == set()


async def test_a_chunk_whose_tuple_became_active_meanwhile_is_skipped(
    engine, current_epoch, queue_app, migrated, cv_store, monkeypatch, caplog
) -> None:
    """L6367: the chunk's candidates are read without a lock; under `active_config FOR SHARE` the chunk checks
    them again, and their tuple, active again since, keeps them all."""
    caplog.set_level(logging.INFO)
    async with engine.begin() as c:
        active = await activate_score_tuple(c)
        superseded = await superseded_tuple(c)
        made = await ended_targets(c, superseded, (400, 500))
    pause = PausePoint(retention.SCORE_HISTORY_BEFORE_CHUNK)
    monkeypatch.setattr(retention, "afault_point", pause)
    passing = asyncio.create_task(retention_pass(engine, queue_app, migrated, cv_store))
    await asyncio.wait_for(pause.reached.wait(), 30)
    async with engine.begin() as c:  # the chunk holds no lock yet: the superseded tuple is reactivated
        await c.execute(_ACTIVATE_TUPLE, {"t": superseded})
    pause.resume()
    report = await asyncio.wait_for(passing, 30)
    assert report["score_history"] == 0
    assert await target_ids(engine, made) == set(made)
    assert [(e["rule"], e["candidates"], e["kept"]) for e in logged(caplog, "retention_targets_kept")] == [
        ("score_history", 2, 2)
    ]
    async with engine.begin() as c:  # superseded again: the next pass deletes them
        await c.execute(_ACTIVATE_TUPLE, {"t": active})
    monkeypatch.setattr(retention, "afault_point", no_fault)
    assert (await retention_pass(engine, queue_app, migrated, cv_store))["score_history"] == 2


_IN_FLIGHT_OR_LISTED: Final = [
    *((state, 425, True) for state in bulk.BATCH_IN_FLIGHT),  # however old
    ("released", 28, True),  # still listed by the reconciler (L3984)
    ("released", 30, False),
    ("reconciled", 30, False),
    ("submit_rejected", 30, False),
    ("abandoned", 30, False),
]


@pytest.mark.parametrize(("state", "started_days", "kept"), _IN_FLIGHT_OR_LISTED)
async def test_a_target_stays_while_an_unresolved_submission_needs_its_item(
    engine, current_epoch, queue_app, migrated, cv_store, state: str, started_days: int, kept: bool
) -> None:
    """Codex r4 B11, r5 B9 (`batches.UNRESOLVED_SUBMISSION_SQL`): a superseded tuple's target ended 14 months
    ago keeps its item and the item's call while its submission is in flight (`bulk.BATCH_IN_FLIGHT`), or
    `released` and listed by the reconciler (29 days from `submit_started_at`); a resolved one's goes."""
    now = utcnow()
    started = now - timedelta(days=started_days)
    columns: dict[str, object] = {"state": state, "created_at": started}
    if state != "preparing":
        columns["submit_started_at"] = started
    if state in ("submitted", "ended", "reconciled"):
        columns["provider_batch_id"] = f"msgbatch_t23_{state}"
    if state == "reconciled":
        columns.update(results_complete_at=started + DAY, reconciled_at=started + DAY)
    if state == "released":
        columns["released_at"] = started + DAY
    async with engine.begin() as c:
        await activate_score_tuple(c)
        superseded = await superseded_tuple(c)
        operation = await insert_bulk_operation(c, superseded, state="cancelled")
        submission = await insert_submission(c, superseded, operation, **columns)
        (target,) = await ended_targets(c, superseded, (425,))
        attempt = await insert_attempt(c, target, mode="batch", outcome="cancelled", lane="batch")
        item = await insert_batch_item(c, submission, target)
        _, call = await insert_scoring_call(
            c, attempt_id=attempt, target_id=target, transport="batch", batch_item_id=item
        )
        await c.execute(_LINK_ITEM, {"c": call, "i": item})
    report = await retention_pass(engine, queue_app, migrated, cv_store, now=now)
    assert report["score_history"] == (0 if kept else 1)
    left = (await target_ids(engine, [target]), await ids_in(engine, "batch_items", (item,)))
    assert left == (({target}, {item}) if kept else (set(), set()))
    assert await ids_in(engine, "llm_calls", (call,)) == ({call} if kept else set())
    assert await ids_in(engine, "batch_submissions", (submission,)) == {submission}  # younger than 13 months


async def test_a_failing_chunk_fails_the_rule_and_the_pass_goes_on(
    engine, current_epoch, queue_app, migrated, cv_store, monkeypatch, caplog
) -> None:
    """Every rule keeps M2's error boundary: the first chunk of 200 commits, the second raises, the rule
    reports -1 and raises `retention.failed` (subject `score_history`, the error class only), the rules after
    it run; the next pass deletes the rest and resolves the alert."""
    jobs = await revisioned_jobs(engine, targets.TARGET_SET_CHUNK + 1, availability="closed")
    async with engine.begin() as c:
        await activate_score_tuple(c)
        superseded = await superseded_tuple(c)
        await c.execute(
            sa.text(
                "INSERT INTO score_targets (job_revision_id, llm_tuple_id, job_id, state, state_reason, "
                "state_changed_at) SELECT r.id, :t, r.job_id, 'cancelled', 'tuple_superseded', "
                "now() - interval '400 days' FROM job_revisions r "
                "WHERE r.job_id = ANY(CAST(:jobs AS bigint[]))"
            ),
            {"t": superseded, "jobs": [job.job_id for job in jobs]},
        )
    real = targets.delete_target_set
    chunks: list[int] = []

    async def failing_second(conn: AsyncConnection, ids: Any, *, now: datetime) -> int:
        chunks.append(len(ids))
        if len(chunks) == 2:
            raise RuntimeError("the second chunk fails")
        return await real(conn, ids, now=now)

    monkeypatch.setattr(targets, "delete_target_set", failing_second)
    report = await retention_pass(engine, queue_app, migrated, cv_store)
    assert chunks == [targets.TARGET_SET_CHUNK, 1]
    assert report["score_history"] == -1
    assert (
        await scalar(engine, "SELECT count(*) FROM score_targets WHERE llm_tuple_id = :t", t=superseded) == 1
    )
    assert [e["rule"] for e in logged(caplog, "retention_rule_failed")] == ["score_history"]
    context = await scalar(engine, _OPEN_FAILURE.text, k="retention.failed:score_history")
    assert context == {"rule": "score_history", "error_class": "RuntimeError"}
    later = [rule.name for rule in retention.RULES][
        [r.name for r in retention.RULES].index("score_history") + 1 :
    ]
    assert all(report[name] >= 0 for name in later), report  # the pass went on
    monkeypatch.setattr(targets, "delete_target_set", real)
    assert (await retention_pass(engine, queue_app, migrated, cv_store))["score_history"] == 1
    async with engine.connect() as c:
        failures = await c.execute(_OPEN_FAILURE, {"k": "retention.failed:score_history"})
        assert failures.first() is None


# ---- targets.delete_target_set (§20.2 L6369-6378) --------------------------------------------------------


async def deleted(engine: AsyncEngine, ids: list[int]) -> int:
    async with engine.begin() as conn:
        await cycles.lock_active_scoring(conn)  # a purge chunk's step 2
        return await targets.delete_target_set(conn, ids, now=utcnow())


async def test_a_set_deletion_cancels_a_follow_up_target_purged_and_gives_its_hold_back_once(
    engine, current_epoch, caplog
) -> None:
    """Step 1 (Rulings P9, P52): the non-terminal follow-up target is cancelled `purged` and its hold given
    back once (its operation held 0.40 with another item's 0.15); a published target of the set is deleted
    without a cancellation."""
    caplog.set_level(logging.INFO)
    async with engine.begin() as c:
        tuple_id = await activate_score_tuple(c)
        follow_up = await state_target(c, await insert_job(c), tuple_id, "pending", lane="batch_followup")
        operation, _ = await followup_hold(c, tuple_id, follow_up, hold=Decimal("0.250000"))
        await c.execute(
            sa.text("UPDATE bulk_operations SET followup_held_usd = 0.4 WHERE id = :b"), {"b": operation}
        )
        published = await scored_history(c, tuple_id, operation, ended=utcnow())
    assert await deleted(engine, [published.target_id, follow_up]) == 2
    assert await target_ids(engine, [follow_up, published.target_id]) == set()
    await assert_gone(engine, published)
    assert (await bulk_row(engine, operation)).followup_held_usd == Decimal("0.150000")  # given back once
    (cancelled,) = logged(caplog, "score_targets_cancelled")
    assert (cancelled["reason"], cancelled["targets"], cancelled["target_ids"]) == ("purged", 1, [follow_up])
    assert len(logged(caplog, "bulk_hold_released")) == 1
    (gone,) = logged(caplog, "score_targets_deleted")
    assert (gone["targets"], gone["cancelled"]) == (2, 1)


async def test_a_set_deletion_ends_a_claim_and_cancels_a_batched_target_alone(
    engine, current_epoch, caplog
) -> None:
    """Step 1 (Carry T10-f, Ruling P8): the claimed target loses its claim and its running attempt ends
    `cancelled`; the batched one is cancelled individually, as a merge cancels it (L3979), its running batch
    attempt `cancelled`, and its submission is left as it was; then both go, the ledger kept."""
    caplog.set_level(logging.INFO)
    async with engine.begin() as c:
        tuple_id = await activate_score_tuple(c)
        operation = await insert_bulk_operation(c, tuple_id)
        submission = await insert_submission(
            c,
            tuple_id,
            operation,
            state="submitted",
            submit_started_at=utcnow(),
            provider_batch_id="msgbatch_t23",
        )
        claimed = await state_target(
            c,
            await insert_job(c),
            tuple_id,
            "in_progress",
            claim_id="00000000-0000-0000-0000-000000000023",
            claim_epoch=1,
            claimed_at=utcnow(),
            claim_heartbeat_at=utcnow(),
            attempt_no=1,
            lane="similarity",
        )
        claimed_attempt = await insert_attempt(c, claimed, lane="similarity")
        claimed_ledger, claimed_call = await insert_scoring_call(
            c, attempt_id=claimed_attempt, target_id=claimed
        )
        batched = await state_target(
            c,
            await insert_job(c),
            tuple_id,
            "in_batch",
            batch_submission_id=submission,
            attempt_no=1,
            lane="batch",
        )
        batched_attempt = await insert_attempt(c, batched, mode="batch", lane="batch")
        item = await insert_batch_item(c, submission, batched)
        batch_ledger, batch_call = await insert_scoring_call(
            c, attempt_id=batched_attempt, target_id=batched, transport="batch", batch_item_id=item
        )
        await c.execute(_LINK_ITEM, {"c": batch_call, "i": item})
    before = (await submission_rows(engine))[0]
    assert await deleted(engine, [batched, claimed]) == 2
    (cancelled,) = logged(caplog, "score_targets_cancelled")
    assert (cancelled["reason"], cancelled["targets"], cancelled["attempts"]) == ("purged", 2, 2)
    assert await target_ids(engine, [claimed, batched]) == set()
    assert await ids_in(engine, "score_attempts", (claimed_attempt, batched_attempt)) == set()
    assert await ids_in(engine, "batch_items", (item,)) == set()
    assert await ids_in(engine, "llm_calls", (claimed_call, batch_call)) == set()
    assert await ids_in(engine, "llm_spend_ledger", (claimed_ledger, batch_ledger)) == {
        claimed_ledger,
        batch_ledger,
    }
    (after,) = await submission_rows(engine)
    assert after == before  # the submission untouched: no cancel requested, no state change


async def test_a_set_deletion_locks_the_bulk_rows_before_its_first_target_row(
    engine, current_epoch, migrated
) -> None:
    """Ruling P52 (T13-a): with the follow-up hold's bulk row held elsewhere, the deletion waits for it while
    no target row of the set is locked yet."""
    async with engine.begin() as c:
        tuple_id = await activate_score_tuple(c)
        plain = await state_target(c, await insert_job(c), tuple_id, "failed_request")
        follow_up = await state_target(c, await insert_job(c), tuple_id, "pending", lane="batch_followup")
        operation, _ = await followup_hold(c, tuple_id, follow_up, hold=Decimal("0.100000"))
    async with engine.connect() as holder:
        await holder.execute(sa.text("BEGIN"))
        await holder.execute(
            sa.text("SELECT id FROM bulk_operations WHERE id = :b FOR NO KEY UPDATE"), {"b": operation}
        )
        holder_pid = int((await holder.execute(sa.text("SELECT pg_backend_pid()"))).scalar_one())
        deleting = asyncio.create_task(deleted(engine, [plain, follow_up]))

        async def waiting() -> bool:
            return deleting.done() or any(
                w.relation == "bulk_operations" for w in lock_wait_details(migrated)
            )

        await await_until(waiting, 10, 0.02)
        assert not deleting.done()
        (wait,) = [w for w in lock_wait_details(migrated) if w.relation == "bulk_operations"]
        assert (wait.key, wait.blocking_pids) == (str(operation), (holder_pid,))
        async with engine.connect() as probe:  # no target row of the set is locked yet
            await probe.execute(sa.text("BEGIN"))
            for target in (plain, follow_up):
                locked = await probe.execute(
                    sa.text("SELECT id FROM score_targets WHERE id = :t FOR UPDATE NOWAIT"), {"t": target}
                )
                assert locked.scalar_one() == target
            await probe.execute(sa.text("ROLLBACK"))
        await holder.execute(sa.text("ROLLBACK"))
    assert await asyncio.wait_for(deleting, 10) == 2
    assert (await bulk_row(engine, operation)).followup_held_usd == Decimal("0.000000")


async def test_a_set_deletion_takes_at_most_one_chunk(engine, current_epoch) -> None:
    async with engine.begin() as conn:
        with pytest.raises(ValueError, match="at most 200"):
            await targets.delete_target_set(conn, list(range(1, 202)), now=utcnow())
        assert await targets.delete_target_set(conn, [], now=utcnow()) == 0


# ---- cycle_threads and score_cycles (§20.1 L6312, L6320) ------------------------------------------------


async def insert_thread(conn: AsyncConnection, thread: str) -> None:
    """A thread with a row in each of the saver's three tables."""
    for statement in _THREAD_ROWS:
        await conn.execute(statement, {"t": thread})


async def thread_rows(engine: AsyncEngine, thread: str) -> list[int]:
    """The rows of `thread` in `checkpoints`, `checkpoint_blobs` and `checkpoint_writes`."""
    return [
        int(await scalar(engine, f"SELECT count(*) FROM {table} WHERE thread_id = :t", t=thread))  # noqa: S608
        for table in CHECKPOINT_TABLES
    ]


async def ended_cycle(
    conn: AsyncConnection, llm_tuple_id: int, *, days: int, status: str = "completed"
) -> int:
    """A cycle that ended `days` ago in `status`, with its checkpoint thread."""
    cycle = await insert_cycle(conn, llm_tuple_id, status=status)
    await conn.execute(
        sa.text("UPDATE score_cycles SET started_at = :at, finished_at = :at WHERE id = :c"),
        {"at": utcnow() - timedelta(days=days), "c": cycle},
    )
    await insert_thread(conn, thread_id(cycle))
    return cycle


async def test_cycle_threads_go_14_days_after_the_cycle_ended_and_no_other_thread(
    engine, current_epoch, queue_app, migrated, cv_store
) -> None:
    """L6320, B R17: the `cycle-<id>` thread of a cycle ended 15 days ago goes with its rows in the three
    tables; that of one ended 13 days ago, a running cycle's, a `cycle-` name without a cycle and the `run-`
    and `profile-` threads no rule owns stay."""
    async with engine.begin() as c:
        tuple_id = await activate_score_tuple(c)
        ended_15 = await ended_cycle(c, tuple_id, days=15)
        ended_13 = await ended_cycle(c, tuple_id, days=13, status="failed")
        running = await insert_cycle(c, tuple_id)
        await insert_thread(c, thread_id(running))
        others = (thread_id(9_000_000), "run-424242-a1", "profile-40-x1-a0")
        for thread in others:
            await insert_thread(c, thread)
    report = await retention_pass(engine, queue_app, migrated, cv_store)
    assert report["cycle_threads"] == 1
    assert await thread_rows(engine, thread_id(ended_15)) == [0, 0, 0]
    for thread in (thread_id(ended_13), thread_id(running), *others):
        assert await thread_rows(engine, thread) == [1, 1, 1], thread
    assert report["score_cycles"] == 0  # 15 days: the cycle row stays
    assert await ids_in(engine, "score_cycles", (ended_15, ended_13, running)) == {
        ended_15,
        ended_13,
        running,
    }
    assert (await retention_pass(engine, queue_app, migrated, cv_store))["cycle_threads"] == 0


async def test_a_cycle_goes_13_months_after_it_ended_once_its_thread_is_gone(
    engine, current_epoch, queue_app, migrated, cv_store
) -> None:
    """L6312: a cycle ended 398 days ago goes in the pass that deletes its thread, its attempts kept with
    `cycle_id` NULL; one ended 396 days ago stays (its thread goes)."""
    async with engine.begin() as c:
        tuple_id = await activate_score_tuple(c)
        old = await ended_cycle(c, tuple_id, days=398)
        recent = await ended_cycle(c, tuple_id, days=396)
        target = await state_target(c, await insert_job(c), tuple_id, "failed_request")
        attempt = await insert_attempt(c, target, outcome="failed_request", cycle_id=old)
    report = await retention_pass(engine, queue_app, migrated, cv_store)
    assert (report["cycle_threads"], report["score_cycles"]) == (2, 1)
    assert await ids_in(engine, "score_cycles", (old, recent)) == {recent}
    assert await scalar(engine, "SELECT cycle_id FROM score_attempts WHERE id = :a", a=attempt) is None


async def test_a_cycle_whose_thread_deletion_failed_keeps_its_row_and_both_go_in_a_later_pass(
    engine, current_epoch, queue_app, migrated, cv_store, caplog
) -> None:
    """Codex r1 #38: the saver fails on the old cycle's thread, so `cycle_threads` fails and `score_cycles`,
    which depends on it, is skipped: the cycle row stays with its thread. The next pass deletes both."""
    async with engine.begin() as c:
        tuple_id = await activate_score_tuple(c)
        cycle = await ended_cycle(c, tuple_id, days=400)

    async def refuse_cycles(real: Callable[[str], Awaitable[None]], thread: str) -> None:
        if thread.startswith("cycle-"):
            raise OSError("the saver is down")
        await real(thread)

    report = await retention_pass(engine, queue_app, migrated, cv_store, delete_thread=refuse_cycles)
    assert (report["cycle_threads"], report["score_cycles"]) == (-1, -1)
    assert [
        e["blocked_by"] for e in logged(caplog, "retention_rule_skipped") if e["rule"] == "score_cycles"
    ] == [["cycle_threads"]]
    assert await ids_in(engine, "score_cycles", (cycle,)) == {cycle}
    assert await thread_rows(engine, thread_id(cycle)) == [1, 1, 1]
    report = await retention_pass(engine, queue_app, migrated, cv_store)
    assert (report["cycle_threads"], report["score_cycles"]) == (1, 1)
    assert await ids_in(engine, "score_cycles", (cycle,)) == set()
    assert await thread_rows(engine, thread_id(cycle)) == [0, 0, 0]


# ---- batch_submissions and bulk_operations (Deviation 31, §20.1 L6312) -----------------------------------


async def history_submission(
    conn: AsyncConnection, llm_tuple_id: int, bulk_operation_id: int, *, state: str, **columns: object
) -> tuple[int, int]:
    """A submission of the bulk operation in `state` with one item of a superseded target that ended 14
    months ago, the item's call naming it; `(submission_id, target_id)`."""
    submission = await insert_submission(conn, llm_tuple_id, bulk_operation_id, state=state, **columns)
    (target,) = await ended_targets(conn, llm_tuple_id, (425,))
    attempt = await insert_attempt(conn, target, mode="batch", outcome="cancelled", lane="batch")
    processed = state == "reconciled"
    item = await insert_batch_item(
        conn,
        submission,
        target,
        result_type="succeeded" if processed else "awaiting",
        processed_at=columns.get("reconciled_at") if processed else None,
        decision="ignored_fenced" if processed else None,
    )
    _, call = await insert_scoring_call(
        conn, attempt_id=attempt, target_id=target, transport="batch", batch_item_id=item
    )
    await conn.execute(_LINK_ITEM, {"c": call, "i": item})
    return submission, target


async def test_a_batch_history_ages_out_13_months_after_its_items_went_then_its_bulk_operation(
    engine, current_epoch, queue_app, migrated, cv_store
) -> None:
    """Codex r1 #37, Deviation 31. The first pass deletes the superseded targets of two reconciled
    submissions, one released 40 days ago and one abandoned (their items with them), but keeps the
    submissions (younger than 13 months) and the targets of a `submit_ambiguous` one and of one released 20
    days before the second pass. The second pass, 398 days later, deletes those four submissions and then
    their bulk operation; the ambiguous and the listed released submissions stay with their targets and their
    operation."""
    now = utcnow()
    later = now + timedelta(days=398)
    async with engine.begin() as c:
        await activate_score_tuple(c)
        superseded = await superseded_tuple(c)
        aged = await insert_bulk_operation(c, superseded, state="cancelled")
        held = await insert_bulk_operation(c, superseded, state="cancelled")
        await c.execute(
            sa.text("UPDATE bulk_operations SET finished_at = :at WHERE id = ANY(CAST(:ids AS bigint[]))"),
            {"at": now - 2 * DAY, "ids": [aged, held]},
        )
        resolved = [
            await history_submission(
                c,
                superseded,
                aged,
                state="reconciled",
                submit_started_at=now - days * DAY,
                provider_batch_id=f"msgbatch_t23_r{days}",
                results_complete_at=now - (days - 1) * DAY,
                reconciled_at=now - (days - 1) * DAY,
            )
            for days in (3, 4)
        ]
        resolved.append(
            await history_submission(
                c,
                superseded,
                aged,
                state="released",
                submit_started_at=now - 41 * DAY,
                released_at=now - 40 * DAY,
            )
        )
        resolved.append(
            await history_submission(c, superseded, aged, state="abandoned", created_at=now - 5 * DAY)
        )
        unresolved = [
            await history_submission(
                c, superseded, held, state="submit_ambiguous", submit_started_at=now - 5 * DAY
            ),
            await history_submission(
                c,
                superseded,
                held,
                state="released",
                submit_started_at=later - 21 * DAY,
                released_at=later - 20 * DAY,
            ),
        ]

    first = await retention_pass(engine, queue_app, migrated, cv_store, now=now)
    assert (first["score_history"], first["batch_submissions"], first["bulk_operations"]) == (4, 0, 0)
    assert await target_ids(engine, [t for _, t in resolved]) == set()
    assert await target_ids(engine, [t for _, t in unresolved]) == {t for _, t in unresolved}
    every = tuple(s for s, _ in resolved + unresolved)
    assert await ids_in(engine, "batch_submissions", every) == set(every)
    assert await scalar(
        engine,
        "SELECT count(*) FROM batch_items WHERE submission_id = ANY(CAST(:s AS bigint[]))",
        s=list(every),
    ) == len(unresolved)

    second = await retention_pass(engine, queue_app, migrated, cv_store, now=later)
    assert (second["score_history"], second["batch_submissions"], second["bulk_operations"]) == (0, 4, 1)
    assert await ids_in(engine, "batch_submissions", every) == {s for s, _ in unresolved}
    assert await ids_in(engine, "bulk_operations", (aged, held)) == {held}
    assert await target_ids(engine, [t for _, t in unresolved]) == {t for _, t in unresolved}


async def quarantined(engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, n: int) -> tuple[Any, int]:
    """`n` targets of the score tuple batched, the batch created with its answer lost (`ambiguous_created`):
    the `submit_ambiguous` row and the tuple."""
    setup = await bulk_with_targets(engine, n)
    for target in setup.targets:
        fake_llm.answer("generate", target.job_revision_id, PUBLISHED_BODY)
    fake_llm.batches(BatchScript(create="ambiguous_created", polls_until_ended=5))
    assert setup.operation_id is not None
    await op_runner.run_one(setup.operation_id)
    (row,) = await submission_rows(engine)
    assert row.state == "submit_ambiguous"
    return row, setup.llm_tuple_id


_COUNTED: Final = "SELECT counted_usd FROM llm_budget WHERE month = :m"
_ENTRIES: Final = sa.text(
    "SELECT l.state, count(*) AS n FROM llm_spend_ledger l JOIN llm_calls c ON c.spend_ledger_id = l.id "
    "JOIN batch_items i ON i.llm_call_id = c.id WHERE i.submission_id = :s GROUP BY l.state"
)


async def entry_states(conn: AsyncConnection, submission_id: int) -> dict[str, int]:
    """The submission's items' ledger entries counted by state, found through the items' calls."""
    return {str(r.state): int(r.n) for r in await conn.execute(_ENTRIES, {"s": submission_id})}


async def test_an_unresolved_submission_keeps_its_targets_through_retention(
    engine, current_epoch, queue_app, migrated, cv_store, op_runner: OpRunner, fake_llm: FakeLLM
) -> None:
    """Codex r4 B11, r5 B9: a superseded tuple's targets, ended 14 months ago, whose items belong to a
    `submit_ambiguous` submission of the same age survive the pass with their items and calls; the user's
    release then moves every item's entry `dispatched → ambiguous` through the join on `llm_calls` with the
    counted spend unchanged (L3997); the next pass deletes the targets (no listing window protects a
    submission started 14 months ago), the ledger entries kept; and 13 months after its release the
    submission goes, then its bulk operation."""
    n = 200  # the smallest batch the settings allow (`batch_min_targets`)
    row, old_tuple = await quarantined(engine, op_runner, fake_llm, n)
    await activate_profile(engine)  # the tuple superseded: the batched targets cancelled one by one
    aged = utcnow() - timedelta(days=425)
    async with engine.begin() as c:
        await c.execute(
            sa.text("UPDATE score_targets SET state_changed_at = :at WHERE llm_tuple_id = :t"),
            {"at": aged, "t": old_tuple},
        )
        await c.execute(
            sa.text("UPDATE batch_submissions SET created_at = :at, submit_started_at = :at WHERE id = :s"),
            {"at": aged, "s": row.id},
        )
    month = budget.month_of(utcnow())
    counted = await scalar(engine, _COUNTED, m=month)
    spent = (await bulk_row(engine, row.bulk_operation_id)).spent_usd

    first = await retention_pass(engine, queue_app, migrated, cv_store)
    assert first["score_history"] == 0
    assert await scalar(engine, "SELECT count(*) FROM batch_items WHERE submission_id = :s", s=row.id) == n
    async with engine.connect() as c:
        assert await entry_states(c, row.id) == {"dispatched": n}

    async with engine.begin() as c:  # the user's release (the route's first lock, Task 21)
        await cycles.lock_active_scoring(c)
        await batches.release(c, row.id, now=utcnow(), defer=defer_in_tx)
    async with engine.connect() as c:
        assert await entry_states(c, row.id) == {"ambiguous": n}
    assert await scalar(engine, _COUNTED, m=month) == counted  # the reservations stay counted
    assert (await bulk_row(engine, row.bulk_operation_id)).spent_usd == spent

    ledger = await scalar(engine, "SELECT count(*) FROM llm_spend_ledger")
    second = await retention_pass(engine, queue_app, migrated, cv_store)
    assert (second["score_history"], second["batch_submissions"]) == (n, 0)
    assert (
        await scalar(engine, "SELECT count(*) FROM score_targets WHERE llm_tuple_id = :t", t=old_tuple) == 0
    )
    assert await scalar(engine, "SELECT count(*) FROM llm_calls WHERE transport = 'batch'") == 0
    assert await scalar(engine, "SELECT count(*) FROM llm_spend_ledger") == ledger
    assert await scalar(engine, _COUNTED, m=month) == counted
    (released,) = await submission_rows(engine)
    assert released.state == "released"

    third = await retention_pass(
        engine, queue_app, migrated, cv_store, now=released.released_at + THIRTEEN_MONTHS + DAY
    )
    assert (third["batch_submissions"], third["bulk_operations"]) == (1, 1)
    assert await submission_rows(engine) == []
    assert await ids_in(engine, "bulk_operations", (row.bulk_operation_id,)) == set()
