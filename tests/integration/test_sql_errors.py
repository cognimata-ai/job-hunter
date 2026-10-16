import pytest
import sqlalchemy as sa
from jobhunter.db.session import make_null_engine
from jobhunter.db.sql import UNDEFINED_TABLE, UNIQUE_VIOLATION, PgErrorInfo, is_unique_violation, pg_error

pytestmark = pytest.mark.integration
ALERT = sa.text("INSERT INTO alerts (code,severity,dedup_key,message) VALUES ('a','warn','sql-errors','m')")


async def test_wrapped_errors_are_classified_and_savepoints_keep_the_transaction(migrated) -> None:
    engine = make_null_engine(migrated.dsn("app", driver="sqlalchemy"))
    async with engine.connect() as c:
        with pytest.raises(sa.exc.ProgrammingError) as missing:
            async with c.begin_nested():
                await c.execute(sa.text("SELECT 1 FROM no_such_table"))
        assert pg_error(missing.value).sqlstate == UNDEFINED_TABLE
        await c.execute(ALERT)  # usable: only the savepoint rolled back
        with pytest.raises(sa.exc.IntegrityError) as dup:
            async with c.begin_nested():
                await c.execute(ALERT)
        assert pg_error(dup.value) == PgErrorInfo(UNIQUE_VIOLATION, "alerts_open_dedup")
        assert is_unique_violation(dup.value, "alerts_open_dedup") and not is_unique_violation(
            dup.value, "runs_slot_uq"
        )
        await c.rollback()
    await engine.dispose()
