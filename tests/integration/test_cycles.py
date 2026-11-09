"""Score cycles on PostgreSQL (spec §3.4 step 6, §6.9.2, §9.3; plan Task 11, Codex r1 #16, #19, #20, #25, r4
B8, r5 B4, r7 B5, Rulings P11, P13, P47, Deviations 5, 19, 20, 39): `scoring.cycles` and
`worker.tick.tick_score`.

`request_cycle` defers one `score_cycle` job per trigger kind and none while one waits; `start_cycle` gates a
`periodic`, `manual` or `batch_followup` cycle on pending targets and the bucket under the month lock (the
first cycle of a month creating its row), records `S`, `ĉ` and the bucket, leaves its transaction usable when
a cycle still runs, and starts a `score_now` cycle outside the bucket without the month lock; every pause
reason blocks it, an acknowledgement lifts the auth and bound pauses, and the cap pause reads the headroom,
never the alert; a lone fenced or exhausted pending target starts no cycle (the start's gate is the
admission's predicate), and a start reads the active configuration once; `ĉ` uses its history only from 20
qualifying attempts; `schema_verified` needs a passed live case for every (schema, model) pair of the policy;
and a lone stalled `score_cycle` job is failed, never retried.

Every datum is invented.
"""

import asyncio
import hashlib
import inspect
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from types import SimpleNamespace
from typing import Any, cast

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.llm import budget
from jobhunter.llm.budget import month_of
from jobhunter.llm.spend_ledger import current_params, ensure_month
from jobhunter.scoring import cycles
from jobhunter.scoring.cycles import CycleTrigger
from jobhunter.settings import validate
from jobhunter.settings.llm_projection import central_cost_per_target
from jobhunter.settings.model import ModelPolicy
from jobhunter.worker import reconcilers, tasks
from jobhunter.worker.runtime import ShutdownState
from jobhunter.worker.tasks import defer_in_tx, job_task_name, release_stalled_jobs
from jobhunter.worker.tick import tick_score
from procrastinate.job_context import JobContext
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.fixtures import count_rows
from tests.support.logs import logged
from tests.support.m3 import insert_attempt, insert_cycle, insert_id
from tests.support.pg import PgServer, lock_wait_details
from tests.support.procs import await_until
from tests.support.scoring import cycle_of, insert_targets, score_llm_tuple

pytestmark = pytest.mark.integration

CENTRAL = central_cost_per_target(ModelPolicy())  # $0.052646, policy B
TRIGGERS: tuple[CycleTrigger, ...] = ("periodic", "score_now", "batch_followup", "manual")
JOBS = sa.text(
    "SELECT queue_name, task_name, lock, queueing_lock, args, status FROM procrastinate_jobs "
    "WHERE task_name = :t ORDER BY id"
)


@pytest.fixture
def now() -> datetime:
    return utcnow()


async def start(
    engine: AsyncEngine, trigger: CycleTrigger, now: datetime, *, fake_llm: bool = True, epoch: int = 1
) -> int | None:
    async with engine.begin() as conn:
        return await cycles.start_cycle(conn, trigger=trigger, now=now, epoch=epoch, fake_llm=fake_llm)


async def pause(engine: AsyncEngine, now: datetime, *, fake_llm: bool = True) -> str | None:
    async with engine.connect() as conn:
        return await cycles.pause_reason(conn, fake_llm=fake_llm, now=now)


async def set_bucket(engine: AsyncEngine, usd: str, at: datetime) -> None:
    """The month of `at` with `usd` in its bucket, accrued to `at`."""
    async with engine.begin() as conn:
        await ensure_month(conn, month_of(at), await current_params(conn), at)
        await conn.execute(
            sa.text("UPDATE llm_budget SET bucket_usd = :b, bucket_at = :at WHERE month = :m"),
            {"b": Decimal(usd), "at": at, "m": month_of(at)},
        )


async def open_alert(engine: AsyncEngine, code: str, subject: str = "x") -> int:
    async with engine.begin() as conn:
        return await insert_id(
            conn,
            "alerts",
            {"code": code, "severity": "critical", "dedup_key": f"{code}:{subject}", "message": "synthetic"},
        )


async def execute(engine: AsyncEngine, sql: str, **params: object) -> None:
    async with engine.begin() as conn:
        await conn.execute(sa.text(sql), params)


# ---- request_cycle ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("trigger", TRIGGERS)
async def test_request_cycle_defers_one_job_per_trigger_and_none_while_one_waits(
    engine: AsyncEngine, queue_app: procrastinate.App, trigger: CycleTrigger
) -> None:
    async with engine.begin() as conn:
        assert await cycles.request_cycle(conn, trigger=trigger, defer=defer_in_tx) is True
        assert await cycles.request_cycle(conn, trigger=trigger, defer=defer_in_tx) is False  # one waits
        assert (await conn.execute(sa.text("SELECT 1"))).scalar_one() == 1  # the transaction is usable
        other: CycleTrigger = "manual" if trigger != "manual" else "periodic"
        assert await cycles.request_cycle(conn, trigger=other, defer=defer_in_tx) is True  # another kind
    async with engine.connect() as conn:
        jobs = [tuple(row) for row in await conn.execute(JOBS, {"t": job_task_name("score_cycle")})]
    assert jobs == [
        ("score", "jh:score_cycle", "score", f"score_cycle:{trigger}", {"trigger": trigger}, "todo"),
        ("score", "jh:score_cycle", "score", f"score_cycle:{other}", {"trigger": other}, "todo"),
    ]


# ---- start_cycle ------------------------------------------------------------------------------------------


async def test_a_periodic_start_needs_pending_targets_the_bucket_and_no_running_cycle(
    engine: AsyncEngine, current_epoch: int, now: datetime
) -> None:
    gate = min(10 * CENTRAL, Decimal("1.50"))  # min(min_cycle_targets x ĉ, B_max): $0.52646
    assert await start(engine, "periodic", now) is None  # no profile tuple yet, no target
    (pending,) = await insert_targets(engine, 1)
    await set_bucket(engine, "0.526", now)  # below the gate
    assert await start(engine, "periodic", now) is None
    async with engine.begin() as conn:  # not yet due: no pending target with not_before passed
        await conn.execute(
            sa.text("UPDATE score_targets SET not_before = :later WHERE id = :t"),
            {"later": now + timedelta(minutes=5), "t": pending.target_id},
        )
    await set_bucket(engine, "1.000000", now)
    assert await start(engine, "periodic", now) is None
    await execute(engine, "UPDATE score_targets SET not_before = NULL")
    cycle_id = await start(engine, "periodic", now)
    assert cycle_id is not None and gate == Decimal("0.526460")
    row = await cycle_of(engine, cycle_id)
    assert (row.status, row.trigger, row.worker_epoch, row.admitted) == ("running", "periodic", 1, {})
    assert row.bucket_usd_at_start == Decimal("1.000000")
    assert row.c_hat_usd == CENTRAL
    assert row.admission_size == 18  # min(60, floor(1.00 / 0.052646))
    assert await start(engine, "periodic", now) is None  # one cycle runs


async def test_a_full_bucket_opens_the_gate_when_the_threshold_is_b_max(
    engine: AsyncEngine, current_epoch: int, now: datetime
) -> None:
    """Task 18 ruling T18-b: with `min_cycle_targets x ĉ >= B_max` the gate's threshold is `B_max`, which a
    full bucket meets at the precision it is stored at (`budget.USD_QUANTUM`, rounded down), even when `B_max`
    itself has more decimals (the dev defaults: $1.551666...). Before the fix the stored $1.551666 never
    reached the unrounded $1.5516666..., so no periodic, manual or follow-up cycle could start again."""
    (pending,) = await insert_targets(engine, 1)
    month = month_of(now)
    async with engine.begin() as conn:
        params = await current_params(conn)
        await ensure_month(conn, month, params, now)
        # the dev stack's cap and buffer, and a bucket a month behind: the lock accrues it to capacity
        await conn.execute(
            sa.text(
                "UPDATE llm_budget SET cap_usd = 100.00, overrun_buffer_usd = 2.00, bucket_usd = 0, "
                "bucket_at = :at WHERE month = :m"
            ),
            {"at": now - timedelta(days=31), "m": month},
        )
        for number in range(1, 21):  # ĉ from history: 20 published attempts at $0.30
            await insert_attempt(
                conn,
                pending.target_id,
                attempt_no=number,
                outcome="published_accepted",
                charged=True,
                cost_usd=Decimal("0.300000"),
                ended_at=now - timedelta(hours=1),
            )
        config = await cycles.scoring_config(conn)
        assert await cycles.periodic_gate(conn, config, now=now) is True
        row = (await conn.execute(sa.text("SELECT * FROM llm_budget WHERE month = :m"), {"m": month})).one()
    row_params = replace(params, cap_usd=row.cap_usd, overrun_buffer_usd=row.overrun_buffer_usd)
    capacity = budget.bucket_capacity(row_params, month)
    assert capacity != capacity.quantize(budget.USD_QUANTUM)  # more than six decimals
    assert 10 * Decimal("0.300000") >= capacity  # the threshold is B_max
    assert row.bucket_usd == capacity.quantize(budget.USD_QUANTUM, rounding=ROUND_FLOOR)  # full, as stored
    cycle_id = await start(engine, "manual", now)
    assert cycle_id is not None
    started = await cycle_of(engine, cycle_id)
    assert (started.trigger, started.c_hat_usd) == ("manual", Decimal("0.300000"))
    assert started.bucket_usd_at_start == row.bucket_usd


async def test_the_admission_size_is_capped_by_max_targets_and_floored_at_zero(
    engine: AsyncEngine, current_epoch: int, now: datetime
) -> None:
    await insert_targets(engine, 2, lane="batch_followup")
    async with engine.begin() as conn:  # a settings version of 2 targets a cycle at most, made active
        version = (
            await conn.execute(
                sa.text(
                    "INSERT INTO settings_versions (version_no, schema_version, payload, scope_version_id, "
                    "model_policy_id, execution_config_id, evaluation_policy_id, effect_classes) "
                    "SELECT (SELECT max(version_no) + 1 FROM settings_versions), v.schema_version, "
                    "jsonb_set(v.payload, '{scoring,max_targets_per_cycle}', '2'), v.scope_version_id, "
                    "v.model_policy_id, v.execution_config_id, v.evaluation_policy_id, '{budget}' "
                    "FROM active_config a JOIN settings_versions v ON v.id = a.settings_version_id "
                    "WHERE a.id = 1 RETURNING id"
                )
            )
        ).scalar_one()
        await conn.execute(sa.text("UPDATE active_config SET settings_version_id = :v"), {"v": version})
    await set_bucket(engine, "1.400000", now)
    cycle_id = await start(engine, "batch_followup", now)
    assert cycle_id is not None
    assert (await cycle_of(engine, cycle_id)).admission_size == 2


async def test_a_second_start_while_one_runs_returns_none_and_the_transaction_stays_usable(
    engine: AsyncEngine, current_epoch: int, now: datetime
) -> None:
    # Codex r1 #16: the partial unique index arbitrates through ON CONFLICT DO NOTHING, no exception
    await insert_targets(engine, 1, lane="score_now")
    async with engine.begin() as conn:
        running = await insert_cycle(conn, await score_llm_tuple(conn))
    async with engine.begin() as conn:
        started = await cycles.start_cycle(conn, trigger="score_now", now=now, epoch=1, fake_llm=True)
        assert started is None
        written = await conn.execute(sa.text("UPDATE score_targets SET updated_at = now()"))
        assert written.rowcount == 1  # the same transaction writes on
    assert await count_rows(engine, "score_cycles") == 1
    assert (await cycle_of(engine, running)).status == "running"


async def test_a_score_now_start_is_outside_the_bucket_and_takes_no_month_lock(
    engine: AsyncEngine, current_epoch: int, now: datetime, migrated: PgServer
) -> None:
    assert await start(engine, "score_now", now) is None  # no score_now target
    await insert_targets(engine, 2)
    assert await start(engine, "score_now", now) is None  # pending, but not Score now
    await insert_targets(engine, 1, lane="score_now")
    await set_bucket(engine, "-0.500000", now)  # the bucket owes; Score now bypasses it
    async with engine.connect() as blocker:
        await blocker.execute(
            sa.text("SELECT 1 FROM llm_budget WHERE month = :m FOR UPDATE"), {"m": month_of(now)}
        )
        cycle_id = await asyncio.wait_for(start(engine, "score_now", now), 10)  # never waits for the row
        assert cycle_id is not None
        assert not [w for w in lock_wait_details(migrated) if w.relation == "llm_budget"]
        # A bucket-gated start does wait for the row (its gate is read under the month lock)
        gated = asyncio.create_task(start(engine, "manual", now))

        async def waits() -> bool:
            return any(w.relation == "llm_budget" for w in lock_wait_details(migrated))

        await await_until(waits, 10, 0.02)
        await blocker.rollback()
    assert await asyncio.wait_for(gated, 10) is None  # the bucket owes
    row = await cycle_of(engine, cycle_id)
    assert (row.trigger, row.admission_size, row.c_hat_usd) == ("score_now", 0, CENTRAL)
    assert row.bucket_usd_at_start == Decimal("-0.500000")


async def test_a_batch_followup_start_needs_a_followup_target_and_the_bucket(
    engine: AsyncEngine, current_epoch: int, now: datetime
) -> None:
    await insert_targets(engine, 1)
    await set_bucket(engine, "1.000000", now)
    assert await start(engine, "batch_followup", now) is None  # no batch_followup target
    await insert_targets(engine, 1, lane="batch_followup")
    await set_bucket(engine, "0.100000", now)
    assert await start(engine, "batch_followup", now) is None  # gated like periodic
    await set_bucket(engine, "1.000000", now)
    assert await start(engine, "batch_followup", now) is not None


@pytest.mark.parametrize("unscorable", ["fenced", "exhausted"])
@pytest.mark.parametrize("trigger", TRIGGERS)
async def test_a_lone_fenced_or_exhausted_pending_target_starts_no_cycle(
    engine: AsyncEngine,
    current_epoch: int,
    now: datetime,
    trigger: CycleTrigger,
    unscorable: str,
) -> None:
    # The start's gate is the admission's predicate (`admission.any_admissible`): a target no round could
    # admit starts nothing, and the tick requests nothing for it
    lane = trigger if trigger in ("score_now", "batch_followup") else None
    (pending,) = await insert_targets(engine, 1, lane=lane)
    await set_bucket(engine, "1.400000", now)
    if unscorable == "fenced":  # a jobs purge fence over the target's job
        await execute(
            engine,
            "INSERT INTO purge_fences (kind, job_ids, manifest_id) VALUES ('jobs', ARRAY[:j]::bigint[], 1)",
            j=pending.job_id,
        )
    else:  # every charged attempt spent
        await execute(
            engine, "UPDATE score_targets SET charged_attempts = 4 WHERE id = :t", t=pending.target_id
        )
    assert await start(engine, trigger, now) is None
    async with engine.begin() as conn:
        assert await cycles.periodic_gate(conn, await cycles.scoring_config(conn), now=now) is False
    if unscorable == "fenced":
        await execute(engine, "UPDATE purge_fences SET released_at = now()")
    else:
        await execute(
            engine, "UPDATE score_targets SET charged_attempts = 3 WHERE id = :t", t=pending.target_id
        )
    assert await start(engine, trigger, now) is not None  # admissible again: the cycle starts


async def test_a_start_reads_the_active_configuration_once(
    engine: AsyncEngine, current_epoch: int, now: datetime
) -> None:
    # The pointers, the policies and the settings, read once for the guard, the gate and the row
    await insert_targets(engine, 1)
    await set_bucket(engine, "1.400000", now)
    statements: list[str] = []

    def seen(*args: Any) -> None:
        statements.append(str(args[2]))  # (conn, cursor, statement, parameters, context, executemany)

    sa.event.listen(engine.sync_engine, "before_cursor_execute", seen)
    try:
        assert await start(engine, "manual", now) is not None
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", seen)
    for table in ("active_config", "model_policies", "settings_versions"):
        assert sum(table in statement for statement in statements) == 1, table
    assert inspect.signature(cycles.pause_reason).parameters["now"].default is inspect.Parameter.empty


@pytest.mark.parametrize("where", ["start_cycle", "tick_score"])
async def test_the_first_cycle_of_a_month_creates_its_row(
    engine: AsyncEngine, current_epoch: int, queue_app: procrastinate.App, now: datetime, where: str
) -> None:
    # Codex r4 B8: no llm_budget row for the month and no other call or budget edit
    await insert_targets(engine, 1)
    assert await count_rows(engine, "llm_budget") == 0
    cycle_id = None
    if where == "start_cycle":
        cycle_id = await start(engine, "periodic", now)
        assert cycle_id is not None
    else:
        report = await tick_score(engine, now=now, epoch=1, defer=defer_in_tx, fake_llm=True)
        assert (report.requested, report.pause_reason) == (True, None)
        assert await count_rows(engine, "procrastinate_jobs", task_name="jh:score_cycle") == 1
    async with engine.connect() as conn:
        p = await current_params(conn)
        row = (
            await conn.execute(
                sa.text("SELECT bucket_usd FROM llm_budget WHERE month = :m"), {"m": month_of(now)}
            )
        ).one()
    assert row.bucket_usd == budget.bucket_capacity(p, month_of(now)).quantize(
        Decimal("0.000001"), "ROUND_FLOOR"
    )
    if cycle_id is not None:
        assert (await cycle_of(engine, cycle_id)).bucket_usd_at_start == row.bucket_usd


async def test_tick_score_requests_a_periodic_cycle_only_through_its_gate(
    engine: AsyncEngine, current_epoch: int, queue_app: procrastinate.App, now: datetime
) -> None:
    async def tick() -> tuple[bool, str | None]:
        report = await tick_score(engine, now=now, epoch=1, defer=defer_in_tx, fake_llm=True)
        return report.requested, report.pause_reason

    assert await tick() == (False, None)  # no tuple, no target
    await insert_targets(engine, 1)
    await set_bucket(engine, "0.300000", now)
    assert await tick() == (False, None)  # the bucket below the gate
    await set_bucket(engine, "1.000000", now)
    async with engine.begin() as conn:
        running = await insert_cycle(conn, await score_llm_tuple(conn))
    assert await tick() == (False, None)  # a cycle runs
    await execute(
        engine, "UPDATE score_cycles SET status = 'completed', finished_at = now() WHERE id = :c", c=running
    )
    assert await tick() == (True, None)
    assert await tick() == (False, None)  # the job waits (its queueing lock)
    await open_alert(engine, "llm.auth_failed")
    assert await tick() == (False, "auth_failed")


# ---- the pause reasons ------------------------------------------------------------------------------------


async def exhaust_headroom(engine: AsyncEngine, now: datetime) -> None:
    """The month's scoring headroom spent: `A - reserve = $93.10` counted."""
    async with engine.begin() as conn:
        await ensure_month(conn, month_of(now), await current_params(conn), now)
        await conn.execute(
            sa.text(
                "UPDATE llm_budget SET counted_usd = 93.10, scoring_counted_usd = 93.10 WHERE month = :m"
            ),
            {"m": month_of(now)},
        )


async def fence_globally(engine: AsyncEngine, now: datetime) -> None:
    await execute(engine, "INSERT INTO purge_fences (kind, manifest_id) VALUES ('global', 1)")


async def bound_violated(engine: AsyncEngine, now: datetime) -> None:
    await open_alert(engine, "llm.reservation_bound_violated")


async def auth_failed(engine: AsyncEngine, now: datetime) -> None:
    await open_alert(engine, "llm.auth_failed")


async def unverified(engine: AsyncEngine, now: datetime) -> None:
    pass  # no live dry-run of the active version exists: outside fake LLM mode, the lane pauses


PAUSES = [
    ("reservation_bound_violated", bound_violated, True),
    ("auth_failed", auth_failed, True),
    ("cap_reached", exhaust_headroom, True),
    ("global_fence", fence_globally, True),
    ("schema_unverified", unverified, False),
]


@pytest.mark.parametrize(("reason", "cause", "fake_llm"), PAUSES)
@pytest.mark.parametrize("trigger", TRIGGERS)
async def test_every_pause_reason_blocks_the_start(
    engine: AsyncEngine,
    current_epoch: int,
    now: datetime,
    reason: str,
    cause: Any,
    fake_llm: bool,
    trigger: CycleTrigger,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    await insert_targets(engine, 1, lane=None if trigger in ("periodic", "manual") else trigger)
    await set_bucket(engine, "1.000000", now)
    assert await pause(engine, now, fake_llm=True) is None  # nothing pauses the lane yet
    await cause(engine, now)
    assert await pause(engine, now, fake_llm=fake_llm) == reason
    assert await start(engine, trigger, now, fake_llm=fake_llm) is None
    assert await count_rows(engine, "score_cycles") == 0
    assert [e["reason"] for e in logged(caplog, "score_cycle_not_started")][-1] == f"paused:{reason}"


@pytest.mark.parametrize(
    ("code", "reason"),
    [("llm.auth_failed", "auth_failed"), ("llm.reservation_bound_violated", "reservation_bound_violated")],
)
async def test_acknowledgement_lifts_the_auth_and_bound_pauses(
    engine: AsyncEngine, current_epoch: int, now: datetime, code: str, reason: str
) -> None:
    # Codex r7 B5, Ruling P13: the route is Task 21's; here a direct UPDATE acknowledges the alert
    alert = await open_alert(engine, code)
    assert await pause(engine, now) == reason
    await execute(engine, "UPDATE alerts SET acknowledged_at = now() WHERE id = :a", a=alert)
    assert await pause(engine, now) is None
    async with engine.connect() as conn:
        resolved = await conn.execute(sa.text("SELECT resolved_at FROM alerts WHERE id = :a"), {"a": alert})
        assert resolved.scalar_one() is None  # the alert stays open
    await execute(engine, "UPDATE alerts SET acknowledged_at = NULL WHERE id = :a", a=alert)
    assert await pause(engine, now) == reason


async def test_the_cap_pause_reads_the_headroom_never_the_cap_alert(
    engine: AsyncEngine, current_epoch: int, now: datetime
) -> None:
    # Codex r5 B4: the ledger decides, so neither the alert's acknowledgement nor its resolution is read
    previous = (month_of(now) - timedelta(days=1)).replace(day=1)
    await open_alert(engine, "llm.cap_reached", previous.isoformat())  # a previous month's open alert
    current = await open_alert(engine, "llm.cap_reached", month_of(now).isoformat())
    assert await pause(engine, now) is None  # the month still has headroom
    await exhaust_headroom(engine, now)
    assert await pause(engine, now) == "cap_reached"
    await execute(engine, "UPDATE alerts SET acknowledged_at = now() WHERE id = :a", a=current)
    assert await pause(engine, now) == "cap_reached"
    await execute(engine, "UPDATE alerts SET resolved_at = now() WHERE code = 'llm.cap_reached'")
    assert await pause(engine, now) == "cap_reached"  # a second denial raises no alert, yet still pauses
    # headroom returns (a settlement): the pause lifts by itself
    await execute(engine, "UPDATE llm_budget SET counted_usd = 92.0, scoring_counted_usd = 92.0")
    assert await pause(engine, now) is None
    next_month = (month_of(now) + timedelta(days=32)).replace(day=1)
    assert await pause(engine, datetime.combine(next_month, now.timetz())) is None  # the rollover: a new row


# ---- ĉ -----------------------------------------------------------------------------------------------------


async def test_c_hat_uses_history_only_at_twenty_qualifying_attempts(
    engine: AsyncEngine, current_epoch: int, now: datetime
) -> None:
    (pending,) = await insert_targets(engine, 1)
    ended = now - timedelta(days=1)
    async with engine.begin() as conn:
        policy = int(
            (await conn.execute(sa.text("SELECT evaluation_policy_id FROM active_config"))).scalar_one()
        )
        number = 0

        async def attempt(**columns: object) -> None:
            nonlocal number
            number += 1
            values: dict[str, object] = {
                "attempt_no": number,
                "outcome": "published_accepted",
                "charged": True,
                "cost_usd": Decimal("0.100000"),
                "ended_at": ended,
                **columns,
            }
            await insert_attempt(conn, pending.target_id, **values)

        for i in range(19):
            await attempt(cost_usd=Decimal("0.100000") if i % 2 else Decimal("0.200000"))
        # not qualifying: a batch attempt, an uncharged one, another outcome, an old one
        await attempt(mode="batch", cost_usd=Decimal(9))
        await attempt(charged=False, cost_usd=Decimal(9))
        await attempt(outcome="refused", outcome_detail="unspecified", cost_usd=Decimal(9))
        await attempt(cost_usd=Decimal(9), ended_at=now - timedelta(days=8))

        async def c_hat() -> tuple[Decimal, str]:
            return await cycles.c_hat(
                conn,
                llm_tuple_id=pending.llm_tuple_id,
                evaluation_policy_id=policy,
                model_policy=ModelPolicy(),
                now=now,
            )

        assert await c_hat() == (CENTRAL, "central")  # 19 qualifying attempts
        await attempt(outcome="published_contested", cost_usd=Decimal("0.100000"))
        # 20: ten at $0.20, ten at $0.10
        assert await c_hat() == (Decimal("0.150000"), "history")
        other = await cycles.c_hat(
            conn,
            llm_tuple_id=pending.llm_tuple_id,
            evaluation_policy_id=policy + 1000,
            model_policy=ModelPolicy(),
            now=now,
        )
        assert other == (CENTRAL, "central")  # another evaluation policy has no history


# ---- schema_verified (Ruling P11) -----------------------------------------------------------------------


def case(schema: str, model: str, *, passed: bool = True) -> dict[str, Any]:
    return {"name": f"{schema}/{model}", "schema": schema, "model": model, "passed": passed}


SONNET, OPUS, HAIKU = "claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"
POLICY_B_CASES = [
    case("generator", SONNET),
    case("critic", OPUS),
    case("profile", OPUS),
    case("queries", OPUS),
]


async def dry_run(
    engine: AsyncEngine, version: int, cases: list[dict[str, Any]], *, transport: str = "live"
) -> None:
    async with engine.begin() as conn:
        await insert_id(
            conn,
            "eval_runs",
            {
                "kind": "schema_dry_run",
                "route": "production",
                "prompt_schema_version_id": version,
                "status": "completed",
                "passed": all(c["passed"] for c in cases),
                "metrics": {"transport": transport, "cases": cases},
                "finished_at": utcnow(),
            },
        )


async def verified(engine: AsyncEngine, version: int, policy: int) -> bool:
    async with engine.connect() as conn:
        return await validate.schema_verified(conn, prompt_schema_version_id=version, model_policy_id=policy)


async def test_schema_verified_needs_a_live_pass_for_every_pair(
    engine: AsyncEngine, current_epoch: int, now: datetime
) -> None:
    async with engine.begin() as conn:
        active = (
            await conn.execute(sa.text("SELECT prompt_schema_version_id, model_policy_id FROM active_config"))
        ).one()
        other_version = int(
            (
                await conn.execute(
                    sa.text("SELECT id FROM prompt_schema_versions WHERE id <> :v ORDER BY id LIMIT 1"),
                    {"v": active.prompt_schema_version_id},
                )
            ).scalar_one()
        )
        payload = ModelPolicy().model_dump(mode="json")
        payload["preset"] = "custom"
        payload["nodes"]["revise"]["model"] = HAIKU
        haiku_revise = await insert_id(
            conn,
            "model_policies",
            {"policy_hash": hashlib.sha256(b"haiku-revise-test").digest(), "payload": payload},
        )
    version, policy = int(active.prompt_schema_version_id), int(active.model_policy_id)
    assert not await verified(engine, version, policy)
    assert await pause(engine, now, fake_llm=False) == "schema_unverified"
    assert await pause(engine, now, fake_llm=True) is None  # never in fake LLM mode
    await dry_run(engine, version, POLICY_B_CASES, transport="fake")  # a fake run never counts
    await dry_run(engine, other_version, POLICY_B_CASES)  # nor a run of another version
    await dry_run(engine, version, [*POLICY_B_CASES[:3], case("queries", OPUS, passed=False)])
    assert not await verified(engine, version, policy)  # the queries pair failed
    await dry_run(engine, version, [case("queries", OPUS)])  # any run of the version counts
    assert await verified(engine, version, policy)
    assert await pause(engine, now, fake_llm=False) is None
    # a policy that moves `revise` to a model with no passed live case is refused, until a run passes it
    assert not await verified(engine, version, haiku_revise)
    await dry_run(engine, version, [case("generator", HAIKU)])
    assert await verified(engine, version, haiku_revise)


# ---- the stalled-job rule (Codex r1 #17) -----------------------------------------------------------------


async def test_a_lone_stalled_score_cycle_job_is_failed_never_retried(
    engine: AsyncEngine, queue_app: procrastinate.App, caplog: pytest.LogCaptureFixture
) -> None:
    async with engine.begin() as conn:
        assert await cycles.request_cycle(conn, trigger="periodic", defer=defer_in_tx)
        operation = await defer_in_tx(conn, "run_operation", operation_id=5)
        await conn.execute(sa.text("UPDATE procrastinate_jobs SET status = 'doing'"))  # no worker: stalled
    assert await release_stalled_jobs(queue_app) == 1  # the operation only
    async with engine.connect() as conn:
        statuses = {
            row.task_name: row.status
            for row in await conn.execute(sa.text("SELECT task_name, status FROM procrastinate_jobs"))
        }
    assert statuses == {"jh:score_cycle": "failed", "jh:run_operation": "todo"}
    assert [m["task_name"] for m in logged(caplog, "stalled_job_failed")] == ["jh:score_cycle"]
    assert [m["job_id"] for m in logged(caplog, "stalled_job_released")] == [operation]


# ---- the tick task's score step (§3.4 step 6) --------------------------------------------------------------


async def test_the_tick_task_requests_a_periodic_cycle_after_its_tick(
    engine: AsyncEngine, current_epoch: int, queue_app: procrastinate.App, monkeypatch: pytest.MonkeyPatch
) -> None:

    await insert_targets(engine, 1)
    monkeypatch.setattr(reconcilers, "REGISTRY", {})  # the tick's own steps only
    runtime = SimpleNamespace(
        engine=engine,
        queue=queue_app,
        epoch=current_epoch,
        embedder=None,
        shutdown=ShutdownState(),
        cfg=SimpleNamespace(fake_llm=True),
    )
    await tasks.tick(cast(JobContext, SimpleNamespace(additional_context={"runtime": runtime})), timestamp=0)
    async with engine.connect() as conn:
        jobs = [tuple(row) for row in await conn.execute(JOBS, {"t": job_task_name("score_cycle")})]
    assert jobs == [
        ("score", "jh:score_cycle", "score", "score_cycle:periodic", {"trigger": "periodic"}, "todo")
    ]
    assert not runtime.shutdown.requested.is_set()
