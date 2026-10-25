import psycopg
import pytest
import sqlalchemy as sa
from jobhunter.db.sql import LOCK_NOT_AVAILABLE

from tests.support.fixtures import reset_database
from tests.support.m2 import (
    digest,
    insert_eval_run,
    insert_llm_call,
    insert_profile_chain,
    insert_proposal,
    insert_vector,
)
from tests.support.pg import MANUAL_RUN_SQL, pg_one, pg_rows

pytestmark = pytest.mark.integration

# The seeded schedule, derived in SQL from the stored settings version 1: its interval, and its anchor on the
# date (in the display zone) on which that version was created.
SEEDED = """\
SELECT ((s.created_at AT TIME ZONE z.tz)::date + (s.payload->'schedule'->>'anchor_local')::time)
         AT TIME ZONE z.tz,
       (s.payload->'schedule'->>'interval_hours')::int * 3600
FROM settings_versions s, LATERAL (SELECT s.payload->'display'->>'timezone' AS tz) z
WHERE s.version_no = 1"""
SCHEDULE = "SELECT anchor_utc, interval_s, enabled FROM schedule_state"
# The slot lies on the schedule, and it is the latest one that is not after the database's now() as the reset
# read it. That instant lies between two instants the test reads before and after the reset, so the slot is
# not after the later one (first parameter) and the slot's successor is after the earlier one (second).
SLOT_IS_LATEST = """\
SELECT last_evaluated_slot <= %s AND last_evaluated_slot + make_interval(secs => interval_s) > %s,
       mod(extract(epoch FROM last_evaluated_slot - anchor_utc)::bigint, interval_s) = 0
FROM schedule_state"""
DIRT = (
    MANUAL_RUN_SQL,
    "INSERT INTO operations (kind,payload,dedup_key,status) VALUES ('export','{}','e','pending')",
    "INSERT INTO alerts (code,severity,dedup_key,message) VALUES ('a','warn','k','m')",
    "INSERT INTO login_attempts (ip, username_hmac, success) VALUES ('127.0.0.1', '\\x00', false)",
)
COUNTS = (
    "SELECT (SELECT count(*) FROM runs), (SELECT count(*) FROM operations), (SELECT count(*) FROM alerts), "
    "(SELECT count(*) FROM login_attempts)"
)


def test_a_test_that_uses_migrated_is_cleaned_by_itself(migrated, request) -> None:
    # `clean_db` is requested by the autouse fixture of tests/integration/conftest.py.
    assert "clean_db" in request.fixturenames


def test_a_test_without_the_database_is_not(request) -> None:
    assert "clean_db" not in request.fixturenames


def test_reset_empties_the_tables_and_restores_the_seeded_schedule(migrated) -> None:
    for statement in DIRT:
        pg_rows(migrated, "migrator", statement)
    assert pg_one(migrated, "migrator", COUNTS) == (1, 1, 1, 1)
    pg_rows(
        migrated,
        "migrator",
        "UPDATE schedule_state SET interval_s = 7200, anchor_utc = anchor_utc + interval '1 hour', "
        "enabled = false, last_evaluated_slot = NULL",
    )

    before = pg_one(migrated, "migrator", "SELECT now()")
    reset_database(migrated)
    after = pg_one(migrated, "migrator", "SELECT now()")

    assert pg_one(migrated, "migrator", COUNTS) == (0, 0, 0, 0)
    seeded = pg_one(migrated, "migrator", SEEDED)
    assert seeded is not None
    assert pg_one(migrated, "migrator", SCHEDULE) == (*seeded, True)
    assert before is not None and after is not None
    assert pg_one(migrated, "migrator", SLOT_IS_LATEST, after[0], before[0]) == (True, True)


def test_a_leaked_lock_fails_the_reset_instead_of_hanging_it(migrated) -> None:
    with psycopg.connect(migrated.dsn("migrator")) as leaked:
        leaked.execute("LOCK TABLE alerts IN ACCESS SHARE MODE")  # held until this transaction ends
        with pytest.raises(psycopg.Error) as blocked:
            reset_database(
                migrated, lock_timeout="50ms"
            )  # short only to fail fast; the lock is never released
        assert blocked.value.sqlstate == LOCK_NOT_AVAILABLE
    reset_database(migrated)  # the lock is gone: the reset works again


# Every column of the one `active_config` row.
ACTIVE_CONFIG = (
    "SELECT id, settings_version_id, scope_version_id, pending_scope_version_id, profile_version_id, "
    "rubric_version_id, prompt_schema_version_id, model_policy_id, execution_config_id, "
    "evaluation_policy_id, llm_tuple_id FROM active_config"
)
M2_COUNTS = (
    "SELECT (SELECT count(*) FROM cv_files), (SELECT count(*) FROM profile_drafts), "
    "(SELECT count(*) FROM profile_versions), (SELECT count(*) FROM llm_tuples), "
    "(SELECT count(*) FROM query_proposals), (SELECT count(*) FROM profile_vectors), "
    "(SELECT count(*) FROM llm_budget), (SELECT count(*) FROM llm_spend_ledger), "
    "(SELECT count(*) FROM llm_calls), (SELECT count(*) FROM eval_runs)"
)
TEST_ROWS = (
    "SELECT (SELECT count(*) FROM rubric_versions WHERE content_hash = %s), "
    "(SELECT count(*) FROM prompt_schema_versions WHERE prompt_schema_hash = %s)"
)


async def test_two_resets_keep_active_config(engine, migrated) -> None:
    """`TRUNCATE ... CASCADE` of a table `active_config` reaches through its foreign keys would empty
    `active_config` itself: the M2 tables are deleted instead, and the pointers are restored first."""
    seeded = pg_one(migrated, "migrator", ACTIVE_CONFIG)
    assert seeded is not None and pg_one(migrated, "migrator", M2_COUNTS) == (0,) * 10
    async with engine.begin() as c:
        chain = await insert_profile_chain(c)
        eval_run = await insert_eval_run(c)
        _, call = await insert_llm_call(c, draft_id=chain.draft_id, eval_run_id=eval_run)
        proposal = await insert_proposal(c, chain.profile_version_id)
        await insert_vector(c, chain.profile_version_id)
        await c.execute(sa.text("UPDATE profile_drafts SET llm_call_id = :c"), {"c": call})
        await c.execute(
            sa.text("UPDATE query_proposals SET llm_call_id = :c WHERE id = :p"), {"c": call, "p": proposal}
        )
        await c.execute(
            sa.text(
                "UPDATE active_config SET profile_version_id = :p, llm_tuple_id = :t, "
                "rubric_version_id = :r, prompt_schema_version_id = :s"
            ),
            {
                "p": chain.profile_version_id,
                "t": chain.llm_tuple_id,
                "r": chain.rubric_version_id,
                "s": chain.prompt_schema_version_id,
            },
        )
    assert pg_one(migrated, "migrator", M2_COUNTS) == (1,) * 10
    assert pg_one(migrated, "migrator", ACTIVE_CONFIG) != seeded

    for _ in range(2):  # the second reset meets the state the first one left
        reset_database(migrated)
        assert pg_rows(migrated, "migrator", ACTIVE_CONFIG) == [seeded]
        assert pg_one(migrated, "migrator", M2_COUNTS) == (0,) * 10
        assert pg_one(migrated, "migrator", TEST_ROWS, digest(1), digest(1)) == (0, 0)
