import psycopg
import pytest
from jobhunter.db.sql import LOCK_NOT_AVAILABLE

from tests.support.fixtures import reset_database
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
