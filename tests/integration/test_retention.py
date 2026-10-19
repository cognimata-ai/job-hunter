from datetime import timedelta

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.graphs.checkpointer import open_saver
from jobhunter.graphs.collect.graph import build_noop_collect_graph
from jobhunter.graphs.collect.state import initial_state
from jobhunter.privacy.retention import RULES, run_retention_daily
from jobhunter.worker.tasks import defer_in_tx
from procrastinate.jobs import Status

pytestmark = pytest.mark.integration


async def test_m0_rules_delete_only_expired_rows(engine, queue_app, migrated) -> None:
    now = utcnow()
    async with engine.begin() as c:
        for ago in (31, 29):
            await c.execute(
                sa.text(
                    "INSERT INTO login_attempts (at, ip, username_hmac, success) "
                    "VALUES (:t, '198.51.100.1', '\\x00', false)"
                ),
                {"t": now - timedelta(days=ago)},
            )
        await c.execute(
            sa.text(
                "INSERT INTO alerts (code,severity,dedup_key,message,resolved_at) "
                "VALUES ('a','info','k1','m',:r), ('a','info','k2','m',:s)"
            ),
            {"r": now - timedelta(days=91), "s": now - timedelta(days=89)},
        )
        await c.execute(
            sa.text(
                "INSERT INTO api_idempotency (route,idem_key,request_sha256,status,expires_at) "
                "VALUES ('r','aaaaaaaaaaaaaaaa','\\x00','in_progress',:e), "
                "('r','bbbbbbbbbbbbbbbb','\\x00','in_progress',:f)"
            ),
            {"e": now - timedelta(seconds=1), "f": now + timedelta(days=1)},
        )
    async with open_saver(migrated.dsn("app")) as saver:
        report = await run_retention_daily(engine, queue_app, saver, now=now)
    assert (report["login_attempts"], report["alerts"], report["api_idempotency"]) == (1, 1, 1)
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM login_attempts"))).scalar_one() == 1
        assert (await c.execute(sa.text("SELECT dedup_key FROM alerts"))).scalar_one() == "k2"


# --- Additional coverage of the windows the brief lists in prose (§20.1; rulings P14, T13-e) ---------------

# Far from the database clock: a window measured from the database's `now()` instead of the caller's `now`
# would delete nothing below, or everything.
SHIFT = timedelta(days=1000)

_RUN = (
    "INSERT INTO runs (trigger, scheduled_slot, status, include_paid, settings_version_id, scope_version_id, "
    "attempt_no, queued_at, started_at, finished_at) "
    "SELECT :trigger, :slot, :status, false, settings_version_id, scope_version_id, :attempt_no, :queued, "
    ":started, :finished FROM active_config RETURNING id"
)
_ATTEMPT = (
    "INSERT INTO run_attempts (run_id, attempt_no, worker_epoch, thread_id) "
    "VALUES (:i, :a, 1, 'run-' || :i || '-a' || :a)"
)


async def insert_run(c, status: str, *, queued, finished=None, attempts: int = 0, slot=None) -> int:
    trigger = "manual" if slot is None else "schedule"
    started = None if attempts == 0 else queued
    params = {
        "trigger": trigger,
        "slot": slot,
        "status": status,
        "attempt_no": attempts,
        "queued": queued,
        "started": started,
        "finished": finished,
    }
    run_id = int((await c.execute(sa.text(_RUN), params)).scalar_one())
    for attempt_no in range(1, attempts + 1):
        await c.execute(sa.text(_ATTEMPT), {"i": run_id, "a": attempt_no})
    return run_id


async def retention_at(engine, queue_app, migrated, now) -> dict[str, int]:
    async with open_saver(migrated.dsn("app")) as saver:
        return await run_retention_daily(engine, queue_app, saver, now=now)


def test_rules_name_every_report_key_with_its_window() -> None:
    assert {rule.name: rule.window for rule in RULES} == {
        "api_idempotency": timedelta(0),
        "sessions": timedelta(days=1),
        "login_attempts": timedelta(days=30),
        "alerts": timedelta(days=90),
        "operations": timedelta(days=30),
        "checkpoints": timedelta(days=14),
        "runs": timedelta(days=397),  # 13 months, never fewer days than any 13 calendar months
        "procrastinate_jobs": timedelta(days=7),
    }
    names = [rule.name for rule in RULES]
    assert names.index("checkpoints") < names.index("runs")  # threads go while their attempts still name them
    # A dependency is named explicitly (ruling T21-f), and only ever on a rule that runs before it.
    assert {rule.name: rule.depends_on for rule in RULES if rule.depends_on} == {"runs": ("checkpoints",)}
    for rule in RULES:
        assert all(names.index(name) < names.index(rule.name) for name in rule.depends_on), rule.name


async def test_sessions_and_operations_go_one_day_after_expiry_and_30_days_after_they_finish(
    engine, queue_app, migrated
) -> None:
    now = utcnow() + SHIFT
    async with engine.begin() as c:
        await c.execute(
            sa.text(
                "INSERT INTO users (id, username, password_hash, password_changed_at) "
                "VALUES (1, 'owner', 'x', now())"
            )
        )
        sessions = (
            ("\\x01", now - timedelta(days=3), now - timedelta(days=2)),  # absolute expiry 2 d ago
            ("\\x02", now - timedelta(hours=26), now + timedelta(days=5)),  # idle expiry 26 h ago
            ("\\x03", now - timedelta(hours=12), now + timedelta(days=5)),  # idle expiry 12 h ago: kept
        )
        for token, idle, absolute in sessions:
            await c.execute(
                sa.text(
                    "INSERT INTO sessions (token_hmac, user_id, idle_expires_at, absolute_expires_at) "
                    "VALUES (CAST(:t AS bytea), 1, :i, :a)"
                ),
                {"t": token, "i": idle, "a": absolute},
            )
        operations = (
            ("old-succeeded", "succeeded", None, now - timedelta(days=31)),
            ("old-cancelled", "cancelled", None, now - timedelta(days=40)),
            ("recent-failed", "failed", "bug", now - timedelta(days=29)),
            ("old-pending", "pending", None, None),  # never finished: kept, however old
        )
        for key, status, error, finished in operations:
            await c.execute(
                sa.text(
                    "INSERT INTO operations (kind, payload, dedup_key, status, last_error_code, created_at, "
                    "finished_at) VALUES ('export', '{}', :k, :s, :e, :created, :f)"
                ),
                {"k": key, "s": status, "e": error, "created": now - timedelta(days=60), "f": finished},
            )
    report = await retention_at(engine, queue_app, migrated, now)
    assert (report["sessions"], report["operations"]) == (2, 2)
    async with engine.connect() as c:
        kept = (await c.execute(sa.text("SELECT token_hmac FROM sessions"))).scalars().all()
        assert kept == [b"\x03"]
        left = (await c.execute(sa.text("SELECT dedup_key FROM operations ORDER BY dedup_key"))).scalars()
        assert list(left) == ["old-pending", "recent-failed"]


async def test_runs_go_with_their_attempts_after_13_months_and_a_live_run_never_goes(
    engine, queue_app, migrated
) -> None:
    now = utcnow() + SHIFT
    old, recent = now - timedelta(days=398), now - timedelta(days=396)
    async with engine.begin() as c:
        finished_old = await insert_run(c, "succeeded", queued=old, finished=old, attempts=2)
        finished_recent = await insert_run(c, "failed", queued=recent, finished=recent, attempts=1)
        missed_old = await insert_run(c, "missed", queued=old, slot=old)
        skipped_recent = await insert_run(c, "skipped_overlap", queued=recent, slot=recent)
        queued_old = await insert_run(c, "queued", queued=old - timedelta(days=30))  # live: never deleted
    report = await retention_at(engine, queue_app, migrated, now)
    assert report["runs"] == 2
    async with engine.connect() as c:
        left = (await c.execute(sa.text("SELECT id FROM runs ORDER BY id"))).scalars().all()
        assert left == [finished_recent, skipped_recent, queued_old]
        attempts = (await c.execute(sa.text("SELECT run_id FROM run_attempts"))).scalars().all()
        assert attempts == [finished_recent]  # the deleted run's two attempts went with it
    assert finished_old not in left and missed_old not in left


async def test_checkpoint_threads_of_runs_finished_over_14_days_ago_are_deleted(
    engine, queue_app, migrated
) -> None:
    now = utcnow() + SHIFT
    async with engine.begin() as c:
        ended = (now - timedelta(days=15), now - timedelta(days=13))
        old, recent = [await insert_run(c, "succeeded", queued=t, finished=t, attempts=1) for t in ended]
    threads = {run_id: f"run-{run_id}-a1" for run_id in (old, recent)}  # each run's one attempt thread
    async with open_saver(migrated.dsn("app")) as saver:
        graph = build_noop_collect_graph(saver)
        for run_id, thread in threads.items():  # real checkpoint threads, written by the collection graph
            await graph.ainvoke(
                initial_state(run_id, 1),
                {"configurable": {"thread_id": thread}},
                durability="sync",
            )
        first = await run_retention_daily(engine, queue_app, saver, now=now)
        second = await run_retention_daily(engine, queue_app, saver, now=now)
    assert (first["checkpoints"], second["checkpoints"]) == (1, 0)  # threads with rows left, so once
    async with engine.connect() as c:
        for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
            stored = await c.execute(sa.text(f"SELECT DISTINCT thread_id FROM {table}"))  # noqa: S608
            assert set(stored.scalars()) <= {threads[recent]}, table
        kept = await c.execute(
            sa.text("SELECT count(*) FROM checkpoints WHERE thread_id = :t"), {"t": threads[recent]}
        )
        assert kept.scalar_one() > 0
        assert (
            await c.execute(sa.text("SELECT count(*) FROM runs"))
        ).scalar_one() == 2  # runs stay 13 months


async def test_finished_procrastinate_jobs_go_after_7_days_and_report_minus_one(
    engine, queue_app, migrated
) -> None:
    async with engine.begin() as c:
        old = await defer_in_tx(c, "retention_daily", timestamp=1)
        recent = await defer_in_tx(c, "retention_daily", timestamp=2)
    manager = queue_app.job_manager
    worker_id = await manager.register_worker()
    try:
        for _ in range(2):
            job = await manager.fetch_job(queues=["tick"], worker_id=worker_id)
            assert job is not None
            await manager.finish_job(job, Status.SUCCEEDED, delete_job=False)
    finally:
        await manager.unregister_worker(worker_id)
    async with engine.begin() as c:
        # procrastinate measures a job's age by its latest event, on the database clock (its own API).
        await c.execute(
            sa.text("UPDATE procrastinate_events SET at = now() - interval '8 days' WHERE job_id = :j"),
            {"j": old},
        )
    report = await retention_at(engine, queue_app, migrated, utcnow())
    assert report["procrastinate_jobs"] == -1  # procrastinate reports no count
    async with engine.connect() as c:
        left = (await c.execute(sa.text("SELECT id FROM procrastinate_jobs"))).scalars().all()
    assert left == [recent]


# --- Ruling T13-g (a): each rule has its own error boundary, with a deduplicated alert --------------------


async def test_a_failing_rule_is_alerted_and_the_rules_after_it_still_run(
    engine, queue_app, migrated, monkeypatch, caplog
) -> None:
    from jobhunter.db.sql import q
    from jobhunter.privacy import retention

    from tests.support.logs import logged

    now = utcnow() + SHIFT
    async with engine.begin() as c:
        await c.execute(
            sa.text(
                "INSERT INTO login_attempts (at, ip, username_hmac, success) "
                "VALUES (:t, '198.51.100.1', '\\x00', false)"
            ),
            {"t": now - timedelta(days=31)},
        )
        await c.execute(
            sa.text(
                "INSERT INTO alerts (code,severity,dedup_key,message,resolved_at) "
                "VALUES ('a','info','k','m',:r)"
            ),
            {"r": now - timedelta(days=91)},
        )
    open_failure = sa.text(
        "SELECT severity, message, context FROM alerts WHERE code = 'retention.failed' "
        "AND dedup_key = 'retention.failed:login_attempts' AND resolved_at IS NULL"
    )
    # The third rule of the list fails on a missing table (no message of it may be stored or logged).
    monkeypatch.setitem(
        retention._DELETES, "login_attempts", q("DELETE FROM no_such_table WHERE at < :cutoff")
    )
    report = await retention_at(engine, queue_app, migrated, now)
    assert report["login_attempts"] == -1
    assert report["alerts"] == 1 and report["checkpoints"] == 0  # the rules after it ran
    async with engine.connect() as c:
        severity, message, context = (await c.execute(open_failure)).one()
        assert (await c.execute(sa.text("SELECT count(*) FROM login_attempts"))).scalar_one() == 1
    assert (severity, context) == ("warn", {"rule": "login_attempts", "error_class": "ProgrammingError"})
    assert "no_such_table" not in message and "no_such_table" not in caplog.text
    assert [(e["rule"], e["error_class"]) for e in logged(caplog, "retention_rule_failed")] == [
        ("login_attempts", "ProgrammingError")
    ]
    monkeypatch.undo()
    report = await retention_at(engine, queue_app, migrated, now)
    assert report["login_attempts"] == 1  # the rule works again, and its alert is resolved
    async with engine.connect() as c:
        assert (await c.execute(open_failure)).first() is None


# --- Ruling T21-f: a rule that depends on another is skipped in a pass where that one failed -------------


async def test_a_failed_checkpoint_rule_keeps_the_runs_that_name_its_threads(
    engine, queue_app, migrated, monkeypatch, caplog
) -> None:
    from tests.support.logs import logged

    now = utcnow() + SHIFT
    old = now - timedelta(days=398)  # past both windows: its threads (14 days) and the run itself (13 months)
    async with engine.begin() as c:
        run_id = await insert_run(c, "succeeded", queued=old, finished=old, attempts=1)
        await c.execute(
            sa.text(
                "INSERT INTO login_attempts (at, ip, username_hmac, success) "
                "VALUES (:t, '198.51.100.1', '\\x00', false)"
            ),
            {"t": now - timedelta(days=31)},
        )
    thread = f"run-{run_id}-a1"
    open_failure = sa.text(
        "SELECT context FROM alerts WHERE dedup_key = 'retention.failed:checkpoints' AND resolved_at IS NULL"
    )

    async def left() -> tuple[int, int, int]:
        async with engine.connect() as c:
            runs = (await c.execute(sa.text("SELECT count(*) FROM runs"))).scalar_one()
            attempts = (await c.execute(sa.text("SELECT count(*) FROM run_attempts"))).scalar_one()
            checkpoints = await c.execute(
                sa.text("SELECT count(*) FROM checkpoints WHERE thread_id = :t"), {"t": thread}
            )
            return runs, attempts, checkpoints.scalar_one()

    async with open_saver(migrated.dsn("app")) as saver:
        await build_noop_collect_graph(saver).ainvoke(
            initial_state(run_id, 1),
            {"configurable": {"thread_id": thread}},
            durability="sync",
        )

        async def unavailable(thread_id: str) -> None:
            raise RuntimeError("checkpoint store unavailable")

        monkeypatch.setattr(saver, "adelete_thread", unavailable)
        first = await run_retention_daily(engine, queue_app, saver, now=now)
        runs, attempts, checkpoints = await left()
        assert (first["checkpoints"], first["runs"], first["login_attempts"]) == (-1, -1, 1)
        assert (runs, attempts) == (1, 1) and checkpoints > 0  # the run and the attempt that names the thread
        async with engine.connect() as c:
            assert (await c.execute(open_failure)).scalar_one() == {
                "rule": "checkpoints",
                "error_class": "RuntimeError",
            }
        assert [(e["rule"], e["blocked_by"]) for e in logged(caplog, "retention_rule_skipped")] == [
            ("runs", ["checkpoints"])
        ]
        monkeypatch.undo()
        second = await run_retention_daily(engine, queue_app, saver, now=now)
    assert (second["checkpoints"], second["runs"]) == (1, 1)
    assert await left() == (0, 0, 0)
    async with engine.connect() as c:
        assert (await c.execute(open_failure)).first() is None  # resolved by the successful pass
