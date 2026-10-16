import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from jobhunter.db.alembic_runner import code_head
from jobhunter.db.migrate import MIGRATE_ORDER, apply_explicit_grants, run_migrate, run_migrate_async
from jobhunter.db.seed import load_settings_v1
from jobhunter.db.session import make_engine
from jobhunter.graphs.checkpointer import setup_checkpointer

from tests.support.pg import PgRole, PgServer, pg_one

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 16, 10, 0, tzinfo=UTC)


def one(server: PgServer, statement: str, role: PgRole = "migrator") -> tuple[object, ...] | None:
    return pg_one(server, role, statement)  # shared helper (Task 4)


async def test_first_install_order_and_artifacts(fresh_pg_server, make_config) -> None:
    assert await run_migrate_async(make_config("migrate", fresh_pg_server), now=NOW) == list(MIGRATE_ORDER)
    assert list(MIGRATE_ORDER) == [
        "reconstruct_and_advance",
        "alembic",
        "advance_again",
        "grants",
        "queue_and_checkpointer",
        "seed",
        "replay",
    ]
    assert one(fresh_pg_server, "SELECT version_num FROM alembic_version") == (code_head(),)
    for t in ("procrastinate_jobs", "checkpoints", "checkpoint_writes", "checkpoint_blobs"):
        assert one(fresh_pg_server, f"SELECT to_regclass('{t}') IS NOT NULL") == (True,)
    assert one(fresh_pg_server, "SELECT count(*) FROM procrastinate_jobs", role="app") == (0,)


def test_sync_wrapper_refuses_a_running_loop(make_config, monkeypatch) -> None:
    import jobhunter.db.migrate as migrate_module

    # Nothing listens on port 1: a wrapper that touched the database first would fail with a connection error.
    cfg = make_config("migrate", JOBHUNTER_DB_PORT="1")
    calls: list[object] = []

    def recorder(*args: object, **kwargs: object):
        calls.append((args, kwargs))
        return asyncio.sleep(0)

    monkeypatch.setattr(migrate_module, "run_migrate_async", recorder)

    async def inner() -> None:
        with pytest.raises(RuntimeError, match="running event loop"):
            run_migrate(cfg)

    asyncio.run(inner())
    # `asyncio.run` inside a loop raises a similar error, but only after the coroutine was created: the
    # explicit check must come first, so that no coroutine exists and nothing can start.
    assert calls == []


async def test_seed_values(fresh_pg_server, make_config) -> None:
    await run_migrate_async(make_config("migrate", fresh_pg_server), now=NOW)
    s = load_settings_v1()
    assert one(fresh_pg_server, "SELECT payload FROM settings_versions WHERE version_no = 1") == (s,)
    assert one(fresh_pg_server, "SELECT schema_version, effect_classes FROM settings_versions") == (
        1,
        ["none"],
    )
    assert one(fresh_pg_server, "SELECT payload->'nodes'->'critic'->>'model' FROM model_policies") == (
        "claude-opus-5-5",
    )
    assert one(
        fresh_pg_server, "SELECT version_no, projected_critic_rate::text FROM evaluation_policies"
    ) == (
        1,
        "0.450",
    )
    assert one(
        fresh_pg_server,
        "SELECT a.settings_version_id = s.id, a.scope_version_id = s.scope_version_id, "
        "a.rubric_version_id IS NULL, a.prompt_schema_version_id IS NULL, a.llm_tuple_id IS NULL "
        "FROM active_config a, settings_versions s WHERE s.version_no = 1",
    ) == (True, True, True, True, True)
    assert one(
        fresh_pg_server,
        "SELECT payload ? 'max_ad_age_days', payload->>'remote_within_germany' FROM scope_versions",
    ) == (False, "true")
    assert one(
        fresh_pg_server, "SELECT anchor_utc, interval_s, enabled, last_evaluated_slot FROM schedule_state"
    ) == (
        datetime(2026, 10, 16, 4, tzinfo=UTC),
        21600,
        True,
        datetime(2026, 10, 16, 10, tzinfo=UTC),
    )
    assert one(fresh_pg_server, "SELECT generation FROM ranking_state") == (0,)


async def test_seed_hashes_are_the_content_hash_of_the_stored_payloads(fresh_pg_server, make_config) -> None:
    from jobhunter.db.seed import content_hash

    await run_migrate_async(make_config("migrate", fresh_pg_server), now=NOW)
    for statement in (
        "SELECT payload, scope_hash FROM scope_versions",
        "SELECT payload, policy_hash FROM model_policies",
        "SELECT payload, config_hash FROM execution_configs",
        "SELECT payload, policy_hash FROM evaluation_policies",
    ):
        row = one(fresh_pg_server, statement)
        assert row is not None
        payload, stored_hash = row
        assert isinstance(payload, dict) and stored_hash == content_hash(payload), statement
    # The settings version points at exactly those rows.
    assert one(
        fresh_pg_server,
        "SELECT s.scope_version_id = (SELECT id FROM scope_versions), "
        "s.model_policy_id = (SELECT id FROM model_policies), "
        "s.execution_config_id = (SELECT id FROM execution_configs), "
        "s.evaluation_policy_id = (SELECT id FROM evaluation_policies), "
        "(SELECT settings_version_id FROM schedule_state) = s.id "
        "FROM settings_versions s WHERE s.version_no = 1",
    ) == (True, True, True, True, True)


async def test_migrate_is_idempotent(fresh_pg_server, make_config) -> None:
    cfg = make_config("migrate", fresh_pg_server)
    await run_migrate_async(cfg, now=NOW)
    await run_migrate_async(cfg, now=datetime(2026, 10, 17, tzinfo=UTC))
    assert one(
        fresh_pg_server,
        "SELECT (SELECT count(*) FROM model_policies), (SELECT count(*) FROM settings_versions), "
        "(SELECT count(*) FROM scope_versions), (SELECT count(*) FROM active_config), "
        "(SELECT anchor_utc FROM schedule_state)",
    ) == (1, 1, 1, 1, datetime(2026, 10, 16, 4, tzinfo=UTC))
    # Nothing else was duplicated or moved by the second run (its `now` would have moved the slot).
    assert one(
        fresh_pg_server,
        "SELECT (SELECT count(*) FROM execution_configs), (SELECT count(*) FROM evaluation_policies), "
        "(SELECT count(*) FROM schedule_state), (SELECT count(*) FROM ranking_state), "
        "(SELECT last_evaluated_slot FROM schedule_state)",
    ) == (1, 1, 1, 1, datetime(2026, 10, 16, 10, tzinfo=UTC))


async def test_a_rerun_recreates_missing_pointer_rows_over_the_existing_versions(
    fresh_pg_server, make_config
) -> None:
    cfg = make_config("migrate", fresh_pg_server)
    await run_migrate_async(cfg, now=NOW)
    for statement in ("DELETE FROM active_config", "DELETE FROM schedule_state", "DELETE FROM ranking_state"):
        one(fresh_pg_server, statement)
    await run_migrate_async(cfg, now=datetime(2026, 10, 17, 7, 0, tzinfo=UTC))
    assert one(
        fresh_pg_server,
        "SELECT (SELECT count(*) FROM model_policies), (SELECT count(*) FROM execution_configs), "
        "(SELECT count(*) FROM evaluation_policies), (SELECT count(*) FROM scope_versions), "
        "(SELECT count(*) FROM settings_versions), (SELECT count(*) FROM active_config), "
        "(SELECT count(*) FROM ranking_state)",
    ) == (1, 1, 1, 1, 1, 1, 1)
    assert one(
        fresh_pg_server,
        "SELECT a.settings_version_id = s.id, sc.settings_version_id = s.id, "
        "sc.anchor_utc, sc.last_evaluated_slot "
        "FROM active_config a, schedule_state sc, settings_versions s WHERE s.version_no = 1",
    ) == (True, True, datetime(2026, 10, 17, 4, tzinfo=UTC), datetime(2026, 10, 17, 4, tzinfo=UTC))


# The advisory locks on the migrate key (7261003) that exist on the server.
ADVISORY_LOCKS = "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = 7261003"
COUNTS = (
    "SELECT (SELECT count(*) FROM scope_versions), (SELECT count(*) FROM model_policies), "
    "(SELECT count(*) FROM execution_configs), (SELECT count(*) FROM evaluation_policies), "
    "(SELECT count(*) FROM settings_versions)"
)


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("evaluation", "relevance_threshold", 61),
        ("scope", "work_modes", ["remote"]),
        ("scoring.model_policy", "preset", "C"),
        ("scoring.execution", "generate", {"max_tokens": 1024, "max_tokens_retry": 2048, "deadline_s": 120}),
    ],
)
async def test_stored_settings_v1_is_frozen_when_a_packaged_default_changes(
    migrated, make_config, monkeypatch, section, key, value
) -> None:
    """A release that edits `settings_v1.json` must not break or grow an installed database."""
    import jobhunter.db.seed as seed_module
    from jobhunter.types import get_object

    before = one(migrated, COUNTS)
    changed = load_settings_v1()
    get_object(changed, section)[key] = value
    monkeypatch.setattr(seed_module, "load_settings_v1", lambda: changed)
    await run_migrate_async(make_config("migrate"), now=NOW)  # exits normally
    assert one(migrated, COUNTS) == before  # no new rows, in particular no orphaned derived row


async def test_recreated_pointer_rows_follow_the_stored_v1_not_the_packaged_defaults(
    fresh_pg_server, make_config, monkeypatch
) -> None:
    import jobhunter.db.seed as seed_module
    from jobhunter.types import get_object

    cfg = make_config("migrate", fresh_pg_server)
    # Berlin is already on 17 October at 23:30 UTC on the 16th: the anchor is 06:00 CEST of the 17th, and the
    # latest slot at or before `now` is the one before it.
    await run_migrate_async(cfg, now=datetime(2026, 10, 16, 23, 30, tzinfo=UTC))
    schedule = "SELECT anchor_utc, interval_s, enabled, last_evaluated_slot FROM schedule_state"
    assert one(fresh_pg_server, schedule) == (
        datetime(2026, 10, 17, 4, tzinfo=UTC),
        21600,
        True,
        datetime(2026, 10, 16, 22, tzinfo=UTC),
    )

    for statement in ("DELETE FROM active_config", "DELETE FROM schedule_state", "DELETE FROM ranking_state"):
        one(fresh_pg_server, statement)
    changed = load_settings_v1()
    get_object(changed, "schedule").update({"interval_hours": 12, "anchor_local": "07:00", "enabled": False})
    get_object(changed, "display")["timezone"] = "UTC"
    monkeypatch.setattr(seed_module, "load_settings_v1", lambda: changed)
    await run_migrate_async(cfg, now=datetime(2026, 10, 17, 7, 0, tzinfo=UTC))

    # The schedule comes from the stored v1 payload (Europe/Berlin, 06:00, 6 h, enabled), not from the file.
    assert one(fresh_pg_server, schedule) == (
        datetime(2026, 10, 17, 4, tzinfo=UTC),
        21600,
        True,
        datetime(2026, 10, 17, 4, tzinfo=UTC),
    )
    assert one(fresh_pg_server, COUNTS) == (1, 1, 1, 1, 1)
    assert one(
        fresh_pg_server,
        "SELECT a.settings_version_id = s.id, a.scope_version_id = s.scope_version_id, "
        "a.model_policy_id = s.model_policy_id, a.execution_config_id = s.execution_config_id, "
        "a.evaluation_policy_id = s.evaluation_policy_id, sc.settings_version_id = s.id "
        "FROM active_config a, schedule_state sc, settings_versions s WHERE s.version_no = 1",
    ) == (True, True, True, True, True, True)


async def test_concurrent_migrates_are_serialised_and_the_second_is_a_noop(
    fresh_pg_server, make_config
) -> None:
    cfg = make_config("migrate", fresh_pg_server)
    first, second = await asyncio.gather(run_migrate_async(cfg, now=NOW), run_migrate_async(cfg, now=NOW))
    assert first == second == list(MIGRATE_ORDER)
    assert one(
        fresh_pg_server,
        "SELECT (SELECT count(*) FROM model_policies), (SELECT count(*) FROM settings_versions), "
        "(SELECT count(*) FROM scope_versions)",
    ) == (1, 1, 1)


async def test_migrate_waits_for_the_lock_and_says_so(fresh_pg_server, make_config) -> None:
    import psycopg
    from jobhunter.db.migrate import MIGRATE_LOCK_KEY
    from structlog.testing import capture_logs

    assert MIGRATE_LOCK_KEY == 7261003
    cfg = make_config("migrate", fresh_pg_server)
    holder = await psycopg.AsyncConnection.connect(fresh_pg_server.dsn("migrator"), autocommit=True)
    try:
        await holder.execute("SELECT pg_advisory_lock(%s)", (MIGRATE_LOCK_KEY,))
        with capture_logs() as logs:
            task = asyncio.create_task(run_migrate_async(cfg, now=NOW))
            for _ in range(400):  # readiness, not timing: the run says that it is queued on the lock
                if task.done() or any(e["event"] == "migrate_lock_wait" for e in logs):
                    break
                await asyncio.sleep(0.05)
            assert not task.done()
            assert [e["event"] for e in logs if e["event"] == "migrate_lock_wait"] == ["migrate_lock_wait"]
            assert not any(e["event"] == "migrate_step" for e in logs)  # not a single step has started
            await holder.execute("SELECT pg_advisory_unlock(%s)", (MIGRATE_LOCK_KEY,))
            assert await task == list(MIGRATE_ORDER)
    finally:
        await holder.close()
    # The lock is released when the run ends, so nothing is left held or waiting.
    assert one(fresh_pg_server, ADVISORY_LOCKS) == (0,)


def test_a_failing_migrate_logs_migrate_failed_and_chains_no_loop_error(make_config) -> None:
    import psycopg
    from structlog.testing import capture_logs

    cfg = make_config("migrate", JOBHUNTER_DB_PORT="1")  # nothing listens there
    with capture_logs() as logs, pytest.raises(psycopg.OperationalError) as caught:
        run_migrate(cfg)
    (failed,) = [e for e in logs if e["event"] == "migrate_failed"]
    assert failed["error_type"] == "OperationalError" and failed["step"] == "lock"
    assert set(failed) <= {"event", "log_level", "step", "error_type"}  # no message text, no DSN
    context = caught.value.__context__
    assert not (isinstance(context, RuntimeError) and "no running event loop" in str(context))
    assert cfg.secret("db_migrator").decode() not in repr(logs)


async def test_a_failing_step_logs_its_name_and_class_only_and_stops_the_run(
    migrated, make_config, monkeypatch
) -> None:
    import jobhunter.db.migrate as migrate_module
    from structlog.testing import capture_logs

    def broken(dsn: str) -> None:
        raise ValueError(f"private detail of {dsn}")

    monkeypatch.setattr(migrate_module, "upgrade_head", broken)
    cfg = make_config("migrate")
    with capture_logs() as logs, pytest.raises(ValueError, match="private detail"):
        await run_migrate_async(cfg, now=NOW)
    (failed,) = [e for e in logs if e["event"] == "migrate_failed"]
    assert (failed["step"], failed["error_type"]) == ("alembic", "ValueError")
    assert "private detail" not in repr(logs) and cfg.secret("db_migrator").decode() not in repr(logs)
    # The steps after the failed one did not start.
    assert [e["step"] for e in logs if e["event"] == "migrate_step"] == list(MIGRATE_ORDER[:2])
    assert one(migrated, ADVISORY_LOCKS) == (0,)  # and the lock was released


async def test_steps_are_logged_in_order_and_without_the_dsn(migrated, make_config) -> None:
    from structlog.testing import capture_logs

    cfg = make_config("migrate")
    with capture_logs() as logs:
        assert await run_migrate_async(cfg, now=NOW) == list(MIGRATE_ORDER)
    steps = [entry["step"] for entry in logs if entry["event"] == "migrate_step"]
    assert steps == list(MIGRATE_ORDER)
    noops = {entry["step"]: entry["reason"] for entry in logs if entry["event"] == "migrate_step_noop"}
    assert noops == dict.fromkeys(
        ("reconstruct_and_advance", "advance_again", "replay"), "manifest_stream_arrives_in_M5"
    )
    assert cfg.secret("db_migrator").decode() not in repr(logs)


def test_cli_migrate_installs_and_logs_json_without_the_password(fresh_pg_server, proc_env) -> None:
    done = subprocess.run(
        [sys.executable, "-m", "jobhunter.cli", "migrate"],
        env=proc_env("migrate", fresh_pg_server),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    events = [json.loads(line) for line in done.stdout.splitlines()]
    assert [e["step"] for e in events if e["event"] == "migrate_step"] == list(MIGRATE_ORDER)
    assert fresh_pg_server.passwords["migrator"] not in done.stdout + done.stderr
    assert one(fresh_pg_server, "SELECT version_num FROM alembic_version") == (code_head(),)
    assert one(fresh_pg_server, "SELECT count(*) FROM settings_versions") == (1,)


async def test_grants_step_skips_absent_tables(migrated) -> None:
    from jobhunter.db.session import make_null_engine

    engine = make_null_engine(migrated.dsn("migrator", driver="sqlalchemy"))
    async with engine.begin() as conn:
        assert await apply_explicit_grants(conn) == []
    await engine.dispose()


async def test_grants_step_grants_the_backup_role_on_the_tables_that_exist(
    fresh_pg_server, make_config
) -> None:
    from jobhunter.db.session import make_null_engine

    await run_migrate_async(make_config("migrate", fresh_pg_server), now=NOW)
    one(fresh_pg_server, "CREATE TABLE backup_sets (id int PRIMARY KEY)")
    one(fresh_pg_server, "CREATE TABLE manifest_shipments (id int PRIMARY KEY)")
    backup_privileges = (
        "SELECT has_table_privilege('jobhunter_backup', 'backup_sets', 'INSERT'), "
        "has_table_privilege('jobhunter_backup', 'backup_sets', 'UPDATE'), "
        "has_table_privilege('jobhunter_backup', 'backup_sets', 'DELETE'), "
        "has_table_privilege('jobhunter_backup', 'manifest_shipments', 'INSERT')"
    )
    assert one(fresh_pg_server, backup_privileges) == (False, False, False, False)
    engine = make_null_engine(fresh_pg_server.dsn("migrator", driver="sqlalchemy"))
    try:
        for _ in range(2):  # idempotent
            async with engine.begin() as conn:
                assert await apply_explicit_grants(conn) == ["backup_sets", "manifest_shipments"]
    finally:
        await engine.dispose()
    assert one(fresh_pg_server, backup_privileges) == (True, True, False, True)


async def test_checkpointer_setup_is_idempotent(migrated) -> None:
    await setup_checkpointer(migrated.dsn("migrator"))
    await setup_checkpointer(migrated.dsn("migrator"))


async def test_the_app_role_can_use_the_saver_on_the_migrated_tables(migrated) -> None:
    from jobhunter.graphs.checkpointer import open_saver
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import empty_checkpoint

    config: RunnableConfig = {"configurable": {"thread_id": "t6-saver", "checkpoint_ns": ""}}
    checkpoint = empty_checkpoint()
    async with open_saver(migrated.dsn("app")) as saver:
        stored = await saver.aput(config, checkpoint, {"source": "input", "step": 0, "parents": {}}, {})
        found = await saver.aget_tuple(stored)
    assert found is not None and found.checkpoint["id"] == checkpoint["id"]


async def test_make_engine_connects_as_the_role_of_the_config_with_a_bounded_pool(
    migrated, make_config
) -> None:
    engine = make_engine(make_config("api"), pool_size=2)
    try:
        async with engine.connect() as conn:
            assert (await conn.execute(sa.text("SELECT current_user"))).scalar_one() == "jobhunter_app"
        assert isinstance(engine.pool, sa.pool.QueuePool) and engine.pool.size() == 2
    finally:
        await engine.dispose()
