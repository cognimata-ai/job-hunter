import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
import sqlalchemy as sa
from jobhunter.observability.metrics import REGISTRY
from jobhunter.worker import reconcilers, tasks
from jobhunter.worker import tick as tick_module
from jobhunter.worker.runtime import ShutdownState
from jobhunter.worker.tasks import defer_in_tx, live_job_exists
from jobhunter.worker.tick import insert_active_collect, run_tick
from procrastinate.job_context import JobContext
from sqlalchemy.exc import IntegrityError

from tests.support.logs import logged

pytestmark = pytest.mark.integration
A = datetime(2026, 10, 16, 4, tzinfo=UTC)


async def schedule(engine, *, interval: int, cursor: datetime | None, enabled: bool = True) -> None:
    async with engine.begin() as c:
        await c.execute(
            sa.text(
                "UPDATE schedule_state SET anchor_utc=:a, interval_s=:i, last_evaluated_slot=:c, enabled=:e"
            ),
            {"a": A, "i": interval, "c": cursor, "e": enabled},
        )


async def stored_cursor(engine) -> datetime | None:
    async with engine.connect() as c:
        cursor: datetime | None = (
            await c.execute(sa.text("SELECT last_evaluated_slot FROM schedule_state"))
        ).scalar_one()
        return cursor


async def runs(engine) -> list[tuple[datetime, str, str]]:
    async with engine.connect() as c:
        return [
            tuple(r)
            for r in await c.execute(
                sa.text("SELECT scheduled_slot, status, trigger FROM runs ORDER BY scheduled_slot, id")
            )
        ]


async def tick(engine, now: datetime):
    async with engine.begin() as c:
        return await run_tick(c, now=now, defer=defer_in_tx)


async def test_due_slot_queues_one_scheduled_run(engine, queue_app) -> None:
    await schedule(engine, interval=21600, cursor=A)
    rep = await tick(engine, A + timedelta(hours=6, minutes=1))
    assert rep.missed == 0 and await runs(engine) == [(A + timedelta(hours=6), "queued", "schedule")]
    async with engine.connect() as c:
        assert await live_job_exists(
            c, "run_collect", args_match={"run_id": rep.queued_run_id, "attempt_no": 1}
        )
        assert (
            await c.execute(
                sa.text(
                    "SELECT r.settings_version_id = a.settings_version_id "
                    "AND r.scope_version_id = a.scope_version_id FROM runs r, active_config a"
                )
            )
        ).scalar_one() is True
    assert (await tick(engine, A + timedelta(hours=6, minutes=2))).queued_run_id is None
    assert len(await runs(engine)) == 1


async def test_overlap_records_skipped_overlap(engine, queue_app) -> None:
    await schedule(engine, interval=21600, cursor=A)
    await tick(engine, A + timedelta(hours=6))
    rep = await tick(engine, A + timedelta(hours=12))
    assert rep.skipped_overlap and await runs(engine) == [
        (A + timedelta(hours=6), "queued", "schedule"),
        (A + timedelta(hours=12), "skipped_overlap", "schedule"),
    ]


_RUN_VERSIONS = sa.text(
    "SELECT r.status, r.trigger, r.settings_version_id = a.settings_version_id "
    "AND r.scope_version_id = a.scope_version_id AS same_versions, r.llm_tuple_id "
    "FROM runs r, active_config a ORDER BY r.id"
)


async def run_versions(engine) -> list[tuple[object, ...]]:
    async with engine.connect() as c:
        return [tuple(r) for r in await c.execute(_RUN_VERSIONS)]


async def test_every_run_a_tick_records_carries_the_active_llm_tuple(engine, queue_app) -> None:
    """Task 23: a run records the LLM tuple active at its insert (node 9 reads the run's column), with the
    settings and scope versions of the same `active_config` read: the missed, the queued and the
    skipped-overlap rows alike. Before any profile is active, the tuple is NULL."""
    from tests.support.settings import activate_profile

    await schedule(engine, interval=21600, cursor=A)
    await tick(engine, A + timedelta(hours=6))
    assert await run_versions(engine) == [("queued", "schedule", True, None)]
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE runs SET status = 'cancelled', finished_at = now()"))
    _, tuple_id = await activate_profile(engine)
    await tick(engine, A + timedelta(hours=18, minutes=1))  # one slot missed, the latest queued
    await tick(engine, A + timedelta(hours=24))  # overlaps the queued run
    assert await run_versions(engine) == [
        ("cancelled", "schedule", True, None),
        ("missed", "schedule", True, tuple_id),
        ("queued", "schedule", True, tuple_id),
        ("skipped_overlap", "schedule", True, tuple_id),
    ]


async def test_a_manual_run_carries_the_active_llm_tuple(engine, queue_app) -> None:
    """Task 23: `request_manual_run` inserts through `insert_active_collect` with the tuple of its own
    `active_config` read."""
    from jobhunter.worker import runs as runs_module

    from tests.support.settings import activate_profile

    _, tuple_id = await activate_profile(engine)
    async with engine.begin() as c:
        row, created = await runs_module.request_manual_run(c, include_paid=False, defer=defer_in_tx)
    assert created
    assert await run_versions(engine) == [("queued", "manual", True, tuple_id)]
    assert row.id == (await runs_ids(engine))[0]


async def runs_ids(engine) -> list[int]:
    async with engine.connect() as c:
        return [int(i) for i in (await c.execute(sa.text("SELECT id FROM runs ORDER BY id"))).scalars()]


async def test_downtime_records_missed_and_catch_up(engine, queue_app) -> None:
    await schedule(engine, interval=21600, cursor=A)
    assert (await tick(engine, A + timedelta(hours=18, minutes=30))).missed == 2
    assert await runs(engine) == [
        (A + timedelta(hours=6), "missed", "schedule"),
        (A + timedelta(hours=12), "missed", "schedule"),
        (A + timedelta(hours=18), "queued", "catch_up"),
    ]


async def test_long_downtime_records_every_slot_once(engine, queue_app) -> None:  # Review Focus 2
    await schedule(engine, interval=3600, cursor=A)
    rep = await tick(engine, A + timedelta(days=9, minutes=30))
    expected = [(A + timedelta(hours=h), "missed", "schedule") for h in range(1, 216)] + [
        (A + timedelta(hours=216), "queued", "catch_up")
    ]
    assert rep.missed == 215 and await runs(engine) == expected
    assert (await tick(engine, A + timedelta(days=9, minutes=31))).missed == 0
    assert await runs(engine) == expected


async def test_tick_with_clock_behind_cursor_is_noop(engine, queue_app) -> None:  # Review Focus 5
    await schedule(engine, interval=21600, cursor=A + timedelta(hours=12))
    rep = await tick(engine, A + timedelta(hours=1))
    assert rep.cursor == A + timedelta(hours=12) and await runs(engine) == []
    assert (await tick(engine, A + timedelta(hours=18))).queued_run_id is not None


async def test_disabled_schedule_moves_cursor_without_rows(engine, queue_app) -> None:
    await schedule(engine, interval=21600, cursor=A, enabled=False)
    rep = await tick(engine, A + timedelta(days=2))
    assert await runs(engine) == [] and rep.cursor == A + timedelta(days=2)


async def test_null_cursor_queues_only_the_latest_slot_and_records_no_missed(engine, queue_app) -> None:
    await schedule(engine, interval=21600, cursor=None)
    now = A + timedelta(hours=18, minutes=1)  # three slots after the anchor, none of them evaluated before
    rep = await tick(engine, now)
    latest = A + timedelta(hours=18)
    assert rep.missed == 0 and rep.cursor == latest and rep.queued_run_id is not None
    assert await runs(engine) == [(latest, "queued", "schedule")]
    assert await stored_cursor(engine) == latest
    assert (await tick(engine, now + timedelta(minutes=1))).queued_run_id is None


async def test_null_cursor_with_disabled_schedule_moves_the_cursor_to_the_latest_slot(
    engine, queue_app
) -> None:
    await schedule(engine, interval=21600, cursor=None, enabled=False)
    rep = await tick(engine, A + timedelta(days=2, hours=1))
    latest = A + timedelta(days=2)
    assert await runs(engine) == [] and rep.cursor == latest
    assert await stored_cursor(engine) == latest


async def test_missed_slots_are_logged_as_a_warning_with_their_count_and_range(
    engine, queue_app, caplog
) -> None:
    await schedule(engine, interval=21600, cursor=A)
    await tick(engine, A + timedelta(hours=6, minutes=1))
    assert logged(caplog, "slots_missed") == []  # a tick on time misses nothing
    await tick(engine, A + timedelta(hours=36, minutes=1))  # 12h..30h are missed; 36h overlaps the 6h run
    [event] = logged(caplog, "slots_missed")
    assert event["count"] == 4
    assert event["first_slot"] == (A + timedelta(hours=12)).isoformat()
    assert event["last_slot"] == (A + timedelta(hours=30)).isoformat()
    [record] = [r for r in caplog.records if isinstance(r.msg, dict) and r.msg.get("event") == "slots_missed"]
    assert record.levelno == logging.WARNING


async def wait_until_a_session_waits_for_a_lock(engine) -> None:
    """Return once another session is blocked on a lock, observed in `pg_stat_activity`.

    Each poll is a transaction of its own: a transaction keeps the first `pg_stat_activity` snapshot it reads.
    The timeout only turns a wait that never ends into a failure.
    """
    async with asyncio.timeout(30):
        while True:
            async with engine.connect() as c:
                waiting = await c.execute(
                    sa.text(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock')"
                    )
                )
                if waiting.scalar_one():
                    return
            await asyncio.sleep(0.02)


async def test_concurrent_ticks_queue_the_slot_once(engine, queue_app) -> None:
    await schedule(engine, interval=21600, cursor=A)
    now = A + timedelta(hours=6)
    async with engine.connect() as first:
        tx = await first.begin()
        rep1 = await run_tick(first, now=now, defer=defer_in_tx)
        second = asyncio.create_task(tick(engine, now))
        try:
            await wait_until_a_session_waits_for_a_lock(engine)
            assert not second.done()  # it waits for the cursor row that the first tick holds
        finally:  # also when the assertion fails: the second tick must not wait for ever
            await tx.commit()
            await asyncio.wait([second])
    rep2 = second.result()
    assert rep1.queued_run_id is not None and rep2.queued_run_id is None and rep2.missed == 0
    assert await runs(engine) == [(now, "queued", "schedule")]


async def test_tick_locks_active_config_before_schedule_state(engine, queue_app) -> None:
    await schedule(engine, interval=21600, cursor=A)
    async with engine.connect() as holder:
        tx = await holder.begin()
        await holder.execute(sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE"))
        blocked = asyncio.create_task(tick(engine, A + timedelta(hours=6)))
        try:
            await wait_until_a_session_waits_for_a_lock(engine)
            async with engine.begin() as probe:  # fails with 55P03 if the waiting tick holds the cursor row
                await probe.execute(sa.text("SELECT 1 FROM schedule_state WHERE id = 1 FOR UPDATE NOWAIT"))
        finally:
            await tx.commit()
            await asyncio.wait([blocked])
    assert blocked.result().queued_run_id is not None


async def test_insert_active_collect_classifies_each_conflict(engine, queue_app, caplog) -> None:
    caplog.set_level(logging.INFO)
    async with engine.begin() as c:
        settings_id, scope_id = (
            await c.execute(sa.text("SELECT settings_version_id, scope_version_id FROM active_config"))
        ).one()
        ids = {
            "include_paid": True,
            "settings_version_id": settings_id,
            "scope_version_id": scope_id,
            "llm_tuple_id": None,
        }
        await c.execute(
            sa.text(
                "INSERT INTO runs (trigger, scheduled_slot, status, include_paid, settings_version_id, "
                "scope_version_id) VALUES ('schedule', :slot, 'missed', true, :s, :c)"
            ),
            {"slot": A, "s": settings_id, "c": scope_id},
        )
        taken = await insert_active_collect(c, trigger="catch_up", scheduled_slot=A, path="tick", **ids)
        assert taken == "slot_conflict"
        manual = await insert_active_collect(c, trigger="manual", scheduled_slot=None, path="manual", **ids)
        assert isinstance(manual, int)
        assert logged(caplog, "active_collect_conflict") == []
        other = A + timedelta(hours=6)
        overlapping = await insert_active_collect(
            c, trigger="schedule", scheduled_slot=other, path="tick", **ids
        )
        assert overlapping == "active_conflict"
        [event] = logged(caplog, "active_collect_conflict")
        assert event["path"] == "tick" and event["sqlstate"] == "23505"
        # A manual run has no slot (CHECK): neither unique index is involved, so the error re-raises.
        with pytest.raises(IntegrityError):
            await insert_active_collect(c, trigger="manual", scheduled_slot=other, path="manual", **ids)
        # every conflict rolled back its savepoint only
        assert (await c.execute(sa.text("SELECT count(*) FROM runs"))).scalar_one() == 2


async def test_skipped_overlap_for_a_slot_that_was_taken_meanwhile_is_a_noop(
    engine, queue_app, monkeypatch
) -> None:
    await schedule(engine, interval=21600, cursor=A)
    slot = A + timedelta(hours=6)

    async def lose_both_races(conn, **kwargs):
        # Another writer recorded the slot between our attempt and our skipped_overlap insert.
        await conn.execute(
            sa.text(
                "INSERT INTO runs (trigger, scheduled_slot, status, include_paid, settings_version_id, "
                "scope_version_id) SELECT 'schedule', :slot, 'skipped_overlap', true, settings_version_id, "
                "scope_version_id FROM active_config"
            ),
            {"slot": slot},
        )
        return "active_conflict"

    monkeypatch.setattr(tick_module, "insert_active_collect", lose_both_races)
    rep = await tick(engine, slot)
    assert not rep.skipped_overlap and rep.queued_run_id is None and rep.cursor == slot
    assert await runs(engine) == [(slot, "skipped_overlap", "schedule")]


async def test_scheduled_run_takes_include_paid_from_the_active_settings(engine, queue_app) -> None:
    await schedule(engine, interval=21600, cursor=A)
    set_flag = sa.text(
        "UPDATE settings_versions "
        "SET payload = jsonb_set(payload, '{schedule,include_paid}', CAST(:v AS jsonb)) "
        "WHERE id = (SELECT settings_version_id FROM active_config)"
    )
    async with engine.begin() as c:  # the settings row is shared by the whole session: put the flag back
        original = (
            await c.execute(
                sa.text(
                    "SELECT payload #>> '{schedule,include_paid}' FROM settings_versions "
                    "WHERE id = (SELECT settings_version_id FROM active_config)"
                )
            )
        ).scalar_one()
        await c.execute(set_flag, {"v": "false"})
    try:
        await tick(engine, A + timedelta(hours=12))
    finally:
        async with engine.begin() as c:
            await c.execute(set_flag, {"v": original})
    async with engine.connect() as c:
        paid = (
            (await c.execute(sa.text("SELECT include_paid FROM runs ORDER BY scheduled_slot")))
            .scalars()
            .all()
        )
    assert paid == [False, False]


async def test_tick_task_runs_the_tick_reconcilers_once_and_then_the_tick(
    engine, current_epoch, queue_app, monkeypatch
) -> None:
    await schedule(engine, interval=21600, cursor=A)
    now = A + timedelta(hours=6)
    released_by: list[str] = []
    seen: list[tuple[reconcilers.ReconcileContext, list[tuple[datetime, str, str]]]] = []

    async def release_in_reconciler(app, **kwargs) -> int:
        released_by.append("reconciler")
        assert app is queue_app
        return 0

    async def release_in_task(app, **kwargs) -> int:
        released_by.append("task")
        return 0

    async def probe(ctx: reconcilers.ReconcileContext) -> int:
        # An assertion here would be swallowed by `run_reconcilers`, so the test records and asserts after.
        seen.append((ctx, await runs(engine)))
        return 0

    monkeypatch.setattr(reconcilers, "REGISTRY", dict(reconcilers.REGISTRY))
    reconcilers.reconciler("probe", "tick")(probe)
    monkeypatch.setattr(reconcilers, "release_stalled_jobs", release_in_reconciler)
    monkeypatch.setattr(tasks, "release_stalled_jobs", release_in_task)
    monkeypatch.setattr(tasks, "utcnow", lambda: now)
    # the fields it reads; its epoch is the current one (the reconcilers and the tick are fenced by it)
    runtime = SimpleNamespace(
        engine=engine, queue=queue_app, epoch=current_epoch, embedder=None, shutdown=ShutdownState()
    )
    context = cast(JobContext, SimpleNamespace(additional_context={"runtime": runtime}))
    await tasks.tick(context, timestamp=0)
    assert released_by == ["reconciler"]  # the `stalled_jobs` reconciler releases; the body does not
    [(ctx, runs_during_reconcile)] = seen
    assert runs_during_reconcile == []  # the reconcilers ran before the tick recorded anything
    assert ctx.defer is defer_in_tx and ctx.epoch == runtime.epoch
    assert ctx.engine is engine and ctx.queue is queue_app and ctx.now == now
    assert await runs(engine) == [(now, "queued", "schedule")]


async def test_the_retention_daily_task_runs_the_daily_reconcilers_after_retention(
    engine, queue_app, monkeypatch
) -> None:  # Task 19 fix round 1, M-1: the `daily` cadence is dispatched
    from jobhunter.privacy import retention

    now = A + timedelta(days=1)
    order: list[str] = []
    seen: list[reconcilers.ReconcileContext] = []

    async def retention_pass(engine_, queue, saver, *, now: datetime, epoch: int) -> None:
        order.append(f"retention@{epoch}")

    async def probe(ctx: reconcilers.ReconcileContext) -> int:
        order.append("daily")
        seen.append(ctx)
        return 0

    monkeypatch.setattr(reconcilers, "REGISTRY", {})  # only the probe
    reconcilers.reconciler("probe", "daily")(probe)
    monkeypatch.setattr(retention, "run_retention_daily", retention_pass)
    monkeypatch.setattr(tasks, "utcnow", lambda: now)
    runtime = SimpleNamespace(
        engine=engine, queue=queue_app, saver=object(), epoch=3, embedder=None, shutdown=ShutdownState()
    )
    context = cast(JobContext, SimpleNamespace(additional_context={"runtime": runtime}))
    await tasks.retention_daily(context, timestamp=0)
    assert order == ["retention@3", "daily"]  # the pass is fenced by the runtime's epoch (final review C2)
    [ctx] = seen
    assert ctx.engine is engine and ctx.queue is queue_app and ctx.defer is defer_in_tx
    assert (ctx.now, ctx.epoch) == (now, runtime.epoch)


async def test_a_fenced_retention_pass_ends_the_daily_task_before_its_reconcilers(
    engine, queue_app, monkeypatch
) -> None:  # final review C2: a superseded worker's daily task writes nothing more after its fenced pass
    from jobhunter.errors import FencedOut
    from jobhunter.privacy import retention

    order: list[str] = []

    async def fenced_pass(engine_, queue, saver, *, now: datetime, epoch: int) -> None:
        order.append("retention")
        raise FencedOut(owner="worker_epoch")

    async def probe(ctx: reconcilers.ReconcileContext) -> int:
        order.append("daily")
        return 0

    monkeypatch.setattr(reconcilers, "REGISTRY", {})  # only the probe
    reconcilers.reconciler("probe", "daily")(probe)
    monkeypatch.setattr(retention, "run_retention_daily", fenced_pass)
    runtime = SimpleNamespace(
        engine=engine, queue=queue_app, saver=object(), epoch=3, embedder=None, shutdown=ShutdownState()
    )
    context = cast(JobContext, SimpleNamespace(additional_context={"runtime": runtime}))
    await tasks.retention_daily(context, timestamp=0)  # returns: the job ends quietly
    assert order == ["retention"]
    assert runtime.shutdown.requested.is_set()  # the superseded worker stops itself (Ruling T24-a)


async def test_a_fence_of_another_owner_fails_the_retention_pass_and_never_stops_the_worker(
    engine, current_epoch, queue_app, monkeypatch, caplog
) -> None:  # fix wave F6 (F5 re-review, nit 2): F5 Minor 1's branch of the daily task
    from jobhunter.errors import FencedOut
    from jobhunter.privacy import retention

    from tests.support.logs import logged

    order: list[str] = []

    async def fenced_pass(engine_, queue, saver, *, now: datetime, epoch: int) -> None:
        order.append("retention")
        raise FencedOut(owner="purge_fence")

    async def probe(ctx: reconcilers.ReconcileContext) -> int:
        order.append("daily")
        return 0

    monkeypatch.setattr(reconcilers, "REGISTRY", {})  # only the probe
    reconcilers.reconciler("probe", "daily")(probe)
    monkeypatch.setattr(retention, "run_retention_daily", fenced_pass)
    runtime = SimpleNamespace(
        engine=engine,
        queue=queue_app,
        saver=object(),
        epoch=current_epoch,  # the daily cadence's own fence admits it
        embedder=None,
        shutdown=ShutdownState(),
    )
    context = cast(JobContext, SimpleNamespace(additional_context={"runtime": runtime}))
    with pytest.raises(FencedOut) as fenced:  # the job fails, after the daily cadence ran
        await tasks.retention_daily(context, timestamp=0)
    assert fenced.value.owner == "purge_fence" and order == ["retention", "daily"]
    [event] = logged(caplog, "retention_daily_failed")
    assert (event["error_class"], event["owner"]) == ("FencedOut", "purge_fence")
    assert not runtime.shutdown.requested.is_set()


async def test_a_fence_of_another_owner_fails_the_tick_and_never_stops_the_worker(
    engine, current_epoch, queue_app, monkeypatch
) -> None:  # F5 review, Minor 1: only the worker's own fence is proof of a takeover
    from jobhunter.errors import FencedOut

    async def fenced_tick(conn, *, now: datetime, defer) -> None:
        raise FencedOut(owner="run")

    monkeypatch.setattr(reconcilers, "REGISTRY", {})  # only the tick itself
    monkeypatch.setattr(tick_module, "run_tick", fenced_tick)
    runtime = SimpleNamespace(
        engine=engine, queue=queue_app, epoch=current_epoch, embedder=None, shutdown=ShutdownState()
    )
    with pytest.raises(FencedOut):
        await tasks.tick(
            cast(JobContext, SimpleNamespace(additional_context={"runtime": runtime})), timestamp=0
        )
    assert not runtime.shutdown.requested.is_set()


async def test_a_retention_pass_that_raises_still_runs_the_daily_reconcilers(
    engine, current_epoch, queue_app, monkeypatch, caplog
) -> None:  # ledger L312: the daily cadence is isolated from the pass; the job still fails after it
    from jobhunter.privacy import retention

    from tests.support.logs import logged

    order: list[str] = []

    async def failing_pass(engine_, queue, saver, *, now: datetime, epoch: int) -> None:
        order.append("retention")
        raise RuntimeError("a detail that must not be logged")

    async def probe(ctx: reconcilers.ReconcileContext) -> int:
        order.append("daily")
        return 0

    monkeypatch.setattr(reconcilers, "REGISTRY", {})  # only the probe
    reconcilers.reconciler("probe", "daily")(probe)
    monkeypatch.setattr(retention, "run_retention_daily", failing_pass)
    runtime = SimpleNamespace(
        engine=engine,
        queue=queue_app,
        saver=object(),
        epoch=current_epoch,
        embedder=None,
        shutdown=ShutdownState(),
    )
    context = cast(JobContext, SimpleNamespace(additional_context={"runtime": runtime}))
    with pytest.raises(RuntimeError):
        await tasks.retention_daily(context, timestamp=0)
    assert order == ["retention", "daily"]
    assert [e["error_class"] for e in logged(caplog, "retention_daily_failed")] == ["RuntimeError"]
    assert "must not be logged" not in caplog.text and not runtime.shutdown.requested.is_set()


async def test_the_reconcile_heavy_task_hands_the_runtimes_embedder_to_its_reconcilers(
    engine, queue_app, monkeypatch
) -> None:  # Task 22 fix round 1, M-2: `awaiting_embedding` gets the process's client
    now = A + timedelta(minutes=10)
    seen: list[reconcilers.ReconcileContext] = []

    async def probe(ctx: reconcilers.ReconcileContext) -> int:
        seen.append(ctx)
        return 0

    monkeypatch.setattr(reconcilers, "REGISTRY", {})  # only the probe
    reconcilers.reconciler("probe", "heavy")(probe)
    monkeypatch.setattr(tasks, "utcnow", lambda: now)
    embedder = object()  # the runtime's client, whatever it is: handed on as is
    runtime = SimpleNamespace(
        engine=engine, queue=queue_app, epoch=3, embedder=embedder, shutdown=ShutdownState()
    )
    context = cast(JobContext, SimpleNamespace(additional_context={"runtime": runtime}))
    await tasks.reconcile_heavy(context, timestamp=0)
    [ctx] = seen
    assert ctx.embedder is embedder
    assert ctx.engine is engine and ctx.queue is queue_app and ctx.defer is defer_in_tx
    assert (ctx.now, ctx.epoch) == (now, runtime.epoch)


# --- Final fix wave, item 4: the terminal runs a tick inserts are counted in RUNS ---------------------------


def runs_total(status: str) -> float:
    """`jobhunter_runs_total{status}` in this process (0 before the first count)."""
    return REGISTRY.get_sample_value("jobhunter_runs_total", {"status": status}) or 0.0


async def test_the_tick_task_counts_the_missed_and_overlapping_runs_it_inserted(
    engine, current_epoch, queue_app, monkeypatch
) -> None:
    await schedule(engine, interval=3600, cursor=A)
    monkeypatch.setattr(reconcilers, "REGISTRY", {})  # only the tick itself
    runtime = SimpleNamespace(  # the fields it reads
        engine=engine, queue=queue_app, epoch=1, embedder=None, shutdown=ShutdownState()
    )
    context = cast(JobContext, SimpleNamespace(additional_context={"runtime": runtime}))

    async def tick_task_at(now: datetime) -> None:
        monkeypatch.setattr(tasks, "utcnow", lambda: now)
        await tasks.tick(context, timestamp=0)

    def counts() -> tuple[float, float]:
        return runs_total("missed"), runs_total("skipped_overlap")

    missed, overlapping = counts()
    await tick_task_at(A + timedelta(hours=3))  # slots +1 h and +2 h missed, +3 h queued
    assert counts() == (missed + 2, overlapping)
    await tick_task_at(A + timedelta(hours=4))  # the +3 h run is still active: +4 h overlaps it
    assert counts() == (missed + 2, overlapping + 1)
    await tick_task_at(A + timedelta(hours=4, minutes=1))  # no new slot, no new row: nothing more counted
    assert counts() == (missed + 2, overlapping + 1)
    assert [status for _, status, _ in await runs(engine)] == [
        "missed",
        "missed",
        "queued",
        "skipped_overlap",
    ]


# --- Final fix wave, item 8: one deferral of a run attempt (ruling P16) -------------------------------------


async def test_the_tick_and_the_runs_defer_a_run_attempt_through_one_function(engine, queue_app) -> None:
    from jobhunter.worker import runs as runs_module

    # Neither module keeps a copy: both call the one in `tasks`.
    assert vars(tick_module)["defer_run_attempt"] is tasks.defer_run_attempt
    assert vars(runs_module)["defer_run_attempt"] is tasks.defer_run_attempt
    await schedule(engine, interval=21600, cursor=A)
    rep = await tick(engine, A + timedelta(hours=6))
    async with engine.connect() as c:
        job = (
            await c.execute(sa.text("SELECT task_name, lock, queueing_lock, args FROM procrastinate_jobs"))
        ).one()
    run_id = rep.queued_run_id
    assert tuple(job) == ("jh:run_collect", "collect", f"run:{run_id}:1", {"run_id": run_id, "attempt_no": 1})


async def test_a_superseded_workers_tick_inserts_no_run_and_moves_no_cursor(
    engine, current_epoch, queue_app, monkeypatch, caplog
) -> None:  # Ruling FR-h: the tick's own scheduling step is fenced by the worker epoch
    from tests.support.logs import logged

    await schedule(engine, interval=3600, cursor=A)
    monkeypatch.setattr(reconcilers, "REGISTRY", {})  # only the tick itself
    monkeypatch.setattr(tasks, "utcnow", lambda: A + timedelta(hours=3))  # two slots missed, one due
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE worker_heartbeat SET worker_epoch = worker_epoch + 1"))  # a takeover
    stale = SimpleNamespace(
        engine=engine, queue=queue_app, epoch=current_epoch, embedder=None, shutdown=ShutdownState()
    )
    await tasks.tick(cast(JobContext, SimpleNamespace(additional_context={"runtime": stale})), timestamp=0)
    assert await runs(engine) == [] and await stored_cursor(engine) == A
    assert [e["current_epoch"] for e in logged(caplog, "worker_epoch_fenced")] == [current_epoch + 1]
    # final wave F5 (F2 re-review, observation 2): the superseded worker requests its graceful stop, once
    assert stale.shutdown.requested.is_set()
    [stop] = logged(caplog, "shutdown_requested")
    assert (stop["level"], stop["reason"]) == ("warning", "epoch_superseded")
    current = SimpleNamespace(
        engine=engine, queue=queue_app, epoch=current_epoch + 1, embedder=None, shutdown=ShutdownState()
    )
    await tasks.tick(cast(JobContext, SimpleNamespace(additional_context={"runtime": current})), timestamp=0)
    assert len(await runs(engine)) == 3 and await stored_cursor(engine) == A + timedelta(hours=3)
    assert not current.shutdown.requested.is_set()
